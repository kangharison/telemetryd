"""NVMe opcode 이름 테이블 및 PRP 관련 상수.

deep/scripts/drgn/02_nvme_queues.py, 04_prp_payload.py 에 있던 상수/표를
그대로 옮겨 backend(mock/drgn) 양쪽이 공유한다.
"""
from __future__ import annotations

# [한국어] NVM 커맨드셋(IO 큐) opcode 일부 — 자주 보는 것만. 나머지는 hex로 표시.
NVM_OPC = {
    0x00: "flush",
    0x01: "write",
    0x02: "read",
    0x09: "dsm",
    0x0d: "write_zeroes",
}

# [한국어] Admin 커맨드셋(qid=0) opcode.
ADM_OPC = {
    0x00: "delete_sq",
    0x01: "create_sq",
    0x02: "get_log",
    0x04: "delete_cq",
    0x05: "create_cq",
    0x06: "identify",
    0x09: "set_features",
    0x0a: "get_features",
    0x0c: "async_event",
}


def opcode_name(opcode: int, is_admin: bool) -> str:
    table = ADM_OPC if is_admin else NVM_OPC
    return table.get(opcode, f"0x{opcode:02x}")


# [한국어] x86_64 기본 페이지 크기. PRP 계산 전체가 이 값을 기준으로 한다.
PAGE_SIZE = 4096
PAGE_MASK = PAGE_SIZE - 1
# [한국어] PRP 리스트 1페이지가 담는 8바이트(__le64) 엔트리 수 = 512.
PRPS_PER_PAGE = PAGE_SIZE // 8
# [한국어] "PRP 확인" 버튼이 페이지당 보여줄 최대 바이트 수 (요구사항: "4k 만큼").
MAX_PAGE_DUMP = PAGE_SIZE


# ===========================================================================
# [한국어] NVMe 완료 상태(Status Field) 디코딩 — A2(에러 status 캡처)용.
#
# CQE의 status 필드는 bit0이 phase 태그이고 나머지가 실제 상태다. 커널
# 드라이버는 `nvme_req(req)->status = le16_to_cpu(cqe->status) >> 1`로 phase를
# 떼어낸 값을 들고 있고(drivers/nvme/host/nvme.h nvme_try_complete_req),
# nvme:nvme_complete_rq 트레이스포인트도 그 값을 그대로 준다. 그래서 이
# 모듈이 다루는 status는 항상 "phase가 제거된" 값이다:
#
#   SC   = bits[7:0]    상태 코드
#   SCT  = bits[10:8]   상태 코드 타입
#   CRD  = bits[12:11]  Command Retry Delay (NVME_SC_CRD  = 0x1800)
#   More = bit[13]      추가 정보가 에러 로그 페이지에 있음 (NVME_SC_MORE = 0x2000)
#   DNR  = bit[14]      Do Not Retry — 재시도해도 소용없는 실패
#
# 이름 표는 NVMe 2.0 스펙 Figure 94~97(Status Code) 및 include/linux/nvme.h의
# NVME_SC_* 열거형 기준. 전부 담지 않고 실무에서 자주 보는 것 위주로 두고,
# 모르는 코드는 hex로 표시한다(opcode_name과 같은 방침).
# ===========================================================================

# [한국어] SCT(Status Code Type) — 이 값에 따라 SC의 의미 자체가 달라지므로
# SC 이름표도 SCT별로 따로 둔다.
SCT_NAMES = {
    0x0: "Generic",            # 커맨드 처리 일반 오류
    0x1: "Command Specific",   # 그 커맨드에만 있는 오류(잘못된 큐 ID 등)
    0x2: "Media/Data Integrity",  # 미디어 오류 — 디바이스 열화의 직접 신호
    0x3: "Path Related",       # 경로/전송 계층(호스트가 만들어 넣는 값 포함)
    0x7: "Vendor Specific",
}

