"""智能体（pi）的权限框架：作用域令牌、只读查询、变更提案、审计与配额。

设计原则（与用户商定的允许范围一一对应）
    允许：当前病情询问 / 联网搜索 / 病情问答 / 数据记录整理归档（经由 GitHub 维护的仓库） / 月经相关询问与预测
    禁止：执行任意命令、写任意文件、读取账号与会话、删除任何记录、修改医学知识文档、直接推送 git、访问白名单外的网络

四道防线
    1) 工具层：pi 只加载本仓库 deploy/pi/extensions 里定义的工具，内置 read/write/edit/bash 一律不在白名单内
    2) 进程层：以非特权用户 + systemd 沙箱运行（见 deploy/pi/ 下的沙箱脚本）
    3) 数据层：智能体不接触 SQLite 文件，只能调用这里的受限接口（只读查询 + 写「提案」）
    4) 动作层：任何写入都先落成 pending 提案，必须由人在网页上批准后才真正落库并提交到仓库
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime

import db as dbm

# 每个工具每日调用上限（防止提示注入导致的循环刷取）
DAILY_QUOTA = {
    "web_search": 30,
    "web_fetch": 40,
    "propose": 20,
    "context": 60,
    "records_query": 120,
    "kb_search": 120,
    "op": 300,          # 直接写操作（write 作用域令牌），按天
}

# 允许智能体抓取的域名白名单（权威医学来源）；不在表内的一律拒绝
FETCH_ALLOWLIST = [
    "acog.org", "who.int", "nice.org.uk", "msdmanuals.com", "mayoclinic.org",
    "cdc.gov", "nih.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov",
    "asrm.org", "hopkinsmedicine.org", "cochrane.org", "bmj.com", "thelancet.com",
    "yiigle.com", "cma.org.cn", "cspm.cma.org.cn", "medlive.cn", "nhc.gov.cn",
    "endometriosis.org", "pcosaa.org", "womenshealth.gov", "obgyn.onlinelibrary.wiley.com",
]

# 允许的记录查询类型（白名单，不接受自由 SQL）
QUERY_KINDS = {
    "cycle_history", "cycle_stats", "conditions", "condition_detail",
    "visits", "day_logs", "search",
}


def _now() -> str:
    return dbm.now()


def _today() -> str:
    return dbm.today()


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    uses INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'read'         -- read=只读 | write=可读可写（直接落库）
);

CREATE TABLE IF NOT EXISTS agent_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    token_label TEXT DEFAULT '',
    tool TEXT NOT NULL,
    decision TEXT NOT NULL,              -- allowed | denied | error
    detail TEXT DEFAULT '',
    preview TEXT DEFAULT '',
    duration_ms INTEGER,
    day TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                  -- cycle_add | day_log | condition_add | condition_event | visit_add | note
    payload TEXT NOT NULL,               -- JSON
    rationale TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | applied | failed
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by INTEGER,
    apply_result TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_day ON agent_audit(day, tool);
CREATE INDEX IF NOT EXISTS idx_agent_prop_status ON agent_proposals(status, id);
"""

# 提案允许的动作 → 落库时执行的函数（在 main 里注入，避免循环依赖）
PROPOSAL_KINDS = {
    "cycle_add": "新增一次月经记录",
    "day_log": "新增一条每日日志",
    "condition_add": "新增一个疾病档案",
    "condition_event": "新增一条病程事件",
    "visit_add": "新增一次就诊记录",
    "condition_update": "修改疾病档案状态/概述",
    "kb_note": "新增一篇知识库笔记（联网资料整理）",
    "note": "仅备注（不落库，供人工参考）",
}


def init(conn) -> None:
    conn.executescript(SCHEMA)
    # 老库升级：agent_tokens 原本没有 scope（那时令牌一律只读）
    _ensure_column(conn, "agent_tokens", "scope", "scope TEXT NOT NULL DEFAULT 'read'")


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# ------------------------------------------------------------------ 令牌

SCOPES = ("read", "write")

