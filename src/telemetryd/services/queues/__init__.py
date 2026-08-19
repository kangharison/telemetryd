"""큐/장치 서비스 — 컨트롤러/큐/SQ/CQ/PRP/구조체 트리(이 도구의 핵심 도메인).

의존: platform.kernel 만 (eBPF 수집기 불필요).
"""
from telemetryd.services.queues.contract import SERVICE_NAME, QueueService
from telemetryd.services.queues.service import DrgnQueueService

__all__ = ["SERVICE_NAME", "QueueService", "DrgnQueueService"]
