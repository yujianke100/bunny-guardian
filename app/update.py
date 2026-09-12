"""软件更新：检查/拉取本项目的更新，以及在需要时维护 pi 智能体。

设计取舍
- 只从**本项目自己的 origin** 拉取（默认 origin/main），不碰别的远端；
- 自动更新默认关闭：要管理员显式打开（网页里勾选），因为它会重启服务；
- 所有命令都带超时，输出截断后回给页面看，失败不抛异常、只返回原因；
- pi 智能体是可选的外部 CLI（@earendil-works/pi-coding-agent），没装就如实说"未安装"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CODE_DIR = APP_DIR.parent
PI_PACKAGE = os.environ.get("PI_PACKAGE", "@earendil-works/pi-coding-agent")
PI_BIN = os.environ.get("PI_BIN", "") or "pi"

SETTING_KEYS = {
    "auto_check": "upd_auto_check",       # "1"/"0"
    "auto_update": "upd_auto_update",     # "1"/"0"
    "interval": "upd_interval_hours",     # 自动检查间隔（小时）
    "last_check": "upd_last_check",
    "last_result": "upd_last_result",
    "branch": "upd_branch",               # 默认 main
}


def _setting(key: str, default: str = "") -> str:
    try:
        import db as dbm
        with dbm.db() as conn:
            return dbm.get_setting(conn, key, default)
    except Exception:      # noqa: BLE001
        return default


def _set(key: str, value: str) -> None:
    import db as dbm
    with dbm.db() as conn:
        dbm.set_setting(conn, key, value)
        conn.commit()


def branch() -> str:
    return (_setting(SETTING_KEYS["branch"], "") or "main").strip()


# ------------------------------------------------------------------ git

def _git(args: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", str(CODE_DIR), *args], capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "没有 git"
    except subprocess.TimeoutExpired:
        return 124, f"git 超时（{timeout}s）"


def local_commit() -> str:
    rc, out = _git(["rev-parse", "--short", "HEAD"], timeout=20)
    return out.splitlines()[0] if rc == 0 and out else ""


def local_subject() -> str:
    rc, out = _git(["log", "-1", "--pretty=%s"], timeout=20)
    return out.splitlines()[0][:80] if rc == 0 and out else ""


def check(do_fetch: bool = True) -> dict:
    """检查有没有新版本。返回 {ok, behind, ahead, local, remote, subject, error}。"""
    info = {"ok": False, "behind": 0, "ahead": 0, "local": local_commit(),
            "remote": "", "subject": local_subject(), "error": ""}
    if do_fetch:
        rc, out = _git(["fetch", "--quiet", "origin", branch()], timeout=90)
        if rc != 0:
            info["error"] = f"拉取远端信息失败：{out[:200]}"
            return info
    rc, out = _git(["rev-list", "--left-right", "--count",
                    f"HEAD...origin/{branch()}"], timeout=30)
    if rc != 0:
        info["error"] = f"无法比较版本：{out[:200]}"
        return info
    try:
        ahead, behind = (int(x) for x in out.split()[:2])
    except ValueError:
        info["error"] = f"解析版本差异失败：{out[:120]}"
        return info
    info.update({"ok": True, "ahead": ahead, "behind": behind})
    rc2, out2 = _git(["rev-parse", "--short", f"origin/{branch()}"], timeout=20)
    info["remote"] = out2.splitlines()[0] if rc2 == 0 and out2 else ""
    if behind:
        rc3, out3 = _git(["log", "-1", "--pretty=%s", f"origin/{branch()}"], timeout=20)
        info["remote_subject"] = out3.splitlines()[0][:80] if rc3 == 0 and out3 else ""
    return info


def apply_update(run_install: bool = True) -> dict:
    """拉取并应用更新（失败就原样返回原因，不做任何破坏性操作）。"""
    out: list[str] = []
    rc, txt = _git(["pull", "--ff-only", "origin", branch()], timeout=180)
    out.append(("$ git pull --ff-only origin " + branch()) + "\n" + txt[-600:])
    if rc != 0:
        return {"ok": False, "log": "\n".join(out), "restart": False}
    if run_install:
        script = CODE_DIR / "deploy" / "install.sh"
        if script.is_file():
            try:
                p = subprocess.run(["sh", str(script), "--no-deps"], capture_output=True,
                                   text=True, timeout=300, cwd=str(CODE_DIR))
                out.append("$ sh deploy/install.sh --no-deps\n" +
                           ((p.stdout or "") + (p.stderr or ""))[-600:])
            except subprocess.TimeoutExpired:
                out.append("$ sh deploy/install.sh --no-deps\n（超时，已跳过）")
        else:
            out.append("（没有 deploy/install.sh，跳过部署脚本）")
    return {"ok": True, "log": "\n".join(out), "restart": True}


def restart_service() -> str:
    """重启应用（--no-block：别把响应卡在这儿）。返回说明。"""
    slug = os.environ.get("BG_APP_SLUG", "") or "bunny-guardian"
    unit = f"{slug}.service"
    try:
        p = subprocess.run(["systemctl", "restart", "--no-block", unit],
                           capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            return f"已请求重启 {unit}"
        return "重启失败：" + ((p.stdout or "") + (p.stderr or "")).strip()[:160]
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return f"重启失败：{e}"


# ------------------------------------------------------------------ pi 智能体（可选）

def pi_state() -> dict:
    """pi 智能体的状态：装没装、什么版本、npm 上最新是什么。"""
    st = {"installed": False, "version": "", "latest": "", "package": PI_PACKAGE,
          "bin": "", "error": ""}
    path = shutil.which(PI_BIN)
    st["bin"] = path or ""
    if path:
        try:
            p = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=25)
            st["version"] = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[0][:40]
            st["installed"] = True
        except (OSError, subprocess.TimeoutExpired) as e:
            st["error"] = f"无法执行 pi：{e}"
    try:
        p = subprocess.run(["npm", "view", PI_PACKAGE, "version"], capture_output=True,
                           text=True, timeout=60)
        if p.returncode == 0:
            st["latest"] = (p.stdout or "").strip().splitlines()[0][:40]
        elif not st["error"]:
            st["error"] = ((p.stdout or "") + (p.stderr or "")).strip()[:160]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        if not st["error"]:
            st["error"] = "没有 npm（可先运行 deploy/setup-node.sh 安装用户级 Node）"
    return st


def install_pi() -> dict:
    """安装/更新 pi：走仓库里的 deploy/setup-node.sh（用户级 Node + pi）。"""
    script = CODE_DIR / "deploy" / "setup-node.sh"
    if not script.is_file():
        return {"ok": False, "log": "找不到 deploy/setup-node.sh"}
    try:
        p = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                           timeout=600, cwd=str(CODE_DIR))
        log = ((p.stdout or "") + (p.stderr or ""))[-800:]
        return {"ok": p.returncode == 0, "log": log}
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "安装超时（10 分钟），请到服务器上手动执行 deploy/setup-node.sh"}
    except OSError as e:
        return {"ok": False, "log": f"执行失败：{e}"}


# ------------------------------------------------------------------ 自动检查 / 自动更新

def auto_settings() -> dict:
    return {"auto_check": _setting(SETTING_KEYS["auto_check"], "0") == "1",
            "auto_update": _setting(SETTING_KEYS["auto_update"], "0") == "1",
            "interval": max(1, min(int(_setting(SETTING_KEYS["interval"], "6") or 6), 168)),
            "last_check": _setting(SETTING_KEYS["last_check"], ""),
            "last_result": _setting(SETTING_KEYS["last_result"], ""),
            "branch": branch(), "local": local_commit(), "subject": local_subject()}


def save_auto(auto_check: bool, auto_update: bool, interval: int) -> None:
    _set(SETTING_KEYS["auto_check"], "1" if auto_check else "0")
    _set(SETTING_KEYS["auto_update"], "1" if auto_update else "0")
    _set(SETTING_KEYS["interval"], str(max(1, min(int(interval or 6), 168))))


def due_for_check() -> bool:
    """按间隔判断该不该自动检查了（供维护任务每小时调用）。"""
    import datetime
    st = auto_settings()
    if not st["auto_check"]:
        return False
    last = st["last_check"]
    if not last:
        return True
    try:
        prev = datetime.datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.datetime.now() - prev) >= datetime.timedelta(hours=st["interval"])


def auto_tick() -> str:
    """维护任务里调用：到点就检查；开了自动更新且确实落后就更新并重启。"""
    if not due_for_check():
        return ""
    import db as dbm
    info = check()
    _set(SETTING_KEYS["last_check"], dbm.now())
    if not info.get("ok"):
        _set(SETTING_KEYS["last_result"], "检查失败：" + (info.get("error") or "")[:120])
        return "检查失败：" + (info.get("error") or "")[:120]
    if not info["behind"]:
        _set(SETTING_KEYS["last_result"], f"已是最新（{info['local']}）")
        return f"已是最新（{info['local']}）"
    msg = f"落后 {info['behind']} 个提交（{info['local']} → {info['remote']}）"
    if not auto_settings()["auto_update"]:
        _set(SETTING_KEYS["last_result"], msg + "（未开启自动更新）")
        return msg + "（未开启自动更新）"
    res = apply_update()
    if res["ok"]:
        _set(SETTING_KEYS["last_result"], msg + "，已自动更新")
        if res["restart"]:
            _set(SETTING_KEYS["last_result"], msg + "，已自动更新并重启：" + restart_service())
    else:
        _set(SETTING_KEYS["last_result"], msg + "，自动更新失败：" + res["log"][-160:])
    return _setting(SETTING_KEYS["last_result"])
