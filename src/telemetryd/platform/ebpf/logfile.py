"""EbpfLogSource의 파일 기반 어댑터 — 지금 쓰는 유일한 구현.

bpftrace를 게스트 안에서 돌리고 그 stdout을 쓰기 가능한 9p 공유에 append하는
구성(DESIGN.md §9.6)에서, 호스트 쪽이 보는 그 파일이 여기의 대상이다.
"""
from __future__ import annotations

import os
from typing import List, Optional


class _FileCursor:
    """오프셋을 들고 새로 append된 완성 줄만 돌려주는 증분 커서.

    원래 타임아웃 리더/에러 리더에 각각 복사돼 있던 로직을 하나로 합쳤다.
    다루는 까다로운 경우 세 가지:

    1. **미완성 줄**: 수집기가 줄 중간까지만 쓴 상태에서 읽을 수 있다. 마지막
       개행 이후는 버리고 오프셋도 거기까지만 전진시켜, 다음 호출에서 완성된
       줄로 다시 읽는다.
    2. **로그 로테이션/수집기 재시작**: 파일이 잘려 크기가 오프셋보다 작아지면
       오프셋을 0으로 되돌린다. 안 그러면 seek이 파일 끝을 넘어가 이후 내용을
       영영 못 읽는다.
    3. **접근 실패**: 파일이 아직 없거나 권한이 없으면 예외 대신 빈 리스트 —
       수집기가 아직 안 떴을 뿐인 정상 상태이기 때문이다.
    """

    def __init__(self, path: Optional[str]):
        self._path = path
        self._offset = 0

    def read_new_lines(self) -> List[str]:
        if not self._path:
            return []
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0          # (2) 잘렸음 — 처음부터
        try:
            with open(self._path, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
        except OSError:
            return []

        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return []                 # (1) 완성된 줄 없음 — 오프셋 유지
        self._offset += last_nl + 1
        return chunk[:last_nl].decode("utf-8", errors="replace").split("\n")


class FileEbpfLogSource:
    """경로 하나를 감싼 EbpfLogSource 구현.

    경로가 None이면 "수집기 미설정"을 뜻하고 모든 읽기가 빈 값을 준다 —
    호출 측이 None 검사를 매번 하지 않아도 되게(Null Object 패턴에 가깝게)
    설계했다. 다만 available로 그 상태를 구분할 수는 있어야 해서 속성으로 준다."""

    def __init__(self, path: Optional[str]):
        self._path = path

    @property
    def path(self) -> Optional[str]:
        """진단 메시지에서 "어느 파일을 보고 있는지" 알려줄 때만 쓴다."""
        return self._path

    @property
    def available(self) -> bool:
        return bool(self._path) and os.path.exists(self._path)

    def read_all(self) -> str:
        if not self._path:
            return ""
        try:
            with open(self._path, "r", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def read_tail(self, max_bytes: int) -> str:
        if not self._path:
            return ""
        try:
            size = os.path.getsize(self._path)
            with open(self._path, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def open_cursor(self) -> _FileCursor:
        return _FileCursor(self._path)


class NullEbpfLogSource(FileEbpfLogSource):
    """수집기가 전혀 없는 구성(예: mock 백엔드)에서 쓰는 명시적 빈 소스.

    FileEbpfLogSource(None)과 동작은 같지만, 타입 이름으로 의도를 드러내려고
    따로 둔다 — "설정을 깜빡한 것"과 "원래 없는 것"은 다른 상황이다."""

    def __init__(self):
        super().__init__(None)


def as_log_source(source) -> FileEbpfLogSource:
    """EbpfLogSource / 경로 문자열 / None 을 EbpfLogSource로 정규화한다.

    서비스 계층이 아직 "경로 문자열"을 주고받던 시절의 호출부(및 그 테스트)를
    깨지 않으면서 플랫폼으로 옮기려고 둔 어댑터다. 새 코드는 EbpfLogSource를
    직접 주입하는 쪽을 쓴다."""
    if source is None:
        return NullEbpfLogSource()
    if isinstance(source, str):
        return FileEbpfLogSource(source)
    return source
