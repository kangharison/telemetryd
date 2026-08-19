"""PCIe 토폴로지 + NVMe 서브시스템을 **하나의 트리로 합쳐** 만드는 빌더(drgn).

=== 이 파일의 역할 ===
"이 NVMe 장치가 시스템에 어떻게 붙어 있는가"를 한 화면에 담기 위해, 서로 다른
두 계층을 한 줄기로 잇는다:

    PCI 호스트 브리지 → (브리지/스위치) → PCIe 엔드포인트(BDF)
        → struct nvme_dev / nvme_ctrl      ← 두 세계가 만나는 지점
            → nvme_subsystem (subnqn/model/serial, 같은 서브시스템의 컨트롤러들)
            → 네임스페이스 (nvme0n1, nsid, LBA 크기, 용량)
            → 큐 (admin + I/O, depth/도어벨)

컨트롤러가 여러 개면 위쪽 PCIe 조상들은 **한 번만** 나오고 그 아래에서 갈라진다
(같은 브리지를 공유하는 게 실제 하드웨어 구조 그대로다) — 노드 id로 병합한다.

=== 전체 아키텍처에서의 위치 ===
DrgnBackend.get_topology()가 이 모듈의 build_topology()를 부른다. treewalk.py의
포인터 트리와 목적이 다르다: treewalk는 "구조체 필드를 있는 그대로" 따라가는
범용 lazy 탐색이고, 이 모듈은 의미 단위로 재구성한 정적 뷰라 한 번에 전부
만들어 보낸다(노드 수십 개 수준).

=== 커널 자료구조 경로 ===
- `nvme_dev.dev`(struct device*) → `container_of(..., struct pci_dev, dev)`로 PCIe 쪽 진입
- `pci_dev.bus`(struct pci_bus*) → `bus.self`(부모 브리지 pci_dev, 루트 버스면 NULL)
  → `bus.parent` … 를 반복해 루트까지 거슬러 올라간다
- `nvme_dev.ctrl` → `ctrl.subsys`(struct nvme_subsystem), `ctrl.namespaces`(list of
  struct nvme_ns), `ns.head`(struct nvme_ns_head), `dev.queues[i]`(struct nvme_queue)

=== 주의 ===
`pci_dev`의 `class` 필드는 파이썬 예약어라 `pdev.member_("class")`로 읽어야 한다.
장치가 PCIe가 아니거나(pcie_cap == 0) 일부 필드를 못 읽어도 트리 전체가 깨지지
않도록, 각 속성 읽기는 개별적으로 예외를 삼킨다 — 토폴로지는 "보이는 만큼
보여주는" 게 목적이지 하나 실패했다고 전부 못 보여주면 안 된다.
"""
from __future__ import annotations

from typing import Dict, List, Optional

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

#: PCIe Capabilities Register(PCI_EXP_FLAGS)의 Device/Port Type 필드 값.
#: 타입은 **bits[7:4]** 다 — `PCI_EXP_FLAGS_TYPE 0x00f0`, 커널의
#: pci_pcie_type()도 `(pcie_flags_reg & 0x00f0) >> 4`로 읽는다. bits[3:0]은
#: capability 버전이라, 마스크를 0xf로 잘못 쓰면 "타입 0x2"(= 버전 2)처럼
#: 엉뚱한 값이 나온다(실제 커널로 확인하다 잡은 버그).
_PCIE_TYPES = {
    0x0: "PCIe 엔드포인트",
    0x1: "레거시 PCIe 엔드포인트",
    0x4: "루트 포트",
    0x5: "업스트림 스위치 포트",
    0x6: "다운스트림 스위치 포트",
    0x7: "PCIe→PCI 브리지",
    0x8: "PCI→PCIe 브리지",
    0x9: "루트 컴플렉스 통합 엔드포인트(RCiEP)",
    0xA: "루트 컴플렉스 이벤트 컬렉터",
}

#: PCI class code 상위 바이트 기준 대략적인 분류 — 정확한 표는 방대해서
#: 이 프로젝트에서 실제로 보게 되는 것만 둔다(나머지는 hex 그대로 표시).
_PCI_CLASSES = {
    0x0108: "NVM Express 컨트롤러",
    0x0106: "SATA 컨트롤러",
    0x0604: "PCI-to-PCI 브리지",
    0x0600: "호스트 브리지",
}


