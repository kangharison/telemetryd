"""이벤트 서비스 구현 — eBPF 수집기 출력만 읽는다(성능 서비스와 마찬가지로 drgn 불필요).

## 소스 합성 구조

이벤트 종류마다 **리더**가 하나씩 있고(타임아웃/에러), 이 서비스는 그것들을
합쳐 시간순으로 정렬해 내보낸다. 새 종류를 추가할 때 손댈 곳은 여기 `_readers`
한 줄과 그 리더 모듈뿐이다 — 계약도, gRPC도, UI 목록도 안 바뀐다(§9.12).

리더는 **상태를 들고 있다**(마지막으로 읽은 로그 오프셋 + 최근 이벤트 링버퍼).
그래서 호출마다 새로 만들면 안 되고 서비스 인스턴스 수명 동안 재사용해야 한다 —
새로 만들면 매번 로그를 처음부터 다시 읽어 같은 이벤트를 계속 중복 보고한다.
"""
from __future__ import annotations

from typing import List, Optional

from telemetryd.backend.ebpf_error_events import ErrorEventReader, read_error_stats
from telemetryd.backend.ebpf_timeout_events import TimeoutEventReader
from telemetryd.backend.event_registry import registered_event_kinds
from telemetryd.models import DeviceErrorStats, EventKindInfo, NvmeEvent
from telemetryd.platform.ebpf import EbpfLogSource


class EbpfEventService:
    """수집기 로그 하나에서 여러 종류의 이벤트를 읽어 합치는 서비스."""

    def __init__(self, log_source: EbpfLogSource):
        self._log = log_source
        # [한국어] 리더는 상태(오프셋/링버퍼)를 들고 있어 지연 생성 후 재사용한다.
        # 같은 로그 파일을 보지만 각자 자기 오프셋을 따로 갖는다(관심 줄이 다름).
        self._readers: Optional[list] = None

    def _sources(self) -> list:
        if self._readers is None:
            self._readers = [
                TimeoutEventReader(self._log),
                ErrorEventReader(self._log),
            ]
        return self._readers

    def get_events(self, device: str) -> List[NvmeEvent]:
        if not self._log.available:
            return []
        events = [e for src in self._sources() for e in src.events_for_device(device)]
        # [한국어] 각 소스는 자기 순서로만 정렬돼 있으므로, 합친 뒤 관측 시각으로
        # 다시 정렬해야 목록 전체가 시간순이 된다.
        events.sort(key=lambda e: e.observed_at)
        return events

    def get_error_stats(self, device: str) -> DeviceErrorStats:
        if not self._log.available:
            return DeviceErrorStats(
                device=device, available=False,
                error="eBPF 수집기 로그 없음 — nvme_perf.bt가 안 떠 있거나 "
                      "로그 경로가 설정되지 않음(DESIGN.md §9.6)",
            )
        return read_error_stats(self._log, device)

    def list_event_kinds(self) -> List[EventKindInfo]:
        """active 플래그로 "등록됐지만 수집기 미설정" 상태를 구분해준다."""
        return registered_event_kinds(active=self._log.available)
