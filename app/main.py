"""健康档案 WebUI —— FastAPI 应用入口。

运行：uvicorn main:app --host 127.0.0.1 --port 9810
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
import urllib.parse
from urllib.parse import quote

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request, UploadFile)
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth as authm
import agent as agentm
import agent_ops as agent_opsm
import backup as backupm
import bmi as bmim
import chart as chartm
import chat as chatm
import cycle as cyclem
import db as dbm
import fertility as fertm
import kb as kbm
import llm as llmm
import media
import mdrender
import outbound
import prune as prunem
import phases as phasesm
import update as updm
import extensions as extm
import websearch as webm
import stats as statsm
import sync_job

APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent          # 系统代码仓库
KB_DIR = kbm.KB_DIR                # 知识库（个人数据）仓库
DATA_DIR = Path(os.environ.get("BG_DATA", REPO_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD = 12 * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif",
                "application/pdf", "text/plain"}

APP_NAME = os.environ.get("BG_APP_NAME", "健康档案")
APP_SLUG_DEFAULT = "bunny-guardian"   # 页面标题与 OpenAPI 标题，由部署配置注入
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

DISCLAIMER = ("本页内容基于个人记录与公开医学资料整理，仅供健康管理参考，"
              "不能替代医生面诊、检查与诊断。")

WHO_CN = {"her": "她（档案主人）", "him": "他（一起记录）", "": "身份未指定"}
FLOW_CN = {"light": "少", "medium": "中", "heavy": "多", "": "未填"}
KIND_CN = {
    "symptom": "症状", "pain": "疼痛", "mood": "情绪", "flow": "经量",
    "medication": "用药", "temperature": "基础体温", "weight": "体重",
    "spotting": "点滴出血", "note": "备注",
}
STATUS_CN = {"active": "治疗中", "monitoring": "随访观察", "resolved": "已痊愈/结案"}
EVENT_CN = {"visit": "就诊", "exam": "检查", "medication": "用药", "surgery": "手术",
            "symptom": "症状变化", "note": "备注"}


# ------------------------------------------------------------------ 工具

def today() -> date:
    return date.today()


def d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def fmt(v) -> str:
    if isinstance(v, date):
        return v.isoformat()
    return v or ""


def _period_check(user) -> dict:
    """临近（或超过）推断经期时，打开页面主动问一句「今天大姨妈来了吗」。

    经期中不问；还没到（提前 >1 天）不问；拖太久（>12 天）也不再天天问。
    是否已经问过由前端按「每天一次」记在本机。
    """
    if user is None:
        return {}
    try:
        conn, pid = user.conn, profile_id(user)
        info = cyclem.analyze(dbm.cycle_starts(conn, pid), dbm.cycle_durations(conn, pid))
    except Exception:      # noqa: BLE001 - 只是提示，出错不影响页面
        return {}
    if not info.get("has_data") or info.get("next_start") is None:
        return {}
    if (info.get("phase") or "") == "menstrual":
        return {}
    days = info.get("days_to_next")
    if days is None or days > 1 or days < -12:
        return {}
    return {"due": True, "days": int(info.get("period_len") or 5),
            "overdue": max(0, -int(days)),
            "next_start": info["next_start"].isoformat()}


def base_context(request: Request, user, **ctx) -> dict:
    """模板上下文：整页渲染与「片段重渲染」（打勾后就地更新卡片）共用同一份，避免两边走偏。"""
    base = {
        "request": request,
        "user": user,
        "nav": [("dashboard", "总览", "/dashboard"), ("cycle", "经期", "/cycle"),
                ("records", "病历", "/records"),
                ("knowledge", "知识库", "/knowledge")],
        "app_name": APP_NAME,
        "app_slug": os.environ.get("BG_APP_SLUG", APP_SLUG_DEFAULT),
        "ring_views": cyclem.RING_VIEWS,
        "ring_colors": cyclem.RING_COLORS,
        "kb_cycle_doc_url": "knowledge?path=" + quote("医学知识/月经周期生理与正常范围.md"),
        "persona_name": persona()["name"],
        "persona_icon": persona()["icon"],
        "today": dbm.today(),
        "period_check": _period_check(user),
        # 品牌链接：默认回总览；「里」的扩展会把它改写到自己空间
        "brand_href": "/dashboard",
        "brand_title": APP_NAME,
    }
    # 扩展逐页改写（品牌链接等）；页面自己传的上下文优先级最高
    base.update(extm.context_for(request, user))
    base.update(ctx)
    base.setdefault("disclaimer", DISCLAIMER)
    base.setdefault("flow_cn", FLOW_CN)
    base.setdefault("kind_cn", KIND_CN)
    base.setdefault("status_cn", STATUS_CN)
    base.setdefault("event_cn", EVENT_CN)
    base.setdefault("active", "")
    return base


def render(request: Request, user, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name=name,
                                      context=base_context(request, user, **ctx))


def _back_to(request: Request, default: str = "/dashboard") -> str:
    """表单提交后回哪个页面：同源 Referer 优先，其次默认页。

    身体数据从经期页搬到了总览，所以保存身高/体重不能再硬跳 /cycle。
    """
    ref = request.headers.get("referer") or ""
    if ref:
        path = urllib.parse.urlparse(ref).path if "://" in ref else ref
        if path.startswith("/") and not path.startswith("//"):
            return path
    return default


def profile_id(user) -> int:
    return dbm.ensure_profile(user.conn)


def client_ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or (request.client.host if request.client else ""))


def guard(user, space: str, need: str = "view") -> None:
    user.require(space, need)


def csrf_ok(request: Request, user, token: str) -> bool:
    if authm.check_csrf(request, user, token):
        return True
    user.audit("csrf_failed", f"path={request.url.path}", client_ip(request))
    raise HTTPException(status_code=403, detail="表单校验失败，请刷新页面后重试")


EXPORT_DISCLAIMER = ("本页内容由个人记录自动导出，仅供健康管理参考，不能替代医生面诊、"
                     "检查与诊断。统计推断存在误差。")


# ------------------------------------------------------------------ 启动

@app.on_event("startup")
def _startup() -> None:
    dbm.init_db()
    with dbm.db() as conn:
        agentm.init(conn)
        authm.purge_expired(conn)
        pid = dbm.ensure_profile(conn)
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            pw = authm.random_password(14)
            conn.execute(
                "INSERT INTO users(username, display_name, password_hash, role,"
                " must_change_password, created_at) VALUES(?,?,?,?,?,?)",
                ("admin", "管理员", authm.hash_password(pw), "admin", 1, dbm.now()),
            )
            dbm.audit(conn, None, "system", "bootstrap_admin", "created initial admin user")
            banner = "\n" + "=" * 62 + f"\n  初始管理员账号： admin\n  初始密码：      {pw}\n" \
                     "  首次登录后请立即修改密码。\n" + "=" * 62 + "\n"
            print(banner, flush=True)
            log = Path(os.environ.get("BG_DB", "")).parent / "INITIAL_ADMIN.txt"
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(banner, encoding="utf-8")
                os.chmod(log, 0o600)
            except OSError:
                pass
        dbm.set_setting(conn, "profile_id", str(pid))
        # 知识库以数据库为唯一数据源：首次启动若库为空且数据仓库里有 Markdown，则导入一次
        n_p, n_g = kbm.import_all(conn, only_if_empty=True, general_only_missing=True)
        if n_p or n_g:
            print(f"[kb] 已导入知识库：数据仓库 {n_p} 篇（{kbm.DOCS_DIR}）"
                  f" + 通用知识 {n_g} 篇（{kbm.GENERAL_DIR}）", flush=True)
    # 同步没有任何定时器：只在有改动时才推。上次退出时若有改动没推出去，
    # 这里补一次（库里没有「待推送」标记时是空操作，不产生网络请求）。
    sync_job.retry_pending_on_start()


# ------------------------------------------------------------------ 健康检查

@app.get("/livez")
def livez():
    """存活探针：只回 ok，不带任何配置细节。**不需要登录**。

    留给反向代理、监控与 `deploy/install.sh` 的就绪检查用——它们只需要知道
    「进程起来了、能应答」，不该拿到模型端点之类的运行期配置。
    """
    return {"ok": True}


@app.get("/healthz")
def healthz(user=Depends(authm.require_login)):
    """详细健康信息（数据库、模型端点、密钥后四位……）：**要求登录**。

    以前这个端点是匿名可访问的，等于把模型端点、密钥后四位与完整模型列表
    挂在公网上；现在细节只给登录用户看，匿名探活请用 /livez。
    """
    ok_db = True
    try:
        with dbm.db() as conn:
            conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001
        ok_db = False
    info = llmm.health()
    return JSONResponse({"ok": ok_db, "db": ok_db, "llm": info, "time": dbm.now()})


# ------------------------------------------------------------------ 登录

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    return render(request, None, "login.html", next=next, active="", title="登录")


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 next: str = Form("/dashboard")):
    with dbm.db() as conn:
        user = authm.get_user(conn, username.strip())
        if user is None or not user["is_active"] or not authm.verify_password(password, user["password_hash"]):
            dbm.audit(conn, None, username[:40], "login_failed", "", client_ip(request))
            return render(request, None, "login.html", next=next, active="",
                          error="账号或密码不正确，或账号已停用。", title="登录")
        token = authm.create_session(conn, int(user["id"]), request)
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (dbm.now(), user["id"]))
        dbm.audit(conn, user["id"], user["username"], "login_ok", "", client_ip(request))
    resp = RedirectResponse(url=next if next.startswith("/") else "/dashboard", status_code=303)
    resp.set_cookie(authm.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", max_age=authm.SESSION_DAYS * 86400,
                    path="/")
    return resp


@app.post("/logout")
async def logout(request: Request, csrf: str = Form("")):
    user = authm.optional_user(request)
    if user is not None:
        authm.destroy_session(user.conn, user.token or "")
        authm.close_user(user)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(authm.SESSION_COOKIE, path="/")
    return resp


@app.get("/")
def root(user=Depends(authm.require_login)):
    return RedirectResponse(url="/dashboard", status_code=303)


# ------------------------------------------------------------------ 总览

def _is_login_redirect(resp) -> bool:
    """未登录/会话过期时写接口会 303 回 /login，那不是「数据变了」。"""
    if resp.status_code not in (301, 302, 303, 307, 308):
        return False
    loc = str(resp.headers.get("location", ""))
    return loc.startswith("/login")


@app.middleware("http")
async def _sync_on_change(request: Request, call_next):
    """改写数据的请求成功后就排队同步一次（同步只在有改动时跑，没有任何定时器）。

    去抖在 sync_job.schedule() 里做（默认 25 秒合并）＋后台线程执行，
    所以这里只是"打个招呼"，不会拖慢请求；失败也只记录。
    """
    resp = await call_next(request)
    try:
        path = request.url.path
        if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                and resp.status_code < 400
                and not _is_login_redirect(resp)
                and not path.startswith(("/login", "/static", "/admin/sync"))):
            sync_job.schedule(f"{request.method} {path}")
    except Exception as e:  # noqa: BLE001 - 触发失败不能影响这次请求，但必须留下痕迹
        try:                # （第一次写错模块名，被这里的 pass 吞掉了，页面上看不到）
            with dbm.db() as conn:
                dbm.set_setting(conn, "kb_last_trigger",
                                f"{dbm.now()}　触发失败：{type(e).__name__} {str(e)[:100]}")
                conn.commit()
        except Exception:   # noqa: BLE001
            pass
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(authm.require_login), view: str = cyclem.DEFAULT_VIEW):
    guard(user, "dashboard")
    conn, pid = user.conn, profile_id(user)
    starts = dbm.cycle_starts(conn, pid)
    durations = dbm.cycle_durations(conn, pid)
    info = cyclem.analyze(starts, durations)
    recent_logs = conn.execute(
        "SELECT * FROM day_logs WHERE profile_id=? ORDER BY log_date DESC, id DESC LIMIT 12", (pid,)
    ).fetchall()
    conditions = conn.execute(
        "SELECT * FROM conditions WHERE profile_id=? AND status<>'resolved' ORDER BY status, id DESC LIMIT 8",
        (pid,)).fetchall()
    visits = conn.execute(
        "SELECT * FROM visits WHERE profile_id=? ORDER BY visit_date DESC LIMIT 5", (pid,)).fetchall()
    ring = cyclem.ring_context(info, view=view)
    ring_rows = cyclem.ring_phase_table(info, view=view)
    return render(request, user, "dashboard.html", active="dashboard", info=info, view=view,
                  ring=ring, ring_rows=ring_rows, recent_logs=recent_logs, conditions=conditions,
                  visits=visits, title="总览", body=_body_data(conn, pid),
                  msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
                  legend_items=phasesm.for_view(view), fert=_fertility_data(user, info),
                  # 病历与就医合并后的最近几条（时间轴）
                  recent_entries=_timeline(conn, pid, None, None)[:4],
                  # 激素示意图：只在医学分期视角出现（通俗视角不混四期术语）
                  hormone=(chartm.hormone_svg(info.get("cycle_len") or 28,
                                              info.get("period_len") or 5,
                                              cycle_day=info.get("cycle_day"))
                           if view == "med" else ""),
                  # 圆心「记录月经」点一下去经期页的日历
                  record_href="/cycle#calwrap")


# ------------------------------------------------------------------ 经期

def cycle_context(user) -> dict:
    conn, pid = user.conn, profile_id(user)
    rows = conn.execute(
        "SELECT * FROM cycles WHERE profile_id=? ORDER BY start_date DESC", (pid,)).fetchall()
    cycles = []
    for r in rows:
        c = dict(r)
        s, e = d(c["start_date"]), d(c["end_date"])
        c["days"] = (e - s).days + 1 if (s and e) else None
        cycles.append(c)
    logs = conn.execute(
        "SELECT * FROM day_logs WHERE profile_id=? ORDER BY log_date DESC, id DESC LIMIT 200",
        (pid,)).fetchall()
    starts = [s for s in (d(r["start_date"]) for r in rows) if s]
    durations = dbm.cycle_durations(conn, pid)
    info = cyclem.analyze(starts, durations)
    return {"cycles": cycles, "logs": logs, "info": info,
            "phase_table": cyclem.cycle_phase_table(info["cycle_len"], info["period_len"])}


def _calendar_view(user, view: str, cal_mode: str, info: dict) -> tuple[dict, list]:
    """日历数据与图例。整页渲染与打勾接口共用同一份 —— 两处不一致会出现：
    页面上是「事件式」只标经期，点一下之后整月变成全阶段着色（曾经如此）。"""
    cal_full = _calendar_data(user, view)
    ranges = [(d(r["start"]), d(r["end"])) for r in cal_full["periods"]]
    cal = dict(cal_full)
    if cal_mode == "event":
        # 事件式（默认，与主流经期 App 一致）：只标「经期 / 易孕窗口 / 排卵日」，其余留白。
        # 用通俗视角的分期（含 fertile 键），再剔掉卵泡期/黄体期/安全期这些"整片铺色"的部分。
        ev = _calendar_data(user, "plain")
        cal["days"] = {k: v for k, v in ev["days"].items()
                       if v.get("p") in ("menstrual", "fertile")}
        cal["ovu"] = cyclem.ovulation_iso_days(ranges, info)
        legend = [{"key": "menstrual", "label": "经期", "color": cyclem.RING_COLORS["menstrual"]},
                  {"key": "fertile", "label": "易孕窗口（推算）",
                   "color": cyclem.RING_COLORS["fertile"]}]
        return cal, legend
    labels = cyclem.MED_LABELS if view == "med" else cyclem.PLAIN_LABELS
    legend = [{"key": k, "label": v, "color": cyclem.RING_COLORS[k]} for k, v in labels.items()]
    legend.append({"key": "unknown", "label": "推迟 / 未确认",
                   "color": cyclem.RING_COLORS["unknown"]})
    return cal, legend


@app.get("/cycle", response_class=HTMLResponse)
def cycle_page(request: Request, user=Depends(authm.require_login), view: str = cyclem.DEFAULT_VIEW,
               cal: str = "event"):
    guard(user, "cycle")
    if view not in cyclem.RING_VIEWS:
        view = cyclem.DEFAULT_VIEW
    cal_mode = "event" if cal != "phase" else "phase"
    ctx = cycle_context(user)
    cal_data, cal_legend = _calendar_view(user, view, cal_mode, ctx["info"])
    return render(request, user, "cycle.html", active="cycle", title="经期记录",
                  can_edit=authm.can(user.perms, "cycle", "edit"), cal=json.dumps(cal_data),
                  cal_legend=cal_legend, view=view, cal_mode=cal_mode,
                  msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
                  **ctx, **_cycle_cards_data(user, ctx["info"], view))


# 打勾后要就地换新的片段（顶层元素都带 id，前端按 id 替换）
BLOCK_TEMPLATES = ("_ring.html", "_fert_card.html", "_hormone_card.html",
                   "_infer_card.html", "_stats_card.html")


def _cycle_cards_data(user, info: dict, view: str) -> dict:
    """受打勾影响的卡片的上下文（环／受孕率／激素图／统计与分布／身体数据）。

    整页渲染和 POST /cycle/tap 的片段重渲染共用这一份，保证点完日历不用刷新就是最新的。
    """
    conn, pid = user.conn, profile_id(user)
    return {
        "dist": statsm.distributions(conn, pid, _calendar_data(user, view)["days"],
                                     view=view, kind_cn=KIND_CN),
        "body": _body_data(conn, pid),
        "today": dbm.today(),
        "ring": cyclem.ring_context(info, view=view),
        "ring_rows": cyclem.ring_phase_table(info, view=view),
        "legend_items": phasesm.for_view(view),
        "fert": _fertility_data(user, info),
        # 激素示意图讲的是医学分期，只在「医学分期」视角里出现；
        # 通俗视角（安全期/易孕期）不混入四期术语，两种说法保持边界。
        "hormone": (chartm.hormone_svg(info.get("cycle_len"), info.get("period_len"),
                                       cycle_day=info.get("cycle_day")) if view == "med" else ""),
        "record_href": "#calwrap",
        "record_inline": True,
    }


def _blocks_html(request: Request, user, view: str, info: dict) -> dict:
    """把上面的卡片重新渲染成 HTML，返回 {模板名: 片段}；前端就地替换，不用整页刷新。"""
    ctx = base_context(request, user, active="cycle", view=view, info=info,
                       **_cycle_cards_data(user, info, view))
    return {name: templates.env.get_template(name).render(ctx) for name in BLOCK_TEMPLATES}


def _fertility_data(user, info: dict) -> dict | None:
    """今日受孕率：按**当前周期**的估算排卵日取有出处的日受孕概率；无记录时返回 None。

    数字与口径见 fertility.py（Wilcox 1995 NEJM 表 1）。用当前周期的排卵日（不是离今天最近的那次），
    这样卡片上的数字、文字与下面那张折线图说的是同一个周期。
    """
    if info.get("example"):
        return None
    ranges = _period_ranges(user.conn, profile_id(user))
    if not ranges:
        return None
    today_d = date.today()
    cl = float(info.get("cycle_len") or 28)
    pl = float(info.get("period_len") or 5)
    last_start = d(ranges[-1]["start"])
    ovu_day = cyclem.ovulation_day(cl, pl)
    ovu = (last_start + timedelta(days=ovu_day - 1)).isoformat() if last_start else None
    f = fertm.today(ovu, today_d.isoformat())
    f["chart"] = chartm.fertility_svg(
        cl, pl, cycle_day=info.get("cycle_day"), ovu_day=ovu_day,
        start_iso=(ranges[-1]["start"] if ranges else ""), next_iso=str(info.get("next_start") or ""))
    f.update({"ovulation": ovu, "note": fertm.note(),
              "source_title": fertm.SOURCE_TITLE, "source_url": fertm.SOURCE_URL})
    return f


def _body_data(conn, pid: int) -> dict:
    """身体数据：身高、体重记录（含每次的 BMI）、当前 BMI 与健康体重区间。"""
    row = conn.execute("SELECT height_cm FROM profiles WHERE id=?", (pid,)).fetchone()
    height = float(row["height_cm"]) if row and row["height_cm"] else None
    rows = conn.execute(
        "SELECT id, log_date, value, note FROM day_logs WHERE profile_id=? AND kind='weight'"
        " ORDER BY log_date DESC, id DESC LIMIT 24", (pid,)).fetchall()
    weights = []
    for r in rows:
        try:
            kg = float((r["value"] or "").replace("kg", "").strip())
        except ValueError:
            continue
        b = bmim.value(kg, height)
        cls = bmim.classify(b)
        weights.append({"id": r["id"], "date": (r["log_date"] or "")[:10], "kg": kg,
                        "bmi": b, "cls": cls["label"] if b else "—",
                        "cls_tag": cls["tag"] if b else "grey", "note": r["note"] or ""})
    prev = None
    for i, w in enumerate(weights):        # 列表是倒序：delta 表示比上一条（更早的一次）
        older = weights[i + 1] if i + 1 < len(weights) else None
        w["delta"] = round(w["kg"] - older["kg"], 1) if older else None
    latest = weights[0] if weights else None
    trend = sorted([(w["date"], w["kg"]) for w in reversed(weights)])
    bmi_now = bmim.value(latest["kg"], height) if latest else None
    return {"height_cm": height, "weights": weights[:12], "latest": latest,
            "bmi": bmi_now,
            "cls": bmim.classify(bmi_now),
            "healthy": bmim.healthy_weight_range(height), "note": bmim.note(),
            "gauge": chartm.bmi_gauge(bmi_now),        # BMI 刻度条：当前落在哪一段
            "chart": chartm.line_svg(trend, color="#b5486a", unit="kg"),
            "first": trend[0] if trend else None, "count": len(weights),
            "source_title": bmim.SOURCE_TITLE, "source_url": bmim.SOURCE_URL}


def _period_ranges(conn, pid: int) -> list[dict]:
    """已记录的经期区段（编辑模式用）。"""
    rows = conn.execute(
        "SELECT start_date, end_date FROM cycles WHERE profile_id=? ORDER BY start_date",
        (pid,)).fetchall()
    return [{"start": r["start_date"], "end": r["end_date"] or r["start_date"]} for r in rows]


def _period_default_days(conn, pid: int) -> int:
    """编辑模式里「点一天自动往后勾几天」的天数：**按历史推算**（记录过的经期中位数），
    没有历史就用 5 天。不给用户设置项——避免"设置值"和"实际记录"两套口径打架。"""
    durs = sorted(dbm.cycle_durations(conn, pid))
    if durs:
        return max(2, min(10, durs[len(durs) // 2]))
    return 5


def normalize_period_ranges(pairs: list[tuple], today_iso: str, max_days: int = 15,
                            allow_future: bool = False) -> list[tuple]:
    """规整经期区段：合并相邻或重叠段、丢弃非法/过长的段。

    合并是必须的：两段相邻若各存一行，会被当成两次经期，周期长度会算出 1 天。
    allow_future=True 时保留今天之后的日期——日历上打勾要一次勾满「按历史推算的天数」，
    否则点经期第一天只会记到当天（只有 1 天，用户会以为没记上）。
    """
    t = d(today_iso) or date.today()
    clean = []
    for s, e in pairs:
        if not s:
            continue
        if s > t and not allow_future:
            continue                                   # 未来日期不接受
        if not e or e < s:
            e = s
        if e > t and not allow_future:
            e = t                                      # 不越过今天
        if (e - s).days + 1 > max_days:
            e = s + timedelta(days=max_days - 1)
        clean.append((s, e))
    clean.sort()
    merged: list[list] = []
    for s, e in clean:
        if merged and s <= merged[-1][1] + timedelta(days=1):
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _apply_period_ranges(conn, pid: int, ranges: list[tuple]) -> tuple[int, int, int]:
    """按开始日期做差量落库：新增 / 只改结束日 / 删除。保留经量症状备注。"""
    existing = {r["start_date"]: r for r in conn.execute(
        "SELECT id, start_date, end_date FROM cycles WHERE profile_id=?", (pid,)).fetchall()}
    ins = upd = dele = 0
    keep = set()
    for s, e in ranges:
        key = s.isoformat()
        keep.add(key)
        row = existing.get(key)
        if row is None:
            conn.execute(
                "INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, key, e.isoformat(), "", "", "", dbm.now(), dbm.now()))
            ins += 1
        elif (row["end_date"] or "")[:10] != e.isoformat():
            conn.execute("UPDATE cycles SET end_date=?, updated_at=? WHERE id=?",
                         (e.isoformat(), dbm.now(), row["id"]))
            upd += 1
    for key, row in existing.items():
        if key not in keep:
            conn.execute("DELETE FROM cycles WHERE id=?", (row["id"],))
            dele += 1
    return ins, upd, dele


def _calendar_data(user, view: str = cyclem.DEFAULT_VIEW) -> dict:
    conn, pid = user.conn, profile_id(user)
    ranges = _period_ranges(conn, pid)
    marks: dict[str, list[str]] = {}
    for r in ranges:
        s, e = d(r["start"]), d(r["end"]) or d(r["start"])
        cur = s
        while cur and e and cur <= e:
            marks.setdefault(cur.isoformat(), []).append("period")
            cur += timedelta(days=1)
    for r in conn.execute("SELECT log_date, kind, name, severity FROM day_logs WHERE profile_id=?",
                          (pid,)):
        marks.setdefault(r["log_date"], []).append(r["kind"])
    info = cyclem.analyze(dbm.cycle_starts(conn, pid), dbm.cycle_durations(conn, pid))
    days = cyclem.calendar_days([(d(r["start"]), d(r["end"])) for r in ranges], info, view=view)
    return {"marks": marks, "days": days, "periods": ranges,
            "default_days": _period_default_days(conn, pid),
            "can_edit": authm.can(user.perms, "cycle", "edit"),
            "today": dbm.today()}


@app.post("/cycle/add")
def cycle_add(request: Request, user=Depends(authm.require_login),
              csrf: str = Form(""), start_date: str = Form(...), end_date: str = Form(""),
              flow: str = Form(""), symptoms: str = Form(""), note: str = Form("")):
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    s, e = d(start_date), d(end_date)
    if s is None:
        raise HTTPException(400, "起始日期格式不正确")
    if e and e < s:
        e = None
    conn.execute(
        "INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pid, s.isoformat(), e.isoformat() if e else None, flow, symptoms.strip(), note.strip(),
         dbm.now(), dbm.now()))
    user.audit("cycle_add", f"{s.isoformat()}~{e.isoformat() if e else ''}", client_ip(request))
    return RedirectResponse(url="/cycle", status_code=303)


@app.post("/cycle/periods")
def cycle_periods(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                  ranges: str = Form(""), default_days: str = Form("")):
    """整批设置经期区段（只改日期，保留各段的经量/症状/备注）。

    ranges 为空则只更新「默认经期天数」，不动任何记录——避免误提交空列表把记录清空。
    """
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    clean = None
    if ranges.strip():
        try:
            payload = json.loads(ranges)
        except json.JSONDecodeError:
            raise HTTPException(400, "区间格式不正确")
        if not isinstance(payload, list):
            raise HTTPException(400, "区间格式不正确")
        pairs = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            s = d(str(item.get("start") or "").strip())
            if s is None:
                continue
            e = d(str(item.get("end") or "").strip()) or s
            pairs.append((s, e))
        clean = normalize_period_ranges(pairs, dbm.today())
        ins, upd, dele = _apply_period_ranges(conn, pid, clean)
        user.audit("cycle_periods", f"新增{ins} 改{upd} 删{dele}", client_ip(request))
    if default_days.strip().isdigit():
        dbm.set_setting(conn, "period_default_days", str(max(1, min(15, int(default_days)))))
    out = {"ok": True, "default_days": _period_default_days(conn, pid)}
    if clean is not None:
        out["ranges"] = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in clean]
    return JSONResponse(out)


def _persist_periods(conn, pid: int, items: list[dict]) -> None:
    """按 id 落库：改日期走 UPDATE（保留经量/症状/备注），新增走 INSERT，缺的删掉。"""
    keep = [x["id"] for x in items if x.get("id")]
    for x in items:
        if x.get("id"):
            conn.execute("UPDATE cycles SET start_date=?, end_date=?, updated_at=?"
                         " WHERE id=? AND profile_id=?",
                         (x["start"].isoformat(), x["end"].isoformat(), dbm.now(), x["id"], pid))
        else:
            conn.execute(
                "INSERT INTO cycles(profile_id, start_date, end_date, flow, symptoms, note,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, x["start"].isoformat(), x["end"].isoformat(), "", "", "", dbm.now(), dbm.now()))
    if keep:
        conn.execute("DELETE FROM cycles WHERE profile_id=? AND id NOT IN (%s)"
                     % ",".join("?" * len(keep)), (pid, *keep))
    else:
        conn.execute("DELETE FROM cycles WHERE profile_id=?", (pid,))


ADJUST_WINDOW_DAYS = 20      # 记录一次经期后，这之内再点某天＝手动增减这一次经期


def tap_period_range(conn, pid: int, day: date, today_iso: str, default_days: int) -> tuple[list, str]:
    """日历上一次点击的规则（服务端唯一实现，前端只调用）：

    1) 点在已记录的经期段内 → 改到「前一天为止」；点的正是起始日 → 整段删除；
    2) 点在**某次经期开始日 ±20 天**内 → 视为手动增减这一次经期：
       点在段后 = 延长到那天；点在段前 = 起始日提前；
    3) 其它空白日 → 新建一次经期，天数按历史推算（首次 5 天），不越过今天。
    相邻段自动合并；未来的日期不接受。
    """
    rows = conn.execute(
        "SELECT id, start_date, end_date FROM cycles WHERE profile_id=? ORDER BY start_date",
        (pid,)).fetchall()
    items = []
    for r in rows:
        s = d(r["start_date"])
        if s:
            items.append({"id": r["id"], "start": s, "end": d(r["end_date"]) or s})
    items.sort(key=lambda x: x["start"])
    today_d = d(today_iso) or date.today()
    if day > today_d:
        return items, "未来的日子先不用勾"

    # 1) 段内 → 缩短 / 删除
    for i, x in enumerate(items):
        if x["start"] <= day <= x["end"]:
            if day == x["start"]:
                items.pop(i)
                msg = f"已删除 {day.isoformat()} 开始的这段经期"
            else:
                prev = day - timedelta(days=1)
                x["end"] = prev
                msg = f"这段经期改为 {x['start'].isoformat()} ~ {prev.isoformat()}"
            _persist_periods(conn, pid, items)
            return items, msg

    # 2) 距某次经期开始 20 天内 → 手动增减天数（不再新建一次）
    for x in items:
        if abs((day - x["start"]).days) <= ADJUST_WINDOW_DAYS:
            if day > x["end"]:
                x["end"] = day
                msg = f"经期延长到 {day.isoformat()}（第 {(day - x['start']).days + 1} 天）"
            else:
                x["start"] = day
                msg = f"这段经期的开始日改到 {day.isoformat()}"
            _persist_periods(conn, pid, items)
            return items, msg

    # 3) 空白日 → 新建一次经期：按历史推算的天数一次勾好（含今天之后的几天，
    #    用户的心智模型是「点经期第一天＝把这次经期记下来」，主流经期 App 也这样）
    end = day + timedelta(days=max(1, default_days) - 1)
    items.append({"id": None, "start": day, "end": end})
    msg = f"已记录经期 {day.isoformat()} ~ {end.isoformat()}（{default_days} 天，按你以往记录推算）"
    _persist_periods(conn, pid, items)
    return items, msg


@app.post("/cycle/tap")
def cycle_tap(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
              day: str = Form("")):
    """打勾一天（编辑模式）。立即生效，返回重算后的逐日阶段供前端重绘。"""
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    target = d(day.strip())
    if target is None:
        raise HTTPException(400, "日期格式不正确")
    default_days = _period_default_days(conn, pid)
    items, msg = tap_period_range(conn, pid, target, dbm.today(), default_days)
    clean = normalize_period_ranges([(x["start"], x["end"]) for x in items], dbm.today(),
                                   allow_future=True)   # 打勾要一次勾满推算天数
    _apply_period_ranges(conn, pid, clean)      # 合并相邻段（按开始日对齐，不会丢元数据）
    user.audit("cycle_tap", f"{target.isoformat()} {msg}", client_ip(request))
    conn = user.conn
    ranges = _period_ranges(conn, pid)
    info = cyclem.analyze(dbm.cycle_starts(conn, pid), dbm.cycle_durations(conn, pid))
    view = request.query_params.get("view", cyclem.DEFAULT_VIEW)
    if view not in cyclem.RING_VIEWS:
        view = cyclem.DEFAULT_VIEW
    # 关键：这里必须和整页用同一套日历数据（事件式要过滤），否则点一下整月都变成阶段着色
    cal_mode = "event" if request.query_params.get("cal", "event") != "phase" else "phase"
    cal_data, _legend = _calendar_view(user, view, cal_mode, info)
    return JSONResponse({"ok": True, "msg": msg, "default_days": default_days,
                         "ranges": ranges, "days": cal_data["days"],
                         "ovu": cal_data.get("ovu", []),
                         # 环／受孕率／激素图／统计／身体数据跟着一起重算，
                         # 前端就地替换这些卡片，不用再刷新整页
                         "blocks": _blocks_html(request, user, view, info)})


@app.post("/cycle/period-start")
def cycle_period_start(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """「今天大姨妈来了吗」点「来了」：先按历史推算的天数记下今天起这一段，
    然后跳到经期页并打开编辑模式，她可以在日历上微调（点一下即可增减）。"""
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    today = date.today()
    default_days = _period_default_days(conn, pid)
    items, msg = tap_period_range(conn, pid, today, dbm.today(), default_days)
    clean = normalize_period_ranges([(x["start"], x["end"]) for x in items], dbm.today(),
                                    allow_future=True)
    _apply_period_ranges(conn, pid, clean)
    user.audit("period_start_quick", f"{today.isoformat()} 起 {default_days} 天 {msg}",
               client_ip(request))
    return RedirectResponse(url="/cycle?edit=1#calwrap", status_code=303)


@app.post("/body/height")
def body_height(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                height_cm: str = Form("")):
    """保存身高（用于算 BMI）。"""
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    pid = profile_id(user)
    raw = height_cm.strip().replace("cm", "")
    try:
        h = float(raw)
    except ValueError:
        return RedirectResponse(url=_back_to(request) + "?err=身高请填数字（厘米）", status_code=303)
    if not (100 <= h <= 220):
        return RedirectResponse(url=_back_to(request) + "?err=身高看起来不对（应在 100–220 cm）", status_code=303)
    user.conn.execute("UPDATE profiles SET height_cm=? WHERE id=?", (round(h, 1), pid))
    user.audit("body_height", f"{h}", client_ip(request))
    return RedirectResponse(url=_back_to(request) + "?msg=身高已保存，BMI 已按它计算", status_code=303)


@app.post("/body/weight")
def body_weight(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                log_date: str = Form(""), weight_kg: str = Form(""), note: str = Form("")):
    """记一次体重（同一日期重复记录会追加一条，便于看出波动）。"""
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    try:
        kg = float(weight_kg.strip().replace("kg", ""))
    except ValueError:
        return RedirectResponse(url=_back_to(request) + "?err=体重请填数字（公斤）", status_code=303)
    if not (25 <= kg <= 200):
        return RedirectResponse(url=_back_to(request) + "?err=体重看起来不对（应在 25–200 kg）", status_code=303)
    day = d(log_date) or date.today()
    if day > date.today():
        return RedirectResponse(url=_back_to(request) + "?err=不能记录未来的体重", status_code=303)
    conn.execute(
        "INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pid, day.isoformat(), "weight", "体重", None, f"{round(kg, 1)}", note.strip(), dbm.now()))
    # 档案里的体重取**日期最近**的那一条（不是最后提交的那条，补录旧日期时才对得上）
    last = conn.execute(
        "SELECT value FROM day_logs WHERE profile_id=? AND kind='weight'"
        " ORDER BY log_date DESC, id DESC LIMIT 1", (pid,)).fetchone()
    if last:
        try:
            conn.execute("UPDATE profiles SET weight_kg=? WHERE id=?",
                         (float((last["value"] or "").strip()), pid))
        except ValueError:
            pass
    user.audit("body_weight", f"{day.isoformat()} {kg}kg", client_ip(request))
    b = bmim.value(kg, _body_data(conn, pid)["height_cm"])
    tip = f"，BMI {b}" if b else "（先填身高才能算 BMI）"
    return RedirectResponse(url=_back_to(request) + f"?msg=体重已记录{tip}", status_code=303)


@app.post("/cycle/delete/{cid}")
def cycle_delete(cid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    user.conn.execute("DELETE FROM cycles WHERE id=? AND profile_id=?", (cid, profile_id(user)))
    user.audit("cycle_delete", str(cid), client_ip(request))
    return RedirectResponse(url="/cycle", status_code=303)


@app.post("/cycle/log")
def cycle_log(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
              log_date: str = Form(...), kind: str = Form(...), name: str = Form(""),
              severity: str = Form(""), value: str = Form(""), note: str = Form("")):
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    pid = profile_id(user)
    ld = d(log_date) or today()
    sev = int(severity) if severity.strip().isdigit() else None
    if sev is not None:
        sev = max(1, min(5, sev))
    user.conn.execute(
        "INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note, created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (pid, ld.isoformat(), kind, name.strip(), sev, value.strip(), note.strip(), dbm.now()))
    user.audit("log_add", f"{ld.isoformat()} {kind} {name}", client_ip(request))
    return RedirectResponse(url="/cycle", status_code=303)


@app.post("/cycle/log/delete/{lid}")
def log_delete(lid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    guard(user, "cycle", "edit")
    csrf_ok(request, user, csrf)
    user.conn.execute("DELETE FROM day_logs WHERE id=? AND profile_id=?", (lid, profile_id(user)))
    # 体重历史现在在「总览」上，删完要回原页
    return RedirectResponse(url=_back_to(request) + "?msg=已删除这条记录", status_code=303)


# ------------------------------------------------------------------ 疾病

# 「病历」= 疾病事件 + 就医，合成一条时间轴。默认只看最近一个月，可切范围或自定义区间。
RECORD_SPANS = {"1m": ("最近一个月", 31), "3m": ("最近三个月", 92), "6m": ("最近半年", 183),
                "1y": ("最近一年", 366), "all": ("全部", None)}
CONDITION_EVENT_CN = {"visit": "复诊", "exam": "检查", "medication": "用药", "surgery": "手术",
                      "symptom": "症状", "note": "备注"}


def _timeline(conn, pid: int, from_d, to_d, include_logs: bool = True) -> list[dict]:
    """疾病事件 + 就医 + 每日记录（症状/用药/体重…）合并，按日期倒序。

    include_logs：总览那张「病历与就医」只看疾病与就医，病历页则连每日记录一起看 ——
    用户截图问过「我刚加了个症状，为什么病历里看不到」，就是因为这里没并进来。
    """
    lo = from_d.isoformat() if from_d else "0000-01-01"
    hi = to_d.isoformat() if to_d else "9999-12-31"
    out: list[dict] = []
    for r in conn.execute(
            "SELECT e.*, c.name AS cname, c.id AS cid FROM condition_events e"
            " JOIN conditions c ON c.id=e.condition_id"
            " WHERE c.profile_id=? AND e.event_date BETWEEN ? AND ?", (pid, lo, hi)).fetchall():
        out.append({"kind": "event", "date": r["event_date"], "id": r["id"],
                    "type_cn": CONDITION_EVENT_CN.get(r["kind"], r["kind"]),
                    "title": r["title"] or CONDITION_EVENT_CN.get(r["kind"], r["kind"]),
                    "cname": r["cname"], "cid": r["cid"], "detail": r["detail"],
                    "result": r["result"], "lines": []})
    for r in conn.execute("SELECT * FROM visits WHERE profile_id=? AND visit_date BETWEEN ? AND ?",
                          (pid, lo, hi)).fetchall():
        lines = [(k, r[k]) for k in ("reason", "findings", "diagnosis", "plan", "doctor", "cost")
                 if r[k] not in (None, "")]
        out.append({"kind": "visit", "date": r["visit_date"], "id": r["id"], "type_cn": "就医",
                    "title": " · ".join(x for x in (r["hospital"], r["department"]) if x) or "就医",
                    "cname": "", "cid": None, "detail": "", "result": "",
                    "lines": [({"reason": "就诊原因", "findings": "检查所见", "diagnosis": "诊断",
                                "plan": "处理方案", "doctor": "医生", "cost": "费用"}[k], v)
                              for k, v in lines]})
    if include_logs:
        for r in conn.execute(
                "SELECT * FROM day_logs WHERE profile_id=? AND log_date BETWEEN ? AND ?"
                " ORDER BY log_date DESC, id DESC", (pid, lo, hi)).fetchall():
            kind_cn = KIND_CN.get(r["kind"], r["kind"] or "记录")
            lines = []
            if r["severity"] not in (None, ""):
                lines.append(("程度", f"{r['severity']}/5"))
            if r["value"] not in (None, ""):
                lines.append(("数值", r["value"]))
            if r["note"]:
                lines.append(("备注", r["note"]))
            out.append({"kind": "log", "date": r["log_date"], "id": r["id"], "type_cn": kind_cn,
                        "title": (r["name"] or kind_cn), "cname": "", "cid": None,
                        "detail": "", "result": "", "lines": lines})
    out.sort(key=lambda x: (x["date"], x["kind"] == "visit"), reverse=True)
    return out


@app.get("/records", response_class=HTMLResponse)
def records_page(request: Request, user=Depends(authm.require_login),
                 span: str = "1m", start: str = "", end: str = ""):
    """病历与就医（只读时间轴）。新增/修改走对话：助手先提提案，用户在问答面板里确认。"""
    guard(user, "conditions")
    conn, pid = user.conn, profile_id(user)
    today_d = date.today()
    span_label, from_d, to_d = RECORD_SPANS.get(span, RECORD_SPANS["1m"])[0], None, None
    s0, e0 = d(start), d(end)
    if s0 and e0 and s0 <= e0:
        span, span_label, from_d, to_d = "custom", "自定义区间", s0, e0
    elif RECORD_SPANS.get(span, RECORD_SPANS["1m"])[1]:
        from_d = today_d - timedelta(days=RECORD_SPANS[span][1])
    entries = _timeline(conn, pid, from_d, to_d)
    conds = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM condition_events e WHERE e.condition_id=c.id) AS n_events,"
        " (SELECT MAX(e.event_date) FROM condition_events e WHERE e.condition_id=c.id) AS last_event"
        " FROM conditions c WHERE c.profile_id=? ORDER BY"
        " CASE c.status WHEN 'active' THEN 0 WHEN 'monitoring' THEN 1 ELSE 2 END, c.id DESC",
        (pid,)).fetchall()
    return render(request, user, "records.html", active="records", title="病历与就医",
                  entries=entries, conditions=conds, spans=RECORD_SPANS, span=span,
                  span_label=span_label, start=s0.isoformat() if s0 else "",
                  end=e0.isoformat() if e0 else "",
                  range_text=(f"{from_d.isoformat()} ~ {to_d.isoformat()}" if from_d and to_d else ""),
                  today=dbm.today(), can_edit=authm.can(user.perms, "conditions", "edit"))


@app.get("/conditions", response_class=HTMLResponse)
def conditions_page(request: Request, user=Depends(authm.require_login)):
    """旧入口：已并入「病历」时间轴。"""
    return RedirectResponse(url="/records", status_code=307)


@app.post("/conditions/add")
def condition_add(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                  name: str = Form(...), category: str = Form(""), status: str = Form("active"),
                  onset_date: str = Form(""), diagnosed_date: str = Form(""),
                  hospital: str = Form(""), department: str = Form(""), doctor: str = Form(""),
                  summary: str = Form("")):
    guard(user, "conditions", "edit")
    csrf_ok(request, user, csrf)
    pid = profile_id(user)
    cur = user.conn.execute(
        "INSERT INTO conditions(profile_id, name, category, status, onset_date, diagnosed_date,"
        " hospital, department, doctor, summary, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, name.strip(), category.strip(), status, fmt(d(onset_date)), fmt(d(diagnosed_date)),
         hospital.strip(), department.strip(), doctor.strip(), summary.strip(), dbm.now(), dbm.now()))
    user.audit("condition_add", name, client_ip(request))
    return RedirectResponse(url=f"/conditions/{cur.lastrowid}", status_code=303)


@app.get("/conditions/{cid}", response_class=HTMLResponse)
def condition_detail(cid: int, request: Request, user=Depends(authm.require_login)):
    guard(user, "conditions")
    conn = user.conn
    row = conn.execute("SELECT * FROM conditions WHERE id=? AND profile_id=?",
                       (cid, profile_id(user))).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该疾病记录")
    events = conn.execute(
        "SELECT * FROM condition_events WHERE condition_id=? ORDER BY event_date DESC, id DESC",
        (cid,)).fetchall()
    # 档案级附件 + 该档案下**所有病程事件**的附件（病历单/报告照片）
    files = conn.execute(
        "SELECT * FROM attachments WHERE (ref_kind='condition' AND ref_id=?)"
        " OR (ref_kind='event' AND ref_id IN (SELECT id FROM condition_events WHERE condition_id=?))"
        " ORDER BY id DESC", (cid, cid)).fetchall()
    by_event: dict[int, list] = {}
    for f in files:
        by_event.setdefault(int(f["ref_id"]), []).append(f)
    return render(request, user, "condition_detail.html", active="conditions", title=row["name"],
                  c=row, events=events, files=files, files_by_event=by_event,
                  can_edit=authm.can(user.perms, "conditions", "edit"), today=dbm.today())


@app.post("/conditions/{cid}/update")
def condition_update(cid: int, request: Request, user=Depends(authm.require_login),
                     csrf: str = Form(""), status: str = Form("active"),
                     category: str = Form(""), summary: str = Form(""), hospital: str = Form(""),
                     department: str = Form(""), doctor: str = Form(""), diagnosed_date: str = Form("")):
    guard(user, "conditions", "edit")
    csrf_ok(request, user, csrf)
    user.conn.execute(
        "UPDATE conditions SET status=?, category=?, summary=?, hospital=?, department=?,"
        " doctor=?, diagnosed_date=?, updated_at=? WHERE id=? AND profile_id=?",
        (status, category.strip(), summary.strip(), hospital.strip(), department.strip(),
         doctor.strip(), fmt(d(diagnosed_date)), dbm.now(), cid, profile_id(user)))
    user.audit("condition_update", str(cid), client_ip(request))
    return RedirectResponse(url=f"/conditions/{cid}", status_code=303)


@app.post("/conditions/{cid}/delete")
def condition_delete(cid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    guard(user, "conditions", "edit")
    csrf_ok(request, user, csrf)
    conn = user.conn
    if conn.execute("SELECT 1 FROM conditions WHERE id=? AND profile_id=?",
                    (cid, profile_id(user))).fetchone():
        conn.execute("DELETE FROM condition_events WHERE condition_id=?", (cid,))
        conn.execute("DELETE FROM conditions WHERE id=?", (cid,))
        user.audit("condition_delete", str(cid), client_ip(request))
    return RedirectResponse(url="/conditions", status_code=303)


@app.post("/conditions/{cid}/event")
def condition_event_add(cid: int, request: Request, user=Depends(authm.require_login),
                        csrf: str = Form(""), event_date: str = Form(...), kind: str = Form("visit"),
                        title: str = Form(""), detail: str = Form(""), result: str = Form("")):
    guard(user, "conditions", "edit")
    csrf_ok(request, user, csrf)
    conn = user.conn
    if conn.execute("SELECT 1 FROM conditions WHERE id=? AND profile_id=?",
                    (cid, profile_id(user))).fetchone():
        conn.execute(
            "INSERT INTO condition_events(condition_id, event_date, kind, title, detail, result, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (cid, fmt(d(event_date) or today()), kind, title.strip(), detail.strip(),
             result.strip(), dbm.now()))
        user.audit("event_add", f"cond={cid} {kind} {title}", client_ip(request))
    return RedirectResponse(url=f"/conditions/{cid}", status_code=303)


@app.post("/conditions/{cid}/event/{eid}/delete")
def condition_event_delete(cid: int, eid: int, request: Request,
                           user=Depends(authm.require_login), csrf: str = Form("")):
    guard(user, "conditions", "edit")
    csrf_ok(request, user, csrf)
    user.conn.execute("DELETE FROM condition_events WHERE id=? AND condition_id=?", (eid, cid))
    return RedirectResponse(url=f"/conditions/{cid}", status_code=303)


# ------------------------------------------------------------------ 就医记录

@app.get("/visits", response_class=HTMLResponse)
def visits_page(request: Request, user=Depends(authm.require_login)):
    """旧入口：就医记录已并入「病历」时间轴。"""
    return RedirectResponse(url="/records", status_code=307)


@app.post("/visits/add")
def visit_add(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
              visit_date: str = Form(...), hospital: str = Form(""), department: str = Form(""),
              doctor: str = Form(""), reason: str = Form(""), findings: str = Form(""),
              diagnosis: str = Form(""), plan: str = Form(""), cost: str = Form("")):
    guard(user, "visits", "edit")
    csrf_ok(request, user, csrf)
    try:
        cost_v = float(cost) if cost.strip() else None
    except ValueError:
        cost_v = None
    user.conn.execute(
        "INSERT INTO visits(profile_id, visit_date, hospital, department, doctor, reason, findings,"
        " diagnosis, plan, cost, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (profile_id(user), fmt(d(visit_date) or today()), hospital.strip(), department.strip(),
         doctor.strip(), reason.strip(), findings.strip(), diagnosis.strip(), plan.strip(),
         cost_v, dbm.now()))
    user.audit("visit_add", f"{visit_date} {hospital}", client_ip(request))
    return RedirectResponse(url="/visits", status_code=303)


@app.post("/visits/delete/{vid}")
def visit_delete(vid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    guard(user, "visits", "edit")
    csrf_ok(request, user, csrf)
    user.conn.execute("DELETE FROM visits WHERE id=? AND profile_id=?", (vid, profile_id(user)))
    return RedirectResponse(url="/visits", status_code=303)


# ------------------------------------------------------------------ 附件

def _link_recent_qa_attachments(conn, ref_kind: str, ref_id: int, hours: int = 24) -> int:
    """把最近通过问答上传的图片/病历单，挂到刚从提案落库的记录上（留档，便于日后对比）。"""
    if not ref_kind or not ref_id:
        return 0
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "UPDATE attachments SET ref_kind=?, ref_id=? WHERE ref_kind='qa' AND created_at>=?",
        (ref_kind, ref_id, since))
    return cur.rowcount or 0


@app.post("/upload/{kind}/{ref_id}")
def upload(kind: str, ref_id: int, request: Request, user=Depends(authm.require_login),
           csrf: str = Form(""), back: str = Form("/dashboard"), file: UploadFile = File(...)):
    space = {"condition": "conditions", "visit": "visits", "cycle": "cycle",
             "event": "conditions"}.get(kind, "conditions")
    guard(user, space, "edit")
    csrf_ok(request, user, csrf)
    if kind == "event":
        row = user.conn.execute(
            "SELECT e.id FROM condition_events e JOIN conditions c ON c.id=e.condition_id"
            " WHERE e.id=? AND c.profile_id=?", (ref_id, profile_id(user))).fetchone()
        if row is None:
            raise HTTPException(404, "病程事件不存在")
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(400, f"不支持的文件类型：{file.content_type}")
    raw = file.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(400, "文件超过 12MB 限制")
    ext = Path(file.filename or "").suffix.lower()[:8] or ".bin"
    stored = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / stored).write_bytes(raw)
    user.conn.execute(
        "INSERT INTO attachments(ref_kind, ref_id, filename, mime, size, stored_name, uploaded_by,"
        " created_at) VALUES(?,?,?,?,?,?,?,?)",
        (kind, ref_id, (file.filename or stored)[:200], file.content_type, len(raw), stored,
         user.id, dbm.now()))
    user.audit("upload", f"{kind}/{ref_id} {file.filename}", client_ip(request))
    return RedirectResponse(url=back if back.startswith("/") else "/dashboard", status_code=303)


@app.get("/attachment/{aid}")
def attachment(aid: int, user=Depends(authm.require_login)):
    row = user.conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if row is None:
        raise HTTPException(404, "附件不存在")
    path = UPLOAD_DIR / row["stored_name"]
    if not path.is_file():
        raise HTTPException(404, "附件文件已丢失")
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type=row["mime"] or "application/octet-stream",
                        filename=row["filename"])


# ------------------------------------------------------------------ 知识库

DIR_SPACE = {"医学知识": "knowledge", "经期记录": "cycle", "疾病档案": "conditions", "就医记录": "visits"}


@app.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request, user=Depends(authm.require_login), path: str = "", q: str = ""):
    guard(user, "knowledge")
    if path:
        rel = path
        space = DIR_SPACE.get(rel.split("/")[0], "knowledge")
        guard(user, space)
        rendered = kbm.render(rel)
        if rendered is None:
            raise HTTPException(404, "文档不存在")
        meta, html = rendered
        rel_kb = rel.lstrip("/")
        return render(request, user, "doc.html", active="knowledge", title=meta.get("title", rel),
                      related=kbm.related(rel_kb),
                      meta=meta, content=html, rel=rel, active_dir=rel.split("/")[0],
                      can_edit=authm.can(user.perms, space, "edit"))
    allowed = [k for k, v in DIR_SPACE.items() if authm.can(user.perms, v)]
    docs = [x for x in kbm.list_docs(allowed)]
    results = kbm.search(q, dirs=allowed, top=12) if q.strip() else []
    return render(request, user, "knowledge.html", active="knowledge", title="知识库",
                  docs=docs, q=q, results=results, dirs=allowed,
                  kb=kbm.stats(),
                  can_edit=authm.can(user.perms, "knowledge", "edit"))


# ------------------------------------------------------------------ 问答

PERSONA_DEFAULT_NAME = "守护兔"
PERSONA_DEFAULT_PROMPT = """你是「守护兔」🐰 —— 一只会替人操心的小兔子，陪着她一起看身体发出的信号。
说话的样子：
1. 先照顾情绪再讲事情。用「我们一起看看」「别急，先把情况理一理」这种口吻，
   像一个真的在替她担心的朋友，不要一条条冷冰冰地丢结论。
