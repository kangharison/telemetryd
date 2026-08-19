"""telemetryd CLI — 순수 라이브러리 import로 동작하는 클라이언트 예제.

DESIGN.md §1의 "cli는 순수 library import 해서 사용하는 형태" 그대로다 — gRPC
를 전혀 거치지 않고 telemetryd.backend.get_backend()를 직접 호출한다.
--backend drgn 은 root 권한(/proc/kcore)이 필요하므로 `sudo -E`로 실행해야
한다(DESIGN.md §0). 기본값 mock은 root 없이 항상 동작한다 — 이 CLI 자체의
출력 형식/동작을 이걸로 검증한다.
"""
from __future__ import annotations

import os
import sys
import time

import click

from telemetryd.backend import DeviceNotFoundError, QueueNotFoundError, get_backend


def _hexdump(data: bytes, base_addr: int) -> str:
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off : off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asci = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {base_addr + off:#010x}  {hexs:<47s}  |{asci}|")
    return "\n".join(lines)


@click.group()
@click.option(
    "--backend",
    type=click.Choice(["mock", "drgn"]),
    default="mock",
    show_default=True,
    help="mock: root 불필요 합성 데이터 / drgn: 실제 라이브 커널",
)
@click.option(
    "--qemu-qmp",
    default=None,
    metavar="UNIX_SOCKET_PATH",
    help="drgn 백엔드가 호스트 대신 이 QEMU 게스트에 QMP로 라이브 접속(root 불필요). "
    "반드시 유닉스 소켓 경로여야 함(TCP 불가 — vmcoreinfo를 SCM_RIGHTS로 받음). "
    "게스트는 '-device vmcoreinfo'로 띄우고 커널은 CONFIG_FW_CFG_SYSFS=y여야 함.",
)
@click.option(
    "--qemu-vmlinux",
    default=None,
    metavar="PATH",
    help="--qemu-qmp 사용 시, 그 게스트가 부팅한 커널과 정확히 같은 빌드의 vmlinux 경로.",
)
@click.option(
    "--extra-symbols",
    "extra_symbols",
    multiple=True,
    metavar="PATH",
    help="로컬(--qemu-qmp 없는) drgn 모드에서도 비표준 위치의 vmlinux를 명시적으로 쓴다. "
    "여러 번 줄 수 있음. 예: QEMU 게스트 안에서 9p로 호스트 rootfs를 마운트/chroot한 뒤 "
    "이 라이브러리를 그대로 실행하는 경우(그 게스트엔 debuginfod/dbgsym 표준 경로가 없음).",
)
@click.option(
    "--ebpf-log",
    default=None,
    metavar="PATH",
    help="ebpf/nvme_perf.bt(bpftrace) 출력 파일 경로 — 'perf' 커맨드가 이걸 읽는다"
    "(DESIGN.md §9.5, 게스트 안에서 별도로 bpftrace를 실행해둬야 함).",
)
@click.pass_context
def cli(ctx: click.Context, backend: str, qemu_qmp: str, qemu_vmlinux: str, extra_symbols: tuple, ebpf_log: str):
    """drgn/eBPF 기반 NVMe telemetry CLI — 라이브러리를 직접 import해서 쓴다(gRPC 안 거침)."""
    kwargs = {}
    if backend == "drgn" and qemu_qmp:
        kwargs = {"qemu_qmp_address": qemu_qmp, "qemu_vmlinux": qemu_vmlinux, "extra_symbols": extra_symbols}
    elif backend == "drgn" and extra_symbols:
        kwargs = {"extra_symbols": extra_symbols}
    if backend == "drgn" and ebpf_log:
        kwargs["ebpf_log_path"] = ebpf_log
    if backend == "drgn" and not qemu_qmp and os.geteuid() != 0:
        click.secho(
            "[경고] --backend drgn 은 (--qemu-qmp 없이는) root가 필요합니다(/proc/kcore). "
            "'sudo -E .venv/bin/telemetryd --backend drgn ...' 로 실행하거나 "
            "--qemu-qmp로 QEMU 게스트에 붙으세요.",
            fg="yellow",
            err=True,
        )
    ctx.obj = get_backend(backend, **kwargs)


@cli.command()
@click.pass_obj
def devices(backend):
    """등록된 컨트롤러 목록."""
    for name in backend.list_devices():
        click.echo(name)


@cli.command()
@click.argument("device")
@click.pass_obj
def snapshot(backend, device):
    """struct nvme_dev 요약 + 큐별 sq_tail/cq_head/inflight (요청사항 1)."""
    try:
        snap = backend.get_device_snapshot(device)
    except DeviceNotFoundError:
        raise click.ClickException(f"디바이스 없음: {device} ('telemetryd devices'로 확인)")
    click.echo(f"{snap.name}  addr={hex(snap.addr)}  model={snap.model!r}  backend={snap.backend_kind}")
    click.echo(
        f"  online_queues={snap.online_queues}  allocated_queues={snap.allocated_queues}  "
        f"iommu={'on' if snap.iommu_enabled else 'off'}"
    )
    click.echo(f"  bar={hex(snap.bar_addr)}  dbs={hex(snap.dbs_addr)}")
    click.echo(f"  {'qid':>4} {'role':>6} {'depth':>6} {'sq_tail':>8} {'cq_head':>8} {'hctx':>5} {'inflight drv/sched':>20}")
    for q in snap.queues:
        role = "ADMIN" if q.is_admin else "IO"
        hctx = "-" if q.hctx_index is None else str(q.hctx_index)
        click.echo(
            f"  {q.qid:>4} {role:>6} {q.depth:>6} {q.sq_tail:>8} {q.cq_head:>8} {hctx:>5} "
            f"{q.inflight_driver:>9}/{q.inflight_sched:<9}"
        )


@cli.command()
@click.argument("device")
@click.argument("qid", type=int)
@click.option("--limit", type=int, default=16, show_default=True, help="0=큐 depth 전체")
@click.option("--from-start", is_flag=True, help="도어벨(sq_tail) 대신 인덱스 0부터 limit개")
@click.pass_obj
def queue(backend, device, qid, limit, from_start):
    """큐를 선택해 SQ 엔트리의 CDW 필드를 덤프 — 기본은 sq_tail 도어벨 바로 앞 최근 것들 (요청사항 2)."""
    try:
        entries = backend.get_queue_entries(device, qid, limit, not from_start)
    except DeviceNotFoundError:
        raise click.ClickException(f"디바이스 없음: {device}")
    except QueueNotFoundError:
        raise click.ClickException(f"큐 없음: qid={qid}")
    for e in entries:
        sgl = "SGL" if e.uses_sgl else "PRP"
        click.echo(
            f"[{e.index}] cid={e.cid:<5} opcode={e.opcode_name:<12}(0x{e.opcode:02x}) "
            f"nsid={e.nsid} flags=0x{e.flags:02x}({sgl})"
        )
        click.echo(
            f"      cdw2={e.cdw2:#010x} cdw3={e.cdw3:#010x} "
            f"cdw10={e.cdw10:#010x} cdw11={e.cdw11:#010x} cdw12={e.cdw12:#010x} "
            f"cdw13={e.cdw13:#010x} cdw14={e.cdw14:#010x} cdw15={e.cdw15:#010x}"
        )
        if not e.uses_sgl:
            click.echo(f"      prp1={hex(e.prp1)} prp2={hex(e.prp2)}")


@cli.command()
@click.argument("device")
@click.argument("qid", type=int)
@click.option("--limit", type=int, default=16, show_default=True, help="0=큐 depth 전체")
@click.option("--from-start", is_flag=True, help="도어벨(cq_head) 대신 인덱스 0부터 limit개")
@click.pass_obj
def cq(backend, device, qid, limit, from_start):
    """큐를 선택해 CQ(완료) 엔트리를 덤프 — 기본은 cq_head 도어벨 바로 앞 최근 것들."""
    try:
        entries = backend.get_completion_entries(device, qid, limit, not from_start)
    except DeviceNotFoundError:
        raise click.ClickException(f"디바이스 없음: {device}")
    except QueueNotFoundError:
        raise click.ClickException(f"큐 없음: qid={qid}")
    for e in entries:
        click.echo(
            f"[{e.index}] cid={e.command_id:<5} sq_id={e.sq_id} sq_head={e.sq_head} "
            f"phase={int(e.phase)} sct={e.status_code_type} sc=0x{e.status_code:02x} "
            f"status_raw=0x{e.status_raw:04x} result=0x{e.result:08x}"
        )


@cli.command()
@click.argument("device")
@click.argument("qid", type=int)
@click.argument("cid", type=int)
@click.pass_obj
def prp(backend, device, qid, cid):
    """"PRP 확인" 버튼 — cid 커맨드가 가리키는 데이터 페이지를 4KB 단위로 hexdump (요청사항 3)."""
    try:
        payload = backend.get_prp_payload(device, qid, cid)
    except DeviceNotFoundError:
        raise click.ClickException(f"디바이스 없음: {device}")
    except QueueNotFoundError:
        raise click.ClickException(f"큐 없음: qid={qid}")
    if payload.uses_sgl:
        click.echo("이 커맨드는 SGL 경로(PSDT!=0)입니다 — PRP 해독 대상이 아닙니다.")
        return
    if payload.error:
        click.secho(f"[안내] {payload.error}", fg="yellow")
    click.echo(f"total_len={payload.total_len}  pages={len(payload.pages)}")
    for i, p in enumerate(payload.pages):
        tag = "LIST" if p.is_list_page else f"DATA[{i}]"
        click.echo(f"-- {tag} phys={hex(p.phys_addr)} offset={p.offset_in_page} bytes={len(p.data)} --")
        click.echo(_hexdump(p.data, p.phys_addr))


@cli.command()
@click.argument("device")
@click.argument("path", nargs=-1)
@click.pass_obj
def tree(backend, device, path):
    """struct nvme_dev 포인터 트리 탐색 (요청사항 4/6, 서버가 depth<=10 강제).

    PATH는 필드명/배열인덱스 시퀀스. 예: telemetryd tree nvme0 ctrl pci_dev
    """
    try:
        exp = backend.get_tree_node(device, list(path))
    except DeviceNotFoundError:
        raise click.ClickException(f"디바이스 없음: {device}")
    if exp.error:
        click.secho(f"[에러] {exp.error}", fg="red")
    n = exp.node
    addr = f" @ {hex(n.address)}" if n.address is not None else ""
    click.echo(f"{'/'.join(('<root>',) + path)}  [{n.kind}] {n.type_name}{addr}")
    click.echo(f"  value: {n.value_repr}")
    if exp.children:
        click.echo("  children:")
        for c in exp.children:
            caddr = f" @ {hex(c.address)}" if c.address is not None else ""
            mark = "->" if c.expandable else "  "
            click.echo(f"   {mark} {c.name:<16} [{c.kind:<8}] {c.type_name:<28} {c.value_repr}{caddr}")


@cli.command()
@click.pass_obj
def doctor(backend):
    """drgn 라이브 세션 헬스체크 (--backend drgn 일 때만 의미 있음)."""
    if backend.kind != "drgn":
        click.echo("mock backend는 항상 정상입니다. 'telemetryd --backend drgn doctor'로 실제 커널을 점검하세요.")
        return
    from telemetryd.backend.drgn_backend import doctor as run_doctor

    result = run_doctor(backend)
    for c in result["checks"]:
        mark = click.style("OK ", fg="green") if c["ok"] else click.style("FAIL", fg="red")
        click.echo(f"[{mark}] {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
    sys.exit(0 if result["ok"] else 1)


@cli.command()
@click.argument("device")
@click.option("--interval", type=float, default=1.0, show_default=True)
@click.option("--count", type=int, default=5, show_default=True, help="갱신 횟수(0=Ctrl-C까지 무한)")
@click.pass_obj
def watch(backend, device, interval, count):
    """일정 주기로 snapshot을 반복 출력 — "실시간" 데모(순수 라이브러리 폴링, gRPC 미사용)."""
    i = 0
    try:
        while count == 0 or i < count:
            try:
                snap = backend.get_device_snapshot(device)
            except DeviceNotFoundError:
                raise click.ClickException(f"디바이스 없음: {device}")
            click.echo(f"--- tick {i} ---")
            for q in snap.queues:
                click.echo(
                    f"  qid={q.qid} sq_tail={q.sq_tail} cq_head={q.cq_head} "
                    f"inflight={q.inflight_driver}/{q.inflight_sched}"
                )
            i += 1
            if count == 0 or i < count:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.argument("device")
@click.option("--watch", "watch_", is_flag=True, help="Ctrl-C까지 1초마다 갱신")
@click.pass_obj
def perf(backend, device, watch_):
    """eBPF(nvme:nvme_setup_cmd/nvme_complete_rq)로 집계한 큐별
    IOPS/대역폭/평균 레이턴시 — 요청사항: "device/개별 queue별로
    iops/bandwidth/latency"."""

    def once():
        try:
            p = backend.get_performance(device)
        except DeviceNotFoundError:
            raise click.ClickException(f"디바이스 없음: {device}")
        if not p.available:
            click.secho(f"[안내] {p.error}", fg="yellow")
            return
        header = (
            f"{'qid':>4} {'iops':>9} {'read/s':>9} {'write/s':>9} {'BW(MB/s)':>10} "
            f"{'avg(us)':>9} {'p50(us)':>9} {'p95(us)':>9} {'p99(us)':>9} {'p99.9(us)':>10}"
        )
        click.echo(header)

        def row(q, label=None):
            qid_str = label if label is not None else str(q.qid)
            click.echo(
                f"{qid_str:>4} {q.iops:>9.0f} {q.read_iops:>9.0f} {q.write_iops:>9.0f} "
                f"{q.bandwidth_bytes_per_sec / 1e6:>10.2f} {q.avg_latency_us:>9.1f} "
                f"{q.p50_latency_us:>9.1f} {q.p95_latency_us:>9.1f} {q.p99_latency_us:>9.1f} "
                f"{q.p999_latency_us:>10.1f}"
            )

        for q in p.queues:
            row(q)
        if p.aggregate is not None:
            click.echo("-" * len(header))
            row(p.aggregate, label="ALL")

    if not watch_:
        once()
        return
    try:
        while True:
            click.clear()
            once()
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def _render_timeout_detail(e) -> None:
    """kind == "timeout" 전용 상세 렌더러 — nvme_timeout 로그에만 있는 필드
    (tag/opcode/nsid/flags/cdw10-15/경과)를 그 종류에 맞게 포매팅한다.

    이런 렌더러를 kind별로 따로 두는 이유: 이벤트 목록은 종류가 섞이는데
    (지금은 timeout뿐이지만 리셋/AER이 추가될 수 있음) 특정 종류의 필드를
    공통 컬럼으로 고정해버리면 다른 종류가 들어올 때 빈 칸 투성이가 되거나
    목록 구조를 다시 갈아엎어야 한다 — 공통은 summary 한 줄, 상세는 여기."""
    d = e.timeout
    cdws = " ".join(
        f"cdw{10 + i}={v:#010x}"
        for i, v in enumerate((d.cdw10, d.cdw11, d.cdw12, d.cdw13, d.cdw14, d.cdw15))
    )
    click.echo(
        f"      tag={d.tag} opcode={d.opcode_name}({d.opcode:#04x}) nsid={d.nsid} "
        f"flags={d.flags:#04x} 경과={d.elapsed_us / 1000:.1f}ms"
    )
    click.echo(f"      {cdws}")


def _render_error_detail(e) -> None:
    """kind == "error" 전용 상세 렌더러 — 에러 status로 반환된 커맨드.

    status를 SCT/SC로 분해해 보여주고, 실무 판단에 직결되는 DNR(재시도 불가)/
    More(에러 로그 페이지에 추가 정보)/CRD(재시도 지연)를 따로 찍는다.
    SLBA/NLB는 read/write일 때만 의미가 있어 그 경우만 출력한다."""
    d = e.error
    flags = []
    if d.dnr:
        flags.append("DNR(재시도 불가)")
    if d.more:
        flags.append("More(에러 로그에 추가 정보)")
    if d.crd:
        flags.append(f"CRD={d.crd}")
    click.echo(
        f"      status={d.status:#06x} SCT={d.sct}({d.sct_name}) SC={d.sc:#04x}({d.sc_name})"
        + (f" [{', '.join(flags)}]" if flags else "")
    )
    line = (
        f"      cid={d.cid} tag={d.tag} opcode={d.opcode_name}({d.opcode:#04x}) "
        f"nsid={d.nsid} retries={d.retries} 제출~완료={d.elapsed_us / 1000:.1f}ms"
    )
    if d.lba_valid:
        line += f" slba={d.slba} nlb={d.nlb}"
    elif not d.submit_cached:
        line += "  (제출 시점을 못 봐서 opcode/nsid/LBA 미상)"
    click.echo(line)


#: kind -> 종류별 상세 렌더러. 여기 없는 kind는 공통 요약(summary) 한 줄만
#: 찍는다 — 모르는 종류가 와도 목록이 깨지지 않게 하는 게 이 구조의 요점.
_EVENT_DETAIL_RENDERERS = {"timeout": _render_timeout_detail, "error": _render_error_detail}


@cli.command()
@click.argument("device")
@click.option("--kind", default=None, help='종류 필터 (예: timeout). 생략하면 전 종류.')
@click.option("--watch", "watch_", is_flag=True, help="Ctrl-C까지 1초마다 갱신")
@click.pass_obj
def events(backend, device, kind, watch_):
    """이 디바이스에서 관측된 NVMe 이벤트 목록 (종류 무관).

    지금 채워지는 종류는 eBPF kprobe:nvme_timeout이 잡는 timeout 하나뿐이지만,
    목록 자체는 특정 종류를 대표로 삼지 않는다 — 공통 컬럼(시간/종류/qid/요약)
    으로 나열하고 종류별 필드는 그 종류 전용 렌더러가 아래 줄에 따로 찍는다.
    """

    def once():
        try:
            evs = backend.get_events(device)
        except DeviceNotFoundError:
            raise click.ClickException(f"디바이스 없음: {device}")
        if kind:
            evs = [e for e in evs if e.kind == kind]
        if not evs:
            click.secho(
                "[안내] 이벤트 없음" + (f" (종류={kind})" if kind else ""), fg="green"
            )
            return
        click.echo(f"{'time':>19} {'종류':>10} {'qid':>4}  요약")
        for e in evs:
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.observed_at))
            # [한국어] 종류별 색: 타임아웃은 장애성이라 빨강, 모르는 종류는
            # 기본색 — 색까지 종류별 렌더러에 넘기면 과하므로 여기서 최소한만.
            color = "red" if e.kind == "timeout" else ("yellow" if e.kind == "error" else None)
            click.secho(f"{t:>19} {e.kind:>10} {e.qid:>4}  {e.summary}", fg=color)
            renderer = _EVENT_DETAIL_RENDERERS.get(e.kind)
            if renderer is not None:
                renderer(e)

    if not watch_:
        once()
        return
    try:
        while True:
            click.clear()
            once()
            time.sleep(1)
    except KeyboardInterrupt:
        pass


