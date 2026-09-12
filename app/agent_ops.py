"""智能体的完整数据操作层（含写入），供持有 write 作用域令牌的外部智能体调用。

与 agent.py 的分工
    agent.py      令牌 / 审计 / 配额 / 隐私围栏 / 只读查询 / 「提案」流程
    agent_ops.py  直接读写全部业务数据（**不需要人工批准**）——本文件

为什么单独一层
    提案流程是给「不可信的智能体」用的：写入先落 pending，由人在网页批准。
    但有些场景（自己维护数据、批量导入、外部 agent 代录）需要直接写。
    于是把「直接写」集中到这一层，用**令牌作用域**控制谁可以：
    只有 scope=write 的令牌才拿得到，默认令牌仍是只读。

边界（有意为之，不是遗漏）
    允许：身体数据、经期、每日日志、疾病档案、病程事件、就诊记录、
          知识库文档的增删改查
    不允许：账号与会话管理、令牌管理、读模型端点密钥、改模型配置、
          备份的恢复与删除 —— 这些属于「凭据与实例控制」。
          一个能自我提权（造管理员账号）或读出密钥的令牌，
          等于把整个实例交出去，所以不进这层。

写操作的安全网
    1) 每次调用都写 agent_audit（动作、参数摘要、结果）；
    2) 支持 dry_run=1：照常校验并执行，然后回滚，用来先验证再真写；
    3) 字段白名单 + 长度上限 + 枚举校验 + 日期校验，不接受自由 SQL；
    4) 删除会连带清掉子表（病程事件、附件行），不留孤儿。
"""
from __future__ import annotations

from datetime import date

import db as dbm

# ---------------------------------------------------------------- 枚举与上限

FLOWS = ("light", "medium", "heavy")
LOG_KINDS = ("symptom", "mood", "flow", "medication", "note",
             "temperature", "weight", "spotting", "pain")
COND_STATUS = ("active", "monitoring", "resolved")
EVENT_KINDS = ("visit", "exam", "medication", "surgery", "symptom", "note")
SEXES = ("female", "male", "")

MAX_TEXT = 2000
MAX_NOTE = 2000
MAX_SHORT = 100


class OpError(ValueError):
    """参数或状态不合法：调用方应回 400 并把原因原样告诉智能体。"""


# ---------------------------------------------------------------- 小工具

def _s(args: dict, key: str, limit: int = MAX_SHORT, required: bool = False,
       allow_empty: bool = True) -> str:
    v = args.get(key)
    if v is None:
        if required:
            raise OpError(f"缺少必填字段 {key}")
        return ""
    s = str(v).strip()[:limit]
    if required and not s and not allow_empty:
        raise OpError(f"{key} 不能为空")
    return s


def _d(args: dict, key: str, required: bool = False) -> str | None:
    """ISO 日期；不合法就报错（不静默改成今天——那会悄悄写错数据）。"""
    v = args.get(key)
    if v in (None, ""):
        if required:
            raise OpError(f"缺少必填日期 {key}（格式 YYYY-MM-DD）")
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise OpError(f"{key} 不是合法日期：{str(v)[:20]}（应为 YYYY-MM-DD）") from None


def _i(args: dict, key: str, required: bool = True, lo: int = 1, hi: int = 10 ** 9) -> int:
    v = args.get(key)
    if v in (None, ""):
        if required:
            raise OpError(f"缺少必填字段 {key}")
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise OpError(f"{key} 必须是整数：{str(v)[:20]}") from None
    if not (lo <= n <= hi):
        raise OpError(f"{key} 超出范围（{lo}..{hi}）：{n}")
    return n


def _f(args: dict, key: str, lo: float, hi: float) -> float | None:
    v = args.get(key)
    if v in (None, ""):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise OpError(f"{key} 必须是数字：{str(v)[:20]}") from None
    if not (lo <= n <= hi):
        raise OpError(f"{key} 超出合理范围（{lo}..{hi}）：{n}")
    return round(n, 2)