2. 每次回答都按三步走（这是最重要的）：
   ① 情况梳理：把这次说的和档案里的记录对齐，指出关键点（时间、频率、变化）。
   ② 可能的原因：列 2~4 个常见可能，各说一句支持点和「常见/较少见」，
      并明确说什么情况需要就医；不要用百分比数字，不要下诊断。
   ③ 接下来怎么办：给具体、当天就能做的小事 —— 记什么、什么时候复诊、挂哪个科、带什么资料。
3. 中文，句子短一点。可以少量用 🐰 和小表情，但别刷屏，别影响信息密度。
4. 不知道就说不知道。涉及诊断、用药剂量、要不要急诊，一律让她找医生，
   并说清「为什么建议现在去」或「为什么可以再观察两天」。
5. 有依据就写清来源（知识库文档名或网页 URL）；没有依据就说明这是经验性的建议。
6. 不要吓人，也不要打包票：语气温柔，但结论要老实。"""


def persona() -> dict:
    """当前生效的 AI 角色（管理员可在网页里改；没改过就用默认的守护兔）。

    icon 留空＝用内置的小兔子 SVG；填了就用那个字符（emoji 或短字符），
    方便别人配自己的人设时换个图标。
    """
    with dbm.db() as conn:
        name = (dbm.get_setting(conn, "ai_persona_name", "") or "").strip()[:20]
        prompt = (dbm.get_setting(conn, "ai_persona_prompt", "") or "").strip()
        icon = (dbm.get_setting(conn, "ai_persona_icon", "") or "").strip()[:4]
    return {"name": name or PERSONA_DEFAULT_NAME,
            "prompt": prompt or PERSONA_DEFAULT_PROMPT,
            "icon": icon,
            "custom": bool(name or prompt or icon)}


def _identity_note(user) -> str:
    """提问者不是她本人时，先告诉模型该怎么理解这些话。"""
    if getattr(user, "is_partner", False):
        return ("【提问者身份】正在提问的是她的伴侣（男友），**不是她本人**。\n"
                "请：1) 把他转述的情况当作「他观察到的」，别写成她自己的原话；\n"
                "2) 需要她本人确认的细节（疼感、心情、经量）明确让他去问一下；\n"
                "3) 记录类信息（就诊、用药、检查）要提醒他「以她本人说的为准」。")
    return ""


QA_SYSTEM = """你是一个个人健康档案的辅助问答助手，服务对象是一位年轻女性的健康管理档案。

