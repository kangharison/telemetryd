"""Backend 추상 인터페이스.

CLI/gRPC서버/Web은 이 Protocol 하나만 바라본다. MockBackend(root 불필요,
테스트용)와 DrgnBackend(실제 라이브 커널, root 필요) 둘 다 이 인터페이스를
구현하므로 상위 계층 코드는 백엔드 종류를 몰라도 된다 — DESIGN.md §2 계층 원칙.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from telemetryd.models import (
    CompletionEntry,
    DeviceErrorStats,
    DevicePerf,
    DeviceSnapshot,
    EventKindInfo,
    NvmeEvent,
    PrpPayload,
    ProcessListEntry,
    ProfileSnapshot,
    QueueEntry,
    TargetRule,
    Topology,
    TreeExpansion,
)


class DeviceNotFoundError(KeyError):
    """요청한 디바이스 이름이 list_devices() 결과에 없을 때."""


class QueueNotFoundError(KeyError):
    """요청한 qid가 해당 디바이스의 dev.queues[] 범위 밖일 때."""


@runtime_checkable
class Backend(Protocol):
    kind: str  # "mock" | "drgn"

    def list_devices(self) -> List[str]:
        """등록된 컨트롤러 이름 목록 (예: ["nvme0", "nvme1"])."""
        ...

    def get_device_snapshot(self, device: str) -> DeviceSnapshot:
        """struct nvme_dev 요약 + 큐 목록(sq_tail/cq_head/inflight 포함)."""
        ...

    def get_queue_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[QueueEntry]:
        """특정 큐 SQ 엔트리(cdw 필드 포함).

        around_doorbell=True(기본)면 sq_tail 도어벨 바로 앞 limit개 — 가장
        최근 제출된 커맨드들(링 wrap 처리). False면 인덱스 0부터
        limit개(0=큐 depth 전체, 옛 CLI 호환용)."""
        ...

    def get_completion_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[CompletionEntry]:
        """특정 큐 CQ 엔트리. around_doorbell=True(기본)면 cq_head 도어벨 바로
        앞 limit개 — 가장 최근 소비된 완료들. False면 인덱스 0부터."""
        ...

    def get_prp_payload(self, device: str, qid: int, cid: int) -> PrpPayload:
        """cid에 해당하는 커맨드의 PRP 페이지들(최대 4096B/페이지) hexdump 원본."""
        ...

    def get_tree_node(self, device: str, path: List[str]) -> TreeExpansion:
        """struct nvme_dev 루트에서 path를 따라간 노드 + 바로 다음 자식(lazy)."""
        ...

    def get_performance(self, device: str) -> DevicePerf:
        """eBPF(nvme:nvme_setup_cmd/nvme_complete_rq)로 집계한 큐별 IOPS/대역폭/
        평균 레이턴시 최신 1초 스냅샷(DESIGN.md §6/§9.5). 수집기가 안 떠 있으면
        DevicePerf.available=False + error 메시지."""
        ...

    def get_events(self, device: str) -> List[NvmeEvent]:
        """이 디바이스에서 관측된 최근 NVMe 이벤트들(발생순, 최대 최근 200개).

        **종류를 가리지 않는 목록**이다 — 지금은 eBPF kprobe:nvme_timeout이
        잡은 kind="timeout"만 들어오지만, 컨트롤러 리셋/AER 같은 다른 종류가
        추가되면 같은 목록에 섞여 나온다. 그래서 반환 타입도 종류별 구조체가
        아니라 공통 봉투 NvmeEvent이고, 종류별 필드는 그 안의 detail 슬롯에만
        있다(models.NvmeEvent 참고). 한 건도 없었으면 빈 리스트."""
        ...

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        """에러 완료(kind="error")의 **SCT/SC 조합별 누적 카운터**.

        이벤트 목록과 성격이 달라 별도 메서드다 — 목록은 "최근 무슨 일이
        있었나"(개별 건, 링버퍼 200개, 폭주 시 샘플링됨)이고, 이건 "여태
        어떤 에러가 몇 번"(전수 집계, 수집기 시작 이후 누적)이다. 수집기가
        없으면 available=False."""
        ...

    def get_topology(self) -> Topology:
        """PCIe 토폴로지와 NVMe 서브시스템 구조를 합친 **통합 트리** 1개.

        get_tree_node(lazy 포인터 탐색)와 목적이 다르다 — 저건 구조체 필드를
        있는 그대로 따라가는 범용 탐색이고, 이건 "장치가 시스템에 어떻게 붙어
        있는가"를 의미 단위로 재구성한 뷰다(호스트 브리지 → 브리지/스위치 →
        엔드포인트 → 컨트롤러 → 서브시스템/네임스페이스/큐). 디바이스별이
        아니라 시스템 전체를 한 트리로 주고, 각 노드는 자기가 속한 디바이스명을
        들고 있어 UI가 선택된 장치를 강조할 수 있다."""
        ...

    # ---- NVMe I/O 프로세스 프로파일러 (대상은 런타임에 선택) ----------------

    def list_processes(self, only_io: bool = False) -> List[ProcessListEntry]:
        """대상 후보 프로세스 목록(명세 1-2). only_io=True면 최근 NVMe I/O를
        발행한 프로세스만 — 기본 필터로 이걸 쓰면 "무엇이 이 SSD를 때리는지"를
        모르는 상태에서도 시작할 수 있다(명세 1-1 (d) 자동 발견)."""
        ...

    def list_targets(self) -> List[TargetRule]:
        """등록된 대상 선택 규칙 목록. 규칙은 프로세스가 죽어도 유지된다."""
        ...

    def add_target(self, rule: TargetRule) -> List[TargetRule]:
        """규칙 추가(pid/name/name_pattern/cmdline_pattern). 데몬 재시작 불필요."""
        ...

    def remove_target(self, kind: str, value: str) -> List[TargetRule]:
        """규칙 제거. 이미 만들어진 세션은 지우지 않는다(데이터 보존)."""
        ...

    def get_profile(self) -> ProfileSnapshot:
        """프로파일 스냅샷 — 세션별 논리 그룹/기대 대조, 미관측 I/O, 장치 귀속."""
        ...

    def list_event_kinds(self) -> List[EventKindInfo]:
        """이 시스템에 등록된 이벤트 종류 목록(backend/event_registry.py).

        UI/CLI가 "무엇을 수집 중인지"를 하드코딩하지 않고 물어보게 하려는 것 —
        종류가 늘어도 화면 문구를 따로 고칠 필요가 없고, 등록은 됐지만 수집기가
        꺼져 있는 상태(active=False)도 그대로 드러난다."""
        ...