def _enum(args: dict, key: str, allowed: tuple, default: str = "",
          required: bool = False) -> str:
    v = str(args.get(key) or "").strip().lower()
    if not v:
        if required:
            raise OpError(f"缺少必填字段 {key}（可选值：{'、'.join(allowed)}）")
        return default
    if v not in allowed:
        raise OpError(f"{key} 只能是 {'、'.join(allowed)}，收到：{v}")
    return v


def _one(conn, sql: str, params: tuple, what: str) -> dict:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise OpError(f"找不到{what}")
    return dict(row)


# ---------------------------------------------------------------- 读操作

def profile_get(conn, pid: int, a: dict) -> dict:
    row = _one(conn, "SELECT * FROM profiles WHERE id=?", (pid,), "档案")
    row.pop("created_at", None)
    return {"档案": row}


def cycle_list(conn, pid: int, a: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM cycles WHERE profile_id=? ORDER BY start_date DESC LIMIT ?",
        (pid, _i(a, "limit", required=False, lo=1, hi=500) or 100)).fetchall()
    return {"记录数": len(rows), "月经记录": [dict(r) for r in rows]}


def cycle_get(conn, pid: int, a: dict) -> dict:
    return {"月经记录": _one(conn, "SELECT * FROM cycles WHERE id=? AND profile_id=?",
                            (_i(a, "id"), pid), "月经记录")}


def log_list(conn, pid: int, a: dict) -> dict:
    sql = "SELECT * FROM day_logs WHERE profile_id=?"
    params: list = [pid]
    if a.get("since"):
        sql += " AND log_date>=?"
        params.append(_d(a, "since"))
    if a.get("until"):
        sql += " AND log_date<=?"
        params.append(_d(a, "until"))
    if a.get("kind"):
        sql += " AND kind=?"
        params.append(_enum(a, "kind", LOG_KINDS))
    sql += " ORDER BY log_date DESC, id DESC LIMIT ?"
    params.append(_i(a, "limit", required=False, lo=1, hi=1000) or 200)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return {"记录数": len(rows), "日志": [dict(r) for r in rows]}


def condition_list(conn, pid: int, a: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM conditions WHERE profile_id=? ORDER BY id DESC LIMIT ?",
        (pid, _i(a, "limit", required=False, lo=1, hi=500) or 100)).fetchall()
    return {"记录数": len(rows), "疾病档案": [dict(r) for r in rows]}


def condition_get(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "id")
    c = _one(conn, "SELECT * FROM conditions WHERE id=? AND profile_id=?", (cid, pid), "疾病档案")
    ev = conn.execute("SELECT * FROM condition_events WHERE condition_id=?"
                      " ORDER BY event_date DESC, id DESC", (cid,)).fetchall()
    return {"疾病": c, "病程事件": [dict(r) for r in ev]}


def visit_list(conn, pid: int, a: dict) -> dict:
    rows = conn.execute(
        "SELECT * FROM visits WHERE profile_id=? ORDER BY visit_date DESC, id DESC LIMIT ?",
        (pid, _i(a, "limit", required=False, lo=1, hi=500) or 100)).fetchall()
    return {"记录数": len(rows), "就诊记录": [dict(r) for r in rows]}


def visit_get(conn, pid: int, a: dict) -> dict:
    return {"就诊记录": _one(conn, "SELECT * FROM visits WHERE id=? AND profile_id=?",
                            (_i(a, "id"), pid), "就诊记录")}


def kb_list(conn, pid: int, a: dict) -> dict:
    rows = conn.execute(
        "SELECT path, title, tags, source, status, origin, scope, updated_at,"
        " length(body) AS 字数 FROM kb_docs ORDER BY updated_at DESC LIMIT ?",
        (_i(a, "limit", required=False, lo=1, hi=1000) or 200,)).fetchall()
    return {"文档数": len(rows), "文档": [dict(r) for r in rows]}


def kb_get(conn, pid: int, a: dict) -> dict:
    path = _kb_path(a)
    return {"文档": _one(conn, "SELECT * FROM kb_docs WHERE path=?", (path,), "知识库文档")}


