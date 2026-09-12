#!/usr/bin/env python3
"""测量 pi agent 在这台云服务器上的真实资源占用（峰值 RSS、耗时、能否完成一次问答）。

用法（服务器上）：
    python3 deploy/pi/measure_rss.py            # RPC 模式（WebUI 集成的用法）
    python3 deploy/pi/measure_rss.py --print    # 单次 print 模式
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

PI = os.environ.get("PI_BIN", "/opt/node22/bin/pi")
PROVIDER = os.environ.get("PI_PROVIDER", "local")        # 由 deploy/pi/run.sh 生成的模型配置决定
MODEL = os.environ.get("PI_MODEL") or os.environ.get("BG_LLM_MODEL") or "local-model"
PROMPT = "用一句话说明黄体期是什么。不要调用任何工具。"
CWD = os.environ.get("PI_CWD") or os.environ.get("BG_AGENT_DIR") or os.getcwd()


def rss_kb(pid: int) -> int:
    try:
        txt = Path(f"/proc/{pid}/status").read_text()
        m = re.search(r"VmRSS:\s+(\d+)\s+kB", txt)
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def meminfo() -> tuple[int, int]:
    txt = Path("/proc/meminfo").read_text()
    total = int(re.search(r"MemTotal:\s+(\d+)", txt).group(1)) // 1024
    avail = int(re.search(r"MemAvailable:\s+(\d+)", txt).group(1)) // 1024
    return total, avail


def children_rss(pid: int) -> tuple[int, int]:
    """返回 (进程树 RSS 合计 MB, 进程数)。"""
    out = subprocess.run(["ps", "-eo", "pid,ppid,rss", "--no-headers"],
                         capture_output=True, text=True).stdout
    tree, rss = {pid}, 0
    changed = True
    while changed:
        changed = False
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            p, pp, r = int(parts[0]), int(parts[1]), int(parts[2])
            if pp in tree and p not in tree:
                tree.add(p)
                changed = True
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and int(parts[0]) in tree:
            rss += int(parts[2])
    return rss // 1024, len(tree)


def run_rpc() -> dict:
    total, avail_before = meminfo()
    env = dict(os.environ)
    env["PATH"] = f"/opt/node22/bin:{env.get('PATH','')}"
    env.setdefault("PI_OFFLINE", "1")
    t0 = time.time()
    proc = subprocess.Popen([PI, "--mode", "rpc", "--no-session",
                             "--provider", PROVIDER, "--model", MODEL],
                            cwd=CWD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, bufsize=1)
    peak_mb, peak_procs, events, answer, done = 0, 0, 0, "", threading.Event()

    def watch() -> None:
        nonlocal peak_mb, peak_procs
        while proc.poll() is None:
            mb, n = children_rss(proc.pid)
            peak_mb = max(peak_mb, mb)
            peak_procs = max(peak_procs, n)
            time.sleep(0.3)

    threading.Thread(target=watch, daemon=True).start()

    def reader() -> None:
        nonlocal events, answer
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            events += 1
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "message_update":
                d = (ev.get("assistantMessageEvent") or {}).get("text_delta")
                if d:
                    answer += d
            if ev.get("type") == "agent_end":
                done.set()

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(2.5)
    idle_mb = children_rss(proc.pid)[0]
    proc.stdin.write(json.dumps({"type": "prompt", "message": PROMPT}) + "\n")
    proc.stdin.flush()
    done.wait(timeout=180)
    elapsed = time.time() - t0
    active_mb = children_rss(proc.pid)[0]
    time.sleep(8)                     # 空闲回落观察
    settled_mb = children_rss(proc.pid)[0]
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    _, avail_after = meminfo()
    return {"mode": "rpc", "ready_mb": idle_mb, "peak_mb": peak_mb, "peak_procs": peak_procs,
            "settled_mb": settled_mb, "events": events, "answer": answer[:160],
            "elapsed_s": round(elapsed, 1), "avail_before_mb": avail_before,
            "avail_after_mb": avail_after, "mem_total_mb": total}


def run_print() -> dict:
    total, avail_before = meminfo()
    env = dict(os.environ)
    env["PATH"] = f"/opt/node22/bin:{env.get('PATH','')}"
    env.setdefault("PI_OFFLINE", "1")
    t0 = time.time()
    proc = subprocess.Popen([PI, "-p", PROMPT, "--no-session", "--provider", PROVIDER,
                             "--model", MODEL, "--tools", ""],
                            cwd=CWD, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    peak_mb, peak_procs = 0, 0
    while proc.poll() is None:
        mb, n = children_rss(proc.pid)
        peak_mb = max(peak_mb, mb)
        peak_procs = max(peak_procs, n)
        time.sleep(0.3)
    out, err = proc.communicate()
    _, avail_after = meminfo()
    return {"mode": "print", "peak_mb": peak_mb, "peak_procs": peak_procs,
            "answer": (out or "").strip()[:160], "stderr": (err or "").strip()[-200:],
            "exit": proc.returncode, "elapsed_s": round(time.time() - t0, 1),
            "avail_before_mb": avail_before, "avail_after_mb": avail_after,
            "mem_total_mb": total}


if __name__ == "__main__":
    mode = "print" if "--print" in sys.argv else "rpc"
    print(f"pi 版本：{subprocess.run([PI, '--version'], capture_output=True, text=True).stdout.strip()}")
    print(f"node ：{subprocess.run(['/opt/node22/bin/node', '-v'], capture_output=True, text=True).stdout.strip()}")
    res = run_rpc() if mode == "rpc" else run_print()
    print(json.dumps(res, ensure_ascii=False, indent=2))
