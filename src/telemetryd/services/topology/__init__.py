"""토폴로지 서비스 — PCIe 계보 + NVMe 서브시스템 통합 트리(DESIGN.md §9.14).

의존: drgn 커널 조회만 (eBPF 수집기 불필요) — perf/events와 정확히 반대편.
"""
from telemetryd.services.topology.contract import SERVICE_NAME, TopologyService
from telemetryd.services.topology.mock import MockTopologyService
from telemetryd.services.topology.service import DrgnTopologyService

__all__ = ["SERVICE_NAME", "TopologyService", "DrgnTopologyService", "MockTopologyService"]
