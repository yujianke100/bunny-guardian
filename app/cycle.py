"""周期统计与阶段推断。

方法学说明（重要）
- 预测完全基于**历史记录的长度统计**（中位数为主，同时给出最小/最大值区间），
  属于统计推断，不是医学检测结果。
- 生理学依据：黄体期相对稳定（约 12–16 天），因此排卵日通常可用
  「预计下次月经日 − 14 天」估算；该估算在周期不规律时误差很大。
  来源见 docs/医学知识/月经周期生理与正常范围.md。
- 本模块的输出在界面上必须标注「推断」并附带免责声明。
"""
from __future__ import annotations

import math
import statistics
from datetime import date, timedelta

DEFAULT_CYCLE = 28
DEFAULT_PERIOD = 5
LUTEAL_DAYS = 14        # 黄体期近似长度（来源：黄体期相对稳定 12–16 天）
MIN_NORMAL, MAX_NORMAL = 21, 35   # 常见正常周期范围（来源：ACOG）

PHASES = [
    ("menstrual", "月经期"),
    ("follicular", "卵泡期"),
    ("ovulation", "排卵期"),
    ("luteal", "黄体期"),
    ("unknown", "未知"),
]


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def stats(starts: list[date], durations: list[int]) -> dict:
    """汇总历史周期数据。starts 需升序。"""
    starts = sorted(starts)
    lengths = [(b - a).days for a, b in zip(starts, starts[1:])]
    lengths = [x for x in lengths if 10 <= x <= 120]
    recent = lengths[-6:]
    out = {
        "n_cycles": len(starts),
        "n_lengths": len(lengths),
        "lengths": lengths,
        "recent_lengths": recent,
        "mean": round(statistics.mean(recent), 1) if recent else None,
        "median": round(_median(recent), 1) if recent else None,
        "min": min(recent) if recent else None,
        "max": max(recent) if recent else None,
        "stdev": round(statistics.pstdev(recent), 1) if len(recent) >= 2 else None,
        "last_start": starts[-1] if starts else None,
        "period_len_mean": round(statistics.mean(durations), 1) if durations else None,
        "period_len_median": round(_median(durations), 1) if durations else None,
    }
    return out


def _confidence(s: dict) -> tuple[str, str]:
    n, sd = s["n_lengths"], s["stdev"]
    if n >= 3 and sd is not None and sd <= 2.5:
        return "较高", f"近 {len(s['recent_lengths'])} 个周期波动较小（标准差 {sd} 天）"
    if n >= 2 and sd is not None and sd <= 4.0:
        return "中等", f"近 {len(s['recent_lengths'])} 个周期存在一定波动（标准差 {sd} 天）"
    if n >= 1:
        return "偏低", "记录周期数较少或波动较大，推断误差可能达到数天以上"
    return "无", "暂无足够记录，以下按 28 天默认值演示"


def _irregular_flags(s: dict) -> list[str]:
    flags = []
    if s["min"] is not None and s["min"] < MIN_NORMAL:
        flags.append(f"存在短于 {MIN_NORMAL} 天的周期（最短 {s['min']} 天）")
    if s["max"] is not None and s["max"] > MAX_NORMAL:
        flags.append(f"存在长于 {MAX_NORMAL} 天的周期（最长 {s['max']} 天）")
    if s["stdev"] is not None and s["stdev"] > 7:
        flags.append(f"周期长度波动较大（标准差 {s['stdev']} 天）")
    if s["period_len_mean"] is not None and s["period_len_mean"] > 7:
        flags.append(f"经期平均长度偏长（{s['period_len_mean']} 天）")
    return flags


def _norm(cycle_len: float, period_len: float) -> tuple[int, int]:
    return max(21, int(round(cycle_len))), max(2, min(9, int(round(period_len))))


def ovulation_day(cycle_len: float, period_len: float) -> int:
    """估算排卵日（周期第几天）：黄体期相对固定，故约为「周期长度 − 14」。"""
    cl, pl = _norm(cycle_len, period_len)
    return max(pl + 1, min(cl - 2, cl - LUTEAL_DAYS))


