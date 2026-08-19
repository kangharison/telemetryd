"""root/실제 커널 없이 CLI·gRPC·Web 전체 파이프라인을 개발·테스트하기 위한
합성 백엔드.

DESIGN.md §0에서 설명한 제약(이 세션에서 non-interactive sudo 불가) 때문에
Claude 자신은 실제 drgn 라이브 세션을 실행할 수 없다. 그래서 이 백엔드가
구조적으로 DrgnBackend와 동일한 모양(같은 dataclass, 같은 Backend Protocol)의
데이터를 만들어내고, 이걸로 상위 계층(CLI/gRPC/Web/C++ 바인딩)을 전부
end-to-end 검증한다. drgn 의존성이 전혀 없다 — 이 파일만 import해도 동작한다.

값은 상태를 안 들고 다니면서도(요청마다 새 인스턴스여도 일관) 매 폴링마다
"움직이는" 것처럼 보이도록 `time.monotonic()` 기반 tick으로 결정론적으로
생성한다 — StreamDeviceMetrics(실시간 갱신) 데모에 자연스러운 변화를 준다.
"""
from __future__ import annotations

import hashlib
import time
from typing import List, Optional

from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.models import (
    CompletionEntry,
    DevicePerf,
    DeviceSnapshot,
    PrpPage,
    PrpPayload,
    QueueEntry,
    QueuePerf,
    QueueSnapshot,
    DeviceErrorStats,
    EventKindInfo,
    NvmeEvent,
    ProcessInfo,
    ProcessIoStat,
    ProcessListEntry,
    ProfileSnapshot,
    TargetRule,
    ThreadIoStat,
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
    TreeExpansion,
    TreeNode,
)
from telemetryd.nvme_const import ADM_OPC, MAX_PAGE_DUMP, NVM_OPC, PAGE_SIZE, opcode_name
from telemetryd.services.events import MockEventService
from telemetryd.services.perf import MockPerfService
from telemetryd.services.profiler import MockProfilerService
from telemetryd.services.topology import MockTopologyService

MAX_DEPTH = 10          # treewalk.py의 실제 규칙과 동일 (§DESIGN 5.5) — 여기선 drgn 비의존을 위해 상수 중복
MAX_ARRAY_CHILDREN = 64

# [한국어] 디바이스별 고정 토폴로지: (online_queues, depth, model).
#  online_queues 는 Admin(qid=0) 포함 개수 — 예를 들어 3이면 Admin+IO#1+IO#2.
#  pcie 항목은 통합 토폴로지(get_topology)용 가짜 PCIe 좌표다 — nvme0은 루트
#  포트 바로 아래, nvme1은 그 아래 스위치를 한 단계 더 거치게 해서 (a) 조상
#  노드 공유(둘 다 같은 루트 포트 아래), (b) 다단계 중첩을 UI/CLI에서 실제로
#  눌러볼 수 있게 한다. 실제 하드웨어가 아니라 렌더링 검증용 합성 데이터다.
_TOPOLOGY = {
    "nvme0": {"online_queues": 3, "depth": 128, "model": "QEMU NVMe Ctrl (mock)",
              "pcie": {"bdf": "0000:00:04.0", "ids": "1b36:0010", "parents": [
                  ("0000:00:02.0", "루트 포트", "8086:1901")]},
              "subsys": 0, "nsid": 1, "lba": 512, "sectors": 2 * 1024 * 1024},
    "nvme1": {"online_queues": 2, "depth": 64, "model": "Samsung PM9A3 (mock)",
              "pcie": {"bdf": "0000:02:00.0", "ids": "144d:a824", "parents": [
                  ("0000:00:02.0", "루트 포트", "8086:1901"),
                  ("0000:01:00.0", "다운스트림 스위치 포트", "1b21:1184")]},
              "subsys": 1, "nsid": 1, "lba": 4096, "sectors": 4 * 1024 * 1024},
}


def _addr_for(*parts: str) -> int:
    """디바이스/필드명 문자열들로부터 결정론적인 가짜 커널 가상주소를 만든다.

    sha256으로 해싱한다 — 단순 곱셈 해시는 짧은 필드명(예: "bar" vs "dbs")
    끼리 충돌이 나서(직접 겪음: 둘 다 0xffff88800000f9e0로 나온 적 있음) 트리
    탐색기에서 서로 다른 포인터가 같은 주소로 보이는 혼란을 준다.
    """
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    val = int.from_bytes(digest[:6], "big")
    return 0xFFFF888000000000 | (val & 0x0000FFFFFFFFFFF0)


