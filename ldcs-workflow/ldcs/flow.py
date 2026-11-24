"""v6 画布工作流执行器 —— 运行时按 config.flow 拓扑真实执行。

设计要点（与 v5 静态 LangGraph 图行为等价，但拓扑来自 config，改图即改行为）：
- state 新增 'at'：下一轮应从哪个节点开始（终结后为 None → 从 start 重新走）
- say 节点：组装 scaffold → 写 stage / at → 本轮结束（对外说一句即停）
- extract 节点：LLM 槽位抽取（huji / gender_age）
- condition / code 节点：注册表 kind 判定 → 返回出口标签 → 沿对应 port 的边前进
- 死循环防护 max_step；兜底 scaffold=fallback

对外接口：run_flow(state, user_input) -> state（兼容原 graph.run_once 调用方）。
"""
import json
import re

from . import config, prompts
from .flow_default import DEFAULT_FLOW, TERMINAL_ACTIONS, get_default_flow
from .llm import (
    INTENT_LOCATION, INTENT_QUERY, INTENT_RETIRE, INTENT_UNEMP,
    classify_intent, get_llm, parse_json,
)
from .retire_calc import category_from_text, normalize_category
from .state import (
    AGE_THRESHOLD_MALE, AGE_THRESHOLD_FEMALE, classify_branch, gender_cn,
    judge_qualify, max_attempt, threshold_cn, threshold_num,
)

# 意图正则（与 nodes.intent_hit 同一来源，避免循环 import 故内联）
_INTENT_RE = re.compile(
    r"(广州.{0,12}(退休金|养老金|退休|领取|待遇))|(领取地|在广州退休|退休在哪|异地参保|领取养老金)"
    # 「想领/能不能领退休金·养老金」这类没提具体城市的诉求，同样属于领取地测算
    r"|((领|拿|享受|要|想).{0,6}(退休金|养老金|养老待遇))"
    r"|((退休金|养老金).{0,8}(怎么|如何|在哪|哪里|能不能|可以|多少))"
)

# 退休时间测算意图：问「什么时候/几岁能退休」这类「退休时间」问题
# 注意：必须优先于领取地意图判定——「我什么时候能在广州退休」问的是时间，走本支线
_RETIRE_INTENT_RE = re.compile(
    r"(什么时候|什么时间|何时|哪一年|哪年|几岁|多大岁数|多大|多少岁|几年后).{0,8}退休"
    r"|退休(时间|年龄)"
    r"|(延迟退休|退休延迟|延迟到)"
)

# 失业保险金测算意图：问「能领多少失业金 / 失业保险金怎么算」这类问题
_UNEMP_INTENT_RE = re.compile(
    r"(失业(保险)?金|失业了.{0,8}(领|拿|算)|领多少.{0,6}失业)"
    r"|(失业.{0,6}(补助|救济|待遇|能领|怎么算|有多少))"
)

_CITY_CN = {
    "guangzhou": "广州", "zhaoqing": "肇庆", "foshan": "佛山", "shenzhen": "深圳",
    "dongguan": "东莞", "shaoguan": "韶关", "zhuhai": "珠海", "huizhou": "惠州",
    "zhongshan": "中山", "jiangmen": "江门", "shantou": "汕头", "zhanjiang": "湛江",
    "qingyuan": "清远", "meizhou": "梅州", "shanwei": "汕尾", "heyuan": "河源",
    "yangjiang": "阳江", "maoming": "茂名", "chaozhou": "潮州", "jieyang": "揭阳",
    "yunfu": "云浮",
    # 地域俗称代号（抽取 prompt 同步约定）
    "chaoshan": "潮汕地区", "yuedong": "粤东地区", "yuexi": "粤西地区",
    "yuebei": "粤北地区", "kejia": "客家地区",
}

MAX_STEP = 40


def intent_hit(text: str) -> bool:
    """正则兜底判：这句话是不是在问「待遇领取地」。

    仅用于规则层兜底（mock 模式 / 模型判不出来时），**不是主判定**：
    真模型走 classify_intent_messages 按语义判，正则一定漏。
    """
    return bool(_INTENT_RE.search(text or ""))


def retire_intent_hit(text: str) -> bool:
    """是否在问「退休时间」（什么时候 / 几岁能退休）。"""
    return bool(_RETIRE_INTENT_RE.search(text or ""))


def unemp_intent_hit(text: str) -> bool:
    """是否在问「失业保险金」（能领多少 / 怎么算）。"""
    return bool(_UNEMP_INTENT_RE.search(text or ""))


# ============================================================
# flow 读取
# ============================================================
def get_flow() -> dict:
    """从 config 读当前 flow（含用户保存的节点坐标/连线）。

    返回深拷贝：防止调用方修改 flow 时污染 config 缓存/DEFAULT（merge 是浅拷贝）。
    """
    from copy import deepcopy
    cfg = config.get_config()
    flow = cfg.get("flow") or {}
    if not flow.get("nodes"):
        return get_default_flow()
    return deepcopy(flow)


def get_node_map(flow: dict) -> dict:
    """节点 id → 节点。执行器每轮都靠它按 id 找节点，id 重复会静默覆盖前一个。"""
    return {n["id"]: n for n in (flow.get("nodes") or [])}


def edges_from(flow: dict, node_id: str):
    """取某节点的所有出边。连线是数据驱动的唯一路由依据，没有隐式跳转。"""
    return [e for e in (flow.get("edges") or []) if e.get("from") == node_id]


def next_by_port(flow: dict, node_id: str, port: str = ""):
    """取某节点 port 出口对应的目标节点；无匹配返回 None。"""
    for e in edges_from(flow, node_id):
        if (e.get("port") or "") == (port or ""):
            return e.get("to")
    # 兼容：该节点没有带 port 边但只有一条普通出边
    if not port:
        plain = [e for e in edges_from(flow, node_id) if not e.get("port")]
        if len(plain) == 1:
            return plain[0].get("to")
    return None


# ============================================================
# 判定 kind 注册表（condition / code 共用）
# 签名：f(state, node_cfg) -> (label, update_dict)
# ============================================================
def _seed_retire(state: dict) -> dict:
    """进退休支线前，把其他支线已收集到的信息播种进来，避免重复问。
    - 性别=男 → 人员类别就是「男职工」，不用问
    - 出生年份已在领取地支线收集过 → 直接带入（月份通常没有，仍要问）
    """
    upd = {}
    if state.get("retire_birth_year") is None and state.get("birth_year"):
        upd["retire_birth_year"] = state["birth_year"]
    if state.get("retire_category") is None and state.get("gender") == "male":
        upd["retire_category"] = "male"
    return upd


# ============================================================
# 用户「改口纠正」：任意环节都能带着新值回到该字段所属支线重跑
# ============================================================
# 可被纠正的槽位 → (取值方式, 所属支线)
# 支线 huji=户籍链 / ga=性别年龄链 / retire=退休时间链
_CORRECTABLE = {
    "province": ("str", "huji"),
    "city": ("str", "huji"),
    "gender": ("gender", "ga"),
    "first_ins_age": ("int", "ga"),
    "birth_year": ("int", "ga"),
    "first_insured_year": ("int", "ga"),
    "gd_years": ("int", "ga"),
    "retire_birth_year": ("int", "retire"),
    "retire_birth_month": ("int", "retire"),
    "retire_category": ("category", "retire"),
    "unemp_paid_months": ("int", "unemp"),
    "unemp_reason": ("reason", "unemp"),
    "unemp_registered": ("bool", "unemp"),
}

# 「改口纠正」的字面线索：模型意图偶尔会把「我说错了，我户口在湖南」判成追问，
# 已出过结论时更不能靠猜 —— 命中改口词 + 业务词就先回那条支线抽取。
_CORRECTION_CUE = re.compile(
    r"(说错|打错|讲错|弄错|记错|填错|写错|选错|更正|改成|改一下|其实(是|我)|"
    r"不(是|对)[^。！？，]{0,10}(是|，)|重新说|刚才说)")
_HUJI_CUE = re.compile(r"(户口|户籍|籍贯)")
_GENDER_CUE = re.compile(r"(性别|男的|女的|男性|女性)")
_RETIRE_CUE = re.compile(r"(退休|出生|生日|年月|人员类别|男职工|女干部|女工人)")
_UNEMP_CUE = re.compile(r"(失业|失业金)")
# 「缴了 5 年」「缴费 18 个月」这类**没指名险种**的缴费时长改口。
# 有养老语境（广州 / 省内 / 养老 / 领取地）时，它属于领取地支线的「省内累计年限」，
# 不能算成失业金缴费；没有养老语境才按失业金支线改（见 _PENSION_CUE）。
_PAID_YEARS_CUE = re.compile(r"缴(费|纳|了|交|过)?\s*\d+\s*(年|个月|月)")
_PENSION_CUE = re.compile(r"(养老|退休金|领取地|待遇领取|广州|省内|社保)")
# 「参保/缴费情况」线索 —— 只说了「我说错了，是 2016 年参保的，那时 25 岁」时，
# 命中的是这条：改的是**领取地支线的首次参保年份/年龄**，不是退休支线的出生年月。
# 没有它，这类改口会落到 followup，用户改了等于没改（面板仍显示旧值）。
# 「省内」也算：省外户籍这条支线要判的就是「在广东省内参保」年限。
_GA_CUE = re.compile(r"(参保|缴|社保|年限|年数|工龄|参保年龄|年龄|岁|省内)")

# 「广东省内 / 省内 / 广东 + … + N 年」= 在广东省内参保的**年限**，
# 不是参保**年份** 2016。公子宇 2026-09-14 截图：话题是「首次在广东省参保的年龄」，
# 用户说「说错了，广东省内应该是 16 年的时间」→ 抽取器顺着话题读成
# first_insured_year=2016（还会连锁重算出生年份），其实他要更正的是**年限 16 年**。
# 三个约束都是为了**不误伤**「哦，是广东省啊，看错了，是16年」这类真的在说参保**年份**的句子：
#   ① 「广东」后面必须紧跟 参保/缴费/缴/交/满/累计 才认（「广东省啊，看错了，是16年」里的
#      「广东」是独立小句，不认）；「省内 / 广东省内」本身就是参保语境，直接认；
#   ② 中间不许夹逗号/句号（夹了就是两句话，不能算成一处）；
#   ③ `(?<!\d)` 免得把「2016 年」截成「16 年」。
_GD_YEARS_CUE = re.compile(
    r"(?:广东省内|省内|广东(?=参保|缴费|缴纳|缴|交|满|累计))"
    r"[^。；;！？\n，,]{0,12}?(?<!\d)(\d{1,2})\s*年")
# 反例护栏：「广东…16 年退休」说的是退休年份，别当年限。
_GD_NOT_YEARS_RE = re.compile(r"(退休|出生|生日|生年)")


