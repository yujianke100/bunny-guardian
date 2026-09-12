#!/bin/sh
# 在 systemd **用户**沙箱里启动智能体（不需要 root）
#
#   sh deploy/pi/agent.sh -p "现在该注意什么？"
#   sh deploy/pi/agent.sh                      # 交互式
#
# 沙箱要点（用户级即可生效）：
#   - ProtectSystem=strict：/ 只读；$HOME 可写（本项目数据本就在 $HOME 下）
#   - 禁止提权、限制系统调用与地址族
#   - 注意：CapabilityBoundingSet / PrivateDevices 在**用户**单元下会以
#     status=218/CAPABILITIES 启动失败，因此不使用（实测逐个验证过）
#   - 内存 420M / CPU 120% / 单次最长 15 分钟
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$HERE/.." && pwd)
INSTALL_ROOT=$(cd "$DEPLOY_DIR/.." && pwd)
BG_DEPLOY_DIR="$DEPLOY_DIR"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$DEPLOY_DIR/config.sh"

if ! systemctl --user is-system-running >/dev/null 2>&1; then
  echo "注意：systemd 用户实例不可用，改为直接运行（无沙箱）。" >&2
  exec "$INSTALL_ROOT/deploy/pi/run.sh" "$@"
fi

exec systemd-run --user --quiet --collect --wait --pipe \
  --working-directory="$AGENT_DIR" \
  -p EnvironmentFile="$CONFIG_DIR/agent.env" \
  -p PrivateTmp=yes \
  -p ProtectSystem=strict \
  -p ProtectKernelTunables=yes \
  -p ProtectControlGroups=yes \
  -p NoNewPrivileges=yes \
  -p RestrictNamespaces=yes \
  -p RestrictAddressFamilies="AF_UNIX AF_INET AF_INET6 AF_NETLINK" \
  -p SystemCallFilter=@system-service \
  -p MemoryMax=420M \
  -p CPUQuota=120% \
  -p RuntimeMaxSec=900 \
  "$INSTALL_ROOT/deploy/pi/run.sh" "$@"
