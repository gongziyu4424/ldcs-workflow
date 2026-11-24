"""v4 验收用例（mock 模式离线跑通）。

校验方式：
- 每轮 state.reply 是 server 调模型生成的最终回复；
- mock 模式下模型返回 scaffold.fallback_text（程序骨架），因此下面 must_contain
  改成"按业务必含事实"做断言；事实缺失即 FAIL。

运行：python -m ldcs.test_cases
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_MODE"] = "mock"  # 验收用例必须锁 mock

from ldcs import graph
from ldcs import llm as llm_mod
from ldcs.llm import reset_llm
from ldcs.state import new_state
from ldcs.unemp_calc import calc_unemp_benefit, paid_months_from_text

# 「从16年到现在」的期望值 —— 用**生产同一个函数**算，避免测试随系统年份漂移。
# 规则本身锁在 test_unemp.RANGE_TEXT_CASES（基准年固定 2026 → 108 个月 / 9 年）。
_PM_FROM16 = paid_months_from_text("从16年到现在没断过")
_B_FROM16 = calc_unemp_benefit(_PM_FROM16)


CASES = [
    # (用例号, 描述, 输入序列, 必含事实, 必不含, 期望最终 stage)
    (1,  "广州户籍",                ["我想领广州退休金", "广州"],
     ["广州户籍", "最后参保地"], [], "done"),
    (2,  "广东省非广州户籍",         ["我想领广州退休金", "广东肇庆"],
     ["广东省内", "5年", "最后参保地"], [], "done"),
    (3,  "非广东·男符合(23岁,参保12年)", ["我想领广州退休金", "江西九江", "男，23岁参保，参保12年"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], [], "done"),
    (4,  "非广东·女符合边界(39岁)",   ["我想领广州退休金", "江西", "女，39岁参保，参保12年"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], [], "done"),
    (5,  "非广东·男不符合(53岁)",     ["我想领广州退休金", "湖南", "男，53岁参保，参保20年"],
     ["53", "年龄门槛", "广州"], [], "done"),
    (6,  "非广东·女超龄边界(40岁)",   ["我想领广州退休金", "广西", "女，40岁参保，参保15年"],
     ["40", "年龄门槛", "广州"], [], "done"),
    (7,  "反1答非所问后补答",         ["我想领广州退休金", "随便", "广州"],
     ["广州户籍", "最后参保地"], [], "done"),
    (8,  "连续闲聊（真答非所问）→兜底",
     ["我想领广州退休金", "今天天气怎么样", "你吃饭了吗", "给我讲个笑话"],
     ["社保经办窗口"], [], "done"),
    (9,  "把当前年龄当参保年龄",       ["我想领广州退休金", "江西", "男，今年45", "男，20岁参保，参保10年"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], [], "done"),
    (12, "省内年限不足→不符合",       ["我想领广州退休金", "江西九江", "男，23岁参保，只参保了3年"],
     ["10年", "不符合"], [], "done"),
    (13, "不记得参保→引导粤省事",     ["我想领广州退休金", "江西九江", "男，不记得什么时候参保了"],
     ["粤省事"], [], "await_gender_age"),
    (14, "粤省事查到后直接出结论",     ["我想领广州退休金", "江西九江", "男，不记得什么时候参保了",
                                        "查到了，2020年第一次参保，我是1995年出生的"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], ["社保经办窗口"], "done"),
    (15, "一次给齐不问参保年数",       ["我想领广州退休金", "湖南", "男，1995年出生，2015年第一次参保"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], ["参保了多少年", "社保经办窗口"], "done"),
    (16, "两位数年份跨轮补齐",         ["我想领广州退休金", "江西九江", "男，不记得什么时候参保了",
                                        "查到了，是15年的时候", "我是95年的"],
     ["首次参保年龄", "10年", "广州", "最后参保地"], ["社保经办窗口"], "done"),
    (10, "与业务无关",                ["医保怎么报"],
     ["养老待遇领取地"], [], "done"),
    (11, "done后追问不重发结论",       ["我想领广州退休金", "广东肇庆", "需要什么材料"],
     [], ["满5年", "广东省内非广州"], "done"),
    # ---- 退休时间测算支线 ----
    (17, "退休时间·男职工(1965-03→2025-04)", ["我什么时候能退休", "男，1965年3月出生"],
     ["2025年4月", "60周岁1个月", "15年"], ["社保经办窗口"], "done"),
    (18, "退休时间·女工人封顶55(1985-01)", ["我什么时候退休", "女工人，1985年1月出生"],
     ["2040年1月", "55周岁", "20年"], ["社保经办窗口"], "done"),
    (19, "退休时间·女性未分干部工人→追问", ["我什么时候退休", "女，1980年5月出生"],
     [], [], "await_retire"),
    (20, "退休时间·只给年份→补问月份", ["我什么时候退休", "男，1965年"],
     [], [], "await_retire"),
    (21, "退休时间·女干部封顶58(1981-09)", ["我什么时候退休", "女干部，1981年9月出生"],
     ["2039年9月", "58周岁", "20年"], ["社保经办窗口"], "done"),
    (22, "退休时间·2025前到龄不延迟", ["我什么时候退休", "男，1963年6月出生"],
     ["2023年6月", "60周岁", "15年"], ["社保经办窗口"], "done"),
    (23, "领取地结论后追问退休时间", ["我想领广州退休金", "江西九江", "男，1995年出生，2015年第一次参保",
                                        "好的，我想问下大概什么时间能退休"],
     # 出生年份已在领取地支线给过 → 这里只该问「出生月份」，不该再让用户重复说
     # 同时禁止把已知信息罗列成「字段名：值」的清单（那是最僵硬的对账口气）
     ["出生月份"], ["出生年份：", "首次参保年份：", "性别："], "await_retire"),
    # ---- 查询指引（引导粤省事自查）----
    # 只许说「可登录粤省事查询或办理、具体路径短信发您」，**不许报具体菜单**
    # （报了「社保费查询 / 个人参保证明」用户就会去搜错入口 —— 电话里不该念菜单）
    (24, "结论后问怎么查缴费记录→引导粤省事",
     ["我想领广州退休金", "江西九江", "男，1995年出生，2015年第一次参保",
      "我要怎么才能知道自己缴费满没满呢"],
     ["粤省事"], ["社保经办窗口", "参保证明", "社保费查询"], "done"),
    (25, "首轮问怎么查参保记录→引导粤省事",
     ["我怎么查自己的参保缴费记录"],
     ["粤省事"], ["社保经办窗口", "参保证明", "社保费查询"], "done"),
    (26, "「不记得/不清楚」不算答非所问（不兜底）",
     ["我想领广州退休金", "江西九江", "男，不记得什么时候参保了", "不清楚", "想不起来了"],
     ["粤省事"], ["社保经办窗口"], "await_gender_age"),
    (27, "「不记得了」不计入反问2重试配额（须为 0，可返回原状态）",
     ["我想领广州退休金", "湖北", "不记得了，能在哪里查吗", "不清楚", "想不起来了"],
     ["粤省事"], ["社保经办窗口"], "await_gender_age",
     {"attempt_gender_age": 0}),
    (28, "真答非所问才消耗配额（问天气→1/2）",
     ["我想领广州退休金", "湖北", "今天天气怎么样"],
     [], [], "await_gender_age",
     {"attempt_gender_age": 1}),
    (29, "已出结论后用户改口纠正户籍→重判为广州",
     ["我想领广州退休金", "湖北", "男，23岁首次参保",
      "不好意思我说错了，我户口其实在广州"],
     ["广州"], [], "done"),
    (30, "用户改口纠正人员类别→按新类别重算退休时间",
     ["我什么时候退休", "女，1985年1月出生", "我说错了，其实我是女干部"],
     ["55周岁"], [], "done"),
    (31, "「16年参保·那时25岁」→ 反推出生年份，退休支线不再问年份",
     ["我想领广州退休金", "湖北", "男，16年参保的，那时候25岁",
      "我要算退休时间", "3月"],
     ["1991"], [], "done",
     {"birth_year": 1991}),
    (32, "首轮只说「女生」→ 属于本业务信息，不能回「不在测算范围」",
     ["女生"], [], ["不在", "测算范围"], "await_huji",
     {"gender": "female"}),
    (33, "已知参保年份+年龄时，回复不得罗列成字段名清单",
     ["我想领广州退休金", "湖北", "16年参保的，那时候25岁"],
     [], ["出生年份：", "首次参保年份：", "首次参保年龄：", "年龄："],
     "await_gender_age"),
    (34, "答不上参保时间：不许声称「记下了」没收到过的信息，且性别仍要继续问",
     ["我想领广州退休金", "湖北的", "我不记得了"],
     ["粤省事", "男性还是女性"],
     ["我记下了", "已记下", "已记录", "性别："],
     "await_gender_age",
     {"gender": None}),
    (35, "改口户籍后答不上参保时间：同上，不得瞎记",
     ["我想领广州退休金", "湖北的", "说错了，是河北", "我不记得了"],
     ["粤省事", "男性还是女性"],
     ["我记下了", "已记下", "已记录"],
     "await_gender_age",
     {"gender": None, "province": "hebei"}),
    (36, "报出参保信息后只问缺项：不得复述用户刚说的原话",
     ["我想领广州退休金", "湖北", "不记得什么时候参保了",
      "查到了，是16年参保的，那时候应该25岁"],
     ["男性还是女性"],
     ["2016 年首次参保、当时 25 岁。还需要", "还需要您告诉我", "16年首次参保、当时"],
     "await_gender_age",
     {"gender": None, "first_ins_age": 25, "birth_year": 1991}),
    # ---- 失业保险金测算支线 ----
    # 规则：12-48 个月每满 12 个月 = 1 个月；≥48 个月每满 6 个月 = 1 个月；封顶 24
    (37, "失业金·首轮带缴费时长直接出结论(3年→3个月)",
     ["我缴了3年失业保险，单位裁员，失业登记也办了，失业金能领多少"],
     ["失业保险金", "3个月", "7236"], ["社保经办窗口"], "done"),
    (38, "失业金·先追问时长再出结论(2年→2个月)",
     ["失业金能领多少", "缴了2年", "单位裁员", "失业登记办好了"],
     ["失业保险金", "2个月", "4824"], ["社保经办窗口"], "done"),
    (39, "失业金·缴费不足12个月→不具领取资格",
     ["我缴了8个月失业保险，单位裁员，失业登记也办了，能领失业金吗"],
     ["失业保险金"], [], "done",
     {"unemp_paid_months": 8, "unemp_reason": "laid_off", "unemp_registered": True}),
    (40, "失业金·4年整（48个月→4个月）",
     ["失业金怎么算，我缴了4年，单位裁员，失业登记也办了"],
     ["失业保险金", "4个月", "9648"], ["社保经办窗口"], "done"),
    (41, "失业金·4年1个月：超出不足6个月不计（→4个月）",
     ["我缴了4年1个月失业保险，失业金能领多少，单位裁员，失业登记也办了"],
     ["失业保险金", "4个月", "9648"], ["社保经办窗口"], "done"),
    (42, "失业金·5年1个月：4年部分4 + 超出13个月→2（6个月）",
     ["失业金怎么算，我缴了5年1个月，单位裁员，失业登记也办了"],
     ["失业保险金", "6个月", "14472"], ["社保经办窗口"], "done"),
    (43, "失业金·缴了25年→封顶24个月",
     ["失业了，我缴了25年保险，单位裁员，失业登记也办了，失业金能领多少"],
     ["失业保险金", "24个月", "57888"], ["社保经办窗口"], "done"),
    # ---- 「答不上来 / 反问怎么办」→ 统一引导「粤省事」查询或办理 + 稍候短信（各支线）----
    (44, "失业金·答不上缴费时长→引导粤省事查询或办理+短信（点名「失业保险金」）",
     ["失业金能领多少", "不记得了"],
     ["失业保险金", "粤省事", "短信"], ["参保证明", "这项业务"], "await_unemp"),
    (45, "失业金·结论后问「怎么申请」→引导粤省事办理+短信（不报具体菜单）",
     ["我缴了5年失业保险，单位裁员，失业登记也办了，失业金能领多少", "那我要怎么申请呢"],
     ["粤省事", "短信"], ["社保经办窗口", "参保证明", "社保费查询"], "done"),
    (46, "退休·答不上出生月份→引导粤省事+短信，不机械重问同一句",
     ["我什么时候能退休", "男，1965年出生", "不记得几月了"],
     ["粤省事", "短信"], [], "await_retire"),
    (47, "首轮问「怎么办退休」→引导粤省事查询或办理",
     ["我要怎么办理退休"],
     ["粤省事", "短信"], ["12333", "参保证明"], "done"),
    (48, "户籍·答不上户籍地→引导粤省事查询或办理",
     ["我想领广州退休金", "不知道"],
     ["粤省事", "短信"], ["参保证明"], "await_huji"),
    (49, "失业金·追问时反问「上哪查」→引导粤省事+短信",
     ["失业金能领多少", "这个上哪查啊"],
     ["粤省事", "短信"], ["参保证明"], "await_unemp"),
    # ---- 业务诉求优先于办理指引：带「怎么操作」但有新诉求，必须进流程算，不能甩粤省事 ----
    (50, "首轮「想领广州退休金怎么操作」→进领取地流程(不可误判 query)",
     ["想领广州退休金怎么操作"],
     ["户籍"], ["粤省事"], "await_huji"),
    (51, "首轮「失业金怎么申请」→进失业金流程先算(不可误判 query)",
     ["失业金怎么申请"],
     [], ["粤省事"], "await_unemp"),
    # ---- 改口纠正：说错了要能返回上一步、参数跟着改、结论跟着重算 ----
    # （用户实测：报了「29 岁」后改口「是 2016 年参保的，那时 25 岁」，
    #   面板与结论里不该再挂着 29 岁）
    (52, "领取地·改口参保年份+年龄→重算并用新值出结论",
     ["想领广州退休金", "江西九江", "男，29岁", "我说错了，是2016年参保的，那时候25岁"],
     ["25岁"], ["29岁"], "done",
     {"first_ins_age": 25, "birth_year": 1991, "first_insured_year": 2016}),
    # 「我第一次参保是 2010 年」说的是**参保年份**：不许把出生年份改成 2010，
    # 也不许把用户甩进他根本没提过的退休支线
    (53, "领取地·改口参保年份→出生年份不变、参保年龄重算",
     ["想领广州退休金", "江西九江", "男，1991年出生", "说错了，我第一次参保是2010年"],
     ["19岁"], ["1 5 岁"], "done",
     {"birth_year": 1991, "first_insured_year": 2010, "first_ins_age": 19}),
    (54, "领取地·改口参保年份→推算来的出生年份跟着重算",
     ["想领广州退休金", "江西九江", "男，2016年参保，那时候25岁", "不对，我记错了，是2012年参保的"],
     [], ["29岁"], "done",
     {"birth_year": 1987, "first_insured_year": 2012, "first_ins_age": 25}),
    (55, "失业金·改口缴费时长→重算出可领月数",
     ["失业金能领多少", "缴了3年，单位裁员，失业登记也办了", "不对，是缴了5年"],
     ["6个月"], [], "done",
     {"unemp_paid_months": 60}),
    (56, "退休·改口出生年份→按新年份重算退休时间",
     ["我什么时候能退休", "男，1965年3月", "我说错了，我是1966年出生的"],
     ["2026年"], [], "done",
     {"retire_birth_year": 1966}),
    # ---- 失业金「能不能领」资格判定 ----
    # 三项可判条件里 ①②由程序判（缴费满 1 年 / 非本人意愿失业），③ 已办失业登记。
    # ⚠️ ①② 不符合 = 真不符合；③ 没办 = **可补救手续**（说「符合，办完登记就能申请」，
    #    并把能领多少算给他）—— 见 case 57 / 58。
    # ④「材料齐全真实」由经办环节核验 —— **不追问**，会话里没提过的项才问。
    (57, "失业金·离职原因是主动辞职→不符合条件②（真不符合）",
     ["失业金能领多少", "缴了3年", "我自己主动辞职的", "失业登记办好了"],
     ["失业保险金", "不符合"], ["只差办理失业登记"], "done",
     {"unemp_reason": "voluntary", "unemp_registered": True}),
    (58, "失业金·还没办失业登记→符合条件、只差办登记；告知能领多少+引导粤省事（不许说「不符合」）",
     ["失业金能领多少", "缴了3年", "单位裁员", "还没办失业登记"],
     ["失业保险金", "失业登记", "3个月", "7236", "粤省事", "短信"],
     ["不符合", "社保经办窗口", "参保证明"], "done",
     {"unemp_reason": "laid_off", "unemp_registered": False}),
    (59, "失业金·单位欠薪被迫辞职→属非本人意愿，符合（3年→3个月）",
     ["失业金能领多少", "缴了3年", "公司拖欠工资我被迫辞职的", "失业登记办好了"],
     ["失业保险金", "3个月", "7236"], [], "done",
     {"unemp_reason": "employer_fault"}),
    (60, "失业金·一次说齐三项→直接出结论（不逐项反问）",
     ["我缴了5年失业保险，单位裁员，失业登记也办了，失业金能领多少"],
     ["失业保险金", "6个月", "14472"], [], "done",
     {"unemp_registered": True, "unemp_reason": "laid_off"}),
    (61, "失业金·追问途中问「怎么申请」→引导粤省事、不耗重试配额",
     ["失业金能领多少", "那我要怎么申请呢"],
     ["粤省事", "短信"], [], "await_unemp"),
    (62, "失业金·会话里没提过离职原因→只补问这一项（不重问缴费时长）",
     ["失业金能领多少", "缴了3年", "单位裁员"],
     [], ["缴了多久", "缴纳失业保险多久"], "await_unemp",
     {"unemp_paid_months": 36, "unemp_reason": "laid_off"}),
    # 真机 bug（公子宇 2026-09-14 截图）：用户已经说过「被工厂裁了」，
    # 旧实现「只抄原话 + 正则硬匹配」认不出夹字的「被工厂裁」，于是又问一遍离职原因。
    # 这里锁死：说过就不再问，且必须认成 laid_off。
    (63, "失业金·说过「被工厂裁了」→认成单位裁员，不再重复问离职原因",
     ["失业金能领多少", "被工厂裁了", "从16年到现在没断过"],
     [], ["怎么离职", "离职的", "合同到期", "缴费16年"], "await_unemp",
     {"unemp_reason": "laid_off", "unemp_paid_months": _PM_FROM16}),
    # 真机 bug（公子宇 2026-09-14 截图）：「从16年到现在」里的「16年」是**年份**，
    # 旧兜底正则把它当成了 16 年时长（192 个月）→ 结论说「缴费 16年」。
    # 锁死：必须按起止式折算，结论里出现的是折算后的年数。
    (64, "失业金·「从16年到现在」按起止式折算（不是 16 年）",
     ["我想问下失业金怎么算", "被工厂裁了", "从16年到现在", "还没办失业登记"],
     [f"{_PM_FROM16 // 12}年", _B_FROM16["months_cn"], _B_FROM16["total_cn"],
      "失业登记", "粤省事"],
     ["缴费16年", "16年，"], "done",
     {"unemp_paid_months": _PM_FROM16, "unemp_reason": "laid_off"}),
]

# 必含事实容差用例：(required_facts, 候选回复, 期望是否通过)
FACT_TOLERANCE_CASES = [
    (["首次参保年龄"], "您25岁首次参保，是女性", True),
    (["首次参保年龄"], "首次参保时25岁，符合门槛", True),
    (["首次参保年龄"], "25岁那年第一次参保", True),
    (["首次参保年龄"], "您首次参保年龄是25岁", True),
    (["首次参保年龄"], "您2016年首次参保", False),      # 只有年份、没年龄 → 不该通过
    (["性别"], "请问您是男性还是女性？", True),           # 自然问法不能判失败
    (["性别"], "请问您的性别是？", True),
    # 金额千分位：模型写「14,472 元」不能判成「事实缺失」（否则兜底段被追加 = 话说两遍）
    (["14472"], "按您累计缴费5年，合计一共14,472元。", True),
    (["失业保险金", "14472"], "失业保险金可以领6个月，合计一共14,472元。", True),
]

# 禁语用例：(forbidden_facts, 候选回复, 期望命中的禁语或 None)
VIOLATION_CASES = [
    (["参保证明"], "在社保费查询或个人参保证明就能看到", "参保证明"),
    (["服务→社保"], "微信搜「粤省事」小程序 → 登录 → 服务 → 社保 → 个人参保证明", "服务→社保"),
    (["参保证明"], "这项业务可以登录「粤省事」查询或办理，具体路径稍候短信发给您。", None),
    # 「这项业务」这种笼统说法（公子宇 2026-09-14）：指引必须点名他正在办的具体业务
    (["这项业务", "这个业务"], "这项业务可以登录「粤省事」查询或办理。", "这项业务"),
    (["这项业务", "这个业务"], "失业登记可以登录「粤省事」办理。", None),
    ([], "随便说什么", None),
]


def check_forbidden_gate():
    """禁语闸门：模型若吐出「参保证明」这类具体菜单，整段回复必须被换成兜底稿。

    这是**程序兜底**（不靠提示词自觉）：提示词已经把菜单层级删干净了，
    但模型仍可能自由发挥，所以再加一道闸门，命中就把整段换掉。
    """
    real = llm_mod.get_llm()

    class _Stub:
        def chat(self, messages):
            return ("微信搜「粤省事」小程序，登录后进服务里的社保，"
                    "在社保费查询或个人参保证明就能看到省内各市的参保月数。")

        def stream_chat(self, messages):
            yield self.chat(messages)

    llm_mod._llm = _Stub()
    try:
        sc = {
            "action": "guide_ss",
            "required_facts": ["粤省事"],
            "forbidden_facts": ["参保证明", "社保费查询"],
            "fallback_text": ("失业登记可以登录「粤省事」查询或办理，"
                              "具体查询或办理路径稍候会通过短信发给您。"),
            "facts": None,
        }
        out = llm_mod.generate_reply(sc)
        # 流式同口径：带 forbidden_facts 不走逐字流，整段给（免得先念错再被换掉）
        streamed = "".join(llm_mod.stream_generate_reply(sc))
        ok = (("参保证明" not in out) and ("社保费查询" not in out)
              and ("粤省事" in out) and streamed == out)
    finally:
        llm_mod._llm = real
    print(f"[{'PASS' if ok else 'FAIL'}] 禁语闸门：命中「参保证明/社保费查询」整段换兜底稿")
    return ok


def check_unemp_conclusion_hint():
    """失业金结论是「同一个 action、三套提示词」，必须按资格结果选对那份。

    真机实测踩到过：不合格时若还给模型「三项条件都符合、可以领取」的提示词，
    模型会把不合格也说成「您符合领取失业保险金的条件」，与后文自相矛盾。
    2026-09-14 又拆出第三种：「只差办失业登记」——**不符合**的话一句都不许有。
    """
    from ldcs import prompts

    def _scaffold_of(turns):
        state = dict(new_state())
        for t in turns:
            state = graph.run_once(state, t)
        return state.get("_stream_scaffold") or {}

    # ① 只差办失业登记：钉 conclusion_unemp_pending；必须只差「手续」、不说「不符合」；
    #    引导要带粤省事 + 点名「失业登记」+ 把能领多少说出来
    pend = _scaffold_of(["失业金能领多少", "缴了3年失业保险", "单位裁员", "还没办失业登记"])
    hint_pend = prompts._action_hint(pend, "conclusion_unemp", pend.get("facts"))
    rf_pend = pend.get("required_facts") or []
    ok_pend = (pend.get("action") == "conclusion_unemp"
               and pend.get("hint_key") == "conclusion_unemp_pending"
               and "粤省事" in rf_pend and "失业登记" in rf_pend
               and "不符合" not in hint_pend and "只差" in hint_pend
               and (pend.get("facts") or {}).get("months"))
    print(f"[{'PASS' if ok_pend else 'FAIL'}] 失业金·只差失业登记 → "
          f"hint_key={pend.get('hint_key')!r} required={rf_pend}")

    # ② 真不合格（本人主动辞职）：钉 conclusion_unemp_ineligible，
    #    hint 要说「不满足」，不能出现「只差」「都符合」
    bad = _scaffold_of(["失业金能领多少", "缴了3年失业保险", "我自己主动辞职的", "还没办失业登记"])
    hint_bad = prompts._action_hint(bad, "conclusion_unemp", bad.get("facts"))
    ok_bad = (bad.get("action") == "conclusion_unemp"
              and bad.get("hint_key") == "conclusion_unemp_ineligible"
              and "不满足" in hint_bad and "都符合" not in hint_bad
              and "只差" not in hint_bad)
    print(f"[{'PASS' if ok_bad else 'FAIL'}] 失业金·不合格结论 → "
          f"hint_key={bad.get('hint_key')!r} required={bad.get('required_facts')}")

    # ③ 全符合：不钉 hint_key（走默认 conclusion_unemp，且不带粤省事硬要求）
    good = _scaffold_of(["我缴了5年失业保险，单位裁员，失业登记也办了，失业金能领多少"])
    hint_good = prompts._action_hint(good, "conclusion_unemp", good.get("facts"))
    ok_good = (good.get("action") == "conclusion_unemp" and not good.get("hint_key")
               and "粤省事" not in (good.get("required_facts") or [])
               and "都符合" in hint_good and "不满足" not in hint_good)
    print(f"[{'PASS' if ok_good else 'FAIL'}] 失业金·符合结论 → "
          f"hint_key={good.get('hint_key')!r}（应为空）required={good.get('required_facts')}")
    return ok_pend and ok_bad and ok_good


def check_huji_not_misread_in_insured_context():
    """参保语境里说的「广东省」不能被当成户籍改口（公子宇 2026-09-14 截图 bug）。

    真机实测：用户答「哦，是广东省啊，看错了，是16年」时，抽取器把「广东省」
    当成**户籍纠正**（corrections.province=guangdong，改提示词前最高 6/6），
    户籍被从「湖北」改成「广东」→ 回户籍链分类 → 缺城市 → 又问「您是广州户籍吗？」。
    用户说的是**参保地**（本工具的参保地本来就是广东），不是户籍。
    """
    from ldcs.flow import _drop_misread_huji

    # ① 参保语境（户籍已收集、正问参保）里的「广东省」→ 丢掉 province/city，其余保留
    st = {"province": "hubei", "stage": "await_gender_age"}
    a = _drop_misread_huji({"province": "guangdong", "first_insured_year": 2016},
                           "哦，是广东省啊，看错了，是16年", st)
    ok_a = a == {"first_insured_year": 2016}
    # ② 明说「户口/户籍/籍贯」要改 → 必须保留（铁律 18：改口必须能返回去）
    b = _drop_misread_huji({"province": "hunan"}, "我说错了，我户口在湖南", st)
    c = _drop_misread_huji({"province": "hunan"}, "不好意思，不是湖北，是湖南", st)
    ok_b = (b == {"province": "hunan"}) and (c == {"province": "hunan"})
    # ③ 正在问户籍（await_huji）时不拦
    d = _drop_misread_huji({"province": "guangdong"}, "广东的",
                           {"province": None, "stage": "await_huji"})
    ok_c = d == {"province": "guangdong"}
    # ④ 桩模型强制制造这个误判 → 走完整轮次，确认户籍没被改、也没再问户籍
    real = llm_mod.get_llm()

    class _Stub:
        def chat(self, messages):
            txt = str((messages or [{}])[-1].get("content", ""))
            if "广东省" in txt:
                return ('{"gender": null, "first_insured_year": 2016, "gd_years": null,'
                        ' "in_context": true, "corrections": {"province": "guangdong"},'
                        ' "reason": null}')
            return "{}"

        def stream_chat(self, messages):
            yield self.chat(messages)

    llm_mod._llm = _Stub()
    try:
        s = dict(new_state())
        s["province"] = "hubei"
        s["stage"] = "await_gender_age"
        s["first_insured_year"] = 2020
        s["first_ins_age"] = 29
        s = graph.run_once(s, "哦，是广东省啊，看错了，是16年")
        sc = s.get("_stream_scaffold") or {}
        ok_d = (s.get("province") == "hubei"
                and s.get("first_insured_year") == 2016
                and str(sc.get("action", "")).startswith("ask_gender_age"))
    finally:
        llm_mod._llm = real
    ok = ok_a and ok_b and ok_c and ok_d
    print(f"[{'PASS' if ok else 'FAIL'}] 参保语境「是广东省」不写成户籍改口 → "
          f"护栏 a={ok_a} b={ok_b} c={ok_c} 整轮 d={ok_d} "
          f"(province={s.get('province')!r} action={sc.get('action')!r})")
    return ok


def check_ask_topic_injected():
    """「当前话题」必须随原话交给抽取器（公子宇 2026-09-14 建议的机制）。

    抽取器是单轮、不带对话历史的。不告诉它本轮在问什么，同一句话就会被按错误
    语境读 —— 上面的 `check_huji_not_misread_in_insured_context` 只是**事后**把
    误写的字段摘掉；这里测的是**事前**就让它别读错：每条抽取消息都带
    「【当前在问】<话题>\n【用户本轮原话】<原话>」。
    """
    from ldcs import prompts
    from ldcs.flow import (ASK_TOPIC, ASK_TOPIC_SHORT, _ask_topic,
                           _ask_topic_short, _run_extract, _stage_topic)

    # ① 四个抽取器都支持话题注入，且用户原话被完整包进去
    text = "哦，是广东省啊，看错了，是16年"
    builds = [
        ("huji", prompts.extract_huji_messages),
        ("gender_age", prompts.extract_gender_age_messages),
        ("retire", prompts.extract_retire_messages),
        ("unemp", prompts.extract_unemp_messages),
    ]
    ok_a = True
    for kind, fn in builds:
        topic = _ask_topic(kind)
        user = fn(text, topic=topic)[-1]["content"]
        ok_a = ok_a and topic and "【当前在问】" in user and "【用户本轮原话】" in user
    # ② 剥离必须还原成原话（mock 规则只该看原话，别把话题文案当用户说的话）
    wrapped = prompts.wrap_user_text(text, _ask_topic("gender_age"))
    ok_b = prompts.strip_topic_prefix(wrapped) == text
    ok_b = ok_b and prompts.strip_topic_prefix(text) == text     # 幂等：没前缀就原样
    # ③ 参保话题必须点明「广东省 = 参保地，不是户籍」，并点明「广东省内 N 年 = 年限」
    ga_topic = ASK_TOPIC.get("gender_age", "")
    ok_c = ("参保地" in ga_topic) and ("不是户籍" in ga_topic) \
        and ("年限" in ga_topic)
    # ④ 抽取节点把**短话题**写回 state（可观测，前端状态面板要显示）
    #    ⚠️ 长版 ASK_TOPIC 是写给模型的内部提示（含「不是户籍」这类判断规则 + markdown
    #    粗体），**绝不能透到界面**（公子宇 2026-09-14 截图反馈）。
    st = dict(new_state())
    st["user_input"] = text
    _run_extract({"kind": "gender_age"}, st)
    st_topic = st.get("ask_topic") or ""
    ok_d = (bool(st_topic) and "**" not in st_topic
            and "不是户籍" not in st_topic and len(st_topic) <= 24)
    # ⑤ 面板只信**最终 stage**：不在 await_* 上（结论已出 done）就没有「当前在问」→ 清空，
    #    否则上一轮的旧话题会一直挂在面板上。
    ok_d = ok_d and _stage_topic({"stage": "done"}) == "" \
        and _stage_topic({"stage": None}) == "" \
        and _stage_topic({"stage": "await_gender_age"}) == _ask_topic_short("gender_age")
    # ⑥ mock 不被话题污染：带话题 / 不带话题，规则抽取结果必须一致
    mock = llm_mod.MockLLM()
    base = [{"role": "system", "content": prompts._extract_ga_system()},
            {"role": "user", "content": text}]
    raw = [{"role": "system", "content": prompts._extract_ga_system()},
           {"role": "user", "content": wrapped}]
    ok_e = mock.chat(base) == mock.chat(raw)
    # ⑦ 话题必须降级成「优先关注点」而不是围栏（公子宇 2026-09-14：
    #    「加个话题表示似乎有控制的太死了」）：
    #    · 每轮都附【字段归属】一行，明说不是抽取范围的限制；
    #    · **没有话题时也要注入**（结论已出 / 用户自己起话题时恰恰最需要抽更正），
    #      写占位说明而不是干脆不给语境。
    no_topic = prompts.wrap_user_text(text)
    ok_f = ("【字段归属】" in raw[-1]["content"]
            and "不是抽取范围的限制" in raw[-1]["content"]
            and "【当前在问】" in no_topic and "没有固定话题" in no_topic
            and prompts.strip_topic_prefix(no_topic) == text)

    ok = ok_a and ok_b and ok_c and ok_d and ok_e and ok_f
    print(f"[{'PASS' if ok else 'FAIL'}] 抽取器带「当前话题」→ "
          f"注入 a={ok_a} 剥离 b={ok_b} 话题文案 c={ok_c} 写回 state d={ok_d} "
          f"mock不污染 e={ok_e} 只是优先项/无话题也注入 f={ok_f}")
    return ok


def check_gd_years_not_read_as_year():
    """「广东省内应该是 16 年」= **年限 16 年**，不是参保年份 2016。

    公子宇 2026-09-14 截图：话题是「首次在广东省参保的年龄」，结论已出后用户更正
    「说错了，广东省内应该是 16 年的时间」——①抽取器顺着话题把它读成
    first_insured_year=2016（真机实测），连带把出生年份重算成 1987；
    ②结论已出时这句话会落到 followup → 回一句「登录粤省事就能查参保缴费记录」
    的客套引导，用户的更正等于没说（他还补了一句「这种客套话没兜呢」）。
    这里三层一起测：程序量/标分辨、结论后路由、整轮行为。
    """
    from ldcs.flow import _correction_route_target, _gd_years_from_text

    # ① 程序能自己分辨「年数」与「年份」（不指望模型）
    pos = ["说错了，广东省内应该是16年的时间", "我广东省内参保16年", "省内交了16年",
           "在广东省内累计满10年", "广东缴了8年"]
    neg = ["我是2016年参保的", "广东省内2016年参保的", "我16年参保的，那时25岁",
           "广东省内16岁参保", "我2016年退休",
           "哦，是广东省啊，看错了，是16年",     # 这里「16 年」真是参保**年份**（旧截图场景）
           "我广东省内参保，2016年的时候"]
    bad_pos = [t for t in pos if _gd_years_from_text(t) is None]
    bad_neg = [t for t in neg if _gd_years_from_text(t) is not None]
    ok_a = not bad_pos and not bad_neg
    # ② 结论已出后的更正必须**优先于**查询指引，回领取地支线重算
    ok_b = (_correction_route_target("说错了，广东省内应该是16年的时间") == "extract_ga"
            and _correction_route_target("谢谢你啊") is None)
    # ③ 整轮：按截图里的路径走一遍，最后一轮必须重新出结论、年数落库、户籍不被误改
    turns = ["我想领广州退休金", "湖北", "女，2016年的时候25岁参保",
             "说错了，广东省内应该是16年的时间"]
    state = dict(new_state())
    for t in turns:
        state = graph.run_once(state, t)
        sc = state.get("_stream_scaffold")
        if sc:
            state["reply"] = llm_mod.generate_reply(sc).strip()
    last_scaffold = state.get("_stream_scaffold") or {}
    reply = state.get("reply") or ""
    ok_c = (state.get("stage") == "done"
            and state.get("gd_years") == 16
            and state.get("first_insured_year") == 2016      # 轮3 真正说过的参保年份，保留
            and state.get("province") == "hubei"             # 没被「广东省内」误改成广东
            and last_scaffold.get("action") == "conclusion"  # 重新出结论，不是 followup 客套
            and "还有别的想了解" not in reply)

    ok = ok_a and ok_b and ok_c
    print(f"[{'PASS' if ok else 'FAIL'}] 「广东省内 16 年」是年限不是年份 2016 → "
          f"量/标 a={ok_a}(误判正例{bad_pos or '无'}/误判反例{bad_neg or '无'}) "
          f"结论后路由 b={ok_b} 整轮重出结论 c={ok_c} "
          f"(gd_years={state.get('gd_years')} action={last_scaffold.get('action')!r})")
    return ok


def check_unemp_apply_points_to_benefit():
    """失业金结论后问「怎么申请」→ 主题是**失业保险金申领**，不是再讲一遍失业登记。

    公子宇 2026-09-14 截图反馈：已经说过失业登记怎么办了，用户接着问「那我要怎么申请呢」，
    助手又回「失业登记可以登录粤省事办理」—— 重复且跑偏。此刻他问的是**待遇怎么申领**。
    """
    from ldcs.flow import _guide_action, _service_name

    # ① 结论已出（只差登记没办）+ 用户问「怎么申请」→ 点名「失业保险金」、动词「申领」
    after = {"user_input": "那我要怎么申请呢", "stage": "done",
             "unemp_result": {"qualified": True, "eligible": False},
             "unemp_registered": False,
             "unemp_missing": ["是否已办理失业登记"]}
    ok_a = (_service_name(after) == "失业保险金"
            and _guide_action(_service_name(after)) == "申领")
    # ② 还在追问阶段（未出结论）→ 问的就是那一项，说「失业登记」
    ask = {"user_input": "还没办", "stage": "await_unemp",
           "unemp_registered": False,
           "unemp_missing": ["是否已办理失业登记"]}
    ok_b = (_service_name(ask) == "失业登记"
            and _guide_action(_service_name(ask)) == "办理")
    # ③ 用户原话里**自己点名**「失业登记」→ 就说失业登记（问什么答什么）
    named = dict(after)
    named["user_input"] = "失业登记在哪办"
    ok_c = _service_name(named) == "失业登记"
    # ④ 端到端整轮：走完失业金 → 问「怎么申请」→ 回复点名失业保险金申领，不再提失业登记
    state = dict(new_state())
    for t in ["我想问下失业金怎么算", "被工厂裁了，缴了5年失业保险",
              "还没办失业登记", "那我要怎么申请呢"]:
        state = graph.run_once(state, t)
        sc = state.get("_stream_scaffold")
        if sc:
            state["reply"] = llm_mod.generate_reply(sc).strip()
    last = state.get("reply") or ""
    sc = state.get("_stream_scaffold") or {}
    ok_d = ("失业保险金" in last and "申领" in last and "失业登记" not in last)
    ok = ok_a and ok_b and ok_c and ok_d
    print(f"[{'PASS' if ok else 'FAIL'}] 失业金结论后问「怎么申请」→ 指向失业金申领 "
          f"a={ok_a} b={ok_b} c={ok_c} 整轮 d={ok_d}")
    if not ok_d:
        print(f"     末轮 action={sc.get('action')!r} 回复: {last[:120]}")
    return ok


def run_case(cid, desc, turns, required_facts, must_not_contain, final_stage,
             expect=None):
    state = dict(new_state())
    replies = []
    for t in turns:
        state = graph.run_once(state, t)
        scaffold = state.get("_stream_scaffold")
        if scaffold:
            state["reply"] = llm_mod.generate_reply(scaffold).strip()
        replies.append(state.get("reply") or "")

    last = replies[-1]
    # 最后一轮 scaffold 的 facts 用于渲染 required_facts 里的 {retire_cn}/{min_years_cn} 等占位符
    last_scaffold = state.get("_stream_scaffold") or {}
    last_facts = last_scaffold.get("facts") or {}
    if not last_facts and state.get("retire_result"):
        # mock 模式可能在 done 后清掉 _stream_scaffold，回填 retire_result 里的关键事实
        r = state["retire_result"]
        last_facts = {
            "retire_cn": r.get("retire_cn", ""),
            "min_years_cn": r.get("min_contribution_cn", ""),
            "min_years": r.get("min_contribution_cn", ""),
        }
    pass_regression = llm_mod.fact_regression(required_facts, last, facts=last_facts)
    leaks = [w for w in must_not_contain if w in last]
    bad_expect = {k: (state.get(k), v) for k, v in (expect or {}).items()
                  if state.get(k) != v}
    ok = (pass_regression and not leaks and state.get("stage") == final_stage
          and not bad_expect)
    print(f"[{'PASS' if ok else 'FAIL'}] 用例{cid} {desc}")
    if not ok:
        print(f"     期望包含事实: {required_facts}  fact_regression={pass_regression}")
        print(f"     期望不含: {must_not_contain}  泄露={leaks or '无'}")
        print(f"     期望stage={final_stage}  实际={state.get('stage')}")
        if bad_expect:
            print(f"     期望状态值不符（实际 vs 期望）: {bad_expect}")
        for i, (t, r) in enumerate(zip(turns, replies)):
            print(f"     轮{i+1} 用户: {t}\n          AI: {r[:120]}")
    return ok


def check_ga_age_spoken_forms():
    """口语报年龄（「女，25」「女的，25」）必须抽得到，且兜底不能被死门槛挡住。

    公子宇 2026-09-17 实测必现路径：问户籍时答「江西人」→ 再答「女，25」→
    系统把 25 判成「当前年龄」，first_ins_age 一直为空 → **反复追问同一句话**。
    三层一起测：
    ① mock 规则认得没带「岁」字的口语写法（且不把「20 年」「58 年」误当年龄）；
    ② 模型硬判 ambiguous=true 时，程序兜底在 await_gender_age 下必须采纳 current_age
       —— 原门槛 `attempt_gender_age >= 1` 与「语境内回应配额归零」互斥，兜底永远够不着；
    ③ 护栏：原话明说「今年」时仍按当前年龄处理，继续追问首次参保那年的周岁。
    """
    import json
    from ldcs import config
    from ldcs.llm import MockLLM, _extract_gender_age_rule

    # ① mock 规则：口语写法（不带「岁」）
    pos = ["女，25", "女，25岁", "女的，25", "女25", "女 25"]
    neg = ["我今年25岁", "女的，我交了20年", "男的58年生的"]
    bad_pos = [t for t in pos if _extract_gender_age_rule(t)["first_ins_age"] != 25]
    bad_neg = [t for t in neg if _extract_gender_age_rule(t)["first_ins_age"] is not None]
    ok_a = not bad_pos and not bad_neg

    # ② 兜底：桩把抽取结果硬写成 ambiguous=true + current_age=25
    #    （patch MockLLM.chat —— 用例锁 mock 模式，get_llm 返回的就是它）
    orig_chat = MockLLM.chat
    ga_system = config.get_config()["system"]["extract_gender_age_system"]

    def stub(self, messages):
        sysm = next((m["content"] for m in messages if m["role"] == "system"), "")
        if sysm == ga_system:
            return json.dumps({
                "gender": "female", "birth_year": None, "first_insured_year": None,
                "first_ins_age": None, "current_age": 25,
                "is_current_age_ambiguous": True, "first_ins_age_unknown": False,
                "gd_years": None, "gd_years_unknown": False,
                "in_context": True, "corrections": {}, "reason": "桩：硬判当前年龄",
            }, ensure_ascii=False)
        return orig_chat(self, messages)

    base = {"stage": "await_gender_age", "branch": "other_prov", "province": "jiangxi"}
    MockLLM.chat = stub
    try:
        st1 = dict(graph.run_once(dict(new_state(), **base), "女，25"))
        st2 = dict(graph.run_once(dict(new_state(), **base), "我今年25岁"))
    finally:
        MockLLM.chat = orig_chat
    ok_b = (st1.get("first_ins_age") == 25
            and (st1.get("_stream_scaffold") or {}).get("action") == "conclusion")
    ok_c = (st2.get("first_ins_age") is None
            and (st2.get("_stream_scaffold") or {}).get("action") == "ask_gender_age")

    # ③ 整轮：真实路径「江西人 → 女，25」第一遍就该出结论，不能重复追问
    state = dict(new_state())
    for t in ["我想问一下养老金在哪领", "江西人", "女，25"]:
        state = dict(graph.run_once(state, t))
    sc = state.get("_stream_scaffold") or {}
    ok_d = state.get("first_ins_age") == 25 and sc.get("action") == "conclusion"

    ok = ok_a and ok_b and ok_c and ok_d
    print(f"[{'PASS' if ok else 'FAIL'}] 口语报年龄「女，25」→ "
          f"mock规则 a={ok_a}(漏{bad_pos or '无'}/误{bad_neg or '无'}) "
          f"兜底采纳 b={ok_b} 今年护栏 c={ok_c} 整轮不重复问 d={ok_d}")
    return ok


def main():
    reset_llm()
    print("=" * 60)
    print("待遇领取地测算工作流 · 验收用例（mock 模式 / v4）")
    print("=" * 60)
    results = [run_case(*c) for c in CASES]
    passed = sum(results)
    print("-" * 60)
    # 必含事实容差：模型把「首次参保年龄」说成「您 25 岁首次参保」这类自然说法时
    # **不能**判失败（判失败就会追加兜底段 = 用户看到「同一段话说两遍」）。
    print("必含事实容差（同义/语义表达）")
    tol = [llm_mod.fact_regression(r, c) == want for r, c, want in FACT_TOLERANCE_CASES]
    for (r, c, want), ok in zip(FACT_TOLERANCE_CASES, tol):
        print(f"[{'PASS' if ok else 'FAIL'}] {r} vs {c!r} 期望 {want}")
    passed += sum(tol)
    # 禁语容差：命中禁语要能认出来（认不出来 = 闸门失效）
    print("禁语识别（不许出现的表述）")
    vio = [llm_mod.fact_violation(r, c) == want for r, c, want in VIOLATION_CASES]
    for (r, c, want), ok in zip(VIOLATION_CASES, vio):
        print(f"[{'PASS' if ok else 'FAIL'}] 禁语{r} vs {c!r} 期望命中 {want}")
    passed += sum(vio)
    gate_ok = check_forbidden_gate()
    passed += 1 if gate_ok else 0
    hint_ok = check_unemp_conclusion_hint()
    passed += 1 if hint_ok else 0
    huji_ok = check_huji_not_misread_in_insured_context()
    passed += 1 if huji_ok else 0
    topic_ok = check_ask_topic_injected()
    passed += 1 if topic_ok else 0
    gd_ok = check_gd_years_not_read_as_year()
    passed += 1 if gd_ok else 0
    apply_ok = check_unemp_apply_points_to_benefit()
    passed += 1 if apply_ok else 0
    ga_form_ok = check_ga_age_spoken_forms()
    passed += 1 if ga_form_ok else 0
    total = (len(CASES) + len(FACT_TOLERANCE_CASES) + len(VIOLATION_CASES) + 7)
    print("-" * 60)
    print(f"通过 {passed}/{total}")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
