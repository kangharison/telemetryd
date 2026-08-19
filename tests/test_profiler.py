"""NVMe I/O 프로세스 프로파일러 테스트 — 대상 선택 일반화 명세.

이 기능의 요점은 "특정 애플리케이션 전용 모니터가 아니라 범용 프로파일러"라는
것이므로, 테스트도 그 원칙을 지키는지를 본다:
  - 대상을 코드에 하드코딩하지 않고 런타임 규칙으로 고른다(PART 1)
  - 여러 프로세스를 동시에 관측한다(PART 2)
  - 앱 지식 없이도 동작하고, 어댑터가 있으면 의미를 더한다(PART 3)
  - 대상이 바뀌어도 데이터 경계가 유지된다(PART 4 세션)
"""
import os
import tempfile

import pytest

from telemetryd.backend.adapters import select_adapter
from telemetryd.backend.adapters.base import compare, measured_from_stats
from telemetryd.backend.adapters.fio import parse_fio_cmdline, parse_job_file, parse_size
from telemetryd.backend.adapters.generic import infer_role
from telemetryd.backend.proc_stats import parse_tick
from telemetryd.backend.targets import TargetRegistry, rule_matches
from telemetryd.models import (
    MeasuredWorkload,
    ProcessInfo,
    ProcessIoStat,
    TargetRule,
    ThreadIoStat,
    WorkloadSpec,
)


@pytest.fixture()
def registry(tmp_path):
    return TargetRegistry(str(tmp_path / "targets.json"))


def _proc(pid=100, comm="fio", cmdline="fio --name=j --rw=read --bs=4k", **kw):
    kw.setdefault("threads", [(pid, comm)])
    kw.setdefault("thread_count", 1)
    return ProcessInfo(pid=pid, comm=comm, cmdline=cmdline, **kw)


def _stat(pid=100, device="nvme0", **kw):
    base = dict(iops=100.0, read_iops=100.0, write_iops=0.0, bandwidth_bps=409600.0,
                avg_latency_us=1000.0, io_size_dominant=4096,
                io_size_hist=[(4096, 100)], queues=[(1, 100)], queue_depth_est=0.1,
                seq_ratio=0.9, threads=[ThreadIoStat(tid=pid, comm="fio", iops=100.0)])
    base.update(kw)
    return ProcessIoStat(device=device, pid=pid, comm="fio", **base)


# ---- PART 1: 대상 선택 -----------------------------------------------------

def test_all_selection_kinds_converge_to_rules():
    """네 가지 선택 방식이 모두 같은 규칙 매칭으로 수렴한다(명세 1-1)."""
    proc = _proc(pid=4821, comm="fio", cmdline="fio --rw=randwrite --bs=4k",
                 exe_path="/usr/bin/fio")
    assert rule_matches(TargetRule(kind="pid", value="4821"), proc)
    assert rule_matches(TargetRule(kind="name", value="fio"), proc)
    assert rule_matches(TargetRule(kind="name_pattern", value="^f.o$"), proc)
    assert rule_matches(TargetRule(kind="cmdline_pattern", value=".*--rw=randwrite.*"), proc)
    # 안 맞는 경우
    assert not rule_matches(TargetRule(kind="pid", value="1"), proc)
    assert not rule_matches(TargetRule(kind="cmdline_pattern", value="--rw=read"), proc)


def test_name_matches_exe_basename_for_truncated_comm():
    """comm은 15자에서 잘리므로 실행파일 이름으로도 매칭돼야 한다."""
    proc = _proc(pid=1, comm="very-long-name-", exe_path="/opt/bin/very-long-name-tool")
    assert rule_matches(TargetRule(kind="name", value="very-long-name-tool"), proc)


def test_invalid_regex_does_not_break_matching():
    """잘못된 정규식은 예외가 아니라 "안 맞음"이어야 한다(명세 7-2)."""
    assert rule_matches(TargetRule(kind="name_pattern", value="[unclosed"), _proc()) is False


def test_rules_persist_across_registry_instances(tmp_path):
    """이름/패턴 규칙은 프로세스가 죽어도, 데몬이 재시작해도 유지된다(1-4)."""
    path = str(tmp_path / "t.json")
    reg = TargetRegistry(path)
    reg.add_rule(TargetRule(kind="name", value="fio"))
    again = TargetRegistry(path)
    assert [(r.kind, r.value) for r in again.rules] == [("name", "fio")]


