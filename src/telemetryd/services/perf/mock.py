"""성능 서비스의 mock 구현 — 수집기도 커널도 없이 도는 합성 데이터.

UI/CLI 개발과 테스트에서 쓴다. **합성값임이 드러나야** 실측과 헷갈리지 않으므로,
값은 tick 기반의 결정적(deterministic) 패턴으로 만든다 — 난수를 쓰면 테스트가
불안정해지고, 화면에서도 "진짜 같아" 보여 오해를 부른다.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from telemetryd.models import DevicePerf, QueuePerf


def _tick() -> int:
    return int(time.time())


class MockPerfService:
    """@queues_of: device -> I/O 큐 개수. 토폴로지를 아는 쪽에서 주입받아
    (서비스끼리 직접 import하지 않는다는 규칙, services/__init__.py 참고)
    mock이 다른 mock 서비스에 묶이지 않게 한다."""

    def __init__(self, queues_of: Optional[Callable[[str], int]] = None,
                 clock: Callable[[], int] = _tick):
        self._queues_of = queues_of or (lambda device: 9)
        self._clock = clock

    def get_performance(self, device: str) -> DevicePerf:
        online = self._queues_of(device)
        t = self._clock()
        queues = []
        for idx in range(1, online):   # admin 큐는 IOPS 개념이 희박해 I/O 큐만
            phase = t + idx * 11
            read_iops = float(500 + (phase * 37) % 3000)
            write_iops = float(300 + (phase * 23) % 2000)
            avg_seg = 4096 + (phase % 4) * 12288   # 4KB~52KB를 오가는 평균 전송 크기
            lat = float(80 + (phase * 7) % 900)
            # [한국어] percentile은 실제 히스토그램이 없으니 avg 기준 배수로 꼬리를
            # 흉내낸다(p50 < avg < p95 < p99 < p99.9). 실측 분포는 아니다.
            queues.append(QueuePerf(
                qid=idx,
                iops=read_iops + write_iops,
                read_iops=read_iops,
                write_iops=write_iops,
                bandwidth_bytes_per_sec=(read_iops + write_iops) * avg_seg,
                avg_latency_us=lat,
                p50_latency_us=lat * 0.85,
                p95_latency_us=lat * 2.0,
                p99_latency_us=lat * 4.0,
                p999_latency_us=lat * 8.0,
            ))

        aggregate = None
        if queues:
            n = len(queues)
            aggregate = QueuePerf(
                qid=-1,
                iops=sum(q.iops for q in queues),
                read_iops=sum(q.read_iops for q in queues),
                write_iops=sum(q.write_iops for q in queues),
                bandwidth_bytes_per_sec=sum(q.bandwidth_bytes_per_sec for q in queues),
                avg_latency_us=sum(q.avg_latency_us for q in queues) / n,
                p50_latency_us=sum(q.p50_latency_us for q in queues) / n,
                p95_latency_us=sum(q.p95_latency_us for q in queues) / n,
                p99_latency_us=sum(q.p99_latency_us for q in queues) / n,
                p999_latency_us=sum(q.p999_latency_us for q in queues) / n,
            )
        return DevicePerf(device=device, queues=queues, available=True, aggregate=aggregate)
