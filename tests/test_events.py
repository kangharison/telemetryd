"""이벤트 계층(종류 무관 봉투 + 종류별 상세) 테스트.

이 파일이 지키는 계약은 하나다 — **어떤 특정 종류(지금은 timeout)가 이벤트
목록 전체를 대표하지 않는다**. 그래서 검증도 두 갈래로 나눠서 한다:

1. 공통 봉투(kind/observed_at/device/qid/summary)는 종류와 무관하게 항상 채워
   지고, 그것만으로 목록 한 줄을 그릴 수 있다.
2. 종류별 상세는 그 종류일 때만(proto oneof) 실리고, 모르는 종류가 와도 봉투는
   멀쩡히 전달된다(= UI/CLI가 안 깨진다).

특히 2번의 "모르는 종류"는 아직 구현되지 않은 리셋/AER 이벤트가 추가됐을 때를
미리 흉내낸 것이다 — 그때 이 테스트가 깨지면 목록 구조가 다시 timeout 전용으로
굳어버렸다는 뜻이다(DESIGN.md §9.11, models.NvmeEvent)."""
import asyncio

import grpc
from google.protobuf.json_format import MessageToDict

from telemetryd.backend import get_backend
from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.grpcserver import telemetryd_pb2_grpc as pb_grpc
from telemetryd.grpcserver.convert import nvme_event_to_pb
from telemetryd.grpcserver.server import TelemetrydServicer
from telemetryd.models import ErrorEventDetail, NvmeEvent, TimeoutEventDetail


def _timeout_event() -> NvmeEvent:
    return NvmeEvent(
        kind="timeout",
        observed_at=1_700_000_000.5,
        device="nvme0",
        qid=3,
        summary="write(0x01) 커맨드가 30.0s 동안 완료되지 않음 (qid=3, tag=42, nsid=1)",
        timeout=TimeoutEventDetail(
            tag=42, opcode=1, opcode_name="write", nsid=1, flags=0,
            cdw10=100, cdw11=0, cdw12=15, cdw13=0, cdw14=0, cdw15=0,
            elapsed_us=30_000_000.0,
        ),
    )


def _error_event() -> NvmeEvent:
    """kind="error" — 에러 status로 반환된 커맨드(요청 A2)."""
    return NvmeEvent(
        kind="error",
        observed_at=1_700_000_000.7,
        device="nvme0",
        qid=3,
        summary="read(0x02) 실패 — Media/Data Integrity/Unrecovered Read Error",
        error=ErrorEventDetail(
            cid=4137, tag=41, opcode=2, opcode_name="read", nsid=1,
            status=0x4281, sct=2, sc=0x81,
            sct_name="Media/Data Integrity", sc_name="Unrecovered Read Error",
            dnr=True, more=False, crd=0, retries=1,
            slba=123456, nlb=8, lba_valid=True, submit_cached=True,
            elapsed_us=250.0,
        ),
    )


def _unknown_kind_event() -> NvmeEvent:
    """아직 구현 안 된 종류(리셋 등)를 흉내 — 상세 슬롯이 비어 있는 봉투."""
    return NvmeEvent(
        kind="reset",
        observed_at=1_700_000_001.0,
        device="nvme0",
        qid=-1,
        summary="컨트롤러 리셋",
    )


def test_timeout_event_to_pb_fills_common_and_detail():
    msg = nvme_event_to_pb(_timeout_event())
    # 공통 봉투
    assert msg.kind == "timeout"
    assert msg.device == "nvme0"
    assert msg.qid == 3
    assert msg.summary.startswith("write(0x01)")
    # 종류별 상세는 oneof — kind에 해당하는 것 하나만 설정돼 있어야 한다.
    assert msg.WhichOneof("detail") == "timeout"
    assert msg.timeout.tag == 42
    assert msg.timeout.cdw12 == 15
    assert msg.timeout.elapsed_us == 30_000_000.0


def test_unknown_kind_keeps_envelope_without_detail():
    """모르는 종류라도 공통 필드는 그대로 전달되고, 상세 슬롯은 비어 있다."""
    msg = nvme_event_to_pb(_unknown_kind_event())
    assert msg.kind == "reset"
    assert msg.summary == "컨트롤러 리셋"
    assert msg.qid == -1
    assert msg.WhichOneof("detail") is None


def test_json_shape_matches_what_web_ui_expects():
    """web/app.py가 브라우저로 넘기는 JSON 모양 — 프론트가 이 키들로 종류별
    렌더러를 고르므로(있으면 상세 렌더, 없으면 요약만) 여기서 고정해 둔다."""
    reply = pb.EventsReply(
        device="nvme0",
        events=[nvme_event_to_pb(_timeout_event()), nvme_event_to_pb(_unknown_kind_event())],
    )
    d = MessageToDict(reply, preserving_proto_field_name=True,
                      always_print_fields_with_no_presence=True)
    ev_timeout, ev_reset = d["events"]

    for ev in (ev_timeout, ev_reset):   # 공통 필드는 종류와 무관하게 항상 있음
        assert {"kind", "observed_at", "device", "qid", "summary"} <= set(ev)

    # [한국어] oneof는 "설정된 멤버만" 직렬화된다 —
    # always_print_fields_with_no_presence는 presence가 없는 필드에만 적용되므로
    # 상세 키의 유무가 그대로 종류 판별에 쓰인다.
    assert "timeout" in ev_timeout
    assert ev_timeout["timeout"]["cdw12"] == 15
    assert "timeout" not in ev_reset