def _tick() -> int:
    """100ms 단위로 증가하는 카운터 — sq_tail/cq_head가 폴링마다 움직이게."""
    return int(time.monotonic() * 10)


def _queue_dynamics(idx: int, depth: int) -> dict:
    """이 큐의 '지금 이 순간' sq_tail/cq_head/inflight.

    get_device_snapshot()과 get_queue_entries()/get_completion_entries()가
    서로 다른 tick에 각자 계산하면 스냅샷에 찍힌 sq_tail과 "도어벨 기준"
    엔트리 윈도우가 어긋나 보일 수 있어 한 곳에 모았다.
    """
    t = _tick()
    phase = t + idx * 7  # [한국어] 큐마다 위상을 살짝 어긋나게 해서 큐별로 다르게 움직여 보이게.
    sq_tail = phase % depth
    inflight_driver = (phase * 3) % max(1, depth // 4)
    cq_head = (sq_tail - inflight_driver) % depth
    inflight_sched = (phase * 2) % max(1, depth // 8)
    return {"sq_tail": sq_tail, "cq_head": cq_head, "inflight_driver": inflight_driver, "inflight_sched": inflight_sched}


def _window_indices(doorbell: int, depth: int, limit: int) -> List[int]:
    """도어벨(sq_tail 또는 cq_head) 바로 앞 최근 limit개 인덱스 — **최신(도어벨
    바로 앞)부터 오래된 순으로** 내림차순(링 버퍼 wrap 처리). limit<=0이거나
    depth 이상이면 전체 depth."""
    n = min(limit, depth) if limit else depth
    return [(doorbell - 1 - i) % depth for i in range(n)]


class MockBackend:
    kind = "mock"

    def __init__(self, target_state=None):
        # [한국어] 도메인별 서비스(services/*)의 mock 구현. 이 백엔드는 그 앞의
        # 파사드다 — DrgnBackend와 같은 구조를 유지해야 둘을 바꿔 끼울 수 있다.
        self._events = MockEventService()
        self._profiler = MockProfilerService(target_state)
        self._topology_svc = MockTopologyService(_TOPOLOGY, _addr_for)
        self._perf = MockPerfService(
            queues_of=lambda device: self._topology(device)["online_queues"]
        )

    def list_devices(self) -> List[str]:
        return list(_TOPOLOGY.keys())

    def _topology(self, device: str) -> dict:
        if device not in _TOPOLOGY:
            raise DeviceNotFoundError(device)
        return _TOPOLOGY[device]

    def get_device_snapshot(self, device: str) -> DeviceSnapshot:
        topo = self._topology(device)
        online = topo["online_queues"]
        depth = topo["depth"]
        queues = []
        for idx in range(online):
            qid = idx
            is_admin = qid == 0
            hctx_index = None if is_admin else qid - 1
            dyn = _queue_dynamics(idx, depth)
            queues.append(
                QueueSnapshot(
                    index=idx,
                    qid=qid,
                    is_admin=is_admin,
                    depth=depth,
                    sq_tail=dyn["sq_tail"],
                    cq_head=dyn["cq_head"],
                    sq_dma_addr=_addr_for(device, f"sq{qid}") & 0xFFFFFFFF,
                    cq_dma_addr=_addr_for(device, f"cq{qid}") & 0xFFFFFFFF,
                    hctx_index=hctx_index,
                    inflight_driver=dyn["inflight_driver"],
                    inflight_sched=0 if is_admin else dyn["inflight_sched"],
                )
            )
        return DeviceSnapshot(
            name=device,
            addr=_addr_for(device, "nvme_dev"),
            model=topo["model"],
            online_queues=online,
            allocated_queues=online,
            bar_addr=_addr_for(device, "bar"),
            dbs_addr=_addr_for(device, "dbs"),
            iommu_enabled=False,
            backend_kind=self.kind,
            queues=queues,
        )

    def _queue_topology(self, device: str, qid: int) -> dict:
        topo = self._topology(device)
        if not (0 <= qid < topo["online_queues"]):
            raise QueueNotFoundError(qid)
        return topo

    def _synthetic_entry(self, device: str, qid: int, index: int) -> QueueEntry:
        """(device, qid, index) 만으로 결정론적으로 만드는 SQ 엔트리 1개.

        상태를 전혀 안 들고 다녀서 get_queue_entries()와 get_prp_payload()가
        같은 cid에 대해 항상 같은 엔트리를 재생성할 수 있다(cid == index로 고정).
        """
        is_admin = qid == 0
        table = ADM_OPC if is_admin else NVM_OPC
        opcodes = list(table.keys())
        opcode = opcodes[index % len(opcodes)]
        # [한국어] 3개 중 1개꼴로 SGL(PSDT!=0)을 섞어서 PRP/SGL 분기를 CLI/Web에서 다 볼 수 있게.
        uses_sgl = (index % 3 == 2)
        flags = (0x1 << 6) if uses_sgl else 0x0
        base = _addr_for(device, f"q{qid}", f"e{index}")
        prp1 = base & ~0xFFF  # 페이지 정렬
        prp2 = (base + PAGE_SIZE * 3) & ~0xFFF
        return QueueEntry(
            index=index,
            cid=index,
            opcode=opcode,
            opcode_name=opcode_name(opcode, is_admin),
            nsid=1,
            flags=flags,
            uses_sgl=uses_sgl,
            cdw2=0,
            cdw3=0,
            cdw10=(index * 37) & 0xFFFFFFFF,
            cdw11=0,
            cdw12=((index % 8) + 1) & 0xFFFF,  # NLB 필드 위치를 흉내(하위 16비트)
            cdw13=0,
            cdw14=0,
            cdw15=0,
            prp1=0 if uses_sgl else prp1,
            prp2=0 if uses_sgl else prp2,
        )

    def get_queue_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[QueueEntry]:
        topo = self._queue_topology(device, qid)
        depth = topo["depth"]
        if around_doorbell:
            sq_tail = _queue_dynamics(qid, depth)["sq_tail"]
            indices = _window_indices(sq_tail, depth, limit or 16)
        else:
            n = min(limit, depth) if limit else depth
            indices = list(range(n))
        return [self._synthetic_entry(device, qid, i) for i in indices]

    def _synthetic_completion(self, device: str, qid: int, index: int, depth: int) -> CompletionEntry:
        """(device, qid, index)만으로 결정론적인 CQ 엔트리 1개.

        command_id는 _synthetic_entry()와 같은 cid==index 관례를 그대로 써서
        "PRP 확인"이 SQ 인덱스로 cid를 찾는 방식과 일관되게 맞춘다(mock 한정
        단순화 — 실제로는 SQ/CQ 인덱스가 서로 독립적이다)."""
        base = _addr_for(device, f"cq{qid}", f"e{index}")
        status_raw = base & 0xFFFF
        return CompletionEntry(
            index=index,
            command_id=index,
            sq_id=qid,
            sq_head=(index + 1) % depth,
            status_raw=status_raw,
            phase=bool(status_raw & 0x1),
            status_code=(status_raw >> 1) & 0xFF,
            status_code_type=(status_raw >> 9) & 0x7,
            result=base & 0xFFFFFFFF,
        )

    def get_completion_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[CompletionEntry]:
        topo = self._queue_topology(device, qid)
        depth = topo["depth"]
        if around_doorbell:
            cq_head = _queue_dynamics(qid, depth)["cq_head"]
            indices = _window_indices(cq_head, depth, limit or 16)
        else:
            n = min(limit, depth) if limit else depth
            indices = list(range(n))
        return [self._synthetic_completion(device, qid, i, depth) for i in indices]

    def get_prp_payload(self, device: str, qid: int, cid: int) -> PrpPayload:
        topo = self._queue_topology(device, qid)
        depth = topo["depth"]
        if not (0 <= cid < depth):
            return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=False,
                               total_len=0, pages=[], error=f"cid={cid} 가 큐 depth({depth}) 범위 밖")
        entry = self._synthetic_entry(device, qid, cid)
        if entry.uses_sgl:
            return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=True, total_len=0, pages=[])

        nlb = entry.cdw12 & 0xFFFF
        total_len = (nlb + 1) * 512  # NLB(0-based) * 512B 섹터 가정 — DrgnBackend의 fallback 공식과 동일
        pages = []
        phys = entry.prp1
        offset = phys & (PAGE_SIZE - 1)
        first_page_bytes = min(total_len, PAGE_SIZE - offset)  # 케이스 판별용 실제 크기(캡과 무관)
        first_bytes = min(first_page_bytes, MAX_PAGE_DUMP)      # 실제로 보여줄 양은 4KB로 캡(요청사항)
        pages.append(_synthetic_page(phys, offset, first_bytes))
        shown = first_bytes
        remaining = total_len - first_page_bytes
        if remaining > 0 and shown < MAX_PAGE_DUMP:
            # [한국어] 04_prp_payload.py의 case B/C 재현: 정확히 2페이지면 PRP2가 두번째 데이터 페이지,
            #  그 이상이면 PRP2는 리스트 페이지 — mock에서는 리스트 엔트리도 합성해서 채운다.
            n_more = -(-remaining // PAGE_SIZE)
            if n_more == 1:
                page_bytes = min(remaining, MAX_PAGE_DUMP - shown)
                if page_bytes > 0:
                    pages.append(_synthetic_page(entry.prp2, 0, page_bytes))
            else:
                # [한국어] 리스트 페이지 자체는 메타데이터라 4KB 캡과 무관하게 항상 보여준다.
                pages.append(_synthetic_page(entry.prp2, 0, min(remaining, PAGE_SIZE), is_list_page=True))
                shown_n = min(n_more, 8)
                for i in range(shown_n):
                    if shown >= MAX_PAGE_DUMP:
                        break
                    entry_phys = (entry.prp2 + PAGE_SIZE * 4 + i * PAGE_SIZE) & ~0xFFF
                    page_bytes = min(PAGE_SIZE, remaining - i * PAGE_SIZE, MAX_PAGE_DUMP - shown)
                    if page_bytes <= 0:
                        break
                    pages.append(_synthetic_page(entry_phys, 0, page_bytes))
                    shown += page_bytes
        return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=False,
                           total_len=total_len, pages=pages)

    # ---- 요청사항 4/6: 포인터 트리 (합성, MAX_DEPTH=10 강제) ----------------

    def get_tree_node(self, device: str, path: List[str]) -> TreeExpansion:
        self._topology(device)  # DeviceNotFoundError 를 여기서 던지게
        depth = len(path)
        if depth > MAX_DEPTH:
            return TreeExpansion(
                node=TreeNode(name=path[-1], type_name="?", kind="unreadable",
                              value_repr="depth 초과", address=None, is_null=False, expandable=False),
                children=[], depth=depth,
                error=f"최대 depth({MAX_DEPTH})를 초과했습니다 (요청 depth={depth})",
            )
        node, children = _mock_tree_step(device, path, self._topology(device))
        return TreeExpansion(node=node, children=children if depth < MAX_DEPTH else [], depth=depth)

    # ---- eBPF 실시간 성능 (요청사항: "device/개별 queue별로 iops/bandwidth/latency") --

    def get_performance(self, device: str) -> DevicePerf:
        """성능 서비스(mock)로 위임 — 로직은 services/perf/mock.py 에 있다.
        큐 개수만 이 백엔드의 토폴로지에서 알려준다(서비스가 다른 서비스를
        직접 import하지 않는다는 규칙 때문에 콜백으로 주입)."""
        self._topology(device)          # 없는 디바이스면 DeviceNotFoundError
        return self._perf.get_performance(device)

    def get_events(self, device: str) -> List[NvmeEvent]:
        """이벤트 서비스(mock)로 위임 — 로직은 services/events/mock.py."""
        self._topology(device)   # 없는 디바이스면 다른 get_*과 똑같이 예외
        return self._events.get_events(device)

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        """이벤트 서비스(mock)로 위임."""
        self._topology(device)
        return self._events.get_error_stats(device)

    def get_topology(self) -> Topology:
        """토폴로지 서비스(mock)로 위임 — 로직은 services/topology/mock.py."""
        return self._topology_svc.get_topology()

    # ---- NVMe I/O 프로세스 프로파일러 (합성 데이터) -----------------------

    def list_processes(self, only_io: bool = False) -> List[ProcessListEntry]:
        """프로파일러 서비스(mock)로 위임."""
        return self._profiler.list_processes(only_io)

    def list_targets(self) -> List[TargetRule]:
        """프로파일러 서비스(mock)로 위임."""
        return self._profiler.list_targets()

    def add_target(self, rule: TargetRule) -> List[TargetRule]:
        """프로파일러 서비스(mock)로 위임."""
        return self._profiler.add_target(rule)

    def remove_target(self, kind: str, value: str) -> List[TargetRule]:
        """프로파일러 서비스(mock)로 위임."""
        return self._profiler.remove_target(kind, value)

    def get_profile(self) -> ProfileSnapshot:
        """프로파일러 서비스(mock)로 위임."""
        return self._profiler.get_profile()

    def list_event_kinds(self) -> List[EventKindInfo]:
        """이벤트 서비스(mock)로 위임."""
        return self._events.list_event_kinds()

