"""토폴로지 서비스 구현 — drgn만 쓴다(eBPF 수집기 불필요).

perf/events가 "eBPF만"이었던 것과 정확히 반대편 사례다. 두 축이 서로 독립이라
수집기가 안 떠 있어도 토폴로지는 보이고, 커널에 못 붙어도 성능은 보인다 —
계층을 나눈 실질적인 이득이 여기서 드러난다.

## 장치 조회를 왜 콜러블로 주입받나

이 서비스는 "어떤 컨트롤러들이 있는지"와 "그 컨트롤러의 커널 객체"가 필요한데,
그건 큐/장치 도메인의 지식이다. 그렇다고 그 서비스를 직접 import하면
services/__init__.py 의 규칙(서비스끼리 직접 import 금지 — 그러면 같은
프로세스에 있어야만 돌아간다)을 깬다. 그래서 **필요한 두 동작만** 콜러블로
주입받는다. 나중에 장치 조회가 원격 서비스로 빠져도 이 서비스는 안 바뀐다.
"""
from __future__ import annotations

from typing import Callable, List

from telemetryd.backend.topology import build_topology
from telemetryd.models import Topology


class DrgnTopologyService:
    """@list_devices: () -> ["nvme0", ...]
    @lookup_device: device -> (struct nvme_dev*, gendisk) 같은 커널 객체 쌍.
      토폴로지 빌더가 거기서 PCIe 조상과 NVMe 하위 구조를 캐낸다."""

    def __init__(self, list_devices: Callable[[], List[str]], lookup_device: Callable):
        self._list_devices = list_devices
        self._lookup_device = lookup_device

    def get_topology(self) -> Topology:
        return build_topology(self._list_devices(), self._lookup_device, backend_kind="drgn")
