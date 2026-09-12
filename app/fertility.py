"""按排卵日推算的「受孕概率」显示。

数字出处（逐条可查）：Wilcox AJ, Weinberg CR, Baird DD.
*Timing of Sexual Intercourse in Relation to Ovulation.* N Engl J Med 1995;333:1517-21，表 1
的"估计受孕概率"：距排卵日 −5 / −4 / −3 / −2 / −1 / 0 天分别为 10% / 16% / 14% / 27% / 31% / 33%。
该研究只在「结束于排卵日的六天窗口」内观察到妊娠；窗口外的估计概率为 0，但 95% 置信区间
上限约 12%（论文原文：we cannot exclude a probability of conception of up to 12 percent）。

必须同时说明的三件事（页面上也要写）：
    1. 这是**健康备孕人群的群体平均**，不是对个人的预测，也不是检测结果；
    2. 排卵日本身由日历推算得出，会随周期波动，可能整体偏移几天；
    3. 因此**不能用于避孕决策**——窗口外不等于安全期。
"""
from __future__ import annotations

from datetime import date

SOURCE_TITLE = "Wilcox AJ, et al. N Engl J Med 1995;333:1517-21（表 1）"
SOURCE_URL = "https://www.nejm.org/doi/full/10.1056/NEJM199512073332301"
WINDOW_OUT_CI_UPPER = 12          # 窗口外的 95% 置信区间上限（%）

# 距排卵日的天数 → 估计受孕概率（%），来自上面那张表
DAY_RATES = {-5: 10, -4: 16, -3: 14, -2: 27, -1: 31, 0: 33}


def _parse(iso) -> date | None:
    if isinstance(iso, date):
        return iso
    try:
        return date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return None


def today(ovulation_iso, today_iso) -> dict:
    """今日的相对受孕概率（按与估算排卵日的天数）＋一句人话说明。"""
    ovu, day = _parse(ovulation_iso), _parse(today_iso)
    if ovu is None or day is None:
        return {"known": False, "pct": None, "text": "先有记录才能推算", "in_window": False}
    offset = (day - ovu).days                 # 负 = 排卵前
    if offset in DAY_RATES:
        pct = DAY_RATES[offset]
        when = "排卵日当天" if offset == 0 else f"排卵前 {abs(offset)} 天"
        return {"known": True, "pct": pct, "offset": offset, "in_window": True,
                "label": f"易孕窗口内（{when}）",
                "text": f"按人群数据，这天的受孕概率约 {pct}%：{when}"}
    if offset == -6 or offset == 1:
        near = "排卵前 6 天" if offset == -6 else "排卵后 1 天"
        desc = f"已在窗口外（{near}）"
    else:
        desc = f"距估算排卵日 {abs(offset)} 天"
    return {"known": True, "pct": 0, "offset": offset, "in_window": False,
            "label": "不在六天窗口内", "text": f"{desc}：该研究未观察到妊娠，但置信区间上限约 "
                                            f"{WINDOW_OUT_CI_UPPER}%，不能当作安全期"}


def note() -> str:
    return ("受孕率按「距估算排卵日的天数」取自一项人群研究（健康备孕女性 625 个周期）的日受孕概率："
            "排卵前 5 天约 10%、前 4 天 16%、前 3 天 14%、前 2 天 27%、前 1 天 31%、排卵日 33%；"
            "六天窗口之外该研究未观察到妊娠（置信区间上限约 12%）。"
            "它是**群体平均**而不是对你个人的预测，排卵日也只是日历推算；"
            "**因此不能用于避孕**，窗口外不等于安全期，也不作为备孕建议。")
