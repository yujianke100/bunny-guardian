"""数据同步：唯一的实现，网页「立即同步」按钮与「有改动就触发」共用同一份逻辑。

触发方式只有一个：**有改动就同步**（HTTP 写操作成功后排队，去抖合并，后台执行）。
没有任何定时器；进程若在去抖窗口里被杀掉，靠启动时的「待推送」标记补一次。

凭据机制（解决「怎么让系统有权读写配置的仓库」）
    1) 部署密钥（推荐，默认）：应用自己生成 ed25519 密钥对，私钥只落盘（600）不显示，
       公钥展示给用户，用户加到 GitHub 仓库的 Deploy key（勾 write access）。
       git 操作通过 GIT_SSH_COMMAND 指定这把私钥 + IdentitiesOnly，不依赖运行账号的环境凭据。
    2) 访问令牌（可选）：细粒度 PAT（Contents: write），存独立文件（600、不回显），
       仅对 https 形式的仓库地址生效（运行时拼进 URL，输出里一律掩码）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbm          # noqa: E402
import kb as kbm          # noqa: E402

DATA = Path(dbm.DB_PATH).parent
SSH_DIR = DATA / "ssh"
KEY_PATH = SSH_DIR / "id_ed25519"
PUB_PATH = SSH_DIR / "id_ed25519.pub"
KNOWN_HOSTS = SSH_DIR / "known_hosts"
# 网页里配置的凭据落到**项目根目录的 .env**（600，已 gitignore）：
#   SYNC_SSH_KEY=<私钥路径>   SYNC_TOKEN=<访问令牌>
ENV_FILE = Path(os.environ.get("BG_ENV_FILE")
                or (Path(__file__).resolve().parent.parent / ".env"))

SECRET_RE = re.compile(r"(https?://)[^@/\s]+:[^@/\s]+@")


def mask(text: str) -> str:
    """输出掩码：任何 https://user:pass@ 形式都替换掉，避免令牌进日志/页面。"""
    return SECRET_RE.sub(r"\1***:***@", text or "")


# ------------------------------------------------------------------ 凭据

def has_key() -> bool:
    return KEY_PATH.is_file()


def public_key() -> str:
    try:
        return PUB_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_env() -> dict:
    """读取项目根目录的 .env（只认识 KEY=VALUE，忽略注释）。"""
    out: dict[str, str] = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def env_set(key: str, value: str) -> None:
    """安全写入 .env 中的某个键（保留其他行；文件 600）。"""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if ENV_FILE.is_file():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    tmp = ENV_FILE.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines).strip() + "\n").encode())
    finally:
        os.close(fd)
    os.replace(tmp, ENV_FILE)
    os.chmod(ENV_FILE, 0o600)


def token() -> str:
    return load_env().get("SYNC_TOKEN", "")


def token_hint() -> str:
    t = token()
    return ("…" + t[-4:]) if len(t) >= 8 else ("已设置" if t else "")


def save_token(secret: str) -> None:
    env_set("SYNC_TOKEN", secret.strip())


def configured_key() -> str:
    """网页/`.env` 里指定的私钥路径；没有则返回空（表示用系统自带凭据）。"""
    path = load_env().get("SYNC_SSH_KEY", "").strip()
    if path and Path(path).is_file():
        return path
    return ""


def cred_mode(conn) -> str:
    """凭据模式：auto=用运行账号自带凭据（默认）；key=用部署密钥；token=用访问令牌。"""
    m = dbm.get_setting(conn, "sync_cred_mode", "auto") or "auto"
    return m if m in ("auto", "key", "token") else "auto"


def keygen(comment: str = "bunny-guardian sync") -> tuple[bool, str]:
    """生成部署密钥。返回 (是否新生成, 公钥或错误信息)。"""
    if has_key():
        return False, public_key()
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment,
                        "-f", str(KEY_PATH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr).strip()[:200]
    os.chmod(KEY_PATH, 0o600)
    os.chmod(PUB_PATH, 0o644)
    return True, public_key()


# ------------------------------------------------------------------ git 调用

