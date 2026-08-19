# telemetryd CLI 사용 가이드

CLI는 **순수 라이브러리 import**로 동작한다 — gRPC 서버를 거치지 않고
`telemetryd.backend.get_backend()`를 직접 호출한다. 즉 **CLI만 쓸 거면 서버를
띄울 필요가 없다.**

```
CLI  ──(직접 import)──>  services/*  ──>  platform(drgn / eBPF)  ──>  커널
Web  ──(gRPC)──> 서버 ──>  services/*  ──>  platform ──────────────>  커널
```

웹 대시보드에서 보던 **모든 정보를 CLI로도 볼 수 있다.** 아래 대응표가 그
매핑이다.

| 웹 화면 | CLI 커맨드 |
|---|---|
| 장치 버튼 목록 | `devices` |
| Queue 탭 — 큐 스냅샷 표 | `snapshot <dev>` |
| Queue 탭 — 실시간 갱신 | `watch <dev>` |
| 큐 클릭 → SQ 엔트리/CDW | `queue <dev> <qid>` |
| 큐 클릭 → CQ 엔트리 | `cq <dev> <qid>` |
| "PRP 확인" 버튼 | `prp <dev> <qid> <cid>` |
| 포인터 트리 탐색기 | `tree <dev> [경로...]` |
| 성능 탭 — 큐별 IOPS/BW/지연 | `perf <dev>` |
| 성능 탭 — 실시간 + 시계열 그래프 | `perf <dev> --watch` |
| 이벤트 탭 — 이벤트 목록 | `events <dev>` |
| 이벤트 탭 — 종류 필터 | `events <dev> --kind timeout` |
| 이벤트 탭 — 누적 에러 카운터 | `error-stats <dev>` |
| 이벤트 탭 — 등록된 종류 카드 | `event-kinds` |
| 토폴로지 탭 | `topology` / `topology --details` |
| 프로파일러 탭 — 프로세스 목록 | `processes` |
| 프로파일러 탭 — 대상 지정/해제 | `target add` / `target remove` / `target list` |
| 프로파일러 탭 — 세션/기대 대조 | `profile` / `profile --watch` |
| (환경 진단 — 웹엔 없음) | `doctor` |

---

## 0. 백엔드 고르기 — 이게 가장 중요하다

```bash
# 기본: mock (합성 데이터). root도 커널도 필요 없다. 출력 형식 익히기/개발용.
telemetryd snapshot nvme0

# 실제 커널: --backend drgn + root
sudo -E .venv/bin/telemetryd --backend drgn snapshot nvme0
```

`sudo -E`의 `-E`가 중요하다 — 가상환경 경로 등 환경변수를 유지해야 한다.

전역 옵션은 **커맨드 앞**에 온다:

```bash
sudo -E .venv/bin/telemetryd --backend drgn --ebpf-log /var/log/nvme_perf.log perf nvme0
#                            └────── 전역 옵션 ──────┘                       └ 커맨드 ┘
```

| 전역 옵션 | 언제 쓰나 |
|---|---|
| `--backend drgn` | 실제 커널을 볼 때 (기본은 mock) |
| `--ebpf-log PATH` | `perf`/`events`/`error-stats`/`profile`에 필요 |
| `--extra-symbols PATH` | 커널 디버그 심볼이 표준 경로에 없을 때 (vmlinux 직접 지정) |
| `--qemu-qmp PATH` | QEMU 게스트를 밖에서 들여다볼 때만. **실기에서는 안 쓴다** |

---

## 1. 처음 실행: doctor 부터

```bash
sudo -E .venv/bin/telemetryd --backend drgn doctor
```

권한·심볼·수집기 상태를 한 번에 점검한다. **실기에서 처음 돌릴 때는 반드시
이걸 먼저** 하고, 통과하면 `snapshot` → 나머지 순으로 넓혀가는 걸 권한다
(로컬 drgn 경로는 이 저장소에서 검증되지 않았다 — README "실기에서 쓰기" 참고).

---

## 2. 장치와 큐 상태

```bash
telemetryd --backend drgn devices          # nvme0, nvme1 ...
telemetryd --backend drgn snapshot nvme0
```

`snapshot` 출력에서 봐야 할 것:

