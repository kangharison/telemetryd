"""Backend 선택 팩토리. CLI/gRPC서버가 --backend mock|drgn 하나만 알면 되게 한다."""
from __future__ import annotations

from telemetryd.backend.base import Backend, DeviceNotFoundError, QueueNotFoundError

_KNOWN = ("mock", "drgn")


def get_backend(kind: str = "mock", **kwargs) -> Backend:
    if kind == "mock":
        from telemetryd.backend.mock_backend import MockBackend

        # [한국어] mock도 프로파일러 대상 저장소 경로를 받을 수 있어야 한다
        # (테스트가 임시 경로로 격리). 그 외 drgn 전용 인자는 조용히 무시한다.
        return MockBackend(target_state=kwargs.get("target_state"))
    if kind == "drgn":
        from telemetryd.backend.drgn_backend import DrgnBackend

        return DrgnBackend(**kwargs)
    raise ValueError(f"알 수 없는 backend 종류: {kind!r} (선택지: {_KNOWN})")


__all__ = ["Backend", "DeviceNotFoundError", "QueueNotFoundError", "get_backend"]
