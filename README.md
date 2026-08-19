# telemetryd

drgn/eBPF 기반 NVMe telemetry 서비스. 아키텍처/설계 배경은 [DESIGN.md](DESIGN.md) 참고.

## 빠른 시작 (fresh clone 기준)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[drgn,grpc,web,dev]"

# proto -> pb2 생성 (gitignore 대상이라 clone 직후 반드시 1회 실행해야 함)
./scripts/gen_proto.sh

# 단위 테스트 (mock backend — root/커널 불필요, 항상 통과해야 함)
.venv/bin/pytest -v
```

## 실기(real machine)에서 쓰기 — QEMU 없이

이 저장소의 검증은 **QEMU 게스트**에서 이뤄졌다(DESIGN.md §9). 검증 환경에서
root를 못 써서 QMP로 게스트에 붙는 우회를 썼기 때문인데, **실기에서는 그
우회가 전혀 필요 없고 오히려 방해가 된다.** 실기에서는 아래만 하면 된다.

### 1. 커널 접근 — QMP 대신 로컬 + root

```bash
# QEMU 검증 환경에서 쓰던 방식(실기에서는 불필요)
#   --qemu-qmp /path/qmp.sock --qemu-vmlinux /path/vmlinux

# 실기: 로컬 커널을 직접 읽는다. /proc/kcore 접근이라 root 필요.
sudo -E .venv/bin/telemetryd --backend drgn doctor
sudo -E .venv/bin/telemetryd --backend drgn snapshot nvme0
```

`doctor`를 먼저 돌려 심볼/권한/수집기 상태를 확인하는 걸 권한다.

**커널 디버그 심볼이 필요하다.** 배포판 패키지로 설치하거나(`debuginfod`가
설정돼 있으면 자동), 없으면 `--extra-symbols /path/to/vmlinux`로 명시한다.

> ⚠️ **이 로컬 경로(`program_from_kernel()`)는 이 저장소에서 실제로 검증되지
> 않았다.** 검증 환경에서 root를 쓸 수 없었기 때문이다(DESIGN.md 상단 경고).
> 코드 경로 자체는 QMP 모드와 대부분을 공유하지만, 실기에서 처음 돌릴 때는
> `doctor` → `snapshot` → 나머지 순으로 하나씩 확인하길 권한다.

### 2. eBPF 수집기 — chroot 없이 그냥 실행

QEMU 검증에서는 게스트에 bpftrace가 없어 9p+chroot로 호스트 바이너리를 빌려
썼다. 실기에서는 그냥 설치해서 돌리면 된다.

```bash
sudo bpftrace ebpf/nvme_perf.bt >> /var/log/nvme_perf.log 2>&1 &
sudo -E .venv/bin/telemetryd --backend drgn --ebpf-log /var/log/nvme_perf.log perf nvme0
```

필요 조건: `CONFIG_BPF_SYSCALL`, `CONFIG_BPF_EVENTS`, `CONFIG_DEBUG_INFO_BTF`,
`CONFIG_PERF_EVENTS`. 배포판 커널은 보통 다 켜져 있다.

### 3. ⚠️ IOMMU가 켜져 있으면 PRP 페이로드 덤프가 안 된다

**실기에서 가장 먼저 부딪힐 차이점이다.** IOMMU(VT-d/AMD-Vi)가 켜지면
PRP1/PRP2는 물리주소가 아니라 **IOVA**(디바이스가 보는 주소)라서, 그 값을
물리주소로 읽으면 엉뚱한 메모리를 읽는다. 검증 환경은 IOMMU가 꺼져 있어
이 조건에 한 번도 안 걸렸다.

지금 코드는 이 경우 **덤프를 하지 않고 이유를 반환한다** — 조용히 틀린 값을
주는 게 가장 나쁜 실패 모드이기 때문이다. `snapshot` 출력의 `iommu=on/off`로
현재 상태를 볼 수 있다.

- PRP 페이로드가 꼭 필요하면: 커널 부팅 옵션에서 IOMMU를 끈다
  (`intel_iommu=off` 또는 `amd_iommu=off`)
- **그 외 기능(큐 상태, CDW, 성능, 이벤트, 토폴로지, 프로파일러)은 IOMMU와
  무관하게 정상 동작한다.**

### 4. 커널 버전 차이

검증은 **Linux 6.1.4** 기준이다. 다른 버전에서는 커널 구조체 필드가 달라
일부 조회가 실패할 수 있다(이미 `cdw2/cdw3`처럼 버전별 분기를 둔 곳도 있다).
`doctor`와 `snapshot`이 먼저 깨지므로 거기서 드러난다.

### 5. QEMU 전용 스크립트는 무시해도 된다

`scripts/qemu_run_guest.sh`, `guest_fio_load.sh`, `guest_fio_profile.sh`,
`guest_start_ssh.sh`, `qemu_verify.sh` 는 전부 **검증 환경 재현용**이다.
실기에서는 쓸 일이 없다(기본 경로도 검증 세션의 임시 디렉터리를 가리킨다).
실기에서 부하가 필요하면 fio를 평소대로 직접 실행하면 된다.

## CLI (순수 라이브러리 import, gRPC 안 거침)

```bash
.venv/bin/telemetryd devices
.venv/bin/telemetryd snapshot nvme0
.venv/bin/telemetryd queue nvme0 1 --limit 8
.venv/bin/telemetryd prp nvme0 1 0
.venv/bin/telemetryd tree nvme0 ctrl pci_dev
.venv/bin/telemetryd perf nvme0                  # eBPF 큐별 IOPS/BW/레이턴시(p50~p99.9)
.venv/bin/telemetryd events nvme0                # NVMe 이벤트 목록(종류 무관 — 현재 kind=timeout)
.venv/bin/telemetryd events nvme0 --kind timeout # 특정 종류만
.venv/bin/telemetryd error-stats nvme0           # 에러 SCT/SC 조합별 누적 카운터
.venv/bin/telemetryd event-kinds                 # 등록된 이벤트 종류(무엇을 수집 중인가)
.venv/bin/telemetryd topology                    # PCIe 토폴로지 + NVMe 서브시스템 통합 트리
.venv/bin/telemetryd topology --details          # 각 노드의 속성까지
.venv/bin/telemetryd processes                   # NVMe I/O를 내는 프로세스(대상 후보)
.venv/bin/telemetryd target add --name fio       # 대상 지정(pid/name/패턴)
.venv/bin/telemetryd profile                     # 세션별 기대 vs 실측 대조