必须遵守：
1. 你不是医生，不做诊断、不下结论、不开药、不给药物剂量。
2. 回答时优先使用下面提供的「档案数据」与「知识库依据」，并在引用知识库时写明文件来源（如 docs/医学知识/xxx.md）。
3. 如果依据中没有相关内容，明确说明「知识库中没有查到相关依据」，再给出一般性常识并标注为一般性说明。
4. 涉及症状时，必须给出就医建议：什么情况下建议尽快就医、什么情况下属于需要立即急诊的情况。
5. 不制造焦虑、不使用夸张措辞（具体语气与角色设定见「【你的角色】」）。
6. 中文回答，结构清晰，可用小标题与要点，总长控制在 600 字以内，除非用户要求更详细。
7. **记录**：当用户提到值得入档的事实（就诊、检查结果、开始或调整用药、症状变化、确诊、月经开始/结束），
   在回答的**最后**附一个记录块，格式严格如下（只写确实已知的字段，不要猜；日期用 YYYY-MM-DD）：
   <record kind="visit_add">{"visit_date": "2026-08-12", "hospital": "某某医院", "department": "妇科", "reason": "月经不规律", "diagnosis": "随访观察", "plan": "3 个月后复查"}</record>
   可用的 kind（只能从这里选）：
   visit_add（一次就诊）、condition_add（新确诊的疾病）、condition_event（某疾病的病程事件，需带 condition 字段写疾病名）、
   cycle_add（月经开始/结束，字段 start_date / end_date）、day_log（当日症状、用药、体重等，需 log_date 与 kind/name）。
   一次回答最多一个记录块；不确定该不该记，就不写。**不要自己说"已经写入"**——
   记录块会变成一条待确认条目，用户在界面上点「确认」后才真正写进档案；
   正文里可以说「我整理了一条待确认的就诊记录，你确认一下」，并复述关键信息。
   注意：出于隐私保护，你能看到的档案数据里**绝对日期显示为 [已隐去]**，这是正常的。
   所以记录块的日期字段请你写**相对写法**：today（今天）/ yesterday（昨天）/ 前天 / -3d（三天前），
   由系统换算成真实日期；**不要**写 [已隐去]，也不要凭空猜一个具体日期。
