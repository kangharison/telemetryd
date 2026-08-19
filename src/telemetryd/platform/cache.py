"""TTL 캐시 — 비싼 조회를 공통 지점에서 보호한다.

DESIGN.md §9.16의 사고에서 나온 설비다. 요약:

  - 어떤 조회는 1회에 수십 초가 걸린다(프로세스마다 페이지테이블을 직접 걸어
    cmdline을 읽는 등).
  - 모든 backend 호출은 단일 워커로 직렬화되므로(§9.8), 그 조회가 도는 동안
    값싼 호출까지 전부 뒤에 줄을 선다.
  - 따라서 **고정 TTL은 위험하다**: TTL이 조회 시간보다 짧으면 만료되자마자
    다음 조회가 시작돼 워커를 100% 점유한다(실제로 TTL 30초 < 조회 60~90초로
    겪었다).

그래서 TTL을 **직전 실측 소요시간에 비례**시킨다. factor=3이면 이 조회는
워커의 1/3 이상을 결코 못 쓴다 — 부하가 늘어 조회가 느려질수록 캐시가 알아서
길어지는 자기조절이 된다.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Generic, Hashable, Optional, Tuple, TypeVar

T = TypeVar("T")


class AdaptiveTtlCache(Generic[T]):
    """키별로 (값, 만료시각)을 들고 있는 캐시. TTL은 생성 시 고정이 아니라
    **값을 만드는 데 실제로 걸린 시간**에서 계산한다.

    실제 TTL = max(min_ttl, 소요시간 * duration_factor)

    스레드 안전성: telemetryd는 모든 backend 호출을 단일 워커 executor로
    직렬화하므로(§9.8) 별도 락을 두지 않는다. 여러 스레드에서 동시에 쓰려면
    호출 측에서 직렬화해야 한다."""

    def __init__(self, min_ttl: float = 30.0, duration_factor: float = 3.0,
                 clock: Callable[[], float] = time.monotonic):
        self._min_ttl = min_ttl
        self._duration_factor = duration_factor
        self._clock = clock
        self._entries: Dict[Hashable, Tuple[float, T, float]] = {}

    def get_or_compute(self, key: Hashable, compute: Callable[[], T]) -> T:
        """키가 살아있으면 캐시값을, 아니면 compute()를 실행해 저장 후 반환."""
        now = self._clock()
        hit = self._entries.get(key)
        if hit is not None:
            cached_at, value, ttl = hit
            if (now - cached_at) < ttl:
                return value

        started = now
        value = compute()
        finished = self._clock()
        ttl = max(self._min_ttl, (finished - started) * self._duration_factor)
        self._entries[key] = (finished, value, ttl)
        return value

    def peek(self, key: Hashable) -> Optional[T]:
        """만료 여부와 무관하게 저장된 값만 본다(없으면 None). 진단용."""
        hit = self._entries.get(key)
        return None if hit is None else hit[1]

    def invalidate(self, key: Hashable = None) -> None:
        """키 하나(또는 key=None이면 전체)를 즉시 만료시킨다 — "지금 당장 새로
        읽어라"(수동 새로고침)를 표현할 때 쓴다."""
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)

    def ttl_of(self, key: Hashable) -> Optional[float]:
        """그 키에 적용된 TTL(초). 캐시가 왜 이만큼 유지되는지 설명할 때 쓴다."""
        hit = self._entries.get(key)
        return None if hit is None else hit[2]