def overview(conn, pid: int, a: dict) -> dict:
    def n(sql: str, *p) -> int:
        return conn.execute(sql, p).fetchone()[0]

    return {
        "计数": {
            "月经记录": n("SELECT COUNT(*) FROM cycles WHERE profile_id=?", pid),
            "每日日志": n("SELECT COUNT(*) FROM day_logs WHERE profile_id=?", pid),
            "疾病档案": n("SELECT COUNT(*) FROM conditions WHERE profile_id=?", pid),
            "病程事件": n("SELECT COUNT(*) FROM condition_events ce JOIN conditions c"
                        " ON c.id=ce.condition_id WHERE c.profile_id=?", pid),
            "就诊记录": n("SELECT COUNT(*) FROM visits WHERE profile_id=?", pid),
            "知识库文档": n("SELECT COUNT(*) FROM kb_docs"),
        },
        "最早与最晚": {
            "月经记录": conn.execute("SELECT MIN(start_date), MAX(start_date) FROM cycles"
                                    " WHERE profile_id=?", (pid,)).fetchone()[:],
            "每日日志": conn.execute("SELECT MIN(log_date), MAX(log_date) FROM day_logs"
                                    " WHERE profile_id=?", (pid,)).fetchone()[:],
        },
    }


# ---------------------------------------------------------------- 写操作

def profile_update(conn, pid: int, a: dict) -> dict:
    sets, params = [], []
    for key, limit in (("name", 60), ("note", MAX_NOTE)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, limit))
    if "sex" in a:
        sets.append("sex=?")
        params.append(_enum(a, "sex", SEXES))
    if "birth_year" in a:
        sets.append("birth_year=?")
        params.append(_i(a, "birth_year", lo=1900, hi=date.today().year))
    for key, lo, hi in (("height_cm", 50, 250), ("weight_kg", 20, 300)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_f(a, key, lo, hi))
    if not sets:
        raise OpError("没有要改的字段（可改：name/sex/birth_year/height_cm/weight_kg/note）")
    params.append(pid)
    conn.execute(f"UPDATE profiles SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"已更新": profile_get(conn, pid, {})["档案"]}


def cycle_add(conn, pid: int, a: dict) -> dict:
    start = _d(a, "start_date", required=True)
    end = _d(a, "end_date")
    if end and end < start:
        raise OpError(f"结束日期 {end} 早于开始日期 {start}")
    cur = conn.execute(
        "INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (pid, start, end, _enum(a, "flow", FLOWS), _s(a, "symptoms", MAX_NOTE),
         _s(a, "note", MAX_NOTE), dbm.now(), dbm.now()))
    return {"已新增月经记录": cur.lastrowid, "开始": start, "结束": end}


def cycle_update(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "id")
    _one(conn, "SELECT id FROM cycles WHERE id=? AND profile_id=?", (cid, pid), "月经记录")
    sets, params = [], []
    if "start_date" in a:
        sets.append("start_date=?")
        params.append(_d(a, "start_date", required=True))
    if "end_date" in a:
        sets.append("end_date=?")
        params.append(_d(a, "end_date"))
    if "flow" in a:
        sets.append("flow=?")
        params.append(_enum(a, "flow", FLOWS))
    for key, lim in (("symptoms", MAX_NOTE), ("note", MAX_NOTE)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, lim))
    if not sets:
        raise OpError("没有要改的字段")
    sets.append("updated_at=?")
    params.extend([dbm.now(), cid])
    conn.execute(f"UPDATE cycles SET {', '.join(sets)} WHERE id=?", tuple(params))
    row = conn.execute("SELECT * FROM cycles WHERE id=?", (cid,)).fetchone()
    if row["end_date"] and row["end_date"] < row["start_date"]:
        raise OpError(f"改完之后结束日期 {row['end_date']} 早于开始日期 {row['start_date']}")
    return {"已更新月经记录": cid}