def phase_of(day: float, cycle_len: float, period_len: float) -> tuple[str, str]:
    """给定周期第几天，返回 (阶段 key, 中文名)。"""
    day = max(1, int(day))
    cycle_len, period_len = _norm(cycle_len, period_len)
    ovulation_day_ = max(period_len + 1, min(cycle_len - 2, cycle_len - LUTEAL_DAYS))
    if day <= period_len:
        return "menstrual", "月经期"
    if day <= ovulation_day_ - 2:
        return "follicular", "卵泡期"
    if day <= ovulation_day_ + 1:
        return "ovulation", "排卵期"
    if day <= cycle_len:
        return "luteal", "黄体期"
    return "unknown", "超出预计周期（经期可能推迟）"


def analyze(starts: list[date], durations: list[int], today: date | None = None) -> dict:
    """合成统计 + 推断结果，供界面与导出使用。"""
    today = today or date.today()
    s = stats(starts, durations)
    result: dict = {"stats": s, "has_data": bool(starts), "disclaimer": (
        "推断基于历史记录长度的统计，个体波动常见，不能用于避孕、备孕决策或疾病诊断；"
        "周期明显异常时请就医评估。")}

    if not starts:
        result.update({
            "cycle_len": DEFAULT_CYCLE, "period_len": DEFAULT_PERIOD, "cycle_day": None,
            "phase": "unknown", "phase_cn": "暂无记录",
            "next_start": None, "next_window": None, "fertile_window": None,
            "ovulation_date": None, "confidence": "无", "confidence_reason": "尚无经期记录",
            "irregular_flags": [], "example": True, "days_to_next": None,
        })
        return result

    last = s["last_start"]
    cycle_len = s["median"] or s["mean"] or DEFAULT_CYCLE
    period_len = s["period_len_median"] or s["period_len_mean"] or DEFAULT_PERIOD
    cycle_len = float(min(60, max(18, cycle_len)))
    period_len = float(min(10, max(2, period_len)))

    day = (today - last).days + 1
    phase_key, phase_cn = phase_of(day, cycle_len, period_len)
    next_start = last + timedelta(days=int(round(cycle_len)))
    lo = last + timedelta(days=int(round(cycle_len - (s["stdev"] or 3))))
    hi = last + timedelta(days=int(round(cycle_len + (s["stdev"] or 3))))
    ovu = next_start - timedelta(days=LUTEAL_DAYS)

    conf, reason = _confidence(s)
    result.update({
        "cycle_len": round(cycle_len, 1),
        "period_len": round(period_len, 1),
        "cycle_day": day if day >= 1 else None,
        "phase": phase_key, "phase_cn": phase_cn,
        "next_start": next_start, "next_window": (lo, hi),
        "days_to_next": (next_start - today).days,
        "ovulation_date": ovu,
        "fertile_window": (ovu - timedelta(days=5), ovu + timedelta(days=1)),
        "confidence": conf, "confidence_reason": reason,
        "irregular_flags": _irregular_flags(s),
        "example": False,
    })
    return result


def cycle_phase_table(cycle_len: float, period_len: float) -> list[dict]:
    """生成一个完整周期的阶段划分表（用于日历图例与导出）。"""
    cycle_len = max(21, int(round(cycle_len)))
    period_len = max(2, min(9, int(round(period_len))))
    rows, current = [], None
    for day in range(1, cycle_len + 1):
        key, cn = phase_of(day, cycle_len, period_len)
        if current is None or current["phase"] != key:
            current = {"phase": key, "phase_cn": cn, "start_day": day, "end_day": day}
            rows.append(current)
        else:
            current["end_day"] = day
    ovu = cycle_len - LUTEAL_DAYS
    for r in rows:
        r["days"] = f"第 {r['start_day']}–{r['end_day']} 天"
        r["note"] = {
            "menstrual": "出血期，注意经量与疼痛记录",
            "follicular": "卵泡发育，雌激素上升",
            "ovulation": f"估算排卵日约在周期第 {ovu} 天（排卵期含前后各 1 天）",
            "luteal": "黄体期，孕激素升高；经前症状多出现在本阶段后段",
        }.get(r["phase"], "")
    return rows


# ------------------------------------------------------------------ 周期环（钟表式）
#
# 环形图把「一个完整周期」摊成一圈：12 点方向是周期第 1 天，顺时针推进。
# 提供两套视角：
#   med   医学分期 —— 月经期 / 卵泡期 / 排卵期 / 黄体期
#   plain 通俗说法 —— 月经期 / 安全期 / 易孕期（俗称危险期）
# 两者都由同一份统计结果推出，仅切分方式与用词不同。

