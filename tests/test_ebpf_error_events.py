"""backend/ebpf_error_events.py 단위 테스트 — ebpf/nvme_perf.bt가 찍는
`ERROR_EVENT ...` 줄과 `@err_count[...]` 누적 맵 파서(요청 A2, DESIGN.md §9.13).

실제 게스트 없이도 항상 돌아가게, 수집기 출력 포맷을 그대로 흉내낸 텍스트로
검증한다. 타임아웃 파서와 마찬가지로 (1) 종류 무관 공통 봉투와 (2) 종류별
상세를 나눠서 본다."""
from telemetryd.backend.ebpf_error_events import (
    KIND,
    ErrorEventReader,
    _parse_line,
    read_error_stats,
)
from telemetryd.nvme_const import decode_status

# [한국어] status=0x4281 = DNR(bit14) | SCT 2(Media) | SC 0x81(Unrecovered Read Error).
# read(opcode 2)라 slba/nlb가 의미 있는 경우.
_LINE_MEDIA = (
    "ERROR_EVENT ts_ns=1000000000 ctrl=0 qid=3 cid=4137 tag=41 opcode=2 nsid=1 "
    "status=17025 sct=2 sc=129 dnr=1 more=0 crd=0 slba=123456 nlb=8 retries=1 "
    "cached=1 elapsed_ns=250000\n"
)
# [한국어] status=0x0002 = Generic/Invalid Field. admin(qid=0) identify 커맨드.
_LINE_ADMIN = (
    "ERROR_EVENT ts_ns=2000000000 ctrl=0 qid=0 cid=7 tag=7 opcode=6 nsid=0 "
    "status=2 sct=0 sc=2 dnr=0 more=0 crd=0 slba=0 nlb=1 retries=0 "
    "cached=1 elapsed_ns=90000\n"
)
# [한국어] 제출(nvme_setup_cmd)을 못 본 커맨드 — 수집기가 뜨기 전에 이미
# in-flight였던 것. opcode/nsid/slba가 0으로 찍히므로 미상 처리돼야 한다.
_LINE_UNCACHED = (
    "ERROR_EVENT ts_ns=3000000000 ctrl=1 qid=2 cid=99 tag=99 opcode=0 nsid=0 "
    "status=16897 sct=2 sc=1 dnr=1 more=0 crd=0 slba=0 nlb=1 retries=0 "
    "cached=0 elapsed_ns=0\n"
)


def test_parse_line_common_envelope():
    ev = _parse_line(_LINE_MEDIA.strip())
    assert ev is not None
    assert ev.kind == KIND == "error"
    assert ev.device == "nvme0"
    assert ev.qid == 3
    # 요약만 봐도 무엇이 왜 실패했고 재시도 가능한지가 보여야 한다.
    assert "read(0x02)" in ev.summary
    assert "Unrecovered Read Error" in ev.summary
    assert "DNR" in ev.summary


def test_parse_line_error_detail_media():
    d = _parse_line(_LINE_MEDIA.strip()).error
    assert d is not None
    assert (d.sct, d.sc) == (2, 129)
    assert d.sct_name == "Media/Data Integrity"
    assert d.sc_name == "Unrecovered Read Error"
    assert d.dnr is True and d.more is False and d.crd == 0
    assert d.cid == 4137 and d.tag == 41 and d.retries == 1
    assert d.lba_valid is True and d.slba == 123456 and d.nlb == 8
    assert d.elapsed_us == 250.0
    # [한국어] 파서는 로그에 같이 찍힌 sct/sc 텍스트를 믿지 않고 status에서
    # 다시 분해한다 — 두 값이 어긋나는 로그가 와도 status가 이긴다.
    assert decode_status(d.status)["sc"] == d.sc
    assert decode_status(d.status)["sct"] == d.sct
    assert decode_status(d.status)["dnr"] is d.dnr


def test_admin_opcode_name_uses_admin_table():
    d = _parse_line(_LINE_ADMIN.strip()).error
    assert d.opcode_name == "identify"      # qid=0이므로 admin 표로 해석
    assert d.sct_name == "Generic" and d.sc_name == "Invalid Field in Command"
    assert d.lba_valid is False             # identify는 LBA 커맨드가 아님


def test_uncached_submission_marks_fields_unknown():
    ev = _parse_line(_LINE_UNCACHED.strip())
    d = ev.error
    assert d.submit_cached is False
    assert d.opcode_name == "미상"          # 0으로 찍힌 opcode를 flush로 오독하면 안 됨
    assert d.lba_valid is False             # slba/nlb도 신뢰 불가


def test_parse_line_ignores_other_lines():
    assert _parse_line("TIMEOUT_EVENT ts_ns=1 ctrl=0 qid=3 tag=42 opcode=1 nsid=1 "
                       "flags=0 cdw10=1 cdw11=0 cdw12=0 cdw13=0 cdw14=0 cdw15=0 "
                       "elapsed_ns=1") is None
    assert _parse_line("@err_count[0, 2, 129]: 12") is None
    assert _parse_line("---TICK---") is None


