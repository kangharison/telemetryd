"""dataclass(models.py) <-> protobuf(telemetryd_pb2) 변환.

로직은 없다 — 필드 매핑뿐. 64bit 주소는 JS Number 정밀도(2^53) 밖으로 나갈 수
있어 전부 hex 문자열로 직렬화한다(proto의 해당 필드가 `string`인 이유).
"""
from __future__ import annotations

from telemetryd.grpcserver import telemetryd_pb2 as pb
from telemetryd.models import (
    CompletionEntry,
    DevicePerf,
    DeviceSnapshot,
    PrpPayload,
    QueueEntry,
    QueuePerf,
    QueueSnapshot,
    DeviceErrorStats,
    EventKindInfo,
    NvmeEvent,
    ProcessListEntry,
    ProfileSnapshot,
    TargetRule,
    Topology,
    TopologyNode,
    TreeExpansion,
    TreeNode,
)


def queue_to_pb(q: QueueSnapshot) -> pb.QueueSnapshot:
    return pb.QueueSnapshot(
        index=q.index,
        qid=q.qid,
        is_admin=q.is_admin,
        depth=q.depth,
        sq_tail=q.sq_tail,
        cq_head=q.cq_head,
        sq_dma_addr=hex(q.sq_dma_addr),
        cq_dma_addr=hex(q.cq_dma_addr),
        hctx_index=q.hctx_index if q.hctx_index is not None else -1,
        inflight_driver=q.inflight_driver,
        inflight_sched=q.inflight_sched,
    )


def device_to_pb(d: DeviceSnapshot) -> pb.DeviceSnapshot:
    return pb.DeviceSnapshot(
        name=d.name,
        addr=hex(d.addr),
        model=d.model,
        online_queues=d.online_queues,
        allocated_queues=d.allocated_queues,
        bar_addr=hex(d.bar_addr),
        dbs_addr=hex(d.dbs_addr),
        iommu_enabled=d.iommu_enabled,
        queues=[queue_to_pb(q) for q in d.queues],
        backend_kind=d.backend_kind,
    )


def entry_to_pb(e: QueueEntry) -> pb.QueueEntry:
    return pb.QueueEntry(
        index=e.index,
        cid=e.cid,
        opcode=e.opcode,
        opcode_name=e.opcode_name,
        nsid=e.nsid,
        flags=e.flags,
        uses_sgl=e.uses_sgl,
        cdw2=e.cdw2,
        cdw3=e.cdw3,
        cdw10=e.cdw10,
        cdw11=e.cdw11,
        cdw12=e.cdw12,
        cdw13=e.cdw13,
        cdw14=e.cdw14,
        cdw15=e.cdw15,
        prp1=hex(e.prp1),
        prp2=hex(e.prp2),
    )


def completion_to_pb(c: CompletionEntry) -> pb.CompletionEntry:
    return pb.CompletionEntry(
        index=c.index,
        command_id=c.command_id,
        sq_id=c.sq_id,
        sq_head=c.sq_head,
        status_raw=c.status_raw,
        phase=c.phase,
        status_code=c.status_code,
        status_code_type=c.status_code_type,
        result=c.result,
    )


def prp_to_pb(p: PrpPayload) -> pb.PrpPayloadReply:
    return pb.PrpPayloadReply(
        device=p.device,
        qid=p.qid,
        cid=p.cid,
        uses_sgl=p.uses_sgl,
        total_len=p.total_len,
        pages=[
            pb.PrpPage(
                phys_addr=hex(pg.phys_addr),
                offset_in_page=pg.offset_in_page,
                data=pg.data,
                is_list_page=pg.is_list_page,
            )
            for pg in p.pages
        ],
        error=p.error or "",
    )


def tree_node_to_pb(n: TreeNode) -> pb.TreeNode:
    return pb.TreeNode(
        name=n.name,
        type_name=n.type_name,
        kind=n.kind,
        value_repr=n.value_repr,
        address=hex(n.address) if n.address is not None else "",
        is_null=n.is_null,
        expandable=n.expandable,
    )


def tree_expansion_to_pb(e: TreeExpansion) -> pb.TreeNodeReply:
    return pb.TreeNodeReply(
        node=tree_node_to_pb(e.node),
        children=[tree_node_to_pb(c) for c in e.children],
        depth=e.depth,
        error=e.error or "",
    )


