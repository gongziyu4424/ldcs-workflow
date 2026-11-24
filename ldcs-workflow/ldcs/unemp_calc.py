"""失业保险金测算程序 —— 按业务口径计算领取资格 / 领取月数 / 总金额。

规则（业务口径）：
0. 领取资格（**四项条件**；条件④「材料齐全且真实有效」不在本程序判定范围，
   由经办环节核验）：① 缴费义务满 1 年（累计缴费 ≥ 12 个月）；
   ② 非因本人意愿中断就业（见 INVOLUNTARY_REASONS 六种情形）；
   ③ 已办理失业登记（求职要求由经办环节核验，流程里只问「是否办了登记」）。
   条件①② 不符合 = 真不符合；条件③ 没办 = **可补救手续**，办了登记就能申请 ——
   这时仍要把月数/金额算给他，并提醒他去办登记，不能说「不符合领取条件」。
   三项都满足 → 直接给月数金额；①②满足但未登记 → 给月数金额 + 提醒办登记；
   否则给出「哪几项不符合」+ 一句下步建议。
1. 领取月数（**取整，不足一整段不计**）：
   · 1-4 年部分（12-48 个月）：每满 12 个月 = 1 个月失业保险金。
   · 4 年以上部分（≥48 个月）：每满 6 个月 = 1 个月失业保险金。
   · 领取月数最长不超过 24 个月。
2. 总金额 = 领取月数 × 最低工资标准（2680 元/月）× 90%。

对外接口：
  calc_unemp(paid_months, reason_code, registered)   # 资格 + 月数金额（flow 用）
  calc_unemp_benefit(paid_months)                    # 只算月数金额（兼容/单测用）
  reason_from_text(text) / registered_from_text(text) # 原话兜底解析
"""
from __future__ import annotations

import datetime as _dt
import re

from typing import Optional

# 最低工资标准（元/月）—— 业务口径，可被 config.business.unemp_min_wage 覆盖
DEFAULT_MIN_WAGE = 2680
# 发放比例
PAY_RATE = 0.9
# 领取月数上限
CAP_MONTHS = 24
# 领取资格门槛：累计缴费 ≥ 12 个月
ELIGIBLE_MIN_MONTHS = 12
# 两段折算：每满 X 个月 = 1 个月失业金（余数不足一整段不计）
SEG1_END = 48                     # 1-4 年部分上界（含 48）
SEG1_FULL = 12                    # 1-4 年部分：每满 12 个月 = 1 个月失业金
SEG2_FULL = 6                     # 4 年以上部分：每满 6 个月 = 1 个月失业金


# ---------------------------------------------------------------------------
# 条件②「非因本人意愿中断就业」的法定情形（业务口径只在本文件与单测里维护）
# ---------------------------------------------------------------------------
INVOLUNTARY_REASONS: dict[str, str] = {
    "contract_end":   "劳动合同期满，或用人单位主体资格消亡（破产、注销、吊销）终止劳动合同",
    "laid_off":       "用人单位因生产经营严重困难、客观情况发生重大变化等原因解除劳动合同",
    "dismissed":      "被用人单位开除、除名、辞退",
    "mutual":         "用人单位与劳动者协商一致解除劳动合同",
    "employer_fault": "用人单位违法（欠薪、未依法缴纳社保等），劳动者单方解除劳动合同",
    "other_lawful":   "法律、法规、规章规定的其他情形",
}
# 话术用的短标签（结论只说结果，不长篇讲政策）
INVOLUNTARY_SHORT: dict[str, str] = {
    "contract_end":   "合同期满终止",
    "laid_off":       "单位裁员",
    "dismissed":      "被单位开除辞退",
    "mutual":         "双方协商一致解除",
    "employer_fault": "单位欠薪或未缴社保",
    "other_lawful":   "其他法定情形",
}
# 因本人意愿中断就业（主动辞职）—— 不符合条件②
VOLUNTARY_REASON = "voluntary"
VOLUNTARY_LABEL = "本人主动辞职（属因本人意愿中断就业）"