def _gd_years_from_text(text: str):
    """从原话里认出「广东省内参保 N 年」的 N（认不出返回 None，绝不猜）。"""
    m = _GD_YEARS_CUE.search(text or "")
    if not m or _GD_NOT_YEARS_RE.search(m.group(0)):
        return None
    v = int(m.group(1))
    return v if 0 <= v <= 45 else None

# 「参保/缴费年份」被误读成「出生年份」的护栏。
# 抽取器看到 4 位年份就往 birth_year 上写，但「我第一次参保是 2010 年」
# 说的是参保年份 —— 不挡掉就会把出生年份一刀改成 2010（人瞬间变 15 岁）。
_YEAR_IS_INSURED_RE = re.compile(r"(参保|缴费|开始缴|交社保|缴社保|参加工作|进厂|第一次交)")
_YEAR_IS_BIRTH_RE = re.compile(r"(出生|生日|生年|哪年生的|那年生的)")

# 支线名 → 抽取节点名（改口后回该支线重跑）
_EXTRACT_NODE = {
    "huji": "extract_huji",
    "retire": "extract_retire",
    "unemp": "extract_unemp",
    "gender_age": "extract_ga",
}

# 「用户说的是本业务的信息」线索（性别 / 年龄 / 参保 / 户籍 / 退休…）。
# 这类内容即使**单独出现**（如首轮只说「女生」，或接着说「65 年的」）也绝不能
# 判成答非所问 —— 他在讲自己社保相关的情况，只是还没说要办什么，
# 属于「想办业务但信息不全」，应当接住并正常开场引导。
_SELF_INFO_CUE = re.compile(
    r"(男|女|先生|女士|岁|出生|生日|参保|缴费|社保|养老|退休|工龄|年限|"
    r"户口|户籍|籍贯|广东|广州|省外|省内|市|县|区|"
    r"工人|干部|管理岗|技术岗|工厂|车间|流水线|办公室|事业单位|公务员|在职|单位)")
# 真正的闲聊 / 无关内容（问天气、聊吃饭、娱乐等）：命中就仍按答非所问处理。
_SMALL_TALK_CUE = re.compile(
    r"(天气|气温|下雨|下雪|几号|几点了|星期|吃饭|吃了没|吃了吗|笑话|故事|"
    r"股票|足球|篮球|电影|游戏|你是谁|你叫什么|聊点别的|无聊|唱歌|音乐|新闻)")


def _coerce_slot(kind: str, v):
    """把模型给出的纠正值转成内部表示；转不了返回 None（宁可不改）。"""
    if kind == "str":
        s = str(v).strip()
        return s or None
    if kind == "int":
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    if kind == "gender":
        return v if v in ("male", "female") else None
    if kind == "reason":
        # 离职原因纠正：既接受「情形码」本身（模型直接判），
        # 也接受用户原话（由程序兜底词表归类）
        from .unemp_calc import REASON_CODES, reason_from_text
        s = str(v).strip()
        if s in REASON_CODES:
            return s
        return reason_from_text(s)
    if kind == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "yes", "1", "是", "已", "已经", "有"):
                return True
            if s in ("false", "no", "0", "否", "没", "未", "没有"):
                return False
        if isinstance(v, (int, float)):
            return bool(v)
        return None
    if kind == "category":
        from .retire_calc import category_from_text
        return normalize_category(v) or category_from_text(str(v))
    return None


def _recompute_derived(state: dict, applied: list) -> None:
    """改口后把**互相推算的字段重算一遍** —— 纠正一个，派生那个不能留旧值。

    三个数是同一根链：`出生年份 = 首次参保年份 − 首次参保年龄`。
    - 出生年份是用户给的（`birth_year_calc=False`）→ 以出生年份为准，重算参保年龄；
    - 出生年份是程序推算的（`birth_year_calc=True`）→ 以参保年龄为准，重算出生年份
      （推算值本来就不该比用户亲口说的更权威）。

    只看「本轮真的改了」的那几个字段，其余一律不动 —— 否则会把用户
    还没纠正的值悄悄改掉。例：改口「是 2016 年参保的，那时 25 岁」后，
    面板与结论里就不该再挂着旧的 29 岁。
    """
    t = set(applied or [])
    if not t:
        return
    if "retire_birth_year" in t:      # 与 birth_year 是同一个事实（下面已同步）
        t.add("birth_year")
    touched = t & {"birth_year", "first_ins_age", "first_insured_year"}
    if not touched:
        return
    iy = state.get("first_insured_year")
    if iy is None:
        return
    iy = int(iy)
    by = state.get("birth_year")
    a = state.get("first_ins_age")
    calc = bool(state.get("birth_year_calc"))
    if touched == {"first_ins_age"}:
        # 只改了参保年龄：出生年份是推算的才反推重算；用户亲手报的出生年份不动
        if calc and a is not None:
            nb = iy - int(a)
            if 1930 <= nb <= 2015:
                state["birth_year"] = nb
        return
    # 改了出生年份 / 参保年份 → 按上面那条权威性规则重算另一个
    if calc and "first_insured_year" in touched and a is not None:
        nb = iy - int(a)
        if 1930 <= nb <= 2015:
            state["birth_year"] = nb
    elif by is not None:
        na = iy - int(by)
        if 14 <= na <= 70:
            state["first_ins_age"] = na


def _apply_corrections(state: dict, corr) -> list:
    """把模型识别出的「用户改口」写回 state（纠正=覆盖，不是合并）。

    返回真正发生了变化的字段名列表（值没变的重复表述不算纠正，
    否则会无缘无故把流程退回去重跑一遍）。
    """
    if not isinstance(corr, dict):
        return []
    applied = []
    for k, v in corr.items():
        spec = _CORRECTABLE.get(k)
        if not spec or v in (None, "", [], {}):
            continue
        nv = _coerce_slot(spec[0], v)
        if nv is None or state.get(k) == nv:
            continue
        state[k] = nv
        applied.append(k)
    # 性别被改口时联动人员类别：改成男 → 类别即男职工；
    # 由男改成女 → 原「男职工」作废，重新按干部/工人问
    if "gender" in applied:
        cat = normalize_category(state.get("retire_category"))
        if state["gender"] == "male":
            state["retire_category"] = "male"
        elif cat == "male":
            state["retire_category"] = None
    # 用户自己报了/改了出生年份 → 之前推算来的标记作废
    if "birth_year" in applied or "retire_birth_year" in applied:
        state["birth_year_calc"] = False
    # 出生年份是**同一个事实**，两个字段（领取地支线的 birth_year / 退休支线的
    # retire_birth_year）必须一起走 —— 否则面板上会出现两个互相打架的出生年份
    # （「我第一次参保是 2010 年」那类误抽就会造成 birth_year=1991 而
    #   retire_birth_year=2010 这种自相矛盾的状态）。
    if "retire_birth_year" in applied and "birth_year" not in applied:
        state["birth_year"] = state["retire_birth_year"]
    if "birth_year" in applied and "retire_birth_year" not in applied \
            and state.get("retire_birth_year") is not None:
        state["retire_birth_year"] = state["birth_year"]
    # 纠正一个、派生那个必须跟着重算（否则面板/结论还挂着改口前的旧值）
    _recompute_derived(state, applied)
    return applied


def _collect_corrections(state: dict, corr, user_text: str = "") -> list:
    """覆盖写回本轮的 corrections，并**累积**到 `state["corrected"]`。

    累积是必要的：一条话里可能同时改了不止一个支线的字段，而每个 extract
    只认自己那条支线的 schema，多支线要各自抽一次 —— 后一次不能把前一次
    的结果冲掉（否则回退时只看得到最后一个支线的纠正）。
    """
    corr = _drop_misread_birth_year(corr, user_text)
    corr = _drop_misread_huji(corr, user_text, state)
    applied = _apply_corrections(state, corr)
    if applied:
        prev = [c for c in (state.get("corrected") or []) if c not in applied]
        state["corrected"] = prev + applied
    return applied


def _insured_year_misread(text: str) -> bool:
    """这句话里的年份是**参保/缴费年份**，不是出生年份？"""
    t = text or ""
    return bool(_YEAR_IS_INSURED_RE.search(t)) and not _YEAR_IS_BIRTH_RE.search(t)


def _drop_misread_birth_year(corr, user_text: str):
    """挡掉「参保年份被当成出生年份」（见 `_YEAR_IS_INSURED_RE` 注释）。

    抽取器见到 4 位年份就往 birth_year 上写，但「我第一次参保是 2010 年」
    说的是**参保年份**；不挡掉就会把出生年份一刀改成 2010（人瞬间 15 岁），
    还会把用户甩进他根本没提过的退休支线。
    """
    if not isinstance(corr, dict):
        return corr
    if _insured_year_misread(user_text):
        return {k: v for k, v in corr.items()
                if k not in ("birth_year", "retire_birth_year")}
    return corr


# 「参保语境里提到的省份 ≠ 户籍」护栏（2026-09-14 截图 bug）。
# 用户在回答「第一次在广东省参保时…」时提到「广东省」，说的是**参保地**
# （本工具的参保地本来就是广东），不是户籍；但抽取器会把它当成户籍改口
# （corrections.province=guangdong，实测命中率最高 6/6），户籍被改成广东后
# `fix_route` 又回户籍链分类 → 缺城市 → 回头问「您是广州户籍吗？」。
# 只有用户明确提「户口 / 户籍 / 籍贯」，或用「不是…」这类**否定自己先前答案**的
# 措辞时，才算真的改户籍。
_HUJI_WORD_RE = re.compile(r"(户口|户籍|籍贯)")
# 注意**故意不含「看错」**：「看错了」是看错了参保记录（参保语境），不是户籍答错；
# 「弄错 / 搞错 / 记错 / 说错 / 不是…」才是更正自己先前给过的户籍答案。
_HUJI_ANSWER_WRONG_RE = re.compile(r"(不是|不对|并非|弄错|搞错|记错|写错|填错|说错)")


def _drop_misread_huji(corr, user_text: str, state: dict):
    """挡掉「参保/退休/失业语境里提到的省份被当成户籍改口」。"""
    if not isinstance(corr, dict):
        return corr
    if "province" not in corr and "city" not in corr:
        return corr
    t = user_text or ""
    # ⚠️ 「说错」二字命中**不足以**判成改户籍：他改的可能是**参保年限**
    # （「说错了，广东省内应该是 16 年」）—— 句子里带「广东省内参保 N 年」时，
    # 这个「广东」是**参保地**，不能写回户籍，否则户籍被改成广东、回头问「您是广州户籍吗？」
    if _HUJI_WORD_RE.search(t):
        return corr
    if _HUJI_ANSWER_WRONG_RE.search(t) and _gd_years_from_text(t) is None:
        return corr
    # 户籍已经收集过、且当前**不是在问户籍**（在问参保/退休/失业）→
    # 本轮出现的省份按「参保地 / 业务发生地」理解，不写回户籍。
    if state.get("province") and state.get("stage") != "await_huji":
        return {k: v for k, v in corr.items() if k not in ("province", "city")}
    return corr


