"""统计与分布：从历史记录里算周期长度、经期天数、经量、症状及其所处阶段。

原则
    - 只用真实记录：推算出来的日子（est=1）不参与统计，避免拿推断喂推断；
    - 样本少就如实说：条数不足时给出 `enough=False`，界面提示"样本少，仅供参考"；
    - 阶段关联用与日历同一份逐日阶段（调用方传入），不另算一套，避免两处口径不一致。
"""
from __future__ import annotations

from datetime import date

CYCLE_BUCKETS = [("≤25 天", None, 25), ("26–30 天", 26, 30), ("31–35 天", 31, 35),
                 ("≥36 天", 36, None)]
PERIOD_BUCKETS = [("≤3 天", None, 3), ("4–5 天", 4, 5), ("6–7 天", 6, 7), ("≥8 天", 8, None)]
FLOW_LABELS = {"light": "少", "medium": "中", "heavy": "多"}
MIN_CYCLES_FOR_TREND = 3          # 少于这个数就不谈趋势


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _bucket(values: list[int], spec: list) -> list[dict]:
    out = []
    for label, lo, hi in spec:
        n = sum(1 for v in values if (lo is None or v >= lo) and (hi is None or v <= hi))
        out.append({"label": label, "n": n})
    return out


def _labels(cycle: dict | None, view: str) -> dict:
    import cycle as cyclem
    return cyclem.MED_LABELS if view == "med" else cyclem.PLAIN_LABELS


# 测量类日志：是数值记录，不是「症状」，不进症状分布与阶段关联（否则「体重」会混进症状里）
NON_SYMPTOM_KINDS = ("weight",)


