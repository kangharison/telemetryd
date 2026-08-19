#!/bin/bash
# telemetryd 검증용 QEMU 게스트 표준 기동 스크립트.
#
# 항상 8개 I/O 큐(admin 포함 9개)가 뜨도록 -smp 8 고정 — blk-mq가
# num_possible_cpus() 기준으로 I/O 큐를 만들기 때문(사용자 요청: "앞으로는
# queue 8개는 만드는 시스템으로"). NVMe 컨트롤러 2개(nvme0/nvme1)도 항상
# 같이 띄운다(DESIGN.md §9.4의 멀티 디바이스 검증과 동일 구성).
#
# 포함하는 것:
#   - -device vmcoreinfo, CONFIG_FW_CFG_SYSFS 커널  -> drgn --qemu(QMP) 라이브 접속(§9)
#   - -fsdev hostroot(읽기전용)                     -> 게스트 안에서 호스트 python3/drgn/fio/bpftrace를
#                                                      chroot로 빌려쓰기 위함(§9, §9.5, §9.6)
#   - -fsdev ebpfout(쓰기가능)                       -> bpftrace 성능 로그를 호스트로 빼내기 위함(§9.6)
#   - CONFIG_BPF_SYSCALL 등을 켠 커널               -> eBPF(bpftrace) 실행 가능(§9.6)
#   - -netdev user + hostfwd 2222->22               -> SSH로 게스트에 직접 접속(사용자가 fio를
#     직접 실행해보고 싶다고 요청). qemu-8.2.2는 원래 libslirp 없이 빌드돼 있어서
#     libslirp-dev를 apt-get download로 받아 재구성(-Dslirp=enabled)+재빌드했음.
#
# 사용법: ./scripts/qemu_run_guest.sh (company/qemu-debug 디렉터리 기준 경로를 씀)
set -euo pipefail

QEMU_DEBUG_DIR="${QEMU_DEBUG_DIR:-/home/harison/company/qemu-debug}"
SCRATCHPAD="${TELEMETRYD_SCRATCHPAD:?TELEMETRYD_SCRATCHPAD 환경변수로 스크래치 디렉터리를 지정해야 함}"
SSH_HOST_PORT="${SSH_HOST_PORT:-2222}"

cd "$QEMU_DEBUG_DIR"
tmux kill-session -t qemu-test 2>/dev/null || true
sleep 1
rm -f "$SCRATCHPAD/qmp.sock"

tmux new-session -d -s qemu-test -x 220 -y 50 "qemu-8.2.2/build/qemu-system-x86_64 \
  -accel tcg -smp 8 -m 2048 \
  -kernel linux-6.1.4/arch/x86/boot/bzImage -initrd initramfs.cpio.gz \
  -append 'console=ttyS0 nokaslr no_hash_pointers panic=-1' \
  -drive file=nvme.img,if=none,id=nvm0,format=raw \
  -device nvme,drive=nvm0,serial=deadbeef,id=nvme0 \
  -drive file=ns2.img,if=none,id=nvm1,format=raw \
  -device nvme,drive=nvm1,serial=beefdead,id=nvme1 \
  -device vmcoreinfo \
  -netdev user,id=net0,hostfwd=tcp::${SSH_HOST_PORT}-:22 \
  -device virtio-net-pci,netdev=net0 \
  -fsdev local,id=fs0,path=/,security_model=none,readonly=on \
  -device virtio-9p-pci,fsdev=fs0,mount_tag=hostroot \
  -fsdev local,id=fs1,path=$SCRATCHPAD/ebpf_out,security_model=none \
  -device virtio-9p-pci,fsdev=fs1,mount_tag=ebpfout \
  -nographic -no-reboot -s \
  -qmp unix:$SCRATCHPAD/qmp.sock,server,nowait"

echo "부팅 대기 중..."
sleep 8
tmux capture-pane -t qemu-test -p | tail -15

echo
echo "=== 게스트 마운트 설정 ==="
tmux send-keys -t qemu-test "mkdir -p /mnt/host /mnt/ebpf && \
  mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000,ro hostroot /mnt/host && \
  mount -t 9p -o trans=virtio,version=9p2000.L,msize=512000 ebpfout /mnt/ebpf && \
  mount -t proc none /mnt/host/proc && mount -t sysfs none /mnt/host/sys && \
  mount --bind /dev /mnt/host/dev && mount -t debugfs none /mnt/host/sys/kernel/debug && \
  mkdir -p /tmp && echo SETUP_OK" Enter
sleep 5
tmux capture-pane -t qemu-test -p | tail -10
