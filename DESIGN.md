# telemetryd — 설계 문서

drgn과 eBPF로 리눅스 NVMe 스택(SQ/CQ, blk-mq inflight, PRP 페이로드, `struct
nvme_dev` 전체 트리)을 실시간 관찰하는 telemetry 서비스. 순수 Python
라이브러리로 시작해 C++에서 CPython 임베딩으로 재사용하고, gRPC로
client-server 확장, CLI/Web 두 종류 클라이언트로 노출한다.

## 0. 배경 자산 (재사용)

이 프로젝트는 맨땅에서 시작하지 않는다. `deep/scripts/drgn/00~04`에 이미
검증된 drgn 탐색 로직이 있고, telemetryd의 `backend/drgn_backend.py`는 이
로직을 라이브러리화한 것이다.

| 스크립트 | 재사용 내용 |
|---|---|
| `00_env_check.py` | 헬스체크 패턴 (`init_uts_ns`, `struct nvme_dev` 타입 해석 여부) → `telemetryd doctor` 커맨드 |
| `01_blkmq_inflight.py` | `request_queue_busy_iter(q, "driver"/"sched")`로 hctx별 inflight 카운트 |
| `02_nvme_queues.py` | `gendisk → nvme_ns → nvme_ctrl → container_of(nvme_dev) → dev.queues[qid]` 경로, SQ/CQ 필드 |
| `04_prp_payload.py` | `nvme_iod`(`blk_mq_rq_to_pdu`) → PRP1/PRP2 디코딩 → `prog.read(phys, n, physical=True)` |

**중요한 환경 제약 (계승):**
- drgn은 DWARF만 쓰고 BTF는 안 쓴다. 커널이 Fedora가 아니면 debuginfod 자동
  다운로드가 꺼져 있어 `linux-image-$(uname -r)-dbgsym` 수동 설치가 필요하다.
- `struct nvme_iod`의 레이아웃은 커널 버전에 따라 다르다(6.15 전후 DMA API
  재작업). 6.17+ 는 `iod.cmd/descriptors[]/flags/total_len`, 구버전은
  `iod.first_dma/sgt/use_sgl/list[]`. `drgn_backend.py`는 두 레이아웃을 모두
  시도하고 실패하면 명확한 에러를 낸다(§5.4).
- `/proc/kcore` 라이브 접근은 root가 필요하고, 이 머신의 sudo는 비밀번호를
  요구한다 → **에이전트(Claude)가 직접 실전 drgn 세션을 실행할 수 없다.**
  그래서 이 프로젝트는 처음부터 **MockBackend**를 1급 시민으로 설계해서,
  root 없이도 CLI/gRPC/Web 전체 파이프라인을 개발·테스트할 수 있게 한다.
  실제 라이브 커널 검증은 사용자가 `sudo -E`로 실행해서 확인한다(§8).
- IOMMU 비활성 환경(이 머신)에서는 PRP = 물리주소라 `physical=True` 직접
  읽기가 된다. IOMMU 활성 환경에서는 PRP가 IOVA라서 이 방식이 안 통한다 —
  `drgn_backend`는 `/sys/class/iommu/`를 확인해 이 사실을 스냅샷에 플래그로
  포함한다.

## 1. 목표와 범위

1. `struct nvme_dev`에 연결된 큐 정보 — 큐 번호별 SQ tail / CQ head, 그리고
   매핑된 blk-mq hctx의 inflight-cmd 개수를 실시간으로 파악한다.
2. 특정 큐를 선택하면 그 큐의 SQ 엔트리들의 CDW(Command Dword) 필드를 모두
   보여준다.
3. "PRP 확인"을 누르면 해당 커맨드가 가리키는 데이터 페이지를 4KB 단위로
   hexdump해서 보여준다.
4. `nvme0`/`nvme1` 같은 컨트롤러 버튼을 누르면 그 컨트롤러의
   `struct nvme_dev` 전체를 포인터를 타고 들어가며 tree로 탐색한다
   (최대 depth 10).
5. 배포 형태 3단: (a) 순수 Python 라이브러리, (b) C++에서 쓰는 CPython
   임베딩 바인딩, (c) 위 라이브러리를 감싼 gRPC 서버 — 이 순서로 우선순위가
   있다((a)(b)가 1차, (c)가 확장).
6. 클라이언트 예제 2종: 라이브러리를 직접 import하는 CLI, 그리고 gRPC 호출로
   실시간/비실시간 갱신하는 Web 대시보드.

## 2. 전체 아키텍처

```
                         ┌─────────────────────────────┐
                         │   Linux Kernel (host, live)  │
                         │  struct nvme_dev, blk-mq,    │
                         │  request_queue, sq/cq rings  │
                         └───────────────┬───────────────┘
                                          │ /proc/kcore (root 필요)
                                          │ DWARF 타입 정보
                       ┌──────────────────▼───────────────────┐
                       │   telemetryd (순수 Python 라이브러리)   │
                       │  ─────────────────────────────────── │
                       │  backend/                             │
                       │    base.py        Backend 프로토콜    │
                       │    drgn_backend.py  실제 drgn 조회     │
                       │    mock_backend.py  합성 데이터(테스트) │
                       │  treewalk.py       포인터 트리 lazy walk│
                       │  models.py         dataclass 스냅샷    │
                       │  ebpf/ (Phase 3)   실시간 카운터 보강   │
                       └───┬───────────────┬───────────────┬───┘
                           │               │               │
              직접 import   │   CPython 임베딩│    grpc.aio 서버 │
                           │   (pybind11    │   (server.py)   │
                           │    embed.h)    │               │
                  ┌────────▼───┐   ┌───────▼──────┐  ┌──────▼─────────┐
                  │  CLI (click)│   │  C++ 프로그램  │  │ gRPC service    │
                  │  telemetryd │   │  cpp/examples │  │ (:50051)        │
                  │  devices/   │   │  embed_example│  └──────┬─────────┘
                  │  queue/prp/ │   └───────────────┘         │ grpc client
                  │  tree       │                             │
                  └─────────────┘                    ┌────────▼─────────┐
                                                       │ Web (FastAPI)     │
                                                       │ REST + WebSocket  │
                                                       │ static/index.html │
                                                       │ (브라우저 대시보드) │
                                                       └───────────────────┘
```

**계층 원칙:** 모든 클라이언트(CLI/C++/gRPC서버)는 `telemetryd.backend.Backend`
프로토콜 하나만 바라본다. drgn 유무·root 유무와 무관하게 동일한 데이터모델
(`models.py`)을 주고받으므로, Web/CLI 코드는 백엔드가 mock이든 real이든
수정 없이 동작한다. gRPC는 이 라이브러리를 "감싸는" 얇은 계층일 뿐 별도
로직을 갖지 않는다(요청사항 3의 "wrapper" 그대로).

## 3. 왜 웹은 gRPC-Web/Envoy를 안 쓰는가 (설계 가정 — 확인 필요)

브라우저는 HTTP/2 트레일러를 못 써서 순수 gRPC를 직접 못 부른다. 정공법은
grpc-web + Envoy 프록시지만, 이 프로젝트 규모에 비해 별도 프록시 인프라를
넣는 건 과하다고 판단했다. 대신:

- **FastAPI 웹 서버 자신이 gRPC *클라이언트***가 되어 telemetryd gRPC
  서버(:50051)를 호출하고, 브라우저에는 REST(JSON) + WebSocket으로 재노출한다.
- 요청사항 3(4)의 "web 형태로 grpc 호출을 통해"는 이 경로로 충족된다 —
  gRPC 호출은 실제로 일어나지만 브라우저가 아니라 웹 서버 프로세스가 한다.
- 브라우저가 grpc-web으로 *직접* 호출해야 한다는 뜻이었다면 알려달라 —
  Envoy 컨테이너 + grpc-web JS stub으로 바꾸는 건 이 구조 위에 추가 레이어만
  얹으면 되므로 나중에 바꿔도 손실이 적다.

## 4. 왜 "CPython 바인딩"을 임베딩으로 해석했는가 (가정 — 확인 필요)

"c++ program에서 사용할 수 있는 형태로 cpython binding 형태로 제공"을
**pybind11의 `embed.h`로 C++ 프로세스 안에 CPython 인터프리터를 띄우고
telemetryd 파이썬 모듈을 import해서 호출하는 것**으로 해석했다(pybind11은
보통 반대 방향 — C++을 Python에 노출 — 로 쓰이지만 `embed.h`는 정확히 이
반대 시나리오를 위한 공식 서브모듈이다). C++ ↔ Python 사이 데이터는 얇은
`telemetryd.ffi` 모듈이 JSON 문자열로 직렬화해서 넘긴다(중첩 dataclass를
pybind11 type caster로 일일이 매핑하는 대신, C++ 쪽에서 원하는 JSON
라이브러리로 자유롭게 파싱하게 함). 만약 원하신 것이 "C++로 짠 코어를
pybind11로 Python에 노출"하는 반대 방향이었다면 구조를 뒤집어야 한다 —
지금 라이브러리 코어가 이미 Python(drgn 자체가 Python 라이브러리라 C++로
옮기면 이점이 없음)이라 임베딩 해석이 더 자연스럽다고 보고 이대로 진행한다.

## 5. 데이터 모델 (`models.py`)

### 5.1 NvmeDeviceSnapshot — 컨트롤러 1개 스냅샷 (요청사항 6의 "버튼" 대상)

| 필드 | 출처 | 의미 |
|---|---|---|
| `name` | 사용자 지정 키 (`nvme0`, `nvme1`) | 디스크/컨트롤러 식별자 |
| `addr` | `dev.value_()` | `struct nvme_dev*` 커널 가상주소 |
| `model` | `ctrl.model_number` | 모델 문자열 |
| `online_queues` | `dev.online_queues` | Admin 포함 살아있는 큐 수 |
| `allocated_queues` | `dev.nr_allocated_queues` | 할당된 큐 배열 크기 |
| `bar_addr` / `dbs_addr` | `dev.bar` / `dev.dbs` | MMIO 주소(값만 — 내용은 못 읽음, §0) |
| `iommu_enabled` | `/sys/class/iommu/` 존재 여부 | PRP가 물리주소인지 IOVA인지 판단용 |
| `queues` | `QueueSnapshot[]` | 아래 |

### 5.2 QueueSnapshot — 요청사항 1

| 필드 | 출처 |
|---|---|
| `index` / `qid` | `dev.queues`의 배열 인덱스 / `nvmeq.qid`(0=Admin) |
| `depth` | `nvmeq.q_depth` |
| `sq_tail` | `nvmeq.sq_tail` |
| `cq_head` | `nvmeq.cq_head` |
| `sq_dma_addr` / `cq_dma_addr` | `nvmeq.sq_dma_addr` / `cq_dma_addr` |
| `hctx_index` | `qid - 1` (Admin은 hctx 없음 → `None`) |
| `inflight_driver` | `request_queue_busy_iter(q,"driver")`를 hctx로 그룹핑한 카운트 |
| `inflight_sched` | 동일, `"sched"` 태그 공간 |

