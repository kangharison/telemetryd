"""KernelSession의 drgn 어댑터.

접속 방식 3가지를 여기 한 곳에서만 안다(원래 DrgnBackend._ensure_program에
있던 로직):

  1. QEMU 게스트 + QMP 유닉스 소켓 — root 불필요. **반드시 유닉스 소켓**이어야
     한다(TCP 불가): drgn이 vmcoreinfo를 얻으려고 QMP의 dump-guest-memory를
     쓰는데 그 결과 fd를 SCM_RIGHTS로 받기 때문. 게스트는 `-device vmcoreinfo`로
     띄우고 커널은 CONFIG_FW_CFG_SYSFS=y + CONFIG_KEXEC=y여야 한다(DESIGN.md §9.1).
  2. 로컬 커널 + 추가 심볼 경로 — 게스트 안에서 9p+chroot로 이 라이브러리를
     그대로 돌릴 때처럼, debuginfod/dbgsym 표준 경로가 없는 환경용.
  3. 로컬 커널 기본 — root(/proc/kcore) 필요.
"""
from __future__ import annotations

from typing import Any, Callable, Hashable, List, Optional

from telemetryd.platform.cache import AdaptiveTtlCache


class DrgnKernelSession:
    """drgn.Program 하나를 지연 생성해 재사용하는 세션."""

    def __init__(
        self,
        program: Any = None,
        qemu_qmp_address: Optional[str] = None,
        qemu_vmlinux: Optional[str] = None,
        extra_symbols: Optional[List[str]] = None,
        cache: Optional[AdaptiveTtlCache] = None,
    ):
        """@program: 이미 만들어진 drgn.Program을 주입(테스트/재사용).
        @qemu_qmp_address: QEMU 게스트 QMP 유닉스 소켓 경로(모듈 docstring 참고).
        @qemu_vmlinux: 그 게스트가 부팅한 커널과 **정확히 같은 빌드**의 vmlinux.
          다르면 build-id 불일치로 "did not match any loaded modules"가 나며
          조용히 무시된다(DESIGN.md §9.1에서 실제로 겪음).
        @extra_symbols: 로컬 모드에서 명시할 추가 vmlinux 경로들.
        @cache: 비싼 조회용 공용 캐시. 안 주면 기본값으로 하나 만든다."""
        self._prog = program
        self._qemu_qmp_address = qemu_qmp_address
        self._qemu_vmlinux = qemu_vmlinux
        self._extra_symbols = list(extra_symbols) if extra_symbols else []
        self._cache = cache if cache is not None else AdaptiveTtlCache()

    def program(self) -> Any:
        if self._prog is None:
            import drgn

            if self._qemu_qmp_address:
                prog = drgn.Program()
                prog.set_qemu_qmp(self._qemu_qmp_address)
                symbols = ([self._qemu_vmlinux] if self._qemu_vmlinux else []) + self._extra_symbols
                prog.load_debug_info(symbols, default=True, main=True)
                self._prog = prog
            elif self._extra_symbols:
                prog = drgn.program_from_kernel()
                prog.load_debug_info(self._extra_symbols, default=True, main=True)
                self._prog = prog
            else:
                self._prog = drgn.program_from_kernel()
        return self._prog

    def cached(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        return self._cache.get_or_compute(key, compute)

    def invalidate(self, key: Hashable = None) -> None:
        """수동 새로고침 — 다음 조회를 강제로 다시 읽게 한다."""
        self._cache.invalidate(key)

    def describe(self) -> str:
        if self._qemu_qmp_address:
            return f"QEMU 게스트(QMP {self._qemu_qmp_address}, vmlinux={self._qemu_vmlinux})"
        if self._extra_symbols:
            return f"로컬 커널(추가 심볼 {len(self._extra_symbols)}개)"
        return "로컬 커널(/proc/kcore, root 필요)"
