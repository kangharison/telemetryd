"""ebpf/nvme_perf.bt(bpftrace)가 찍는 로그를 파싱해서 최신 1초 틱의
(ctrl_id, qid)별 IOPS/대역폭/평균 레이턴시 + p50/p95/p99/p99.9 레이턴시를
돌려준다.

DrgnBackend가 이 모듈로 위임한다 — 순수 파일 읽기+정규식 파싱이라 drgn이 전혀
필요 없다(DESIGN.md §6에서 예고한 "eBPF = 저오버헤드 카운터, drgn = 온디맨드
구조체 스냅샷" 역할 분담의 실제 구현). bpftrace 프로세스 자체는 게스트 안에서
9p+chroot로 별도 실행한다(DESIGN.md §9.5) — 이 모듈은 그 출력 파일을 읽기만
한다.

로그 형식은 bpftrace의 `print(@map)` 네이티브 덤프를 그대로 쓴다(bpftrace
스크립트 안에서 JSON을 손으로 만드는 것보다 훨씬 덜 취약해서 이 파일에서
정규식/라인 파싱으로 판다). 두 가지 맵 형식이 섞여 나온다:

  - 스칼라 맵(op_count 등) 한 줄: `@op_count[0, 1]: 5`
  - 히스토그램 맵(lat_hist) 여러 줄:
        @lat_hist[0, 1]:
        [512, 1K)              3 |@@@@@@@@@@@@@@                          |
        [1K, 2K)                7 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@     |

percentile(p50/p95/p99/p99.9, "QoS nine" 표기)은 이 히스토그램의 누적분포에서
목표 비율을 처음 채우는 버킷의 **상한**을 근사치로 쓴다 — bcc의
biolatency/runqlat 같은 표준 eBPF 툴과 같은 방식. 버킷이 2배수라 오차가
버킷 폭만큼 있을 수 있지만(예: 512~1024ns 버킷이면 그 사이 어딘가), 정확한
값을 얻으려면 요청마다 레이턴시를 다 로그로 남겨야 해서 비용이 크다 —
QoS 모니터링 용도로는 이 근사가 표준적으로 쓰인다."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from telemetryd.models import DevicePerf, QueuePerf
from telemetryd.platform.ebpf import as_log_source as _as_log_source

_SCALAR_RE = re.compile(r"^@(\w+)\[(-?\d+),\s*(-?\d+)\]:\s*(\d+)\s*$")
_HIST_KEY_RE = re.compile(r"^@(\w+)\[(-?\d+),\s*(-?\d+)\]:\s*$")
_HIST_BUCKET_RE = re.compile(r"^\[([0-9.]+[KMG]?), ([0-9.]+[KMG]?)\)\s+(\d+)\s+\|")
_TICK_INTERVAL_SEC = 1.0  # ebpf/nvme_perf.bt의 `interval:s:1` 과 반드시 일치해야 함

_SIZE_MULT = {"K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}

Bucket = Tuple[int, int, int]  # (lo_ns, hi_ns, count)


def _parse_bucket_bound(s: str) -> int:
    """bpftrace hist() 버킷 경계 표기("512", "1K", "2M" 등)를 정수로.
    bpftrace는 항상 2의 거듭제곱 경계만 쓰므로 소수점은 실무상 안 나오지만,
    혹시 몰라 float 경유로 안전하게 처리."""
    s = s.strip()
    if s and s[-1] in _SIZE_MULT:
        return int(float(s[:-1]) * _SIZE_MULT[s[-1]])
    return int(float(s))


def _parse_segment(
    segment: str,
) -> Tuple[Dict[Tuple[int, int], Dict[str, int]], Dict[Tuple[int, int], List[Bucket]]]:
    """`---TICK---`으로 잘린 한 구간 안의 스칼라 맵들과 히스토그램 맵(lat_hist)을
    한 번에 파싱한다. 스칼라는 한 줄짜리라 바로 읽고, 히스토그램은 `@name[k1,k2]:`
    로 시작해 빈 줄(또는 버킷 형식이 아닌 줄)이 나올 때까지 이어지는 블록이다."""
    scalars: Dict[Tuple[int, int], Dict[str, int]] = {}
    hists: Dict[Tuple[int, int], List[Bucket]] = {}
    lines = segment.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        m = _SCALAR_RE.match(line)
        if m:
            _name, k1, k2, value = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            scalars.setdefault((k1, k2), {})[m.group(1)] = value
            i += 1
            continue
        hm = _HIST_KEY_RE.match(line)
        if hm:
            k1, k2 = int(hm.group(2)), int(hm.group(3))
            i += 1
            buckets: List[Bucket] = []
            while i < n:
                bl = lines[i].strip()
                bm = _HIST_BUCKET_RE.match(bl)
                if not bm:
                    break
                lo = _parse_bucket_bound(bm.group(1))
                hi = _parse_bucket_bound(bm.group(2))
                buckets.append((lo, hi, int(bm.group(3))))
                i += 1
            # [한국어] 같은 키가 이 구간에 두 번 나올 일은 없지만(1초에 한 번만
            # print), 방어적으로 합친다.
            hists.setdefault((k1, k2), []).extend(buckets)
            continue
        i += 1
    return scalars, hists


def _parse_last_complete_tick(
    text: str,
) -> Tuple[Dict[Tuple[int, int], Dict[str, int]], Dict[Tuple[int, int], List[Bucket]]]:
    """"---TICK---" 로 나눈 구간 중 마지막으로 완전히 끝난 것만 판다 — 파일
    맨 끝 구간은 bpftrace가 아직 쓰는 중일 수 있어(파일 append 도중 읽기) 뺀다."""
    parts = text.split("---TICK---")
    if len(parts) < 2:
        return {}, {}
    return _parse_segment(parts[-2])


def _merge_buckets(bucket_lists: List[List[Bucket]]) -> List[Bucket]:
    """여러 큐의 히스토그램을 (lo,hi) 기준으로 합산 — 전체 합산(aggregate) 행 계산용.
    같은 hist() 호출에서 나온 버킷들이라 경계가 이미 정렬돼 있다."""
    merged: Dict[Tuple[int, int], int] = {}
    for buckets in bucket_lists:
        for lo, hi, count in buckets:
            merged[(lo, hi)] = merged.get((lo, hi), 0) + count
    return [(lo, hi, count) for (lo, hi), count in merged.items()]


def _percentile_ns(buckets: List[Bucket], p: float) -> float:
    """buckets의 누적분포에서 p(0~1) 비율을 처음 채우는 버킷의 상한을 반환(ns).
    표본이 없으면 0.0."""
    total = sum(c for _, _, c in buckets)
    if total == 0:
        return 0.0
    target = p * total
    cum = 0
    for lo, hi, count in sorted(buckets, key=lambda b: b[0]):
        cum += count
        if cum >= target:
            return float(hi)
    return float(sorted(buckets, key=lambda b: b[0])[-1][1])


def _build_queue_perf(qid: int, counters: Dict[str, int], buckets: List[Bucket]) -> QueuePerf:
    op_count = counters.get("op_count", 0)
    read_count = counters.get("read_count", 0)
    write_count = counters.get("write_count", 0)
    bytes_sum = counters.get("bytes_sum", 0)
    lat_sum = counters.get("lat_sum", 0)
    lat_count = counters.get("lat_count", 0)
    return QueuePerf(
        qid=qid,
        iops=op_count / _TICK_INTERVAL_SEC,
        read_iops=read_count / _TICK_INTERVAL_SEC,
        write_iops=write_count / _TICK_INTERVAL_SEC,
        bandwidth_bytes_per_sec=bytes_sum / _TICK_INTERVAL_SEC,
        avg_latency_us=(lat_sum / lat_count / 1000.0) if lat_count else 0.0,
        p50_latency_us=_percentile_ns(buckets, 0.50) / 1000.0,
        p95_latency_us=_percentile_ns(buckets, 0.95) / 1000.0,
        p99_latency_us=_percentile_ns(buckets, 0.99) / 1000.0,
        p999_latency_us=_percentile_ns(buckets, 0.999) / 1000.0,
    )


def read_latest_performance(
    log_path: str, device_instance: int
) -> Tuple[List[QueuePerf], Optional[QueuePerf]]:
    """log_path(nvme_perf.bt 출력 파일)에서 device_instance(= struct
    nvme_ctrl.instance = "nvme{N}"의 N)에 해당하는 큐들의 최신 성능과, 그
    큐들을 전부 합친 전체(aggregate) 성능을 함께 뽑는다. 파일이 없거나(수집기
    미실행) 아직 틱이 한 번도 안 끝났으면 ([], None)."""
    source = _as_log_source(log_path)
    if not source.available:
        return [], None
    text = source.read_all()

    scalars, hists = _parse_last_complete_tick(text)
    queues: List[QueuePerf] = []
    dev_bucket_lists: List[List[Bucket]] = []
    dev_counters: Dict[str, int] = {}
    for (ctrl_id, qid), counters in sorted(scalars.items()):
        if ctrl_id != device_instance:
            continue
        buckets = hists.get((ctrl_id, qid), [])
        queues.append(_build_queue_perf(qid, counters, buckets))
        dev_bucket_lists.append(buckets)
        for k, v in counters.items():
            dev_counters[k] = dev_counters.get(k, 0) + v

    if not queues:
        return [], None

    aggregate = _build_queue_perf(-1, dev_counters, _merge_buckets(dev_bucket_lists))
    return queues, aggregate


def device_instance_from_name(device: str) -> int:
    """"nvme0" -> 0, "nvme1" -> 1 — DrgnBackend._DISK_RE와 동일 관례."""
    m = re.match(r"^nvme(\d+)$", device)
    if not m:
        raise ValueError(f"디바이스 이름 형식이 아님: {device!r}")
    return int(m.group(1))


def read_device_performance(log_path: str, device: str) -> DevicePerf:
    try:
        instance = device_instance_from_name(device)
    except ValueError as e:
        return DevicePerf(device=device, queues=[], available=False, error=str(e))
    queues, aggregate = read_latest_performance(log_path, instance)
    if not queues:
        return DevicePerf(
            device=device,
            queues=[],
            available=False,
            error="eBPF 수집기 데이터 없음 — nvme_perf.bt가 안 떠 있거나 아직 첫 틱 전 "
            "(DESIGN.md §9.5 참고: chroot로 게스트에서 bpftrace 실행 필요)",
        )
    return DevicePerf(device=device, queues=queues, available=True, aggregate=aggregate)
