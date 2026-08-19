"""성능 서비스 계약.

메서드가 하나뿐인 게 요점이다 — 이 서비스를 쓰는 쪽(웹 성능 탭, CLI perf)이
필요로 하는 게 그것뿐이라서다. 기존 God Interface(`Backend`, 메서드 17개)에서는
성능만 필요한 소비자도 큐/트리/토폴로지/프로파일러 메서드를 다 가진 객체를
받아야 했다.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from telemetryd.models import DevicePerf

#: 레지스트리/설정에서 이 서비스를 가리키는 이름. 문자열을 여기서만 정의해
#: 오타로 어긋나는 걸 막는다(이벤트 kind 상수와 같은 방침).
SERVICE_NAME = "perf"


@runtime_checkable
class PerfService(Protocol):
    """eBPF 수집기가 집계한 큐별 성능의 최신 스냅샷."""

    def get_performance(self, device: str) -> DevicePerf:
        """@device: "nvme0" 같은 컨트롤러 이름.
        @return: 큐별 IOPS/대역폭/평균지연 + p50/p95/p99/p99.9 + 전체 합산.
          수집기가 안 떠 있거나 아직 첫 틱 전이면 available=False + error 메시지
          (예외를 던지지 않는다 — "아직 데이터가 없음"은 정상 상태이고, 대시보드가
          그 사유를 그대로 보여줘야 하기 때문)."""
        ...
