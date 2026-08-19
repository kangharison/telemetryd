"""대상 선택 + 세션 관리(명세 PART 1 / PART 4).

=== 이 파일이 하는 일 ===
"무엇을 관측할 것인가"를 런타임에 정하는 계층이다. 명세의 네 가지 선택 방식
(pid / name / name_pattern / cmdline_pattern)은 전부 TargetRule 목록 하나로
수렴하고, 프로세스 목록과 대조해 매 갱신마다 대상 PID 집합을 다시 계산한다 —
그래서 데몬이 먼저 떠 있다가 대상이 나중에 실행돼도 자동으로 붙고(명세 1-4),
대상이 죽어도 규칙은 남아 다음 실행에 다시 붙는다.

=== 왜 세션인가 ===
대상이 런타임에 바뀌므로 데이터 경계가 필요하다. 세션 = 대상 프로세스 하나의
관측 생애. PID 재사용에 대비해 (pid, start_time_ns)로 식별하며, 프로세스가
끝나도 세션은 finished 상태로 남아 "이 결과는 어떤 조건이었나"의 근거(cmdline
전체)를 보존한다.

=== eBPF 필터와의 관계 ===
명세 5-1은 커널 쪽 target_pids 맵으로 필터링하라고 하지만, 이 프로젝트의
수집기는 bpftrace라 유저스페이스에서 맵을 갱신할 수 없다. 그래서 커널은 전
프로세스를 세고(탐색 모드), **필터링은 여기서** 한다. 관측 결과는 동일하고,
오히려 "관측 대상이 아닌 프로세스가 같은 장치를 때리고 있다"(명세 2-2)를
공짜로 알 수 있다. 커널 쪽 필터가 필요해지면 libbpf 기반 수집기로 바꾸면 되고,
그때도 이 계층의 인터페이스는 그대로다.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from telemetryd.backend.adapters import select_adapter
from telemetryd.models import (
    DeviceAttribution,
    LogicalGroup,
    MeasuredWorkload,
    ProcessInfo,
    ProcessIoStat,
    ProfileSnapshot,
    Session,
    TargetRule,
    UnmonitoredIo,
)

VALID_KINDS = ("pid", "name", "name_pattern", "cmdline_pattern")


def default_state_path() -> str:
    """규칙/세션을 저장할 기본 경로. 데몬과 CLI가 같은 파일을 보게 해서, CLI로
    대상을 추가하고 웹에서 결과를 보는 흐름이 자연스럽게 되도록 한다."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "telemetryd", "targets.json")


def rule_matches(rule: TargetRule, proc: ProcessInfo) -> bool:
    """규칙 1개가 프로세스에 붙는가.

    - pid: 정확 일치(가장 명시적)
    - name: comm 또는 실행파일 basename 일치. comm은 15자로 잘리므로 exe도 본다
    - name_pattern / cmdline_pattern: 정규식. 잘못된 정규식은 "안 맞음"으로 처리
      (예외를 밖으로 던져 전체 갱신을 멈추지 않는다 — 명세 7-2)
    """
    try:
        if rule.kind == "pid":
            return str(proc.pid) == str(rule.value).strip()
        if rule.kind == "name":
            want = rule.value.strip()
            return proc.comm == want or os.path.basename(proc.exe_path or "") == want
        if rule.kind == "name_pattern":
            rx = re.compile(rule.value)
            return bool(rx.search(proc.comm or "") or
                        rx.search(os.path.basename(proc.exe_path or "")))
        if rule.kind == "cmdline_pattern":
            return bool(re.search(rule.value, proc.cmdline or ""))
    except re.error:
        return False
    return False


def _session_id(pid: int, start_ns: int, taken: Optional[set] = None) -> str:
    """명세 4-1 형식: sess_<날짜>_<시각>_<pid>.

    ⚠ 같은 PID가 **같은 초 안에** 재사용되면 이 형식만으로는 id가 겹친다(테스트로
    잡힌 실제 버그 — 겹치면 새 세션이 옛 세션을 덮어써서 서로 다른 프로세스의
    데이터가 한 세션에 섞인다). 그래서 충돌할 때만 프로세스 시작 시각에서 뽑은
    짧은 접미사를 붙인다 — 평상시 id는 명세 형식 그대로 유지된다."""
    base = f"sess_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}_{pid}"
    if taken is None or base not in taken:
        return base
    candidate = f"{base}_{start_ns & 0xFFFF:04x}"
    n = 2
    while candidate in taken:
        candidate = f"{base}_{start_ns & 0xFFFF:04x}_{n}"
        n += 1
    return candidate