SCOPE_CN = {
    "read": "只读（提问、检索、查询；写入只能提交待批准提案）",
    "write": "可写（在只读之外，可直接增删改业务数据，无需人工批准）",
}


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_token(conn, label: str = "pi agent", created_by: int | None = None,
                 scope: str = "read") -> str:
    """生成新令牌（明文只返回一次，库里只存哈希）。

    scope 决定这个令牌能干什么，**默认 read**：
      read   只读 + 提交待批准提案（原有行为）
      write  在只读之外可直接增删改业务数据（见 app/agent_ops.py）
    为什么默认只读：令牌一旦泄露，write 令牌等价于「能改病历的那把钥匙」，
    所以要有人明确选择，而不是建令牌时顺手拿到全部权限。
    """
    scope = scope if scope in SCOPES else "read"
    raw = "bg_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO agent_tokens(label, token_hash, created_at, created_by, scope)"
        " VALUES(?,?,?,?,?)",
        (label[:60], hash_token(raw), _now(), created_by, scope))
    return raw


def revoke_all(conn) -> None:
    conn.execute("UPDATE agent_tokens SET revoked=1")


def check_token(conn, raw: str | None) -> tuple[bool, str, str]:
    """校验令牌；返回 (是否有效, 标签或原因, 作用域)。

    作用域只在令牌有效时才有意义；无效时第三项固定为 "read"（调用方用不上）。
    """
    if not raw:
        return False, "缺少令牌", "read"
    if get_setting(conn, "agent_enabled", "0") != "1":
        return False, "智能体功能当前已停用", "read"
    row = conn.execute(
        "SELECT * FROM agent_tokens WHERE token_hash=? AND revoked=0", (hash_token(raw),)).fetchone()
    if row is None:
        return False, "令牌无效或已吊销", "read"
    conn.execute("UPDATE agent_tokens SET last_used_at=?, uses=uses+1 WHERE id=?",
                 (_now(), row["id"]))
    scope = (row["scope"] if "scope" in row.keys() else "read") or "read"
    return True, row["label"], (scope if scope in SCOPES else "read")


def active_token_info(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, created_at, last_used_at, uses, revoked, scope FROM agent_tokens"
        " ORDER BY id DESC LIMIT 10").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 设置与配额

def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def agent_enabled(conn) -> bool:
    return get_setting(conn, "agent_enabled", "0") == "1"


def quota_left(conn, tool: str) -> int:
    limit = DAILY_QUOTA.get(tool, 0)
    n = conn.execute("SELECT COUNT(*) c FROM agent_audit WHERE day=? AND tool=? AND decision='allowed'",
                     (_today(), tool)).fetchone()["c"]
    return max(0, limit - n)


