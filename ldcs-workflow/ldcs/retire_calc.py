"""退休时间测算程序 —— 依据《关于实施渐进式延迟法定退休年龄的决定》（2025-01-01 起施行）。

规则（与业务口径一致）：
1. 先按「原法定退休年龄」算出**原退休时间**；若原退休时间在 2025 年 1 月 1 日之前，
   不适用延迟，直接按原年龄退休：男职工 60 岁、女干部 55 岁、女工人 50 岁。
2. 若原退休时间在 2025 年 1 月 1 日及以后，按渐进式延迟：
   - 男职工、原 55 岁退休女职工：每 4 个月延迟 1 个月（延迟速率 0.25 年/年）
   - 原 50 岁退休女职工：每 2 个月延迟 1 个月（延迟速率 0.5 年/年）
   - 延迟上限：男 63 岁、原 55 岁女 58 岁、原 50 岁女 55 岁
3. 最低缴费年限（企业职工口径，含男职工、原55岁女职工、原50岁女职工）：
   · 2029-12-31 及之前退休：15 年；
   · 2030-01-01 起：每年提高 6 个月，逐步提高至 20 年。
     2030-01 = 15年6个月；2031-01 = 16年；……；2039-01 = 20 年（上限）。

对外接口：calc_retirement(birth_year, birth_month, category) -> dict
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# 人员类别
# ---------------------------------------------------------------------------
MALE = "male"                     # 男职工（原 60 岁）
FEMALE_CADRE = "female_cadre"     # 原 55 岁退休女职工（女干部 / 管理技术岗）
FEMALE_WORKER = "female_worker"   # 原 50 岁退休女职工（女工人）

CATEGORY_CN = {
    MALE: "男职工",
    FEMALE_CADRE: "原55周岁退休女职工",
    FEMALE_WORKER: "原50周岁退休女职工",
}

# 原法定退休年龄（单位：月）
BASE_AGE_MONTHS = {
    MALE: 60 * 12,
    FEMALE_CADRE: 55 * 12,
    FEMALE_WORKER: 50 * 12,
}

# 延迟步长：每 STEP 个月延迟 1 个月
DELAY_STEP = {
    MALE: 4,
    FEMALE_CADRE: 4,
    FEMALE_WORKER: 2,
}

# 延迟上限（单位：月）—— 男 63 / 女干部 58 / 女工人 55
DELAY_CAP = {
    MALE: 36,
    FEMALE_CADRE: 36,
    FEMALE_WORKER: 60,
}

# 政策施行时间
POLICY_START_YEAR = 2025
POLICY_START_MONTH = 1

# ---------------------------------------------------------------------------
# 最低缴费年限（企业职工口径）：按实际退休时间（年-月）动态计算。
# ---------------------------------------------------------------------------
_MIN_YEARS_BASELINE_MONTHS = 15 * 12   # 2029-12-31 及之前退休：15 年
_MIN_YEARS_START_YEAR = 2030           # 2030-01-01 起算
_MIN_YEARS_START_MONTH = 1
_MIN_YEARS_STEP_MONTHS = 6             # 每跨 1 年 +6 个月
_MIN_YEARS_CAP_MONTHS = 20 * 12        # 上限 20 年


def min_contribution_for(retire_year: int, retire_month: int) -> tuple:
    """按实际退休时间计算企业职工最低缴费年限。

    返回 (总月数, 中文表达)：
      · "15年" / "15年6个月" / "16年" / "16年6个月" / … / "20年"
    """
    retire_idx = int(retire_year) * 12 + (int(retire_month) - 1)
    cutoff_idx = 2029 * 12 + 11                       # 2029-12（仍为 15 年）
    if retire_idx <= cutoff_idx:
        total = _MIN_YEARS_BASELINE_MONTHS
    else:
        # 2030 年起：每跨一整年 +6 个月
        #   2030年内 → +6（15年6个月）；2031年内 → +12（16年）；……；2039年内 → +60（20年）。
        # 2040 年起封顶 20 年。
        years_offset = int(retire_year) - _MIN_YEARS_START_YEAR + 1
        total = min(_MIN_YEARS_BASELINE_MONTHS + years_offset * _MIN_YEARS_STEP_MONTHS,
                    _MIN_YEARS_CAP_MONTHS)
    return total, _fmt_months(total)


def _fmt_months(months: int) -> str:
    """把「月数」格式化成「X年」/「X年Y个月」。"""
    y, m = divmod(int(months), 12)
    return f"{y}年" if m == 0 else f"{y}年{m}个月"

# 别名 → 标准类别（抽取层可能给出中文或简称，这里做归一）
_ALIAS = {
    "male": MALE, "男": MALE, "男职工": MALE, "男性": MALE, "男士": MALE,
    "female_cadre": FEMALE_CADRE, "女干部": FEMALE_CADRE, "女管理": FEMALE_CADRE,
    "女管理人员": FEMALE_CADRE, "女技术人员": FEMALE_CADRE, "女专业技术": FEMALE_CADRE,
    "原55": FEMALE_CADRE, "女55": FEMALE_CADRE, "55岁女职工": FEMALE_CADRE,
    "female_worker": FEMALE_WORKER, "女工人": FEMALE_WORKER,
    "女工人岗": FEMALE_WORKER, "原50": FEMALE_WORKER, "女50": FEMALE_WORKER,
    "50岁女职工": FEMALE_WORKER,
    # 注意：「女职工」单独出现是歧义的（原55 / 原50 都叫女职工）→ 不映射，交由程序追问
}


def normalize_category(cat: Optional[str]) -> Optional[str]:
    """把各种写法归一为标准类别；无法识别返回 None。"""
    if not cat:
        return None
    c = str(cat).strip()
    if c in CATEGORY_CN:
        return c
    return _ALIAS.get(c) or _ALIAS.get(c.lower())


# ---------------------------------------------------------------------------
# 用户原话 → 人员类别（程序兜底）
# ---------------------------------------------------------------------------
# 明确带「女」的强特征词：命中即定性，不需要再问
_STRONG_WORKER = ("女工人", "女工人岗", "原50", "女50", "50岁女职工", "50岁退休")
_STRONG_CADRE = ("女干部", "女管理", "女技术人员", "女专业技术",
                 "原55", "女55", "55岁女职工", "55岁退休")
_STRONG_MALE = ("男职工", "男性", "男士")


def category_from_text(text: Optional[str], gender: Optional[str] = None):
    """从用户**原话**里判定人员类别（抽取层没给出来时的程序兜底）。

    用户常常直接回一句「女工人」「我是女干部」就把这项答了，模型若漏抽，
    程序必须自己认出来，不能反过来再问一遍「是干部还是工人」。

    优先级：明确带性别的强特征词 > 结合已知性别的弱特征词。
    「女」「女职工」「管理岗」这类**不含性别**或**有歧义**的说法不单独定性
    （原 55 / 原 50 都叫女职工），继续交由程序追问，避免猜错。
    """
    if not text:
        return None
    t = str(text)
    for kw in _STRONG_WORKER:
        if kw in t:
            return FEMALE_WORKER
    for kw in _STRONG_CADRE:
        if kw in t:
            return FEMALE_CADRE
    for kw in _STRONG_MALE:
        if kw in t:
            return MALE
    # 弱特征词（不带性别的岗位说法）：只有在已知性别时才敢定性。
    # 用户常回一句「在工厂打工」「坐办公室」来回答「干部还是工人」，
    # 这类**岗位描述**必须能落到类别上，不能反过来再问一遍。
    if gender == "female":
        if re.search(r"工人|一线|生产|车间|工厂|厂里|打工|流水线|操作工|普工|"
                     r"打螺丝|计件|体力", t):
            return FEMALE_WORKER
        if re.search(r"干部|管理|技术|行政|办公室|机关|事业单位|公务员|"
                     r"文员|职员|教师|医生|护士|会计|工程师|坐班", t):
            return FEMALE_CADRE
    elif gender == "male":
        if re.search(r"男|先生", t):
            return MALE
    return None


# ---------------------------------------------------------------------------
# 月份工具
# ---------------------------------------------------------------------------
def _month_index(year: int, month: int) -> int:
    """把「年月」压成一个可加减的整数序号（月粒度）。"""
    return year * 12 + (month - 1)


def _from_index(idx: int) -> tuple:
    return idx // 12, idx % 12 + 1


def _fmt_ym(year: int, month: int) -> str:
    return f"{year}年{month}月"


def _fmt_age(months: int) -> str:
    """把「总月数」格式化成「X周岁」/「X周岁Y个月」。"""
    y, m = months // 12, months % 12
    return f"{y}周岁" if m == 0 else f"{y}周岁{m}个月"


# ---------------------------------------------------------------------------
# 核心计算
# ---------------------------------------------------------------------------
def calc_retirement(birth_year: int, birth_month: int, category: str) -> dict:
    """计算退休时间 + 该退休时点对应的最低缴费年限。

    参数：
      birth_year / birth_month —— 出生年月（月粒度，1-12）
      category —— male | female_cadre | female_worker（或中文别名）

    返回：dict，含原退休年龄/原退休时间/延迟退休年龄/延迟退休时间，
    以及按「实际退休时间」动态算出的最低缴费年限。
    参数非法时抛 ValueError。
    """
    cat = normalize_category(category)
    if cat is None:
        raise ValueError(f"未知人员类别: {category!r}")

    by, bm = int(birth_year), int(birth_month)
    if not (1 <= bm <= 12):
        raise ValueError(f"出生月份非法: {bm}")
    if not (1930 <= by <= 2015):
        raise ValueError(f"出生年份超出合理范围: {by}")

    base_months = BASE_AGE_MONTHS[cat]
    orig_idx = _month_index(by, bm) + base_months          # 原退休时间
    start_idx = _month_index(POLICY_START_YEAR, POLICY_START_MONTH)

    if orig_idx < start_idx:
        # 2025-01-01 前退休 → 不延迟
        delayed = False
        delay_months = 0
    else:
        delayed = True
        x = orig_idx - start_idx                            # 距政策起点已过的月数
        delay_months = min(x // DELAY_STEP[cat] + 1, DELAY_CAP[cat])

    total_age_months = base_months + delay_months
    ry, rm = _from_index(orig_idx + delay_months)           # 延迟后的退休时间
    oy, om = _from_index(orig_idx)                          # 原退休时间

    min_months, min_cn = min_contribution_for(ry, rm)

    return {
        "ok": True,
        "category": cat,
        "category_cn": CATEGORY_CN[cat],
        "birth_year": by,
        "birth_month": bm,
        "birth_cn": _fmt_ym(by, bm),
        # 原退休年龄 / 原退休时间
        "base_age_months": base_months,
        "base_age_cn": _fmt_age(base_months),
        "original_retire_year": oy,
        "original_retire_month": om,
        "original_retire_cn": _fmt_ym(oy, om),
        # 延迟情况
        "delayed": delayed,
        "delay_months": delay_months,
        "retire_age_months": total_age_months,
        "retire_age_cn": _fmt_age(total_age_months),
        "retire_year": ry,
        "retire_month": rm,
        "retire_cn": _fmt_ym(ry, rm),
        # 最低缴费年限（按实际退休时间动态计算）
        "min_contribution_months": min_months,
        "min_contribution_years": min_months // 12,        # 数值字段，向后保留
        "min_contribution_cn": min_cn,                      # "15年" / "15年6个月" / … / "20年"
    }


def format_result_cn(res: dict) -> str:
    """把结果格式化成一段中文，便于直接展示或喂给模型。"""
    if not res or not res.get("ok"):
        return "退休时间测算失败：信息不足。"
    lines = [
        f"出生年月：{res['birth_cn']}",
        f"人员类别：{res['category_cn']}",
        f"原退休年龄：{res['base_age_cn']}",
        f"原退休时间：{res['original_retire_cn']}",
    ]
    if res.get("delayed"):
        lines.append(f"延迟月数：{res['delay_months']}个月")
        lines.append(f"延迟退休年龄：{res['retire_age_cn']}")
        lines.append(f"延迟退休时间：{res['retire_cn']}")
    else:
        lines.append("延迟退休年龄：不适用（2025年1月1日前已到龄，按原年龄退休）")
        lines.append(f"退休时间：{res['original_retire_cn']}")
    lines.append(f"最低缴费年限：{res['min_contribution_cn']}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    for args in [(1965, 3, MALE), (1975, 5, FEMALE_WORKER), (1981, 9, FEMALE_CADRE),
                 (1964, 12, MALE), (1976, 9, MALE)]:
        r = calc_retirement(*args)
        print(format_result_cn(r))
        print(f"  最低缴费：{r['min_contribution_cn']}（{r['min_contribution_months']} 个月）")
        print("-" * 32)