RING_VIEWS = {
    "med": {
        "label": "医学分期",
        "hint": "按激素变化划分的四个阶段，用于理解周期本身。",
    },
    "plain": {
        "label": "通俗说法",
        "hint": "日常说法。安全期的判断依赖日历推算，可靠性有限，不能作为避孕依据。",
    },
}
DEFAULT_VIEW = "med"

RING_COLORS = {
    "menstrual": "#ef7091",
    "follicular": "#7ba7e6",
    "ovulation": "#8f6fd8",       # 排卵期/排卵日：紫（与激素示意图里的 LH 峰同色）
    "luteal": "#e5b25c",
    "safe": "#7cc7a9",
    "fertile": "#f0a851",
    "unknown": "#c9c1c9",
}
RING_TRACK = "var(--ring-track)"          # 环底：分段之间的空隙露出它，读作「轨道」而不是「缺数据」
MED_LABELS = {"menstrual": "月经期", "follicular": "卵泡期", "ovulation": "排卵期", "luteal": "黄体期"}
PLAIN_LABELS = {"menstrual": "月经期", "safe": "安全期", "fertile": "易孕期（俗称危险期）"}
MED_NOTES = {
    "menstrual": "子宫内膜脱落出血，第 1 天即周期起点",
    "follicular": "卵泡发育、雌激素上升，内膜增厚",
    "ovulation": "估算排卵日前后，受精可能性最高的窗口",
    "luteal": "黄体形成、孕激素升高，经前症状多在此段后段",
}
PLAIN_NOTES = {
    "menstrual": "来月经的这几天",
    "safe": "按日历推算受精可能性较低的日子（推算，非保证）",
    "fertile": "估算的受精窗口：排卵日前 5 天至排卵日后 1 天",
}

R_C, R_OUT, R_IN = 120.0, 100.0, 80.0
R_MID = (R_OUT + R_IN) / 2.0                     # 描边中心线半径（模板按此画带圆头的环段）
RING_STROKE = R_OUT - R_IN                       # 环的粗细
# 每段两端各内缩的角度：让分段之间露出环底（读作「轨道上的珠子」）。
# 只影响观感，天↔角度的映射仍按真实天数 —— 指针与边界位置不受影响。
RING_GAP_DEG = 7.0
# 端帽是半圆（stroke-linecap=round），本身就会把每段收成弧形，所以内缩只要一点点：
#   可见缝隙 = 2 × (内缩弧长 − 端帽半径)，端帽半径 = 环粗/2 = 10
#   7° → 弧长 100×0.122 = 12.2 → 可见缝隙 ≈ 4.4，约 0.2×环粗（原来 12° 留出 ≈22，视觉上像被切开）
FERTILE_BEFORE, FERTILE_AFTER = 5, 1
OVU_R = 7.4                       # 排卵日六边形标记的外接圆半径
BADGE_R = 16.0                    # 「今天」白底圆牌半径（牌内写周期第几天）


def _pt(deg: float, r: float, cx: float = R_C, cy: float = R_C) -> tuple[float, float]:
    rad = math.radians(deg)
    return (cx + r * math.cos(rad), cy + r * math.sin(rad))


def _donut_path(a0: float, a1: float, r_out: float = R_OUT, r_in: float = R_IN) -> str:
    """生成一段圆环的 SVG 路径（角度制，0° 为 3 点方向，顺时针为正）。"""
    span = (a1 - a0) % 360
    large = 1 if span > 180 else 0
    x0, y0 = _pt(a0, r_out)
    x1, y1 = _pt(a1, r_out)
    x2, y2 = _pt(a1, r_in)
    x3, y3 = _pt(a0, r_in)
    return (f"M {x0:.2f} {y0:.2f} A {r_out} {r_out} 0 {large} 1 {x1:.2f} {y1:.2f} "
            f"L {x2:.2f} {y2:.2f} A {r_in} {r_in} 0 {large} 0 {x3:.2f} {y3:.2f} Z")


def _arc_path(a0: float, a1: float, r: float = R_MID) -> str:
    """一段圆弧（描边中心线）；端点圆不圆由模板的 stroke-linecap 决定。

    角度约定与 _donut_path 一致：0° 在 3 点方向、角度增大为顺时针。
    """
    x0, y0 = _pt(a0, r)
    x1, y1 = _pt(a1, r)
    large = 1 if abs(a1 - a0) > 180.0 else 0
    return (f"M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}")


