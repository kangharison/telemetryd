"""ebpf/nvme_perf.bt의 **프로세스/스레드 단위 맵**을 파싱한다 — 범용 NVMe I/O
프로파일러의 "탐색 모드"(명세 5-2) 수집 결과.

무엇을 파싱하나 (모두 1초 틱 카운터):
  @proc_ops[ctrl, tgid, comm]      제출 커맨드 수      -> IOPS
  @proc_rd/@proc_wr[ctrl, tgid]    read/write 분리     -> R/W 비율
  @proc_bytes[ctrl, tgid]          전송 바이트         -> 대역폭
  @proc_bs[ctrl, tgid, bytes]      I/O 크기 분포       -> 실측 bs (기대값 대조용)
  @proc_q[ctrl, tgid, qid]         큐 사용             -> 큐 공유/경합 분석
  @proc_lat_sum/@proc_lat_cnt      완료 지연 합/건수   -> 평균 지연, QD 근사
  @thr_ops[ctrl, tgid, tid, comm]  스레드별 제출 수    -> 논리 그룹(job) 매핑

왜 여기서 프로세스를 거르지 않나: bpftrace는 유저스페이스에서 맵을 갱신할 수
없어서(명세 5-1의 target_pids 필터 맵은 libbpf 기반 수집기가 필요) 커널 쪽은
**항상 전 프로세스를 센다**. 대신 대상 선택/필터링은 이 데이터를 받은 호스트
쪽(backend/targets.py)에서 한다 — 관측 결과는 같고, 커널 쪽 오버헤드만 늘어난다
(실측치는 DESIGN.md §9.15). 이 구조 덕분에 "관측 대상이 아닌 프로세스가 같은
장치에 I/O를 내고 있다"(명세 2-2/5-2)는 것도 공짜로 알 수 있다.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from telemetryd.models import ProcessIoStat, ThreadIoStat
from telemetryd.platform.ebpf import as_log_source as _as_log_source

_TICK_INTERVAL_SEC = 1.0        # nvme_perf.bt의 interval:s:1 과 일치해야 함
_TAIL_BYTES = 2 * 1024 * 1024   # 로그 끝에서 이만큼만 읽는다(수집기 로그는 수백MB까지 자람)

# [한국어] comm은 공백/특수문자가 들어갈 수 있어 마지막 키는 탐욕적으로 받는다.
_RE_PROC_OPS = re.compile(r"^@proc_ops\[(-?\d+), (-?\d+), (.*)\]:\s*(\d+)$")
_RE_THR_OPS = re.compile(r"^@thr_ops\[(-?\d+), (-?\d+), (-?\d+), (.*)\]:\s*(\d+)$")
_RE_2KEY = re.compile(r"^@(proc_bytes|proc_rd|proc_wr|proc_lat_sum|proc_lat_cnt|proc_seq|proc_rand)\[(-?\d+), (-?\d+)\]:\s*(\d+)$")
_RE_3KEY = re.compile(r"^@(proc_bs|proc_q)\[(-?\d+), (-?\d+), (-?\d+)\]:\s*(\d+)$")


def _read_tail(log_path) -> str:
    """로그 끝부분만 읽는다 — 이 맵들은 매 틱 전체가 다시 찍히므로 끝만 봐도
    충분하고, 로그가 수백 MB로 자라기 때문에 전체 읽기는 피해야 한다.
    실제 파일 접근은 플랫폼(EbpfLogSource)이 담당한다."""
    return _as_log_source(log_path).read_tail(_TAIL_BYTES)


def _last_complete_tick(text: str) -> str:
    """마지막으로 **완전히 끝난** 틱 구간. 파일 맨 끝 구간은 수집기가 아직 쓰는
    중일 수 있어 제외한다(backend/ebpf_perf.py와 같은 규칙)."""
    parts = text.split("---TICK---")
    return parts[-2] if len(parts) >= 2 else ""


def parse_tick(segment: str) -> Dict[Tuple[int, int], dict]:
    """한 틱 구간 -> {(ctrl_id, tgid): 원시 카운터 dict}."""
    procs: Dict[Tuple[int, int], dict] = {}

    def slot(ctrl: int, tgid: int) -> dict:
        return procs.setdefault((ctrl, tgid), {
            "comm": "", "ops": 0, "read": 0, "write": 0, "bytes": 0,
            "lat_sum": 0, "lat_cnt": 0, "seq": 0, "rand": 0,
            "sizes": {}, "queues": {}, "threads": {},
        })

    for line in segment.splitlines():
        line = line.strip()
        if not line.startswith("@proc") and not line.startswith("@thr_ops"):
            continue
        m = _RE_PROC_OPS.match(line)
        if m:
            ctrl, tgid, comm, n = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
            s = slot(ctrl, tgid)
            s["comm"] = comm
            s["ops"] += n
            continue
        m = _RE_THR_OPS.match(line)
        if m:
            ctrl, tgid, tid, comm, n = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                        m.group(4), int(m.group(5)))
            s = slot(ctrl, tgid)
            t = s["threads"].setdefault(tid, {"comm": comm, "ops": 0})
            t["ops"] += n
            t["comm"] = comm
            continue
        m = _RE_2KEY.match(line)
        if m:
            name, ctrl, tgid, v = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            s = slot(ctrl, tgid)
            key = {"proc_bytes": "bytes", "proc_rd": "read", "proc_wr": "write",
                   "proc_lat_sum": "lat_sum", "proc_lat_cnt": "lat_cnt",
                   "proc_seq": "seq", "proc_rand": "rand"}[name]
            s[key] += v
            continue
        m = _RE_3KEY.match(line)
        if m:
            name, ctrl, tgid, k, v = (m.group(1), int(m.group(2)), int(m.group(3)),
                                      int(m.group(4)), int(m.group(5)))
            s = slot(ctrl, tgid)
            bucket = "sizes" if name == "proc_bs" else "queues"
            s[bucket][k] = s[bucket].get(k, 0) + v
    return procs


def _to_stat(ctrl_id: int, tgid: int, raw: dict) -> ProcessIoStat:
    ops = raw["ops"]
    lat_cnt = raw["lat_cnt"]
    avg_lat_us = (raw["lat_sum"] / lat_cnt / 1000.0) if lat_cnt else 0.0
    iops = ops / _TICK_INTERVAL_SEC
    # [한국어] 실측 큐 깊이(QD)는 직접 셀 수 없어(제출/완료가 다른 컨텍스트) 리틀의
    # 법칙으로 근사한다: 평균 재고 = 도착률 × 체류시간 = IOPS × 평균 지연.
    # fio --iodepth 와 대조하는 용도라 근사로 충분하고, 근사임을 모델에 명시한다.
    qd_est = iops * (avg_lat_us / 1_000_000.0) if avg_lat_us else 0.0
    sizes = sorted(raw["sizes"].items(), key=lambda kv: -kv[1])
    seq_total = raw["seq"] + raw["rand"]
    seq_ratio = (raw["seq"] / seq_total) if seq_total else None
    threads = [ThreadIoStat(tid=tid, comm=t["comm"], iops=t["ops"] / _TICK_INTERVAL_SEC)
               for tid, t in sorted(raw["threads"].items(), key=lambda kv: -kv[1]["ops"])]
    return ProcessIoStat(
        device=f"nvme{ctrl_id}",
        pid=tgid,
        comm=raw["comm"] or (threads[0].comm if threads else ""),
        iops=iops,
        read_iops=raw["read"] / _TICK_INTERVAL_SEC,
        write_iops=raw["write"] / _TICK_INTERVAL_SEC,
        bandwidth_bps=raw["bytes"] / _TICK_INTERVAL_SEC,
        avg_latency_us=avg_lat_us,
        io_size_dominant=sizes[0][0] if sizes else 0,
        io_size_hist=[(size, cnt) for size, cnt in sizes],
        queues=[(qid, cnt) for qid, cnt in sorted(raw["queues"].items())],
        queue_depth_est=qd_est,
        seq_ratio=seq_ratio,
        threads=threads,
    )


def read_process_stats(log_path: str, device: Optional[str] = None) -> List[ProcessIoStat]:
    """수집기 로그에서 최신 틱의 프로세스별 I/O 통계를 뽑는다.

    device를 주면 그 컨트롤러 것만, 없으면 전 장치. 한 프로세스가 여러 장치에
    I/O를 내면 (device, pid) 조합마다 한 항목이 나온다 — 어느 장치를 때리는지가
    대상 선택의 핵심 정보라(명세 1-2 target_devices) 합치지 않는다."""
    procs = parse_tick(_last_complete_tick(_read_tail(log_path)))
    want = None
    if device is not None:
        m = re.match(r"^nvme(\d+)$", device)
        if not m:
            return []
        want = int(m.group(1))
    out = [_to_stat(ctrl, tgid, raw) for (ctrl, tgid), raw in procs.items()
           if want is None or ctrl == want]
    out.sort(key=lambda s: -s.iops)
    return out
