"""ebpf/nvme_perf.bt가 찍는 `ERROR_EVENT ...` 줄과 `@err_count[...]` 누적 맵을
읽어, **kind="error" NvmeEvent 목록**과 **SCT/SC 조합별 누적 카운터**를 만든다
(요청 A2 — 에러 status로 반환된 커맨드 캡처, DESIGN.md §9.13).

왜 이 이벤트가 필요한가: 디바이스 이상 징후 중 타임아웃(§9.11)까지 도달하는
것은 극히 일부다. 대부분은 그 전에 에러 status로 반환되고, nvme 코어가
재시도(nvme_retry_req)로 흡수해버려 애플리케이션에서는 아무 일도 없던 것처럼
보인다 — 그래서 완료 경로(tracepoint:nvme:nvme_complete_rq)에서 status != 0을
직접 잡지 않으면 영영 안 보인다.

구조는 backend/ebpf_timeout_events.py와 **일부러 똑같이** 맞췄다(같은 증분 tail
리더 + 종류별 상세를 공통 봉투 NvmeEvent에 담는 방식) — 이벤트 종류를 추가할
때 "리더 모듈 하나 + 봉투 슬롯 하나"만 늘리면 되게 하려는 설계다
(models.NvmeEvent 독스트링).

이 모듈만의 추가 항목이 하나 있다: 이벤트 줄과 별개로 유지되는 누적 카운터
(`@err_count[ctrl, sct, sc]`). 이벤트 줄은 로그 폭주 방지를 위해 초당 인쇄
예산으로 샘플링될 수 있지만(nvme_perf.bt의 @err_budget), 카운터는 전부 세므로
"어떤 에러가 여태 몇 번 났는지"는 정확하다."""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Tuple

from telemetryd.models import DeviceErrorStats, ErrorEventDetail, ErrorStatusCount, NvmeEvent
from telemetryd.nvme_const import decode_status, opcode_name, sc_name, sct_name
from telemetryd.platform.ebpf import as_log_source as _as_log_source

#: 이 리더가 만들어내는 이벤트의 kind 식별자(UI/CLI 렌더러가 이걸로 분기).
KIND = "error"

#: read/write일 때만 cdw10/11/12가 SLBA/NLB 자리다 — 그 외 opcode에서는 같은
#: 자리가 전혀 다른 의미라 lba_valid=False로 표시하고 화면에 안 띄운다.
_LBA_OPCODES = (0x01, 0x02)   # write, read

_EVENT_RE = re.compile(
    r"^ERROR_EVENT ts_ns=(\d+) ctrl=(-?\d+) qid=(-?\d+) cid=(-?\d+) tag=(-?\d+) "
    r"opcode=(-?\d+) nsid=(-?\d+) status=(-?\d+) sct=(-?\d+) sc=(-?\d+) "
    r"dnr=(-?\d+) more=(-?\d+) crd=(-?\d+) slba=(\d+) nlb=(-?\d+) "
    r"retries=(-?\d+) cached=(-?\d+) elapsed_ns=(\d+)\s*$"
)

#: `@err_count[0, 2, 129]: 12` — (ctrl_id, sct, sc) 3-키 맵.
#: backend/ebpf_perf.py의 스칼라 정규식은 2-키만 받으므로 그쪽에서는 이 줄이
#: 그냥 무시된다(성능 파싱과 서로 간섭하지 않는다).
_ERR_COUNT_RE = re.compile(r"^@err_count\[(-?\d+),\s*(-?\d+),\s*(-?\d+)\]:\s*(\d+)\s*$")

#: 누적 맵을 뽑을 때 파일 전체를 읽지 않고 끝에서 이만큼만 읽는다 — 수집기
#: 로그는 며칠이면 수백MB가 되는데(실측 4일 114MB) 1초마다 전체를 읽으면
#: 안 된다. 한 틱 블록은 큐 수 × 맵 수라서 수십KB를 넘지 않는다.
_TAIL_BYTES = 512 * 1024


