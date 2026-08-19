#!/bin/sh
# 게스트 안에서(9p+chroot로) 서로 다른 워크로드의 fio 2개를 동시에 띄운다 —
# NVMe I/O 프로세스 프로파일러(대상 선택 일반화) 검증용.
#
# 왜 두 개인가: (a) 다중 대상 동시 관측, (b) 장치별 귀속/미귀속 계산,
# (c) fio 어댑터의 "기대 vs 실측" 대조를 서로 다른 옵션으로 확인하기 위함.
#   job A: seqread   nvme0, rw=read,      bs=128k, iodepth=8   (큰 I/O — 분할 여부 확인)
#   job B: randwrite nvme1, rw=randwrite, bs=4k,   iodepth=32  (작은 랜덤 쓰기)
#
# 사용법(게스트 셸에서):
#   chroot /mnt/host /home/harison/company/telemetryd/scripts/guest_fio_profile.sh &
set -eu

FIO_DIR="${FIO_DIR:-/tmp/claude-1000/-home-harison-company/a075a1e4-e200-48ee-adf8-6161b6183432/scratchpad/fio_extract}"
export LD_LIBRARY_PATH="$FIO_DIR/usr/lib/x86_64-linux-gnu"
RUNTIME="${FIO_RUNTIME:-1800}"
# [한국어] chroot 안에서 본 쓰기 가능한 9p 공유 경로. DESIGN.md §9.7의 레시피대로
# 게스트에서 `mount --bind /mnt/ebpf /mnt/host/mnt` 를 해두면 chroot 안에서는
# /mnt 로 보인다(게스트 최상위의 /mnt/ebpf 가 아니다 — 경로를 헷갈리면 fio가
# 출력 파일을 못 만들고 즉시 죽는다).
OUT_DIR="${FIO_OUT_DIR:-/mnt}"

"$FIO_DIR/usr/bin/fio" --name=seqread --filename=/dev/nvme0n1 --rw=read \
  --bs=128k --iodepth=8 --numjobs=1 --ioengine=libaio --direct=1 \
  --runtime="$RUNTIME" --time_based > "$OUT_DIR/fio_seqread.log" 2>&1 &

"$FIO_DIR/usr/bin/fio" --name=randwrite --filename=/dev/nvme1n1 --rw=randwrite \
  --bs=4k --iodepth=32 --numjobs=1 --ioengine=libaio --direct=1 \
  --runtime="$RUNTIME" --time_based > "$OUT_DIR/fio_randwrite.log" 2>&1 &

wait