def queue_perf_to_pb(q: QueuePerf) -> pb.QueuePerf:
    return pb.QueuePerf(
        qid=q.qid,
        iops=q.iops,
        read_iops=q.read_iops,
        write_iops=q.write_iops,
        bandwidth_bytes_per_sec=q.bandwidth_bytes_per_sec,
        avg_latency_us=q.avg_latency_us,
        p50_latency_us=q.p50_latency_us,
        p95_latency_us=q.p95_latency_us,
        p99_latency_us=q.p99_latency_us,
        p999_latency_us=q.p999_latency_us,
    )


def device_perf_to_pb(d: DevicePerf) -> pb.DevicePerf:
    msg = pb.DevicePerf(
        device=d.device,
        queues=[queue_perf_to_pb(q) for q in d.queues],
        available=d.available,
        error=d.error or "",
    )
    if d.aggregate is not None:
        msg.aggregate.CopyFrom(queue_perf_to_pb(d.aggregate))
    return msg


def nvme_event_to_pb(e: NvmeEvent) -> pb.NvmeEvent:
    """NvmeEvent(공통 봉투 + 종류별 상세) -> protobuf.

    공통 필드는 항상 채우고, 종류별 상세는 kind에 해당하는 oneof 멤버 하나만
    채운다 — 그래서 수신 측(웹/CLI)은 `WhichOneof("detail")`(또는 JSON에서
    해당 키의 존재 여부)로 종류별 렌더러를 고를 수 있다. 새 종류를 추가할
    때는 여기 elif 한 줄과 proto의 oneof 멤버만 늘리면 되고, 모르는 종류가
    섞여도 공통 필드는 그대로 전달되므로 목록이 깨지지 않는다."""
    msg = pb.NvmeEvent(
        kind=e.kind,
        observed_at=e.observed_at,
        device=e.device,
        qid=e.qid,
        summary=e.summary,
    )
    if e.error is not None:
        d = e.error
        msg.error.CopyFrom(
            pb.ErrorEventDetail(
                cid=d.cid, tag=d.tag, opcode=d.opcode, opcode_name=d.opcode_name,
                nsid=d.nsid, status=d.status, sct=d.sct, sc=d.sc,
                sct_name=d.sct_name, sc_name=d.sc_name,
                dnr=d.dnr, more=d.more, crd=d.crd, retries=d.retries,
                slba=d.slba, nlb=d.nlb, lba_valid=d.lba_valid,
                submit_cached=d.submit_cached, elapsed_us=d.elapsed_us,
            )
        )
    if e.timeout is not None:
        d = e.timeout
        msg.timeout.CopyFrom(
            pb.TimeoutEventDetail(
                tag=d.tag,
                opcode=d.opcode,
                opcode_name=d.opcode_name,
                nsid=d.nsid,
                flags=d.flags,
                cdw10=d.cdw10,
                cdw11=d.cdw11,
                cdw12=d.cdw12,
                cdw13=d.cdw13,
                cdw14=d.cdw14,
                cdw15=d.cdw15,
                elapsed_us=d.elapsed_us,
            )
        )
    return msg


def error_stats_to_pb(st: DeviceErrorStats) -> pb.ErrorStatsReply:
    """SCT/SC 누적 카운터 표 -> protobuf. 이벤트 목록과 달리 종류별 봉투가
    필요 없다 — 이건 애초에 에러 종류 전용 집계이기 때문(base.py 참고)."""
    return pb.ErrorStatsReply(
        device=st.device,
        counts=[
            pb.ErrorStatusCount(sct=c.sct, sc=c.sc, sct_name=c.sct_name,
                                sc_name=c.sc_name, count=c.count)
            for c in st.counts
        ],
        total=st.total,
        available=st.available,
        error=st.error or "",
    )


def event_kind_to_pb(k: EventKindInfo) -> pb.EventKind:
    return pb.EventKind(kind=k.kind, label=k.label, source=k.source,
                        description=k.description, active=k.active)


def topology_node_to_pb(n: TopologyNode) -> pb.TopologyNode:
    """통합 토폴로지 노드 -> protobuf(재귀).

    노드 종류별 필드를 메시지에 박지 않고 details(key-value 목록)로 넘기므로,
    새 노드 종류가 생겨도 이 변환 함수는 그대로다."""
    return pb.TopologyNode(
        id=n.id,
        kind=n.kind,
        label=n.label,
        sublabel=n.sublabel,
        device=n.device,
        details=[pb.TopologyDetail(key=d.key, value=d.value) for d in n.details],
        children=[topology_node_to_pb(c) for c in n.children],
    )