def _synthetic_page(phys: int, offset: int, nbytes: int, is_list_page: bool = False) -> PrpPage:
    nbytes = max(0, min(nbytes, PAGE_SIZE))
    # [한국어] 사람이 hexdump에서 패턴을 알아보기 쉽게: 바이트[i] = (phys + i) & 0xff.
    data = bytes(((phys + i) & 0xFF) for i in range(nbytes))
    return PrpPage(phys_addr=phys, offset_in_page=offset, data=data, is_list_page=is_list_page)


def _leaf(name: str, type_name: str, value: str) -> TreeNode:
    return TreeNode(name=name, type_name=type_name, kind="scalar", value_repr=value,
                     address=None, is_null=False, expandable=False)


def _mock_tree_step(device: str, path: List[str], topo: dict) -> tuple:
    """path의 마지막 세그먼트를 보고 (그 노드, 자식 리스트)를 합성으로 만든다.

    실제 struct nvme_dev의 대표적인 포인터 관계(ctrl/pci_dev/bus, kobject 부모
    체인, dev.queues[]->nvme_queue->dev 백포인터)를 흉내내서 (a) struct/pointer/
    array/scalar/string 4가지 kind, (b) 순환 참조 안전성(같은 모양이 반복돼도
    무한루프가 아니라 "누를 때마다 한 단계"라는 것)을 CLI/Web에서 실제로
    눌러볼 수 있게 한다.
    """
    last = path[-1] if path else None

    if last is None:  # 루트: struct nvme_dev
        node = TreeNode(name=device, type_name="struct nvme_dev", kind="struct",
                         value_repr="<struct nvme_dev>", address=_addr_for(device, "nvme_dev"),
                         is_null=False, expandable=True)
        children = [
            TreeNode("ctrl", "struct nvme_ctrl", "struct", "<struct nvme_ctrl>",
                     _addr_for(device, "ctrl"), False, True),
            TreeNode("queues", "struct nvme_queue *", "pointer", hex(_addr_for(device, "queues")),
                     _addr_for(device, "queues"), False, True),
            _leaf("online_queues", "unsigned int", str(topo["online_queues"])),
            TreeNode("model_number", "char[40]", "string", topo["model"], None, False, False),
            TreeNode("bar", "void __iomem *", "pointer", hex(_addr_for(device, "bar")),
                     _addr_for(device, "bar"), False, False),
        ]
        return node, children

    if last == "ctrl":
        node = TreeNode("ctrl", "struct nvme_ctrl", "struct", "<struct nvme_ctrl>",
                         _addr_for(device, "ctrl"), False, True)
        children = [
            TreeNode("pci_dev", "struct pci_dev *", "pointer", hex(_addr_for(device, "pci_dev")),
                     _addr_for(device, "pci_dev"), False, True),
            _leaf("state", "enum nvme_ctrl_state", "NVME_CTRL_LIVE"),
            TreeNode("name", "char[12]", "string", device, None, False, False),
            TreeNode("subsys", "struct nvme_subsystem *", "pointer", hex(_addr_for(device, "subsys")),
                     _addr_for(device, "subsys"), False, True),
        ]
        return node, children

    if last == "pci_dev":
        node = TreeNode("pci_dev", "struct pci_dev", "struct", "<struct pci_dev>",
                         _addr_for(device, "pci_dev"), False, True)
        children = [
            TreeNode("bus", "struct pci_bus *", "pointer", hex(_addr_for(device, "bus")),
                     _addr_for(device, "bus"), False, True),
            _leaf("vendor", "u16", "0x8086"),
            _leaf("device", "u16", "0x5030"),
        ]
        return node, children

    if last in ("bus", "self"):
        # [한국어] bus -> self -> bus -> self ... 무한 체인. 실제 struct는 pci_bus.self(struct
        #  pci_dev*)처럼 부모/자식을 서로 가리키는 필드가 흔하다. path 길이가 MAX_DEPTH를
        #  넘으면 상위(get_tree_node)에서 이미 걸러지므로 여기선 그냥 계속 같은 모양을 낸다.
        addr = _addr_for(device, last, str(len(path)))
        node = TreeNode(last, "struct pci_bus", "struct", "<struct pci_bus>", addr, False, True)
        children = [
            TreeNode("self", "struct pci_dev *", "pointer", hex(_addr_for(device, "self", str(len(path)))),
                     _addr_for(device, "self", str(len(path))), False, True),
            TreeNode("name", "char[]", "string", "pci0000:00", None, False, False),
        ]
        return node, children

    if last == "subsys":
        node = TreeNode("subsys", "struct nvme_subsystem", "struct", "<struct nvme_subsystem>",
                         _addr_for(device, "subsys"), False, True)
        children = [
            _leaf("instance", "int", "0"),
            # [한국어] 필드명을 "sysdev"로 둔다("dev"는 아래 nvme_queue.dev 백포인터 전용으로
            #  예약 — 실제 struct nvme_queue 에 `struct nvme_dev *dev;` 필드가 있어서 이름이
            #  겹치면 디스패치가 꼬인다).
            TreeNode("sysdev", "struct device *", "pointer", hex(_addr_for(device, "sysdev")),
                     _addr_for(device, "sysdev"), False, True),
        ]
        return node, children

    if last == "sysdev":
        addr = _addr_for(device, "sysdev", str(len(path)))
        node = TreeNode("sysdev", "struct device", "struct", "<struct device>", addr, False, True)
        children = [
            TreeNode("parent", "struct device *", "pointer",
                     hex(_addr_for(device, "sysdev_parent", str(len(path)))),
                     _addr_for(device, "sysdev_parent", str(len(path))), False, True),
            TreeNode("kobj", "struct kobject", "struct", "<struct kobject>",
                     _addr_for(device, "kobj", str(len(path))), False, True),
        ]
        return node, children

    if last == "parent":
        # [한국어] device.parent -> struct device -> .parent -> ... depth-cap 테스트용 순환 체인.
        #  (last를 "sysdev"로 바꿔서 재귀 — 같은 모양이 계속 나온다.)
        return _mock_tree_step(device, path[:-1] + ["sysdev"], topo)

    if last == "kobj":
        addr = _addr_for(device, "kobj", str(len(path)))
        node = TreeNode("kobj", "struct kobject", "struct", "<struct kobject>", addr, False, True)
        children = [TreeNode("name", "const char *", "string", "nvme0", None, False, False)]
        return node, children

    if last == "queues":
        online = topo["online_queues"]
        node = TreeNode("queues", "struct nvme_queue *", "pointer", hex(_addr_for(device, "queues")),
                         _addr_for(device, "queues"), False, True)
        n = min(online, MAX_ARRAY_CHILDREN)
        children = [
            TreeNode(f"[{i}]", "struct nvme_queue", "struct", "<struct nvme_queue>",
                     _addr_for(device, "queues", str(i)), False, True)
            for i in range(n)
        ]
        return node, children

    if last.startswith("[") and last.endswith("]"):
        # [한국어] 이 mock에서 배열 자식은 "queues"에서만 나오므로 상위 세그먼트 확인 없이 바로
        #  nvme_queue 리프로 취급한다.
        idx = int(last[1:-1])
        addr = _addr_for(device, "queues", str(idx))
        node = TreeNode(last, "struct nvme_queue", "struct", "<struct nvme_queue>", addr, False, True)
        depth = topo["depth"]
        t = _tick() + idx * 7
        children = [
            _leaf("qid", "u16", str(idx)),
            _leaf("q_depth", "u16", str(depth)),
            _leaf("sq_tail", "u16", str(t % depth)),
            _leaf("cq_head", "u16", str((t - 3) % depth)),
            TreeNode("dev", "struct nvme_dev *", "pointer", hex(_addr_for(device, "nvme_dev")),
                     _addr_for(device, "nvme_dev"), False, True),  # 루트로 되돌아가는 백포인터
        ]
        return node, children

    if last == "dev":
        # [한국어] struct nvme_queue.dev 백포인터를 눌러서 struct nvme_dev 루트로 돌아온 경우.
        #  루트를 재계산하되 이름만 "dev"(방금 누른 필드명)로 바꿔 보여준다 — 순환 참조 확인용.
        from dataclasses import replace

        root_node, root_children = _mock_tree_step(device, [], topo)
        return replace(root_node, name="dev"), root_children

    # [한국어] 알 수 없는 경로 세그먼트 — 실제 drgn 백엔드라면 LookupError에 해당.
    node = TreeNode(last, "?", "unreadable", f"알 수 없는 필드: {last!r}", None, False, False)
    return node, []