def _segments(cycle_len: float, period_len: float, view: str) -> list[tuple[str, int, int]]:
    """把一个周期切成 (阶段 key, 起始天, 结束天) 序列，天数为闭区间且连续无缝。"""
    cl, pl = _norm(cycle_len, period_len)
    if view == "plain":
        ovu = ovulation_day(cl, pl)
        f0 = max(pl + 1, ovu - FERTILE_BEFORE)
        f1 = max(f0, min(cl, ovu + FERTILE_AFTER))
        segs: list[tuple[str, int, int]] = [("menstrual", 1, pl)]
        if f0 > pl + 1:
            segs.append(("safe", pl + 1, f0 - 1))
        segs.append(("fertile", f0, f1))
        if f1 < cl:
            segs.append(("safe", f1 + 1, cl))
        return segs
    segs = []
    for day in range(1, cl + 1):
        key, _ = phase_of(day, cl, pl)
        if segs and segs[-1][0] == key:
            segs[-1] = (key, segs[-1][1], day)
        else:
            segs.append((key, day, day))
    return segs


def plain_phase_of(day: int, cycle_len: float, period_len: float) -> str:
    cl, pl = _norm(cycle_len, period_len)
    ovu = ovulation_day(cl, pl)
    if day <= pl:
        return "menstrual"
    if ovu - FERTILE_BEFORE <= day <= ovu + FERTILE_AFTER:
        return "fertile"
    return "safe"


def ring(cycle_len: float, period_len: float, view: str = DEFAULT_VIEW,
         cycle_day: int | None = None, next_start: date | None = None,
         next_window: tuple[date, date] | None = None, confidence: str = "",
         example: bool = False) -> dict:
    """生成周期环的完整绘制数据（分段路径 + 当前位置指针 + 圆心文案）。"""
    view = view if view in RING_VIEWS else DEFAULT_VIEW
    cl, pl = _norm(cycle_len, period_len)
    labels = MED_LABELS if view == "med" else PLAIN_LABELS
    notes = MED_NOTES if view == "med" else PLAIN_NOTES
    step = 360.0 / cl

    segments = []
    for key, d0, d1 in _segments(cl, pl, view):
        a0 = (d0 - 1) * step - 90.0          # -90°：第 1 天落在 12 点方向
        a1 = d1 * step - 90.0
        # 两端内缩留出空隙；窄段自动少缩，避免小段被空格吃掉。
        # 圆头端帽会向外鼓出半个笔宽，所以实际留缝 = 2×(inset − 端帽角度)。
        inset = min(RING_GAP_DEG, max(0.0, (a1 - a0) / 2.0 - 4.0))
        segments.append({
            "key": key,
            "label": labels.get(key, key),
            "color": RING_COLORS.get(key, RING_COLORS["unknown"]),
            "start_day": d0,
            "end_day": d1,
            "days": (f"第 {d0} 天" if d0 == d1 else f"第 {d0}–{d1} 天"),
            "note": notes.get(key, ""),
            "path": _arc_path(a0 + inset, a1 - inset),
            "mid_angle": (a0 + a1) / 2.0,
        })

    # 当前位置指针：钟表式，指向「今天」在环上的位置
    overdue = 0
    if cycle_day is None:
        marker_day = 1
    else:
        marker_day = cycle_day
        if marker_day > cl:
            overdue = marker_day - cl
            marker_day = cl
        marker_day = max(1, marker_day)
    m_angle = (marker_day - 0.5) * step - 90.0
    mx, my = _pt(m_angle, R_MID)
    # 「今天」：白底圆牌压在环上，牌内写周期第几天（角度仍是唯一定位依据，位置不受影响）
    # 估算排卵日：六边形标记，表示「事件」而不是「一段时期」
    ovu_day = ovulation_day(cl, pl)
    ovu_angle = (ovu_day - 0.5) * step - 90.0
    ovu_x, ovu_y = _pt(ovu_angle, R_MID)
    ovu_pts = " ".join(f"{px:.2f},{py:.2f}" for px, py in
                       (_pt(ovu_angle + 60 * i, OVU_R, ovu_x, ovu_y) for i in range(6)))
    # 排卵日的文字标签摆在环内、与标记同一条半径线上
    ovu_tx, ovu_ty = _pt(ovu_angle, R_IN - 12)

    if view == "med":
        cur_key, cur_label = phase_of(marker_day, cl, pl)
    else:
        cur_key = plain_phase_of(marker_day, cl, pl)
        cur_label = PLAIN_LABELS[cur_key]

    if example:
        center_phase = "暂无记录"
    elif overdue > 0:
        center_phase = f"推迟 {overdue} 天"
    else:
        center_phase = cur_label

    if example:
        meta = "录入一次月经开始日期，即可推断周期与阶段。"
    elif overdue > 0:
        meta = f"推断经期 {next_start.isoformat() if next_start else '—'} 已过 {overdue} 天 · 原推断阶段 {cur_label}"
    else:
        meta = (f"推断区间 {next_window[0].isoformat()} ~ {next_window[1].isoformat()} · "
                f"{confidence}置信度") if next_window else ""

    # 圆心倒计时：经期中＝距经期结束；其余＝距下次经期（已推迟时不给数字，避免误导）
    if example:
        count_label, count_num = "先记录一次月经", None
    elif marker_day <= pl:
        remain = max(0, int(round(pl)) - marker_day)
        count_label = "距离经期结束" if remain > 0 else "经期预计今天结束"
        count_num = remain or None
    else:
        count_label = "距离下次经期"
        count_num = None
        if next_start:
            dn = (next_start - date.today()).days
            if dn >= 0:
                count_num = dn
            else:
                # 推算的经期已经过了：与环外那句「已过 N 天」用同一个数，别两处差一天
                count_label, count_num = "已推迟", (overdue or abs(dn))

    return {
        "view": view,
        "view_label": RING_VIEWS[view]["label"],
        "view_hint": RING_VIEWS[view]["hint"],
        "segments": segments,
        "cycle_len": cl,
        "period_len": pl,
        "track": RING_TRACK,
        "stroke": RING_STROKE,
        "radius": R_MID,
        "ovulation": {
            "x": round(ovu_x, 2), "y": round(ovu_y, 2), "day": ovu_day,
            "color": RING_COLORS["ovulation"],
            "points": ovu_pts,                       # 六边形顶点，模板直接画 polygon
            "text_x": round(ovu_tx, 2), "text_y": round(ovu_ty, 2), "text": "排卵日",
            "label": f"估算排卵日 · 第 {ovu_day} 天",
        },
        "marker": {"x": round(mx, 2), "y": round(my, 2), "r": BADGE_R,
                   "text": str(marker_day),
                   "color": RING_COLORS.get(cur_key, RING_COLORS["unknown"])},
        "meta": meta,
        "center": {
            "phase": center_phase,
            "real_label": cur_label,
            "phase_key": cur_key,
            "color": RING_COLORS.get(cur_key, RING_COLORS["unknown"]),
            "day": marker_day,
            "next": next_start.isoformat() if next_start else "—",
            "window": (f"{next_window[0].isoformat()} ~ {next_window[1].isoformat()}"
                       if next_window else ""),
            "confidence": confidence,
            "overdue": overdue,
            "example": example,
            "count_label": count_label,
            "count_num": count_num,
        },
    }