#: 토폴로지 노드 종류별 접두 기호 — CLI에서 PCIe 계층과 NVMe 계층이 한눈에
#: 구분되게 한다(모르는 종류는 기본 기호).
_TOPO_MARKS = {
    "system": "■", "host_bridge": "▣", "pci_bridge": "◆", "pci_endpoint": "●",
    "nvme_ctrl": "◎", "nvme_subsystem": "◈", "namespace": "▪", "queue_group": "▤", "queue": "·",
}


def _print_topo_node(node, prefix: str = "", is_last: bool = True, show_details: bool = False) -> None:
    """통합 트리를 ASCII 트리로 출력(재귀).

    prefix/is_last로 └─ ├─ │ 를 이어 그린다 — 노드가 어느 부모에 붙었는지가
    PCIe 계보에서 특히 중요해서(브리지를 공유하는지 여부) 들여쓰기만으로는
    부족하다."""
    connector = "└─ " if is_last else "├─ "
    mark = _TOPO_MARKS.get(node.kind, "•")
    head = f"{prefix}{connector}{mark} {node.label}"
    if node.sublabel:
        head += f"  — {node.sublabel}"
    click.echo(head)
    child_prefix = prefix + ("   " if is_last else "│  ")
    if show_details:
        for d in node.details:
            click.secho(f"{child_prefix}    {d.key}: {d.value}", fg="bright_black")
    for i, c in enumerate(node.children):
        _print_topo_node(c, child_prefix, i == len(node.children) - 1, show_details)


