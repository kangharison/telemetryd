"""telemetryd 플랫폼 계층 — 도메인 지식이 전혀 없는 기반 설비.

서비스(services/*)가 "무엇을 보여줄지"를 담당한다면, 플랫폼은 **"어떻게 커널에
닿을지"**만 담당한다. NVMe/큐/이벤트 같은 도메인 어휘가 이 아래로 내려오면 안
된다 — 그 순간 플랫폼이 특정 서비스에 묶여서 재사용이 깨진다.

구성:
  platform.kernel  — drgn 세션(Program 생명주기·심볼 로딩·접속 방식) 추상화
  platform.ebpf    — bpftrace 로그 읽기(전체/꼬리/증분 커서) 추상화
  platform.cache   — TTL 캐시(비싼 조회를 공통 지점에서 보호, DESIGN.md §9.16)

의존 방향은 한 방향뿐이다:

    services/*  ->  platform/*        (O)
    platform/*  ->  services/*        (X — 절대 금지)

각 플랫폼 설비는 **포트(Protocol)**로 규정하고 구현(어댑터)을 갈아끼울 수
있게 한다(Ports & Adapters). 그래서 서비스 테스트는 drgn이나 실제 로그 파일
없이도 가짜 어댑터로 돌릴 수 있다.
"""