# 실제 라이브 커널 조회 (root 필요 — DESIGN.md §0)
sudo -E .venv/bin/telemetryd --backend drgn doctor
```

## gRPC 서버 + Web 대시보드

```bash
.venv/bin/python -m telemetryd.grpcserver.server --backend mock --port 50051 &
.venv/bin/uvicorn telemetryd.web.app:app --port 8000
# http://localhost:8000
```

대시보드는 디바이스를 고른 뒤 탭 4개로 나뉜다 — **Queue 정보**(스냅샷/SQ·CQ
엔트리/PRP/포인터 트리), **성능(eBPF)**(큐별 IOPS·대역폭·레이턴시 + 실시간
그래프), **이벤트**(NVMe 이벤트 목록), **프로파일러**(NVMe I/O를 내는 임의의 프로세스를
런타임에 골라 관측 — 대상 선택/세션/논리 그룹/기대 대조, DESIGN.md §9.15), **토폴로지**(PCIe 계보와 NVMe 서브시스템을
합친 통합 트리 — 호스트 브리지 → 브리지/스위치 → 엔드포인트 → 컨트롤러 →
서브시스템/네임스페이스/큐, DESIGN.md §9.14). 탭을 클릭해야 그 탭의 WebSocket이
열린다(안 보는 탭은 끊어 자원을 아낌).

현재 등록된 이벤트 종류는 두 가지다 — `timeout`(eBPF `kprobe:nvme_timeout`)과
`error`(eBPF `tracepoint:nvme:nvme_complete_rq`의 status != 0). 후자는 "이상
징후 대부분은 타임아웃 전에 에러 status로 반환되고 상위가 재시도로 흡수해
애플리케이션에선 안 보인다"는 이유로 잡는 것이고, SCT/SC/DNR로 분해해 보여주며
조합별 누적 카운터도 따로 유지한다(DESIGN.md §9.13).

이벤트 탭은 **특정 종류에 고정돼 있지 않다** — 목록 컬럼은 종류 무관
(시간/종류/qid/요약)이고, 행을 클릭하면 그 종류 전용 상세가 펼쳐진다(현재
구현된 종류는 eBPF `kprobe:nvme_timeout`의 `timeout` 하나 — tag/opcode/nsid/
flags/CDW10-15/제출~타임아웃 경과). 새 종류(리셋 등) 추가 절차는 DESIGN.md
§9.12 참고.

## C++ CPython 임베딩 예제

```bash
./scripts/build_cpp.sh mock
```

## 백엔드: mock vs drgn

- `mock` (기본값): root/실제 커널 불필요. 합성 데이터로 전체 파이프라인(CLI/gRPC/Web/C++)을 검증하는 용도.
- `drgn`: 실제 라이브 커널(`/proc/kcore`)을 조회한다. root 필요, 대상 커널의 DWARF 디버그 심볼(`nvme` 모듈 타입 해석 가능해야 함) 필요. 자세한 제약은 DESIGN.md §0 참고.

## 현재 검증 상태

- mock backend 기준 CLI/gRPC/Web/C++ 임베딩 전부 실제 실행으로 검증됨(`pytest` 25건 통과).
- `drgn` backend는 아직 실제 라이브 커널로 검증되지 못했다 — 진행 상황은 DESIGN.md §8/§9 참고.