def cycle_delete(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "id")
    _one(conn, "SELECT id FROM cycles WHERE id=? AND profile_id=?", (cid, pid), "月经记录")
    conn.execute("DELETE FROM attachments WHERE ref_kind='cycle' AND ref_id=?", (cid,))
    conn.execute("DELETE FROM cycles WHERE id=?", (cid,))
    return {"已删除月经记录": cid}


def log_add(conn, pid: int, a: dict) -> dict:
    sev = a.get("severity")
    severity = None
    if sev not in (None, ""):
        severity = _i(a, "severity", lo=1, hi=5)
    cur = conn.execute(
        "INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pid, _d(a, "log_date", required=True), _enum(a, "kind", LOG_KINDS, "note", required=True),
         _s(a, "name", MAX_SHORT), severity, _s(a, "value", MAX_SHORT),
         _s(a, "note", MAX_NOTE), dbm.now()))
    return {"已新增日志": cur.lastrowid}


def log_update(conn, pid: int, a: dict) -> dict:
    lid = _i(a, "id")
    _one(conn, "SELECT id FROM day_logs WHERE id=? AND profile_id=?", (lid, pid), "日志")
    sets, params = [], []
    if "log_date" in a:
        sets.append("log_date=?")
        params.append(_d(a, "log_date", required=True))
    if "kind" in a:
        sets.append("kind=?")
        params.append(_enum(a, "kind", LOG_KINDS, required=True))
    if "severity" in a:
        sets.append("severity=?")
        params.append(None if a.get("severity") in (None, "")
                      else _i(a, "severity", lo=1, hi=5))
    for key, lim in (("name", MAX_SHORT), ("value", MAX_SHORT), ("note", MAX_NOTE)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, lim))
    if not sets:
        raise OpError("没有要改的字段")
    params.append(lid)
    conn.execute(f"UPDATE day_logs SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"已更新日志": lid}


def log_delete(conn, pid: int, a: dict) -> dict:
    lid = _i(a, "id")
    _one(conn, "SELECT id FROM day_logs WHERE id=? AND profile_id=?", (lid, pid), "日志")
    conn.execute("DELETE FROM day_logs WHERE id=?", (lid,))
    return {"已删除日志": lid}


def condition_add(conn, pid: int, a: dict) -> dict:
    cur = conn.execute(
        "INSERT INTO conditions(profile_id, name, category, status, onset_date, diagnosed_date,"
        " hospital, department, doctor, summary, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, _s(a, "name", MAX_SHORT, required=True, allow_empty=False),
         _s(a, "category", MAX_SHORT), _enum(a, "status", COND_STATUS, "active"),
         _d(a, "onset_date"), _d(a, "diagnosed_date"), _s(a, "hospital", MAX_SHORT),
         _s(a, "department", MAX_SHORT), _s(a, "doctor", MAX_SHORT),
         _s(a, "summary", MAX_TEXT), dbm.now(), dbm.now()))
    return {"已新增疾病档案": cur.lastrowid}


