"""토폴로지 서비스의 mock 구현.

실제 커널을 안 보므로 값은 전부 가짜지만 **구조는 drgn 구현과 동일하게** 만든다
(호스트 브리지 → 브리지 체인 → 엔드포인트 → 컨트롤러 → 서브시스템/네임스페이스/큐).
그래야 UI/CLI 렌더링과 **조상 노드 공유** 로직을 root 없이 그대로 검증할 수 있다 —
조상 공유는 이 뷰의 핵심이라(§9.14) mock에서 빠지면 검증 가치가 크게 준다.

장치 인벤토리(_TOPOLOGY)와 가짜 주소 생성기는 주입받는다 — mock 백엔드가 다른
mock 서비스들과 같은 인벤토리를 공유해야 화면이 일관되기 때문이고, 서비스가
백엔드를 거꾸로 import하지 않게 하기 위함이다.
"""
from __future__ import annotations

from typing import Callable, Dict

from telemetryd.models import (
    TOPO_HOST_BRIDGE,
    TOPO_NAMESPACE,
    TOPO_NVME_CTRL,
    TOPO_NVME_SUBSYS,
    TOPO_PCI_BRIDGE,
    TOPO_PCI_ENDPOINT,
    TOPO_QUEUE,
    TOPO_QUEUE_GROUP,
    TOPO_SYSTEM,
    Topology,
    TopologyDetail,
    TopologyNode,
)


class MockTopologyService:
    """@topology_map: {device: {...}} 형태의 합성 장치 인벤토리.
    @addr_for: (문자열들) -> 결정론적 가짜 커널 주소."""

    def __init__(self, topology_map: Dict[str, dict], addr_for: Callable[..., int]):
        self._topo = topology_map
        self._addr_for = addr_for

    def get_topology(self) -> Topology:
        """합성 PCIe+NVMe 통합 트리.

        실제 커널을 안 보므로 값은 전부 가짜지만, **구조는 drgn 백엔드가 만드는
        것과 동일하게** 만든다(호스트 브리지 → 브리지 체인 → 엔드포인트 →
        컨트롤러 → 서브시스템/네임스페이스/큐). 그래야 UI/CLI 렌더링과 조상
        노드 공유 로직을 root 없이도 그대로 검증할 수 있다."""
        root = TopologyNode(
            id="system", kind=TOPO_SYSTEM, label="시스템 (mock)",
            sublabel="PCI 호스트 브리지 → PCIe 계보 → NVMe 컨트롤러/서브시스템/네임스페이스/큐",
        )
        host = TopologyNode(
            id="hostbridge:0000:00", kind=TOPO_HOST_BRIDGE, label="pci0000:00",
            sublabel="PCI 호스트 브리지 (루트 버스)",
            details=[TopologyDetail(key="도메인:버스", value="0000:00"),
                     TopologyDetail(key="버스 번호 범위", value="0–255")],
        )
        root.children.append(host)
        index = {host.id: host}

        for device, topo in self._topo.items():
            pcie = topo["pcie"]
            parent = host
            # [한국어] 조상 브리지들은 id로 병합 — nvme0/nvme1이 같은 루트 포트를
            # 공유하면 그 노드는 한 번만 나오고 거기서 갈라진다(실제 트리와 동일).
            for bdf, kind_text, ids in pcie["parents"]:
                node = index.get(f"pci:{bdf}")
                if node is None:
                    node = TopologyNode(
                        id=f"pci:{bdf}", kind=TOPO_PCI_BRIDGE, label=bdf, sublabel=kind_text,
                        details=[TopologyDetail(key="vendor:device", value=ids),
                                 TopologyDetail(key="class", value="PCI-to-PCI 브리지 (0x060400)"),
                                 TopologyDetail(key="PCIe 타입", value=kind_text)],
                    )
                    index[node.id] = node
                    parent.children.append(node)
                parent = node

            endpoint = TopologyNode(
                id=f"pci:{pcie['bdf']}", kind=TOPO_PCI_ENDPOINT, label=pcie["bdf"],
                sublabel="NVM Express 컨트롤러 (0x010802)", device=device,
                details=[TopologyDetail(key="vendor:device", value=pcie["ids"]),
                         TopologyDetail(key="class", value="NVM Express 컨트롤러 (0x010802)"),
                         TopologyDetail(key="PCIe 타입", value="PCIe 엔드포인트"),
                         TopologyDetail(key="인터럽트 모드", value="MSI-X"),
                         TopologyDetail(key="struct pci_dev", value=hex(self._addr_for(device, "pci_dev")))],
            )
            parent.children.append(endpoint)

            online, depth = topo["online_queues"], topo["depth"]
            queues = TopologyNode(
                id=f"queues:{device}", kind=TOPO_QUEUE_GROUP, label=f"큐 {online}개",
                sublabel=f"admin 1 + I/O {online - 1} (할당 {online})", device=device,
                details=[TopologyDetail(key="online_queues", value=str(online))],
                children=[
                    TopologyNode(
                        id=f"q:{device}:{qid}", kind=TOPO_QUEUE,
                        label=f"qid {qid}" + (" (admin)" if qid == 0 else ""),
                        sublabel=f"depth {depth}", device=device,
                        details=[TopologyDetail(key="qid", value=str(qid)),
                                 TopologyDetail(key="depth", value=str(depth)),
                                 TopologyDetail(key="blk-mq hctx",
                                                value="없음 (admin)" if qid == 0 else f"hctx[{qid - 1}]")],
                    )
                    for qid in range(online)
                ],
            )
            cap = topo["sectors"] * 512
            ns = TopologyNode(
                id=f"ns:{device}n1", kind=TOPO_NAMESPACE, label=f"{device}n1",
                sublabel=f"네임스페이스 nsid={topo['nsid']}", device=device,
                details=[TopologyDetail(key="nsid", value=str(topo["nsid"])),
                         TopologyDetail(key="LBA 크기", value=f"{topo['lba']} B"),
                         TopologyDetail(key="용량",
                                        value=f"{cap / (1 << 30):.2f} GiB ({topo['sectors']} × 512B 섹터)")],
            )
            subsys = TopologyNode(
                id=f"subsys:{topo['subsys']}", kind=TOPO_NVME_SUBSYS,
                label=f"nvme-subsys{topo['subsys']}", sublabel="NVMe 서브시스템", device=device,
                details=[TopologyDetail(key="subnqn", value=f"nqn.2019-08.org.qemu:mock{topo['subsys']}"),
                         TopologyDetail(key="모델", value=topo["model"]),
                         TopologyDetail(key="시리얼", value=f"MOCK{topo['subsys']:04d}"),
                         TopologyDetail(key="펌웨어", value="1.0"),
                         TopologyDetail(key="소속 컨트롤러", value="1개")],
            )
            endpoint.children.append(TopologyNode(
                id=f"ctrl:{device}", kind=TOPO_NVME_CTRL, label=device,
                sublabel="NVMe 컨트롤러 (struct nvme_dev / nvme_ctrl)", device=device,
                details=[TopologyDetail(key="상태", value="NVME_CTRL_LIVE"),
                         TopologyDetail(key="queue_count", value=str(online)),
                         TopologyDetail(key="네임스페이스", value="1개"),
                         TopologyDetail(key="struct nvme_dev", value=hex(self._addr_for(device, "nvme_dev")))],
                children=[subsys, ns, queues],
            ))
        return Topology(root=root, backend_kind="mock")

    # ---- NVMe I/O 프로세스 프로파일러 (합성 데이터) -----------------------
