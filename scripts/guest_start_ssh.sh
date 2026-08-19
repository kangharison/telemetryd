#!/bin/sh
# 게스트 안에서(9p+chroot로) dropbear SSH 서버를 띄운다 — 사용자가 태블릿/
# 호스트에서 직접 게스트에 SSH로 들어가 fio 등을 손으로 실행해보기 위함.
# DESIGN.md §9.7 참고. qemu_run_guest.sh가 이미 -netdev user,hostfwd=2222->22
# 로 SSH 포트를 열어둔 상태여야 하고(스크립트 자체가 이미 처리함), 게스트
# 안에서 eth0에 IP를 준 다음(§9.7) 이 스크립트를 실행한다.
#
# 사용법(게스트 셸에서, chroot 없이 최상위 셸에서 직접):
#   DROPBEAR_DIR=... SSHROOT_ETC_DIR=... KEYS_DIR=... ./guest_start_ssh.sh
# 세 디렉터리 모두 마운트된 9p 공유를 통해 게스트에서 보이는 경로여야 한다
# (hostroot=/mnt/host 아래 절대경로, 또는 ebpfout=/mnt/ebpf 아래 절대경로).
#
# 왜 -R(호스트키 자동생성)을 안 쓰는가: dropbear 2022.83에서 -R은 -r로 준
# 커스텀 경로를 무시하고 컴파일타임 기본 경로(/etc/dropbear/...)에 쓰려고
# 시도해 hostroot가 읽기전용이라 매번 실패한다(DESIGN.md §9.7에 재현 기록).
# 그래서 dropbearkey로 미리 만들고 dropbear는 -r로 "읽기"만 하게 한다.
set -eu

DROPBEAR_DIR="${DROPBEAR_DIR:?DROPBEAR_DIR(apt-get download dropbear-bin 등을 dpkg-deb -x로 푼 경로)를 지정해야 함}"
SSHROOT_ETC_DIR="${SSHROOT_ETC_DIR:?격리된 passwd/shadow/group 파일이 있는 디렉터리를 지정해야 함}"
KEYS_DIR="${KEYS_DIR:?dropbear 호스트키를 저장할 쓰기가능 디렉터리를 지정해야 함}"
SSH_PORT="${SSH_PORT:-22}"
LOG_FILE="${LOG_FILE:-/mnt/ebpf/dropbear.log}"

LIBP="$DROPBEAR_DIR/usr/lib/x86_64-linux-gnu"
DBK="$DROPBEAR_DIR/usr/bin/dropbearkey"
BIN="$DROPBEAR_DIR/usr/sbin/dropbear"

# 호스트의 진짜 /etc/passwd, /etc/shadow는 절대 건드리지 않는다 — chroot 뷰
# 안에서만 보이는 격리된 파일을 bind mount로 위에 덮어씌운다.
mount --bind "$SSHROOT_ETC_DIR/passwd" /mnt/host/etc/passwd
mount --bind "$SSHROOT_ETC_DIR/shadow" /mnt/host/etc/shadow
mount --bind "$SSHROOT_ETC_DIR/group"  /mnt/host/etc/group

mkdir -p "$KEYS_DIR"
if [ ! -f "$KEYS_DIR/dropbear_rsa_host_key" ]; then
  chroot /mnt/host env LD_LIBRARY_PATH="$LIBP" "$DBK" -t rsa -f "$KEYS_DIR/dropbear_rsa_host_key"
fi
if [ ! -f "$KEYS_DIR/dropbear_ed25519_host_key" ]; then
  chroot /mnt/host env LD_LIBRARY_PATH="$LIBP" "$DBK" -t ed25519 -f "$KEYS_DIR/dropbear_ed25519_host_key"
fi

# -B: 빈 비밀번호 로그인 허용(우리 shadow의 비밀번호 필드가 비어있음).
# root 로그인은 dropbear 기본 허용값이라 별도 플래그 불필요.
chroot /mnt/host env LD_LIBRARY_PATH="$LIBP" "$BIN" -F -E -B -p "$SSH_PORT" \
  -r "$KEYS_DIR/dropbear_rsa_host_key" \
  -r "$KEYS_DIR/dropbear_ed25519_host_key" \
  >> "$LOG_FILE" 2>&1 &

echo "dropbear started (pid $!), log: $LOG_FILE"
