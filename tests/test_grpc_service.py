"""gRPC wrapper 테스트 — 실제 grpc.aio 서버를 임시 포트(0=자동할당)에 띄우고
클라이언트로 붙어 RPC를 왕복시킨다. mock backend로만 검증한다(§DESIGN 0)."""
import asyncio

import grpc

from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.grpcserver import telemetryd_pb2_grpc as pb_grpc
from telemetryd.grpcserver.server import TelemetrydServicer


async def _with_server(fn):
    server = grpc.aio.server()
    pb_grpc.add_TelemetrydServicer_to_server(TelemetrydServicer("mock"), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.TelemetrydStub(channel)
            await fn(stub)
    finally:
        await server.stop(None)


def test_list_devices():
    async def go(stub):
        reply = await stub.ListDevices(pb.Empty())
        assert set(reply.names) == {"nvme0", "nvme1"}

    asyncio.run(_with_server(go))


def test_snapshot_and_queue_entries():
    async def go(stub):
        snap = await stub.GetDeviceSnapshot(pb.DeviceRequest(device="nvme0"))
        assert snap.online_queues == 3
        entries = await stub.GetQueueEntries(
            pb.QueueRequest(device="nvme0", qid=1, limit=3, around_doorbell=False)
        )
        assert len(entries.entries) == 3
        assert [e.cid for e in entries.entries] == [0, 1, 2]

    asyncio.run(_with_server(go))


def test_queue_entries_doorbell_anchored_default_count():
    async def go(stub):
        entries = await stub.GetQueueEntries(
            pb.QueueRequest(device="nvme0", qid=1, around_doorbell=True)
        )
        assert len(entries.entries) == 16  # limit=0 -> around_doorbell 경로에서 16 취급

    asyncio.run(_with_server(go))


def test_completion_entries_over_grpc():
    async def go(stub):
        reply = await stub.GetCompletionEntries(
            pb.QueueRequest(device="nvme0", qid=1, limit=5, around_doorbell=True)
        )
        assert len(reply.entries) == 5
        assert all(0 <= e.status_code_type <= 7 for e in reply.entries)

    asyncio.run(_with_server(go))


def test_prp_payload_over_grpc():
    async def go(stub):
        reply = await stub.GetPrpPayload(pb.PrpRequest(device="nvme0", qid=1, cid=1))
        assert not reply.uses_sgl
        assert reply.pages
        assert reply.pages[0].data  # bytes 필드가 실제로 넘어오는지

    asyncio.run(_with_server(go))


def test_not_found_maps_to_grpc_status():
    async def go(stub):
        try:
            await stub.GetDeviceSnapshot(pb.DeviceRequest(device="nvme9"))
            assert False, "NOT_FOUND 를 기대했음"
        except grpc.aio.AioRpcError as e:
            assert e.code() == grpc.StatusCode.NOT_FOUND

    asyncio.run(_with_server(go))


def test_tree_depth_cap_over_grpc():
    async def go(stub):
        path = ["ctrl", "pci_dev"] + ["bus", "self"] * 5  # depth=12 > 10
        reply = await stub.GetTreeNode(pb.TreeNodeRequest(device="nvme0", path=path))
        assert "10" in reply.error

    asyncio.run(_with_server(go))


def test_stream_metrics_yields_multiple_ticks():
    async def go(stub):
        n = 0
        async for _snap in stub.StreamDeviceMetrics(pb.StreamRequest(device="nvme0", interval_sec=0.1)):
            n += 1
            if n >= 2:
                break
        assert n == 2

    asyncio.run(_with_server(go))


def test_get_performance_over_grpc():
    """요청사항: "device/개별 queue별로 iops/bandwidth/latency"."""
    async def go(stub):
        reply = await stub.GetPerformance(pb.DeviceRequest(device="nvme0"))
        assert reply.available
        assert reply.queues
        for q in reply.queues:
            assert q.qid >= 1  # admin(0)은 mock에서 성능 지표 대상 아님
            assert q.iops >= 0
            assert q.bandwidth_bytes_per_sec >= 0
            assert q.avg_latency_us >= 0

    asyncio.run(_with_server(go))


def test_stream_performance_yields_multiple_ticks():
    async def go(stub):
        n = 0
        async for _perf in stub.StreamPerformance(pb.StreamRequest(device="nvme0", interval_sec=0.1)):
            n += 1
            if n >= 2:
                break
        assert n == 2

    asyncio.run(_with_server(go))
