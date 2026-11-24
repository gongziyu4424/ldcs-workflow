"""广州养老待遇领取地测算工作流 —— 状态定义 + 业务规则。

v5：所有可配置项（业务阈值、提示词、必含事实）从 config 读，config 修改即生效。
"""
from typing import Optional, TypedDict

from . import config


class LdcsState(TypedDict, total=False):
    """会话状态。多轮持久化由调用方持有本 dict，每轮传入 run_once。

    字段分三类，改代码前先分清，否则很容易把「每轮重置」的当成「跨轮保留」用：

    ① 输入/输出（每轮被覆盖）
       user_input   本轮用户原话
       reply        本轮最终对外话术
       stage        idle | await_huji | await_gender_age | done
       _intent      本轮意图：hit / miss / followup
       _stream_scaffold  本轮要说的话的骨架（含 action / facts / required_facts）。
                     **运行态，不要持久化**，每轮开头被清空；否则下一轮会重播上一轮结论。

    ② 业务事实（跨轮保留，抽取一律「合并语义」：新值为空则保留旧值）
       province / city / gender / first_ins_age / birth_year / first_insured_year / gd_years
       retire_birth_year / retire_birth_month / retire_category
       unemp_paid_months / unemp_reason / unemp_registered
       另有若干 *_unknown 标记：用户明确表示「不知道 / 不记得」，用来换追问话术，
       与「没回答」是两回事，不能混。

    ③ 流程控制（下面重点说，最容易读不懂）

       attempt_*   重试配额。**只有真正的答非所问才 +1**；用户补了新信息、或在本流程
                   语境里回应（含「不知道」），一律归零。判定见 flow._k_attempt_left。
                   搞错这一条，用户就会被无辜踢出流程 —— 这是本项目踩过的坑。

       *_progress    本轮抽取**拿到新信息**（true/false，每轮重算，不累积）
       *_in_context  用户本轮的话题**仍在本支线语境里**（哪怕答「不知道」也算 true）
       *_asked       已经就这个话题**问过用户**（say 节点置位后一直为 true）
                   progress / in_context / asked 三个一组，缺一个就会算错配额或重复追问。
                   实测区别：答「不记得户籍了」→ in_context=true、progress=false；
                   问天气 → 两者都 false，这才消耗配额。

       corrected / extract_src  本轮「改口纠正」写回的字段名与来源支线，
                   只在当轮有效，每轮开头清空。
       parse_missing  已废弃，flow 不再读写，保留仅为兼容旧存档。

    新增支线时必须同时做三件事：播种已有信息、抽取用合并语义、追问只列缺项。
    少做一件，用户就会被重复追问已经答过的内容。
    """
    user_input: str
    stage: str                       # idle | await_huji | await_gender_age | done
    reply: Optional[str]
    scaffold: Optional[dict]
    province: Optional[str]
    city: Optional[str]
    gender: Optional[str]
    first_ins_age: Optional[int]
    birth_year: Optional[int]        # 出生年份（跨轮保留，配合参保年份换算年龄）
    first_insured_year: Optional[int]  # 广东首次参保年份（跨轮保留）
    age_ambiguous: bool
    age_unknown: bool                # 用户明确表示不知道/不记得首次参保时间
    gd_years: Optional[int]          # 在广东省内累计参保年数（省外分支判定用）
    years_unknown: bool              # 用户明确表示不知道参保了多少年
    retry_kind: Optional[str]        # 追问类型：normal | unknown（unknown=答不上来→给粤省事指引）
    judge_reason: Optional[str]      # unqualify 原因：age_threshold | years_limit
    branch: Optional[str]            # gz | gd_other | other_prov
    parse_missing: bool
    judge: Optional[str]             # qualify | unqualify
    attempt_huji: int
    attempt_gender_age: int
    # 各支线「用户本轮答不上来 / 反问怎么查怎么办」（追问时改用粤省事指引话术）
    huji_unknown: bool
    retire_unknown: bool
    unemp_unknown: bool
    # 失业保险金支线：三个槽位（缴费月数 / 离职情形码 / 失业登记+求职要求）
    unemp_paid_months: Optional[int]
    unemp_reason: Optional[str]
    unemp_registered: Optional[bool]
    _intent: str
    _stream_scaffold: Optional[dict]


