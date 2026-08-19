"""이벤트 서비스의 mock 구현.

**의도적으로 이벤트를 합성하지 않는다.** 타임아웃/에러는 정상 운영에서 거의 안
일어나는 것들이라, 안 난 이벤트를 난 것처럼 보여주면 화면을 믿을 수 없게 된다.
mock에서는 "이벤트 0건"이 정상 상태이고, UI/CLI가 빈 목록을 어떻게 그리는지가
이걸로 검증된다. 파싱/봉투 구성 로직 자체는 실제 로그 포맷을 그대로 흉내낸
tests/test_ebpf_*_events.py가 덮는다.
"""
from __future__ import annotations

from typing import List

from telemetryd.backend.event_registry import registered_event_kinds
from telemetryd.models import DeviceErrorStats, EventKindInfo, NvmeEvent


class MockEventService:
    def get_events(self, device: str) -> List[NvmeEvent]:
        return []

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        # [한국어] "수집은 되지만 0건" — available=True 로 두어야 UI가 "수집기
        # 없음"과 "에러가 안 났음"을 구분해 보여줄 수 있다.
        return DeviceErrorStats(device=device, counts=[], total=0, available=True)

    def list_event_kinds(self) -> List[EventKindInfo]:
        # [한국어] 등록 목록은 백엔드와 무관한 시스템 속성이라 같은 목록을 주되,
        # mock엔 수집기가 없으므로 active=False — "등록은 됐지만 지금 수집되진
        # 않는다"가 화면에 그대로 드러난다.
        return registered_event_kinds(active=False)
