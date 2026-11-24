"""退休时间测算程序 · 单元测试。

对照《渐进式延迟法定退休年龄时间表》官方口径验证边界：
- 男职工（原 60）→ 63；原 55 岁女职工 → 58；原 50 岁女职工 → 55
- 2025-01-01 前到龄不延迟
- 每 4 个月 / 每 2 个月延迟 1 个月的档位换算

运行：python -m ldcs.test_retire
"""
from __future__ import annotations

from .retire_calc import (
    FEMALE_CADRE, FEMALE_WORKER, MALE, calc_retirement, category_from_text,
    format_result_cn, min_contribution_for, normalize_category,
)

# (出生年, 出生月, 类别, 期望延迟月数, 期望退休年龄, 期望退休年月)
CASES = [
    # ---- 男职工：原 60 岁，每 4 个月延迟 1 个月，封顶 63 ----
    (1964, 12, MALE, 0, "60周岁", (2024, 12)),      # 2025 前到龄，不延迟
    (1965, 1, MALE, 1, "60周岁1个月", (2025, 2)),
    (1965, 4, MALE, 1, "60周岁1个月", (2025, 5)),
    (1965, 5, MALE, 2, "60周岁2个月", (2025, 7)),
    (1965, 8, MALE, 2, "60周岁2个月", (2025, 10)),
    (1965, 9, MALE, 3, "60周岁3个月", (2025, 12)),
    (1966, 1, MALE, 4, "60周岁4个月", (2026, 5)),
    (1976, 9, MALE, 36, "63周岁", (2039, 9)),        # 封顶 63
    (1977, 1, MALE, 36, "63周岁", (2040, 1)),        # 封顶后维持 63

    # ---- 原 55 岁女职工（女干部）：每 4 个月延迟 1 个月，封顶 58 ----
    (1969, 12, FEMALE_CADRE, 0, "55周岁", (2024, 12)),
    (1970, 1, FEMALE_CADRE, 1, "55周岁1个月", (2025, 2)),
    (1970, 5, FEMALE_CADRE, 2, "55周岁2个月", (2025, 7)),
    (1981, 9, FEMALE_CADRE, 36, "58周岁", (2039, 9)),   # 封顶 58

    # ---- 原 50 岁女职工（女工人）：每 2 个月延迟 1 个月，封顶 55 ----
    (1974, 12, FEMALE_WORKER, 0, "50周岁", (2024, 12)),
    (1975, 1, FEMALE_WORKER, 1, "50周岁1个月", (2025, 2)),
    (1975, 3, FEMALE_WORKER, 2, "50周岁2个月", (2025, 5)),
    (1975, 5, FEMALE_WORKER, 3, "50周岁3个月", (2025, 8)),
    (1984, 10, FEMALE_WORKER, 59, "54周岁11个月", (2039, 9)),
    (1984, 11, FEMALE_WORKER, 60, "55周岁", (2039, 11)),  # 封顶 55
    (1985, 1, FEMALE_WORKER, 60, "55周岁", (2040, 1)),
]

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print(f"  ✗ {msg}")