# ---- 交给模型直接判定的「离职情形码」白名单 + 大白话说明 ----
# ⚠️ 分工（2026-09-14 公子宇纠正）：**识别走模型，词表只兜底**。
#   用户说「被工厂裁了」这种夹了字的说法，正则很容易漏（旧词表只有「被裁」两字相邻）；
#   而「这是不是他本人主动想走的」本来就是模型最擅长的语义判断。
#   所以由模型按语义给出下列码之一，程序只做白名单校验；
#   `_REASON_PATTERNS` 仅在模型给不出码时兜底（宁缺勿错，认不出就回头问）。
#   提示词里给的就是这张表（码 + 一句人话），不再让模型"只许照抄、不许归类"。
REASON_CODE_HINTS: dict[str, str] = {
    "contract_end":   "劳动合同期满终止，或单位破产 / 注销 / 吊销导致合同终止",
    "laid_off":       "单位裁员、缩编、撤岗，或单位单方面解除劳动合同",
    "dismissed":      "被单位开除、除名、辞退、解雇",
    "mutual":         "单位与本人协商一致解除劳动合同",
    "employer_fault": "单位违法（欠薪、未依法缴社保等），本人被迫离职",
    "other_lawful":   "其他非因本人意愿中断就业的法定情形",
    "voluntary":      "本人主动提出辞职（自己不想干了 / 想换个工作）",
}
# 白名单（含 voluntary，用于校验模型输出）
REASON_CODES: frozenset = frozenset(REASON_CODE_HINTS)

# 原话 → 情形码 的**兜底**词表（模型给不出码时才用；顺序即优先级）
# ⚠️ 不要把它当主判定：正则一定漏（「被工厂裁了」这种夹字说法就漏过）；
#   这里只补最稳的几类，宁可返回 None 回头问一句，也不要判反本人意愿/单位原因。
_REASON_PATTERNS: list[tuple[str, str]] = [
    ("employer_fault",
     r"(欠薪|拖欠工资|没(给|有)?缴社保|没依法缴|未依法缴|未缴社保|不缴社保|不发工资|少发工资|"
     r"被迫辞|逼我辞|单位违法|单位违规)"),
    ("dismissed", r"(开除|除名|辞退|被解雇|被炒|因违纪|被劝退|被清退)"),
    # 「裁」字常被别的词夹在中间（被**工厂**裁了），故允许中间隔几个字
    ("laid_off",
     r"(裁员|裁人|裁掉|裁减|经济性裁员|被裁|"
     r"(工厂|厂里|单位|公司|企业|车间)[^，。；、！？]{0,4}裁|"
     r"单位解除|公司解除|岗位取消|撤岗|精简|优化|经营困难|停产|"
     r"单位原因|公司原因|被下岗|不要我了|让我走人|不让我干了)"),
    ("contract_end",
     r"(合同到期|合同期(满|限到了)|劳动合同终止|单位破产|公司破产|企业破产|公司倒闭|"
     r"单位倒闭|注销|吊销|主体资格(消亡|灭失)|单位没了|公司黄了|关门)"),
    ("mutual", r"(协商一致|双方协商|商量着?解除|谈好了解除)"),
    ("voluntary",
     r"(主动辞|自己辞|我要辞|提出辞职|本人提出离职|自己离职|自己走的|个人原因|"
     r"不想干|另找工作|跳槽|不在那干了|辞职|辞了|辞掉|不干了|换(个|份)?工作)"),
]


def reason_from_text(text: Optional[str]) -> Optional[str]:
    """从用户原话里解析「中断就业原因」情形码（程序兜底，抽取层漏抽时用）。

    认不出返回 None —— **不猜**：宁可回头问一句，也不把本人意愿/单位原因判反。
    """
    if not text:
        return None
    t = str(text)
    for code, pat in _REASON_PATTERNS:
        if re.search(pat, t):
            return code
    return None


