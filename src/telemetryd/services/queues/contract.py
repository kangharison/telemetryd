"""큐/장치 서비스 계약 — 이 도구의 원래 핵심 도메인.

컨트롤러 발견부터 SQ/CQ 링 내용, PRP 페이로드, 커널 구조체 트리 탐색까지
"NVMe 장치의 현재 상태를 커널 자료구조에서 직접 읽는" 일을 담당한다.

메서드가 6개로 다른 서비스보다 많지만, 전부 **같은 커널 객체 그래프
(nvme_dev → nvme_queue → SQ/CQ → PRP)를 다른 각도로 보는 것**이라 한 서비스로
묶는 게 맞다. 억지로 더 쪼개면 같은 조회를 반복하게 되고 응집도만 떨어진다.
(반대로 perf/events/topology/profiler는 서로 다른 데이터 소스라 나눈 것이다.)
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from telemetryd.models import (
    CompletionEntry,
    DeviceSnapshot,
    PrpPayload,
    QueueEntry,
    TreeExpansion,
)

SERVICE_NAME = "queues"


@runtime_checkable
class QueueService(Protocol):
    """NVMe 컨트롤러/큐의 라이브 상태."""

    def list_devices(self) -> List[str]:
        """등록된 컨트롤러 이름 (예: ["nvme0", "nvme1"])."""
        ...

    def get_device_snapshot(self, device: str) -> DeviceSnapshot:
        """struct nvme_dev 요약 + 큐별 sq_tail/cq_head/inflight."""
        ...

    def get_queue_entries(self, device: str, qid: int, limit: int = 16,
                          around_doorbell: bool = True) -> List[QueueEntry]:
        """SQ 엔트리(CDW 전체). around_doorbell=True면 sq_tail 바로 앞 최근 N개를
        **최신순**으로."""
        ...

    def get_completion_entries(self, device: str, qid: int, limit: int = 16,
                               around_doorbell: bool = True) -> List[CompletionEntry]:
        """CQ 엔트리. around_doorbell=True면 cq_head 바로 앞 최근 N개."""
        ...

    def get_prp_payload(self, device: str, qid: int, cid: int) -> PrpPayload:
        """그 커맨드가 가리키는 데이터 페이지(최대 4KB) hexdump 원본.
        SGL 경로(PSDT!=0) 커맨드면 uses_sgl=True로 표시하고 해독하지 않는다."""
        ...

    def get_tree_node(self, device: str, path: List[str]) -> TreeExpansion:
        """struct nvme_dev 루트에서 path를 따라간 노드 + 바로 다음 자식(lazy).
        깊이는 서버에서 10으로 제한한다."""
        ...
