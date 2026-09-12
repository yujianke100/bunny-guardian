#!/bin/sh
# 卸载（用户级）：停用并删除单元。默认保留数据，--purge 才会删数据目录。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"

PURGE=0
for a in "$@"; do [ "$a" = "--purge" ] && PURGE=1; done

echo "==> 停用用户级单元"
# 含已废弃的 sync.timer：同步现在没有定时器了，但老版本装过的机器也要能清干净
for u in "$APP_SLUG" "$APP_SLUG-sync.timer" "$APP_SLUG-sync.service"; do
  systemctl --user disable --now "$u" >/dev/null 2>&1 && echo "    已停用 $u" || true
  rm -f "$UNITS_DIR/$u"
done
systemctl --user daemon-reload 2>/dev/null || true

if [ "$PURGE" -eq 1 ]; then
  echo "==> 删除数据（$DATA_DIR、$AGENT_DIR、$CONFIG_DIR、$STATE_DIR、$INSTALL_ROOT）"
  rm -rf "$DATA_DIR" "$AGENT_DIR" "$CONFIG_DIR" "$STATE_DIR" "$INSTALL_ROOT"
  echo "    已删除。Node 与 pi 见 $NODE_DIR / $BIN_DIR（如需一并删除请手动）"
else
  echo "已卸载单元，数据保留在："
  echo "  数据   $DATA_DIR"
  echo "  配置   $CONFIG_DIR"
  echo "  代码   $INSTALL_ROOT"
  echo "（要一并删除：sh deploy/uninstall.sh --purge）"
fi