#: pci_power_t 값 -> 이름. 이건 enum이 아니라 `typedef int pci_power_t`라서
#: drgn이 열거자 이름을 못 준다(str()이 "(pci_power_t)0"으로 나옴) — 그래서
#: include/linux/pci.h의 PCI_D0~PCI_POWER_ERROR 정의를 여기 표로 옮겨 둔다.
_PCI_POWER_STATES = {
    0: "D0 (완전 동작)", 1: "D1", 2: "D2", 3: "D3hot", 4: "D3cold",
    5: "unknown", -1: "error",
}


def pcie_type_name(pcie_flags_reg: int) -> str:
    """PCIe Capabilities Register -> Device/Port Type 이름(순수 함수, 테스트용)."""
    t = (pcie_flags_reg & 0x00F0) >> 4
    return _PCIE_TYPES.get(t, f"타입 0x{t:x}")


def pci_power_state_name(value: int) -> str:
    """pci_dev.current_state(pci_power_t = int) -> 사람이 읽는 전원 상태."""
    return _PCI_POWER_STATES.get(value, f"0x{value:x}")


def _d(key: str, value) -> TopologyDetail:
    return TopologyDetail(key=key, value=str(value))


def _try(fn, default=None):
    """drgn 읽기 하나가 실패해도 트리 전체를 포기하지 않기 위한 헬퍼.

    커널 버전/설정에 따라 없는 필드(domain_nr 등)나 읽을 수 없는 주소(MMIO,
    미매핑)가 섞이는데, 토폴로지는 "보이는 만큼" 보여주는 게 목적이라 개별
    속성 실패는 조용히 넘긴다."""
    try:
        return fn()
    except Exception:
        return default


def _enum_name(obj, default: str = "?") -> str:
    """drgn enum Object -> 열거자 이름. str(obj)가 "(enum X)NAME" 형태라 뒷부분만.
    treewalk.describe()가 쓰는 것과 같은 규칙(§9.3에서 실측으로 정한 처리)."""
    def go():
        rendered = str(obj)
        close = rendered.find(")")
        return rendered[close + 1:] if rendered.startswith("(enum") and close != -1 else rendered
    return _try(go, default) or default


def _cstr(obj, default: str = "") -> str:
    """char 배열 -> 파이썬 문자열(널 종료/공백 정리)."""
    return _try(lambda: obj.string_().decode(errors="replace").strip(), default) or default


def _domain_nr(bus) -> int:
    """PCI 도메인 번호. CONFIG_PCI_DOMAINS_GENERIC 커널은 pci_bus.domain_nr,
    x86은 bus.sysdata(struct pci_sysdata).domain에 들어 있다. 둘 다 없으면 0
    (도메인이 하나뿐인 흔한 경우)."""
    from drgn import cast

    v = _try(lambda: int(bus.domain_nr))
    if v is not None and v >= 0:
        return v
    v = _try(lambda: int(cast("struct pci_sysdata *", bus.sysdata).domain))
    return v if v is not None else 0


def _bdf(pdev) -> str:
    """"0000:00:04.0" 형식의 PCI 주소. devfn은 (device << 3) | function 인코딩."""
    bus = pdev.bus
    devfn = int(pdev.devfn)
    return f"{_domain_nr(bus):04x}:{int(bus.number):02x}:{devfn >> 3:02x}.{devfn & 7}"


def _class_text(pdev) -> str:
    """class code(3바이트: base/sub/prog-if)를 사람이 읽는 이름 + 원본 hex로."""
    cls = _try(lambda: int(pdev.member_("class")))
    if cls is None:
        return "?"
    name = _PCI_CLASSES.get(cls >> 8)
    return f"{name} (0x{cls:06x})" if name else f"0x{cls:06x}"


def _pcie_type_text(pdev) -> str:
    """PCIe 포트 종류. pcie_cap == 0 이면 PCIe capability 자체가 없는 레거시 PCI."""
    if not _try(lambda: int(pdev.pcie_cap), 0):
        return "레거시 PCI (PCIe capability 없음)"
    reg = _try(lambda: int(pdev.pcie_flags_reg))
    return pcie_type_name(reg) if reg is not None else "?"