"""
# 助手回答里的记录块（在界面上变成「待确认」条目；用户在问答面板里点确认才落库）
_RECORD_RE = re.compile(r"<record\s+kind=\"([a-z_]+)\"\s*>(.*?)</record>", re.S)


_RECORD_DATE_FIELDS = ("visit_date", "start_date", "end_date", "event_date", "log_date",
                       "onset_date", "diagnosed_date")


_REL_DATE_TOKENS = {"today": 0, "今天": 0, "yesterday": -1, "昨天": -1, "前天": -2, "tomorrow": 1, "明天": 1}


def _clean_record_payload(payload: dict, today_iso: str | None = None) -> dict:
    """日期字段只接受 YYYY-MM-DD，或 today / yesterday / -3d 这类相对写法。

    背景：外发内容里的绝对日期会被隐私围栏替换成 [已隐去]，模型看到的就是这个占位。
    所以约定它可以写相对日期，由**本地**换算成真实日期；其它杂值一律丢掉，
    绝不让 [已隐去] 这种占位落库（否则确认后档案里会出现一个假日期）。
    """
    base = d(today_iso or dbm.today()) or date.today()
    out: dict = {}
    for k, v in (payload or {}).items():
        if k in _RECORD_DATE_FIELDS and isinstance(v, str):
            s = v.strip()
            low = s.lower()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
                out[k] = s
            elif low in _REL_DATE_TOKENS:
                out[k] = (base + timedelta(days=_REL_DATE_TOKENS[low])).isoformat()
            elif re.fullmatch(r"-\d{1,3}d", low):
                out[k] = (base - timedelta(days=int(low[1:-1]))).isoformat()
            continue                      # 其它写法（如 [已隐去]）直接丢弃
        out[k] = v
    return out


def _extract_records(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """把回答里的 <record kind="...">{json}</record> 取出来，并从正文里剥掉。"""
    found: list[tuple[str, dict]] = []
    for m in _RECORD_RE.finditer(text or ""):
        try:
            payload = json.loads(m.group(2).strip())
        except Exception:  # noqa: BLE001 - 记录块不合法就当没写
            continue
        payload = _clean_record_payload(payload) if isinstance(payload, dict) else {}
        if payload:
            found.append((m.group(1), payload))
    return _RECORD_RE.sub("", text or "").strip(), found



VISION_SYSTEM = """本次用户上传了医学影像/化验单照片。解读纪律：
1. 先说明「我看到的」——把图片里的关键字段、数值、单位、参考范围逐条抄录出来，不要凭印象概括。
2. 再对照知识库里的参考范围解释每项偏高/偏低可能意味着什么；必须说明「不同实验室参考区间不同，以报告单标注为准」。
3. 不确定或图片模糊的地方直接说看不清，不要猜测数值。
4. 不给诊断结论、不给药物剂量；明确哪些情况需要尽快就医。
5. 提醒用户：照片解读不能替代医生对原始报告的判断。"""


def _profile_summary(conn, pid: int) -> str:
    info = cyclem.analyze(dbm.cycle_starts(conn, pid), dbm.cycle_durations(conn, pid))
    lines = []
    if info["example"]:
        lines.append("经期记录：暂无记录。")
    else:
        s = info["stats"]
        lines.append(
            f"经期记录：共 {s['n_cycles']} 次记录；近 {len(s['recent_lengths'])} 个周期长度 "
            f"{s['recent_lengths']}（中位数 {s['median']} 天，范围 {s['min']}–{s['max']} 天）；"
            f"经期平均 {s['period_len_mean'] or '未知'} 天；今天为本次周期第 {info['cycle_day']} 天，"
            f"处于{info['phase_cn']}；推断下次经期约 {info['next_start']}（区间 {info['next_window'][0]} ~ "
            f"{info['next_window'][1]}，置信度{info['confidence']}）。")
        if info["irregular_flags"]:
            lines.append("记录中的异常提示：" + "；".join(info["irregular_flags"]))
    conds = conn.execute(
        "SELECT name, status, category, diagnosed_date, summary FROM conditions"
        " WHERE profile_id=? AND status<>'resolved' ORDER BY id DESC LIMIT 8", (pid,)).fetchall()
    if conds:
        lines.append("疾病档案（未结案）：" + "；".join(
            f"{c['name']}（{STATUS_CN.get(c['status'], c['status'])}"
            f"{'，' + c['diagnosed_date'] if c['diagnosed_date'] else ''}）" for c in conds))
    visits = conn.execute(
        "SELECT visit_date, hospital, department, diagnosis FROM visits WHERE profile_id=?"
        " ORDER BY visit_date DESC LIMIT 5", (pid,)).fetchall()
    if visits:
        lines.append("最近就诊：" + "；".join(
            f"{v['visit_date']} {v['hospital']}{v['department']} {v['diagnosis'] or ''}".strip()
            for v in visits))
    body = _body_data(conn, pid)
    if body["height_cm"]:
        line = f"身高 {body['height_cm']} cm"
        if body["latest"]:
            line += (f"；最近一次体重 {body['latest']['kg']} kg（{body['latest']['date']}），"
                     f"BMI {body['bmi']}（{body['cls']['label']}，按中国标准 WS/T 428-2013）")
            if body["healthy"]:
                line += f"；该身高的正常体重区间约 {body['healthy'][0]}–{body['healthy'][1]} kg"
        lines.append(line)
    logs = conn.execute(
        "SELECT log_date, kind, name, severity FROM day_logs WHERE profile_id=?"
        " ORDER BY log_date DESC LIMIT 20", (pid,)).fetchall()
    if logs:
        lines.append("近期日志：" + "；".join(
            f"{l['log_date']} {KIND_CN.get(l['kind'], l['kind'])}"
            f"{('-' + l['name']) if l['name'] else ''}"
            f"{('(' + str(l['severity']) + '/5)') if l['severity'] else ''}" for l in logs))
    return "\n".join("- " + x for x in lines) or "- （档案中暂无记录）"


PAYLOAD_CN = {"visit_date": "日期", "hospital": "医院", "department": "科室", "doctor": "医生",
              "reason": "就诊原因", "findings": "检查所见", "diagnosis": "诊断", "plan": "处理方案",
              "cost": "费用", "condition": "疾病", "name": "名称", "status": "状态",
              "category": "分类",
              "onset_date": "起病日期", "diagnosed_date": "确诊日期", "summary": "说明",
              "event_date": "日期", "kind": "类型", "title": "标题", "detail": "详情",
              "result": "结果", "start_date": "开始日期", "end_date": "结束日期",
              "flow": "经量", "symptoms": "症状", "note": "备注", "log_date": "日期",
              "value": "数值", "severity": "程度", "title": "标题", "path": "保存位置",
              "body": "正文（摘要）"}


@app.get("/qa/pending")
def qa_pending(user=Depends(authm.require_login)):
    """待确认的写入提案（问答面板里让用户点「确认 / 取消」才落库）。字段名转成中文。"""
    guard(user, "qa")
    items = agentm.list_proposals(user.conn, "pending", limit=10)
    def _short(v):
        text = "" if v is None else str(v)
        return text if len(text) <= 240 else text[:240] + "…（确认后写全文）"

    return {"items": [{"id": p["id"], "kind": p["kind_cn"],
                       "summary": {PAYLOAD_CN.get(k, k): _short(v)
                                   for k, v in (p["payload_obj"] or {}).items()},
                       "rationale": p.get("rationale", ""), "created_at": p["created_at"]}
                      for p in items]}


@app.post("/qa/proposal/{pid}/decide")
def qa_proposal_decide(request: Request, pid: int, user=Depends(authm.require_login),
                       csrf: str = Form(""), action: str = Form("approve")):
    """用户在问答面板里确认/取消一条提案（不必进管理页）。"""
    guard(user, "qa", "edit")
    csrf_ok(request, user, csrf)
    ok, msg, _applied = agentm.decide(user.conn, pid, user.id, approve=(action == "approve"))
    user.audit("proposal_decide", f"#{pid} {action} -> {msg}", client_ip(request))
    return JSONResponse({"ok": ok, "msg": msg})


def _slug(s: str, n: int = 20) -> str:
    """把标题变成安全的文件名片段（保留中文、字母、数字）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (s or "").strip()).strip("-")
    return (s or "问答")[:n]


DOC_EXT = {".pdf", ".txt", ".md", ".csv", ".json", ".log"}


def _extract_doc_text(raw: bytes, ext: str) -> tuple[str, str]:
    """尽量把上传文件变成可读文本；抽不出来就明说抽不出来（不让模型假装看到）。

    PDF 依赖可选的 pypdf（纯 Python，装了就更好用，没装也不影响其它功能）。
    """
    if ext == ".pdf":
        try:
            import io as _io
            import pypdf
        except ImportError:
            return "", "服务器没装 PDF 解析库，只能把文件存下来（请把关键数字打出来，或拍一张照片）"
        try:
            reader = pypdf.PdfReader(_io.BytesIO(raw))
            pages = reader.pages[:20]
            text = "\n".join((p.extract_text() or "") for p in pages)
            text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
            if text:
                return text[:8000], f"已解析 PDF（{len(reader.pages)} 页，取前 8000 字）"
            return "", "这个 PDF 是扫描件，抽不出文字（请把关键数字打出来，或拍一张照片）"
        except Exception as e:  # noqa: BLE001
            return "", f"PDF 解析失败（{str(e)[:60]}），文件已保存"
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            return text[:8000], f"已读入文本（{len(text)} 字，取前 8000 字）"
        except UnicodeDecodeError:
            continue
    return "", "这个文件不是文本，抽不出内容（文件已保存）"


def _parse_card(raw: str) -> dict:
    """从模型输出里抠出卡片 JSON（容忍 ``` 围栏与前后废话），并只保留白名单字段。"""
    s = (raw or "").strip()
    if "```" in s:
        s = re.sub(r"```[a-zA-Z]*", "", s).replace("```", "")
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        obj = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k in ("name", "category", "status", "onset_date", "diagnosed_date",
              "hospital", "department", "doctor", "summary"):
        v = obj.get(k)
        out[k] = ("" if v is None else str(v).strip())[:500]
    out["name"] = out["name"][:100]
    if out.get("status") not in ("active", "monitoring", "resolved"):
        out["status"] = "active"
    for k in ("onset_date", "diagnosed_date"):      # 日期只认 YYYY-MM-DD，其余留空给用户补
        try:
            out[k] = date.fromisoformat(out[k][:10]).isoformat() if out[k] else ""
        except ValueError:
            out[k] = ""
    return out


ARCHIVE_SYSTEM = """你在把一段健康问答整理成一篇可以长期留存的知识库笔记。
要求：
1. 只写对话里确实出现过的内容，不补充、不推断、不给新的医学建议；
2. 用户自述的身体情况和时间点要写清楚；拿不准的地方写「未确认」；
3. 输出 Markdown，固定三级结构：
   ## 这次聊了什么
   ## 结论与待办
   ## 出处（对话里出现过的来源链接；没有就写「无」）
4. 直接给内容，不要写「根据对话」「作为 AI」之类的话。"""


