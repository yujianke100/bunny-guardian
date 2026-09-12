#!/bin/sh
# 安装用户级 Node 与 pi（不需要 root，全部落在 $HOME/.local 下）
#   sh deploy/setup-node.sh          # 安装/更新 Node + pi
#   sh deploy/setup-node.sh --check  # 只检查现状
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"

if [ "${1:-}" = "--check" ]; then
  echo "Node 目录：$NODE_DIR"
  [ -x "$NODE_DIR/bin/node" ] && "$NODE_DIR/bin/node" -v || echo "  （未安装）"
  echo "pi 包装脚本：$BIN_DIR/pi"
  [ -x "$BIN_DIR/pi" ] && "$BIN_DIR/pi" --version 2>/dev/null || echo "  （未安装）"
  exit 0
fi

ARCH=$(uname -m); case "$ARCH" in x86_64|aarch64) : ;; *) echo "不支持的架构 $ARCH" >&2; exit 1;; esac
TGZ="node-v$NODE_VERSION-linux-$([ "$ARCH" = x86_64 ] && echo x64 || echo arm64).tar.xz"

echo "==> 下载 Node $NODE_VERSION（$NODE_MIRROR）"
mkdir -p "$(dirname "$NODE_DIR")"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
if ! curl -fsSL -o "$TMP/node.tar.xz" "$NODE_MIRROR/v$NODE_VERSION/$TGZ" 2>/dev/null; then
  curl -fsSL -o "$TMP/node.tar.xz" "https://nodejs.org/dist/v$NODE_VERSION/$TGZ"
fi
rm -rf "$NODE_DIR"; mkdir -p "$NODE_DIR"
tar -xJf "$TMP/node.tar.xz" -C "$NODE_DIR" --strip-components=1
"$NODE_DIR/bin/node" -v

echo "==> 安装 pi（全局装到用户目录）"
export PATH="$NODE_DIR/bin:$PATH"
"$NODE_DIR/bin/npm" config set registry "${NPM_REGISTRY:-https://registry.npmmirror.com}" >/dev/null 2>&1 || true
"$NODE_DIR/bin/npm" install -g --ignore-scripts "${PI_PACKAGE:-@earendil-works/pi-coding-agent}" 2>&1 | tail -3

echo "==> 包装脚本 $BIN_DIR/pi"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/pi" <<WRAP
#!/bin/sh
# pi 需要 Node >= 22；固定使用用户级 Node，避免误用系统里的旧版本
PATH="$NODE_DIR/bin:\$PATH"
export PATH
exec "$NODE_DIR/bin/pi" "\$@"
WRAP
chmod 755 "$BIN_DIR/pi"
"$BIN_DIR/pi" --version

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo
     echo "提示：把 $BIN_DIR 加入 PATH，例如在 ~/.profile 里加："
     echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
