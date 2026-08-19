"""프로파일러 서비스의 mock 구현.

실제 커널/수집기 없이 UI·CLI를 개발·검증하기 위한 합성 프로세스와 I/O 통계.
값은 결정적으로 만든다(난수 X) — 대상 규칙 매칭이나 세션 유지 같은 로직은
"같은 입력이면 같은 결과"여야 테스트가 안정적이다.

대상 규칙/세션 저장소(TargetRegistry)는 mock에서도 **실제와 같은 것**을 쓴다.
규칙 매칭·세션 생명주기가 프로파일러의 핵심 로직이라, 여기까지 가짜로 만들면
정작 검증하고 싶은 부분이 안 덮이기 때문이다. 저장 경로만 주입받아 테스트가
임시 디렉터리를 쓸 수 있게 한다.
"""
from __future__ import annotations

from typing import List, Optional

from telemetryd.backend.targets import TargetRegistry, rule_matches
from telemetryd.models import (
    ProcessInfo,
    ProcessIoStat,
    ProcessListEntry,
    ProfileSnapshot,
    TargetRule,
    ThreadIoStat,
)


class MockProfilerService:
    """@target_state: 규칙/세션 영속화 경로(None이면 기본 XDG state)."""

    def __init__(self, target_state: Optional[str] = None):
        self._target_state = target_state
        self._targets = None

    def _registry(self) -> TargetRegistry:
        if self._targets is None:
            self._targets = TargetRegistry(self._target_state)
        return self._targets

    def _mock_processes(self) -> List[ProcessInfo]:
        """대상 선택 화면을 root/게스트 없이 눌러볼 수 있게 만든 합성 프로세스들.

        의도적으로 서로 다른 성격을 섞는다: 어댑터가 붙는 fio 2개(옵션이 다름),
        어댑터가 없는 일반 프로세스, 그리고 선택 불가여야 하는 커널 스레드."""
        return [
            ProcessInfo(pid=4821, comm="ioworker", cmdline="./ioworker --threads 4",
                        exe_path="/home/user/bin/ioworker", uid=1000, start_time_ns=111_000,
                        thread_count=4,
                        threads=[(4821, "ioworker"), (4822, "worker_00"),
                                 (4823, "worker_01"), (4824, "worker_02")]),
            ProcessInfo(pid=5102, comm="fio", uid=1000, start_time_ns=222_000,
                        exe_path="/usr/bin/fio", thread_count=1, threads=[(5102, "fio")],
                        cmdline="fio --name=randwrite --rw=randwrite --bs=4k --iodepth=32 "
                                "--numjobs=4 --ioengine=io_uring --direct=1 "
                                "--filename=/dev/nvme1n1"),
            ProcessInfo(pid=3390, comm="fio", uid=1000, start_time_ns=333_000,
                        exe_path="/usr/bin/fio", thread_count=1, threads=[(3390, "fio")],
                        cmdline="fio --name=seqread --rw=read --bs=128k --iodepth=8 "
                                "--ioengine=libaio --direct=1 --filename=/dev/nvme0n1"),
            ProcessInfo(pid=9, comm="kworker/0:1", thread_count=1, threads=[(9, "kworker/0:1")],
                        error="커널 스레드(mm 없음) — cmdline/exe 없음"),
        ]

    def _mock_proc_stats(self) -> List[ProcessIoStat]:
        """합성 I/O 통계. seqread(3390)은 기대 128k인데 실측 4k로 두어, 어댑터의
        기대값 대조가 **불일치를 실제로 잡아내는 것**을 mock에서도 보여준다
        (명세 3-2의 핵심 가치 — bio 분할/max_sectors_kb 제한 상황)."""
        return [
            ProcessIoStat(device="nvme0", pid=4821, comm="ioworker", iops=2840.0,
                          read_iops=1200.0, write_iops=1640.0, bandwidth_bps=46_530_560.0,
                          avg_latency_us=118.0, io_size_dominant=16384,
                          io_size_hist=[(16384, 2840)], queues=[(1, 1400), (2, 1440)],
                          queue_depth_est=0.34, seq_ratio=0.9,
                          threads=[ThreadIoStat(tid=4822, comm="worker_00", iops=1420.0),
                                   ThreadIoStat(tid=4823, comm="worker_01", iops=1420.0)]),
            ProcessIoStat(device="nvme1", pid=5102, comm="fio", iops=1120.0,
                          read_iops=0.0, write_iops=1120.0, bandwidth_bps=4_587_520.0,
                          avg_latency_us=28_500.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 1120)], queues=[(3, 1120)],
                          queue_depth_est=31.9, seq_ratio=0.02,
                          threads=[ThreadIoStat(tid=5102, comm="fio", iops=1120.0)]),
            ProcessIoStat(device="nvme0", pid=3390, comm="fio", iops=640.0,
                          read_iops=640.0, write_iops=0.0, bandwidth_bps=2_621_440.0,
                          avg_latency_us=12_500.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 640)], queues=[(4, 640)],
                          queue_depth_est=8.0, seq_ratio=0.95,
                          threads=[ThreadIoStat(tid=3390, comm="fio", iops=640.0)]),
            ProcessIoStat(device="nvme0", pid=9, comm="kworker/0:1", iops=12.0,
                          read_iops=0.0, write_iops=12.0, bandwidth_bps=49_152.0,
                          avg_latency_us=800.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 12)], queues=[(1, 12)],
                          queue_depth_est=0.01, seq_ratio=0.5, threads=[]),
        ]

    def list_processes(self, only_io: bool = False) -> List[ProcessListEntry]:
        io_by_pid = {}
        for st in self._mock_proc_stats():
            e = io_by_pid.setdefault(st.pid, {"rate": 0.0, "devices": set()})
            e["rate"] += st.iops
            e["devices"].add(st.device)
        rules = self._registry().rules
        out = []
        for proc in self._mock_processes():
            io = io_by_pid.get(proc.pid)
            matched = next((r for r in rules if rule_matches(r, proc)), None)
            entry = ProcessListEntry(
                info=proc, io_active=bool(io and io["rate"] > 0),
                io_rate=io["rate"] if io else 0.0,
                target_devices=sorted(io["devices"]) if io else [],
                is_target=matched is not None,
                matched_rule=f"{matched.kind}={matched.value}" if matched else None,
            )
            if proc.error and "커널 스레드" in proc.error:
                entry.selectable = False
                entry.unselectable_reason = "커널 스레드 — 프로파일 대상이 아님"
            out.append(entry)
        if only_io:
            out = [e for e in out if e.io_active]
        out.sort(key=lambda e: (-e.io_rate, e.info.pid))
        return out

    def list_targets(self) -> List[TargetRule]:
        return list(self._registry().rules)

    def add_target(self, rule: TargetRule) -> List[TargetRule]:
        self._registry().add_rule(rule)
        return list(self._registry().rules)

    def remove_target(self, kind: str, value: str) -> List[TargetRule]:
        self._registry().remove_rule(kind, value)
        return list(self._registry().rules)

    def get_profile(self) -> ProfileSnapshot:
        return self._registry().refresh(self._mock_processes(), self._mock_proc_stats())
