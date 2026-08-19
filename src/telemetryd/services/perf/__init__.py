"""성능 서비스 — eBPF 수집기가 집계한 큐별 IOPS/대역폭/지연(percentile 포함).

의존: platform.ebpf 만 (drgn 불필요).
"""
from telemetryd.services.perf.contract import SERVICE_NAME, PerfService
from telemetryd.services.perf.mock import MockPerfService
from telemetryd.services.perf.service import EbpfPerfService

__all__ = ["SERVICE_NAME", "PerfService", "EbpfPerfService", "MockPerfService"]
