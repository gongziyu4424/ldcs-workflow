"""v5：所有提示词均从 config 读 —— config 改即生效，不需重启。

提供：
- `SYSTEM_PROMPT` / `SYSTEM_PROMPT_EXTRACT_HUJI` / `SYSTEM_PROMPT_EXTRACT_GENDER_AGE`
  现在是 module-level 函数 getter（保持调用方便、内容热改）
- `build_messages(scaffold)` 拼接 [system, user]
- `_action_hint(scaffold, action, facts)` 从 config 读 hint 模板并 format 事实变量
"""
from __future__ import annotations

import os
import re

from . import config


# ---- 提示词 getter ----
# 为什么全是函数：提示词在配置页能改，改完要立即生效。写成模块级字符串会固化在
# import 那一刻，运营改完不重启就等于没改 —— 这是最容易「改了没反应」的坑。
# 每个 getter 对应 config.system 下的一个键，键名就是函数名去掉下划线与 _system 后缀。
def _system() -> str:
    """对外说话用的主 system 提示词（话术风格、称呼、禁语）。"""
    return config.get_config()["system"]["agent_system_prompt"]


def _extract_huji_system() -> str:
    """户籍抽取器 system。输出受影响字段 + corrections（改口纠正）。"""
    return config.get_config()["system"]["extract_huji_system"]


def _extract_ga_system() -> str:
    """性别年龄抽取器 system。要区分「首次参保年龄」与「现在年龄」，别混。"""
    return config.get_config()["system"]["extract_gender_age_system"]


def _extract_retire_system() -> str:
    """退休测算抽取器 system。人员类别要落到 male / female_cadre / female_worker。"""
    return config.get_config()["system"]["extract_retire_system"]


def _extract_unemp_system() -> str:
    """失业金抽取器 system。离职情形必须落在 REASON_CODE_HINTS 的枚举内。"""
    return config.get_config()["system"]["extract_unemp_system"]


# 兼容旧名字（属性访问形式）—— 让 prompts.SYSTEM_PROMPT 这种 import 也能工作
class _Proxy:
    """属性代理：每次访问都从 config 现读。"""
    def __getattr__(self, name):
        c = config.get_config()
        if name == "SYSTEM_PROMPT":
            return c["system"]["agent_system_prompt"]
        if name == "SYSTEM_PROMPT_EXTRACT_HUJI":
            return c["system"]["extract_huji_system"]
        if name == "SYSTEM_PROMPT_EXTRACT_GENDER_AGE":
            return c["system"]["extract_gender_age_system"]
        raise AttributeError(name)


SYSTEM_PROMPT = _Proxy()  # type: ignore
SYSTEM_PROMPT_EXTRACT_HUJI = _Proxy()  # type: ignore
SYSTEM_PROMPT_EXTRACT_GENDER_AGE = _Proxy()  # type: ignore


def build_messages(scaffold: dict) -> list[dict]:
    """拼发送给模型的 [system, user] 两条消息。

    结构刻意保持极简：system 放硬规则，user 放「本轮任务 JSON + hint + 必含事实」。
    不要把历史对话拼进来 —— 抽取器和话术生成都是单轮设计，塞历史会让模型
    把上一轮的结论当成本轮事实，出现「同一话题说两遍」。
    """
    return [
        {"role": "system", "content": _system()},
        {"role": "user", "content": _user_prompt(scaffold)},
    ]