def _summary(d: ErrorEventDetail, qid: int) -> str:
    """종류를 모르는 소비자(목록 테이블/CLI 한 줄)가 그대로 뿌릴 요약문.

    한 줄만 봐도 "무엇이 왜 실패했고 재시도 가능한가"가 보이게 쓴다 — 에러
    이벤트에서 실무적으로 가장 중요한 판단이 DNR(재시도해도 소용없음) 여부라
    그것을 요약에 넣는다."""
    where = f", LBA {d.slba}+{d.nlb}" if d.lba_valid else ""
    # [한국어] NSID 0xFFFFFFFF는 "모든 네임스페이스"(broadcast)라는 정상 값이라
    # 4294967295로 그대로 쓰면 오히려 이상해 보인다(admin 커맨드에서 흔함).
    nsid_txt = "0xFFFFFFFF(broadcast)" if d.nsid == 0xFFFFFFFF else str(d.nsid)
    dnr = ", DNR(재시도 불가)" if d.dnr else ""
    retried = f", 재시도 {d.retries}회" if d.retries else ""
    return (
        f"{d.opcode_name}(0x{d.opcode:02x}) 실패 — "
        f"{d.sct_name}/{d.sc_name} (status=0x{d.status:04x})"
        f"{dnr}{retried} (qid={qid}, cid={d.cid}, nsid={nsid_txt}{where})"
    )


def _parse_line(line: str) -> Optional[NvmeEvent]:
    """`ERROR_EVENT ...` 한 줄 -> NvmeEvent(kind="error"). 형식이 안 맞으면 None."""
    m = _EVENT_RE.match(line.strip())
    if not m:
        return None
    (ts_ns, ctrl, qid, cid, tag, opcode, nsid, status, _sct, _sc,
     _dnr, _more, _crd, slba, nlb, retries, cached, elapsed_ns) = m.groups()
    # [한국어] bpftrace는 트레이스포인트의 u8/u32 필드를 부호 있는 정수로 넘겨서
    # 큰 값이 음수로 찍힌다(실측: admin-passthru --opcode=0xff -> opcode=-1,
    # broadcast NSID 0xffffffff -> nsid=-1). 원래 폭으로 다시 마스킹해야
    # "0x-1" 같은 표시나 잘못된 opcode 해석이 안 나온다.
    qid_i, opcode_i = int(qid), int(opcode) & 0xFF
    nsid_i = int(nsid) & 0xFFFFFFFF
    status_i = int(status)
    cached_b = bool(int(cached))
    # [한국어] sct/sc/dnr/more/crd는 로그 줄에도 이미 분해돼 찍혀 있지만(사람이
    # grep으로 바로 읽으라고), 여기서는 **status 하나만을 진실로 삼아 다시
    # 분해한다**(nvme_const.decode_status). 같은 값을 두 곳에서 따로 계산하면
    # 언젠가 한쪽 비트 배치만 고쳐져 조용히 어긋나기 때문 — 스크립트의 분해는
    # 사람이 읽는 용도, 프로그램이 쓰는 값은 항상 status에서 나온다.
    dec = decode_status(status_i)
    sct_i, sc_i = dec["sct"], dec["sc"]
    # [한국어] 제출을 못 본 커맨드(cached=0)는 opcode/nsid/slba가 전부 0으로
    # 찍혀 나온다 — 그걸 진짜 값처럼 보여주면 오독하므로 LBA는 무효 처리한다.
    # [한국어] admin 큐(qid=0)에는 LBA 커맨드가 없다 — opcode 2는 I/O 큐에서는
    # read지만 admin에서는 get_log라, qid를 안 보면 get_log의 cdw10/11(로그
    # 페이지 필드)을 SLBA로 착각한다(실측으로 잡힌 버그).
    lba_valid = cached_b and qid_i != 0 and opcode_i in _LBA_OPCODES
    detail = ErrorEventDetail(
        cid=int(cid),
        tag=int(tag),
        opcode=opcode_i,
        opcode_name=opcode_name(opcode_i, qid_i == 0) if cached_b else "미상",
        nsid=nsid_i,
        status=status_i,
        sct=sct_i,
        sc=sc_i,
        sct_name=dec["sct_name"],
        sc_name=dec["sc_name"],
        dnr=dec["dnr"],
        more=dec["more"],
        crd=dec["crd"],
        retries=int(retries),
        slba=int(slba),
        nlb=int(nlb),
        lba_valid=lba_valid,
        submit_cached=cached_b,
        elapsed_us=int(elapsed_ns) / 1000.0,
    )
    return NvmeEvent(
        kind=KIND,
        observed_at=time.time(),
        device=f"nvme{int(ctrl)}",
        qid=qid_i,
        summary=_summary(detail, qid_i),
        error=detail,
    )