def audit(conn, token_label: str, tool: str, decision: str, detail: str = "",
          preview: str = "", duration_ms: int | None = None) -> None:
    conn.execute(
        "INSERT INTO agent_audit(at, token_label, tool, decision, detail, preview, duration_ms, day)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (_now(), token_label, tool[:40], decision, detail[:300], preview[:300], duration_ms, _today()))


# ------------------------------------------------------------------ 隐私围栏

_DATE_RE = re.compile(r"\b(19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b")


def privacy_check(conn, text: str) -> tuple[bool, str, str]:
    """联网外发内容检查：返回 (是否放行, 改写后的文本, 拒绝原因)。

    规则（宁可拒绝，不可猜测）：
      - 身份信息（姓名、账号、域名、邮箱）、具体日期、站点专属词、工程内部信息
        —— 统一复用 app/outbound.py 的策略，避免两处维护同一份词表；
      - 长度上限 200 字。
    对检索词采用「命中即拒绝」而不是静默替换：让模型学会写干净的查询。
    """
    import outbound

    text = (text or "").strip()
    if not text:
        return False, "", "内容为空"
    if len(text) > 200:
        return False, text[:200], "过长（>200 字），请只发送通用检索词"

    _cleaned, hits = outbound.scrub(text, conn)
    if hits:
        if "具体日期" in hits:
            return False, "", "包含具体日期，请改为通用描述（如「经期推迟」）"
        return False, "", f"包含个人标识或站点专属信息（{'、'.join(hits)}），不允许外发"
    return True, text, ""


def url_allowed(url: str) -> tuple[bool, str]:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False, "URL 无法解析"
    if u.scheme not in ("http", "https"):
        return False, "只允许 http/https"
    host = (u.hostname or "").lower()
    for d in FETCH_ALLOWLIST:
        if host == d or host.endswith("." + d):
            return True, ""
    return False, f"域名 {host} 不在医学来源白名单内"


# ------------------------------------------------------------------ 只读查询

def _cycle_payload(conn, pid: int) -> dict:
    import cycle as cyclem
    starts = dbm.cycle_starts(conn, pid)
    durations = dbm.cycle_durations(conn, pid)
    info = cyclem.analyze(starts, durations)
    s = info["stats"]
    return {
        "有记录": not info["example"],
        "记录次数": s["n_cycles"],
        "近期周期长度": s["recent_lengths"],
        "中位周期长度": s["median"],
        "范围": [s["min"], s["max"]],
        "标准差": s["stdev"],
        "经期平均长度": s["period_len_mean"],
        "上次经期": s["last_start"].isoformat() if s["last_start"] else None,
        "推断下次经期": info["next_start"].isoformat() if info["next_start"] else None,
        "推断区间": [d.isoformat() for d in info["next_window"]] if info["next_window"] else None,
        "置信度": info["confidence"],
        "当前周期第几天": info["cycle_day"],
        "当前阶段": info["phase_cn"],
        "异常提示": info["irregular_flags"],
        "说明": "以上为基于历史记录的统计推断，不是医学检测结果，不能用于避孕或确诊。",
    }


def context(conn, pid: int) -> dict:
    """智能体的基础上下文：只给摘要，不给原始隐私字段（医院/医生/费用等）。"""
    conds = conn.execute(
        "SELECT id, name, category, status, diagnosed_date FROM conditions"
        " WHERE profile_id=? ORDER BY id DESC LIMIT 20", (pid,)).fetchall()
    visits = conn.execute(
        "SELECT id, visit_date, department, diagnosis FROM visits WHERE profile_id=?"
        " ORDER BY visit_date DESC LIMIT 10", (pid,)).fetchall()
    logs = conn.execute(
        "SELECT log_date, kind, name, severity FROM day_logs WHERE profile_id=?"
        " ORDER BY log_date DESC LIMIT 20", (pid,)).fetchall()
    return {
        "月经": _cycle_payload(conn, pid),
        "进行中疾病": [dict(r) for r in conds],
        "近期就诊": [dict(r) for r in visits],
        "近期日志": [dict(r) for r in logs],
        "可做的事": ["回答病情与月经相关问题", "检索知识库", "联网查权威资料", "提交数据录入提案（需人工批准）"],
        "禁止的事": ["执行命令", "直接改写数据或知识文档", "删除任何记录", "访问来源白名单外的网站"],
    }


def records_query(conn, pid: int, kind: str, payload: dict) -> dict:
    if kind not in QUERY_KINDS:
        raise ValueError(f"不支持的查询类型：{kind}（允许：{sorted(QUERY_KINDS)}）")
    if kind == "cycle_history":
        rows = conn.execute("SELECT start_date, end_date, flow, symptoms, note FROM cycles"
                            " WHERE profile_id=? ORDER BY start_date DESC LIMIT 50", (pid,)).fetchall()
        return {"记录": [dict(r) for r in rows]}
    if kind == "cycle_stats":
        return _cycle_payload(conn, pid)
    if kind == "conditions":
        rows = conn.execute(
            "SELECT id, name, category, status, onset_date, diagnosed_date, summary FROM conditions"
            " WHERE profile_id=? ORDER BY id DESC LIMIT 50", (pid,)).fetchall()
        return {"疾病档案": [dict(r) for r in rows]}
    if kind == "condition_detail":
        cid = int(payload.get("id", 0))
        c = conn.execute("SELECT id, name, category, status, onset_date, diagnosed_date, summary"
                         " FROM conditions WHERE id=? AND profile_id=?", (cid, pid)).fetchone()
        if c is None:
            return {"错误": "未找到该疾病档案"}
        ev = conn.execute(
            "SELECT event_date, kind, title, detail, result FROM condition_events"
            " WHERE condition_id=? ORDER BY event_date DESC LIMIT 50", (cid,)).fetchall()
        return {"疾病": dict(c), "病程事件": [dict(r) for r in ev]}
    if kind == "visits":
        rows = conn.execute(
            "SELECT visit_date, department, reason, findings, diagnosis, plan FROM visits"
            " WHERE profile_id=? ORDER BY visit_date DESC LIMIT 30", (pid,)).fetchall()
        return {"就诊记录": [dict(r) for r in rows]}
    if kind == "day_logs":
        rows = conn.execute(
            "SELECT log_date, kind, name, severity, value, note FROM day_logs WHERE profile_id=?"
            " ORDER BY log_date DESC LIMIT 100", (pid,)).fetchall()
        return {"日志": [dict(r) for r in rows]}
    # search：在记录里按关键词找（LIKE，行数上限），不允许自由 SQL
    q = str(payload.get("q", "")).strip()[:40]
    if not q:
        return {"错误": "缺少关键词 q"}
    like = f"%{q}%"
    out = {}
    for label, sql, cols in [
        ("疾病", "SELECT id, name, summary FROM conditions WHERE profile_id=? AND (name LIKE ? OR summary LIKE ?) LIMIT 20",
         ("id", "name", "summary")),
        ("就诊", "SELECT visit_date, department, diagnosis FROM visits WHERE profile_id=? AND (reason LIKE ? OR diagnosis LIKE ? OR findings LIKE ?) LIMIT 20",
         ("visit_date", "department", "diagnosis")),
        ("日志", "SELECT log_date, kind, name, note FROM day_logs WHERE profile_id=? AND (name LIKE ? OR note LIKE ?) LIMIT 20",
         ("log_date", "kind", "name", "note")),
    ]:
        n = sql.count("?") - 1
        rows = conn.execute(sql, (pid, *([like] * n))).fetchall()
        out[label] = [dict(r) for r in rows]
    return {"关键词": q, "结果": out}


# ------------------------------------------------------------------ 提案

MAX_PAYLOAD = 20000      # 提案内容上限（知识库笔记带正文，4000 字不够用）


def create_proposal(conn, kind: str, payload: dict, rationale: str = "") -> int:
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"不支持的提案类型：{kind}（允许：{sorted(PROPOSAL_KINDS)}）")
    blob = json.dumps(payload, ensure_ascii=False)
    if len(blob) > MAX_PAYLOAD:
        # 以前这里直接 [:4000] 截断，结果是确认时 JSON 解析失败（线上踩过：
        # 知识库笔记正文被截，卡片点了确认却写不进去，状态变成 failed）
        raise ValueError(f"提案内容过长（{len(blob)} 字 > {MAX_PAYLOAD}），请精简后重试")
    cur = conn.execute(
        "INSERT INTO agent_proposals(kind, payload, rationale, created_at) VALUES(?,?,?,?)",
        (kind, blob, rationale[:500], _now()))
    return int(cur.lastrowid)


