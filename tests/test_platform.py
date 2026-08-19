"""플랫폼 계층(platform/*) 단위 테스트.

플랫폼은 도메인 지식이 없어야 하므로 여기 테스트에도 NVMe 어휘가 안 나온다 —
그게 계층이 제대로 분리됐다는 신호다. drgn도 실제 로그 파일도 없이 돈다."""
import time

from telemetryd.platform.cache import AdaptiveTtlCache
from telemetryd.platform.ebpf import FileEbpfLogSource, NullEbpfLogSource


# ---- AdaptiveTtlCache ------------------------------------------------------

def test_cache_returns_cached_value_within_ttl():
    calls = []
    clock = [0.0]
    c = AdaptiveTtlCache(min_ttl=10.0, clock=lambda: clock[0])

    def compute():
        calls.append(1)
        return "v"

    assert c.get_or_compute("k", compute) == "v"
    clock[0] = 5.0
    assert c.get_or_compute("k", compute) == "v"
    assert len(calls) == 1        # TTL 안이라 재계산 안 함


def test_cache_recomputes_after_ttl():
    calls = []
    clock = [0.0]
    c = AdaptiveTtlCache(min_ttl=10.0, clock=lambda: clock[0])
    c.get_or_compute("k", lambda: calls.append(1))
    clock[0] = 11.0
    c.get_or_compute("k", lambda: calls.append(1))
    assert len(calls) == 2


def test_ttl_scales_with_measured_duration():
    """핵심 규칙(DESIGN.md §9.16): TTL은 고정이 아니라 **직전 소요시간 × factor**.
    고정 TTL이면 조회가 TTL보다 오래 걸릴 때 만료되자마자 다시 돌아 워커를
    100% 점유한다 — 실제로 그렇게 겪어서 이 규칙이 생겼다."""
    clock = [0.0]

    def slow():
        clock[0] += 60.0          # 이 조회가 60초 걸렸다고 흉내
        return "v"

    c = AdaptiveTtlCache(min_ttl=30.0, duration_factor=3.0, clock=lambda: clock[0])
    c.get_or_compute("k", slow)
    assert c.ttl_of("k") == 180.0     # max(30, 60*3)


def test_ttl_falls_back_to_min_for_fast_calls():
    clock = [0.0]
    c = AdaptiveTtlCache(min_ttl=30.0, duration_factor=3.0, clock=lambda: clock[0])
    c.get_or_compute("k", lambda: "v")   # 순식간(0초)
    assert c.ttl_of("k") == 30.0


def test_invalidate_forces_recompute():
    calls = []
    c = AdaptiveTtlCache(min_ttl=999.0)
    c.get_or_compute("k", lambda: calls.append(1))
    c.invalidate("k")
    c.get_or_compute("k", lambda: calls.append(1))
    assert len(calls) == 2


def test_keys_are_independent():
    c = AdaptiveTtlCache(min_ttl=999.0)
    assert c.get_or_compute("a", lambda: 1) == 1
    assert c.get_or_compute("b", lambda: 2) == 2
    assert c.get_or_compute("a", lambda: 99) == 1   # a는 그대로


# ---- FileEbpfLogSource -----------------------------------------------------

def test_missing_path_is_unavailable_and_reads_empty():
    src = NullEbpfLogSource()
    assert src.available is False
    assert src.read_all() == ""
    assert src.read_tail(100) == ""
    assert src.open_cursor().read_new_lines() == []


def test_read_all_and_tail(tmp_path):
    p = tmp_path / "log"
    p.write_text("abcdefghij")
    src = FileEbpfLogSource(str(p))
    assert src.available is True
    assert src.read_all() == "abcdefghij"
    assert src.read_tail(4) == "ghij"
    assert src.read_tail(1000) == "abcdefghij"   # 파일보다 크면 전체


def test_cursor_returns_only_new_complete_lines(tmp_path):
    p = tmp_path / "log"
    p.write_text("one\ntwo\n")
    cur = FileEbpfLogSource(str(p)).open_cursor()
    assert cur.read_new_lines() == ["one", "two"]
    assert cur.read_new_lines() == []             # 새로 추가된 게 없음

    with open(p, "a") as f:
        f.write("three\n")
    assert cur.read_new_lines() == ["three"]      # 새 줄만


def test_cursor_holds_back_incomplete_trailing_line(tmp_path):
    """수집기가 줄 중간까지만 쓴 상태 — 완성될 때까지 안 돌려준다."""
    p = tmp_path / "log"
    p.write_text("done\npartial")
    cur = FileEbpfLogSource(str(p)).open_cursor()
    assert cur.read_new_lines() == ["done"]

    with open(p, "a") as f:
        f.write(" now complete\n")
    assert cur.read_new_lines() == ["partial now complete"]


def test_cursor_resets_after_truncation(tmp_path):
    """수집기 재시작으로 파일이 잘리면 오프셋을 0으로 되돌려야 한다 — 안 그러면
    seek이 파일 끝을 넘어가 이후 내용을 영영 못 읽는다."""
    p = tmp_path / "log"
    p.write_text("a\nb\nc\n")
    cur = FileEbpfLogSource(str(p)).open_cursor()
    assert cur.read_new_lines() == ["a", "b", "c"]

    p.write_text("x\n")                      # 훨씬 짧게 재생성
    assert cur.read_new_lines() == ["x"]     # 처음부터 다시 읽어 새 내용을 봄