### 5.3 QueueEntry — 요청사항 2 (큐 클릭 → CDW 전체)

SQ 링의 `nvme_command`에서 `opcode, cid, nsid, flags(PSDT 포함), nsid,
cdw2, cdw3, cdw10~cdw15, dptr.prp1, dptr.prp2`를 전부 담는다 — "cdw 필드값을
모두" 요구를 그대로 반영. `common` 유니온이라 read/write/admin 커맨드 모두
동일 오프셋에서 cdw10~15를 뽑아 opcode별 의미(NLB, SLBA 등)는 부가 해석으로
opcode 테이블에 얹는다(`02_nvme_queues.py`의 `NVM_OPC`/`ADM_OPC` 재사용).

### 5.4 PrpPage — 요청사항 3 ("PRP 확인" → 4KB 페이로드)

`04_prp_payload.py`의 `analyze_prp()`를 그대로 라이브러리화. 입력은
`(device, qid, cid)` — SQ 엔트리의 opcode/PSDT만으로는 총 전송 길이를 몰라
`blk_mq_rq_to_pdu`로 짝이 되는 `struct request`/`nvme_iod`를 찾아
`iod.total_len`을 얻는다(요청이 이미 완료됐으면 못 찾음 → 에러로 보고).
반환은 `PrpPage[]`: 각 페이지 `{phys_addr, offset_in_page, data: bytes(≤4096),
is_list_page}`. 기존 스크립트는 32바이트만 hexdump했지만 요구사항대로 페이지당
최대 4096바이트 전체를 읽어 반환한다(표시 시 CLI/Web에서 hex+ascii 렌더링).

### 5.5 TreeNode — 요청사항 4 (포인터 타고 tree, depth 10)

Eager 직렬화는 안 한다 — `struct nvme_dev`는 `pci_dev`, `device`, `kobject`
sysfs 트리까지 물고 있어 전체를 한 번에 펼치면 수만 노드가 나온다. 대신
**lazy 1-depth expansion**: 클라이언트가 `path`(필드명/인덱스 시퀀스, 예:
`["ctrl", "pci_dev", "dev", "kobj"]`)를 보내면 서버는 그 노드 하나와 "바로
다음 자식들의 요약(이름/타입/축약값/expandable 여부)"만 돌려준다. `len(path)
> 10`이면 서버가 명시적으로 거부(요구사항의 "최대 10 depth"를 서버측에서
강제). 순환 참조는 트리에서 자연스럽게 처리된다 — 같은 주소를 다시 눌러도
그냥 한 단계 더 펼쳐질 뿐 무한루프가 아니다(사용자가 직접 누르는 pull 모델이라
자동 재귀 폭발이 없음). 스칼라/문자열은 값을 바로 보여주고, MMIO/미매핑
포인터는 `FaultError`를 잡아 "읽기 불가(MMIO)"로 표시한다.

## 6. eBPF의 역할 — 큐별 IOPS/대역폭/레이턴시 (구현 완료)

drgn은 스냅샷 조회에는 강하지만(임의 포인터를 자유롭게 따라감), "초당 몇
번 제출/완료됐나" 같은 **레이트** 지표에는 안 맞는다(폴링 시점의 sq_tail/
cq_head 차이로 근사만 가능). 그래서 eBPF로 실제 제출/완료 이벤트를 커널
안에서 직접 카운팅한다.

- **트레이스포인트**: `tracepoint:nvme:nvme_setup_cmd`(제출 — qid/cid/opcode/
  cdw10 캡처), `tracepoint:nvme:nvme_complete_rq`(완료 — qid/cid). 둘 다
  `drivers/nvme/host/trace.h`에 이미 정의돼 있어 커널에 `CONFIG_BPF_SYSCALL`
  (+`BPF_EVENTS`/`PERF_EVENTS`/`DEBUG_INFO_BTF`)만 켜면 바로 쓸 수 있다.
- **집계**: `ebpf/nvme_perf.bt`(bpftrace)가 (ctrl_id, qid)별로 op_count/
  read_count/write_count/bytes_sum(제출 시 cdw12 NLB에서 유도)/lat_sum(제출~
  완료 타임스탬프 차)/lat_count 를 BPF map에 쌓고, `interval:s:1`로 1초마다
  `print(@map)`(bpftrace 네이티브 덤프) 후 `clear()` — JSON을 bpftrace 언어로
  손수 만드는 대신 호스트 쪽 Python(`backend/ebpf_perf.py`)이 정규식으로
  파싱한다(훨씬 덜 취약).
- **분업**: eBPF = 저오버헤드 레이트 카운터(이 섹션), drgn = 온디맨드 구조체
  스냅샷(§5.1, §5.3~5.5, 클릭 시에만 발생하는 무거운 조회) — 처음 설계했던
  분업 그대로 구현됐다.
- **실행 환경**: bpftrace 프로세스 자체는 게스트 커널에 붙어야 하므로 §9의
  9p+chroot 트릭으로 게스트 안에서 실행하고(호스트에 설치된 bpftrace를
  빌려씀), 출력은 **쓰기 가능한 별도 9p 공유**로 호스트 파일에 쌓는다 — 상세
  레시피와 실측 데이터는 §9.5/§9.6.

## 7. 디렉토리 구조

```
telemetryd/
├── DESIGN.md
├── pyproject.toml
├── proto/telemetryd.proto
├── src/telemetryd/
│   ├── models.py
│   ├── ffi.py                 # C++ 임베딩용 JSON 계층
│   ├── backend/
│   │   ├── base.py  drgn_backend.py  mock_backend.py
│   ├── treewalk.py
│   ├── grpcserver/            # 생성 pb2 + server.py + convert.py
│   ├── cli/main.py
│   └── web/app.py + static/index.html
├── cpp/CMakeLists.txt, examples/embed_example.cpp
└── tests/
```

## 8. 검증 계획 (요청사항 7)

| 항목 | 방법 | 누가 실행 |
|---|---|---|
| pure lib + mock backend | `pytest tests/` | Claude가 직접 실행·확인 |
| CLI (library-direct) | `telemetryd --backend mock devices/snapshot/queue/prp/tree` 출력이 mock 데이터와 일치하는지 assert | Claude가 직접 실행·확인 |
| gRPC 서버 + Web 대시보드 | mock backend로 grpc 서버 기동 → FastAPI 기동 → curl/websocket으로 REST/실시간 갱신 확인 | Claude가 직접 실행·확인 |
| C++ CPython 임베딩 | cmake 빌드 → 바이너리 실행 → JSON 출력 확인 | Claude가 직접 실행·확인 |
| **실제 라이브 커널 drgn 조회** | `telemetryd --backend drgn --qemu-qmp <sock> --qemu-vmlinux <path> devices/snapshot/queue/tree/prp/doctor` | Claude가 QEMU 게스트로 직접 실행·확인 (§9) — 호스트 자체 라이브(`sudo -E ... --backend drgn`)는 호스트 sudo 비밀번호를 몰라 여전히 사용자 확인 필요 |

이번 세션 결과 보고 시 위 표의 "Claude가 직접 실행" 항목은 실제 실행 로그로
증명하고, 호스트 라이브 항목은 정확한 실행 커맨드를 안내하는 것으로 마무리한다.

## 9. 실제 라이브 커널 검증 — QEMU 게스트에 root 없이 붙기

호스트의 sudo가 비밀번호를 요구해(§0) `DrgnBackend`를 호스트 라이브 커널로
검증할 수 없었다. 대신 **QEMU 게스트에 QMP로 라이브 접속하는 방법을 찾아
실제 커널로 전체 백엔드를 검증했다** — root가 전혀 필요 없다.

### 9.1 핵심 발견: `drgn --qemu`와 그 요구조건

drgn은 `--qemu ADDRESS`(CLI) / `Program.set_qemu_qmp(address)`(Python API)로
QEMU Machine Protocol(QMP)을 통해 게스트에 라이브로 붙을 수 있다. 내부적으로
`dump-guest-memory`를 QMP로 실행해 VMCOREINFO 노트를 얻는데(그래야 사용자가
준 vmlinux를 "이게 진짜 지금 이 게스트의 커널이다"라고 검증할 수 있음),
이 과정이 **SCM_RIGHTS로 fd를 넘겨받는 방식**이라 **QMP가 유닉스 도메인
소켓이어야만 동작한다(TCP QMP는 안 됨)** — `libdrgn/qemu_machine_protocol.c`
`qmp_read_vmcoreinfo()` 참고. 처음 TCP로 시도했을 때 `-s vmlinux`를 줘도
"did not match any loaded modules; ignoring"로 계속 무시된 이유가 이거였다.

요구조건 (`libdrgn/program.c`의 에러 메시지 그대로):
1. QEMU를 **`-device vmcoreinfo`** 로 띄운다.
2. 게스트 커널이 **`CONFIG_FW_CFG_SYSFS=y`**, **`CONFIG_KEXEC=y`** 로 빌드돼
   있어야 한다(이 프로젝트가 참고한 qemu-debug/linux-6.1.4 커널은 KEXEC은
   이미 켜져 있었지만 FW_CFG_SYSFS는 꺼져 있어서 `./scripts/config --enable
   FW_CFG_SYSFS && make olddefconfig && make bzImage` 로 다시 빌드해야 했다).
3. QMP는 **유닉스 소켓**으로 연다: `-qmp unix:/path/to/qmp.sock,server,nowait`.
4. `-s vmlinux`로 준 파일은 **게스트가 실제로 부팅한 빌드와 정확히 같아야**
   한다(build-id 매칭). 커널 소스 트리에서 vmlinux와 bzImage를 따로 빌드해
   보관해두면(예: 버그 재현/수정 버전을 bzImage-*.bak으로만 백업하고 vmlinux는
   덮어씀) 둘이 어긋나기 쉽다 — `make bzImage` 한 번으로 둘 다 같이 만들어야
   짝이 보장된다.

### 9.2 재현 커맨드

```bash
# 1) 게스트 (예: qemu-debug 프로젝트의 커스텀 NVMe 연구 커널)
qemu-system-x86_64 -accel tcg -smp 4 -m 1536 \
  -kernel linux-6.1.4/arch/x86/boot/bzImage -initrd irfs.cpio.gz \
  -append "console=ttyS0 nokaslr no_hash_pointers panic=-1" \
  -drive file=m00.img,if=none,id=d00,format=raw \
  -device nvme-subsys,id=subsys0,nqn=testsubsys \
  -device nvme,id=nvme0,serial=dev0,subsys=subsys0,max_ioqpairs=8 \
  -device nvme-ns,drive=d00,bus=nvme0,nsid=1,nvmsetid=1 \
  -device vmcoreinfo \
  -nographic -no-reboot \
  -qmp unix:/tmp/qmp.sock,server,nowait

