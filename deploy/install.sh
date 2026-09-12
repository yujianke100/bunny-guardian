#!/bin/sh
# 安装 / 更新：全部在用户目录内完成，**不需要 root**。
#
#   sh deploy/install.sh              # 安装或更新
#   sh deploy/install.sh --no-deps    # 只更新代码与单元，不动 Python 依赖
#   sh deploy/install.sh --print      # 只打印将使用的配置
#
# 前置：已把本仓库克隆到 $INSTALL_ROOT（或本脚本所在的仓库就是 $INSTALL_ROOT）
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
BG_DEPLOY_DIR="$HERE"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$HERE/config.sh"

REPO_DIR=$(cd "$HERE/.." && pwd)
SKIP_DEPS=0
for a in "$@"; do
  [ "$a" = "--no-deps" ] && SKIP_DEPS=1
  if [ "$a" = "--print" ]; then config_summary; exit 0; fi
done

echo "==> 配置"
config_summary

echo "==> 1/6 目录"
mkdir -p "$DATA_DIR" "$DATA_DIR/uploads" "$AGENT_DIR" "$CONFIG_DIR" "$UNITS_DIR" "$STATE_DIR" "$BIN_DIR"
chmod 700 "$DATA_DIR" "$CONFIG_DIR" 2>/dev/null || true

echo "==> 2/6 代码"
if [ -d "$INSTALL_ROOT/.git" ] && [ "$INSTALL_ROOT" != "$REPO_DIR" ]; then
  git -C "$INSTALL_ROOT" fetch --quiet origin "$GIT_BRANCH" || true
  git -C "$INSTALL_ROOT" merge --ff-only --quiet "origin/$GIT_BRANCH" || \
    echo "    （本地有未推送提交，跳过快进）"
elif [ "$INSTALL_ROOT" != "$REPO_DIR" ]; then
  git clone --quiet "${GIT_REMOTE:?请设置 GIT_REMOTE，或先把仓库克隆到 $INSTALL_ROOT}" "$INSTALL_ROOT"
fi

echo "==> 2.8/6 数据仓库（私人档案，与代码分离）"
if [ -d "$KB_DIR/.git" ]; then
  git -C "$KB_DIR" fetch --quiet origin "$KB_BRANCH" 2>/dev/null || true
  git -C "$KB_DIR" merge --ff-only --quiet "origin/$KB_BRANCH" 2>/dev/null || true
  echo "    已更新已有数据仓库：$KB_DIR"
elif [ -n "$KB_REMOTE" ]; then
  git clone --quiet "$KB_REMOTE" "$KB_DIR"
  echo "    已从远端克隆数据仓库：$KB_DIR"
elif [ ! -d "$KB_DIR/docs" ]; then
  mkdir -p "$KB_DIR"
  cp -r "$INSTALL_ROOT/templates/kb/." "$KB_DIR/"
  [ -f "$KB_DIR/CLAUDE.md.tmpl" ] && mv "$KB_DIR/CLAUDE.md.tmpl" "$KB_DIR/CLAUDE.md"
  echo "    未配置 KB_REMOTE，已用 templates/kb 建了一个空的起步知识库：$KB_DIR"
else
  echo "    使用已有目录：$KB_DIR"
fi