def _k_route_correction(state, cfg):
    """extract 之后的统一回退口：用户改口了哪条支线的字段，就回到哪条支线重跑。

    户籍决定走哪条支线（广州 / 省内非广州 / 省外），所以户籍一旦改口
    必须回户籍链重新分类；其余按字段归属回对应链的「是否齐全」判定，
    齐全就直接往前走（出结论 / 重算），缺什么才问什么 —— 不再重复问已答过的项。
    """
    corr = state.get("corrected") or []
    src = state.get("extract_src") or "huji"
    back = {"corrected": []}          # 本轮纠正信号用完即清，避免反复回退
    if "province" in corr or "city" in corr:
        return "huji", back
    # 退休支线**专属**字段（出生月份 / 人员类别）改了 → 必回退休支线重算。
    # 只改了「出生年份」时：它跟领取地支线是同一个事实（见 _recompute_derived 的同步），
    # 用户没说退休，就别把他一脚踢进退休支线，按来源支线回去重算即可。
    if any(str(k) in ("retire_birth_month", "retire_category") for k in corr) \
            or (any(str(k).startswith("retire_") for k in corr) and src == "retire"):
        return "retire", back
    if any(str(k).startswith("unemp_") for k in corr):
        return "unemp", back
    if src == "huji":
        return "huji", back
    if src == "ga":
        return "ga", back
    if src == "unemp":
        return "unemp", back
    return "retire", back


def _correction_route_target(text: str):
    """已出结论后，「改口 / 补正」该回哪条支线抽取 —— 返回 extract 节点名或 None。

    模型意图偶尔会把这类句子判成 followup，甚至判成 query（截图：结论后
    「说错了，广东省内应该是 16 年的时间」被回了一句「登录粤省事就能查参保缴费记录」
    的客套引导）。所以这里再加一层程序兜底：改口词、或「广东省内参保 N 年」这类
    明确的业务年限线索，先回支线把新值抽回来重算，再谈怎么查、怎么办。
    """
    t = text or ""
    if not (_CORRECTION_CUE.search(t) or _gd_years_from_text(t) is not None):
        return None
    if _HUJI_CUE.search(t):
        return "extract_huji"
    if _RETIRE_CUE.search(t):
        return "extract_retire"
    # 失业语境、或「缴了 5 年」这种没指名险种的缴费时长（且没提养老/领取地）
    # → 按失业金支线改。说「我在广州缴了 8 年」属于省内累计年限，落到下面。
    if _UNEMP_CUE.search(t) or (
            _PAID_YEARS_CUE.search(t) and not _PENSION_CUE.search(t)):
        return "extract_unemp"
    # 「参保/缴费/年龄/岁/省内」这类**领取地支线**的字段：以前没有这条线索，
    # 「我说错了，是 2016 年参保的，那时 25 岁」会落到 followup → 改了等于没改，
    # 面板上还挂着旧值（如首次参保年龄 29 岁）。放最后，前面几条更明确。
    if _GENDER_CUE.search(t) or _GA_CUE.search(t):
        return "extract_ga"
    return None


def _k_route_intent(state, cfg):
    """按 stage + 用户意图把"本轮"分流。

    意图由模型语义判断（llm.classify_intent），不再靠正则硬匹配字面词；
    模型不可用时 classify_intent 内部会回退规则判定。
    """
    stage = state.get("stage")
    text = state.get("user_input", "") or ""
    if stage == "await_huji":
        return "extract_huji", {}
    if stage == "await_gender_age":
        return "extract_ga", {}
    if stage == "await_retire":
        return "extract_retire", {}
    if stage == "await_unemp":
        return "extract_unemp", {}
    # done：结论已给，允许用户接着提新业务（如退休时间）；idle/None：首轮意图判定
    done = stage == "done"
    intent = classify_intent(text, stage_done=done)
    if intent == INTENT_RETIRE:
        return "retire", _seed_retire(state)
    if intent == INTENT_UNEMP:
        # 先抽取再判：用户首轮可能已把缴费时长一并说了
        # （「我缴了3年失业金能领多少」→ 直接出结论，不该再反问一遍）
        return "extract_unemp", {}
    # 已出过结论后的「改口纠正 / 补正」要**优先于查询指引**：他是在更正数据、
    # 不是问怎么查 —— 先抽取重算，再谈手续。（顺序反了就会出现截图里那种
    # 「用户更正年限、系统回一句『登录粤省事查缴费记录』」的答非所问。）
    if done:
        target = _correction_route_target(text)
        if target:
            return target, {}
    # 「怎么查自己的参保/缴费情况」→ 引导粤省事自查（不论是否已出过结论都适用）
    if intent == INTENT_QUERY:
        return "guide_ss", {}
    if done:
        if intent == INTENT_LOCATION:
            return "extract_huji", {}
        return "followup", {}
    if intent == INTENT_LOCATION:
        return "ask_huji", {}
    # 程序复核：模型把「女生」这类**本业务字段**判成答非所问时（他其实在回答
    # 性别/年龄/参保等问题，只是还没说要办什么），绝不能回「不在测算范围」把人踢走。
    # 先把能抽到的字段记下来（跨支线各抽一次，避免他重复说），再正常开场问户籍。
    if _SELF_INFO_CUE.search(text) and not _SMALL_TALK_CUE.search(text):
        _run_extract({"kind": "gender_age"}, state)
        _run_extract({"kind": "huji"}, state)
        return "ask_huji", {}
    return "off_topic", {}


def _k_missing_huji(state, cfg):
    """户籍信息齐不齐。齐 → ok；还没问过 → ask（首问）；问过但仍缺 → miss（走重试链）。

    三种返回决定完全不同的走向，改判定前先想清楚：
      ok    信息齐了，可以分类
      ask   首次提问，用普通问法
      miss  问过了还缺，交回重试配额机制处理（可能追问、也可能换粤省事指引、或放弃）

    ⚠️ huji_asked 是「已经问过他」的终身标记，不是「本轮问没问」。
    改口纠正回退过来时靠它区分「该首问」还是「该追问」，别当临时变量用。
    """
    # 与 v5 classify_branch 语义一致：缺省才算缺失；
    # 外省给到省（无市）也算齐全（走 other_prov）；广东缺市才算缺失。
    _, missing = classify_branch(state.get("province"), state.get("city"))
    if not missing:
        return "ok", {}
    upd = {}
    # 本轮他答不上户籍 / 反问怎么办 → 追问改用「粤省事查询或办理」指引话术
    upd["retry_kind"] = "unknown" if state.get("huji_unknown") else "normal"
    # 与另外两条反问同一口径：本轮补进了新信息、或仍在语境里回应
    # （如「不记得户籍了」）→ 追问配额归零，不把有效互动当成失败重试
    if state.get("huji_progress") or state.get("huji_in_context"):
        upd["attempt_huji"] = 0
    # 还没问过户籍 → 走首问问法（改口纠正回退过来时，缺什么才问什么）
    if not state.get("huji_asked"):
        return "ask", upd
    return "miss", upd


def _k_classify_huji(state, cfg):
    """按户籍分三支（gz / gd_other / other_prov）并写回 branch。缺市仍算 miss。

    与外省口径的差异全在 classify_branch 里，这里只做「齐了才分类」。
    """
    branch, missing = classify_branch(state.get("province"), state.get("city"))
    if missing:
        return "miss", {}
    return branch, {"branch": branch}


# 每条支线的重试配额，对应「本轮是否仍在语境里回应」的标记字段。
# 语境内回应（「不记得了」「不清楚」「能在哪里查吗」等在回应当前提问）
# **不算一次失败重试**，绝不能消耗配额；只有真正的答非所问
# （问天气、问时间、聊别的）才消耗。extract 节点每轮刷新这两个标记。
_CTX_FIELDS = {
    "attempt_gender_age": ("ga_progress", "ga_in_context"),
    "attempt_retire": ("retire_progress", "retire_in_context"),
    "attempt_huji": ("huji_progress", "huji_in_context"),
    "attempt_unemp": ("unemp_progress", "unemp_in_context"),
}


def _k_attempt_left(state, cfg):
    """重试配额闸门。返回 retry（继续引导）/ giveup（放弃，交回主流程）。

    这是「不把用户踢走」的核心保障，改动前务必读懂上面 _CTX_FIELDS 那段：
    在语境里回应（含「不知道」）→ 配额归零；只有真答非所问才 +1。
    配额用完返回 giveup，由上层决定换话题还是结束，**不要在这里直接说放弃话术**。
    """
    field = (cfg or {}).get("field", "attempt_huji")
    max_field = (cfg or {}).get("max_field", "max_huji_retry")
    limit = int(config.get_config()["business"].get(max_field, 2))
    # 关键：本轮是「在语境里回应」（补了新信息、或答不上来但在回应本流程）
    # → 不是答非所问，不递增配额（保持 0），继续正常引导/追问。
    prog_f, ctx_f = _CTX_FIELDS.get(field, (None, None))
    if ctx_f and (state.get(ctx_f) or (prog_f and state.get(prog_f))):
        return "retry", {field: 0}
    cur = int(state.get(field) or 0)
    if cur < limit:
        return "retry", {field: cur + 1}
    return "giveup", {}


def _ga_ask_items(state):
    """算出性别年龄支线「本轮还该问什么」——只列缺项，问法按搭配规则来。

    搭配规则：
      · 缺年龄时，若用户**已经在广东省首次参保的年份**（如「16年参保的」）
        → 问**出生年份**（用它换算参保那年的周岁，比直接问他周岁更省事、更准）；
      · 缺年龄且用户**没提过参保年份** → 直接问**首次参保年龄**；
      · 缺性别 → 问性别。
    完全齐了返回空列表。
    """
    ask = []
    if state.get("gender") not in ("male", "female"):
        ask.append("性别")
    if state.get("first_ins_age") is None or state.get("age_ambiguous"):
        ask.append("出生年份" if state.get("first_insured_year") else "首次参保年龄")
    return ask


def _ga_known_text(state):
    """把已收集到的信息摘成**一句自然口语**，给模型当「这些别重问」的依据。

    刻意避开字段名清单腔（不再输出「出生年份：1991年、首次参保年份：2016年、
    首次参保年龄：25岁」这种对账格式）：
      · 参保年份与参保年龄本是同一件事 → 合成一条说（「您 2016 年首次参保、当时 25 岁」）；
      · 由「参保年份 − 参保年龄」推算来的出生年份 → 不再单独提（本来就从参保信息推出来的）。
    返回形如「您 2016 年首次参保、当时 25 岁，是男性。」；什么都没收集到时返回空串。
    """
    seg = ""
    iy, ia = state.get("first_insured_year"), state.get("first_ins_age")
    if iy and ia is not None:
        seg = f"您 {int(iy)} 年首次参保、当时 {int(ia)} 岁"
    elif iy:
        seg = f"您 {int(iy)} 年首次参保"
    elif ia is not None:
        seg = f"您首次参保时 {int(ia)} 岁"
    if not seg and state.get("birth_year"):
        # 只有推算来的出生年份这一项（罕见）：标一下，用户看着不对可当场改口
        _calc = "（按首次参保年龄推算）" if state.get("birth_year_calc") else ""
        seg = f"您是 {int(state['birth_year'])} 年出生的{_calc}"
    g = state.get("gender")
    if g in ("male", "female"):
        gtxt = "男性" if g == "male" else "女性"
        seg = f"{seg}，是{gtxt}" if seg else f"您是{gtxt}"
    if not seg:
        return ""
    return seg + "。"


