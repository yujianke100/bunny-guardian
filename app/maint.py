"""定时维护：问答会话压缩/清理 + 问答记录与附件的按保留期删除 + 知识库目录刷新。

由 `<slug>-maint.timer` 每小时触发（不是每次改动都跑）。

上下文策略由后端决定（见 chat.py），这里只负责按时执行。
知识库目录是「wiki 的入口页」：列出所有文档与标签，内容变了才写库（避免无意义改动）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat as chatm     # noqa: E402
import db as dbm         # noqa: E402
import kb as kbm         # noqa: E402
import llm as llmm       # noqa: E402
import prune as prunem   # noqa: E402
import update as updm    # noqa: E402

INDEX_REL = f"{kbm.KNOWLEDGE_DIR}/INDEX.md"


def refresh_index(conn) -> str:
    """重写知识库目录（内容没变就不动库）。返回动作说明。"""
    body = kbm.index_md()
    old = kbm.read_text(INDEX_REL)
    if old == body:
        return "目录未变"
    kbm.upsert_doc(conn, INDEX_REL, body,
                   {"title": "知识库目录", "tags": ["目录", "索引"],
                    "source": "自动生成", "status": "current",
                    "last_updated": dbm.today()},
                   origin="maint", scope=kbm.SCOPE_PERSONAL)
    return f"目录已更新（{len(body)} 字）"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    dbm.init_db()
    with dbm.db() as conn:
        if cmd == "run":
            # 传 llm：能连上模型就写一句更连贯的摘要，连不上自动退回抽取式摘要
            st = chatm.cleanup(conn, llm=llmm)
            print(f"[maint] 压缩会话 {st['compressed_sessions']}"
                  f"（{st['compressed_msgs']} 条消息）　清理 {st['purged']} 条 ")
            # 问答记录与随问答上传的附件按保留期删除（见 prune.py 的边界说明）
            print(f"[maint] {prunem.describe(prunem.run(conn))}")
            print(f"[maint] {refresh_index(conn)}")
            msg = updm.auto_tick()
            if msg:
                print(f"[maint] 更新检查：{msg[:160]}")
        elif cmd == "index":
            print(f"[maint] {refresh_index(conn)}")
            msg = updm.auto_tick()
            if msg:
                print(f"[maint] 更新检查：{msg[:160]}")
        elif cmd == "status":
            print(json.dumps(chatm.settings(), ensure_ascii=False, indent=2))
        else:
            print("用法：maint.py [run|index|status]", file=sys.stderr)
            sys.exit(2)