def registered_from_text(text: Optional[str]) -> Optional[bool]:
    """从原话里解析「是否已办理失业登记」；看不出返回 None（**不猜**）。

    ⚠️ 口径（公子宇 2026-09-14）：这一项**只问是否办理了失业登记**，
    不再把「有求职要求」并进来问（求职要求由经办环节核验，和材料齐全一样）。
    所以这里也只认「登记」的信号：
      · 明确说「还没办失业登记」→ False；明确说「办了 / 已经登记」→ True；
      · 只说「我在找工作」→ 不能当成办过登记 → None（回头把登记这一项问清）；
      · 完全没提 → None。
    """
    if not text:
        return None
    t = str(text)
    # ① 明确否认办过失业登记（「还没办」「没去登记」「没办过失业登记」）
    if re.search(r"(还没|没有|没|未|尚未|不曾|还没来得及)\s*(去)?\s*"
                 r"(办理|办|做|搞|进行)?\s*(过)?\s*(失业登记|登记)", t):
        return False
    # 「失业登记」在后（「失业登记还没弄」「登记还没办」）
    if re.search(r"(失业登记|登记)[^，。；、！？]{0,4}(还|都)?\s*(没|未)(有)?\s*"
                 r"(办|做|弄|登记|办理)?", t):
        return False
    # ② 明确办过失业登记（动词可在「失业登记」前或后）
    if re.search(r"(失业登记|登记)(手续)?[^，。；、！？]{0,6}"
                 r"(已经?|也|都)?\s*(办|做|进行|登记)[好完]?了?", t):
        return True
    if re.search(r"(已经?|已|也|都)\s*(办理|办|做|进行|登记)[好完]?了?"
                 r"[^，。；、！？]{0,3}(失业登记)", t):
        return True
    if re.search(r"(失业登记|登记)(手续)?(都|也|已经?)?\s*(办|做)?[好完]?了", t):
        return True
    if re.search(r"(办理|办|做|进行|登记)过[^，。；、！？]{0,3}(失业登记)", t):
        return True
    if re.search(r"(失业登记|登记)[^，。；、！？]{0,3}(办|办理|登记)过", t):
        return True
    # ③ 没提过登记（哪怕说了「我在找工作」）→ None，回头把「是否办理失业登记」问清
    return None


