"""이벤트 서비스 계약.

DESIGN.md §9.12의 "종류 무관 이벤트 목록" 설계를 계약 수준에서 지킨다 —
반환 타입이 `NvmeEvent`(공통 봉투)이지 타임아웃/에러 전용 타입이 아니다.
새 종류가 늘어도 이 계약은 안 바뀐다.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from telemetryd.models import DeviceErrorStats, EventKindInfo, NvmeEvent

SERVICE_NAME = "events"


@runtime_checkable
class EventService(Protocol):
    """NVMe 이벤트(타임아웃/에러/…)의 목록·집계·등록 종류."""

    def get_events(self, device: str) -> List[NvmeEvent]:
        """이 디바이스의 최근 이벤트(발생순). 관측된 게 없으면 빈 리스트.

        이벤트가 0건인 건 대부분의 시간 동안 **정상**이라, 성능처럼
        available=False로 구분하지 않는다 — 그렇게 하면 UI만 시끄러워진다."""
        ...

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        """SCT/SC 조합별 누적 카운터.

        이벤트 목록과 별개로 유지하는 이유(DESIGN.md §9.13): 이벤트 줄은 로그
        폭주를 막으려고 초당 인쇄 예산으로 샘플링될 수 있지만 카운터는 전부
        센다 — "각 건의 상세"는 놓쳐도 "몇 건 났는지"는 정확하다."""
        ...

    def list_event_kinds(self) -> List[EventKindInfo]:
        """등록된 이벤트 종류. UI가 종류 목록을 하드코딩하지 않고 물어본다."""
        ...