def list_proposals(conn, status: str = "pending", limit: int = 50) -> list[dict]:
    if status == "all":
        rows = conn.execute("SELECT * FROM agent_proposals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM agent_proposals WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload_obj"] = json.loads(d["payload"])
        except Exception:
            d["payload_obj"] = {}
        d["kind_cn"] = PROPOSAL_KINDS.get(d["kind"], d["kind"])
        out.append(d)
    return out


def _payload_preview(kind: str, payload: dict) -> str:
    parts = []
    for k, v in list(payload.items())[:6]:
        parts.append(f"{k}={str(v)[:40]}")
    return f"{PROPOSAL_KINDS.get(kind, kind)}：" + "，" .join(parts)


def decide(conn, proposal_id: int, user_id: int, approve: bool) -> tuple[bool, str, dict]:
    """批准/拒绝提案。批准时执行真正的落库动作。"""
    row = conn.execute("SELECT * FROM agent_proposals WHERE id=?", (proposal_id,)).fetchone()
    if row is None:
        return False, "提案不存在", {}
    if row["status"] != "pending":
        return False, f"提案已是 {row['status']} 状态，不能重复处理", {}
    if not approve:
        conn.execute("UPDATE agent_proposals SET status='rejected', decided_at=?, decided_by=?"
                     " WHERE id=?", (_now(), user_id, proposal_id))
        return True, "已拒绝", {}
    try:
        payload = json.loads(row["payload"])
    except Exception:
        conn.execute("UPDATE agent_proposals SET status='failed', apply_result='payload 解析失败',"
                     " decided_at=?, decided_by=? WHERE id=?", (_now(), user_id, proposal_id))
        return False, "payload 解析失败", {}
    pid = dbm.ensure_profile(conn)
    try:
        msg, ref_kind, ref_id = apply_proposal(conn, pid, row["kind"], payload)
    except Exception as e:  # noqa: BLE001
        conn.execute("UPDATE agent_proposals SET status='failed', apply_result=?, decided_at=?,"
                     " decided_by=? WHERE id=?", (str(e)[:300], _now(), user_id, proposal_id))
        return False, f"落库失败：{e}", {}
    conn.execute("UPDATE agent_proposals SET status='applied', apply_result=?, decided_at=?,"
                 " decided_by=? WHERE id=?", (msg[:300], _now(), user_id, proposal_id))
    return True, msg, {"kind": ref_kind, "id": ref_id}