def distributions(conn, pid: int, days_map: dict, view: str = "med",
                  kind_cn: dict | None = None) -> dict:
    """days_map：同日历的逐日阶段（{"YYYY-MM-DD": {"p":..., "est":0/1}}），用于阶段关联。"""
    import cycle as cyclem

    rows = conn.execute(
        "SELECT start_date, end_date, flow, symptoms FROM cycles WHERE profile_id=?"
        " ORDER BY start_date", (pid,)).fetchall()
    cycles = []
    for r in rows:
        s = cyclem.date.fromisoformat((r["start_date"] or "")[:10]) if r["start_date"] else None
        e = cyclem.date.fromisoformat((r["end_date"] or "")[:10]) if r["end_date"] else None
        if s:
            cycles.append({"s": s, "e": e or s, "flow": (r["flow"] or "").strip(),
                           "symptoms": (r["symptoms"] or "").strip()})

    log_rows = conn.execute(
        "SELECT log_date, kind, name, severity, value FROM day_logs WHERE profile_id=?"
        " ORDER BY log_date", (pid,)).fetchall()

    starts = [c["s"] for c in cycles]
    gaps = [(b - a).days for a, b in zip(starts, starts[1:])]
    durs = [(c["e"] - c["s"]).days + 1 for c in cycles]
    med_cycle = _median(gaps)
    med_period = _median(durs)

    # 周期长度：按时间顺序列出每个间隔（让人看出波动，而不是只看中位数）
    intervals = []
    for i, (a, b) in enumerate(zip(starts, starts[1:])):
        intervals.append({"label": f"{a.strftime('%m/%d')} → {b.strftime('%m/%d')}",
                          "value": (b - a).days, "unit": "天"})

    # 经量：记录在 cycles.flow 上，另外 day_logs 里 kind=flow 的也计入
    flow = {"light": 0, "medium": 0, "heavy": 0}
    for c in cycles:
        if c["flow"] in flow:
            flow[c["flow"]] += 1
    for r in log_rows:
        if r["kind"] == "flow":
            v = (r["value"] or r["name"] or "").strip()
            if v in flow:
                flow[v] += 1
    n_flow = sum(flow.values())

    # 症状/日志计数：按**名称**归并（同一症状可能一处记在经期症状里、一处记在每日日志里，
    # 类型不同也该合并计数；没有名称时用类型名兜底）
    counts: dict[str, dict] = {}

    def _bump(disp: str, n: int = 1) -> None:
        disp = disp.strip()
        if not disp:
            return
        c = counts.setdefault(disp, {"name": disp, "n": 0})
        c["n"] += n

    for r in log_rows:
        if (r["kind"] or "") in NON_SYMPTOM_KINDS:
            continue                       # 体重这类测量值不算症状
        _bump((r["name"] or "").strip() or (kind_cn or {}).get(r["kind"], r["kind"]))
    for c in cycles:                      # periods 里手写的症状也算进分布
        for part in [x.strip() for x in c["symptoms"].replace("，", ",").split(",") if x.strip()]:
            _bump(part)
    # 不截断：数量多时由界面用表格 + 翻页呈现（原来只给前 8，长名字还会被挤掉）
    _all_sym = sorted(counts.values(), key=lambda x: -x["n"])
    _total_sym = sum(x["n"] for x in _all_sym) or 1
    for _x in _all_sym:
        _x["pct"] = round(_x["n"] / _total_sym * 100)
    symptoms = _all_sym[:200]           # 极端情况兜底，避免一次渲染上千行

    # 阶段关联：把真实发生过的日志日期映射到阶段，看某个症状主要落在哪一期
    import db as dbm
    today_iso = dbm.today()
    labels = _labels(None, view)
    per_phase: dict[str, dict[str, int]] = {}
    for r in log_rows:
        if (r["kind"] or "") in NON_SYMPTOM_KINDS:
            continue                       # 同理：测量值不参与「症状最常出现在哪个阶段」
        day = (r["log_date"] or "")[:10]
        if not day or day > today_iso:
            continue                       # 未来的日期不算「已发生」
        p = (days_map.get(day) or {})
        if not p or p.get("est"):
            continue                       # 只统计真实发生过的日子
        key = p.get("p") or ""
        label = labels.get(key)
        if not label:
            continue
        disp = (r["name"] or "").strip() or (kind_cn or {}).get(r["kind"], r["kind"])
        per_phase.setdefault(disp, {})
        per_phase[disp][label] = per_phase[disp].get(label, 0) + 1
    links = []
    for name, phases in per_phase.items():
        total = sum(phases.values())
        top = max(phases.items(), key=lambda kv: kv[1])
        links.append({"name": name, "total": total, "phase": top[0], "in_phase": top[1],
                      "share": round(top[1] * 100.0 / total)})
    links.sort(key=lambda x: (-x["total"], -x["share"]))

    logged_days = len({(r["log_date"] or "")[:10] for r in log_rows})
    span = (max(starts) - min(starts)).days + 1 if len(starts) > 1 else 0
    return {
        "n_cycles": len(cycles),
        "n_gaps": len(gaps),
        "n_logged_days": logged_days,
        "span_days": span,
        "enough": len(gaps) >= MIN_CYCLES_FOR_TREND,
        "min_cycles": MIN_CYCLES_FOR_TREND,
        "cycle_len": {"n": len(gaps), "median": round(med_cycle, 1) if gaps else 0,
                      "min": min(gaps) if gaps else 0, "max": max(gaps) if gaps else 0,
                      "buckets": _bucket(gaps, CYCLE_BUCKETS), "intervals": intervals},
        "period_len": {"n": len(durs), "median": round(med_period, 1) if durs else 0,
                       "min": min(durs) if durs else 0, "max": max(durs) if durs else 0,
                       "buckets": _bucket(durs, PERIOD_BUCKETS)},
        "flow": {"counts": flow, "n": n_flow,
                 "rows": [{"label": FLOW_LABELS[k], "n": flow[k]} for k in ("light", "medium", "heavy")]},
        "symptoms": symptoms,
        "phase_links": links[:6],
    }
