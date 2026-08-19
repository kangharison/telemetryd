"""서비스 레지스트리 — 조립 지점(composition root).

"어떤 서비스가 있고, 각각 어떤 구현으로 채워졌는가"를 아는 **유일한** 곳이다.
서비스 자신도, 서비스를 쓰는 어댑터(gRPC/REST/CLI)도 이 조립을 몰라야 한다 —
그래야 같은 서비스 코드가 모놀리스와 마이크로서비스 양쪽에서 그대로 돈다.

## 두 형태가 어떻게 갈리는가

    모놀리스      registry.register("perf", EbpfPerfService(log))       # 인프로세스
    마이크로서비스  registry.register("perf", RemotePerfClient(addr))     # 원격 스텁

둘 다 PerfService 계약을 만족하므로 호출 측 코드는 동일하다(Strategy).
지금은 요구대로 모놀리스 조립만 제공하고, 원격 스텁이 들어올 자리를 비워 둔다.

## 왜 dict 하나로 안 하고 클래스를 두나

- **미등록 접근을 명확한 에러로**: 서비스가 빠진 구성(예: 수집기 없이 띄움)에서
  KeyError 대신 "그 서비스가 이 구성에 등록되지 않았다"는 메시지를 준다.
- **계약 위반을 등록 시점에 잡는다**: 나중에 호출하다 AttributeError로 터지는
  대신, 조립하는 순간 어긋난 걸 알 수 있다.
- **가용 서비스 열거**: 진단(`doctor`)과 UI의 기능 노출 판단에 쓴다.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


class ServiceNotRegisteredError(KeyError):
    """이 구성에 등록되지 않은 서비스를 요청했을 때."""


class ServiceRegistry:
    """이름 -> 서비스 구현 매핑."""

    def __init__(self):
        self._services: Dict[str, Any] = {}

    def register(self, name: str, service: Any, contract: Optional[type] = None) -> "ServiceRegistry":
        """@contract를 주면 등록 시점에 계약 충족 여부를 검사한다.
        runtime_checkable Protocol은 메서드 **존재**만 보므로 시그니처까지
        보장하지는 않지만, 오탈자/누락 같은 흔한 실수는 여기서 걸린다.
        체이닝할 수 있게 self를 돌려준다."""
        if contract is not None and not isinstance(service, contract):
            raise TypeError(
                f"서비스 {name!r}({type(service).__name__})가 계약 "
                f"{contract.__name__}을 만족하지 않는다 — 필요한 메서드가 빠졌는지 확인"
            )
        self._services[name] = service
        return self

    def get(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            available = ", ".join(sorted(self._services)) or "(없음)"
            raise ServiceNotRegisteredError(
                f"서비스 {name!r}가 이 구성에 등록되지 않았다. 등록된 것: {available}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> List[str]:
        return sorted(self._services)

    def __contains__(self, name: object) -> bool:
        return name in self._services

    def __iter__(self) -> Iterable[str]:
        return iter(sorted(self._services))
