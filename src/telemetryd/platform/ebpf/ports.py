"""eBPF 로그 접근 포트(Protocol).

서비스는 "파일"을 몰라야 한다 — 지금은 9p 공유 위의 파일이지만, 나중에
소켓/파이프/원격 수집기로 바뀔 수 있다. 서비스가 open()/seek()을 직접 하면
그 변경이 서비스 전부로 번진다. 그래서 읽기 방식만 포트로 규정한다.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable


@runtime_checkable
class EbpfLogCursor(Protocol):
    """증분 읽기 커서 — "새로 추가된 완성된 줄들"만 돌려준다.

    한 번 돌려준 줄은 다시 돌려주지 않는다(이벤트 유실/중복 방지). 수집기가
    아직 쓰는 중인 미완성 마지막 줄은 보류했다가 완성되면 그때 준다."""

    def read_new_lines(self) -> List[str]:
        """직전 호출 이후 새로 완성된 줄들. 없으면 빈 리스트."""
        ...


@runtime_checkable
class EbpfLogSource(Protocol):
    """수집기 출력에 대한 읽기 전용 접근."""

    @property
    def available(self) -> bool:
        """수집기 출력에 접근 가능한가(경로 미설정/파일 없음이면 False)."""
        ...

    def read_all(self) -> str:
        """전체 내용. 접근 불가면 빈 문자열."""
        ...

    def read_tail(self, max_bytes: int) -> str:
        """끝에서 최대 max_bytes만. 접근 불가면 빈 문자열.

        바이트 단위로 자르므로 첫 줄은 잘려 있을 수 있다 — 호출 측은 줄 단위
        파싱에서 매칭 안 되는 줄을 그냥 버리는 식으로 견뎌야 한다."""
        ...

    def open_cursor(self) -> EbpfLogCursor:
        """증분 커서를 새로 연다. 커서는 자기 오프셋을 독립적으로 들고 있다."""
        ...