# 2) 호스트 (telemetryd venv, root 불필요)
telemetryd --backend drgn --qemu-qmp /tmp/qmp.sock \
  --qemu-vmlinux linux-6.1.4/vmlinux devices
```

`DrgnBackend(qemu_qmp_address=..., qemu_vmlinux=...)`가 내부적으로
`drgn.Program(); prog.set_qemu_qmp(addr); prog.load_debug_info([vmlinux],
default=True, main=True)` 를 한다(`backend/drgn_backend.py`).

### 9.3 이 검증으로 실제로 잡은 버그 2개

mock backend만으로는 절대 못 잡는, 실제 커널 구조체를 걸었을 때만 드러나는
문제였다 — `treewalk.py`에서 수정:

1. **struct/enum/array-of-struct의 `type_name`이 필드 전체를 나열한 멀티라인
   정의 본문으로 나옴** (drgn의 `str(t)`가 원래 그렇게 동작 — `struct nvme_dev`
   노드의 type_name이 수십 줄짜리 텍스트였음). `_short_type_name()`을 추가해
   태그 이름만 뽑도록 수정(`struct nvme_dev`, `struct nvme_id_power_state [32]`,
   `struct <anonymous>` 등).
2. **enum 값이 정수로만 나옴** (`nvmset_state=0`). `describe()`에 enum 전용
   분기를 추가해 `str(obj)`(`"(enum nvmset_state)NVMSET_STATE_D"`)에서
   열거자 이름만 뽑아 `NVMSET_STATE_D`로 표시하도록 수정.
3. **`telemetryd doctor` 커맨드가 `--qemu-qmp`를 무시**하고 항상 호스트
   `program_from_kernel()`을 시도하는 버그도 발견해 고쳤다 —
   `doctor(backend: DrgnBackend | None)`가 이제 CLI가 구성한 backend의 연결
   방식을 그대로 재사용한다.

### 9.4 이 세션에서 실제로 실행해 확인한 것

`telemetryd --backend drgn --qemu-qmp ... --qemu-vmlinux ...`로 실제 CLI/gRPC/
웹 전체 경로를 실제 커널로 검증했다: `devices`/`snapshot`(멀티 큐, 실시간
inflight), `queue`/`cq`(도어벨 기준 최근 16개, SQ+CQ 둘 다), `tree`(다단계
포인터 탐색, 커스텀 커널 고유 필드까지), `prp`(실제 read/write 데이터가 담긴
PRP 페이로드 hexdump 확인 완료 — §9.5 참고), `doctor`(헬스체크 OK). 컨트롤러
2개(`nvme0`+`nvme1`) 동시 연결도 검증해 디바이스 간 데이터 crosstalk이 없음을
확인했다(서로 다른 `struct nvme_dev` 주소, 독립된 cid 시퀀스).

### 9.5 실제 I/O 부하로 PRP 채우기 (fio, 9p+chroot, SGL/PRP 경로 강제)

게스트 initramfs(busybox)엔 fio가 없다. 호스트에 `apt-get download fio`로
받은 뒤(설치는 안 함, `.deb`만 `dpkg-deb -x`로 풀어서 씀) §9의 9p 마운트를
그대로 이용해 게스트 안에서 chroot로 실행한다 — drgn/python3를 게스트에서
쓴 것과 완전히 같은 트릭:

```bash
# 게스트 안에서 (호스트 rootfs가 9p로 /mnt/host에 마운트돼 있다고 가정, §9 참고)
mount --bind /dev /mnt/host/dev      # chroot 안에서 /dev/nvme0n1이 보이게

# fio가 필요로 하는 라이브러리 중 libgfapi.so.0/libnbd.so.0(glusterfs/nbd
# 엔진용, 실제로는 안 씀)는 우분투에 패키지가 없어 못 받는다 — 실제 호출 안
# 될 심볼만 채운 버전스크립트 스텁 .so를 직접 만들어 LD_LIBRARY_PATH로 우회.
LD_LIBRARY_PATH=<fio_extract>/usr/lib/x86_64-linux-gnu \
  chroot /mnt/host <fio_extract>/usr/bin/fio \
  --name=load --filename=/dev/nvme0n1 --rw=randrw \
  --bs=16k --iodepth=16 --numjobs=4 --ioengine=libaio --direct=1 \
  --runtime=60 --time_based --group_reporting
```

**SGL vs PRP 경로**: NVMe 드라이버는 `avg_seg_size >= sgl_threshold`(기본
32768, `drivers/nvme/host/pci.c` `nvme_pci_use_sgls()`)면 SGL을, 아니면 PRP를
쓴다. `bs`를 threshold보다 작게 줘도(예: 16k) 세그먼트 크기에 따라 SGL이 섞여
나올 수 있다 — **PRP만 강제하려면 threshold를 0으로 낮춘다**:

```bash
echo 0 > /sys/module/nvme/parameters/sgl_threshold
# nvme_pci_use_sgls()의 `if (!sgl_threshold || ...) return false;` 에 걸려
# 이후 모든 요청이 무조건 PRP 경로를 탄다. 반대로 SGL만 보고 싶으면 1로.
```

이렇게 하면 활성 큐의 SQ 엔트리가 전부 `uses_sgl=false`가 되고, 실제
read/write 데이터가 담긴 4KB PRP 페이로드를 `telemetryd prp`/웹의 "PRP 확인"
으로 그대로 확인할 수 있다(실측: `total_len=16384`인 16KB 전송에서 첫 4096
바이트가 정확히 캡되어 반환됨 — §"prp payload 최대 4k" 대응).

### 9.6 eBPF로 큐별 IOPS/대역폭/레이턴시 (bpftrace, §6의 실제 구현)

**커널 준비**: 이 프로젝트가 참고한 qemu-debug/linux-6.1.4는 기본적으로
`CONFIG_BPF_SYSCALL`이 꺼져 있었다(`bpf()` 시스템콜 자체가 없어 bpftrace가
"ENOSYS"류로 즉시 실패). 아래를 켜고 재빌드해야 한다:

```bash
./scripts/config --enable BPF_SYSCALL --enable BPF_EVENTS \
  --enable BPF_JIT --enable PERF_EVENTS --enable DEBUG_INFO_BTF
make olddefconfig && make -j4 bzImage
```

`DEBUG_INFO_BTF`는 `pahole`(dwarves 패키지)이 PATH에 있어야 켜진다 — 없으면
`make olddefconfig`가 조용히 다시 꺼버린다. BTF 없이도 bpftrace는 뜨지만,
`nvme:nvme_setup_cmd`처럼 `u8`/`u16`/`bool` 같은 커널 typedef를 쓰는
트레이스포인트는 `unknown type name 'u8'` 로 죽는다 — 커널 헤더만으로는
이 typedef들을 못 풀어서다. `pahole`도 apt 패키지가 없으면
`apt-get download pahole libbpf1 libdw1t64`(호스트에 이미 있는 의존성은
자동 스킵됨)로 받아 `PATH`에 추가하면 된다.

**쓰기 가능한 9p 공유 추가**: §9의 `hostroot`(읽기전용) 말고, bpftrace
출력을 호스트로 빼낼 **별도의 쓰기 가능** 9p 공유가 하나 더 필요하다
(hostroot를 통째로 쓰기 가능하게 열면 게스트가 호스트 전체에 쓸 수 있게 돼
과함 — 스크래치 디렉터리 하나만 좁게 연다):

```bash
qemu-system-x86_64 ... \
  -fsdev local,id=fs1,path=/host/scratch/ebpf_out,security_model=none \
  -device virtio-9p-pci,fsdev=fs1,mount_tag=ebpfout
# 게스트 안: mount -t 9p -o trans=virtio,version=9p2000.L ebpfout /mnt/ebpf
```

**수집기 실행** (게스트 안, §9의 9p+chroot로 호스트 bpftrace를 게스트
커널에 붙임 — debugfs도 마운트해야 트레이스포인트 포맷을 읽는다):

```bash
mount -t debugfs none /mnt/host/sys/kernel/debug
chroot /mnt/host /usr/bin/bpftrace /path/to/ebpf/nvme_perf.bt \
  >> /mnt/ebpf/nvme_perf.log 2>&1 &
```

**telemetryd 연결**: `DrgnBackend(ebpf_log_path=...)` / CLI
`--ebpf-log PATH` / gRPC 서버 `--ebpf-log PATH` 로 이 로그 파일 경로를
알려주면 `get_performance()`/`GetPerformance`/`StreamPerformance`가 최신
완료 틱을 읽어 돌려준다.

**실측** (fio `bs=16k, numjobs=4` 부하 중, `telemetryd perf nvme0`):
```
 qid      iops    read/s   write/s   BW(MB/s)   lat(us)
   3      4902      2638      2533      84.93     735.4
   5      3776      2127      2029      68.22     737.7
   6      4664      2474      2487      81.56     739.4
   7      4972      2586      2560      84.56     731.2
```
웹 대시보드의 `/ws/devices/{device}/performance` WebSocket으로도 1초마다
갱신되는 걸 실시간으로 확인했다(활성 큐 집합 자체가 매 초 바뀜 — fio
스레드가 CPU/hctx를 옮겨다녀서).

### 9.7 게스트에 직접 SSH로 접속하기 (사용자가 fio를 직접 실행/관찰하고 싶을 때)

**왜 필요했나**: 지금까지는 fio/bpftrace를 이 세션이 tmux serial console로
대신 실행해줬는데, 사용자가 직접 게스트에 들어가서 fio를 돌려보고 싶다고
요청함. 원래 이 프로젝트의 커스텀 qemu-8.2.2 빌드는 네트워크 백엔드가
아예 없었다(`-netdev user` 시도 시 "network backend 'user' is not compiled
into this binary" — configure 로그 확인 결과 `slirp: NO`). `tap`은
CAP_NET_ADMIN이 없어 "/dev/net/tun 설정 불가"로 실패. 그래서 libslirp를
넣어 QEMU를 재빌드했다.

**QEMU 재빌드 (slirp 포함)**:
```bash
# libslirp-dev는 apt-get download로만 받고(설치 권한 없음), pkg-config가
# 그걸 찾게 slirp.pc의 prefix=를 추출 경로로 고쳐야 하지만, 실제로는
# meson이 -Dslirp=enabled를 강제하면 vendored subproject를 대신 골라서
# 이 수동 준비가 최종 빌드에는 안 쓰였다(그래도 pkg-config 경로가 있으면
# meson이 우선 시도하므로 준비 자체는 무해).
meson setup --reconfigure build -Dslirp=enabled   # 'auto'는 최초 configure때
                                                    # "not found"로 캐시돼서 재사용됨