def new_state() -> dict:
    """新会话的初始状态。**重置会话 = 调这个**，没有别的清空方式。

    注意：这里必须把字段全部显式列出（而不是用 defaultdict 之类）。
    因为 run_flow 每轮会先 `new_state()` 再 update 旧 state，字段缺失会让
    「上一轮的残留值」被当成这一轮的初值，出现幽灵状态。
    """
    return {
        "stage": "idle",
        "reply": None,
        "scaffold": None,
        "province": None,
        "city": None,
        "gender": None,
        "first_ins_age": None,
        "birth_year": None,
        "birth_year_calc": False,     # 出生年份是否由「参保年份−参保年龄」推算而来
        "first_insured_year": None,
        "age_ambiguous": False,
        "age_unknown": False,
        "gd_years": None,
        "years_unknown": False,
        "retry_kind": None,
        # 「当前在问什么」——本轮抽取时告知模型的话题（见 flow.ASK_TOPIC）。
        # 抽取器单轮无记忆，不告诉它话题就会按错误语境解读（如把参保地的
        # 「广东省」读成户籍）。这里留档：可观测、可断言、前端可展示。
        "ask_topic": "",
        "judge_reason": None,
        "branch": None,
        "parse_missing": False,
        "judge": None,
        "attempt_huji": 0,
        "huji_progress": False,
        "huji_in_context": False,
        "huji_asked": False,
        "attempt_gender_age": 0,
        "ga_progress": False,
        "ga_in_context": False,
        "ga_asked": False,
        # 「答不上参保时间」分支里**仍可问**的项（如性别）——
        # 参保年龄已确认问不到就不列进去了，避免重复追问
        "ga_still_ask": None,
        "retire_birth_year": None,
        "retire_birth_month": None,
        "retire_category": None,
        "retire_result": None,
        "retire_missing": None,
        "attempt_retire": 0,
        "retire_progress": False,
        "retire_in_context": False,
        "retire_asked": False,
        "retire_unknown": False,
        "huji_unknown": False,
        # 用户「改口纠正」：本轮被覆盖写回的字段名 + 来自哪条 extract
        "corrected": [],
        "extract_src": None,
        # ---- 失业保险金测算支线 ----
        "unemp_paid_months": None,       # 累计缴费月数（月粒度）
        # 条件②非因本人意愿中断就业：情形码（见 unemp_calc.INVOLUNTARY_REASONS / VOLUNTARY_REASON）
        "unemp_reason": None,
        # 条件③已办理失业登记并有求职要求：True/False/None(未确认)
        "unemp_registered": None,
        "unemp_result": None,            # 计算程序输出（含 eligible / months / total）
        "unemp_missing": None,           # 待收集缺项
        "attempt_unemp": 0,
        "unemp_progress": False,
        "unemp_in_context": False,
        "unemp_asked": False,
        "unemp_unknown": False,
    }


# ============================================================
# 业务常量（运行时从 config 读，配置可热改）
# ============================================================
# 为什么是函数而不是模块级常量：运营在配置页改了阈值要**立即生效**，
# 常量在 import 时就固化死了，必须重启服务。所以每次用都现读一次 config。
# ⚠️ 但也别在高频循环里反复调，每次都会走一遍配置合并。
def AGE_THRESHOLD_MALE() -> int:
    """男性「到龄」判定线（周岁）。达到或超过即不再符合领取条件。"""
    return int(config.get_config()["business"]["male_age_threshold"])


def AGE_THRESHOLD_FEMALE() -> int:
    """女性「到龄」判定线（周岁）。"""
    return int(config.get_config()["business"]["female_age_threshold"])


def GD_OTHER_YEARS() -> int:
    """广东省内（非广州）分支要求的最低累计缴费年限。"""
    return int(config.get_config()["business"]["gd_other_years_required"])


def MAX_HUJI_RETRY() -> int:
    """户籍支线允许的「答非所问」次数，超过则放弃引导。"""
    return int(config.get_config()["business"]["max_huji_retry"])


def MAX_GENDER_AGE_RETRY() -> int:
    """性别年龄支线允许的「答非所问」次数。"""
    return int(config.get_config()["business"]["max_gender_age_retry"])


def max_attempt(category: str) -> int:
    """category: 'huji' 或 'gender_age'。统一入口。"""
    return MAX_HUJI_RETRY() if category == "huji" else MAX_GENDER_AGE_RETRY()


# 兼容旧名字（MAX_ATTEMPT = 2）—— 直接给 2 是错的，改为委托
def MAX_ATTEMPT(category: str = "huji") -> int:
    return max_attempt(category)