def _ask_sentence(ask):
    """把缺项清单拼成**一句能直接说出口的问句**（给兜底文案用）。

    兜底文案是「模型没说好时」的替代品，必须自己就自然：只有一项时直接问；
    两项就顺势用「还有，」接一句 —— 不要拼成「性别、出生年份」这种字段清单腔。
    """
    parts = []
    if "性别" in ask:
        parts.append("您是男性还是女性")
    if "出生年份" in ask:
        parts.append("您的出生年份")
    if "首次参保年龄" in ask:
        parts.append("您第一次在广东省参加养老保险时是多少岁")
    if not parts:
        return ""
    if len(parts) == 1:
        return f"请问{parts[0]}？"
    return "请问" + "？还有，".join(parts) + "？"


_RETIRE_CAT_CN = {
    "male": "男职工",
    "female_cadre": "原 55 岁退休女职工",
    "female_worker": "原 50 岁退休女职工",
}


def _retire_known_text(state):
    """退休支线版：把已收集到的信息摘成一句自然口语（不罗列字段名）。

    出生年月与人员类别合成一句（「您是 1965 年出生的男职工」），
    比「出生年份：1965年、人员类别：男职工」这种对账口气自然得多。
    """
    by, bm = state.get("retire_birth_year"), state.get("retire_birth_month")
    if by and bm:
        birth = f"{int(by)} 年 {int(bm)} 月出生"
    elif by:
        # 由「首次参保年份 − 首次参保年龄」推算来的年份，标注一下，
        # 用户看到能顺手纠正（改口纠正机制会带着新值重算）
        _calc = "（按首次参保年龄推算）" if state.get("birth_year_calc") else ""
        birth = f"{int(by)} 年出生{_calc}"
    else:
        birth = ""
    cat = _RETIRE_CAT_CN.get(state.get("retire_category") or "") or ""
    if not cat and state.get("gender") == "female":
        cat = "女性"
    if birth and cat:
        return f"您是 {birth}的{cat}。"
    if birth:
        return f"您是 {birth}的。"
    if cat:
        return f"您是{cat}。"
    return ""


def _unemp_known_text(state):
    """失业金支线：已收集信息摘成一句自然口语（缴费时长 / 离职原因 / 失业登记）。"""
    from .unemp_calc import INVOLUNTARY_SHORT, VOLUNTARY_REASON
    parts = []
    m = state.get("unemp_paid_months")
    if m is not None:
        parts.append(f"累计缴费 {_fmt_months_cn(int(m))}")
    code = state.get("unemp_reason")
    if code:
        label = ("本人主动辞职" if code == VOLUNTARY_REASON
                 else (INVOLUNTARY_SHORT.get(code) or "其他情形"))
        parts.append(f"离职原因是{label}")
    reg = state.get("unemp_registered")
    if reg is True:
        parts.append("已办理失业登记")
    elif reg is False:
        parts.append("还没办理失业登记")
    if not parts:
        return ""
    return "您" + "，".join(parts) + "。"


def _unemp_ask_sentence(missing) -> str:
    """按「还缺的第一项」拼一句现成问句 —— 兜底稿直接照说也不生硬，且一次只问一项。"""
    first = (list(missing) or [""])[0]
    if first.startswith("累计缴费"):
        return "请问您累计缴纳失业保险多久了？几年或几个月都可以。"
    if first.startswith("离职原因"):
        return "请问您这次是怎么离职的？是合同到期、单位裁员辞退，还是本人主动辞职？"
    if first.startswith("是否已办理失业登记"):
        return "请问您办理失业登记了吗？"
    return ""


# 「怎么查自己的参保缴费记录」这类**查询意图**线索：句子里带「养老」不等于要办退休，
# 判业务名要先认这个（公子宇 2026-09-14 复核发现）。
_QUERY_RECORD_CUE = re.compile(
    r"怎么查|在哪查|哪里查|如何查|查一下|查查|查询|明细|记录|打印|"
    r"缴(了|纳)?(多少|几年|多久)|缴够|够不够|够了没")


def _service_name(state) -> str:
    """当前用户正在咨询 / 办理的**具体业务名** —— 「粤省事」指引话术里必须点名它。

    ⚠️ 公子宇 2026-09-14：不要用「这项业务可以登录粤省事查询或办理」这种**通用话术**，
    他正在办什么就说什么（问失业金说「失业保险金」、差办登记就说「失业登记」）。
    这个值作为 `{service}` 注入提示词与兜底稿；模型若仍说「这项业务」会被禁语闸门整段替换。
    """
    t = f"{state.get('user_input') or ''}"
    # 失业金支线（正在追问 / 已出结论）—— 若还缺失业登记，那就直接点名「失业登记」
    in_unemp = (
        str(state.get("stage") or "").startswith("await_unemp")
        or state.get("unemp_result") is not None
        or state.get("unemp_missing") is not None
        or state.get("unemp_paid_months") is not None
        or state.get("unemp_reason") is not None
        or state.get("unemp_registered") is not None
    )
    if "失业登记" in t:
        # 用户原话里**自己点名**了失业登记 → 就答这一项（问什么答什么）
        return "失业登记"
    if in_unemp or "失业" in t:
        # 结论已出：他此刻问的是「失业金这待遇怎么办 / 怎么申请」——主题是
        # **失业保险金本身**，不能因为「还差办登记」又把话题拉回失业登记，
        # 否则他说「那我要怎么申请呢」会被反复回一句「失业登记可以登录粤省事办理」。
        # （公子宇 2026-09-14 反馈：主题重点是失业金申领）
        if state.get("unemp_result") is not None:
            return "失业保险金"
        # 还在追问阶段：只看**正在问的那一项**（缺项表的第一个）
        # —— 整表命中会误判成「失业登记」
        first = next(iter(state.get("unemp_missing") or []), "")
        if (state.get("unemp_registered") is False
                or first.startswith("是否已办理失业登记")):
            return "失业登记"
        return "失业保险金"
    # 「怎么查 / 在哪看自己的参保缴费记录」→ 主业是**查记录**，不是办退休手续
    # （「又带养老又带怎么查」的句子要按查询走）
    if _QUERY_RECORD_CUE.search(t):
        return "参保缴费记录"
    if "退休" in t or "养老" in t:
        return "退休手续"
    if "户籍" in t or "户口" in t:
        return "户籍信息"
    return "参保缴费记录"


def _guide_action(service: str) -> str:
    """「{service}可以登录粤省事 ___」里的动词 —— 按业务性质选，别一律「查询或办理」。

    失业金是**申领**（一份待遇）、登记/户籍/退休是**办理**、记录类才是**查询**。
    """
    if service == "失业保险金":
        return "申领"
    if service == "参保缴费记录":
        return "查询"
    return "办理"


def _huji_known_text(state) -> str:
    """户籍支线：把已收到的户籍摘成一句口语（给模型看的内部参考，避免重复追问）。

    只讲"在哪"这一件事，不做字段名清单腔；没有就返回空串。
    """
    p, c = state.get("province"), state.get("city")
    if not p:
        return ""
    if p == "guangdong":
        cn = _CITY_CN.get(c or "", "")
        return f"他户籍在{cn}。" if cn else "他户籍在广东省内。"
    return "他户籍在广东省外。"


def _fmt_months_cn(m: int) -> str:
    """月数 → 中文（"2年" / "2年6个月" / "18个月"）。"""
    y, r = divmod(int(m), 12)
    if y and r:
        return f"{y}年{r}个月"
    if y:
        return f"{y}年"
    return f"{m}个月"


def _k_check_ga(state, cfg):
    """性别年龄支线「还缺什么」检查。缺项用 _ga_ask_items 精确算，只问缺的。

    两条业务约定写在这里，改之前先确认：
      ① 用户在省内累计参保几年 **不是必收项** —— 不主动问，结论时才统一讲「满 N 年」；
         只有他自己提了年数，才拿去判年限不足（见 _k_judge_age）。
      ② 用户说「不记得首次参保时间」时，不能把整条支线停掉 —— 他知道的项（如性别）
         还要接着问。整条停掉会出现「没问也没说」的空档，模型为了圆场会编一句
         「性别我记下了」，用户一眼就能看出系统在瞎记。
    """
    unknown = state.get("age_unknown")
    years_unknown = state.get("years_unknown")

    # 精确算出还缺哪几项，追问时只问缺的，绝不重复问已确认过的
    # 注意：省内累计参保年数**不是必收项**——不主动问，结论时统一告知「满 N 年」条件；
    # 用户主动说了年数才用于年限不足判定（见 _k_judge_age）
    ask = _ga_ask_items(state)

    if not ask:
        return "ok", {}

    known = _ga_known_text(state)
    # 用户明确表示不知道首次参保时间 → 不重复追问，改为指导查询（粤省事）
    # 粤省事能查到首次参保时间，无论还缺哪几项都应先引导查询，
    # 而不是机械地继续追问
    if unknown or years_unknown:
        # 他不知道的只是「参保时间」这一项 —— 性别这种他答得上来的仍要接着问。
        # 若把整条支线一并停掉，就会出现「性别没收着、却既不问也不再提」的空档，
        # 模型为了圆场还会编出一句「性别我记下了」（用户一眼看出系统在瞎记）。
        still = [x for x in ask if x not in ("首次参保年龄", "出生年份")]
        upd = {
            "retry_reason": "用户不知道/不记得首次参保时间或参保年数",
            "retry_kind": "unknown",
            "ga_missing": ask,
            "ga_ask": still,
            "ga_still_ask": still,
            "ga_known": known,
        }
    else:
        upd = {
            "retry_reason": "、".join(ask) + "缺失，请按搭配规则补问",
            "retry_kind": "normal",
            "ga_missing": ask,
            "ga_ask": ask,
            "ga_known": known,
        }
    # 本轮用户补进了新信息（如刚查到「15年参保」），
    # 或者本轮虽没给信息、但**仍在回应本流程的提问**（如「不记得了」「回头查一下」），
    # 都算正常互动而非答非所问 → 追问配额归零重新给足；
    # 只有纯闲聊（问天气/问时间等 ga_in_context=false）才消耗配额。
    if state.get("ga_progress") or state.get("ga_in_context"):
        upd["attempt_gender_age"] = 0
    # 本条支线还**没问过**就说（如「户籍改口回退后重判、信息仍不全」，
    # 或刚被判为省外）→ 用首问问法，别一上来就"重试"口吻；
    # 问过一次之后（say 节点会置 ga_asked）才走 ga_attempt 的重试/放弃链。
    if not state.get("ga_asked"):
        return "ask", upd
    return "miss", upd