class TargetRegistry:
    """규칙 + 세션 상태를 들고 있는 저장소. 파일에 영속화한다."""

    def __init__(self, state_path: Optional[str] = None, max_finished: int = 50):
        self._path = state_path if state_path is not None else default_state_path()
        self._max_finished = max_finished
        self.rules: List[TargetRule] = []
        self.sessions: Dict[str, Session] = {}
        # [한국어] (pid, start_time_ns) -> session_id. PID 재사용 구분의 핵심 —
        # 같은 PID라도 프로세스 시작 시각이 다르면 다른 세션이다(명세 1-4/4-2).
        self._by_identity: Dict[Tuple[int, int], str] = {}
        self._load()

    # ---- 영속화 ---------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for r in data.get("rules", []):
            if r.get("kind") in VALID_KINDS:
                self.rules.append(TargetRule(kind=r["kind"], value=r["value"],
                                             adapter=r.get("adapter")))
        for s in data.get("sessions", []):
            try:
                sess = Session(**{k: v for k, v in s.items()
                                  if k in Session.__dataclass_fields__})
                # [한국어] 되살린 세션은 일단 finished로 둔다 — 데몬이 꺼져 있던
                # 동안은 실제로 관측하지 않았으므로 "계속 관측 중이었던 척"하면
                # 안 된다. 다만 **같은 프로세스가 아직 살아 있으면**(pid +
                # start_time이 동일) 다음 refresh에서 이 세션을 다시 active로
                # 이어붙인다(아래 _by_identity 복원) — 안 그러면 데몬을 재시작할
                # 때마다 같은 프로세스에 세션이 하나씩 더 생겨 화면이 중복된다
                # (실측으로 발견: 프로세스 4개가 카드 8개로 보였다).
                sess.status = "finished"
                sess.logical_groups = []
                self.sessions[sess.session_id] = sess
                self._by_identity[(sess.pid, sess.start_time_ns)] = sess.session_id
            except TypeError:
                continue

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            payload = {
                "rules": [asdict(r) for r in self.rules],
                # [한국어] 시계열은 저장하지 않는다 — 세션 메타데이터(특히
                # cmdline)만 남겨 "이 결과는 어떤 조건이었나"에 답할 수 있게 한다.
                "sessions": [
                    {k: v for k, v in asdict(s).items()
                     if k not in ("logical_groups", "aggregate")}
                    for s in list(self.sessions.values())[-self._max_finished:]
                ],
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except OSError:
            pass    # 저장 실패가 관측을 막으면 안 된다

    # ---- 규칙 -----------------------------------------------------------
    def add_rule(self, rule: TargetRule) -> None:
        if rule.kind not in VALID_KINDS:
            raise ValueError(f"알 수 없는 규칙 종류: {rule.kind!r} (가능: {VALID_KINDS})")
        for r in self.rules:
            if r.kind == rule.kind and r.value == rule.value:
                r.adapter = rule.adapter
                self._save()
                return
        self.rules.append(rule)
        self._save()

    def remove_rule(self, kind: str, value: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if not (r.kind == kind and r.value == value)]
        changed = len(self.rules) != before
        if changed:
            self._save()
        return changed

    def clear_rules(self) -> None:
        self.rules = []
        self._save()

    # ---- 대상 해석 ------------------------------------------------------
    def resolve(self, processes: List[ProcessInfo]) -> Dict[int, TargetRule]:
        """지금 살아있는 프로세스 중 규칙에 맞는 것 -> {pid: 매칭된 규칙}."""
        hits: Dict[int, TargetRule] = {}
        for proc in processes:
            for rule in self.rules:
                if rule_matches(rule, proc):
                    hits[proc.pid] = rule
                    break       # 먼저 맞은 규칙을 채택(설명 가능성을 위해 하나만)
        return hits

    # ---- 세션 -----------------------------------------------------------
    def refresh(self, processes: List[ProcessInfo],
                stats: List[ProcessIoStat]) -> ProfileSnapshot:
        """프로세스 목록 + eBPF 통계 -> 프로파일 스냅샷(명세 PART 6 schema 2.0).

        여기서 (a) 규칙 해석, (b) 세션 생성/종료, (c) 어댑터 적용, (d) 장치
        귀속과 미관측 I/O 계산을 한 번에 한다."""
        now_ns = time.time_ns()
        by_pid = {p.pid: p for p in processes}
        matched = self.resolve(processes)

        # 통계를 pid별로 묶는다(한 프로세스가 여러 장치를 쓰면 여러 항목).
        stats_by_pid: Dict[int, List[ProcessIoStat]] = {}
        for st in stats:
            stats_by_pid.setdefault(st.pid, []).append(st)

        active_ids = set()
        for pid, rule in sorted(matched.items()):
            proc = by_pid[pid]
            identity = (pid, proc.start_time_ns)
            sid = self._by_identity.get(identity)
            if sid is None or sid not in self.sessions:
                # [한국어] 같은 PID의 예전 세션이 아직 active로 남아 있으면(PID
                # 재사용) 먼저 닫는다 — 서로 다른 프로세스의 데이터가 한 세션에
                # 섞이는 게 이 구조에서 가장 위험한 오류다.
                for old_id, old in self.sessions.items():
                    if old.pid == pid and old.status == "active" and old.start_time_ns != proc.start_time_ns:
                        old.status = "finished"
                        old.session_end_ns = now_ns
                sid = _session_id(pid, proc.start_time_ns, set(self.sessions))
                adapter = select_adapter(proc, rule.adapter)
                self.sessions[sid] = Session(
                    session_id=sid, pid=pid, comm=proc.comm, cmdline=proc.cmdline,
                    exe_path=proc.exe_path, adapter=adapter.name, status="active",
                    start_time_ns=proc.start_time_ns, session_start_ns=now_ns,
                    matched_rule=f"{rule.kind}={rule.value}",
                )
                self._by_identity[identity] = sid
                self._save()

            sess = self.sessions[sid]
            if sess.status == "finished":
                # [한국어] 재기동 후 같은 프로세스를 다시 만난 경우 — 세션을
                # 이어서 쓴다. 관측이 끊겼던 구간이 있다는 사실은 남겨 둔다.
                sess.session_end_ns = None
            sess.status = "active"
            sess.comm = proc.comm or sess.comm
            sess.thread_count_active = proc.thread_count
            my_stats = stats_by_pid.get(pid, [])
            sess.devices = sorted({s.device for s in my_stats})
            adapter = select_adapter(proc, rule.adapter)
            sess.adapter = adapter.name
            try:
                sess.logical_groups = adapter.get_logical_groups(proc, my_stats)
            except Exception as e:
                # [한국어] 어댑터가 터져도 세션 자체는 살린다 — 실측 값은 이미
                # 있으므로 그룹핑만 포기하고 사유를 남긴다(명세 7-2).
                sess.logical_groups = [LogicalGroup(
                    name=proc.comm or f"pid{pid}", type="process", source="fallback",
                    thread_tids=[t for t, _ in (proc.threads or [])],
                    mismatch_reasons=[f"어댑터 오류로 그룹핑 생략: {e}"], inferred=True)]
            from telemetryd.backend.adapters import measured_from_stats
            sess.aggregate = measured_from_stats(my_stats)
            active_ids.add(sid)

        # 사라진 대상 -> finished (데이터는 보존)
        for sid, sess in self.sessions.items():
            if sess.status == "active" and sid not in active_ids:
                sess.status = "finished"
                sess.session_end_ns = now_ns
                self._save()

        # 미관측 I/O + 장치 귀속
        session_pids = {s.pid for s in self.sessions.values() if s.status == "active"}
        unmonitored: List[UnmonitoredIo] = []
        devices: Dict[str, DeviceAttribution] = {}
        for st in stats:
            dev = devices.setdefault(st.device, DeviceAttribution(name=st.device))
            dev.total_iops += st.iops
            if st.pid in session_pids:
                dev.attributed_iops += st.iops
                for s in self.sessions.values():
                    if s.pid == st.pid and s.status == "active" and s.session_id not in dev.contributing_sessions:
                        dev.contributing_sessions.append(s.session_id)
            else:
                dev.unattributed_iops += st.iops
                if st.iops > 0:
                    unmonitored.append(UnmonitoredIo(
                        pid=st.pid, comm=st.comm, device=st.device, io_rate=st.iops))
        for dev in devices.values():
            # [한국어] 한 장치에 여러 프로세스가 I/O를 보내면 성능 수치를 특정
            # 프로세스에 귀속시킬 수 없다 — 그 사실을 경고로 올린다(명세 2-2).
            procs_on_dev = {st.pid for st in stats if st.device == dev.name and st.iops > 0}
            dev.multi_process_warning = len(procs_on_dev) > 1

        return ProfileSnapshot(
            collected_at_ns=now_ns,
            sessions=sorted(self.sessions.values(),
                            key=lambda s: (s.status != "active", -s.session_start_ns)),
            unmonitored_io=sorted(unmonitored, key=lambda u: -u.io_rate),
            devices=sorted(devices.values(), key=lambda d: d.name),
            rules=list(self.rules),
        )