def ovulation_iso_days(cycles: list[tuple], info: dict, forward: int = 2) -> list[str]:
    """估算排卵日的日期列表：已记录的周期用**当次真实长度**，之后再推 forward 个周期。"""
    cl = float(info.get("cycle_len") or DEFAULT_CYCLE)
    pl_med = float(info.get("period_len") or DEFAULT_PERIOD)
    cl_int = max(1, int(round(cl)))
    cs = sorted([(s, e) for s, e in cycles if s])
    out: list[str] = []
    for i, (s, e) in enumerate(cs):
        nxt = cs[i + 1][0] if i + 1 < len(cs) else None
        length = (nxt - s).days if nxt else cl
        pl_this = ((e - s).days + 1) if e else pl_med
        out.append((s + timedelta(days=ovulation_day(length, pl_this) - 1)).isoformat())
    ns = info.get("next_start")
    if ns and not info.get("example"):
        for k in range(forward):
            s = ns + timedelta(days=cl_int * k)
            out.append((s + timedelta(days=ovulation_day(cl, pl_med) - 1)).isoformat())
    return sorted(set(out))


def ring_context(info: dict, view: str = DEFAULT_VIEW) -> dict:
    """从 analyze() 的结果直接生成环图数据。"""
    return ring(info.get("cycle_len", DEFAULT_CYCLE), info.get("period_len", DEFAULT_PERIOD),
                view=view, cycle_day=info.get("cycle_day"),
                next_start=info.get("next_start"), next_window=info.get("next_window"),
                confidence=info.get("confidence", ""), example=bool(info.get("example")))


