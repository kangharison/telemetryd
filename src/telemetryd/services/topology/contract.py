"""토폴로지 서비스 계약 — PCIe 계보 + NVMe 서브시스템 통합 트리(DESIGN.md §9.14).

device 인자가 없는 게 요점이다: 이 뷰의 핵심이 "같은 브리지 아래 붙은 장치들이
조상 노드를 **공유**한다"는 것이라, 장치 하나씩 따로 만들면 계보가 왜곡된다.
그래서 서비스가 전체 트리를 한 번에 만든다.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from telemetryd.models import Topology

SERVICE_NAME = "topology"


@runtime_checkable
class TopologyService(Protocol):
    def get_topology(self) -> Topology:
        """시스템 전체의 PCIe+NVMe 통합 트리.

        실시간 스트림이 아니다 — 장치 구성은 거의 안 바뀌는 반면 drgn 조회가
        장치·큐마다 반복돼 비싸다(실측 컨트롤러 2개+큐 18개에 10~12초). 호출
        측은 탭을 열 때 한 번 받아 캐시하고 갱신은 명시적으로 한다."""
        ...
