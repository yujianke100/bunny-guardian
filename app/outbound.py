"""外发内容策略：区分「可以外发的健康信息」与「绝不允许外发的智能体/工程信息」。

背景（用户定下的规则）
- 档案主人的健康数据发往所配置的模型端点（含公网中转）**可以接受**；
- 但智能体自身的信息——仓库结构、部署路径、令牌、服务器地址、其他项目与科研内容——
  **绝对不允许**发到那台服务器的 API 上。

因此本模块做三件事：
1. 身份信息（姓名、账号、域名、邮箱）无论端点内外一律抹掉；
2. 工程/智能体内部信息（路径、令牌、主机、其他项目名）一律抹掉；
3. 端点为公网时，抹掉后若仍命中内部特征 → 直接拒绝发送（fail-closed），并记入审计。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 通用规则：与具体站点无关，任何部署都适用（不要在这里写站点专属值）
# 「本环境专属的词」（项目名、集群名、域名、内部工具名、运行时名等）不要写在这里，
# 它们属于站点配置：数据目录下的 scrub_terms.txt（见 deploy/scrub-terms.example.txt）。
GENERIC_INTERNAL_PATTERNS: list[tuple[str, str]] = [
    (r"/(?:opt|srv|var/lib|etc|usr/local)/[\w./-]{2,}", "系统路径"),
    (r"/(?:home|Users)/[\w.-]+/[\w./-]{2,}", "用户目录路径"),
    (r"\b[a-z][a-z0-9_-]{2,}_(?:token|key|secret|password)\b", "凭据字段名"),
    (r"\b(?:bg|sk|ghp|github_pat|xox[bp])[_-][A-Za-z0-9_-]{8,}", "令牌或密钥"),
    (r"\bBearer\s+\S+", "认证头"),
    # 完整 IPv4（含内网段）。注意必须匹配四段，否则会出现「10.1.2.3」只抹掉「10.1.2」的半截结果
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "IP 地址"),
    (r"\bdeploy/[\w./-]+", "部署脚本路径"),
    (r"\b(?:models\.json|agent\.sh|run\.sh|systemd-run|systemctl)\b", "部署细节"),
    (r"\b(?:CLAUDE|AGENTS)\.md\b", "仓库内部文档"),
    # 运行时/工具名属于站点专属词，放数据目录的 scrub_terms.txt，
    # 不写进这份公开代码。
]

# 站点专属词（项目名、集群名、域名等）从数据目录的 scrub_terms.txt 读取，
# 一行一个，用 # 注释。示例见 deploy/scrub-terms.example.txt。
# 这样 fork 出去时无需改代码：把自己的环境词写进各自的 scrub_terms.txt。
SCRUB_TERMS_FILE_ENV = "BG_SCRUB_TERMS"


def _terms_file_candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get(SCRUB_TERMS_FILE_ENV)
    if env:
        out.append(Path(env))
    try:
        import db as dbm
        out.append(Path(dbm.DB_PATH).parent / "scrub_terms.txt")
    except Exception:  # noqa: BLE001
        pass
    out.append(Path(__file__).resolve().parent.parent / "deploy" / "scrub-terms.example.txt")
    return out


def instance_terms() -> list[tuple[str, str]]:
    """站点专属敏感词（可配置；文件不存在时返回空列表）。"""
    pats: list[tuple[str, str]] = []
    for f in _terms_file_candidates():
        try:
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8").splitlines():
                term = line.split("#", 1)[0].strip()
                if len(term) >= 2:
                    pats.append((re.escape(term), "站点专属词"))
            break
        except OSError:
            continue
    return pats


INTERNAL_PATTERNS: list[tuple[str, str]] = []
def _all_internal() -> list[tuple[str, str]]:
    return GENERIC_INTERNAL_PATTERNS + instance_terms()

# 身份信息：跨端点一律不发送
IDENTITY_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{4}-\d{2}-\d{2}\b", "具体日期"),
]

REDACTED = "[已隐去]"


class OutboundBlocked(RuntimeError):
    """外发内容命中禁止项，拒绝发送。"""


def identity_patterns(conn=None) -> list[tuple[str, str]]:
    """身份词：静态部分（姓名/域名/邮箱）始终生效，动态部分（账号、档案主人名）从库里取。"""
    pats: list[tuple[str, str]] = [
        (r"[\w.+-]+@[\w-]+\.[\w.]+", "邮箱"),
    ]
    # 姓名、昵称、域名等**站点专属**词条请在 <数据目录>/scrub_terms.txt 里配置
    # （见 deploy/scrub-terms.example.txt），不要写进代码。
    if conn is None:
        return pats
    try:
        for row in conn.execute("SELECT username, display_name FROM users"):
            for v in (row["username"], row["display_name"]):
                if v and len(str(v)) >= 2:
                    pats.append((re.escape(str(v)), "账号标识"))
        for row in conn.execute("SELECT name FROM profiles"):
            if row["name"] and len(str(row["name"])) >= 2:
                pats.append((re.escape(str(row["name"])), "档案主人姓名"))
    except Exception:  # noqa: BLE001
        pass
    return pats


def verify_no_internals(messages: list[dict]) -> None:
    """安全网：确认清洗后的外发内容里不再有工程内部信息，否则拒绝发送。"""
    for m in messages:
        text = _content_to_text(m.get("content"))
        for pat, label in _all_internal():
            if re.search(pat, text, flags=re.IGNORECASE):
                raise OutboundBlocked(f"外发内容仍包含工程内部信息（{label}），已阻止发送")


def scrub(text: str, conn=None) -> tuple[str, list[str]]:
    """抹掉禁止外发的内容，返回 (清洗后的文本, 命中类别列表)。"""
    hits: list[str] = []
    out = text or ""
    for pat, label in identity_patterns(conn) + IDENTITY_PATTERNS + _all_internal():
        new = re.sub(pat, REDACTED, out, flags=re.IGNORECASE)
        if new != out:
            hits.append(label)
            out = new
    return out, sorted(set(hits))


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text":
                    parts.append(str(c.get("text", "")))
                elif c.get("type") == "image_url":
                    parts.append("<图片>")
            else:
                parts.append(str(c))
        return "\n".join(parts)
    return str(content)


def guard_messages(messages: list[dict], public: bool, conn=None) -> tuple[list[dict], list[str]]:
    """清洗所有外发消息。

    - 一律抹掉身份与工程内部信息（这是主要手段，用户可见的影响只是被替换为 [已隐去]）；
    - 端点为公网时，再用 verify_no_internals 复核一遍，仍有残留就拒绝发送（安全网）。
    返回 (清洗后的消息, 命中类别)。
    """
    hits: list[str] = []
    cleaned: list[dict] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            new, h = scrub(content, conn)
            hits += h
            cleaned.append({**m, "content": new})
        elif isinstance(content, list):
            new_list = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    new, h = scrub(str(part.get("text", "")), conn)
                    hits += h
                    new_list.append({**part, "text": new})
                else:
                    new_list.append(part)      # 图片等原样保留（不含文本）
            cleaned.append({**m, "content": new_list})
        else:
            cleaned.append(m)

    if public:
        verify_no_internals(cleaned)      # fail-closed：清洗后仍有内部信息就拒绝发送
    return cleaned, sorted(set(hits))