def _k_judge_age(state, cfg):
    """年龄性别 + 省内缴费年限的**硬判定**（两个条件串联，缺一不可）。

    条件一：性别 / 到龄（judge_qualify 程序判，阈值走 config）。
    条件二：**仅省外户籍**才追加 —— 在广东省内累计参保须满 other_prov_years_required
            （默认 10 年）。这条只对 other_prov 有意义，广州户籍和省内其他市不查。

    注意 gd_years 为 None 时**不算年限不足**：省内累计年数不是必收项，用户没说就
    不能用「没说」推断成「不够」。这是很容易写错的一处，会把没答的人误判为不合格。

    judge_reason 分两种，结论话术不同，别混：
      age_threshold → 已到龄，不能领
      years_limit   → 到龄了但省内缴费不够，也不能领（原因完全不同，话术要分开）
    """
    j = judge_qualify(state.get("gender"), state.get("first_ins_age"))
    if j == "qualify":
        # 年龄达标后还要看省外户籍的第二个硬条件：在广东省内参保满 N 年
        years = state.get("gd_years")
        years_min = int(config.get_config()["business"].get("other_prov_years_required", 10))
        if years is not None and years < years_min:
            return "unqualify", {"judge": "unqualify", "judge_reason": "years_limit"}
        return "qualify", {"judge": "qualify", "judge_reason": None}
    if j == "unqualify":
        return "unqualify", {"judge": "unqualify", "judge_reason": "age_threshold"}
    return "miss", {"judge": None}


def _k_check_retire(state, cfg):
    """退休时间测算：出生年月 + 人员类别 是否齐全。只列缺的项。
    性别=男 → 人员类别就是「男职工」，直接落位，绝不问「干部还是工人」。
    """
    missing, upd = [], {}
    # 只列**真正还缺**的那一项：已知出生年份就别再说"出生年月"，
    # 直接说"出生月份" —— 已经给过的信息不再重复索要。
    _by, _bm = state.get("retire_birth_year"), state.get("retire_birth_month")
    if _by is None and _bm is None:
        missing.append("出生年月（具体到哪年哪月）")
    elif _by is None:
        missing.append("出生年份")
    elif _bm is None:
        missing.append("出生月份")
    if normalize_category(state.get("retire_category")) is None:
        if state.get("gender") == "male":
            upd["retire_category"] = "male"      # 已知男性：类别自动确定
        elif state.get("gender") == "female":
            # 已知女性：只问干部/工人，别再把「男职工」列进去
            missing.append("人员类别（女干部 / 女工人）")
        else:
            missing.append("人员类别（男 / 女干部 / 女工人）")
    if not missing:
        return "ok", upd
    upd["retire_missing"] = missing
    # 本轮他答不上出生年月/类别 / 反问怎么办 → 追问改用「粤省事查询或办理」指引话术
    upd["retry_kind"] = "unknown" if state.get("retire_unknown") else "normal"
    # 与性别年龄支线同一口径：本轮补进了新信息、或仍在语境里回应
    # （如「不记得出生月份」）→ 追问配额归零，不算答非所问
    if state.get("retire_progress") or state.get("retire_in_context"):
        upd["attempt_retire"] = 0
    # 还没问过 → 首问问法（改口纠正回退过来时同样适用）
    if not state.get("retire_asked"):
        return "ask", upd
    return "miss", upd


def _k_check_unemp(state, cfg):
    """失业保险金：收集三个槽位 —— 累计缴费时长（月）、离职原因、失业登记+求职要求。

    「申领材料齐全」不在本程序判定范围（经办环节核验），所以不追问。
    **只问会话里真正还缺的那一项**：已经说过的绝不重复问。
    """
    missing = []
    if state.get("unemp_paid_months") is None:
        missing.append("累计缴费时间（缴了多少年或多少个月）")
    if state.get("unemp_reason") is None:
        missing.append("离职原因（是合同到期、单位裁员辞退，还是本人主动辞职）")
    if state.get("unemp_registered") is None:
        missing.append("是否已办理失业登记")
    if not missing:
        return "ok", {}
    upd = {"unemp_missing": missing}
    # 本轮他答不上被问的那一项 / 反问怎么办 → 追问改用「粤省事查询或办理」指引话术
    upd["retry_kind"] = "unknown" if state.get("unemp_unknown") else "normal"
    # 同其他支线口径：本轮补了新信息、或仍在语境里回应 → 配额归零
    if state.get("unemp_progress") or state.get("unemp_in_context"):
        upd["attempt_unemp"] = 0
    if not state.get("unemp_asked"):
        return "ask", upd
    return "miss", upd


KIND_REGISTRY = {
    "route_intent": _k_route_intent,
    "route_correction": _k_route_correction,
    "missing_huji": _k_missing_huji,
    "classify_huji": _k_classify_huji,
    "attempt_left": _k_attempt_left,
    "check_ga": _k_check_ga,
    "judge_age": _k_judge_age,
    "check_retire": _k_check_retire,
    "check_unemp": _k_check_unemp,
}


# ============================================================
# 节点执行
# ============================================================
def _fmt(tpl: str, vars_dict: dict) -> str:
    class _D(dict):
        def __missing__(self, k):
            return "{" + k + "}"
    return (tpl or "").format_map(_D(vars_dict or {}))


def _facts_for_branch(branch: str, state: dict) -> dict:
    """按分支准备结论话术要用的事实变量（供模板 format 填充）。

    为什么按分支给：三条分支的结论要讲的东西不一样，给多了模型会拿无关变量瞎发挥。
      gz        广州市内，讲「最后参保地」即可，不需要户籍城市
      gd_other  要说出「您户籍在 XX 市」，所以给 city_cn
      other_*   省外，要讲年龄门槛与省内缴费年限两个条件，给性别/年龄/年限

    _CITY_CN 查不到时兜底「该地市」，不要让它变成空字符串 —— 否则话术会出现
    「您户籍在 ，」这种断句。
    """
    if branch == "gz":
        return {}
    if branch == "gd_other":
        return {"city_cn": _CITY_CN.get(state.get("city") or "", "该地市")}
    # other_qualify / other_unqualify（省外户籍）
    g = state.get("gender") or "male"
    return {
        "gender": g,
        "gender_cn": gender_cn(g),
        "first_ins_age": state.get("first_ins_age"),
        "threshold_cn": threshold_cn(g),
        "threshold_num": threshold_num(g),
        "gd_years": state.get("gd_years"),
        "years_required": int(config.get_config()["business"].get("other_prov_years_required", 10)),
    }