def git_env(mode: str = "auto") -> dict:
    """构造 git 运行环境。

    凭据优先级：**显式配置 > 运行账号自带**。
      - auto（默认）：若 .env 里配了 SYNC_SSH_KEY / SYNC_TOKEN 则用，否则完全用运行账号
        自己的凭据（~/.ssh、credential helper、gh 等）——不做任何覆盖。
      - key：用部署密钥（.env 的 SYNC_SSH_KEY，或应用生成的 data/ssh/id_ed25519）。
      - token：用访问令牌（.env 的 SYNC_TOKEN，仅 https 地址）。
    """
    env = os.environ.copy()
    key = configured_key()
    if mode == "key" and not key and has_key():
        key = str(KEY_PATH)
    if mode == "auto" and os.environ.get("BG_SYNC_SSH_KEY"):
        key = os.environ["BG_SYNC_SSH_KEY"]
    if key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={KNOWN_HOSTS} -o BatchMode=yes")
    env.setdefault("GIT_AUTHOR_NAME", os.environ.get("BG_GIT_AUTHOR_NAME", "bunny-guardian"))
    env.setdefault("GIT_AUTHOR_EMAIL", os.environ.get("BG_GIT_AUTHOR_EMAIL",
                                                      "bunny-guardian@localhost"))
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    # 只允许非交互：宁可直接失败，也不要卡住等密码
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def effective_remote(repo: str, mode: str = "auto") -> str:
    """https 仓库 + 启用令牌 → 运行时拼接令牌（只在进程内存在，不落盘、输出掩码）。"""
    tok = token() if mode in ("auto", "token") else ""
    if tok and repo.startswith("https://"):
        return repo.replace("https://", f"https://x-access-token:{tok}@", 1)
    return repo


def git(args: list[str], cwd: Path, mode: str = "auto") -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env=git_env(mode))
    return r.returncode, mask((r.stdout + r.stderr).strip())


# ------------------------------------------------------------------ 设置

def settings(conn) -> dict:
    return {
        "repo": dbm.get_setting(conn, "kb_repo", ""),
        "branch": dbm.get_setting(conn, "kb_branch", "main") or "main",
        "enabled": dbm.get_setting(conn, "kb_sync_enabled", "1") == "1",
        # 默认关闭：快照写在数据仓的 data/ 下，而数据仓 .gitignore 排除 data/，
        # 所以它推不出去、只是同盘多一份副本（占地方又没有异地价值）。
        # 私人记录的异地留档由「导出 markdown + 提交推送」负责，不需要它。
        "snapshot": dbm.get_setting(conn, "kb_snapshot_db", "0") == "1",
        "last": dbm.get_setting(conn, "kb_last_sync", ""),
        "last_test": dbm.get_setting(conn, "kb_last_test", ""),
        "mode": cred_mode(conn),
        "key": has_key(),
        "pub": public_key(),
        "env_key": configured_key(),
        "token": bool(token()),
        "token_hint": token_hint(),
        "env_file": str(ENV_FILE),
        "env_exists": ENV_FILE.is_file(),
    }


# ------------------------------------------------------------------ 主要动作

def test_access(conn, repo: str | None = None) -> tuple[bool, str]:
    """探测仓库可达性与凭据是否有效（git ls-remote）。"""
    repo = (repo or settings(conn)["repo"]).strip()
    if not repo:
        return False, "还没有配置仓库地址"
    mode = cred_mode(conn)
    if not Path(kbm.KB_DIR).is_dir():
        Path(kbm.KB_DIR).mkdir(parents=True, exist_ok=True)
    rc, out = git(["ls-remote", "--exit-code", effective_remote(repo, mode), "HEAD"],
                  Path(kbm.KB_DIR), mode)
    ok = rc == 0
    if not ok:
        if mode == "auto" and not token() and not configured_key():
            out += "\n→ 当前是「用系统自带凭据」模式：确认运行账号本机已能访问该仓库（ssh -T git@github.com 或已登录 gh）"
        if "Permission denied" in out or "publickey" in out:
            out += "\n→ 若用的是部署密钥：请把公钥加到该仓库的 Deploy key（勾 write access）"
        elif "Could not resolve host" in out or "Connection" in out:
            out += "\n→ 网络不可达或地址写错"
    stamp = dbm.now()
    first = (out.splitlines()[0] if out.strip() else f"git 退出码 {rc}")
    dbm.set_setting(conn, "kb_last_test",
                    f"{stamp} → {'可达' if ok else '失败：' + first[:120]}")
    return ok, out


