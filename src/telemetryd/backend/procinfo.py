"""프로세스 인벤토리 — 대상 선택 후보 목록(명세 1-2)을 만든다.

=== 왜 /proc 이 아니라 task_struct 인가 ===
명세는 `/proc/<pid>/comm`, `/cmdline`, `/exe`, `/status`, `/stat`, `/task/`를
소스로 든다. 하지만 이 프로젝트의 관측 대상은 **QEMU 게스트 안에서 도는
프로세스**이고 telemetryd 데몬은 호스트에서 돈다 — 호스트의 /proc에는 게스트
프로세스가 아예 없다. 그래서 이미 이 프로젝트의 전제인 drgn(게스트 커널 라이브
조회)으로 커널 자료구조에서 같은 정보를 직접 읽는다. 호스트 라이브(sudo drgn,
/proc/kcore) 환경에서도 **같은 코드가 그대로** 동작하므로 분기도 필요 없다.

| 명세의 /proc 필드      | 여기서 읽는 커널 위치                                  |
|------------------------|--------------------------------------------------------|
| /proc/<pid>/comm       | task.comm                                              |
| /proc/<pid>/cmdline    | task.mm->arg_start..arg_end (유저 메모리 직접 읽기)     |
| /proc/<pid>/exe        | task.mm->exe_file->f_path -> d_path()                  |
| /proc/<pid>/status(uid)| task.cred->uid.val                                     |
| /proc/<pid>/stat(시작) | task.start_boottime (PID 재사용 구분용)                 |
| /proc/<pid>/task/      | 전체 task 순회 후 tgid로 묶음(스레드 목록)              |

=== 실패 처리 ===
프로세스가 순회 도중 사라지거나(경합) 유저 메모리를 못 읽는 경우가 정상적으로
발생한다. 명세 7-2/7-3 대로 예외를 밖으로 던지지 않고 해당 필드만 비운 채
`ProcessInfo.error`에 사유만 남긴다 — 목록 전체가 실패하면 안 된다.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from telemetryd.models import ProcessInfo

#: cmdline이 비정상적으로 길 때 잘라내는 상한(관측용 표시라 이 정도면 충분).
_CMDLINE_MAX = 4096

#: x86-64 4단계 페이징 상수 — 아래 수동 페이지 테이블 워커에서 쓴다.
_PTE_PHYS_MASK = 0x000F_FFFF_FFFF_F000   # 엔트리에서 물리 주소 부분
_PTE_PRESENT = 1 << 0
_PTE_PSE = 1 << 7                         # 큰 페이지(2MB/1GB)
_PAGE_SIZE = 4096
#: KASLR 없는 x86-64의 직접 매핑(direct map) 시작 주소. page_offset_base 심볼을
#: 못 읽을 때의 폴백 — 이 프로젝트의 게스트는 nokaslr로 부팅한다(DESIGN.md §9.2).
_DEFAULT_PAGE_OFFSET = 0xFFFF_8880_0000_0000


def _page_offset_base(prog) -> int:
    v = _try(lambda: int(prog["page_offset_base"]))
    return v if v else _DEFAULT_PAGE_OFFSET


def _read_user_via_pagetable(prog, mm, addr: int, size: int) -> bytes:
    """대상 프로세스의 페이지 테이블을 직접 걸어 유저 메모리를 읽는다.

    왜 필요한가: drgn의 access_process_vm()은 QMP로 붙은 라이브 게스트에서
    "recursive address translation; page table may be missing from core dump"로
    실패한다(실측). 페이지 테이블 엔트리를 읽으려면 커널 가상주소를 다시 변환해야
    하는데 그 변환이 재귀에 걸리기 때문이다. 반면 **물리 주소 직접 읽기**
    (prog.read(..., physical=True))는 이 환경에서 이미 검증돼 있다(PRP 페이로드
    덤프가 그 방식, DESIGN.md §9.5). 그래서 pgd만 물리 주소로 바꾼 뒤 그 아래
    모든 단계를 물리 읽기로 처리한다.

    x86-64 4단계 페이징(PGD→PUD→PMD→PTE)만 다룬다. 2MB/1GB 큰 페이지는 PSE
    비트로 감지해 그 지점에서 오프셋을 더해 끝낸다. 5단계 페이징(LA57)은 이
    프로젝트 게스트에 해당 없어 지원하지 않는다 — 만나면 그냥 실패로 떨어져
    상위에서 사유가 기록된다."""
    pgd_va = int(mm.pgd)
    if not pgd_va:
        raise ValueError("mm->pgd 없음")
    pgd_pa = pgd_va - _page_offset_base(prog)

    def entry(table_pa: int, index: int) -> int:
        raw = prog.read(table_pa + index * 8, 8, physical=True)
        return int.from_bytes(raw, "little")

    out = bytearray()
    remaining = size
    va = addr
    while remaining > 0:
        e = entry(pgd_pa, (va >> 39) & 0x1FF)
        if not (e & _PTE_PRESENT):
            break
        e = entry(e & _PTE_PHYS_MASK, (va >> 30) & 0x1FF)
        if not (e & _PTE_PRESENT):
            break
        if e & _PTE_PSE:                                  # 1GB 페이지
            phys = (e & _PTE_PHYS_MASK) + (va & ((1 << 30) - 1))
            chunk = min(remaining, (1 << 30) - (va & ((1 << 30) - 1)))
        else:
            e = entry(e & _PTE_PHYS_MASK, (va >> 21) & 0x1FF)
            if not (e & _PTE_PRESENT):
                break
            if e & _PTE_PSE:                              # 2MB 페이지
                phys = (e & _PTE_PHYS_MASK) + (va & ((1 << 21) - 1))
                chunk = min(remaining, (1 << 21) - (va & ((1 << 21) - 1)))
            else:
                e = entry(e & _PTE_PHYS_MASK, (va >> 12) & 0x1FF)
                if not (e & _PTE_PRESENT):
                    break
                phys = (e & _PTE_PHYS_MASK) + (va & (_PAGE_SIZE - 1))
                chunk = min(remaining, _PAGE_SIZE - (va & (_PAGE_SIZE - 1)))
        out += prog.read(phys, chunk, physical=True)
        va += chunk
        remaining -= chunk
    return bytes(out)


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _comm(task) -> str:
    return _try(lambda: task.comm.string_().decode(errors="replace"), "") or ""


def _cmdline(prog, task):
    """task의 유저 주소공간에서 argv 영역을 읽어 공백으로 잇는다.

    반환: (cmdline, 실패 사유). 실패를 조용히 삼키지 않고 사유를 돌려주는 이유:
    cmdline이 비면 fio 같은 어댑터가 워크로드를 못 읽어 관측 품질이 확 떨어지는데,
    "커널 스레드라 원래 없음"인지 "읽기에 실패함"인지 구분되지 않으면 원인을 못
    찾는다(명세 7-2: 해당 필드만 비우되 사유는 남긴다)."""
    from drgn.helpers.linux.mm import access_process_vm

    mm = _try(lambda: task.mm)
    if mm is None or not mm:
        return "", None      # 커널 스레드 — 원래 cmdline이 없다
    start = _try(lambda: int(mm.arg_start), 0)
    end = _try(lambda: int(mm.arg_end), 0)
    if not start or end <= start:
        return "", "mm->arg_start/arg_end가 비어 있음"
    n = min(end - start, _CMDLINE_MAX)
    try:
        raw = access_process_vm(task, start, n)
    except Exception as helper_err:
        # [한국어] QMP 라이브 게스트에서는 이 헬퍼가 주소 변환 재귀로 실패한다 —
        # 페이지 테이블을 직접 걸어 물리 읽기로 우회한다(위 함수 주석 참고).
        try:
            raw = _read_user_via_pagetable(prog, mm, start, n)
        except Exception as e:
            return "", (f"유저 메모리 읽기 실패: {type(helper_err).__name__} "
                        f"-> 페이지테이블 폴백도 실패: {type(e).__name__}: {e}")
        if not raw:
            return "", "유저 메모리가 매핑돼 있지 않음(스왑/미할당)"
    if raw is None:
        return "", "유저 메모리 읽기 실패(빈 결과)"
    # [한국어] argv는 널로 구분된 문자열들의 나열이라 공백으로 이어 붙인다
    # (/proc/<pid>/cmdline과 같은 표현 — fio 옵션 확인에 그대로 쓴다).
    return " ".join(x for x in raw.decode("utf-8", errors="replace").split("\x00") if x), None


def _exe_path(task) -> str:
    from drgn.helpers.linux.fs import d_path

    mm = _try(lambda: task.mm)
    if mm is None or not mm:
        return ""
    exe = _try(lambda: mm.exe_file)
    if exe is None or not exe:
        return ""
    return _try(lambda: d_path(exe.f_path.address_of_()).decode(errors="replace"), "") or ""


def list_processes(prog, with_cmdline: bool = True) -> List[ProcessInfo]:
    """게스트(또는 호스트) 커널의 전체 프로세스 목록.

    전체 task를 한 번 순회하면서 tgid로 묶는다 — 프로세스마다 스레드 목록을
    따로 조회하는 것보다 훨씬 싸다(명세 7-4: 목록 스캔은 저빈도로 충분하며
    한 번의 순회로 끝낸다).

    with_cmdline=False면 유저 메모리 읽기를 건너뛴다(빠른 갱신용)."""
    from drgn.helpers.linux.pid import for_each_task

    groups: Dict[int, dict] = {}
    for task in for_each_task(prog):
        tgid = _try(lambda: int(task.tgid))
        tid = _try(lambda: int(task.pid))
        if tgid is None or tid is None:
            continue
        g = groups.setdefault(tgid, {"leader": None, "threads": []})
        g["threads"].append((tid, _comm(task)))
        if tid == tgid:
            g["leader"] = task

    out: List[ProcessInfo] = []
    for tgid, g in groups.items():
        leader = g["leader"]
        threads = sorted(g["threads"])
        if leader is None:
            # [한국어] 리더 task를 못 본 경우(순회 중 종료 등) — 스레드 정보만으로
            # 최소한의 항목을 만든다. 목록에서 통째로 빠지는 것보다 낫다.
            out.append(ProcessInfo(pid=tgid, comm=threads[0][1] if threads else "",
                                   thread_count=len(threads), threads=threads,
                                   error="스레드 그룹 리더를 못 찾음(종료 중일 수 있음)"))
            continue
        cmdline, cmd_err = _cmdline(prog, leader) if with_cmdline else ("", None)
        info = ProcessInfo(
            pid=tgid,
            comm=_comm(leader),
            cmdline=cmdline,
            exe_path=_exe_path(leader),
            uid=_try(lambda: int(leader.cred.uid.val), -1),
            start_time_ns=_try(lambda: int(leader.start_boottime), 0),
            thread_count=len(threads),
            threads=threads,
        )
        if cmd_err:
            info.error = cmd_err
        if not info.cmdline and not info.exe_path:
            # [한국어] mm이 없는 커널 스레드 — 사용자가 "왜 cmdline이 비었나"를
            # 헷갈리지 않게 사유를 남긴다(선택 불가 대상임을 UI가 표시할 근거).
            info.error = "커널 스레드(mm 없음) — cmdline/exe 없음"
        out.append(info)
    out.sort(key=lambda p: p.pid)
    return out


def find_process(prog, pid: int) -> Optional[ProcessInfo]:
    for p in list_processes(prog):
        if p.pid == pid:
            return p
    return None