def test_add_rule_rejects_unknown_kind(registry):
    with pytest.raises(ValueError):
        registry.add_rule(TargetRule(kind="hostname", value="x"))


# ---- PART 2: 다중 대상 -----------------------------------------------------

def test_multiple_processes_get_separate_sessions(registry):
    registry.add_rule(TargetRule(kind="name", value="fio"))
    procs = [_proc(pid=101, start_time_ns=1), _proc(pid=102, start_time_ns=2)]
    stats = [_stat(pid=101), _stat(pid=102)]
    snap = registry.refresh(procs, stats)
    assert len({s.session_id for s in snap.sessions}) == 2
    assert {s.pid for s in snap.sessions} == {101, 102}


def test_unmonitored_io_and_device_attribution(registry):
    """관측 대상 외 프로세스의 I/O는 숨기지 않고 미귀속으로 드러낸다(2-2/5-2)."""
    registry.add_rule(TargetRule(kind="pid", value="101"))
    procs = [_proc(pid=101, start_time_ns=1), _proc(pid=999, comm="dd", start_time_ns=2)]
    stats = [_stat(pid=101, iops=1000.0), _stat(pid=999, iops=250.0)]
    snap = registry.refresh(procs, stats)

    assert [ (u.pid, u.io_rate) for u in snap.unmonitored_io ] == [(999, 250.0)]
    dev = snap.devices[0]
    assert dev.total_iops == 1250.0
    assert dev.attributed_iops == 1000.0
    assert dev.unattributed_iops == 250.0
    assert dev.multi_process_warning is True     # 성능 수치를 한 프로세스에 귀속 못 함


def test_single_process_has_no_multi_process_warning(registry):
    registry.add_rule(TargetRule(kind="pid", value="101"))
    snap = registry.refresh([_proc(pid=101, start_time_ns=1)], [_stat(pid=101)])
    assert snap.devices[0].multi_process_warning is False


# ---- PART 4: 세션 생명주기 -------------------------------------------------

def test_session_finishes_when_process_disappears(registry):
    registry.add_rule(TargetRule(kind="name", value="fio"))
    snap = registry.refresh([_proc(pid=101, start_time_ns=1)], [_stat(pid=101)])
    sid = snap.sessions[0].session_id
    assert snap.sessions[0].status == "active"

    snap2 = registry.refresh([], [])          # 프로세스가 사라짐
    finished = [s for s in snap2.sessions if s.session_id == sid][0]
    assert finished.status == "finished"
    assert finished.session_end_ns is not None
    # 데이터(특히 cmdline)는 보존된다 — "이 결과는 어떤 조건이었나"의 근거.
    assert finished.cmdline


def test_pid_reuse_creates_separate_session(registry):
    """같은 PID라도 start_time이 다르면 다른 프로세스 -> 다른 세션(1-4/4-2)."""
    registry.add_rule(TargetRule(kind="name", value="fio"))
    snap1 = registry.refresh([_proc(pid=101, start_time_ns=1)], [_stat(pid=101)])
    first = snap1.sessions[0].session_id

    snap2 = registry.refresh([_proc(pid=101, start_time_ns=999)], [_stat(pid=101)])
    ids = {s.session_id for s in snap2.sessions}
    assert first in ids and len(ids) == 2, "PID 재사용이 한 세션에 섞이면 안 된다"
    old = [s for s in snap2.sessions if s.session_id == first][0]
    assert old.status == "finished"


def test_rule_removal_keeps_existing_session_data(registry):
    registry.add_rule(TargetRule(kind="name", value="fio"))
    snap = registry.refresh([_proc(pid=101, start_time_ns=1)], [_stat(pid=101)])
    sid = snap.sessions[0].session_id
    registry.remove_rule("name", "fio")
    snap2 = registry.refresh([_proc(pid=101, start_time_ns=1)], [_stat(pid=101)])
    assert any(s.session_id == sid for s in snap2.sessions)      # 데이터 보존
    assert [s for s in snap2.sessions if s.session_id == sid][0].status == "finished"


# ---- PART 3: 어댑터 --------------------------------------------------------