def main():
    print("=" * 62)
    print("退休时间测算 · 单元测试")
    print("=" * 62)

    for by, bm, cat, exp_delay, exp_age, (ey, em) in CASES:
        r = calc_retirement(by, bm, cat)
        tag = f"{by}-{bm:02d} {r['category_cn']}"
        check(r["delay_months"] == exp_delay,
              f"{tag} 延迟月数 期望{exp_delay} 实得{r['delay_months']}")
        check(r["retire_age_cn"] == exp_age,
              f"{tag} 退休年龄 期望{exp_age} 实得{r['retire_age_cn']}")
        check((r["retire_year"], r["retire_month"]) == (ey, em),
              f"{tag} 退休时间 期望{ey}-{em} 实得{r['retire_year']}-{r['retire_month']}")
        # 最低缴费年限：必须按实际退休时间算出（2030 起每年 +6 个月，上限 20 年）
        exp_min_m, exp_min_cn = min_contribution_for(ey, em)
        check(r["min_contribution_months"] == exp_min_m,
              f"{tag} 最低缴费月数 期望{exp_min_m} 实得{r['min_contribution_months']}")
        check(r["min_contribution_cn"] == exp_min_cn,
              f"{tag} 最低缴费年限 期望{exp_min_cn} 实得{r['min_contribution_cn']}")
        print(f"  · {tag} → 原{r['original_retire_cn']} / {r['retire_age_cn']} / "
              f"{r['retire_cn']} / 最低{r['min_contribution_cn']}")

    # 原退休时间 = 出生年月 + 原退休年龄
    r = calc_retirement(1965, 3, MALE)
    check(r["original_retire_cn"] == "2025年3月", f"原退休时间应为 2025年3月，实得{r['original_retire_cn']}")
    check(r["base_age_cn"] == "60周岁", f"原退休年龄应为 60周岁，实得{r['base_age_cn']}")

    # 不延迟场景：延迟字段应明确标注不适用
    r = calc_retirement(1963, 6, MALE)
    check(r["delayed"] is False, "1963-06 男职工不应延迟")
    check(r["retire_cn"] == r["original_retire_cn"], "不延迟时退休时间应等于原退休时间")
    check(r["retire_age_cn"] == "60周岁", "不延迟时退休年龄应为 60周岁")

    # 别名归一
    for alias, expect in [("男", MALE), ("男职工", MALE), ("女干部", FEMALE_CADRE),
                          ("女职工", None), ("女工人", FEMALE_WORKER),
                          ("原55", FEMALE_CADRE), ("原50", FEMALE_WORKER),
                          ("male", MALE), ("female_worker", FEMALE_WORKER)]:
        check(normalize_category(alias) == expect, f"别名 {alias} 应归一为 {expect}")
    check(normalize_category("火星人") is None, "无法识别的类别应返回 None")

    # 用户原话 → 类别（程序兜底）：抽取层漏抽时靠这个兜住，不能再问第二遍
    for text, gender, expect in [
        ("女工人", None, FEMALE_WORKER),
        ("我是女工人", None, FEMALE_WORKER),
        ("女干部，1981年9月出生", None, FEMALE_CADRE),
        ("女职工", None, None),            # 歧义说法不能猜
        ("女", None, None),                # 只知道性别，不能猜
        ("我是管理岗", None, None),          # 不带性别不能猜
        ("我是管理岗", "female", FEMALE_CADRE),
        ("女的，工人", "female", FEMALE_WORKER),
        ("工人身份", "female", FEMALE_WORKER),
        # 用户常直接用岗位描述回答「干部还是工人」，必须能落类别
        ("在工厂打工", "female", FEMALE_WORKER),
        ("厂里上班", "female", FEMALE_WORKER),
        ("车间", "female", FEMALE_WORKER),
        ("普工", "female", FEMALE_WORKER),
        ("坐办公室", "female", FEMALE_CADRE),
        ("在事业单位", "female", FEMALE_CADRE),
        ("教师", "female", FEMALE_CADRE),
        # 性别未知时不能凭岗位描述猜（「在工厂打工」也可能是男的）
        ("在工厂打工", None, None),
        ("在工厂打工", "male", None),
        ("男的", "male", MALE),
    ]:
        check(category_from_text(text, gender) == expect,
              f"原话 {text!r}(gender={gender}) 应判为 {expect}")

    # 非法入参
    for bad in [((1965, 13, MALE), "月份越界"),
                ((1900, 1, MALE), "年份越界"),
                ((1965, 3, "未知类别"), "类别非法")]:
        args, label = bad
        try:
            calc_retirement(*args)
            check(False, f"{label} 应抛 ValueError")
        except ValueError:
            pass

    # 文本格式化
    txt = format_result_cn(calc_retirement(1965, 3, MALE))
    for kw in ["出生年月", "原退休年龄", "原退休时间", "延迟退休年龄", "延迟退休时间", "最低缴费年限"]:
        check(kw in txt, f"格式化文本应包含「{kw}」")

    print("=" * 62)
    total = len(CASES)
    if FAIL:
        print(f"FAIL：{len(FAIL)} 项断言未通过")
        for m in FAIL:
            print("  -", m)
        raise SystemExit(1)
    print(f"PASS：{total} 组对照用例 + 归一/边界/格式化断言全部通过")


if __name__ == "__main__":
    main()
