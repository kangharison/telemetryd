"""telemetryd 웹 대시보드 서버 — DESIGN.md §3.

브라우저는 HTTP/2 트레일러가 필요한 gRPC를 직접 못 부른다(grpc-web/Envoy
없이는). 그래서 이 FastAPI 프로세스 자신이 grpc.aio *클라이언트*가 되어
telemetryd gRPC 서버(기본 :50051)를 호출하고, 결과를 REST(JSON) + WebSocket
으로 브라우저에 재노출한다.

"실시간/비실시간" 갱신 요구사항은 이렇게 나뉜다:
  - 실시간: /ws/devices/{device}/metrics — StreamDeviceMetrics를 그대로
    WebSocket으로 흘려보내 큐 테이블(sq_tail/cq_head/inflight)이 자동 갱신.
  - 비실시간: /api/.../entries, /api/.../prp/{cid}, /api/.../tree — 사용자가
    큐를 클릭하거나 "PRP 확인"을 누를 때만 1회 호출되는 REST.

실행: uvicorn telemetryd.web.app:app --port 8000
      (미리 telemetryd gRPC 서버가 떠 있어야 함: python -m telemetryd.grpcserver.server)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List

import grpc
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.protobuf.json_format import MessageToDict

from telemetryd.grpcserver.client import TelemetrydClient

_STATIC_DIR = Path(__file__).parent / "static"
_TARGET = os.environ.get("TELEMETRYD_GRPC_TARGET", "localhost:50051")

app = FastAPI(title="telemetryd web")
_client = TelemetrydClient(_TARGET)


def _dict(msg) -> dict:
    # [한국어] always_print_fields_with_no_presence 없이는 값이 proto 기본값(0,
    # false, "")과 같은 필드를 JSON에서 통째로 생략해버린다 — admin 큐의
    # qid=0/index=0가 이래서 빠져서, 프론트 JS의 q.qid가 undefined가 되고
    # "IO Q -> ADMIN Q" 전환 시 API가 /queues/undefined/entries로 호출돼
    # 조용히 실패하는 버그로 이어졌다(실사용 중 발견). 전 필드에 적용해 같은
    # 종류의 문제(hctx_index=0, sq_tail=0, phase=false 등)를 한 번에 막는다.
    return MessageToDict(msg, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


def _prp_dict(reply) -> dict:
    # [한국어] PrpPage.data는 bytes라 MessageToDict가 base64로 인코딩해버린다.
    #  프론트에서 바로 hexdump하기 좋게 여기서 hex 문자열로 직접 변환한다.
    return {
        "device": reply.device,
        "qid": reply.qid,
        "cid": reply.cid,
        "uses_sgl": reply.uses_sgl,
        "total_len": reply.total_len,
        "error": reply.error,
        "pages": [
            {
                "phys_addr": p.phys_addr,
                "offset_in_page": p.offset_in_page,
                "is_list_page": p.is_list_page,
                "data_hex": p.data.hex(),
            }
            for p in reply.pages
        ],
    }


async def _call(coro):
    try:
        return await coro
    except grpc.aio.AioRpcError as e:
        status = 404 if e.code() == grpc.StatusCode.NOT_FOUND else 502
        raise HTTPException(status_code=status, detail=e.details() or str(e.code()))


async def _watch_disconnect(websocket: WebSocket) -> None:
    # [한국어] 이 WS 핸들러들은 서버->브라우저로 보내기만 하고 receive()는
    # 안 불러서, 브라우저가 먼저 끊어도(탭 전환 등으로 ws.close() 호출) 그걸
    # "다음 걸 보내려다 실패"할 때까지는 전혀 모른다 — 그런데 "다음 것"은
    # backend 조회(느리면 초 단위, drgn/QMP 백엔드 기준 실측 ~4초)가 끝나야
    # 나온다. 그래서 receive()를 별도 태스크로 계속 돌려 websocket.disconnect
    # 를 즉시 감지하고, _stream_ws()가 그걸 진행 중인 backend 조회와 경쟁시켜
    # 끊기자마자 취소할 수 있게 한다.
    while True:
        msg = await websocket.receive()
        if msg.get("type") == "websocket.disconnect":
            return


async def _stream_ws(websocket: WebSocket, gen) -> None:
    """gen(비동기 제너레이터)의 각 아이템을 JSON으로 websocket에 보낸다.

    "다음 아이템 기다리기"와 "클라이언트 끊김 감지"를 asyncio.wait로 경합시켜,
    backend 조회가 오래 걸리는 동안에도(§9.7 근처에서 실측된 문제 — 탭을
    빠르게 반복 전환하면 아직 시작 안 한 backend 호출들이 executor 큐에
    쌓여서 그 뒤 REST 조회까지 수십 초씩 밀렸다) 끊김을 즉시 알아채 아직
    시작 안 한(또는 이제 막 시작한) 조회를 곧바로 취소한다 — 이미 실행 중인
    호출 자체는 스레드라 강제로 못 멈추지만, 적어도 뒤에 쌓이는 걸 막는다."""
    disconnect_task = asyncio.ensure_future(_watch_disconnect(websocket))
    try:
        while True:
            next_task = asyncio.ensure_future(gen.__anext__())
            done, _pending = await asyncio.wait(
                {next_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect_task in done:
                next_task.cancel()
                # [한국어] cancel()은 취소를 "요청"만 할 뿐 즉시 반영되지 않는다 —
                # gen.__anext__()이 실제로 CancelledError를 받아 완전히 풀릴
                # 때까지 await로 기다려야 한다. 이걸 안 기다리고 바로 아래
                # finally의 gen.aclose()를 부르면 "generator가 아직 실행
                # 중"이라며 RuntimeError가 나서(실측: 재현됨) 이 WS가 죽고,
                # 클라이언트 자동재연결이 같은 경쟁을 계속 반복해 스냅샷이
                # 영영 안 뜨는 것처럼 보인다.
                try:
                    await next_task
                except BaseException:
                    pass
                return
            item = next_task.result()  # StopAsyncIteration이면 아래 except로
            await websocket.send_json(_dict(item))
    except StopAsyncIteration:
        return
    except WebSocketDisconnect:
        return
    finally:
        disconnect_task.cancel()
        try:
            await disconnect_task
        except BaseException:
            pass
        try:
            await gen.aclose()
        except RuntimeError:
            # [한국어] 위에서 next_task를 항상 기다리므로 정상 경로에선 이제
            # 안 나야 하지만, 방어적으로 한 번 더 막아 이 WS 핸들러 자체가
            # 죽는 일은 없게 한다.
            pass


@app.get("/api/devices")
async def api_devices():
    return await _call(_client.list_devices())


@app.get("/api/devices/{device}/snapshot")
async def api_snapshot(device: str):
    return _dict(await _call(_client.get_device_snapshot(device)))


@app.get("/api/devices/{device}/queues/{qid}/entries")
async def api_entries(device: str, qid: int, limit: int = 16, around_doorbell: bool = True):
    return _dict(await _call(_client.get_queue_entries(device, qid, limit, around_doorbell)))


@app.get("/api/devices/{device}/queues/{qid}/completions")
async def api_completions(device: str, qid: int, limit: int = 16, around_doorbell: bool = True):
    return _dict(await _call(_client.get_completion_entries(device, qid, limit, around_doorbell)))


@app.get("/api/devices/{device}/queues/{qid}/prp/{cid}")
async def api_prp(device: str, qid: int, cid: int):
    return _prp_dict(await _call(_client.get_prp_payload(device, qid, cid)))


@app.get("/api/devices/{device}/tree")
async def api_tree(device: str, path: List[str] = Query(default=[])):
    return _dict(await _call(_client.get_tree_node(device, path)))


@app.get("/api/devices/{device}/performance")
async def api_performance(device: str):
    return _dict(await _call(_client.get_performance(device)))


@app.get("/api/devices/{device}/events")
async def api_events(device: str):
    """이 디바이스의 최근 NVMe 이벤트 목록(종류 무관 — timeout/reset/… 이 섞임).

    응답의 각 원소는 공통 필드(kind/observed_at/device/qid/summary) + kind에
    해당하는 상세 키 하나(현재는 "timeout")를 갖는다. proto의 oneof는 설정된
    멤버만 JSON에 실리므로(_dict의 always_print_fields_with_no_presence는
    presence가 없는 필드에만 적용됨), 프론트는 그 키의 존재 여부로 종류별
    렌더러를 고르면 된다."""
    return _dict(await _call(_client.get_events(device)))


@app.get("/api/devices/{device}/error-stats")
async def api_error_stats(device: str):
    """에러 완료(SCT/SC) 조합별 누적 카운터 — 이벤트 목록과 별개의 집계.

    목록은 개별 건(최근 200개, 폭주 시 샘플링될 수 있음)이고 이건 전수 누적이라
    "실제로 몇 번 났는지"는 이쪽이 정확하다(DESIGN.md §9.13)."""
    return _dict(await _call(_client.get_error_stats(device)))


@app.get("/api/processes")
async def api_processes(only_io: bool = True):
    """대상 후보 프로세스 목록. 기본 필터가 "NVMe I/O 발행 중"이다 — 전체를
    나열하면 수백 개라 고를 수 없지만, I/O를 내는 프로세스는 보통 한 자릿수라
    이름을 몰라도 여기서 시작할 수 있다(명세 1-1 (d), 1-3)."""
    return _dict(await _call(_client.list_processes(only_io)))


@app.get("/api/targets")
async def api_targets():
    return _dict(await _call(_client.list_targets()))


@app.post("/api/targets")
async def api_add_target(rule: dict = Body(...)):
    """대상 규칙 추가 — 관측 중에도 데몬 재시작 없이 대상을 늘릴 수 있다(1-3)."""
    return _dict(await _call(_client.add_target(
        rule.get("kind", ""), str(rule.get("value", "")), rule.get("adapter", ""))))


@app.delete("/api/targets")
async def api_remove_target(kind: str, value: str):
    return _dict(await _call(_client.remove_target(kind, value)))


@app.get("/api/profile")
async def api_profile():
    """프로파일 스냅샷(schema 2.0) — 세션/논리 그룹/기대 대조/미관측 I/O."""
    return _dict(await _call(_client.get_profile()))


@app.websocket("/ws/profile")
async def ws_profile(websocket: WebSocket, interval: float = 2.0):
    """프로파일 실시간 채널. 기본 2초 — 프로세스 목록 스캔이 섞여 있어 1초보다
    느리게 잡는다(명세 7-4: 목록 스캔은 저빈도로 충분)."""
    await websocket.accept()
    gen = _client.stream_profile(interval)
    try:
        await _stream_ws(websocket, gen)
    except grpc.aio.AioRpcError as e:
        await websocket.close(code=1011, reason=e.details() or "grpc error")


@app.get("/api/topology")
async def api_topology():
    """PCIe 토폴로지 + NVMe 서브시스템 통합 트리(시스템 전체 1개).

    drgn 라이브 조회라 응답이 수 초 걸릴 수 있다(디바이스마다 커널 구조체를
    여러 번 읽는다) — 프론트는 "조회 중" 표시부터 먼저 그린다."""
    return _dict(await _call(_client.get_topology()))


@app.get("/api/event-kinds")
async def api_event_kinds():
    """등록된 이벤트 종류 목록 — 프론트가 "무엇을 수집 중인지"를 하드코딩하지
    않고 이 목록을 그려서, 종류가 늘어도 화면 문구가 어긋나지 않게 한다."""
    return _dict(await _call(_client.list_event_kinds()))


@app.websocket("/ws/devices/{device}/performance")
async def ws_performance(websocket: WebSocket, device: str, interval: float = 1.0):
    """실시간 성능 채널 — eBPF(nvme_perf.bt) 집계를 StreamPerformance로 중계
    (요청사항: "device/개별 queue별로 iops/bandwidth/latency" 실시간)."""
    await websocket.accept()
    gen = _client.stream_performance(device, interval)
    try:
        await _stream_ws(websocket, gen)
    except grpc.aio.AioRpcError as e:
        await websocket.close(code=1011, reason=e.details() or "grpc error")


@app.websocket("/ws/devices/{device}/events")
async def ws_events(websocket: WebSocket, device: str, interval: float = 1.0):
    """실시간 이벤트 리스트 채널 — StreamEvents를 그대로 중계(웹 대시보드의
    "이벤트" 탭). 지금 실려오는 건 kprobe:nvme_timeout이 잡은 kind="timeout"
    뿐이지만, 이 채널 자체는 종류를 가리지 않는다(DESIGN.md §9.11)."""
    await websocket.accept()
    gen = _client.stream_events(device, interval)
    try:
        await _stream_ws(websocket, gen)
    except grpc.aio.AioRpcError as e:
        await websocket.close(code=1011, reason=e.details() or "grpc error")


@app.websocket("/ws/devices/{device}/metrics")
async def ws_metrics(websocket: WebSocket, device: str, interval: float = 0.5):
    """실시간 갱신 채널 — StreamDeviceMetrics를 그대로 WebSocket으로 중계."""
    await websocket.accept()
    gen = _client.stream_device_metrics(device, interval)
    try:
        await _stream_ws(websocket, gen)
    except grpc.aio.AioRpcError as e:
        await websocket.close(code=1011, reason=e.details() or "grpc error")


@app.on_event("shutdown")
async def _shutdown():
    await _client.close()


@app.get("/")
async def index():
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
