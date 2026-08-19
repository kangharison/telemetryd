"""프로파일러 서비스 계약 — NVMe I/O를 내는 프로세스의 범용 프로파일러(§9.15).

"특정 애플리케이션 전용 모니터가 아니라 관측 대상이 런타임에 선택되는 범용
프로파일러"라는 요구가 계약에 그대로 드러난다 — 어떤 애플리케이션 이름도
여기 없고, 대상은 `TargetRule`(pid/name/name_pattern/cmdline_pattern)로만
표현된다.
"""
from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from telemetryd.models import ProcessListEntry, ProfileSnapshot, TargetRule

SERVICE_NAME = "profiler"


@runtime_checkable
class ProfilerService(Protocol):
    """프로세스 목록 조회 + 관측 대상 규칙 관리 + 프로파일 스냅샷."""

    def list_processes(self, only_io: bool = False) -> List[ProcessListEntry]:
        """커널의 프로세스 목록 + 관측된 I/O 활동을 합친 것.

        @only_io: True면 실제로 NVMe I/O가 잡힌 프로세스만.

        **비싸다**: 프로세스마다 페이지테이블을 걸어 cmdline을 읽으므로 실측
        60~90초. 호출 측은 자동 폴링하면 안 되고(§9.16에서 대시보드 전체를
        멈춘 원인), 구현은 캐시로 스스로를 보호해야 한다."""
        ...

    def list_targets(self) -> List[TargetRule]: ...

    def add_target(self, rule: TargetRule) -> List[TargetRule]:
        """@return: 추가 후의 전체 규칙 목록."""
        ...

    def remove_target(self, kind: str, value: str) -> List[TargetRule]:
        """@return: 제거 후의 전체 규칙 목록."""
        ...

    def get_profile(self) -> ProfileSnapshot:
        """세션/논리 그룹/기대 대조/미관측 I/O를 한 번에 담은 스냅샷."""
        ...