def _pci_dev_details(pdev) -> List[TopologyDetail]:
    """PCI 장치 노드 공통 속성 — 브리지든 엔드포인트든 같은 항목을 보여준다."""
    details = [
        _d("vendor:device", f"{_try(lambda: int(pdev.vendor), 0):04x}:{_try(lambda: int(pdev.device), 0):04x}"),
        _d("class", _class_text(pdev)),
        _d("PCIe 타입", _pcie_type_text(pdev)),
        _d("revision", f"0x{_try(lambda: int(pdev.revision), 0):02x}"),
    ]
    subv = _try(lambda: int(pdev.subsystem_vendor))
    subd = _try(lambda: int(pdev.subsystem_device))
    if subv or subd:
        details.append(_d("subsystem", f"{subv or 0:04x}:{subd or 0:04x}"))
    irq = _try(lambda: int(pdev.irq))
    if irq:
        details.append(_d("IRQ", irq))
    msi = _try(lambda: bool(pdev.msi_enabled), False)
    msix = _try(lambda: bool(pdev.msix_enabled), False)
    details.append(_d("인터럽트 모드", "MSI-X" if msix else ("MSI" if msi else "레거시 INTx")))
    # [한국어] current_state는 enum이 아니라 typedef int라 _enum_name으로는
    # "(pci_power_t)0"이 그대로 나온다 — 값 -> 이름 표로 직접 변환한다.
    power = _try(lambda: int(pdev.current_state))
    details.append(_d("전원 상태", pci_power_state_name(power) if power is not None else "?"))
    details.append(_d("struct pci_dev", hex(pdev.value_())))
    return details


def _bus_chain(pdev) -> List:
    """엔드포인트에서 루트 버스까지의 조상 브리지 목록(루트에 가까운 순).

    pci_bus.self 는 "이 버스를 만든 부모 쪽 브리지 장치"이고, 루트 버스에서는
    NULL이다. 그래서 self를 따라 올라가다 NULL을 만나면 그 버스가 루트다."""
    chain = []
    bus = pdev.bus
    seen = set()
    while True:
        addr = _try(lambda: bus.value_())
        if addr is None or addr in seen:      # 방어: 순환이면 중단
            break
        seen.add(addr)
        bridge = _try(lambda: bus.self)
        if bridge is None or not bridge:
            chain.append((bus, None))          # 루트 버스(호스트 브리지 쪽)
            break
        chain.append((bus, bridge))
        bus = bridge.bus
    chain.reverse()
    return chain


def _namespace_nodes(ctrl, device: str) -> List[TopologyNode]:
    """ctrl.namespaces 리스트를 돌며 네임스페이스 노드를 만든다.

    용량/블록 크기는 ns(lba_shift)와 gendisk(part0.bd_nr_sectors)에서 얻는다 —
    NVMe가 보고하는 LBA 크기와 블록 계층이 아는 섹터 수를 함께 보여주면
    "이 네임스페이스가 실제로 몇 GB인지"가 한 줄로 확인된다."""
    from drgn.helpers.linux.block import disk_name
    from drgn.helpers.linux.list import list_for_each_entry

    nodes: List[TopologyNode] = []
    def each():
        return list(list_for_each_entry("struct nvme_ns", ctrl.namespaces.address_of_(), "list"))

    for ns in (_try(each) or []):
        head = _try(lambda: ns.head)
        nsid = _try(lambda: int(head.ns_id), 0) if head else 0
        name = _try(lambda: disk_name(ns.disk).decode(errors="replace"), f"nsid{nsid}")
        lba_shift = _try(lambda: int(ns.lba_shift), 0)
        lba_size = 1 << lba_shift if lba_shift else 0
        sectors = _try(lambda: int(ns.disk.part0.bd_nr_sectors), 0) or 0
        # [한국어] bd_nr_sectors는 항상 512바이트 단위(블록 계층 관례)라 LBA
        # 크기와 무관하게 512를 곱해야 실제 바이트 용량이 된다.
        cap_bytes = sectors * 512
        details = [
            _d("nsid", nsid),
            _d("LBA 크기", f"{lba_size} B" if lba_size else "?"),
            _d("용량", f"{cap_bytes / (1 << 30):.2f} GiB ({sectors} × 512B 섹터)" if sectors else "?"),
            _d("struct nvme_ns", hex(ns.value_())),
        ]
        shared = _try(lambda: bool(head.shared))
        if shared is not None:
            details.append(_d("공유(multipath)", "예" if shared else "아니오"))
        nvmset = _try(lambda: int(head.nvmset_id))
        if nvmset:
            details.append(_d("NVM Set ID", nvmset))
        nodes.append(TopologyNode(
            id=f"ns:{name}", kind=TOPO_NAMESPACE, label=name,
            sublabel=f"네임스페이스 nsid={nsid}", device=device, details=details,
        ))
    return nodes


