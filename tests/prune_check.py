"""清理逻辑（app/prune.py）的自检：不需要服务在跑。

重点证明三件事：
  1. 超期的问答记录与「随问答上传」的附件会被删（含磁盘文件）；
  2. **挂到真实记录上的附件绝不动**（红线，删错等于把病历单删了）；
  3. 开关关着时什么都不删；force 才动手；孤儿文件会被收走。

    cd app && BG_UNIT_DIR=/tmp/pu python <本文件>
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))

TMP = pathlib.Path(os.environ.get("BG_UNIT_DIR") or tempfile.mkdtemp(prefix="bprune-"))
os.environ.setdefault("BG_DB", str(TMP / "x.db"))
os.environ.setdefault("BG_DATA", str(TMP))
os.environ.setdefault("BG_KB_DIR", str(TMP / "kb"))
(TMP / "kb" / "docs").mkdir(parents=True, exist_ok=True)
UP = TMP / "uploads"
UP.mkdir(parents=True, exist_ok=True)
os.environ["BG_DATA"] = str(TMP)

import db as dbm          # noqa: E402
import prune as prunem    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  << " + str(extra)[:300]))


def ago(days: int, hours: int = 0) -> str:
    return (datetime.now() - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def make_file(name: str, data: bytes = b"x" * 100) -> pathlib.Path:
    p = UP / name
    p.write_bytes(data)
    return p


dbm.init_db()
conn = dbm.connect()
pid = dbm.ensure_profile(conn)
conn.commit()


def add_msg(text: str, ts: str) -> int:
    cur = conn.execute(
        "INSERT INTO qa_messages(session_id, role, content, meta, created_at)"
        " VALUES(?,?,?,?,?)", (1, "user", text, "", ts))
    conn.commit()
    return cur.lastrowid


def add_att(kind: str, ref: int, stored: str, ts: str) -> int:
    cur = conn.execute(
        "INSERT INTO attachments(ref_kind, ref_id, filename, mime, size, stored_name,"
        " uploaded_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (kind, ref, stored, "image/jpeg", 100, stored, 1, ts))
    conn.commit()
    return cur.lastrowid


# ---- 造数据：新旧各一份 ----
old_msg = add_msg("很久以前的问题", ago(40))
new_msg = add_msg("昨天的问题", ago(1))
old_qa = add_att("qa", 1, make_file("old_qa.jpg").name, ago(40))
new_qa = add_att("qa", 1, make_file("new_qa.jpg").name, ago(1))

# 真实记录 + 挂在它上面的老附件（这是红线：必须留着）
conn.execute("INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note,"
             " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
             (pid, "2026-01-01", "2026-01-05", "medium", "", "", dbm.now(), dbm.now()))
conn.commit()
cid = conn.execute("SELECT id FROM cycles ORDER BY id DESC LIMIT 1").fetchone()["id"]
old_record_file = make_file("old_record.jpg")     # 很老，但挂在真实记录上
kept_att = add_att("visit", 1, old_record_file.name, ago(400))

# 孤儿文件（数据库里没有行指向）。把它改成 40 天前的 mtime：
# 清理只收「早于保留期」的孤儿，避免误删正在上传、还没入库的文件。
orphan = make_file("orphan.jpg")
_old = (datetime.now() - timedelta(days=40)).timestamp()
os.utime(orphan, (_old, _old))
fresh_orphan = make_file("uploading.jpg")      # 刚出现的孤儿：这一轮不该被动

check("清理默认是开启的", prunem.settings(conn)["enabled"] is True)
check("默认保留期 30 天", prunem.settings(conn)["qa_keep_days"] == 30)

st = prunem.run(conn, force=True)
print("\n  统计：", {k: st[k] for k in ("msgs", "files", "orphans", "kept_record_files")})

check("超期的问答记录被删", conn.execute(
    "SELECT COUNT(*) FROM qa_messages WHERE id=?", (old_msg,)).fetchone()[0] == 0)
check("没超期的问答记录还在", conn.execute(
    "SELECT COUNT(*) FROM qa_messages WHERE id=?", (new_msg,)).fetchone()[0] == 1)
check("超期的问答附件行被删", conn.execute(
    "SELECT COUNT(*) FROM attachments WHERE id=?", (old_qa,)).fetchone()[0] == 0)
check("没超期的问答附件行还在", conn.execute(
    "SELECT COUNT(*) FROM attachments WHERE id=?", (new_qa,)).fetchone()[0] == 1)
check("超期问答附件的磁盘文件被删（不只是删行）", not (UP / "old_qa.jpg").exists())
check("没超期的问答附件文件还在", (UP / "new_qa.jpg").exists())

# 红线
check("【红线】挂在真实记录上的老附件行没被删", conn.execute(
    "SELECT COUNT(*) FROM attachments WHERE id=?", (kept_att,)).fetchone()[0] == 1)
check("【红线】它的磁盘文件也没被删", old_record_file.exists())
check("统计里如实报告保护了多少个记录附件", st["kept_record_files"] == 1, st)
check("统计里没有把受保护文件算进删除数", st["files"] == 1, st)

check("孤儿文件被收走", not orphan.exists() and st["orphans"] == 1, st)
check("刚出现的孤儿先留着（可能在传，还没入库）", fresh_orphan.exists(), st)

# ---- 开关关掉时不动手 ----
m = add_msg("关掉开关之后的老消息", ago(90))
f = make_file("another_old.jpg")
a = add_att("qa", 1, f.name, ago(90))
prunem.set_settings(conn, enabled=False, qa_days=30, uploads_days=30)
conn.commit()
st2 = prunem.run(conn)
check("开关关着时不删（skipped）", st2["skipped"] is True and st2["msgs"] == 0, st2)
check("关着时记录还在", conn.execute(
    "SELECT COUNT(*) FROM qa_messages WHERE id=?", (m,)).fetchone()[0] == 1)
check("关着时附件还在", f.exists())
st3 = prunem.run(conn, force=True)
check("force 时即使关着也照做", st3["skipped"] is False and st3["msgs"] == 1, st3)
check("force 后那条被删了", conn.execute(
    "SELECT COUNT(*) FROM qa_messages WHERE id=?", (m,)).fetchone()[0] == 0)

# ---- 保留期可改 ----
prunem.set_settings(conn, enabled=True, qa_days=365, uploads_days=7)
conn.commit()
cfg = prunem.settings(conn)
check("保留期能改并读回", cfg["qa_keep_days"] == 365 and cfg["uploads_keep_days"] == 7)
prunem.set_settings(conn, enabled=True, qa_days=99999, uploads_days=0)
check("保留期被夹在 1..3650", prunem.settings(conn)["qa_keep_days"] == 3650
      and prunem.settings(conn)["uploads_keep_days"] == 1)

check("上次清理时间写进了设置", "→" in prunem.settings(conn)["last"])
check("describe 能读", "问答记录" in prunem.describe({"msgs": 1, "files": 2, "orphans": 0,
                                                    "freed": 1024, "kept_record_files": 3}))
check("关闭时 describe 说明没删", "已关闭" in prunem.describe({"skipped": True}))

print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for x in FAIL:
    print("  -", x)
sys.exit(1 if FAIL else 0)
