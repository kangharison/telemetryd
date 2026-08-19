"""backend/ebpf_timeout_events.py 단위 테스트 — ebpf/nvme_perf.bt의
kprobe:nvme_timeout이 찍는 `TIMEOUT_EVENT ...` 한 줄 로그 파서(DESIGN.md
§9.11). 실제 게스트 없이도 항상 돌아가게, 그 출력 포맷을 그대로 흉내낸
텍스트로 검증한다.

파서가 만드는 건 종류별 구조체가 아니라 **공통 봉투 NvmeEvent(kind="timeout")
+ 종류별 상세(NvmeEvent.timeout)** 다 — 이벤트 목록에 나중에 리셋 같은 다른
종류가 섞여도 목록/API/UI가 안 바뀌게 하려는 구조라(models.NvmeEvent), 이
테스트도 그 두 겹을 나눠서 검증한다."""
from telemetryd.backend.ebpf_timeout_events import KIND, TimeoutEventReader, _parse_line

_LINE_QID3 = (
    "TIMEOUT_EVENT ts_ns=1000000000 ctrl=0 qid=3 tag=42 opcode=1 nsid=1 flags=0 "
    "cdw10=100 cdw11=0 cdw12=15 cdw13=0 cdw14=0 cdw15=0 elapsed_ns=30000000000\n"
)
_LINE_NVME1 = (
    "TIMEOUT_EVENT ts_ns=2000000000 ctrl=1 qid=5 tag=7 opcode=2 nsid=1 flags=0 "
    "cdw10=200 cdw11=0 cdw12=31 cdw13=0 cdw14=0 cdw15=0 elapsed_ns=30500000000\n"
)


def test_parse_line_common_envelope():
    """공통 봉투 — 종류를 모르는 소비자(목록 테이블/CLI)가 쓰는 필드들."""
    ev = _parse_line(_LINE_QID3.strip())
    assert ev is not None
    assert ev.kind == KIND == "timeout"
    assert ev.device == "nvme0"
    assert ev.qid == 3
    assert ev.observed_at > 0
    # [한국어] summary는 "종류를 몰라도 한 줄로 뿌릴 수 있어야 한다"는 계약이라
    # 무슨 커맨드가 얼마나 오래 안 끝났는지가 다 들어 있어야 한다.
    assert "write" in ev.summary
    assert "30.0s" in ev.summary
    assert "tag=42" in ev.summary


def test_parse_line_timeout_detail():
    """종류별 상세 — kind == "timeout"일 때만 채워지는 슬롯."""
    ev = _parse_line(_LINE_QID3.strip())
    assert ev.timeout is not None
    d = ev.timeout
    assert d.tag == 42
    assert d.opcode == 1
    assert d.opcode_name == "write"   # opcode 1 = write (I/O 큐, admin 아님)
    assert d.nsid == 1
    assert d.cdw12 == 15
    assert d.elapsed_us == 30000000000 / 1000.0


def test_parse_line_ignores_non_matching_lines():
    assert _parse_line("Attaching 7 probes...") is None
    assert _parse_line("@op_count[0, 1]: 5") is None
    assert _parse_line("---TICK---") is None


def test_reader_reads_complete_lines_incrementally(tmp_path):
    log = tmp_path / "nvme_perf.log"
    log.write_text(_LINE_QID3)
    reader = TimeoutEventReader(str(log))

    events = reader.poll()
    assert len(events) == 1
    assert events[0].qid == 3

    # [한국어] 같은 내용을 다시 poll해도 중복으로 안 쌓여야 한다(오프셋을
    # 전진시켰으므로 새로 추가된 바이트가 없으면 그대로).
    events2 = reader.poll()
    assert len(events2) == 1

    with open(log, "a") as f:
        f.write(_LINE_NVME1)
    events3 = reader.poll()
    assert len(events3) == 2
    assert {e.device for e in events3} == {"nvme0", "nvme1"}


def test_reader_ignores_incomplete_trailing_line(tmp_path):
    log = tmp_path / "nvme_perf.log"
    # [한국어] 줄바꿈 없이 끝나는 partial 줄 — 수집기가 아직 쓰는 중인 상태를 흉내.
    partial = _LINE_QID3.rstrip("\n")
    log.write_text(partial)
    reader = TimeoutEventReader(str(log))

    assert reader.poll() == []   # 완성된 줄이 없으니 아직 아무것도 안 읽힘

    with open(log, "a") as f:
        f.write("\n")   # 줄바꿈이 도착해서 완성됨
    events = reader.poll()
    assert len(events) == 1


def test_reader_filters_by_device(tmp_path):
    log = tmp_path / "nvme_perf.log"
    log.write_text(_LINE_QID3 + _LINE_NVME1)
    reader = TimeoutEventReader(str(log))

    assert [e.qid for e in reader.events_for_device("nvme0")] == [3]
    assert [e.qid for e in reader.events_for_device("nvme1")] == [5]
    assert reader.events_for_device("nvme9") == []


def test_reader_caps_ring_buffer(tmp_path):
    log = tmp_path / "nvme_perf.log"
    lines = "".join(
        _LINE_QID3.replace("tag=42", f"tag={i}") for i in range(5)
    )
    log.write_text(lines)
    reader = TimeoutEventReader(str(log), max_events=3)

    events = reader.poll()
    assert len(events) == 3
    # [한국어] 가장 최근(마지막) 3개만 남아야 한다 — 오래된 게 앞에서 밀려남.
    assert [e.timeout.tag for e in events] == [2, 3, 4]


def test_reader_survives_log_truncation(tmp_path):
    """수집기가 재시작되며 로그가 더 짧게 다시 만들어져도, 그 뒤에 새로 온
    이벤트를 놓치지 않아야 한다.

    오프셋 되감기 같은 **메커니즘**은 이제 플랫폼 커서의 책임이고
    (test_platform.py::test_cursor_resets_after_truncation이 덮는다), 여기서는
    리더가 그 위에서 관측 가능한 계약을 지키는지만 본다 — 내부 필드를 들여다보지
    않으므로 플랫폼 구현이 바뀌어도 이 테스트는 안 깨진다."""
    log = tmp_path / "nvme_perf.log"
    log.write_text(_LINE_QID3 + _LINE_NVME1)
    reader = TimeoutEventReader(str(log))
    assert len(reader.poll()) == 2

    log.write_text("x\n")                     # 훨씬 짧게 재생성(수집기 재시작)
    reader.poll()

    with open(log, "a") as f:
        f.write(_LINE_NVME1.replace("tag=7", "tag=99"))
    events = reader.poll()
    assert any(e.timeout.tag == 99 for e in events)
