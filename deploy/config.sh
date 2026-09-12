#!/bin/sh
# 唯一配置源：所有部署脚本都 source 本文件。
#
# 设计目标（为脱敏与开源做准备）
#   - 全部落在用户目录内，安装不需要 root
#   - 路径、名称、端口、模型都不写死在脚本里；fork 后只改本文件（或写一份不提交的 config.local.sh）
#   - 不引用 /opt、/etc、/var/lib、/usr/local 等系统路径
#
# 覆盖方式（优先级从高到低）：
#   1) 环境变量            例如 APP_PORT=9000 sh deploy/install.sh
#   2) deploy/config.local.sh（本地覆盖，已在 .gitignore 中）
#   3) 下面的默认值
set -eu

# ---- 身份与命名（开源时改这里即可换成你自己的实例）----
APP_SLUG=${APP_SLUG:-bunny-guardian}              # 用于 unit 名、配置目录名、数据库文件名
APP_NAME=${APP_NAME:-"Bunny Guardian"}       # 页面标题与 unit Description

# ---- 路径：默认全部在 $HOME 下 ----
HOME_DIR=${HOME_DIR:-$HOME}
INSTALL_ROOT=${INSTALL_ROOT:-$HOME_DIR/$APP_SLUG}           # 代码（git 工作副本）
# 数据默认落在**项目文件夹内**（data/ 已在 .gitignore 中），备份=整个文件夹一起拷
DATA_DIR=${DATA_DIR:-$INSTALL_ROOT/data}                    # SQLite、上传附件、API Key
AGENT_DIR=${AGENT_DIR:-$INSTALL_ROOT/agent}                 # 智能体工作目录（只放净化后的指令）
CONFIG_DIR=${CONFIG_DIR:-$INSTALL_ROOT/config}              # 运行期配置（含令牌）
STATE_DIR=${STATE_DIR:-$INSTALL_ROOT/state}                 # 日志与智能体状态
# 单元作用域：user（默认，免 root）或 system（装到 /etc/systemd/system，以当前用户运行）
UNIT_SCOPE=${UNIT_SCOPE:-user}
# 注意：UNITS_DIR / WANTED_BY 与 NODE_DIR 必须在**读取本地覆盖之后**再推导，
# 否则 config.local.sh 里改的 UNIT_SCOPE 不会生效（曾因此把系统单元写进了家目录）。
NODE_VERSION=${NODE_VERSION:-22.19.0}
NODE_MIRROR=${NODE_MIRROR:-https://npmmirror.com/mirrors/node}

# ---- 服务 ----
APP_HOST=${APP_HOST:-127.0.0.1}
APP_PORT=${APP_PORT:-9810}
APP_MEMORY_MAX=${APP_MEMORY_MAX:-320M}
APP_CPU_QUOTA=${APP_CPU_QUOTA:-120%}

# ---- 仓库 ----
GIT_REMOTE=${GIT_REMOTE:-}                  # 系统代码远端（留空则用当前 checkout 的 origin）
GIT_BRANCH=${GIT_BRANCH:-main}
# 数据仓库（私人档案）：与系统代码严格分离
KB_DIR=${KB_DIR:-$HOME_DIR/kb}              # 数据仓库的工作副本
KB_REMOTE=${KB_REMOTE:-}                    # 数据仓库远端；留空则用 templates/kb 起步
KB_BRANCH=${KB_BRANCH:-main}
KB_SYNC_ENABLED=${KB_SYNC_ENABLED:-1}       # 是否启用同步（有改动时自动推送；网页里也能改）
# 数据仓库里自动提交时使用的身份（留空则用中性默认 bunny-guardian <bunny-guardian@localhost>）
GIT_AUTHOR_NAME=${GIT_AUTHOR_NAME:-}
GIT_AUTHOR_EMAIL=${GIT_AUTHOR_EMAIL:-}

# ---- 模型默认值（可在网页「模型接入配置」里改，这里只是初始值）----
LLM_BASE_URL=${LLM_BASE_URL:-http://127.0.0.1:8080/v1}
LLM_MODEL=${LLM_MODEL:-local-model}

# ---- 扩展（「里」：情侣空间 / AI 跑团）----
# 指向扩展所在目录列表（多个用 : 分隔）。留空＝只跑表。
# 例：EXT_DIR=$HOME_DIR/ex-bunny-guardian/extensions
EXT_DIR=${EXT_DIR:-}

# ---- 可选的外部反向代理（不属于本项目，按需替换）----
DOMAIN=${DOMAIN:-example.com}
VHOST_PORT=${VHOST_PORT:-8444}              # 反代到此端口，再由 proxy 指向 APP_PORT

# 本地覆盖：不要提交这个文件
# 注意：本文件通常是被 source 的，此时 $0 是**调用脚本**的路径而非本文件的路径，
# 因此调用方应显式导出 BG_DEPLOY_DIR=<deploy 目录>；否则退回 $0 推断。
_SELF_DIR=${BG_DEPLOY_DIR:-$(dirname "$0")}
if [ -r "$_SELF_DIR/config.local.sh" ]; then
  # shellcheck disable=SC1090
  . "$_SELF_DIR/config.local.sh"
fi
unset _SELF_DIR

# ---- 依赖作用域/本地覆盖的推导（必须在 config.local.sh 之后）----
if [ -z "${UNITS_DIR:-}" ]; then
  if [ "$UNIT_SCOPE" = "system" ]; then UNITS_DIR=/etc/systemd/system; else UNITS_DIR=$HOME_DIR/.config/systemd/user; fi
fi
if [ -z "${WANTED_BY:-}" ]; then
  if [ "$UNIT_SCOPE" = "system" ]; then WANTED_BY=multi-user.target; else WANTED_BY=default.target; fi
fi
if [ -z "${NODE_DIR:-}" ]; then NODE_DIR=$INSTALL_ROOT/tools/node-v${NODE_VERSION:-22.19.0}; fi
if [ -z "${BIN_DIR:-}" ]; then BIN_DIR=$INSTALL_ROOT/tools/bin; fi
# 通用医学知识（可再获取，不进数据仓库）：安装时从代码仓 knowledge/general 展开到本地。
# **必须放在本地覆盖之后**：它由 DATA_DIR 推导，写在前面会用到覆盖前的默认 DATA_DIR
# （实测因此把通用知识建到了 /root/<旧 APP_SLUG>/data/ 下）。
if [ -z "${KB_GENERAL_DIR:-}" ]; then KB_GENERAL_DIR=$DATA_DIR/kb-general; fi

export APP_SLUG APP_NAME INSTALL_ROOT DATA_DIR AGENT_DIR CONFIG_DIR UNITS_DIR STATE_DIR \
       NODE_DIR BIN_DIR NODE_VERSION NODE_MIRROR APP_HOST APP_PORT APP_MEMORY_MAX \
       APP_CPU_QUOTA GIT_REMOTE GIT_BRANCH KB_DIR KB_REMOTE KB_BRANCH \
       UNIT_SCOPE UNITS_DIR WANTED_BY KB_SYNC_ENABLED KB_GENERAL_DIR \
       LLM_BASE_URL LLM_MODEL DOMAIN VHOST_PORT GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL EXT_DIR

# 供脚本显示用
config_summary() {
  cat <<EOF
  APP_SLUG      = $APP_SLUG
  APP_NAME      = $APP_NAME
  代码目录      = $INSTALL_ROOT
  数据仓库      = $KB_DIR${KB_REMOTE:+（远端 $KB_REMOTE）}
  单元作用域    = $UNIT_SCOPE（$UNITS_DIR）
  数据目录      = $DATA_DIR
  配置目录      = $CONFIG_DIR
  智能体目录    = $AGENT_DIR
  systemd 用户单元 = $UNITS_DIR
  监听          = $APP_HOST:$APP_PORT
  Node          = $NODE_DIR（$NODE_VERSION）
  扩展（里）    = ${EXT_DIR:-（未挂）}
EOF
}
