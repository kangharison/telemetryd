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
