#!/bin/sh
# 定时维护：问答会话轮转/压缩/清理（每小时一次，具体是否动手由设置决定）
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
INSTALL_ROOT=$(cd "$HERE/.." && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"
LOG="$STATE_DIR/maint.log"
mkdir -p "$STATE_DIR"
BG_DB="${BG_DB:-$DATA_DIR/bunny.db}"
export BG_DB
PY="$INSTALL_ROOT/app/.venv/bin/python"
[ -x "$PY" ] || { echo "$(date '+%F %T') 找不到虚拟环境，跳过" >>"$LOG"; exit 0; }
OUT=$("$PY" "$INSTALL_ROOT/app/maint.py" run 2>&1)
echo "$(date '+%F %T') ${OUT:-（无输出）}" >>"$LOG"
printf '%s\n' "$OUT"
