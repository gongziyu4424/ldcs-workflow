"""失业保险金测算程序 · 单元测试。

对照业务口径验证：
- 领取资格四项条件（①②③由本程序判，④材料齐全由经办核验）：
  ① 累计缴费满 1 年（≥12 个月）
  ② 非因本人意愿中断就业（法定情形；本人主动辞职不符合）
  ③ 已办理失业登记并有求职要求
- 1-4 年部分（12-48）：每满 12 个月 = 1 个月（不足 12 个月不计）
- 4 年以上（≥48）：每满 6 个月 = 1 个月（不足 6 个月不计）
- 上限 24 个月；金额 = 月数 × 2680 × 90%（= 2412 元/月）

运行：python -m ldcs.test_unemp
"""
from __future__ import annotations

from .unemp_calc import (
    calc_unemp, calc_unemp_benefit, judge_unemp_eligibility,
    paid_months_from_text, reason_from_text, registered_from_text,
)

# (缴费月数, 期望是否合格, 期望领取月数, 期望总金额)
CASES = [
    (0,    False, None, None),
    (6,    False, None, None),
    (11,   False, None, None),
    (12,   True,  1,  2412),      # 12/12=1
    (15,   True,  1,  2412),      # 不足 24 → 仍 1
    (23,   True,  1,  2412),      # 边界：差 1 个月不满 2 年
    (24,   True,  2,  4824),      # 2 年整
    (36,   True,  3,  7236),
    (47,   True,  3,  7236),      # 不到 4 年 → 3
    (48,   True,  4,  9648),      # 4 年整：48/12=4；超出 0
    (49,   True,  4,  9648),      # 超出 1 个月：1/6 不足 → 0
    (53,   True,  4,  9648),      # 超出 5 个月：不足 6 → 0
    (54,   True,  5,  12060),     # 超出 6 个月：+1
    (59,   True,  5,  12060),     # 超出 11 个月：仍 1
    (60,   True,  6,  14472),     # 超出 12：+2
    (72,   True,  8,  19296),     # 超出 24：+4
    (96,   True,  12, 28944),     # 超出 48：+8
    (144,  True,  20, 48240),     # 超出 96：+16
    (156,  True,  22, 53064),     # 超出 108：+18
    (168,  True,  24, 57888),     # 超出 120：+20 → 4+20=24 刚好封顶
    (180,  True,  24, 57888),     # 超出 132：+22 → 26 → 封顶 24
    (288,  True,  24, 57888),
    (500,  True,  24, 57888),     # 远超 → 封顶
]

# ⚠️ 「起止式」说法（公子宇 2026-09-14 截图 bug）：句中的「16年」是**年份**，不是 16 年时长。
#    口径：年数 = 结束年 − 起始年，「到现在」的结束年取上一个完整年度。
#    基准年固定为 2026 传入，避免测试结果随系统年份漂移。
RANGE_TEXT_CASES = [
    ("从16年到现在",        108),   # 2025 − 2016 = 9 年
    ("从2016年到现在",      108),
    ("16年到现在没断过",      108),
    ("从16年开始缴到现在",     108),
    ("2016年开始缴，一直没断过", 108),
    ("从一六年缴到现在",       108),
    ("从2018年到现在",       84),   # 7 年
    ("从2010年到现在",      180),   # 15 年
    ("从05年到现在",        240),   # 20 年
    ("从16年到25年",        108),   # 显式终止年，口径同上
    # 反例：这两条是**时长**、不是起止，不能被带跑
    ("缴了12年，一直没断过",    144),
    ("缴了16年",            192),
    ("从今年3月开始缴",         3),   # 没给起止年份 → 仍走时长解析
    ("缴了两年",              24),
]

MONTHS_TEXT_CASES = [
    ("3年", 36),
    ("18个月", 18),
    ("3年6个月", 42),
    ("缴了两年", 24),
    ("一年零三个月", 15),
    ("半年", 6),
    ("我缴费24个月", 24),
    ("我不记得了", None),
    ("湖北的", None),
    ("", None),
    (None, None),
]

