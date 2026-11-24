"""全部节点函数（LangGraph node）—— v4 模型问答 + 程序判定。

- 程序节点：派发 / 户籍三分支 / 年龄性别判定 / 重试次数 / done 收尾
- LLM 节点：抽户籍 / 抽性别年龄（只抽槽位，不产生对外回复）
- 每个"对外开口"节点 return _stream_scaffold（dict）后由 server 层调流式
  模型生成自然回复，程序不再写"硬话术"。
"""
import re

from . import prompts
from .llm import fact_regression, get_llm, parse_json
from .state import (
    SCAFFOLD_ASK_GENDER_AGE, SCAFFOLD_ASK_HUJI, SCAFFOLD_FALLBACK,
    SCAFFOLD_OFF_TOPIC,
    classify_branch, gender_cn, judge_qualify, max_attempt,
    scaffold_ask_gender_age_retry,
    scaffold_ask_huji_retry, scaffold_conclusion, scaffold_finished_followup,
    threshold_cn, threshold_num,
)


# 意图正则硬匹配（先于任何 LLM 兜底）
_INTENT_RE = re.compile(
    r"(广州.{0,12}(退休金|养老金|退休|领取|待遇))|(领取地|在广州退休|退休在哪|异地参保|领取养老金)"
)


def intent_hit(text: str) -> bool:
    return bool(_INTENT_RE.search(text or ""))


# 拼音 → 中文（结论文案中 city_cn 用）
_CITY_CN = {
    "guangzhou": "广州", "zhaoqing": "肇庆", "foshan": "佛山", "shenzhen": "深圳",
    "dongguan": "东莞", "shaoguan": "韶关", "zhuhai": "珠海", "huizhou": "惠州",
    "zhongshan": "中山", "jiangmen": "江门", "shantou": "汕头", "zhanjiang": "湛江",
    "qingyuan": "清远", "meizhou": "梅州", "shanwei": "汕尾", "heyuan": "河源",
    "yangjiang": "阳江", "maoming": "茂名", "chaozhou": "潮州", "jieyang": "揭阳",
    "yunfu": "云浮",
}


# ============================================================
# 入口分发
# ============================================================
def n_intent(state: dict) -> dict:
    """意图识别 + 首次进入写反问 1。

    - stage=idle 且意图命中（首次进入）：写 _stream_scaffold=ASK_HUJI + stage=await_huji；
      因为 _stream_scaffold 已写，路由走 END。
    - stage=await_huji 或 await_gender_age（继续流程）：不写 scaffold，路由走流程节点。
    - 意图未命中：写 _stream_scaffold=OFF_TOPIC，路由 off_topic。
    - stage=done：写 _stream_scaffold=FOLLOWUP，路由 followup。
    """
    stage = state.get("stage")
    # 每轮显式清空上一轮 _stream_scaffold，避免路由残留判断错
    if stage == "done":
        return {
            "_intent": "followup",
            "_stream_scaffold": scaffold_finished_followup(),
        }
    if stage in ("await_huji", "await_gender_age"):
        return {"_intent": "hit", "_stream_scaffold": None}
    if intent_hit(state.get("user_input", "")):
        return {
            "_intent": "hit",
            "_stream_scaffold": SCAFFOLD_ASK_HUJI(),
            "stage": "await_huji",
        }
    return {
        "_intent": "miss",
        "_stream_scaffold": SCAFFOLD_OFF_TOPIC(),
    }


def n_off_topic(state: dict) -> dict:
    """用户问题不在本工具范围——由模型自然告知，不要硬话术。"""
    return {
        "_stream_scaffold": SCAFFOLD_OFF_TOPIC(),
        "stage": "done",
        "reply": None,
    }


def n_followup(state: dict) -> dict:
    """done 阶段用户继续追问细节 —— 模型针对追问作答，不重发结论。"""
    return {
        "_stream_scaffold": scaffold_finished_followup(),
        "stage": "done",
        "reply": None,
    }


# ============================================================
# 反问1：抽取户籍（LLM 通过问答拿到槽位 → 程序判定）
# ============================================================
def n_extract_huji(state: dict) -> dict:
    """LLM 抽户籍槽位（province/city）。"""
    user_input = state.get("user_input", "")
    msgs = prompts.extract_huji_messages(user_input)
    raw = get_llm().chat(msgs)
    out = parse_json(raw)
    return {
        "province": out.get("province"),
        "city": out.get("city"),
    }


