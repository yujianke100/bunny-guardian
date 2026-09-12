"""认证与权限：密码散列（stdlib scrypt）、服务端会话、按知识域授权。

安全设计
- 密码用 hashlib.scrypt 加盐散列（n=2^14, r=8），不引入额外依赖。
- 会话是服务端随机 token + HttpOnly Cookie，可随时吊销；POST 走 CSRF 双提交校验。
- 权限模型参考 LeafWiki / Wiki-Go 的做法：全局角色（admin/member）+ 按知识域(space)的
  none/view/edit 三级授权，admin 拥有全部权限。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

import db as dbm

SESSION_COOKIE = "bg_session"
SESSION_DAYS = 30
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
LEVELS = {"none": 0, "view": 1, "edit": 2}


# ------------------------------------------------------------------ 密码

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=64 * 1024 * 1024,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), dklen=len(base64.b64decode(hash_b64)),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


def random_password(length: int = 14) -> str:
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ------------------------------------------------------------------ 会话

def create_session(conn, user_id: int, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sessions(token, user_id, csrf, created_at, expires_at, ip, user_agent)"
        " VALUES(?,?,?,?,?,?,?)",
        (token, user_id, csrf, dbm.now(), expires,
         (request.client.host if request.client else ""), request.headers.get("user-agent", "")[:200]),
    )
    return token


def destroy_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def purge_expired(conn) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (dbm.now(),))


# ------------------------------------------------------------------ 用户

def get_user(conn, username: str):
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def perms_for(conn, user) -> dict[str, str]:
    """**数据权限对所有账号一律完整**。

    这套系统默认只由两个人使用（档案主人与一起记录的人），他们共同维护同一份档案，
    因此经期/疾病/就医/知识库/问答这些**数据**对所有账号都是可读可写、彼此互通。
    唯一的权限差异是**系统配置**（账号、模型 API、备份/同步、审计、智能体权限），
    由 role='admin' 控制（管理员可以设一个或两个）。space_perms 表保留但不再参与判定。
    """
    if user is None:
        return {s: "none" for s in dbm.SPACES}
    return {s: "edit" for s in dbm.SPACES}


def set_who(conn, user_id: int, who: str) -> None:
    """设置账号身份：her / him / ''（未指定）。"""
    who = who if who in ("her", "him", "") else ""
    conn.execute("UPDATE users SET who=? WHERE id=?", (who, user_id))


def set_admin(conn, user_id: int, is_admin: bool) -> None:
    conn.execute("UPDATE users SET role=? WHERE id=?",
                 ("admin" if is_admin else "member", user_id))


def set_perms(conn, user_id: int, perms: dict[str, str]) -> None:
    for space, level in perms.items():
        if space not in dbm.SPACES:
            continue
        level = level if level in LEVELS else "none"
        conn.execute(
            "INSERT INTO space_perms(user_id, space, level) VALUES(?,?,?) "
            "ON CONFLICT(user_id, space) DO UPDATE SET level=excluded.level",
            (user_id, space, level),
        )


def can(perms: dict[str, str], space: str, need: str = "view") -> bool:
    return LEVELS.get(perms.get(space, "none"), 0) >= LEVELS.get(need, 1)


# ------------------------------------------------------------------ 依赖

class CurrentUser:
    def __init__(self, conn, user, perms, token: str | None):
        self.conn = conn
        self.row = user
        self.perms = perms
        self.token = token

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def username(self) -> str:
        return self.row["username"]

    @property
    def display_name(self) -> str:
        return self.row["display_name"] or self.row["username"]

    @property
    def is_admin(self) -> bool:
        return self.row["role"] == "admin"

    def csrf(self) -> str:
        row = self.conn.execute("SELECT csrf FROM sessions WHERE token=?", (self.token,)).fetchone()
        return row["csrf"] if row else ""

    @property
    def nickname(self) -> str:
        """昵称：用户可改；留空时一律退回用户名（默认显示用户名）。"""
        try:
            return (self.row["display_name"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return ""

    @property
    def shown_name(self) -> str:
        """界面上显示的名字：有昵称用昵称，没有就用用户名。"""
        return self.nickname or self.username

    @property
    def who(self) -> str:
        """her=她（档案主人）/ him=他（一起记录的人）/ ''=未指定。"""
        try:
            return (self.row["who"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return ""

    @property
    def is_partner(self) -> bool:
        """提问的人不是她本人（男友/家人）：回答里要说清「这是转述」。"""
        return self.who == "him"

    def require(self, space: str, need: str = "view") -> None:
        if not can(self.perms, space, need):
            raise HTTPException(status_code=403, detail="无权访问该模块")

    def require_admin(self) -> None:
        if not self.is_admin:
            raise HTTPException(status_code=403, detail="需要管理员权限")

    def audit(self, action: str, detail: str = "", ip: str = "") -> None:
        dbm.audit(self.conn, self.id, self.username, action, detail, ip)


def _load(conn, token: str | None):
    if not token:
        return None
    row = conn.execute(
        "SELECT s.token, s.user_id, s.expires_at, u.* FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token=?",
        (token,),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < dbm.now() or not row["is_active"]:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    return row


def optional_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    conn = dbm.connect()
    row = _load(conn, token)
    if not row:
        conn.close()
        return None
    user = get_user_by_id(conn, int(row["user_id"]))
    return CurrentUser(conn, user, perms_for(conn, user), token)


def close_user(user) -> None:
    if user is not None:
        try:
            user.conn.close()
        except Exception:
            pass


async def require_login(request: Request):
    """FastAPI 依赖：要求已登录，否则重定向到登录页。

    数据库提交/回滚在这里统一处理：路由里通过 user.conn 执行的写入必须提交，
    否则（sqlite3 默认隔离级别）事务会随连接关闭而丢弃。
    """
    user = optional_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?next={request.url.path}"},
        )
    try:
        yield user
        user.conn.commit()
    except Exception:
        try:
            user.conn.rollback()
        except Exception:
            pass
        raise
    finally:
        close_user(user)


def check_csrf(request: Request, user, form_token: str) -> bool:
    """CSRF 校验。注意：hmac.compare_digest 对非 ASCII str 会抛 TypeError，
    表单里塞中文会 500——统一按字节比较，非法 token 一律判为不通过。"""
    token = user.csrf()
    if not form_token or not token:
        return False
    try:
        return hmac.compare_digest(form_token.encode("utf-8"), token.encode("utf-8"))
    except (TypeError, UnicodeError):
        return False


def login_redirect(next_path: str = "/dashboard") -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={next_path}", status_code=303)