# 资格判定：缴费月数 / 离职情形码 / 是否已办失业登记 → 期望资格 + 期望失败项
ELIG_CASES = [
    (36, "laid_off",       True,  True,  []),
    (12, "contract_end",   True,  True,  []),
    (60, "employer_fault", True,  True,  []),
    (48, "mutual",         True,  True,  []),
    (48, "dismissed",      True,  True,  []),
    (48, "other_lawful",   True,  True,  []),
    (36, "voluntary",      True,  False, ["reason"]),          # 主动辞职 → 条件②不符
    (36, "laid_off",       False, False, ["register"]),        # 没办失业登记 → 条件③不符
    (36, None,             None,  False, ["reason", "register"]),
    (8,  "laid_off",       True,  False, ["pay"]),             # 缴费不满 1 年 → 条件①不符
    (8,  "voluntary",      False, False, ["pay", "reason", "register"]),
    (11, "laid_off",       True,  False, ["pay"]),
]

# 离职原因原话 → 情形码（认不出 None，不猜）
# ⚠️ 这只是**模型给不出码时的兜底**（主路径是模型按语义直接判 reason_code）。
#    下面几条「裁」字被夹在中间的说法是真机漏过的用例（公子宇 2026-09-14 截图），锁死别再漏。
REASON_TEXT_CASES = [
    ("劳动合同到期了", "contract_end"),
    ("公司破产了", "contract_end"),
    ("单位被吊销营业执照", "contract_end"),
    ("单位把我辞退了", "dismissed"),
    ("因为违纪被开除", "dismissed"),
    ("单位裁员", "laid_off"),
    ("公司经营困难优化人员", "laid_off"),
    ("被工厂裁了", "laid_off"),          # ← 真机漏过：正则只有「被裁」两字相邻
    ("是被工厂裁掉的", "laid_off"),        # ← 真机漏过
    ("刚刚被公司裁了", "laid_off"),
    ("厂里不要我了", "laid_off"),
    ("不让我干了", "laid_off"),
    ("双方协商一致解除", "mutual"),
    ("公司拖欠工资我被迫辞职", "employer_fault"),
    ("单位没依法缴社保", "employer_fault"),
    ("我自己主动辞职的", "voluntary"),
    ("想换个工作就辞了", "voluntary"),
    ("缴了3年", None),
    ("失业金能领多少", None),
    ("", None),
    (None, None),
]

# 失业登记原话 → 布尔（看不出 None）
# ⚠️ 口径（公子宇 2026-09-14）：这一项**只问是否办理了失业登记**，
#    求职要求由经办核验，所以「我在找工作 / 不打算找工作」都**不能**当登记项的信号 → None。
REGISTERED_TEXT_CASES = [
    ("还没办失业登记", False),
    ("没办过失业登记", False),
    ("失业登记还没弄", False),
    ("失业登记没办", False),
    ("已经办了失业登记", True),
    ("失业登记办好了", True),
    ("已经登记了", True),
    ("办过失业登记", True),
    ("不打算找工作了", None),
    ("我在找工作", None),
    ("失业金能领多少", None),
    ("缴了5年", None),
    ("不记得了", None),
]


