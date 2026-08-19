"""프로파일러 서비스 구현 — drgn(프로세스 정보) + eBPF(I/O 활동) 둘 다 쓴다.

perf/events(eBPF 전용)나 topology(drgn 전용)와 달리 **두 축이 모두 필요한**
유일한 서비스다. 두 축이 만나야 "지금 이 SSD를 때리는 프로세스"를 이름 없이도
고를 수 있기 때문(§9.15 명세 1-1 (d)).

  drgn  -> task_struct 순회로 pid/comm/cmdline/exe/uid/threads (procinfo)
  eBPF  -> 프로세스별 IOPS/대역폭/지연/큐 사용 (proc_stats)
  파일  -> 대상 규칙/세션 영속화 (targets) — 데몬과 CLI가 같은 파일을 공유

## 비싼 조회 보호

`list_processes`는 프로세스마다 페이지테이블을 걸어 cmdline을 읽어서 실측
60~90초가 걸린다. 이걸 자동 폴링하면 단일 워커 executor가 포화돼 대시보드
전체가 멈춘다(§9.16에서 실제로 겪음). 그래서 커널 조회 부분은 **플랫폼의
세션 공용 캐시**를 통해 나간다 — 캐시를 서비스가 아니라 세션(공통 지점)에
두는 이유는, 같은 조회를 부르는 경로가 둘(list_processes / get_profile 2초
스트림)이라 한쪽에만 달면 다른 쪽이 그대로 우회하기 때문이다.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from telemetryd.backend.proc_stats import read_process_stats
from telemetryd.backend.procinfo import list_processes as _kernel_list_processes
from telemetryd.backend.targets import TargetRegistry, rule_matches
from telemetryd.models import (
    ProcessInfo,
    ProcessListEntry,
    ProfileSnapshot,
    TargetRule,
)
from telemetryd.platform.ebpf import EbpfLogSource
from telemetryd.platform.kernel import KernelSession

#: 세션 공용 캐시에서 이 조회를 가리키는 키. 다른 서비스가 같은 커널 조회를
#: 쓰게 되면 같은 키를 써야 캐시를 공유한다.
PROC_INFO_CACHE_KEY = "procinfo.list_processes"


class NvmeProfilerService:
    """@kernel: 커널 세션(프로세스 정보 조회 + 공용 캐시).
    @log_source: eBPF 수집기 출력(프로세스별 I/O 활동).
    @target_state: 대상 규칙/세션을 영속화할 경로(None이면 기본 XDG state)."""

    def __init__(self, kernel: KernelSession, log_source: EbpfLogSource,
                 target_state: Optional[str] = None):
        self._kernel = kernel
        self._log = log_source
        self._target_state = target_state
        self._targets = None

    # ---- 내부 헬퍼 ---------------------------------------------------------

    def _registry(self) -> TargetRegistry:
        """대상 규칙/세션 저장소(지연 생성) — 파일 I/O라 커널과 무관."""
        if self._targets is None:
            self._targets = TargetRegistry(self._target_state)
        return self._targets

    def _process_stats(self):
        """eBPF 탐색 모드 결과. 수집기가 없으면 빈 목록(에러 아님)."""
        if not self._log.available:
            return []
        return read_process_stats(self._log)

    def _proc_infos(self):
        """비싼 커널 프로세스 조회 — 세션 공용 캐시를 통해 나간다(모듈 docstring)."""
        return self._kernel.cached(
            PROC_INFO_CACHE_KEY,
            lambda: _kernel_list_processes(self._kernel.program()),
        )

    # ---- 계약 구현 ---------------------------------------------------------

    def list_processes(self, only_io: bool = False):
        """게스트(또는 호스트) 커널의 프로세스 목록 + 관측된 I/O 활동을 합친다.

        프로세스 정보는 drgn으로 task_struct에서 읽고(procinfo.py), I/O 활동은
        eBPF 수집기 로그에서 읽는다 — 두 축이 만나야 "지금 이 SSD를 때리는
        프로세스"를 이름 없이도 고를 수 있다(명세 1-1 (d)).

        비싼 부분(프로세스 정보 조회)은 _proc_infos()가 캐시한다 — 그쪽
        docstring 참고. 여기서 하는 합치기/필터링은 값싸므로 매번 새로 한다
        (I/O 활동은 eBPF 로그 읽기라 저렴해서 항상 최신값이 반영된다)."""
        stats = self._process_stats()
        io_by_pid = {}
        for st in stats:
            e = io_by_pid.setdefault(st.pid, {"rate": 0.0, "devices": set(), "comm": st.comm})
            e["rate"] += st.iops
            e["devices"].add(st.device)

        procs = self._proc_infos()
        rules = self._registry().rules
        entries = []
        seen = set()
        for proc in procs:
            seen.add(proc.pid)
            io = io_by_pid.get(proc.pid)
            matched = next((r for r in rules if rule_matches(r, proc)), None)
            entry = ProcessListEntry(
                info=proc,
                io_active=bool(io and io["rate"] > 0),
                io_rate=io["rate"] if io else 0.0,
                target_devices=sorted(io["devices"]) if io else [],
                is_target=matched is not None,
                matched_rule=f"{matched.kind}={matched.value}" if matched else None,
            )
            if proc.error and "커널 스레드" in proc.error:
                # [한국어] 커널 스레드는 대상으로 지정해도 의미가 없다(cmdline도
                # 없고 워크로드 개념이 없음) — 목록에는 두되 선택 불가로.
                entry.selectable = False
                entry.unselectable_reason = "커널 스레드 — 프로파일 대상이 아님"
            entries.append(entry)

        # [한국어] 프로세스 목록에는 없는데 I/O는 잡힌 경우(순회 직후 종료 등)도
        # 버리지 않고 최소 정보로 올린다 — 미관측 I/O 판단에 필요하다.
        for pid, io in io_by_pid.items():
            if pid in seen:
                continue
            entries.append(ProcessListEntry(
                info=ProcessInfo(pid=pid, comm=io["comm"],
                                 error="프로세스 목록에서 사라짐(종료 중일 수 있음)"),
                io_active=io["rate"] > 0, io_rate=io["rate"],
                target_devices=sorted(io["devices"]),
                selectable=False, unselectable_reason="이미 종료된 것으로 보임"))

        if only_io:
            entries = [e for e in entries if e.io_active]
        entries.sort(key=lambda e: (-e.io_rate, e.info.pid))
        return entries

    def list_targets(self):
        return list(self._registry().rules)

    def add_target(self, rule):
        self._registry().add_rule(rule)
        return list(self._registry().rules)

    def remove_target(self, kind: str, value: str):
        self._registry().remove_rule(kind, value)
        return list(self._registry().rules)

    def get_profile(self):
        """세션/논리 그룹/기대 대조/미관측 I/O를 한 번에 만든 스냅샷.

        StreamProfile이 2초 간격으로 부르므로 프로세스 정보는 반드시
        _proc_infos()(캐시됨)로 얻는다 — 예전엔 여기서 procinfo.list_processes()를
        직접 불러 2초마다 60~90초짜리 조회를 태우고 있었다(§9.16)."""
        stats = self._process_stats()
        if not self._log.available:
            snap = self._registry().refresh(self._proc_infos(), [])
            snap.available = False
            snap.error = ("eBPF 수집기 없음 — 프로세스별 I/O 측정값이 비어 있다. "
                          "세션 자체는 만들어지므로 대상 선택은 계속 쓸 수 있다")
            return snap
        return self._registry().refresh(self._proc_infos(), stats)
