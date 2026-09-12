"""数据备份与恢复。

设计
    - 备份 = SQLite 一致性快照（用 sqlite3 的 backup API，含 WAL 中未落盘的写入）；
      可选把照片（uploads/）一起打成 .tar.gz。
    - 备份文件落在 <数据目录>/backups/（已 gitignore），权限 600；保留最近 N 份。
    - 恢复前**自动把当前库另存一份**（pre-restore-*.db），避免恢复错了不可挽回。
    - 定时：由用户级/系统级 timer 每小时触发一次 `backup.py run`，
      由本模块根据设置里的频率（每天/每周）与上次备份时间决定「这次要不要备」——
      这样改频率不需要重写 systemd 单元。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as dbm          # noqa: E402
import kb as kbm          # noqa: E402

DATA = Path(dbm.DB_PATH).parent
BACKUP_DIR = DATA / "backups"
UPLOADS = DATA / "uploads"
SQLITE_MAGIC = b"SQLite format 3\x00"


# ------------------------------------------------------------------ 设置

def settings(conn) -> dict:
    return {
        "enabled": dbm.get_setting(conn, "backup_enabled", "1") == "1",
        "interval": dbm.get_setting(conn, "backup_interval", "daily") or "daily",
        "keep": int(dbm.get_setting(conn, "backup_keep", "7") or 7),
        "with_uploads": dbm.get_setting(conn, "backup_uploads", "0") == "1",
        "last": dbm.get_setting(conn, "last_backup", ""),
        "last_restore": dbm.get_setting(conn, "last_restore", ""),
        "dir": str(BACKUP_DIR),
    }


def _due(conn) -> tuple[bool, str]:
    cfg = settings(conn)
    if not cfg["enabled"]:
        return False, "定时备份已关闭"
    last = dbm.get_setting(conn, "last_backup_at", "")
    if not last:
        return True, "尚无备份"
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True, "上次备份时间无法解析"
    need = timedelta(days=7 if cfg["interval"] == "weekly" else 1)
    if datetime.now() - last_dt >= need:
        return True, "已到期"
    return False, "未到期"


# ------------------------------------------------------------------ 备份

def list_backups() -> list[dict]:
    out = []
    if not BACKUP_DIR.is_dir():
        return out
    for f in sorted(BACKUP_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not f.is_file():
            continue
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size,
                    "size_h": f"{st.st_size / 1048576:.2f}MB" if st.st_size > 1048576
                              else f"{st.st_size / 1024:.0f}KB",
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "含照片" if f.name.endswith(".tar.gz") else "仅数据库"})
    return out


def create_backup(conn, with_uploads: bool | None = None, tag: str = "manual") -> dict:
    """生成一致性备份。返回 {name, path, size, sha256, with_uploads}。"""
    cfg = settings(conn)
    if with_uploads is None:
        with_uploads = cfg["with_uploads"]
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tmpdir = Path(tempfile.mkdtemp(dir=str(BACKUP_DIR)))
    db_copy = tmpdir / "data.db"
    try:
        # 一致性快照：把当前库（含 WAL）完整写到新文件
        with sqlite3.connect(str(dbm.DB_PATH)) as src, sqlite3.connect(str(db_copy)) as dst:
            src.backup(dst)
        os.chmod(db_copy, 0o600)

        if with_uploads and UPLOADS.is_dir():
            name = f"bunny-guardian-{tag}-{stamp}.tar.gz"
            target = BACKUP_DIR / name
            with tarfile.open(target, "w:gz") as tar:
                tar.add(db_copy, arcname="data.db")
                tar.add(UPLOADS, arcname="uploads")
            os.chmod(target, 0o600)
            db_sha = hashlib.sha256(db_copy.read_bytes()).hexdigest()
        else:
            name = f"bunny-guardian-{tag}-{stamp}.db"
            target = BACKUP_DIR / name
            shutil.copy2(db_copy, target)
            os.chmod(target, 0o600)
            db_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    size = target.stat().st_size
    dbm.set_setting(conn, "last_backup_at", dbm.now())
    dbm.set_setting(conn, "last_backup",
                    f"{dbm.now()} → {name}（{size / 1024:.0f}KB，sha256 {db_sha[:12]}）")
    prune(conn)
    return {"name": name, "path": str(target), "size": size, "sha256": db_sha,
            "with_uploads": bool(with_uploads)}


def prune(conn) -> int:
    """只保留最近 N 份（N 来自设置）。返回删除数量。"""
    keep = max(1, settings(conn)["keep"])
    files = sorted((f for f in BACKUP_DIR.glob("*") if f.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    n = 0
    for f in files[keep:]:
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


# ------------------------------------------------------------------ 恢复

def _looks_like_sqlite(data: bytes) -> bool:
    return data[:16] == SQLITE_MAGIC


def restore_from_file(conn, path: Path) -> str:
    """从备份文件恢复。支持 .db 与 .tar.gz（含 photos）。"""
    if not path.is_file():
        raise FileNotFoundError("备份文件不存在")
    safety = BACKUP_DIR / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(dbm.DB_PATH)) as src, sqlite3.connect(str(safety)) as dst:
        src.backup(dst)
    os.chmod(safety, 0o600)

    restored_uploads = 0
    if path.name.endswith(".tar.gz"):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            with tarfile.open(path, "r:gz") as tar:
                # 只接受我们自己的结构（data.db / uploads/…），避免解压任意路径
                for m in tar.getmembers():
                    if m.name.startswith("/") or ".." in m.name.split("/"):
                        raise ValueError("备份包内含非法路径，已拒绝")
                tar.extractall(tmpdir)
            src_db = tmpdir / "data.db"
            if not src_db.is_file() or not _looks_like_sqlite(src_db.read_bytes()):
                raise ValueError("备份包里没有有效的数据库")
            up = tmpdir / "uploads"
            if up.is_dir():
                UPLOADS.mkdir(parents=True, exist_ok=True)
                for f in up.glob("*"):
                    if f.is_file():
                        shutil.copy2(f, UPLOADS / f.name)
                        restored_uploads += 1
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    else:
        src_db = path
        if not _looks_like_sqlite(path.read_bytes()[:16]):
            raise ValueError("这不是一个 SQLite 数据库文件")

    # 原子替换：先挪走旧库与其 WAL，再放入新库
    for suffix in ("-wal", "-shm"):
        p = Path(str(dbm.DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    os.replace(str(src_db), str(dbm.DB_PATH))
    os.chmod(dbm.DB_PATH, 0o600)

    # 校验：能打开且表结构在
    with sqlite3.connect(str(dbm.DB_PATH)) as c:
        c.execute("SELECT COUNT(*) FROM users").fetchone()

    msg = f"已从 {path.name} 恢复（照片 {restored_uploads} 张）；恢复前现场另存为 {safety.name}"
    dbm.set_setting(conn, "last_restore", f"{dbm.now()} → {msg}")
    return msg


def restart_service(slug: str = "") -> str:
    """尽力重启服务让新库立即生效（在容器/非 systemd 环境下静默失败）。"""
    slug = slug or os.environ.get("BG_APP_SLUG", "bunny-guardian")
    for cmd in (["systemctl", "restart", slug],
                ["systemctl", "--user", "restart", slug]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return f"服务已重启（{' '.join(cmd)}）"
        except Exception:  # noqa: BLE001
            continue
    return "请手动重启服务使新数据生效"


# ------------------------------------------------------------------ CLI（供 timer 调用）

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    dbm.init_db()
    with dbm.db() as conn:
        if cmd == "run":
            due, why = _due(conn)
            if not due:
                print(f"[backup] 跳过：{why}")
                return 0
            info = create_backup(conn, tag="auto")
            print(f"[backup] 已备份 {info['name']}（{info['size'] / 1024:.0f}KB，{why}）")
        elif cmd == "now":
            info = create_backup(conn, tag="manual")
            print(f"[backup] 已备份 {info['name']}（{info['size'] / 1024:.0f}KB）")
        elif cmd == "list":
            for b in list_backups():
                print(f"  {b['mtime']}  {b['size_h']:>8}  {b['kind']}  {b['name']}")
        elif cmd == "restore" and len(argv) > 2:
            print("[backup] " + restore_from_file(conn, Path(argv[2])))
        elif cmd == "status":
            print(json.dumps({**settings(conn), "count": len(list_backups())},
                             ensure_ascii=False, indent=2))
        else:
            print("用法：backup.py [run|now|list|restore <file>|status]", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