def _user_prompt(scaffold: dict) -> str:
    """把 scaffold 转成本轮的任务指令。

    两个必须知道的约束：
    ① 非 mock 模式下**剔除 fallback_text**（下方有大段解释，别删）。
    ② required_facts 是硬约束，模型漏了会被 llm.generate_reply 整段换成兜底稿，
       所以这里要明确写进指令，减少被替换的概率。
    """
    action = scaffold.get("action", "")
    facts = scaffold.get("facts") or {}
    # 【重要】不要把 fallback_text 一起喂给模型。
    # 它是「模型没说好时的兜底稿」，本身就是模板 / 对账腔（{known} 还会把用户刚说的话
    # 原样复述一遍）。模型一旦在 JSON 里看到它，就会**照抄**，于是对外的话术就变成了
    # 又僵硬又像在重复用户的话。只留 action / facts / hint 这些真正的指令信息即可。
    # 例外：mock 模式离线用例靠 prompt 里这段兜底稿做断言，故 mock 模式照旧透出。
    if os.environ.get("LLM_MODE") == "mock":
        visible = scaffold
    else:
        visible = {k: v for k, v in scaffold.items() if k != "fallback_text"}
    head = "本次任务 JSON：\n" + repr(visible) + "\n\n"
    required = scaffold.get("required_facts") or []
    rule = (
        "请遵守 system 的硬行为规则。"
        f"你的回复中必须保留以下必要事实：{' / '.join(required) if required else '（无硬性约束，可自由表达）'}。"
        "组织成自然口语段落，不要列举、不要前缀客套。"
    )
    hints = _action_hint(scaffold, action, facts)
    return head + hints + "\n\n" + rule


def _fmt(tpl: str, vars_dict: dict) -> str:
    class _D(dict):
        def __missing__(self, k):
            return "{" + k + "}"
    return tpl.format_map(_D(vars_dict or {}))


def _action_hint(scaffold: dict, action: str, facts) -> str:
    """按 action 取出本轮的口头指令模板，并填入事实变量。

    取值优先级（高 → 低），改话术时必须按这个顺序找，否则会改错层：
      1. scaffold["hint"]        画布上单节点改的话术（最高，临时覆盖）
      2. scaffold["hint_key"]    指定用哪个现成 hint（如「答不上来 → 粤省事指引」）
      3. hints["<action>_<x>"]   带参数变体（如 ask_retire_male / conclusion_other_unqualify）
      4. hints[action]           默认模板
      5. 兜底一句「请按上述 JSON 与 system 规则自由组织」

    facts 里的占位符缺失时**原样保留 {xxx}**（见 _fmt），不会抛异常，
    但会出现「您户籍在 {city_cn}」这种半截话术 —— 模板新增占位时记得同步给 facts。
    """
    cfg = config.get_config()
    hints = cfg["hints"]

    # 节点级 hint 覆盖（画布单节点改话术时用）
    node_hint = (scaffold.get("hint") or "").strip()

    # scaffold 指定了 hint_key（如「答不上来 / 反问怎么办 → 粤省事指引」）→ 直接用它。
    # 各条反问支线（反问1~4）都能换话术，不必逐条加特例。
    key_ov = scaffold.get("hint_key") or ""
    if key_ov and key_ov in hints:
        return _fmt(node_hint or hints[key_ov], facts or {})

    if action == "greet":
        return node_hint or hints["greet"]
    if action == "off_topic":
        return node_hint or hints["off_topic"]
    if action == "ask_huji":
        return node_hint or hints["ask_huji"]
    if action == "ask_huji_retry":
        attempt = scaffold.get("attempt", 2)
        return _fmt(node_hint or hints["ask_huji_retry"], {"attempt": attempt})
    if action == "ask_gender_age":
        return _fmt(node_hint or hints["ask_gender_age"], facts or {})
    if action == "ask_gender_age_retry":
        attempt = scaffold.get("attempt", 2)
        reason = scaffold.get("reason", "信息不够明确")
        missing = scaffold.get("missing") or "（程序未标注，请按 JSON 里的 facts 判断还缺什么）"
        key = scaffold.get("hint_key") or "ask_gender_age_retry"
        return _fmt(node_hint or hints.get(key, hints["ask_gender_age_retry"]),
                    {"attempt": attempt, "reason": reason, "missing": missing,
                     "known": (facts or {}).get("known", ""),
                     "recap": (facts or {}).get("recap", ""),
                     "note": (facts or {}).get("note", "")})
    if action == "conclusion":
        branch = scaffold.get("branch", "")
        key = f"conclusion_{branch}"
        tpl = node_hint or hints.get(key, "请告知用户结论。")
        return _fmt(tpl, facts or {})
    if action == "ask_retire":
        # 已收集的信息不再问：性别已知就按性别选模板，别再提「如果您是女性…」
        key = {"male": "ask_retire_male", "female": "ask_retire_female"}.get(
            scaffold.get("gender") or "", "ask_retire")
        tpl = hints.get(key) or hints["ask_retire"]
        return _fmt(node_hint or tpl, {"missing": scaffold.get("missing") or "",
                                       "known": scaffold.get("known") or ""})
    if action == "ask_retire_retry":
        attempt = scaffold.get("attempt", 2)
        missing = scaffold.get("missing") or "（程序未标注，请按 JSON 里的 facts 判断还缺什么）"
        return _fmt(node_hint or hints["ask_retire_retry"],
                    {"attempt": attempt, "missing": missing,
                     "known": scaffold.get("known") or ""})
    if action == "conclusion_retire":
        return _fmt(node_hint or hints["conclusion_retire"], facts or {})
    if action == "ask_unemp":
        return _fmt(node_hint or hints["ask_unemp"],
                    {"missing": scaffold.get("missing") or "",
                     "known": scaffold.get("known") or "",
                     "ask_direct": (facts or {}).get("ask_direct", "")})
    if action == "ask_unemp_retry":
        attempt = scaffold.get("attempt", 2)
        return _fmt(node_hint or hints["ask_unemp_retry"],
                    {"attempt": attempt,
                     "missing": scaffold.get("missing") or "",
                     "known": scaffold.get("known") or "",
                     "ask_direct": (facts or {}).get("ask_direct", ""),
                     "note": (facts or {}).get("note", "")})
    if action == "conclusion_unemp":
        return _fmt(node_hint or hints["conclusion_unemp"], facts or {})
    if action == "fallback":
        return node_hint or hints["fallback"]
    if action == "followup":
        return node_hint or hints["followup"]
    if action == "guide_ss":
        # 指引话术要点名具体业务（{service}）与对应动词（{guide_action}），所以这里必须 fmt
        return _fmt(node_hint or hints["guide_ss"], facts or {})
    return "请按上述 JSON 任务与 system 规则自由组织自然回复。"