@cli.command()
@click.option("--details", "show_details", is_flag=True,
              help="각 노드의 속성(BDF/클래스/nsid/용량 등)까지 출력")
@click.pass_obj
def topology(backend, show_details):
    """PCIe 토폴로지 + NVMe 서브시스템 통합 트리.

    호스트 브리지에서 시작해 브리지/스위치를 타고 내려가 PCIe 엔드포인트에
    닿고, 거기서 NVMe 컨트롤러 → 서브시스템/네임스페이스/큐로 이어진다 —
    두 계층이 한 트리에서 만나는 게 이 뷰의 핵심이다.
    """
    topo = backend.get_topology()
    root = topo.root
    click.secho(f"{_TOPO_MARKS.get(root.kind, '•')} {root.label}", bold=True)
    if root.sublabel:
        click.secho(f"  {root.sublabel}", fg="bright_black")
    for i, c in enumerate(root.children):
        _print_topo_node(c, "", i == len(root.children) - 1, show_details)
    if topo.error:
        click.secho(f"[경고] 일부 장치를 못 읽음: {topo.error}", fg="yellow")


@cli.command("error-stats")
@click.argument("device")
@click.option("--watch", "watch_", is_flag=True, help="Ctrl-C까지 1초마다 갱신")
@click.pass_obj
def error_stats(backend, device, watch_):
    """에러 완료의 SCT/SC 조합별 누적 카운터.

    이벤트 목록(`events`)이 "최근 무슨 일이 있었나"라면 이건 "여태 어떤 에러가
    몇 번"이다 — 목록은 로그 폭주 방지로 샘플링될 수 있지만 이 카운터는 전수라
    건수는 이쪽이 정확하다."""

    def once():
        try:
            st = backend.get_error_stats(device)
        except DeviceNotFoundError:
            raise click.ClickException(f"디바이스 없음: {device}")
        if not st.available:
            click.secho(f"[안내] {st.error}", fg="yellow")
            return
        if not st.counts:
            click.secho("[안내] 에러 완료 0건 (수집 중)", fg="green")
            return
        click.echo(f"{'SCT':>4} {'SC':>6} {'타입':<22} {'상태 코드':<40} {'누적':>8}")
        for c in st.counts:
            click.echo(
                f"{c.sct:>4} {c.sc:#06x} {c.sct_name:<22} {c.sc_name:<40} {c.count:>8}"
            )
        click.echo(f"{'':>4} {'':>6} {'':<22} {'합계':<40} {st.total:>8}")

    if not watch_:
        once()
        return
    try:
        while True:
            click.clear()
            once()
            time.sleep(1)
    except KeyboardInterrupt:
        pass


