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
        # [한국어] 프로파일러 대상 규칙/세션 저장소. 테스트가 임시 경로를 줄 수
        # 있게 주입 가능하게 둔다(기본은 실제 데몬과 같은 XDG state 경로).
        self._target_state = target_state
        self._targets = None

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
        topo = self._topology(device)
        online = topo["online_queues"]
        t = _tick()
        queues = []
        for idx in range(1, online):  # admin 큐는 IOPS 개념이 희박해 IO 큐만 합성
            phase = t + idx * 11
            read_iops = float(500 + (phase * 37) % 3000)
            write_iops = float(300 + (phase * 23) % 2000)
            avg_seg = 4096 + (phase % 4) * 12288  # 4KB~52KB 사이를 오가는 평균 전송 크기 흉내
            bw = (read_iops + write_iops) * avg_seg
            lat = float(80 + (phase * 7) % 900)
            # [한국어] percentile은 실제 히스토그램이 없으니 avg 기준으로 꼬리를
            # 흉내낸 배수로 합성한다(p50 < avg < p95 < p99 < p99.9, 실측 eBPF
            # 히스토그램의 전형적인 모양을 대략 재현 — 정확한 분포는 아님).
            queues.append(
                QueuePerf(
                    qid=idx,
                    iops=read_iops + write_iops,
                    read_iops=read_iops,
                    write_iops=write_iops,
                    bandwidth_bytes_per_sec=bw,
                    avg_latency_us=lat,
                    p50_latency_us=lat * 0.85,
                    p95_latency_us=lat * 2.0,
                    p99_latency_us=lat * 4.0,
                    p999_latency_us=lat * 8.0,
                )
            )
        aggregate = None
        if queues:
            n = len(queues)
            total_iops = sum(q.iops for q in queues)
            total_read = sum(q.read_iops for q in queues)
            total_write = sum(q.write_iops for q in queues)
            total_bw = sum(q.bandwidth_bytes_per_sec for q in queues)
            aggregate = QueuePerf(
                qid=-1,
                iops=total_iops,
                read_iops=total_read,
                write_iops=total_write,
                bandwidth_bytes_per_sec=total_bw,
                avg_latency_us=sum(q.avg_latency_us for q in queues) / n,
                p50_latency_us=sum(q.p50_latency_us for q in queues) / n,
                p95_latency_us=sum(q.p95_latency_us for q in queues) / n,
                p99_latency_us=sum(q.p99_latency_us for q in queues) / n,
                p999_latency_us=sum(q.p999_latency_us for q in queues) / n,
            )
        return DevicePerf(device=device, queues=queues, available=True, aggregate=aggregate)

    def get_events(self, device: str) -> List[NvmeEvent]:
        # [한국어] 타임아웃/리셋 같은 이벤트는 정상적으로는 거의 안 일어나는
        # 것들이라 mock에서 합성하지 않는다(실제로 일어난 것처럼 흉내내는 게
        # 오히려 오해를 살 수 있음) — 파싱/봉투 구성 로직 자체는
        # test_ebpf_timeout_events.py가 실제 로그 포맷을 그대로 흉내낸
        # 텍스트로 검증한다. mock에서는 "이벤트 없음"이 정상 상태고, UI/CLI가
        # 빈 목록을 어떻게 그리는지도 이걸로 확인된다.
        self._topology(device)  # DeviceNotFoundError를 다른 get_* 메서드들과 똑같이 내게
        return []

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        # [한국어] get_events와 같은 이유로 합성하지 않는다 — 안 난 에러를
        # 난 것처럼 보여주면 안 된다. "수집은 되지만 0건"으로 응답한다.
        self._topology(device)
        return DeviceErrorStats(device=device, counts=[], total=0, available=True)

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

        for device, topo in _TOPOLOGY.items():
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
                         TopologyDetail(key="struct pci_dev", value=hex(_addr_for(device, "pci_dev")))],
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
                         TopologyDetail(key="struct nvme_dev", value=hex(_addr_for(device, "nvme_dev")))],
                children=[subsys, ns, queues],
            ))
        return Topology(root=root, backend_kind="mock")

    # ---- NVMe I/O 프로세스 프로파일러 (합성 데이터) -----------------------

    def _registry(self):
        from telemetryd.backend.targets import TargetRegistry

        if self._targets is None:
            self._targets = TargetRegistry(self._target_state)
        return self._targets

    def _mock_processes(self) -> List[ProcessInfo]:
        """대상 선택 화면을 root/게스트 없이 눌러볼 수 있게 만든 합성 프로세스들.

        의도적으로 서로 다른 성격을 섞는다: 어댑터가 붙는 fio 2개(옵션이 다름),
        어댑터가 없는 일반 프로세스, 그리고 선택 불가여야 하는 커널 스레드."""
        return [
            ProcessInfo(pid=4821, comm="ioworker", cmdline="./ioworker --threads 4",
                        exe_path="/home/user/bin/ioworker", uid=1000, start_time_ns=111_000,
                        thread_count=4,
                        threads=[(4821, "ioworker"), (4822, "worker_00"),
                                 (4823, "worker_01"), (4824, "worker_02")]),
            ProcessInfo(pid=5102, comm="fio", uid=1000, start_time_ns=222_000,
                        exe_path="/usr/bin/fio", thread_count=1, threads=[(5102, "fio")],
                        cmdline="fio --name=randwrite --rw=randwrite --bs=4k --iodepth=32 "
                                "--numjobs=4 --ioengine=io_uring --direct=1 "
                                "--filename=/dev/nvme1n1"),
            ProcessInfo(pid=3390, comm="fio", uid=1000, start_time_ns=333_000,
                        exe_path="/usr/bin/fio", thread_count=1, threads=[(3390, "fio")],
                        cmdline="fio --name=seqread --rw=read --bs=128k --iodepth=8 "
                                "--ioengine=libaio --direct=1 --filename=/dev/nvme0n1"),
            ProcessInfo(pid=9, comm="kworker/0:1", thread_count=1, threads=[(9, "kworker/0:1")],
                        error="커널 스레드(mm 없음) — cmdline/exe 없음"),
        ]

    def _mock_proc_stats(self) -> List[ProcessIoStat]:
        """합성 I/O 통계. seqread(3390)은 기대 128k인데 실측 4k로 두어, 어댑터의
        기대값 대조가 **불일치를 실제로 잡아내는 것**을 mock에서도 보여준다
        (명세 3-2의 핵심 가치 — bio 분할/max_sectors_kb 제한 상황)."""
        return [
            ProcessIoStat(device="nvme0", pid=4821, comm="ioworker", iops=2840.0,
                          read_iops=1200.0, write_iops=1640.0, bandwidth_bps=46_530_560.0,
                          avg_latency_us=118.0, io_size_dominant=16384,
                          io_size_hist=[(16384, 2840)], queues=[(1, 1400), (2, 1440)],
                          queue_depth_est=0.34, seq_ratio=0.9,
                          threads=[ThreadIoStat(tid=4822, comm="worker_00", iops=1420.0),
                                   ThreadIoStat(tid=4823, comm="worker_01", iops=1420.0)]),
            ProcessIoStat(device="nvme1", pid=5102, comm="fio", iops=1120.0,
                          read_iops=0.0, write_iops=1120.0, bandwidth_bps=4_587_520.0,
                          avg_latency_us=28_500.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 1120)], queues=[(3, 1120)],
                          queue_depth_est=31.9, seq_ratio=0.02,
                          threads=[ThreadIoStat(tid=5102, comm="fio", iops=1120.0)]),
            ProcessIoStat(device="nvme0", pid=3390, comm="fio", iops=640.0,
                          read_iops=640.0, write_iops=0.0, bandwidth_bps=2_621_440.0,
                          avg_latency_us=12_500.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 640)], queues=[(4, 640)],
                          queue_depth_est=8.0, seq_ratio=0.95,
                          threads=[ThreadIoStat(tid=3390, comm="fio", iops=640.0)]),
            ProcessIoStat(device="nvme0", pid=9, comm="kworker/0:1", iops=12.0,
                          read_iops=0.0, write_iops=12.0, bandwidth_bps=49_152.0,
                          avg_latency_us=800.0, io_size_dominant=4096,
                          io_size_hist=[(4096, 12)], queues=[(1, 12)],
                          queue_depth_est=0.01, seq_ratio=0.5, threads=[]),
        ]

    def list_processes(self, only_io: bool = False) -> List[ProcessListEntry]:
        from telemetryd.backend.targets import rule_matches

        io_by_pid = {}
        for st in self._mock_proc_stats():
            e = io_by_pid.setdefault(st.pid, {"rate": 0.0, "devices": set()})
            e["rate"] += st.iops
            e["devices"].add(st.device)
        rules = self._registry().rules
        out = []
        for proc in self._mock_processes():
            io = io_by_pid.get(proc.pid)
            matched = next((r for r in rules if rule_matches(r, proc)), None)
            entry = ProcessListEntry(
                info=proc, io_active=bool(io and io["rate"] > 0),
                io_rate=io["rate"] if io else 0.0,
                target_devices=sorted(io["devices"]) if io else [],
                is_target=matched is not None,
                matched_rule=f"{matched.kind}={matched.value}" if matched else None,
            )
            if proc.error and "커널 스레드" in proc.error:
                entry.selectable = False
                entry.unselectable_reason = "커널 스레드 — 프로파일 대상이 아님"
            out.append(entry)
        if only_io:
            out = [e for e in out if e.io_active]
        out.sort(key=lambda e: (-e.io_rate, e.info.pid))
        return out

    def list_targets(self) -> List[TargetRule]:
        return list(self._registry().rules)

    def add_target(self, rule: TargetRule) -> List[TargetRule]:
        self._registry().add_rule(rule)
        return list(self._registry().rules)

    def remove_target(self, kind: str, value: str) -> List[TargetRule]:
        self._registry().remove_rule(kind, value)
        return list(self._registry().rules)

    def get_profile(self) -> ProfileSnapshot:
        return self._registry().refresh(self._mock_processes(), self._mock_proc_stats())

    def list_event_kinds(self) -> List[EventKindInfo]:
        # [한국어] 등록 목록 자체는 백엔드와 무관한 시스템 속성이라 mock도 같은
        # 목록을 준다. 다만 mock에는 eBPF 수집기가 없으므로 active=False —
        # "등록은 돼 있지만 지금 수집되진 않는다"가 화면에 그대로 드러난다.
        from telemetryd.backend.event_registry import registered_event_kinds

        return registered_event_kinds(active=False)


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