def n_decide_huji(state: dict) -> dict:
    """程序硬编码：户籍三分支 + 缺失重问/兜底。

    - gz / gd_other → 写 conclusion scaffold（让模型自然告知结论）
    - other_prov → 写 ask_gender_age scaffold（让模型自然反问2）
    - 缺失 → 写 retry / fallback scaffold（让模型自然告知）
    """
    branch, missing = classify_branch(state.get("province"), state.get("city"))

    if not missing:
        if branch in ("gz", "gd_other"):
            city_cn = _CITY_CN.get(state.get("city") or "", "该地市")
            facts = {"city_cn": city_cn}
            return {
                "_stream_scaffold": scaffold_conclusion(branch, facts),
                "stage": "done",
                "branch": branch,
                "reply": None,
            }
        # other_prov：进入反问2
        return {
            "_stream_scaffold": SCAFFOLD_ASK_GENDER_AGE(),
            "stage": "await_gender_age",
            "branch": branch,
            "reply": None,
        }

    # 缺失：重问 or 兜底
    attempt = state.get("attempt_huji", 0)
    if attempt < max_attempt("huji"):
        new_attempt = attempt + 1
        return {
            "_stream_scaffold": scaffold_ask_huji_retry(new_attempt),
            "stage": "await_huji",
            "attempt_huji": new_attempt,
            "reply": None,
        }
    return {
        "_stream_scaffold": SCAFFOLD_FALLBACK(),
        "stage": "done",
        "reply": None,
    }


# ============================================================
# 反问2：抽取性别+首次参保年龄 → 程序判定
# ============================================================
def n_extract_gender_age(state: dict) -> dict:
    """LLM 抽性别 + 首次参保年龄（is_current_age_ambiguous 标记歧义）。"""
    user_input = state.get("user_input", "")
    msgs = prompts.extract_gender_age_messages(user_input)
    raw = get_llm().chat(msgs)
    out = parse_json(raw)
    return {
        "gender": out.get("gender"),
        "first_ins_age": out.get("first_ins_age"),
        "age_ambiguous": bool(out.get("is_current_age_ambiguous")),
    }


def n_decide_age(state: dict) -> dict:
    """程序硬编码：年龄性别阈值判定 + 重问/兜底。

    - gender & first_ins_age 齐全且非歧义 → 写 conclusion scaffold
    - 信息不全或歧义 → 重问 or 兜底
    """
    gender = state.get("gender")
    age = state.get("first_ins_age")
    ambiguous = state.get("age_ambiguous")
    has_data = gender in ("male", "female") and age is not None and not ambiguous

    if not has_data:
        attempt = state.get("attempt_gender_age", 0)
        reason = ("用户给的是当前年龄，不是首次参保年龄，请重问"
                  if ambiguous else "信息不全（性别或首次参保年龄缺失），请重问")
        if attempt < max_attempt("gender_age"):
            new_attempt = attempt + 1
            return {
                "_stream_scaffold": scaffold_ask_gender_age_retry(new_attempt, reason),
                "stage": "await_gender_age",
                "attempt_gender_age": new_attempt,
                "reply": None,
            }
        return {
            "_stream_scaffold": SCAFFOLD_FALLBACK(),
            "stage": "done",
            "reply": None,
        }

    j = judge_qualify(gender, age)
    facts = {
        "gender": gender,
        "gender_cn": gender_cn(gender),
        "first_ins_age": age,
        "threshold_cn": threshold_cn(gender),
        "threshold_num": threshold_num(gender),
    }
    branch = "other_qualify" if j == "qualify" else "other_unqualify"
    return {
        "_stream_scaffold": scaffold_conclusion(branch, facts),
        "stage": "done",
        "judge": j,
        "reply": None,
    }


# 重新导出以方便 server 端校验复用
__all__ = [
    "n_intent", "n_off_topic", "n_followup",
    "n_extract_huji", "n_decide_huji",
    "n_extract_gender_age", "n_decide_age",
    "intent_hit", "fact_regression",
]
