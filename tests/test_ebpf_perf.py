"""ebpf/nvme_perf.bt(bpftrace) 출력 파서(backend/ebpf_perf.py) 단위 테스트.

실제 게스트/bpftrace 없이도 항상 돌아가게, bpftrace의 네이티브 맵 덤프 형식을
그대로 흉내낸 텍스트를 파일로 써서 검증한다 — 이 형식은 실제 라이브 게스트로
검증하며 확인한 그대로다(DESIGN.md §9.5)."""
from telemetryd.backend.ebpf_perf import device_instance_from_name, read_device_performance

_SAMPLE_LOG = """\
Attaching 5 probes...
nvme_perf.bt attached
@op_count[0, 1]: 100
@read_count[0, 1]: 60
@write_count[0, 1]: 40
@bytes_sum[0, 1]: 4194304
@lat_sum[0, 1]: 50000000
@lat_count[0, 1]: 100
@op_count[1, 2]: 200
@bytes_sum[1, 2]: 8388608
@lat_sum[1, 2]: 100000000
@lat_count[1, 2]: 200
---TICK---
@op_count[0, 1]: 150
@read_count[0, 1]: 90
@write_count[0, 1]: 60
@bytes_sum[0, 1]: 6291456
@lat_sum[0, 1]: 75000000
@lat_count[0, 1]: 150
---TICK---
@op_count[0, 1]: 999
"""


def test_device_instance_from_name():
    assert device_instance_from_name("nvme0") == 0
    assert device_instance_from_name("nvme12") == 12
    try:
        device_instance_from_name("bogus")
        assert False, "ValueError 를 기대했음"
    except ValueError:
        pass


def test_reads_last_complete_tick_only(tmp_path):
    """마지막 "---TICK---" 뒤의 미완성 구간(@op_count[0,1]: 999)은 무시하고,
    바로 앞의 완전한 틱(150/90/60/...)만 써야 한다."""
    log = tmp_path / "nvme_perf.log"
    log.write_text(_SAMPLE_LOG)

    perf = read_device_performance(str(log), "nvme0")
    assert perf.available
    assert len(perf.queues) == 1
    q = perf.queues[0]
    assert q.qid == 1
    assert q.iops == 150.0
    assert q.read_iops == 90.0
    assert q.write_iops == 60.0
    assert q.bandwidth_bytes_per_sec == 6291456.0
    assert q.avg_latency_us == (75000000 / 150) / 1000.0


def test_filters_by_device_instance(tmp_path):
    log = tmp_path / "nvme_perf.log"
    log.write_text(_SAMPLE_LOG)

    perf0 = read_device_performance(str(log), "nvme0")
    assert {q.qid for q in perf0.queues} == {1}

    # [한국어] nvme1(ctrl_id=1, qid=2)은 첫 번째(유일하게 완전한) 틱에만 있었고
    # 마지막 완전 틱(두 번째)에는 등장 안 함 — 그 시점엔 트래픽이 없었다는 뜻.
    perf1 = read_device_performance(str(log), "nvme1")
    assert perf1.queues == []


def test_missing_file_is_unavailable():
    perf = read_device_performance("/no/such/file.log", "nvme0")
    assert not perf.available
    assert perf.queues == []
    assert perf.error


def test_no_data_yet_is_unavailable(tmp_path):
    log = tmp_path / "nvme_perf.log"
    log.write_text("Attaching 5 probes...\nnvme_perf.bt attached\n")  # 첫 틱도 아직 안 끝남

    from telemetryd.backend.ebpf_perf import read_device_performance as read_dev

    perf = read_dev(str(log), "nvme0")
    assert not perf.available
    assert perf.error