def _subsystem_node(ctrl, device: str) -> Optional[TopologyNode]:
    """nvme_subsystem 노드 — 컨트롤러 여러 개가 공유할 수 있는 상위 개념.

    NVMe에서 "서브시스템"은 같은 subnqn/모델/시리얼을 공유하는 컨트롤러들의
    묶음이다(멀티패스 구성에서 컨트롤러 2개가 한 서브시스템에 붙는다). 그래서
    이 노드는 컨트롤러 밑에 두되, "이 서브시스템에 컨트롤러가 몇 개 물려 있고
    네임스페이스 헤드가 몇 개인지"를 같이 보여줘 관계가 드러나게 한다."""
    from drgn.helpers.linux.list import list_for_each_entry

    subsys = _try(lambda: ctrl.subsys)
    if subsys is None or not subsys:
        return None
    instance = _try(lambda: int(subsys.instance), 0)
    n_ctrls = _try(lambda: len(list(list_for_each_entry(
        "struct nvme_ctrl", subsys.ctrls.address_of_(), "subsys_entry"))), 0)
    n_heads = _try(lambda: len(list(list_for_each_entry(
        "struct nvme_ns_head", subsys.nsheads.address_of_(), "entry"))), 0)
    details = [
        _d("subnqn", _cstr(subsys.subnqn, "?")),
        _d("모델", _cstr(subsys.model, "?")),
        _d("시리얼", _cstr(subsys.serial, "?")),
        _d("펌웨어", _cstr(subsys.firmware_rev, "?")),
        _d("CMIC", f"0x{_try(lambda: int(subsys.cmic), 0):02x}"),
        _d("소속 컨트롤러", f"{n_ctrls}개"),
        _d("네임스페이스 헤드", f"{n_heads}개"),
        _d("struct nvme_subsystem", hex(subsys.value_())),
    ]
    subtype = _try(lambda: _enum_name(subsys.subtype))
    if subtype:
        details.append(_d("subtype", subtype))
    return TopologyNode(
        id=f"subsys:{instance}", kind=TOPO_NVME_SUBSYS, label=f"nvme-subsys{instance}",
        sublabel="NVMe 서브시스템", device=device, details=details,
    )


def _queue_group_node(dev, device: str) -> Optional[TopologyNode]:
    """dev.queues[] 요약 + 큐별 자식 노드. 큐는 개수가 많아 UI에서 접어 둔다."""
    online = _try(lambda: int(dev.online_queues), 0) or 0
    allocated = _try(lambda: int(dev.nr_allocated_queues), 0) or 0
    if not online:
        return None
    children: List[TopologyNode] = []
    for i in range(online):
        q = _try(lambda: dev.queues[i])
        if q is None:
            continue
        qid = _try(lambda: int(q.qid), i)
        depth = _try(lambda: int(q.q_depth), 0)
        is_admin = qid == 0
        children.append(TopologyNode(
            id=f"q:{device}:{qid}", kind=TOPO_QUEUE,
            label=f"qid {qid}" + (" (admin)" if is_admin else ""),
            sublabel=f"depth {depth}", device=device,
            details=[
                _d("qid", qid),
                _d("depth", depth),
                _d("sq_tail", _try(lambda: int(q.sq_tail), 0)),
                _d("cq_head", _try(lambda: int(q.cq_head), 0)),
                _d("sq_dma_addr", hex(_try(lambda: int(q.sq_dma_addr), 0))),
                _d("cq_dma_addr", hex(_try(lambda: int(q.cq_dma_addr), 0))),
                # [한국어] I/O 큐는 blk-mq hctx와 1:1로 대응한다(qid-1 = hctx 인덱스).
                _d("blk-mq hctx", "없음 (admin)" if is_admin else f"hctx[{qid - 1}]"),
            ],
        ))
    return TopologyNode(
        id=f"queues:{device}", kind=TOPO_QUEUE_GROUP, label=f"큐 {online}개",
        sublabel=f"admin 1 + I/O {online - 1} (할당 {allocated})", device=device,
        details=[_d("online_queues", online), _d("nr_allocated_queues", allocated)],
        children=children,
    )