def ensure_repo(conn, repo: str, branch: str, mode: str = "auto") -> list[str]:
    """保证知识库目录是本仓库的工作副本（含 remote 指向）。"""
    steps: list[str] = []
    kdir = Path(kbm.KB_DIR)
    kdir.mkdir(parents=True, exist_ok=True)
    if not (kdir / ".git").is_dir():
        rc, out = git(["init", "-q", "-b", branch], kdir, mode)
        steps.append("init" if rc == 0 else f"init失败:{out[:60]}")
    rc, out = git(["remote", "get-url", "origin"], kdir, mode)
    if rc != 0:
        rc, out = git(["remote", "add", "origin", repo], kdir, mode)
        steps.append("remote+" if rc == 0 else f"remote失败:{out[:60]}")
    elif out.strip() != repo and not out.strip().startswith(repo.split("@")[0]):
        rc, _ = git(["remote", "set-url", "origin", repo], kdir, mode)
        steps.append("remote~" if rc == 0 else "remote~失败")
    return steps


def run_sync(conn, do_snapshot: bool | None = None, push: bool = True) -> tuple[str, int]:
    """导出数据库 → 提交 → 推送。返回 (摘要, 导出篇数)。"""
    cfg = settings(conn)
    repo, branch = cfg["repo"], cfg["branch"]
    if not cfg["enabled"]:
        # 网页开关关闭时，自动触发与手动按钮都不同步：数据只留在本地
        dbm.set_setting(conn, "kb_last_sync", f"{dbm.now()} → 已关闭同步（数据仅在本地）")
        return "已关闭同步（数据仅在本地）", 0
    if not repo:
        return "未配置仓库地址", 0
    if do_snapshot is None:
        do_snapshot = cfg["snapshot"]
    mode = cred_mode(conn)
    steps = ensure_repo(conn, repo, branch, mode)

    rc, out = git(["fetch", "--quiet", "origin", branch], Path(kbm.KB_DIR), mode)
    if rc == 0:
        rc, out = git(["merge", "--ff-only", "--quiet", f"origin/{branch}"], Path(kbm.KB_DIR), mode)
        if rc == 0:
            steps.append("ff")

    n_docs = kbm.export_to_dir(conn, Path(kbm.KB_DIR) / "docs", scope=kbm.SCOPE_PERSONAL)
    steps.append(f"导出{n_docs}篇")

    if do_snapshot:
        try:
            snap = Path(kbm.KB_DIR) / "data" / "bunny-guardian.db"
            snap.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(dbm.DB_PATH)) as src, sqlite3.connect(str(snap)) as dst:
                src.backup(dst)
            steps.append("快照")
        except Exception as e:  # noqa: BLE001
            steps.append(f"快照失败:{str(e)[:40]}")

    git(["add", "-A"], Path(kbm.KB_DIR), mode)
    rc, out = git(["status", "--porcelain"], Path(kbm.KB_DIR), mode)
    if out:
        rc, out2 = git(["commit", "-q", "-m", f"sync: {dbm.now()}（{n_docs} 篇文档）"],
                       Path(kbm.KB_DIR), mode)
        steps.append("commit" if rc == 0 else f"commit失败:{out2.splitlines()[0][:60]}")
        if push:
            rc, out3 = git(["push", "--quiet", "origin", f"HEAD:{branch}"], Path(kbm.KB_DIR), mode)
            steps.append("push✓" if rc == 0 else f"push失败:{out3.splitlines()[0][:80]}")
    else:
        steps.append("无变化")

    summary = "　".join(steps)
    dbm.set_setting(conn, "kb_last_sync", f"{dbm.now()} → {summary}")
    return summary, n_docs


# ------------------------------------------------------------------ 变更触发（唯一方式）

_SCHED_LOCK = threading.Lock()
_TIMER = None
_DEBOUNCE = float(os.environ.get("BG_SYNC_DEBOUNCE", "25"))
# 「还有改动没推出去」的标记：排队时置 1，推送成功才清 0。
# 它是没有定时器之后的唯一补网手段（进程在去抖窗口里被杀掉时用）。
PENDING_KEY = "kb_sync_pending"
_STATE: dict = {"pending": False, "reason": "", "at": "", "runs": 0, "last": ""}


