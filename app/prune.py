"""按保留期清理「不需要留档」的东西：AI 问答记录与随问答上传的照片/文件。

用途（与档案主人商定的边界）
    需要长期留的只有**私人记录**：经期、每日日志、疾病档案、病程事件、就诊记录。
    那部分由变更触发的同步导出成 markdown 推到私人仓库（异地 + 版本化），不需要这里管。
    另外两类是「用完就完了」的：
      - AI 问答历史（qa_messages）：上下文压缩自己会滚动，旧的不必留；
      - 随问答上传的照片/病历单（attachments 里 ref_kind='qa' 的那些）。
    这两类没有长期价值，留着只是占地方、扩大泄露面，所以按保留期删掉。

**红线：只删 ref_kind='qa' 的附件。**
一旦某张图被挂到真实记录上（`_link_recent_qa_attachments` 会把最近 24 小时的上传改挂到
cycle / condition / event / visit），它的 ref_kind 就不再是 'qa'，这里一律不碰——
否则会把病历单从就诊记录里删掉。

顺带做一次孤儿文件清扫：uploads/ 里已经没有数据库行指向的文件（比如手工删过行、
或早期版本留下的），一并清掉。只处理「早于保留期」的，避免误删正在上传的文件。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import db as dbm

# 绝不删除的附件所属（真实记录）
PROTECTED_KINDS = ("cycle", "condition", "event", "visit")

DEFAULTS = {"enabled": "1", "qa_keep_days": "30", "uploads_keep_days": "30"}


def _upload_dir() -> Path:
    import os
    data = Path(os.environ.get("BG_DATA") or (Path(dbm.DB_PATH).parent))
    return data / "uploads"


def settings(conn) -> dict:
    def n(key: str) -> int:
        try:
            return max(1, int(dbm.get_setting(conn, key, DEFAULTS[key]) or DEFAULTS[key]))
        except ValueError:
            return int(DEFAULTS[key])

    return {
        "enabled": dbm.get_setting(conn, "prune_enabled", DEFAULTS["enabled"]) == "1",
        "qa_keep_days": n("qa_keep_days"),
        "uploads_keep_days": n("uploads_keep_days"),
        "last": dbm.get_setting(conn, "last_prune", ""),
    }


def set_settings(conn, enabled: bool, qa_days: int, uploads_days: int) -> None:
    dbm.set_setting(conn, "prune_enabled", "1" if enabled else "0")
    dbm.set_setting(conn, "qa_keep_days", str(max(1, min(3650, qa_days))))
    dbm.set_setting(conn, "uploads_keep_days", str(max(1, min(3650, uploads_days))))


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _rm(path: Path) -> int:
    try:
        n = path.stat().st_size
        path.unlink()
        return n
    except OSError:
        return 0


def run(conn, force: bool = False) -> dict:
    """跑一次清理。返回统计；`enabled=0` 且非 force 时什么都不做。"""
    cfg = settings(conn)
    stat = {"skipped": False, "msgs": 0, "files": 0, "orphans": 0, "freed": 0,
            "kept_record_files": 0, "qa_keep_days": cfg["qa_keep_days"],
            "uploads_keep_days": cfg["uploads_keep_days"]}
    if not cfg["enabled"] and not force:
        stat["skipped"] = True
        return stat

    up = _upload_dir()
    qa_cut = _cutoff(cfg["qa_keep_days"])
    up_cut = _cutoff(cfg["uploads_keep_days"])

    # ---- 1) 问答附件：只删 ref_kind='qa' 的 ----
    rows = conn.execute(
        "SELECT id, stored_name FROM attachments"
        " WHERE ref_kind='qa' AND created_at<?", (up_cut,)).fetchall()
    for r in rows:
        stat["freed"] += _rm(up / r["stored_name"])
        conn.execute("DELETE FROM attachments WHERE id=?", (r["id"],))
        stat["files"] += 1

    # 数一下被保护的真实记录附件（只用于给用户看，证明没误删）
    stat["kept_record_files"] = conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE ref_kind IN (%s)"
        % ",".join("?" * len(PROTECTED_KINDS)), PROTECTED_KINDS).fetchone()[0]

    # ---- 2) 问答历史 ----
    cur = conn.execute("DELETE FROM qa_messages WHERE created_at<?", (qa_cut,))
    stat["msgs"] = cur.rowcount or 0

    # ---- 3) 孤儿文件（数据库里没有行指向、且早于保留期）----
    if up.is_dir():
        known = {r["stored_name"] for r in conn.execute("SELECT stored_name FROM attachments")}
        for f in up.iterdir():
            if not f.is_file() or f.name in known:
                continue
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) > datetime.now() - timedelta(
                        days=cfg["uploads_keep_days"]):
                    continue          # 可能正在上传，先留着
            except OSError:
                continue
            stat["freed"] += _rm(f)
            stat["orphans"] += 1

    dbm.set_setting(conn, "last_prune",
                    f"{dbm.now()} → 问答 {stat['msgs']} 条 / 附件 {stat['files']} 个"
                    f" / 孤儿 {stat['orphans']} 个（释放 {stat['freed'] // 1024}KB）")
    return stat


def describe(stat: dict) -> str:
    if stat.get("skipped"):
        return "清理已关闭（问答与附件都保留）"
    return (f"问答记录 {stat['msgs']} 条、问答附件 {stat['files']} 个、"
            f"孤儿文件 {stat['orphans']} 个（释放 {stat['freed'] // 1024}KB）；"
            f"记录自带附件 {stat['kept_record_files']} 个未动")