def judge_unemp_eligibility(paid_months, reason_code=None, registered=None,
                            min_wage: int = DEFAULT_MIN_WAGE) -> dict:
    """按条件判「能不能领失业保险金」（条件④材料齐全由经办环节核验，不在本程序判定）。

    ⚠️ 关键口径（公子宇 2026-09-14 纠正）：
    三条可判条件要分两层 ——
      · 条件① 缴费满 1 年、条件② 非本人意愿失业 = **资格本身**，不符合就是真不符合；
      · 条件③ 已办失业登记 = **可补救的申领手续**，没办 ≠ 不符合资格，
        而是「办了登记就能申请」。所以「只差失业登记」绝不能输出「不符合领取条件」。

    返回 dict：
      qualified   —— 资格本身是否符合（① 且 ②）
      eligible    —— 三项是否全部就绪（qualified 且 已办失业登记）→ 可直接申领
      checks      —— {"pay": bool, "reason": bool, "register": bool}
      failed      —— 不满足的项（"pay" / "reason" / "register"）
      blocked     —— 真正无法补救的不符合项（"pay" / "reason"）
      pending     —— 只差一条可补救手续的项（"register"）
      fail_cn     —— 供话术直接转述的「哪几项不符合 / 还差哪一步」短句
      advice_cn   —— 一句下步建议（程序给，不让模型自己编政策）
    非法入参抛 ValueError。
    """
    m = _to_int(paid_months)
    if m is None or m < 0:
        raise ValueError(f"累计缴费月数非法: {paid_months!r}")

    checks = {
        "pay": m >= ELIGIBLE_MIN_MONTHS,
        "reason": reason_code in INVOLUNTARY_REASONS,
        "register": registered is True,
    }
    failed = [k for k, ok in checks.items() if not ok]
    blocked = [k for k in failed if k != "register"]
    pending = [k for k in failed if k == "register"]
    qualified = not blocked

    fail_cn, advice_cn = [], []
    if "pay" in failed:
        fail_cn.append(f"累计缴费 {_fmt_months(m)}，未满 1 年")
        advice_cn.append("继续参保缴费，累计满 1 年后即可申领")
    if "reason" in failed:
        fail_cn.append("这次是本人主动辞职，不属于「非因本人意愿中断就业」")
        advice_cn.append("如果是单位原因导致的离职（合同期满、被辞退、单位欠薪等），"
                         "可以说一下当时的情况，我帮您重新判断")
    if "register" in failed:
        if qualified:
            # 资格已符合，只差办失业登记这道手续 —— 说「还差一步」，不说「不符合」
            fail_cn.append("缴费和离职情形都符合，还差办理失业登记这一步")
            advice_cn.append("办理失业登记后就可以申领失业保险金了，"
                             "可以先登录「粤省事」办理失业登记，"
                             "具体办理路径稍候会通过短信发给您")
        else:
            fail_cn.append("还没有办理失业登记")
            # 「失业登记没办」这类**要走办理手续**的，一律引导「粤省事」办理，
            # 具体办理路径走短信（与 guide_ss 同一口径：不报菜单、不推窗口）。
            advice_cn.append("可以先登录「粤省事」办理失业登记，"
                             "具体办理路径稍候会通过短信发给您")

    return {
        "ok": True,
        "qualified": qualified,
        "eligible": not failed,
        "paid_months": m,
        "paid_cn": _fmt_months(m),
        "checks": checks,
        "failed": failed,
        "blocked": blocked,
        "pending": pending,
        "reason_code": reason_code,
        "reason_cn": (INVOLUNTARY_SHORT.get(reason_code)
                      or (VOLUNTARY_LABEL if reason_code == VOLUNTARY_REASON else "")),
        "registered": registered,
        "fail_cn": "；".join(fail_cn),
        "advice_cn": "；".join(advice_cn),
    }


def calc_unemp(paid_months, reason_code=None, registered=None,
               min_wage: int = DEFAULT_MIN_WAGE) -> dict:
    """flow 用的统一入口：先判条件①②③，**只要资格本身符合（①②）就把月数/金额算出来**。

    因为「还没办失业登记」只是差一道手续，办了就能申请 —— 结论要能顺带告诉
    他能领多少，而不是把他挡在门外（so eligible=False 但有 qualified=True 时也有金额）。
    ``eligible`` 一律以资格判定为准，不被金额函数覆盖。

    资格不符合（缴费不满 1 年 / 本人主动辞职）→ 只给资格判断（含 fail_cn / advice_cn），不算月数。
    """
    res = judge_unemp_eligibility(paid_months, reason_code, registered, min_wage)
    if not res["qualified"]:
        return res
    ben = calc_unemp_benefit(res["paid_months"], min_wage)
    ben.pop("eligible", None)      # 资格结论由 judge 负责，别被金额函数覆盖
    return {**res, **ben}