@app.post("/qa/clear")
def qa_clear(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """清空当前对话：把这段对话收起来（archived），开一段全新会话。消息不删除。"""
    guard(user, "qa", "edit")
    csrf_ok(request, user, csrf)
    conn = user.conn
    sid, _ = chatm.active_session(conn, user.id)
    n = conn.execute("SELECT COUNT(*) c FROM qa_messages WHERE session_id=? AND archived=0",
                     (sid,)).fetchone()["c"]
    conn.execute("UPDATE qa_messages SET archived=1 WHERE session_id=?", (sid,))
    conn.execute("INSERT INTO qa_sessions(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
                 (user.id, "日常问答", dbm.now(), dbm.now()))
    conn.commit()
    user.audit("qa_clear", f"收起会话 #{sid}（{n} 条对话）", client_ip(request))
    return JSONResponse({"ok": True, "msg": f"已清空（{n} 条对话收起来了，需要时可在管理页回看）"})


@app.post("/qa/archive")
def qa_archive(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """把这轮对话总结成一篇知识库笔记（个人层、标注「待核对」），然后开新会话。"""
    guard(user, "qa", "edit")
    csrf_ok(request, user, csrf)
    conn = user.conn
    sid, _ = chatm.active_session(conn, user.id)
    transcript, msg_n = _session_transcript(conn, sid)
    if not transcript.strip():
        return JSONResponse({"ok": False, "msg": "这段对话还是空的，没什么可归档"})
    try:
        summary = llmm.chat([{"role": "system", "content": ARCHIVE_SYSTEM},
                             {"role": "user", "content": transcript}],
                            temperature=0.2, max_tokens=1400, timeout=240).strip()
    except llmm.LLMError as e:
        return JSONResponse({"ok": False, "msg": "总结失败（模型不可用）：" + str(e)[:120]})
    if not summary:
        return JSONResponse({"ok": False, "msg": "总结失败：模型返回为空，稍后再试"})
    trow = conn.execute("SELECT title FROM qa_sessions WHERE id=?", (sid,)).fetchone()
    title = ((trow["title"] if trow else "") or "问答").strip()
    day = dbm.today()
    rel = f"医学知识/问答归档/{day}-{_slug(title)}.md"
    body = ("> 本文由问答自动总结生成，可能有错漏；涉及诊断与用药请以医生意见为准。\n"
            f"> 原始对话 {msg_n} 条，整理于 {dbm.now()}。\n\n" + summary + "\n")
    kbm.upsert_doc(conn, rel, body,
                   {"title": f"问答归档 {day} {title}", "tags": ["问答归档"],
                    "source": "问答自动总结", "status": "待核对", "last_updated": day},
                   origin="ai", scope=kbm.SCOPE_PERSONAL)
    conn.execute("UPDATE qa_messages SET archived=1 WHERE session_id=?", (sid,))
    conn.execute("INSERT INTO qa_sessions(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
                 (user.id, "日常问答", dbm.now(), dbm.now()))
    conn.commit()
    user.audit("qa_archive", f"会话 #{sid} → {rel}", client_ip(request))
    return JSONResponse({"ok": True, "msg": f"已总结归档到知识库：{rel}", "rel": rel})


def _session_transcript(conn, sid: int, limit: int = 12000) -> tuple[str, int]:
    """把当前会话（未归档的部分）拼成一段文本，供「总结归档 / 生成病症卡片」用。"""
    rows = conn.execute("SELECT role, content FROM qa_messages"
                        " WHERE session_id=? AND archived=0 ORDER BY id", (sid,)).fetchall()
    text = "\n".join(f"{'用户' if r['role'] == 'user' else '助手'}：{(r['content'] or '')[:600]}"
                     for r in rows)
    return text[-limit:], len(rows)


CARD_SYSTEM = """你在把一段健康问答整理成一张「疾病档案卡」，供用户自己在档案里长期跟踪。
只输出一个 JSON 对象（不要解释、不要代码块围栏），字段如下：
{"name": "疾病或症状的简短名称",
 "category": "分类，如 妇科 / 内分泌 / 皮肤 / 消化 / 其他",
 "status": "active 还在 / monitoring 观察中 / resolved 已好",
 "onset_date": "", "diagnosed_date": "",
 "hospital": "", "department": "", "doctor": "",
 "summary": "3-6 句：怎么发现的、时间线、目前情况、在做什么、还有什么没确认"}
硬性要求：
1. 只写对话里确实出现过的内容；没有的信息一律留空字符串，不要编；
2. 拿不准的日期留空（用户自己补）；
3. 不做诊断、不写用药剂量；
4. summary 用平实中文，不要写「根据对话」这类话。"""


@app.post("/qa/condition_card")
def qa_condition_card(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """把当前对话整理成一张病症卡片（走「待确认」提案，用户点确认才写入疾病档案）。"""
    guard(user, "qa", "edit")
    csrf_ok(request, user, csrf)
    conn = user.conn
    sid, _ = chatm.active_session(conn, user.id)
    transcript, n = _session_transcript(conn, sid)
    if not transcript.strip():
        return JSONResponse({"ok": False, "msg": "这段对话还是空的，先聊点具体的再来生成"})
    try:
        raw = llmm.chat([{"role": "system", "content": CARD_SYSTEM},
                         {"role": "user", "content": transcript}],
                        temperature=0.2, max_tokens=900, timeout=240)
    except llmm.LLMError as e:
        return JSONResponse({"ok": False, "msg": "生成失败（模型不可用）：" + str(e)[:120]})
    payload = _parse_card(raw)
    if not payload.get("name"):
        return JSONResponse({"ok": False, "msg": "模型没给出可用的卡片内容，稍后再试"})
    pid = agentm.create_proposal(conn, "condition_add", payload,
                                 f"来自问答总结（{n} 条对话）：确认后写入疾病档案")
    conn.commit()
    user.audit("qa_condition_card", f"#{pid} {payload.get('name')}", client_ip(request))
    return JSONResponse({"ok": True, "pid": pid,
                         "msg": f"已生成病症卡片「{payload['name']}」，在下面点确认才会写入档案"})


@app.get("/admin/search")
def admin_search_page(request: Request, user=Depends(authm.require_login)):
    """联网检索（Hyperbrowser 云浏览器）配置页：开关 + 密钥 + 测试。"""
    user.require_admin()
    return render(request, user, "admin_search.html", active="admin", title="联网检索",
                  cfg=webm.config(), key_path=str(webm.key_file()),
                  msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""))


@app.post("/admin/search/save")
def admin_search_save(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                      enabled: str = Form("0"), base_url: str = Form(""), api_key: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    try:
        webm.set_config(enabled == "1", base_url)
        if api_key.strip():
            webm.save_key(api_key.strip())
    except Exception as e:  # noqa: BLE001
        return RedirectResponse(url=f"/admin/search?err=保存失败：{str(e)[:80]}", status_code=303)
    user.audit("websearch_save", f"enabled={enabled} key={'变更' if api_key.strip() else '未变'}",
               client_ip(request))
    return RedirectResponse(url="/admin/search?msg=已保存", status_code=303)


@app.post("/admin/search/key/clear")
def admin_search_key_clear(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    webm.clear_key()
    user.audit("websearch_key_clear", "", client_ip(request))
    return RedirectResponse(url="/admin/search?msg=已清除密钥", status_code=303)


@app.post("/admin/search/test")
def admin_search_test(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    info = webm.health()
    user.audit("websearch_test", ("成功" if info["ok"] else f"失败 {info['error'][:80]}"),
               client_ip(request))
    msg = ("测试成功，示例结果：" + "；".join(info["sample"])) if info["ok"] else f"测试失败：{info['error']}"
    return RedirectResponse(url=f"/admin/search?{'msg' if info['ok'] else 'err'}={quote(msg[:200])}",
                           status_code=303)


def kb_scope_stats(conn) -> dict:
    """知识库规模：(文档数, 总字数, 资料笔记数, 资料笔记字数)。累积很多时一眼看清。"""
    rows = conn.execute("SELECT path, length(body) AS n, origin FROM kb_docs").fetchall()
    docs = len(rows)
    chars = sum(int(r["n"] or 0) for r in rows)
    notes = [r for r in rows if (r["path"] or "").startswith("医学知识/资料整理/")]
    return {"docs": docs, "chars": chars, "notes": len(notes),
            "note_chars": sum(int(r["n"] or 0) for r in notes)}


@app.post("/admin/kb/cleanup")
def admin_kb_cleanup(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                     days: int = Form(180)):
    """清理很久以前的 AI 资料笔记（公开资料，丢了能再搜；文档本身不删）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    days = max(30, min(int(days or 180), 3650))
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = user.conn
    rows = conn.execute("SELECT path, updated_at FROM kb_docs WHERE path LIKE ?",
                        ("医学知识/资料整理/%",)).fetchall()
    n = 0
    for r in rows:
        if (r["updated_at"] or "")[:10] and (r["updated_at"] or "")[:10] < cutoff:
            kbm.delete_doc(conn, r["path"])
            n += 1
    conn.commit()
    user.audit("kb_cleanup", f"清理 {days} 天前的资料笔记 {n} 篇", client_ip(request))
    return RedirectResponse(url=f"/admin?msg=已清理 {n} 篇 {days} 天前的资料笔记", status_code=303)


@app.post("/admin/update/check")
def admin_update_check(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """检查有没有新版本（git fetch + 比较）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    info = updm.check()
    user.audit("update_check", ("落后 %d 个提交" % info["behind"]) if info.get("ok") else info.get("error", ""),
               client_ip(request))
    if not info.get("ok"):
        return RedirectResponse(url=f"/admin?err=检查失败：{quote(info.get('error', '')[:120])}",
                               status_code=303)
    if info["behind"]:
        msg = (f"有新版本：落后 {info['behind']} 个提交（{info['local']} → {info['remote']}）"
               f"　最新：{info.get('remote_subject', '')}")
    else:
        msg = f"已是最新（{info['local']}　{info['subject']}）"
    return RedirectResponse(url=f"/admin?msg={quote(msg[:200])}", status_code=303)


@app.post("/admin/update/apply")
def admin_update_apply(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """立即更新：git pull + 部署脚本 + 请求重启。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    res = updm.apply_update()
    user.audit("update_apply", ("成功" if res["ok"] else "失败"), client_ip(request))
    if not res["ok"]:
        return RedirectResponse(url=f"/admin?err=更新失败：{quote(res['log'][-160:])}", status_code=303)
    note = updm.restart_service() if res["restart"] else ""
    return RedirectResponse(url=f"/admin?msg={quote(('更新完成，' + note) if note else '更新完成')}",
                           status_code=303)


@app.post("/admin/update/auto")
def admin_update_auto(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                      auto_check: str = Form("0"), auto_update: str = Form("0"),
                      interval: int = Form(6)):
    user.require_admin()
    csrf_ok(request, user, csrf)
    updm.save_auto(auto_check == "1", auto_update == "1", interval)
    user.audit("update_auto", f"check={auto_check} update={auto_update} every={interval}h",
               client_ip(request))
    return RedirectResponse(url="/admin?msg=自动更新设置已保存", status_code=303)


@app.post("/admin/update/pi")
def admin_update_pi(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """安装/更新 pi 智能体（deploy/setup-node.sh，用户级 Node + pi）。可能要一两分钟。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    res = updm.install_pi()
    user.audit("update_pi", "成功" if res["ok"] else "失败", client_ip(request))
    key = "msg" if res["ok"] else "err"
    tail = res["log"].strip().splitlines()[-1][:120] if res["log"].strip() else ""
    return RedirectResponse(url=f"/admin?{key}={quote(('pi 已处理：' + tail) if res['ok'] else 'pi 处理失败：' + tail)}",
                           status_code=303)


@app.get("/admin/persona")
def admin_persona_page(request: Request, user=Depends(authm.require_login)):
    """AI 角色设定（管理员）：名字 + 提示词。默认是「守护兔」。"""
    user.require_admin()
    with dbm.db() as conn:
        saved_name = dbm.get_setting(conn, "ai_persona_name", "")
        saved_prompt = dbm.get_setting(conn, "ai_persona_prompt", "")
        saved_icon = dbm.get_setting(conn, "ai_persona_icon", "")
    return render(request, user, "admin_persona.html", active="admin", title="AI 角色",
                  cfg={"name": saved_name, "prompt": saved_prompt, "icon": saved_icon,
                       "current": persona()},
                  defaults={"name": PERSONA_DEFAULT_NAME, "prompt": PERSONA_DEFAULT_PROMPT},
                  msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""))


@app.post("/admin/persona/save")
def admin_persona_save(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                       name: str = Form(""), prompt: str = Form(""), icon: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    with dbm.db() as conn:
        dbm.set_setting(conn, "ai_persona_name", (name or "").strip()[:20])
        dbm.set_setting(conn, "ai_persona_prompt", (prompt or "").strip()[:4000])
        dbm.set_setting(conn, "ai_persona_icon", (icon or "").strip()[:4])
        conn.commit()
    user.audit("persona_save", f"name={(name or '').strip()[:20]}", client_ip(request))
    return RedirectResponse(url="/admin/persona?msg=角色已保存（下次提问生效）", status_code=303)


@app.post("/admin/persona/reset")
def admin_persona_reset(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    with dbm.db() as conn:
        dbm.set_setting(conn, "ai_persona_name", "")
        dbm.set_setting(conn, "ai_persona_prompt", "")
        dbm.set_setting(conn, "ai_persona_icon", "")
        conn.commit()
    user.audit("persona_reset", "", client_ip(request))
    return RedirectResponse(url="/admin/persona?msg=已恢复默认角色（守护兔）", status_code=303)


@app.get("/qa")
def qa_page_gone():
    """独立问答页已移除：问答只在右下角悬浮窗里。老书签不至于落到空页。"""
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/api/qa/history")
def qa_history(user=Depends(authm.require_login)):
    """悬浮问答打开时拉取历史：服务端渲染好 HTML，前端直接插入。"""
    guard(user, "qa")
    conn = user.conn
    sid, created = chatm.active_session(conn, user.id)
    rows = conn.execute(
        "SELECT id, role, content, meta, created_at FROM qa_messages"
        " WHERE session_id=? AND archived=0 ORDER BY id", (sid,)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        out.append({"id": r["id"], "role": r["role"], "created_at": r["created_at"],
                    "html": mdrender.to_html(r["content"] or ""),
                    "image": meta.get("image") or "",
                    "think": meta.get("think") or "",
                    "doc_name": meta.get("doc_name") or "",
                    "sources": meta.get("sources") or []})
    return {"sid": sid, "created": created, "title": "日常问答",
            "summary": chatm.session_brief(conn, sid), "total": len(out),
            "compress_over": chatm.COMPRESS_OVER, "messages": out}


@app.get("/qa/msg/{mid}")
def qa_message_html(mid: int, user=Depends(authm.require_login)):
    """单条消息的渲染结果：流式结束后把纯文本换成 HTML。"""
    guard(user, "qa")
    row = user.conn.execute(
        "SELECT m.id, m.role, m.content, m.meta FROM qa_messages m"
        " JOIN qa_sessions s ON s.id=m.session_id WHERE m.id=? AND s.user_id=?",
        (mid, user.id)).fetchone()
    if row is None:
        raise HTTPException(404, "消息不存在")
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {"id": row["id"], "role": row["role"],
            "html": mdrender.to_html(row["content"] or ""),
            "sources": meta.get("sources") or []}


@app.post("/api/qa/quick")
def qa_quick(user=Depends(authm.require_login)):
    """悬浮问答用：拿到（或开创）当前用户的活跃会话 id。一个用户同一时间只有一个。"""
    guard(user, "qa", "edit")
    sid, created = chatm.active_session(user.conn, user.id)
    return {"sid": sid, "created": created, "title": "日常问答"}


PING = ": ping\n\n"          # SSE 注释行：不是 data:，前端解析不到会直接忽略


class _SlowJob:
    """把一段可能很慢的活丢到后台线程，主线程好一边等一边吐心跳。

    手机在长静默（联网检索、让模型判断知识库够不够）时容易被系统或运营商掐掉连接，
    所以这两个阶段改成：线程干活 → 主线程每 5 秒 yield 一个注释行，连接一直是活的。
    """

    def __init__(self, fn):
        self.value = None
        self.error = None
        self._th = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def _run(self, fn):
        try:
            self.value = fn()
        except Exception as e:      # noqa: BLE001 - 由调用方决定怎么降级
            self.error = e

    def start(self):
        self._th.start()
        return self

    def done(self) -> bool:
        return not self._th.is_alive()

    def join(self, timeout: float) -> None:
        self._th.join(timeout)


def _kb_sufficient(question: str, used: list) -> bool:
    """让模型先看一眼《依据》能不能回答这个问题（只是话题相关不算够）。

    覆盖率中间地带才走这一步（很低的直接去联网、很高的直接用），
    这样只在真正需要时才多付一次很小的模型调用。
    """
    if not used:
        return False
    digest = "\n\n".join(f"### {h.get('title', '')} — {h.get('section', '')}\n"
                          f"{(h.get('text') or h.get('snippet') or '')[:1200]}" for h in used[:3])
    try:
        out = llmm.chat([
            {"role": "system",
             "content": "只回两个字之一：够 / 不够。判断《依据》能不能回答用户的问题 ——"
                        "能给出具体答案才算「够」，只是话题相关、缺少具体数字或结论都算「不够」。"},
            {"role": "user", "content": f"问题：{question}\n\n依据：\n{digest}"},
        ], temperature=0.0, max_tokens=8, timeout=30)
    except llmm.LLMError:
        return len(used) >= 2          # 模型不可用时退回保守判断
    return (out or "").strip().startswith("够")


def _kb_note_payload(question: str, srcs: list, ctx: str) -> dict:
    """把这次联网查到的资料整理成一条「知识库笔记」提案（用户点确认才写库）。"""
    title = webm.strip_private(question)[:60].strip() or "联网资料笔记"
    body = re.sub(r"^### .*?\n来源：\S+\n", "", ctx.strip(), flags=re.M)   # 去掉给模型用的包装
    lines = ["> 由问答联网检索自动整理，可能有错漏；涉及诊断与用药请以医生意见为准。",
             f"> 整理于 {dbm.now()}，来源 {len(srcs)} 篇公开网页。", "",
             "## 要点摘录", "", body[:6000], "", "## 来源", ""]
    for s2 in srcs:
        lines.append(f"- [{s2.get('title') or s2['rel']}]({s2['rel']})")
    return {"title": title,
            "path": f"医学知识/资料整理/{dbm.today()}-{_slug(title)}.md",
            "source": "、".join(s2["rel"] for s2 in srcs)[:300],
            "body": "\n".join(lines)[:8000]}


def _web_context(question: str, conn, pages: int = 2) -> tuple[str, list, str]:
    """知识库不够时联网找依据。返回 (依据文本, 来源列表, 说明)。

    只发通用检索词（先摘掉日期/邮箱，再过一遍外发策略）；抓回来的正文带来源 URL。
    """
    q, why = webm.pick_query(question, conn)
    if not q:
        return "", [], f"没联网检索（{why}）"
    try:
        hits = webm.search(q, limit=6)
    except webm.WebSearchError as e:
        return "", [], f"联网检索失败：{e}"
    if not hits:
        return "", [], f"联网检索「{q}」没有结果"
    # 排序：权威医学站点 → 普通站点 → 聚合/自媒体（百度系在 search() 里已经丢掉）
    order = sorted(hits, key=lambda h: -webm.host_rank(h["url"]))[:pages]
    parts, srcs, failed = [], [], []
    for h in order:
        try:
            md, _note = webm.fetch_markdown(h["url"], max_chars=4500)
        except webm.WebSearchError as e:
            failed.append(f"{h['url']}（{e}）")
            continue
        if md:
            parts.append(f"### {h['title']}\n来源：{h['url']}\n{md}")
            srcs.append({"rel": h["url"], "title": h["title"]})
    if not parts:      # 正文抓不到就退化成搜索摘要，让模型知道只看到标题/摘要
        body = "\n".join(f"- {h['title']}（{h['url']}）：{h['description']}" for h in hits[:6])
        return (f"（只取到搜索摘要，正文抓取失败：{'；'.join(failed)[:200]}）\n{body}",
                [{"rel": h["url"], "title": h["title"]} for h in hits[:6]], "")
    return "\n\n".join(parts), srcs, ""


def _audit(user, action: str, detail: str, request) -> None:
    """生成器阶段用的审计：自己开连接（那时依赖注入的连接已被回收）。

    审计失败绝不能影响回答，所以这里吞掉异常。
    """
    try:
        with dbm.db() as c:
            dbm.audit(c, user.id, user.username, action, detail, client_ip(request))
    except Exception:  # noqa: BLE001
        pass


@app.post("/qa/{sid}/ask")
async def qa_ask(sid: int, request: Request, user=Depends(authm.require_login),
                 question: str = Form(...), csrf: str = Form(""),
                 image: UploadFile | None = File(None),
                 doc: UploadFile | None = File(None)):
    """提问（可选带一张图片或一个文件，如化验单照片 / 导出的报告）。SSE 流式返回。"""
    guard(user, "qa", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    question = question.strip()[:2000]
    if not question:
        raise HTTPException(400, "问题不能为空")

    # ---------- 图片：校验 → 压缩 → 落盘（仅服务器本地）----------
    img_data_url, img_note, img_stored = "", "", ""
    if image is not None and image.filename:
        if (image.content_type or "") not in media.ALLOWED:
            raise HTTPException(400, f"不支持的图片类型：{image.content_type}")
        raw = image.file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(400, "图片超过 12MB 限制")
        data, mime, _dim, img_note = media.prepare_image(raw, image.content_type or "")
        ext = ".jpg" if mime == "image/jpeg" else (Path(image.filename).suffix.lower()[:8] or ".img")
        img_stored = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / img_stored).write_bytes(data)
        img_data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}" if img_note else ""
        if not img_note:
            img_data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"

    # ---------- 文件：本地留存 + 尽量抽出文字（PDF 要 pypdf，没有就明说） ----------
    doc_stored, doc_name, doc_text, doc_note = "", "", "", ""
    if doc is not None and doc.filename:
        doc_name = Path(doc.filename).name[:120]
        ext = Path(doc_name).suffix.lower()[:8]
        if ext not in DOC_EXT:
            raise HTTPException(400, "只支持 PDF / 文本类文件（.pdf .txt .md .csv .json .log）")
        raw_doc = doc.file.read(MAX_UPLOAD + 1)
        if len(raw_doc) > MAX_UPLOAD:
            raise HTTPException(400, "文件超过 12MB 限制")
        doc_stored = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / doc_stored).write_bytes(raw_doc)
        doc_text, doc_note = _extract_doc_text(raw_doc, ext)

    session = conn.execute("SELECT * FROM qa_sessions WHERE id=? AND user_id=?",
                           (sid, user.id)).fetchone()
    if session is None:
        cur = conn.execute(
            "INSERT INTO qa_sessions(user_id, title, created_at, updated_at) VALUES(?,?,?,?)",
            (user.id, question[:24], dbm.now(), dbm.now()))
        sid = int(cur.lastrowid)
        session = conn.execute("SELECT * FROM qa_sessions WHERE id=?", (sid,)).fetchone()
    # 先取历史（不含本次提问）：摘要 + 最近若干条（由 chat.py 统一管理）
    history = chatm.history_for_model(conn, sid)
    brief = chatm.session_brief(conn, sid)
    conn.execute(
        "INSERT INTO qa_messages(session_id, role, content, meta, created_at) VALUES(?,?,?,?,?)",
        (sid, "user", question,
         json.dumps({**({"image": img_stored, "image_note": img_note} if img_stored else {}),
                     **({"doc": doc_stored, "doc_name": doc_name, "doc_note": doc_note}
                        if doc_stored else {})}, ensure_ascii=False), dbm.now()))
    if session["title"] in ("", "新对话"):
        conn.execute("UPDATE qa_sessions SET title=? WHERE id=?", (question[:24], sid))
    conn.execute("UPDATE qa_sessions SET updated_at=? WHERE id=?", (dbm.now(), sid))
    conn.commit()
    if img_stored:
        conn.execute(
            "INSERT INTO attachments(ref_kind, ref_id, filename, mime, size, stored_name,"
            " uploaded_by, created_at) VALUES('qa',?,?,?,?,?,?,?)",
            (sid, (image.filename or img_stored)[:200], "image/jpeg",
             (UPLOAD_DIR / img_stored).stat().st_size, img_stored, user.id, dbm.now()))
        conn.commit()
    if doc_stored:
        conn.execute(
            "INSERT INTO attachments(ref_kind, ref_id, filename, mime, size, stored_name,"
            " uploaded_by, created_at) VALUES('qa',?,?,?,?,?,?,?)",
            (sid, doc_name, (doc.content_type or "")[:60],
             (UPLOAD_DIR / doc_stored).stat().st_size, doc_stored, user.id, dbm.now()))
        conn.commit()

    if img_data_url:
        user_turn = {"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": img_data_url}},
        ]}
    else:
        user_turn = {"role": "user", "content": question}

    # ---------- 依据与提示词的组装放进 gen()：界面能实时看到进行到哪一步 ----------
    def _build_messages(db_conn) -> tuple[list[dict], list[dict], str]:
        """返回 (messages, 命中的依据, 被拦截的原因)。

        注意：这个函数在 gen() 里跑，那时依赖注入的 user.conn 已经被回收关闭
        （响应头已发出、生成器还在跑），必须用调用方新开的连接。
        """
        kb_ctx, used = kbm.context_for_question(question)
        if img_stored:
            ref = kbm.read_text("医学知识/检查项目与参考值.md") or ""
            if ref:
                kb_ctx = (kb_ctx + "\n\n### 依据：检查项目与参考值（节选）\n" + ref[:3500]).strip()
                used = used + [{"rel": "医学知识/检查项目与参考值.md"}]
        _pn = persona()
        system_parts = [QA_SYSTEM, f"【你的角色】{_pn['name']}\n{_pn['prompt']}"]
        _idn = _identity_note(user)
        if _idn:
            system_parts.append(_idn)
        system_parts.append("【档案数据】\n" + _profile_summary(db_conn, pid))
        if brief:
            system_parts.append("【历史对话摘要】\n" + brief)
        if kb_ctx:
            system_parts.append("【知识库依据】\n" + kb_ctx)
        else:
            system_parts.append("【知识库依据】\n（本次检索未命中知识库文档。）")
        if img_stored:
            system_parts.append(VISION_SYSTEM)
        if doc_text:
            system_parts.append(f"【附件内容：{doc_name}】\n{doc_text}")
        elif doc_note:
            system_parts.append(f"【附件】用户上传了 {doc_name}，但{doc_note}。"
                                "请明确告诉用户你看不到文件内容，并请他把关键数字打出来。")
        msgs = [{"role": "system", "content": "\n\n".join(system_parts)}]
        msgs += history
        msgs.append(user_turn)
        # 外发内容策略：身份/工程信息一律抹掉，公网端点 fail-closed
        public = not llmm.is_private_host(
            urllib.parse.urlparse(llmm.config()["base_url"]).hostname or "")
        blocked = ""
        try:
            msgs, hits = outbound.guard_messages(msgs, public=public, conn=db_conn)
            if hits:
                _audit(user, "qa_outbound_scrub", f"public={public} 抹除={hits}", request)
        except outbound.OutboundBlocked as e:
            blocked = str(e)
            _audit(user, "qa_outbound_blocked", f"public={public} {blocked}", request)
            db_conn.commit()
        return msgs, used, blocked

    def _pipeline():
        """整段问答（准备 → 联网 → 生成 → 落库）。跑在自己的线程里，见下面的 gen()。"""
        def sse(obj: dict) -> str:
            return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

        yield sse({"s": "读取档案与知识库…"})
        try:
            with dbm.db() as db_conn:
                msgs, used, blocked = _build_messages(db_conn)
        except Exception as e:  # noqa: BLE001
            yield sse({"e": "准备上下文失败：" + str(e)[:200]})
            yield sse({"done": True, "sid": sid, "mid": 0})
            return
        if blocked:
            yield sse({"e": blocked})
            yield sse({"done": True, "sid": sid, "mid": 0})
            return
        # 知识库够不够？先用覆盖率粗筛（很低＝肯定不够、很高＝肯定够），中间地带问一次模型。
        # 注意：这里要自己开连接（上面那个 with 已经关掉了）
        _cov = kbm.coverage(question, used)
        _need_web = False
        if webm.enabled() and not img_stored:
            if _cov < 0.15 or not used:
                _need_web = True
            elif _cov > 0.75:
                _need_web = False
            else:
                yield sse({"s": "先看知识库够不够回答…"})
                _job = _SlowJob(lambda: _kb_sufficient(question, used)).start()
                while not _job.done():
                    _job.join(5.0)
                    if not _job.done():
                        yield PING
                _need_web = len(used) < 2 if _job.error is not None else not _job.value
        if _need_web:
            yield sse({"s": f"知识库不够（覆盖 {int(_cov * 100)}%），正在联网检索…"})
            web_ctx, web_srcs, note = "", [], ""

            def _do_web() -> tuple:
                with dbm.db() as c_web:
                    return _web_context(question, c_web)

            _job = _SlowJob(_do_web).start()
            while not _job.done():
                _job.join(5.0)
                if not _job.done():
                    yield PING
            if _job.error is not None:
                note = f"联网检索异常：{str(_job.error)[:120]}"
            elif _job.value:
                web_ctx, web_srcs, note = _job.value
            if web_ctx:
                msgs[0]["content"] += "\n\n【联网依据（公开网页，请按 URL 注明来源）】\n" + web_ctx
                used = used + web_srcs
                _audit(user, "qa_web_search", f"命中 {len(web_srcs)} 条网页", request)
                # 自动归档：AI 判断这次查到的资料值得留下就直接写进知识库
                # （用户要求：知识库不用逐条确认，也不需要仓库备份，本地保存和维护即可）
                try:
                    with dbm.db() as c_note:
                        _msg, _k, _rid = agentm.apply_kb_note(
                            c_note, _kb_note_payload(question, web_srcs, web_ctx))
                        c_note.commit()
                    _audit(user, "qa_kb_note_saved", _msg, request)
                    msgs[0]["content"] += (
                        f"\n\n【已自动归档】本次查到的外部资料已经收进知识库（{_msg}）。"
                        "如果合适，可以在回答最后一句顺带提一下「我把找到的资料整理进知识库了」。")
                except Exception as e:  # noqa: BLE001 - 归档失败不影响回答
                    _audit(user, "qa_kb_note_failed", str(e)[:120], request)
            elif note:
                extra = f"\n\n【联网检索】{note}。请据此说明你没能查到外部资料，不要编。"
                try:
                    with dbm.db() as c_cat:
                        cat = kbm.catalog()
                    if cat:
                        extra += "当前知识库里只有这些文档，可以告诉用户还能看哪一篇：\n" + cat
                except Exception:  # noqa: BLE001
                    pass
                msgs[0]["content"] += extra
                yield sse({"s": note[:60]})
        yield sse({"s": (f"取到 {len(used)} 段依据，正在生成…" if used
                         else "知识库没命中，正在生成…")})
        parts: list[str] = []
        thoughts: list[str] = []
        raw = ""            # 模型原始输出（可能末尾带记录块）
        emitted = 0         # 已经吐给前端的正文字符数
        t_start = time.time()
        try:
            for ev in llmm.stream_chat_events(msgs):
                if ev.get("r"):                      # 模型的思考过程：单独一条通道给界面
                    thoughts.append(ev["r"])
                    yield sse({"th": ev["r"]})
                    continue
                delta = ev.get("t") or ""
                if not delta:
                    continue
                parts.append(delta)
                raw += delta
                # 先压住尾巴几十个字符：记录块不该在界面上一闪而过
                cut = len(raw) - 60
                i = raw.find("<record")
                if 0 <= i < cut:
                    cut = i
                if cut > emitted:
                    yield sse({"t": raw[emitted:cut]})
                    emitted = cut
        except llmm.LLMError as e:
            yield sse({"e": str(e)})
        except Exception as e:  # noqa: BLE001
            yield sse({"e": "生成中断：" + str(e)[:200]})
        clean, blocks = _extract_records("".join(parts))
        rest = clean[emitted:].strip() if len(clean) > emitted else ""
        if rest:
            yield sse({"t": rest})
        # 记录块 → 一条待确认提案（用户在问答面板里点「确认」才写入）
        created = []
        for kind, payload in blocks[:1]:
            try:
                if kind == "kb_note":      # 知识笔记：AI 自己判断，直接入库
                    with dbm.db() as c0:
                        _m, _k, _r = agentm.apply_kb_note(c0, payload)
                        c0.commit()
                    _audit(user, "qa_kb_note_saved", _m, request)
                    continue
                with dbm.db() as c1:
                    nid = agentm.create_proposal(c1, kind, payload,
                                                 "来自问答：用户提到的记录，待你确认")
                created.append({"id": nid, "kind": agentm.PROPOSAL_KINDS.get(kind, kind)})
                _audit(user, "qa_proposal", f"#{nid} {kind}", request)
            except Exception:  # noqa: BLE001 - 记录块不合法也不影响这次回答
                pass
        answer = clean
        mid = 0
        think_text = "".join(thoughts)[-4000:]      # 留在消息里，回看时点开还能看到
        with dbm.db() as c2:
            cur = c2.execute(
                "INSERT INTO qa_messages(session_id, role, content, meta, created_at) VALUES(?,?,?,?,?)",
                (sid, "assistant", answer,
                 json.dumps({"sources": list(dict.fromkeys(h["rel"] for h in used)),
                             "think": think_text}, ensure_ascii=False), dbm.now()))
            mid = int(cur.lastrowid or 0)
            c2.execute("UPDATE qa_sessions SET updated_at=? WHERE id=?", (dbm.now(), sid))
        # 每次问答留一条可查的记录：这次是客户端断开还是服务端出错，事后一眼能看出来
        _audit(user, "qa_done",
               f"{time.time() - t_start:.1f}s 答 {len(answer)} 字 依据 {len(used)} 段，"
               f"mid={mid}", request)
        # 先把答复吐完（界面立刻可用），再顺手压一次上下文：压缩规则由后端决定，用户不用管
        yield sse({"done": True, "sid": sid, "mid": mid, "created": created,
                   "think": bool(think_text)})
        try:
            with dbm.db() as c3:
                if chatm.needs_compress(c3, sid):
                    chatm.compress_session(c3, sid, llm=llmm)
        except Exception:  # noqa: BLE001 - 压缩失败不影响本次问答
            pass

    # 生成放在自己的线程里，通过队列喂给响应。
    # 为什么：手机切网/锁屏/切后台时浏览器会直接掐断连接，Starlette 只是停止消费这个
    # 生成器（0.41 的 iterate_in_threadpool 连 close 都不调用），于是「落库」那段永远
    # 走不到 —— 用户回来只看到自己那个没人回答的问题，答案凭空消失。
    # 现在落库由生产者线程负责，跟客户端在不在没关系：断开也照样把整段回答存下来。
    q: queue.Queue = queue.Queue()

    def _produce() -> None:
        try:
            for chunk in _pipeline():
                q.put(chunk)
        except Exception as e:  # noqa: BLE001 - 兜底，别让消费端一直等
            try:
                q.put("data: " + json.dumps({"e": "生成中断：" + str(e)[:200]},
                                            ensure_ascii=False) + "\n\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            q.put(None)                      # 结束哨兵

    async def gen():
        threading.Thread(target=_produce, daemon=True).start()
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                # 客户端断开时这里会被取消，生产者线程自己把剩下的活干完
                await asyncio.sleep(0.2)
                continue
            if item is None:
                break
            yield item

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ------------------------------------------------------------------ 账号

@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(authm.require_login), msg: str = "", err: str = ""):
    return render(request, user, "account.html", active="account", title="我的账号",
                  msg=msg, err=err, must_change=bool(user.row["must_change_password"]))


@app.post("/account/profile")
def account_profile(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                    nickname: str = Form("")):
    """改昵称（留空＝显示用户名）。界面上右上角就是这个名字。"""
    csrf_ok(request, user, csrf)
    nick = re.sub(r"\s+", " ", (nickname or "").strip())[:20]
    user.conn.execute("UPDATE users SET display_name=? WHERE id=?", (nick, user.id))
    user.conn.commit()
    user.audit("profile_update", f"昵称={'（清空）' if not nick else nick}", client_ip(request))
    return RedirectResponse(url="/account?msg=昵称已更新", status_code=303)


@app.post("/account/password")
def account_password(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                     old_password: str = Form(...), new_password: str = Form(...),
                     confirm: str = Form(...)):
    csrf_ok(request, user, csrf)
    if not authm.verify_password(old_password, user.row["password_hash"]):
        return RedirectResponse(url="/account?err=原密码不正确", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/account?err=新密码至少 8 位", status_code=303)
    if new_password != confirm:
        return RedirectResponse(url="/account?err=两次输入的新密码不一致", status_code=303)
    user.conn.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                      (authm.hash_password(new_password), user.id))
    user.conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user.id, user.token or ""))
    user.audit("password_change", "", client_ip(request))
    return RedirectResponse(url="/account?msg=密码已更新，其他设备的会话已注销", status_code=303)


# ------------------------------------------------------------------ 管理

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(authm.require_login), msg: str = "", err: str = "",
               new_password: str = ""):
    user.require_admin()
    conn = user.conn
    users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    sessions = conn.execute(
        "SELECT s.user_id, COUNT(*) n, MAX(s.created_at) last FROM sessions s"
        " GROUP BY s.user_id").fetchall()
    sess_map = {int(r["user_id"]): dict(r) for r in sessions}
    logs = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 40").fetchall()
    return render(request, user, "admin.html", active="admin", title="管理", users=users,
                  who_cn=WHO_CN, sess_map=sess_map, logs=logs, msg=msg, err=err,
                  new_password=new_password, llm=llmm.config(),
                  llm_check=dbm.get_setting(conn, "llm_last_check", ""),
                  kb=kbm.stats(), kb_dir=str(kbm.DOCS_DIR), kb_stat=kb_scope_stats(conn),
                  upd=updm.auto_settings(), pi=updm.pi_state(),
                  sync=_sync_settings(conn), bk=backupm.settings(conn), chat=chatm.settings(),
                  sync_trigger=dbm.get_setting(conn, "kb_last_trigger", ""),
                  backups=backupm.list_backups(), pr=_prune_ctx(conn))