# [한국어] SCT=0 Generic Command Status.
_SC_GENERIC = {
    0x00: "Success",
    0x01: "Invalid Command Opcode",
    0x02: "Invalid Field in Command",
    0x03: "Command ID Conflict",
    0x04: "Data Transfer Error",
    0x05: "Commands Aborted due to Power Loss",
    0x06: "Internal Error",
    0x07: "Command Abort Requested",
    0x08: "Command Aborted due to SQ Deletion",
    0x0b: "Invalid Namespace or Format",
    0x0c: "Command Sequence Error",
    0x1c: "Namespace Not Ready",
    0x80: "LBA Out of Range",
    0x81: "Capacity Exceeded",
    0x82: "Namespace Not Ready",
    0x83: "Reservation Conflict",
}

# [한국어] SCT=1 Command Specific Status.
_SC_CMD_SPECIFIC = {
    0x00: "Completion Queue Invalid",
    0x01: "Invalid Queue Identifier",
    0x02: "Invalid Queue Size",
    0x05: "Abort Command Limit Exceeded",
    0x0a: "Invalid Format",
    0x0e: "Invalid Log Page",
    0x80: "Conflicting Attributes",
    0x81: "Invalid Protection Information",
    0x82: "Attempted Write to Read Only Range",
}

# [한국어] SCT=2 Media and Data Integrity Errors — 디스크 자체의 이상.
# 여기 뜨는 건 재시도로 흡수되더라도 반드시 눈여겨봐야 하는 신호다.
_SC_MEDIA = {
    0x80: "Write Fault",
    0x81: "Unrecovered Read Error",
    0x82: "End-to-end Guard Check Error",
    0x83: "End-to-end Application Tag Check Error",
    0x84: "End-to-end Reference Tag Check Error",
    0x85: "Compare Failure",
    0x86: "Access Denied",
    0x87: "Deallocated or Unwritten Logical Block",
}

# [한국어] SCT=3 Path Related Status — 0x70대는 커널이 직접 채우는 값이다
# (NVME_SC_HOST_PATH_ERROR 등): 디바이스가 아니라 호스트/전송 계층에서 죽은 것.
_SC_PATH = {
    0x00: "Internal Path Error",
    0x01: "Asymmetric Access Persistent Loss",
    0x02: "Asymmetric Access Inaccessible",
    0x03: "Asymmetric Access Transition",
    0x60: "Controller Path Error",
    0x70: "Host Path Error",
    0x71: "Command Aborted by Host",
}

_SC_TABLES = {0x0: _SC_GENERIC, 0x1: _SC_CMD_SPECIFIC, 0x2: _SC_MEDIA, 0x3: _SC_PATH}


def sct_name(sct: int) -> str:
    """SCT 숫자 -> 사람이 읽는 이름. 모르는 값은 숫자로."""
    return SCT_NAMES.get(sct, f"SCT 0x{sct:x}")


def sc_name(sct: int, sc: int) -> str:
    """(SCT, SC) -> 상태 코드 이름. SC의 의미는 SCT에 따라 달라지므로 둘 다 받는다.
    모르는 조합은 hex로 표시한다(잘못된 이름을 붙이는 것보다 낫다)."""
    table = _SC_TABLES.get(sct)
    if table is None:
        return f"0x{sc:02x}"
    return table.get(sc, f"0x{sc:02x}")


def decode_status(status: int) -> dict:
    """phase가 제거된 status 값을 필드별로 분해한다.

    반환: {"sc","sct","crd","more","dnr","sct_name","sc_name"}.
    ebpf 스크립트(nvme_perf.bt)가 이미 같은 분해를 해서 로그에 찍지만, 호스트
    쪽에서도 원본 status 하나만 있으면 언제든 같은 결과를 얻을 수 있게 둔다
    (테스트와 mock에서 쓰고, 로그 포맷이 바뀌어도 이 함수가 기준이 된다)."""
    sc = status & 0xFF
    sct = (status >> 8) & 0x7
    return {
        "sc": sc,
        "sct": sct,
        "crd": (status >> 11) & 0x3,
        "more": bool((status >> 13) & 0x1),
        "dnr": bool((status >> 14) & 0x1),
        "sct_name": sct_name(sct),
        "sc_name": sc_name(sct, sc),
    }