def condition_update(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "id")
    _one(conn, "SELECT id FROM conditions WHERE id=? AND profile_id=?", (cid, pid), "疾病档案")
    sets, params = [], []
    if "status" in a:
        sets.append("status=?")
        params.append(_enum(a, "status", COND_STATUS, "active"))
    for key in ("onset_date", "diagnosed_date"):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_d(a, key))
    for key, lim in (("name", MAX_SHORT), ("category", MAX_SHORT), ("hospital", MAX_SHORT),
                     ("department", MAX_SHORT), ("doctor", MAX_SHORT), ("summary", MAX_TEXT)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, lim))
    if not sets:
        raise OpError("没有要改的字段")
    sets.append("updated_at=?")
    params.extend([dbm.now(), cid])
    conn.execute(f"UPDATE conditions SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"已更新疾病档案": cid}


def condition_delete(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "id")
    _one(conn, "SELECT id FROM conditions WHERE id=? AND profile_id=?", (cid, pid), "疾病档案")
    evs = [r["id"] for r in conn.execute(
        "SELECT id FROM condition_events WHERE condition_id=?", (cid,)).fetchall()]
    for eid in evs:
        conn.execute("DELETE FROM attachments WHERE ref_kind='event' AND ref_id=?", (eid,))
    conn.execute("DELETE FROM attachments WHERE ref_kind='condition' AND ref_id=?", (cid,))
    conn.execute("DELETE FROM condition_events WHERE condition_id=?", (cid,))
    conn.execute("DELETE FROM conditions WHERE id=?", (cid,))
    return {"已删除疾病档案": cid, "连带删除的病程事件": len(evs)}


def event_add(conn, pid: int, a: dict) -> dict:
    cid = _i(a, "condition_id")
    _one(conn, "SELECT id FROM conditions WHERE id=? AND profile_id=?", (cid, pid), "疾病档案")
    cur = conn.execute(
        "INSERT INTO condition_events(condition_id, event_date, kind, title, detail, result,"
        " created_at) VALUES(?,?,?,?,?,?,?)",
        (cid, _d(a, "event_date", required=True),
         _enum(a, "kind", EVENT_KINDS, "note", required=True), _s(a, "title", MAX_SHORT),
         _s(a, "detail", MAX_TEXT), _s(a, "result", MAX_TEXT), dbm.now()))
    return {"已新增病程事件": cur.lastrowid, "所属疾病": cid}


def _event_own(conn, pid: int, eid: int) -> dict:
    return _one(conn, "SELECT ce.* FROM condition_events ce JOIN conditions c ON c.id=ce.condition_id"
                      " WHERE ce.id=? AND c.profile_id=?", (eid, pid), "病程事件")


def event_update(conn, pid: int, a: dict) -> dict:
    eid = _i(a, "id")
    _event_own(conn, pid, eid)
    sets, params = [], []
    if "event_date" in a:
        sets.append("event_date=?")
        params.append(_d(a, "event_date", required=True))
    if "kind" in a:
        sets.append("kind=?")
        params.append(_enum(a, "kind", EVENT_KINDS, required=True))
    for key, lim in (("title", MAX_SHORT), ("detail", MAX_TEXT), ("result", MAX_TEXT)):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, lim))
    if not sets:
        raise OpError("没有要改的字段")
    params.append(eid)
    conn.execute(f"UPDATE condition_events SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"已更新病程事件": eid}


def event_delete(conn, pid: int, a: dict) -> dict:
    eid = _i(a, "id")
    _event_own(conn, pid, eid)
    conn.execute("DELETE FROM attachments WHERE ref_kind='event' AND ref_id=?", (eid,))
    conn.execute("DELETE FROM condition_events WHERE id=?", (eid,))
    return {"已删除病程事件": eid}


def visit_add(conn, pid: int, a: dict) -> dict:
    cur = conn.execute(
        "INSERT INTO visits(profile_id, visit_date, hospital, department, doctor, reason, findings,"
        " diagnosis, plan, cost, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, _d(a, "visit_date", required=True), _s(a, "hospital", MAX_SHORT),
         _s(a, "department", MAX_SHORT), _s(a, "doctor", MAX_SHORT), _s(a, "reason", MAX_TEXT),
         _s(a, "findings", MAX_TEXT), _s(a, "diagnosis", MAX_TEXT), _s(a, "plan", MAX_TEXT),
         _f(a, "cost", 0, 10 ** 7), dbm.now()))
    return {"已新增就诊记录": cur.lastrowid}


def visit_update(conn, pid: int, a: dict) -> dict:
    vid = _i(a, "id")
    _one(conn, "SELECT id FROM visits WHERE id=? AND profile_id=?", (vid, pid), "就诊记录")
    sets, params = [], []
    if "visit_date" in a:
        sets.append("visit_date=?")
        params.append(_d(a, "visit_date", required=True))
    for key in ("hospital", "department", "doctor"):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, MAX_SHORT))
    for key in ("reason", "findings", "diagnosis", "plan"):
        if key in a:
            sets.append(f"{key}=?")
            params.append(_s(a, key, MAX_TEXT))
    if "cost" in a:
        sets.append("cost=?")
        params.append(_f(a, "cost", 0, 10 ** 7))
    if not sets:
        raise OpError("没有要改的字段")
    params.append(vid)
    conn.execute(f"UPDATE visits SET {', '.join(sets)} WHERE id=?", tuple(params))
    return {"已更新就诊记录": vid}