ninja -C build qemu-system-x86_64
```

**네트워크 기동** (`scripts/qemu_run_guest.sh`에 반영됨):
```
-netdev user,id=net0,hostfwd=tcp::2222-:22 -device virtio-net-pci,netdev=net0
```
게스트 안에서: `ip link set eth0 up && ip addr add 10.0.2.15/24 dev eth0 && ip route add default via 10.0.2.2`
(slirp 기본 규약: 게스트 IP 10.0.2.15/24, 게이트웨이/DNS 프록시 10.0.2.2)

**SSH 서버 (dropbear)**: OpenSSH 대신 의존성이 적은 dropbear를
`apt-get download`로 받아 §9의 9p+chroot로 게스트 커널 위에서 실행한다.

- 호스트의 진짜 `/etc/passwd`/`/etc/shadow`는 절대 안 건드린다(hostroot는
  읽기전용으로 마운트돼 있어 애초에 못 쓰지만, 쓸 수 있어도 호스트 계정
  파일을 고치는 건 부적절함). 대신 쓰기 가능한 `ebpfout` 공유에 root만
  들어있는 최소 `passwd`/`shadow`(빈 비밀번호)/`group` 파일을 만들어
  `mount --bind`로 chroot의 `/etc/passwd` 등 **위에** 덮어씌운다 — 마운트
  네임스페이스 안에서만 보이고 호스트 파일 자체는 그대로다.
- **버그 발견**: `dropbear -R`(호스트키 자동 생성)은 `-r <커스텀경로>`를
  줘도 그 경로를 무시하고 컴파일타임 기본 경로
  (`/etc/dropbear/dropbear_*_host_key`)에 생성을 시도한다(이 dropbear
  2022.83 빌드에서 재현 확인 — 호스트에서 직접 실행해도 동일). hostroot가
  읽기전용이라 `/etc/dropbear/`에 쓸 수 없어 매번 "Couldn't create new
  file ... No such file or directory"로 실패. **해결**: `-R`을 쓰지 말고
  `dropbearkey -t rsa/-t ed25519 -f <쓰기가능경로>`로 키를 미리 만든 다음
  `-r <그 경로>`만 줘서 dropbear가 "읽기"만 하게 한다.

```bash
# 마운트: 쓰기가능 공유 + 격리된 인증 파일
mount --bind /mnt/ebpf /mnt/host/mnt   # /mnt/host/mnt 는 이미 있는 빈 디렉터리
mount --bind /mnt/ebpf/sshroot_etc/passwd /mnt/host/etc/passwd
mount --bind /mnt/ebpf/sshroot_etc/shadow /mnt/host/etc/shadow
mount --bind /mnt/ebpf/sshroot_etc/group  /mnt/host/etc/group

# 호스트키를 먼저 생성 (chroot 밖 경로 표기 기준 /mnt/dropbear_keys)
LIBP=<dropbear_extract>/usr/lib/x86_64-linux-gnu
DBK=<dropbear_extract>/usr/bin/dropbearkey
chroot /mnt/host env LD_LIBRARY_PATH=$LIBP $DBK -t rsa -f /mnt/dropbear_keys/dropbear_rsa_host_key
chroot /mnt/host env LD_LIBRARY_PATH=$LIBP $DBK -t ed25519 -f /mnt/dropbear_keys/dropbear_ed25519_host_key

# -R 없이 -r 만으로 기동
BIN=<dropbear_extract>/usr/sbin/dropbear
chroot /mnt/host env LD_LIBRARY_PATH=$LIBP $BIN -F -E -B -p 22 \
  -r /mnt/dropbear_keys/dropbear_rsa_host_key \
  -r /mnt/dropbear_keys/dropbear_ed25519_host_key \
  >> /mnt/ebpf/dropbear.log 2>&1 &