def calc_unemp_benefit(paid_months, min_wage: int = DEFAULT_MIN_WAGE) -> dict:
    """按累计缴费月数计算失业保险金。

    参数：
      paid_months —— 累计缴费月数（接受 "3年" / "3年6个月" 之外的 int/float/str 数字）
      min_wage —— 最低工资标准（元/月），默认 2680

    返回：dict
      eligible=False 时只含资格判断结论；
      eligible=True 时含领取月数与总金额。
    非法入参抛 ValueError。
    """
    m = _to_int(paid_months)
    if m is None or m < 0:
        raise ValueError(f"累计缴费月数非法: {paid_months!r}")

    if m < ELIGIBLE_MIN_MONTHS:
        return {
            "ok": True,
            "eligible": False,
            "paid_months": m,
            "paid_cn": _fmt_months(m),
            "reason": "累计缴费不足12个月",
            "reason_cn": f"您累计缴费 {m} 个月，不足 12 个月，暂不具备领取失业保险金的资格。",
        }

    # 1-4 年部分：12-48 个月（每满 12 个月 = 1 个月；不足 12 个月已被资格挡掉）
    part1 = min(m, SEG1_END)
    seg1 = part1 // SEG1_FULL
    # 4 年以上部分：≥48 个月（每满 6 个月 = 1 个月）
    over = max(m - SEG1_END, 0)
    seg2 = over // SEG2_FULL

    months = min(seg1 + seg2, CAP_MONTHS)
    total = round(months * min_wage * PAY_RATE)
    return {
        "ok": True,
        "eligible": True,
        "paid_months": m,
        "paid_cn": _fmt_months(m),
        "seg1_months": seg1,               # 1-5 年部分折算
        "seg2_months": seg2,               # 5 年以上部分折算
        "months": months,                  # 最终领取月数（封顶后）
        "capped": seg1 + seg2 > CAP_MONTHS,
        "months_cn": f"{months}个月",
        "min_wage": int(min_wage),
        "total": total,
        "total_cn": f"{total:,}元",
        "formula_cn": (f"{months}个月 × {int(min_wage)}元/月 × 90% = {total:,}元"),
    }


