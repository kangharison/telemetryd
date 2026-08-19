"""실제 라이브 커널을 drgn으로 조회하는 백엔드.

deep/scripts/drgn/00~04번 스크립트에서 검증된 경로를 그대로 라이브러리화한
것이다 — gendisk -> nvme_ns -> nvme_ctrl -> container_of(nvme_dev), SQ/CQ
필드, request_queue_busy_iter를 이용한 hctx별 inflight, PRP 디코딩.

⚠️ 이 백엔드는 root 권한(/proc/kcore)이 필요하고, 이 세션(Claude)은 sudo가
비밀번호를 요구해 non-interactive로 실행할 수 없다(DESIGN.md §0). 그래서
이 파일은 기존 검증된 스크립트의 패턴을 최대한 그대로 재사용했지만,
**실제 라이브 커널에 대해 이 파일 자체를 실행해 검증하지는 못했다** — 신규
로직(트리 워커 연결, cdw2/cdw3 필드명 fallback, PRP total_len 추정)은
사용자가 `sudo -E`로 직접 검증해야 한다(§8, README 실행법 참조).
"""
from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional, Tuple

from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.models import CompletionEntry, DeviceSnapshot, PrpPage, PrpPayload, QueueEntry, QueueSnapshot
from telemetryd.nvme_const import MAX_PAGE_DUMP, PAGE_MASK, PAGE_SIZE, PRPS_PER_PAGE, opcode_name
from telemetryd.platform.ebpf import as_log_source
from telemetryd.platform.kernel import DrgnKernelSession
from telemetryd.services.events import EbpfEventService
from telemetryd.services.perf import EbpfPerfService
from telemetryd.services.profiler import NvmeProfilerService
from telemetryd.services.queues import DrgnQueueService
from telemetryd.services.topology import DrgnTopologyService
from telemetryd import treewalk


class DrgnBackend:
    kind = "drgn"

    def __init__(
        self, program=None, qemu_qmp_address=None, qemu_vmlinux=None,
        extra_symbols=None, ebpf_log_path=None, target_state=None,
    ):
        """@program: 이미 구성된 drgn.Program을 직접 주입(테스트/재사용용).
        @qemu_qmp_address: 주어지면 호스트 대신 이 주소의 QEMU 게스트에 QMP로 라이브
          접속한다 — root 권한이 필요 없다. **반드시 유닉스 소켓 경로**여야 한다
          (TCP 불가): drgn이 vmcoreinfo를 얻으려고 QMP의 dump-guest-memory를
          쓰는데, 그 결과 fd를 SCM_RIGHTS로 넘겨받으므로 유닉스 도메인 소켓
          연결에서만 동작한다. 게스트 쪽에는 QEMU를 `-device vmcoreinfo`로
          띄우고, 커널은 CONFIG_FW_CFG_SYSFS=y + CONFIG_KEXEC=y로 빌드해야
          drgn이 vmcoreinfo를 찾을 수 있다(DESIGN.md §9, libdrgn/program.c의
          "run QEMU with '-device vmcoreinfo'..." 에러 메시지가 근거).
        @qemu_vmlinux: qemu_qmp_address 사용 시, 그 게스트가 부팅한 커널과
          **정확히 같은 빌드**의 vmlinux 경로(빌드가 다르면 build-id 불일치로
          "did not match any loaded modules"가 나며 조용히 무시된다). 내부적으론
          extra_symbols와 합쳐져 load_debug_info()에 전달된다.
        @extra_symbols: 로컬(program_from_kernel()) 모드에서도 비표준 경로의
          vmlinux를 명시적으로 쓰고 싶을 때(예: QEMU 게스트를 9p로 호스트 rootfs를
          마운트해 chroot 후 이 라이브러리를 그대로 실행하는 경우 — 그 게스트
          로컬 커널엔 debuginfod/표준 dbgsym 경로가 없어 program_from_kernel()
          만으로는 심볼을 못 찾는다). 경로 리스트.
        @ebpf_log_path: ebpf/nvme_perf.bt(bpftrace)가 계속 append하는 로그
          파일 경로. get_performance()가 이 파일을 읽는다 — drgn과 무관한
          순수 파일 I/O(DESIGN.md §6/§9.5). 수집기는 게스트 안에서
          `chroot /mnt/host bpftrace nvme_perf.bt >> <이 경로> 2>&1 &`로
          별도 실행해야 하며, 이 경로는 host에서 그 출력이 실제로 쌓이는
          위치(예: QEMU 쓰기 가능 9p 공유의 host 쪽 마운트포인트)여야 한다."""
        # [한국어] 커널 접속 방식(QMP/로컬/추가 심볼) 판단과 Program 생명주기,
        # 그리고 비싼 조회 캐시는 전부 플랫폼(platform.kernel)이 책임진다 —
        # 이 백엔드는 "무엇을 읽을지"(도메인)만 안다. eBPF 로그 접근도 마찬가지로
        # platform.ebpf의 EbpfLogSource로 감싼다.
        self._kernel = DrgnKernelSession(
            program=program,
            qemu_qmp_address=qemu_qmp_address,
            qemu_vmlinux=qemu_vmlinux,
            extra_symbols=extra_symbols,
        )
        self._ebpf = as_log_source(ebpf_log_path)
        # [한국어] 하위호환: 기존 코드/테스트가 참조하던 경로 속성은 남겨둔다.
        self._ebpf_log_path = ebpf_log_path
        # [한국어] 도메인별 서비스(services/*)로 옮겨간 것들. 이 백엔드는 이제
        # 그 서비스들 앞의 파사드 역할을 하며, 옮겨간 메서드는 위임만 한다.
        self._queues = DrgnQueueService(self._kernel)
        self._perf = EbpfPerfService(self._ebpf)
        self._events = EbpfEventService(self._ebpf)
        self._profiler = NvmeProfilerService(
            kernel=self._kernel, log_source=self._ebpf, target_state=target_state)
        self._topology_svc = DrgnTopologyService(
            list_devices=self.list_devices,
            lookup_device=self._get_dev_and_disk,
        )

    def _ensure_program(self):
        """drgn.Program — 접속 방식 판단/생성은 플랫폼 세션이 한다."""
        return self._kernel.program()

    # ---- 디바이스 탐색 (02_nvme_queues.py 재사용) --------------------------

    # ---- 큐/장치: services/queues 로 위임(Facade) ------------------------

    def _get_dev_and_disk(self, device: str):
        """토폴로지 서비스가 콜러블로 주입받는 커널 객체 조회."""
        return self._queues.lookup_device(device)

    def list_devices(self) -> List[str]:
        return self._queues.list_devices()

    def get_device_snapshot(self, device: str) -> DeviceSnapshot:
        return self._queues.get_device_snapshot(device)

    def get_queue_entries(self, device: str, qid: int, limit: int = 16,
                          around_doorbell: bool = True) -> List[QueueEntry]:
        return self._queues.get_queue_entries(device, qid, limit, around_doorbell)

    def get_completion_entries(self, device: str, qid: int, limit: int = 16,
                               around_doorbell: bool = True) -> List[CompletionEntry]:
        return self._queues.get_completion_entries(device, qid, limit, around_doorbell)

    def get_prp_payload(self, device: str, qid: int, cid: int) -> PrpPayload:
        return self._queues.get_prp_payload(device, qid, cid)

    def get_tree_node(self, device: str, path: List[str]):
        return self._queues.get_tree_node(device, path)

    def get_performance(self, device: str):
        """성능 서비스로 위임(Facade).

        이 백엔드는 리팩토링 동안 기존 호출부(gRPC 서버/CLI/웹)를 안 깨뜨리려고
        남겨둔 **파사드**다 — 실제 로직은 services/perf 로 옮겨졌다. 서비스가
        전부 이사하면 이 클래스는 얇은 위임만 남고, 그때 호출부를 서비스에
        직접 붙이면서 걷어낼 수 있다(services/__init__.py 의 규칙 참고)."""
        return self._perf.get_performance(device)

    def get_events(self, device: str):
        """이벤트 서비스로 위임(Facade) — 로직은 services/events 에 있다."""
        return self._events.get_events(device)

    def get_error_stats(self, device: str):
        """이벤트 서비스로 위임(Facade)."""
        return self._events.get_error_stats(device)

    def get_topology(self):
        """토폴로지 서비스로 위임(Facade) — 로직은 services/topology 에 있다."""
        return self._topology_svc.get_topology()

    # ---- NVMe I/O 프로세스 프로파일러 ------------------------------------

    def list_processes(self, only_io: bool = False):
        """프로파일러 서비스로 위임(Facade)."""
        return self._profiler.list_processes(only_io)

    def list_targets(self):
        """프로파일러 서비스로 위임(Facade)."""
        return self._profiler.list_targets()

    def add_target(self, rule):
        """프로파일러 서비스로 위임(Facade)."""
        return self._profiler.add_target(rule)

    def remove_target(self, kind: str, value: str):
        """프로파일러 서비스로 위임(Facade)."""
        return self._profiler.remove_target(kind, value)

    def get_profile(self):
        """프로파일러 서비스로 위임(Facade)."""
        return self._profiler.get_profile()

    def list_event_kinds(self):
        """이벤트 서비스로 위임(Facade)."""
        return self._events.list_event_kinds()

