"""fio 어댑터(명세 3-2) — 표준 벤치마크라 전용 어댑터의 가치가 크다.

실질적 가치는 **기대값 대조**에 있다. job 이름으로 그룹핑하는 것 자체보다,
`--bs=128k`로 지정했는데 실제 SQE가 4K로 나가는 상황(bio 분할, max_sectors_kb
제한)을 잡아내는 것이 목적이다.

cmdline에서 뽑는 것: --name(job 이름) / --rw / --bs / --iodepth / --numjobs /
--ioengine / --direct / --filename|--directory / --runtime|--size.

job 파일을 쓰는 경우(`fio jobfile.fio`)엔 cmdline에 워크로드 정의가 없다 —
그때는 job 파일 경로를 cmdline에서 추출해 읽는다. 상대경로는 프로세스의 cwd
기준이어야 하지만, 이 데몬은 대상(게스트) 파일시스템에 접근할 수 없을 수 있어
읽기에 실패하면 조용히 generic 수준으로 축소한다(명세 7-2).
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Dict, List, Optional

from telemetryd.backend.adapters.base import measured_from_stats
from telemetryd.models import LogicalGroup, ProcessInfo, ProcessIoStat, WorkloadSpec

#: fio가 쓰는 크기 접미사. bs=16k -> 16384. 대소문자 무시.
_SIZE_MULT = {"k": 1024, "m": 1024 * 1024, "g": 1024 ** 3, "b": 1}


def parse_size(text: str) -> Optional[int]:
    """"4k", "128K", "1m", "4096" -> 바이트. 해석 불가면 None."""
    if not text:
        return None
    m = re.fullmatch(r"(\d+)\s*([kKmMgGbB]?)i?[bB]?", text.strip())
    if not m:
        return None
    return int(m.group(1)) * _SIZE_MULT.get(m.group(2).lower(), 1)


def _pattern_of(rw: Optional[str]) -> Optional[str]:
    if not rw:
        return None
    return "random" if rw.startswith("rand") else "sequential"


def parse_fio_cmdline(cmdline: str) -> Dict[str, List[dict]]:
    """fio cmdline -> {"jobs": [job dict...], "jobfiles": [경로...]}.

    fio는 `--name=X` 가 나올 때마다 새 job이 시작되고, 그 뒤의 옵션은 그 job에
    속한다(앞에 나온 전역 옵션은 모든 job의 기본값). 이 규칙을 그대로 구현한다."""
    try:
        tokens = shlex.split(cmdline)
    except ValueError:
        tokens = cmdline.split()
    globals_: dict = {}
    jobs: List[dict] = []
    jobfiles: List[str] = []
    cur: Optional[dict] = None

    for tok in tokens[1:]:            # tokens[0]은 실행파일 경로
        if not tok.startswith("-"):
            # [한국어] 옵션이 아닌 인자는 job 파일 경로로 본다(fio jobfile.fio).
            if tok.endswith(".fio") or "=" not in tok:
                jobfiles.append(tok)
            continue
        body = tok.lstrip("-")
        if "=" in body:
            key, val = body.split("=", 1)
        else:
            key, val = body, "1"       # --direct 처럼 값 없는 플래그
        key = key.strip()
        if key == "name":
            cur = dict(globals_)
            cur["name"] = val
            jobs.append(cur)
            continue
        target = cur if cur is not None else globals_
        target[key] = val
    return {"jobs": jobs, "jobfiles": jobfiles, "globals": globals_}


def _spec_from_opts(opts: dict) -> WorkloadSpec:
    rw = opts.get("rw") or opts.get("readwrite")
    direct = opts.get("direct")
    runtime = opts.get("runtime")
    return WorkloadSpec(
        io_size=parse_size(opts.get("bs", "")),
        rw=rw,
        pattern=_pattern_of(rw),
        queue_depth=int(opts["iodepth"]) if str(opts.get("iodepth", "")).isdigit() else None,
        ioengine=opts.get("ioengine"),
        direct=(direct not in (None, "0", "false")),
        filename=opts.get("filename") or opts.get("directory"),
        numjobs=int(opts["numjobs"]) if str(opts.get("numjobs", "")).isdigit() else None,
        runtime_sec=int(re.sub(r"\D", "", runtime)) if runtime and re.search(r"\d", runtime) else None,
    )


def parse_job_file(path: str) -> List[dict]:
    """fio job 파일(ini 형식) -> job 옵션 dict 목록. 읽기 실패면 빈 목록."""
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    jobs: List[dict] = []
    global_opts: dict = {}
    cur: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.split(";")[0].split("#")[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name.lower() == "global":
                cur = None
                continue
            cur = dict(global_opts)
            cur["name"] = name
            jobs.append(cur)
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if cur is None:
            global_opts[key] = val or "1"
        else:
            cur[key] = val or "1"
    return jobs


class FioAdapter:
    name = "fio"

    def matches(self, proc: ProcessInfo) -> bool:
        # [한국어] comm은 16바이트로 잘리므로 exe 경로도 같이 본다. fio를
        # 래퍼 스크립트로 띄우면 comm이 다를 수 있어 cmdline 첫 토큰도 확인.
        if proc.comm == "fio" or os.path.basename(proc.exe_path or "") == "fio":
            return True
        first = (proc.cmdline or "").split(" ", 1)[0]
        return os.path.basename(first) == "fio"

    def _jobs(self, proc: ProcessInfo) -> List[dict]:
        parsed = parse_fio_cmdline(proc.cmdline or "")
        jobs = parsed["jobs"]
        if jobs:
            return jobs
        # [한국어] cmdline에 job 정의가 없으면 job 파일을 시도한다. 대상이
        # 게스트 안이면 이 경로를 호스트에서 못 읽는 게 정상이라, 실패해도
        # 예외 없이 빈 목록으로 떨어진다(그러면 generic 수준으로 축소).
        for path in parsed["jobfiles"]:
            jobs = parse_job_file(path)
            if jobs:
                return jobs
        return []

    def get_expected_workload(self, proc: ProcessInfo) -> Optional[WorkloadSpec]:
        jobs = self._jobs(proc)
        if not jobs:
            return None
        return _spec_from_opts(jobs[0])

    def get_logical_groups(self, proc: ProcessInfo,
                           stats: List[ProcessIoStat]) -> List[LogicalGroup]:
        jobs = self._jobs(proc)
        if not jobs:
            # [한국어] fio인 건 알지만 워크로드 정의를 못 얻은 경우 —
            # 그룹 하나로 두고 기대값 없이 실측만 보여준다(우아한 축소).
            return [LogicalGroup(
                name=proc.comm or "fio", type="fio_job", source="cmdline_parse",
                thread_tids=[t for t, _ in (proc.threads or [])],
                measured_workload=measured_from_stats(stats),
                expectation_match=None,
                mismatch_reasons=["fio job 정의를 찾지 못함(job 파일 미접근 등)"],
            )]

        all_tids = [t for t, _ in (proc.threads or [])]
        thread_comm = {t: c for t, c in (proc.threads or [])}
        groups: List[LogicalGroup] = []
        assigned: set = set()

        for job in jobs:
            job_name = job.get("name", "job")
            # [한국어] fio는 스레드 이름을 job 이름 기반으로 잡는데 comm은 16바이트
            # 제한으로 잘린다 — 그래서 완전 일치가 아니라 prefix로 매칭한다.
            tids = [t for t, c in thread_comm.items()
                    if c and (job_name.startswith(c) or c.startswith(job_name[:15]))]
            tids = [t for t in tids if t not in assigned]
            if not tids and len(jobs) == 1:
                # [한국어] job이 하나뿐이면 이 프로세스의 스레드 전부가 그 job이다
                # (fio가 numjobs를 fork로 처리하면 프로세스당 job 하나 = 흔한 경우).
                tids = [t for t in all_tids if t not in assigned]
            assigned.update(tids)
            spec = _spec_from_opts(job)
            measured = measured_from_stats(stats, tids if len(jobs) > 1 else None)
            seq_ratio = stats[0].seq_ratio if stats else None
            from telemetryd.backend.adapters.base import compare
            ok, reasons = compare(spec, measured, seq_ratio)
            groups.append(LogicalGroup(
                name=job_name, type="fio_job", source="cmdline_parse",
                thread_tids=sorted(tids),
                expected_workload=spec, measured_workload=measured,
                expectation_match=ok, mismatch_reasons=reasons,
            ))
        return groups

    def get_progress(self, proc: ProcessInfo) -> Optional[float]:
        # [한국어] runtime 기반 진행률은 프로세스 시작 시각이 필요한데, 그건
        # 세션 계층이 들고 있다(어댑터는 프로세스 정보만 본다) — 여기서는
        # 계산하지 않고 None. 필요해지면 세션에서 runtime_sec으로 계산한다.
        return None
