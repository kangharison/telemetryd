"""telemetryd 핵심 데이터 모델.

MockBackend/DrgnBackend 둘 다 이 dataclass들만 돌려준다. gRPC 계층
(grpcserver/convert.py)과 ffi 계층(ffi.py)은 여기 정의된 구조를 각각
protobuf 메시지/JSON으로 직렬화만 한다 — 실제 조회 로직은 backend/*.py 에만
있다. 상위 계층(CLI/Web/C++)은 백엔드가 mock이든 drgn이든 이 모델만 보고
동작하므로 수정 없이 재사용된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class QueueSnapshot:
    """struct nvme_queue 1개 스냅샷 (요청사항 1: sq_tail/cq_head/inflight)."""

    index: int                 # dev.queues[] 배열 인덱스
    qid: int                   # 실제 qid (0 = Admin)
    is_admin: bool
    depth: int                 # q_depth
    sq_tail: int
    cq_head: int
    sq_dma_addr: int
    cq_dma_addr: int
    hctx_index: Optional[int]  # Admin 큐는 blk-mq hctx가 없으므로 None
    inflight_driver: int       # request_queue_busy_iter(q, "driver") 카운트
    inflight_sched: int        # request_queue_busy_iter(q, "sched") 카운트


@dataclass
class DeviceSnapshot:
    """struct nvme_dev 1개 요약 (요청사항 6의 "버튼" 대상 최상위 노드)."""

    name: str                  # 사용자 지정 키, 예: "nvme0"
    addr: int                  # struct nvme_dev* 커널 가상주소
    model: str
    online_queues: int
    allocated_queues: int
    bar_addr: int               # MMIO 주소 값 (내용은 못 읽음)
    dbs_addr: int
    iommu_enabled: bool
    backend_kind: str            # "mock" | "drgn"
    queues: List[QueueSnapshot] = field(default_factory=list)


@dataclass
class QueueEntry:
    """SQ 엔트리 1개 — 요청사항 2: CDW 필드 전체."""

    index: int
    cid: int
    opcode: int
    opcode_name: str
    nsid: int
    flags: int
    uses_sgl: bool              # PSDT != 0
    cdw2: int
    cdw3: int
    cdw10: int
    cdw11: int
    cdw12: int
    cdw13: int
    cdw14: int
    cdw15: int
    prp1: int
    prp2: int


@dataclass
class CompletionEntry:
    """CQ 엔트리 1개 — cq_head(도어벨) 기준 최근 N개 조회용.

    struct nvme_completion { union nvme_result result; __le16 sq_head; __le16
    sq_id; __u16 command_id; __le16 status; }. status 필드는 bit0=phase(P),
    bits[15:1]에 SCT/SC/M/DNR이 실려 있어 여기서 미리 풀어서 담는다.
    """

    index: int              # CQ 링 안에서의 절대 인덱스(0..depth-1)
    command_id: int
    sq_id: int
    sq_head: int             # 완료 시점의 SQ 소비 위치(드라이버가 free space 계산에 씀)
    status_raw: int          # 원본 status 필드(phase 포함)
    phase: bool               # status 비트0 — 현재 phase tag와 일치해야 "새 완료"
    status_code: int          # SC (bits 8:1)
    status_code_type: int     # SCT (bits 11:9)
    result: int                # DW0 (command-specific result, u32)


@dataclass
class PrpPage:
    """PRP가 가리키는 데이터(또는 리스트) 페이지 1개 — 요청사항 3."""

    phys_addr: int
    offset_in_page: int
    data: bytes                 # 최대 4096바이트
    is_list_page: bool = False


@dataclass
class PrpPayload:
    device: str
    qid: int
    cid: int
    uses_sgl: bool
    total_len: int
    pages: List[PrpPage] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class TreeNode:
    """포인터 트리의 노드 1개 요약 — 요청사항 4."""

    name: str                   # 필드명 / "[idx]" / 루트면 디바이스명
    type_name: str
    kind: str                   # "struct"|"pointer"|"array"|"scalar"|"string"|"unreadable"
    value_repr: str
    address: Optional[int]
    is_null: bool
    expandable: bool


@dataclass
class TreeExpansion:
    """GetTreeNode 응답 — 요청한 노드 + 바로 다음 자식들(lazy 1-depth)."""

    node: TreeNode
    children: List[TreeNode] = field(default_factory=list)
    depth: int = 0
    error: Optional[str] = None


def h(n: Optional[int]) -> str:
    """정수를 hex 문자열로 (None이면 빈 문자열). CLI/JSON 출력 공통 헬퍼."""
    return "" if n is None else hex(n)


@dataclass
class QueuePerf:
    """큐 1개의 실시간 성능 스냅샷 — eBPF(nvme:nvme_setup_cmd/nvme_complete_rq)로
    1초 간격 집계한 최신 값(DESIGN.md §6 "eBPF = 저오버헤드 카운터" 역할의
    실제 구현, ebpf/nvme_perf.bt + backend/ebpf_perf.py)."""

    qid: int
    iops: float                     # read_iops + write_iops
    read_iops: float
    write_iops: float
    bandwidth_bytes_per_sec: float  # read+write 합산
    avg_latency_us: float           # nvme_setup_cmd~nvme_complete_rq 평균(마이크로초)
    # p50/p95/p99/p99.9(QoS "nine" 표기) — bpftrace hist()의 2배수 버킷 상한을
    # percentile 근사치로 씀(bcc의 biolatency/runqlat과 같은 표준 방식). 정확한
    # 값이 아니라 "이 값 이하에 해당 비율의 요청이 들어간 버킷의 상한"이다.
    p50_latency_us: float = 0.0
    p95_latency_us: float = 0.0
    p99_latency_us: float = 0.0
    p999_latency_us: float = 0.0


@dataclass
class DevicePerf:
    """디바이스 1개의 큐별 성능 스냅샷 목록 + 전체 큐 합산 행."""

    device: str
    queues: List[QueuePerf] = field(default_factory=list)
    available: bool = True          # eBPF 수집기가 안 떠 있거나 로그가 없으면 False
    error: Optional[str] = None
    # 이 디바이스의 모든 큐(admin 포함, 실제로 활동이 있었던 큐만) 히스토그램을
    # 합쳐서 계산한 전체 percentile/iops/bw — qid는 의미 없음(-1로 채움).
    aggregate: Optional[QueuePerf] = None


@dataclass
class TimeoutEventDetail:
    """kind == "timeout" 이벤트의 **종류별 상세** — kprobe:nvme_timeout 전용 필드.

    타임아웃 시점엔 원본 커맨드가 struct request에서 직접 안 보여서(제출 때
    이미 SQ에 쓰인 뒤라 IOD 등엔 안 남음), nvme_setup_cmd 때 (ctrl,qid,tag)
    키로 캐시해 둔 CDW를 타임아웃 순간 조회해서 만든다 — 그래서 opcode/nsid/
    flags/cdw10-15는 "타임아웃난 그 커맨드가 제출될 때 실제로 가졌던 값"이고,
    나중에 drgn으로 다시 조회한 값이 아니다(그 시점엔 이미 재시도/리셋으로
    없어졌을 수 있어 신뢰 못 함). DESIGN.md §9.11.

    이 필드들은 **타임아웃에만 의미가 있다** — 리셋/AER 같은 다른 종류의
    이벤트는 tag도 cdw도 없다. 그래서 공통 봉투(NvmeEvent)가 아니라 여기
    종류별 상세로 분리해 둔다.
    """

    tag: int              # blk-mq 태그(req->tag). NVMe cid의 하위 12비트와 같음
    opcode: int
    opcode_name: str
    nsid: int
    flags: int
    cdw10: int
    cdw11: int
    cdw12: int
    cdw13: int
    cdw14: int
    cdw15: int
    elapsed_us: float     # 커맨드 제출~타임아웃 감지까지 걸린 시간


@dataclass
class ErrorEventDetail:
    """kind == "error" 이벤트의 종류별 상세 — 에러 status로 반환된 커맨드 1건
    (tracepoint:nvme:nvme_complete_rq에서 status != 0인 완료).

    왜 잡는가: 디바이스 이상 징후 중 타임아웃까지 도달하는 건 극히 일부다.
    대부분은 그 전에 에러 status로 반환되고 nvme 코어/blk-mq가 재시도로
    흡수해버려서(nvme_retry_req) 애플리케이션에서는 아무 일도 없었던 것처럼
    보인다 — 그래서 완료 경로에서 직접 세지 않으면 영영 안 보인다.

    status는 phase 비트가 제거된 값이다(드라이버의 nvme_req(req)->status
    그대로 — nvme_const.decode_status 독스트링 참고). sct/sc/dnr/more/crd는
    거기서 분해한 것이고, sct_name/sc_name은 사람이 읽을 이름이다.

    slba/nlb는 완료 이벤트에 없어서 제출 시점(nvme_setup_cmd)에 캐시해 둔
    CDW에서 복원한다 — read/write가 아니거나(lba_valid=False) 제출을 못 본
    커맨드(submit_cached=False)면 의미 없는 값이므로 표시하면 안 된다."""

    cid: int              # NVMe command id (genctr<<12 | tag)
    tag: int              # blk-mq 태그 = cid 하위 12비트
    opcode: int
    opcode_name: str
    nsid: int
    status: int           # phase 제거된 원본 status (SCT|SC|DNR|More|CRD 포함)
    sct: int              # bits[10:8]
    sc: int               # bits[7:0]
    sct_name: str
    sc_name: str
    dnr: bool             # bit[14] — 재시도해도 소용없음(상위가 흡수 못 하는 실패)
    more: bool            # bit[13] — 추가 정보가 에러 로그 페이지에 있음
    crd: int              # bits[12:11] — Command Retry Delay
    retries: int          # 커널이 이 요청을 이미 몇 번 재시도했는지(nvme_req->retries)
    slba: int             # read/write일 때만 유효
    nlb: int              # 실제 블록 수(0-based NLB + 1). read/write일 때만 유효
    lba_valid: bool       # opcode가 read/write라서 slba/nlb가 의미 있는가
    submit_cached: bool   # 제출(nvme_setup_cmd)을 봤는가 — False면 opcode/nsid/slba 전부 미상
    elapsed_us: float     # 제출~완료 (submit_cached=False면 0)


@dataclass
class NvmeEvent:
    """NVMe 드라이버에서 발생한 이벤트 1건의 **종류 무관 공통 봉투(envelope)**.

    이벤트는 타임아웃만 있는 게 아니다 — 컨트롤러 리셋(nvme_reset_ctrl),
    AER, 네임스페이스 변경 등 성격이 완전히 다른 것들이 같은 목록에 섞여
    들어온다. 그래서 목록/API/UI 어디서도 특정 종류(지금은 timeout)를
    대표로 삼아 컬럼을 고정하지 않는다 — 목록은 여기 공통 필드
    (kind/observed_at/device/qid/summary)만으로 나열하고, 종류별 필드는
    아래 detail 슬롯(kind에 대응하는 것 **하나만** 채워짐)에서 그 종류
    전용 렌더러가 따로 포매팅한다.

    현재 채워지는 kind는 "timeout" 하나뿐이다(ebpf/nvme_perf.bt의
    kprobe:nvme_timeout). 새 종류를 추가할 때 손댈 곳은 (1) 그 종류의
    Detail dataclass, (2) 여기 Optional 슬롯 하나, (3) proto의 oneof
    멤버, (4) UI/CLI의 종류별 렌더러 — 목록/스트림/탭 구조 자체는 그대로다.
    모르는 kind가 와도 소비자는 summary만으로 한 줄을 그릴 수 있어야 한다
    (그게 이 봉투에 summary를 둔 이유).
    """

    kind: str            # "timeout" | (향후) "reset" 등 — 소문자 식별자
    observed_at: float   # 호스트가 이 이벤트를 로그에서 읽은 시각(epoch 초) — 발생 시각의 근사
    device: str          # "nvme0" 등
    qid: int             # 관련 큐. 큐와 무관한 종류면 -1
    summary: str         # 종류를 몰라도 목록 한 줄로 뿌릴 수 있는 요약문(생산자가 만든다)
    timeout: Optional[TimeoutEventDetail] = None   # kind == "timeout"일 때만 채워짐
    error: Optional[ErrorEventDetail] = None       # kind == "error"일 때만 채워짐


@dataclass
class ErrorStatusCount:
    """(SCT, SC) 조합 하나의 누적 발생 횟수 — 이벤트 "목록"과 별개로 유지하는
    집계다.

    이벤트 줄은 로그 폭주를 막으려고 초당 인쇄 예산(nvme_perf.bt의 @err_budget)
    으로 샘플링될 수 있지만, 이 카운터(@err_count)는 예산과 무관하게 전부
    센다 — "각 건의 상세"는 놓칠 수 있어도 "몇 건 났는지"는 정확하다.
    수집기(bpftrace)가 뜬 시점부터의 누적이며, 수집기를 재시작하면 0부터 다시
    센다."""

    sct: int
    sc: int
    sct_name: str
    sc_name: str
    count: int


@dataclass
class DeviceErrorStats:
    """디바이스 1개의 SCT/SC 조합별 누적 카운터 표."""

    device: str
    counts: List[ErrorStatusCount] = field(default_factory=list)
    total: int = 0
    available: bool = True     # 수집기가 안 떠 있거나 로그가 없으면 False
    error: Optional[str] = None


@dataclass
class EventKindInfo:
    """이 시스템에 **등록된** 이벤트 종류 1개의 메타데이터.

    "지금 어떤 이벤트를 잡고 있는가"를 UI/CLI가 하드코딩하지 않고 물어볼 수
    있게 하려는 것 — 종류가 늘어날 때 화면 문구를 따로 고치지 않아도 되고,
    수집기 설정이 빠져 있으면(active=False) 그 사실이 그대로 드러난다."""

    kind: str          # "timeout" | "error" | ...
    label: str         # 사람이 읽는 이름
    source: str        # 어디서 오는가 (예: "eBPF kprobe:nvme_timeout")
    description: str   # 무엇을 잡는가 / 왜 보는가
    active: bool       # 이 백엔드에서 실제로 수집 중인가(수집기 로그 경로 설정 여부 등)


@dataclass
class TopologyDetail:
    """토폴로지 노드 1개의 속성 한 줄(key: value).

    노드 종류마다 보여줄 필드가 완전히 달라서(PCIe 엔드포인트는 BDF/링크,
    네임스페이스는 nsid/용량) 종류별 필드를 모델에 박지 않고 key-value 목록으로
    둔다 — 이벤트 봉투(NvmeEvent)에서 쓴 것과 같은 방침이라, 새 노드 종류를
    추가해도 gRPC/REST/UI를 안 고쳐도 된다."""

    key: str
    value: str


@dataclass
class TopologyNode:
    """PCIe 토폴로지 + NVMe 서브시스템을 **하나로 합친 트리**의 노드.

    이 트리의 핵심은 두 세계가 한 줄기로 이어진다는 것이다:

        PCI 호스트 브리지 → (브리지/스위치들) → PCIe 엔드포인트(BDF)
            → struct nvme_dev/nvme_ctrl   ← 여기서 PCIe와 NVMe가 만난다
                → nvme_subsystem (subnqn/model/serial, 같은 서브시스템의 컨트롤러들)
                → 네임스페이스 (nvme0n1, nsid, LBA 크기, 용량)
                → 큐 (admin + I/O, depth/도어벨)

    lazy 확장인 treewalk.py의 포인터 트리와는 목적이 다르다 — 저건 "구조체
    필드를 그대로 따라가는" 범용 탐색이고, 이건 "장치가 시스템에 어떻게 붙어
    있는가"를 의미 단위로 재구성한 뷰다. 그래서 한 번에 전부 만들어 보낸다
    (노드 수가 수십 개 수준이라 lazy가 필요 없다).
    """

    id: str                 # 안정적인 노드 키 (예: "pci:0000:00:04.0", "ns:nvme0n1")
    kind: str               # 아래 KIND 상수 참고 — UI가 색/아이콘을 고르는 데만 씀
    label: str              # 한 줄 제목 (예: "0000:00:04.0")
    sublabel: str = ""      # 부가 설명 (예: "NVMe 컨트롤러 (8086:5845)")
    device: str = ""        # 이 노드가 속한 telemetryd 디바이스명("nvme0") — 선택 강조용
    details: List[TopologyDetail] = field(default_factory=list)
    children: List["TopologyNode"] = field(default_factory=list)


#: TopologyNode.kind 값들. UI/CLI는 모르는 kind를 만나도 label/sublabel/details만
#: 으로 그릴 수 있어야 한다(이벤트 kind와 같은 규약).
TOPO_SYSTEM = "system"
TOPO_HOST_BRIDGE = "host_bridge"
TOPO_PCI_BRIDGE = "pci_bridge"
TOPO_PCI_ENDPOINT = "pci_endpoint"
TOPO_NVME_CTRL = "nvme_ctrl"
TOPO_NVME_SUBSYS = "nvme_subsystem"
TOPO_NAMESPACE = "namespace"
TOPO_QUEUE_GROUP = "queue_group"
TOPO_QUEUE = "queue"


@dataclass
class Topology:
    """GetTopology 응답 — 통합 트리 1개."""

    root: TopologyNode
    backend_kind: str = ""
    error: Optional[str] = None


# ===========================================================================
# [한국어] NVMe I/O 프로세스 프로파일러 (범용 — 대상은 런타임에 선택한다).
#
# 설계 원칙(명세): 대상을 코드/설정에 하드코딩하지 않는다, 여러 프로세스를 동시에
# 관측한다, 애플리케이션 사전 지식 없이도 기본 관측이 동작한다, 앱별 특성은
# 어댑터로 분리한다. 아래 모델들은 그 원칙을 데이터 구조로 굳힌 것이다 —
# 코어에는 어떤 애플리케이션 이름도 등장하지 않는다.
# ===========================================================================


@dataclass
class ThreadIoStat:
    """스레드 1개의 I/O 발행량(최근 1초). 논리 그룹(fio job 등) 매핑의 기본 단위."""

    tid: int
    comm: str
    iops: float


@dataclass
class ProcessIoStat:
    """(장치, 프로세스) 조합 1개의 최근 1초 I/O 통계 — eBPF 탐색 모드 결과.

    한 프로세스가 여러 장치를 때리면 장치마다 한 항목이 나온다(어느 장치를
    쓰는지가 대상 선택의 핵심 정보라 합치지 않는다)."""

    device: str
    pid: int              # TGID
    comm: str
    iops: float
    read_iops: float
    write_iops: float
    bandwidth_bps: float
    avg_latency_us: float
    io_size_dominant: int             # 가장 많이 나온 I/O 크기(바이트) — 기대 bs 대조용
    io_size_hist: List[tuple] = field(default_factory=list)   # [(bytes, count)]
    queues: List[tuple] = field(default_factory=list)          # [(qid, count)] — 큐 공유 분석
    # [한국어] 리틀의 법칙(IOPS × 평균지연) 근사. 제출/완료가 다른 컨텍스트라
    # 실제 재고를 직접 셀 수 없어서 추정값이며, fio --iodepth 대조용으로 쓴다.
    queue_depth_est: float = 0.0
    # [한국어] 직전 I/O 끝 LBA와 이번 시작 LBA가 이어진 비율 — 순차/랜덤 판정용.
    # 멀티스레드 프로세스가 여러 영역을 동시에 다루면 실제보다 랜덤하게 보이는
    # 근사치다(eBPF가 프로세스 단위로 직전 위치를 들고 있어서).
    seq_ratio: Optional[float] = None
    threads: List[ThreadIoStat] = field(default_factory=list)


@dataclass
class ProcessInfo:
    """프로세스 인벤토리 1건 — 대상 선택 화면(명세 1-2)의 후보.

    이 프로젝트의 관측 대상은 QEMU 게스트 안에서 도는 프로세스라 호스트의
    /proc으로는 볼 수 없다. 그래서 명세의 /proc 필드들을 **커널 task_struct**
    에서 직접 읽는다(drgn) — 필드 대응은 backend/procinfo.py 참고. 호스트
    라이브(sudo drgn) 환경에서도 같은 코드가 그대로 동작한다."""

    pid: int                 # TGID
    comm: str
    cmdline: str = ""        # 전체 인자(공백 결합). fio는 여기에 워크로드 정의가 전부 들어 있다
    exe_path: str = ""
    uid: int = -1
    start_time_ns: int = 0   # PID 재사용 구분용(task.start_boottime)
    thread_count: int = 0
    threads: List[tuple] = field(default_factory=list)   # [(tid, comm)]
    state: str = ""
    # [한국어] 읽기 실패(권한/경합)는 예외로 터뜨리지 않고 여기 사유만 남긴다
    # (명세 7-2/7-3: 해당 필드만 비우고 전체 수집은 계속한다).
    error: Optional[str] = None


@dataclass
class TargetRule:
    """대상 선택 규칙 1개. 모든 선택 방식이 이 규칙으로 수렴한다(명세 1-1).

    kind: "pid" | "name" | "name_pattern" | "cmdline_pattern"
    이름/패턴 규칙은 프로세스가 죽어도 유지되고, 다음 실행 때 자동으로 다시
    붙는다(명세 1-4) — 그래서 규칙과 세션을 분리해서 저장한다."""

    kind: str
    value: str
    adapter: Optional[str] = None    # 명시적 어댑터 지정(없으면 자동 선택)


@dataclass
class WorkloadSpec:
    """어댑터가 알아낸 **의도된** 워크로드 — 실측과 대조할 기준값(명세 3-2)."""

    io_size: Optional[int] = None
    rw: Optional[str] = None
    pattern: Optional[str] = None        # sequential | random
    queue_depth: Optional[int] = None
    ioengine: Optional[str] = None
    direct: Optional[bool] = None
    filename: Optional[str] = None
    numjobs: Optional[int] = None
    runtime_sec: Optional[int] = None


@dataclass
class MeasuredWorkload:
    """eBPF로 실제 관측된 워크로드."""

    io_size_dominant: int = 0
    read_ratio: float = 0.0
    write_ratio: float = 0.0
    queue_depth_avg: float = 0.0
    iops: float = 0.0
    bandwidth_bps: float = 0.0


@dataclass
class LogicalGroup:
    """스레드를 묶은 논리 단위(fio job, 시나리오, worker pool 등) — 어댑터가 만든다."""

    name: str
    type: str                 # "fio_job" | "thread_prefix" | "process" | ...
    source: str               # "cmdline_parse" | "comm_prefix" | "inferred" | "usdt" ...
    thread_tids: List[int] = field(default_factory=list)
    expected_workload: Optional[WorkloadSpec] = None
    measured_workload: Optional[MeasuredWorkload] = None
    expectation_match: Optional[bool] = None      # 기대값이 없으면 None(판단 불가)
    mismatch_reasons: List[str] = field(default_factory=list)
    inferred: bool = False    # 관측으로 "추정"한 정보면 True (명세 3-4: 확정 정보와 구분)
    progress_pct: Optional[float] = None


@dataclass
class Session:
    """관측 단위(명세 PART 4). 대상 프로세스 하나의 생애가 곧 세션 하나다.

    PID 재사용에 대비해 (pid, start_time_ns)로 식별한다. 프로세스가 끝나도
    세션은 finished 상태로 남아 데이터가 보존된다 — "이 결과는 어떤 조건이었나"에
    답할 유일한 근거인 cmdline을 통째로 들고 있는다."""

    session_id: str
    pid: int
    comm: str
    cmdline: str
    exe_path: str = ""
    adapter: str = "generic"
    status: str = "active"          # active | finished
    start_time_ns: int = 0          # 프로세스 시작(task.start_boottime)
    session_start_ns: int = 0       # 관측 시작(epoch ns)
    session_end_ns: Optional[int] = None
    devices: List[str] = field(default_factory=list)
    thread_count_active: int = 0
    aggregate: Optional[MeasuredWorkload] = None
    logical_groups: List[LogicalGroup] = field(default_factory=list)
    matched_rule: Optional[str] = None   # 어떤 규칙으로 붙었는지(디버깅/설명용)


@dataclass
class UnmonitoredIo:
    """관측 대상이 아닌데 같은 장치에 I/O를 내고 있는 프로세스(명세 2-2/5-2).

    성능 측정 신뢰도에 직결되는 정보라 숨기지 않고 그대로 올린다."""

    pid: int
    comm: str
    device: str
    io_rate: float
    note: str = "관측 대상 아님. 세션 집계에 포함되지 않음"


@dataclass
class DeviceAttribution:
    """장치 1개의 I/O를 세션에 귀속시킨 결과 + 미귀속분."""

    name: str
    total_iops: float = 0.0
    attributed_iops: float = 0.0
    unattributed_iops: float = 0.0
    contributing_sessions: List[str] = field(default_factory=list)
    multi_process_warning: bool = False


@dataclass
class ProfileSnapshot:
    """프로파일러 스냅샷 — 명세 PART 6의 schema 2.0에 대응."""

    schema_version: str = "2.0"
    collected_at_ns: int = 0
    sessions: List[Session] = field(default_factory=list)
    unmonitored_io: List[UnmonitoredIo] = field(default_factory=list)
    devices: List[DeviceAttribution] = field(default_factory=list)
    rules: List[TargetRule] = field(default_factory=list)
    available: bool = True
    error: Optional[str] = None


@dataclass
class ProcessListEntry:
    """대상 선택 화면의 한 줄(명세 1-2). 프로세스 정보 + 관측된 I/O 활동.

    io_active/io_rate가 이 목록의 핵심이다 — 시스템 전체 프로세스는 수백 개라
    고르기 어렵지만 NVMe I/O를 실제로 내는 프로세스는 보통 한 자릿수라, 그
    기준으로 거르면 대상을 모르는 상태에서도 시작할 수 있다."""

    info: ProcessInfo
    io_active: bool = False
    io_rate: float = 0.0                                   # 최근 1초 IOPS 합
    target_devices: List[str] = field(default_factory=list)  # 어느 nvme를 때리는지
    is_target: bool = False                                 # 이미 관측 대상인가
    matched_rule: Optional[str] = None
    selectable: bool = True                                 # 권한 등으로 선택 불가면 False
    unselectable_reason: Optional[str] = None
