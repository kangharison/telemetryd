"""큐 도메인의 순수 디코딩 헬퍼 — 커널 세션이 필요 없는 것들만 모았다.

링 버퍼 윈도우 계산, PRP 페이지 디코딩, CDW 필드 호환 처리 등. 서비스 본체와
분리해 두면 이것들만 따로 단위 테스트할 수 있고, 서비스는 "무엇을 읽을지"에
집중된다.
"""
from __future__ import annotations

import os
import re
import struct
from typing import List, Tuple

from telemetryd.models import PrpPage
from telemetryd.nvme_const import MAX_PAGE_DUMP, PAGE_MASK, PAGE_SIZE, PRPS_PER_PAGE

def _window_indices(doorbell: int, depth: int, limit: int) -> List[int]:
    """도어벨(sq_tail 또는 cq_head) 바로 앞 최근 limit개 인덱스 — **최신(도어벨
    바로 앞)부터 오래된 순으로** 내림차순(링 버퍼 wrap 처리). limit<=0이거나
    depth 이상이면 전체 depth. mock_backend.py의 동명 함수와 동일 규칙(실제 커널판)."""
    n = min(limit, depth) if limit else depth
    return [(doorbell - 1 - i) % depth for i in range(n)]

#: gendisk 이름 -> (컨트롤러 인스턴스, 네임스페이스 ID).
#
# **네임스페이스 번호를 1로 고정하면 안 된다**(예전엔 `nvme(\d+)n1$` 이었다).
# QEMU 검증 환경은 항상 nsid=1이라 안 드러났지만, 실기 엔터프라이즈 SSD는
# 네임스페이스 관리로 NSID가 1이 아닌 경우가 흔하다(예: nvme0n2만 존재). 그런
# 컨트롤러는 목록에서 통째로 빠져 "장치가 안 보인다"가 된다.
#
# NVMe 멀티패스가 켜져 있으면 개별 경로가 `nvme0c1n1` 형태로도 나타나는데,
# 그건 head 장치(`nvme0n1`)와 같은 것을 가리키는 숨은 경로라 여기서는 안 잡는다
# (안 그러면 같은 컨트롤러가 중복으로 보인다).
_DISK_RE = re.compile(rb"^nvme(\d+)n(\d+)$")



def _iommu_enabled() -> bool:
    """/sys/class/iommu/ 가 비어있지 않으면 IOMMU 활성 — PRP가 물리주소가 아닌
    IOVA일 수 있어(§DESIGN 0) 스냅샷에 플래그로 남긴다. 이건 drgn이 아니라
    이 프로세스가 돌아가는 호스트 자체의 sysfs를 보는 것(같은 머신이므로 OK)."""
    try:
        return len(os.listdir("/sys/class/iommu")) > 0
    except FileNotFoundError:
        return False


def _decode_prp_pages(prog, prp1: int, prp2: int, total_len: int) -> List[PrpPage]:
    """04_prp_payload.py의 analyze_prp()를 그대로 옮기되, print 대신 PrpPage 리스트로.

    리스트 페이지(case C)의 엔트리도 커널 가상주소(iod.descriptors[0])를 거치지
    않고 prp2 물리주소를 직접 읽어(physical=True) 파싱한다 — IOMMU 비활성
    환경에서는 PRP2 자체가 이미 그 리스트 페이지의 물리주소이므로 이렇게 해도
    같은 내용을 얻는다(§DESIGN 0).

    요구사항: "PRP 확인"은 **데이터 페이로드를 최대 MAX_PAGE_DUMP(4096B)까지만**
    보여준다. bs=64k 같은 큰 I/O는 실제로 여러 페이지(case C, PRP 리스트)에
    걸치는데, 예전엔 리스트 페이지당 최대 8개 데이터 페이지(최대 32KB)까지
    다 읽어서 응답이 UI로 다루기엔 너무 커졌다(실사용 중 발견). 그래서 아래
    `shown` 누적치로 데이터 바이트 총합을 4096에서 끊는다 — PRP 리스트 페이지
    자체(`is_list_page=True`, 메타데이터)는 데이터가 아니라 캡 대상에서 뺀다.
    """
    pages: List[PrpPage] = []
    if prp1 == 0 or total_len <= 0:
        return pages

    offset = prp1 & PAGE_MASK
    first_page_bytes = min(total_len, PAGE_SIZE - offset)  # 케이스 판별(1/2/3+페이지)은 캡과 무관하게 실제 크기로
    first_bytes = min(first_page_bytes, MAX_PAGE_DUMP)      # 실제로 읽어서 보여줄 양만 캡
    pages.append(_read_page(prog, prp1, offset, first_bytes))
    shown = first_bytes
    remaining = total_len - first_page_bytes
    if remaining <= 0 or shown >= MAX_PAGE_DUMP:
        return pages

    n_more = -(-remaining // PAGE_SIZE)  # ceil div
    if n_more == 1:
        page_bytes = min(remaining, MAX_PAGE_DUMP - shown)
        if page_bytes > 0:
            pages.append(_read_page(prog, prp2, 0, page_bytes))
        return pages

    # case C: PRP2 는 PRP 리스트 페이지의 물리주소. 메타데이터라 4KB 캡과 무관하게 항상 보여준다.
    pages.append(_read_page(prog, prp2, 0, min(remaining, PAGE_SIZE), is_list_page=True))
    if shown >= MAX_PAGE_DUMP:
        return pages
    show = min(n_more, PRPS_PER_PAGE - 1, 8)  # 리스트 자체가 너무 길면 8엔트리까지만 조회
    try:
        raw = prog.read(prp2, show * 8, physical=True)
        entries = struct.unpack(f"<{show}Q", raw)
    except Exception:
        entries = ()
    for i, entry_phys in enumerate(entries):
        if shown >= MAX_PAGE_DUMP:
            break
        page_bytes = min(PAGE_SIZE, remaining - i * PAGE_SIZE, MAX_PAGE_DUMP - shown)
        if page_bytes <= 0:
            break
        pages.append(_read_page(prog, entry_phys, 0, page_bytes))
        shown += page_bytes
    return pages


def _read_page(prog, phys: int, offset: int, nbytes: int, is_list_page: bool = False) -> PrpPage:
    nbytes = max(0, min(nbytes, PAGE_SIZE))
    try:
        data = bytes(prog.read(phys, nbytes, physical=True))
    except Exception:
        data = b""  # MMIO/미매핑/P2P 메모리 등 — FaultError를 조용히 삼키고 빈 페이로드로 표기
    return PrpPage(phys_addr=phys, offset_in_page=offset, data=data, is_list_page=is_list_page)


def _cdw2_cdw3(common) -> Tuple[int, int]:
    """커널 버전에 따라 struct nvme_common_command 의 cdw2 필드가 개별
    cdw2/cdw3 인지, `__le32 cdw2[2]` 배열인지가 다를 수 있어 둘 다 시도한다."""
    try:
        return int(common.cdw2), int(common.cdw3)
    except (AttributeError, LookupError, TypeError):
        try:
            arr = common.cdw2
            return int(arr[0]), int(arr[1])
        except Exception:
            return 0, 0