def topology_to_pb(t: Topology) -> pb.TopologyReply:
    return pb.TopologyReply(
        root=topology_node_to_pb(t.root),
        backend_kind=t.backend_kind,
        error=t.error or "",
    )


# ---- NVMe I/O 프로세스 프로파일러 ------------------------------------------

def process_entry_to_pb(e: ProcessListEntry) -> pb.ProcessListEntryMsg:
    i = e.info
    return pb.ProcessListEntryMsg(
        info=pb.ProcessInfoMsg(
            pid=i.pid, comm=i.comm, cmdline=i.cmdline, exe_path=i.exe_path,
            uid=i.uid, start_time_ns=max(0, i.start_time_ns), thread_count=i.thread_count,
            threads=[pb.ThreadRef(tid=t, comm=c) for t, c in (i.threads or [])],
            error=i.error or "",
        ),
        io_active=e.io_active, io_rate=e.io_rate,
        target_devices=list(e.target_devices), is_target=e.is_target,
        matched_rule=e.matched_rule or "", selectable=e.selectable,
        unselectable_reason=e.unselectable_reason or "",
    )


def target_rule_to_pb(r: TargetRule) -> pb.TargetRuleMsg:
    return pb.TargetRuleMsg(kind=r.kind, value=r.value, adapter=r.adapter or "")


def _workload_spec_to_pb(w) -> pb.WorkloadSpecMsg:
    """기대 워크로드. 어댑터가 아무것도 못 알아낸 경우와 구분하려고 has_spec을 둔다
    (proto3는 0/빈문자열과 '없음'을 구분하지 못하므로)."""
    if w is None:
        return pb.WorkloadSpecMsg(has_spec=False)
    return pb.WorkloadSpecMsg(
        io_size=w.io_size or 0, rw=w.rw or "", pattern=w.pattern or "",
        queue_depth=w.queue_depth or 0, ioengine=w.ioengine or "",
        direct=bool(w.direct), filename=w.filename or "",
        numjobs=w.numjobs or 0, runtime_sec=w.runtime_sec or 0, has_spec=True,
    )


def _measured_to_pb(m) -> pb.MeasuredWorkloadMsg:
    if m is None:
        return pb.MeasuredWorkloadMsg()
    return pb.MeasuredWorkloadMsg(
        io_size_dominant=m.io_size_dominant, read_ratio=m.read_ratio,
        write_ratio=m.write_ratio, queue_depth_avg=m.queue_depth_avg,
        iops=m.iops, bandwidth_bps=m.bandwidth_bps,
    )


def profile_to_pb(snap: ProfileSnapshot) -> pb.ProfileReply:
    return pb.ProfileReply(
        schema_version=snap.schema_version,
        collected_at_ns=snap.collected_at_ns,
        sessions=[
            pb.SessionMsg(
                session_id=s.session_id, pid=s.pid, comm=s.comm, cmdline=s.cmdline,
                exe_path=s.exe_path, adapter=s.adapter, status=s.status,
                start_time_ns=max(0, s.start_time_ns), session_start_ns=s.session_start_ns,
                session_end_ns=s.session_end_ns or 0, devices=list(s.devices),
                thread_count_active=s.thread_count_active,
                aggregate=_measured_to_pb(s.aggregate),
                matched_rule=s.matched_rule or "",
                logical_groups=[
                    pb.LogicalGroupMsg(
                        name=g.name, type=g.type, source=g.source,
                        thread_tids=list(g.thread_tids),
                        expected_workload=_workload_spec_to_pb(g.expected_workload),
                        measured_workload=_measured_to_pb(g.measured_workload),
                        has_expectation=g.expectation_match is not None,
                        expectation_match=bool(g.expectation_match),
                        mismatch_reasons=list(g.mismatch_reasons),
                        inferred=g.inferred, progress_pct=g.progress_pct or 0.0,
                    ) for g in s.logical_groups
                ],
            ) for s in snap.sessions
        ],
        unmonitored_io=[
            pb.UnmonitoredIoMsg(pid=u.pid, comm=u.comm, device=u.device,
                                io_rate=u.io_rate, note=u.note)
            for u in snap.unmonitored_io
        ],
        devices=[
            pb.DeviceAttributionMsg(
                name=d.name, total_iops=d.total_iops, attributed_iops=d.attributed_iops,
                unattributed_iops=d.unattributed_iops,
                contributing_sessions=list(d.contributing_sessions),
                multi_process_warning=d.multi_process_warning)
            for d in snap.devices
        ],
        rules=[target_rule_to_pb(r) for r in snap.rules],
        available=snap.available, error=snap.error or "",
    )
