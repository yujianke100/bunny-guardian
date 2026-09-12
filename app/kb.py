"""知识库：**以 SQLite 为唯一数据源**，检索走内存倒排索引（不扫文件）。

为什么不用文件：
    - 每次请求都 read_text + 重新解析 Markdown，是纯浪费；
    - 全文检索在文件上只能逐个扫描，文档一多就线性退化；
    - 备份/同步从此只有一个 .db 文件，不用管目录树。

索引设计：
    中文用「字符二元组（bigram）」建倒排表——FTS5 的 unicode61 分词器不切中文、
    trigram 又要求查询 ≥3 字，都不适合「痛经」这种 2 字查询。
    倒排索引在进程内构建，写入时失效重建（当前规模 ~250KB 文本，重建 <10ms）。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import markdown as md

APP_DIR = Path(__file__).resolve().parent
CODE_DIR = APP_DIR.parent
KB_DIR = Path(__import__("os").environ.get("BG_KB_DIR") or CODE_DIR)
DOCS_DIR = KB_DIR / "docs"          # 仅用于导入/导出，不再是读取源

# 通用医学知识：随代码仓分发、不随数据仓库走。
# 理由：可再获取（丢了大不了重下一次）、体积大、不含任何个人信息。
# 本地目录优先（BG_KB_GENERAL），缺省用代码仓自带的 knowledge/general 作种子。
GENERAL_DIR = Path(__import__("os").environ.get("BG_KB_GENERAL")
                   or (CODE_DIR / "knowledge" / "general"))
# scope=personal 的文档才会被导出回数据仓库并 git 同步；general 只进数据库供检索。
SCOPE_PERSONAL = "personal"
SCOPE_GENERAL = "general"

KNOWLEDGE_DIR = "医学知识"
DATA_DIRS = ["经期记录", "疾病档案", "就医记录"]
ALL_DIRS = [KNOWLEDGE_DIR] + DATA_DIRS

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_WIKI_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

_MD = md.Markdown(extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


# ------------------------------------------------------------------ frontmatter

def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), text[m.end():]
    meta: dict = {}
    key = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*-\s+", line) and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.strip()[2:].strip())
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            v = v.strip()
            meta[key] = v if v else []
    for k in ("title", "last_updated", "source", "status"):
        if isinstance(meta.get(k), str):
            meta[k] = meta[k].strip().strip('"').strip("'")
    return meta, body


def dump_frontmatter(meta: dict) -> str:
    order = ["title", "tags", "aliases", "source", "status", "last_updated"]
    lines = ["---"]
    for k in order:
        if k not in meta:
            continue
        v = meta[k]
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {x}" for x in v]
        else:
            lines.append(f"{k}: {v}")
    for k, v in meta.items():
        if k in order:
            continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {x}" for x in v]
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 写入

def upsert_doc(conn, path: str, body: str, meta: dict | None = None, origin: str = "web",
               scope: str = SCOPE_PERSONAL) -> None:
    meta = meta or {}
    conn.execute(
        "INSERT INTO kb_docs(path, title, tags, aliases, source, status, body, updated_at, origin, scope)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(path) DO UPDATE SET title=excluded.title, tags=excluded.tags,"
        " aliases=excluded.aliases, source=excluded.source, status=excluded.status,"
        " body=excluded.body, updated_at=excluded.updated_at, origin=excluded.origin,"
        " scope=excluded.scope",
        (path.strip("/"), str(meta.get("title") or Path(path).stem),
         json.dumps(meta.get("tags") or [], ensure_ascii=False),
         json.dumps(meta.get("aliases") or [], ensure_ascii=False),
         str(meta.get("source") or ""), str(meta.get("status") or ""),
         body, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), origin,
         scope if scope in (SCOPE_PERSONAL, SCOPE_GENERAL) else SCOPE_PERSONAL))
    invalidate()


def delete_doc(conn, path: str) -> None:
    conn.execute("DELETE FROM kb_docs WHERE path=?", (path.strip("/"),))
    invalidate()


# ------------------------------------------------------------------ 读取

def list_docs(dirs: list[str] | None = None) -> list[dict]:
    import db as dbm
    want = dirs or ALL_DIRS
    if not want:
        return []
    where = " OR ".join(["path LIKE ?"] * len(want))
    with dbm.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM kb_docs WHERE {where} ORDER BY path",
            tuple(f"{d}/%" for d in want)).fetchall()
    out = []
    for r in rows:
        body = r["body"] or ""
        out.append({
            "dir": (r["path"].split("/")[0] if "/" in r["path"] else ""),
            "rel": r["path"], "name": r["path"].split("/")[-1], "title": r["title"],
            "tags": json.loads(r["tags"] or "[]"), "source": r["source"],
            "scope": (r["scope"] if "scope" in r.keys() else SCOPE_PERSONAL) or SCOPE_PERSONAL,
            "updated": (r["updated_at"] or "")[:10],
            "mtime": (r["updated_at"] or "")[:10], "size": len(body.encode()),
            "words": len(body), "placeholder": "待有数据后导出生成" in body or "（暂无内容）" in body,
        })
    return out


def get_doc(path: str) -> dict | None:
    import db as dbm
    with dbm.db() as conn:
        r = conn.execute("SELECT * FROM kb_docs WHERE path=?", (str(path).strip("/"),)).fetchone()
    if r is None:
        return None
    return {"path": r["path"], "title": r["title"], "body": r["body"],
            "tags": json.loads(r["tags"] or "[]"), "aliases": json.loads(r["aliases"] or "[]"),
            "source": r["source"], "status": r["status"], "updated_at": r["updated_at"],
            "origin": r["origin"]}


def _linkify(html: str) -> str:
    def repl(m: re.Match) -> str:
        target, label = m.group(1).strip(), (m.group(2) or "").strip()
        text = label or target.split("/")[-1].removesuffix(".md")
        name = target.split("/")[-1]
        if not name.endswith(".md"):
            name += ".md"
        for d in ALL_DIRS:
            if get_doc(f"{d}/{name}"):
                return f'<a href="/knowledge?path={d}/{name}">{text}</a>'
        return text
    return _WIKI_RE.sub(repl, html)


def render(rel: str) -> tuple[dict, str] | None:
    doc = get_doc(rel)
    if doc is None:
        return None
    _MD.reset()
    html = _MD.convert(doc["body"])
    html = html.replace("<table>", '<div class="tablewrap"><table>').replace("</table>", "</table></div>")
    meta = {"title": doc["title"], "tags": doc["tags"], "source": doc["source"],
            "status": doc["status"], "last_updated": (doc["updated_at"] or "")[:10]}
    return meta, _linkify(html)


def read_text(rel: str) -> str | None:
    doc = get_doc(rel)
    return doc["body"] if doc else None


# ------------------------------------------------------------------ 内存倒排索引

_INDEX: dict = {"version": -1, "sections": [], "docs_meta": [], "head": {}, "tag": {},
                 "body": {}, "docs": 0}
_VERSION = 0


def invalidate() -> None:
    global _VERSION
    _VERSION += 1


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _sections(conn) -> tuple[list[dict], list[dict]]:
    """把每篇文档切成「章节」用于检索，并记住章节属于哪篇文档。

    之前是靠「标题回查文档」找 rel，两篇同名就串了；现在直接把路径带在章节上。
    """
    rows = conn.execute("SELECT path, title, body, tags, aliases FROM kb_docs").fetchall()
    sections, docs = [], []
    for r in rows:
        body = r["body"] or ""
        try:
            tags = json.loads(r["tags"] or "[]") + json.loads(r["aliases"] or "[]")
        except json.JSONDecodeError:
            tags = []
        docs.append({"rel": r["path"], "title": r["title"] or "",
                     "tags": [str(t) for t in tags],
                     "head": (re.sub(r"\s+", " ", body.strip().splitlines()[0]).lstrip("# ").strip()
                              if body.strip() else "")})
        for part in re.split(r"\n(?=##\s)", body):
            part = part.strip()
            if not part:
                continue
            lines = part.splitlines()
            head = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else "（开头）"
            sections.append({"rel": r["path"], "doc": r["title"] or "", "head": head,
                             "text": part, "tags": " ".join(str(t) for t in tags)})
    return sections, docs


def _ensure_index() -> dict:
    global _VERSION
    if _INDEX["version"] == _VERSION:
        return _INDEX
    import db as dbm
    with dbm.db() as conn:
        sections, docs = _sections(conn)
    head: dict[str, set[int]] = defaultdict(set)     # 标题/小节名命中
    tag: dict[str, set[int]] = defaultdict(set)      # 标签/别名命中
    body: dict[str, set[int]] = defaultdict(set)     # 正文命中
    for i, sec in enumerate(sections):
        for g in _bigrams(sec["doc"] + " " + sec["head"]):
            head[g].add(i)
        for g in _bigrams(sec["tags"]):
            tag[g].add(i)
        for g in _bigrams(sec["text"]):
            body[g].add(i)
    _INDEX.update({"version": _VERSION, "sections": sections, "docs_meta": docs,
                   "head": dict(head), "tag": dict(tag), "body": dict(body),
                   "docs": len(docs)})
    return _INDEX


def search(query: str, dirs: list[str] | None = None, top: int = 8) -> list[dict]:
    """检索：标题/小节 ×4、标签/别名 ×2、正文 ×1，按章节长度做轻度归一，同篇只出一条。

    选题顺序：得分高的优先；同一篇文档只贡献一个章节，避免一篇长文占满结果。
    """
    q = (query or "").strip()
    if not q:
        return []
    idx = _ensure_index()
    grams = _bigrams(q)
    if not grams:
        return []
    scores: dict[int, float] = defaultdict(float)
    for g in grams:
        for i in idx["head"].get(g, ()):
            scores[i] += 4.0
        for i in idx["tag"].get(g, ()):
            scores[i] += 2.0
        for i in idx["body"].get(g, ()):
            scores[i] += 1.0
    want = tuple(dirs or ALL_DIRS)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    results, seen = [], set()
    for i, sc in ranked:
        sec = idx["sections"][i]
        if not _in_dirs(sec["rel"], want):
            continue
        if sec["rel"] in seen:                     # 同一篇只出一条
            continue
        seen.add(sec["rel"])
        norm = sc / (1.0 + len(sec["text"]) / 4000.0)
        results.append({"rel": sec["rel"], "title": sec["doc"], "section": sec["head"],
                        "score": round(norm, 2), "raw_score": round(sc, 2),
                        "tags": sec["tags"], "text": sec["text"],
                        "snippet": re.sub(r"\s+", " ", sec["text"])[:320]})
        if len(results) >= top:
            break
    return results


def _in_dirs(rel: str, want: tuple) -> bool:
    return any(rel.startswith(d + "/") for d in want)


def coverage(query: str, hits: list[dict], top: int = 3) -> float:
    """命中的查询字组占全部的比例（0~1）。

    用来判断「知识库是不是真的答到了这个问题」：只按命中条数判断不够 ——
    实测「子宫内膜异位症术后复发率」会命中疾病档案那篇（有「子宫内膜异位症」），
    但库里根本没有复发率的数据，模型只能回「知识库没有」。这种情况应该去联网。
    """
    grams = _bigrams(query or "")
    if not grams:
        return 0.0
    have: set[str] = set()
    for h in (hits or [])[:top]:
        have |= _bigrams(f"{h.get('title') or ''} {h.get('section') or ''} {h.get('tags') or ''}")
        have |= _bigrams((h.get("text") or "")[:1500])
    return round(len(grams & have) / float(len(grams)), 3)


def catalog(dirs: list[str] | None = None, limit: int = 40) -> str:
    """知识库里到底有什么：一行一篇（标题 + 标签）。检索没命中时告诉模型这份清单，
    它就能说「库里现在只有这些，要不要去查外部资料」，而不是凭空编。"""
    docs = list_docs(list(dirs) if dirs else None)
    lines = []
    for d in docs[:limit]:
        if d.get("placeholder"):
            continue
        tags = "、".join(d["tags"][:4])
        scope = "通用" if d.get("scope") == SCOPE_GENERAL else "我的"
        lines.append(f"- {d['title']}（{scope}｜docs/{d['rel']}）" + (f" 标签：{tags}" if tags else ""))
    return "\n".join(lines)


def _norm_rel(target: str) -> str:
    """把正文里的 wiki 链接目标规整成库内路径：去掉 docs/ 前缀、补 .md。"""
    t = (target or "").strip().strip("/")
    if t.startswith("docs/"):
        t = t[5:]
    if t and not t.endswith(".md") and "." not in t.split("/")[-1]:
        t += ".md"
    return t


def related(rel: str, limit: int = 8) -> dict:
    """Wiki 式的「相关文档」：本篇链出去的 + 链进本篇的（去重、规整路径）。"""
    rel = str(rel or "").strip("/")
    me = get_doc(rel)
    if me is None:
        return {"out": [], "incoming": []}
    known = {d["rel"] for d in list_docs()}
    names = {rel.split("/")[-1].removesuffix(".md"), rel, (me.get("title") or "")}
    out, seen = [], set()
    for target, label in _WIKI_RE.findall(me["body"] or ""):
        t = _norm_rel(target)
        if not t or t == rel or t in seen:
            continue
        seen.add(t)
        out.append({"rel": t, "label": (label or t).strip(), "exists": t in known})
    incoming, seen_in = [], set()
    for d in list_docs():
        if d["rel"] == rel or d["rel"] in seen_in:
            continue
        body = (get_doc(d["rel"]) or {}).get("body") or ""
        for target in _WIKI_RE.findall(body):
            t = _norm_rel(target[0])
            if t == rel or t.split("/")[-1].removesuffix(".md") in names:
                incoming.append({"rel": d["rel"], "label": d["title"], "exists": True})
                seen_in.add(d["rel"])
                break
    return {"out": out[:limit], "incoming": sorted(incoming, key=lambda x: x["label"])[:limit]}


def index_md(dirs: list[str] | None = None) -> str:
    """生成 INDEX.md 正文（知识库目录）——由维护任务写进库里，也方便在数据仓库里看。"""
    docs = [d for d in list_docs(list(dirs) if dirs else None) if not d.get("placeholder")]
    lines = ["# 知识库目录", "",
             f"共 {len(docs)} 篇（自动生成，勿手改；改动请在对应文档里改）。", ""]
    for d in docs:
        scope = "通用" if d.get("scope") == SCOPE_GENERAL else "我的"
        tags = "、".join(d["tags"][:5])
        lines.append(f"- [[{d['rel']}|{d['title']}]] — {scope}"
                     + (f"｜{tags}" if tags else "")
                     + (f"｜更新 {d['updated']}" if d.get("updated") else ""))
    return "\n".join(lines) + "\n"


def context_for_question(query: str, max_sections: int = 5, max_chars: int = 7000,
                         per_section: int = 1600) -> tuple[str, list[dict]]:
    """给模型的「依据」：每段取较完整的正文（默认 1600 字），而不是 320 字摘要。

    实测：只给摘要时模型经常回「知识库没有相关内容」，其实文档里写着。
    """
    hits = search(query, top=max_sections)
    chunks, used, total = [], [], 0
    for h in hits:
        body = (h.get("text") or h.get("snippet") or "").strip()[:per_section]
        block = f"### 依据：{h['title']} — {h['section']}（docs/{h['rel']}）\n{body}"
        if total + len(block) > max_chars:
            break
        chunks.append(block)
        used.append(h)
        total += len(block)
    return "\n\n".join(chunks), used


# ------------------------------------------------------------------ 导入 / 导出（与数据仓库交换）

def import_from_dir(conn, docs_dir: Path | None = None, only_if_empty: bool = False,
                    scope: str = SCOPE_PERSONAL, only_missing: bool = False) -> int:
    """把 Markdown 文件导入数据库（迁移/从仓库拉取后调用）。返回导入数量。

    only_missing：库里已有同路径文档就跳过。启动时用它导入通用知识——
    既不会每次重启覆盖你在网页里改过的内容，也能补回本地被删掉的文件。
    """
    base = Path(docs_dir or DOCS_DIR)
    if only_if_empty and conn.execute("SELECT 1 FROM kb_docs LIMIT 1").fetchone():
        return 0
    n = 0
    if not base.is_dir():
        return 0
    for f in sorted(base.rglob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = parse_frontmatter(text)
        rel = str(f.relative_to(base))
        if only_missing and conn.execute("SELECT 1 FROM kb_docs WHERE path=?",
                                         (rel.strip("/"),)).fetchone():
            continue
        upsert_doc(conn, rel, body, meta, origin="import", scope=scope)
        n += 1
    return n


def reclassify(conn) -> int:
    """把 GENERAL_DIR 里存在的文档标记为 general（只改 scope，不动正文）。

    用于早期版本迁移：通用知识原先混在数据仓库里、导入时一律是 personal，
    若不纠正，导出会把它们又写回数据仓库。
    """
    if not GENERAL_DIR.is_dir():
        return 0
    n = 0
    for f in sorted(GENERAL_DIR.rglob("*.md")):
        rel = str(f.relative_to(GENERAL_DIR)).strip("/")
        row = conn.execute("SELECT scope FROM kb_docs WHERE path=?", (rel,)).fetchone()
        if row and row["scope"] != SCOPE_GENERAL:
            conn.execute("UPDATE kb_docs SET scope=? WHERE path=?", (SCOPE_GENERAL, rel))
            n += 1
    if n:
        invalidate()
    return n


def import_all(conn, only_if_empty: bool = False, general_only_missing: bool = False) -> tuple[int, int]:
    """导入两层知识库：数据仓库（personal）+ 本地通用知识（general）。

    通用知识放在 <GENERAL_DIR>/医学知识/ 下，相对路径与数据仓库里的一致，
    因此同一篇文档重复导入只会更新 scope，不会产生重复行。
    """
    reclassify(conn)
    n_personal = import_from_dir(conn, DOCS_DIR, only_if_empty=only_if_empty,
                                 scope=SCOPE_PERSONAL)
    n_general = import_from_dir(conn, GENERAL_DIR, scope=SCOPE_GENERAL,
                                only_missing=general_only_missing)
    return n_personal, n_general


# 知识笔记不导出到数据仓库：用户明确说「知识库本地保存和维护就行」。
# 记录类文档（经期/疾病/就医）照旧导出，保证记录有仓库备份。
NO_EXPORT_PREFIXES = (f"{KNOWLEDGE_DIR}/",)


def export_to_dir(conn, docs_dir: Path | None = None, scope: str | None = SCOPE_PERSONAL) -> int:
    """把数据库中的文档写回 Markdown（同步/备份用，保留人能读、git 能 diff 的性质）。

    默认只导出 scope=personal（数据仓库那一层）；通用知识不进数据仓库，避免被同步带进去。
    """
    base = Path(docs_dir or DOCS_DIR)
    n = 0
    sql = "SELECT * FROM kb_docs"
    args: tuple = ()
    if scope:
        sql += " WHERE scope=?"
        args = (scope,)
    rows = conn.execute(sql + " ORDER BY path", args).fetchall()
    for r in rows:
        if (r["path"] or "").startswith(NO_EXPORT_PREFIXES):
            continue            # 知识笔记只留本地（用户要求：知识库不需要仓库备份）
        meta = {"title": r["title"], "tags": json.loads(r["tags"] or "[]"),
                "aliases": json.loads(r["aliases"] or "[]"),
                "last_updated": (r["updated_at"] or "")[:10], "status": r["status"] or "current"}
        if r["source"]:
            meta["source"] = r["source"]
        p = base / r["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_frontmatter(meta) + "\n" + (r["body"] or "") + "\n", encoding="utf-8")
        n += 1
    return n


def stats() -> dict:
    import db as dbm
    with dbm.db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM kb_docs").fetchone()["c"]
        by_dir = {d: conn.execute("SELECT COUNT(*) c FROM kb_docs WHERE path LIKE ?",
                                  (f"{d}/%",)).fetchone()["c"] for d in ALL_DIRS}
        by_scope = {s: conn.execute("SELECT COUNT(*) c FROM kb_docs WHERE scope=?",
                                    (s,)).fetchone()["c"]
                    for s in (SCOPE_PERSONAL, SCOPE_GENERAL)}
    idx = _ensure_index()
    return {"docs": n, "by_dir": by_dir, "by_scope": by_scope,
            "general_dir": str(GENERAL_DIR),
            "sections": len(idx["sections"]),
            "terms": len(set(idx["head"]) | set(idx["tag"]) | set(idx["body"])),
            "index_version": idx["version"]}
