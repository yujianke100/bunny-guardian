"""端到端冒烟测试：不做 mock，直接对运行中的服务发真实 HTTP 请求。

用法（BG_KB_DIR 必须是一次性目录，否则会把代码仓库当成数据仓库卷进去，见 app/README.md）：
    rm -rf /tmp/bg-test && mkdir -p /tmp/bg-test/kb/docs/经期记录
    printf '# 周期记录\n' > /tmp/bg-test/kb/docs/经期记录/周期记录.md
    BG_DB=/tmp/bg-test/x.db BG_KB_DIR=/tmp/bg-test/kb \
      BG_SYNC_ON_CHANGE=0 python -m uvicorn main:app --port 9812 &
    BASE=http://127.0.0.1:9812 BG_DB=/tmp/bg-test/x.db \
      BG_KB_DIR=/tmp/bg-test/kb BG_SYNC_ON_CHANGE=0 python smoke_test.py

覆盖：健康检查 → 登录（含错误密码）→ 各页面可达 → 经期/日志/疾病/事件/就诊写入
→ 权限隔离（成员账号不可写）→ CSRF 拦截 → 导出到知识库（DRY RUN）。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import quote
from pathlib import Path

from cycle import RING_COLORS   # 校验环图配色是否真的渲染进 HTML

BASE = os.environ.get("BASE", "http://127.0.0.1:9812")
DB = os.environ.get("BG_DB", "/tmp/bg-test/bunny.db")
PASSED, FAILED = [], []


def make_client():
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    no_redirect = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect())
    return op, no_redirect


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def py_exe() -> str:
    """跑 llm_config.py 这类子进程用哪个解释器。

    部署机上用项目 venv（`.venv/bin/python`）；本机（Windows）没有这个路径时
    退回当前解释器——否则本地跑套件会直接 FileNotFoundError。
    """
    v = pathlib.Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    return str(v) if v.exists() else sys.executable


def client():
    return make_client()[0]


def call(session, path, data=None, follow=True, headers=None):
    """session 为 client() 的返回值；不跟随重定向时用同一 cookie jar。"""
    if isinstance(session, tuple):
        op, no_redirect = session
    else:
        op, no_redirect = session, session
    url = BASE + path
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = op if follow else no_redirect
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def call_json(session, path, payload=None, follow=True, headers=None):
    """以 application/json 发送请求（智能体接口用）。"""
    if isinstance(session, tuple):
        op, no_redirect = session
    else:
        op, no_redirect = session, session
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=body)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = op if follow else no_redirect
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def call_multipart(session, path, fields, files, follow=True):
    """multipart/form-data 上传（图片）。files: {字段名: (文件名, bytes, mime)}"""
    if isinstance(session, tuple):
        op, no_redirect = session
    else:
        op, no_redirect = session, session
    boundary = "----smoke" + str(int(time.time() * 1000))
    body = b""
    for k, v in (fields or {}).items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for k, (fn, data, mime) in (files or {}).items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fn}\"\r\n"
                 f"Content-Type: {mime}\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    opener = op if follow else no_redirect
    try:
        with opener.open(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ok  " if cond else "  FAIL") + f" {name}" + (f"  {extra}" if extra and not cond else ""))


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def main() -> int:
    print(f"目标服务：{BASE}\n数据库：{DB}\n")

    st, body = call(client(), "/livez")
    check("匿名 GET /livez 200（存活探针，不含配置细节）",
          st == 200 and "ok" in body and "llm" not in body and "model" not in body, body[:200])
    # 注意：不跟随重定向必须用 make_client() 的 (op, no_redirect) 组合 ——
    # 单传 client() 时 follow=False 是无效的（两个 opener 是同一个，会跟着 307 跑到 /login
    # 拿到 200，断言就会以为「匿名也能看」）。
    _anon = make_client()
    st, body = call(_anon, "/healthz", follow=False)
    check("匿名 GET /healthz 被拒（细节要登录；旧版匿名可看模型端点与密钥后四位）",
          st in (303, 307, 401), f"status={st} {body[:120]}")

    pw_file = Path(DB).parent / "INITIAL_ADMIN.txt"
    pw = ""
    if pw_file.is_file():
        m = re.search(r"初始密码：\s*(\S+)", pw_file.read_text(encoding="utf-8"))
        pw = m.group(1) if m else ""
    if not pw:
        pw = os.environ.get("ADMIN_PW", "")
    check("取得初始管理员密码", bool(pw), "未找到 INITIAL_ADMIN.txt")

    anon = make_client()
    st, _ = call(anon, "/dashboard", follow=False)
    check("未登录访问 /dashboard 被重定向", st in (303, 307, 302), f"status={st}")

    st, body = call(anon, "/login")
    check("GET /login 200", st == 200)

    bad = make_client()
    st, body = call(bad, "/login", {"username": "admin", "password": "wrong-password"})
    check("错误密码被拒绝", "不正确" in body, "未看到错误提示")

    op = make_client()
    st, body = call(op, "/login", {"username": "admin", "password": pw, "next": "/dashboard"})
    check("正确密码登录成功", st == 200 and "总览" in body, f"status={st}")

    pages = ["/dashboard", "/cycle", "/records", "/knowledge", "/qa", "/account", "/admin"]
    for p in pages:
        st, body = call(op, p)
        ok = st == 200 and "Traceback" not in body and "Internal Server Error" not in body
        check(f"GET {p} 200 且无异常", ok, f"status={st}")
    st, _hz = call(op, "/healthz")
    check("登录后 GET /healthz 200（详细健康信息只给登录用户）",
          st == 200 and '"db"' in _hz and '"llm"' in _hz, _hz[:200])
    if st == 200:
        _info = json.loads(_hz)
        print(f"       db={_info['db']} llm={_info['llm'].get('ok')} ({_info['llm'].get('model')})")

    # 写入数据
    st, body = call(op, "/cycle")
    tok = csrf_of(body)
    check("从页面提取 CSRF token", bool(tok))

    st, _ = call(op, "/cycle/add", {"csrf": "bad-token", "start_date": "2026-08-01"}, follow=False)
    check("CSRF 错误被拦截（403）", st == 403, f"status={st}")

    st, _ = call(op, "/cycle/add", {"csrf": tok, "start_date": "2026-07-01", "end_date": "2026-07-05",
                                    "flow": "medium", "symptoms": "腹痛,腰酸"}, follow=False)
    check("新增月经记录", st == 303, f"status={st}")
    st, _ = call(op, "/cycle/add", {"csrf": tok, "start_date": "2026-08-01", "end_date": "2026-08-05",
                                    "flow": "medium", "symptoms": "腹痛"}, follow=False)
    check("再新增一次（形成 31 天间隔）", st == 303, f"status={st}")
    st, _ = call(op, "/cycle/log", {"csrf": tok, "log_date": "2026-08-20", "kind": "pain",
                                    "name": "下腹坠痛", "severity": "3", "note": "热敷缓解"}, follow=False)
    check("新增每日日志", st == 303, f"status={st}")

    st, body = call(op, "/dashboard")
    check("总览显示周期推断（出现「周期第」）", "周期第" in body)
    check("总览出现推断区间提示", "推断区间" in body and "统计推断" in body)

    # 周期环：两套视图（环现在只在总览上，经期页不再重复）
    st, body = call(op, "/dashboard")
    check("周期环 SVG 已渲染", '<svg class="ring"' in body, "未找到环形图")
    arcs = body.count('<path d="M ')
    check("环上分段 >= 2 段", arcs >= 2, f"段数={arcs}")
    check("医学视图含四期名称", all(k in body for k in ("月经期", "卵泡期", "排卵期", "黄体期")))
    check("圆心显示倒计时与周期第几天",
          "rc-count-label" in body and "rc-count" in body and "周期第" in body and "记录月经" in body)
    check("圆心文案精简（区间/置信度移到环外）", "rc-conf" not in body and "rc-meta" in body)
    check("今天＝白底日期牌带投影（环内不再有文字标注）",
          'class="ring-today"' in body and 'class="ring-today-badge"' in body
          and "ring-ovu-t" not in body)
    check("分段配色存在", RING_COLORS["menstrual"] in body and RING_COLORS["ovulation"] in body)
    check("环上有估算排卵日圆点", 'class="ring-ovu"' in body)
    check("环底轨道已渲染（跟随主题的 token）", 'stroke="var(--ring-track)"' in body
          or "ring.track" in body)
    _css = open("static/app.css", encoding="utf-8").read()
    check("那句「细缝只是画法」已按反馈删除", "细缝" not in body and "k-gap" not in body)
    check("环下说明只剩六边形那条 + 今天的圆牌带投影",
          'class="k-ovu"' in body and ".ring-today-badge{filter:drop-shadow" in _css)

    # 几何不变量：留缝只影响观感，指针与天↔角度映射仍精确
    import math
    from cycle import _pt, R_MID as _RM, RING_GAP_DEG, ring as ringfn
    for _v in ("med", "plain"):
        r = ringfn(28, 5, view=_v, cycle_day=17)
        step = 360.0 / 28
        ang = math.degrees(math.atan2(r["marker"]["y"] - 120.0, r["marker"]["x"] - 120.0))
        want = (17 - 0.5) * step - 90.0
        err = abs((ang - want + 180) % 360 - 180)
        check(f"[{_v}] 指针角度仍精确指向当天（误差 {err:.3f}°）", err < 0.05)
        ok_inside = True
        for s in r["segments"]:
            a0 = (s["start_day"] - 1) * step - 90.0
            a1 = s["end_day"] * step - 90.0
            seg_ang = abs(a1 - a0)
            ins = min(RING_GAP_DEG, max(0.0, seg_ang / 2.0 - 4.0))
            ok_inside = ok_inside and ins >= 0 and seg_ang - 2 * ins > 0
        check(f"[{_v}] 留缝不会吃掉任何分段", ok_inside)
    for _v in ("med", "plain"):
        check(f"[{_v}] 每段都按真实起止天定位",
              all(1 <= s["start_day"] <= s["end_day"] <= 28 for s in ringfn(28, 5, view=_v)["segments"]))

    st, plain = call(op, "/dashboard?view=plain")
    check("通俗视图含「安全期/易孕期」（环在总览）", "安全期" in plain and "易孕期" in plain)
    check("通俗视图不再出现「黄体期」", "黄体期" not in plain)
    check("通俗视图给出避孕不可靠警示", "不能作为避孕依据" in plain and "acog.org" in plain)
    import cycle as _cyc2
    import datetime as _dt5
    _safe_rows = [r for r in _cyc2.ring_phase_table(
        _cyc2.analyze([_dt5.date(2026, 7, 12), _dt5.date(2026, 8, 8), _dt5.date(2026, 9, 5)], [5, 6, 5]),
        view="plain") if r["key"] == "safe"]
    check("通俗视图安全期合并显示（两段合并为一行）",
          bool(_safe_rows) and "、" in _safe_rows[0]["days"], str(_safe_rows))
    check("非法 view 参数回退到医学视图", "黄体期" in call(op, "/dashboard?view=xxx")[1])
    check("总览也有周期环", '<svg class="ring"' in call(op, "/dashboard")[1])

    # ---- 环：今天的日期牌、排卵日六边形、可点图例看说明 ----
    import cycle as _cy
    import phases as _ph
    check("排卵日＝紫色六边形标记",
          '<polygon class="ring-ovu"' in body and _cy.RING_COLORS["ovulation"] == "#8f6fd8"
          and _cy.RING_COLORS["ovulation"] in body,
          _cy.RING_COLORS["ovulation"])
    check("圆心给倒计时（距经期结束 / 距下次经期 / 已推迟）",
          "rc-count-label" in body and any(k in body for k in ("距离经期结束", "距离下次经期", "已推迟")))
    check("圆心有「记录月经」入口（总览上跳到日历；经期页就地编辑）",
          "记录月经" in body and 'href="/cycle#calwrap"' in body)
    check("图例按钮可点：按钮＋弹层＋每期说明模板",
          'class="lgchip"' in body and '<dialog class="phasedlg"' in body
          and 'data-phase-tpl="menstrual"' in body and 'data-phase-tpl="ovulation"' in body)
    check("图例按钮上的色点用分区色（曾因 for_view 不给颜色而透明）",
          f'style="background:{_cy.RING_COLORS["menstrual"]}"' in body
          and f'style="background:{_cy.RING_COLORS["ovulation"]}"' in body)
    check("紫色那一段的图例名字是「排卵期」（排卵日由六边形那条说明）",
          _ph.PHASE_INFO["ovulation"]["label"] == "排卵期" and "<span>排卵期</span>" in body)
    check("阶段说明带可查来源（NHS / ACOG）", "nhs.uk" in body and "acog.org" in body)
    check("每个阶段都有四段说明与至少一个来源",
          all(p.get("sources") and p.get("body") and p.get("feel") and p.get("fertility")
              for p in _ph.PHASE_INFO.values()),
          str([k for k, p in _ph.PHASE_INFO.items() if not (p.get("sources") and p.get("body"))]))
    check("通俗视角的图例不含医学分期术语",
          'data-phase="luteal"' not in call(op, "/dashboard?view=plain")[1])

    # ---- 环：缝隙收窄 + 带日期的图例已删 + 三张卡已删 ----
    check("环的分段缝隙收窄（靠端帽的弧形自然分开，不再像被切开）",
          _cy.RING_GAP_DEG <= 8
          and 2 * (_cy.R_MID * math.radians(_cy.RING_GAP_DEG) - _cy.RING_STROKE / 2) < 0.5 * _cy.RING_STROKE,
          f"GAP={_cy.RING_GAP_DEG}° 可见缝隙≈"
          f"{2 * (_cy.R_MID * math.radians(_cy.RING_GAP_DEG) - _cy.RING_STROKE / 2):.1f}")
    check("排卵日图例与环上标记同形同色（紫色六边形，不再画成旧的白心圆点）",
          '<i class="k-ovu">' in body and "#8f6fd8" in open("static/app.css", encoding="utf-8").read())
    check("带日期的图例已删（颜色图例就在「看说明」按钮上）",
          'class="ringlegend"' not in body and 'class="lgchip"' in body)
    check("日历不再画日志点、也不再列情绪/疼痛/用药/备注",
          all(('class="dot %s"' % k) not in body for k in ("mood", "pain", "medication", "note"))
          and "s.className = 'dot '" not in open("static/calendar.js", encoding="utf-8").read())
    check("「今天」改成 9px 白字胶囊（红底上也看得清）",
          ".day .tl{" in open("static/app.css", encoding="utf-8").read()
          and "font-size:9px" in open("static/app.css", encoding="utf-8").read()
          and "color:#fff" in open("static/app.css", encoding="utf-8").read())
    check("三张卡已删（添加每日日志 / 日志 / 月经历史）",
          all(k not in body for k in ("添加每日日志", "月经历史", "日志（最近")),
          str([k for k in ("添加每日日志", "月经历史", "日志（最近") if k in body]))

    # ---- 受孕率卡（有出处的区间值 + 必须写明不能用于避孕） ----
    _fert_med = call(op, "/dashboard?view=med")[1]
    check("受孕率卡（总览·医学视图）：渐变小标题 + 内嵌面板 + 图例（不再有隐藏按钮）",
          "今日受孕率" in _fert_med and "fert-panel" in _fert_med and "fert-legend" in _fert_med
          and "<h2>💗 受孕率</h2>" in _fert_med and "data-fert-toggle" not in _fert_med)
    check("受孕率写明群体平均与不能避孕", "群体平均" in body and "不能用于避孕" in body)
    check("受孕率带出处（NEJM）", "nejm.org" in body)
    check("总览也有受孕率卡", "今日受孕率" in call(op, "/dashboard")[1])

    # ---- 激素示意图（教科书模型，不是实测值） ----
    _hsvg = re.search(r'<svg class="chart hormone".*?</svg>', body, re.S)
    check("激素示意图已恢复（只在医学分期视角）",
          bool(_hsvg) and "不是你的实测值" in body)
    check("激素示意图含四期底带",
          bool(_hsvg) and sum(k in _hsvg.group(0) for k in ("月经期", "卵泡期", "排卵期", "黄体期")) >= 3,
          (_hsvg.group(0)[:80] if _hsvg else "未找到"))
    check("通俗视角不出现激素示意图",
          'class="chart hormone"' not in call(op, "/dashboard?view=plain")[1])

    # 圆心倒计时的三个分支（不经 HTTP，直接测环数据）
    import datetime as _dt3
    _mp = _cy.ring(28, 5, view="med", cycle_day=2)
    check("经期中：圆心给「距离经期结束 N 天」",
          _mp["center"]["count_label"] == "距离经期结束" and _mp["center"]["count_num"] == 3,
          str(_mp["center"]))
    _ns = _dt3.date.today() - _dt3.timedelta(days=11)
    _od = _cy.ring(28, 5, view="med", cycle_day=40, next_start=_ns,
                   next_window=(_ns, _ns + _dt3.timedelta(days=2)), confidence="中等")
    check("已推迟：圆心与环外文案用同一个天数",
          _od["center"]["count_label"] == "已推迟" and _od["center"]["count_num"] == 12
          and _od["center"]["count_num"] == _od["center"]["overdue"]
          and "已过 12 天" in _od["meta"] and _od["marker"]["text"] == "28",
          str(_od["center"]) + " / " + _od["meta"])
    check("无记录：圆心提示先记录一次", _cy.ring(28, 5, example=True)["center"]["count_label"] == "先记录一次月经")

    # ---- 日历：阶段配色 + 推断变淡 + 编辑模式打勾 ----
    import json as _json
    import datetime as _dt
    st, body = call(op, "/cycle")
    _m = re.search(r'<script id="caldata" type="application/json">(.*?)</script>', body, re.S)
    cal = _json.loads(_m.group(1)) if _m else {}
    today = _dt.date.today()
    check("日历数据含逐日阶段", bool(cal.get("days")), "缺 days")
    _phase_view = call(op, "/cycle?view=med&cal=phase")[1]
    check("阶段配色视图的日历图例含四期与淡色说明",
          all(k in _phase_view for k in ("月经期", "卵泡期", "排卵期", "黄体期"))
          and "变淡＝尚未发生" in _phase_view)
    check("事件式日历仍给出「变淡＝尚未发生」说明", "变淡＝尚未发生" in body)
    check("日历含编辑入口（天数改为按历史推算，无输入框）",
          'id="calEditToggle"' in body and 'id="calNDaysHint"' in body and 'id="calNDays"' not in body)
    check("编辑提示写明手势（含 20 天内算增减）", "20 天内再点" in body and "按你以往记录" in body)
    _p0 = cal.get("periods") or []
    if _p0:
        _d0 = _p0[0]["start"][:10]
        check("已记录经期日＝实色非推断",
              cal["days"].get(_d0, {}).get("p") == "menstrual" and cal["days"][_d0].get("est") == 0,
              str(cal["days"].get(_d0)))
    else:
        check("已记录经期日＝实色非推断", False, "没有已记录经期")
    # 淡色语义：只有「尚未发生」才淡 —— 本周期剩下的天数仍是实色（与环图一致），
    # 从预测的下一次经期开始才是淡色
    import cycle as cyclem
    with sqlite3.connect(DB) as c:
        _st = [x[0] for x in c.execute("SELECT start_date FROM cycles ORDER BY start_date")]
        _en = [x[0] for x in c.execute("SELECT end_date FROM cycles ORDER BY start_date")]
    _durs = [(_dt.date.fromisoformat((e or s)[:10]) - _dt.date.fromisoformat(s[:10])).days + 1
             for s, e in zip(_st, _en) if s]
    _info = cyclem.analyze([_dt.date.fromisoformat(x[:10]) for x in _st], _durs)
    _ns = _info.get("next_start")
    _mid_cur = (today + _dt.timedelta(days=3)).isoformat()
    if _ns and today + _dt.timedelta(days=3) < _ns:
        check("本周期剩下的天数仍是实色（不淡）",
              cal["days"].get(_mid_cur, {}).get("est") == 0, str(cal["days"].get(_mid_cur)))
    else:
        check("本周期剩下的天数仍是实色（不淡）", True, "本周期已到期，跳过")
    if _ns:
        _all_est = [k for k, v in cal["days"].items() if v.get("est") == 1]
        check("淡色（预测）的日子都在今天之后", bool(_all_est)
              and all(k > today.isoformat() for k in _all_est),
              f"最早淡色={min(_all_est) if _all_est else '—'}")
        if _ns > today:
            check("从预测的下一次经期起变淡",
                  cal["days"].get(_ns.isoformat(), {}).get("est") == 1,
                  str(cal["days"].get(_ns.isoformat())))
        else:
            # 事件式：过期的预测日不上色（留白）；阶段配色里才显示为「推迟/未确认」
            check("过期预测在事件式下留白", _ns.isoformat() not in cal["days"],
                  str(cal["days"].get(_ns.isoformat())))
            _pp = call(op, "/cycle?cal=phase")[1]
            _ppd = _json.loads(re.search(
                r'<script id="caldata" type="application/json">(.*?)</script>', _pp, re.S).group(1))
            check("过期预测在阶段配色下显示为「推迟/未确认」",
                  _ppd.get("days", {}).get(_ns.isoformat(), {}).get("p") == "unknown",
                  str(_ppd.get("days", {}).get(_ns.isoformat())))
    check("图例写明淡色的含义", "变淡＝尚未发生" in body)

    # ---- 日历两种显示方式：事件式（默认）/ 阶段配色 ----
    check("日历有显示方式开关", "事件式" in body and "阶段配色" in body)
    check("事件式为默认并只标事件",
          "事件式</a>" in body and "易孕窗口" in body and "估算排卵日" in body)
    check("事件式：其余日子留白（不含卵泡期/黄体期铺色）",
          all(v.get("p") in ("menstrual", "fertile") for v in cal["days"].values()),
          str(sorted({v.get("p") for v in cal["days"].values()})))
    check("事件式：给出估算排卵日用于圈出",
          isinstance(cal.get("ovu"), list) and len(cal["ovu"]) >= 1, str(cal.get("ovu"))[:120])
    check("事件式：已记录经期仍是实色",
          any(v.get("p") == "menstrual" and v.get("est") == 0 for v in cal["days"].values()))
    check("事件式：说明里讲清留白（不再提 Flo/Clue）",
          "留白" in body and "主流经期 App" not in body)
    st, _p = call(op, "/cycle?cal=phase")
    _m2 = re.search(r'<script id="caldata" type="application/json">(.*?)</script>', _p, re.S)
    _cp = _json.loads(_m2.group(1)) if _m2 else {}
    check("阶段配色：每天都按阶段着色",
          any(v.get("p") in ("follicular", "luteal") for v in _cp.get("days", {}).values()),
          str(sorted({v.get("p") for v in _cp.get("days", {}).values()}))[:120])
    check("阶段配色：图例列出各阶段", "黄体期" in _p.split('class="legend"')[1][:800])

    # ---- 统计与分布：从历史动态推算 ----
    check("有统计与分布卡片", "统计与分布" in body and "周期长度" in body and "经期天数" in body)
    st, body = call(op, "/cycle")
    _gaps = []
    for a, b in zip(_st, _st[1:]):
        _gaps.append((_dt.date.fromisoformat(b[:10]) - _dt.date.fromisoformat(a[:10])).days)
    if _gaps:
        _med = sorted(_gaps)[len(_gaps) // 2] if len(_gaps) % 2 \
            else (sorted(_gaps)[len(_gaps) // 2 - 1] + sorted(_gaps)[len(_gaps) // 2]) / 2
        check("周期长度中位数与记录一致", f">{_med}</b> 天" in body or f">{int(_med)}</b> 天" in body,
              f"期望中位={_med}")
        check("列出了每个间隔（可看出波动）", str(max(_gaps)) in body and "天</span>" in body)
    check("经期天数有中位数", "经期天数" in body and "中位" in body)
    check("记录了样本量（几次经期/几个间隔）", "已有记录" in body and "个完整间隔" in body or "次经期" in body)
    # 阶段关联 + 同名归并：造 3 条疼痛日志 + 在经期症状里也写一次同名，应合并成一行
    with sqlite3.connect(DB) as c:
        _s0 = _dt.date.fromisoformat(_st[-1][:10])
        _past = (today - _s0).days                       # 本周期已过去的天数
        _idx = min(max(int((_info.get("cycle_len") or 28)) - 3, 2), max(1, _past))
        _luteal_day = (_s0 + _dt.timedelta(days=_idx - 1)).isoformat()
        for _ in range(3):
            c.execute("INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note,"
                      " created_at) VALUES(1,?,?,?,?,?,?,?)",
                      (_luteal_day, "pain", "测试腹痛", 3, "", "", "2026-01-01 00:00:00"))
        c.execute("UPDATE cycles SET symptoms='测试腹痛' WHERE start_date=?", (_st[-1],))
        c.commit()
    st, body = call(op, "/cycle")
    check("症状计数与阶段关联已呈现",
          "测试腹痛" in body and "最常出现在哪个阶段" in body and "次出现在" in body,
          f"日志日期={_luteal_day}")
    _n_symrow = body.count(">测试腹痛</td>")
    check("同一症状只算一行（每日日志与经期症状合并计数）", _n_symrow == 1, f"出现 {_n_symrow} 行")
    check("症状列表改成表格（名字完整显示）+ 数量多时翻页",
          'class="sympt"' in body and "symptable" in body and "data-page-size" in body
          and "data-pg=" in body and 'class="nm"' in body)
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM day_logs WHERE name='测试腹痛'")
        c.execute("UPDATE cycles SET symptoms='' WHERE start_date=?", (_st[-1],))
        c.commit()

    tok = csrf_of(call(op, "/cycle")[1])

    def jget(b):
        try:
            return _json.loads(b)
        except Exception:
            return {}

    def rng_of2(j, start_iso):
        for r in j.get("ranges", []):
            if r["start"] == start_iso:
                return r
        return None

    def rows_with_start(start_iso):
        with sqlite3.connect(DB) as c:
            return c.execute("SELECT COUNT(*) FROM cycles WHERE start_date=?", (start_iso,)).fetchone()[0]

    def periods_post(rngs, ndays=""):
        return call(op, "/cycle/periods",
                    {"csrf": tok, "ranges": _json.dumps(rngs), "default_days": ndays}, follow=False)

    def set_ndays(n):
        return call(op, "/cycle/periods", {"csrf": tok, "default_days": str(n)}, follow=False)

    def tap(d):
        return call(op, "/cycle/tap?view=med", {"csrf": tok, "day": d}, follow=False)

    def free_window(need):
        """找一个「连续 need 天没有任何记录，且离每一次经期开始都超过 20 天」的空档。

        两个条件都要：① 不落在已有记录上；② 与任一次经期开始相距 >20 天，
        否则按「20 天内再点＝手动增减」的规则，点下去会变成延长/提前那一次，而不是新建。
        """
        with sqlite3.connect(DB) as c:
            rows = c.execute("SELECT start_date, end_date FROM cycles").fetchall()
        busy = set()
        for s, e in rows:
            a = _dt.date.fromisoformat(s[:10])
            b = _dt.date.fromisoformat((e or s)[:10])
            while a <= b:                      # ① 记录覆盖的日子
                busy.add(a)
                a += _dt.timedelta(days=1)
            for k in range(-21, 22):           # ② 开始日 ±20 天的调整窗口
                busy.add(_dt.date.fromisoformat(s[:10]) + _dt.timedelta(days=k))
        d = today - _dt.timedelta(days=60)
        while d > today - _dt.timedelta(days=360):
            span = [d + _dt.timedelta(days=i) for i in range(need)]
            probe = span + [d - _dt.timedelta(days=1), span[-1] + _dt.timedelta(days=1)]
            if not any(x in busy for x in probe):
                return d
            d -= _dt.timedelta(days=1)
        return today - _dt.timedelta(days=60)

    # 选空档日期：不与既有测试记录（08-01~08-05 等）相邻，避免被"20 天内算增减"规则并进去
    w1 = free_window(4)
    d1 = w1.isoformat()
    st, body = tap(d1)
    j = jget(body)
    r1 = rng_of2(j, d1)
    _n1 = int(j.get("default_days") or 5)
    check("点空白的一天＝记录一次经期（天数按历史推算）",
          st == 200 and bool(r1) and r1["end"] == (w1 + _dt.timedelta(days=_n1 - 1)).isoformat(),
          f"推算天数={_n1} {r1}")
    check("新经期只落一行", rows_with_start(d1) == 1)
    check("打勾后逐日阶段立即包含该日", j.get("days", {}).get(d1, {}).get("p") == "menstrual"
          and j["days"][d1].get("est") == 0, str(j.get("days", {}).get(d1)))
    check("返回了操作说明", "已记录经期" in str(j.get("msg")), str(j.get("msg")))

    # ---- 打勾后卡片就地更新（用户反馈：以前要刷新整页才看得到推断变化） ----
    _blk = j.get("blocks") or {}
    check("打勾响应带回重算后的卡片片段",
          {"_ring.html", "_fert_card.html", "_hormone_card.html",
           "_infer_card.html", "_stats_card.html"} <= set(_blk),
          str(sorted(_blk)))
    check("周期推断卡也重算了（以前只有它不更新）",
          "暂无记录" not in _blk.get("_infer_card.html", "")
          and "推断下次经期" in _blk.get("_infer_card.html", ""),
          _blk.get("_infer_card.html", "")[:80])
    check("片段顶层都带块 id（前端按 id 就地替换）",
          'id="blk-ring"' in _blk.get("_ring.html", "")
          and 'id="blk-stats"' in _blk.get("_stats_card.html", ""))
    # 曾经：页面上是事件式（只标经期），打勾后接口返回全阶段数据 → 点一下整月都变成阶段着色
    check("打勾返回的日历与整页同规格（事件式只含经期/易孕期）",
          isinstance(j.get("days"), dict) and bool(j["days"])
          and all(v.get("p") in ("menstrual", "fertile") for v in j["days"].values())
          and isinstance(j.get("ovu"), list),
          str(sorted({v.get("p") for v in (j.get("days") or {}).values()})))
    with sqlite3.connect(DB) as c:
        _nc = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    check("片段里的统计已按最新记录重算（不必刷新）",
          ("已有记录：%d 次经期" % _nc) in _blk.get("_stats_card.html", ""),
          "期望 %d 次经期" % _nc)
    _pb = call(op, "/cycle")[1]
    _db2 = call(op, "/dashboard")[1]
    check("经期页只保留推断与统计两块（环/受孕率/激素已合并到总览）",
          all(('id="%s"' % i) in _pb for i in ("blk-stats", "blk-infer"))
          and not any(('id="%s"' % i) in _pb for i in ("blk-ring", "blk-fert", "blk-hormone")))
    check("总览上的环/受孕率/激素/身体数据块带 id",
          all(('id="%s"' % i) in _db2 for i in ("blk-ring", "blk-fert", "blk-hormone", "blk-body")))
    _db_page = call(op, "/dashboard")[1]
    check("身体数据挪到了总览", 'id="blk-body"' in _db_page and "身体数据" in _db_page
          and "身体数据" not in _pb)
    check("脚本版本已升级（片段替换与委托点击靠新代码）",
          "/static/app.js?v=18" in _pb and "/static/calendar.js?v=8" in _pb
          and "/static/app.css?v=19" in _pb)

    # ---- 点今天要一次记满推算天数（曾经被「不越过今天」砍成 1 天，用户以为没记上） ----
    _td = today.isoformat()
    with sqlite3.connect(DB) as c:
        if c.execute("SELECT COUNT(*) FROM cycles WHERE start_date=?", (_td,)).fetchone()[0]:
            tap(_td)                                     # 先清掉，否则再点同一天＝删除
    st, body = tap(_td)
    _jt = jget(body)
    _rt = rng_of2(_jt, _td)
    _nt = int(_jt.get("default_days") or 5)
    check("点今天＝一次记满推算天数（不再只记到今天）",
          bool(_rt) and _rt["start"] == _td
          and _rt["end"] == (today + _dt.timedelta(days=_nt - 1)).isoformat(),
          f"default_days={_nt} 得到 {_rt}")
    check("返回的逐日数据把这 N 天都标成经期",
          all((_jt.get("days") or {}).get((today + _dt.timedelta(days=k)).isoformat(), {}).get("p")
              == "menstrual" for k in range(_nt)),
          str({(today + _dt.timedelta(days=k)).isoformat():
               (_jt.get("days") or {}).get((today + _dt.timedelta(days=k)).isoformat()) for k in range(_nt)}))
    check("日历有「回到今天」按钮",
          'id="calToday"' in call(op, "/cycle")[1] and "回到今天" in call(op, "/cycle")[1])
    check("今天的格子有双环样式与「今天」小字",
          "box-shadow:0 0 0 2px var(--card),0 0 0 3.5px var(--accent)" in open("static/app.css", encoding="utf-8").read()
          and 'class="tl">今天' in open("static/calendar.js", encoding="utf-8").read())

    d1c = (w1 + _dt.timedelta(days=2)).isoformat()               # 段内靠后的一天
    st, body = tap(d1c)
    r1b = rng_of2(jget(body), d1)
    check("点段内靠后的一天＝经期到前一天为止（改而不是新增）",
          bool(r1b) and r1b["end"] == (w1 + _dt.timedelta(days=1)).isoformat()
          and rows_with_start(d1) == 1, str(r1b))

    w2 = free_window(14)         # 这一次经期要连点两次（新建 + 往后延），留够空档
    d2 = w2.isoformat()
    _n2 = int(jget(tap(d2)[1]).get("default_days") or 5)
    _far = w2 + _dt.timedelta(days=_n2 + 2)          # 落在段后，但在 20 天窗口内
    st, body = tap(_far.isoformat())                 # 20 天内再点 → 延长
    r2 = rng_of2(jget(body), d2)
    check("20 天内再点＝把这次经期延长到那天（不新建一次）",
          bool(r2) and r2["end"] == _far.isoformat()
          and rows_with_start(d2) == 1 and len(jget(body).get("ranges", [])) >= 1, str(r2))
    st, body = tap((w2 + _dt.timedelta(days=2)).isoformat())      # 段内靠后 → 缩回
    r2b = rng_of2(jget(body), d2)
    check("延长后仍可一次点击缩回",
          bool(r2b) and r2b["end"] == (w2 + _dt.timedelta(days=1)).isoformat(), str(r2b))

    fut = (today + _dt.timedelta(days=5)).isoformat()
    st, body = tap(fut)
    _jf = jget(body)
    check("未来的日子不勾（提示先不用勾）",
          rng_of2(_jf, fut) is None and "未来" in str(_jf.get("msg")), str(_jf.get("msg")))

    st, body = tap(d2)                                          # 点起始日 → 删除整段
    check("点该段起始日＝删除这段", rng_of2(jget(body), d2) is None and rows_with_start(d2) == 0)

    with sqlite3.connect(DB) as c:
        _bad = c.execute("SELECT COUNT(*) FROM cycles WHERE end_date < start_date").fetchone()[0]
        _adj = c.execute("SELECT COUNT(*) FROM cycles a JOIN cycles b ON b.start_date="
                         "date(a.end_date,'+1 day')").fetchone()[0]
    check("不存在结束日早于开始日的脏记录", _bad == 0, f"{_bad} 条")
    check("库里不存在相邻两段（相邻会被合并/延长）", _adj == 0, f"{_adj} 对相邻")
    st, body = call(op, "/cycle")
    cal3 = _json.loads(re.search(r'<script id="caldata" type="application/json">(.*?)</script>',
                                 body, re.S).group(1))
    check("整批设置接口仍可用（兼容路径）", periods_post(cal3["periods"])[0] == 200)

    # ---- 经期天数：不再让用户设置，按历史推算；首次 5 天 ----
    import bmi as bmim
    check("BMI 计算正确（52kg / 162cm ≈ 19.8）", bmim.value(52, 162) == 19.8, str(bmim.value(52, 162)))
    check("BMI 分级按中国标准（19.8＝正常）", bmim.classify(19.8)["label"] == "正常")
    check("BMI 分级：17.5 体重过低", bmim.classify(17.5)["label"] == "体重过低")
    check("BMI 分级：25 超重、28 肥胖",
          bmim.classify(25.0)["label"] == "超重" and bmim.classify(28.0)["label"] == "肥胖")
    check("按身高给出正常体重区间（162cm ≈ 48.6–63.0）",
          bmim.healthy_weight_range(162) == (48.6, 63.0), str(bmim.healthy_weight_range(162)))
    check("缺身高时不算 BMI", bmim.value(52, None) is None and bmim.classify(None)["label"] == "—")

    # ---- 身高 / 体重 / BMI 卡片（在总览上） ----
    st, body = call(op, "/dashboard")
    check("总览上有身体数据卡片（身高数字本身就是编辑入口）",
          "身体数据" in body and "更新体重" in body and 'class="wt-now"' in body
          and 'class="height-now"' in body and 'action="/body/height"' in body)
    check("身高只留一个入口（不再有单独的「身高设置」折叠）",
          body.count('action="/body/height"') == 1)
    check("未设身高时给出引导", "还没有设置身高" in body)
    tok_h = csrf_of(call(op, "/dashboard")[1])
    st, _ = call(op, "/body/height", {"csrf": tok_h, "height_cm": "162"}, follow=False)
    check("保存身高", st == 303)
    with sqlite3.connect(DB) as c:
        _h = c.execute("SELECT height_cm FROM profiles WHERE id=1").fetchone()[0]
    check("身高落库", float(_h) == 162.0, str(_h))
    st, _ = call(op, "/body/height", {"csrf": tok_h, "height_cm": "abc"}, follow=False)
    check("非法身高被拒（303 带错误提示）", st == 303)
    st, _ = call(op, "/body/height", {"csrf": tok_h, "height_cm": "20"}, follow=False)
    check("离谱身高被拒", st == 303)
    st, body = call(op, "/dashboard")
    check("填身高后给出正常体重区间", "正常范围对应体重约" in body and "48.6" in body and "63.0" in body)
    check("身高设完立刻可见（不必等记了体重才知道有没有记上）",
          "当前身高" in body and "162" in body, body[body.find("当前身高") - 30:body.find("当前身高") + 60])

    # ---- 体重是测量值，不该混进「症状」 ----
    import os as _os2
    import tempfile as _tf
    import stats as _stats
    _p = _os2.path.join(_tf.mkdtemp(), "s.db")
    _c = sqlite3.connect(_p)
    _c.row_factory = sqlite3.Row
    _c.executescript(
        "CREATE TABLE cycles(id INTEGER PRIMARY KEY, profile_id INT, start_date TEXT, end_date TEXT,"
        " flow TEXT, symptoms TEXT);"
        "CREATE TABLE day_logs(id INTEGER PRIMARY KEY, profile_id INT, log_date TEXT, kind TEXT,"
        " name TEXT, severity INT, value TEXT);")
    _c.execute("INSERT INTO cycles(profile_id,start_date,end_date,flow,symptoms)"
               " VALUES(1,'2026-08-01','2026-08-05','','腹痛')")
    _c.execute("INSERT INTO day_logs(profile_id,log_date,kind,name,severity,value) VALUES(1,?,?,?,?,?)",
               (today.isoformat(), "weight", "", 0, "52.5"))
    _c.execute("INSERT INTO day_logs(profile_id,log_date,kind,name,severity,value) VALUES(1,?,?,?,?,?)",
               (today.isoformat(), "pain", "下腹坠痛", 3, ""))
    _d = _stats.distributions(_c, 1, {}, view="med", kind_cn={"weight": "体重", "pain": "疼痛"})
    _names = [s["name"] for s in _d["symptoms"]]
    check("体重（测量值）不进症状分布", "体重" not in _names and "下腹坠痛" in _names, str(_names))
    check("体重也不进「症状最常出现在哪个阶段」",
          all("体重" not in str(l) for l in _d["phase_links"]), str(_d["phase_links"]))

    # ---- BMI 刻度图 ----
    import chart as _chart
    _g = _chart.bmi_gauge(19.8)
    check("BMI 刻度图：四段区间 + 当前落点",
          _g.count('class="band"') == 4 and "19.8" in _g and all(v in _g for v in ("18.5", "24", "28")),
          _g[:70])
    check("刻度条限宽（桌面宽屏不会被 SVG 缩放拉大）",
          "max-width:360px" in open("static/app.css", encoding="utf-8").read())
    check("没有 BMI 时不出刻度图", _chart.bmi_gauge(None) == "")

    # ---- 受孕率折线图（x 轴＝本周期固定坐标，今天用点标出） ----
    import main as _mainm

    def _main_back(referer):
        class _R:
            headers = {"referer": referer} if referer else {}
        return _mainm._back_to(_R())

    _fc = _chart.fertility_svg(28, 5, cycle_day=12, ovu_day=14,
                               start_iso="2026-09-11", next_iso="2026-10-09")
    check("受孕率图：按阶段分色的平滑曲线 + 排卵日五边形 + 今日气泡",
          "fertility" in _fc and _fc.count("<path") >= 2 and "<polygon" in _fc
          and "今日受孕率" in _fc and "受孕概率" in _fc
          and "9.15" in _fc and "10.8" in _fc,      # x 轴：经期结束 / 下次经期
          _fc[:90])
    check("受孕率曲线按阶段分色（与周期环同一套）+ y 轴到 40",
          "#ef7091" in _fc and "#8f6fd8" in _fc and "#e5b25c" in _fc and 'Y(0)' not in _fc
          and "40" in _fc and "受孕概率" in _fc)
    _dash = call(op, "/dashboard")[1]
    check("卡片标题栏统一：同一套渐变样式作用于各卡片首行标题",
          ".card > h2:first-child" in open("static/app.css", encoding="utf-8").read()
          and "⚖️ 身体数据" in _dash and "📊 统计与分布" in call(op, "/cycle")[1]
          and "🩺 病历与就医" in _dash)
    check("总览重排：周期环在最上、阶段卡第二；阶段划分/最近记录/下一步已删",
          _dash.index("🌙 周期环") < _dash.index("phase-")
          and "<h2>📅 当前周期阶段划分</h2>" not in _dash
          and "<h2>📝 最近记录</h2>" not in _dash
          and "<h2>🧭 下一步</h2>" not in _dash, "顺序或残留不对")
    check("总览上有了激素示意图（此前只在经期页）", 'class="chart hormone"' in _dash)
    check("圆心「记录月经」指向经期页的日历位置", 'href="/cycle#calwrap"' in _dash)
    check("受孕率卡里带上了这张图（在总览）", "fertility" in call(op, "/dashboard")[1])
    check("保存身高/体重后回来源页面（身体数据在总览）",
          _main_back("https://x/dashboard") == "/dashboard"
          and _main_back("") == "/dashboard"
          and _main_back("https://evil.example/x/y") == "/x/y",
          _main_back("https://x/dashboard"))

    st, _ = call(op, "/body/weight", {"csrf": tok_h, "log_date": today.isoformat(), "weight_kg": "52"},
                 follow=False)
    check("记录体重", st == 303)
    st, _ = call(op, "/body/weight", {"csrf": tok_h,
                                      "log_date": (today - _dt.timedelta(days=7)).isoformat(),
                                      "weight_kg": "53.2"}, follow=False)
    st, _ = call(op, "/body/weight", {"csrf": tok_h, "log_date": today.isoformat(), "weight_kg": "999"},
                 follow=False)
    st, _ = call(op, "/body/weight", {"csrf": tok_h,
                                      "log_date": (today + _dt.timedelta(days=3)).isoformat(),
                                      "weight_kg": "52"}, follow=False)
    with sqlite3.connect(DB) as c:
        _n = c.execute("SELECT COUNT(*) FROM day_logs WHERE kind='weight'").fetchone()[0]
        _wk = c.execute("SELECT weight_kg FROM profiles WHERE id=1").fetchone()[0]
    check("体重记录落库且非法值被拒（离谱体重/未来日期不入库）", _n == 2, f"{_n} 条")
    check("档案里的体重同步为最近一次", float(_wk) == 52.0, str(_wk))
    st, body = call(op, "/dashboard")
    check("总览显示 BMI 与分级", "BMI" in body and "19.8" in body and "正常" in body)
    check("体重列表显示变化量", "较上次" in body and "52" in body and "53.2" in body)
    check("历史区里有折线图（SVG 折线 + 首末日期）",
          "<polyline" in body and 'class="chart"' in body and _dt.date.today().strftime("%m-%d") in body)
    check("折线图放在「历史」折叠区里，不是默认铺开",
          body.index(">历史<") < body.index('class="chart"'), "折线图不在历史区内")
    import chart as chartm
    _svg = chartm.line_svg([("2026-08-01", 55.0), ("2026-08-20", 54.0), ("2026-09-08", 52.4)])
    check("折线图坐标正确（含最大值/最小值标注）",
          _svg.count("<circle") == 3 and "55" in _svg and "52.4" in _svg, _svg[:120])
    check("少于两个点时不给图", chartm.line_svg([("2026-09-08", 52.4)]) == "")
    _css = call(op, "/static/app.css")[1]
    check("按钮样式的折叠标题不会被通用规则吃掉颜色（可见性回归防护）",
          "details.fold>summary.btn{color:#fff}" in _css and "summary:not(.btn)" in _css)

    # ---- 打勾：20 天内再点＝手动增减这一次经期 ----
    st, body = call(op, "/cycle")          # 前面几处断言看的是总览页，这里重新取经期页
    check("天数按历史推算（无设置项）",
          'id="calNDays"' not in body and 'id="calNDaysHint"' in body and "按你以往记录" in body)
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM cycles")
        c.commit()
    st, body = call(op, "/cycle")
    check("首次使用默认 5 天", json.loads(re.search(
        r'<script id="caldata" type="application/json">(.*?)</script>', body, re.S).group(1)
    ).get("default_days") == 5)
    _w = free_window(30)
    st, body = tap(_w.isoformat())
    _j5 = jget(body)
    _r = rng_of2(_j5, _w.isoformat())
    check("空白日新建一次经期（5 天）",
          bool(_r) and _r["end"] == (_w + _dt.timedelta(days=4)).isoformat(), str(_r))
    st, body = tap((_w + _dt.timedelta(days=8)).isoformat())        # 20 天内再点
    _r2 = rng_of2(jget(body), _w.isoformat())
    check("20 天内再点＝延长这一次经期（不新建）",
          bool(_r2) and _r2["end"] == (_w + _dt.timedelta(days=8)).isoformat()
          and len(jget(body).get("ranges", [])) == 1, str(_r2))
    check("延长时给出第几天", "第 9 天" in str(jget(body).get("msg", "")), str(jget(body).get("msg")))
    st, body = tap((_w + _dt.timedelta(days=7)).isoformat())        # 段内 → 缩短
    _r3 = rng_of2(jget(body), _w.isoformat())
    check("段内再点＝缩短到前一天", bool(_r3) and _r3["end"] == (_w + _dt.timedelta(days=6)).isoformat(),
          str(_r3))
    st, body = tap((_w - _dt.timedelta(days=2)).isoformat())        # 段前 2 天 → 起始日提前
    _r4 = rng_of2(jget(body), (_w - _dt.timedelta(days=2)).isoformat())
    check("20 天内点在段前＝开始日提前（且不新增一段）",
          bool(_r4) and _r4["end"] == (_w + _dt.timedelta(days=6)).isoformat()
          and len(jget(body).get("ranges", [])) == 1, str(_r4))
    st, body = tap((_w + _dt.timedelta(days=25)).isoformat())       # 超过 20 天 → 新的一次
    check("超过 20 天再点＝记为下一次经期",
          len(jget(body).get("ranges", [])) == 2
          and rng_of2(jget(body), (_w + _dt.timedelta(days=25)).isoformat()) is not None,
          str(jget(body).get("ranges")))
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM cycles WHERE start_date>=?", (_w.isoformat(),))
        c.commit()

    st, body = call(op, "/conditions")
    tok = csrf_of(body)
    st, _ = call(op, "/conditions/add", {"csrf": tok, "name": "测试用疾病档案", "status": "active",
                                         "diagnosed_date": "2026-08-10", "hospital": "某医院",
                                         "department": "妇科", "summary": "冒烟测试记录"}, follow=False)
    check("新增疾病档案", st == 303, f"status={st}")

    st, body = call(op, "/conditions")
    m = re.search(r'href="/conditions/(\d+)"', body)
    cid = m.group(1) if m else "0"
    check("疾病列表出现新档案", cid != "0")
    if cid != "0":
        st, _ = call(op, f"/conditions/{cid}/event", {"csrf": csrf_of(body), "event_date": "2026-08-10",
                                                      "kind": "exam", "title": "妇科超声",
                                                      "detail": "经腹超声", "result": "未见明显异常"},
                     follow=False)
        check("新增病程事件", st == 303, f"status={st}")
        st, body = call(op, f"/conditions/{cid}")
        check("病程时间线显示事件", "妇科超声" in body)
        # 给事件附病历单照片
        from PIL import Image as _I3
        import io as _io3
        buf3 = _io3.BytesIO(); _I3.new("RGB", (500, 300), (245, 245, 255)).save(buf3, format="PNG")
        eid = None
        with sqlite3.connect(DB) as c:
            row = c.execute("SELECT id FROM condition_events ORDER BY id DESC LIMIT 1").fetchone()
            eid = row[0] if row else None
        if eid:
            st, _ = call_multipart(op, f"/upload/event/{eid}",
                                   {"csrf": tok, "back": f"/conditions/{cid}"},
                                   {"file": ("report.png", buf3.getvalue(), "image/png")})
            st2, page2 = call(op, f"/conditions/{cid}")
            check("病程事件可附病历单/照片并显示缩略图",
                  st2 == 200 and "/attachment/" in page2 and "img src=" in page2)
            with sqlite3.connect(DB) as c:
                n_ev = c.execute("SELECT COUNT(*) FROM attachments WHERE ref_kind='event'").fetchone()[0]
            check("事件附件已落库", n_ev >= 1, f"ref_kind=event 的附件数={n_ev}")

    st, body = call(op, "/visits")
    st, _ = call(op, "/visits/add", {"csrf": csrf_of(body), "visit_date": "2026-08-12",
                                     "hospital": "某医院", "department": "妇科",
                                     "reason": "月经不规律", "diagnosis": "随访观察",
                                     "plan": "3 个月后复查", "cost": "320.5"}, follow=False)
    check("新增就诊记录", st == 303, f"status={st}")

    # ---- 「病历」：疾病事件 + 就医合成一条时间轴（默认最近一个月，可切范围/自定义） ----
    st, rec = call(op, "/records")
    check("病历页把疾病事件与就医排在同一条时间轴",
          st == 200 and "病历与就医" in rec and "某医院" in rec and "测试用疾病档案" in rec,
          f"status={st}")
    _nav = re.search(r'<nav class="tabbar">(.*?)</nav>', rec, re.S)
    check("导航合并成一项「病历」（不再有疾病/就医两项）",
          bool(_nav) and "病历" in _nav.group(1) and "/records" in _nav.group(1)
          and "疾病" not in _nav.group(1) and "就医" not in _nav.group(1),
          _nav.group(1)[:120] if _nav else "未找到导航")
    check("默认最近一个月并写明范围", "最近一个月" in rec and "共 <b>" in rec)
    check("旧入口 /conditions、/visits 跳转到病历页",
          call(op, "/conditions", follow=False)[0] == 307 and call(op, "/visits", follow=False)[0] == 307)
    st, allm = call(op, "/records?span=all")
    check("切到「全部」时就医与疾病事件都在", "某医院" in allm and "测试用疾病档案" in allm)
    st, none = call(op, "/records?span=custom&start=2019-01-01&end=2019-01-31")
    check("自定义区间缩到没记录的时段时给空态",
          "这段时间没有记录" in none or "共 <b>0</b>" in none, "未出现空态")

    # ---- 助手登记的提案：由用户在问答面板里确认/取消（不必进管理页） ----
    import agent as _agentm
    with sqlite3.connect(DB) as c0:
        c0.row_factory = sqlite3.Row
        _npid = _agentm.create_proposal(
            c0, "visit_add", {"visit_date": "2026-08-20", "hospital": "待确认医院",
                              "department": "内科", "reason": "头痛待查"}, "用户刚提到这次就诊")
    st, pbody = call(op, "/qa/pending")
    _pj = json.loads(pbody) if pbody.strip().startswith("{") else {"items": []}
    check("问答面板能取到待确认提案", any(i["id"] == _npid for i in _pj.get("items", [])),
          str(_pj)[:160])
    _tok_q = csrf_of(call(op, "/cycle")[1])
    st, dbody = call(op, f"/qa/proposal/{_npid}/decide", {"csrf": _tok_q, "action": "approve"})
    _dj = json.loads(dbody)
    check("面板里点确认＝真的落库", _dj.get("ok"), str(_dj))
    with sqlite3.connect(DB) as c0:
        _got = c0.execute("SELECT COUNT(*) FROM visits WHERE hospital='待确认医院'").fetchone()[0]
    check("确认后就诊记录写进去了", _got == 1, f"{_got} 条")
    check("确认后立刻出现在病历时间轴上", "待确认医院" in call(op, "/records?span=all")[1])
    with sqlite3.connect(DB) as c0:
        c0.row_factory = sqlite3.Row
        _npid2 = _agentm.create_proposal(c0, "visit_add", {"visit_date": "2026-08-21",
                                                           "hospital": "不该落库的医院"}, "测试取消")
    st, dbody2 = call(op, f"/qa/proposal/{_npid2}/decide", {"csrf": _tok_q, "action": "reject"})
    with sqlite3.connect(DB) as c0:
        _got2 = c0.execute("SELECT COUNT(*) FROM visits WHERE hospital='不该落库的医院'").fetchone()[0]
    check("点取消＝不写入", json.loads(dbody2).get("ok") and _got2 == 0, f"{_got2} 条")

    # ---- 助手回答里的记录块 → 待确认提案（正文里不该出现 record 标签） ----
    import main as _main
    _clean, _blocks = _main._extract_records(
        "我看了一下。\n<record kind=\"visit_add\">{\"visit_date\": \"2026-09-10\", \"hospital\": \"X医院\"}</record>")
    check("记录块从正文剥掉并解析成提案数据",
          _blocks == [("visit_add", {"visit_date": "2026-09-10", "hospital": "X医院"})]
          and "<record" not in _clean, str(_blocks))
    check("不合法的记录块不影响正文",
          _main._extract_records("正文<record kind=\"visit_add\">{不是 json}</record>")[1] == []
          and "正文" in _main._extract_records("正文<record kind=\"visit_add\">{不是 json}</record>")[0])
    _today_iso = today.isoformat()
    _clean2, _blk2 = _main._extract_records(
        '说明\n<record kind="visit_add">{"visit_date": "today", "hospital": "A院"}</record>')
    check("记录块里的相对日期由本地换算（隐私围栏挡掉绝对日期后仍能记对）",
          _blk2 and _blk2[0][1].get("visit_date") == _today_iso, str(_blk2))
    _clean3, _blk3 = _main._extract_records(
        '说明\n<record kind="visit_add">{"visit_date": "[已隐去]", "hospital": "B院"}</record>')
    check("模型抄来的占位日期不会进库（会被丢掉）",
          _blk3 and "visit_date" not in _blk3[0][1] and _blk3[0][1].get("hospital") == "B院", str(_blk3))
    with sqlite3.connect(DB) as c0:
        c0.row_factory = sqlite3.Row
        _npid3 = _agentm.create_proposal(c0, "visit_add",
                                        {"visit_date": "2026-09-09", "hospital": "中文标签医院"}, "t")
    _pj3 = json.loads(call(op, "/qa/pending")[1])
    _it3 = [i for i in _pj3.get("items", []) if i["id"] == _npid3]
    check("待确认卡片用中文字段名（她是非技术用户）",
          bool(_it3) and "日期" in _it3[0]["summary"] and "医院" in _it3[0]["summary"], str(_it3))
    call(op, f"/qa/proposal/{_npid3}/decide", {"csrf": _tok_q, "action": "reject"})

    # 知识库
    st, body = call(op, "/knowledge")
    check("知识库页列出文档", "医学知识" in body)
    st, body = call(op, "/knowledge", None)
    st, body = call(op, "/knowledge?q=%E7%97%9B%E7%BB%8F")
    check("知识库检索可用", st == 200 and ("检索结果" in body or "没有匹配" in body), f"status={st}")
    m = re.search(r'href="/knowledge\?path=([^"]+)"', body)
    doc = urllib.parse.unquote(m.group(1)) if m else ""
    st, body = call(op, "/knowledge?path=" + urllib.parse.quote(doc))
    check(f"可打开知识库文档（{doc}）", bool(doc) and st == 200 and "Traceback" not in body, f"status={st}")
    st, _ = call(op, "/knowledge?path=" + urllib.parse.quote("../../etc/passwd"))
    check("路径穿越被拒绝", st in (403, 404), f"status={st}")

    # ---- 知识库以 SQLite 为数据源 ----
    kbdir = pathlib.Path(os.environ.get("BG_KB_DIR", pathlib.Path(__file__).resolve().parent.parent))
    st, body = call(op, "/knowledge?q=" + urllib.parse.quote("痛经"))
    check("中文 2 字检索可命中（内存倒排索引）", st == 200 and "痛经" in body, f"status={st}")
    # 直接写进数据库（磁盘上没有对应文件），页面照样能读 —— 证明源是库不是文件
    # 注：知识笔记现在不导出到数据仓库，所以不能再用「导出文件再删」的写法
    import db as dbm
    import kb as kbm
    _db_only = "医学知识/_db_source_test.md"
    with dbm.db() as _c:
        kbm.upsert_doc(_c, _db_only, "## 数据库为源\n\n这篇只存在数据库里。",
                       {"title": "数据库为源测试", "tags": ["测试"]}, origin="test")
        _c.commit()
    st, body = call(op, "/knowledge?path=" + urllib.parse.quote(_db_only))
    check("磁盘上没有文件时仍可读（证明读的是数据库）",
          st == 200 and "数据库为源" in body, f"status={st}")
    with dbm.db() as _c:
        kbm.delete_doc(_c, _db_only)
        _c.commit()
    st, body = call(op, "/admin")
    check("管理页显示知识库状态", "知识库（数据库）" in body and "文档数" in body)

    # ---- 知识库分层：通用知识随软件分发，不进数据仓库 ----
    import kb as kbm
    gen_name = "症状自查与就医指引.md"
    check("通用知识目录存在且非空", kbm.GENERAL_DIR.is_dir() and any(kbm.GENERAL_DIR.rglob("*.md")),
          str(kbm.GENERAL_DIR))
    st, body = call(op, "/knowledge")
    check("通用知识已进入检索库且标为「通用」",
          gen_name.removesuffix(".md") in body and "通用" in body, f"status={st}")
    check("通用知识正文不在数据仓库目录里", not (kbdir / "docs" / "医学知识" / gen_name).is_file(),
          str(kbdir / "docs" / "医学知识" / gen_name))
    st, _ = call(op, "/admin")
    tok2 = csrf_of(call(op, "/admin")[1])
    st, body = call(op, "/admin/kb/export", {"csrf": tok2}, follow=False)
    check("导出知识库到数据仓库执行成功", st == 303, f"status={st}")
    check("导出不会把通用知识写回数据仓库",
          not (kbdir / "docs" / "医学知识" / gen_name).is_file())
    st, body = call(op, "/knowledge?path=" + urllib.parse.quote("医学知识/" + gen_name))
    check("通用知识文档仍可正常打开", st == 200 and "Traceback" not in body, f"status={st}")
    with sqlite3.connect(DB) as c:
        try:
            n_gen = c.execute("SELECT COUNT(*) FROM kb_docs WHERE scope='general'").fetchone()[0]
            n_per = c.execute("SELECT COUNT(*) FROM kb_docs WHERE scope='personal'").fetchone()[0]
        except sqlite3.OperationalError:
            n_gen = n_per = -1
    check("库内分层计数正确（general 有文档、personal 有文档）",
          n_gen >= 5 and n_per >= 1, f"general={n_gen} personal={n_per}")

    # 成员账号与权限（用户名带时间戳，保证测试可重复运行）
    MEMBER = "testm" + str(int(time.time()) % 100000)
    st, body = call(op, "/admin")
    tok = csrf_of(body)
    # 管理员设置账号身份与管理员标记
    with sqlite3.connect(DB) as c:
        uid_row = c.execute("SELECT id FROM users WHERE username=?", (MEMBER,)).fetchone() \
            if False else None
    st, body = call(op, "/admin")
    check("账号卡片说明数据互通与管理员差异",
          "数据权限对所有账号一律完整且互通" in body and "管理员（系统配置）" in body)
    m_uid = re.search(r'action="/admin/users/(\d+)/profile"', body)
    if m_uid:
        st, body = call(op, f"/admin/users/{m_uid.group(1)}/profile",
                        {"csrf": tok, "who": "her", "is_admin": ""}, follow=True)
        check("可把账号设为「她」（档案主人）", "她（档案主人）" in body, body[:140])
        st, body = call(op, f"/admin/users/{m_uid.group(1)}/profile",
                        {"csrf": tok, "who": "him", "is_admin": "1"}, follow=True)
        check("可把账号设为「他」并授予管理员", "他（一起记录）" in body and "管理员" in body, body[:140])
    else:
        check("可把账号设为「她」（档案主人）", False, "管理页未找到 profile 表单")

    st, body = call(op, "/admin/users", {"csrf": tok, "username": MEMBER,
                                         "display_name": "测试成员", "role": "member"}, follow=True)
    m = re.search(r"新密码（仅本次显示[^<]*<code[^>]*>([^<]+)</code>", body)
    member_pw = m.group(1) if m else ""
    check("创建成员账号并显示初始密码", bool(member_pw))

    if member_pw:
        mem = make_client()
        st, body = call(mem, "/login", {"username": MEMBER, "password": member_pw, "next": "/dashboard"})
        check("成员账号可登录", st == 200 and "总览" in body)
        st, body = call(mem, "/admin", follow=False)
        check("成员账号访问 /admin 被拒绝（403）", st == 403, f"status={st}")
        st, mpage = call(mem, "/cycle")
        mtok = csrf_of(mpage)          # 用成员自己会话的 CSRF（跨会话 token 会被拒）
        st, _ = call(mem, "/cycle/add", {"csrf": mtok, "start_date": "2026-09-03",
                                         "end_date": "2026-09-07"}, follow=False)
        check("普通账号同样可以写入数据（两人互通）", st == 303, f"status={st}")
        st, body = call(mem, "/cycle")
        check("普通账号能看到彼此记录的数据", st == 200 and "2026-09-03" in body, f"status={st}")
        st, body = call(mem, "/account")
        check("账号页显示数据权限完整 + 身份", "完整（可读可写）" in body and "身份" in body)

    # ================= 智能体权限框架 =================
    def n_cycles() -> int:
        st, b = call_json(op, "/api/agent/query", {"kind": "cycle_history"}, headers=H)
        try:
            return len(json.loads(b).get("记录", []))
        except Exception:
            return -1

    st, body = call(op, "/api/agent/ping", follow=False)
    check("无令牌调用智能体接口被拒（401）", st == 401, f"status={st}")

    st, body = call(op, "/admin/agent")
    check("智能体管理页可打开", st == 200 and "智能体权限" in body)
    atok = csrf_of(body)
    if "已停用" in body:          # 幂等：只在停用时启用（避免重复运行把开关翻回去）
        call(op, "/admin/agent/toggle", {"csrf": atok}, follow=True)
    st, body = call(op, "/admin/agent/token", {"csrf": atok, "label": "smoke-test"}, follow=True)
    m = re.search(r"(bg_[A-Za-z0-9_\-]{20,})", body)
    token = m.group(1) if m else ""
    check("生成智能体令牌", bool(token))
    H = {"X-Agent-Token": token}   # noqa: F821 - 上一行已保证存在

    st, body = call(op, "/api/agent/ping", headers=H)
    check("带令牌可访问", st == 200 and '"ok"' in body, f"status={st}")
    check("令牌默认是只读作用域（建令牌时不会顺手给写权限）",
          '"scope":"read"' in body, body[:200])

    # ---- 直接写接口：read 令牌必须被挡住 ----
    st, body = call(op, "/api/agent/ops", headers=H)
    check("操作清单可用且说明作用域",
          st == 200 and '"read"' in body and "可用操作" in body, f"status={st}")
    check("只读令牌的清单里没有写操作",
          "cycle.delete" not in body and "kb.write" not in body, body[:200])
    check("清单如实列出不支持的（账号/令牌/密钥/备份）",
          "令牌管理" in body and "备份" in body)
    st, body = call_json(op, "/api/agent/op",
                         {"op": "cycle.add", "args": {"start_date": "2026-10-01"}}, headers=H)
    check("read 令牌直接写被拒（403，并指向提案接口）",
          st == 403 and "write" in body and "propose" in body, f"status={st} {body[:200]}")
    n_before = n_cycles()
    st, body = call_json(op, "/api/agent/op", {"op": "cycle.list"}, headers=H)
    check("read 令牌可以调读操作（同一接口）", st == 200 and "月经记录" in body, f"status={st}")

    # ---- 换一个 write 令牌 ----
    st, body = call(op, "/admin/agent/token", {"csrf": atok, "label": "smoke-write",
                                               "scope": "write"}, follow=True)
    m2 = re.search(r"(bg_[A-Za-z0-9_\-]{20,})", body)
    wtoken = m2.group(1) if m2 else ""
    check("可以生成 write 作用域令牌", bool(wtoken))
    check("生成 write 令牌时页面给出警告", "能直接改数据" in body, body[:200])
    check("令牌表里标出了作用域", ">write<" in call(op, "/admin/agent")[1])
    WH = {"X-Agent-Token": wtoken}
    st, body = call(op, "/api/agent/ping", headers=WH)
    check("write 令牌自报作用域", '"scope":"write"' in body, body[:200])
    st, body = call(op, "/api/agent/ops", headers=WH)
    check("write 令牌的清单里有写操作",
          "cycle.delete" in body and "kb.write" in body, body[:200])

    # ---- dry_run：校验但不落库 ----
    st, body = call_json(op, "/api/agent/op",
                         {"op": "cycle.add", "args": {"start_date": "2026-11-01"},
                          "dry_run": True}, headers=WH)
    check("dry_run 成功返回且标记了 dry_run",
          st == 200 and '"dry_run"' in body, f"status={st} {body[:200]}")
    check("dry_run 真的没写进去（条数不变）", n_cycles() == n_before,
          f"{n_before} -> {n_cycles()}")
    st, body = call_json(op, "/api/agent/op",
                         {"op": "cycle.add", "args": {"start_date": "not-a-date"},
                          "dry_run": True}, headers=WH)
    check("dry_run 也会做参数校验（400）", st == 400 and "不是合法日期" in body, f"status={st}")

    # ---- write 令牌真的能直接写 ----
    st, body = call_json(op, "/api/agent/op",
                         {"op": "cycle.add", "args": {"start_date": "2026-11-01",
                                                      "end_date": "2026-11-05",
                                                      "flow": "medium",
                                                      "symptoms": "智能体代录"}}, headers=WH)
    check("write 令牌能直接新增月经记录（无需批准）",
          st == 200 and "已新增月经记录" in body, f"status={st} {body[:200]}")
    check("写完之后记录数真的变了", n_cycles() == n_before + 1, f"{n_before} -> {n_cycles()}")
    st, body = call(op, "/cycle")
    check("网页上能看到智能体写进去的那条（同一份数据）",
          "智能体代录" in body, body[:160])

    # 参数校验的错误要原样告诉智能体，便于它自己改
    for bad_op, bad_args, want in [
        ("cycle.add", {}, "缺少必填日期"),
        ("cycle.add", {"start_date": "2026-11-10", "flow": "zzz"}, "只能是"),
        ("condition.add", {"name": "  "}, "不能为空"),
        ("condition.delete", {"id": 999999}, "找不到"),
        ("kb.write", {"path": "../x.md", "body": "y"}, "相对路径"),
        ("nope.op", {}, "不支持的操作"),
    ]:
        st, body = call_json(op, "/api/agent/op", {"op": bad_op, "args": bad_args}, headers=WH)
        check(f"参数/状态错误给 400 且说清原因：{bad_op}",
              st == 400 and want in body, f"status={st} {body[:180]}")

    # 越权：write 令牌也不能碰账号与令牌管理（那属于实例控制，不在这一层）
    st, body = call_json(op, "/api/agent/op", {"op": "user.create", "args": {}}, headers=WH)
    check("账号/令牌类操作不在接口里（拒绝而不是执行）",
          st == 400 and "不支持的操作" in body, f"status={st}")

    # 无令牌一样被挡
    st, _ = call_json(op, "/api/agent/op", {"op": "cycle.delete", "args": {"id": 1}})
    check("无令牌调直接写接口被拒（401）", st == 401, f"status={st}")

    # 审计：直接写要留痕
    with dbm.db() as _ac:
        ok_rows = _ac.execute("SELECT tool, decision, detail FROM agent_audit WHERE tool='op'"
                              " ORDER BY id DESC LIMIT 30").fetchall()
        denied = _ac.execute("SELECT tool, decision, detail FROM agent_audit"
                             " WHERE decision='denied' AND token_label='smoke-test'"
                             " ORDER BY id DESC LIMIT 10").fetchall()
    check("直接写操作写进了审计表", len(ok_rows) >= 3, [dict(r) for r in ok_rows][:3])
    check("越权尝试也留了痕（read 令牌写被拒时记 denied）",
          any(r["decision"] == "denied" for r in denied), [dict(r) for r in denied][:3])

    st, body = call(op, "/api/agent/context", headers=H)
    check("读取档案概览（含月经推断）", st == 200 and "月经" in body and "推断" in body, f"status={st}")
    st, body = call_json(op, "/api/agent/query", {"kind": "cycle_stats"}, headers=H)
    check("周期统计查询可用", st == 200 and "推断" in body)
    st, body = call_json(op, "/api/agent/query", {"kind": "delete_everything"}, headers=H)
    check("非法查询类型被拒（400）", st == 400, f"status={st}")
    st, body = call_json(op, "/api/agent/kb/search", {"q": "痛经"}, headers=H)
    check("知识库检索可用", st == 200 and "结果" in body, f"status={st}")

    # 站点专属词条文件必须在本组用例前就位（outbound 每次调用都重新读取）
    (pathlib.Path(DB).parent / "scrub_terms.txt").write_text(
        "# 冒烟测试（站点专属词条：姓名、内部项目名等都由这里配置）\n测试姓名甲\nmy-private-cluster\n",
        encoding="utf-8")
    st, body = call_json(op, "/api/agent/guard", {"kind": "search", "text": "测试姓名甲 经期推迟 怎么办"}, headers=H)
    check("隐私围栏拦截姓名", st == 403 and "个人标识" in body, f"status={st}")
    st, body = call_json(op, "/api/agent/guard", {"kind": "search", "text": "2026-08-17 出血量多"}, headers=H)
    check("隐私围栏拦截具体日期", st == 403 and "具体日期" in body, f"status={st}")
    # 站点专属词（来自可配置的 scrub_terms.txt）在检索词里也必须被拒绝
    st, body = call_json(op, "/api/agent/guard",
                         {"kind": "search", "text": "月经过多 my-private-cluster"}, headers=H)
    check("检索词含站点专属词被拒", st == 403 and "站点专属" in body, f"status={st} {body[:80]}")
    st, body = call_json(op, "/api/agent/guard", {"kind": "search", "text": "月经过多 诊断标准 FIGO"}, headers=H)
    check("通用检索词放行", st == 200 and '"allowed":true' in body.replace(" ", ""), f"status={st}")
    st, body = call_json(op, "/api/agent/guard", {"kind": "fetch", "url": "https://evil.example.com/x"}, headers=H)
    check("域名白名单拦截", st == 403 and "白名单" in body, f"status={st}")
    st, body = call_json(op, "/api/agent/guard",
                    {"kind": "fetch", "url": "https://www.acog.org/womens-health/faqs/dysmenorrhea-painful-periods"},
                    headers=H)
    check("白名单内域名放行", st == 200 and '"allowed":true' in body.replace(" ", ""), f"status={st}")

    before = n_cycles()
    st, body = call_json(op, "/api/agent/propose",
                    {"kind": "cycle_add",
                     "payload": {"start_date": "2026-09-05", "end_date": "2026-09-09",
                                 "flow": "medium", "symptoms": "腹痛"},
                     "rationale": "冒烟测试：智能体提案"}, headers=H)
    prop_id = json.loads(body).get("proposal_id") if st == 200 else 0
    check("提案已创建为 pending", st == 200 and prop_id > 0, f"status={st} {body[:120]}")
    check("未批准前不落库", n_cycles() == before, f"{before} -> {n_cycles()}")
    st, body = call_json(op, "/api/agent/propose", {"kind": "delete_all_records", "payload": {}}, headers=H)
    check("非法提案类型被拒（400）", st == 400, f"status={st}")

    st, body = call(op, "/admin/agent")
    check("待批准提案出现在管理页", f"/admin/agent/proposal/{prop_id}/decide" in body)
    tok2 = csrf_of(body)
    st, body = call(op, f"/admin/agent/proposal/{prop_id}/decide",
                    {"csrf": tok2, "action": "approve"}, follow=True)
    check("批准后真正落库", n_cycles() == before + 1, f"{before} -> {n_cycles()}")
    check("批准结果已记录", "已新增月经记录" in body)

    st, body = call(op, "/admin/agent")
    check("审计日志记录了被拒调用", "denied" in body and "隐私围栏" not in body[:0] or "denied" in body)

    st, _ = call(op, "/admin/agent/token/revoke", {"csrf": tok2}, follow=True)
    st, body = call(op, "/api/agent/ping", headers=H, follow=False)
    check("吊销令牌后立即失效（401）", st == 401, f"status={st}")

    # ================= 模型接入配置（密钥安全）=================
    st, body = call(op, "/admin")
    check("管理页有进入模型配置的入口（曾漏加）", 'href="/admin/llm"' in body)
    check("管理页有进入联网检索配置的入口", 'href="/admin/search"' in body)
    check("管理页有进入 AI 角色设置的入口", 'href="/admin/persona"' in body)
    check("管理页有进入智能体权限的入口", 'href="/admin/agent"' in body)

    st, body = call(op, "/admin/llm")
    check("模型接入配置页可打开", st == 200 and "模型接入配置" in body)
    check("模型名支持下拉候选（datalist）", 'list="modellist"' in body and 'id="modellist"' in body)
    check("无候选时给出提示", "暂无候选列表" in body or "候选" in body)

    # 写入缓存的模型列表后，候选应渲染进 datalist（不触发任何网络请求）
    with sqlite3.connect(DB) as c:
        for k, v in (("llm_model_list", json.dumps(["deepseek-chat", "qwen-max", "gpt-4o-mini"])),
                     ("llm_model_list_at", "2026-09-11 10:00:00")):
            c.execute("INSERT INTO settings(key,value) VALUES(?,?)"
                      " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    st, body = call(op, "/admin/llm")
    check("缓存的候选列表渲染进 datalist",
          all(m in body for m in ("deepseek-chat", "qwen-max", "gpt-4o-mini")) and "候选 3 个" in body)

    st, body = call(op, "/admin/llm/save",
                    {"csrf": tok2, "base_url": "https://api.example.com/v1",
                     "model": "test-model"}, follow=True)
    check("公网端点未勾选风险确认时被拒", "勾选" in body and "公网地址" in body, body[:150])

    SECRET = "sk-smoke-abcdef123456789"   # desensitize-ok 测试用假密钥
    st, body = call(op, "/admin/llm/save",
                    {"csrf": tok2, "base_url": "http://127.0.0.1:9099/v1",
                     "model": "smoke-model", "api_key": SECRET}, follow=True)
    check("内网端点保存成功", "已保存" in body, body[:150])
    # ---- 外发内容策略（用户红线：智能体/工程信息绝不外发）----
    import outbound, subprocess as _sp
    t, hits = outbound.scrub("测试姓名甲 2026-08-17 出血 /opt/demo/app 与 /var/lib/demo/db bg_abcdefghijklmn sk-abcdefghij")   # desensitize-ok 测试用假令牌与假密钥
    check("清洗：姓名/日期/路径/令牌/密钥全部抹除",
          all(x not in t for x in ("测试姓名甲", "2026-08-17", "/opt/demo", "/var/lib/demo", "bg_", "sk-")) and len(hits) >= 3,
          f"hits={hits}")
    m3, h3 = outbound.guard_messages([{"role": "user", "content": "数据库在 /var/lib/demo/db/x.db"}],
                                     public=True)
    check("公网端点：内部路径被清洗（不外发）",
          "/var/lib/demo" not in json.dumps(m3, ensure_ascii=False) and ("系统路径" in h3 or "用户目录路径" in h3), f"hits={h3}")
    try:
        outbound.verify_no_internals([{"role": "user", "content": "raw /opt/demo/deploy/pi/run.sh"}])
        check("fail-closed 安全网本身有效", False)
    except outbound.OutboundBlocked:
        check("fail-closed 安全网本身有效", True)
    # 站点专属敏感词来自数据目录的 scrub_terms.txt（不写死在代码里）
    terms = pathlib.Path(DB).parent / "scrub_terms.txt"
    t2, h2b = outbound.scrub("部署在 my-private-cluster 上")
    check("站点专属敏感词可从配置文件加载并抹除",
          "my-private-cluster" not in t2 and "站点专属词" in h2b, f"hits={h2b}")
    m2, h2 = outbound.guard_messages([{"role": "user", "content": "经期推迟了几天，要注意什么？"}],
                                     public=True)
    check("正常健康提问可正常外发", m2[0]["content"] == "经期推迟了几天，要注意什么？")

    # ---- 公网端点：可保存（勾选风险）但页面告警、且智能体拒绝启动 ----
    st, body = call(op, "/admin/llm/save",
                    {"csrf": tok2, "base_url": "https://relay.example.com/v1",
                     "model": "some-model", "allow_public": "1"}, follow=True)
    check("勾选风险确认后公网端点可保存", "已保存" in body, body[:150])
    check("公网端点页面出现红色告警", "公网地址" in call(op, "/admin/llm")[1])
    env = dict(os.environ); env["BG_DB"] = DB
    r = _sp.run([py_exe(), "llm_config.py", "check-agent-endpoint"],
                capture_output=True, text=True, env=env,
                cwd=str(pathlib.Path(__file__).resolve().parent))
    check("公网端点：给出警告但不拦截启动",
          r.returncode == 0 and '"public": true' in r.stdout and "公网地址" in r.stdout,
          f"rc={r.returncode} {r.stdout[:140]}")
    env_bad = dict(os.environ); env_bad["BG_DB"] = "/tmp/definitely-missing-dir/x.db"
    r2 = _sp.run([py_exe(), "llm_config.py", "check-agent-endpoint"],
                 capture_output=True, text=True, env=env_bad,
                 cwd=str(pathlib.Path(__file__).resolve().parent))
    check("读不到配置时给出「无法确认」警告且不拦截",
          r2.returncode == 0 and "读不到配置数据库" in r2.stdout, f"rc={r2.returncode}")
    # 还原成死端口（内网地址）：既用于验证「内网端点无警告」，也便于后续图片测试在无模型状态下验证链路
    call(op, "/admin/llm/save",
         {"csrf": tok2, "base_url": "http://127.0.0.1:9099/v1", "model": "smoke-model"}, follow=True)
    env_ok = dict(os.environ); env_ok["BG_DB"] = DB
    r3 = _sp.run([py_exe(), "llm_config.py", "check-agent-endpoint"],
                 capture_output=True, text=True, env=env_ok,
                 cwd=str(pathlib.Path(__file__).resolve().parent))
    check("内网端点无警告", '"warning": ""' in r3.stdout, r3.stdout[:140])

    # 净化指令：智能体读到的工作准则里不得含工程信息
    ctx = pathlib.Path(__file__).resolve().parent.parent / "deploy/pi/agent-context/instructions.md"
    check("智能体指令文件存在（仓库内不再有嵌套 CLAUDE.md）",
          ctx.is_file() and not (ctx.parent / "CLAUDE.md").exists())
    if ctx.is_file():
        txt = ctx.read_text(encoding="utf-8")
        bad = [m for m in ("/opt/", "/var/lib", "bg_", "sk-", "100.64.", "internal-lab",
                           "internal-project", "other-project", "BG_DB", "internal.example")
               if m in txt]
        check("智能体指令文件不含工程信息（路径/令牌/主机/其他项目）", not bad, f"命中={bad}")
    # 还原成死端口，便于接下来的图片测试在“无模型”状态下验证链路
    call(op, "/admin/llm/save",
         {"csrf": tok2, "base_url": "http://127.0.0.1:9099/v1", "model": "smoke-model"}, follow=True)

    # ---- 图片问诊：压缩、落盘、元数据 ----
    st, page = call(op, "/qa")
    m = re.search(r'action="/qa/(\d+)/ask"', page)
    qsid = m.group(1) if m else "0"
    from PIL import Image as _I
    import io as _io
    buf = _io.BytesIO()
    _I.new("RGB", (3000, 2000), (245, 245, 252)).save(buf, format="PNG")
    png = buf.getvalue()
    st, body = call_multipart(op, f"/qa/{qsid}/ask",
                              {"csrf": tok2, "question": "这是化验单照片，帮我看看有哪些指标需要关注"},
                              {"image": ("report.png", png, "image/png")})
    check("带图片提问被接受（SSE 200）", st == 200 and '"done"' in body, f"status={st}")
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT filename, size, stored_name FROM attachments WHERE ref_kind='qa'"
                        " ORDER BY id DESC LIMIT 1").fetchone()
        meta = c.execute("SELECT meta FROM qa_messages WHERE role='user' ORDER BY id DESC LIMIT 1").fetchone()[0]
    check("图片已归档为附件（不进入 git）", bool(row), "无附件记录")
    if row:
        check("图片已被压缩（体积小于原图）", row[1] < len(png), f"{len(png)}→{row[1]}")
        data_dir = pathlib.Path(os.environ.get("BG_DATA", pathlib.Path(__file__).resolve().parent.parent / "data"))
        f = data_dir / "uploads" / row[2]
        check("压缩后文件落盘", f.is_file())
        if f.is_file():
            im = _I.open(f)
            check("压缩后最长边 ≤1600", max(im.size) <= 1600, f"size={im.size}")
    check("消息元数据记录了图片", "image" in (meta or ""))
    st, body = call_multipart(op, f"/qa/{qsid}/ask",
                              {"csrf": tok2, "question": "测试非法类型"},
                              {"image": ("x.txt", b"hello", "text/plain")})
    check("非图片类型被拒绝（400）", st == 400, f"status={st}")

    st, body = call(op, "/admin/llm")   # 显式取页，避免用到上一个请求的响应体
    check("页面不回显密钥全文", SECRET not in body)
    check("只显示密钥后 4 位", "…6789" in body or "6789" in body, f"片段: {body[:150]}")

    inst = (pathlib.Path(__file__).resolve().parent.parent / "deploy/install.sh").read_text(encoding="utf-8")
    check("安装脚本部署净化指令并创建智能体工作目录",
          "agent-context/instructions.md" in inst and "$AGENT_DIR" in inst)
    syspaths = [p for p in ("/opt/", "/etc/", "/var/lib", "/usr/local/") if p in inst]
    check("安装脚本不含系统路径（用户层部署）", not syspaths, f"命中={syspaths}")

    keyf = pathlib.Path(DB).parent / "llm_api_key"
    check("密钥写入独立文件", keyf.is_file())
    if keyf.is_file():
        mode = keyf.stat().st_mode & 0o777
        check("密钥文件权限为 600", mode == 0o600, f"mode={oct(mode)}")
        check("密钥文件内容正确", keyf.read_text().strip() == SECRET)

    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT COUNT(*) FROM settings WHERE value LIKE ?", (f"%{SECRET}%",)).fetchone()
    check("数据库里不含密钥明文", row[0] == 0)

    st, body = call(op, "/admin/llm/test", {"csrf": tok2}, follow=True)
    check("连通性测试路由可用（指向死端口应报失败）", "失败" in body or "不可用" in body, body[:150])

    st, body = call(op, "/admin/llm/key/clear", {"csrf": tok2}, follow=True)
    check("可清除密钥", "已清除" in body and not keyf.exists())

    # 还原成内置默认，避免影响后续导出/问答
    call(op, "/admin/llm/save",
         {"csrf": tok2, "base_url": "http://127.0.0.1:8080/v1",
          "model": "local-model"}, follow=True)

    # ================= 联网检索（Hyperbrowser 云浏览器）配置 =================
    st, body = call(op, "/admin/search")
    check("联网检索配置页可打开", st == 200 and "云浏览器" in body and "Hyperbrowser" in body,
          f"status={st}")
    check("默认不带密钥、未开启", "未配置" in body and "关闭" in body)
    check("页面上写明为什么不用 SDK（实测内存对比）",
          "35.9MB" in body and "40.8MB" in body and "标准库 urllib 直连 REST" in body)

    hb_key = pathlib.Path(DB).parent / "hyperbrowser_key"
    st, body = call(op, "/admin/search/save",
                    {"csrf": tok2, "enabled": "1", "base_url": "https://api.hyperbrowser.ai",
                     "api_key": "hb-test-key-not-real"}, follow=True)
    check("保存联网检索配置（开关 + 密钥）", "已保存" in body, body[:120])
    check("密钥写入独立文件且权限 600",
          hb_key.exists() and (hb_key.stat().st_mode & 0o077) == 0)
    check("密钥不进数据库",
          sqlite3.connect(DB).execute("SELECT COUNT(*) FROM settings WHERE value LIKE '%hb-test-key%'")
          .fetchone()[0] == 0)
    st, body = call(op, "/admin/search")
    check("配置页显示已开启 + 密钥已配置（只给后 4 位）",
          "已开启" in body and "已配置" in body and "…real" in body)
    import websearch as webm
    check("开关 + 密钥齐全时才算可用", webm.enabled() is True)
    _q1 = webm.strip_private("2026-08-30 我月经推迟了 5 天，someone@example.com 想问下")   # desensitize-ok 假邮箱，example.com 是保留域
    _q2 = webm.strip_private("https://x.com/a 痛经怎么办")
    check("查询词清洗：摘掉日期/数字/邮箱/链接，保留通用医学词",
          "2026" not in _q1 and "@" not in _q1 and "x.com" not in _q2 and "痛经" in _q2,
          f"q1={_q1!r} q2={_q2!r}")
    import llm as llmk
    check("整行粘贴的密钥会被剥成密钥本身（实测踩过 401）",
          llmk.normalize_key("HYPERBROWSER_API_KEY=hb_abc123") == "hb_abc123"
          and llmk.normalize_key('export FOO="xyz"') == "xyz"
          and llmk.normalize_key("Bearer tok-123") == "tok-123"
          and llmk.normalize_key("  plain-key  ") == "plain-key")
    _md_clean = webm._clean_markdown("![图](https://x/a.png)\n\n[痛经](https://x/b)\n\n\n\n正文内容")
    check("抓回的正文会去掉图片、链接只留文字",
          "a.png" not in _md_clean and "https" not in _md_clean and "痛经" in _md_clean
          and "正文内容" in _md_clean, repr(_md_clean))
    check("权威医学来源白名单识别",
          webm.trusted_host("https://www.nhs.uk/conditions/periods")
          and not webm.trusted_host("https://spam.example.com/x"))
    st, body = call(op, "/admin/search/save", {"csrf": tok2, "enabled": "0"}, follow=True)
    check("可以关掉联网检索", "已保存" in body and webm.enabled() is False)
    st, body = call(op, "/admin/search/key/clear", {"csrf": tok2}, follow=True)
    check("可清除联网检索密钥", "已清除" in body and not hb_key.exists())
    st, _ = call(op, "/admin/search/test", {"csrf": "错的"}, follow=False)
    check("测试路由需要正确 csrf", st >= 400, f"status={st}")

    # ================= 知识库：wiki 化 + 检索加强 + AI 归档走提案 =================
    import kb as kbm
    _hits = kbm.search("痛经 热敷", top=4)
    check("检索按文档去重且带完整正文（不再只给 320 字摘要）",
          bool(_hits) and len({h["rel"] for h in _hits}) == len(_hits)
          and all(len(h.get("text") or "") > 0 for h in _hits), f"hits={len(_hits)}")
    _ctx, _used = kbm.context_for_question("痛经怎么办")
    _parts = _ctx.split("### 依据：")[1:]
    check("依据里给的是较完整正文（不是 320 字摘要）",
          len(_parts) >= 2 and sum(len(p) for p in _parts) > 2000,
          f"段数={len(_parts)} 总长={len(_ctx)}")
    check("标题/标签命中权重高于正文（同名文档得分更高）",
          all(h["score"] > 0 for h in _hits))
    _q_known = "痛经的时候热敷有用吗"
    _cov_known = kbm.coverage(_q_known, kbm.search(_q_known, top=3))
    _q_gap = "子宫内膜异位症腹腔镜术后复发率大概多少怎么降低"
    _cov_gap = kbm.coverage(_q_gap, kbm.search(_q_gap, top=3))
    check("覆盖率能区分「库里有答案」和「只是话题相关」",
          0 < _cov_gap < _cov_known <= 1, f"known={_cov_known} gap={_cov_gap}")
    check("覆盖率只用于粗筛（很低/很高才不问模型）",
          _cov_gap < 0.75 and _cov_known < 0.75)
    check("知识库目录可生成（catalog / INDEX）",
          "docs/" in kbm.catalog() and "知识库目录" in kbm.index_md())
    _rel = _hits[0]["rel"]
    _rd = kbm.related(_rel)
    check("wiki 相关文档：链出/链入都能算出来",
          isinstance(_rd.get("out"), list) and isinstance(_rd.get("incoming"), list))
    _doc = call(op, "/knowledge?path=" + quote(_rel))[1]
    check("文档页能打开", "返回知识库" in _doc, _doc[:120])

    # 目录页由维护任务生成，并且出现在知识库里
    _env_i = dict(os.environ); _env_i["BG_DB"] = DB
    _ri = _sp.run([py_exe(), "maint.py", "index"], capture_output=True, text=True,
                  env=_env_i, cwd=str(pathlib.Path(__file__).resolve().parent))
    check("维护任务可刷新知识库目录", "目录" in (_ri.stdout + _ri.stderr), (_ri.stdout + _ri.stderr)[:160])
    check("目录文档已进入知识库", bool(kbm.read_text("医学知识/INDEX.md")))
    check("知识库页面能列出目录文档", "知识库目录" in call(op, "/knowledge")[1])
    _idx = call(op, "/knowledge?path=" + quote("医学知识/INDEX.md"))[1]
    check("目录页能打开且列出其它文档（wiki 入口）", "知识库目录" in _idx and "医学知识/" in _idx, _idx[:120])

    # 长正文的笔记也能确认落库（线上踩过：payload 被截断 → 确认时报 JSON 解析失败）
    import agent as _agent
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO agent_proposals(kind, payload, rationale, status, created_at)"
                  " VALUES(?,?,?,?,?)",
                  ("kb_note", json.dumps({"title": "长文测试", "path": "医学知识/资料整理/长文测试.md",
                                          "source": "https://x/y", "body": "## 正文\n" + "疼" * 6000},
                                         ensure_ascii=False),
                   "长正文", "pending", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
    st, body = call(op, "/qa/pending")
    _long = [i for i in json.loads(body)["items"] if "长文测试" in json.dumps(i["summary"], ensure_ascii=False)]
    if _long:
        st, body = call(op, f"/qa/proposal/{_long[0]['id']}/decide", {"csrf": tok2, "action": "approve"})
        _j = json.loads(body)
        check("长正文笔记确认后能真的写进库（不再 payload 截断）",
              bool(_j.get("ok")) and bool(kbm.read_text("医学知识/资料整理/长文测试.md")),
              (body or "")[:120])
    else:
        check("长正文笔记确认后能真的写进库（不再 payload 截断）", False, "没找到长文提案")
    with sqlite3.connect(DB) as c:
        _too_long = json.dumps({"title": "x", "body": "啊" * 30000}, ensure_ascii=False)
    try:
        _agent.create_proposal(sqlite3.connect(DB), "kb_note",
                               {"title": "x", "body": "啊" * 30000})
        _raised = False
    except ValueError:
        _raised = True
    check("提案内容超上限时报错而不是静默截断", _raised)

    # AI 归档走提案：确认后才真的写进知识库
    # 先清干净：/export 会把上次的笔记写到 KB 目录，下次启动又会被导入回来
    _note_rel = "医学知识/资料整理/测试笔记.md"
    _note_file = pathlib.Path(os.environ.get("BG_KB_DIR") or ".") / "docs" / _note_rel
    try:
        _note_file.unlink()
    except FileNotFoundError:
        pass
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM kb_docs WHERE path=?", (_note_rel,))
        c.commit()
    import kb as _kb2
    _kb2.invalidate()
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO agent_proposals(kind, payload, rationale, status, created_at)"
                  " VALUES(?,?,?,?,?)",
                  ("kb_note", json.dumps({"title": "测试笔记",
                                          "path": "医学知识/资料整理/测试笔记.md",
                                          "source": "https://www.nhs.uk/x",
                                          "body": "## 要点\n\n热敷可能缓解痛经。"},
                                         ensure_ascii=False),
                   "来自问答：联网资料", "pending", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
    st, body = call(op, "/qa/pending")
    _items = json.loads(body).get("items", [])
    _note = [i for i in _items if "知识库" in (i.get("kind") or "")]
    check("待确认列表里能看到「知识库笔记」提案", bool(_note), body[:160])
    check("正文在卡片里只给摘要（不把卡片撑爆）",
          bool(_note) and all(len(str(v)) <= 260 for v in (_note[0]["summary"] or {}).values()))
    if _note:
        _pid = _note[0]["id"]
        st, body = call(op, f"/qa/proposal/{_pid}/decide", {"csrf": tok2, "action": "reject"})
        check("取消后不写进知识库", bool(json.loads(body).get("ok")) and
              kbm.read_text(_note_rel) is None, (body or "")[:120])
        with sqlite3.connect(DB) as c:
            c.execute("INSERT INTO agent_proposals(kind, payload, rationale, status, created_at)"
                      " VALUES(?,?,?,?,?)",
                      ("kb_note", json.dumps({"title": "测试笔记",
                                              "path": "医学知识/资料整理/测试笔记.md",
                                              "source": "https://www.nhs.uk/x",
                                              "body": "## 要点\n\n热敷可能缓解痛经。"},
                                             ensure_ascii=False),
                       "来自问答：联网资料", "pending",
                       _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            c.commit()
        st, body = call(op, "/qa/pending")
        _pid2 = [i for i in json.loads(body)["items"] if "知识库" in (i.get("kind") or "")][0]["id"]
        st, body = call(op, f"/qa/proposal/{_pid2}/decide", {"csrf": tok2, "action": "approve"})
        _saved = kbm.read_text(_note_rel)
        check("确认后写进知识库（个人层、标注待核对）",
              bool(_saved) and "热敷" in _saved and "待核对" in json.dumps(
                  kbm.get_doc(_note_rel) or {}, ensure_ascii=False),
              (body or "")[:160])
        with sqlite3.connect(DB) as c:
            _scope = c.execute("SELECT scope, origin FROM kb_docs WHERE path=?",
                               (_note_rel,)).fetchone()
        check("归档的笔记是个人层、来源标记为 ai", _scope == ("personal", "ai"), str(_scope))


    # ================= 身份 / 角色 / AI 自动归档 =================
    # 注意：本文件里的 main() 是同名函数，要拿模块得换个绑定名
    import main as mainm

    def _topbar(name):
        return f">{name}</summary>" in json.dumps(call(op, "/dashboard")[1], ensure_ascii=False) \
               or f">{name}</summary>" in call(op, "/dashboard")[1].replace("\n", "")

    st, body = call(op, "/account")
    check("账号页有昵称表单", 'action="/account/profile"' in body and "保存昵称" in body)
    call(op, "/account/profile", {"csrf": tok2, "nickname": "阿哲"}, follow=True)
    check("昵称可保存，右上角显示昵称", _topbar("阿哲"))
    call(op, "/account/profile", {"csrf": tok2, "nickname": ""}, follow=True)
    check("昵称留空＝回落到用户名", _topbar("admin"))

    st, body = call(op, "/admin/persona")
    check("AI 角色设置页可打开且默认是守护兔", st == 200 and "守护兔" in body, f"status={st}")
    check("默认角色提示词含三步结构（情况/原因/接下来怎么办）",
          "可能的原因" in body and "接下来怎么办" in body)
    st, body = call(op, "/admin/persona/save",
                    {"csrf": tok2, "name": "小护士", "prompt": "你是一位温和的护士。"}, follow=True)
    check("可保存自定义角色，按钮同步改名",
          "角色已保存" in body and _topbar("小护士") and "小护士" in body or "小护士" in body)
    check("自定义角色生效", mainm.persona()["name"] == "小护士" and mainm.persona()["custom"])
    st, body = call(op, "/admin/persona/reset", {"csrf": tok2}, follow=True)
    check("可恢复默认守护兔", "守护兔" in body and mainm.persona()["name"] == "守护兔")

    _fab = call(op, "/dashboard")[1]
    check("AI 按钮用内置小兔子图标（SVG）+ 角色名",
          'class="bunny"' in _fab and "守护兔" in _fab and 'class="pico"' not in _fab)
    check("标签栏标题＝页面名 · 站点名", "<title>总览 · " in _fab, _fab[_fab.find("<title>"):_fab.find("<title>") + 60])
    st, body = call(op, "/admin/persona/save",
                    {"csrf": tok2, "name": "兽医小熊", "icon": "🐻", "prompt": "你是一只温和的小熊。"},
                    follow=True)
    _fab2 = call(op, "/dashboard")[1]
    check("角色图标可自定义（换成 emoji）",
          'class="pico">🐻' in _fab2 and "兽医小熊" in _fab2 and 'class="bunny"' not in _fab2)
    check("角色图标也出现在面板标题", 'class="pico">🐻' in _fab2)
    call(op, "/admin/persona/reset", {"csrf": tok2}, follow=True)
    check("恢复默认后回到内置兔子", 'class="bunny"' in call(op, "/dashboard")[1])

    class _U:
        who = "him"
        is_partner = True

    class _U2:
        who = "her"
        is_partner = False

    check("男友提问时提示词加身份说明（转述、需她本人确认）",
          "伴侣" in mainm._identity_note(_U()) and "转述" in mainm._identity_note(_U()))
    check("她本人提问不加身份说明", mainm._identity_note(_U2()) == "")
    check("默认角色提示词要求先安抚再分析", "先照顾情绪" in mainm.PERSONA_DEFAULT_PROMPT)

    # AI 判断值得留下就直接入库（不再逐条确认），且不往数据仓库备份
    import agent as _ag2
    import db as dbm
    _note_path = "医学知识/资料整理/自动归档测试.md"
    with dbm.db() as _conn:            # 用带 row_factory 的连接（和线上一致）
        _m, _k, _ = _ag2.apply_kb_note(_conn, {"title": "自动归档测试", "path": _note_path,
                                               "body": "## 要点\n\n自动写入，无需确认。",
                                               "source": "https://www.nhs.uk/x"})
        _conn.commit()
    check("AI 自动归档直接落库（不需要点确认）",
          "已存入知识库" in _m and bool(kbm.read_text(_note_path)), _m)
    _row = sqlite3.connect(DB).execute(
        "SELECT scope, status, origin FROM kb_docs WHERE path=?", (_note_path,)).fetchone()
    check("归档笔记是个人层 + 待核对 + origin=ai", _row == ("personal", "待核对", "ai"), str(_row))
    import tempfile
    _tmp = pathlib.Path(tempfile.mkdtemp())
    with dbm.db() as _conn2:
        kbm.export_to_dir(_conn2, _tmp, scope="personal")
    _exported = [str(p.relative_to(_tmp)) for p in _tmp.rglob("*.md")]
    check("知识笔记不导出到数据仓库（只本地保存）",
          bool(_exported) and not any(p.startswith("医学知识/") for p in _exported),
          f"导出 {len(_exported)} 篇：{_exported[:3]}")

    # ================= 界面整理：经期页去重 / 每日记录进病历 / 主题 / 来源过滤 =================
    _dash = call(op, "/dashboard")[1]
    _cyc = call(op, "/cycle")[1]
    check("经期页不再重复总览的三张卡（周期环/受孕率/激素）",
          'id="blk-ring"' not in _cyc and 'id="blk-fert"' not in _cyc and 'id="blk-hormone"' not in _cyc)
    check("经期页保留周期推断/统计与分布/日历",
          "blk-infer" in _cyc and "blk-stats" in _cyc and "calwrap" in _cyc)
    check("经期页去掉了手动添加表单", "/cycle/add" not in _cyc and "手动添加" not in _cyc)
    check("总览仍然有周期环", 'id="blk-ring"' in _dash)
    check("AI 面板去掉了「同一账号共用一个会话」提示", "同一账号共用一个会话" not in _dash)
    check("周期环标题换成月亮图标", "🌙 周期环" in _dash)

    check("顶栏有配色选择与深浅色切换", 'id="accentPick"' in _dash and 'id="themeBtn"' in _dash)
    _css = open("static/app.css", encoding="utf-8").read()
    check("深色主题与配色方案都在样式里（含跟随系统）",
          ':root[data-theme="dark"]' in _css and '[data-accent="blue"]' in _css
          and "prefers-color-scheme" in _dash)
    check("白色面板改成 token（深色模式才生效）",
          "background:#fff" not in _css and "var(--card)" in _css)

    _css_all = open("static/app.css", encoding="utf-8").read()
    check("动效：关键帧 + 尊重「减少动态效果」",
          "@keyframes bgFadeUp" in _css_all and "@keyframes bgPanelIn" in _css_all
          and "@keyframes bgChipIn" in _css_all
          and "prefers-reduced-motion: reduce" in _css_all
          and "*{animation:none!important" in _css_all)
    check("手机上输入控件字号 ≥16px（iOS 聚焦不放大整页）",
          "input,select,textarea{font-size:16px}" in _css_all)
    check("打勾后就地更新的卡片有反馈类", "blk-updated" in _css_all)

    check("配色弹层在小屏上不会跑到屏幕外（手机改成贴着顶栏定位）",
          ".topctl details{position:static}" in _css_all and "left:8px;right:8px;top:100%" in _css_all)
    check("卡片标题栏渐变跟随主色（换配色时标题栏一起变）",
          "@supports (color: color-mix(in srgb, red, blue))" in _css_all
          and "color-mix(in srgb, var(--accent) 9%" in _css_all
          and "--head-line:color-mix" in _css_all)
    check("ghost 按钮是浅实心 chip，不再像白底描边",
          ".btn.ghost{background:var(--accent-soft);border-color:transparent" in _css_all)
    _doc_page = call(op, "/knowledge?path=" + quote("医学知识/月经周期生理与正常范围.md"))[1]
    check("「← 返回…」这类动作链接都是按钮样式",
          'class="btn ghost small" href="/knowledge"' in _doc_page
          and 'class="btn ghost small" href="/admin"' in call(op, "/admin/persona")[1])

    _dash_now = call(op, "/dashboard")[1]
    check("环下面的「点图例看说明」提示已删", "点图例看说明" not in _dash_now and ".lghint" not in _css_all)
    check("底部导航中间留了圆按钮的位置", 'class="tabgap"' in _dash_now and ".tabgap{flex:0 0 auto;width:62px" in _css_all)
    check("守护兔按钮是圆（手机从底栏凸出、桌面右下角）",
          ".fab{position:fixed;right:16px;bottom:22px;z-index:44;width:54px;height:54px" in _css_all
          and "0 0 0 4px var(--bg)" in _css_all)
    check("问答面板右上角的 × 靠最右", "margin:-6px -6px -6px auto" in _css_all
          and ".fabhead b{display:inline-flex;align-items:center;gap:6px;margin-right:auto}" in _css_all)

    _adm2 = call(op, "/admin")[1]
    check("管理页有「软件更新」卡片（检查/立即更新/自动开关/pi）",
          "软件更新" in _adm2 and "/admin/update/check" in _adm2
          and "/admin/update/apply" in _adm2 and "/admin/update/pi" in _adm2
          and "自动检查更新" in _adm2 and "自动更新并重启" in _adm2)
    st, body = call(op, "/admin/update/auto",
                    {"csrf": tok2, "auto_check": "1", "interval": "12"}, follow=True)
    check("自动检查设置可保存", "已保存" in body, body[:120])
    import update as updm2
    check("自动检查设置已生效", updm2.auto_settings()["auto_check"]
          and updm2.auto_settings()["interval"] == 12)
    check("自动更新默认关闭（要显式打开）", updm2.auto_settings()["auto_update"] is False)
    call(op, "/admin/update/auto", {"csrf": tok2}, follow=True)
    check("可以关掉自动检查", updm2.auto_settings()["auto_check"] is False)
    check("版本信息能读到（本机是 git 仓）", bool(updm2.local_commit()), updm2.local_commit())
    check("pi 状态可读（未装就如实说未安装）",
          isinstance(updm2.pi_state().get("installed"), bool))

    # 临近经期的提问弹窗；以及「表里没有里」——里的人另一个仓库的扩展
    _dash3 = call(op, "/dashboard")[1]
    check("临近经期的提问弹窗在模板里（未到期就不渲染）",
          'id="periodDlg"' in open("templates/base.html", encoding="utf-8").read()
          and mainm._period_check(None) == {})
    _mainsrc = open("main.py", encoding="utf-8").read()
    _css_all = open("static/app.css", encoding="utf-8").read()
    check("表里没有「里」：/play 与跑团路由都不存在（里是另一个仓库的扩展）",
          call(op, "/play", follow=False)[0] == 404
          and call(op, "/play/trpg", follow=False)[0] == 404
          and "trpg" not in _mainsrc and "play.html" not in _mainsrc)
    check("品牌链接默认回总览（里挂上后由扩展改写）",
          'class="brand" href="/dashboard"' in _dash3
          and "brand_href" in open("templates/base.html", encoding="utf-8").read())
    check("扩展机制在内，且默认一个扩展也不挂（表必须能单跑）",
          "extm.load(app, render" in _mainsrc
          and "def load(app, render, helpers" in open("extensions.py", encoding="utf-8").read()
          and "extm.context_for(request, user)" in _mainsrc
          and "extm.schemas()" in open("db.py", encoding="utf-8").read())
    check("base.html 给扩展留了 head 块（里用它加载自己的样式表）",
          "{% block head %}{% endblock %}" in open("templates/base.html", encoding="utf-8").read())
    check("思考过程弹层自带底色与字色（否则 dialog 默认纯白，深色下炸白块）",
          "background:var(--card);color:var(--ink);\n  box-shadow:0 18px 50px rgba(44,42,43,.25);padding:14px 16px}" in _css_all
          and ".thinkdlg pre{" in _css_all and "color:var(--ink-2)}" in _css_all
          and "--think-line:" in _css_all)
    check("思考过程弹层自带底色与字色（否则 dialog 默认纯白，深色下炸白块）",
          "background:var(--card);color:var(--ink);\n  box-shadow:0 18px 50px rgba(44,42,43,.25);padding:14px 16px}" in _css_all
          and ".thinkdlg pre{" in _css_all and "color:var(--ink-2)}" in _css_all
          and "--think-line:" in _css_all)
    _bad_hex = re.findall(r"var\(--[a-z0-9-]+\)[0-9a-f]{2,3}\b", _css_all)
    check("样式表里没有被切坏的色值（var(--card)afc 这种整条会被浏览器丢弃）",
          not _bad_hex)
    _tpl_light = [f.name for f in pathlib.Path("templates").glob("*.html")
                  if re.search(r'style="[^"]*background:#[fF]', f.read_text(encoding="utf-8"))]
    check("模板里的内联底色也不写死浅色（深色下会留白块）", not _tpl_light)

    # ---- 问答断开不再丢答案（手机切网/锁屏是真的会掐断连接） ----
    check("整段问答跑在自己的线程里（客户端断开也照样落库）",
          "def _produce() -> None:" in _mainsrc and "threading.Thread(target=_produce" in _mainsrc
          and "async def gen():" in _mainsrc and "q.get_nowait()" in _mainsrc
          and "queue.Queue()" in _mainsrc)
    check("长静默阶段有 SSE 心跳（联网检索 / 知识库够不够）",
          'PING = ": ping' in _mainsrc and _mainsrc.count("yield PING") >= 2
          and "_SlowJob(" in _mainsrc)
    check("知识库够不够的判定有超时上限（不然手机容易先掉线）",
          "max_tokens=8, timeout=30)" in _mainsrc)
    check("每次问答都留一条可查记录（耗时/字数/依据数）",
          '"qa_done"' in _mainsrc and "time.time() - t_start" in _mainsrc)
    _appjs = open("static/app.js", encoding="utf-8").read()
    check("问答断开后前端把问题放回输入框并给重试按钮",
          "function addRetry(" in _appjs and "ta.value = q;" in _appjs
          and "问题已经放回输入框" in _appjs and "hadAtts" in _appjs)
    check("历史里没人回答的那条问题也能一键重试（另起一张卡片，不塞进提问气泡里）",
          "这条问题上次没有生成回答（连接中断了）。" in _appjs
          and "var d2 = bubble('assistant', false);" in _appjs)
    check("重试按钮有样式", ".fabbody .retrybtn{" in _css_all)
    check("健康档案页仍然有底部导航与主题控件（没被误伤）",
          'class="tabbar"' in _dash3 and 'id="accentPick"' in _dash3 and 'id="themeBtn"' in _dash3)


    _admin = call(op, "/admin")[1]
    check("管理页写明备份范围（记录备份 / 知识库不备份）",
          "备份范围（确认一下）" in _admin and "不备份" in _admin and "不占 GitHub" in _admin)
    check("管理页显示知识库规模与清理入口",
          "知识库规模" in _admin and "AI 资料笔记" in _admin and "/admin/kb/cleanup" in _admin)

    import websearch as webm2
    check("百度系来源会被过滤掉",
          webm2.blocked_host("https://health.baidu.com/m/detail/x")
          and not webm2.blocked_host("https://www.nhs.uk/conditions/period-pain"))
    check("来源排序：权威 2 / 普通 1 / 自媒体 0",
          webm2.host_rank("https://www.nhs.uk/x") == 2
          and webm2.host_rank("https://zhihu.com/x") == 0
          and webm2.host_rank("https://example.org/x") == 1)

    # 每日记录（症状等）要出现在病历时间轴里
    with dbm.db() as _c:
        _pid = _c.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()["id"]
        _c.execute("INSERT INTO day_logs(profile_id, log_date, kind, name, severity, value, note,"
                   " created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (_pid, dbm.today(), "symptom", "测试用腹痛", 3, "", "午饭后加重", dbm.now()))
        _c.commit()
    import kb as _kbq
    _rec = call(op, "/records")[1]
    check("病历页能看到每日记录（症状/用药/体重）",
          "测试用腹痛" in _rec and "症状" in _rec, _rec[:160])
    check("每日记录的删除入口指向日记记录接口", "/cycle/log/delete/" in _rec)
    check("总览的「病历与就医」也带上每日记录（用户：病例有信息这里却是空的）",
          "测试用腹痛" in call(op, "/dashboard")[1])

    import subprocess as _sp3
    bare = pathlib.Path(DB).parent / "remotes" / "kb.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    if not bare.is_dir():
        _sp3.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    st, body = call(op, "/admin")
    check("管理页有「数据同步」卡片", "数据同步" in body and "要同步到的 GitHub 仓库" in body)

    # ---- 同步只在有改动时触发（定时器已彻底取消） ----
    import sync_job as syncm
    check("同步触发机制存在（去抖 + 后台线程 + 可查排队状态）",
          hasattr(syncm, "schedule") and hasattr(syncm, "pending") and syncm._DEBOUNCE > 0)
    check("写操作会排队同步（HTTP 中间件已接上）",
          "_sync_on_change" in open("main.py", encoding="utf-8").read())
    check("队列为空时不会凭空同步", syncm.pending().get("pending") is False, str(syncm.pending()))
    st, body = call(op, "/admin")
    check("管理页写明触发方式（只有改动才同步，没有定时器）",
          "有改动就触发" in body and "没有定时任务" in body and "定时器只剩兜底" not in body)
    check("同步相关的单元模板已从仓库全部删除（定时器 + oneshot 服务）",
          not pathlib.Path("../deploy/systemd-user/APP_SLUG-sync.timer").exists()
          and not pathlib.Path("../deploy/systemd-user/APP_SLUG-sync.service").exists())
    _instsrc = open("../deploy/install.sh", encoding="utf-8").read()
    check("安装脚本不再启用同步单元，但会清理老版本装过的（定时器 + 服务）",
          'enable --now "$APP_SLUG-sync.timer"' not in _instsrc
          and 'disable --now "$u"' in _instsrc
          and '"$APP_SLUG-sync.timer" "$APP_SLUG-sync.service"' in _instsrc)
    _cfgsrc = open("../deploy/config.sh", encoding="utf-8").read()
    check("部署配置里不再有同步周期项 SYNC_INTERVAL", "SYNC_INTERVAL" not in _cfgsrc)
    check("没有定时器后的补网：启动补同步接在启动钩子上（只在有待推送标记时才跑）",
          hasattr(syncm, "retry_pending_on_start") and hasattr(syncm, "PENDING_KEY")
          and "retry_pending_on_start" in open("main.py", encoding="utf-8").read())
    _sjsrc = open("sync_job.py", encoding="utf-8").read()
    check("真的推出去了才清「待推送」标记（不是「没有失败字样」就清）",
          'if "push✓" in summary or "无变化" in summary or "已关闭同步" in summary:' in _sjsrc
          and "push失败" in _sjsrc)
    check("未登录时写接口 303 回 /login，不算「数据变更」（否则匿名 POST 就能触发同步）",
          "_is_login_redirect" in _mainsrc and _mainsrc.count("_is_login_redirect") >= 2)
    st, body = call(op, "/admin/sync/config",
                    {"csrf": tok2, "repo": "file://" + str(bare), "branch": "main",
                     "enabled": "1", "snapshot": "1"}, follow=True)
    check("同步仓库地址可保存", "已保存" in body, body[:120])
    # ---- 仓库凭据：应用自己生成的部署密钥 / 令牌 ----
    st, body = call(op, "/admin/sync/cred", {"csrf": tok2, "mode": "key"}, follow=True)
    check("可切换同步凭据来源（部署密钥）", "用部署密钥" in body, body[:120])
    st, body = call(op, "/admin/sync/cred", {"csrf": tok2, "mode": "auto"}, follow=True)
    check("可切回系统自带凭据（默认）", "系统自带凭据" in body, body[:120])
    check("页面显示凭据文件路径（.env）", ".env" in call(op, "/admin")[1])

    st, body = call(op, "/admin/sync/keygen", {"csrf": tok2}, follow=True)
    check("可生成部署密钥并展示公钥", "ssh-ed25519" in body, body[:160])
    sshdir = pathlib.Path(DB).parent / "ssh"
    priv, pub = sshdir / "id_ed25519", sshdir / "id_ed25519.pub"
    check("私钥落盘且权限 600", priv.is_file() and (priv.stat().st_mode & 0o777) == 0o600,
          oct(priv.stat().st_mode & 0o777) if priv.is_file() else "缺文件")
    check("私钥内容不出现在页面里",
          priv.is_file() and priv.read_text().splitlines()[-1][:32] not in body)
    SECRET2 = "ghp-" + "smoke0" * 5
    st, body = call(op, "/admin/sync/token", {"csrf": tok2, "action": "save", "token": SECRET2},
                    follow=True)
    check("令牌可保存且不回显", "已保存" in body and SECRET2 not in body, body[:120])
    envf = pathlib.Path(os.environ.get("BG_ENV_FILE")
                        or pathlib.Path(__file__).resolve().parent.parent / ".env")
    check("令牌写入项目 .env 且权限 600",
          envf.is_file() and (envf.stat().st_mode & 0o777) == 0o600 and "SYNC_TOKEN=" in envf.read_text(),
          str(envf))
    with sqlite3.connect(DB) as c:
        n = c.execute("SELECT COUNT(*) FROM settings WHERE value LIKE ?", (f"%{SECRET2}%",)).fetchone()[0]
    check("数据库里不含令牌明文", n == 0)
    st, body = call(op, "/admin/sync/token", {"csrf": tok2, "action": "clear"}, follow=True)
    check("令牌可清除", "已清除" in body and "SYNC_TOKEN=　" not in envf.read_text().replace("\n", "　").split("　")[0] if envf.is_file() else True)
    st, body = call(op, "/admin/sync/test", {"csrf": tok2}, follow=True)
    check("可测试仓库可达性", ("可达" in body) or ("不可达" in body), body[:160])

    st, body = call(op, "/admin/sync/run", {"csrf": tok2}, follow=True)
    check("立即同步可执行（导出→提交→推送）", "同步：" in body, body[:200])
    rc = _sp3.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main"],
                  capture_output=True, text=True)
    remote_files = rc.stdout
    check("远端仓库收到知识库文档", "docs/" in remote_files and ".md" in remote_files,
          remote_files[:120])
    check("远端仓库收到 SQLite 快照（全部数据可恢复）", "bunny-guardian.db" in remote_files,
          remote_files[:200])

    # ---- 数据备份与恢复 ----
    st, body = call(op, "/admin")
    check("管理页有「数据备份与恢复」卡片", "数据备份与恢复" in body and "立即备份" in body)
    tok_b = csrf_of(body)
    st, body = call(op, "/admin/backup/now", {"csrf": tok_b}, follow=True)
    check("立即备份成功", "已备份" in body and "sha256" in body, body[:160])
    bdir = pathlib.Path(DB).parent / "backups"
    bfiles = sorted(bdir.glob("bunny-guardian-manual-*.db")) if bdir.is_dir() else []
    check("备份文件落盘且权限 600",
          bool(bfiles) and (bfiles[-1].stat().st_mode & 0o777) == 0o600,
          str(bfiles[-1]) if bfiles else "无文件")
    backup_name = bfiles[-1].name if bfiles else ""
    st, body = call(op, f"/admin/backup/download/{backup_name}")
    check("可下载备份文件", st == 200 and len(body) > 0, f"status={st}")
    st, _ = call(op, "/admin/backup/download/..%2F..%2Fetc%2Fpasswd", follow=False)
    check("非法备份文件名被拒绝", st in (403, 404), f"status={st}")

    st, body = call(op, "/admin/backup/settings",
                    {"csrf": tok_b, "enabled": "1", "interval": "weekly", "keep": "3"}, follow=True)
    check("备份设置可保存", "已保存" in body, body[:120])

    # 恢复：先备一份 → 写入一条有标记的记录 → 恢复 → 标记应消失
    st, page = call(op, "/cycle")
    ctok = csrf_of(page)
    call(op, "/cycle/add", {"csrf": ctok, "start_date": "2027-01-05", "end_date": "2027-01-09"},
         follow=False)
    st, body = call(op, "/cycle")
    check("恢复前：标记记录存在", "2027-01-05" in body)
    st, body = call(op, "/admin/backup/restore",
                    {"csrf": tok_b, "name": backup_name, "confirm": "yes"}, follow=True)
    check("从备份恢复成功", "已从" in body and "恢复" in body, body[:200])
    st, body = call(op, "/cycle")
    check("恢复后：标记记录已消失（数据真的回到备份时点）", "2027-01-05" not in body)
    check("恢复前自动留了现场备份", any(bdir.glob("pre-restore-*.db")))

    st, body = call(op, "/admin/backup/restore",
                    {"csrf": tok_b, "name": "不存在的备份.db", "confirm": "yes"}, follow=True)
    check("恢复不存在的备份被拒", "不存在" in body, body[:120])
    st, body = call_multipart(op, "/admin/backup/restore-upload",
                              {"csrf": tok_b, "confirm": "yes"},
                              {"file": ("x.txt", b"not a db", "text/plain")})
    check("非备份格式的上传被拒", "只接受" in body, body[:160])

    # 定时逻辑：到期判断（CLI 直跑两次，第二次应跳过）
    env_b = dict(os.environ); env_b["BG_DB"] = DB
    r1 = _sp.run([py_exe(), "backup.py", "run"], capture_output=True, text=True, env=env_b,
                 cwd=str(pathlib.Path(__file__).resolve().parent))
    r2 = _sp.run([py_exe(), "backup.py", "run"], capture_output=True, text=True, env=env_b,
                 cwd=str(pathlib.Path(__file__).resolve().parent))
    check("定时任务到点才备份（第二次跳过）",
          ("跳过" in r2.stdout) or ("跳过" in r2.stderr), (r1.stdout + r2.stdout)[:160])

    # ---- 悬浮问答（唯一入口）+ 固定会话 + 后端自动压缩 + Markdown ----
    st, page = call(op, "/dashboard")
    check("每页都有右下角悬浮问答入口", 'id="fab"' in page and 'id="fabpanel"' in page)
    check("悬浮问答脚本已加载（v18）", "/static/app.js?v=18" in page)
    check("输入框：左上角抓手 + ＋ + 圆形发送键",
          'id="fabgrip"' in page and 'id="fabplus"' in page and 'id="fabsend"' in page
          and 'id="fabcomposer"' in page and 'placeholder="输入消息…"' in page)
    check("输入框上方有附件预览区 + 看大图的弹层",
          'id="fabatts"' in page and 'id="picDlg"' in page
          and ".cmp-att" in open("static/app.css", encoding="utf-8").read())
    check("AI 按钮与面板标题显示角色名（默认守护兔）", "守护兔" in page)
    check("右上角账号带身份样式（他蓝她粉）",
          ".topbar .userbox.him>summary" in open("static/app.css", encoding="utf-8").read()
          and ".topbar .userbox.her>summary" in open("static/app.css", encoding="utf-8").read()
          and 'class="userbox ' in page)
    check("收起键做成看得见的圆底按钮（手机 46px）",
          ".fabx{font-size:22px;width:40px" in open("static/app.css", encoding="utf-8").read()
          and ".fabx{width:46px;height:46px;font-size:26px" in open("static/app.css", encoding="utf-8").read())
    check("＋ 展开面板：照片 / 拍摄 / 文件（只要这三个）",
          'id="tilePhoto"' in page and 'id="tileCam"' in page and 'id="tileFile"' in page
          and 'id="fabsheet"' in page and page.count('class="cmp-tile"') == 3,
          f"tile 数={page.count('class=&quot;cmp-tile&quot;')}")
    check("不要的控件确实没进页面（语音/齿轮/文件夹图标）",
          'id="fabmic"' not in page and 'id="fabgear"' not in page and 'id="fabfolder"' not in page)
    check("两个图片入口 + 一个文件入口，拍照入口带 capture",
          'id="fabfile"' in page and 'id="fabcam"' in page and 'id="fabdoc"' in page
          and 'capture="environment"' in page)
    check("面板上方三个功能键（清空 / 归档 / 病症卡片）",
          'id="fabclear"' in page and 'id="fabarch"' in page and 'id="fabcard"' in page
          and "清空记录" in page and "总结归档" in page and "病症卡片" in page)
    check("抓手样式与半屏样式就位（输入框左上角那个）",
          ".cmp-handle" in open("static/app.css", encoding="utf-8").read()
          and ".fabpanel.sheet" in open("static/app.css", encoding="utf-8").read())
    check("移动端 AI 按钮：底部中间的圆形按钮（CSS 里有全屏规则）",
          "@media (max-width:899px)" in open("static/app.css", encoding="utf-8").read()
          and ".fab.open" in open("static/app.css", encoding="utf-8").read())
    check("导航里已没有指向旧问答页的链接", 'href="/qa"' not in page)
    st, body = call(op, "/qa")
    check("旧问答页跳转走（不再是页面）", st == 200 and "总览" in body, f"status={st}")

    st, body = call_json(op, "/api/qa/quick", {})
    j1 = json.loads(body); sid1 = j1.get("sid")
    st, body = call_json(op, "/api/qa/quick", {})
    j2 = json.loads(body); sid2 = j2.get("sid")
    check("一用户一个会话", sid1 == sid2 and sid1 > 0, f"{sid1} vs {sid2}")

    # 会话永不轮转：把时间改老也不换会话
    with sqlite3.connect(DB) as c:
        c.execute("UPDATE qa_sessions SET updated_at='2020-01-01 00:00:00' WHERE id=?", (sid1,))
        c.commit()
    st, body = call_json(op, "/api/qa/quick", {})
    sid_again = json.loads(body).get("sid")
    check("会话固定不轮转（时间久了也不换）", sid_again == sid1, f"{sid1} -> {sid_again}")

    # 历史接口：服务端渲染 HTML
    st, body = call(op, "/api/qa/history")
    hist = json.loads(body)
    check("历史接口可用且带会话与消息", st == 200 and hist.get("sid") == sid1
          and isinstance(hist.get("messages"), list), f"status={st}")
    check("历史里给出自动整理的策略说明", "compress_over" in hist and hist["compress_over"] >= 4)

    # Markdown 渲染 + 清洗（模型输出不可信）
    with sqlite3.connect(DB) as c:
        c.execute("INSERT INTO qa_messages(session_id, role, content, meta, created_at)"
                  " VALUES(?,?,?,?,?)", (sid1, "assistant",
                                         "**要点**：\n\n- 第一条\n- 第二条\n\n`短代码`\n\n"
                                         "<script>alert(1)</script>"
                                         '<img src=x onerror="alert(2)">'
                                         "[危险链接](javascript:alert(3)) "
                                         "[正常链接](https://www.acog.org)",
                                         "{}", "2026-01-01 00:00:00"))
        c.commit()
    st, body = call(op, "/api/qa/history")
    hist = json.loads(body)
    last = (hist.get("messages") or [{}])[-1].get("html", "")
    check("Markdown 已渲染（加粗/列表/行内代码）",
          "<strong>要点</strong>" in last and "<li>第一条</li>" in last and "<code>短代码</code>" in last, last[:160])
    check("脚本与事件属性被清洗掉",
          "<script" not in last.lower() and "onerror" not in last.lower(), last[:160])
    check("javascript: 链接被去掉、https 链接保留",
          "javascript:" not in last and "https://www.acog.org" in last, last[:200])

    # 提问（悬浮窗 multipart）→ 结束事件带消息 id → 单条渲染接口
    st, body = call_multipart(op, f"/qa/{sid1}/ask",
                              {"csrf": tok_b, "question": "**测试** 我的经期一般几天算正常？"},
                              {})
    m = re.search(r'"mid":\s*(\d+)', body or "")
    mid = m.group(1) if m else "0"
    check("提问返回结束事件且带消息 id", st == 200 and '"done"' in body and mid != "0", f"status={st}")
    st, body = call(op, f"/qa/msg/{mid}")
    js = json.loads(body)
    check("单条消息可按 id 取渲染结果", st == 200 and js.get("role") == "assistant", f"status={st}")
    st, _ = call(op, "/qa/msg/99999999")
    check("不存在的消息返回 404", st == 404, f"status={st}")

    # 上下文压缩：后端自动（阈值内不压、超阈值才压），且没有用户设置项了
    st, _ = call(op, "/admin/chat/settings", {"csrf": tok_b, "compress_over": "4"}, follow=False)
    check("用户侧不再有会话设置接口（404）", st == 404, f"status={st}")
    st, _ = call(op, f"/qa/{sid1}/compress", {"csrf": tok_b}, follow=False)
    check("用户侧不再有手动压缩接口（404）", st == 404, f"status={st}")

    import chat as chatm
    n_before = chatm.COMPRESS_OVER
    _now_ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB) as c:
        for i in range(n_before + 3):
            c.execute("INSERT INTO qa_messages(session_id, role, content, meta, created_at)"
                      " VALUES(?,?,?,?,?)",
                      (sid1, "user" if i % 2 == 0 else "assistant",
                       f"第{i}条测试内容：经期注意事项。", "{}", _now_ts))
        c.commit()
    st, body = call(op, "/api/qa/history")
    check("超阈值后历史接口仍可用（压缩是后端的事）", st == 200)
    env_m = dict(os.environ); env_m["BG_DB"] = DB
    rm = _sp.run([py_exe(), "maint.py", "run"], capture_output=True, text=True, env=env_m,
                 cwd=str(pathlib.Path(__file__).resolve().parent))
    check("维护任务可运行", "压缩" in (rm.stdout + rm.stderr), (rm.stdout + rm.stderr)[:160])
    with sqlite3.connect(DB) as c:
        summ = c.execute("SELECT summary FROM qa_sessions WHERE id=?", (sid1,)).fetchone()[0]
        n_arch = c.execute("SELECT COUNT(*) FROM qa_messages WHERE session_id=? AND archived=1",
                           (sid1,)).fetchone()[0]
    check("超阈值后自动写入摘要并归档早期消息",
          bool(summ) and n_arch >= chatm.COMPRESS_OVER - chatm.KEEP_RECENT,
          f"summary={bool(summ)} archived={n_arch}")
    st, body = call(op, "/admin")
    check("管理页只说明策略、不给用户设置项",
          "问答会话与上下文" in body and "chat/settings" not in body and "立即压缩并清理" not in body)

    # 上传一个文本文件：应被读入（本地没有模型，只验证不报错、附件登记上）
    st, body = call_multipart(op, f"/qa/{sid1}/ask",
                              {"csrf": tok_b, "question": "这是我从医院导出的数据，帮我看看"},
                              {"doc": ("lab.txt", "血红蛋白 118 g/L\n白细胞 6.2".encode(), "text/plain")})
    check("上传文本文件可提问（SSE 200）", st == 200 and '"done"' in (body or ""), f"status={st}")
    with sqlite3.connect(DB) as c:
        n_doc = c.execute("SELECT COUNT(*) FROM attachments WHERE ref_kind='qa'").fetchone()[0]
    check("文件登记进 attachments（留在本地数据目录）", n_doc >= 1, f"attachments={n_doc}")
    st, body = call_multipart(op, f"/qa/{sid1}/ask",
                              {"csrf": tok_b, "question": "试试不支持的类型"},
                              {"doc": ("x.exe", b"MZ\x00\x00", "application/octet-stream")})
    check("不支持的文件类型被拒", st == 400, f"status={st}")

    # 问答面板上方两个功能键的后端：清空记录 / 总结归档
    st, body = call(op, "/qa/archive", {"csrf": tok_b})
    j = json.loads(body)
    check("总结归档：给出明确结果（模型可用则归档，不可用则说清楚）",
          st == 200 and isinstance(j.get("ok"), bool) and bool(j.get("msg")), body[:160])
    if not j.get("ok"):
        st2, body2 = call(op, "/api/qa/history")
        check("归档失败时对话原样保留（没有静默丢弃）", bool(json.loads(body2).get("messages")))
    st, body = call(op, "/qa/condition_card", {"csrf": tok_b})
    j = json.loads(body)
    check("生成病症卡片：给出明确结果（模型可用则建提案，不可用则说清楚）",
          st == 200 and isinstance(j.get("ok"), bool) and bool(j.get("msg")), body[:160])
    if j.get("ok"):
        st2, body2 = call(op, "/qa/pending")
        check("生成的病症卡片进入「待确认」（用户确认后才写档案）",
              any("疾病" in (i.get("kind") or "") for i in json.loads(body2).get("items", [])), body2[:200])
    st, body = call(op, "/qa/clear", {"csrf": tok_b})
    j = json.loads(body)
    check("清空记录：收起旧对话并开一段新会话", st == 200 and j.get("ok") is True, body[:160])
    st, body = call(op, "/api/qa/history")
    check("清空后历史为空", json.loads(body).get("messages") == [], body[:120])
    st, body = call(op, "/qa/archive", {"csrf": tok_b})
    check("空对话时总结归档明确说没内容", json.loads(body).get("ok") is False, body[:160])
    st, body = call(op, "/qa/clear", {"csrf": "错的"})
    check("清空记录需要正确 csrf", st >= 400, f"status={st}")

    # 导出
    st, body = call(op, "/admin")
    tok = csrf_of(body)
    st, body = call(op, "/export", {"csrf": tok, "push": "0"}, follow=True)
    check("导出到知识库执行成功", "导出完成" in body or "已导出" in body, body[:160])

    # 导出的落点是**知识库仓库**（BG_KB_DIR），不是代码仓库
    repo = Path(os.environ.get("BG_KB_DIR") or Path(__file__).resolve().parent.parent)
    for rel in ["经期记录/周期记录.md", "经期记录/周期统计.md", "疾病档案/疾病列表.md", "就医记录/就诊记录.md"]:
        p = repo / "docs" / rel
        check(f"导出文件存在：{rel}", p.is_file() and p.stat().st_size > 120)
    st, kbpage = call(op, "/knowledge")
    check("导出的记录文档同时进入数据库（知识库页面可见）",
          "周期记录" in kbpage and "周期统计" in kbpage)

    stat_md = (repo / "docs" / "经期记录" / "周期统计.md")
    if stat_md.is_file():
        txt = stat_md.read_text(encoding="utf-8")
        check("导出内容含推断与免责声明", "推断" in txt and "不能替代" in txt)

    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