def doctor(backend: "DrgnBackend | None" = None) -> dict:
    """00_env_check.py를 구조화한 버전 — `telemetryd doctor` CLI 커맨드가 쓴다.
    root가 아니거나 DWARF 심볼이 없으면 여기서 바로 원인이 드러난다.

    @backend: CLI가 --qemu-qmp로 구성한 DrgnBackend를 넘기면 그 접속 방식
      (host root 또는 QEMU QMP)을 그대로 재사용한다. None이면 기본(host
      program_from_kernel())으로 새로 만든다 — 예전엔 이 매개변수가 없어서
      doctor가 --qemu-qmp를 무시하고 항상 호스트 /proc/kcore를 시도하는
      버그가 있었다(실제 QEMU 게스트로 검증하다가 발견)."""
    checks = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        from drgn import FaultError
    except ImportError as e:
        add("drgn import", False, str(e))
        return {"ok": False, "checks": checks}
    add("drgn import", True)

    if backend is None:
        backend = DrgnBackend()
    try:
        prog = backend._ensure_program()
    except PermissionError as e:
        add("프로그램 연결 — /proc/kcore 접근(root 필요)", False, str(e))
        return {"ok": False, "checks": checks}
    except Exception as e:
        add("프로그램 연결", False, str(e))
        return {"ok": False, "checks": checks}
    add("프로그램 연결 (host root 또는 --qemu-qmp)", True)

    try:
        uts = prog["init_uts_ns"].name
        release = uts.release.string_().decode()
        add("init_uts_ns 읽기 (전역심볼+타입+메모리 3박자)", True, f"release={release}")
    except (KeyError, FaultError) as e:
        add("init_uts_ns 읽기", False, str(e))

    try:
        t = prog.type("struct nvme_dev")
        add("struct nvme_dev 타입 해석 (모듈 DWARF)", True, f"size={t.size}B")
    except LookupError as e:
        add("struct nvme_dev 타입 해석 — nvme 모듈 미로드 또는 dbgsym 없음", False, str(e))

    add("IOMMU", True, "enabled" if _iommu_enabled() else "disabled (PRP=물리주소 직접 읽기 가능)")

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
