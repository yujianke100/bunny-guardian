"""BMI（体质指数）计算与分级。

分级采用**中国成人标准**《成人体重判定》WS/T 428—2013（国家卫生健康委发布）：
    BMI < 18.5          体重过低
    18.5 ≤ BMI < 24.0   正常
    24.0 ≤ BMI < 28.0   超重
    BMI ≥ 28.0          肥胖
WHO 的切点是 25.0 / 30.0（比中国标准宽松），本页按中国标准显示并注明差异。

必须说清的局限（否则容易把 BMI 当结论）：
    - BMI 只是**人群筛查**指标，不区分肌肉和脂肪，运动员、孕产妇、水肿等情况不适用；
    - 它不反映脂肪分布：腹部肥胖要看腰围（女性 ≥85 cm 为中心型肥胖，同标准表 2）；
    - 单次数值说明不了什么，**趋势**比单点更有意义；不适请就医，不要据此自行减重/用药。
"""
from __future__ import annotations

SOURCE_TITLE = "《成人体重判定》WS/T 428—2013（国家卫生健康委）"
SOURCE_URL = "https://www.nhc.gov.cn/ewebeditor/uploadfile/2013/08/20130808135715967.pdf"
UNDER, NORMAL, OVER, OBESE = 18.5, 24.0, 28.0, None
CUTS = [(UNDER, "体重过低", "warn"), (NORMAL, "正常", "ok"),
        (OVER, "超重", "warn"), (None, "肥胖", "danger")]
NORMAL_LO, NORMAL_HI = 18.5, 24.0          # 正常范围的上下界（算健康体重区间用）


def value(weight_kg: float | None, height_cm: float | None) -> float | None:
    """BMI = 体重(kg) / 身高(m)²。参数不全或非法时返回 None。"""
    try:
        w, h = float(weight_kg or 0), float(height_cm or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return round(w / ((h / 100.0) ** 2), 1)


def classify(bmi: float | None) -> dict:
    """按中国标准给出分级标签与样式标记。"""
    if bmi is None:
        return {"label": "—", "tag": "grey", "bmi": None}
    for cut, label, tag in CUTS:
        if cut is None or bmi < cut:
            return {"label": label, "tag": tag, "bmi": bmi}
    return {"label": "肥胖", "tag": "danger", "bmi": bmi}


def healthy_weight_range(height_cm: float | None) -> tuple[float, float] | None:
    """按身高给出「正常」范围的体重区间（kg），用于给自己一个参考区间。"""
    try:
        h = float(height_cm or 0) / 100.0
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    return (round(NORMAL_LO * h * h, 1), round(NORMAL_HI * h * h, 1))


def note() -> str:
    return ("BMI 按中国成人标准分级（WS/T 428—2013）：<18.5 体重过低、18.5–24 正常、"
            "24–28 超重、≥28 肥胖（WHO 的切点是 25/30，比中国标准宽松）。"
            "BMI 只是人群筛查指标：不区分肌肉与脂肪，孕期、运动员等情况不适用；"
            "腹部肥胖要看腰围（女性 ≥85 cm）。看趋势比看单次数值更有意义。")