def _phase_key(day_index: float, cycle_len: float, period_len: float, view: str) -> str:
    """某一天的阶段 key（两种视角共用入口，避免日历和环图算法分叉）。"""
    if view == "plain":
        return plain_phase_of(int(day_index), cycle_len, period_len)
    return phase_of(day_index, cycle_len, period_len)[0]


def calendar_days(cycles: list[tuple], info: dict, view: str = DEFAULT_VIEW,
                  today: date | None = None, fwd_days: int = 150) -> dict:
    """逐日阶段图：已记录的按**当次真实周期长度**算，未记录的按推断算（est=1）。

    cycles：[(开始日, 结束日或 None), ...]（顺序不限）。
    返回 {"YYYY-MM-DD": {"p": 阶段key, "est": 0/1}}，只覆盖「最早记录前一周 ~ 今天+150天」。
    est=1 表示推断（前端颜色变淡）；est=0 表示到那天为止已有真实记录。
    """
    today = today or date.today()
    cl = float(info.get("cycle_len") or DEFAULT_CYCLE)
    pl_med = float(info.get("period_len") or DEFAULT_PERIOD)
    cl_int = max(1, int(round(cl)))
    cs = sorted([(s, e) for s, e in cycles if s])
    out: dict[str, dict] = {}

    def put(d: date, key: str, est: int) -> None:
        out[d.isoformat()] = {"p": key, "est": est}

    for i, (s, e) in enumerate(cs):
        nxt = cs[i + 1][0] if i + 1 < len(cs) else None
        pl_this = ((e - s).days + 1) if e else pl_med          # 这次经期实际几天
        if nxt:
            length = (nxt - s).days                            # 两次经期之间的真实长度
            last = nxt - timedelta(days=1)
            est = 0
            beyond = None
        else:                                                  # 最近一次：长度用中位数推算
            length = cl
            last = s + timedelta(days=cl_int - 1)
            est = 0                                            # 本周期剩余天数仍算实色（与环图一致）
            beyond = today                                     # 超过中位长度的天数记为「推迟」
        d = s
        while d <= last:
            idx = (d - s).days + 1
            put(d, _phase_key(idx, length, pl_this, view), est)
            d += timedelta(days=1)
        if beyond:
            while d <= beyond:
                put(d, "unknown", 0)
                d += timedelta(days=1)

    # 推断：从预测的下一次经期起，往后两个周期
    ns = info.get("next_start")
    if ns and not info.get("example"):
        for k in range(2):
            s = ns + timedelta(days=cl_int * k)
            for idx in range(1, cl_int + 1):
                d = s + timedelta(days=idx - 1)
                if d.isoformat() not in out:
                    put(d, _phase_key(idx, cl, pl_med, view), 1)

    # 裁到有用范围：最早记录前 7 天 ~ 今天 + fwd_days
    lo = (min(s for s, _ in cs) - timedelta(days=7)) if cs else (today - timedelta(days=400))
    hi = today + timedelta(days=fwd_days)
    return {k: v for k, v in out.items() if lo.isoformat() <= k <= hi.isoformat()}


def ring_phase_table(info: dict, view: str = DEFAULT_VIEW) -> list[dict]:
    """环图下方的阶段明细（同一阶段出现多段时合并，如通俗视图的两个安全期）。"""
    cl = info.get("cycle_len", DEFAULT_CYCLE)
    pl = info.get("period_len", DEFAULT_PERIOD)
    labels = MED_LABELS if view == "med" else PLAIN_LABELS
    notes = MED_NOTES if view == "med" else PLAIN_NOTES
    order: list[str] = []
    ranges: dict[str, list[tuple[int, int]]] = {}
    for key, d0, d1 in _segments(cl, pl, view):
        if key not in ranges:
            ranges[key] = []
            order.append(key)
        ranges[key].append((d0, d1))
    rows = []
    for key in order:
        rs = ranges[key]
        total = sum(b - a + 1 for a, b in rs)
        rows.append({
            "key": key,
            "label": labels.get(key, key),
            "color": RING_COLORS.get(key, RING_COLORS["unknown"]),
            "days": "、".join(f"第 {a}–{b} 天" if b > a else f"第 {a} 天" for a, b in rs),
            "count": total,
            "note": notes.get(key, ""),
        })
    return rows
