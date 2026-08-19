#!/bin/bash
# 실제 QEMU 게스트 커널로 DrgnBackend를 검증하는 커맨드 모음.
# 전제조건(DESIGN.md §9): 게스트가 `-device vmcoreinfo`로 떠 있고,
# `-qmp unix:$QMP_SOCK,server,nowait`로 QMP 유닉스 소켓이 열려 있고,
# 게스트 커널이 CONFIG_FW_CFG_SYSFS=y + CONFIG_KEXEC=y 로 빌드돼 있어야 한다.
# QMP는 TCP로는 동작하지 않는다(vmcoreinfo를 SCM_RIGHTS로 받아야 해서).
#
# 사용법:
#   QMP_SOCK=/tmp/qmp.sock VMLINUX=/path/to/vmlinux DEVICE=nvme0 ./scripts/qemu_verify.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${QMP_SOCK:?QMP_SOCK 환경변수 필요 (예: /tmp/qmp.sock)}"
: "${VMLINUX:?VMLINUX 환경변수 필요 (게스트가 부팅한 커널과 정확히 같은 빌드)}"
DEVICE="${DEVICE:-nvme0}"

TD=".venv/bin/telemetryd --backend drgn --qemu-qmp $QMP_SOCK --qemu-vmlinux $VMLINUX"

echo "=== doctor ==="
$TD doctor
echo
echo "=== devices ==="
$TD devices
echo
echo "=== snapshot $DEVICE ==="
$TD snapshot "$DEVICE"
echo
echo "=== tree $DEVICE (루트) ==="
$TD tree "$DEVICE"