def _build_say_scaffold(node_cfg: dict, state: dict) -> dict:
    """组装 say 节点的 scaffold（overrides > 顶层 config；attempt/reason 现取）。"""
    cfg = config.get_config()
    action = node_cfg.get("action", "")
    ov = node_cfg.get("overrides") or {}
    base_key = action
    if action == "conclusion":
        branch = node_cfg.get("branch", "")
        facts = _facts_for_branch(branch, state)
        fb_key = rf_key = f"conclusion_{branch}"
        # 省外未达标分两种原因：超龄 / 省内参保年限不足 → 用不同结论模板
        if branch == "other_unqualify" and state.get("judge_reason") == "years_limit":
            fb_key = rf_key = "conclusion_other_unqualify_years"
    elif action == "conclusion_retire":
        # 退休时间测算结论：facts 直接来自「退休计算程序」的输出
        r = state.get("retire_result") or {}
        facts = {
            "birth_cn": r.get("birth_cn") or "",
            "category_cn": r.get("category_cn") or "",
            "base_age_cn": r.get("base_age_cn") or "",
            "original_retire_cn": r.get("original_retire_cn") or "",
            "retire_age_cn": r.get("retire_age_cn") or "",
            "retire_cn": r.get("retire_cn") or "",
            "delay_months": r.get("delay_months", 0),
            "delayed": bool(r.get("delayed")),
            "min_years_cn": r.get("min_contribution_cn", "15年"),
        }
        fb_key = rf_key = "conclusion_retire"
    elif action == "conclusion_unemp":
        # 失业保险金结论：facts 来自「失业金计算程序」的输出。
        # 够条件（缴费/离职原因/失业登记三项都满足）→ 报月数与金额；
        # 不够条件 → 报「哪几项不符合」+ 程序给的一句下步建议（不让模型自己编政策）。
        r = state.get("unemp_result") or {}
        facts = {
            "paid_cn": r.get("paid_cn") or "",
            "paid_months": r.get("paid_months"),
            "months": r.get("months"),
            "months_cn": r.get("months_cn") or "",
            "min_wage": r.get("min_wage"),
            "total": r.get("total"),
            "total_cn": r.get("total_cn") or "",
            "formula_cn": r.get("formula_cn") or "",
            # 不合格时由程序给出的「哪几项不符合 / 下步建议」
            "fail_cn": r.get("fail_cn") or "",
            "advice_cn": r.get("advice_cn") or "",
        }
        if r.get("eligible") is False:
            # 分两种「没到位」（公子宇 2026-09-14 口径）：
            #   · 资格本身不符合（缴费不满 1 年 / 本人主动辞职）→ 才说「不符合」
            #   · 只差办失业登记（①②都符合）→ 说「符合，办了登记就能申请」，
            #     并把能领多少一起告诉他（程序已在 qualified 时算好月数金额）
            fb_key = rf_key = ("conclusion_unemp_ineligible" if r.get("blocked")
                               else "conclusion_unemp_pending")
        else:
            fb_key = rf_key = "conclusion_unemp"
    else:
        facts = {}
        fb_key = rf_key = base_key

    # 「答不上来 / 反问怎么查怎么办」→ 统一换成「可以登录粤省事查询或办理、
    # 具体路径稍候短信发给您」的指引话术与兜底（各支线反问共用一套，不新增节点）。
    hint_key = None
    if action.startswith("ask_") and state.get("retry_kind") == "unknown":
        fb_key = "ask_unknown_guide"
        hint_key = "ask_unknown_guide"
    # 失业金结论是**同一个 action、两套以上提示词**：把 hint_key 钉在 scaffold 上，
    # 不合格 / 只差登记时必须各给对应那份 —— 否则模型拿到「三项都符合、可以领取」的提示词，
    # 会把不合格也说成「您符合领取条件」（真机实测踩到过）。
    if action == "conclusion_unemp" and fb_key in ("conclusion_unemp_ineligible",
                                                   "conclusion_unemp_pending"):
        hint_key = fb_key

    # 反问 2（首问 / 重试）：注入「已收集到什么」「本轮还缺什么」，
    # 让模型按搭配规则只问缺的那一项 —— 缺年龄时：
    #   用户报过首次参保年份 → 问出生年份；没报过 → 问首次参保年龄。
    # 首问（ask_gender_age）是从 classify_huji 直接进来的、check_ga 还没跑，
    # 所以要在这里现算，不能依赖 state["ga_missing"]。
    # 反问 1（户籍）：同口径也给 {known}/{missing}/{ask_tail}，
    # 「答不上来」时改用粤省事指引话术时这几个变量要能用。
    if action in ("ask_huji", "ask_huji_retry"):
        _prov = state.get("province")
        facts = {
            "missing": ("户籍所在市" if (_prov == "guangdong" and not state.get("city"))
                        else "户籍地（省 / 市）"),
            "known": _huji_known_text(state),
            "ask_tail": "",
        }

    ga_ask = []
    if action in ("ask_gender_age", "ask_gender_age_retry"):
        # 「答不上参保时间」分支：只把**他答得上来**的项列进待问清单
        # （参保年龄已确认问不到，别再问第二遍）
        if (action == "ask_gender_age_retry" and state.get("retry_kind") == "unknown"
                and state.get("ga_still_ask") is not None):
            ga_ask = state.get("ga_still_ask") or []
        else:
            ga_ask = state.get("ga_ask") or _ga_ask_items(state)
        _known_full = state.get("ga_known") or _ga_known_text(state)
        # 「已知」是给模型看的内部参考（这些别再问）；
        # 但**本轮刚说过的**不要再复述回去 —— 把用户上一句的原话学一遍，
        # 观感就是「怎么又重复问了」。故给兜底文案的 recap 在本轮有新信息时留空。
        _recap = "" if state.get("ga_progress") else _known_full
        facts = {
            "missing": "、".join(ga_ask) or "无",
            "known": _known_full,
            "recap": _recap,
            # 还能问的项 → 拼一句现成问句给兜底文案，直接照说也不生硬
            "ask_direct": _ask_sentence(ga_ask),
            # 还能问的项 → 顺带给兜底文案一句可直接照说的话
            "ask_tail": "另外，请问您是男性还是女性？" if "性别" in ga_ask else "",
            # 「本轮用户到底有没有在回应本流程」程序是准确知道的：
            # extract 节点算出 ga_progress（有新信息）/ ga_in_context（在语境里回应，
            # 如「不记得了」）。二者都不成立才是**真正的答非所问**（问天气、聊别的），
            # 这时明确告诉模型别演「已记下」，别指望模型自己判断。
            # 只对重试节点生效——首问（ask_gender_age）时 extract 还没跑过、没有这个信号。
            "note": ("" if action == "ask_gender_age"
                     or state.get("ga_progress") or state.get("ga_in_context") else
                     "注意：本轮用户说的是**与本业务无关**的内容（答非所问），"
                     "不要说「已记下」这类套话，只需温和地把话题拉回来、"
                     "继续问缺的那一项。\n"),
        }
    elif action in ("ask_retire", "ask_retire_retry"):
        # 退休支线同口径：把已收集到的出生年月 / 人员类别摘成**一句自然口语**
        # （如「您是 1965 年出生的男职工。」）连同缺项一起喂给模型。
        # 刻意不做成「字段名：值」的清单，也不写死句式让模型照抄 —— 那样最僵硬。
        _miss = state.get("retire_missing")
        if action == "ask_retire" or not _miss:
            # 首问时 check_retire 还没跑，必须在这里现算
            _, _upd = _k_check_retire(state, {})
            _miss = _upd.get("retire_missing") or []
        _miss_txt = "、".join(_miss)
        # 若还有「他能直接答」的项（人员类别），答不上来那轮也顺带问一句
        _tail = ""
        if "人员类别" in _miss_txt:
            if state.get("gender") == "female":
                _tail = "另外，您原来是 55 岁退休（女干部）还是 50 岁退休（女工人）？"
            elif state.get("gender") is None:
                _tail = "另外，请问您是男性还是女性？"
        facts = {
            "missing": _miss_txt or "无",
            "known": _retire_known_text(state),
            "ask_tail": _tail,
        }
    elif action in ("ask_unemp", "ask_unemp_retry"):
        # 失业金支线：收「缴费时长 / 离职原因 / 失业登记」三项，**一次只问还缺的第一项**
        # （已说过的不再问 —— 会话里记住的上下文照旧生效）
        _miss = state.get("unemp_missing")
        if action == "ask_unemp" or not _miss:
            _, _upd = _k_check_unemp(state, {})
            _miss = _upd.get("unemp_missing") or []
        facts = {
            "missing": "、".join(_miss) or "无",
            "known": _unemp_known_text(state),
            # 程序拼好的一句现成问句：兜底稿直接照说也不生硬
            "ask_direct": _unemp_ask_sentence(_miss),
            # 「答不上来 / 反问怎么办」会换用共用兜底稿，它结尾带 {ask_tail}；
            # 失业金这条不需要再补问，给空串免得占位符原样漏进回复
            "ask_tail": "",
        }

    # 「粤省事」指引话术要**点名他正在办的业务**（`{service}`），
    # 不许用「这项业务」这种笼统说法 —— 见 _service_name。
    # 动词（`{guide_action}`）跟着业务定：失业金「申领」、登记「办理」、记录「查询」。
    if facts is None:
        facts = {}
    facts.setdefault("service", _service_name(state))
    facts.setdefault("guide_action", _guide_action(facts.get("service") or ""))

    fallback_tpl = ov.get("fallback") if ov.get("fallback") is not None \
        else cfg["fallback_texts"].get(fb_key, "")
    sc = {
        "action": action,
        "fallback_text": _fmt(fallback_tpl, facts),
        "required_facts": [],
        "facts": facts or None,
    }
    if hint_key:
        sc["hint_key"] = hint_key
    if action == "conclusion":
        sc["branch"] = node_cfg.get("branch", "")
        rf_list = ov.get("required_facts") if ov.get("required_facts") is not None \
            else cfg["required_facts"].get(rf_key, [])
        sc["required_facts"] = [_fmt(x, facts) for x in rf_list]
    else:
        sc["required_facts"] = list(ov.get("required_facts")
                                    if ov.get("required_facts") is not None
                                    else cfg["required_facts"].get(rf_key, []))
    # 反问 2：必含事实跟着缺失项走 —— 不缺性别就不再要求回复里出现「性别」，
    # 否则 fact_regression 必失败、触发兜底追加（重复段落）
    if action in ("ask_gender_age", "ask_gender_age_retry") and not ov.get("required_facts"):
        sc["required_facts"] = ["性别"] if "性别" in ga_ask else []
    # 「答不上来 / 反问怎么办」这一轮的主体是**引导去粤省事查询或办理**：
    # 必含事实统一为「粤省事」（回复里不一定出现「性别」二字，可能说的是
    # 「男性还是女性」，硬要求会校验失败、把同一段兜底再追加一遍 = 重复话术）。
    if hint_key == "ask_unknown_guide" and not ov.get("required_facts"):
        sc["required_facts"] = ["粤省事"]
    # 失业金结论：若「还没办失业登记」正是失败项，回复里必须带上**办理指引**——
    # 可登录「粤省事」办理、具体路径稍候短信发您（与查询/办理指引同一口径）。
    if (action == "conclusion_unemp" and not ov.get("required_facts")
            and (state.get("unemp_result") or {}).get("checks", {}).get("register") is False
            and "粤省事" not in sc["required_facts"]):
        sc["required_facts"].append("粤省事")
    # 禁语（程序闸门，不靠提示词自觉）：命中即整段换 fallback_text。
    # 「粤省事指引」类话术只许说「可登录粤省事查询或办理、具体路径短信发您」，
    # 不许报出「社保费查询 / 个人参保证明」这类具体菜单。
    sc["forbidden_facts"] = list(ov.get("forbidden_facts")
                                if ov.get("forbidden_facts") is not None
                                else (cfg.get("forbidden_facts") or {}).get(fb_key, []))
    # hint override（模板，会 fmt）
    if ov.get("hint"):
        sc["hint"] = _fmt(ov["hint"], facts)

    # retry 类：attempt / reason 现取
    if action in ("ask_huji_retry",):
        sc["attempt"] = int(state.get("attempt_huji") or 0)
    elif action in ("ask_gender_age_retry",):
        sc["attempt"] = int(state.get("attempt_gender_age") or 0)
        sc["reason"] = state.get("retry_reason") or "信息不够明确，请重问"
        sc["missing"] = "、".join(ga_ask) or "无"
    elif action in ("ask_retire_retry",):
        sc["attempt"] = int(state.get("attempt_retire") or 0)
        sc["missing"] = facts.get("missing") or "无"
        sc["known"] = facts.get("known") or ""
    elif action == "ask_retire":
        # 首问也要只问缺的：性别已知（男）就不该再提「女干部还是女工人」
        sc["gender"] = state.get("gender") or ""
        sc["missing"] = facts.get("missing") or "无"
        sc["known"] = facts.get("known") or ""
    elif action == "ask_unemp_retry":
        sc["attempt"] = int(state.get("attempt_unemp") or 0)
        sc["missing"] = facts.get("missing") or "无"
        sc["known"] = facts.get("known") or ""
    elif action == "ask_unemp":
        sc["missing"] = facts.get("missing") or "无"
        sc["known"] = facts.get("known") or ""
    return sc


# 「当前在问什么」——抽取器是单轮的、不带对话历史，必须显式告诉它本轮处在哪个话题里，
# 否则同一句原话会被按错误语境读。实测踩过：问「第一次在广东省参加养老保险时几岁」，
# 用户答「哦，是广东省啊，看错了，是16年」，抽取器把「广东省」当成**户籍**纠正写进
# corrections → 户籍从湖北被改成广东、流程跳回户籍支线，反过来问「您是广州户籍吗」。
# 话题按 kind（本次抽哪类字段 = 在问哪类问题）取：kind 就是抽取器本次干的事，最准。
# 新增一条支线时，往这张表里补一行即可（不必再往 system 提示词里塞话题硬规则）。
ASK_TOPIC = {
    "huji": "户籍所在的省、市（问的是他户口在哪，不是他在哪参保）",
    "gender_age": ("性别，以及他第一次在广东省参加养老保险时的年龄。"
                   "本话题里出现的「广东 / 广东省」一律指**参保地**"
                   "（本工具的参保地固定就是广东），**不是户籍**；"
                   "只有他明确提到「户口 / 户籍 / 籍贯」要更正时，才算户籍改口。"
                   "但注意：「广东省内（参保）16 年」说的是**年限**（gd_years=16），"
                   "**不是**参保年份 2016 ——「16 年」是年数不是年份。"),
    "retire": ("退休时间测算要用的信息：出生年份、出生月份、"
               "人员类别（职工 / 灵活就业 / 女干部 / 女工人等）"),
    "unemp": "失业保险金测算要用的信息：累计缴费月数、离职情形、是否已办理失业登记",
}


