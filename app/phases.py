"""阶段图例里「点开看说明」的文案。

只写通用医学常识（群体规律），不针对个人、不做诊断、不给药物剂量。
每条都带可查来源，链接在 2026-09 实测可访问。
"""
from __future__ import annotations

# 来源（标题, 链接）——全部实测 200
S_NHS_PERIODS = ("NHS：月经（Periods）", "https://www.nhs.uk/conditions/periods/")
S_NHS_PAIN = ("NHS：痛经（Period pain）", "https://www.nhs.uk/conditions/period-pain/")
S_NHS_PMS = ("NHS：经前综合征（PMS）", "https://www.nhs.uk/conditions/pre-menstrual-syndrome/")
S_NHS_OVU = ("NHS：排卵痛（Ovulation pain）", "https://www.nhs.uk/conditions/ovulation-pain/")
S_NHS_LATE = ("NHS：月经推迟或停经", "https://www.nhs.uk/conditions/stopped-or-missed-periods/")
S_ACOG_FABM = ("ACOG：为什么日历法不可靠（生育意识类方法）",
               "https://www.acog.org/womens-health/faqs/fertility-awareness-based-methods-of-family-planning")
S_ACOG_PMS = ("ACOG：经前综合征（PMS）", "https://www.acog.org/womens-health/faqs/premenstrual-syndrome")
S_ACOG_AUB = ("ACOG：异常子宫出血", "https://www.acog.org/womens-health/faqs/abnormal-uterine-bleeding")
S_ACOG_FIRST = ("ACOG：第一次月经与周期常识", "https://www.acog.org/womens-health/faqs/your-first-period")
S_OWH_CYCLE = ("美国妇女健康办公室：你的月经周期",
               "https://www.womenshealth.gov/menstrual-cycle/your-menstrual-cycle")
S_CLEVELAND = ("Cleveland Clinic：月经周期", "https://my.clevelandclinic.org/health/articles/10132-menstrual-cycle")
S_WILCOX = ("Wilcox AJ, et al. N Engl J Med 1995;333:1517-21（日受孕概率表）",
            "https://www.nejm.org/doi/full/10.1056/NEJM199512073332301")

# 视角 → 图例里出现的阶段，按出现次序
LEGEND_ORDER = {
    "med": ["menstrual", "follicular", "ovulation", "luteal"],
    "plain": ["menstrual", "fertile", "safe"],
}

