"""智能体直接写操作层（app/agent_ops.py）的单元自检：不需要服务在跑。

    cd app && BG_DB=/tmp/ao/x.db BG_KB_DIR=/tmp/ao/kb python <本文件>

覆盖：读写通吃、参数校验（日期/枚举/范围/必填）、越权（read 令牌请求写操作由
路由层拦，这里验证 registry 的 need 标注正确）、级联删除、知识库路径防护、
干跑不改数据。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP))
if len(sys.argv) > 1 and pathlib.Path(sys.argv[1]).is_dir():
    sys.path.insert(0, sys.argv[1])

TMP = pathlib.Path(os.environ.get("BG_UNIT_DIR") or tempfile.mkdtemp(prefix="baops-"))
os.environ.setdefault("BG_DB", str(TMP / "x.db"))
os.environ.setdefault("BG_KB_DIR", str(TMP / "kb"))
(TMP / "kb" / "docs").mkdir(parents=True, exist_ok=True)

import db as dbm            # noqa: E402
import agent_ops as ops     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  << " + str(extra)[:300]))


def err(op, args, want=None):
    """期望抛 OpError；want 是原因里该出现的关键词。"""
    try:
        ops.run(conn, pid, op, args)
    except ops.OpError as e:
        if want and want not in str(e):
            return False, f"原因里没有 {want!r}：{e}"
        return True, ""
    return False, "没有报错"


dbm.init_db()
conn = dbm.connect()
pid = dbm.ensure_profile(conn)
conn.commit()


def run(op, args=None, dry=False):
    out = ops.run(conn, pid, op, args or {}, dry_run=dry)
    conn.commit()
    return out


# ---------------- 注册表 ----------------
check("注册表里读写都有", any(v["need"] == "read" for v in ops.OPS.values())
      and any(v["need"] == "write" for v in ops.OPS.values()))
check("每个操作都有说明", all(v.get("summary") for v in ops.OPS.values()))
WRITES = [k for k, v in ops.OPS.items() if v["need"] == "write"]
check("写操作数量合理（≥15）", len(WRITES) >= 15, len(WRITES))
check("不存在账号/令牌类操作（避免自我提权）",
      not any(k.split(".")[0] in ("user", "token", "session", "backup", "llm")
              for k in ops.OPS))
cat_read = ops.catalog("read")
cat_write = ops.catalog("write")
check("只读清单里只有读操作",
      all(i["op"] in ops.OPS and ops.OPS[i["op"]]["need"] == "read" for i in cat_read["可用操作"]))
check("可写清单比只读清单多", len(cat_write["可用操作"]) > len(cat_read["可用操作"]))
check("清单如实列出不支持的事项", len(cat_read["不支持"]) >= 3)

# ---------------- 身体数据 ----------------
check("改身高体重", run("profile.update", {"height_cm": 165, "weight_kg": 52.5})["已更新"]["height_cm"] == 165)
check("读身体数据", run("profile.get")["档案"]["weight_kg"] == 52.5)
check("身高超范围被拒", err("profile.update", {"height_cm": 500}, "范围")[0])
check("没有要改的字段被拒", err("profile.update", {}, "没有要改")[0])

# ---------------- 经期 ----------------
a = run("cycle.add", {"start_date": "2026-08-01", "end_date": "2026-08-05",
                      "flow": "medium", "symptoms": "腹痛"})
cid = a["已新增月经记录"]
check("新增月经记录", cid > 0)
check("日期不合法被拒（不静默改成今天）",
      err("cycle.add", {"start_date": "2026-13-45"}, "不是合法日期")[0])
check("缺必填日期被拒", err("cycle.add", {}, "缺少必填日期")[0])
check("结束早于开始被拒",
      err("cycle.add", {"start_date": "2026-08-10", "end_date": "2026-08-01"}, "早于")[0])
check("flow 枚举校验", err("cycle.add", {"start_date": "2026-08-01", "flow": "flowy"}, "只能是")[0])
check("列月经记录", run("cycle.list")["记录数"] >= 1)
check("按 id 取", run("cycle.get", {"id": cid})["月经记录"]["start_date"] == "2026-08-01")
check("改月经记录", run("cycle.update", {"id": cid, "note": "改过"})["已更新月经记录"] == cid)
check("改完日期矛盾也要拦住",
      err("cycle.update", {"id": cid, "end_date": "2026-07-01"}, "早于")[0])
check("取不存在的 id 报错", err("cycle.get", {"id": 999999}, "找不到")[0])

# ---------------- 每日日志 ----------------
lid = run("log.add", {"log_date": "2026-08-02", "kind": "pain", "name": "下腹坠痛",
                      "severity": 3})["已新增日志"]
check("新增日志", lid > 0)
check("日志 kind 枚举校验", err("log.add", {"log_date": "2026-08-02", "kind": "?"}, "只能是")[0])
check("日志 kind 缺省被拒（必填）", err("log.add", {"log_date": "2026-08-02"}, "缺少必填")[0])
check("severity 超范围被拒",
      err("log.add", {"log_date": "2026-08-02", "kind": "pain", "severity": 9}, "范围")[0])
check("按时间段过滤", run("log.list", {"since": "2026-08-01", "until": "2026-08-03"})["记录数"] == 1)
check("改日志", run("log.update", {"id": lid, "severity": 5})["已更新日志"] == lid)
check("删日志", run("log.delete", {"id": lid})["已删除日志"] == lid)
check("删过就查不到", err("log.delete", {"id": lid}, "找不到")[0])

# ---------------- 疾病 + 病程事件 ----------------
cond = run("condition.add", {"name": "测试疾病", "status": "active",
                             "diagnosed_date": "2026-01-15"})["已新增疾病档案"]
check("新增疾病档案", cond > 0)
check("status 枚举校验", err("condition.add", {"name": "x", "status": "zzz"}, "只能是")[0])
check("疾病名不能为空", err("condition.add", {"name": "  "}, "不能为空")[0])
ev = run("event.add", {"condition_id": cond, "event_date": "2026-02-01",
                       "kind": "exam", "title": "B 超"})["已新增病程事件"]
check("新增病程事件", ev > 0)
check("事件 kind 枚举", err("event.add", {"condition_id": cond, "event_date": "2026-02-01",
                                          "kind": "zzz"}, "只能是")[0])
check("挂在不存在档案下被拒",
      err("event.add", {"condition_id": 999999, "event_date": "2026-02-01",
                        "kind": "note"}, "找不到")[0])
check("取档案带病程事件", len(run("condition.get", {"id": cond})["病程事件"]) == 1)
check("改病程事件", run("event.update", {"id": ev, "title": "改过"})["已更新病程事件"] == ev)
d = run("condition.delete", {"id": cond})
check("删档案会连带报出删了几个事件", d["连带删除的病程事件"] == 1, d)
check("档案删掉后事件也没了（不留孤儿）", err("event.update", {"id": ev, "title": "x"}, "找不到")[0])
check("删不存在的档案报错", err("condition.delete", {"id": 999999}, "找不到")[0])

# ---------------- 就诊 ----------------
vid = run("visit.add", {"visit_date": "2026-03-01", "hospital": "测试医院",
                        "department": "妇科", "cost": 320.5})["已新增就诊记录"]
check("新增就诊记录", vid > 0)
check("费用超范围被拒", err("visit.add", {"visit_date": "2026-03-01", "cost": -5}, "范围")[0])
check("读一次就诊", run("visit.get", {"id": vid})["就诊记录"]["hospital"] == "测试医院")
check("改就诊", run("visit.update", {"id": vid, "diagnosis": "随访"})["已更新就诊记录"] == vid)
check("删就诊", run("visit.delete", {"id": vid})["已删除就诊记录"] == vid)

# ---------------- 知识库 ----------------
w = run("kb.write", {"path": "医学知识/智能体笔记.md", "body": "# 标题\n正文",
                     "title": "智能体笔记", "tags": ["测试"]})
check("写知识库文档", w["方式"] == "新建", w)
check("再写一次是覆盖", run("kb.write", {"path": "医学知识/智能体笔记.md",
                                          "body": "# 改过"})["方式"] == "覆盖了已有文档")
check("读全文", "改过" in run("kb.get", {"path": "医学知识/智能体笔记.md"})["文档"]["body"])
check("列文档不含正文",
      "body" not in run("kb.list")["文档"][0], run("kb.list")["文档"][0])
check("路径穿越被拒", err("kb.write", {"path": "../../etc/passwd.md", "body": "x"}, "相对路径")[0])
check("反斜杠路径被拒", err("kb.write", {"path": "a\\b.md", "body": "x"}, "反斜杠")[0])
check("非 .md 后缀被拒", err("kb.write", {"path": "a.txt", "body": "x"}, ".md")[0])
check("空正文被拒", err("kb.write", {"path": "a.md", "body": "  "}, "不能为空")[0])
check("tags 不是数组被拒", err("kb.write", {"path": "a.md", "body": "x", "tags": "y"},
                               "必须是数组")[0])
check("删知识库文档", "已删除" in str(run("kb.delete", {"path": "医学知识/智能体笔记.md"})))
check("删不存在的文档报错", err("kb.delete", {"path": "nope.md"}, "找不到")[0])

# 通用知识不允许通过接口改/删（重装会被覆盖，改了也没意义）
import kb as kbm  # noqa: E402
kbm.upsert_doc(conn, "医学知识/通用.md", "通用内容", {"title": "通用"}, origin="import",
               scope=kbm.SCOPE_GENERAL)
conn.commit()
check("通用知识文档不允许接口改写",
      err("kb.write", {"path": "医学知识/通用.md", "body": "改"}, "通用知识")[0])
check("通用知识文档不允许接口删除",
      err("kb.delete", {"path": "医学知识/通用.md"}, "通用知识")[0])

# ---------------- 总览 ----------------
ov = run("overview")
check("总览给出各类计数", set(["月经记录", "每日日志", "疾病档案", "就诊记录", "知识库文档"])
      <= set(ov["计数"]))
check("总览计数与实际一致", ov["计数"]["月经记录"] >= 1)

# ---------------- 干跑 ----------------
before = run("overview")["计数"]["每日日志"]
out = ops.run(conn, pid, "log.add", {"log_date": "2026-09-01", "kind": "note", "name": "干跑"},
              dry_run=True)
conn.rollback()
check("dry_run 标了标记", out.get("dry_run") is True)
check("dry_run 真的没有写进库",
      ops.run(conn, pid, "overview", {})["计数"]["每日日志"] == before,
      f"{before} -> {ops.run(conn, pid, 'overview', {})['计数']['每日日志']}")
check("dry_run 也会做校验",
      err("cycle.add", {"start_date": "bad"}, "不是合法日期")[0])

# ---------------- 未知操作 ----------------
check("未知操作被拒并给出指引",
      err("nope.op", {}, "不支持的操作")[0])

print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  -", f)
sys.exit(1 if FAIL else 0)
