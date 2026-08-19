"""실제 라이브 커널을 drgn으로 조회하는 백엔드.

deep/scripts/drgn/00~04번 스크립트에서 검증된 경로를 그대로 라이브러리화한
것이다 — gendisk -> nvme_ns -> nvme_ctrl -> container_of(nvme_dev), SQ/CQ
필드, request_queue_busy_iter를 이용한 hctx별 inflight, PRP 디코딩.

⚠️ 이 백엔드는 root 권한(/proc/kcore)이 필요하고, 이 세션(Claude)은 sudo가
비밀번호를 요구해 non-interactive로 실행할 수 없다(DESIGN.md §0). 그래서
이 파일은 기존 검증된 스크립트의 패턴을 최대한 그대로 재사용했지만,
**실제 라이브 커널에 대해 이 파일 자체를 실행해 검증하지는 못했다** — 신규
로직(트리 워커 연결, cdw2/cdw3 필드명 fallback, PRP total_len 추정)은
사용자가 `sudo -E`로 직접 검증해야 한다(§8, README 실행법 참조).
"""
from __future__ import annotations

import os
import re
import struct
from typing import Dict, List, Optional, Tuple

from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.models import CompletionEntry, DeviceSnapshot, PrpPage, PrpPayload, QueueEntry, QueueSnapshot
from telemetryd.nvme_const import MAX_PAGE_DUMP, PAGE_MASK, PAGE_SIZE, PRPS_PER_PAGE, opcode_name
from telemetryd.platform.ebpf import as_log_source
from telemetryd.platform.kernel import DrgnKernelSession
from telemetryd.services.events import EbpfEventService
from telemetryd.services.perf import EbpfPerfService
from telemetryd import treewalk


def _window_indices(doorbell: int, depth: int, limit: int) -> List[int]:
    """도어벨(sq_tail 또는 cq_head) 바로 앞 최근 limit개 인덱스 — **최신(도어벨
    바로 앞)부터 오래된 순으로** 내림차순(링 버퍼 wrap 처리). limit<=0이거나
    depth 이상이면 전체 depth. mock_backend.py의 동명 함수와 동일 규칙(실제 커널판)."""
    n = min(limit, depth) if limit else depth
    return [(doorbell - 1 - i) % depth for i in range(n)]

_DISK_RE = re.compile(rb"^nvme(\d+)n1$")



def _iommu_enabled() -> bool:
    """/sys/class/iommu/ 가 비어있지 않으면 IOMMU 활성 — PRP가 물리주소가 아닌
    IOVA일 수 있어(§DESIGN 0) 스냅샷에 플래그로 남긴다. 이건 drgn이 아니라
    이 프로세스가 돌아가는 호스트 자체의 sysfs를 보는 것(같은 머신이므로 OK)."""
    try:
        return len(os.listdir("/sys/class/iommu")) > 0
    except FileNotFoundError:
        return False


