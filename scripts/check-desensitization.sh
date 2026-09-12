#!/bin/sh
# 脱敏自检：扫描**已纳入版本控制**的文件，找出与个人/部署环境绑定的信息。
#
#   sh scripts/check-desensitization.sh            # 只报告
#   sh scripts/check-desensitization.sh --strict   # 有命中则退出码 1（可用于 CI / 开源前把关）
#
# 两类规则：
#   1) 通用规则（写在本文件里，任何项目都适用，不含任何个人词条）
#   2) 个人词条（从 scripts/desensitize-patterns.local.txt 读取，**该文件不进仓库**）
#      格式：一行一个正则片段，# 之后是注释。示例见 desensitize-patterns.example.txt
#
# 另支持白名单：deploy/desensitize-allow.txt（每行一个路径前缀）。
set -u
cd "$(dirname "$0")/.."
STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

ALLOW_FILE=deploy/desensitize-allow.txt
LOCAL_PATTERNS=scripts/desensitize-patterns.local.txt

# 严格规则（--strict 会因它失败）：真实秘密值 + 邮箱。不要把绝对路径写进来——
# 文档里举例子必然出现路径，那是正常的，放到下面的提示规则里。
STRICT_PATTERNS='\b(bg|sk|ghp|github_pat|xox[bp])[_-][A-Za-z0-9_-]{8,}|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
# 提示规则（只报告，不影响退出码）：绝对路径、主机名/IP
INFO_PATTERNS='/(opt|srv|var/lib|etc|usr/local|home)/[A-Za-z0-9._/-]{3,}|\b\d{1,3}(?:\.\d{1,3}){3}\b'

# 个人词条（可选）
PERSONAL=""
if [ -f "$LOCAL_PATTERNS" ]; then
  PERSONAL=$(grep -v '^[[:space:]]*#' "$LOCAL_PATTERNS" | grep -v '^[[:space:]]*$' | paste -sd'|' -)
fi

if [ -z "$PERSONAL" ]; then
  echo "提示：未找到 $LOCAL_PATTERNS，本轮只做通用规则扫描。"
  echo "      开源前请从 desensitize-patterns.example.txt 复制一份并填入自己的词条（不会进 git）。"
  echo
  PATTERNS="$STRICT_PATTERNS"
else
  PATTERNS="$STRICT_PATTERNS|$PERSONAL"
fi
REPORT_PATTERNS="$INFO_PATTERNS"

files=$(git ls-files | while read -r f; do
  if [ -f "$ALLOW_FILE" ] && grep -qF "$f" "$ALLOW_FILE" 2>/dev/null; then continue; fi
  case "$f" in *.png|*.jpg|*.jpeg|*.gif|*.pdf|*.woff*|*.ico) continue;; esac
  echo "$f"
done)

echo "扫描 $(printf '%s\n' "$files" | grep -c .) 个受版本控制的文件…"
echo

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
STRICT_HITS=$(mktemp); trap 'rm -f "$tmp" "$STRICT_HITS"' EXIT
SHOW_INFO=0
[ "${1:-}" = "--all" ] && SHOW_INFO=1
printf '%s\n' "$files" | while IFS= read -r f; do
  [ -n "$f" ] || continue
  # 行内出现 desensitize-ok 的行视为有意保留（例如测试里的假密钥），不参与匹配
  # 排除：行内豁免标记；以及 SSH/git 主地址形式（git@host、git@host:path、git@host/path
  # 会被邮箱规则误判——它们不是邮箱，保留这类写法是有用的提示文本）
  hits=$(grep -nEI "$PATTERNS" "$f" 2>/dev/null | grep -v "desensitize-ok" | grep -vE "git@[A-Za-z0-9.-]+(:|/|[^A-Za-z0-9._-]|$)" | head -20) || true
  if [ -n "$hits" ]; then
    echo "── $f"
    printf '%s\n' "$hits" | sed 's/^/    /' | cut -c1-160
    echo "$f" >> "$STRICT_HITS" 2>/dev/null || true
  elif [ "$SHOW_INFO" -eq 1 ]; then
    info=$(grep -nEI "$REPORT_PATTERNS" "$f" 2>/dev/null | grep -v "desensitize-ok" | grep -vE "git@[A-Za-z0-9.-]+(:|/|[^A-Za-z0-9._-]|$)" | head -5) || true
    [ -n "$info" ] && { echo "·· $f（仅提示：路径/IP 等）"; printf '%s\n' "$info" | sed 's/^/    /' | cut -c1-160; }
  fi
done > "$tmp"

if [ -s "$tmp" ]; then
  cat "$tmp"
  echo
  echo "命中文件数：$(grep -c '^──' "$tmp")"
  echo "提醒：个人记录文档、姓名、域名、IP、令牌与站点专属路径都不应进入公开仓库；"
  echo "      参见 docs/open-source-checklist.md。"
  [ "$STRICT" -eq 1 ] && exit 1
else
  echo "未发现个人/部署专属信息 ✓"
fi
exit 0
