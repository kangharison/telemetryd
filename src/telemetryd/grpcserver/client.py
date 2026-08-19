"""FastAPI 웹 서버가 쓰는 grpc.aio 클라이언트.

telemetryd gRPC 서버(기본 :50051)를 호출해서 REST/WebSocket으로 브라우저에
재노출한다(DESIGN.md §3 — 브라우저가 grpc-web으로 직접 부르지 않고, 이
프로세스가 대신 gRPC를 호출하는 구조).
"""
from __future__ import annotations

from typing import AsyncIterator, List, Optional

import grpc

from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.grpcserver import telemetryd_pb2_grpc as pb_grpc


class TelemetrydClient:
    def __init__(self, target: str = "localhost:50051"):
        self._target = target
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[pb_grpc.TelemetrydStub] = None

    def _ensure(self) -> pb_grpc.TelemetrydStub:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self._target)
            self._stub = pb_grpc.TelemetrydStub(self._channel)
        return self._stub

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def list_devices(self) -> List[str]:
        reply = await self._ensure().ListDevices(pb.Empty())
        return list(reply.names)

    async def get_device_snapshot(self, device: str) -> pb.DeviceSnapshot:
        return await self._ensure().GetDeviceSnapshot(pb.DeviceRequest(device=device))

    async def get_queue_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> pb.QueueEntriesReply:
        req = pb.QueueRequest(device=device, qid=qid, limit=limit, around_doorbell=around_doorbell)
        return await self._ensure().GetQueueEntries(req)

    async def get_completion_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> pb.CompletionEntriesReply:
        req = pb.QueueRequest(device=device, qid=qid, limit=limit, around_doorbell=around_doorbell)
        return await self._ensure().GetCompletionEntries(req)

    async def get_prp_payload(self, device: str, qid: int, cid: int) -> pb.PrpPayloadReply:
        return await self._ensure().GetPrpPayload(pb.PrpRequest(device=device, qid=qid, cid=cid))

    async def get_tree_node(self, device: str, path: List[str]) -> pb.TreeNodeReply:
        return await self._ensure().GetTreeNode(pb.TreeNodeRequest(device=device, path=path))

    async def stream_device_metrics(
        self, device: str, interval_sec: float = 0.5
    ) -> AsyncIterator[pb.DeviceSnapshot]:
        req = pb.StreamRequest(device=device, interval_sec=interval_sec)
        async for snap in self._ensure().StreamDeviceMetrics(req):
            yield snap

    async def get_performance(self, device: str) -> pb.DevicePerf:
        return await self._ensure().GetPerformance(pb.DeviceRequest(device=device))

    async def stream_performance(
        self, device: str, interval_sec: float = 1.0
    ) -> AsyncIterator[pb.DevicePerf]:
        req = pb.StreamRequest(device=device, interval_sec=interval_sec)
        async for perf in self._ensure().StreamPerformance(req):
            yield perf

    async def get_error_stats(self, device: str) -> pb.ErrorStatsReply:
        return await self._ensure().GetErrorStats(pb.DeviceRequest(device=device))

    async def list_processes(self, only_io: bool = False) -> pb.ProcessListReply:
        return await self._ensure().ListProcesses(pb.ProcessListRequest(only_io=only_io))

    async def list_targets(self) -> pb.TargetsReply:
        return await self._ensure().ListTargets(pb.Empty())

    async def add_target(self, kind: str, value: str, adapter: str = "") -> pb.TargetsReply:
        return await self._ensure().AddTarget(
            pb.TargetRuleMsg(kind=kind, value=value, adapter=adapter))

    async def remove_target(self, kind: str, value: str) -> pb.TargetsReply:
        return await self._ensure().RemoveTarget(pb.TargetRuleMsg(kind=kind, value=value))

    async def get_profile(self) -> pb.ProfileReply:
        return await self._ensure().GetProfile(pb.Empty())

    async def stream_profile(self, interval_sec: float = 2.0):
        req = pb.StreamRequest(device="", interval_sec=interval_sec)
        async for reply in self._ensure().StreamProfile(req):
            yield reply

    async def get_topology(self) -> pb.TopologyReply:
        return await self._ensure().GetTopology(pb.Empty())

    async def list_event_kinds(self) -> pb.EventKindsReply:
        return await self._ensure().ListEventKinds(pb.Empty())

    async def get_events(self, device: str) -> pb.EventsReply:
        return await self._ensure().GetEvents(pb.DeviceRequest(device=device))

    async def stream_events(
        self, device: str, interval_sec: float = 1.0
    ) -> AsyncIterator[pb.EventsReply]:
        req = pb.StreamRequest(device=device, interval_sec=interval_sec)
        async for reply in self._ensure().StreamEvents(req):
            yield reply
