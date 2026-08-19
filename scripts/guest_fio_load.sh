#!/bin/sh
# 게스트 안에서(9p+chroot로) 실행하는 fio 부하 — 실시간 성능(eBPF) 확인용.
# telemetryd/DESIGN.md §9.5와 동일한 fio 바이너리/스텁 라이브러리를 쓴다.
#
# 사용법(게스트 셸에서):
#   chroot /mnt/host /home/harison/company/telemetryd/scripts/guest_fio_load.sh &
#
# numjobs=8 로 8개 I/O 큐를 골고루 다 쓰게 한다(사용자 요청: "queue 8개는
# 만드는 시스템으로"). bs=16k는 sgl_threshold(32768) 미만이라 PRP 경로 위주로
# 나온다(완전히 PRP만 강제하려면 게스트에서 별도로
# `echo 0 > /sys/module/nvme/parameters/sgl_threshold` 실행).
set -eu

# [한국어] 이 스크립트는 QEMU 검증 환경 재현용이다 — 실기에서는 쓸 일이 없고,
# 부하가 필요하면 fio를 평소대로 직접 실행하면 된다(README "실기에서 쓰기" 참고).
# 검증 환경에서는 `apt-get download fio` 후 `dpkg-deb -x`로 푼 디렉터리를 준다.
# 기본값을 두지 않는 이유: 예전엔 검증 세션의 임시 경로가 기본값이었는데, 그
# 경로는 세션이 끝나면 사라져서 나중에 조용히 깨진다. 차라리 즉시 실패시킨다.
FIO_DIR="${FIO_DIR:?FIO_DIR 환경변수로 fio 추출 경로를 지정해야 한다 (예: /opt/fio_extract)}"
export LD_LIBRARY_PATH="$FIO_DIR/usr/lib/x86_64-linux-gnu"

exec "$FIO_DIR/usr/bin/fio" \
  --name=live --filename=/dev/nvme0n1 --rw=randrw \
  --bs=16k --iodepth=16 --numjobs=8 --ioengine=libaio --direct=1 \
  --runtime="${FIO_RUNTIME:-600}" --time_based --group_reporting