def schedule(reason: str = "", delay: float | None = None) -> bool:
    """数据变了就排队同步一次（唯一的自动触发方式，没有任何定时器）。

    - 去抖：默认 25 秒内的多次改动合并成一次（连续打勾/改配置不会打一堆提交）
    - 后台线程：不阻塞请求；失败只记录，不影响这次写入
    - 关掉同步（网页开关）或关掉本机制（BG_SYNC_ON_CHANGE=0）时直接返回 False
    - 排队时留一个「待推送」标记：进程若在去抖窗口里被杀掉，
      下次启动由 retry_pending_on_start() 补一次（没有定时器可兜底）
    """
    global _TIMER
    if os.environ.get("BG_SYNC_ON_CHANGE", "1") != "1":
        return False
    try:
        dbm.init_db()
        with dbm.db() as conn:
            if not settings(conn)["enabled"]:
                return False
            dbm.set_setting(conn, PENDING_KEY, "1")
    except Exception:      # noqa: BLE001 - 读不到配置就别自动跑
        return False
    wait = _DEBOUNCE if delay is None else max(0.0, float(delay))
    with _SCHED_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = threading.Timer(wait, _run_once, args=(reason,))
        _TIMER.daemon = True
        _TIMER.start()
        _STATE.update({"pending": True, "reason": reason or "数据变更", "at": dbm.now()})
    return True


def pending() -> dict:
    """当前有没有排队中的同步（给界面/测试看）。"""
    return dict(_STATE)


def _run_once(reason: str = "") -> None:
    global _TIMER
    with _SCHED_LOCK:
        _TIMER = None
        _STATE["pending"] = False
    summary = ""
    try:
        dbm.init_db()
        with dbm.db() as conn:
            summary, _n = run_sync(conn)
            dbm.set_setting(conn, "kb_last_trigger",
                            f"{dbm.now()}　触发：{reason or '数据变更'}　→　{summary[:140]}")
            # 真的推出去了才清「待推送」标记。
            # 注意：不能只看「没有失败字样」——仓库没配好、同步被关掉时 summary 里
            # 也没有失败字样，但一个字节都没推出去，标记必须留着等下次启动补。
            if "push✓" in summary or "无变化" in summary or "已关闭同步" in summary:
                dbm.set_setting(conn, PENDING_KEY, "0")
            conn.commit()
    except Exception as e:  # noqa: BLE001
        summary = f"失败：{str(e)[:120]}"
        try:
            with dbm.db() as conn:
                dbm.set_setting(conn, "kb_last_trigger",
                                f"{dbm.now()}　触发：{reason or '数据变更'}　→　{summary}")
                conn.commit()
        except Exception:  # noqa: BLE001
            pass
    with _SCHED_LOCK:
        _STATE["runs"] = int(_STATE.get("runs", 0)) + 1
        _STATE["last"] = summary[:160]


def retry_pending_on_start() -> bool:
    """启动补同步：上次退出时还有改动没来得及推出去（去抖窗口里重启/崩溃）。

    定时器取消之后，这是唯一的补网手段，而且**不引入任何周期性动作**：
    库里没有「待推送」标记时直接返回 False，不会产生网络请求。
    """
    try:
        dbm.init_db()
        with dbm.db() as conn:
            if dbm.get_setting(conn, PENDING_KEY, "0") != "1":
                return False
    except Exception:      # noqa: BLE001
        return False
    return schedule("上次退出时未完成的同步", delay=10)


# ------------------------------------------------------------------ CLI（手动运维/排障）

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    dbm.init_db()
    with dbm.db() as conn:
        if cmd == "run":
            summary, _ = run_sync(conn)
            print(f"[sync] {summary}")
        elif cmd == "test":
            ok, out = test_access(conn)
            print(f"[sync] {'可达' if ok else '失败'}：{out[:300]}")
            return 0 if ok else 1
        elif cmd == "keygen":
            created, info = keygen()
            print(("[sync] 已生成部署密钥\n" if created else "[sync] 已存在部署密钥\n") + info)
        elif cmd == "status":
            with dbm.db() as c:
                print(json.dumps(settings(c), ensure_ascii=False, indent=2))
        else:
            print("用法：sync_job.py [run|test|keygen|status]", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
