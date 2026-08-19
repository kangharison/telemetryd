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

FIO_DIR="${FIO_DIR:-/tmp/claude-1000/-home-harison-company/a075a1e4-e200-48ee-adf8-6161b6183432/scratchpad/fio_extract}"
export LD_LIBRARY_PATH="$FIO_DIR/usr/lib/x86_64-linux-gnu"

exec "$FIO_DIR/usr/bin/fio" \
  --name=live --filename=/dev/nvme0n1 --rw=randrw \
  --bs=16k --iodepth=16 --numjobs=8 --ioengine=libaio --direct=1 \
  --runtime="${FIO_RUNTIME:-600}" --time_based --group_reporting
