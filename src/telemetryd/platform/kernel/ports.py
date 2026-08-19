"""커널 접근 포트(Protocol)."""
from __future__ import annotations

from typing import Any, Callable, Hashable, Protocol, runtime_checkable


@runtime_checkable
class KernelSession(Protocol):
    """라이브 커널에 대한 세션.

    `program()`이 돌려주는 건 drgn.Program이다 — 이 포트가 drgn을 완전히
    감추지는 않는다. 감추려면 커널 자료구조 접근 전체를 추상화해야 하는데,
    그건 drgn의 표현력(타입 인식 포인터 순회)을 통째로 잃는 대가가 너무 크다.
    대신 **접속 구성과 생명주기만** 감춘다 — 서비스가 "QMP 소켓 경로"나
    "vmlinux 경로"를 아는 일은 없게 한다."""

    def program(self) -> Any:
        """drgn.Program (지연 생성 후 재사용)."""
        ...

    def cached(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        """비싼 커널 조회를 세션 공용 캐시로 감싼다.

        캐시를 세션(=플랫폼)에 두는 이유는 DESIGN.md §9.16에서 실측한 문제
        때문이다: 같은 비싼 조회를 여러 서비스가 각자 부르면, 한 서비스에만
        캐시를 달아도 다른 경로가 그대로 우회해 워커를 점유한다. 공통 지점인
        세션에 두면 호출자가 늘어도 자동으로 보호된다."""
        ...

    def describe(self) -> str:
        """이 세션이 어디에 어떻게 붙었는지 사람이 읽을 한 줄(진단용)."""
        ...
