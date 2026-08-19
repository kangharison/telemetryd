"""통합 토폴로지(PCIe + NVMe 서브시스템) 트리 테스트.

이 기능의 핵심은 "두 계층이 한 트리로 이어진다"는 것이므로, 검증도 그 구조에
맞춘다: (1) 호스트 브리지에서 시작해 PCIe 계보를 타고 내려가 엔드포인트에
닿고, (2) 그 엔드포인트 아래에서 NVMe 컨트롤러 → 서브시스템/네임스페이스/큐로
이어지며, (3) 같은 브리지를 공유하는 장치는 조상 노드를 공유한다.

drgn 백엔드는 실제 커널이 필요해 여기서 못 돌리므로 mock으로 검증한다. mock은
값만 합성이고 **구조는 drgn 백엔드와 동일하게** 만들도록 되어 있어(mock_backend
.get_topology 주석) 렌더링/직렬화 경로 검증에는 충분하다."""
import asyncio

import grpc
from google.protobuf.json_format import MessageToDict

from telemetryd.backend import get_backend
from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.grpcserver import telemetryd_pb2_grpc as pb_grpc
from telemetryd.grpcserver.convert import topology_to_pb
from telemetryd.grpcserver.server import TelemetrydServicer
from telemetryd.models import (
    TOPO_HOST_BRIDGE,
    TOPO_NAMESPACE,
    TOPO_NVME_CTRL,
    TOPO_NVME_SUBSYS,
    TOPO_PCI_BRIDGE,
    TOPO_PCI_ENDPOINT,
    TOPO_QUEUE_GROUP,
    TOPO_SYSTEM,
)


def _walk(node, acc=None):
    acc = acc if acc is not None else []
    acc.append(node)
    for c in node.children:
        _walk(c, acc)
    return acc


def _find(node, kind, label=None):
    return [n for n in _walk(node)
            if n.kind == kind and (label is None or n.label == label)]


def _parent_of(root, target_id):
    for n in _walk(root):
        if any(c.id == target_id for c in n.children):
            return n
    return None


def test_root_is_system_with_host_bridge():
    root = get_backend("mock").get_topology().root
    assert root.kind == TOPO_SYSTEM
    bridges = [c for c in root.children if c.kind == TOPO_HOST_BRIDGE]
    assert len(bridges) == 1, "호스트 브리지가 트리의 진입점이어야 한다"


def test_pcie_chain_reaches_endpoint_then_nvme():
    """이 기능의 핵심 — PCIe 계보를 타고 내려가면 그 끝에서 NVMe 계층이 이어진다."""
    root = get_backend("mock").get_topology().root
    ep = _find(root, TOPO_PCI_ENDPOINT, "0000:00:04.0")[0]
    # 엔드포인트의 조상은 PCI 브리지 -> 호스트 브리지 순으로 이어져야 한다.
    bridge = _parent_of(root, ep.id)
    assert bridge.kind == TOPO_PCI_BRIDGE
    assert _parent_of(root, bridge.id).kind == TOPO_HOST_BRIDGE

    # 엔드포인트 바로 아래가 NVMe 컨트롤러 — 두 계층이 만나는 지점.
    ctrl = [c for c in ep.children if c.kind == TOPO_NVME_CTRL]
    assert [c.label for c in ctrl] == ["nvme0"]
    kinds = {c.kind for c in ctrl[0].children}
    assert {TOPO_NVME_SUBSYS, TOPO_NAMESPACE, TOPO_QUEUE_GROUP} <= kinds


def test_shared_ancestor_is_not_duplicated():
    """두 장치가 같은 루트 포트 아래 있으면 그 노드는 트리에 한 번만 나온다
    (실제 하드웨어 구조 그대로 — 병합이 안 되면 계보가 왜곡된다)."""
    root = get_backend("mock").get_topology().root
    shared = _find(root, TOPO_PCI_BRIDGE, "0000:00:02.0")
    assert len(shared) == 1
    # nvme1은 그 아래 스위치를 한 단계 더 거친다.
    ep1 = _find(root, TOPO_PCI_ENDPOINT, "0000:02:00.0")[0]
    assert _parent_of(root, ep1.id).label == "0000:01:00.0"


def test_every_nvme_node_carries_device_name():
    """UI가 선택된 장치 경로를 강조하려면 NVMe 쪽 노드가 자기 디바이스명을
    들고 있어야 한다."""
    root = get_backend("mock").get_topology().root
    for kind in (TOPO_NVME_CTRL, TOPO_NVME_SUBSYS, TOPO_NAMESPACE, TOPO_QUEUE_GROUP):
        for n in _find(root, kind):
            assert n.device in ("nvme0", "nvme1"), f"{kind} 노드에 device가 비었다"


