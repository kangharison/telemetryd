"""어댑터 레지스트리와 선택 로직(명세 3-5).

선택 순서:
  1. 사용자가 명시적으로 지정한 어댑터가 있으면 그것
  2. 각 어댑터의 matches()를 등록 순서대로 호출, 첫 매칭 채택
  3. 매칭 없으면 generic

플러그인처럼 나중에 추가할 수 있도록 목록 한 곳(_ADAPTERS)에서만 관리한다
(예: mkfs, dd, 데이터베이스 프로세스, 자체 검증 도구). 코어 수집기에는 어떤
애플리케이션 이름도 들어가지 않는다 — 여기가 유일한 접점이다.
"""
from __future__ import annotations

from typing import List, Optional

from telemetryd.backend.adapters.base import AppAdapter, compare, measured_from_stats
from telemetryd.backend.adapters.fio import FioAdapter
from telemetryd.backend.adapters.generic import GenericAdapter
from telemetryd.models import ProcessInfo

#: 등록 순서 = 매칭 우선순위. generic은 항상 마지막(모든 프로세스에 매칭됨).
_ADAPTERS: List[AppAdapter] = [FioAdapter(), GenericAdapter()]

_GENERIC = _ADAPTERS[-1]


def available_adapters() -> List[str]:
    return [a.name for a in _ADAPTERS]


def select_adapter(proc: ProcessInfo, explicit: Optional[str] = None) -> AppAdapter:
    if explicit:
        for a in _ADAPTERS:
            if a.name == explicit:
                return a
        # [한국어] 없는 어댑터를 지정했으면 조용히 generic으로 — 관측 자체가
        # 중단되는 것보다 낫다(명세 7-2 실패 처리 방침).
        return _GENERIC
    for a in _ADAPTERS:
        try:
            if a.matches(proc):
                return a
        except Exception:
            continue    # 어댑터 하나가 터져도 다음 것으로 넘어간다
    return _GENERIC


__all__ = ["AppAdapter", "available_adapters", "select_adapter", "compare", "measured_from_stats"]