# 面板 / 调试口用的**短话题**：同一个话题只对它本身命名，不夹带提示词正文。
# ⚠️ 公子宇 2026-09-14：`ASK_TOPIC` 是**写给模型看的内部提示**（含「不是户籍」这类
# 判断规则、markdown 粗体），**绝不能原样透到界面上**。对外只显示短版。
ASK_TOPIC_SHORT = {
    "huji": "户籍所在省、市",
    "gender_age": "性别 / 首次在广东参保的年龄",
    "retire": "出生年月 / 人员类别",
    "unemp": "缴费月数 / 离职情形 / 失业登记",
}


def _ask_topic(kind: str) -> str:
    """本次抽取对应的「当前话题」文案 —— **给模型看的长版**（未登记返回空串）。"""
    return ASK_TOPIC.get(kind or "", "")


def _ask_topic_short(kind: str) -> str:
    """本次抽取对应的「当前话题」—— **给界面看的短版**。"""
    return ASK_TOPIC_SHORT.get(kind or "", "")


# stage → 话题键：stage 记录的是「现在在等用户答哪一类问题」，也就是本轮抽取的语境。
_STAGE_TOPIC_KEY = {
    "await_huji": "huji",
    "await_gender_age": "gender_age",
    "await_retire": "retire",
    "await_unemp": "unemp",
}


def _stage_topic(state: dict) -> str:
    """按 stage 取「此刻在等什么」的**短话题**（未处在某个 await 上则空串）。

    只认 stage：不在任何 await 上（idle / done / end_*）就没有「当前在问」这一说，
    返回空串 —— 免得把上一轮抽取时写下的旧话题残留在面板上。
    """
    return ASK_TOPIC_SHORT.get(_STAGE_TOPIC_KEY.get(state.get("stage") or "", ""), "")


def _run_extract(node_cfg: dict, state: dict):
    """extract 节点：LLM 槽位抽取，直接改写 state 字段。"""
    kind = node_cfg.get("kind", "huji")
    user_text = state.get("user_input") or ""
    # 话题记录：本轮在抽哪类字段 = 当前在问什么。
    # 随原话交给模型的是**长版**（含判断规则，消除语义误读）；
    # 写回 state 的是**短版**（可观测/可断言，且不会把内部提示透到界面）。
    topic = _ask_topic(kind)
    state["ask_topic"] = _ask_topic_short(kind)
    try:
        if kind == "gender_age":
            msgs = prompts.extract_gender_age_messages(user_text, topic=topic)
            out = parse_json(get_llm().chat(msgs))
            # 程序复核（不指望模型）：「广东省内 / 省内参保 N 年」是**年限**，不是参保年份。
            # 话题写着「首次在广东省参保的年龄」时，抽取器会被引着把它读成
            # first_insured_year=2016（真机实测命中），一写下去还会连锁重算出生年份
            # （2016−29=1987），越改越远。程序认得出年限就按 gd_years 写、并撤掉误读的年份。
            _gy = _gd_years_from_text(user_text)
            if _gy is not None:
                out["first_insured_year"] = None
                out["gd_years"] = _gy
                _c = out.get("corrections")
                if isinstance(_c, dict):
                    _c.pop("first_insured_year", None)
                    _c["gd_years"] = _gy
            # 记录抽取前的槽位快照：用于判断「本轮用户是否提供了新信息」。
            # 配额（attempt_gender_age）只应对「答非所问」消耗，不该把
            # 「提供了新信息的正常补答」也算成一次失败重试（否则会提前兜底）。
            _before = (state.get("gender"), state.get("first_ins_age"),
                       state.get("gd_years"), state.get("birth_year"),
                       state.get("first_insured_year"))
            # 合并语义：用户常分多轮补信息，抽取为 null 的字段保留旧值，不覆盖
            g = out.get("gender")
            a = out.get("first_ins_age")
            y = out.get("gd_years")
            by = out.get("birth_year")
            iy = out.get("first_insured_year")
            if g:
                state["gender"] = g
            if a is not None:
                state["first_ins_age"] = a
            if y is not None:
                state["gd_years"] = y
            if by is not None:
                state["birth_year"] = by
                # 用户自己报了出生年份 → 之前推算来的标记作废（只看本轮抽取结果，
                # 不看 state 里的旧值，否则「参保年份改口」那轮会把推算标记误清）
                state["birth_year_calc"] = False
            if iy is not None:
                state["first_insured_year"] = iy
            # 用户改口纠正（如「不好意思，我说错了，我户口在湖南」）：
            # 纠正=覆盖写回（值没变的不算），并记录来源支线，
            # 由 fix_route 带新值回到对应支线重跑 —— 这是「返回去」的入口。
            state["extract_src"] = "ga"
            _collect_corrections(state, out.get("corrections"), user_text)
            # 改口后**派生字段必须重算**：注意改口值的写回主要走上面的「合并语义」直写，
            # 所以不能只看 corrections 里变了什么 —— 直接比对本轮前后的快照，
            # 谁变了就把派生那个重算（否则出生年份/参保年龄会留着改口前的旧值）。
            _changed = {n for n, b, f in zip(
                ("gender", "first_ins_age", "gd_years", "birth_year", "first_insured_year"),
                _before,
                (state.get("gender"), state.get("first_ins_age"), state.get("gd_years"),
                 state.get("birth_year"), state.get("first_insured_year")))
                if b != f}
            _recompute_derived(state, sorted(_changed))
            a = state.get("first_ins_age")
            y = state.get("gd_years")
            by = state.get("birth_year")
            iy = state.get("first_insured_year")
            # 年份可跨轮补齐（如这轮说「是15年的时候」、下轮说「我是95年的」），
            # 合并后若已有出生年份+参保年份仍缺周岁 → 程序兜底换算
            if (state.get("first_ins_age") is None
                    and state.get("birth_year") and state.get("first_insured_year")):
                calc = int(state["first_insured_year"]) - int(state["birth_year"])
                if 14 <= calc <= 70:
                    state["first_ins_age"] = calc
            # 反向推算：已知「首次参保年份 + 首次参保年龄」→ 出生年份一并记下，
            # 后面（退休支线）就不用再问一遍了。
            # 例：「16年参保的，那时候25岁」→ 出生年份 = 2016 - 25 = 1991。
            # 用户自己报了出生年份时以用户为准（上面的合并里已把推算标记清掉）。
            if (state.get("birth_year") is None
                    and state.get("first_insured_year") is not None
                    and state.get("first_ins_age") is not None):
                _b = int(state["first_insured_year"]) - int(state["first_ins_age"])
                if 1930 <= _b <= 2015:
                    state["birth_year"] = _b
                    state["birth_year_calc"] = True
            # 用户顺着「首次参保年龄」的追问报年龄（「23岁」「女，25」「女的，25」），
            # 模型常保守判成「当前年龄」（first_ins_age=null、ambiguous=true）。
            # 此时按 current_age 兜底采纳。
            #
            # ⚠️ 兜底**不能**以 attempt_gender_age >= 1 为门槛：该计数只被「答非所问」
            # 消耗，而正常回答的轮次会被 _k_attempt_left 显式归零
            # （见本文件 `return "retry", {field: 0}`），门槛永远够不着 ——
            # 兜底形同不存在，同一句话被无限重复追问。
            # 真机必现路径：户籍答「江西人」→ 再答「女，25」→ 反复问同一句。
            #
            # 改为：本轮 stage 正处在 await_gender_age（即系统刚问的就是这一项，
            # 用户是在回应它）即采纳；唯一例外是原话把年龄明确绑到「现在 / 今年」
            # （那描述的确实是当前年龄，该继续问首次参保那年的周岁）。
            _amb = bool(out.get("is_current_age_ambiguous"))
            _cur = out.get("current_age")
            _explicit_current = bool(
                re.search(r"现在|今年|目前|当前|眼下|如今", user_text))
            _asking_this = state.get("stage") == "await_gender_age"
            if (a is None and _amb and _cur is not None
                    and not _explicit_current
                    and (int(state.get("attempt_gender_age") or 0) >= 1
                         or _asking_this)):
                try:
                    _ca = int(_cur)
                except (TypeError, ValueError):
                    _ca = None
                if _ca is not None and 14 <= _ca <= 70:
                    state["first_ins_age"] = _ca
                    a = _ca
                    _amb = False
            state["age_ambiguous"] = _amb and a is None
            state["age_unknown"] = bool(out.get("first_ins_age_unknown")) and a is None
            state["years_unknown"] = bool(out.get("gd_years_unknown")) and y is None
            # 本轮是否在回应本流程的提问（模型语义判断）：
            # 「不记得了」「不清楚」这类**答不上来但在语境里**的回应也算 true，
            # 只有纯粹闲聊（问天气/问时间/聊别的）才是 false。
            # 缺省按 false（保守，行为回退到改造前），避免模型没给字段时误宽纵。
            state["ga_in_context"] = bool(out.get("in_context"))
            # 本轮是否补进了新的槽位值（重复说同样的内容不算）
            _after = (state.get("gender"), state.get("first_ins_age"),
                      state.get("gd_years"), state.get("birth_year"),
                      state.get("first_insured_year"))
            state["ga_progress"] = _after != _before
        elif kind == "retire":
            msgs = prompts.extract_retire_messages(user_text, topic=topic)
            out = parse_json(get_llm().chat(msgs))
            # 同性别年龄支线：记录抽取前快照，用于「本轮是否有新信息」判定
            _rbefore = (state.get("retire_birth_year"), state.get("retire_birth_month"),
                        state.get("retire_category"))
            # 合并语义：分多轮补信息时，本轮没提到的保留旧值
            by = out.get("birth_year")
            bm = out.get("birth_month")
            cat = out.get("category")
            gd = out.get("gender")
            # 用户在**别的支线**上顺口说「我第一次参保是 2010 年」时，
            # 抽取器会把年份当成出生年份；不是退休支线在问出生年月，就不采纳。
            if by is not None and (state.get("stage") == "await_retire"
                                   or not _insured_year_misread(user_text)):
                state["retire_birth_year"] = int(by)
                state["birth_year_calc"] = False   # 用户自己报了年份 → 推算标记作废
            if bm is not None:
                state["retire_birth_month"] = int(bm)
            if cat:
                state["retire_category"] = cat
            # 性别是全局字段：退休支线里用户说了「女」也要落位，
            # 否则后面还会把「男职工」列进待确认项
            if gd in ("male", "female"):
                state["gender"] = gd
                # 已知男性 → 类别即男职工；已知女性但没说干部/工人 → 保留 null 继续问
                if gd == "male" and not state.get("retire_category"):
                    state["retire_category"] = "male"
            # 程序兜底：用户直接回「女工人」「女干部」这类原话时，
            # 模型偶尔会漏抽 category，这里用原话再判一次 —— 已经答过的项
            # 绝不能再问一遍（「女工人」= 原50岁退休女职工）。
            if not normalize_category(state.get("retire_category")):
                _fallback_cat = category_from_text(user_text, state.get("gender"))
                if _fallback_cat:
                    state["retire_category"] = _fallback_cat
            _rafter = (state.get("retire_birth_year"), state.get("retire_birth_month"),
                       state.get("retire_category"))
            state["retire_progress"] = _rafter != _rbefore
            state["retire_in_context"] = bool(out.get("in_context"))
            # 答不上出生年月/人员类别，或反问「这怎么查、去哪办」→ 追问时改用粤省事指引
            state["retire_unknown"] = (bool(out.get("unknown"))
                                       and not state["retire_progress"]
                                       and bool(state.get("retire_asked")))
            # 用户改口纠正（如「出生年份我说错了，是1980年」）：覆盖写回 + 回退重算
            state["extract_src"] = "retire"
            _collect_corrections(state, out.get("corrections"), user_text)
        elif kind == "unemp":
            msgs = prompts.extract_unemp_messages(user_text, topic=topic)
            out = parse_json(get_llm().chat(msgs))
            _ubefore = (state.get("unemp_paid_months"), state.get("unemp_reason"),
                        state.get("unemp_registered"))
            # 合并语义：本轮没提到就保留旧值
            pm = out.get("paid_months")
            if pm is None:
                # 程序兜底：模型漏抽时用原话再解析一遍（「3年」「18个月」…）
                from .unemp_calc import paid_months_from_text
                pm = paid_months_from_text(user_text)
            if pm is not None:
                try:
                    state["unemp_paid_months"] = int(pm)
                except (TypeError, ValueError):
                    pass
            # 条件②离职原因：**模型按语义直接判「情形码」**（识别走模型），
            # 程序只做白名单校验；模型给不出码时才用原话词表兜底。
            # （2026-09-14 公子宇纠正：旧做法让模型「只许照抄原话、不许归类」，
            #  全靠正则硬匹配 —— 「被工厂裁了」这种夹了字的说法就漏了，
            #  于是同一件事被反复问。识别本来就是模型该干的活。）
            from .unemp_calc import (REASON_CODES, reason_from_text,
                                     registered_from_text)
            rc = out.get("reason_code")
            if rc not in REASON_CODES:
                rc = None
            if rc is None:
                rt = out.get("reason_text")
                rc = reason_from_text(" ".join([x for x in (rt, user_text) if x]) or user_text)
            if rc:
                state["unemp_reason"] = rc
            # 条件③是否已办理失业登记并有求职要求：看不出就不写（回头问一句，不蒙）
            reg = out.get("registered")
            if reg is None:
                reg = registered_from_text(user_text)
            if reg is not None:
                state["unemp_registered"] = bool(reg)
            _uafter = (state.get("unemp_paid_months"), state.get("unemp_reason"),
                       state.get("unemp_registered"))
            state["unemp_progress"] = _uafter != _ubefore
            state["unemp_in_context"] = bool(out.get("in_context"))
            # 答不上累计缴费时长 / 离职原因，或反问「这怎么查、去哪办」→ 追问时改用粤省事指引
            state["unemp_unknown"] = (bool(out.get("unknown"))
                                      and not state["unemp_progress"]
                                      and bool(state.get("unemp_asked")))
            state["extract_src"] = "unemp"
            _collect_corrections(state, out.get("corrections"), user_text)
        else:
            msgs = prompts.extract_huji_messages(user_text, topic=topic)
            out = parse_json(get_llm().chat(msgs))
            _hbefore = (state.get("province"), state.get("city"))
            # 合并语义：没提到的保留旧值（同一会话不重置就要记住上下文）
            if out.get("province"):
                state["province"] = out.get("province")
            if out.get("city"):
                state["city"] = out.get("city")
            _hafter = (state.get("province"), state.get("city"))
            state["huji_progress"] = _hafter != _hbefore
            state["huji_in_context"] = bool(out.get("in_context"))
            # 答不上户籍 / 反问「这怎么查、去哪办」→ 追问时改用粤省事指引话术。
            # 只在「已经就户籍问过他」（huji_asked）且本轮没给新信息时才认，
            # 免得首轮正常提问被误判成答不上来。
            state["huji_unknown"] = (bool(out.get("unknown"))
                                     and not state["huji_progress"]
                                     and bool(state.get("huji_asked")))
            # 用户改口纠正户籍：覆盖写回 + 由 fix_route 回户籍链重判
            state["extract_src"] = "huji"
            _collect_corrections(state, out.get("corrections"), user_text)
    except Exception:
        # 抽取失败：保留已收集的上下文，绝不把旧值清空
        # （同一会话不重置就要记住上下文；缺失项靠 None 自然触发重问）
        pass