def apply_proposal(conn, pid: int, kind: str, p: dict) -> tuple[str, str, int]:
    """真正写入数据库。字段经过白名单过滤，缺字段用默认值。"""
    def g(key: str, default: str = "") -> str:
        v = p.get(key, default)
        return "" if v is None else str(v)[:500]

    def dt(key: str) -> str:
        v = g(key)
        try:
            return date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            return _today()

    if kind == "cycle_add":
        s = dt("start_date")
        e = g("end_date")
        try:
            e_iso = date.fromisoformat(e[:10]).isoformat() if e else None
        except ValueError:
            e_iso = None
        cur = conn.execute("INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note,"
                           " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                           (pid, s, e_iso, g("flow")[:10], g("symptoms"), g("note")[:200], _now(), _now()))
        return f"已新增月经记录（开始 {s}）", "cycle", int(cur.lastrowid)
    if kind == "day_log":
        sev = p.get("severity")
        try:
            sev = max(1, min(5, int(sev))) if sev not in (None, "") else None
        except (TypeError, ValueError):
            sev = None
        cur = conn.execute("INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note,"
                           " created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (pid, dt("log_date"), g("kind") or "note", g("name"), sev, g("value"),
                            g("note"), _now()))
        return f"已新增日志（{dt('log_date')} {g('kind') or 'note'}）", "day_log", int(cur.lastrowid)
    if kind == "condition_add":
        cur = conn.execute(
            "INSERT INTO conditions(profile_id, name, category, status, onset_date, diagnosed_date,"
            " hospital, department, doctor, summary, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, g("name")[:100] or "未命名", g("category")[:50],
             g("status") if g("status") in ("active", "monitoring", "resolved") else "active",
             g("onset_date") or None, g("diagnosed_date") or None, g("hospital")[:100],
             g("department")[:50], g("doctor")[:50], g("summary"), _now(), _now()))
        return f"已新增疾病档案（id={cur.lastrowid}）", "condition", int(cur.lastrowid)
    if kind == "condition_event":
        cid = int(p.get("condition_id", 0))
        if not conn.execute("SELECT 1 FROM conditions WHERE id=? AND profile_id=?", (cid, pid)).fetchone():
            raise ValueError(f"疾病档案 {cid} 不存在")
        cur = conn.execute(
            "INSERT INTO condition_events(condition_id, event_date, kind, title, detail, result,"
            " created_at) VALUES(?,?,?,?,?,?,?)",
            (cid, dt("event_date"), g("kind") or "note", g("title"), g("detail"), g("result"), _now()))
        return f"已新增病程事件（档案 {cid}）", "event", int(cur.lastrowid)
    if kind == "visit_add":
        try:
            cost = float(p.get("cost")) if p.get("cost") not in (None, "") else None
        except (TypeError, ValueError):
            cost = None
        cur = conn.execute(
            "INSERT INTO visits(profile_id, visit_date, hospital, department, doctor, reason, findings,"
            " diagnosis, plan, cost, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, dt("visit_date"), g("hospital"), g("department"), g("doctor"), g("reason"),
             g("findings"), g("diagnosis"), g("plan"), cost, _now()))
        return f"已新增就诊记录（{dt('visit_date')}）", "visit", int(cur.lastrowid)
    if kind == "condition_update":
        cid = int(p.get("condition_id", 0))
        if not conn.execute("SELECT 1 FROM conditions WHERE id=? AND profile_id=?", (cid, pid)).fetchone():
            raise ValueError(f"疾病档案 {cid} 不存在")
        conn.execute("UPDATE conditions SET status=?, summary=?, updated_at=? WHERE id=?",
                     (g("status") if g("status") in ("active", "monitoring", "resolved") else "active",
                      g("summary") or None, _now(), cid))
        return f"已更新疾病档案 {cid}", "condition", cid
    if kind == "kb_note":
        return apply_kb_note(conn, p)
    if kind == "note":
        return "已记录备注（无需落库）", "", 0
    raise ValueError(f"未知提案类型 {kind}")


