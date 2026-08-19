"""이벤트 서비스 — 종류를 가리지 않는 NVMe 이벤트 목록/집계(DESIGN.md §9.12).

의존: platform.ebpf 만 (drgn 불필요).
"""
from telemetryd.services.events.contract import SERVICE_NAME, EventService
from telemetryd.services.events.mock import MockEventService
from telemetryd.services.events.service import EbpfEventService

__all__ = ["SERVICE_NAME", "EventService", "EbpfEventService", "MockEventService"]