```
`-B`는 빈 비밀번호 로그인 허용(우리 shadow 파일의 비밀번호 필드가
비어있으므로 필요). root 로그인은 dropbear 기본값이 허용이라 별도 플래그
불필요(`-w`를 주면 오히려 막힘).

**검증**: 호스트에서 `ssh -p 2222 root@127.0.0.1` (비밀번호 빈 채로 엔터)
→ 로그인 성공, `uname -a`로 게스트 커널(6.1.4) 확인됨. Tailscale로
연결된 이 머신의 IP로도 2222 포트가 그대로 hostfwd 되므로 태블릿에서도
동일하게 접속 가능.

**주의**: QMP는 클라이언트를 하나만 받는다(§9.2) — grpc 서버가 이미 QMP를
붙잡고 있으면 새 QMP 클라이언트가 연결을 못 맺고 그냥 멈춘다. 네트워크
핫플러그나 재설정처럼 QMP를 직접 써야 하는 작업 전에는 grpc 서버를
먼저 내려야 한다.

### 9.8 웹 대시보드 탭 구조 + auto refresh, 그리고 이 과정에서 잡은 실제 버그

사용자 요청("디바이스 클릭하면 queue정보랑 performance탭을 만들어서 그걸
클릭해야 정보를 보여 달라 — 처음부터 보여주지 말고", "전체 기능에 대해
auto refresh")에 맞춰 `index.html`을 두 탭(Queue 정보 / 성능eBPF)으로
재구성했다. 디바이스를 고르는 것만으로는 아무 WebSocket도 안 열리고,
탭을 처음 클릭한 시점에만 그 탭에 필요한 연결이 시작된다(`state.activeTab`
이 `null`인 동안은 두 패널 다 대기 상태). 탭을 바꾸면 안 보이는 탭의
WebSocket은 끊어 자원을 아낀다. SQ/CQ 엔트리 패널은 WebSocket 푸시가
없어서, 큐를 고른 동안 REST를 주기적으로 다시 불러 auto refresh를
흉내낸다.

**이 작업 중 실측으로 발견한 실제 버그 2개** (Playwright 헤드리스 브라우저로
직접 클릭/탭전환을 재현해서 잡음):

1. **WebSocket 스트림 고아(leak) 문제** — `ws_metrics`/`ws_performance`
   핸들러가 `async for snap in _client.stream_device_metrics(...):
   await websocket.send_json(...)` 패턴이었는데, 브라우저가 먼저 소켓을
   닫아도 이 루프는 grpc 스트림의 "다음 메시지"를 계속 기다리다가 그게
   도착해 `send_json()`이 실패해야만 예외로 알아챈다. 그런데 이 핸들러는
   `websocket.receive()`를 전혀 안 불러서(보내기만 함) ASGI 서버가 disconnect
   를 능동적으로 알려줄 방법이 없다 — 결과적으로 서버 쪽 gRPC 스트림이
   끊긴 뒤에도 계속 살아남아 폴링을 반복한다.
2. **이벤트 루프 블로킹 + 직렬화 병목** — `get_device_snapshot()`은 drgn/QMP
   라이브 조회라 1회에 약 4초 걸린다(실측). grpc 서버가 이 동기 함수를
   이벤트 루프에서 직접 불렀기 때문에, 그 4초 동안 `GetPerformance`처럼
   전혀 무관하고 원래 빠른(순수 파일 I/O) RPC까지 전부 멈췄다. 탭을
   빠르게 반복 전환하는 걸로 재현했더니(1번 버그와 겹쳐서) 그 직후의 단순
   REST 스냅샷 조회가 30~45초까지 밀리는 걸 실측했다.

**고친 방법**:
- `grpcserver/server.py`: 모든 backend 호출을 `ThreadPoolExecutor(max_workers=1)`
  로 감싼 `_run()`을 통해 실행(`asyncio.to_thread`와 동일한 효과를 명시적
  executor로). worker 1개라서 drgn/QMP 동시 접근은 여전히 안 되지만(그건
  QMP 자체의 제약, §9.2), 최소한 이벤트 루프는 안 막혀 다른 RPC가 계속
  돌아간다.
- `web/app.py`: `_stream_ws()` 헬퍼를 만들어, "다음 값 기다리기"와
  "`websocket.receive()`로 disconnect 감지하기"를 `asyncio.wait(...,
  FIRST_COMPLETED)`로 경합시킨다. 클라이언트가 끊기면 아직 시작 안 했거나
  막 시작한 backend 호출을 즉시 취소해(이미 실행 중인 건 스레드라 강제로
  못 멈추지만) 뒤에 안 쌓이게 한다. `finally: await gen.aclose()`로 grpc
  스트림 자체도 명시적으로 닫는다.
- `web/static/index.html`: SQ/CQ 엔트리 auto refresh를 `setInterval(2000)`
  대신 "이전 조회가 끝난 뒤에만 다음 걸 예약"하는 재귀 `setTimeout`으로
  구현 — 응답이 2초보다 오래 걸려도(실측 조합 약 2~5초, 서버 부하에 따라
  변동) 요청이 쌓이지 않는다. 큐를 클릭한 직후엔 "조회 중..." 문구부터
  바로 표시해 수 초짜리 대기 동안 화면이 멈춘 것처럼 보이지 않게 했다.

**실측 개선**: 탭 6회 빠른 반복 전환 직후 REST 스냅샷 조회 — 수정 전
29.8초 → 수정 후 5.4초(사실상 "그 순간 실행 중이던 조회 1개"만큼으로 수렴).

**남는 근본 한계**: drgn `--qemu`(QMP `dump-guest-memory`) 방식은 태생적으로
조회 1번에 초 단위가 걸린다(게스트 메모리를 매번 다시 받아오는 구조로
보임 — 2048MB 게스트 기준 약 4초). 이건 코드 버그가 아니라 이 라이브
조회 방식 자체의 비용이라, "0.5초 간격 실시간"을 요청해도 실제 갱신은
그보다 느리게(수 초 간격으로) 온다. 더 빠르게 하려면 QMP
dump-guest-memory를 매번 다시 하지 않는 다른 라이브 조회 방식이
필요한데, 이번 작업 범위 밖이라 손대지 않았다.

### 9.9 레이턴시 QoS percentile (p50/p95/p99/p99.9)

사용자 요청("latency에 대해서 qos 형태로 2nine, 3nine 이런것도 추가")에 맞춰
평균(avg) 레이턴시만 보여주던 걸 p50/p95/p99/p99.9로 확장했다. avg는 꼬리
지연(tail latency)을 못 보여준다는 게 실무에서 percentile을 따로 보는 이유
그대로다.

**방식**: `ebpf/nvme_perf.bt`의 `nvme_complete_rq`에서 레이턴시를 합산
(`@lat_sum`)하는 것과 별개로 `@lat_hist[ctrl_id, qid] = hist($lat)`로
2배수(log2) 버킷 히스토그램을 같이 쌓는다. bcc의 `biolatency`/`runqlat`
같은 표준 eBPF 툴이 percentile을 낼 때 쓰는 것과 같은 방식 — **정확한
값이 아니라 버킷 상한 근사치**다(예: p99가 1024~2048ns 버킷에 걸리면
2048ns를 p99로 본다). 매 요청의 정확한 레이턴시를 다 로그로 남기는 방식보다
훨씬 싸면서, QoS 모니터링 용도로는 실무 표준 정밀도다.

**파싱**: `backend/ebpf_perf.py`가 이제 두 가지 맵 포맷을 한 번에 판다 —
스칼라 맵(`@op_count[0,1]: 5` 한 줄)과 히스토그램 맵(`@lat_hist[0,1]:` 뒤에
`[512, 1K)   80  |...|` 같은 버킷 줄 여러 개). `_percentile_ns()`가 버킷을
저->고 순으로 누적하며 목표 비율(0.50/0.95/0.99/0.999)을 처음 채우는
버킷의 상한을 반환한다.

**전체 합산(aggregate) 행**: 개별 큐는 초당 표본 수가 적어(특히 유휴 큐)
버킷이 들쭉날쭉할 수 있어, 사용자 요청대로 "큐별 + 디바이스 전체 합산"을
같이 낸다 — 그 디바이스의 모든 큐(활동이 있었던 큐만) 히스토그램을
`(lo,hi)` 버킷 기준으로 합쳐서 하나의 percentile 세트를 추가로 계산
(`DevicePerf.aggregate`, `QueuePerf.qid=-1`). 웹 UI는 이걸 `perf-table`의
`<tfoot>`에 굵게 고정 행("전체")으로, CLI(`telemetryd perf`)는 구분선 뒤
"ALL" 행으로 보여준다.

**바뀐 부분**: `proto/telemetryd.proto`(QueuePerf에 p50/p95/p99/p999,
DevicePerf에 aggregate 필드 추가) → `models.py` → `ebpf_perf.py`(파서+계산)
→ `mock_backend.py`(avg 기준 배수로 합성 percentile 흉내, 테스트/CLI가
드라이버 없이도 돌아가게) → `grpcserver/convert.py` → CLI `perf` 컬럼 →
web `perf-table` 컬럼+tfoot. `tests/test_ebpf_perf.py`에 실제 bpftrace
히스토그램 출력 포맷을 그대로 흉내낸 파싱/percentile/aggregate-merge
테스트 추가(라이브 게스트에서 `bpftrace -e '...hist(...)...'`로 직접 찍어
포맷을 확인한 뒤 작성).

**주의(재기동 시 버퍼링 지연)**: bpftrace를 파일로 리다이렉트(`>> log 2>&1 &`)
하면 표준 C 라이브러리가 non-tty stdout을 기본적으로 완전 버퍼링해서,
막 띄운 직후엔 버퍼가 찰 때까지(실측 수십 초) 로그가 안 늘어난다 — 수집기가
죽은 게 아니라 첫 flush를 기다리는 것뿐이다. `stdbuf -oL`로 줄 단위
버퍼링을 시도했으나 bpftrace 자체 출력 경로엔 효과가 없었다(라이브러리
수준 setvbuf 가로채기가 안 먹힘) — 근본적으로 고치려면 bpftrace를
`--unbuffered`류 옵션으로 띄우거나 표준출력 대신 소켓/파이프로 직접
스트리밍하는 방식이 필요한데, 이번 범위 밖이라 "재기동 직후 수십 초는
비어 보일 수 있다"로 문서화만 해 둔다.

### 9.10 성능 탭 — 큐 클릭 시 작업 관리자 스타일 실시간 그래프

사용자 요청("윈도우 작업관리자 - 성능 탭처럼 각 큐를 눌렀을 때 실시간 수치 +
시계열 그래프")에 맞춰 `perf-table`의 행(큐별 또는 "전체" 합산 행)을 누르면
아래에 그래프 3개(IOPS read/write, 대역폭, 레이턴시 avg/p99)가 뜨는
`perf-detail-panel`을 추가했다.

**설계**: 서버(`GetPerformance`/`StreamPerformance`)는 매 틱의 "순간값"만
주고 이력을 안 쌓아 보내므로(서버를 상태 없이 유지하려는 기존 설계, §6),
시계열은 전적으로 **클라이언트가** WebSocket 메시지를 받을 때마다
`state.perfHistory[qid]`에 append하며 만든다(최근 60틱=대략 1분, 링버퍼).
탭을 벗어나면 §9.8에서 이미 그렇듯 성능 WebSocket이 끊기므로 그 구간은
이력에 비게 되고, 다시 열면 그 시점부터 이어서 쌓인다 — 서버 재시작 전
구간을 영구 보존하는 건 아니다(요청 범위 밖으로 판단, 필요하면 서버 쪽
링버퍼를 별도로 둬야 함).

**렌더링**: 외부 차트 라이브러리 없이 `<canvas>` 2D API로 직접 그린다
(이 프로젝트가 CDN 의존성이 전혀 없는 걸 유지하는 기존 방침과 일치).
x축은 항상 `PERF_HISTORY_LEN`(=60) 고정 폭 기준으로 매핑해서, 버퍼가
아직 안 찼을 때는 왼쪽부터 채워지다가 다 차면 오른쪽에서 새 데이터가
들어오고 왼쪽이 밀려나는 통상적인 모니터링 그래프 스크롤 방식이 된다.

**실측 확인**: Playwright로 큐 행 클릭 → 8초 동안 이력이 7→9→11→12개로
쌓이는 것, canvas에 실제 픽셀이 그려지는 것(투명이 아닌 픽셀 1705개),
닫기 버튼 동작을 모두 확인.

### 9.11 NVMe 커맨드 타임아웃 이벤트 로그 (eBPF, kprobe:nvme_timeout)

사용자 요청("nvme timeout에 대해 hook 걸고, 발생했을 때 timeout났던 request
정보, cdw 정보등을 로그로 남겨서... event 리스트 창"). §9.6 성능 수집기와
같은 `ebpf/nvme_perf.bt`에 `kprobe:nvme_timeout`을 추가해 구현했다.

**왜 kprobe인가 / 왜 nvme_setup_cmd에서 CDW를 미리 캐시하는가**: `nvme_timeout
(struct request *req)`(drivers/nvme/host/pci.c)를 직접 읽어보면, 타임아웃
시점엔 원본 SQE(opcode/cdw10-15 등)가 `req`에서 바로 안 보인다 — 그 지역
변수 `struct nvme_command cmd = {};`는 새로 만들 ABORT 커맨드용이지 원본이
아니다. 그래서 **제출 시점**(`nvme_setup_cmd`, 이미 IOPS/레이턴시 집계에
쓰던 훅)에 `(ctrl_id, qid, tag)` 키로 opcode/nsid/flags/cdw10-15와 제출
타임스탬프를 미리 캐시해 뒀다가, 타임아웃 발생 시 그 캐시를 조회해서 찍는다.
`tag`는 `req->tag`(kprobe에서 바로 읽힘)로 통일했다 — NVMe cid는
`(genctr<<12)|tag`라 genctr까지 필요 없게(drivers/nvme/host/nvme.h). 정상
완료되면(`nvme_complete_rq`) 캐시를 지워 메모리가 안 쌓이게 한다.

**구조체 체인**: `struct request*` → `->mq_hctx->driver_data`를
`struct nvme_queue*`로 캐스트(PCI 드라이버가 hctx 초기화 때 그렇게 심어둠)
→ `->dev`(`struct nvme_dev*`) → `->ctrl.instance`(=ctrl_id) / `->qid`.

**⚠️ 이 작업 중 실제로 게스트가 한 번 죽었다**: 이 kprobe를 처음 테스트할 때
(tmux 포그라운드에서 실행 중 Ctrl-C로 인터럽트) 직후 tmux 서버 자체가
사라지고 QEMU 프로세스도 같이 죽었다. 처음엔 "위험한 struct 접근이 커널을
크래시시켰다"고 판단해 사용자에게 그렇게 보고했는데, 재조사 결과 그 판단은
**틀렸을 가능성이 크다**:
- `/proc/kallsyms`로 확인해 보니 `nvme_timeout`은 실제로 별도 심볼로 존재
  했다(인라인 안 됨) — bpftrace가 띄운 "not traceable" 경고는 이 커널이
  `CONFIG_DYNAMIC_FTRACE`를 안 켜서 `available_filter_functions` 파일 자체가
  없어 생긴 오탐으로 보인다(kprobe 자체는 ftrace 없이도 동작 가능).
- eBPF는 검증기가 있어서 애초에 "이상한 포인터를 읽어 커널을 죽이는" 게
  구조적으로 안 된다 — verifier가 모든 메모리 접근을 안전한 헬퍼로 강제한다.
- 게스트를 재기동한 뒤, **문제가 됐던 것과 완전히 동일한 kprobe+구조체 체인**
  을 백그라운드로(포그라운드 tty 아님) 단계적으로(필드 하나씩 늘려가며) 4번
  재현했는데 전혀 문제없었다.
- 결론: 크래시의 실제 원인은 이 eBPF 프로브가 아니라, **tmux 포그라운드에서
  실행 중이던 프로세스에 Ctrl-C를 보낸 것과 관련된 뭔가**(pty/tmux 레이어
  이슈, 또는 무관한 호스트 쪽 우연)였을 가능성이 높다. 교훈: bpftrace
  테스트는 항상 백그라운드로(`&`, 출력 리다이렉트) 돌리고 `timeout`으로
  종료시키지, 포그라운드 tty에서 실행하다 수동으로 인터럽트하지 않는다.

이 크래시 복구 과정에서 **실제 버그 2개**를 더 찾아 고쳤다(§9.8에서 이미
있던 것과 같은 부류 — drgn/QMP 동시성):
1. `grpcserver/server.py`의 `ListDevices`만 유일하게 `_run()`(단일 워커
   executor) 없이 이벤트 루프에서 직접 `self._backend.list_devices()`를
   불렀다 — 마침 활성 상태였던 `StreamDeviceMetrics`(executor 스레드)와
   같은 drgn Program 객체를 서로 다른 스레드에서 동시에 건드리면서
   "recursive address translation; page table may be missing" 오류가
   났다. 모든 backend 호출은 예외 없이 `_run()`을 거쳐야 한다.
2. `web/app.py`의 `_stream_ws()`에서 disconnect 감지 시 `next_task.cancel()`
   만 하고 안 기다린 채 바로 `finally`의 `gen.aclose()`를 불러
   `RuntimeError: aclose(): asynchronous generator is already running`가
   났다 — cancel()은 취소를 요청만 할 뿐이라, 실제로 취소가 반영될 때까지
   `await next_task`로 기다린 뒤에 닫아야 한다.

**타임아웃을 실제로 재현해서 끝까지 확인하려던 시도**: `nvme_core`의
`io_timeout` 파라미터(`/sys/module/nvme_core/parameters/io_timeout`, 기본
30초)를 1~2초로 낮추고, QMP `stop`/`cont`(전체 정지→재개)와 게스트 8개
vCPU에 busy-loop를 걸어보는 것까지 시도했지만 실제 타임아웃을 못 일으켰다:
- `stop`/`cont`는 게스트의 가상 클록 자체를 같이 멈춰서, 재개해도 게스트
  입장에선 "시간이 하나도 안 지난" 걸로 보여 데드라인 비교에 안 걸린다.
- CPU busy-loop로 유저스페이스(fio)를 굶겨도, NVMe 완료 인터럽트 처리는
  IRQ 컨텍스트(유저스페이스보다 우선순위 높음)라 영향을 거의 안 받는다.
- 이 busy-loop 실험이 오히려 그 시점 fio 벤치마크를 일찍 끝내버리는 부작용을
  냈다(fio 재기동으로 복구).

진짜로 재현하려면 `blkdebug` QEMU 블록 드라이버로 특정 I/O에 인위적 지연을
주입해야 하는데, 그러려면 드라이브 재설정 + 게스트 재기동이 필요해서(이미
이 세션에서 게스트를 여러 번 죽였다 살렸다 한 참이라) 더 이상의 중단을
피하려고 하지 않았다. 대신:
- 파싱/집계 로직은 **실제 라이브 게스트에서 확인한 bpftrace `hist()` 출력
  포맷 그대로**를 흉내낸 텍스트로 `tests/test_ebpf_timeout_events.py`가
  7개 케이스로 검증(증분 tail 읽기, 파일 truncation 복구, 디바이스 필터,
  링버퍼 상한 포함).
- kprobe 자체의 구조체 접근 체인은 라이브 게스트에서 4단계로 안전성을
  실측 확인(위 크래시 재조사 항목).
- REST/WebSocket/웹 UI는 "이벤트 없음" 빈 상태로 실제 라이브 서버를 통해
  엔드투엔드 확인(콘솔 에러 없음). **경로/화면 위치는 §9.12에서 바뀌었다** —
  당시엔 `/api/devices/{d}/timeout-events`와 성능 탭 하단 "타임아웃 이벤트"
  패널이었지만, 지금은 종류 무관 `/api/devices/{d}/events` + 독립된 "이벤트"
  탭이다(타임아웃은 그 목록의 한 종류).
- 실제 타임아웃이 발생했을 때의 전체 파이프라인(캐시 조회 → printf →
  로그 → 파서 → API → UI)만 아직 "진짜 이벤트"로는 미확인 — 필요하면
  `blkdebug`로 재현하는 걸 다음 작업으로 남긴다.

### 9.12 이벤트 탭 — 종류를 가리지 않는 이벤트 목록

사용자 요청("queue정보/성능(ebpf)/event 탭 하나 만들어서 event 쪽에서 보여주자.
event는 reset일 수도, timeout일 수도 있으니 뭔가 항목을 대표해서 나타내버리면
안 된다. nvme_timeout에 맞는 로그만 포매팅해서 보여주면 된다")에 맞춰,
§9.11에서 성능 탭 하단에 붙여 뒀던 "타임아웃 이벤트" 패널을 **세 번째 탭
"이벤트"**로 분리하면서 계층 전체를 종류 무관 구조로 바꿨다.

**핵심 설계 — 봉투(envelope) + 종류별 상세**: 이벤트 목록의 어떤 컬럼도
특정 종류의 필드가 아니게 만든다.

| 계층 | 종류 무관 공통 | 종류별 상세 |
|---|---|---|
| `models.py` | `NvmeEvent{kind, observed_at, device, qid, summary}` | `NvmeEvent.timeout: TimeoutEventDetail` (Optional 슬롯) |
| `proto` | `NvmeEvent` 필드 1~5 | `oneof detail { TimeoutEventDetail timeout = 10; }` |
| gRPC | `GetEvents` / `StreamEvents` (← `GetTimeoutEvents`/`StreamTimeoutEvents`) | — |
| REST/WS | `/api/devices/{d}/events`, `/ws/devices/{d}/events` | JSON에 `timeout` 키가 있으면 그 종류 |
| CLI | `telemetryd events <dev> [--kind ...]` (← `timeout-events`) | `_EVENT_DETAIL_RENDERERS[kind]` |
| Web | 컬럼 = 시간/종류/qid/요약 | 행 클릭 시 `EVENT_DETAIL_RENDERERS[kind]` |

- **왜 `summary`를 봉투에 두나**: 소비자(목록 테이블/CLI 한 줄)가 종류를 몰라도
  한 줄은 그릴 수 있어야 하기 때문. 요약문은 그 종류를 아는 **생산자**(지금은
  `ebpf_timeout_events.py`)가 만든다 — 예: `write(0x01) 커맨드가 30.0s 동안
  완료되지 않음 (qid=3, tag=42, nsid=1)`.
- **왜 proto `oneof`인가**: 설정된 멤버만 직렬화되므로 JSON에서 키의 유무가
  그대로 종류 판별이 된다(`always_print_fields_with_no_presence`는 presence가
  없는 필드에만 적용되므로 oneof를 강제로 채우지 않는다 — `tests/test_events.py`
  가 이 JSON 모양을 고정해 둔다).
- **모르는 종류가 와도 안 깨진다**: 상세 렌더러가 없는 kind는 요약 한 줄로만
  표시되고 행이 펼쳐지지 않는다. `tests/test_events.py`가 아직 구현 안 된
  `kind="reset"` 이벤트를 흉내내 이 경로를 미리 고정해 둔다 — 이 테스트가
  깨지면 목록이 다시 timeout 전용으로 굳었다는 뜻이다.
- **CDW는 여전히 timeout 전용 포매팅**: 요청대로 nvme_timeout 로그에 맞는
  형식(tag/opcode/nsid/flags/CDW10-15/제출~타임아웃 경과)은 그대로 유지하되,
  그 표는 목록 컬럼이 아니라 **펼친 행 안**에만 존재한다.

**새 종류를 추가할 때 손댈 곳** (목록/스트림/탭 구조는 그대로):
1. 그 종류의 리더 모듈(예: `backend/ebpf_reset_events.py`) — 봉투를 만들어 반환
2. `DrgnBackend.get_events()`의 `sources` 리스트에 리더 추가(합쳐서 시간순 정렬)
3. `models.py`의 Optional 상세 슬롯 + `proto`의 oneof 멤버 + `convert.py` 분기
4. UI/CLI의 종류별 렌더러 등록(`EVENT_DETAIL_RENDERERS` / `_EVENT_DETAIL_RENDERERS`)

**탭 생명주기**: 이벤트 WS는 이제 성능 탭이 아니라 이벤트 탭에 종속된다(§9.8의
"안 보이는 탭의 연결은 끊는다" 원칙 그대로). 탭이 3개가 되면서 if/else 분기로는
누락이 생기기 쉬워 `TABS` 배열을 두고 루프로 처리하도록 바꿨다.

**검증**: 라이브 로그를 오염시키지 않으려고(실제로 안 난 타임아웃을 난 것처럼
남기면 나중에 오해가 됨) 별도 포트(50052/8001)에 스텁 백엔드 스택을 띄우고
Playwright로 확인했다 — 실제 `nvme_perf.bt` 출력 포맷 그대로의 TIMEOUT_EVENT 줄
2건을 진짜 파서에 통과시키고, 거기에 상세 렌더러가 없는 `kind="reset"` 1건을
섞었다. 확인 항목: 탭 클릭 전에는 WS 미연결(행 0), 클릭 후 3건 표시(최신순),
타임아웃 행 클릭 시 CDW 상세 펼침, WS가 1초마다 목록을 다시 보내도 펼침 유지,
reset 행은 펼쳐지지 않음, 종류 필터(전체/reset/timeout) 동작, 탭 전환 시
패널 숨김·복귀, 콘솔 에러 0건.

### 9.13 A2 — 에러 status 캡처 (tracepoint:nvme:nvme_complete_rq, status != 0)

사용자 요청: "실무에서 발생하는 device 이상 징후 중 timeout까지 도달하는 것은
극히 일부다. 대부분은 그 전에 에러 status로 반환되고, 상위 계층이 재시도로
흡수해버려서 애플리케이션에서는 보이지 않는다 — 이것도 등록해서 같이 보자."

§9.12에서 만든 종류 무관 이벤트 구조 위에 **두 번째 종류(kind="error")를
등록**한 것이다. 목록/스트림/탭/REST 경로는 하나도 안 바뀌었다 — 설계 의도가
그대로 확인된 셈(리더 모듈 1개 + 상세 슬롯 1개 + proto oneof 멤버 1개 +
렌더러 1개 + 레지스트리 한 줄).

**status 비트 분해**: `nvme:nvme_complete_rq`의 `status`는 드라이버의
`nvme_req(req)->status`, 즉 `le16_to_cpu(cqe->status) >> 1`이라 이미 phase
비트가 빠진 값이다(drivers/nvme/host/nvme.h `nvme_try_complete_req`). 따라서
SC=bits[7:0], SCT=bits[10:8], CRD=bits[12:11], More=bit[13], DNR=bit[14]로
바로 분해된다(include/linux/nvme.h의 `NVME_SC_CRD/MORE/DNR` 마스크와 일치).
호스트 파서는 로그에 같이 찍힌 sct/sc 텍스트를 믿지 않고 **status에서 다시
분해한다**(`nvme_const.decode_status`) — 같은 값을 두 곳에서 계산하면 언젠가
한쪽만 고쳐져 조용히 어긋나기 때문.

**SLBA/NLB**: 완료 이벤트엔 없어서 §9.11의 타임아웃과 똑같이 제출 시점
(`nvme_setup_cmd`)에 (ctrl,qid,tag)로 캐시해 둔 CDW에서 복원한다.

**누적 카운터(@err_count[ctrl, sct, sc])**: 이벤트 줄과 **별개로** 유지한다.
이벤트 줄은 로그 폭주를 막으려고 초당 인쇄 예산(`@err_budget`, 컨트롤러당
50건/초)으로 샘플링될 수 있지만 카운터는 전부 센다 — "각 건의 상세"는 놓쳐도
"몇 건 났는지"는 정확하다. 성능 맵과 달리 `interval`에서 clear하지 않으므로
매 틱 누적 총계가 다시 찍히고, 파서는 마지막으로 끝난 틱의 값을 그대로 쓴다
(합치면 틱 수만큼 부풀려진다 — `tests/test_ebpf_error_events.py`가 고정).
카운터 조회는 로그 끝 512KB만 읽는다(perf 쪽이 파일 전체를 읽는 것과 다름 —
§"남은 개선거리").

**API**: `GetErrorStats`/`ListEventKinds` RPC + `/api/devices/{d}/error-stats`,
`/api/event-kinds` REST. 후자는 "등록된 이벤트를 알 수 있는가?"에 대한 답으로,
UI가 종류 목록을 하드코딩하지 않고 서버에 물어본다(`backend/event_registry.py`가
단일 진실 공급원, `active` 플래그로 "등록됐지만 수집기 미설정" 상태를 구분).
CLI에도 `telemetryd event-kinds` / `telemetryd error-stats`가 붙는다.

**실제 커널로 검증 (게스트에서 진짜 에러를 만들어 확인)**: mock으로는 못 잡는
문제들을 실제 데이터가 드러냈다. 게스트에서 호스트 nvme-cli를 9p+chroot로
빌려 써서 세 종류의 에러를 냈다:

| 만든 방법 | 실제 반환 | 확인된 것 |
|---|---|---|
| `nvme admin-passthru /dev/nvme0 --opcode=0xff` | `0x4001` Invalid Command Opcode | admin 큐(qid=0), DNR |
| `nvme read /dev/nvme0n1 --start-block=99999999 ...` | `0x4080` LBA Out of Range | I/O 큐(qid=3/7), SLBA=99999999 복원 |
| `nvme get-log --log-id=0xfe` | `0x4002` Invalid Field in Command | broadcast NSID(0xFFFFFFFF) |

이 과정에서 잡은 실제 버그 4개:
1. **bpftrace 스크립트 컴파일 실패** — 에러 캡처 블록을 `$tag` 할당보다 위에
   넣어 "Undefined or undeclared variable: $tag". (TCG 게스트에서 bpftrace
   컴파일에 ~80초가 걸려, 로그가 한동안 0바이트인 게 정상이라는 것도 확인.)
2. **opcode 0xff가 -1로 찍힘** — bpftrace가 트레이스포인트의 u8을 부호 있는
   값으로 넘긴다. 호스트 파서에서 8비트로 다시 마스킹(안 하면 `0x-1` 표시).
3. **admin의 opcode 2(get_log)를 read로 오독** — `lba_valid` 판정에 qid를
   안 봐서, get_log의 cdw10/11(로그 페이지 필드)을 SLBA로 표시했다. admin
   큐(qid=0)는 LBA 커맨드가 없으므로 조건에 `qid != 0`을 추가.
4. **broadcast NSID가 protobuf 범위를 넘김** — NSID 0xFFFFFFFF는 정상 값인데
   proto가 `int32`라 `ValueError: Value out of range: 4294967295`로 RPC가
   502를 냈다. `uint32`로 바꿨다(같은 잠재 버그가 TimeoutEventDetail과
   QueueEntry에도 있어서 함께 수정).

이 4개는 전부 `tests/`에 회귀 테스트로 고정했다(실제 로그 줄을 그대로 픽스처로
씀). 웹 UI는 Playwright로 이벤트 5건 표시/행 펼침/누적 표(3+1+1=5)/등록 종류
카드 2개/콘솔 에러 0건을 라이브 서버로 확인.

### 9.14 통합 토폴로지 뷰 — PCIe 계보 + NVMe 서브시스템을 한 트리로

사용자 요청: "장치에 대한 pcie topology와 nvme subsystem 구조를 같이 표현하는
창을 만들어 달라. **통합 트리가 이 기능의 핵심**."

네 번째 탭 "토폴로지". 기존 `tree`(§5.5의 lazy 포인터 탐색)와 목적이 다르다 —
저건 구조체 필드를 있는 그대로 따라가는 범용 탐색이고, 이건 **"장치가 시스템에
어떻게 붙어 있는가"를 의미 단위로 재구성한 정적 뷰**다. 노드가 수십 개 수준이라
lazy 확장 없이 한 번에 만들어 보낸다.

```
시스템
└─ pci0000:00                호스트 브리지(루트 버스)         ← PCIe 계층(파랑)
   ├─ 0000:00:03.0           PCIe 엔드포인트, NVMe 클래스
   │  └─ nvme0               struct nvme_dev / nvme_ctrl      ← 두 계층이 만나는 지점
   │     ├─ nvme-subsys0     subnqn/model/serial/펌웨어       ← NVMe 계층(초록)
   │     ├─ nvme0n1          nsid=1, LBA 512B, 0.06 GiB
   │     └─ 큐 9개            admin 1 + I/O 8 (qid별 depth/도어벨/hctx)
   └─ 0000:00:04.0 → nvme1 …
