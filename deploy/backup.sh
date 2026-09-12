#!/bin/sh
# 定时备份：每小时触发一次，由应用根据「频率设置 + 上次备份时间」决定这次要不要真备
# （所以改频率不需要重写 systemd 单元）。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
INSTALL_ROOT=$(cd "$HERE/.." && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"

LOG="$STATE_DIR/backup.log"
mkdir -p "$STATE_DIR"
BG_DB="${BG_DB:-$DATA_DIR/bunny.db}"
BG_KB_DIR="${BG_KB_DIR:-$KB_DIR}"
export BG_DB BG_KB_DIR

PY="$INSTALL_ROOT/app/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%F %T') 找不到虚拟环境，跳过" >>"$LOG"
  exit 0
fi

OUT=$("$PY" "$INSTALL_ROOT/app/backup.py" run 2>&1)
echo "$(date '+%F %T') ${OUT:-（无输出）}" >>"$LOG"
printf '%s\n' "$OUT"