@app.post("/admin/users")
def admin_create_user(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                      username: str = Form(...), display_name: str = Form(""),
                      who: str = Form(""), is_admin: str = Form(""), password: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    name = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", name):
        return RedirectResponse(url="/admin?err=用户名需为 3-32 位小写字母/数字/._-", status_code=303)
    conn = user.conn
    if authm.get_user(conn, name):
        return RedirectResponse(url="/admin?err=该用户名已存在", status_code=303)
    pw = password.strip() or authm.random_password(14)
    cur = conn.execute(
        "INSERT INTO users(username, display_name, password_hash, role, who,"
        " must_change_password, created_at) VALUES(?,?,?,?,?,1,?)",
        (name, display_name.strip() or name, authm.hash_password(pw),
         "admin" if is_admin == "1" else "member", who if who in ("her", "him") else "",
         dbm.now()))
    # 数据权限对所有账号一律完整（两人共同维护同一份档案），无需再逐模块授权
    user.audit("user_create", f"{name} who={who} admin={is_admin == '1'}", client_ip(request))
    msg = f"已创建账号 {name}（{WHO_CN.get(who, '身份未指定')}"
    msg += "，管理员" if is_admin == "1" else "，普通账号"
    msg += "）"
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg)}&new_password={pw}",
                            status_code=303)


@app.post("/admin/users/{uid}/profile")
def admin_user_profile(uid: int, request: Request, user=Depends(authm.require_login),
                       csrf: str = Form(""), who: str = Form(""), is_admin: str = Form("")):
    """设置账号身份（她/他）与是否为管理员。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    target = authm.get_user_by_id(conn, uid)
    if target is None:
        return RedirectResponse(url="/admin?err=用户不存在", status_code=303)
    if int(target["id"]) == user.id and is_admin != "1":
        return RedirectResponse(url="/admin?err=不能取消自己的管理员权限（请让另一位管理员操作）",
                                status_code=303)
    authm.set_who(conn, uid, who)
    authm.set_admin(conn, uid, is_admin == "1")
    user.audit("user_profile", f"{target['username']} who={who} admin={is_admin == '1'}",
               client_ip(request))
    label = WHO_CN.get(who, "身份未指定")
    role_label = "管理员" if is_admin == "1" else "普通账号"
    msg = f"已更新 {target['username']}：{label}，{role_label}"
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg)}", status_code=303)


@app.post("/admin/users/{uid}/password")
def admin_reset_password(uid: int, request: Request, user=Depends(authm.require_login),
                         csrf: str = Form(""), new_password: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    target = authm.get_user_by_id(conn, uid)
    if target is None:
        return RedirectResponse(url="/admin?err=用户不存在", status_code=303)
    pw = new_password.strip() or authm.random_password(14)
    if len(pw) < 8:
        return RedirectResponse(url="/admin?err=密码至少 8 位", status_code=303)
    conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
                 (authm.hash_password(pw), uid))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    user.audit("password_reset", target["username"], client_ip(request))
    return RedirectResponse(url=f"/admin?msg=已重置 {target['username']} 的密码&new_password={pw}",
                            status_code=303)


@app.post("/admin/users/{uid}/toggle")
def admin_toggle_user(uid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    target = authm.get_user_by_id(conn, uid)
    if target is None or int(target["id"]) == user.id:
        return RedirectResponse(url="/admin?err=不能停用当前登录账号", status_code=303)
    conn.execute("UPDATE users SET is_active=? WHERE id=?",
                 (0 if target["is_active"] else 1, uid))
    user.audit("user_toggle", f"{target['username']} active={1 - int(target['is_active'])}",
               client_ip(request))
    return RedirectResponse(url="/admin?msg=账号状态已更新", status_code=303)


@app.post("/admin/users/{uid}/delete")
def admin_delete_user(uid: int, request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    target = authm.get_user_by_id(conn, uid)
    if target is None or int(target["id"]) == user.id:
        return RedirectResponse(url="/admin?err=不能删除当前登录账号", status_code=303)
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM space_perms WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    user.audit("user_delete", target["username"], client_ip(request))
    return RedirectResponse(url="/admin?msg=账号已删除", status_code=303)


# ------------------------------------------------------------------ 导出到知识库

EXPORT_TARGETS = {
    "医学知识": "knowledge",
    "经期记录": "cycle",
    "疾病档案": "conditions",
    "就医记录": "visits",
}


@app.post("/export")
def export_md(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
              push: str = Form("1")):
    guard(user, "knowledge", "edit")
    csrf_ok(request, user, csrf)
    conn, pid = user.conn, profile_id(user)
    written = []

    def write(rel: str, text: str) -> None:
        p = KB_DIR / "docs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        # 同步进数据库（知识库页面/检索都读数据库）
        meta, body = kbm.parse_frontmatter(text)
        kbm.upsert_doc(conn, rel, body, meta, origin="export")
        written.append(rel)

    cycles = conn.execute("SELECT * FROM cycles WHERE profile_id=? ORDER BY start_date DESC",
                          (pid,)).fetchall()
    rows = ["| 起始日期 | 结束日期 | 经期天数 | 经量 | 症状 | 备注 |", "|---|---|---|---|---|---|"]
    for c in cycles:
        s, e = d(c["start_date"]), d(c["end_date"])
        n = (e - s).days + 1 if (s and e) else ""
        rows.append(f"| {c['start_date']} | {c['end_date'] or ''} | {n} | "
                    f"{FLOW_CN.get(c['flow'], c['flow'] or '')} | {c['symptoms'] or ''} | {c['note'] or ''} |")
    write("经期记录/周期记录.md", f"""---
title: 周期记录
tags:
  - 经期
  - 个人数据
aliases:
  - 周期记录
last_updated: {dbm.today()}
status: current
---

# 周期记录

由 WebUI 导出生成，请勿手工编辑结构。共 {len(cycles)} 次记录。