echo "==> 2.9/6 通用医学知识（可再获取，单独放本地，不进数据仓库）"
mkdir -p "$KB_GENERAL_DIR"
_seeded=0
for f in "$INSTALL_ROOT"/knowledge/general/医学知识/*.md; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  if [ ! -e "$KB_GENERAL_DIR/医学知识/$name" ]; then
    mkdir -p "$KB_GENERAL_DIR/医学知识"
    cp "$f" "$KB_GENERAL_DIR/医学知识/$name"
    _seeded=$((_seeded + 1))
  fi
done
echo "    通用知识目录：$KB_GENERAL_DIR（本次补齐 $_seeded 篇；已有文件不覆盖）"

echo "==> 3/6 智能体指令目录（净化版，不含部署路径与令牌）"
install -m 644 "$INSTALL_ROOT/deploy/pi/agent-context/instructions.md" "$AGENT_DIR/CLAUDE.md"

# 站点专属敏感词清单：不存在时用示例初始化（之后自行编辑，不进 git）
if [ ! -f "$DATA_DIR/scrub_terms.txt" ]; then
  install -m 600 "$INSTALL_ROOT/deploy/scrub-terms.example.txt" "$DATA_DIR/scrub_terms.txt"
  echo "    已初始化 $DATA_DIR/scrub_terms.txt（请按自己的环境补充词条）"
fi

echo "==> 4/6 Python 环境"
# 搬过目录的 venv：bin/python 只是指向系统解释器的符号链接（仍有效），
# 但 bin/pip 等脚本的 shebang 写死旧路径 → 必须用「能否执行」来判断，而不是看 python 是否在。
if [ -d "$INSTALL_ROOT/app/.venv" ] && ! "$INSTALL_ROOT/app/.venv/bin/pip" --version >/dev/null 2>&1; then
  echo "    检测到 venv 不可用（目录搬迁导致 shebang 失效），重建"
  rm -rf "$INSTALL_ROOT/app/.venv"
fi
if ! python3 -c 'import ensurepip' 2>/dev/null; then
  echo "    缺少 python3-venv/ensurepip，请先安装（如 apt install python3-venv）" >&2
  exit 1
fi
if [ "$SKIP_DEPS" -eq 0 ]; then
  python3 -m venv "$INSTALL_ROOT/app/.venv"
  "$INSTALL_ROOT/app/.venv/bin/pip" install --quiet --upgrade pip
  "$INSTALL_ROOT/app/.venv/bin/pip" install --quiet -r "$INSTALL_ROOT/app/requirements.txt"
  echo "    依赖已安装：$("$INSTALL_ROOT/app/.venv/bin/python" -c 'import fastapi;print("fastapi",fastapi.__version__)')"
else
  echo "    已跳过（--no-deps）"
fi

echo "==> 5/6 systemd 用户单元"
render() {   # render 模板 → 目标文件，替换 @@占位符@@
  src=$1; dst=$2
  sed -e "s|@@APP_SLUG@@|$APP_SLUG|g" \
      -e "s|@@APP_NAME@@|$APP_NAME|g" \
      -e "s|@@INSTALL_ROOT@@|$INSTALL_ROOT|g" \
      -e "s|@@DATA_DIR@@|$DATA_DIR|g" \
      -e "s|@@CONFIG_DIR@@|$CONFIG_DIR|g" \
      -e "s|@@STATE_DIR@@|$STATE_DIR|g" \
      -e "s|@@KB_DIR@@|$KB_DIR|g" \
      -e "s|@@KB_GENERAL_DIR@@|$KB_GENERAL_DIR|g" \
      -e "s|@@GIT_AUTHOR_NAME@@|$GIT_AUTHOR_NAME|g" \
      -e "s|@@GIT_AUTHOR_EMAIL@@|$GIT_AUTHOR_EMAIL|g" \
      -e "s|@@AGENT_DIR@@|$AGENT_DIR|g" \
      -e "s|@@APP_HOST@@|$APP_HOST|g" \
      -e "s|@@APP_PORT@@|$APP_PORT|g" \
      -e "s|@@APP_MEMORY_MAX@@|$APP_MEMORY_MAX|g" \
      -e "s|@@APP_CPU_QUOTA@@|$APP_CPU_QUOTA|g" \
      -e "s|@@LLM_BASE_URL@@|$LLM_BASE_URL|g" \
      -e "s|@@LLM_MODEL@@|$LLM_MODEL|g" \
      -e "s|@@WANTED_BY@@|$WANTED_BY|g" \
      -e "s|@@EXT_DIR@@|${EXT_DIR:-}|g" "$src" > "$dst"
}
for t in "$INSTALL_ROOT"/deploy/systemd-user/*.service "$INSTALL_ROOT"/deploy/systemd-user/*.timer; do
  [ -e "$t" ] || continue
  render "$t" "$UNITS_DIR/$(basename "$t" | sed "s|APP_SLUG|$APP_SLUG|g")"
done
if [ "$UNIT_SCOPE" = "system" ]; then
  SCTL="systemctl"
else
  SCTL="systemctl --user"
fi
$SCTL daemon-reload
# 同步不再有定时器（只在有改动时推送）。定时器单元已从仓库删除，
# 这里负责把**老版本装过的**定时器停掉并删掉，避免升级后还在定期跑。
$SCTL disable --now "$APP_SLUG-sync.timer" >/dev/null 2>&1 || true
if [ -e "$UNITS_DIR/$APP_SLUG-sync.timer" ]; then
  rm -f "$UNITS_DIR/$APP_SLUG-sync.timer"
  $SCTL daemon-reload
  echo "    已移除旧的定时同步单元（同步现在只在有改动时触发）"
fi
$SCTL enable --now "$APP_SLUG-backup.timer" >/dev/null 2>&1 || \
  echo "    （定时备份单元未启用，稍后手动：$SCTL enable --now $APP_SLUG-backup.timer）"
$SCTL enable --now "$APP_SLUG-maint.timer" >/dev/null 2>&1 || \
  echo "    （会话维护单元未启用，稍后手动：$SCTL enable --now $APP_SLUG-maint.timer）"
$SCTL enable "$APP_SLUG.service" >/dev/null 2>&1 || true
$SCTL restart "$APP_SLUG.service"

echo "==> 6/6 健康检查"
# 用 /livez（存活探针，匿名可访问且不含任何配置细节）。
# /healthz 是详细健康信息，要求登录，安装脚本不去碰它。
i=0
while [ $i -lt 20 ]; do
  if curl -sf "http://$APP_HOST:$APP_PORT/livez" >/dev/null 2>&1; then
    echo "    就绪（进程在应答）"
    break
  fi
  i=$((i + 1)); sleep 1
  if [ $i -ge 20 ]; then
    if [ "$UNIT_SCOPE" = "system" ]; then
      echo "    启动失败，请查看：journalctl -u $APP_SLUG -n 50" >&2
    else
      echo "    启动失败，请查看：journalctl --user -u $APP_SLUG -n 50" >&2
    fi
    exit 1
  fi
done

cat <<EOF

完成（全部在用户目录内，无需 root）。

常用命令（作用域：$UNIT_SCOPE）：
  $SCTL status $APP_SLUG             # 状态
  $SCTL restart $APP_SLUG            # 重启
  $SCTL list-timers | grep $APP_SLUG # 定时器（备份/维护）

让用户级服务在退出登录后继续运行（可选，需要 root 一次性执行）：
  sudo loginctl enable-linger $(id -un)

外部反向代理（可选，本项目不负责）：把 https://$DOMAIN 反代到
  http://$APP_HOST:$APP_PORT
参考 deploy/nginx-vhost.conf.example。
EOF