# ============================================================
# 业务分类函数
# ============================================================
def classify_branch(province: Optional[str], city: Optional[str]) -> tuple:
    """户籍分类，决定走哪条判定分支。返回 (branch | None, missing)。

    三个分支的业务含义不同，别混：
      gz          广州市户籍 → 市内待遇领取地以「最后参保地」为准
      gd_other    广东省内其他市户籍 → 还要看省内累计缴费年限够不够 GD_OTHER_YEARS()
      other_prov  省外户籍 → 要按「户籍地 / 最后参保地 / 缴费满 10 年地」三者规则判

    missing 的判定口径（与 v5 保持一致，改这里等于改政策）：
      广东给到市才算齐；外省**只给到省就算齐**（走 other_prov），
      因为省外分支不依赖具体城市。所以外省用户只答「湖南」不该被追问城市。
    """
    if not province:
        return None, True
    if province == "guangdong":
        if not city:
            return None, True
        return ("gz" if city == "guangzhou" else "gd_other"), False
    return "other_prov", False


def judge_qualify(gender: Optional[str], age: Optional[int]):
    """年龄性别硬判定（程序判，不经模型）。返回 qualify | unqualify | None(缺数据)。

    口径：`age < 阈值` 才算符合。等于阈值即「已到龄」，属于不符合 ——
    边界取闭区间右侧排除，这是政策原文的「达到法定退休年龄」语义。
    阈值走 config，男女各一条，不在这里写死数字。
    """
    if gender not in ("male", "female") or age is None:
        return None
    thr = AGE_THRESHOLD_MALE() if gender == "male" else AGE_THRESHOLD_FEMALE()
    return "qualify" if age < thr else "unqualify"


def gender_cn(gender: str) -> str:
    return "男" if gender == "male" else "女"


def threshold_cn(gender: str) -> str:
    return f"男{AGE_THRESHOLD_MALE()}周岁" if gender == "male" else f"女{AGE_THRESHOLD_FEMALE()}周岁"


def threshold_num(gender) -> str:
    return str(AGE_THRESHOLD_MALE() if gender == "male" else AGE_THRESHOLD_FEMALE())


# ============================================================
# 任务骨架（scaffold）—— 每个 action 单独工厂，从 config 读 fallback_text + required_facts
# ============================================================
def _fmt(fmt_str: str, vars_dict: dict) -> str:
    """对 fallback_text/hint 做事实变量 format，缺失的占位原样保留。"""
    class _D(dict):
        def __missing__(self, k):
            return "{" + k + "}"
    return fmt_str.format_map(_D(vars_dict))


def _scaffold(action: str, **extra) -> dict:
    cfg = config.get_config()
    sc = {
        "action": action,
        "fallback_text": cfg["fallback_texts"].get(action, ""),
        "required_facts": list(cfg["required_facts"].get(action, [])),
    }
    sc.update(extra)
    return sc


# ---------- 简单 action ----------
def SCAFFOLD_GREET() -> dict:                  return _scaffold("greet")
def SCAFFOLD_OFF_TOPIC() -> dict:              return _scaffold("off_topic")
def SCAFFOLD_ASK_HUJI() -> dict:               return _scaffold("ask_huji")
def SCAFFOLD_ASK_GENDER_AGE() -> dict:         return _scaffold("ask_gender_age")
def SCAFFOLD_FALLBACK() -> dict:               return _scaffold("fallback")

# ---------- 带参数 action ----------
def scaffold_ask_huji_retry(attempt: int) -> dict:
    return _scaffold("ask_huji_retry", attempt=attempt)


def scaffold_ask_gender_age_retry(attempt: int, reason: str) -> dict:
    return _scaffold("ask_gender_age_retry", attempt=attempt, reason=reason)


def scaffold_conclusion(branch: str, facts: dict) -> dict:
    """结论分支的骨架（gz / gd_other / other_qualify / other_unqualify）。"""
    cfg = config.get_config()
    fb_key = f"conclusion_{branch}"
    rf_key = fb_key
    template = cfg["fallback_texts"].get(fb_key, "")
    sc = {
        "action": "conclusion",
        "branch": branch,
        "facts": facts,
        "fallback_text": _fmt(template, facts),
        "required_facts": [],
    }
    # required_facts 里有 {threshold_num} 等占位，运行时替换
    rf_template = cfg["required_facts"].get(rf_key, [])
    sc["required_facts"] = [_fmt(x, facts) for x in rf_template]
    return sc


def scaffold_finished_followup() -> dict:
    return _scaffold("followup")
