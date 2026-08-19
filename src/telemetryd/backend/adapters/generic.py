"""Generic 어댑터 — 어떤 어댑터도 매칭되지 않을 때의 폴백(명세 3-4).

애플리케이션 지식이 전혀 없이 **관측 데이터만으로** 동작한다:
- 그룹핑: 스레드 comm의 공통 prefix로 묶는다(worker_00, worker_01 -> "worker_*").
- 역할 추론: 관측된 I/O 패턴으로 역할을 추정한다.

추정 결과에는 반드시 `inferred=True`를 달아 확정 정보와 구분한다(명세 3-4 요구).
기대 워크로드는 알 수 없으므로 None을 돌려주고, 그러면 대조도 하지 않는다
("판단 불가"와 "불일치"는 다르다).
"""
from __future__ import annotations

import os
import re
from typing import List, Optional

from telemetryd.backend.adapters.base import measured_from_stats
from telemetryd.models import LogicalGroup, ProcessInfo, ProcessIoStat, WorkloadSpec

_SUFFIX_RE = re.compile(r"[-_]?\d+$")     # worker_00, thr-3 처럼 끝의 번호를 떼기 위한 패턴


def _group_key(comm: str) -> str:
    """스레드 이름에서 번호 접미사를 떼어 공통 prefix를 만든다."""
    base = _SUFFIX_RE.sub("", comm).strip("-_")
    return base or comm


def infer_role(stat: Optional[ProcessIoStat]) -> str:
    """관측된 동작만으로 역할을 추정한다(명세 3-4의 표를 그대로 구현).

    확정 정보가 아니라 추정이므로 호출자는 결과에 inferred 표시를 해야 한다."""
    if stat is None or stat.iops <= 0:
        return "I/O 없음 (control/monitor 추정)"
    rw_total = stat.read_iops + stat.write_iops
    if rw_total <= 0:
        # [한국어] read/write가 아닌 커맨드만 발행 = admin 계열(identify, get_log 등).
        return "admin 커맨드만 발행 (admin 추정)"
    write_share = stat.write_iops / rw_total
    seq = stat.seq_ratio
    if write_share >= 0.8:
        if seq is not None and seq >= 0.8:
            return "sequential write 추정"
        return "random write 추정" if (seq is not None and seq <= 0.2) else "write 위주 추정"
    if write_share <= 0.2:
        if seq is not None and seq <= 0.2:
            return "random read 추정"
        return "sequential read 추정" if (seq is not None and seq >= 0.8) else "read 위주 추정"
    return "read/write 혼합 추정"


class GenericAdapter:
    name = "generic"

    def matches(self, proc: ProcessInfo) -> bool:
        # [한국어] 최후의 폴백이라 항상 참 — 선택 로직(adapters/__init__.py)이
        # 다른 어댑터를 먼저 시도하고 여기로 떨어진다.
        return True

    def get_expected_workload(self, proc: ProcessInfo) -> Optional[WorkloadSpec]:
        return None      # 앱을 모르니 의도된 워크로드도 알 수 없다

    def get_logical_groups(self, proc: ProcessInfo,
                           stats: List[ProcessIoStat]) -> List[LogicalGroup]:
        buckets: dict = {}
        for tid, comm in (proc.threads or []):
            buckets.setdefault(_group_key(comm), []).append(tid)
        if not buckets:
            buckets = {proc.comm or f"pid{proc.pid}": [proc.pid]}

        role = infer_role(stats[0] if stats else None)
        groups: List[LogicalGroup] = []
        for name, tids in sorted(buckets.items()):
            measured = measured_from_stats(stats, tids if len(buckets) > 1 else None)
            groups.append(LogicalGroup(
                name=name if len(tids) == 1 else f"{name}_*",
                type="thread_prefix",
                source="comm_prefix",
                thread_tids=sorted(tids),
                expected_workload=None,
                measured_workload=measured,
                expectation_match=None,        # 기대값이 없으니 판단 불가
                mismatch_reasons=[f"역할 추정: {role}"],
                inferred=True,                  # 이 그룹핑/역할은 전부 추정이다
            ))
        return groups

    def get_progress(self, proc: ProcessInfo) -> Optional[float]:
        return None