def test_reader_incremental_and_device_filter(tmp_path):
    log = tmp_path / "nvme_perf.log"
    log.write_text(_LINE_MEDIA)
    reader = ErrorEventReader(str(log))
    assert len(reader.poll()) == 1
    assert len(reader.poll()) == 1          # 같은 내용을 다시 읽지 않는다

    with open(log, "a") as f:
        f.write(_LINE_UNCACHED)
    assert len(reader.poll()) == 2
    assert [e.qid for e in reader.events_for_device("nvme0")] == [3]
    assert [e.qid for e in reader.events_for_device("nvme1")] == [2]


def test_error_stats_reads_cumulative_map(tmp_path):
    """@err_count는 누적 맵이라 매 틱 같은 총계가 다시 찍힌다 — 합치지 말고
    마지막 값을 써야 한다(합치면 틱 수만큼 부풀려진다)."""
    log = tmp_path / "nvme_perf.log"
    log.write_text(
        "@err_count[0, 2, 129]: 3\n@err_count[0, 0, 2]: 1\n---TICK---\n"
        "@err_count[0, 2, 129]: 7\n@err_count[0, 0, 2]: 1\n"
        "@err_count[1, 2, 129]: 5\n---TICK---\n"
    )
    st = read_error_stats(str(log), "nvme0")
    assert st.available is True
    # 많이 난 것부터 정렬
    assert [(c.sct, c.sc, c.count) for c in st.counts] == [(2, 129, 7), (0, 2, 1)]
    assert st.counts[0].sc_name == "Unrecovered Read Error"
    assert st.total == 8                    # 7 + 1 (누적값의 합, 틱 수와 무관)

    # 다른 컨트롤러 것은 섞이면 안 된다.
    st1 = read_error_stats(str(log), "nvme1")
    assert [(c.sct, c.sc, c.count) for c in st1.counts] == [(2, 129, 5)]


def test_error_stats_missing_log(tmp_path):
    st = read_error_stats(str(tmp_path / "없는파일.log"), "nvme0")
    assert st.available is False and "수집기" in st.error


def test_error_stats_empty_when_no_errors(tmp_path):
    """수집은 되는데 에러가 0건인 상태 — "수집기 없음"과 구분돼야 한다."""
    log = tmp_path / "nvme_perf.log"
    log.write_text("@op_count[0, 1]: 5\n---TICK---\n")
    st = read_error_stats(str(log), "nvme0")
    assert st.available is True and st.counts == [] and st.total == 0


# ===========================================================================
# [한국어] 아래 3건은 라이브 게스트에서 실제로 찍힌 줄로 잡은 버그들이다
# (nvme-cli로 일부러 에러를 낸 뒤 수집기 로그를 그대로 가져옴). 합성 데이터만
# 보고 있었으면 못 잡았을 것들이라 회귀 테스트로 고정해 둔다.
# ===========================================================================

def test_admin_opcode_0xff_is_not_negative():
    """`nvme admin-passthru --opcode=0xff` 실측: bpftrace가 u8을 부호 있는
    값으로 넘겨 opcode=-1로 찍힌다 — 8비트로 다시 마스킹해야 한다."""
    line = ("ERROR_EVENT ts_ns=18407259121583 ctrl=0 qid=0 cid=28681 tag=9 opcode=-1 "
            "nsid=0 status=16385 sct=0 sc=1 dnr=1 more=0 crd=0 slba=0 nlb=1 retries=0 "
            "cached=1 elapsed_ns=11698470")
    d = _parse_line(line).error
    assert d.opcode == 0xFF
    assert d.opcode_name == "0xff"          # "0x-1" 같은 게 나오면 안 됨
    assert d.sc_name == "Invalid Command Opcode" and d.dnr is True


def test_admin_get_log_is_not_mistaken_for_read():
    """`nvme get-log --log-id=0xfe` 실측: admin 큐의 opcode 2는 read가 아니라
    get_log다. qid를 안 보면 cdw10/11(로그 페이지 필드)을 SLBA로 착각한다."""
    line = ("ERROR_EVENT ts_ns=18415318171423 ctrl=0 qid=0 cid=12 tag=12 opcode=2 "
            "nsid=-1 status=16386 sct=0 sc=2 dnr=1 more=0 crd=0 slba=8323326 nlb=1 "
            "retries=0 cached=1 elapsed_ns=3969800")
    d = _parse_line(line).error
    assert d.opcode_name == "get_log"
    assert d.lba_valid is False             # 8323326을 SLBA로 보여주면 안 됨
    assert d.nsid == 0xFFFFFFFF             # broadcast NSID가 -1로 새면 안 됨
    assert d.sc_name == "Invalid Field in Command"


def test_io_read_lba_out_of_range_keeps_lba():
    """`nvme read --start-block=99999999` 실측: I/O 큐의 read라 SLBA가 유효하다."""
    line = ("ERROR_EVENT ts_ns=18409609705233 ctrl=0 qid=7 cid=61762 tag=322 opcode=2 "
            "nsid=1 status=16512 sct=0 sc=128 dnr=1 more=0 crd=0 slba=99999999 nlb=1 "
            "retries=0 cached=1 elapsed_ns=5049080")
    ev = _parse_line(line)
    d = ev.error
    assert d.opcode_name == "read" and d.lba_valid is True and d.slba == 99999999
    assert d.sc_name == "LBA Out of Range" and d.sct_name == "Generic"
    assert "LBA 99999999" in ev.summary