def visit_delete(conn, pid: int, a: dict) -> dict:
    vid = _i(a, "id")
    _one(conn, "SELECT id FROM visits WHERE id=? AND profile_id=?", (vid, pid), "就诊记录")
    conn.execute("DELETE FROM attachments WHERE ref_kind='visit' AND ref_id=?", (vid,))
    conn.execute("DELETE FROM visits WHERE id=?", (vid,))
    return {"已删除就诊记录": vid}


# ---------------------------------------------------------------- 知识库

def _kb_path(a: dict) -> str:
    path = _s(a, "path", 200, required=True, allow_empty=False).strip("/")
    if ".." in path or path.startswith("/") or "\\" in path:
        raise OpError("path 只能是 docs/ 下的相对路径，且不能包含 .. 或反斜杠")
    if not path.endswith(".md"):
        raise OpError("path 必须以 .md 结尾")
    return path


def kb_write(conn, pid: int, a: dict) -> dict:
    import kb as kbm

    path = _kb_path(a)
    body = str(a.get("body") or "")
    if not body.strip():
        raise OpError("body 不能为空")
    if len(body) > 40000:
        raise OpError(f"body 过长（{len(body)} 字 > 40000）")
    tags = a.get("tags")
    if tags is not None and not isinstance(tags, list):
        raise OpError("tags 必须是数组")
    existed = conn.execute("SELECT scope FROM kb_docs WHERE path=?", (path,)).fetchone()
    # 通用知识是随软件分发的那一层，改它没有意义（重装会被覆盖），所以拒绝
    if existed and (existed["scope"] or "") == kbm.SCOPE_GENERAL:
        raise OpError("这是随软件分发的通用知识文档，不允许通过接口改写")
    kbm.upsert_doc(conn, path, body,
                   {"title": _s(a, "title", 80), "tags": tags or [],
                    "source": _s(a, "source", 300), "status": _s(a, "status", 40)},
                   origin="agent")
    return {"已写入知识库文档": path, "字数": len(body),
            "方式": "覆盖了已有文档" if existed else "新建"}


def kb_delete(conn, pid: int, a: dict) -> dict:
    import kb as kbm

    path = _kb_path(a)
    row = conn.execute("SELECT scope FROM kb_docs WHERE path=?", (path,)).fetchone()
    if row is None:
        raise OpError("找不到这篇文档")
    if (row["scope"] or "") == kbm.SCOPE_GENERAL:
        raise OpError("这是随软件分发的通用知识文档，不允许通过接口删除")
    kbm.delete_doc(conn, path)
    return {"已删除知识库文档": path}


# ---------------------------------------------------------------- 注册表

