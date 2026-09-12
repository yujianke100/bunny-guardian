"""SQLite 数据层：连接、建表、常用查询。

设计要点
- 单文件 SQLite，WAL 模式，适合低流量个人应用（云主机仅 961MB 内存）。
- 个人健康数据只存在服务器本地库；导出到知识库仓库由用户显式触发。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
DB_PATH = Path(os.environ.get("BG_DB", REPO_DIR / "data" / "bunny.db"))

# 知识域（用于权限控制）
SPACES = {
    "dashboard": "总览",
    "cycle": "经期记录",
    "conditions": "疾病档案",
    "visits": "就医记录",
    "records": "病历与就医",       # 疾病事件 + 就医合成的时间轴（页面用这个键做导航与权限）
    "knowledge": "医学知识",
    "qa": "智能问答",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',          -- admin | member
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    csrf TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS space_perms (
    user_id INTEGER NOT NULL,
    space TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'none',           -- none | view | edit
    PRIMARY KEY (user_id, space)
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sex TEXT DEFAULT 'female',
    birth_year INTEGER,
    height_cm REAL,
    weight_kg REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    flow TEXT DEFAULT '',                          -- light | medium | heavy
    symptoms TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    kind TEXT NOT NULL,                            -- symptom | mood | flow | medication | note | temperature | weight | spotting | pain
    name TEXT DEFAULT '',
    severity INTEGER,                              -- 1..5
    value TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',         -- active | monitoring | resolved
    onset_date TEXT,
    diagnosed_date TEXT,
    hospital TEXT DEFAULT '',
    department TEXT DEFAULT '',
    doctor TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS condition_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    kind TEXT NOT NULL,                            -- visit | exam | medication | surgery | symptom | note
    title TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    result TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    visit_date TEXT NOT NULL,
    hospital TEXT DEFAULT '',
    department TEXT DEFAULT '',
    doctor TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    findings TEXT DEFAULT '',
    diagnosis TEXT DEFAULT '',
    plan TEXT DEFAULT '',
    cost REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_kind TEXT NOT NULL,                        -- cycle | condition | event | visit | qa
    ref_id INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL,
    mime TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    stored_name TEXT NOT NULL,
    uploaded_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '新对话',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,                            -- user | assistant
    content TEXT NOT NULL,
    meta TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    ip TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,          -- 相对 docs/ 的路径，如 医学知识/痛经.md
    title TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',    -- JSON 数组
    aliases TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,                 -- Markdown 正文（不含 frontmatter）
    updated_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'import',  -- import | web | agent
    scope TEXT NOT NULL DEFAULT 'personal'  -- personal=随数据仓库同步 | general=本地通用知识
);

CREATE TABLE IF NOT EXISTS kb_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cycles_profile ON cycles(profile_id, start_date);
CREATE INDEX IF NOT EXISTS idx_daylogs_profile ON day_logs(profile_id, log_date);
CREATE INDEX IF NOT EXISTS idx_events_condition ON condition_events(condition_id, event_date);
CREATE INDEX IF NOT EXISTS idx_qa_msgs ON qa_messages(session_id, id);

"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def connect() -> sqlite3.Connection:
    """新建连接。

    check_same_thread=False：FastAPI 的同步路由跑在线程池里，而依赖（require_login）
    在事件循环线程中建立连接，二者线程不同。CPython 的 sqlite3 在 SQLITE_THREADSAFE=1
    构建下是 serialized 模式（sqlite3.threadsafety==3），配合 WAL 与 busy_timeout 可安全共享。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # PRAGMA 尽力而为：在只读挂载（如沙箱里只读的配置目录）下 WAL 无法设置，
    # 但读取仍然可用——这对「智能体只读配置」的场景是必需的。
    for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON",
                   "PRAGMA busy_timeout=8000"):
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            pass
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """轻量迁移：列不存在时补上（SQLite 的 ADD COLUMN 没有 IF NOT EXISTS）。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _extension_schemas() -> list[tuple[str, str]]:
    """扩展注册的建表 SQL（「里」的 trpg_* 等）。没有扩展时是空列表。"""
    try:
        import extensions as extm
        return extm.schemas()
    except Exception:          # noqa: BLE001
        return []


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # 扩展（里）自己的表：表的建表脚本先跑，扩展的随后
        for _name, _sql in _extension_schemas():
            try:
                conn.executescript(_sql)
            except Exception as e:                 # noqa: BLE001
                print(f"[ext] 建表失败（{_name}）：{e}", flush=True)
        # 账号身份：her=她（档案主人）/ him=他（一起记录的人）/ ''=未指定
        _ensure_column(conn, "users", "who", "who TEXT NOT NULL DEFAULT ''")
        # 会话上下文管理：摘要、归档标记、消息级归档
        _ensure_column(conn, "qa_sessions", "summary", "summary TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "qa_sessions", "archived", "archived INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "qa_sessions", "compressed_at", "compressed_at TEXT DEFAULT ''")
        _ensure_column(conn, "qa_messages", "archived", "archived INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "kb_docs", "scope", "scope TEXT NOT NULL DEFAULT 'personal'")


# ---------------------------------------------------------------- 便捷查询

def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def primary_profile(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()


def ensure_profile(conn, name: str = "档案主人") -> int:
    row = primary_profile(conn)
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO profiles(name, sex, created_at) VALUES(?, 'female', ?)",
        (name, now()),
    )
    return int(cur.lastrowid)


def audit(conn, user_id, username: str, action: str, detail: str = "", ip: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log(user_id, username, action, detail, ip, created_at) VALUES(?,?,?,?,?,?)",
        (user_id, username, action, detail[:500], ip, now()),
    )


def cycle_starts(conn, profile_id: int) -> list[date]:
    rows = conn.execute(
        "SELECT start_date FROM cycles WHERE profile_id=? ORDER BY start_date",
        (profile_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(date.fromisoformat(r["start_date"]))
        except ValueError:
            continue
    return out


def cycle_durations(conn, profile_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT start_date, end_date FROM cycles WHERE profile_id=? "
        "AND end_date IS NOT NULL AND end_date<>'' ORDER BY start_date",
        (profile_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            d = (date.fromisoformat(r["end_date"]) - date.fromisoformat(r["start_date"])).days + 1
        except ValueError:
            continue
        if 1 <= d <= 15:
            out.append(d)
    return out