PHASE_INFO = {
    "menstrual": {
        "label": "月经期",
        "tagline": "子宫内膜脱落、出血的这几天，周期从出血第 1 天算起。",
        "body": "这个周期没有受孕的话，雌激素和孕激素降到最低点，子宫内膜脱落并随血液经阴道排出。"
                "（如果受孕，内膜会保留下来，也就不会来月经。）",
        "feel": "常见：小腹绞痛或坠胀、腰酸、乏力、头痛、情绪低落。出血量多在头两三天最多，之后逐渐减少。"
                "痛经通常在出血开始前后最明显，多数在 1–3 天内缓解。",
        "fertility": "受孕可能性最低的一段。但它不是「安全期」：周期偏短或不规律时，"
                     "排卵可能紧跟在出血结束后不久，而精子在体内最长可存活约 5 天。",
        "record": ["点日历记下开始的那一天（周期长度就是从这天算起的）",
                   "顺手记经量（少 / 中 / 多）和疼痛程度，几次之后就能看出自己的规律",
                   "如果出血超过 7 天、量大到每小时就要换、或疼痛影响正常生活，值得去就诊"],
        "sources": [S_NHS_PERIODS, S_ACOG_FIRST, S_OWH_CYCLE],
    },
    "follicular": {
        "label": "卵泡期",
        "tagline": "从出血结束到排卵前：卵泡在发育，雌激素在上升。",
        "body": "垂体分泌的促卵泡激素让一批卵泡开始发育，通常只有一个成为优势卵泡；"
                "卵泡分泌的雌激素使子宫内膜重新增厚，为可能的着床做准备。",
        "feel": "多数人这段时间精力和情绪相对较好。白带逐渐增多、变得清亮、可拉丝。"
                "少数人会有排卵前的少量点滴出血。",
        "fertility": "越接近排卵，受孕可能性越高——从排卵前 5 天起就进入易孕窗口。"
                     "这一段的长短因人而异（几天到两三周都算常见），也是各人周期长度差异的主要来源。",
        "record": ["记白带的性状（清亮 / 拉丝 / 粘稠）——它会随激素变化，是自己的一个参考",
                   "想更准确判断排卵，可以配合基础体温或排卵试纸，再回来看这里的推算是否吻合",
                   "周期忽长忽短，多半是这一阶段长短在变，不一定是「不规律」"],
        "sources": [S_OWH_CYCLE, S_CLEVELAND],
    },
    "ovulation": {
        "label": "排卵期",
        "tagline": "估算排卵日前后各 1 天；其中排卵日通常是下次月经前约 14 天。",
        "body": "雌激素高峰触发黄体生成素（LH）陡升，约 24–36 小时后卵泡破裂、卵子排出。"
                "卵子排出后大约只能存活 12–24 小时，而精子在女性生殖道内可存活约 5 天——"
                "所以受精主要发生在排卵前的几天，而不是排卵之后。",
        "feel": "一部分人会在排卵那一侧下腹感到一阵钝痛或抽痛（排卵痛），持续几分钟到一两天；"
                "也有人白带明显变多、变清、拉丝。完全没有感觉同样常见。",
        "fertility": "六天窗口内受孕概率最高的是排卵前一两天。人群数据（健康备孕女性，625 个周期）："
                     "排卵前 2 天约 27%、前 1 天约 31%、排卵日约 33%。",
        "record": ["这里的排卵日是「按日历推算」得出的（下次经期前约 14 天），不是检测结果，会随周期波动",
                   "想要更可靠，可搭配排卵试纸或基础体温，对照记录几个周期",
                   "排卵日前后出现的一侧下腹痛，如果剧烈、持续不缓解或伴发热，应及时就诊"],
        "sources": [S_NHS_OVU, S_WILCOX, S_OWH_CYCLE],
    },
    "luteal": {
        "label": "黄体期",
        "tagline": "排卵后到下次出血前，是周期里长度最稳定的一段（通常 12–14 天）。",
        "body": "排卵后的卵泡转变成黄体，分泌孕激素，让子宫内膜更适合着床。"
                "如果没有受孕，黄体大约在 14 天后萎缩，激素水平下降，内膜脱落，下一次月经开始。",
        "feel": "经前一周左右可能出现乳房胀痛、腹胀、水肿、体重小幅波动、食欲变化、情绪波动、长痘等"
                "（经前综合征 PMS），一般在出血开始后减轻。",
        "fertility": "排卵之后受孕概率迅速下降——因为卵子的存活时间很短。"
                     "这也是「排卵后到下次月经」在日历法里被当作不易受孕期的原因，"
                     "但它同样建立在「排卵日推算准确」这个前提上。",
        "record": ["记经前症状（乳房胀痛 / 情绪 / 皮肤 / 腹胀）和出现的时间点",
                   "连记两三个周期，就能看出自己的 PMS 是不是固定出现在这几天",
                   "如果经前症状明显影响工作或生活，可以带着记录去就诊，这属于可以治疗的问题"],
        "sources": [S_NHS_PMS, S_ACOG_PMS, S_OWH_CYCLE],
    },
    "fertile": {
        "label": "易孕期",
        "tagline": "按日历推算的受孕可能性较高的几天：排卵日前 5 天到排卵日后 1 天。",
        "body": "窗口之所以从排卵日前 5 天开始，是因为精子在体内可存活约 5 天；"
                "之所以到排卵后 1 天结束，是因为卵子排出后只能存活约 12–24 小时。",
        "feel": "和排卵前后那几天一样：白带变多、变清、可拉丝；少数人有排卵痛或点滴出血。",
        "fertility": "这是受孕概率最高的几天，但具体概率取决于离排卵日的远近"
                     "（排卵前 2 天约 27%、前 1 天约 31%、排卵日约 33%）。",
        "record": ["推算出来的窗口只能当参考：排卵会因压力、睡眠、生病、旅行而提前或推后",
                   "想更准确，用排卵试纸或基础体温对照几个周期"],
        "sources": [S_ACOG_FABM, S_WILCOX],
    },
    "safe": {
        "label": "安全期",
        "tagline": "按日历推算出来的、受孕可能性较低的日子——是推算，不是保证。",
        "body": "日历法的前提是「排卵总在下一次月经前 14 天、而且每个周期都一样」。"
                "实际上排卵会受压力、睡眠、疾病、体重变化、旅行等影响而提前或推后，"
                "偶尔还会在一个周期里排卵两次。",
        "feel": "没有特别的体感；这一段通常落在排卵之后、下次出血之前，以及出血刚结束的几天。",
        "fertility": "不能当作「不会怀孕」的日子。相关研究在六天窗口之外没有观察到妊娠，"
                     "但无法排除最高约 12% 的受孕概率。",
        "record": ["需要可靠避孕时，请使用避孕套或激素类等有明确有效率的方法，并咨询医生",
                   "如果只是用来自我了解周期，注意每次推算都会随记录更新而变化"],
        "sources": [S_ACOG_FABM],
    },
    "unknown": {
        "label": "推迟 / 未确认",
        "tagline": "推算的经期已经到了，但还没有记录到出血。",
        "body": "偶尔推迟很常见：压力、作息紊乱、体重明显变化、生病、剧烈运动、旅行等都可能让排卵延后，"
                "进而让整个周期变长。",
        "feel": "常见的是没有明显感觉，也可能有原本该来的经前症状（乳房胀痛、腹胀、情绪波动）"
                "持续存在却不来月经。",
        "fertility": "无法推算——排卵日期未知，因此任何一天都不能按「安全期」来看待。",
        "record": ["先确认有没有漏记：回到日历上把这次出血的开始日补上",
                   "如果月经间隔长期超过 35 天、连续 3 个周期以上不规律，或停经超过 90 天，建议就诊",
                   "若有可能怀孕、或伴明显腹痛与异常出血，应尽快就诊而不是继续等"],
        "sources": [S_NHS_LATE, S_ACOG_AUB],
    },
}


def for_view(view: str) -> list[dict]:
    """按视角返回图例条目：[{key, label, color, info}]，info 为该阶段说明。

    颜色取自 cycle.RING_COLORS（与环上分段同一份），图例按钮上的色点就是它。
    """
    import cycle as cyclem                     # 延迟导入：避免与 cycle 形成循环依赖
    keys = LEGEND_ORDER.get(view, LEGEND_ORDER["med"])
    out = []
    for k in keys:
        info = PHASE_INFO.get(k)
        if info:
            out.append({"key": k, "label": info["label"], "info": info,
                        "color": cyclem.RING_COLORS.get(k, cyclem.RING_COLORS["unknown"])})
    return out