def _to_int(v) -> Optional[int]:
    """宽容地把各种写法转成 int（"24" / 24 / 24.0 / " 24 "）；失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    s = str(v).strip()
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
            return int(f) if f == int(f) else None
        except ValueError:
            return None


def _fmt_months(m: int) -> str:
    """月数 → 中文（"18个月" / "2年6个月" / "2年"）。"""
    y, r = divmod(int(m), 12)
    if y and r:
        return f"{y}年{r}个月"
    if y:
        return f"{y}年"
    return f"{m}个月"


# ---- 「起止式」说法的兜底解析（2026-09-14 公子宇截图 bug）---------------
# 用户答缴费时长时，常直接说起止：「从16年到现在」「16年开始缴，一直没断过」。
# 这类句子里那个「16年」是**年份**，不是 16 年的时长 —— 绝不能按时长读，
# 否则「从16年到现在」会被算成 16 年（192 个月），待遇直接算错。
# 折算口径（与真模型一致：2016→9 年、2018→7 年、2010→15 年、2005→20 年）：
#   年数 = 结束年 − 起始年；「到现在」的结束年取**上一个完整年度**（当前年份 − 1）。
#   只报年份、没报月份 → 按**已满的完整年度**算，保守、不把零头算进去，
#   免得把缴费年限和待遇算多。
# 例：2026 年时说「从16年到现在」→ 2025 − 2016 = 9 年 → 108 个月。
_RANGE_TO_NOW_RE = re.compile(
    r"(?:从|自)?\s*(\d{2,4})\s*年\s*(?:开始|起|初)?"
    r"[^。；;！？\n]{0,12}?"
    r"(?:到|至|截止)\s*(?:现在|今|今天|今年|目前|当前|眼下)"
)
# ⚠️ 这条锚点是「没断过 / 一直在缴」，**不带「到现在」**——
# 于是「缴了12年，一直没断过」也长得像。所以必须要求「从/自」开头，
# 或者年份写成四位数，才认定它说的是**起止年份**而不是时长。
_RANGE_NO_BREAK_RE = re.compile(
    r"(?:(从|自)\s*)?(\d{2,4})\s*年\s*(?:开始|起|初)?"
    r"[^。；;！？\n]{0,12}?"
    r"(?:一直|连续|都|就)?\s*(?:没断过|没停过|没中断过|没断|一直在缴|连续缴|一直在交)"
)
_RANGE_BOTH_YEARS_RE = re.compile(
    r"(?:从|自)?\s*(\d{2,4})\s*年\s*(?:开始|起)?\s*(?:到|至|截止)\s*(\d{2,4})\s*年"
)


def _full_year(yy: int, now_year: int) -> int:
    """两位年份补全成四位：`16` → 2016；`99` → 1999（大于当前年两位数则视为上个世纪）。"""
    if yy >= 1000:
        return yy
    if yy > now_year % 100:
        return 1900 + yy
    return 2000 + yy


def _years_between(start_year: int, end_year: int) -> int:
    """两个年份之间的**已满完整年度**数（不足一年不计）= 结束年 − 起始年。"""
    return max(0, int(end_year) - int(start_year))


def range_months_from_text(text: Optional[str],
                           now_year: Optional[int] = None) -> Optional[int]:
    """解析「从 X 年到现在 / 至今 / 没断过 / 到 Y 年」这类**起止式**说法 → 月数。

    只报年份、没报月份时按**已满的完整年度**折算（见上方口径）。
    认不出、或算出来不到一年 → None（**不猜**，回头正常追问）。
    """
    if not text:
        return None
    t = str(text)
    now = int(now_year or _dt.date.today().year)

    m = _RANGE_BOTH_YEARS_RE.search(t)
    if m:
        s, e = _full_year(int(m.group(1)), now), _full_year(int(m.group(2)), now)
        y = _years_between(s, e)
        return y * 12 if y > 0 else None

    m = _RANGE_TO_NOW_RE.search(t)
    if m:
        y = _years_between(_full_year(int(m.group(1)), now), now - 1)
        return y * 12 if y > 0 else None

    m = _RANGE_NO_BREAK_RE.search(t)
    if m:
        prefix, digits = m.group(1), m.group(2)
        # 没写「从/自」又只有两位数（如「缴了12年，一直没断过」）→ 那是时长，不是年份
        if prefix or len(digits) >= 4:
            y = _years_between(_full_year(int(digits), now), now - 1)
            return y * 12 if y > 0 else None
    return None


def paid_months_from_text(text: Optional[str],
                          now_year: Optional[int] = None) -> Optional[int]:
    """从用户原话里解析累计缴费时长（程序兜底，抽取层漏抽时用）。

    支持："3年" "18个月" "3年6个月" "一年零三个月" "缴了两年" "半年" 等。
    返回总月数；解析不出返回 None。**不猜**：只认明确带时间单位的说法。
    """
    if not text:
        return None
    t = str(text)

    # 汉字数字归一（一二三 → 1/2/3，两 → 2）
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    for cn, d in digits.items():
        t = t.replace(cn, str(d))
    # 「十几」「二十几」的组合已在上面逐字替换里覆盖（二十→2 0 需要 join），
    # 这里用简单方案：把「十」当 10，"二十"→"2 0" 不对，故先处理 X十[ Y] 结构
    import re as _re
    def _cn_num(m_):
        s = m_.group(0)
        if "十" in s:
            a, _, b = s.partition("十")
            tens = digits.get(a, 1) if a else 1
            ones = digits.get(b, 0) if b else 0
            return str(tens * 10 + ones)
        return s
    t = _re.sub(r"[零一二两三四五六七八九十]{1,3}", _cn_num, t)

    # 先认「起止式」说法（「从16年到现在」里的 16 是年份，不是 16 年时长）——
    # 必须抢在下面的时长正则之前，否则「16年」会被读成 16 年。
    rng = range_months_from_text(t, now_year=now_year)
    if rng is not None:
        return rng

    years = months = None
    my = _re.search(r"(\d+)\s*年", t)
    if my:
        years = int(my.group(1))
    mm = _re.search(r"(\d+)\s*个?\s*月", t)
    if mm:
        months = int(mm.group(1))
    # 「半年」→ 6 个月
    if years is None and months is None and _re.search(r"半年", t):
        return 6
    if years is None and months is None:
        return None
    return (years or 0) * 12 + (months or 0)


if __name__ == "__main__":  # pragma: no cover
    for m in [6, 11, 12, 15, 24, 59, 60, 61, 63, 67, 68, 71, 72, 100, 144, 288]:
        r = calc_unemp_benefit(m)
        if r["eligible"]:
            print(f"  缴费{r['paid_cn']:<8} → 领 {r['months']:>2} 个月 → {r['formula_cn']}"
                  + ("（封顶）" if r["capped"] else ""))
        else:
            print(f"  缴费{r['paid_cn']:<8} → 不合格（{r['reason']}）")