# --- p50/p95/p99/p99.9 히스토그램 파싱(§ "latency QoS nine 표기") -----------
#
# 실제 bpftrace hist() 출력 포맷(라이브 게스트에서 `bpftrace -e '... hist(...)
# ...'`로 직접 찍어 확인한 그대로) 그대로 흉내낸다: `@lat_hist[k1, k2]: ` 한 줄
# 뒤에 `[lo, hi)   count |bar|` 버킷 줄들이 빈 줄까지 이어진다.

_HIST_LOG_ONE_QUEUE = """\
Attaching 7 probes...
nvme_perf.bt attached
@op_count[0, 1]: 100
@read_count[0, 1]: 60
@write_count[0, 1]: 40
@bytes_sum[0, 1]: 4194304
@lat_sum[0, 1]: 50000000
@lat_count[0, 1]: 100
@lat_hist[0, 1]:
[256, 512)             10 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@                       |
[512, 1K)               80 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
[1K, 2K)                9 |@@@@@                                              |
[2K, 4K)                1 |                                                   |

---TICK---
"""


def test_percentiles_from_histogram_bucket_upper_bound(tmp_path):
    """분포: 256~512(10), 512~1K(80), 1K~2K(9), 2K~4K(1) — 합 100.
    p50(목표 50)은 누적 10+80=90에서 처음 채워지므로 [512,1K) 버킷 상한(1024ns=1.024us).
    p95(목표 95)/p99(목표 99)는 누적 90+9=99에서 처음 채워지므로 [1K,2K) 상한(2.048us).
    p99.9(목표 99.9)는 누적 99+1=100에서야 채워지므로 [2K,4K) 상한(4.096us)."""
    log = tmp_path / "nvme_perf.log"
    log.write_text(_HIST_LOG_ONE_QUEUE)

    perf = read_device_performance(str(log), "nvme0")
    assert perf.available
    q = perf.queues[0]
    assert q.p50_latency_us == 1.024
    assert q.p95_latency_us == 2.048
    assert q.p99_latency_us == 2.048
    assert q.p999_latency_us == 4.096


def test_aggregate_merges_histograms_across_queues(tmp_path):
    """qid=1과 qid=2의 히스토그램을 합쳐서 디바이스 전체(aggregate, qid=-1)
    percentile을 계산해야 한다.
    qid=1: [256,512)=5, [512,1K)=5 (합 10)
    qid=2: [512,1K)=5, [1K,2K)=5 (합 10)
    합산: [256,512)=5, [512,1K)=10, [1K,2K)=5 (합 20)
    p50(목표 10)은 누적 5+10=15에서 처음 채워지므로 [512,1K) 상한(1.024us).
    p99(목표 19.8)은 누적 15+5=20에서야 채워지므로 [1K,2K) 상한(2.048us)."""
    log_text = """\
Attaching 7 probes...
nvme_perf.bt attached
@op_count[0, 1]: 10
@read_count[0, 1]: 6
@write_count[0, 1]: 4
@bytes_sum[0, 1]: 40960
@lat_sum[0, 1]: 500000
@lat_count[0, 1]: 10
@lat_hist[0, 1]:
[256, 512)              5 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
[512, 1K)               5 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|

@op_count[0, 2]: 10
@read_count[0, 2]: 5
@write_count[0, 2]: 5
@bytes_sum[0, 2]: 40960
@lat_sum[0, 2]: 1000000
@lat_count[0, 2]: 10
@lat_hist[0, 2]:
[512, 1K)               5 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|
[1K, 2K)                5 |@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@|

---TICK---
"""
    log = tmp_path / "nvme_perf.log"
    log.write_text(log_text)

    perf = read_device_performance(str(log), "nvme0")
    assert perf.available
    assert {q.qid for q in perf.queues} == {1, 2}
    assert perf.aggregate is not None
    assert perf.aggregate.qid == -1
    assert perf.aggregate.iops == 20.0  # 두 큐의 op_count 합(10+10)
    assert perf.aggregate.p50_latency_us == 1.024
    assert perf.aggregate.p99_latency_us == 2.048
