#!/bin/sh
# 手动同步：调用应用内的统一实现（与网页「立即同步」共用同一份逻辑与凭据）。
#
# 定时器已取消（同步只在有改动时自动触发），这个脚本留给手动/排障使用：
#   sh deploy/sync.sh
#
# 凭据来自应用自己管理的部署密钥（<数据目录>/ssh/id_ed25519）
# 或访问令牌（<数据目录>/sync_token），不再依赖运行账号的环境凭据。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
INSTALL_ROOT=$(cd "$HERE/.." && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"

LOG="$STATE_DIR/sync.log"
mkdir -p "$STATE_DIR"

# 应用内的 CLI 需要与应用服务一致的环境（数据目录 = SQLite 位置，知识库 = 数据仓库工作副本）
BG_DB="${BG_DB:-$DATA_DIR/bunny.db}"
BG_KB_DIR="${BG_KB_DIR:-$KB_DIR}"
export BG_DB BG_KB_DIR

PY="$INSTALL_ROOT/app/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%F %T') 找不到虚拟环境，跳过同步" >>"$LOG"
  exit 0
fi

OUT=$("$PY" "$INSTALL_ROOT/app/sync_job.py" run 2>&1)
echo "$(date '+%F %T') ${OUT:-（无输出）}" >>"$LOG"
printf '%s\n' "$OUT"