- `iommu=on/off` — **on이면 PRP 페이로드 덤프가 막힌다**(IOVA라서). 나머지
  기능은 영향 없다.
- 큐별 `sq_tail` / `cq_head` — 둘의 차이가 대략 미완료(in-flight) 양이다.
- `inflight drv/sched` — 드라이버 태그 / 스케줄러 태그 기준 미완료 수.

**admin 큐(qid=0)는 `sq_tail`과 `cq_head`가 늘 벌어져 있는 게 정상이다** —
비동기 이벤트 요청(AER)이 완료되지 않은 채 계속 매달려 있기 때문이다.

실시간으로 보려면:

```bash
telemetryd --backend drgn watch nvme0 --interval 0.5
```

---

## 3. 큐 안을 들여다보기 (SQ / CQ / PRP)

```bash
telemetryd --backend drgn queue nvme0 1            # 최근 제출 16개(CDW 전체)
telemetryd --backend drgn queue nvme0 1 --limit 4
telemetryd --backend drgn cq    nvme0 1            # 최근 완료 16개
```

기본은 **도어벨 바로 앞 최근 것부터** 보여준다(최신 → 과거). 링 버퍼 처음부터
보려면 `--from-start`.

`queue` 출력의 `flags=0x..(PRP|SGL)`가 데이터 포인터 방식이다. **PRP인 항목의
`cid`만** 페이로드를 덤프할 수 있다:

```bash
telemetryd --backend drgn prp nvme0 1 <cid>
```

> 커맨드는 매우 빨리 완료되므로, `queue`로 본 cid가 `prp` 시점엔 이미 다른
> 커맨드로 덮여 있을 수 있다. 그러면 "SQ 링에서 cid를 찾지 못함"이 나온다 —
> 실패가 아니라 타이밍이다. 몇 번 다시 시도하면 잡힌다.

SGL만 나온다면 커널이 SGL 경로를 쓰는 것이다. PRP를 강제하려면:

```bash
echo 0 | sudo tee /sys/module/nvme/parameters/sgl_threshold
```

---

## 4. 성능 — 큐별 IOPS / 대역폭 / 지연 percentile

**eBPF 수집기가 떠 있어야 한다:**

```bash
sudo bpftrace ebpf/nvme_perf.bt >> /var/log/nvme_perf.log 2>&1 &
```

```bash
sudo -E .venv/bin/telemetryd --backend drgn --ebpf-log /var/log/nvme_perf.log perf nvme0
```

```
 qid      iops    read/s   write/s   BW(MB/s)   avg(us)   p50(us)   p95(us)   p99(us)  p99.9(us)
   1      2900      1395      1505     118.78     825.0     701.2    1650.0    3300.0     6600.0
   2      3560      1802      1758     102.07     902.0     766.7    1804.0    3608.0     7216.0
------------------------------------------------------------------------------------------------
 ALL      6460      3197      3263     220.86     863.5     734.0    1727.0    3454.0     6908.0
```

- `ALL` 행은 전체 큐 합산이다. **개별 큐 percentile은 표본이 적어 들쭉날쭉할
  수 있으니, 꼬리 지연 판단은 ALL 행을 보는 게 안정적이다.**
