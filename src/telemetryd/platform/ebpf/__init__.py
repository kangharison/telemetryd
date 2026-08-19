"""eBPF 수집기 출력 접근 플랫폼.

bpftrace(ebpf/nvme_perf.bt)는 stdout을 파일로 append한다. 그 파일을 읽는
방식은 소비자마다 다르고, 이 차이가 실제로 성능/정확성에 영향을 준다:

  read_all()        전체를 읽는다 — 성능 스냅샷(마지막 완료 틱만 필요하지만
                    틱 경계를 찾으려면 뒤에서부터 봐야 해서 현재는 전체 읽기).
  read_tail(n)      끝 n바이트만 — 누적 카운터처럼 "매 틱 전체가 다시 찍히는"
                    맵은 끝부분만 봐도 충분하다(로그가 수백 MB로 자라므로 중요).
  open_cursor()     증분 커서 — 이벤트 스트림처럼 **한 번 나온 줄을 놓치면 안
                    되는** 소비자용. 마지막으로 읽은 오프셋을 들고 새로 append된
                    부분만 읽는다.

증분 커서 로직(오프셋 추적, 미완성 줄 보류, 로그 로테이션 감지)은 원래
타임아웃 리더와 에러 리더에 **똑같이 복사돼 있었다** — 한쪽만 고쳐지면 조용히
어긋나는 종류의 중복이라 여기로 올렸다.
"""
from telemetryd.platform.ebpf.ports import EbpfLogCursor, EbpfLogSource
from telemetryd.platform.ebpf.logfile import (
    FileEbpfLogSource,
    NullEbpfLogSource,
    as_log_source,
)

__all__ = [
    "EbpfLogSource",
    "EbpfLogCursor",
    "FileEbpfLogSource",
    "NullEbpfLogSource",
    "as_log_source",
]