def test_queue_group_has_one_child_per_online_queue():
    root = get_backend("mock").get_topology().root
    qg = [n for n in _find(root, TOPO_QUEUE_GROUP) if n.device == "nvme0"][0]
    assert len(qg.children) == 3          # admin + I/O 2 (mock nvme0의 online_queues)
    assert qg.children[0].label.endswith("(admin)")


def test_details_are_generic_key_values():
    """노드 종류별 필드를 모델에 박지 않았는지 — 새 종류가 생겨도 UI가 안 깨지는
    구조인지 확인한다."""
    root = get_backend("mock").get_topology().root
    ns = _find(root, TOPO_NAMESPACE, "nvme0n1")[0]
    keys = {d.key for d in ns.details}
    assert {"nsid", "LBA 크기", "용량"} <= keys
    assert all(isinstance(d.value, str) for d in ns.details)


def test_topology_to_pb_roundtrip_preserves_tree():
    """재귀 메시지 직렬화 — 깊이/자식 수가 그대로 유지돼야 한다."""
    topo = get_backend("mock").get_topology()
    msg = topology_to_pb(topo)
    assert msg.backend_kind == "mock"

    def depth(n):
        return 1 + max((depth(c) for c in n.children), default=0)

    def pb_depth(n):
        return 1 + max((pb_depth(c) for c in n.children), default=0)

    assert pb_depth(msg.root) == depth(topo.root)
    assert len(_walk(topo.root)) == len(list(_pb_walk(msg.root)))


def _pb_walk(node):
    yield node
    for c in node.children:
        yield from _pb_walk(c)


def test_get_topology_rpc_and_json_shape():
    """웹이 받는 JSON 모양 — 프론트가 kind/label/details/children만으로 그린다."""
    async def go(stub):
        reply = await stub.GetTopology(pb.Empty())
        d = MessageToDict(reply, preserving_proto_field_name=True,
                          always_print_fields_with_no_presence=True)
        root = d["root"]
        assert {"id", "kind", "label", "children"} <= set(root)
        host = root["children"][0]
        assert host["kind"] == TOPO_HOST_BRIDGE
        # 깊은 노드까지 details가 살아 있어야 한다.
        def find_kind(n, kind):
            if n.get("kind") == kind:
                return n
            for c in n.get("children", []):
                got = find_kind(c, kind)
                if got:
                    return got
            return None
        ns = find_kind(root, TOPO_NAMESPACE)
        assert ns and any(x["key"] == "nsid" for x in ns["details"])

    async def run():
        server = grpc.aio.server()
        pb_grpc.add_TelemetrydServicer_to_server(TelemetrydServicer("mock"), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
                await go(pb_grpc.TelemetrydStub(ch))
        finally:
            await server.stop(None)

    asyncio.run(run())


# ===========================================================================
# [한국어] 아래 두 개는 실제 커널로 토폴로지를 처음 돌려보다 잡은 버그의 회귀
# 테스트다. drgn 없이 돌 수 있도록 비트 해석/값 매핑을 순수 함수로 분리해 뒀다.
# ===========================================================================

def test_pcie_type_uses_bits_7_4_not_3_0():
    """PCI_EXP_FLAGS_TYPE는 0x00f0(bits 7:4)이다. bits[3:0]은 capability
    버전이라, 마스크를 0xf로 쓰면 QEMU NVMe(버전 2, 엔드포인트)가
    "타입 0x2"로 잘못 나온다 — 실제 게스트에서 그렇게 나와서 잡았다."""
    from telemetryd.backend.topology import pcie_type_name

    assert pcie_type_name(0x0002) == "PCIe 엔드포인트"      # 버전2 + 타입0
    assert pcie_type_name(0x0042) == "루트 포트"            # 타입 4
    assert pcie_type_name(0x0062) == "다운스트림 스위치 포트"  # 타입 6
    assert pcie_type_name(0x0052) == "업스트림 스위치 포트"    # 타입 5


def test_pci_power_state_is_decoded_from_int():
    """current_state는 enum이 아니라 typedef int라 drgn이 이름을 못 준다
    (실측: "(pci_power_t)0"). 값->이름 매핑을 직접 갖고 있어야 한다."""
    from telemetryd.backend.topology import pci_power_state_name

    assert pci_power_state_name(0).startswith("D0")
    assert pci_power_state_name(3) == "D3hot"
    assert pci_power_state_name(-1) == "error"
