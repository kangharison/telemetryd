"""큐/장치 서비스 구현 — drgn으로 커널 자료구조를 직접 읽는다.

deep/scripts/drgn/00~04번 스크립트에서 검증된 경로를 그대로 옮긴 것이다:
gendisk -> nvme_ns -> nvme_ctrl -> container_of(nvme_dev), SQ/CQ 필드,
request_queue_busy_iter를 이용한 hctx별 inflight, PRP 디코딩.

플랫폼(platform.kernel)이 Program 생명주기와 접속 방식을 책임지므로 이 서비스는
"무엇을 읽을지"만 안다 — QMP 소켓 경로나 vmlinux 경로를 여기서 알 일이 없다.
"""
from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional, Tuple

from telemetryd.backend.base import DeviceNotFoundError, QueueNotFoundError
from telemetryd.models import (
    CompletionEntry,
    DeviceSnapshot,
    PrpPage,
    PrpPayload,
    QueueEntry,
    QueueSnapshot,
)
from telemetryd.nvme_const import (
    MAX_PAGE_DUMP,
    PAGE_MASK,
    PAGE_SIZE,
    PRPS_PER_PAGE,
    opcode_name,
)
from telemetryd.platform.kernel import KernelSession
from telemetryd import treewalk
from telemetryd.services.queues.decode import (
    _DISK_RE,
    _cdw2_cdw3,
    _decode_prp_pages,
    _iommu_enabled,
    _window_indices,
)


class DrgnQueueService:
    """@kernel: 커널 세션. Program 생성/재사용은 여기서 신경 쓰지 않는다."""

    #: 스냅샷에 실려 나가는 백엔드 종류 표기 — 화면에서 "지금 보고 있는 게
    #: 실제 커널인지 합성 데이터인지"를 구분하는 용도라 값이 의미를 가진다.
    kind = "drgn"

    def __init__(self, kernel: KernelSession):
        self._kernel = kernel

    def _ensure_program(self):
        return self._kernel.program()

    def _find_disk(self, disk_name_bytes: bytes):
        from drgn.helpers.linux.block import disk_name, for_each_disk

        prog = self._ensure_program()
        for disk in for_each_disk(prog):
            if disk_name(disk) == disk_name_bytes:
                return disk
        return None

    def lookup_device(self, device: str):
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
        dev, disk = self.lookup_device(device)
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

        dev, _disk = self.lookup_device(device)
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

        dev, _disk = self.lookup_device(device)
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
        dev, disk = self.lookup_device(device)
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

        # [한국어] IOMMU가 켜져 있으면 PRP1/PRP2는 물리주소가 아니라 **IOVA**다
        # (디바이스가 보는 주소). 그런데 페이로드 덤프는 prog.read(..., physical=True)로
        # 그 값을 물리주소로 간주해 읽으므로, IOVA를 그대로 읽으면 **엉뚱한 메모리를
        # 읽거나(조용히 쓰레기 데이터) FaultError로 빈 페이지가 나온다**.
        #
        # 이 조건은 QEMU 검증 환경(IOMMU off)에서는 한 번도 안 걸렸지만, 실기
        # 서버는 VT-d/AMD-Vi가 켜져 있는 경우가 흔하다. "조용히 틀린 값"이 가장
        # 나쁜 실패 모드라, 읽지 않고 이유를 분명히 알려준다.
        # (IOVA->물리 변환은 iommu_domain의 페이지테이블을 따라가야 하는데,
        #  그건 이 도구가 아직 안 하는 별도 작업이다.)
        if _iommu_enabled():
            return PrpPayload(
                device=device, qid=qid, cid=cid, uses_sgl=False,
                total_len=total_len, pages=[],
                error="IOMMU가 켜져 있어 PRP 페이로드를 덤프하지 않았다 — PRP1/PRP2가 "
                      "물리주소가 아니라 IOVA라서 그대로 읽으면 엉뚱한 메모리를 읽는다. "
                      "덤프가 필요하면 커널 부팅 옵션에서 IOMMU를 끄거나"
                      "(intel_iommu=off / amd_iommu=off), IOVA->물리 변환을 거쳐야 한다. "
                      "CDW/큐 상태 등 나머지 기능은 IOMMU와 무관하게 정상 동작한다.",
            )

        pages = _decode_prp_pages(prog, prp1, prp2, total_len) if total_len > 0 else []
        return PrpPayload(device=device, qid=qid, cid=cid, uses_sgl=False, total_len=total_len,
                           pages=pages, error=note)

    # ---- 요청사항 4/6: 포인터 트리 (depth<=10) ------------------------------

    def get_tree_node(self, device: str, path: List[str]):
        dev, _disk = self.lookup_device(device)
        root_obj = dev[0]  # struct nvme_dev* -> struct nvme_dev (구조체 자체를 루트 노드로)
        return treewalk.expand(root_obj, device, path)

    # ---- eBPF 실시간 성능 (drgn과 무관 — 순수 파일 읽기, DESIGN.md §6/§9.5) ------