- percentile은 **2배수 버킷 근사치**다(정확한 값이 아니라 "이 값 이하 버킷의
  상한"). bcc의 `biolatency`와 같은 방식이라 QoS 모니터링에는 충분하다.

`--watch`를 주면 1초마다 갱신되고 **오른쪽에 시계열 스파크라인**이 붙는다
(웹 성능 탭의 그래프에 대응):

```bash
... perf nvme0 --watch
 qid      iops   ...  p99.9(us) 추이(IOPS)
   1      2960   ...     6656.0 ▃▄▂▅▇█▆▄▃▅
```

스파크라인 눈금은 **그 창 안의 최대값 기준**이라 큐마다 규모가 달라도 추세가
보인다(절대량 비교용이 아니다).

---

## 5. 이벤트 — 타임아웃 / 에러 완료

```bash
telemetryd --backend drgn --ebpf-log /var/log/nvme_perf.log events nvme0
telemetryd ... events nvme0 --kind error      # 종류 필터
telemetryd ... events nvme0 --watch           # 실시간
telemetryd ... error-stats nvme0              # SCT/SC 조합별 누적
telemetryd event-kinds                        # 무엇을 수집 중인지
```

- **이벤트 0건이 정상이다.** 타임아웃은 거의 안 일어나고, 에러도 정상 장비면
  드물다.
- `event-kinds`의 `○ 미수집`은 "등록은 됐지만 지금 수집 안 됨"(수집기 미설정)을
  뜻한다. `--ebpf-log`를 주면 `● 수집중`으로 바뀐다.
- **에러가 더 중요한 신호다** — 이상 징후 대부분은 타임아웃까지 안 가고 에러
  status로 반환되며, 커널이 재시도로 흡수해 애플리케이션에서는 안 보인다.
- 이벤트 목록은 **종류를 가리지 않는다**(공통 봉투 구조). 상세 필드(CDW 등)는
  종류별로 다르게 표시된다.

---

## 6. 토폴로지 — PCIe 계보 + NVMe 서브시스템

```bash
telemetryd --backend drgn topology
telemetryd --backend drgn topology --details    # 각 노드 속성까지
```

같은 브리지 아래 붙은 장치는 조상 노드를 **공유**해서 나온다(실제 하드웨어
구조 그대로). drgn 조회가 장치·큐마다 반복돼 **수 초 걸린다** — 실시간
스트림이 아니라 필요할 때 한 번 보는 용도다.

---

## 7. 프로파일러 — 무엇이 이 SSD를 때리고 있나

관측 대상은 **런타임에 선택**한다. 특정 애플리케이션 전용이 아니다.

```bash
# 1) 지금 NVMe I/O를 내는 프로세스 보기
telemetryd ... processes

# 2) 대상 지정 (pid / 이름 / 패턴)
telemetryd ... target add --pid 1234
telemetryd ... target add --name fio
telemetryd ... target add --cmdline-pattern 'rw=randwrite'
telemetryd ... target list
telemetryd ... target remove --name fio      # 위치 인자도 가능: target remove name fio

# 3) 프로파일 보기
telemetryd ... profile
telemetryd ... profile --watch
```

`profile`이 보여주는 것:
- 세션별 **기대 vs 실측** 대조(fio라면 `--bs`/`--iodepth` 같은 옵션을 파싱해
  실제 I/O와 비교)
- **미관측 I/O** — 대상이 아닌 프로세스가 같은 장치를 때리고 있는지
- 큐 깊이는 리틀의 법칙(IOPS × 평균지연) **근사치**다 — 제출/완료가 다른
  컨텍스트라 직접 셀 수 없다.

> ⚠️ `processes`는 **비싸다**(프로세스마다 페이지테이블을 걸어 cmdline을 읽는다).
> 실측 수십 초까지 걸리므로 **짧은 주기로 반복 호출하지 말 것.** 웹에서도
> 같은 이유로 자동 폴링을 뺐다.

---

## 8. 자주 막히는 곳

| 증상 | 원인/해결 |
|---|---|
| `[경고] --backend drgn 은 root가 필요합니다` | `sudo -E`로 실행 |
| 심볼 관련 오류 | 커널 debuginfo 설치, 또는 `--extra-symbols /path/vmlinux` |
| `perf`가 "수집기 로그 없음" | bpftrace 미실행 또는 `--ebpf-log` 경로 누락 |
| bpftrace 실행 직후 로그가 빔 | 정상. 표준출력 버퍼링 때문에 첫 flush까지 수십 초 |
| PRP가 "IOMMU가 켜져 있어..." | 정상 동작. `intel_iommu=off`로 끄거나 PRP 덤프를 포기 |
| PRP가 "cid를 찾지 못함" | 커맨드가 이미 완료됨. 재시도 |
| 이벤트가 계속 0건 | 대체로 정상. `event-kinds`로 수집 여부부터 확인 |
| QEMU 검증 환경에서 CLI가 멈춤 | gRPC 서버가 QMP 소켓을 이미 점유. **QMP는 클라이언트 1개만** 받으므로 서버를 내리고 쓸 것. (실기 로컬 모드는 `/proc/kcore`라 이 제약이 없다) |