> 仅供健康管理参考，不能替代医生诊断。

{chr(10).join(rows) if cycles else '（暂无记录）'}
""")

    info = cyclem.analyze(dbm.cycle_starts(conn, pid), dbm.cycle_durations(conn, pid))
    s = info["stats"]
    table = ["| 阶段 | 天数 | 说明 |", "|---|---|---|"]
    for r in cyclem.cycle_phase_table(info["cycle_len"], info["period_len"]):
        table.append(f"| {r['phase_cn']} | {r['days']} | {r['note']} |")
    logs = conn.execute(
        "SELECT * FROM day_logs WHERE profile_id=? ORDER BY log_date DESC LIMIT 120", (pid,)).fetchall()
    log_rows = ["| 日期 | 类型 | 名称 | 程度 | 数值 | 备注 |", "|---|---|---|---|---|---|"]
    for l in logs:
        log_rows.append(f"| {l['log_date']} | {KIND_CN.get(l['kind'], l['kind'])} | {l['name'] or ''} | "
                        f"{l['severity'] or ''} | {l['value'] or ''} | {l['note'] or ''} |")
    write("经期记录/周期统计.md", f"""---
title: 周期统计与推断
tags:
  - 经期
  - 统计
aliases:
  - 周期统计
last_updated: {dbm.today()}
status: current
---

# 周期统计与推断

由 WebUI 导出生成。**以下预测为基于历史记录的统计推断，不是医学检测结果。**

| 指标 | 数值 |
|---|---|
| 记录次数 | {s['n_cycles']} |
| 已有间隔数 | {s['n_lengths']} |
| 近 6 个周期长度 | {', '.join(map(str, s['recent_lengths'])) or '—'} |
| 中位数周期长度 | {s['median'] or '—'} 天 |
| 平均周期长度 | {s['mean'] or '—'} 天 |
| 波动范围 | {s['min'] or '—'} ~ {s['max'] or '—'} 天 |
| 标准差 | {s['stdev'] or '—'} 天 |
| 经期平均长度 | {s['period_len_mean'] or '—'} 天 |
| 上次经期起始 | {s['last_start'] or '—'} |
| 推断下次经期 | {info['next_start'] or '—'} |
| 推断区间 | {(info['next_window'][0].isoformat() + ' ~ ' + info['next_window'][1].isoformat()) if info['next_window'] else '—'} |
| 置信度 | {info['confidence']}（{info['confidence_reason']}） |
| 当前阶段 | {info['phase_cn']}（周期第 {info['cycle_day'] or '—'} 天） |

## 一个完整周期的阶段划分

{chr(10).join(table)}

## 记录中的异常提示

{chr(10).join('- ' + x for x in info['irregular_flags']) if info['irregular_flags'] else '- 暂无（基于现有记录）'}

## 近期日志

{chr(10).join(log_rows) if logs else '（暂无日志）'}

> {EXPORT_DISCLAIMER}
""")

    conds = conn.execute("SELECT * FROM conditions WHERE profile_id=? ORDER BY id", (pid,)).fetchall()
    crows = ["| 疾病 | 分类 | 状态 | 确诊日期 | 医院 | 科室 | 病程事件数 |", "|---|---|---|---|---|---|---|"]
    for c in conds:
        n = conn.execute("SELECT COUNT(*) AS n FROM condition_events WHERE condition_id=?",
                         (c["id"],)).fetchone()["n"]
        crows.append(f"| [[{c['name']}]] | {c['category'] or ''} | {STATUS_CN.get(c['status'], c['status'])} | "
                     f"{c['diagnosed_date'] or ''} | {c['hospital'] or ''} | {c['department'] or ''} | {n} |")
    write("疾病档案/疾病列表.md", f"""---
title: 疾病列表
tags:
  - 疾病档案
  - 个人数据
aliases:
  - 疾病列表
last_updated: {dbm.today()}
status: current
---

# 疾病列表

由 WebUI 导出生成。共 {len(conds)} 条。

{chr(10).join(crows) if conds else '（暂无记录）'}

> {EXPORT_DISCLAIMER}
""")
    for c in conds:
        events = conn.execute(
            "SELECT * FROM condition_events WHERE condition_id=? ORDER BY event_date", (c["id"],)).fetchall()
        erows = ["| 日期 | 类型 | 标题 | 详情 | 结果 |", "|---|---|---|---|---|"]
        for e in events:
            erows.append(f"| {e['event_date']} | {EVENT_CN.get(e['kind'], e['kind'])} | {e['title'] or ''} | "
                         f"{(e['detail'] or '').replace(chr(10), ' ')} | {(e['result'] or '').replace(chr(10), ' ')} |")
        write(f"疾病档案/{c['name']}.md", f"""---
title: {c['name']}
tags:
  - 疾病档案
  - 个人数据
aliases:
  - {c['name']}
last_updated: {dbm.today()}
status: current
---

# {c['name']}

| 字段 | 内容 |
|---|---|
| 状态 | {STATUS_CN.get(c['status'], c['status'])} |
| 分类 | {c['category'] or '—'} |
| 起病时间 | {c['onset_date'] or '—'} |
| 确诊时间 | {c['diagnosed_date'] or '—'} |
| 医院/科室 | {c['hospital'] or '—'} / {c['department'] or '—'} |
| 医生 | {c['doctor'] or '—'} |

## 概述

{c['summary'] or '（暂无）'}

## 病程事件

{chr(10).join(erows) if events else '（暂无事件记录）'}

> {EXPORT_DISCLAIMER}
""")

    visits = conn.execute("SELECT * FROM visits WHERE profile_id=? ORDER BY visit_date DESC",
                          (pid,)).fetchall()
    vrows = ["| 日期 | 医院 | 科室 | 医生 | 就诊原因 | 检查所见 | 诊断 | 处置 | 费用 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for v in visits:
        vrows.append(f"| {v['visit_date']} | {v['hospital'] or ''} | {v['department'] or ''} | "
                     f"{v['doctor'] or ''} | {v['reason'] or ''} | {v['findings'] or ''} | "
                     f"{v['diagnosis'] or ''} | {v['plan'] or ''} | {v['cost'] if v['cost'] is not None else ''} |")
    write("就医记录/就诊记录.md", f"""---
title: 就诊记录
tags:
  - 就医
  - 个人数据
aliases:
  - 就诊记录
last_updated: {dbm.today()}
status: current
---

# 就诊记录

由 WebUI 导出生成，共 {len(visits)} 次。

{chr(10).join(vrows) if visits else '（暂无记录）'}

> {EXPORT_DISCLAIMER}
""")

    result = {"files": written, "committed": False, "pushed": False, "message": ""}
    if os.environ.get("BG_DRY_RUN", "0") == "1":
        result["message"] = "（DRY RUN：未提交）"
        return RedirectResponse(url=f"/admin?msg=已导出 {len(written)} 个文件（DRY RUN）", status_code=303)
    try:
        git = shutil.which("git") or "/usr/bin/git"
        env = dict(os.environ)
        # 提交身份：优先用部署配置注入的环境变量（见 deploy/config.sh 的 GIT_AUTHOR_*），
        # 否则用中性默认值——绝不在代码里写死个人邮箱。
        who = os.environ.get("BG_GIT_AUTHOR_NAME", "bunny-guardian")
        mail = os.environ.get("BG_GIT_AUTHOR_EMAIL", "bunny-guardian@localhost")
        env.setdefault("GIT_AUTHOR_NAME", who)
        env.setdefault("GIT_AUTHOR_EMAIL", mail)
        env.setdefault("GIT_COMMITTER_NAME", who)
        env.setdefault("GIT_COMMITTER_EMAIL", mail)

        def run(*args: str) -> str:
            # 提交/推送发生在**知识库仓库**，与系统代码仓库完全分开
            r = subprocess.run([git, *args], cwd=str(KB_DIR), capture_output=True, text=True, env=env)
            return (r.stdout + r.stderr).strip()

        run("add", "docs")
        diff = run("status", "--porcelain", "--", "docs")
        if diff:
            run("commit", "-m", f"record: 导出健康记录 {dbm.today()}（{len(written)} 个文件）")
            result["committed"] = True
            if push == "1":
                # 服务以非 root 用户运行，推送通过部署脚本（见 deploy/README.md）
                push_cmd = os.environ.get("BG_PUSH_CMD", "")
                if push_cmd:
                    argv = shlex.split(push_cmd)
                    r = subprocess.run(argv, cwd=str(REPO_DIR), capture_output=True, text=True, env=env)
                    out = (r.stdout + r.stderr).strip()
                    result["pushed"] = r.returncode == 0
                else:
                    out = run("push", "origin", "HEAD")
                    result["pushed"] = "error" not in out.lower() and "rejected" not in out.lower()
                result["message"] = out[:200]
        else:
            result["message"] = "内容无变化"
    except Exception as e:  # noqa: BLE001
        result["message"] = f"git 操作失败：{e}"
    user.audit("export", json.dumps(result, ensure_ascii=False), client_ip(request))
    state = ("已提交并推送" if result["pushed"] else
             "已提交（推送失败，见日志）" if result["committed"] else "无变化")
    return RedirectResponse(
        url=f"/admin?msg=导出完成：{len(written)} 个文件，{state} {result['message'][:80]}",
        status_code=303)


# ================================================================== 智能体（pi）权限框架
#
# 允许范围：病情询问 / 联网搜索 / 病情问答 / 数据记录整理归档（经 GitHub 维护） / 月经询问与预测
# 禁止：执行命令、写任意文件、读账号与会话、删除记录、改知识文档、直接 push、访问白名单外网络
#
# 这里只暴露「只读查询 + 写提案」两类接口；任何写入都必须由人在 /admin/agent 批准。

AGENT_TOKEN_HEADER = "x-agent-token"


class AgentCtx:
    """智能体调用上下文：数据库连接 + 令牌标签 + 作用域，统一写审计。"""

    def __init__(self, conn, label: str, scope: str = "read"):
        self.conn = conn
        self.label = label
        self.scope = scope if scope in agentm.SCOPES else "read"

    @property
    def can_write(self) -> bool:
        return self.scope == "write"

    def log_durable(self, tool: str, decision: str, detail: str = "",
                    preview: str = "") -> None:
        """写一条**不会被回滚**的审计。

        require_agent 在本请求抛错（403/400）时会 rollback，所以被拒的记录必须当场提交，
        否则越权尝试不留痕。

        两个坑（都踩过）：
          1) 用 self.conn 写完之后**必须显式 commit**，否则还是会被之后那一次 rollback 抹掉；
          2) 不能改开另一个连接来写——本连接此时持有未提交的写事务（check_token 会
             `UPDATE agent_tokens ... uses+1`），SQLite 会把第二个连接挡在锁外，
             而「写审计失败就静默放过」正好让这条记录凭空消失。
        所以：先本连接提交；真失败了再退到独立连接，并把失败打到日志里，不再静默。
        """
        try:
            agentm.audit(self.conn, self.label, tool, decision, detail, preview)
            self.conn.commit()
            return
        except Exception as e:                       # noqa: BLE001
            print(f"[audit] 拒绝记录写入失败（本连接）：{e}", flush=True)
        try:
            with dbm.db() as c:
                agentm.audit(c, self.label, tool, decision, detail, preview)
        except Exception as e:                       # noqa: BLE001
            print(f"[audit] 拒绝记录写入失败：{e}", flush=True)

    def need_write(self, tool: str) -> None:
        """写操作的门：必须是 write 作用域令牌。"""
        if not self.can_write:
            self.log_durable(tool, "denied", "令牌作用域为 read，不允许直接写")
            raise HTTPException(
                status_code=403,
                detail="当前令牌是只读作用域（read），不能直接改数据。"
                       "请让档案主人在「管理 → 智能体权限」里生成一个 write 作用域令牌，"
                       "或改用 /api/agent/propose 提交待批准提案。")

    def log(self, tool: str, decision: str, detail: str = "", preview: str = "") -> None:
        agentm.audit(self.conn, self.label, tool, decision, detail, preview)

    def need_quota(self, tool: str) -> None:
        if agentm.quota_left(self.conn, tool) <= 0:
            agentm.audit(self.conn, self.label, tool, "denied", "超出每日配额")
            raise HTTPException(status_code=429, detail=f"工具 {tool} 已达当日配额上限")


async def require_agent(request: Request):
    """智能体接口鉴权：作用域令牌 + 全局开关。"""
    raw = request.headers.get(AGENT_TOKEN_HEADER) or ""
    conn = dbm.connect()
    ok, info, scope = agentm.check_token(conn, raw)
    if not ok:
        agentm.audit(conn, "", "auth", "denied", info, request.url.path)
        conn.commit()
        conn.close()
        raise HTTPException(status_code=401, detail=f"智能体令牌校验失败：{info}")
    try:
        yield AgentCtx(conn, info, scope)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/agent/ping")
def agent_ping(user=Depends(require_agent)):
    user.log("ping", "allowed")
    can_write = user.can_write
    allowed = ["病情询问", "联网搜索", "病情问答", "月经询问与预测"]
    allowed += (["直接读写业务数据（POST /api/agent/op，无需人工批准）"] if can_write
                else ["提交待批准提案（POST /api/agent/propose）"])
    return {"ok": True, "label": user.label, "scope": user.scope,
            "作用域说明": agentm.SCOPE_CN[user.scope], "quotas": agentm.quota_view(user.conn),
            "允许": allowed,
            "禁止": ["执行命令", "账号与会话管理", "令牌管理", "读取模型端点密钥",
                    "备份的恢复与删除", "白名单外网络"]}


@app.get("/api/agent/context")
def agent_context(user=Depends(require_agent)):
    user.need_quota("context")
    pid = dbm.ensure_profile(user.conn)
    user.log("context", "allowed")
    return agentm.context(user.conn, pid)


@app.post("/api/agent/query")
async def agent_query(request: Request, user=Depends(require_agent)):
    user.need_quota("records_query")
    body = await request.json()
    kind = str(body.get("kind", ""))
    payload = body.get("payload") or {}
    pid = dbm.ensure_profile(user.conn)
    try:
        data = agentm.records_query(user.conn, pid, kind, payload)
    except ValueError as e:
        user.log("records_query", "denied", f"{kind}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    user.log("records_query", "allowed", kind, json.dumps(payload, ensure_ascii=False)[:200])
    return data


@app.post("/api/agent/kb/search")
async def agent_kb_search(request: Request, user=Depends(require_agent)):
    user.need_quota("kb_search")
    body = await request.json()
    q = str(body.get("q", ""))[:80]
    hits = kbm.search(q, top=6)
    user.log("kb_search", "allowed", q)
    return {"关键词": q, "结果": hits}


@app.post("/api/agent/guard")
async def agent_guard(request: Request, user=Depends(require_agent)):
    """联网前的守门人：隐私围栏 + 域名白名单 + 配额。策略在服务端，不可被提示词绕过。"""
    body = await request.json()
    kind = str(body.get("kind", "search"))
    if kind == "search":
        user.need_quota("web_search")
        ok, text, reason = agentm.privacy_check(user.conn, str(body.get("text", "")))
        user.log("web_search", "allowed" if ok else "denied", reason, text[:120])
        if not ok:
            return JSONResponse(status_code=403, content={"allowed": False, "reason": reason})
        return {"allowed": True, "text": text, "left": agentm.quota_left(user.conn, "web_search")}
    if kind == "fetch":
        user.need_quota("web_fetch")
        url = str(body.get("url", ""))[:500]
        ok, reason = agentm.url_allowed(url)
        user.log("web_fetch", "allowed" if ok else "denied", reason, url[:120])
        if not ok:
            return JSONResponse(status_code=403, content={"allowed": False, "reason": reason})
        return {"allowed": True, "url": url, "left": agentm.quota_left(user.conn, "web_fetch")}
    raise HTTPException(status_code=400, detail="kind 只能是 search 或 fetch")


@app.get("/api/agent/ops")
def agent_ops_list(user=Depends(require_agent)):
    """这个令牌能用的操作清单（按作用域过滤），供外部智能体自述能力。"""
    user.log("ops", "allowed", user.scope)
    return agent_opsm.catalog(user.scope)


@app.post("/api/agent/op")
async def agent_op(request: Request, user=Depends(require_agent)):
    """**直接**执行一个数据操作（增删改查），不需要人工批准。

    只有 scope=write 的令牌能用写入类操作；只读类操作任何令牌都能用。
    支持 dry_run=1：照常校验并执行，然后回滚——先验证再真写。
    `GET /api/agent/ops` 有完整清单。
    """
    body = await request.json()
    op = str(body.get("op", ""))
    args = body.get("args") or {}
    dry_run = bool(body.get("dry_run"))
    need = agent_opsm.OPS.get(op, {}).get("need", "read")
    if need == "write":
        user.need_write(op)
        user.need_quota("op")
    pid = dbm.ensure_profile(user.conn)
    try:
        out = agent_opsm.run(user.conn, pid, op, args, dry_run=dry_run)
    except agent_opsm.OpError as e:
        # 同 need_write：这次请求会被回滚，所以审计要单独提交
        user.log_durable(op, "denied", str(e), json.dumps(args, ensure_ascii=False)[:200])
        raise HTTPException(status_code=400, detail=str(e))
    if dry_run:
        # 校验与执行都跑过了，把事务丢掉——dry_run 不该改变任何东西
        user.conn.rollback()
        user.log("op", "allowed", f"{op} (dry_run)")
        return out
    user.log("op", "allowed", op, json.dumps(out, ensure_ascii=False)[:200])
    return out


@app.post("/api/agent/propose")
async def agent_propose(request: Request, user=Depends(require_agent)):
    user.need_quota("propose")
    body = await request.json()
    kind = str(body.get("kind", ""))
    payload = body.get("payload") or {}
    rationale = str(body.get("rationale", ""))[:400]
    try:
        new_id = agentm.create_proposal(user.conn, kind, payload, rationale)
    except ValueError as e:
        user.log("propose", "denied", f"{kind}: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    user.log("propose", "allowed", f"{kind} -> #{new_id}",
             json.dumps(payload, ensure_ascii=False)[:200])
    return {"ok": True, "proposal_id": new_id, "status": "pending",
            "note": "已提交为待批准提案，需档案主人在 /admin/agent 批准后才会写入数据库。"}


@app.get("/api/agent/proposals")
def agent_proposals(user=Depends(require_agent)):
    user.log("proposals", "allowed")
    return {"待批准": agentm.list_proposals(user.conn, "pending", limit=20)}


@app.post("/api/agent/audit")
async def agent_audit(request: Request, user=Depends(require_agent)):
    """智能体自报工具调用（用于记录联网检索等发生在服务端的动作）。"""
    body = await request.json()
    agentm.audit(user.conn, user.label, str(body.get("tool", "unknown"))[:40],
                 str(body.get("decision", "allowed"))[:20], str(body.get("detail", "")),
                 str(body.get("preview", ""))[:200])
    return {"ok": True}


# ------------------------------------------------------------------ 问答记录与附件清理

def _prune_ctx(conn) -> dict:
    """给管理页的清理卡片：设置 + 当前占用（数据库、上传目录）。"""
    cfg = prunem.settings(conn)
    try:
        db_size = Path(dbm.DB_PATH).stat().st_size
    except OSError:
        db_size = 0
    up = Path(dbm.DB_PATH).parent / "uploads"
    files = [f for f in up.iterdir() if f.is_file()] if up.is_dir() else []
    up_size = sum(f.stat().st_size for f in files)
    n_qa = conn.execute("SELECT COUNT(*) FROM qa_messages").fetchone()[0]
    n_att = conn.execute("SELECT COUNT(*) FROM attachments WHERE ref_kind='qa'").fetchone()[0]
    cfg["usage"] = (f"数据库 {db_size / 1024 / 1024:.1f}MB（问答 {n_qa} 条）；"
                    f"上传 {len(files)} 个文件 {up_size / 1024:.0f}KB"
                    f"（其中问答附件 {n_att} 个）")
    return cfg


@app.post("/admin/prune/settings")
def admin_prune_settings(request: Request, user=Depends(authm.require_login),
                         csrf: str = Form(""), enabled: str = Form(""),
                         qa_keep_days: str = Form("30"), uploads_keep_days: str = Form("30")):
    """保存清理设置（保留期）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)

    def num(v: str, default: int) -> int:
        try:
            return max(1, min(3650, int(v)))
        except (TypeError, ValueError):
            return default

    prunem.set_settings(user.conn, enabled == "1",
                        num(qa_keep_days, 30), num(uploads_keep_days, 30))
    user.audit("prune_settings",
               f"enabled={enabled == '1'} qa={qa_keep_days} uploads={uploads_keep_days}",
               client_ip(request))
    return RedirectResponse(url="/admin?msg=清理设置已保存", status_code=303)


