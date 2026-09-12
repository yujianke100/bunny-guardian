"""内联 SVG 小图表：无 JS、无外部依赖，服务端生成后直接嵌进页面。

只做一件事：把一串按日期排列的数值画成折线，用于体重趋势。
数值与日期都是程序算出来的（不是用户输入），所以拼接进 SVG 是安全的；
即便将来接用户文本，也只允许进 <text> 前先 html.escape。
"""
from __future__ import annotations

from datetime import date

W, H = 320.0, 112.0
PAD_L, PAD_R, PAD_T, PAD_B = 8.0, 34.0, 14.0, 18.0


def _ord(iso: str) -> int:
    try:
        return date.fromisoformat(iso[:10]).toordinal()
    except ValueError:
        return 0


def bars_svg(values: list[int], color: str = "#7ba7e6", width: float = 150.0,
             height: float = 44.0, highlight_last: bool = True) -> str:
    """把一串整数画成竖条（如最近几次经期天数）。数量少于 2 返回空串。"""
    vals = [int(v) for v in values if v]
    if len(vals) < 2:
        return ""
    n = len(vals)
    top = max(vals) or 1
    gap = 3.0
    bw = max(4.0, (width - gap * (n - 1)) / n)
    bars = []
    for i, v in enumerate(vals):
        h = max(4.0, (v / top) * height)
        x = i * (bw + gap)
        fill = color if (not highlight_last or i < n - 1) else "#b5486a"
        bars.append(f'<rect x="{x:.1f}" y="{height - h:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                    f'rx="{min(3.0, bw / 2):.1f}" fill="{fill}" opacity="{0.55 + 0.45 * v / top:.2f}"/>')
    return (f'<svg class="barsvg" viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
            f'aria-label="最近 {n} 次：{vals}">{"".join(bars)}</svg>')


def hormone_svg(cycle_len: int, period_len: int, cycle_day: int | None = None,
                width: float = 320.0, height: float = 150.0) -> str:
    """激素变化**示意图**（教科书模型：雌激素、LH、孕激素随周期的典型走向）。

    重要：这是示意图，不是她的实测值——本系统不测激素。图上明确标注，
    阶段划分按（周期长度, 经期天数）画。返回内联 SVG。
    """
    cl = max(15, int(cycle_len or 28))
    pl = max(1, int(period_len or 5))
    # 关键点（按周期比例）：排卵约在 周期-14 天处
    ovu = max(pl + 2, cl - 14)
    w, h = width, height
    pad_l, pad_r, pad_t, pad_b = 6.0, 30.0, 10.0, 34.0
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(day: float) -> float:
        return pad_l + (day - 1) / max(1, cl - 1) * iw

    def Y(v: float) -> float:                      # v: 0..1
        return pad_t + (1 - v) * ih

    def curve(points: list[tuple[float, float]], color: str, dash: str = "") -> str:
        d = " ".join(("M" if i == 0 else "L") + f"{X(x):.1f} {Y(y):.1f}"
                     for i, (x, y) in enumerate(points))
        da = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round"{da}/>'

    # 雌激素：卵泡期升、排卵前峰、黄体期次峰
    est = [(1, .12), (pl, .18), (ovu - 3, .55), (ovu - 1, .92), (ovu + 1, .7),
           (ovu + 4, .62), (cl - 3, .45), (cl, .3)]
    # LH：排卵前陡峰
    lh = [(1, .1), (pl, .12), (ovu - 4, .2), (ovu - 2, .35), (ovu - 1, .95),
          (ovu, .7), (ovu + 2, .22), (cl, .12)]
    # 孕激素：排卵后上升，黄体中期峰
    prog = [(1, .06), (pl, .07), (ovu - 2, .08), (ovu, .1), (ovu + 3, .5),
            (ovu + 7, .85), (cl - 5, .6), (cl, .25)]
    bands = [
        (1, pl, "#ef7091", "月经期"), (pl + 0.001, ovu - 1, "#7ba7e6", "卵泡期"),
        (ovu - 1, ovu + 1, "#8f6fd8", "排卵期"), (ovu + 1.001, cl, "#e5b25c", "黄体期")]
    band_svg, band_tx = "", ""
    for x0, x1, c, name in bands:
        if x1 <= x0:
            continue
        band_svg += (f'<rect x="{X(x0):.1f}" y="{pad_t}" width="{max(1.0, X(x1) - X(x0)):.1f}" '
                     f'height="{ih:.1f}" fill="{c}" opacity="0.10"/>')
        tx = max(pad_l + 2, min(X((x0 + x1) / 2) - 12, w - pad_r - 26))
        band_tx += (f'<text x="{tx:.1f}" y="{h - pad_b + 11:.0f}" font-size="8.5" fill="var(--chart-text)">'
                    f'{name}</text>')
    today = ""
    if cycle_day:
        d = max(1, min(cl, int(cycle_day)))
        today = (f'<line x1="{X(d):.1f}" y1="{pad_t}" x2="{X(d):.1f}" y2="{pad_t + ih:.1f}" '
                 f'stroke="var(--marker-ink)" stroke-width="1" stroke-dasharray="3 3" opacity="0.6"/>'
                 f'<circle cx="{X(d):.1f}" cy="{pad_t + 2:.1f}" r="2.4" fill="var(--marker-ink)"/>')
    legend = "".join(
        f'<g transform="translate({pad_l + i * 74:.0f},{pad_t + 2:.0f})">'
        f'<line x1="0" y1="8" x2="14" y2="8" stroke="{c}" stroke-width="2"{da}/>'
        f'<text x="18" y="11" font-size="8.5" fill="var(--chart-text)">{name}</text></g>'
        for i, (name, c, da) in enumerate([("雌激素", "#ef7091", ""), ("LH", "#8f6fd8", ""),
                                           ("孕激素", "#e5b25c", "3 2")]))
    return (f'<svg class="chart hormone" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
            f'aria-label="激素变化示意图（教科书模型，不是实测值）">'
            f'{band_svg}{curve(est, "#ef7091")}{curve(lh, "#8f6fd8")}'
            f'{curve(prog, "#e5b25c", "3 2")}{today}{band_tx}{legend}</svg>')

def line_svg(points: list[tuple], color: str = "#b5486a",
             unit: str = "kg", dots: bool = True) -> str:
    """points: [(iso_date, value), ...]（按日期升序）。少于两个点返回空串。"""
    pts = [(d, float(v)) for d, v in points if d and v is not None]
    if len(pts) < 2:
        return ""
    xs = [_ord(d) for d, _ in pts]
    ys = [v for _, v in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 - y0 < 0.6:                     # 避免一条完全水平的线看不出起伏
        y0, y1 = y0 - 0.3, y1 + 0.3

    def sx(x: int) -> float:
        return PAD_L + (x - x0) / (x1 - x0) * (W - PAD_L - PAD_R)

    def sy(y: float) -> float:
        return H - PAD_B - (y - y0) / (y1 - y0) * (H - PAD_T - PAD_B)

    coords = [(sx(x), sy(y)) for x, y in zip(xs, ys)]
    path = " ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
    grid = "".join(
        f'<line x1="{PAD_L}" y1="{sy(y0 + (y1 - y0) * k / 3):.1f}" '
        f'x2="{W - PAD_R}" y2="{sy(y0 + (y1 - y0) * k / 3):.1f}" '
        f'stroke="var(--chart-line)" stroke-width="1"/>' for k in (0, 1, 2, 3))
    marks = ""
    if dots:
        marks = "".join(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="2.6" fill="var(--card)" '
                        f'stroke="{color}" stroke-width="2"/>' for px, py in coords)
    lo_i = ys.index(min(ys))
    hi_i = ys.index(max(ys))
    labels = (
        f'<text x="{W - PAD_R + 4:.0f}" y="{sy(max(ys)) + 3:.1f}" font-size="9" fill="var(--chart-text)">{max(ys):g}</text>'
        f'<text x="{W - PAD_R + 4:.0f}" y="{sy(min(ys)) + 3:.1f}" font-size="9" fill="var(--chart-text)">{min(ys):g}</text>'
        f'<text x="{PAD_L}" y="{H - 5:.0f}" font-size="9" fill="var(--chart-text)">{pts[0][0][5:]}</text>'
        f'<text x="{W - PAD_R}" y="{H - 5:.0f}" font-size="9" fill="var(--chart-text)" text-anchor="end">'
        f'{pts[-1][0][5:]}</text>')
    return (f'<svg class="chart" viewBox="0 0 {W:.0f} {H:.0f}" role="img" '
            f'aria-label="体重趋势折线图，从 {pts[0][1]:g}{unit} 到 {pts[-1][1]:g}{unit}">'
            f'{grid}<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"/>{marks}{labels}</svg>')


def fertility_svg(cycle_len: float, period_len: float, cycle_day: int | None = None,
                  ovu_day: int | None = None, start_iso: str = "", next_iso: str = "",
                  width: float = 360.0, height: float = 252.0) -> str:
    """受孕率折线图（版式参照主流经期 App）：

    x 轴固定为「本次月经开始 → 下一次月经开始前」，只标 4 个日期（经期结束、易孕窗口开始、
    排卵日、下次经期）；y 轴 0–40%；曲线按阶段分色（与周期环同一套颜色），易孕窗口内填淡色，
    排卵日画六边形，今天画空心圆并用气泡标出当天受孕率。

    数值：fertility.DAY_RATES（Wilcox 1995 NEJM 表 1）；六天窗口外为 0。
    这是群体数据，不是对个人的预测。
    """
    import fertility as fertm
    import cycle as cyclem                                   # 阶段色与阶段划分都用同一份
    from datetime import date as _date, timedelta as _td
    cl = max(15, int(round(cycle_len or 28)))
    pl = max(1, min(cl, int(round(period_len or 5))))
    ovu = max(1, min(cl, int(ovu_day or max(1, cl - 14))))
    w, h = width, height
    pad_l, pad_r, pad_t, pad_b = 36.0, 14.0, 28.0, 44.0
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
    ymax = 40.0
    C = cyclem.RING_COLORS

    def X(day: float) -> float:
        return pad_l + (day - 1) / max(1.0, cl - 1) * iw

    def Y(pct: float) -> float:
        return pad_t + (1 - min(pct, ymax) / ymax) * ih

    def D(day: int) -> str:                                  # 日期标签：8.13 这种写法
        if not start_iso:
            return ""
        try:
            d = _date.fromisoformat(str(start_iso)[:10]) + _td(days=int(day) - 1)
        except ValueError:
            return ""
        return f"{d.month}.{d.day}"

    vals = {d: fertm.DAY_RATES.get(d - ovu, 0) for d in range(1, cl + 1)}
    P = [(X(d), Y(vals[d])) for d in range(1, cl + 1)]

    # 平滑曲线：均匀 Catmull-Rom 转三次贝塞尔；控制点 y 夹在 0..ymax，避免冲过 0 线
    segs: list[tuple[str, float, str, tuple, tuple]] = []
    for i in range(len(P) - 1):
        p0 = P[i - 1] if i > 0 else P[i]
        p1, p2 = P[i], P[i + 1]
        p3 = P[i + 2] if i + 2 < len(P) else P[i + 1]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, min(max(p1[1] + (p2[1] - p0[1]) / 6.0, Y(ymax)), Y(0)))
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, min(max(p2[1] - (p3[1] - p1[1]) / 6.0, Y(ymax)), Y(0)))
        mid = (i + 1 + i + 2) / 2.0
        key = cyclem.phase_of(int(round(mid)), cl, pl)[0]
        segs.append((key, mid,
                     f"C{c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} {p2[0]:.1f} {p2[1]:.1f}",
                     p1, p2))
    groups: list[list] = []
    for key, mid, seg, p1, p2 in segs:
        if groups and groups[-1][0] == key:
            groups[-1][1].append(seg)
        else:
            groups.append([key, [seg], p1])
    curve = "".join(
        f'<path d="M{g[2][0]:.1f} {g[2][1]:.1f} {" ".join(g[1])}" fill="none" '
        f'stroke="{C.get(g[0], C["unknown"])}" stroke-width="2.6" stroke-linecap="round"/>'
        for g in groups)
    # 易孕窗口内的面积填充（淡青）
    win = [t for t in segs if ovu - 5 <= t[1] <= ovu + 0.6]
    area = ""
    if win:
        first, last = win[0][3], win[-1][4]
        area = (f'<path d="M{first[0]:.1f} {Y(0):.1f} L{first[0]:.1f} {first[1]:.1f} '
                f'{" ".join(t[2] for t in win)} L{last[0]:.1f} {Y(0):.1f} Z" '
                f'fill="#6fcfe4" opacity="0.30"/>')

    grid = ""
    for pct in range(0, 41, 10):
        grid += (f'<line x1="{pad_l}" y1="{Y(pct):.1f}" x2="{w - pad_r:.1f}" y2="{Y(pct):.1f}" '
                 f'stroke="#f7dde7" stroke-width="1" stroke-dasharray="3 4"/>')
        grid += (f'<text x="{pad_l - 8:.0f}" y="{Y(pct) + 3.4:.1f}" font-size="10" fill="var(--chart-text-2)" '
                 f'text-anchor="end">{pct}</text>')
    axis_title = (f'<text x="{pad_l - 30:.0f}" y="12" font-size="10" fill="var(--chart-text-2)">受孕概率</text>'
                  f'<text x="{pad_l - 30:.0f}" y="23" font-size="10" fill="var(--chart-text-2)">(%)</text>'
                  f'<text x="{pad_l - 30:.0f}" y="{h - 8:.0f}" font-size="10" fill="var(--chart-text-2)">日期</text>')
    # 下次经期：右侧虚线
    vline = (f'<line x1="{X(cl):.1f}" y1="{pad_t - 6:.1f}" x2="{X(cl):.1f}" y2="{Y(0):.1f}" '
             f'stroke="#f3b8cd" stroke-width="1" stroke-dasharray="3 3"/>')
    xticks = ""
    for day, anchor in ((pl, "middle"), (max(1, ovu - 5), "middle"), (ovu, "middle"), (cl, "end")):
        lab = D(day)
        if lab:
            x = X(day) if anchor != "end" else min(X(day), w - pad_r + 6)
            xticks += (f'<text x="{x:.1f}" y="{h - 26:.0f}" font-size="10" fill="var(--chart-text-2)" '
                       f'text-anchor="{anchor}">{lab}</text>')
    ovu_mark = (f'<polygon points="{_pentagon(X(ovu), Y(vals[ovu]), 6.6)}" fill="{C["ovulation"]}" '
                f'stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/>')
    today = ""
    if cycle_day:
        cd = max(1, min(cl, int(cycle_day)))
        cx, cy, pct = X(cd), Y(vals[cd]), vals[cd]
        key = cyclem.phase_of(cd, cl, pl)[0]
        today = (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5.4" fill="var(--card)" '
                 f'stroke="{C.get(key, C["unknown"])}" stroke-width="2.6"/>')
        bw, bh = 92.0, 44.0
        bx = min(max(cx - bw / 2, pad_l), w - pad_r - bw)
        by = min(max(cy - 16 - bh, 2.0), Y(0) - bh - 4)
        tail = f"{cx:.1f},{by + bh:.1f} {cx - 5:.1f},{by + bh - 5:.1f} {cx + 5:.1f},{by + bh - 5:.1f}"
        today += (f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.0f}" height="{bh:.0f}" rx="10" '
                  f'fill="url(#fert-bub)"/>'
                  f'<polygon points="{tail}" fill="#8d7de8"/>'
                  f'<text x="{bx + bw / 2:.1f}" y="{by + 17:.1f}" font-size="10.5" fill="var(--card)" '
                  f'text-anchor="middle" opacity="0.92">今日受孕率</text>'
                  f'<text x="{bx + bw / 2:.1f}" y="{by + 36:.1f}" font-size="17" font-weight="700" '
                  f'fill="var(--card)" text-anchor="middle">{pct}%</text>')
    return (f'<svg class="chart fertility" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
            f'aria-label="本周期每日受孕概率（群体数据，不是个人预测）">'
            f'<defs><linearGradient id="fert-bub" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#a99bf0"/><stop offset="1" stop-color="#8d7de8"/>'
            f'</linearGradient></defs>'
            f'{grid}{axis_title}{vline}{area}{curve}{ovu_mark}{today}{xticks}</svg>')