def main():
    print("失业保险金测算 · 单元测试")
    print("=" * 62)
    fails = []
    for m, elig, months, total in CASES:
        r = calc_unemp_benefit(m)
        tag = f"缴费{m:>3}个月"
        if r["eligible"] != elig:
            fails.append(f"{tag} 资格 期望{elig} 实得{r['eligible']}")
            continue
        if not elig:
            print(f"  [PASS] {tag} → 不合格（{r['reason']}）")
            continue
        if r["months"] != months:
            fails.append(f"{tag} 月数 期望{months} 实得{r['months']}")
            continue
        if r["total"] != total:
            fails.append(f"{tag} 金额 期望{total} 实得{r['total']}")
            continue
        print(f"  [PASS] {tag} → 领 {r['months']} 个月 → {r['formula_cn']}"
              + ("（封顶）" if r["capped"] else ""))

    # ---- 资格四项条件（①②③ 程序判；④材料齐全由经办核验，不在本程序范围）----
    for m, rc, reg, elig, failed in ELIG_CASES:
        r = judge_unemp_eligibility(m, rc, reg)
        got = sorted(r["failed"])
        if r["eligible"] != elig or got != sorted(failed):
            fails.append(f"资格 缴费{m} 原因{rc} 登记{reg} "
                         f"期望 eligible={elig} failed={failed} "
                         f"实得 eligible={r['eligible']} failed={got}")
            continue
        tag = f"缴费{m:>3} 原因{str(rc):<13} 登记{str(reg):<5}"
        print(f"  [PASS] {tag} → {'符合' if elig else '不符合：' + r['fail_cn']}")

    # calc_unemp：够条件才带月数金额；不够只给资格判断
    r_ok = calc_unemp(60, "laid_off", True)
    if not (r_ok["eligible"] and r_ok["months"] == 6 and r_ok["total"] == 14472):
        fails.append(f"calc_unemp 符合 期望 6 个月/14472，实得 {r_ok.get('months')}/{r_ok.get('total')}")
    else:
        print("  [PASS] calc_unemp(60, laid_off, True) → 6 个月 / 14472")
    r_bad = calc_unemp(60, "voluntary", True)
    if r_bad["eligible"] or "months" in r_bad or not r_bad.get("fail_cn"):
        fails.append(f"calc_unemp 主动辞职 应只给资格判断，实得 {r_bad}")
    else:
        print(f"  [PASS] calc_unemp(60, voluntary, True) → 不符合（{r_bad['fail_cn']}）")
    r_low = calc_unemp(8, "laid_off", True)
    if r_low["eligible"] or "months" in r_low:
        fails.append(f"calc_unemp 缴费不足 应只给资格判断，实得 {r_low}")
    else:
        print(f"  [PASS] calc_unemp(8, laid_off, True) → 不符合（{r_low['fail_cn']}）")

    # 「失业登记没办」= 要走办理手续 → 建议里必须带「粤省事」办理指引 + 稍候短信；
    # 「主动辞职」只是不符合条件、并无手续要办 → 不该给粤省事指引。
    r_reg = judge_unemp_eligibility(36, "laid_off", False)
    if ("register" in r_reg["failed"]
            and "粤省事" in r_reg["advice_cn"] and "短信" in r_reg["advice_cn"]):
        print(f"  [PASS] 没办失业登记 → 引导粤省事办理（{r_reg['advice_cn']}）")
    else:
        fails.append(f"没办失业登记应引导粤省事办理，实得 {r_reg['advice_cn']!r}")

    # 分层口径（公子宇 2026-09-14）：「只差办失业登记」≠「不符合领取条件」——
    # qualified（①②符合）= 真；blocked 为空；pending=['register']；eligible=False；
    # 且 calc_unemp **仍要把月数/金额算出来**（结论要告诉他能领多少 + 提醒去办登记）。
    r_pend = calc_unemp(36, "laid_off", False)
    if not (r_pend["qualified"] and r_pend["blocked"] == []
            and r_pend["pending"] == ["register"] and not r_pend["eligible"]
            and r_pend.get("months") and r_pend.get("total")):
        fails.append(f"只差失业登记应 qualified=True 且带金额、eligible=False，实得 {r_pend}")
    else:
        print(f"  [PASS] 只差失业登记 → 符合且能算（领 {r_pend['months']} 个月 / "
              f"{r_pend['total']} 元）；说法：{r_pend['fail_cn']}")
    if "不符合" in r_pend["fail_cn"] or "不符合" in r_pend["advice_cn"]:
        fails.append(f"只差失业登记不许出现「不符合」，实得 {r_pend['fail_cn']!r}")
    else:
        print("  [PASS] 只差失业登记 → fail_cn / advice_cn 均不含「不符合」")
    if "失业登记" not in r_pend["advice_cn"] or "粤省事" not in r_pend["advice_cn"]:
        fails.append(f"只差失业登记应点名「失业登记」并引导粤省事，实得 {r_pend['advice_cn']!r}")
    else:
        print("  [PASS] 只差失业登记 → 建议点名「失业登记」+ 粤省事")

    # 抽取提示词里的枚举码必须与 REASON_CODES 一致 ——
    # 词表改了、提示词没同步 = 模型给出的码会被白名单拒掉（识别白做）
    from ldcs import config as _cfg
    from ldcs.unemp_calc import REASON_CODES
    _prompt = _cfg.DEFAULT_CONFIG["system"]["extract_unemp_system"]
    _lack = [c for c in sorted(REASON_CODES) if c not in _prompt]
    if _lack:
        fails.append(f"抽取提示词缺少情形码 {_lack}（模型给码会被白名单拒掉）")
    else:
        print(f"  [PASS] 抽取提示词含全部 {len(REASON_CODES)} 个情形码"
              "（与 REASON_CODES 同步）")
    r_vol = judge_unemp_eligibility(36, "voluntary", True)
    if "粤省事" in r_vol["advice_cn"]:
        fails.append(f"主动辞职不该出现粤省事指引，实得 {r_vol['advice_cn']!r}")
    else:
        print("  [PASS] 主动辞职 → 建议里不出现粤省事（只提示单位原因可重判）")

    # 自定义最低工资（config.business 可覆盖的口子）：12 个月 = 1 个月 → 1×3000×90%
    r = calc_unemp_benefit(12, min_wage=3000)
    if r["total"] != 1 * 3000 * 0.9:
        fails.append(f"自定义最低工资 3000 应得 2700，实得 {r['total']}")
    else:
        print("  [PASS] 自定义最低工资 3000 → 2700")

    # 原话解析（程序兜底）
    for text, expect in MONTHS_TEXT_CASES:
        got = paid_months_from_text(text)
        if got != expect:
            fails.append(f"原话 {text!r} 解析 期望{expect} 实得{got}")
        else:
            print(f"  [PASS] 原话 {text!r} → {expect}")

    # 起止式说法（基准年固定 2026）
    for text, expect in RANGE_TEXT_CASES:
        got = paid_months_from_text(text, now_year=2026)
        if got != expect:
            fails.append(f"起止式 {text!r} 期望{expect} 实得{got}")
        else:
            print(f"  [PASS] 起止式 {text!r} → {expect}")

    # 离职原因原话 → 情形码
    for text, expect in REASON_TEXT_CASES:
        got = reason_from_text(text)
        if got != expect:
            fails.append(f"离职原因 {text!r} 期望{expect} 实得{got}")
        else:
            print(f"  [PASS] 离职原因 {text!r} → {expect}")

    # 失业登记 / 求职要求原话 → 布尔
    for text, expect in REGISTERED_TEXT_CASES:
        got = registered_from_text(text)
        if got != expect:
            fails.append(f"失业登记 {text!r} 期望{expect} 实得{got}")
        else:
            print(f"  [PASS] 失业登记 {text!r} → {expect}")

    # 非法入参
    for bad in [-1, "abc", None if False else ""]:
        try:
            calc_unemp_benefit(bad)
            fails.append(f"入参 {bad!r} 应抛 ValueError")
        except ValueError:
            print(f"  [PASS] 入参 {bad!r} → ValueError")

    print("=" * 62)
    if fails:
        print(f"FAIL：{len(fails)} 项断言未通过")
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print(f"PASS：{len(CASES)} 组对照 + 资格 {len(ELIG_CASES)} 组 + "
          f"原话解析 {len(MONTHS_TEXT_CASES)}/{len(REASON_TEXT_CASES)}/"
          f"{len(REGISTERED_TEXT_CASES)} 条 + 起止式 {len(RANGE_TEXT_CASES)} 条 "
          f"+ 边界全部通过")


if __name__ == "__main__":
    main()