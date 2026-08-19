"""애플리케이션 어댑터 인터페이스(명세 PART 3).

=== 왜 어댑터인가 ===
프로세스마다 스레드 구조와 논리 단위가 다르다 — fio는 job, 자체 검증 도구는
시나리오/페이즈, 그 외는 아무 규칙이 없을 수도 있다. **코어 수집기는 이런
애플리케이션 지식을 전혀 갖지 않고**(어떤 앱 이름도 코어에 등장하지 않는다),
어댑터가 관측 데이터에 의미를 부여한다. 어댑터가 없거나 파싱에 실패하면
generic으로 우아하게 축소된다(명세 설계 원칙 3, 7-2).

=== 어댑터가 하는 일 ===
1) matches()        — 이 프로세스를 자기가 처리할 수 있는가
2) logical_groups() — 스레드를 논리 단위로 묶는다(job/시나리오/worker pool)
3) expected_workload() — "의도된" 워크로드. 실측과 대조할 기준값
4) progress()       — 진행률(알 수 있으면)

3번이 이 구조의 실질적 가치다: `--bs=128k`로 지정했는데 실제 SQE가 4K로 나가는
상황(bio 분할, max_sectors_kb 제한)을 잡아낸다.
"""
from __future__ import annotations

from typing import List, Optional, Protocol

from telemetryd.models import (
    LogicalGroup,
    MeasuredWorkload,
    ProcessInfo,
    ProcessIoStat,
    WorkloadSpec,
)


class AppAdapter(Protocol):
    name: str

    def matches(self, proc: ProcessInfo) -> bool:
        """이 어댑터가 해당 프로세스를 처리할 수 있는가."""
        ...

    def get_expected_workload(self, proc: ProcessInfo) -> Optional[WorkloadSpec]:
        """의도된 워크로드 정의(없으면 None — 대조를 하지 않는다)."""
        ...

    def get_logical_groups(self, proc: ProcessInfo,
                           stats: List[ProcessIoStat]) -> List[LogicalGroup]:
        """스레드를 논리 단위로 묶는다."""
        ...

    def get_progress(self, proc: ProcessInfo) -> Optional[float]:
        """진행률 퍼센트(알 수 없으면 None)."""
        ...


def measured_from_stats(stats: List[ProcessIoStat],
                        tids: Optional[List[int]] = None) -> MeasuredWorkload:
    """프로세스의 (장치별) 통계를 합쳐 실측 워크로드를 만든다.

    tids를 주면 그 스레드들의 IOPS 비중만큼 안분한다 — 한 프로세스 안에 여러
    논리 그룹(fio가 --thread로 job을 스레드로 돌리는 경우)이 있을 때 필요하다.
    크기/패턴 분포는 스레드 단위로 수집하지 않으므로(오버헤드) 프로세스 값을
    그대로 쓴다 — 같은 job의 스레드들은 같은 워크로드를 돌리는 게 전제다."""
    if not stats:
        return MeasuredWorkload()
    total_iops = sum(s.iops for s in stats)
    share = 1.0
    if tids is not None:
        thread_iops = sum(t.iops for s in stats for t in s.threads if t.tid in tids)
        share = (thread_iops / total_iops) if total_iops else 0.0

    read = sum(s.read_iops for s in stats) * share
    write = sum(s.write_iops for s in stats) * share
    rw_total = read + write
    # [한국어] 지배적 I/O 크기는 건수 기준 최빈값 — 여러 장치에 걸쳐 있으면
    # 장치별 히스토그램을 합쳐서 고른다.
    hist: dict = {}
    for s in stats:
        for size, cnt in s.io_size_hist:
            hist[size] = hist.get(size, 0) + cnt
    dominant = max(hist.items(), key=lambda kv: kv[1])[0] if hist else 0
    return MeasuredWorkload(
        io_size_dominant=dominant,
        read_ratio=(read / rw_total) if rw_total else 0.0,
        write_ratio=(write / rw_total) if rw_total else 0.0,
        queue_depth_avg=sum(s.queue_depth_est for s in stats) * share,
        iops=total_iops * share,
        bandwidth_bps=sum(s.bandwidth_bps for s in stats) * share,
    )


def compare(expected: Optional[WorkloadSpec],
            measured: MeasuredWorkload,
            seq_ratio: Optional[float] = None) -> tuple:
    """기대 vs 실측 대조 -> (일치 여부, 불일치 사유 목록).

    기대값이 아예 없으면 (None, []) — "판단 불가"와 "불일치"는 다르다.
    각 항목은 있는 것만 비교한다(어댑터가 일부만 알아냈을 수 있음).

    허용 오차:
    - I/O 크기: 정확히 일치해야 한다. 여기가 어긋나는 게 바로 이 기능의 목적
      (bio 분할/max_sectors_kb 제한으로 SQE가 쪼개지는 상황)이라 봐준다는 개념이 없다.
    - R/W 비율: 방향만 본다(read-only 기대인데 write가 5% 넘게 섞이면 불일치).
    - 큐 깊이: 리틀의 법칙 근사라 오차가 커서 ±30%까지 같다고 본다.
    - 패턴: 순차 비율 0.8 이상이면 sequential, 0.2 이하면 random으로 판정.
    """
    if expected is None:
        return None, []
    # [한국어] 아직 I/O가 한 건도 안 잡힌 대상은 "일치"가 아니라 **판단 불가**다.
    # (실측 fio에서 확인: 워커를 fork하는 fio 메인 프로세스는 자기 자신은 I/O를
    # 내지 않는데, 이걸 "기대대로 동작 중"으로 표시하면 완전한 오독이 된다.)
    if measured.iops <= 0 and measured.io_size_dominant == 0:
        return None, ["아직 이 대상에서 관측된 I/O 없음 — 대조할 실측값이 없다"]
    reasons: List[str] = []

    if expected.io_size and measured.io_size_dominant:
        if expected.io_size != measured.io_size_dominant:
            reasons.append(
                f"I/O 크기: 기대 {expected.io_size}B vs 실측 {measured.io_size_dominant}B "
                "(bio 분할 또는 max_sectors_kb 제한 가능성)")

    if expected.rw:
        rw = expected.rw.lower()
        want_read = "read" in rw or rw in ("randrw", "rw", "readwrite")
        want_write = "write" in rw or rw in ("randrw", "rw", "readwrite")
        if want_read and not want_write and measured.write_ratio > 0.05:
            reasons.append(f"R/W: 기대 {expected.rw}인데 write가 {measured.write_ratio:.0%}")
        if want_write and not want_read and measured.read_ratio > 0.05:
            reasons.append(f"R/W: 기대 {expected.rw}인데 read가 {measured.read_ratio:.0%}")

    if expected.queue_depth and measured.queue_depth_avg:
        lo, hi = expected.queue_depth * 0.7, expected.queue_depth * 1.3
        if not (lo <= measured.queue_depth_avg <= hi):
            reasons.append(
                f"큐 깊이: 기대 {expected.queue_depth} vs 실측 근사 "
                f"{measured.queue_depth_avg:.1f} (리틀의 법칙 추정치)")

    if expected.pattern and seq_ratio is not None:
        got = "sequential" if seq_ratio >= 0.8 else ("random" if seq_ratio <= 0.2 else "mixed")
        if got != expected.pattern:
            reasons.append(f"패턴: 기대 {expected.pattern} vs 실측 {got}(순차 비율 {seq_ratio:.0%})")

    return (len(reasons) == 0), reasons