# 抽取器是**单轮、不带对话历史**的：如果不告诉它「本轮在问什么」，
# 同一句话会被按错误语境解读。实测踩过：问「第一次在广东省参保时几岁」时，
# 用户答「哦，是广东省啊」，模型把「广东省」当成户籍纠正写进 corrections，
# → 户籍被误改、流程跳回户籍支线。
# 所以每次抽取都把「当前话题」贴在原话前面（与 classify_intent_messages 同一手法）。
#
# ⚠️ 公子宇 2026-09-14 反馈「加个话题表示似乎有控制的太死了」：
# 话题只写「本轮在问什么」时，抽取器会把它读成**抽取范围的围栏** ——
# 话题是「首次在广东省参保的年龄」，他下面那句「说错了，广东省内应该是 16 年」
# 就被硬套成 first_insured_year=2016（年份），而不是 gd_years=16（年限）。
# 所以：① 无话题时也注入占位，明说「按词面照常抽」，别再出现「没话题就不抽」；
#        ② 每轮附一行【字段归属】，把话题降级为**优先关注点**，并点明量/标先分辨。
_ASK_TOPIC_PREFIX = "【当前在问】"
_USER_TEXT_PREFIX = "【用户本轮原话】"
# 结论已给出 / 用户自己起话题时没有固定话题 —— 这时恰恰最需要抽取（更正、补信息），
# 所以给一句占位说明，而不是干脆不注入语境。
_NO_TOPIC = ("（本轮没有固定话题：可能结论已给出，或用户在主动补充 / 更正 —— "
             "照常抽取，不要因为没话题就放弃）")
