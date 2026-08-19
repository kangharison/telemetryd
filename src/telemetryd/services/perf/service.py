"""성능 서비스 구현 — eBPF 수집기 출력만 읽는다.

**이 서비스는 drgn을 전혀 쓰지 않는다.** DESIGN.md §6에서 정한 역할 분담
("eBPF = 저오버헤드 상시 카운터, drgn = 온디맨드 구조체 스냅샷")이 의존성으로
그대로 드러난 것이다. 그래서 커널 세션 없이도 생성·테스트할 수 있고,
마이크로서비스로 떼면 drgn/QMP 접근 권한이 아예 필요 없는 프로세스가 된다.
"""
from __future__ import annotations

from telemetryd.backend.ebpf_perf import read_device_performance
from telemetryd.models import DevicePerf
from telemetryd.platform.ebpf import EbpfLogSource


class EbpfPerfService:
    """EbpfLogSource 하나만 있으면 되는 성능 서비스."""

    def __init__(self, log_source: EbpfLogSource):
        self._log = log_source

    def get_performance(self, device: str) -> DevicePerf:
        if not self._log.available:
            return DevicePerf(
                device=device, queues=[], available=False,
                error="eBPF 수집기 로그 없음 — nvme_perf.bt가 안 떠 있거나 "
                      "로그 경로가 설정되지 않음(DESIGN.md §9.6)",
            )
        return read_device_performance(self._log, device)
