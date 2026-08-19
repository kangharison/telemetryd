"""CLI(순수 라이브러리 import) 테스트 — click.testing.CliRunner로 실제
서브커맨드를 실행해서 mock backend 결과가 기대한 형식/값으로 나오는지 확인."""
from click.testing import CliRunner

from telemetryd.cli.main import cli


def _run(*args):
    result = CliRunner().invoke(cli, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_devices():
    out = _run("devices")
    assert "nvme0" in out and "nvme1" in out


def test_snapshot():
    out = _run("snapshot", "nvme0")
    assert "online_queues=3" in out
    assert "ADMIN" in out and "IO" in out


def test_queue_entries_from_start():
    out = _run("queue", "nvme0", "1", "--limit", "3", "--from-start")
    assert "cid=0" in out
    assert "cdw12=" in out


def test_queue_entries_default_is_doorbell_anchored():
    """--from-start 없이 기본으로 실행하면 sq_tail 도어벨 기준 16개(요청사항)."""
    out = _run("queue", "nvme0", "1")
    assert out.count("] cid=") == 16
    assert "cdw12=" in out


def test_cq_entries_default_is_doorbell_anchored():
    out = _run("cq", "nvme0", "1")
    assert out.count("] cid=") == 16
    assert "sq_head=" in out and "status_raw=" in out


def test_prp_prp_case():
    out = _run("prp", "nvme0", "1", "1")  # cid=1 -> uses_sgl False (1%3 != 2)
    assert "total_len=" in out


def test_prp_sgl_case():
    out = _run("prp", "nvme0", "1", "2")  # cid=2 -> uses_sgl True (2%3 == 2)
    assert "SGL" in out


def test_tree_root():
    out = _run("tree", "nvme0")
    assert "struct nvme_dev" in out
    assert "ctrl" in out


def test_tree_depth_cap_rejected():
    args = ["tree", "nvme0", "ctrl", "pci_dev"] + ["bus", "self"] * 5  # depth=12
    result = CliRunner().invoke(cli, args)
    assert "depth" in result.output.lower() or "10" in result.output


def test_unknown_device_is_error():
    result = CliRunner().invoke(cli, ["snapshot", "nvme9"])
    assert result.exit_code != 0


def test_doctor_mock_short_circuits():
    out = _run("doctor")
    assert "mock" in out


def test_events_empty_on_mock():
    # [한국어] mock은 이벤트를 합성하지 않으므로 "이벤트 없음"이 정상 출력이다.
    out = _run("events", "nvme0")
    assert "이벤트 없음" in out


def test_events_kind_filter_is_accepted():
    """--kind는 목록을 특정 종류로 좁히는 용도 — 종류가 여러 개일 수 있다는
    전제 자체가 CLI 인터페이스에도 반영돼 있어야 한다(timeout 전용 커맨드가
    아니어야 한다)."""
    out = _run("events", "nvme0", "--kind", "timeout")
    assert "종류=timeout" in out


def test_events_unknown_device():
    result = CliRunner().invoke(cli, ["events", "nvme9"])
    assert result.exit_code != 0
    assert "디바이스 없음" in result.output


def test_event_kinds_lists_registered_kinds():
    """등록된 이벤트 종류를 CLI로도 확인할 수 있어야 한다(하드코딩된 문구가
    아니라 레지스트리에서 나온다)."""
    out = _run("event-kinds")
    assert "timeout" in out and "error" in out
    assert "nvme_complete_rq" in out       # 에러 종류의 출처(eBPF 훅)
    assert "미수집" in out                  # mock에는 수집기가 없음


def test_error_stats_empty_on_mock():
    out = _run("error-stats", "nvme0")
    assert "0건" in out


def test_topology_tree_shows_both_layers():
    """CLI 통합 트리 — PCIe 계보(브리지/엔드포인트)와 NVMe 계층(컨트롤러/
    서브시스템/네임스페이스/큐)이 한 출력에 이어져 나와야 한다."""
    out = _run("topology")
    for expected in ("pci0000:00", "0000:00:02.0", "0000:00:04.0",
                     "nvme0", "nvme-subsys0", "nvme0n1", "큐 3개"):
        assert expected in out, f"{expected} 가 트리에 없음"
    assert "└─" in out and "├─" in out      # 트리 연결선


def test_topology_details_flag():
    out = _run("topology", "--details")
    assert "vendor:device" in out and "nsid" in out