```

**커널 경로**: `nvme_dev.dev`(struct device*) → `container_of(..., struct pci_dev, dev)`
로 PCIe 쪽에 진입하고, `pci_dev.bus` → `bus.self`(부모 브리지) → `bus.parent`를
반복해 루트 버스까지 거슬러 올라간다(`bus.self == NULL`이면 루트). NVMe 쪽은
`ctrl.subsys`(nvme_subsystem: subnqn/model/serial/firmware_rev, ctrls/nsheads
리스트 길이), `ctrl.namespaces`(nvme_ns → head.ns_id, lba_shift, disk.part0
.bd_nr_sectors), `dev.queues[i]`(nvme_queue).

**조상 노드 병합**: 컨트롤러가 여러 개여도 위쪽 PCIe 조상은 노드 id로 병합해
한 번만 나오고 거기서 갈라진다 — 같은 브리지를 공유하는 게 실제 하드웨어
구조라, 병합을 안 하면 계보가 왜곡된다(`tests/test_topology.py`가 고정).

**노드 모델**: `TopologyNode{id, kind, label, sublabel, device, details[], children[]}`.
종류별 필드를 모델/proto에 박지 않고 `details`(key-value 목록)로 넘기는 건
이벤트 봉투(§9.12)와 같은 방침이다 — 새 노드 종류가 생겨도 gRPC/REST/UI를
고칠 필요가 없고, UI는 모르는 kind가 와도 label/sublabel/details만으로 그린다.
각 NVMe 노드는 자기가 속한 `device`를 들고 있어 UI가 선택된 장치의 경로를
자동으로 펼치고 강조한다(큐 그룹은 자식이 9개씩이라 자동 펼침에서 제외 —
안 그러면 트리를 열자마자 큐 목록이 화면을 다 차지한다).

**성능**: drgn 라이브 조회라 디바이스마다 커널 구조체를 여러 번 읽는다 —
실측 컨트롤러 2개 + 큐 18개 기준 **10~12초**(fio 부하가 도는 TCG 게스트 기준.
§9.8에 적어둔 대로 QMP dump-guest-memory 방식 자체가 조회 1회에 초 단위가
걸리고, 토폴로지는 그 조회를 장치·큐마다 반복한다). 실시간 스트림이 아니라 탭을 열 때 한 번
받고 캐시하며, 갱신은 새로고침 버튼으로 명시한다(장치 구성은 거의 안 바뀐다).
gRPC 서버는 이 호출을 반드시 `_run()`(단일 워커 executor)으로 돌린다 — §9.8에서
실측한 "느린 drgn 호출이 이벤트 루프를 막아 무관한 RPC까지 멈추는" 문제 그대로다.

**실제 커널로 검증하며 잡은 버그 2개** (둘 다 mock으로는 절대 안 드러남):
1. **PCIe 포트 타입을 잘못 읽음** — `PCI_EXP_FLAGS_TYPE`은 `0x00f0`(bits 7:4)인데
   `& 0xf`로 읽어 bits[3:0](capability 버전)을 타입으로 표시했다. QEMU NVMe가
   "타입 0x2"로 나와서 발견 — 커널 `pci_pcie_type()`과 같이 `>> 4`로 수정.
2. **`current_state`가 `(pci_power_t)0`으로 표시** — `pci_power_t`는 enum이 아니라
   `typedef int`라 drgn이 열거자 이름을 못 준다. `PCI_D0~PCI_POWER_ERROR` 표를
   직접 들고 "D0 (완전 동작)"으로 변환.
둘 다 순수 함수(`pcie_type_name`/`pci_power_state_name`)로 분리해 drgn 없이도
도는 회귀 테스트를 붙였다.

**검증**: mock 기준 8개 테스트(구조/조상 공유/device 태깅/재귀 직렬화/JSON 모양)
+ CLI 2개. 라이브 게스트에서는 실제 트리(호스트 브리지 → 엔드포인트 2개 →
컨트롤러/서브시스템/네임스페이스/큐 9개씩)를 Playwright로 확인 — 선택 장치
경로 자동 펼침/강조, 노드 클릭 시 속성 표(BDF·클래스·IRQ·전원 상태·subnqn·
용량 등), 디바이스 전환 시 강조 이동, 콘솔 에러 0건.

### 9.15 NVMe I/O 프로세스 프로파일러 — 대상 선택 일반화

요청: "이 도구는 특정 애플리케이션 전용 모니터가 아니라 **NVMe I/O를 발행하는
프로세스에 대한 범용 프로파일러**다. 관측 대상은 런타임에 선택된다."

다섯 번째 탭 "프로파일러". 코어 수집 로직에는 어떤 애플리케이션 이름도 들어가지
않고, 애플리케이션별 의미 부여는 어댑터가 전담한다.

#### 이 환경에 맞춘 두 가지 구조적 결정 (명세와 다른 점)

| 명세 | 이 구현 | 이유 |
|---|---|---|
| 5-1: 커널 `target_pids` 맵으로 필터 | 커널은 **전 프로세스를 집계**, 필터는 호스트에서 | bpftrace는 유저스페이스에서 맵을 갱신할 수 없다(런타임 대상 추가/제거가 불가). libbpf 기반 수집기로 바꾸면 그대로 옮길 수 있게 계층은 분리해 뒀다 |
| 1-2: `/proc/<pid>/...` 로 프로세스 정보 | drgn으로 **task_struct 순회** | 관측 대상은 QEMU 게스트 안 프로세스라 호스트 /proc에는 존재하지 않는다. 호스트 라이브(sudo drgn)에서도 같은 코드가 동작한다 |

두 번째 결정의 부수 효과가 하나 있다: 커널이 전 프로세스를 세므로 **"관측 대상이
아닌 프로세스가 같은 장치에 I/O를 내고 있다"(명세 2-2/5-2)를 공짜로 얻는다**.

`/proc` 필드 → 커널 위치 대응: comm=`task.comm`, cmdline=`mm->arg_start..arg_end`
(유저 메모리), exe=`mm->exe_file→d_path()`, uid=`cred->uid.val`,
start_time=`task.start_boottime`, threads=전체 task를 tgid로 묶음.

**유저 메모리 읽기 문제와 우회**: drgn의 `access_process_vm()`은 QMP로 붙은 라이브
게스트에서 `FaultError: recursive address translation`으로 실패한다(실측) — 페이지
테이블 엔트리를 읽으려면 커널 가상주소를 다시 변환해야 하는데 그게 재귀에 걸린다.
그래서 `mm->pgd`만 `page_offset_base`로 물리 주소로 바꾼 뒤, **PGD→PUD→PMD→PTE를
전부 물리 읽기(`prog.read(..., physical=True)`)로 직접 걷는** 폴백을 넣었다
(2MB/1GB 큰 페이지 포함). 이 방식은 §9.5의 PRP 페이로드 덤프에서 이미 검증된
접근이다. 이게 없으면 fio 어댑터가 cmdline을 못 읽어 기능의 핵심(기대값 대조)이
통째로 죽는다.

#### 계층

```
ebpf/nvme_perf.bt (탐색 모드: 전 프로세스 집계)
  @proc_ops[ctrl,tgid,comm] @proc_bytes @proc_rd/@proc_wr @proc_bs[크기]
  @proc_q[qid] @proc_seq/@proc_rand(LBA 연속성) @proc_lat_sum/cnt @thr_ops[tid]
        │
