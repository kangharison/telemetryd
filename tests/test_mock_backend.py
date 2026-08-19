"""MockBackend 단위 테스트 — root/실제 커널 없이 돌아가는 유일한 백엔드라
CI/개발 중 항상 실행 가능하다. DrgnBackend는 root+라이브 커널이 필요해
(DESIGN.md §0) 여기서 테스트하지 못한다 — 사용자가 sudo -E로 직접 검증한다."""
import pytest

from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.backend.mock_backend import MockBackend


def test_list_devices():
    b = MockBackend()
    assert set(b.list_devices()) == {"nvme0", "nvme1"}


def test_device_not_found():
    b = MockBackend()
    with pytest.raises(DeviceNotFoundError):
        b.get_device_snapshot("nvme9")


def test_snapshot_shape():
    b = MockBackend()
    snap = b.get_device_snapshot("nvme0")
    assert snap.online_queues == len(snap.queues) == 3
    assert snap.queues[0].is_admin and snap.queues[0].hctx_index is None
    for q in snap.queues[1:]:
        assert not q.is_admin
        assert q.hctx_index == q.qid - 1
        assert 0 <= q.sq_tail < q.depth
        assert 0 <= q.cq_head < q.depth


def test_queue_entries_from_start_deterministic_cid():
    b = MockBackend()
    entries = b.get_queue_entries("nvme0", 1, limit=10, around_doorbell=False)
    assert [e.cid for e in entries] == list(range(10))
    assert [e.index for e in entries] == list(range(10))


def test_window_indices_newest_first_wraps_correctly():
    """도어벨 윈도우 계산(_window_indices)은 최신(도어벨 바로 앞)부터 오래된
    순서로 내림차순 — 요청사항("최근 new -> old 순"). 시간에 안 좌우되니 순수하게 검증."""
    from telemetryd.backend.mock_backend import _window_indices

    # wrap 없는 경우
    idx = _window_indices(doorbell=20, depth=128, limit=16)
    assert idx == list(range(19, 3, -1))
    assert idx[0] == 19  # doorbell - 1 (가장 최근) 이 맨 앞
    assert idx[-1] == 4  # 가장 오래된 게 맨 뒤

    # wrap 되는 경우(도어벨이 링 앞부분이라 뒤에서부터 넘어와야 함)
    idx = _window_indices(doorbell=3, depth=128, limit=16)
    assert len(idx) == 16
    assert idx[0] == 2  # (doorbell - 1) % depth — 가장 최근이 맨 앞
    assert idx[-1] == (3 - 16) % 128  # 가장 오래된 게 맨 뒤


def test_queue_entries_default_is_doorbell_anchored_count_and_range():
    b = MockBackend()
    entries = b.get_queue_entries("nvme0", 1)  # limit/around_doorbell 기본값 그대로
    assert len(entries) == 16
    assert all(0 <= e.index < 128 for e in entries)  # nvme0 큐 depth=128


def test_completion_entries_default_is_doorbell_anchored_count_and_range():
    b = MockBackend()
    entries = b.get_completion_entries("nvme0", 1)
    assert len(entries) == 16
    assert all(0 <= e.index < 128 for e in entries)


def test_queue_not_found():
    b = MockBackend()
    with pytest.raises(QueueNotFoundError):
        b.get_queue_entries("nvme0", 99)


def test_prp_matches_entry_for_same_cid():
    b = MockBackend()
    entries = b.get_queue_entries("nvme0", 1, limit=10)
    prp_entry = next(e for e in entries if not e.uses_sgl)
    payload = b.get_prp_payload("nvme0", 1, prp_entry.cid)
    assert not payload.uses_sgl
    assert payload.pages
    assert payload.pages[0].phys_addr == prp_entry.prp1
    assert all(len(p.data) <= 4096 for p in payload.pages)


def test_prp_payload_capped_at_4kb():
    """요청사항: "PRP 확인"은 데이터 페이로드를 최대 4KB까지만 보여준다 —
    total_len이 더 커도(NLB 큰 write/read) 실제로 보여주는 바이트 합은 4096을
    넘지 않아야 한다(리스트 페이지 자체 같은 메타데이터 제외)."""
    b = MockBackend()
    # index%8==7 -> nlb=8 -> total_len=(8+1)*512=4608 > MAX_PAGE_DUMP(4096).
    entries = [b._synthetic_entry("nvme0", 1, i) for i in range(16)]
    big_entry = next(e for e in entries if not e.uses_sgl and (e.cdw12 & 0xFFFF) == 8)
    payload = b.get_prp_payload("nvme0", 1, big_entry.cid)
    assert payload.total_len > 4096
    data_pages = [p for p in payload.pages if not p.is_list_page]
    assert sum(len(p.data) for p in data_pages) <= 4096


def test_prp_sgl_short_circuits():
    b = MockBackend()
    entries = b.get_queue_entries("nvme0", 1, limit=10)
    sgl_entry = next(e for e in entries if e.uses_sgl)
    payload = b.get_prp_payload("nvme0", 1, sgl_entry.cid)
    assert payload.uses_sgl
    assert payload.pages == []


def test_tree_root_and_children():
    b = MockBackend()
    root = b.get_tree_node("nvme0", [])
    assert root.node.kind == "struct"
    names = {c.name for c in root.children}
    assert {"ctrl", "queues", "online_queues"} <= names


def test_tree_depth_cap_is_10():
    """요청사항 4/6: "최대 10 depth" — 11단계 경로는 서버가 거부해야 한다."""
    b = MockBackend()
    ok_path = ["ctrl", "pci_dev"] + ["bus", "self"] * 4  # len == 10, 허용
    exp_ok = b.get_tree_node("nvme0", ok_path)
    assert exp_ok.error is None

    too_deep = ["ctrl", "pci_dev"] + ["bus", "self"] * 5  # len == 12, 거부
    exp_bad = b.get_tree_node("nvme0", too_deep)
    assert exp_bad.error is not None
    assert "10" in exp_bad.error


def test_tree_cycle_back_to_root_via_queue_dev_pointer():
    """struct nvme_queue.dev 백포인터를 눌러 struct nvme_dev 루트로 순환되는지."""
    b = MockBackend()
    exp = b.get_tree_node("nvme0", ["queues", "[1]", "dev"])
    assert exp.node.type_name == "struct nvme_dev"
    assert "ctrl" in {c.name for c in exp.children}


def test_get_performance_covers_all_io_queues():
    """요청사항: "device/개별 queue별로 iops/bandwidth/latency"."""
    b = MockBackend()
    snap = b.get_device_snapshot("nvme0")
    perf = b.get_performance("nvme0")
    assert perf.available
    io_qids = {q.qid for q in snap.queues if not q.is_admin}
    assert {q.qid for q in perf.queues} == io_qids
    for q in perf.queues:
        assert q.iops == q.read_iops + q.write_iops
        assert q.bandwidth_bytes_per_sec >= 0
        assert q.avg_latency_us >= 0