def _decode_prp_pages(prog, prp1: int, prp2: int, total_len: int) -> List[PrpPage]:
    """04_prp_payload.py의 analyze_prp()를 그대로 옮기되, print 대신 PrpPage 리스트로.

    리스트 페이지(case C)의 엔트리도 커널 가상주소(iod.descriptors[0])를 거치지
    않고 prp2 물리주소를 직접 읽어(physical=True) 파싱한다 — IOMMU 비활성
    환경에서는 PRP2 자체가 이미 그 리스트 페이지의 물리주소이므로 이렇게 해도
    같은 내용을 얻는다(§DESIGN 0).

    요구사항: "PRP 확인"은 **데이터 페이로드를 최대 MAX_PAGE_DUMP(4096B)까지만**
    보여준다. bs=64k 같은 큰 I/O는 실제로 여러 페이지(case C, PRP 리스트)에
    걸치는데, 예전엔 리스트 페이지당 최대 8개 데이터 페이지(최대 32KB)까지
    다 읽어서 응답이 UI로 다루기엔 너무 커졌다(실사용 중 발견). 그래서 아래
    `shown` 누적치로 데이터 바이트 총합을 4096에서 끊는다 — PRP 리스트 페이지
    자체(`is_list_page=True`, 메타데이터)는 데이터가 아니라 캡 대상에서 뺀다.
    """
    pages: List[PrpPage] = []
    if prp1 == 0 or total_len <= 0:
        return pages

    offset = prp1 & PAGE_MASK
    first_page_bytes = min(total_len, PAGE_SIZE - offset)  # 케이스 판별(1/2/3+페이지)은 캡과 무관하게 실제 크기로
    first_bytes = min(first_page_bytes, MAX_PAGE_DUMP)      # 실제로 읽어서 보여줄 양만 캡
    pages.append(_read_page(prog, prp1, offset, first_bytes))
    shown = first_bytes
    remaining = total_len - first_page_bytes
    if remaining <= 0 or shown >= MAX_PAGE_DUMP:
        return pages

    n_more = -(-remaining // PAGE_SIZE)  # ceil div
    if n_more == 1:
        page_bytes = min(remaining, MAX_PAGE_DUMP - shown)
        if page_bytes > 0:
            pages.append(_read_page(prog, prp2, 0, page_bytes))
        return pages

    # case C: PRP2 는 PRP 리스트 페이지의 물리주소. 메타데이터라 4KB 캡과 무관하게 항상 보여준다.
    pages.append(_read_page(prog, prp2, 0, min(remaining, PAGE_SIZE), is_list_page=True))
    if shown >= MAX_PAGE_DUMP:
        return pages
    show = min(n_more, PRPS_PER_PAGE - 1, 8)  # 리스트 자체가 너무 길면 8엔트리까지만 조회
    try:
        raw = prog.read(prp2, show * 8, physical=True)
        entries = struct.unpack(f"<{show}Q", raw)
    except Exception:
        entries = ()
    for i, entry_phys in enumerate(entries):
        if shown >= MAX_PAGE_DUMP:
            break
        page_bytes = min(PAGE_SIZE, remaining - i * PAGE_SIZE, MAX_PAGE_DUMP - shown)
        if page_bytes <= 0:
            break
        pages.append(_read_page(prog, entry_phys, 0, page_bytes))
        shown += page_bytes
    return pages


def _read_page(prog, phys: int, offset: int, nbytes: int, is_list_page: bool = False) -> PrpPage:
    nbytes = max(0, min(nbytes, PAGE_SIZE))
    try:
        data = bytes(prog.read(phys, nbytes, physical=True))
    except Exception:
        data = b""  # MMIO/미매핑/P2P 메모리 등 — FaultError를 조용히 삼키고 빈 페이로드로 표기
    return PrpPage(phys_addr=phys, offset_in_page=offset, data=data, is_list_page=is_list_page)


def _cdw2_cdw3(common) -> Tuple[int, int]:
    """커널 버전에 따라 struct nvme_common_command 의 cdw2 필드가 개별
    cdw2/cdw3 인지, `__le32 cdw2[2]` 배열인지가 다를 수 있어 둘 다 시도한다."""
    try:
        return int(common.cdw2), int(common.cdw3)
    except (AttributeError, LookupError, TypeError):
        try:
            arr = common.cdw2
            return int(arr[0]), int(arr[1])
        except Exception:
            return 0, 0


class DrgnBackend:
    kind = "drgn"

    def __init__(
        self, program=None, qemu_qmp_address=None, qemu_vmlinux=None,
        extra_symbols=None, ebpf_log_path=None, target_state=None,
    ):
        """@program: 이미 구성된 drgn.Program을 직접 주입(테스트/재사용용).
        @qemu_qmp_address: 주어지면 호스트 대신 이 주소의 QEMU 게스트에 QMP로 라이브
          접속한다 — root 권한이 필요 없다. **반드시 유닉스 소켓 경로**여야 한다
          (TCP 불가): drgn이 vmcoreinfo를 얻으려고 QMP의 dump-guest-memory를
          쓰는데, 그 결과 fd를 SCM_RIGHTS로 넘겨받으므로 유닉스 도메인 소켓
          연결에서만 동작한다. 게스트 쪽에는 QEMU를 `-device vmcoreinfo`로
          띄우고, 커널은 CONFIG_FW_CFG_SYSFS=y + CONFIG_KEXEC=y로 빌드해야
          drgn이 vmcoreinfo를 찾을 수 있다(DESIGN.md §9, libdrgn/program.c의
          "run QEMU with '-device vmcoreinfo'..." 에러 메시지가 근거).
        @qemu_vmlinux: qemu_qmp_address 사용 시, 그 게스트가 부팅한 커널과
          **정확히 같은 빌드**의 vmlinux 경로(빌드가 다르면 build-id 불일치로
          "did not match any loaded modules"가 나며 조용히 무시된다). 내부적으론
          extra_symbols와 합쳐져 load_debug_info()에 전달된다.
        @extra_symbols: 로컬(program_from_kernel()) 모드에서도 비표준 경로의
          vmlinux를 명시적으로 쓰고 싶을 때(예: QEMU 게스트를 9p로 호스트 rootfs를
          마운트해 chroot 후 이 라이브러리를 그대로 실행하는 경우 — 그 게스트
          로컬 커널엔 debuginfod/표준 dbgsym 경로가 없어 program_from_kernel()
          만으로는 심볼을 못 찾는다). 경로 리스트.
        @ebpf_log_path: ebpf/nvme_perf.bt(bpftrace)가 계속 append하는 로그
          파일 경로. get_performance()가 이 파일을 읽는다 — drgn과 무관한
          순수 파일 I/O(DESIGN.md §6/§9.5). 수집기는 게스트 안에서
          `chroot /mnt/host bpftrace nvme_perf.bt >> <이 경로> 2>&1 &`로
          별도 실행해야 하며, 이 경로는 host에서 그 출력이 실제로 쌓이는
          위치(예: QEMU 쓰기 가능 9p 공유의 host 쪽 마운트포인트)여야 한다."""
        # [한국어] 커널 접속 방식(QMP/로컬/추가 심볼) 판단과 Program 생명주기,
        # 그리고 비싼 조회 캐시는 전부 플랫폼(platform.kernel)이 책임진다 —
        # 이 백엔드는 "무엇을 읽을지"(도메인)만 안다. eBPF 로그 접근도 마찬가지로
        # platform.ebpf의 EbpfLogSource로 감싼다.
        self._kernel = DrgnKernelSession(
            program=program,
            qemu_qmp_address=qemu_qmp_address,
            qemu_vmlinux=qemu_vmlinux,
            extra_symbols=extra_symbols,
        )
        self._ebpf = as_log_source(ebpf_log_path)
        # [한국어] 하위호환: 기존 코드/테스트가 참조하던 경로 속성은 남겨둔다.
        self._ebpf_log_path = ebpf_log_path
        # [한국어] 도메인별 서비스(services/*)로 옮겨간 것들. 이 백엔드는 이제
        # 그 서비스들 앞의 파사드 역할을 하며, 옮겨간 메서드는 위임만 한다.
        self._perf = EbpfPerfService(self._ebpf)
        self._events = EbpfEventService(self._ebpf)
        # [한국어] 프로파일러 대상 규칙/세션 저장소. 데몬과 CLI가 같은 파일을
        # 공유하도록 경로를 주입받을 수 있게 한다(기본은 XDG state 경로).
        self._targets = None
        self._target_state = target_state

    def _ensure_program(self):
        """drgn.Program — 접속 방식 판단/생성은 플랫폼 세션이 한다."""
        return self._kernel.program()

    # ---- 디바이스 탐색 (02_nvme_queues.py 재사용) --------------------------

    def _find_disk(self, disk_name_bytes: bytes):
        from drgn.helpers.linux.block import disk_name, for_each_disk

        prog = self._ensure_program()
        for disk in for_each_disk(prog):
            if disk_name(disk) == disk_name_bytes:
                return disk
        return None

    def _get_dev_and_disk(self, device: str):
        from drgn import cast, container_of

        disk = self._find_disk(f"{device}n1".encode())
        if disk is None:
            raise DeviceNotFoundError(device)
        ns = cast("struct nvme_ns *", disk.private_data)
        ctrl = ns.ctrl
        dev = container_of(ctrl, "struct nvme_dev", "ctrl")
        return dev, disk

    def list_devices(self) -> List[str]:
        from drgn.helpers.linux.block import disk_name, for_each_disk

        prog = self._ensure_program()
        names = []
        for disk in for_each_disk(prog):
            m = _DISK_RE.match(disk_name(disk))
            if m:
                names.append(f"nvme{int(m.group(1))}")
        return sorted(names, key=lambda s: int(s[4:]))

    # ---- 요청사항 1: 큐 스냅샷 (sq_tail/cq_head/inflight) ------------------

    def _inflight_by_hctx(self, q) -> Tuple[Dict[int, int], Dict[int, int]]:
        from drgn import FaultError
        from drgn.helpers.linux.block import request_queue_busy_iter

        driver: Dict[int, int] = {}
        sched: Dict[int, int] = {}
        for tags, bucket in (("driver", driver), ("sched", sched)):
            try:
                for rq in request_queue_busy_iter(q, tags):
                    hctx = rq.mq_hctx
                    if not hctx:
                        continue
                    idx = int(hctx.queue_num)
                    bucket[idx] = bucket.get(idx, 0) + 1
            except (FaultError, LookupError):
                # [한국어] 스케줄러가 none 이면 "sched" 태그공간 자체가 없을 수 있음 — 조용히 스킵.
                continue
        return driver, sched

    def get_device_snapshot(self, device: str) -> DeviceSnapshot:
        dev, disk = self._get_dev_and_disk(device)
        ctrl = dev.ctrl
        online = int(dev.online_queues)
        nr_alloc = int(dev.nr_allocated_queues) if hasattr(dev, "nr_allocated_queues") else online
        try:
            model = ctrl.model_number.string_().decode(errors="replace").strip()
        except Exception:
            model = "?"

        driver_counts, sched_counts = self._inflight_by_hctx(disk.queue)

        queues: List[QueueSnapshot] = []
        for qid in range(online):
            nvmeq = dev.queues[qid]
            is_admin = qid == 0
            hctx_index = None if is_admin else qid - 1
            queues.append(
                QueueSnapshot(
                    index=qid,
                    qid=int(nvmeq.qid),
                    is_admin=is_admin,
                    depth=int(nvmeq.q_depth),
                    sq_tail=int(nvmeq.sq_tail),
                    cq_head=int(nvmeq.cq_head),
                    sq_dma_addr=int(nvmeq.sq_dma_addr),
                    cq_dma_addr=int(nvmeq.cq_dma_addr),
                    hctx_index=hctx_index,
                    inflight_driver=driver_counts.get(hctx_index, 0) if hctx_index is not None else 0,
                    inflight_sched=sched_counts.get(hctx_index, 0) if hctx_index is not None else 0,
                )
            )

        return DeviceSnapshot(
            name=device,
            addr=int(dev.value_()),
            model=model,
            online_queues=online,
            allocated_queues=nr_alloc,
            bar_addr=int(dev.bar.value_()),
            dbs_addr=int(dev.dbs.value_()),
            iommu_enabled=_iommu_enabled(),
            backend_kind=self.kind,
            queues=queues,
        )

    # ---- 요청사항 2: 큐 클릭 -> CDW 전체 -----------------------------------

    def get_queue_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[QueueEntry]:
        from drgn import FaultError, cast

        dev, _disk = self._get_dev_and_disk(device)
        online = int(dev.online_queues)
        if not (0 <= qid < online):
            raise QueueNotFoundError(qid)
        nvmeq = dev.queues[qid]
        depth = int(nvmeq.q_depth)
        is_admin = qid == 0
        if around_doorbell:
            indices = _window_indices(int(nvmeq.sq_tail), depth, limit or 16)
        else:
            indices = list(range(min(limit, depth) if limit else depth))

        sqc = cast("struct nvme_command *", nvmeq.sq_cmds)
        out: List[QueueEntry] = []
        for i in indices:
            try:
                common = sqc[i].common
                opcode = int(common.opcode)
                flags = int(common.flags)
                cdw2, cdw3 = _cdw2_cdw3(common)
                out.append(
                    QueueEntry(
                        index=i,
                        cid=int(common.command_id),
                        opcode=opcode,
                        opcode_name=opcode_name(opcode, is_admin),
                        nsid=int(common.nsid),
                        flags=flags,
                        uses_sgl=((flags >> 6) & 0x3) != 0,
                        cdw2=cdw2,
                        cdw3=cdw3,
                        cdw10=int(common.cdw10),
                        cdw11=int(common.cdw11),
                        cdw12=int(common.cdw12),
                        cdw13=int(common.cdw13),
                        cdw14=int(common.cdw14),
                        cdw15=int(common.cdw15),
                        prp1=int(common.dptr.prp1),
                        prp2=int(common.dptr.prp2),
                    )
                )
            except FaultError:
                continue
        return out

    def get_completion_entries(
        self, device: str, qid: int, limit: int = 16, around_doorbell: bool = True
    ) -> List[CompletionEntry]:
        """CQ 링(nvmeq.cqes, struct nvme_completion[]) 덤프. status 필드는
        bit0=phase, bits[15:1]에 SCT/SC/M/DNR이 실려있어 여기서 미리 분해한다."""
        from drgn import FaultError, cast

        dev, _disk = self._get_dev_and_disk(device)
        online = int(dev.online_queues)
        if not (0 <= qid < online):
            raise QueueNotFoundError(qid)
        nvmeq = dev.queues[qid]
        depth = int(nvmeq.q_depth)
        if around_doorbell:
            indices = _window_indices(int(nvmeq.cq_head), depth, limit or 16)
        else:
            indices = list(range(min(limit, depth) if limit else depth))

        cqes = cast("struct nvme_completion *", nvmeq.cqes)
        out: List[CompletionEntry] = []
        for i in indices:
            try:
                cqe = cqes[i]
                status_raw = int(cqe.status)
                try:
                    result = int(cqe.result.u32)
                except (AttributeError, LookupError, TypeError):
                    result = int(cqe.result.u16)  # 구버전/축소 레이아웃 fallback
                out.append(
                    CompletionEntry(
                        index=i,
                        command_id=int(cqe.command_id),
                        sq_id=int(cqe.sq_id),
                        sq_head=int(cqe.sq_head),
                        status_raw=status_raw,
                        phase=bool(status_raw & 0x1),
                        status_code=(status_raw >> 1) & 0xFF,
                        status_code_type=(status_raw >> 9) & 0x7,
                        result=result,
                    )
                )
            except FaultError:
                continue
        return out

    # ---- 요청사항 3: "PRP 확인" -> 4KB 페이로드 -----------------------------

    def _resolve_total_len(self, disk, qid: int, cid: int, cmd, is_admin: bool) -> Tuple[int, Optional[str]]:
        """총 전송 길이. 1순위: blk-mq iod.total_len(가장 정확, in-flight일 때만
        가능) — 04_prp_payload.py와 동일한 경로. 2순위: read/write 커맨드의
        cdw12(NLB, 0-based) * 512B 추정(요청이 이미 완료돼 blk-mq에서 못 찾을 때)."""
        from drgn import FaultError, cast
        from drgn.helpers.linux.block import blk_mq_rq_to_pdu, blk_rq_bytes, request_queue_busy_iter

        hctx_index = None if is_admin else qid - 1
        try:
            for rq in request_queue_busy_iter(disk.queue, "driver"):
                if int(rq.tag) != cid:
                    continue
                hctx = rq.mq_hctx
                if hctx_index is not None and (not hctx or int(hctx.queue_num) != hctx_index):
                    continue
                iod = cast("struct nvme_iod *", blk_mq_rq_to_pdu(rq))
                try:
                    return int(iod.total_len), None
                except (AttributeError, FaultError):
                    return int(blk_rq_bytes(rq)), "iod.total_len 없음 → blk_rq_bytes로 대체"
        except (FaultError, LookupError):
            pass

        opcode = int(cmd.common.opcode)
        if not is_admin and opcode in (0x01, 0x02):  # write, read
            try:
                nlb = int(cmd.rw.length)
            except Exception:
                nlb = int(cmd.common.cdw12) & 0xFFFF
            return (nlb + 1) * 512, "요청을 blk-mq에서 못 찾음(이미 완료됐을 수 있음) → cdw12(NLB)*512B 추정"
        if is_admin and opcode == 0x06:  # identify
            # [한국어] Identify 데이터 구조는 스펙상 항상 정확히 4096B(1페이지) 고정이라
            # NLB 같은 CDW 필드에 안 실려있다 — blk-mq에서 못 찾아도(이미 완료돼도)
            # 걱정 없이 상수로 확정할 수 있다(웹에서 identify PRP가 "총 전송 길이를
            # 알 수 없음"으로 막히던 문제 — 실사용 중 발견).
            return PAGE_SIZE, None
        if is_admin and opcode == 0x02:  # get_log_page
            # [한국어] NUMDL(cdw10[31:16]) + NUMDU(cdw11[15:0]) = 0-based dword 개수.
            cdw10 = int(cmd.common.cdw10)
            cdw11 = int(cmd.common.cdw11)
            numdl = (cdw10 >> 16) & 0xFFFF
            numdu = cdw11 & 0xFFFF
            ndw = ((numdu << 16) | numdl) + 1
            return ndw * 4, None
        return 0, "총 전송 길이를 알 수 없음(비-R/W/identify/get_log 커맨드이며 in-flight 요청도 못 찾음)"

    def get_prp_payload(self, device: str, qid: int, cid: int) -> PrpPayload:
        from drgn import FaultError, cast

        prog = self._ensure_program()
        dev, disk = self._get_dev_and_disk(device)
        online = int(dev.online_queues)
        if not (0 <= qid < online):
            raise QueueNotFoundError(qid)
        nvmeq = dev.queues[qid]
        depth = int(nvmeq.q_depth)
        is_admin = qid == 0

        sqc = cast("struct nvme_command *", nvmeq.sq_cmds)
        cmd = None
        for i in range(depth):
            try:
                c = sqc[i]
                if int(c.common.command_id) == cid:
                    cmd = c
                    break
            except FaultError:
                continue
        if cmd is None:
            return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=False, total_len=0, pages=[],
                               error=f"SQ 링에서 cid={cid} 를 찾지 못함 (큐 depth={depth})")

        flags = int(cmd.common.flags)
        psdt = (flags >> 6) & 0x3
        if psdt != 0:
            return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=True, total_len=0, pages=[])

        prp1 = int(cmd.common.dptr.prp1)
        prp2 = int(cmd.common.dptr.prp2)
        total_len, note = self._resolve_total_len(disk, qid, cid, cmd, is_admin)
        pages = _decode_prp_pages(prog, prp1, prp2, total_len) if total_len > 0 else []
        return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=False, total_len=total_len,
                           pages=pages, error=note)

    # ---- 요청사항 4/6: 포인터 트리 (depth<=10) ------------------------------

    def get_tree_node(self, device: str, path: List[str]):
        dev, _disk = self._get_dev_and_disk(device)
        root_obj = dev[0]  # struct nvme_dev* -> struct nvme_dev (구조체 자체를 루트 노드로)
        return treewalk.expand(root_obj, device, path)

    # ---- eBPF 실시간 성능 (drgn과 무관 — 순수 파일 읽기, DESIGN.md §6/§9.5) ------

    def get_performance(self, device: str):
        """성능 서비스로 위임(Facade).

        이 백엔드는 리팩토링 동안 기존 호출부(gRPC 서버/CLI/웹)를 안 깨뜨리려고
        남겨둔 **파사드**다 — 실제 로직은 services/perf 로 옮겨졌다. 서비스가
        전부 이사하면 이 클래스는 얇은 위임만 남고, 그때 호출부를 서비스에
        직접 붙이면서 걷어낼 수 있다(services/__init__.py 의 규칙 참고)."""
        return self._perf.get_performance(device)

    def get_events(self, device: str):
        """이벤트 서비스로 위임(Facade) — 로직은 services/events 에 있다."""
        return self._events.get_events(device)

    def get_error_stats(self, device: str):
        """이벤트 서비스로 위임(Facade)."""
        return self._events.get_error_stats(device)

    def get_topology(self):
        """통합 토폴로지 트리 — backend/topology.py에 위임한다.

        list_devices()로 찾은 모든 컨트롤러를 한 트리에 넣으므로, 같은 브리지
        아래 붙은 장치들은 조상 노드를 공유한다. drgn 조회가 디바이스당 한
        번씩 일어나 시간이 걸린다(QMP 백엔드 기준 수 초) — 그래서 gRPC 서버는
        이 호출을 executor로 돌린다(server.py)."""
        from telemetryd.backend.topology import build_topology

        return build_topology(self.list_devices(), self._get_dev_and_disk, backend_kind="drgn")

    # ---- NVMe I/O 프로세스 프로파일러 ------------------------------------

    def _registry(self):
        """대상 규칙/세션 저장소(지연 생성) — 파일 I/O라 drgn과 무관."""
        from telemetryd.backend.targets import TargetRegistry

        if self._targets is None:
            self._targets = TargetRegistry(self._target_state)
        return self._targets

    def _process_stats(self):
        """eBPF 탐색 모드 결과(프로세스별 I/O). 수집기가 없으면 빈 목록."""
        from telemetryd.backend.proc_stats import read_process_stats

        if not self._ebpf_log_path:
            return []
        return read_process_stats(self._ebpf_log_path)

    def _proc_infos(self):
        """procinfo.list_processes()(비싼 drgn 조회)의 캐시된 래퍼.

        **이 프로젝트에서 가장 비싼 단일 호출이다** — 프로세스마다 `mm->pgd`부터
        페이지테이블을 직접 걸어 유저 메모리에서 cmdline을 읽어오느라 실측
        60~90초가 걸린다(§9.15). 그런데 모든 backend 호출은 단일 워커
        executor로 직렬화되므로(§9.8), 이게 도는 동안 `/api/devices` 같은
        값싼 호출까지 전부 그 뒤에 줄을 선다.

        캐시를 여기(가장 아래 공통 지점)에 두는 이유: 이 조회를 부르는 곳이
        `list_processes()`(REST /api/processes)와 `get_profile()`(WS
        /ws/profile, **2초 간격 스트림**) 두 군데인데, 처음엔
        `list_processes()`에만 캐시를 달았다가 get_profile 쪽이 캐시를 통째로
        우회해 2초마다 이 조회를 계속 태우는 걸 실측으로 발견했다. 공통
        지점에 두면 호출자가 늘어도 자동으로 보호된다.

        TTL은 고정값이면 안 된다 — 처음 30초로 뒀더니 조회 자체가 60~90초라
        만료되자마자 다음 조회가 시작돼 executor를 여전히 100% 점유했다.
        직전 소요시간에 비례시키는 규칙 자체는 이제 플랫폼
        (platform.cache.AdaptiveTtlCache)이 들고 있고, 세션 공용 캐시를 통해
        적용된다 — 그래서 다른 서비스가 같은 조회를 부르더라도 같은 보호를 받는다."""
        from telemetryd.backend.procinfo import list_processes

        return self._kernel.cached(
            "procinfo.list_processes",
            lambda: list_processes(self._ensure_program()),
        )

    def list_processes(self, only_io: bool = False):
        """게스트(또는 호스트) 커널의 프로세스 목록 + 관측된 I/O 활동을 합친다.

        프로세스 정보는 drgn으로 task_struct에서 읽고(procinfo.py), I/O 활동은
        eBPF 수집기 로그에서 읽는다 — 두 축이 만나야 "지금 이 SSD를 때리는
        프로세스"를 이름 없이도 고를 수 있다(명세 1-1 (d)).

        비싼 부분(프로세스 정보 조회)은 _proc_infos()가 캐시한다 — 그쪽
        docstring 참고. 여기서 하는 합치기/필터링은 값싸므로 매번 새로 한다
        (I/O 활동은 eBPF 로그 읽기라 저렴해서 항상 최신값이 반영된다)."""
        from telemetryd.backend.targets import rule_matches
        from telemetryd.models import ProcessListEntry

        stats = self._process_stats()
        io_by_pid = {}
        for st in stats:
            e = io_by_pid.setdefault(st.pid, {"rate": 0.0, "devices": set(), "comm": st.comm})
            e["rate"] += st.iops
            e["devices"].add(st.device)

        procs = self._proc_infos()
        rules = self._registry().rules
        entries = []
        seen = set()
        for proc in procs:
            seen.add(proc.pid)
            io = io_by_pid.get(proc.pid)
            matched = next((r for r in rules if rule_matches(r, proc)), None)
            entry = ProcessListEntry(
                info=proc,
                io_active=bool(io and io["rate"] > 0),
                io_rate=io["rate"] if io else 0.0,
                target_devices=sorted(io["devices"]) if io else [],
                is_target=matched is not None,
                matched_rule=f"{matched.kind}={matched.value}" if matched else None,
            )
            if proc.error and "커널 스레드" in proc.error:
                # [한국어] 커널 스레드는 대상으로 지정해도 의미가 없다(cmdline도
                # 없고 워크로드 개념이 없음) — 목록에는 두되 선택 불가로.
                entry.selectable = False
                entry.unselectable_reason = "커널 스레드 — 프로파일 대상이 아님"
            entries.append(entry)

        # [한국어] 프로세스 목록에는 없는데 I/O는 잡힌 경우(순회 직후 종료 등)도
        # 버리지 않고 최소 정보로 올린다 — 미관측 I/O 판단에 필요하다.
        from telemetryd.models import ProcessInfo
        for pid, io in io_by_pid.items():
            if pid in seen:
                continue
            entries.append(ProcessListEntry(
                info=ProcessInfo(pid=pid, comm=io["comm"],
                                 error="프로세스 목록에서 사라짐(종료 중일 수 있음)"),
                io_active=io["rate"] > 0, io_rate=io["rate"],
                target_devices=sorted(io["devices"]),
                selectable=False, unselectable_reason="이미 종료된 것으로 보임"))

        if only_io:
            entries = [e for e in entries if e.io_active]
        entries.sort(key=lambda e: (-e.io_rate, e.info.pid))
        return entries

    def list_targets(self):
        return list(self._registry().rules)

    def add_target(self, rule):
        self._registry().add_rule(rule)
        return list(self._registry().rules)

    def remove_target(self, kind: str, value: str):
        self._registry().remove_rule(kind, value)
        return list(self._registry().rules)

    def get_profile(self):
        """세션/논리 그룹/기대 대조/미관측 I/O를 한 번에 만든 스냅샷.

        StreamProfile이 2초 간격으로 부르므로 프로세스 정보는 반드시
        _proc_infos()(캐시됨)로 얻는다 — 예전엔 여기서 procinfo.list_processes()를
        직접 불러 2초마다 60~90초짜리 조회를 태우고 있었다(§9.16)."""
        from telemetryd.models import ProfileSnapshot

        stats = self._process_stats()
        if not self._ebpf_log_path:
            snap = self._registry().refresh(self._proc_infos(), [])
            snap.available = False
            snap.error = ("ebpf_log_path 미설정 — 프로세스별 I/O 수집 결과가 없어 "
                          "세션은 만들어지지만 측정값이 비어 있다")
            return snap
        return self._registry().refresh(self._proc_infos(), stats)

    def list_event_kinds(self):
        """이벤트 서비스로 위임(Facade)."""
        return self._events.list_event_kinds()