@app.post("/admin/prune/now")
def admin_prune_now(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """立即按保留期清理一次（force：即使开关关着也照做，是显式点击）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    stat = prunem.run(user.conn, force=True)
    user.audit("prune_now", prunem.describe(stat), client_ip(request))
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote('清理完成：' + prunem.describe(stat))}",
                            status_code=303)


# ------------------------------------------------------------------ 智能体管理台（需管理员登录）

@app.get("/admin/agent", response_class=HTMLResponse)
def admin_agent(request: Request, user=Depends(authm.require_login), msg: str = "", err: str = ""):
    user.require_admin()
    conn = user.conn
    pending = agentm.list_proposals(conn, "pending", limit=30)
    recent = agentm.list_proposals(conn, "all", limit=30)
    return render(request, user, "admin_agent.html", active="admin", title="智能体权限",
                  st=agentm.stats(conn), tokens=agentm.active_token_info(conn),
                  pending=pending, recent=recent, audit=agentm.audit_view(conn, 60),
                  kinds=agentm.PROPOSAL_KINDS, scopes=agentm.SCOPES,
                  scope_cn=agentm.SCOPE_CN, ops=agent_opsm.OPS,
                  ops_denied=agent_opsm.NOT_SUPPORTED, msg=msg, err=err)


# ------------------------------------------------------------------ 数据同步（网页可配目标仓库）

def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _sync_settings(conn) -> dict:
    return sync_job.settings(conn)


@app.post("/admin/sync/config")
def admin_sync_config(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                      repo: str = Form(""), branch: str = Form("main"),
                      enabled: str = Form(""), snapshot: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    repo = repo.strip()
    if repo and not (repo.startswith("git@") or repo.startswith("https://") or repo.startswith("ssh://")
                     or repo.startswith("file://")):
        return RedirectResponse(url="/admin?err=仓库地址格式应为 git@host:path 或 https://…",
                                status_code=303)
    conn = user.conn
    dbm.set_setting(conn, "kb_repo", repo)
    dbm.set_setting(conn, "kb_branch", branch.strip() or "main")
    dbm.set_setting(conn, "kb_sync_enabled", "1" if enabled == "1" else "0")
    dbm.set_setting(conn, "kb_snapshot_db", "1" if snapshot == "1" else "0")
    user.audit("sync_config", f"repo={repo} branch={branch} enabled={enabled}", client_ip(request))
    return RedirectResponse(url="/admin?msg=同步设置已保存", status_code=303)


@app.post("/admin/sync/run")
def admin_sync_run(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """立即同步：导出数据库内容到数据仓库 → 提交 → 推送（与「有改动就触发」共用同一实现）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    if not sync_job.settings(conn)["repo"]:
        return RedirectResponse(url="/admin?err=请先填写要同步的 GitHub 仓库地址", status_code=303)
    summary, _n = sync_job.run_sync(conn)
    user.audit("sync_run", summary[:300], client_ip(request))
    key = "err" if "失败" in summary else "msg"
    return RedirectResponse(url=f"/admin?{key}={urllib.parse.quote('同步：' + summary)}",
                            status_code=303)


# ------------------------------------------------------------------ 数据备份与恢复（仅管理员）

@app.post("/admin/backup/now")
def admin_backup_now(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                     with_uploads: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    info = backupm.create_backup(user.conn, with_uploads=(with_uploads == "1"), tag="manual")
    user.audit("backup_now", f"{info['name']} {info['size']}B", client_ip(request))
    msg = f"已备份 {info['name']}（{info['size'] / 1024:.0f}KB，sha256 {info['sha256'][:12]}）"
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg)}", status_code=303)


@app.post("/admin/backup/settings")
def admin_backup_settings(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                          enabled: str = Form(""), interval: str = Form("daily"),
                          keep: str = Form("7"), with_uploads: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    conn = user.conn
    dbm.set_setting(conn, "backup_enabled", "1" if enabled == "1" else "0")
    dbm.set_setting(conn, "backup_interval", interval if interval in ("daily", "weekly") else "daily")
    try:
        n = max(1, min(60, int(keep)))
    except ValueError:
        n = 7
    dbm.set_setting(conn, "backup_keep", str(n))
    dbm.set_setting(conn, "backup_uploads", "1" if with_uploads == "1" else "0")
    user.audit("backup_settings", f"{interval} keep={n} uploads={bool(with_uploads)}",
               client_ip(request))
    return RedirectResponse(url="/admin?msg=备份设置已保存", status_code=303)


@app.get("/admin/backup/download/{name}")
def admin_backup_download(name: str, user=Depends(authm.require_login)):
    """下载某个备份文件（仅管理员；文件名必须出现在备份列表里，防目录穿越）。"""
    user.require_admin()
    names = {b["name"] for b in backupm.list_backups()}
    if name not in names:
        raise HTTPException(404, "备份文件不存在")
    p = backupm.BACKUP_DIR / name
    from fastapi.responses import FileResponse
    return FileResponse(p, media_type="application/octet-stream", filename=name)


@app.post("/admin/backup/restore")
def admin_backup_restore(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                         name: str = Form(""), confirm: str = Form("")):
    """从服务器上的某个备份恢复（恢复前会自动另存当前现场）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    if confirm != "yes":
        return RedirectResponse(url="/admin?err=恢复前请勾选确认", status_code=303)
    names = {b["name"] for b in backupm.list_backups()}
    if name not in names:
        return RedirectResponse(url="/admin?err=备份文件不存在", status_code=303)
    try:
        msg = backupm.restore_from_file(user.conn, backupm.BACKUP_DIR / name)
    except Exception as e:  # noqa: BLE001
        user.audit("backup_restore_failed", f"{name}: {e}", client_ip(request))
        return RedirectResponse(url=f"/admin?err={urllib.parse.quote('恢复失败：' + str(e)[:150])}",
                                status_code=303)
    user.conn.commit()
    user.audit("backup_restore", msg, client_ip(request))
    restarted = backupm.restart_service(os.environ.get("BG_APP_SLUG", ""))
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg + '；' + restarted)}",
                            status_code=303)


@app.post("/admin/backup/restore-upload")
def admin_backup_restore_upload(request: Request, user=Depends(authm.require_login),
                                csrf: str = Form(""), confirm: str = Form(""),
                                file: UploadFile = File(...)):
    """上传一个备份文件（.db 或 .tar.gz）并恢复。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    if confirm != "yes":
        return RedirectResponse(url="/admin?err=恢复前请勾选确认", status_code=303)
    raw = file.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        return RedirectResponse(url="/admin?err=备份文件超过 12MB 限制", status_code=303)
    name = (file.filename or "uploaded.db")
    if not (name.endswith(".db") or name.endswith(".tar.gz")):
        return RedirectResponse(url="/admin?err=只接受 .db 或 .tar.gz 备份文件", status_code=303)
    backupm.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    staged = backupm.BACKUP_DIR / f"uploaded-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{Path(name).name}"
    staged.write_bytes(raw)
    os.chmod(staged, 0o600)
    try:
        msg = backupm.restore_from_file(user.conn, staged)
    except Exception as e:  # noqa: BLE001
        staged.unlink(missing_ok=True)
        user.audit("backup_restore_failed", f"upload {name}: {e}", client_ip(request))
        return RedirectResponse(url=f"/admin?err={urllib.parse.quote('恢复失败：' + str(e)[:150])}",
                                status_code=303)
    user.conn.commit()
    user.audit("backup_restore_upload", msg, client_ip(request))
    restarted = backupm.restart_service(os.environ.get("BG_APP_SLUG", ""))
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg + '；' + restarted)}",
                            status_code=303)


@app.post("/admin/sync/cred")
def admin_sync_cred(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                    mode: str = Form("auto")):
    """选择同步凭据来源：auto=用系统自带（默认）/ key=部署密钥 / token=访问令牌。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    mode = mode if mode in ("auto", "key", "token") else "auto"
    dbm.set_setting(user.conn, "sync_cred_mode", mode)
    user.audit("sync_cred_mode", mode, client_ip(request))
    label = {"auto": "用系统自带凭据（默认）", "key": "用部署密钥", "token": "用访问令牌"}[mode]
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote('同步凭据已设为：' + label)}",
                            status_code=303)


@app.post("/admin/sync/keygen")
def admin_sync_keygen(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """生成部署密钥（私钥只落盘 600，公钥展示给用户去 GitHub 添加）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    created, info = sync_job.keygen()
    if created and info and info.startswith("ssh-"):
        sync_job.env_set("SYNC_SSH_KEY", str(sync_job.KEY_PATH))   # 显式记录到项目 .env
    user.audit("sync_keygen", "created" if created else "exists", client_ip(request))
    if not info or "失败" in info or "error" in info.lower():
        return RedirectResponse(url=f"/admin?err=生成失败：{urllib.parse.quote(info[:150])}",
                                status_code=303)
    msg = ("已生成部署密钥，请复制下面的公钥到 GitHub → 仓库 Settings → Deploy keys（勾 write access）："
           if created else "部署密钥已存在，公钥如下：")
    return RedirectResponse(url=f"/admin?msg={urllib.parse.quote(msg)}&pubkey={urllib.parse.quote(info)}",
                            status_code=303)


@app.post("/admin/sync/token")
def admin_sync_token(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                     token: str = Form(""), action: str = Form("save")):
    """保存/清除访问令牌（只用于 https 形式仓库；不回显、不入库）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    if action == "clear":
        sync_job.save_token("")
    else:
        sync_job.save_token(token.strip())
    user.audit("sync_token", "saved" if token.strip() and action != "clear" else "cleared",
               client_ip(request))
    return RedirectResponse(
        url="/admin?msg=" + urllib.parse.quote(
            "令牌已保存（仅 https 仓库生效）" if action != "clear" else "令牌已清除"),
        status_code=303)


@app.post("/admin/sync/test")
def admin_sync_test(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """测试仓库可达性与凭据是否有效。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    ok, out = sync_job.test_access(user.conn)
    user.audit("sync_test", f"ok={ok}", client_ip(request))
    key = "msg" if ok else "err"
    head = "仓库可达" if ok else "仓库不可达"
    return RedirectResponse(
        url=f"/admin?{key}={urllib.parse.quote(head + '：' + out[:300])}", status_code=303)


@app.post("/admin/kb/import")
def admin_kb_import(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """从数据仓库的 Markdown 导入/覆盖数据库中的知识库文档。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    n_p, n_g = kbm.import_all(user.conn)
    user.audit("kb_import", f"数据仓库 {n_p} 篇 / 通用知识 {n_g} 篇", client_ip(request))
    return RedirectResponse(url=f"/admin?msg=已导入：数据仓库 {n_p} 篇、通用知识 {n_g} 篇",
                            status_code=303)


@app.post("/admin/kb/export")
def admin_kb_export(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """把数据库中的知识库文档写回数据仓库（供 git 同步与人工阅读）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    n = kbm.export_to_dir(user.conn, kbm.DOCS_DIR)
    user.audit("kb_export", f"{n} 篇", client_ip(request))
    return RedirectResponse(url=f"/admin?msg=已把 {n} 篇文档写回仓库（{kbm.DOCS_DIR}）",
                            status_code=303)


@app.post("/admin/agent/toggle")
def admin_agent_toggle(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    turn_on = not agentm.agent_enabled(user.conn)
    agentm.set_setting(user.conn, "agent_enabled", "1" if turn_on else "0")
    user.audit("agent_toggle", f"enabled={turn_on}", client_ip(request))
    return RedirectResponse(url=f"/admin/agent?msg=智能体已{'启用' if turn_on else '停用'}",
                            status_code=303)


@app.post("/admin/agent/token")
def admin_agent_token(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                      label: str = Form("pi agent"), scope: str = Form("read")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    scope = scope if scope in agentm.SCOPES else "read"
    raw = agentm.create_token(user.conn, label.strip() or "pi agent", user.id, scope=scope)
    user.audit("agent_token_create", f"{label} scope={scope}", client_ip(request))
    msg = (f"已生成新令牌（作用域 {scope}，仅本次显示）"
           + ("——**这个令牌能直接改数据**，泄露等于把病历交出去，请妥善保管"
              if scope == "write" else ""))
    return RedirectResponse(
        url=f"/admin/agent?msg={urllib.parse.quote(msg)}&token={raw}", status_code=303)


@app.post("/admin/agent/token/revoke")
def admin_agent_revoke(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    agentm.revoke_all(user.conn)
    user.audit("agent_token_revoke_all", "", client_ip(request))
    return RedirectResponse(url="/admin/agent?msg=已吊销全部令牌", status_code=303)


@app.post("/admin/agent/proposal/{pid}/decide")
def admin_agent_decide(pid: int, request: Request, user=Depends(authm.require_login),
                       csrf: str = Form(""), action: str = Form("approve")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    ok, msg, applied = agentm.decide(user.conn, pid, user.id, approve=(action == "approve"))
    if ok and applied.get("id"):
        n = _link_recent_qa_attachments(user.conn, applied["kind"], applied["id"])
        if n:
            msg += f"；已把最近 {n} 张问答上传的图片挂到该记录"
    user.audit("agent_proposal_decide", f"#{pid} {action} -> {msg}", client_ip(request))
    key = "msg" if ok else "err"
    return RedirectResponse(url=f"/admin/agent?{key}={msg}", status_code=303)


# ------------------------------------------------------------------ 模型接入配置（需管理员）

@app.get("/admin/llm", response_class=HTMLResponse)
def admin_llm(request: Request, user=Depends(authm.require_login)):
    user.require_admin()
    cfg = llmm.config()
    cfg["public_endpoint"] = not llmm.is_private_host(
        urllib.parse.urlparse(cfg["base_url"]).hostname or "")
    models, models_at = llmm.cached_models()
    # 页面加载只读缓存，不发网络请求；实时连通性由「测试并刷新」按钮触发
    return render(request, user, "admin_llm.html", active="admin", title="模型接入配置",
                  cfg=cfg, models=models, models_at=models_at, key_path=str(llmm.key_file()))


@app.post("/admin/llm/save")
def admin_llm_save(request: Request, user=Depends(authm.require_login), csrf: str = Form(""),
                   base_url: str = Form(...), model: str = Form(...),
                   api_key: str = Form(""), allow_public: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    url = base_url.strip().rstrip("/")
    mdl = model.strip()
    allow = allow_public == "1"
    ok, why = llmm.check_endpoint(url, allow_public=allow)
    if not ok:
        return RedirectResponse(url=f"/admin/llm?err={urllib.parse.quote(why)}", status_code=303)
    if not mdl:
        return RedirectResponse(url="/admin/llm?err=模型名不能为空", status_code=303)
    key_changed = bool(api_key.strip())
    if key_changed:
        llmm.save_key(api_key.strip())
    conn = user.conn
    for k, v in (("llm_base_url", url), ("llm_model", mdl),
                 ("llm_allow_public", "1" if allow else "0"),
                 ("llm_updated_at", dbm.now())):
        dbm.set_setting(conn, k, v)
    # 审计只记录端点与模型，永不记录密钥内容
    user.audit("llm_config_update", f"base={url} model={mdl} key_changed={key_changed}",
               client_ip(request))
    msg = f"已保存：{mdl} @ {url}" + ("（密钥已更新）" if key_changed else "")
    return RedirectResponse(url=f"/admin/llm?msg={urllib.parse.quote(msg)}", status_code=303)


@app.post("/admin/llm/test")
def admin_llm_test(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    """连通性测试 + 刷新可选模型列表（这是本页唯一会发网络请求的动作）。"""
    user.require_admin()
    csrf_ok(request, user, csrf)
    info = llmm.health(timeout=10)
    conn = user.conn
    stamp = dbm.now()
    dbm.set_setting(conn, "llm_last_check",
                    f"{stamp} → {'可用' if info['ok'] else '失败：' + (info.get('error') or '')[:120]}")
    if info["ok"] and info.get("all_models"):
        dbm.set_setting(conn, "llm_model_list",
                        json.dumps(info["all_models"], ensure_ascii=False))
        dbm.set_setting(conn, "llm_model_list_at", stamp)
    user.audit("llm_test", f"ok={info['ok']} models={info.get('model_count', 0)}", client_ip(request))
    if info["ok"]:
        msg = f"连通正常，拉取到 {info.get('model_count', 0)} 个模型" \
              + (f"（{', '.join(info.get('models', [])[:3])}…）" if info.get("models") else "")
    else:
        msg = f"连接失败：{info.get('error', '')}"
    return RedirectResponse(url=f"/admin/llm?msg={urllib.parse.quote(msg[:200])}", status_code=303)


@app.post("/admin/llm/key/clear")
def admin_llm_key_clear(request: Request, user=Depends(authm.require_login), csrf: str = Form("")):
    user.require_admin()
    csrf_ok(request, user, csrf)
    llmm.clear_key()
    user.audit("llm_key_clear", "", client_ip(request))
    return RedirectResponse(url="/admin/llm?msg=已清除 API Key", status_code=303)


# ------------------------------------------------------------------ 扩展（里）
# 「表」之外的功能（情侣空间 / AI 跑团 = 里）以扩展的形式叠在这里跑，
# 代码在另一个仓库，不在本仓库：见 app/extensions.py 与 README「叠加里空间」。
EXT = extm.load(app, render, {
    "csrf_ok": csrf_ok, "client_ip": client_ip, "guard": guard,
    "audit": _audit, "ping": PING,
})
extm.apply_template_dirs(templates.env, [APP_DIR / "templates"])