@cli.command("event-kinds")
@click.pass_obj
def event_kinds(backend):
    """이 시스템에 등록된 이벤트 종류 목록 — 지금 무엇을 수집 중인지."""
    for k in backend.list_event_kinds():
        mark = click.style("● 수집 중", fg="green") if k.active else click.style("○ 미수집", fg="yellow")
        click.echo(f"{mark}  {k.kind:<8} {k.label}")
        click.echo(f"           출처: {k.source}")
        click.echo(f"           {k.description}")


# ===========================================================================
# [한국어] NVMe I/O 프로세스 프로파일러 — 대상은 런타임에 선택한다.
# 특정 애플리케이션 전용이 아니라 "NVMe I/O를 내는 프로세스"에 대한 범용 도구다.
# ===========================================================================

def _fmt_rate(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M/s"
    if v >= 1000:
        return f"{v / 1000:.2f}K/s"
    return f"{v:.0f}/s"


@cli.command()
@click.option("--all", "show_all", is_flag=True,
              help="I/O를 안 내는 프로세스까지 전부(기본은 NVMe I/O 발행 중인 것만)")
@click.option("--cmdline", "show_cmdline", is_flag=True, help="전체 cmdline까지 출력")
@click.pass_obj
def processes(backend, show_all, show_cmdline):
    """대상 후보 프로세스 목록 — 무엇이 이 SSD를 때리고 있는지.

    기본 필터가 "NVMe I/O 발행 중"인 이유: 시스템 전체는 수백 개라 못 고르지만
    실제로 I/O를 내는 프로세스는 보통 한 자릿수라, 대상 이름을 몰라도 여기서
    시작할 수 있다."""
    entries = backend.list_processes(only_io=not show_all)
    if not entries:
        click.secho("[안내] 조건에 맞는 프로세스 없음 (--all 로 전체 보기)", fg="green")
        return
    click.echo(f"{'PID':>7} {'NAME':<16} {'THR':>4} {'IO RATE':>10}  {'DEVICE':<12} {'대상':<6}")
    for e in entries:
        i = e.info
        mark = click.style("●", fg="green") if e.is_target else " "
        line = (f"{i.pid:>7} {i.comm[:16]:<16} {i.thread_count:>4} "
                f"{_fmt_rate(e.io_rate):>10}  {','.join(e.target_devices) or '-':<12} {mark}")
        click.secho(line, fg=None if e.selectable else "bright_black")
        if not e.selectable and e.unselectable_reason:
            click.secho(f"         └ 선택 불가: {e.unselectable_reason}", fg="bright_black")
        if show_cmdline and i.cmdline:
            click.secho(f"         └ {i.cmdline}", fg="bright_black")


@cli.group()
def target():
    """관측 대상 규칙 관리 (pid / name / 패턴). 규칙은 프로세스가 죽어도 남는다."""


@target.command("add")
@click.option("--pid", type=int, multiple=True, help="PID 직접 지정(복수 가능)")
@click.option("--name", multiple=True, help="실행파일명/comm 일치")
@click.option("--name-pattern", multiple=True, help="이름 정규식")
@click.option("--cmdline-pattern", multiple=True, help="cmdline 정규식(fio 워크로드 구분에 유용)")
@click.option("--adapter", default=None, help="어댑터 강제 지정(기본은 자동 선택)")
@click.pass_obj
def target_add(backend, pid, name, name_pattern, cmdline_pattern, adapter):
    """대상 규칙 추가. 데몬이 먼저 떠 있어도 대상이 나중에 실행되면 자동으로 붙는다."""
    from telemetryd.models import TargetRule

    specs = ([("pid", str(v)) for v in pid] + [("name", v) for v in name]
             + [("name_pattern", v) for v in name_pattern]
             + [("cmdline_pattern", v) for v in cmdline_pattern])
    if not specs:
        raise click.ClickException("--pid / --name / --name-pattern / --cmdline-pattern 중 하나는 필요")
    rules = []
    for kind, value in specs:
        rules = backend.add_target(TargetRule(kind=kind, value=value, adapter=adapter))
    for r in rules:
        click.echo(f"  {r.kind}={r.value}" + (f" (adapter={r.adapter})" if r.adapter else ""))


@target.command("remove")
@click.argument("kind")
@click.argument("value")
@click.pass_obj
def target_remove(backend, kind, value):
    """대상 규칙 제거. 이미 만들어진 세션 데이터는 지우지 않는다."""
    rules = backend.remove_target(kind, value)
    click.secho(f"제거: {kind}={value}", fg="yellow")
    for r in rules:
        click.echo(f"  {r.kind}={r.value}")


@target.command("list")
@click.pass_obj
def target_list(backend):
    """등록된 대상 규칙."""
    rules = backend.list_targets()
    if not rules:
        click.secho("[안내] 등록된 규칙 없음", fg="green")
        return
    for r in rules:
        click.echo(f"  {r.kind}={r.value}" + (f" (adapter={r.adapter})" if r.adapter else ""))


def _print_profile(snap) -> None:
    if snap.error:
        click.secho(f"[안내] {snap.error}", fg="yellow")
    if not snap.sessions:
        click.secho("[안내] 활성 세션 없음 — `telemetryd target add` 로 대상을 지정하세요", fg="green")
    for s in snap.sessions:
        color = "green" if s.status == "active" else "bright_black"
        agg = s.aggregate
        head = (f"[{s.status}] {s.comm} (pid {s.pid})  adapter={s.adapter}  "
                f"규칙={s.matched_rule or '-'}  장치={','.join(s.devices) or '-'}")
        click.secho(head, fg=color, bold=(s.status == "active"))
        click.secho(f"   session={s.session_id}", fg="bright_black")
        click.secho(f"   cmdline: {s.cmdline or '(없음)'}", fg="bright_black")
        if agg and agg.iops:
            click.echo(f"   실측 합계: {_fmt_rate(agg.iops)} IOPS, "
                       f"{agg.bandwidth_bps / 1e6:.1f} MB/s, "
                       f"read {agg.read_ratio:.0%}/write {agg.write_ratio:.0%}, "
                       f"QD~{agg.queue_depth_avg:.1f}")
        for g in s.logical_groups:
            if g.expectation_match is None:
                badge = click.style("판단불가", fg="bright_black")
            elif g.expectation_match:
                badge = click.style("일치 ✓", fg="green")
            else:
                badge = click.style("불일치 ⚠", fg="yellow")
            extra = " (추정)" if g.inferred else ""
            click.echo(f"   └ {g.name} [{g.type}{extra}] 스레드 {len(g.thread_tids)}개  {badge}")
            e, m = g.expected_workload, g.measured_workload
            if e:
                click.secho(f"        기대: bs={e.io_size} rw={e.rw} QD={e.queue_depth} "
                            f"engine={e.ioengine} direct={e.direct}", fg="bright_black")
            if m:
                click.secho(f"        실측: bs={m.io_size_dominant} "
                            f"r/w={m.read_ratio:.0%}/{m.write_ratio:.0%} "
                            f"QD~{m.queue_depth_avg:.1f} {m.iops:.0f} IOPS", fg="bright_black")
            for reason in g.mismatch_reasons:
                click.secho(f"        - {reason}", fg="yellow" if g.expectation_match is False else "bright_black")

    if snap.unmonitored_io:
        click.secho("\n[경고] 관측 대상이 아닌 프로세스가 같은 장치에 I/O를 내고 있음:", fg="yellow")
        for u in snap.unmonitored_io:
            click.secho(f"   pid {u.pid} {u.comm} -> {u.device}  {_fmt_rate(u.io_rate)}", fg="yellow")
    for d in snap.devices:
        warn = click.style("  ⚠ 여러 프로세스", fg="yellow") if d.multi_process_warning else ""
        click.echo(f"{d.name}: 총 {_fmt_rate(d.total_iops)} "
                   f"(귀속 {_fmt_rate(d.attributed_iops)} / 미귀속 {_fmt_rate(d.unattributed_iops)}){warn}")


@cli.command()
@click.option("--watch", "watch_", is_flag=True, help="Ctrl-C까지 2초마다 갱신")
@click.pass_obj
def profile(backend, watch_):
    """프로파일 스냅샷 — 세션별 논리 그룹, 기대 vs 실측 대조, 미관측 I/O."""
    if not watch_:
        _print_profile(backend.get_profile())
        return
    try:
        while True:
            click.clear()
            _print_profile(backend.get_profile())
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
