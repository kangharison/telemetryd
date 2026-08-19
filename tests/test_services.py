"""서비스 계층 구조 테스트 — 각 서비스가 **독립적으로** 성립하는지 본다.

여기 테스트가 통과한다는 건 "이 서비스를 별도 프로세스로 떼어낼 수 있다"는
뜻이다. 반대로 여기가 깨지면 서비스가 다시 God Object로 뭉치기 시작했다는 신호다.
"""
import pytest

from telemetryd.models import DevicePerf
from telemetryd.platform.ebpf import FileEbpfLogSource, NullEbpfLogSource
from telemetryd.services.perf import (
    SERVICE_NAME as PERF_SERVICE_NAME,
    EbpfPerfService,
    MockPerfService,
    PerfService,
)
from telemetryd.services.registry import ServiceNotRegisteredError, ServiceRegistry


# ---- 서비스 독립성 ---------------------------------------------------------

def test_perf_service_needs_only_an_ebpf_log_source():
    """성능 서비스는 커널 세션(drgn) 없이 만들어져야 한다 — DESIGN.md §6의
    역할 분담("eBPF=카운터, drgn=구조체 스냅샷")이 의존성으로 드러난 것.
    이게 성립해야 이 서비스를 drgn 권한 없는 프로세스로 뗄 수 있다."""
    svc = EbpfPerfService(NullEbpfLogSource())
    assert isinstance(svc, PerfService)


def test_both_implementations_satisfy_the_same_contract():
    """실구현과 mock이 같은 계약을 만족해야 조립 시점에 바꿔 끼울 수 있다
    (Strategy). 나중에 원격 클라이언트가 들어올 자리도 이 계약이다."""
    assert isinstance(EbpfPerfService(NullEbpfLogSource()), PerfService)
    assert isinstance(MockPerfService(), PerfService)


def test_perf_service_reports_missing_collector_instead_of_raising():
    """수집기가 없는 건 예외가 아니라 정상 상태다 — 대시보드가 그 사유를
    그대로 보여줘야 하므로 available=False + error 로 돌려준다."""
    perf = EbpfPerfService(NullEbpfLogSource()).get_performance("nvme0")
    assert isinstance(perf, DevicePerf)
    assert perf.available is False
    assert perf.error


def test_perf_service_reads_from_the_injected_log_source(tmp_path):
    """서비스는 "파일"이 아니라 주입된 EbpfLogSource에서 읽는다 — 소스를 갈아
    끼우면 서비스 코드 변경 없이 다른 곳에서 읽게 된다."""
    log = tmp_path / "nvme_perf.log"
    log.write_text(
        "@op_count[0, 1]: 100\n"
        "@read_count[0, 1]: 60\n"
        "@write_count[0, 1]: 40\n"
        "@bytes_sum[0, 1]: 4194304\n"
        "@lat_sum[0, 1]: 50000000\n"
        "@lat_count[0, 1]: 100\n"
        "---TICK---\n"
    )
    perf = EbpfPerfService(FileEbpfLogSource(str(log))).get_performance("nvme0")
    assert perf.available is True
    assert [q.qid for q in perf.queues] == [1]
    assert perf.queues[0].iops == 100.0


def test_mock_perf_service_is_deterministic_for_a_fixed_clock():
    """합성값은 결정적이어야 한다 — 난수면 테스트가 흔들리고, 화면에서도
    "진짜 같아" 보여 실측과 헷갈린다."""
    a = MockPerfService(queues_of=lambda d: 4, clock=lambda: 1000)
    b = MockPerfService(queues_of=lambda d: 4, clock=lambda: 1000)
    pa, pb = a.get_performance("nvme0"), b.get_performance("nvme0")
    assert [q.iops for q in pa.queues] == [q.iops for q in pb.queues]
    assert len(pa.queues) == 3          # admin 제외(1..online-1)


# ---- 레지스트리(조립) ------------------------------------------------------

def test_registry_registers_and_resolves():
    reg = ServiceRegistry().register(PERF_SERVICE_NAME, MockPerfService(), PerfService)
    assert reg.has(PERF_SERVICE_NAME)
    assert PERF_SERVICE_NAME in reg
    assert reg.names() == [PERF_SERVICE_NAME]
    assert isinstance(reg.get(PERF_SERVICE_NAME), PerfService)


def test_registry_rejects_a_service_that_breaks_the_contract():
    """조립 시점에 계약 위반을 잡는다 — 안 그러면 한참 뒤 호출할 때
    AttributeError로 터져서 원인을 찾기 어렵다."""
    class NotAPerfService:
        pass

    with pytest.raises(TypeError, match="계약"):
        ServiceRegistry().register(PERF_SERVICE_NAME, NotAPerfService(), PerfService)


def test_registry_gives_a_clear_error_for_unregistered_service():
    """서비스가 빠진 구성(예: 수집기 없이 띄움)에서 KeyError 대신 무엇이
    등록돼 있는지 알려준다."""
    reg = ServiceRegistry().register(PERF_SERVICE_NAME, MockPerfService(), PerfService)
    with pytest.raises(ServiceNotRegisteredError) as ei:
        reg.get("topology")
    assert "topology" in str(ei.value)
    assert PERF_SERVICE_NAME in str(ei.value)      # 등록된 것도 같이 알려줌