backend/proc_stats.py   맵 파싱 -> ProcessIoStat (장치 × 프로세스)
backend/procinfo.py     drgn task 순회 -> ProcessInfo (cmdline/exe/uid/threads)
backend/targets.py      규칙 해석 + 세션 생명주기 + 장치 귀속/미관측 I/O
backend/adapters/       fio | generic (플러그인 구조, 첫 매칭 채택)
        │
gRPC ListProcesses/ListTargets/AddTarget/RemoveTarget/GetProfile/StreamProfile
REST /api/processes, /api/targets(GET/POST/DELETE), /api/profile, WS /ws/profile
CLI  telemetryd processes | target add/remove/list | profile
```

- **대상 선택 4종**(pid/name/name_pattern/cmdline_pattern)이 전부 `TargetRule`
  하나로 수렴한다. 규칙은 파일에 저장돼 데몬과 CLI가 공유하고, 프로세스가 죽어도
  남아 다음 실행 때 자동으로 다시 붙는다.
- **세션**: `(pid, start_time_ns)`로 식별해 PID 재사용을 구분한다. 프로세스가
  끝나도 finished로 남아 cmdline 등 메타데이터를 보존한다. 데몬을 재시작해도
  같은 프로세스면 세션을 이어 쓴다(안 그러면 살아있는 프로세스에 세션이 하나씩
  더 생겨 화면이 중복된다 — 실측으로 발견).
- **큐 깊이 실측**: 제출/완료가 다른 컨텍스트라 재고를 직접 셀 수 없어 리틀의
  법칙(IOPS × 평균지연)으로 근사한다. 근사임을 모델/화면 문구에 명시한다.
- **완료 귀속**: 완료 훅에는 제출자 정보가 없어서, 제출 시 `@cmd_tgid[ctrl,qid,cid]`
  에 tgid를 매달아 두고 완료 때 그걸로 프로세스별 지연을 집계한다.
- **귀속의 한계**: O_DIRECT 제출은 발행 프로세스 컨텍스트지만 버퍼드 쓰기의
  writeback은 kworker가 대신 제출한다 — 그 경우 comm이 커널 스레드로 잡히며,
  가짜 귀속을 만들지 않고 그대로 표시한다.

#### 실제 게스트로 검증 (fio 2개 동시)

`scripts/guest_fio_profile.sh`로 서로 다른 워크로드를 동시에 띄워 확인했다 —
seqread(nvme0, rw=read, bs=128k, QD=8)와 randwrite(nvme1, rw=randwrite, bs=4k,
QD=32).

- **자동 발견**: 이름을 몰라도 `/api/processes?only_io=true`가 I/O를 내는 프로세스
  2개만 정확히 골라냈다(게스트 전체 95개 프로세스 중).
- **기대 vs 실측**: bs 128K/4K, R/W 100%/0% 모두 일치. **큐 깊이는 불일치**
  (기대 8 vs 실측 근사 1.5, 기대 32 vs 3.1) — TCG 에뮬레이션 환경에서 fio가
  요청한 깊이를 채우지 못한다는 실제 관측이며, 이런 걸 잡으라고 만든 기능이다.
- **cmdline 패턴 대상 지정**: `.*--rw=randwrite.*` 규칙 하나로 같은 `fio`
  실행파일 중 randwrite만 관측 대상이 되고, 나머지 seqread(pid 427)는
  "관측 대상 아님 — nvme0에 1650 IOPS"로 미귀속에 뜬다.

#### 이 과정에서 잡은 버그 3개
1. **세션 ID 충돌** — `sess_<날짜시각>_<pid>` 형식이라 같은 PID가 같은 초에
   재사용되면 id가 겹쳐 서로 다른 프로세스의 데이터가 한 세션에 섞였다. 충돌 시에만
   시작 시각 기반 접미사를 붙이도록 수정.
2. **I/O 0건인데 "일치"로 표시** — fio는 워커를 fork하는 메인 프로세스가 자기는
   I/O를 안 내는데, 실측이 없는 상태를 "기대대로 동작 중"으로 보고했다. 실측이
   없으면 **판단 불가**로 바꿨다("불일치"와도 다르다).
3. **데몬 재시작 시 세션 중복** — 프로세스 4개가 카드 8개(active+finished)로
   보였다. 저장된 세션의 신원 맵을 복원해 같은 프로세스면 이어 쓰도록 수정.

#### 아직 안 한 것 (명세 대비)
- 커널 쪽 필터 맵(5-1): bpftrace 제약. 지금은 전 프로세스 집계 + 호스트 필터.
  제출 훅당 맵 갱신이 6개 → 14개로 늘었는데, 워크로드가 바뀌어 통제된 A/B 측정은
  하지 못했다 — 오버헤드 수치는 미측정으로 남긴다.
- libnvme 어댑터(3-3): 이 저장소에 해당 애플리케이션이 없어 인터페이스만 준비.
  USDT/사이드채널을 어댑터 안에 넣는 자리는 그대로 비어 있다.
- 세션 비교 뷰(4-3), 프로세스 간 간섭 상관(2-2 마지막 항목), fio `--status-interval`
  출력 연동(3-2 선택 항목).

### 9.16 프로파일러 폴링이 대시보드 전체를 멈춘 건 (운영 중 실측 → 수정)

"현 상태 체크" 중 발견. 증상은 **대시보드 전체가 사실상 멈춤** — 웹 페이지를
열어도 장치 버튼이 안 뜨고, `/api/devices` 같은 값싼 호출이 **44초**가 걸렸다
(정상 0.85초).

**원인**: 프로파일러 탭의 `/api/processes`가 1회에 **60~90초** 걸린다(§9.15 —
프로세스마다 `mm->pgd`부터 페이지테이블을 직접 걸어 유저 메모리에서 cmdline을
읽는다). 이걸 웹 UI가 **4초 간격으로 자동 폴링**하고 있었고, 브라우저가 2개
붙어 있었다. 모든 backend 호출은 단일 워커 executor로 직렬화되므로(§9.8),
executor가 영원히 못 따라가는 백로그가 쌓이고 그 뒤에 줄 선 모든 요청이 밀렸다.
"4초마다 폴링"과 "1회 60~90초"의 조합이라 백로그는 시간이 갈수록 커지기만 한다.

**고친 것 2겹** (클라이언트만 고치면 이미 열려 있는 옛 탭이 그대로 재발시킨다 —
실제로 수정 후에도 태블릿의 옛 탭이 계속 폴링해 증상이 이어지는 걸 실측했다):

1. **클라이언트**: 프로세스 목록 자동 폴링 제거 → 탭 진입 시 1회 + 새로고침
   버튼(토폴로지 탭과 같은 방침). 조회 중에는 버튼을 비활성화하고 "조회 중...
   수십 초 걸릴 수 있음"을 표시한다(수십 초 무반응은 먹통으로 보인다). 연타로
   중복 요청이 쌓이는 것도 in-flight 플래그로 막는다.
2. **서버**: `list_processes()` 결과를 캐시. **TTL을 고정값으로 두면 안 된다** —
   처음에 30초로 뒀더니 조회 자체가 60~90초라 캐시가 만료되자마자 다음 조회가
   시작돼 executor를 여전히 100% 점유했다(실측으로 확인). 그래서
   **TTL = max(30초, 직전 조회 소요시간 × 3)**으로 직전 실측에 비례시켰다 —
   이 엔드포인트가 executor의 1/3 이상을 절대 못 쓰게 된다.

**결과**: `/api/devices` 44초 → 평상시 **0.82~0.87초**. 다만 옛 JS를 물고 있는
브라우저 탭이 계속 폴링하는 상태에서 15초 간격으로 120초를 재 보면 **주기적인
스파이크가 남는다**:

```
t=15s  16.06s   ← 수정 전에 이미 쌓여 있던 요청이 빠지는 구간
t=30s   0.84s
t=45s   0.86s
t=60s   0.84s
t=75s   0.82s
t=90s   0.82s
t=105s 18.34s   ← 캐시 만료 → 비싼 재조회가 도는 동안 그 뒤에 줄 섬
t=120s  0.87s
```

즉 서버 쪽 방어는 **"상시 마비"를 "주기적 스파이크"로 낮춘 것**이지 없앤 게
아니다 — 캐시가 만료될 때마다 누군가(여기선 옛 탭의 자동 폴링) 재조회를
촉발하면 그 조회 시간만큼은 뒤의 요청이 밀린다. 이 트리거 자체를 없애는 건
클라이언트 수정(자동 폴링 제거)이고, 그건 **해당 탭을 새로고침해야** 적용된다.
두 겹이 모두 필요한 이유가 여기서 실측으로 드러난다: 서버 캐시는 최악을
막고(44초 → 18초, 상시 → 주기적), 클라이언트 수정은 트리거를 없앤다.

**교훈**: 단일 워커로 직렬화되는 백엔드에서는 "느린 엔드포인트 1개"가 곧 전체
장애다. 새 엔드포인트를 추가할 때는 **1회 소요시간 × 폴링 빈도**가 워커 하나를
넘지 않는지 반드시 따져야 한다. 자동 폴링을 붙이기 전에 실제 소요시간부터 잰다.

**같이 발견한 것**: 서버 로그에 `ValueError: Value out of range: 4294967295`로
`GetEvents`/`StreamEvents`가 반복 실패한 기록이 남아 있었다(§9.13의 broadcast
NSID 문제). 현재 코드에서는 재현되지 않아 이미 고쳐진 것으로 확인했는데, 그때
붙인 회귀 테스트가 **파서까지만** 덮고 있어 정작 터졌던 protobuf 변환 경로는
비어 있었다 — `tests/test_events.py`에 그 경로를 덮는 테스트를 추가했다.

**추가로 드러난 진짜 원인**: 위 캐시를 `list_processes()`에만 달았더니 증상이
줄기만 하고 안 사라졌다. `get_profile()`(프로파일러 세션 스냅샷)이
`procinfo.list_processes()`를 **직접** 불러 캐시를 통째로 우회하고 있었고,
이건 `StreamProfile`이 **2초 간격**으로 부르는 경로였다 — 4초 폴링보다 더
나빴다. 그래서 캐시를 두 호출자의 공통 지점인 `DrgnBackend._proc_infos()`로
내렸다. **비싼 호출은 그 호출 지점이 아니라 가장 아래 공통 지점에서 캐시해야
호출자가 늘어도 자동으로 보호된다.**
