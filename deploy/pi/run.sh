#!/bin/sh
# 启动智能体（用户级；只加载本项目的白名单扩展，内置文件/命令工具不在白名单内）
#
# 路径与配置全部来自 deploy/config.sh，不写死任何系统路径。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
DEPLOY_DIR=$(cd "$HERE/.." && pwd)
INSTALL_ROOT=$(cd "$DEPLOY_DIR/.." && pwd)
BG_DEPLOY_DIR="$DEPLOY_DIR"
export BG_DEPLOY_DIR
# shellcheck disable=SC1090
. "$DEPLOY_DIR/config.sh"

ENV_FILE=${BG_AGENT_ENV:-$CONFIG_DIR/agent.env}
if [ -r "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

if [ -z "${BG_AGENT_TOKEN:-}" ]; then
  echo "未配置 BG_AGENT_TOKEN。" >&2
  echo "1) 在网页「管理 → 智能体权限」生成令牌" >&2
  echo "2) 写入 $ENV_FILE（权限 600），内容形如：" >&2
  echo "   BG_AGENT_URL=http://$APP_HOST:$APP_PORT" >&2
  echo "   BG_AGENT_TOKEN=..." >&2
  echo "   BG_DB=$DATA_DIR/bunny.db" >&2
  exit 2
fi

BG_AGENT_URL=${BG_AGENT_URL:-http://$APP_HOST:$APP_PORT}
BG_DB=${BG_DB:-$DATA_DIR/bunny.db}
# 环境文件里可能残留旧路径（搬迁后最常见的问题）：不存在就回退并明确告警
if [ ! -f "$BG_DB" ]; then
  echo "[提示] 环境文件里配置的数据库不存在：$BG_DB" >&2
  echo "       改用 $DATA_DIR/bunny.db（请同步修正 $ENV_FILE 里的 BG_DB）" >&2
  BG_DB="$DATA_DIR/bunny.db"
fi
export BG_AGENT_URL BG_DB

VENV_PY="$INSTALL_ROOT/app/.venv/bin/python"
AGENT_HOME=${BG_AGENT_HOME:-$STATE_DIR/agent-home}
umask 077
mkdir -p "$AGENT_HOME/.pi/agent" 2>/dev/null || true
MODELS_JSON="$AGENT_HOME/.pi/agent/models.json"

# 端点检查：公网端点只给警告（是否使用由用户决定）
if [ -x "$VENV_PY" ]; then
  "$VENV_PY" "$INSTALL_ROOT/app/llm_config.py" check-agent-endpoint 2>/dev/null | while IFS= read -r line; do
    w=$(printf '%s' "$line" | sed -n 's/.*"warning": "\([^"]*\)".*/\1/p')
    [ -n "$w" ] && printf '\n\033[33m[提示] %s\033[0m\n\n' "$w" >&2
    printf '%s' "$line" | sed -n 's/.*"base_url": "\([^"]*\)".*/[模型端点] \1/p' >&2
  done
  # 模型清单与模型名都取自网页配置
  "$VENV_PY" "$INSTALL_ROOT/app/llm_config.py" models-json > "$MODELS_JSON" 2>/dev/null || true
  CONFIG_MODEL=$("$VENV_PY" "$INSTALL_ROOT/app/llm_config.py" current-model 2>/dev/null | head -1 | tr -d '\r\n')
fi
if [ ! -s "$MODELS_JSON" ]; then
  # 兜底：用 deploy/config.sh 里的模型默认值现生成一份（不依赖任何站点专属文件）
  cat > "$MODELS_JSON" <<JSON
{
  "providers": {
    "bg": {
      "name": "健康档案模型",
      "baseUrl": "${LLM_BASE_URL:-http://127.0.0.1:8080/v1}",
      "api": "openai-completions",
      "apiKey": "${BG_LLM_API_KEY:-none}",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false,
        "maxTokensField": "max_tokens",
        "thinkingFormat": "qwen-chat-template"
      },
      "models": [{ "id": "${LLM_MODEL:-local-model}" }]
    }
  }
}
JSON
  echo "提示：未能从网页配置生成模型清单，已按 deploy/config.sh 的默认值生成。" >&2
fi
AGENT_MODEL=${CONFIG_MODEL:-${BG_AGENT_MODEL:-$LLM_MODEL}}
HOME="$AGENT_HOME"
export HOME

# 工作目录：只放净化后的指令，避免把仓库里的部署细节带入提示词
cd "$AGENT_DIR"

if [ "${BG_AGENT_CHECK_ONLY:-0}" = "1" ]; then
  KEYLEN=$(sed -n 's/.*"apiKey": "\([^"]*\)".*/\1/p' "$MODELS_JSON" | head -1 | wc -c)
  echo "[干跑] 代码目录=$INSTALL_ROOT"
  echo "[干跑] 工作目录=$AGENT_DIR（净化指令）"
  echo "[干跑] 模型清单=$MODELS_JSON（密钥长度 ${KEYLEN}，内容不回显；权限 $(stat -c %a "$MODELS_JSON" 2>/dev/null || echo '?'))"
  sed -e 's/"apiKey": "[^"]*"/"apiKey": "***"/' "$MODELS_JSON" | head -12
  echo "[干跑] 模型=$AGENT_MODEL  后端=$BG_AGENT_URL"
  echo "[干跑] 工具白名单=health_context,record_query,kb_search,web_search,web_fetch,submit_record,list_pending"
  exit 0
fi

PATH="$NODE_DIR/bin:$BIN_DIR:$PATH"
export PATH
exec pi \
  --no-session \
  --provider bg \
  --model "$AGENT_MODEL" \
  -e "$INSTALL_ROOT/deploy/pi/extensions/health-tools.ts" \
  --tools health_context,record_query,kb_search,web_search,web_fetch,submit_record,list_pending \
  "$@"