def test_registry_swaps_implementations_without_touching_callers():
    """같은 이름에 다른 구현을 넣어도 호출 측 코드는 그대로 — 모놀리스(인프로세스)
    와 마이크로서비스(원격 스텁)를 가르는 지점이 여기 하나라는 걸 고정한다."""
    def caller(reg):                      # 호출 측: 계약만 안다
        return reg.get(PERF_SERVICE_NAME).get_performance("nvme0")

    mock_reg = ServiceRegistry().register(PERF_SERVICE_NAME, MockPerfService(), PerfService)
    ebpf_reg = ServiceRegistry().register(
        PERF_SERVICE_NAME, EbpfPerfService(NullEbpfLogSource()), PerfService)

    assert caller(mock_reg).available is True
    assert caller(ebpf_reg).available is False     # 구현만 다르고 호출은 동일


# ---- 조립 루트(composition root) -------------------------------------------

def test_monolith_mock_registry_wires_services():
    from telemetryd.composition import build_monolith_mock

    reg = build_monolith_mock()
    assert reg.names() == ["events", "perf", "profiler"]
    assert reg.get("perf").get_performance("nvme0").available is True


def test_monolith_drgn_registry_wires_all_five_services():
    """drgn 조립은 서비스 5개를 전부 등록한다. 여기서는 커널에 실제로 붙지
    않는다 — DrgnKernelSession이 Program을 **지연 생성**하기 때문이라,
    조립 자체는 drgn 없이도 검증할 수 있다(그 지연 생성이 곧 이 테스트의
    가치이기도 하다)."""
    from telemetryd.composition import build_monolith_drgn

    reg = build_monolith_drgn(ebpf_log_path=None)
    assert reg.names() == ["events", "perf", "profiler", "queues", "topology"]


def test_services_share_one_kernel_session(tmp_path):
    """큐/토폴로지/프로파일러가 **같은** 커널 세션을 공유해야 한다.

    세션을 서비스마다 따로 만들면 캐시도 따로 생겨서, 비싼 조회가 서비스 수만큼
    반복된다 — DESIGN.md §9.16에서 대시보드를 멈춘 문제가 그대로 재현되는
    구조다. 조립 루트가 그걸 막고 있는지 고정한다."""
    from telemetryd.composition import build_monolith_drgn

    reg = build_monolith_drgn(ebpf_log_path=str(tmp_path / "log"))
    queues = reg.get("queues")
    profiler = reg.get("profiler")
    assert queues._kernel is profiler._kernel


def test_topology_gets_device_lookup_without_importing_queues():
    """토폴로지는 큐 서비스를 import하지 않고 콜러블만 주입받는다 — 이게
    지켜져야 두 서비스를 서로 다른 프로세스로 뗄 수 있다."""
    import ast, pathlib
    import telemetryd.services.topology.service as topo_mod

    src = pathlib.Path(topo_mod.__file__).read_text()
    imported = {n.module for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(m.startswith("telemetryd.services.queues") for m in imported)


# ---- 실기(real machine) 대비 ------------------------------------------------

def test_prp_dump_refuses_when_iommu_is_enabled(monkeypatch):
    """IOMMU가 켜진 실기에서 PRP를 **조용히 틀리게** 읽지 않는지 고정한다.

    PRP1/PRP2는 IOMMU가 켜지면 물리주소가 아니라 IOVA다. 덤프 코드는 그 값을
    physical=True로 읽으므로 IOVA를 그대로 읽으면 엉뚱한 메모리를 읽거나 빈
    페이지가 나온다 — QEMU 검증 환경(IOMMU off)에서는 절대 안 걸리지만 실기
    서버는 VT-d/AMD-Vi가 켜져 있는 경우가 흔하다. "조용히 틀린 값"을 내느니
    이유를 밝히고 안 읽는 게 맞다."""
    import telemetryd.services.queues.service as svc_mod

    monkeypatch.setattr(svc_mod, "_iommu_enabled", lambda: True)

    calls = []
    monkeypatch.setattr(svc_mod, "_decode_prp_pages",
                        lambda *a, **k: calls.append(1) or [])

    # PRP 경로까지 가는 최소 가짜 커널 객체들
    class _F:
        def __init__(self, v): self._v = v
        def __int__(self): return self._v

    class _Common:
        command_id, flags = _F(7), _F(0)          # PSDT=0 -> PRP 경로
        class dptr:
            prp1, prp2 = _F(0x1000), _F(0x2000)

    class _Cmd:
        common = _Common()

    class _Q:
        q_depth, sq_cmds = _F(1), object()

    class _Dev:
        online_queues = _F(1)
        queues = [_Q()]

    service = svc_mod.DrgnQueueService(kernel=type("K", (), {"program": lambda s: None})())
    monkeypatch.setattr(service, "lookup_device", lambda d: (_Dev(), object()))
    monkeypatch.setattr(service, "_resolve_total_len", lambda *a: (4096, None))
    # [한국어] service.py가 함수 안에서 `from drgn import cast` 하므로 모듈
    # 속성 패치로는 안 잡힌다 — drgn 모듈 자체를 패치해야 한다.
    import drgn
    monkeypatch.setattr(drgn, "cast", lambda *a: [_Cmd()])

    payload = service.get_prp_payload("nvme0", 0, 7)

    assert payload.pages == []
    assert "IOMMU" in payload.error
    assert not calls, "IOMMU가 켜졌는데도 물리주소를 읽으려 시도했다"
