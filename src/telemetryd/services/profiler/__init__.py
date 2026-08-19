"""프로파일러 서비스 — NVMe I/O를 내는 프로세스의 범용 프로파일러(§9.15).

의존: platform.kernel(프로세스 정보) + platform.ebpf(I/O 활동) — **두 축 모두**
필요한 유일한 서비스다.
"""
from telemetryd.services.profiler.contract import SERVICE_NAME, ProfilerService
from telemetryd.services.profiler.mock import MockProfilerService
from telemetryd.services.profiler.service import NvmeProfilerService

__all__ = ["SERVICE_NAME", "ProfilerService", "NvmeProfilerService", "MockProfilerService"]
