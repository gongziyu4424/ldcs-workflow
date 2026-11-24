"""LLM 客户端抽象（v4 模型问答版）。

- MockLLM：离线跑通 / 单测用。抽取走规则实现；面向用户生成时回退 scaffold.fallback_text。
- OpenAICompatLLM：DeepSeek/百炼等 OpenAI 兼容 API，生产用。

环境变量：
  LLM_MODE=mock|openai       默认 mock
  LLM_BASE_URL / LLM_API_KEY / LLM_MODEL   openai 模式必填
  LDCS_SKIP_DOTENV=1         跳过读取 .env（受限沙箱/CI 里跑离线用例时用）
"""
import json
import os
import re
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

from . import config, prompts

# 允许在没有 .env 读取权限的环境（受限沙箱 / CI）里离线跑用例：
#   LDCS_SKIP_DOTENV=1 python -m ldcs.test_cases
# 生产/日常本地开发不设该变量，行为与原来完全一致。
if os.environ.get("LDCS_SKIP_DOTENV") != "1":
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


# ============================================================
# JSON & 规则抽取
# ============================================================
def parse_json(text: str) -> dict:
    """从 LLM 输出中稳健地取出第一段 JSON。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"LLM 输出中没有 JSON: {text[:200]}")
    return json.loads(m.group(0))


# ---- 「用户答不上来 / 反过来问怎么办」的统一线索（Mock 兜底；生产由模型输出 unknown 字段）----
# 两类都该给「可以登录粤省事查询或办理，具体查询或办理路径稍候短信发给您」的指引：
#   ① 答不上被问的那一项（不记得 / 不知道 / 不清楚 …）
#   ② 反过来问「这个怎么查、在哪办、怎么申请」
_UNKNOWN_RE = re.compile(
    r"不知道|不清楚|不记得|记不清|记不得|忘了|忘记|没印象|说不上来|说不清|查不到|"
    r"想不起来|想不起|没记住|没留意|不确定|不了解|搞不清|弄不明白|"
    r"怎么查|在哪查|哪里查|上哪查|怎么申请|如何申请|咋申请|怎么办理|如何办理|"
    r"在哪办|哪里办|怎么办|咋办|如何申领|怎么申领|怎么领|如何领|"
    r"怎么操作|如何操作|怎么弄|如何弄"
)


def _unknown_hit(text: str) -> bool:
    """用户本轮是否「答不上来 / 反问怎么查怎么办」——据此改用粤省事指引话术。"""
    return bool(_UNKNOWN_RE.search(text or ""))


def _extract_huji_rule(text: str) -> dict:
    """规则式户籍抽取（Mock 用）。生产走 LLM 节点。"""
    t = (text or "").strip()
    unk = _unknown_hit(t)
    if not t:
        return {"province": None, "city": None, "unknown": False,
                "raw": text, "reason": "空输入"}
    if "广州" in t:
        return {"province": "guangdong", "city": "guangzhou", "unknown": unk,
                "raw": t, "reason": None}
    gd_cities = {
        "肇庆": "zhaoqing", "佛山": "foshan", "深圳": "shenzhen", "东莞": "dongguan",
        "韶关": "shaoguan", "珠海": "zhuhai", "惠州": "huizhou", "中山": "zhongshan",
        "江门": "jiangmen", "汕头": "shantou", "湛江": "zhanjiang",
        "清远": "qingyuan", "梅州": "meizhou", "汕尾": "shanwei", "河源": "heyuan",
        "阳江": "yangjiang", "茂名": "maoming", "潮州": "chaozhou", "揭阳": "jieyang",
        "云浮": "yunfu",
        # 地域俗称（只关心是否广州 / 是否省内，俗称足以判定为「省内非广州」）
        "潮汕": "chaoshan", "粤东": "yuedong", "粤西": "yuexi",
        "粤北": "yuebei", "客家": "kejia",
    }
    for name, py in gd_cities.items():
        if name in t:
            return {"province": "guangdong", "city": py, "unknown": unk,
                    "raw": t, "reason": None}
    if "广东" in t:
        return {"province": "guangdong", "city": None, "unknown": unk, "raw": t,
                "reason": "广东但未说明具体地市"}
    other_prov = {
        "江西九江": ("jiangxi", "jiujiang"), "九江": ("jiangxi", "jiujiang"),
        "江西": ("jiangxi", None), "湖南长沙": ("hunan", "changsha"), "长沙": ("hunan", "changsha"),
        "湖南": ("hunan", None), "广西": ("guangxi", None), "湖北": ("hubei", None),
        "四川": ("sichuan", None), "上海": ("shanghai", None), "北京": ("beijing", None),
        "重庆": ("chongqing", None), "福建": ("fujian", None), "安徽": ("anhui", None),
        "浙江杭州": ("zhejiang", "hangzhou"), "杭州": ("zhejiang", "hangzhou"),
        "浙江": ("zhejiang", None), "江苏": ("jiangsu", None),
        # 直辖市候补（避免被前缀误吞）
        "海南": ("hainan", None), "贵州": ("guizhou", None), "云南": ("yunnan", None),
        "陕西": ("shaanxi", None), "甘肃": ("gansu", None), "青海": ("qinghai", None),
        "宁夏": ("ningxia", None), "新疆": ("xinjiang", None), "西藏": ("xizang", None),
        "内蒙古": ("innermongolia", None), "黑龙江": ("heilongjiang", None),
        "吉林": ("jilin", None), "辽宁": ("liaoning", None), "河北": ("hebei", None),
        "山西": ("shanxi", None), "河南": ("henan", None), "山东": ("shandong", None),
        "天津": ("tianjin", None),
    }
    for key, (prov, city) in other_prov.items():
        if key in t:
            return {"province": prov, "city": city, "unknown": unk, "raw": t, "reason": None}
    return {"province": None, "city": None, "unknown": unk, "raw": t,
            "reason": "无法识别户籍地"}


def _extract_gender_age_rule(text: str) -> dict:
    """规则式性别/年龄抽取（Mock 用）。生产走 LLM 节点。

    原则：放宽识别，不那么严。
    - gender：含"男"→male，含"女"→female。
    - first_ins_age：从文本中匹配年龄数字（周岁），合理范围 14-70。
      带「岁」字的直接取；没带「岁」字的口语写法（「女，25」「女的，25」「女 25」）
      取紧跟在性别词后面的那个数字。
    - is_current_age_ambiguous：仅当用户原话明确说"现在/今年/目前/当前"且**只**给出年龄时标 true。
      含"参保/首次/那会儿/当时/以前"等语境词 → 直接采纳数字，不标 ambiguous。
    """
    t = (text or "").strip()
    gender = "male" if re.search(r"男", t) else ("female" if re.search(r"女", t) else None)

    m = re.search(r"(\d{1,2})\s*岁", t)
    if not m:
        # 没带「岁」字的口语写法：「女，25」「女的，25」「女 25」。
        # 只认紧跟性别词（≤3 个非数字字符）的数字，且后面不接数字或「年」——
        # 否则「女的，我交了 20 年」的 20、「男的 58 年生的」的 58 会被误当年龄。
        m = re.search(r"(?:男|女)(?:的|人|士)?[^0-9]{0,3}(\d{1,2})(?!\d)(?!年)", t)
    age = int(m.group(1)) if m else None
    if age is not None and not (14 <= age <= 70):
        age = None  # 超出合理范围 → 当作未提供
    current_age = age  # 用户报的年龄数字（歧义时也保留，供程序在追问过后兜底采纳）

    # 当前年龄歧义：仅当用户原话有显式"现在/今年/目前/当前"且没有提到"参保/那会儿/当时/以前/首次"时
    has_current_hint = re.search(r"现在|今年|目前|当前|现在有|今年有", t)
    has_insurance_hint = re.search(r"参保|那会儿|当时|以前|首次|第一次|当年", t)
    ambiguous = bool(has_current_hint) and not has_insurance_hint and age is not None

    if has_current_hint and not has_insurance_hint:
        # 嫌疑场景：把当前年龄报上来了
        age = None  # 先不放行，等程序追问
    # 孤立年龄数字（无性别词、无参保语境、无四位年份）→ 意图不明，
    # 与真模型保持一致判为「当前年龄歧义」：数字只留在 current_age，由程序在
    # 已就此项追问过之后兜底采纳（否则 mock 会"过"而真机不过，白测一轮）
    if (age is not None and gender is None and not has_insurance_hint
            and not has_current_hint and not re.search(r"\d{4}", t)):
        ambiguous = True
        age = None
    # 用户明确表示不记得 / 不知道首次参保时间 → 需要给「粤省事查询」指引
    # （口径与真模型一致：「不清楚」「想不起来了」这类答不上来的回应同样算数，
    #   都属"在语境里回应"，绝不能当成答非所问）
    unk_kw = _unknown_hit(t)
    years_unknown = bool(unk_kw and re.search(r"几年|多少年|多久|年限|参保年数", t))
    first_unknown = bool(unk_kw and not years_unknown)

    # ---- 出生年份 / 首次参保年份 → 自行换算首次参保年龄 ----
    all_years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", t)]
    birth_year = insured_year = None
    if all_years:
        if re.search(r"出生|我是\s*(\d{4})\s*年的", t):
            # 显式/半显式：两个年份时，小=出生年，大=参保年
            if len(all_years) >= 2:
                birth_year, insured_year = min(all_years), max(all_years)
            else:
                birth_year = all_years[0]
        elif re.search(r"参保|第一次参加|首次参加|查到了", t):
            insured_year = all_years[0]
        elif len(all_years) >= 2:
            birth_year, insured_year = min(all_years), max(all_years)

    # 两位数年份：「查到了，是15年的时候」→参保2015；「我是95年的」→出生1995
    yy_spans = []  # 已按年份解释的片段位置，算参保年数前要屏蔽
    if insured_year is None:
        m_t = re.search(r"(?:查到了|参保|第一次|首次).{0,8}?(?:是\s*)?(\d{2})\s*年的?时候", t)
        if m_t:
            insured_year = 2000 + int(m_t.group(1))
            yy_spans.append(m_t.span(1))
    # 「16年参保的」「15年第一次参保」→ 参保年份 2016 / 2015（两位数在前）
    if insured_year is None:
        m_i = re.search(
            r"(\d{2})\s*年(?:的时候)?\s*(?:就|开始|第一次|首次)?\s*(?:参保|参加|交|缴)", t)
        if m_i:
            insured_year = 2000 + int(m_i.group(1))
            yy_spans.append(m_i.span(1))
    if birth_year is None:
        m_b = (re.search(r"我是\s*(\d{2})\s*年的", t)
               or re.search(r"(\d{2})\s*年出生", t))
        if m_b:
            yy = int(m_b.group(1))
            birth_year = (1900 + yy) if yy >= 30 else (2000 + yy)
            yy_spans.append(m_b.span(1))
    if age is None and birth_year and insured_year and insured_year > birth_year:
        calc = insured_year - birth_year
        if 14 <= calc <= 70:
            age = calc
            ambiguous = False
    need_birth = (age is None and insured_year is not None
                  and birth_year is None and not first_unknown)

    # 在广东省内累计参保年数（如「参保 8 年」「缴了12年」）——
    # 只在用户主动提到时抽取；屏蔽已被解释为年份的两位数片段，
    # 也排除「2020年」这类 4 位年份被截出「20年」（(?<!\d)）
    t_for_years = list(t)
    for s, e in yy_spans:
        t_for_years[s:e] = "□" * (e - s)
    m_year = re.search(r"(?<!\d)(\d{1,2})\s*年", "".join(t_for_years).replace("岁", "岁 "))
    gd_years = None
    if m_year:
        yv = int(m_year.group(1))
        if 0 <= yv <= 45:
            gd_years = yv

    if gender and age is not None and not ambiguous:
        reason = None
    elif need_birth:
        reason = "用户只报了参保年份，缺出生年份或年龄，请请他补充"
    elif unk_kw and first_unknown and age is None:
        reason = "用户明确表示不知道首次参保时间"
    elif ambiguous:
        reason = "用户上报的是当前年龄，不是首次参保年龄"
    elif gender is None:
        reason = "未识别到性别"
    elif age is None:
        reason = "未识别到首次参保年龄"
    else:
        reason = None

    return {
        "gender": gender,
        "birth_year": birth_year,
        "first_insured_year": insured_year,
        "first_ins_age": age,
        "current_age": current_age,
        "is_current_age_ambiguous": ambiguous,
        "first_ins_age_unknown": first_unknown and age is None,
        "gd_years": gd_years,
        "gd_years_unknown": years_unknown and gd_years is None,
        "raw": t,
        "reason": reason,
    }


def _extract_retire_rule(text: str) -> dict:
    """规则式「退休时间测算」抽取（Mock 用）。生产走 LLM 节点。

    - birth_year：4 位年份，或两位数（「我是65年的」→1965，≥30 按 19xx）。
    - birth_month：1-12；没给 → null。
    - category：male / female_cadre / female_worker；只给「女」不区分 → null。
    """
    t = (text or "").strip()
    birth_year = birth_month = None

    m4 = re.search(r"(19\d{2}|20\d{2})\s*年", t)
    if m4:
        birth_year = int(m4.group(1))
    else:
        m2 = (re.search(r"我是\s*(\d{2})\s*年", t)
              or re.search(r"(\d{2})\s*年出生", t)
              or re.search(r"(\d{2})\s*年的", t))
        if m2:
            yy = int(m2.group(1))
            birth_year = (1900 + yy) if yy >= 30 else (2000 + yy)

    mm = re.search(r"(\d{1,2})\s*月", t)
    if mm:
        mv = int(mm.group(1))
        if 1 <= mv <= 12:
            birth_month = mv

    category = None
    if re.search(r"男", t):
        category = "male"
    elif re.search(r"女干部|女管理|管理岗|技术岗|专业技术|女技术人员|干部身份|"
                   r"办公室|机关|事业单位|公务员|文员|教师|医生|会计", t):
        category = "female_cadre"
    elif re.search(r"女工人|一线工人|生产岗|工人身份|女工人岗|"
                   r"工厂|车间|流水线|操作工|普工|打工|计件", t):
        category = "female_worker"

    # 性别独立于类别：只说「女」时 gender=female 但 category=null
    gender = None
    if category == "male" or re.search(r"男", t):
        gender = "male"
    elif re.search(r"女", t):
        gender = "female"

    if category is None and re.search(r"女", t):
        reason = "女性未区分女干部（原55岁退休）/女工人（原50岁退休）"
    elif birth_year is None:
        reason = "未识别到出生年份"
    elif birth_month is None:
        reason = "未识别到出生月份"
    else:
        reason = None

    return {
        "birth_year": birth_year,
        "birth_month": birth_month,
        "gender": gender,
        "category": category,
        "unknown": _unknown_hit(t),
        "raw": t,
        "reason": reason,
    }


def _extract_unemp_rule(text: str) -> dict:
    """规则式「失业保险金」抽取（Mock 用）。生产走 LLM 节点。

    paid_months：累计缴费总月数；只认明确带时间单位的说法（3年/18个月/半年…），
    解析不出 → null（保守：交给程序追问，不蒙）。
    reason_code：**离职情形码**（真模型按语义直接判；mock 用同一张兜底词表模拟）；
    reason_text：离职原因原话（留作线索与兜底）；
    registered：是否已办理失业登记。看不出都是 None，不猜。
    """
    from .unemp_calc import paid_months_from_text, reason_from_text, registered_from_text
    t = (text or "").strip()
    pm = paid_months_from_text(t)
    reg = registered_from_text(t)
    rc = reason_from_text(t)
    missing = []
    if pm is None:
        missing.append("累计缴费时长")
    if rc is None:
        missing.append("离职原因")
    if reg is None:
        missing.append("失业登记")
    return {
        "paid_months": pm,
        "reason_code": rc,
        "reason_text": t or None,
        "registered": reg,
        "unknown": _unknown_hit(t),
        "raw": t,
        "reason": ("未识别到" + "、".join(missing)) if missing else None,
    }


# ---- 「是否在回应本流程」的规则判定（Mock 用；生产由模型按提示词判断）----
# 口径与真模型提示词一致：「不记得了」「不知道」这类答不上来但在语境里的回应
# 同样算「在语境里」；只有明显与本业务无关的闲聊才算答非所问。
_OFF_TOPIC_RE = re.compile(
    r"(天气|气温|下雨|下雪|今天几号|明天几号|几号了|几点了|星期几|今天星期|吃饭|吃了没|吃了吗|"
    r"笑话|讲个故事|股票|足球|篮球|电影|游戏|你叫什么|你是谁|"
    r"聊点别的|无聊|唱首歌)"
)


def _in_context_rule(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return not bool(_OFF_TOPIC_RE.search(t))


# ---- 「用户改口纠正」的规则判定（Mock 用；生产由模型按提示词判断）----
# 只有用户**否定 / 更正先前已给出**的信息才算，首次填空不算。
_CORRECTION_RE = re.compile(
    r"(说错|打错|讲错|弄错|记错|填错|写错|选错|更正|改成|改一下|其实(是|我)|"
    r"不(是|对)[^。！？，]{0,10}(是|，)|重新说|刚才说)"
)

# 跨支线补抽退休信息前的**话题护栏**：只有这句话确实在讲出生/退休，
# 才把它当退休字段的改口。否则「我第一次参保是 2010 年」会被
# _extract_retire_rule 的「4 位年份即出生年」抓走，把出生年份一刀改成 2010，
# 还把用户甩进他根本没提过的退休支线。
_RETIRE_TOPIC_RE = re.compile(r"(退休|出生|生日|生年|年月|人员类别|男职工|女干部|女工人)")


def _corrections_rule(text: str, base: dict, keymap: dict) -> dict:
    """mock：出现改口说法时，把本轮抽到的字段收进 corrections。

    keymap：抽取结果键名 → 纠正键名（如 retirement 的 birth_year → retire_birth_year）。
    另外跨支线补抽户籍 / 退休信息 —— 用户常在别的环节顺手更正这些字段。
    """
    if not text or not _CORRECTION_RE.search(text):
        return {}
    corr = {}
    for k, target in (keymap or {}).items():
        v = (base or {}).get(k)
        if v not in (None, False, "", 0):
            corr[target] = v
    h = _extract_huji_rule(text)
    for k in ("province", "city"):
        if h.get(k):
            corr[k] = h[k]
    if _RETIRE_TOPIC_RE.search(text):
        r = _extract_retire_rule(text)
        if r.get("birth_year"):
            corr["retire_birth_year"] = r["birth_year"]
        if r.get("birth_month"):
            corr["retire_birth_month"] = r["birth_month"]
        if r.get("category"):
            corr["retire_category"] = r["category"]
    return corr


# ============================================================
# 意图识别（模型判为主，规则兜底）
# ============================================================
INTENT_RETIRE = "retire"
INTENT_LOCATION = "location"
INTENT_UNEMP = "unemp"          # 问失业保险金（能领多少/怎么算/够不够资格）
INTENT_QUERY = "query"          # 问「怎么查自己的参保/缴费情况」→ 引导粤省事
INTENT_FOLLOWUP = "followup"
INTENT_OFF_TOPIC = "off_topic"
_VALID_INTENTS = {INTENT_RETIRE, INTENT_LOCATION, INTENT_UNEMP, INTENT_QUERY,
                  INTENT_FOLLOWUP, INTENT_OFF_TOPIC}

# 查询 / 办理指引意图的规则兜底（模型不可用时用）：
# 「怎么/在哪 + 查/看/知道」搭配「缴费/参保/年限/记录」才算，
# 避免把「我怎么才能领养老金」这类误判成查询；
# 另外「怎么申请 / 怎么办理 / 去哪办」同样归到这里 —— 一并引导「粤省事」查询或办理。
_QUERY_INTENT_RE = re.compile(
    r"(怎么|如何|怎样|咋|在哪|哪里|哪儿|什么地方).{0,12}(查|看|知道|了解|确认|核对|查询)"
    r".{0,12}(缴费|参保|社保|年限|记录|明细|缴够|缴满|交了|月数|累计)"
    r"|(查|查询|看看|看一下|打印).{0,8}(缴费记录|参保记录|参保缴费|缴费明细|参保证明|缴费年限)"
    r"|(怎么|如何|怎样|咋|在哪|哪里|哪儿|什么地方).{0,10}"
    r"(申请|办理|申领|办手续|办退休|办失业金|办养老金)"
    r"|(申请|办理|申领).{0,10}(失业金|失业保险金|退休|养老金|待遇)"
)


def query_intent_hit(text: str) -> bool:
    """是否在问「怎么查自己的参保/缴费情况」（查询指引类）。"""
    return bool(_QUERY_INTENT_RE.search(text or ""))


def _classify_intent_rule(text: str, stage_done: bool = False) -> str:
    """规则兜底（模型不可用/输出异常时）。

    延迟 import flow 里的正则，避免 llm ↔ flow 循环依赖；
    保持与历史行为一致：退休优先于领取地。
    """
    from .flow import intent_hit, retire_intent_hit, unemp_intent_hit
    if retire_intent_hit(text):
        return INTENT_RETIRE
    if unemp_intent_hit(text):
        return INTENT_UNEMP
    if intent_hit(text):
        return INTENT_LOCATION
    # 「怎么查 / 怎么办手续」永远放最后判：它是**兜底性指引**，只在用户
    # 本轮没提出任何业务诉求（退休时间 / 失业金 / 在哪领养老金）时才轮到它。
    # 「想领广州退休金怎么操作」有 location 诉求、「失业金怎么申请」有 unemp 诉求，
    # 都必须**先进流程把事算清楚**，不能因为带了「怎么」就被抢去当纯办理指引。
    if query_intent_hit(text):
        return INTENT_QUERY
    return INTENT_FOLLOWUP if stage_done else INTENT_OFF_TOPIC


def classify_intent(user_text: str, stage_done: bool = False) -> str:
    """判断用户本轮意图 —— 由模型语义判断，返回六个取值之一。

    retire（问退休时间/年龄） / location（问在哪领养老金）
    / unemp（问失业保险金）
    / query（问怎么查自己的参保缴费情况 → 引导粤省事）
    / followup（结论后的追问） / off_topic（无关）。

    模型调用失败或输出非法时回退 _classify_intent_rule，保证流程不卡死。
    """
    try:
        raw = get_llm().chat(prompts.classify_intent_messages(user_text, stage_done))
        obj = parse_json(raw)
        intent = str(obj.get("intent") or "").strip().lower()
        if intent in _VALID_INTENTS:
            # 未拿到结论时不存在「追问」语境 → 归到无关，避免误吞
            if intent == INTENT_FOLLOWUP and not stage_done:
                return INTENT_OFF_TOPIC
            return intent
    except Exception:
        pass
    return _classify_intent_rule(user_text, stage_done)


# ============================================================
# 必含事实验证（含汉字数字归一化）
# ============================================================
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUM_RE = re.compile(r"[零一二两三四五六七八九十]{1,4}")


def _cn_num_to_int(s):
    if not s:
        return None
    if "十" not in s:
        return _CN_DIGITS.get(s)
    parts = s.split("十")
    if len(parts) != 2:
        return None
    tens = _CN_DIGITS.get(parts[0], 1) if parts[0] else 1
    ones = _CN_DIGITS.get(parts[1], 0) if parts[1] else 0
    return tens * 10 + ones


def _normalize_cn_digits(text):
    def _sub(m):
        v = _cn_num_to_int(m.group(0))
        return str(v) if v is not None else m.group(0)
    return _CN_NUM_RE.sub(_sub, text or "")


def _strip_thousands(s: str) -> str:
    """去掉数字里的千分位逗号。

    模型爱把金额写成「14,472 元」，而必含事实里写的是 `14472`；
    硬比会判「事实缺失」→ 把兜底段**追加**到回复后面（用户看到同一段话说两遍）。
    """
    return re.sub(r"(?<=\d),(?=\d)", "", s or "")


def fact_regression(required_facts, candidate, facts=None):
    """校验必含事实在候选回复中是否齐全（允许汉字数字等价：五==5、五十==50）。

    进一步容差：
    - 数字与单位/关键字之间的空格（如 "5 年" vs "5年"）不影响匹配。
    - required_facts 里的 `{key}` 占位符会用传入的 facts dict 替换（如 `{threshold_num}` → `50`）。
    - synonym dict：模型用同义表达（"首次参保那年的周岁"）也算覆盖"首次参保年龄"等字面 fact。

    required_facts 可以是字符串列表，或一个 scaffold dict（含 'required_facts' 与 'facts' keys）。
    返回 bool。
    """
    if isinstance(required_facts, dict):
        facts = facts or required_facts.get("facts") or {}
        required_facts = required_facts.get("required_facts") or []
    else:
        facts = facts or {}
    if not required_facts:
        return True
    # 把占位符 {xxx} 替换为 facts[xxx] 的字面值
    resolved = []
    for f in required_facts:
        s = f or ""
        for k, v in (facts or {}).items():
            s = s.replace("{" + k + "}", str(v))
        resolved.append(s)
    cand = (candidate or "")
    # 把"5 年""5年""五十"统一为同一可比形态：去空格 + 汉字数字归一化
    def _norm(s: str) -> str:
        return _normalize_cn_digits(_strip_thousands(s).replace(" ", "").replace("\u3000", ""))
    cand_n = _norm(cand)
    # 宽松正则要用**没做汉字数字归一化**的版本：归一化会把「第一次」写成「第1次」，
    # 反而把「25 岁那年第一次参保」这种说法漏掉。
    cand_s = _strip_thousands(cand).replace(" ", "").replace("\u3000", "")
    for fact in resolved:
        if not fact:
            continue
        if _norm(fact) in cand_n:
            continue
        # 退路：原文含空格/不归一也算
        if fact in cand or _strip_thousands(fact) in cand_s:
            continue
        # 退路：synonym 同义表达（如 "首次参保那年的周岁" 覆盖 "首次参保年龄"）
        syns = _SYNONYM_FACTS.get(fact) or []
        if any((_norm(s) in cand_n) or (s in cand) or (_strip_thousands(s) in cand_s)
               for s in syns):
            continue
        # 退路：语义项宽松正则（如「您 25 岁首次参保」也算覆盖「首次参保年龄」）
        _pat = _LOOSE_FACT_PATTERNS.get(fact)
        if _pat and re.search(_pat, cand_s):
            continue
        return False
    return True


# 同义事实字典：模型写得不严格但语义正确的情况（如"首次参保那年的周岁"代替"首次参保年龄"）。
# 加在这里即可生效，不必改 config required_facts。
_SYNONYM_FACTS: dict[str, list[str]] = {
    # 注意：本字典的 key 必须唯一！之前这里和下面的 "性别" 重复，
    # 后一条把这一条整个覆盖掉，导致「请问您是男性还是女性？」这种自然问法
    # 校验不过必含事实「性别」→ 被兜底模板接管（用户看到的就是重复/僵硬的模板稿）。
    "性别": [
        "性别", "男性", "女性", "男士", "女士", "男生", "女生",
        "男还是女", "是男还是女", "男，还是女",
        "性别是", "您的性别", "什么性别",
    ],
    # 「首次参保年龄」是**语义项**，模型会说成「您 25 岁首次参保」「首次参保时 25 岁」
    # 「25 岁那年第一次参保」…… 只认死字面会把这类**正确**回复判失败 →
    # 于是追加兜底段，用户看到的就是「同一段话说了两遍」。
    # 这里把常见说法列全；更松的口径见 _LOOSE_FACT_PATTERNS。
    "首次参保年龄": [
        "首次参保那年的周岁", "首次参保那年的年龄",
        "首次参保时的年龄", "首次参保时的周岁",
        "首次参保的年龄", "首次参保的周岁",
        "首次参保年纪", "首次参保年龄周岁",
        "首次参保时", "第一次参保时", "头一次参保时",
        "第一次在广东参保时", "第一次在广东省参保时",
        "首次参加养老保险时", "第一次参加养老保险时",
        "岁首次参保", "岁第一次参保", "岁那年首次参保",
        "首次参保年龄", "第一次参保年龄",
    ],
    "广州户籍": [
        "属于广州户籍", "属于广州", "您是广州人",
        "广州本地户口", "广州户口", "广州本地户籍",
    ],
    "广东省内": [
        "广东省内", "广东省内非广州", "广东", "广东省",
    ],
    "最后参保地": [
        "最终参保地", "末次参保地", "最后参加社保的地方",
        "最后的参保地", "最后参保", "末次参保",
    ],
    "基本养老保险": ["基本养老保险", "养老保险", "养老险"],
    "养老待遇领取地": ["养老待遇领取地", "待遇领取地", "养老领取地"],
    "周岁": ["岁", "周岁", "年龄", "岁数"],
    "广州": ["广州市", "广州", "广州城"],
    "5年": ["5 年", "5年", "5周年", "5 周年", "五年", "5个年头", "5个周年"],
    "出生年月": [
        "出生年份和月份", "出生年份与月份", "出生年份", "哪年哪月", "哪一年哪一月",
        "出生日期", "出生时间", "出生的年月", "出生年月日",
    ],
}

# 必含事实里有些是**语义项**（不是固定术语），模型会换着法儿说。给这类项留一条
# 宽松正则退路：既要求出现该语义，又要求**带上数字** —— 免得模型只说年份、不给年龄
# 也蒙混过去。命中即算覆盖，不再追加兜底段（也就不会出现「同一段话说两遍」）。
# 匹配对象是去空格 + 汉字数字归一化之后的文本。
_LOOSE_FACT_PATTERNS = {
    "首次参保年龄": (
        r"(首次|第一次|头一次)[^，。；]{0,12}(参保|参加(基本)?养老保险)[^，。；]{0,10}\d+岁"
        r"|\d+岁[^，。；]{0,8}(首次|第一次|头一次)[^，。；]{0,8}(参保|参加(基本)?养老保险)"
    ),
}


# ============================================================
# LLM 客户端
# ============================================================
class BaseLLM:
    """统一以 messages 列表为入参的 chat 与 stream_chat 接口。"""
    def chat(self, messages):
        raise NotImplementedError

    def stream_chat(self, messages):
        raise NotImplementedError


class MockLLM(BaseLLM):
    """离线 mock：抽取走规则，生成返回 fallback_text。"""

    def chat(self, messages):
        sys_msg = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        # 抽取入参里带了「【当前在问】…【用户本轮原话】…」语境前缀（见 prompts.wrap_user_text），
        # 规则抽取只该看**用户原话**，先剥掉 —— 否则话题文案会被正则当成用户说的话。
        raw_msg = prompts.strip_topic_prefix(user_msg)
        cfg = config.get_config()

        # 用具体的 system prompt 字符串比对（config 改即生效）
        if sys_msg == cfg["system"]["extract_huji_system"]:
            d = _extract_huji_rule(raw_msg)
            d["in_context"] = _in_context_rule(raw_msg)
            d["corrections"] = _corrections_rule(
                raw_msg, d, {"province": "province", "city": "city"})
            return json.dumps(d, ensure_ascii=False)
        if sys_msg == cfg["system"]["extract_gender_age_system"]:
            d = _extract_gender_age_rule(raw_msg)
            d["in_context"] = _in_context_rule(raw_msg)
            d["corrections"] = _corrections_rule(raw_msg, d, {
                "gender": "gender", "birth_year": "birth_year",
                "first_insured_year": "first_insured_year",
                "first_ins_age": "first_ins_age", "gd_years": "gd_years"})
            return json.dumps(d, ensure_ascii=False)
        if sys_msg == cfg["system"]["extract_retire_system"]:
            d = _extract_retire_rule(raw_msg)
            d["in_context"] = _in_context_rule(raw_msg)
            d["corrections"] = _corrections_rule(raw_msg, d, {
                "birth_year": "retire_birth_year",
                "birth_month": "retire_birth_month",
                "category": "retire_category"})
            return json.dumps(d, ensure_ascii=False)
        if sys_msg == cfg["system"]["extract_unemp_system"]:
            d = _extract_unemp_rule(raw_msg)
            d["in_context"] = _in_context_rule(raw_msg)
            d["corrections"] = _corrections_rule(raw_msg, d, {
                "paid_months": "unemp_paid_months",
                "reason_text": "unemp_reason",
                "registered": "unemp_registered"})
            return json.dumps(d, ensure_ascii=False)
        if sys_msg == cfg["system"]["classify_intent_system"]:
            text = raw_msg
            done = "已经拿到过一次结论" in user_msg
            return json.dumps({"intent": _classify_intent_rule(text, done),
                               "reason": "mock 规则判定"}, ensure_ascii=False)

        # 面向用户的回复生成：mock 直接返回 scaffold 的 fallback_text（保证离线断言）
        scaffold = _parse_scaffold_from_user_prompt(user_msg)
        return (scaffold.get("fallback_text") or "").strip() or "(空)"

    def stream_chat(self, messages):
        full = self.chat(messages)
        for i in range(0, len(full), 4):
            yield full[i:i + 4]


def _parse_scaffold_from_user_prompt(text):
    """从 user prompt 文本里反解回 scaffold dict（仅 mock 解析用）。"""
    m = re.search(r"本次任务 JSON：\s*(\{.*?\})\s*\n\n", text, re.S)
    if not m:
        return {}
    raw = m.group(1)
    try:
        return eval(raw)
    except Exception:
        return {}


class OpenAICompatLLM(BaseLLM):
    def __init__(self):
        self.base = os.environ["LLM_BASE_URL"].rstrip("/")
        self.key = os.environ["LLM_API_KEY"]
        self.model = os.environ.get("LLM_MODEL", "qwen3.6-plus")

    def _payload(self, messages, stream):
        return {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "stream": stream,
            "enable_thinking": False,  # 关闭深度思考
        }

    def chat(self, messages):
        body = json.dumps(self._payload(messages, stream=False)).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0].get("message") or {}
        return msg.get("content") or ""

    def stream_chat(self, messages):
        body = json.dumps(self._payload(messages, stream=True)).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                # 跳过 thinking 行（reasoning_content）
                if "content" not in delta:
                    continue
                chunk = delta.get("content")
                if chunk:
                    yield chunk


def fact_violation(forbidden_facts, candidate):
    """回复里**不许出现**的表述（如把查询路径写成「个人参保证明」「社保费查询」）。

    与 fact_regression 相反：那个管「必须说到」，这个管「绝不许说」。
    命中任一禁语返回命中的那条（方便打日志），没命中返回 None。
    比对前去掉空格/全角空格，免得「服务 → 社保」这类写法绕过去。
    """
    if not forbidden_facts:
        return None
    cand = (candidate or "").replace(" ", "").replace("\u3000", "")
    for f in forbidden_facts:
        key = (f or "").replace(" ", "").replace("\u3000", "")
        if key and key in cand:
            return f
    return None


# ============================================================
# 单例
# ============================================================
_llm = None


def get_llm():
    global _llm
    if _llm is None:
        _llm = OpenAICompatLLM() if os.environ.get("LLM_MODE") == "openai" else MockLLM()
    return _llm


def reset_llm():
    global _llm
    _llm = None


# ============================================================
# v4：基于 scaffold 生成 / 流式生成回复
# ============================================================
def generate_reply(scaffold: dict) -> str:
    """同步回复（用于离线测试或非流式路径）。"""
    msg = prompts.build_messages(scaffold)
    raw = get_llm().chat(msg).strip()
    fallback = (scaffold.get("fallback_text") or "").strip()
    # 命中禁语 → 整段换成程序兜底稿（不靠提示词自觉）
    if fact_violation(scaffold.get("forbidden_facts"), raw):
        return (fallback or raw).strip()
    if not fact_regression(scaffold.get("required_facts"), raw):
        return (fallback or raw).strip()
    return raw


def stream_generate_reply(scaffold: dict):
    """流式生成回复（yield 增量文本片段）。调用方累积。

    若流式累积结果未通过必含事实验证，会在末尾追加一段 '\n\n[fallback]...' 前缀
    并 yield 一个特殊 'event: fallback' 友好的 SSE 事件（外层 SSE 解析器识别）。

    带 forbidden_facts 的 scaffold（「粤省事指引」这类固定短话术）**不走逐字流**：
    先整体生成再校验，命中禁语就整段换兜底稿 —— 逐字推出去的话没法收回，
    界面上就会出现「参保证明」那段被念完再被换掉。
    """
    if scaffold.get("forbidden_facts"):
        yield generate_reply(scaffold)
        return

    required = scaffold.get("required_facts") or []
    fallback = (scaffold.get("fallback_text") or "").strip()
    msg = prompts.build_messages(scaffold)

    accumulated = []
    for chunk in get_llm().stream_chat(msg):
        accumulated.append(chunk)
        yield chunk

    full = "".join(accumulated).strip()
    # 校验必含事实（带 synonym + facts 占位符替换）；若模型完整生成了一段合理回复，
    # 一般会通过校验，避免再追加 fallback 段造成重复。
    if required and not fact_regression(required, full, facts=scaffold.get("facts")) and fallback:
        yield ("__FALLBACK_MARKER__" + fallback)
