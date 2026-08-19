"""이 시스템에 **등록된** 이벤트 종류 목록 — 단일 진실 공급원(single source of truth).

사용자 질문 "등록된 이벤트에 대해서도 알 수 있는가?"에 대한 답이다. UI/CLI가
"지금 수집 중인 종류는 timeout입니다" 같은 문구를 하드코딩하면 종류가 늘 때마다
화면 문구가 어긋난다 — 대신 여기 등록된 목록을 API로 그대로 노출해서, 화면은
받은 걸 그리기만 한다.

각 항목은 "무엇을 잡는가 / 어디서 오는가 / 왜 보는가"를 담는다. 새 종류를
추가할 때는 (1) 리더 모듈, (2) models.NvmeEvent의 상세 슬롯, (3) proto oneof
멤버, (4) UI/CLI 렌더러와 함께 **여기 한 줄**을 추가하면 등록이 끝난다.

active 플래그는 "이 백엔드 구성에서 실제로 수집되고 있는가"다 — eBPF 수집기
로그 경로가 설정돼 있어야(그리고 게스트에서 bpftrace가 떠 있어야) True.
정의상 등록은 돼 있지만 수집은 꺼져 있는 상태를 화면에서 구분하기 위한 것."""
from __future__ import annotations

from typing import List

from telemetryd.backend import ebpf_error_events, ebpf_timeout_events
from telemetryd.models import EventKindInfo

#: (kind, label, source, description) — 등록 순서가 곧 UI 표시 순서.
_REGISTRY = (
    (
        ebpf_timeout_events.KIND,
        "타임아웃",
        "eBPF kprobe:nvme_timeout",
        "blk-mq가 커맨드를 시간 내 완료하지 못해 nvme_timeout()을 부른 경우. "
        "제출 시점에 캐시해 둔 CDW로 어떤 커맨드였는지 복원해서 함께 기록한다.",
    ),
    (
        ebpf_error_events.KIND,
        "에러 완료",
        "eBPF tracepoint:nvme:nvme_complete_rq (status != 0)",
        "에러 status로 반환된 커맨드. 타임아웃까지 가는 건 이상 징후의 극히 "
        "일부이고 대부분은 여기서 잡히는데, nvme 코어가 재시도로 흡수해버려 "
        "애플리케이션에서는 안 보인다. SCT/SC/DNR로 분해해 기록하고 조합별 "
        "누적 카운터도 따로 유지한다.",
    ),
)


def registered_event_kinds(active: bool) -> List[EventKindInfo]:
    """등록된 종류 전체. active는 이 백엔드가 실제로 수집 중인지 여부로,
    현재는 두 종류가 같은 수집기(nvme_perf.bt) 출력에서 나오므로 값이 같다 —
    수집기가 나뉘면 종류별로 따로 판단하도록 이 함수만 고치면 된다."""
    return [
        EventKindInfo(kind=k, label=label, source=source, description=desc, active=active)
        for (k, label, source, desc) in _REGISTRY
    ]