OPS: dict[str, dict] = {
    # ---- 只读 ----
    "profile.get": {"need": "read", "fn": profile_get, "summary": "读身体数据与基本情况"},
    "overview": {"need": "read", "fn": overview, "summary": "各类记录的条数与时间跨度"},
    "cycle.list": {"need": "read", "fn": cycle_list, "summary": "列月经记录（可 limit）"},
    "cycle.get": {"need": "read", "fn": cycle_get, "summary": "按 id 取一条月经记录"},
    "log.list": {"need": "read", "fn": log_list, "summary": "列每日日志（since/until/kind/limit）"},
    "condition.list": {"need": "read", "fn": condition_list, "summary": "列疾病档案"},
    "condition.get": {"need": "read", "fn": condition_get, "summary": "按 id 取档案 + 病程事件"},
    "visit.list": {"need": "read", "fn": visit_list, "summary": "列就诊记录"},
    "visit.get": {"need": "read", "fn": visit_get, "summary": "按 id 取一次就诊"},
    "kb.list": {"need": "read", "fn": kb_list, "summary": "列知识库文档（不含正文）"},
    "kb.get": {"need": "read", "fn": kb_get, "summary": "按 path 取知识库文档全文"},

    # ---- 写入 ----
    "profile.update": {"need": "write", "fn": profile_update,
                       "summary": "改身体数据（name/sex/birth_year/height_cm/weight_kg/note）"},
    "cycle.add": {"need": "write", "fn": cycle_add,
                  "summary": "新增月经记录（start_date 必填；flow=light|medium|heavy）"},
    "cycle.update": {"need": "write", "fn": cycle_update, "summary": "改一条月经记录（id 必填）"},
    "cycle.delete": {"need": "write", "fn": cycle_delete, "summary": "删一条月经记录（连带附件行）"},
    "log.add": {"need": "write", "fn": log_add,
                "summary": f"新增每日日志（log_date、kind 必填；kind={'|'.join(LOG_KINDS)}）"},
    "log.update": {"need": "write", "fn": log_update, "summary": "改一条日志（id 必填）"},
    "log.delete": {"need": "write", "fn": log_delete, "summary": "删一条日志"},
    "condition.add": {"need": "write", "fn": condition_add, "summary": "新增疾病档案（name 必填）"},
    "condition.update": {"need": "write", "fn": condition_update, "summary": "改疾病档案"},
    "condition.delete": {"need": "write", "fn": condition_delete,
                         "summary": "删疾病档案（连带病程事件与附件行）"},
    "event.add": {"need": "write", "fn": event_add,
                  "summary": f"新增病程事件（condition_id、event_date、kind 必填；"
                             f"kind={'|'.join(EVENT_KINDS)}）"},
    "event.update": {"need": "write", "fn": event_update, "summary": "改病程事件"},
    "event.delete": {"need": "write", "fn": event_delete, "summary": "删病程事件"},
    "visit.add": {"need": "write", "fn": visit_add, "summary": "新增就诊记录（visit_date 必填）"},
    "visit.update": {"need": "write", "fn": visit_update, "summary": "改就诊记录"},
    "visit.delete": {"need": "write", "fn": visit_delete, "summary": "删就诊记录"},
    "kb.write": {"need": "write", "fn": kb_write,
                 "summary": "新建/覆盖知识库文档（path 必填、以 .md 结尾；body 必填）"},
    "kb.delete": {"need": "write", "fn": kb_delete, "summary": "删知识库文档"},
}

# 这一层**不做**的事（写进 /api/agent/ops 的返回值，让智能体知道边界而不是反复试）
NOT_SUPPORTED = [
    "账号与会话管理（建用户、改密码、踢下线）",
    "令牌管理（新建/吊销令牌）",
    "读取模型端点密钥或改模型配置",
    "备份的恢复与删除",
]


def catalog(scope: str) -> dict:
    """给智能体看的操作清单（按自己的作用域过滤）。"""
    can = [{"op": k, "说明": v["summary"]}
           for k, v in sorted(OPS.items()) if v["need"] == "read" or scope == "write"]
    return {
        "作用域": scope,
        "说明": ("read 只能读；write 可读可写。写操作不需要人工批准，请先 dry_run=1 验证。"
                 if scope == "write" else "当前令牌只读。写操作需要 scope=write 的令牌。"),
        "可用操作": can,
        "不支持": NOT_SUPPORTED,
    }


def run(conn, pid: int, op: str, args: dict, dry_run: bool = False) -> dict:
    """执行一个操作。参数或状态不合法抛 OpError（调用方回 400）。"""
    op = str(op or "").strip()
    if op not in OPS:
        raise OpError(f"不支持的操作 {op!r}（用 GET /api/agent/ops 看可用清单）")
    if not isinstance(args, dict):
        raise OpError("args 必须是对象")
    fn = OPS[op]["fn"]
    out = fn(conn, pid, args)
    if not isinstance(out, dict):
        out = {"结果": out}
    out["操作"] = op
    if dry_run:
        out["dry_run"] = True
    return out