def apply_kb_note(conn, p: dict) -> tuple[str, str, int]:
    """写一篇知识库笔记（个人层、待核对）。AI 判断值得留下就直接写，不走待确认。

    知识笔记不参与数据仓库同步（用户要求：本地保存和维护就够了）。
    """
    import kb as kbm

    def g(key: str, default: str = "") -> str:
        v = p.get(key, default)
        return "" if v is None else str(v)[:500]

    title = str(p.get("title") or "")[:80] or "资料笔记"
    body = str(p.get("body") or "")[:12000]
    if not body.strip():
        raise ValueError("笔记正文为空")
    day = _today()
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-")[:20] or "资料"
    rel = str(p.get("path") or "")[:120].lstrip("/") or f"医学知识/资料整理/{day}-{slug}.md"
    if not rel.endswith(".md"):
        rel += ".md"
    kbm.upsert_doc(conn, rel, body,
                   {"title": title, "tags": ["联网资料", "AI 整理"],
                    "source": g("source")[:300], "status": "待核对",
                    "last_updated": day},
                   origin="ai", scope=kbm.SCOPE_PERSONAL)
    return f"已存入知识库：{rel}", "kb_doc", 0


def audit_view(conn, limit: int = 60) -> list[dict]:
    rows = conn.execute("SELECT * FROM agent_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def quota_view(conn) -> list[dict]:
    out = []
    for tool, limit in DAILY_QUOTA.items():
        used = conn.execute("SELECT COUNT(*) c FROM agent_audit WHERE day=? AND tool=? AND decision='allowed'",
                            (_today(), tool)).fetchone()["c"]
        out.append({"tool": tool, "used": used, "limit": limit, "left": max(0, limit - used)})
    return out


def stats(conn) -> dict:
    p = conn.execute("SELECT COUNT(*) c FROM agent_proposals WHERE status='pending'").fetchone()["c"]
    a = conn.execute("SELECT COUNT(*) c FROM agent_audit WHERE day=?", (_today(),)).fetchone()["c"]
    d = conn.execute("SELECT COUNT(*) c FROM agent_audit WHERE day=? AND decision='denied'",
                     (_today(),)).fetchone()["c"]
    return {"pending": p, "today_calls": a, "today_denied": d,
            "enabled": agent_enabled(conn), "today": _today()}


def _dt_str(v) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v)