def _ctrl_node(dev, device: str) -> TopologyNode:
    """PCIe 엔드포인트와 NVMe 세계를 잇는 노드 — 이 트리의 접합점.

    struct nvme_dev는 PCI 드라이버의 장치 구조체이고 그 안에 struct nvme_ctrl이
    박혀 있다(container_of 관계). 위로는 pci_dev, 아래로는 서브시스템/네임스페이스/
    큐가 붙으므로 여기가 두 계층이 만나는 지점이다."""
    ctrl = dev.ctrl
    children: List[TopologyNode] = []
    subsys = _subsystem_node(ctrl, device)
    if subsys is not None:
        children.append(subsys)
    ns_nodes = _namespace_nodes(ctrl, device)
    children.extend(ns_nodes)
    qg = _queue_group_node(dev, device)
    if qg is not None:
        children.append(qg)

    details = [
        _d("상태", _enum_name(ctrl.state)),
        _d("cntlid", _try(lambda: int(ctrl.cntlid), 0)),
        _d("queue_count", _try(lambda: int(ctrl.queue_count), 0)),
        _d("sqsize", _try(lambda: int(ctrl.sqsize), 0)),
        _d("max_hw_sectors", _try(lambda: int(ctrl.max_hw_sectors), 0)),
        _d("numa_node", _try(lambda: int(ctrl.numa_node), -1)),
        _d("네임스페이스", f"{len(ns_nodes)}개"),
        _d("struct nvme_dev", hex(dev.value_())),
        _d("struct nvme_ctrl", hex(_try(lambda: ctrl.address_of_().value_(), 0))),
    ]
    return TopologyNode(
        id=f"ctrl:{device}", kind=TOPO_NVME_CTRL, label=device,
        sublabel="NVMe 컨트롤러 (struct nvme_dev / nvme_ctrl)", device=device,
        details=details, children=children,
    )


def build_topology(devices: List[str], get_dev_and_disk, backend_kind: str = "drgn") -> Topology:
    """디바이스 목록 전체를 하나의 PCIe+NVMe 통합 트리로 만든다.

    get_dev_and_disk는 DrgnBackend._get_dev_and_disk(device) — (nvme_dev, gendisk)를
    돌려주는 콜러블이다(이 모듈이 DrgnBackend를 import하지 않게 주입받는다).

    조상 PCIe 노드는 id로 병합해서, 컨트롤러 2개가 같은 브리지 밑에 있으면
    브리지가 한 번만 나오고 거기서 갈라진다 — 실제 하드웨어 구조 그대로."""
    from drgn import container_of

    root = TopologyNode(
        id="system", kind=TOPO_SYSTEM, label="시스템",
        sublabel="PCI 호스트 브리지 → PCIe 계보 → NVMe 컨트롤러/서브시스템/네임스페이스/큐",
    )
    index: Dict[str, TopologyNode] = {"system": root}
    errors: List[str] = []

    for device in devices:
        try:
            dev, _disk = get_dev_and_disk(device)
            pdev = container_of(dev.dev, "struct pci_dev", "dev")
        except Exception as e:                      # 이 디바이스만 건너뛴다
            errors.append(f"{device}: {e}")
            continue

        parent = root
        # [한국어] 루트 버스 -> ... -> 엔드포인트 바로 위 브리지 순으로 붙인다.
        for bus, bridge in _bus_chain(pdev):
            if bridge is None or not bridge:
                bus_id = f"hostbridge:{_domain_nr(bus):04x}:{int(bus.number):02x}"
                node = index.get(bus_id)
                if node is None:
                    node = TopologyNode(
                        id=bus_id, kind=TOPO_HOST_BRIDGE,
                        label=_cstr(bus.name, f"pci{_domain_nr(bus):04x}:{int(bus.number):02x}"),
                        sublabel="PCI 호스트 브리지 (루트 버스)",
                        details=[
                            _d("도메인:버스", f"{_domain_nr(bus):04x}:{int(bus.number):02x}"),
                            _d("버스 번호 범위", f"{_try(lambda: int(bus.busn_res.start), 0)}"
                                              f"–{_try(lambda: int(bus.busn_res.end), 0)}"),
                            _d("struct pci_bus", hex(bus.value_())),
                        ],
                    )
                    index[bus_id] = node
                    parent.children.append(node)
                parent = node
                continue

            bdf = _bdf(bridge)
            node_id = f"pci:{bdf}"
            node = index.get(node_id)
            if node is None:
                node = TopologyNode(
                    id=node_id, kind=TOPO_PCI_BRIDGE, label=bdf,
                    sublabel=_pcie_type_text(bridge),
                    details=_pci_dev_details(bridge) + [
                        _d("하위 버스", f"{int(bus.number)}"),
                    ],
                )
                index[node_id] = node
                parent.children.append(node)
            parent = node

        # 엔드포인트(= NVMe 컨트롤러의 PCI 함수)
        bdf = _bdf(pdev)
        ep_id = f"pci:{bdf}"
        ep = index.get(ep_id)
        if ep is None:
            ep = TopologyNode(
                id=ep_id, kind=TOPO_PCI_ENDPOINT, label=bdf,
                sublabel=f"{_class_text(pdev)}", device=device,
                details=_pci_dev_details(pdev),
            )
            index[ep_id] = ep
            parent.children.append(ep)
        ep.children.append(_ctrl_node(dev, device))

    return Topology(root=root, backend_kind=backend_kind,
                    error="; ".join(errors) if errors else None)
