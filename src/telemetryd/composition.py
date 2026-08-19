"""조립 루트(composition root) — 플랫폼과 서비스를 엮어 레지스트리를 만든다.

프로그램 전체에서 **"무엇을 무엇으로 채울지" 결정하는 유일한 곳**이다. 서비스도,
서비스를 쓰는 어댑터(gRPC/REST/CLI)도 이 파일을 몰라야 한다 — 그래야 같은
서비스 코드가 배포 형태를 바꿔도 그대로 돈다.

## 두 배포 형태

    모놀리스(지금)          한 프로세스가 모든 서비스를 인스턴스로 들고 조립
    마이크로서비스(구조만)   각 서비스를 별도 프로세스로 띄우고, 호출 측에는
                            같은 계약을 구현한 원격 클라이언트를 주입

전자는 `build_monolith()`가 한다. 후자로 가는 데 필요한 건 이 파일에
`build_remote()`를 추가해 각 서비스 자리에 원격 스텁을 넣는 것뿐이고,
**서비스/어댑터 코드는 한 줄도 안 바뀐다**(Strategy). 지금은 요구대로
구조만 갖추고 모놀리스로 구동한다.

## 서비스별 의존을 여기서 보면 배포 단위가 그대로 보인다

    queues     platform.kernel                 (drgn 필요, 수집기 불필요)
    topology   platform.kernel                 (drgn 필요, 수집기 불필요)
    perf       platform.ebpf                   (수집기 필요, drgn 불필요)
    events     platform.ebpf                   (수집기 필요, drgn 불필요)
    profiler   platform.kernel + platform.ebpf (둘 다 필요)

perf/events는 drgn/QMP 접근 권한이 아예 없는 프로세스로 뗄 수 있고, 반대로
queues/topology는 수집기가 안 떠 있어도 완전히 동작한다 — 계층 분리의 실질적
이득이 이 표에 그대로 드러난다.
"""
from __future__ import annotations

from typing import List, Optional

from telemetryd.platform.ebpf import EbpfLogSource, as_log_source
from telemetryd.platform.kernel import DrgnKernelSession, KernelSession
from telemetryd.services import events as events_pkg
from telemetryd.services import perf as perf_pkg
from telemetryd.services import profiler as profiler_pkg
from telemetryd.services import queues as queues_pkg
from telemetryd.services import topology as topology_pkg
from telemetryd.services.registry import ServiceRegistry


def build_monolith_drgn(
    program=None,
    qemu_qmp_address: Optional[str] = None,
    qemu_vmlinux: Optional[str] = None,
    extra_symbols: Optional[List[str]] = None,
    ebpf_log_path: Optional[str] = None,
    target_state: Optional[str] = None,
) -> ServiceRegistry:
    """실제 커널(drgn) + eBPF 수집기로 전 서비스를 인프로세스 조립한다.

    플랫폼 인스턴스(커널 세션/로그 소스)를 **하나씩만** 만들어 서비스들이
    공유하는 게 핵심이다. 특히 커널 세션의 캐시를 공유해야 비싼 조회가 한 번만
    돌고 다른 서비스가 그 결과를 재사용한다 — 서비스마다 세션을 따로 만들면
    §9.16에서 겪은 "캐시를 우회해 워커를 점유하는" 문제가 그대로 재현된다."""
    kernel: KernelSession = DrgnKernelSession(
        program=program,
        qemu_qmp_address=qemu_qmp_address,
        qemu_vmlinux=qemu_vmlinux,
        extra_symbols=extra_symbols,
    )
    log: EbpfLogSource = as_log_source(ebpf_log_path)

    queue_svc = queues_pkg.DrgnQueueService(kernel)
    return (
        ServiceRegistry()
        .register(queues_pkg.SERVICE_NAME, queue_svc, queues_pkg.QueueService)
        .register(perf_pkg.SERVICE_NAME, perf_pkg.EbpfPerfService(log), perf_pkg.PerfService)
        .register(events_pkg.SERVICE_NAME, events_pkg.EbpfEventService(log), events_pkg.EventService)
        .register(
            topology_pkg.SERVICE_NAME,
            # [한국어] 토폴로지는 장치 조회가 필요하지만 큐 서비스를 직접
            # import하지 않는다(서비스 간 직접 의존 금지) — 필요한 두 동작만
            # 콜러블로 넘긴다. 나중에 큐 서비스가 원격으로 빠져도 그대로다.
            topology_pkg.DrgnTopologyService(
                list_devices=queue_svc.list_devices,
                lookup_device=queue_svc.lookup_device,
            ),
            topology_pkg.TopologyService,
        )
        .register(
            profiler_pkg.SERVICE_NAME,
            profiler_pkg.NvmeProfilerService(kernel=kernel, log_source=log,
                                             target_state=target_state),
            profiler_pkg.ProfilerService,
        )
    )


def build_monolith_mock(target_state: Optional[str] = None) -> ServiceRegistry:
    """커널도 수집기도 없이 도는 조립 — UI/CLI 개발과 테스트용.

    큐 서비스는 아직 mock 구현이 없어(MockBackend 안에 남아 있다) 등록하지
    않는다. 레지스트리가 "없는 서비스"를 명확한 에러로 알려주므로, 이 구성에서
    큐를 부르면 무엇이 빠졌는지 바로 드러난다."""
    return (
        ServiceRegistry()
        .register(perf_pkg.SERVICE_NAME, perf_pkg.MockPerfService(), perf_pkg.PerfService)
        .register(events_pkg.SERVICE_NAME, events_pkg.MockEventService(), events_pkg.EventService)
        .register(
            profiler_pkg.SERVICE_NAME,
            profiler_pkg.MockProfilerService(target_state),
            profiler_pkg.ProfilerService,
        )
    )