class _StubBackend:
    """이벤트가 실제로 있는 상황을 gRPC 왕복으로 확인하기 위한 최소 백엔드.

    MockBackend는 의도적으로 이벤트를 합성하지 않으므로(mock_backend.py 주석
    참고 — 안 일어난 장애를 일어난 것처럼 보이게 하지 않기 위함), 여기서만
    가짜 이벤트를 넣어 서버->클라이언트 직렬화 경로를 검증한다."""

    kind = "stub"

    def __init__(self):
        self._inner = get_backend("mock")

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_events(self, device):
        return [_timeout_event(), _unknown_kind_event()] if device == "nvme0" else []


async def _with_stub_server(fn):
    server = grpc.aio.server()
    servicer = TelemetrydServicer("mock")
    servicer._backend = _StubBackend()
    pb_grpc.add_TelemetrydServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            await fn(pb_grpc.TelemetrydStub(channel))
    finally:
        await server.stop(None)


def test_get_events_roundtrip_mixed_kinds():
    async def go(stub):
        reply = await stub.GetEvents(pb.DeviceRequest(device="nvme0"))
        assert reply.device == "nvme0"
        assert [e.kind for e in reply.events] == ["timeout", "reset"]
        assert reply.events[0].timeout.tag == 42
        assert reply.events[1].WhichOneof("detail") is None
        # 이벤트가 하나도 없는 디바이스는 빈 목록(에러 아님).
        empty = await stub.GetEvents(pb.DeviceRequest(device="nvme1"))
        assert list(empty.events) == []

    asyncio.run(_with_stub_server(go))


def test_stream_events_yields_multiple_ticks():
    async def go(stub):
        n = 0
        async for reply in stub.StreamEvents(pb.StreamRequest(device="nvme0", interval_sec=0.1)):
            n += 1
            assert len(reply.events) == 2
            if n >= 2:
                break
        assert n == 2

    asyncio.run(_with_stub_server(go))


def test_mock_backend_reports_no_events():
    backend = get_backend("mock")
    assert backend.get_events("nvme0") == []


def test_error_event_to_pb_uses_its_own_oneof_slot():
    """종류가 둘이 되어도 각자 자기 상세 슬롯만 채운다 — 봉투 구조의 핵심."""
    msg = nvme_event_to_pb(_error_event())
    assert msg.kind == "error"
    assert msg.WhichOneof("detail") == "error"
    assert msg.error.sct == 2 and msg.error.sc == 0x81
    assert msg.error.dnr is True
    assert msg.error.lba_valid is True and msg.error.slba == 123456

    # 타임아웃 이벤트는 여전히 timeout 슬롯만 — 서로 침범하지 않는다.
    tmsg = nvme_event_to_pb(_timeout_event())
    assert tmsg.WhichOneof("detail") == "timeout"


def test_broadcast_nsid_survives_pb_conversion():
    """NSID 0xFFFFFFFF(broadcast)는 정상 값인데 proto 필드가 int32면
    `ValueError: Value out of range`로 RPC 전체가 죽는다 — 실제로 라이브
    서버 로그에서 GetEvents/StreamEvents가 이걸로 계속 실패하고 있었다
    (§9.13에서 고쳤지만 회귀 테스트는 파서까지만 덮고 있어 이 변환 경로는
    비어 있었다). 파서 단위 테스트(test_ebpf_error_events.py)와 달리 여기선
    protobuf 직렬화까지 통과하는지를 본다."""
    ev = _error_event()
    ev.error.nsid = 0xFFFFFFFF
    msg = nvme_event_to_pb(ev)
    assert msg.error.nsid == 0xFFFFFFFF


def test_json_shape_distinguishes_kinds_by_detail_key():
    reply = pb.EventsReply(
        device="nvme0",
        events=[nvme_event_to_pb(e) for e in
                (_timeout_event(), _error_event(), _unknown_kind_event())],
    )
    d = MessageToDict(reply, preserving_proto_field_name=True,
                      always_print_fields_with_no_presence=True)
    ev_t, ev_e, ev_u = d["events"]
    assert "timeout" in ev_t and "error" not in ev_t
    assert "error" in ev_e and "timeout" not in ev_e
    assert "timeout" not in ev_u and "error" not in ev_u   # 모르는 종류는 요약만
    assert ev_e["error"]["sc_name"] == "Unrecovered Read Error"


def test_list_event_kinds_rpc_reports_registry():
    """"등록된 이벤트를 알 수 있는가" — UI/CLI가 하드코딩 대신 물어보는 경로."""
    async def go(stub):
        reply = await stub.ListEventKinds(pb.Empty())
        kinds = {k.kind: k for k in reply.kinds}
        assert {"timeout", "error"} <= set(kinds)
        assert "nvme_timeout" in kinds["timeout"].source
        assert "nvme_complete_rq" in kinds["error"].source
        # mock 백엔드에는 eBPF 수집기가 없으므로 "등록됐지만 미수집"이어야 한다.
        assert all(not k.active for k in reply.kinds)

    asyncio.run(_with_stub_server(go))


def test_get_error_stats_rpc_empty_on_mock():
    async def go(stub):
        reply = await stub.GetErrorStats(pb.DeviceRequest(device="nvme0"))
        assert reply.device == "nvme0"
        assert list(reply.counts) == [] and reply.total == 0
        assert reply.available is True   # "수집 중인데 0건"과 "수집기 없음"은 다름

    asyncio.run(_with_stub_server(go))