def doctor(backend: "DrgnBackend | None" = None) -> dict:
    """00_env_check.py를 구조화한 버전 — `telemetryd doctor` CLI 커맨드가 쓴다.
    root가 아니거나 DWARF 심볼이 없으면 여기서 바로 원인이 드러난다.

    @backend: CLI가 --qemu-qmp로 구성한 DrgnBackend를 넘기면 그 접속 방식
      (host root 또는 QEMU QMP)을 그대로 재사용한다. None이면 기본(host
      program_from_kernel())으로 새로 만든다 — 예전엔 이 매개변수가 없어서
      doctor가 --qemu-qmp를 무시하고 항상 호스트 /proc/kcore를 시도하는
      버그가 있었다(실제 QEMU 게스트로 검증하다가 발견)."""
    checks = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})

    try:
        from drgn import FaultError
    except ImportError as e:
        add("drgn import", False, str(e))
        return {"ok": False, "checks": checks}
    add("drgn import", True)

    if backend is None:
        backend = DrgnBackend()
    try:
        prog = backend._ensure_program()
    except PermissionError as e:
        add("프로그램 연결 — /proc/kcore 접근(root 필요)", False, str(e))
        return {"ok": False, "checks": checks}
    except Exception as e:
        add("프로그램 연결", False, str(e))
        return {"ok": False, "checks": checks}
    add("프로그램 연결 (host root 또는 --qemu-qmp)", True)

    try:
        uts = prog["init_uts_ns"].name
        release = uts.release.string_().decode()
        add("init_uts_ns 읽기 (전역심볼+타입+메모리 3박자)", True, f"release={release}")
    except (KeyError, FaultError) as e:
        add("init_uts_ns 읽기", False, str(e))

    try:
        t = prog.type("struct nvme_dev")
        add("struct nvme_dev 타입 해석 (모듈 DWARF)", True, f"size={t.size}B")
    except LookupError as e:
        add("struct nvme_dev 타입 해석 — nvme 모듈 미로드 또는 dbgsym 없음", False, str(e))

    add("IOMMU", True, "enabled" if _iommu_enabled() else "disabled (PRP=물리주소 직접 읽기 가능)")

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
