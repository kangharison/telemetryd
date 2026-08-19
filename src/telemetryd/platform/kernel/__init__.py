"""라이브 커널 접근 플랫폼 (drgn).

서비스는 "drgn Program을 어떻게 만들었는지"를 몰라야 한다 — 로컬
/proc/kcore인지, QEMU 게스트에 QMP로 붙은 건지, 어떤 vmlinux를 물렸는지는
접속 구성의 문제이지 도메인의 문제가 아니다.

여기서 함께 책임지는 것:
  - Program 생명주기(지연 생성 + 재사용)
  - 접속 방식 선택(QMP / 로컬 / 추가 심볼)
  - 비싼 조회 캐시(platform.cache)를 붙일 지점 제공
"""
from telemetryd.platform.kernel.ports import KernelSession
from telemetryd.platform.kernel.drgn_session import DrgnKernelSession

__all__ = ["KernelSession", "DrgnKernelSession"]