# 通用字段归属原则：随每一次抽取注入。话题表是唯一真源，这条只说「话题不是围栏」。
_TOPIC_SCOPE_LINE = (
    "【字段归属】上面【当前在问】只是本轮**优先关注**的字段，"
    "**不是抽取范围的限制**——用户主动提到或要更正的其他字段，一律照常抽取"
    "（该进 corrections 就进）。遇到「数字 + 单位」先判是**量**还是**标**，"
    "别因为落在当前话题里就往该话题的字段上硬套。"
)


def wrap_user_text(user_text: str, topic: str | None = None) -> str:
    """把「当前在问什么」贴在用户原话前，作为抽取器的语境。

    无话题（`topic` 空）时**也要注入**：写占位说明 + 通用字段归属原则，
    免得抽取器把「没话题」理解成「本轮不用抽」。
    """
    return (f"{_ASK_TOPIC_PREFIX}{topic or _NO_TOPIC}\n"
            f"{_TOPIC_SCOPE_LINE}\n"
            f"{_USER_TEXT_PREFIX}{user_text}")


def strip_topic_prefix(text: str) -> str:
    """从抽取入参里剥回**用户原话**（mock 规则抽取只该看原话）。"""
    m = re.search(re.escape(_USER_TEXT_PREFIX) + r"(.*)$", text or "", re.S)
    return (m.group(1) if m else (text or "")).strip()


# ---- 抽取器消息 ----
# 四条支线共用同一套骨架：system 换成对应抽取器，user 一律走 wrap_user_text
# （把「当前在问什么」贴在原话前）。**别在这里绕开 wrap_user_text**：
# 抽取器单轮无记忆，少了话题语境就会把「广东省」读成户籍之类（下面有完整踩坑记录）。
def extract_huji_messages(user_text: str, topic: str | None = None) -> list[dict]:
    """户籍抽取：省 / 市，以及用户改口纠正。"""
    return [
        {"role": "system", "content": _extract_huji_system()},
        {"role": "user", "content": wrap_user_text(user_text, topic)},
    ]


def extract_gender_age_messages(user_text: str, topic: str | None = None) -> list[dict]:
    """性别年龄抽取：性别、首次参保年龄、出生年份、省内累计年限。"""
    return [
        {"role": "system", "content": _extract_ga_system()},
        {"role": "user", "content": wrap_user_text(user_text, topic)},
    ]


def extract_retire_messages(user_text: str, topic: str | None = None) -> list[dict]:
    """退休测算抽取：出生年月 + 人员类别。"""
    return [
        {"role": "system", "content": _extract_retire_system()},
        {"role": "user", "content": wrap_user_text(user_text, topic)},
    ]


def extract_unemp_messages(user_text: str, topic: str | None = None) -> list[dict]:
    """失业金抽取：缴费月数、离职情形码、是否已办失业登记。"""
    return [
        {"role": "system", "content": _extract_unemp_system()},
        {"role": "user", "content": wrap_user_text(user_text, topic)},
    ]


def _classify_intent_system() -> str:
    """意图分类器 system（hit / miss / followup）。"""
    return config.get_config()["system"]["classify_intent_system"]


def classify_intent_messages(user_text: str, stage_done: bool = False) -> list[dict]:
    """意图识别（模型判）：本轮想问退休时间 / 领取地 / 追问 / 无关。"""
    ctx = ("用户在本轮之前已经拿到过一次结论，"
           "注意区分他是在提新的测算诉求，还是只是在追问细节。"
           if stage_done else
           "用户本轮刚开始对话，还没有拿到过任何结论。")
    return [
        {"role": "system", "content": _classify_intent_system()},
        {"role": "user", "content": f"【对话状态】{ctx}\n【用户本轮原话】{user_text}"},
    ]
