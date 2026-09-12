"""问答会话管理：**一个用户一个固定会话**，上下文由后端自己维护（不给用户设置项）。

策略（全部自动，无需用户干预）
    - 会话：每个用户只有一个长期会话，永不轮转；打开面板就是它。
    - 压缩：未归档消息超过 COMPRESS_OVER（默认 24 条）时，把较早的消息交给模型压成
      ≤400 字摘要（模型不可用时退化为「抽取式摘要」：保留提问 + 回答首句），
      摘要存进 qa_sessions.summary；被压缩的消息标记 archived。
    - 送模型：只带 摘要（作为系统提示）+ 最近 KEEP_RECENT（默认 8）条消息。
    - 清理：archived 且早于 RETENTION_DAYS（默认 180 天）的消息删除。

会话消息本身不删（除超期归档），所以用户回看历史不会丢内容。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import db as dbm

COMPRESS_OVER = 24          # 未归档消息超过这个数就压缩较早的部分
KEEP_RECENT = 8             # 每次送模型保留的最近消息条数
RETENTION_DAYS = 180        # 归档消息的保留天数


def settings() -> dict:
    """当前生效的上下文策略（只读，供界面/诊断展示；不给用户改）。"""
    return {"compress_over": COMPRESS_OVER, "keep_recent": KEEP_RECENT,
            "retention_days": RETENTION_DAYS, "fixed_session": True}


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def active_session(conn, user_id: int) -> tuple[int, bool]:
    """返回 (会话 id, 是否新建)。一个用户只有一个会话，永久复用。"""
    row = conn.execute(
        "SELECT id FROM qa_sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user_id,)).fetchone()
    if row is not None:
        return int(row["id"]), False
    cur = conn.execute(
        "INSERT INTO qa_sessions(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
        (user_id, "日常问答", dbm.now(), dbm.now()))
    return int(cur.lastrowid), True


def history_for_model(conn, session_id: int) -> list[dict]:
    """送给模型的历史：最近若干条（摘要由调用方作为 system 提示带上）。"""
    rows = conn.execute(
        "SELECT role, content FROM qa_messages WHERE session_id=? AND archived=0"
        " ORDER BY id DESC LIMIT ?", (session_id, KEEP_RECENT)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def session_brief(conn, session_id: int) -> str:
    row = conn.execute("SELECT summary FROM qa_sessions WHERE id=?", (session_id,)).fetchone()
    return (row["summary"] if row else "") or ""


def _extractive_summary(msgs: list) -> str:
    """模型不可用时的退化摘要：保留提问，回答只留首句。"""
    parts = []
    for r in msgs:
        text = re.sub(r"\s+", " ", (r["content"] or "")).strip()
        if r["role"] == "user":
            parts.append("问：" + text[:60])
        else:
            first = re.split(r"[。！？\n]", text)[0][:60]
            if first:
                parts.append("答：" + first)
    return "（历史摘要）" + "；".join(parts[-12:])[:400]


def needs_compress(conn, session_id: int) -> bool:
    n = conn.execute("SELECT COUNT(*) c FROM qa_messages WHERE session_id=? AND archived=0",
                     (session_id,)).fetchone()["c"]
    return n > COMPRESS_OVER


def compress_session(conn, session_id: int, llm=None, force: bool = False) -> dict:
    """把较早的消息压成摘要。llm 需提供 chat(messages) -> str，可为 None。"""
    total = conn.execute("SELECT COUNT(*) c FROM qa_messages WHERE session_id=? AND archived=0",
                         (session_id,)).fetchone()["c"]
    if not force and total <= COMPRESS_OVER:
        return {"compressed": 0, "reason": f"消息数 {total} ≤ {COMPRESS_OVER}，无需压缩"}
    rows = conn.execute(
        "SELECT id, role, content FROM qa_messages WHERE session_id=? AND archived=0"
        " ORDER BY id", (session_id,)).fetchall()
    older = rows[:-KEEP_RECENT] if len(rows) > KEEP_RECENT else []
    if not older:
        return {"compressed": 0, "reason": "没有可压缩的较早消息"}

    old_summary = session_brief(conn, session_id)
    text_in = old_summary + "\n" + "\n".join(
        f"{'用户' if r['role'] == 'user' else '助手'}：{(r['content'] or '')[:400]}" for r in older)
    summary = ""
    if llm is not None:
        try:
            summary = llm.chat([
                {"role": "system", "content":
                 "你在维护一份健康档案问答的历史摘要。把下面的对话压缩成不超过 400 字的中文摘要，"
                 "只保留：健康状况相关的事实、用户关心的问题、给出的结论与就医建议。"
                 "不要编造，不要添加建议，直接输出摘要正文。"},
                {"role": "user", "content": text_in[:6000]},
            ], temperature=0.2, max_tokens=600).strip()
        except Exception:  # noqa: BLE001 - 模型不可用时退化
            summary = ""
    if not summary:
        summary = _extractive_summary(older)

    conn.execute("UPDATE qa_sessions SET summary=?, compressed_at=? WHERE id=?",
                 (summary[:2000], dbm.now(), session_id))
    conn.execute("UPDATE qa_messages SET archived=1 WHERE id IN ({})".format(
        ",".join(str(r["id"]) for r in older)))
    return {"compressed": len(older), "summary": summary[:200], "reason": "ok"}


def cleanup(conn, llm=None) -> dict:
    """定时维护：压缩超长会话 + 删除超期归档消息（会话永不轮转）。"""
    stat = {"compressed_sessions": 0, "compressed_msgs": 0, "purged": 0}
    for u in conn.execute("SELECT id FROM users WHERE is_active=1"):
        sid, _created = active_session(conn, int(u["id"]))
        if needs_compress(conn, sid):
            r = compress_session(conn, sid, llm=llm)
            if r.get("compressed"):
                stat["compressed_sessions"] += 1
                stat["compressed_msgs"] += int(r["compressed"])
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("DELETE FROM qa_messages WHERE archived=1 AND created_at<?", (cutoff,))
    stat["purged"] = cur.rowcount or 0
    return stat