def test_fio_adapter_parses_cmdline_options():
    parsed = parse_fio_cmdline(
        "fio --name=randwrite --rw=randwrite --bs=4k --iodepth=32 --numjobs=4 "
        "--ioengine=io_uring --direct=1 --filename=/dev/nvme0n1 --runtime=60s")
    job = parsed["jobs"][0]
    assert job["name"] == "randwrite" and job["bs"] == "4k" and job["iodepth"] == "32"
    assert parse_size("4k") == 4096 and parse_size("128K") == 131072
    assert parse_size("1m") == 1048576 and parse_size("4096") == 4096


def test_fio_adapter_multiple_jobs_and_global_options():
    """--name 이후 옵션은 그 job 것, 앞의 것은 전역 기본값."""
    parsed = parse_fio_cmdline(
        "fio --direct=1 --ioengine=libaio --name=a --bs=4k --name=b --bs=128k")
    jobs = {j["name"]: j for j in parsed["jobs"]}
    assert jobs["a"]["bs"] == "4k" and jobs["b"]["bs"] == "128k"
    assert jobs["a"]["ioengine"] == "libaio" and jobs["b"]["direct"] == "1"


def test_fio_adapter_reads_job_file(tmp_path):
    """job 파일을 쓰면 cmdline에 워크로드 정의가 없다 — 파일을 읽어야 한다."""
    jf = tmp_path / "load.fio"
    jf.write_text("[global]\nioengine=libaio\ndirect=1\n\n[seqread]\nrw=read\nbs=128k\niodepth=8\n")
    jobs = parse_job_file(str(jf))
    assert jobs[0]["name"] == "seqread" and jobs[0]["bs"] == "128k"
    assert jobs[0]["ioengine"] == "libaio"      # global이 상속됐는지

    proc = _proc(pid=1, comm="fio", cmdline=f"fio {jf}")
    spec = select_adapter(proc).get_expected_workload(proc)
    assert spec.io_size == 131072 and spec.rw == "read"


def test_fio_adapter_missing_job_file_degrades_gracefully():
    """읽을 수 없는 job 파일 -> 예외 없이 기대값 없음으로 축소(명세 7-2)."""
    proc = _proc(pid=1, comm="fio", cmdline="fio /nonexistent/path.fio")
    adapter = select_adapter(proc)
    assert adapter.name == "fio"
    assert adapter.get_expected_workload(proc) is None
    groups = adapter.get_logical_groups(proc, [_stat(pid=1)])
    assert groups and groups[0].expectation_match is None    # 판단 불가(불일치 아님)


def test_expectation_mismatch_on_io_size():
    """이 기능의 핵심 가치 — 지정한 bs와 실제 SQE 크기가 다른 상황을 잡아낸다."""
    spec = WorkloadSpec(io_size=131072, rw="read", pattern="sequential", queue_depth=8)
    measured = MeasuredWorkload(io_size_dominant=4096, read_ratio=1.0, write_ratio=0.0,
                                queue_depth_avg=8.0)
    ok, reasons = compare(spec, measured, seq_ratio=0.95)
    assert ok is False
    assert any("131072" in r and "4096" in r for r in reasons)


def test_expectation_match_when_everything_agrees():
    spec = WorkloadSpec(io_size=4096, rw="randwrite", pattern="random", queue_depth=32)
    measured = MeasuredWorkload(io_size_dominant=4096, read_ratio=0.0, write_ratio=1.0,
                                queue_depth_avg=31.2)
    ok, reasons = compare(spec, measured, seq_ratio=0.02)
    assert ok is True and reasons == []


def test_compare_without_expectation_is_unknown_not_mismatch():
    ok, reasons = compare(None, MeasuredWorkload(io_size_dominant=4096))
    assert ok is None and reasons == []


def test_generic_adapter_groups_by_comm_prefix_and_marks_inferred():
    proc = _proc(pid=10, comm="myapp", cmdline="./myapp",
                 threads=[(10, "myapp"), (11, "worker_00"), (12, "worker_01")],
                 thread_count=3)
    adapter = select_adapter(proc)
    assert adapter.name == "generic"
    groups = {g.name: g for g in adapter.get_logical_groups(proc, [_stat(pid=10)])}
    assert "worker_*" in groups            # worker_00/01 -> 공통 prefix로 묶임
    assert all(g.inferred for g in groups.values())          # 추정임을 반드시 표시
    assert all(g.expectation_match is None for g in groups.values())