def _pentagon(cx: float, cy: float, r: float) -> str:
    """正五边形（排卵日标记）：顶点朝上。"""
    import math as _m
    pts = []
    for i in range(5):
        a = _m.radians(-90 + i * 72)
        pts.append(f"{cx + r * _m.cos(a):.1f},{cy + r * _m.sin(a):.1f}")
    return " ".join(pts)


def bmi_gauge(bmi: float | None, width: float = 320.0, lo: float = 15.0, hi: float = 35.0) -> str:
    """BMI 刻度条：按中国标准（WS/T 428—2013）分四段，指针标出当前值落在哪儿。

    只画刻度与落点，不下判断；等级文案由 bmi.classify() 给（同一份切点）。
    版式：一条两端圆角的色带（四段拼成）+ 上方指针与数值 + 下方切点刻度。
    渲染尺寸由 CSS 限宽（.bmi-gauge svg），否则在桌面宽屏会被拉大好几倍。
    """
    if not bmi:
        return ""
    bands = ((18.5, "#7ba7e6"), (24.0, "#63c2a2"), (28.0, "#e5b25c"), (hi + 0.999, "#ef7091"))
    top, bar_y, bar_h = 16.0, 26.0, 13.0
    bottom = bar_y + bar_h

    def x(v: float) -> float:
        v = min(max(v, lo), hi)
        return (v - lo) / (hi - lo) * width

    parts = [f'<clipPath id="bmi-clip"><rect x="0" y="{bar_y:.0f}" width="{width:.0f}" '
             f'height="{bar_h:.0f}" rx="{bar_h / 2:.1f}"/></clipPath>']
    band_svg, prev = [], lo
    for up, color in bands:
        x0, x1 = x(prev), x(up)
        band_svg.append(f'<rect class="band" x="{x0:.1f}" y="{bar_y:.0f}" '
                        f'width="{max(0.0, x1 - x0):.1f}" height="{bar_h:.0f}" fill="{color}"/>')
        prev = up
    parts.append(f'<g clip-path="url(#bmi-clip)">{"".join(band_svg)}</g>')
    for v in (18.5, 24.0, 28.0):                        # 切点刻度
        parts.append(f'<line x1="{x(v):.1f}" y1="{bottom:.0f}" x2="{x(v):.1f}" y2="{bottom + 4:.0f}" '
                     f'stroke="#d8cfd4" stroke-width="1"/>'
                     f'<text x="{x(v):.1f}" y="{bottom + 15:.0f}" font-size="9" fill="#8b7d84" '
                     f'text-anchor="middle">{v:g}</text>')
    mx = x(bmi)
    tx = min(max(mx, 15.0), width - 15.0)               # 数值别贴到边上
    parts.append(
        f'<line x1="{mx:.1f}" y1="{bar_y:.0f}" x2="{mx:.1f}" y2="{bottom:.0f}" '
        f'stroke="#ffffff" stroke-width="1.6" opacity="0.9"/>'
        f'<polygon points="{mx:.1f},{bar_y:.0f} {mx - 4.6:.1f},{top:.0f} {mx + 4.6:.1f},{top:.0f}" '
        f'fill="#332a2e"/>'
        f'<text x="{tx:.1f}" y="{top - 5:.0f}" font-size="11" font-weight="700" fill="#332a2e" '
        f'text-anchor="middle">{bmi:g}</text>')
    return (f'<svg class="bmigauge" viewBox="0 0 {width:.0f} {bottom + 20:.0f}" role="img" '
            f'aria-label="BMI 刻度：当前 {bmi:g}，正常范围 18.5–24">{"".join(parts)}</svg>')