def _run_tool(node_cfg: dict, state: dict):
    """tool 节点：调用外部程序。

    当前内置「退休时间计算程序」（kind=calc_retire）——程序输出写入 state['retire_result']，
    由后续 say 节点取 facts 播报给用户。
    """
    kind = (node_cfg or {}).get("kind", "")
    if kind == "calc_retire":
        from .retire_calc import calc_retirement
        try:
            state["retire_result"] = calc_retirement(
                state.get("retire_birth_year"),
                state.get("retire_birth_month"),
                state.get("retire_category"),
            )
        except Exception:
            state["retire_result"] = None
    elif kind == "calc_unemp":
        # 先判「能不能领」（条件①②③），够条件再算月数金额；条件④材料齐全由经办环节核验
        from .unemp_calc import calc_unemp
        try:
            min_wage = int(config.get_config()["business"].get("unemp_min_wage", 2680))
            state["unemp_result"] = calc_unemp(
                state.get("unemp_paid_months"),
                state.get("unemp_reason"),
                state.get("unemp_registered"),
                min_wage=min_wage)
        except Exception:
            state["unemp_result"] = None


def _apply_say_outcome(state: dict, scaffold: dict, flow: dict, node_id: str):
    """say 执行后的 stage/at 落位。"""
    action = scaffold.get("action", "")
    # 「这条支线已经问过一轮」标记：之后缺项就用重试口吻，不再重复首问问法。
    # 放在这里统一置位，三条支线（户籍 / 性别年龄 / 退休）口径一致。
    _asked = {"ask_huji": "huji_asked",
              "ask_gender_age": "ga_asked",
              "ask_retire": "retire_asked",
              "ask_unemp": "unemp_asked"}.get(action)
    if _asked:
        state[_asked] = True
    if action in TERMINAL_ACTIONS:
        state["stage"] = "done"
        state["at"] = None
        return
    node_map = get_node_map(flow)
    node = node_map.get(node_id) or {}
    stage_after = node.get("config", {}).get("stage_after")
    if not stage_after:
        from .flow_default import DEFAULT_STAGE_AFTER
        stage_after = DEFAULT_STAGE_AFTER.get(action, "await_huji")
    state["stage"] = stage_after
    state["at"] = next_by_port(flow, node_id, "")  # 等用户回答后从该节点继续


def run_flow(state: dict, user_input: str) -> dict:
    """按 flow 拓扑执行一轮，返回更新后的 state。

    与 v5 相同：每轮至多对外说一句 —— 遇到 say 节点生成 _stream_scaffold 即返回，
    由 server 层调流式模型生成回复。
    """
    from .state import new_state
    merged = dict(new_state())
    merged.update({k: v for k, v in (state or {}).items() if v is not None})
    merged["user_input"] = user_input
    merged["_stream_scaffold"] = None
    # 「改口纠正」信号只在当轮 extract → fix_route 之间有效，每轮开头清空，
    # 否则上一轮的纠正会把这一轮又退回支线重跑一遍。
    merged["corrected"] = []
    merged["extract_src"] = None

    flow = get_flow()
    node_map = get_node_map(flow)

    # 起点：有 at 则从 at 继续；否则从 start 开始
    cursor = merged.get("at") or "start"
    step = 0
    scaffold = None

    while cursor and step < MAX_STEP:
        step += 1
        node = node_map.get(cursor)
        if node is None:
            # 未知节点（配置被删）→ 兜底结束
            cursor = None
            break
        ntype = node.get("type", "tool")
        ncfg = node.get("config") or {}

        if ntype == "start":
            cursor = next_by_port(flow, cursor, "")
            continue

        if ntype == "end":
            merged["stage"] = "done"
            merged["at"] = None
            cursor = None
            continue

        if ntype == "say":
            scaffold = _build_say_scaffold(ncfg, merged)
            merged["_stream_scaffold"] = scaffold
            _apply_say_outcome(merged, scaffold, flow, node["id"])
            break

        if ntype == "extract":
            _run_extract(ncfg, merged)
            cursor = next_by_port(flow, cursor, "")
            continue

        if ntype in ("condition", "code"):
            fn = KIND_REGISTRY.get(ncfg.get("kind", ""))
            if not fn:
                cursor = None
                break
            try:
                label, update = fn(merged, ncfg)
            except Exception:
                label, update = None, {}
            if update:
                merged.update(update)
            if not label:
                cursor = None
                break
            nxt = next_by_port(flow, cursor, label)
            if not nxt:
                # 出口未连线：程序兜底 —— 转 fallback
                cursor = None
                merged["stage"] = "done"
                merged["_stream_scaffold"] = None
                from .state import SCAFFOLD_FALLBACK
                scaffold = SCAFFOLD_FALLBACK()
                merged["_stream_scaffold"] = scaffold
                merged["at"] = None
                break
            cursor = nxt
            continue

        if ntype == "tool":
            _run_tool(ncfg, merged)
            cursor = next_by_port(flow, cursor, "")
            continue

        # 未知类型：兜底
        cursor = None
        break

    # 未产生 scaffold（图被截断/纯 end）→ 兜底 fallback，避免空回复
    if not merged.get("_stream_scaffold"):
        from .state import SCAFFOLD_FALLBACK
        merged["_stream_scaffold"] = SCAFFOLD_FALLBACK()
        merged["stage"] = "done"
        merged["at"] = None
    # 状态面板的「当前话题」按**最终 stage** 定（= 我们现在在等他答什么），
    # 而不是停留在「本轮抽取时用的话题」—— 后者在换支线的那一轮会滞后一步
    # （例：刚问完性别/参保年龄，面板还写着「户籍」）。
    # ⚠️ 只认 stage：结论已出（done）就没在等任何字段，此时必须**清空**，
    # 否则面板会把上一轮的旧话题一直挂着（公子宇 2026-09-14 截图反馈）。
    merged["ask_topic"] = _stage_topic(merged)
    return merged


def validate_flow(flow: dict) -> list:
    """浅校验：节点 id 唯一、边引用的节点存在、存在 start。返回错误列表。"""
    errs = []
    if not isinstance(flow, dict):
        return ["flow 不是对象"]
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ids = [n.get("id") for n in nodes]
    if len(set(ids)) != len(ids):
        errs.append("存在重复节点 id")
    if not any(n.get("type") == "start" for n in nodes):
        errs.append("缺少开始节点")
    id_set = set(ids)
    for e in edges:
        if e.get("from") not in id_set:
            errs.append(f"边起点不存在: {e.get('from')}")
        if e.get("to") not in id_set:
            errs.append(f"边终点不存在: {e.get('to')}")
    return errs