def test_generic_role_inference_from_observed_pattern():
    seq_write = _stat(read_iops=0.0, write_iops=100.0, seq_ratio=0.95)
    rand_read = _stat(read_iops=100.0, write_iops=0.0, seq_ratio=0.05)
    admin_only = _stat(read_iops=0.0, write_iops=0.0, iops=5.0)
    idle = _stat(iops=0.0, read_iops=0.0, write_iops=0.0)
    assert "sequential write" in infer_role(seq_write)
    assert "random read" in infer_role(rand_read)
    assert "admin" in infer_role(admin_only)
    assert "I/O 없음" in infer_role(idle)


def test_adapter_selection_prefers_specific_then_generic():
    assert select_adapter(_proc(comm="fio")).name == "fio"
    assert select_adapter(_proc(comm="dd", cmdline="dd if=/dev/zero")).name == "generic"
    # 명시 지정이 우선하고, 없는 어댑터를 지정하면 generic으로 축소된다.
    assert select_adapter(_proc(comm="dd"), explicit="fio").name == "fio"
    assert select_adapter(_proc(comm="fio"), explicit="nonexistent").name == "generic"


def test_measured_from_stats_apportions_by_thread_share():
    stats = [_stat(pid=5, iops=100.0, threads=[
        ThreadIoStat(tid=5, comm="a", iops=75.0), ThreadIoStat(tid=6, comm="b", iops=25.0)])]
    assert measured_from_stats(stats, tids=[5]).iops == pytest.approx(75.0)
    assert measured_from_stats(stats, tids=[6]).iops == pytest.approx(25.0)
    assert measured_from_stats(stats).iops == pytest.approx(100.0)


# ---- eBPF 파서 -------------------------------------------------------------

def test_parse_tick_reads_all_process_maps():
    """수집기가 찍는 프로세스/스레드 맵 형식(실제 게스트 출력과 동일 포맷)."""
    seg = """
@proc_ops[0, 328, fio]: 734
@proc_bytes[0, 328]: 12026368
@proc_rd[0, 328]: 360
@proc_wr[0, 328]: 374
@proc_bs[0, 328, 16384]: 734
@proc_q[0, 328, 3]: 700
@proc_q[0, 328, 5]: 34
@proc_lat_sum[0, 328]: 1421127510
@proc_lat_cnt[0, 328]: 393
@proc_seq[0, 328]: 12
@proc_rand[0, 328]: 722
@thr_ops[0, 328, 328, fio]: 734
"""
    procs = parse_tick(seg)
    raw = procs[(0, 328)]
    assert raw["comm"] == "fio" and raw["ops"] == 734
    assert raw["sizes"] == {16384: 734} and raw["queues"] == {3: 700, 5: 34}
    assert raw["seq"] == 12 and raw["rand"] == 722
    assert raw["threads"][328]["ops"] == 734


def test_parse_tick_handles_comm_with_spaces():
    """comm에 공백/특수문자가 들어가도 마지막 키로 온전히 읽혀야 한다."""
    procs = parse_tick("@proc_ops[0, 5, my proc]: 3\n")
    assert procs[(0, 5)]["comm"] == "my proc"


def test_no_io_is_unknown_not_match():
    """I/O가 한 건도 없는 대상을 "기대대로 동작 중"으로 표시하면 오독이다.
    (실측 fio에서 발견 — 워커를 fork하는 fio 메인 프로세스가 이 경우다.)"""
    spec = WorkloadSpec(io_size=4096, rw="read", queue_depth=8)
    ok, reasons = compare(spec, MeasuredWorkload())
    assert ok is None
    assert any("관측된 I/O 없음" in r for r in reasons)


def test_daemon_restart_resumes_same_session(tmp_path):
    """데몬을 재시작해도 같은 프로세스(pid+start_time)면 세션을 이어 쓴다.

    안 그러면 재시작할 때마다 살아있는 프로세스에 세션이 하나씩 더 생겨,
    화면에 같은 프로세스가 active/finished로 중복된다(실측으로 발견)."""
    path = str(tmp_path / "t.json")
    reg = TargetRegistry(path)
    reg.add_rule(TargetRule(kind="name", value="fio"))
    proc = _proc(pid=101, start_time_ns=42)
    sid = reg.refresh([proc], [_stat(pid=101)]).sessions[0].session_id

    reloaded = TargetRegistry(path)                 # 데몬 재기동
    snap = reloaded.refresh([proc], [_stat(pid=101)])
    assert [s.session_id for s in snap.sessions] == [sid]
    assert snap.sessions[0].status == "active"
    assert snap.sessions[0].session_end_ns is None
