"""ebpf/nvme_perf.bt의 kprobe:nvme_timeout이 찍는 `TIMEOUT_EVENT ...` 한 줄
로그를 읽어 **kind="timeout" NvmeEvent** 목록으로 만든다(DESIGN.md §9.11).

이 모듈은 "이벤트 소스 1개"에 해당한다 — 이벤트 목록 전체(NvmeEvent)는 종류
무관 봉투라서, 나중에 리셋(nvme_reset_ctrl)이나 AER 같은 다른 종류를 추가할
때는 이 파일을 고치는 게 아니라 **같은 모양의 리더 모듈을 하나 더 만들어**
backend.get_events()에서 합치면 된다(models.NvmeEvent 독스트링 참고). 그래서
이 파일의 파싱 규칙/정규식은 오직 nvme_timeout 로그 줄에만 맞춰져 있고,
다른 종류의 줄은 조용히 무시한다.

get_performance()용 backend/ebpf_perf.py와 같은 로그 파일을 보지만 성격이
다르다 — ebpf_perf.py는 "1초 틱마다 최신 구간만" 보면 되는 반면, 이건
"한 번 나온 이벤트는 놓치면 안 되는" 이벤트 스트림이라 매번 파일 전체를
다시 읽지 않고 마지막으로 읽은 바이트 위치부터 증분(tail)으로 읽는다 —
수집기가 며칠씩 켜져 있으면 로그가 수백MB까지 자라기 때문에(실측 4일에
114MB) 매 폴링마다 전체를 다시 읽으면 안 된다.

이 모듈의 TimeoutEventReader는 상태(마지막 읽은 오프셋 + 최근 이벤트
링버퍼)를 인스턴스에 들고 있어야 해서, backend/ebpf_perf.py의 순수 함수
스타일과 달리 클래스다 — DrgnBackend가 이 인스턴스를 하나 캐싱해서 매
호출마다 재사용한다."""
from __future__ import annotations

import os
import re
import time
from typing import List, Optional

from telemetryd.models import NvmeEvent, TimeoutEventDetail
from telemetryd.nvme_const import opcode_name
from telemetryd.platform.ebpf import as_log_source as _as_log_source

#: 이 리더가 만들어내는 이벤트의 kind 식별자. UI/CLI의 종류별 렌더러가 이
#: 문자열로 분기하므로 한 곳에서만 정의해 오타로 어긋나는 걸 막는다.
KIND = "timeout"

_EVENT_RE = re.compile(
    r"^TIMEOUT_EVENT ts_ns=(\d+) ctrl=(-?\d+) qid=(-?\d+) tag=(-?\d+) "
    r"opcode=(-?\d+) nsid=(-?\d+) flags=(-?\d+) "
    r"cdw10=(\d+) cdw11=(\d+) cdw12=(\d+) cdw13=(\d+) cdw14=(\d+) cdw15=(\d+) "
    r"elapsed_ns=(\d+)\s*$"
)


def _elapsed_text(elapsed_us: float) -> str:
    """경과 시간을 사람이 읽기 좋은 단위로 — 타임아웃은 보통 초 단위(기본
    io_timeout 30초)라 1초 이상이면 초로, 그 미만이면 밀리초로 쓴다."""
    if elapsed_us >= 1_000_000:
        return f"{elapsed_us / 1_000_000:.1f}s"
    return f"{elapsed_us / 1000:.1f}ms"


def _summary(detail: TimeoutEventDetail, qid: int) -> str:
    """종류를 모르는 소비자(목록 테이블/CLI 한 줄)가 그대로 뿌릴 요약문.

    NvmeEvent.summary의 계약대로 "이 한 줄만 봐도 무슨 일이 났는지"가 되게
    쓴다 — 어떤 커맨드가(opcode) 어느 네임스페이스에서(nsid) 얼마나 오래
    응답이 없었는지(경과). 상세 CDW는 종류별 렌더러가 따로 보여준다."""
    return (
        f"{detail.opcode_name}(0x{detail.opcode:02x}) 커맨드가 "
        f"{_elapsed_text(detail.elapsed_us)} 동안 완료되지 않음 "
        f"(qid={qid}, tag={detail.tag}, nsid={detail.nsid})"
    )


def _parse_line(line: str) -> Optional[NvmeEvent]:
    """`TIMEOUT_EVENT ...` 한 줄 -> NvmeEvent(kind="timeout"). 형식이 안 맞으면
    None — 같은 로그 파일에 섞여 있는 성능 틱(@map 덤프)/부팅 메시지 줄들을
    이 한 줄짜리 판정으로 전부 걸러낸다."""
    m = _EVENT_RE.match(line.strip())
    if not m:
        return None
    (ts_ns, ctrl, qid, tag, opcode, nsid, flags,
     cdw10, cdw11, cdw12, cdw13, cdw14, cdw15, elapsed_ns) = m.groups()
    qid_i = int(qid)
    opcode_i = int(opcode)
    detail = TimeoutEventDetail(
        tag=int(tag),
        opcode=opcode_i,
        opcode_name=opcode_name(opcode_i, qid_i == 0),
        nsid=int(nsid),
        flags=int(flags),
        cdw10=int(cdw10), cdw11=int(cdw11), cdw12=int(cdw12),
        cdw13=int(cdw13), cdw14=int(cdw14), cdw15=int(cdw15),
        elapsed_us=int(elapsed_ns) / 1000.0,
    )
    return NvmeEvent(
        kind=KIND,
        observed_at=time.time(),
        device=f"nvme{int(ctrl)}",
        qid=qid_i,
        summary=_summary(detail, qid_i),
        timeout=detail,
    )


class TimeoutEventReader:
    """log_path를 증분으로 tail하며 TIMEOUT_EVENT 줄만 골라 최근
    max_events개를 링버퍼로 들고 있는다. poll()을 부를 때마다 파일에 새로
    추가된 바이트만 읽어 파싱한다."""

    def __init__(self, log_source, max_events: int = 200):
        """@log_source: EbpfLogSource, 또는 하위호환용 로그 파일 경로 문자열.
        오프셋 추적·미완성 줄 보류·로테이션 감지는 전부 플랫폼 커서가 한다
        (원래 이 클래스와 에러 리더에 똑같이 복사돼 있던 로직)."""
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
