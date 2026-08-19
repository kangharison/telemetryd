"""grpc.aio 기반 Telemetryd 서비스 서버.

로직은 없다 — backend.Backend 하나를 골라 호출하고 결과를 convert.py로
직렬화할 뿐(DESIGN.md §2, "wrapper"). 매 호출마다 backend에 새로 조회하므로
(mock은 tick 기반 합성, drgn은 매번 실제 /proc/kcore 조회) StreamDeviceMetrics
가 그대로 "실시간 폴링"이 된다(interval은 요청의 interval_sec, 기본 0.5초).

실행: python -m telemetryd.grpcserver.server --backend mock --port 50051
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import logging

import grpc

from telemetryd.backend import get_backend
from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.grpcserver import telemetryd_pb2_grpc as pb_grpc
from telemetryd.grpcserver.convert import (
    completion_to_pb,
    device_perf_to_pb,
    device_to_pb,
    entry_to_pb,
    error_stats_to_pb,
    event_kind_to_pb,
    nvme_event_to_pb,
    prp_to_pb,
    process_entry_to_pb,
    profile_to_pb,
    target_rule_to_pb,
    topology_to_pb,
    tree_expansion_to_pb,
)

logger = logging.getLogger("telemetryd.grpcserver")


class TelemetrydServicer(pb_grpc.TelemetrydServicer):
    def __init__(self, backend_kind: str = "mock", **backend_kwargs):
        self._backend = get_backend(backend_kind, **backend_kwargs)
        self._backend_kind = backend_kind
        # [한국어] drgn/QMP 백엔드는 QMP 유닉스 소켓 커넥션 하나로 동작해서
        # 동시 접근이 안전하지 않다(§9.2/§9.7의 "QMP는 클라이언트 1개만"과
        # 같은 이유가 프로세스 내부 스레드 차원에서도 적용됨). worker 1개짜리
        # executor로 모든 backend 호출을 강제 직렬화하되, asyncio 이벤트
        # 루프 자체는 막지 않는다 — 실측 결과 get_device_snapshot() 1번
        # 호출이 QMP 라이브 메모리 조회 때문에 4초 가까이 걸리는데, 이걸
        # 이벤트 루프에서 동기 호출로 그냥 부르면 그 4초 동안 StreamPerformance
        # 같은 무관한 다른 RPC까지 전부 멈춘다(실제로 재현됨: 탭을 빠르게
        # 반복 전환하니 그 뒤 단순 REST 스냅샷 조회가 45초 넘게 걸렸음 —
        # 밀린 블로킹 호출들이 한 이벤트 루프 스레드에서 순서대로 쌓였기 때문).
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="telemetryd-backend"
        )

    async def _run(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def ListDevices(self, request, context):
        # [한국어] 이것만 _run()을 안 거치고 이벤트 루프에서 self._backend를
        # 직접 불렀던 게 실제 버그였다 — StreamDeviceMetrics(실행 중인 WS가
        # 있으면 executor 스레드에서 계속 돎)와 이 호출이 같은 drgn Program을
        # 동시에 다른 스레드에서 건드리면서 "recursive address translation;
        # page table may be missing" 같은 오류로 나타났다(§9.8에서 도입한
        # 단일 워커 executor 직렬화를 이 핸들러 하나가 빠뜨리고 있었음).
        names = await self._run(self._backend.list_devices)
        return pb.DeviceListReply(names=names)

    async def GetDeviceSnapshot(self, request, context):
        try:
            snap = await self._run(self._backend.get_device_snapshot, request.device)
        except DeviceNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
        return device_to_pb(snap)

    async def GetQueueEntries(self, request, context):
        try:
            entries = await self._run(
                self._backend.get_queue_entries,
                request.device, request.qid, request.limit, request.around_doorbell,
            )
        except DeviceNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
        except QueueNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"qid not found: {request.qid}")
        return pb.QueueEntriesReply(device=request.device, qid=request.qid,
                                     entries=[entry_to_pb(e) for e in entries])

    async def GetCompletionEntries(self, request, context):
        try:
            entries = await self._run(
                self._backend.get_completion_entries,
                request.device, request.qid, request.limit, request.around_doorbell,
            )
        except DeviceNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
        except QueueNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"qid not found: {request.qid}")
        return pb.CompletionEntriesReply(device=request.device, qid=request.qid,
                                          entries=[completion_to_pb(e) for e in entries])

    async def GetPrpPayload(self, request, context):
        try:
            payload = await self._run(
                self._backend.get_prp_payload, request.device, request.qid, request.cid
            )
        except DeviceNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
        except QueueNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"qid not found: {request.qid}")
        return prp_to_pb(payload)

    async def GetTreeNode(self, request, context):
        try:
            exp = await self._run(self._backend.get_tree_node, request.device, list(request.path))
        except DeviceNotFoundError:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
        return tree_expansion_to_pb(exp)

    async def StreamDeviceMetrics(self, request, context):
        # [한국어] grpc.aio의 ServicerContext에는 sync API의 is_active() 가 없다.
        #  클라이언트가 끊으면 이 코루틴 자체가 asyncio.CancelledError로 취소되므로
        #  그걸 잡아서 조용히 끝내는 것으로 충분하다. get_device_snapshot()은
        #  위 _run()을 통해 별도 스레드에서 실행되므로, 이 루프가 대기하는
        #  동안에도(회당 최대 수 초) 이벤트 루프가 다른 RPC를 계속 처리한다.
        interval = request.interval_sec or 0.5
        try:
            while True:
                try:
                    snap = await self._run(self._backend.get_device_snapshot, request.device)
                except DeviceNotFoundError:
                    await context.abort(grpc.StatusCode.NOT_FOUND, f"device not found: {request.device}")
                    return
                yield device_to_pb(snap)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def GetPerformance(self, request, context):
        perf = self._backend.get_performance(request.device)
        return device_perf_to_pb(perf)

    async def StreamPerformance(self, request, context):
        # [한국어] StreamDeviceMetrics와 같은 패턴 — eBPF 로그는 1초 틱이라
        # interval을 그보다 짧게 줘도 같은 틱을 반복해서 보낼 뿐(해로울 것 없음).
        interval = request.interval_sec or 1.0
        try:
            while True:
                perf = self._backend.get_performance(request.device)
                yield device_perf_to_pb(perf)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def GetEvents(self, request, context):
        # [한국어] get_performance()와 같은 이유로 _run()(drgn/QMP 직렬화용
        # executor) 없이 이벤트 루프에서 직접 부른다 — 순수 파일 tail 읽기라
        # drgn Program을 안 건드림(§9.11).
        events = self._backend.get_events(request.device)
        return pb.EventsReply(
            device=request.device, events=[nvme_event_to_pb(e) for e in events]
        )

    async def GetErrorStats(self, request, context):
        # [한국어] GetEvents와 같은 이유로 executor 없이 직접 호출 — 로그 파일
        # 끝부분만 읽는 순수 파일 I/O라 drgn Program을 안 건드린다.
        return error_stats_to_pb(self._backend.get_error_stats(request.device))

    async def GetTopology(self, request, context):
        # [한국어] 이건 이벤트/성능과 달리 **drgn 조회**라 반드시 _run()(단일
        # 워커 executor)으로 돌린다 — 디바이스마다 커널 메모리를 여러 번 읽어
        # QMP 백엔드 기준 수 초가 걸리는데, 이벤트 루프에서 직접 부르면 그동안
        # 다른 RPC가 전부 멈춘다(§9.8에서 실측한 문제와 같은 부류).
        return topology_to_pb(await self._run(self._backend.get_topology))

    # ---- NVMe I/O 프로세스 프로파일러 ------------------------------------

    async def ListProcesses(self, request, context):
        # [한국어] 프로세스 목록은 drgn으로 task를 순회하므로 반드시 executor로.
        entries = await self._run(self._backend.list_processes, request.only_io)
        return pb.ProcessListReply(processes=[process_entry_to_pb(e) for e in entries])

    async def ListTargets(self, request, context):
        # 규칙 조회는 파일/메모리라 executor 불필요.
        return pb.TargetsReply(rules=[target_rule_to_pb(r) for r in self._backend.list_targets()])

    async def AddTarget(self, request, context):
        from telemetryd.models import TargetRule

        rule = TargetRule(kind=request.kind, value=request.value,
                          adapter=request.adapter or None)
        try:
            rules = self._backend.add_target(rule)
        except ValueError as e:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
        return pb.TargetsReply(rules=[target_rule_to_pb(r) for r in rules])

    async def RemoveTarget(self, request, context):
        rules = self._backend.remove_target(request.kind, request.value)
        return pb.TargetsReply(rules=[target_rule_to_pb(r) for r in rules])

    async def GetProfile(self, request, context):
        # [한국어] 프로세스 목록(drgn) + eBPF 로그를 함께 보므로 executor 경유.
        return profile_to_pb(await self._run(self._backend.get_profile))

    async def StreamProfile(self, request, context):
        interval = request.interval_sec or 2.0
        try:
            while True:
                yield profile_to_pb(await self._run(self._backend.get_profile))
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    async def ListEventKinds(self, request, context):
        # [한국어] 정적인 등록 목록 조회(백엔드 구성만 반영) — 역시 drgn 무관.
        return pb.EventKindsReply(
            kinds=[event_kind_to_pb(k) for k in self._backend.list_event_kinds()]
        )

    async def StreamEvents(self, request, context):
        # [한국어] StreamPerformance와 같은 패턴 — 매 interval마다 현재 보유한
        # 최근 이벤트 전체(최대 200개, 작음)를 다시 통째로 보낸다. 새 이벤트가
        # 없으면 같은 목록을 반복 전송할 뿐이라 해로울 게 없고, 클라이언트
        # 쪽 구현이 "델타만 추적" 안 해도 돼 단순하다.
        interval = request.interval_sec or 1.0
        try:
            while True:
                events = self._backend.get_events(request.device)
                yield pb.EventsReply(
                    device=request.device, events=[nvme_event_to_pb(e) for e in events]
                )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


async def serve(backend_kind: str = "mock", port: int = 50051, **backend_kwargs) -> None:
    server = grpc.aio.server()
    pb_grpc.add_TelemetrydServicer_to_server(TelemetrydServicer(backend_kind, **backend_kwargs), server)
    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    logger.info("telemetryd grpc server listening on %s (backend=%s)", listen_addr, backend_kind)
    await server.start()
    await server.wait_for_termination()


def main() -> None:
    parser = argparse.ArgumentParser(description="telemetryd gRPC 서버")
    parser.add_argument("--backend", choices=["mock", "drgn"], default="mock")
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--qemu-qmp", default=None, metavar="UNIX_SOCKET_PATH",
                        help="drgn 백엔드가 이 QEMU 게스트에 QMP로 라이브 접속(유닉스 소켓, root 불필요)")
    parser.add_argument("--qemu-vmlinux", default=None, metavar="PATH")
    parser.add_argument("--extra-symbols", action="append", default=[], metavar="PATH",
                        help="로컬 drgn 모드에서 비표준 위치의 vmlinux를 명시(여러 번 가능)")
    parser.add_argument("--ebpf-log", default=None, metavar="PATH",
                        help="ebpf/nvme_perf.bt(bpftrace) 출력 파일 경로 — GetPerformance/"
                        "StreamPerformance가 이걸 읽는다(DESIGN.md §9.5)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend_kwargs = {}
    if args.backend == "drgn":
        if args.qemu_qmp:
            backend_kwargs = {"qemu_qmp_address": args.qemu_qmp, "qemu_vmlinux": args.qemu_vmlinux,
                              "extra_symbols": args.extra_symbols}
        elif args.extra_symbols:
            backend_kwargs = {"extra_symbols": args.extra_symbols}
        if args.ebpf_log:
            backend_kwargs["ebpf_log_path"] = args.ebpf_log
    asyncio.run(serve(args.backend, args.port, **backend_kwargs))


if __name__ == "__main__":
    main()