class ErrorEventReader:
    """log_path를 증분으로 tail하며 ERROR_EVENT 줄만 골라 최근 max_events개를
    링버퍼로 들고 있는다 — TimeoutEventReader와 동일한 규약(같은 로그 파일을
    각자의 오프셋으로 따로 읽는다)."""

    def __init__(self, log_source, max_events: int = 200):
        """@log_source: EbpfLogSource, 또는 하위호환용 로그 파일 경로 문자열."""
        self._source = _as_log_source(log_source)
        self._cursor = self._source.open_cursor()
        self._max_events = max_events
        self._events: List[NvmeEvent] = []

    def poll(self) -> List[NvmeEvent]:
        for line in self._cursor.read_new_lines():
            ev = _parse_line(line)
            if ev is not None:
                self._events.append(ev)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]
        return list(self._events)

    def events_for_device(self, device: str) -> List[NvmeEvent]:
        self.poll()
        return [e for e in self._events if e.device == device]


def _parse_err_counts(text: str) -> Dict[Tuple[int, int, int], int]:
    """텍스트에서 `@err_count[ctrl, sct, sc]: n` 줄들을 (ctrl,sct,sc) -> n 으로.

    누적 맵이라 매 틱 같은 총계가 다시 찍힌다 — 그래서 "마지막 값이 곧 현재
    누적"이고, 같은 키가 여러 번 나오면 나중 것으로 덮어쓴다(합치면 안 된다)."""
    counts: Dict[Tuple[int, int, int], int] = {}
    for line in text.splitlines():
        m = _ERR_COUNT_RE.match(line.strip())
        if m:
            counts[(int(m.group(1)), int(m.group(2)), int(m.group(3)))] = int(m.group(4))
    return counts


def read_error_stats(log_path: str, device: str) -> DeviceErrorStats:
    """수집기 로그 끝부분에서 이 디바이스의 SCT/SC 조합별 누적 카운터를 뽑는다.

    파일 전체가 아니라 끝 _TAIL_BYTES만 읽는다(로그가 수백MB까지 자라므로).
    한 번도 에러가 없었으면 counts=[] + available=True — "수집은 되고 있는데
    에러가 0건"과 "수집기가 없음"은 다른 상태라서 구분한다."""
    m = re.match(r"^nvme(\d+)$", device)
    if not m:
        return DeviceErrorStats(device=device, available=False,
                                error=f"디바이스 이름 형식이 아님: {device!r}")
    instance = int(m.group(1))
    source = _as_log_source(log_path)
    if not source.available:
        return DeviceErrorStats(
            device=device, available=False,
            error="eBPF 수집기 로그 없음 — nvme_perf.bt가 안 떠 있음 "
                  "(DESIGN.md §9.6 참고: chroot로 게스트에서 bpftrace 실행 필요)",
        )
    text = source.read_tail(_TAIL_BYTES)

    counts = _parse_err_counts(text)
    rows = [
        ErrorStatusCount(sct=sct, sc=sc, sct_name=sct_name(sct),
                         sc_name=sc_name(sct, sc), count=n)
        for (ctrl, sct, sc), n in counts.items()
        if ctrl == instance
    ]
    # [한국어] 많이 난 것부터 — 화면/CLI 모두 "무엇이 제일 문제인가"를 위에 둔다.
    rows.sort(key=lambda r: (-r.count, r.sct, r.sc))
    return DeviceErrorStats(device=device, counts=rows,
                            total=sum(r.count for r in rows), available=True)
