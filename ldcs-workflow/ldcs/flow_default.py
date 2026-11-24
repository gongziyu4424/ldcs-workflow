"""v6 默认流程定义（DEFAULT_FLOW）—— 画布初始布局 + 运行时引擎的拓扑依据。

- nodes: [{id, type, label, x, y, config}]  type ∈ start|say|extract|condition|code|tool|end
- edges: [{from, to, port, label}]           port 用于条件/代码节点按出口标签连线，
                                            非条件节点为 ""（默认出口）。
坐标 x/y 为画布平面坐标（可负，前端负责缩放平移；拖动后写回）。
"""
from copy import deepcopy

NODE_TYPES = {
    "start":     {"label": "开始",     "color": "#2e7d32"},
    "say":       {"label": "处理",     "color": "#185fa5"},
    "extract":   {"label": "LLM抽取",  "color": "#7f77dd"},
    "condition": {"label": "条件",     "color": "#d85a30"},
    "code":      {"label": "代码",     "color": "#a32d2d"},
    "tool":      {"label": "工具调用", "color": "#d4537e"},
    "end":       {"label": "结束",     "color": "#5f5e5a"},
}

# 终结类 action：say 节点执行后 stage=done、at=None（不再等用户输入）
TERMINAL_ACTIONS = {"conclusion", "conclusion_retire", "conclusion_unemp",
                    "fallback", "off_topic", "followup", "guide_ss"}

# 非终结 say 默认 stage_after（若节点未显式配置）
DEFAULT_STAGE_AFTER = {
    "ask_huji": "await_huji",
    "ask_huji_retry": "await_huji",
    "ask_gender_age": "await_gender_age",
    "ask_gender_age_retry": "await_gender_age",
    "ask_retire": "await_retire",
    "ask_retire_retry": "await_retire",
    "ask_unemp": "await_unemp",
    "ask_unemp_retry": "await_unemp",
}


def _n(node_id, ntype, label, x, y, cfg=None):
    return {"id": node_id, "type": ntype, "label": label,
            "x": x, "y": y, "config": cfg or {}}


def _e(frm, to, port="", label=""):
    return {"from": frm, "to": to, "port": port, "label": label}


DEFAULT_FLOW = {
    "nodes": [
        # --- 入口 ---
        _n("start", "start", "开始", 40, 300, {}),
        _n("route_intent", "condition", "意图路由", 210, 300,
           {"kind": "route_intent", "hint": "按 stage 与用户输入分流"}),
        # --- 改口纠正回退：用户说错了要改，就带新值回到对应支线重跑 ---
        _n("fix_route", "condition", "改口纠正回退", 720, 300,
           {"kind": "route_correction",
            "hint": "用户改口纠正了哪条支线的字段，就回哪条支线重判"}),
        # --- 分支节点（说一句话即停，等用户）---
        _n("ask_huji", "say", "反问1 · 户籍", 380, 140,
           {"action": "ask_huji", "stage_after": "await_huji"}),
        _n("off_topic", "say", "业务无关", 380, 460,
           {"action": "off_topic"}),
        _n("followup", "say", "追问细节", 380, 560,
           {"action": "followup"}),
        _n("guide_ss", "say", "粤省事查询指引", 380, 760,
           {"action": "guide_ss"}),
        # --- 户籍线 ---
        _n("extract_huji", "extract", "抽户籍", 550, 140,
           {"kind": "huji"}),
        _n("missing_huji", "condition", "户籍是否齐全", 720, 140,
           {"kind": "missing_huji"}),
        _n("classify_huji", "condition", "户籍分类", 890, 140,
           {"kind": "classify_huji"}),
        _n("conclude_gz", "say", "结论 · 广州户籍", 1060, 60,
           {"action": "conclusion", "branch": "gz"}),
        _n("conclude_gd", "say", "结论 · 省内非广州", 1060, 180,
           {"action": "conclusion", "branch": "gd_other"}),
        _n("ask_ga", "say", "反问2 · 性别年龄", 1060, 320,
           {"action": "ask_gender_age", "stage_after": "await_gender_age"}),
        # --- 重试 / 兜底（huji 段）---
        _n("huji_attempt", "condition", "户籍重试上限", 720, 400,
           {"kind": "attempt_left", "field": "attempt_huji", "max_field": "max_huji_retry"}),
        _n("ask_huji_retry", "say", "反问1 · 重试", 550, 400,
           {"action": "ask_huji_retry", "stage_after": "await_huji",
            "attempt_field": "attempt_huji"}),
        _n("fallback", "say", "兜底 · 转线下", 1060, 430,
           {"action": "fallback"}),
        # --- 性别年龄线 ---
        _n("extract_ga", "extract", "抽性别年龄", 1230, 320,
           {"kind": "gender_age"}),
        _n("check_ga", "condition", "性别年龄是否齐全", 1400, 320,
           {"kind": "check_ga"}),
        _n("judge_age", "condition", "年龄门槛判定", 1570, 320,
           {"kind": "judge_age"}),
        _n("conclude_q", "say", "结论 · 跨省符合", 1740, 220,
           {"action": "conclusion", "branch": "other_qualify"}),
        _n("conclude_u", "say", "结论 · 跨省超龄", 1740, 420,
           {"action": "conclusion", "branch": "other_unqualify"}),
        # --- 重试 / 兜底（ga 段）---
        _n("ga_attempt", "condition", "性别年龄重试上限", 1400, 480,
           {"kind": "attempt_left", "field": "attempt_gender_age",
            "max_field": "max_gender_age_retry"}),
        _n("ask_ga_retry", "say", "反问2 · 重试", 1230, 480,
           {"action": "ask_gender_age_retry", "stage_after": "await_gender_age",
            "attempt_field": "attempt_gender_age"}),
        # --- 退休时间测算支线（工具调用：调用退休计算程序）---
        _n("ask_retire", "say", "反问3 · 出生年月", 380, 660,
           {"action": "ask_retire", "stage_after": "await_retire"}),
        _n("extract_retire", "extract", "抽出生年月·人员类别", 550, 660,
           {"kind": "retire"}),
        _n("check_retire", "condition", "退休信息是否齐全", 720, 660,
           {"kind": "check_retire"}),
        _n("calc_retire", "tool", "调用退休计算程序", 890, 660,
           {"kind": "calc_retire"}),
        _n("conclude_retire", "say", "结论 · 退休时间", 1060, 660,
           {"action": "conclusion_retire"}),
        _n("retire_attempt", "condition", "退休信息重试上限", 720, 780,
           {"kind": "attempt_left", "field": "attempt_retire",
            "max_field": "max_retire_retry"}),
        _n("ask_retire_retry", "say", "反问3 · 重试", 550, 780,
           {"action": "ask_retire_retry", "stage_after": "await_retire"}),
        # --- 失业保险金测算支线（工具调用：调用失业金计算程序）---
        _n("ask_unemp", "say", "反问4 · 失业金信息", 380, 900,
           {"action": "ask_unemp", "stage_after": "await_unemp"}),
        _n("extract_unemp", "extract", "抽失业金槽位", 550, 900,
           {"kind": "unemp"}),
        _n("check_unemp", "condition", "缴费/离职/登记是否齐全", 720, 900,
           {"kind": "check_unemp"}),
        _n("calc_unemp", "tool", "调用失业金计算程序", 890, 900,
           {"kind": "calc_unemp"}),
        _n("conclude_unemp", "say", "结论 · 失业保险金", 1060, 900,
           {"action": "conclusion_unemp"}),
        _n("unemp_attempt", "condition", "失业金重试上限", 720, 1020,
           {"kind": "attempt_left", "field": "attempt_unemp",
            "max_field": "max_unemp_retry"}),
        _n("ask_unemp_retry", "say", "反问4 · 重试", 550, 1020,
           {"action": "ask_unemp_retry", "stage_after": "await_unemp"}),
        # --- 结束 ---
        _n("end_node", "end", "结束", 1910, 240, {}),
    ],
    "edges": [
        _e("start", "route_intent", "", ""),
        # 意图路由 → 分支
        _e("route_intent", "ask_huji", "ask_huji", "命中"),
        _e("route_intent", "off_topic", "off_topic", "无关"),
        _e("route_intent", "followup", "followup", "已完成"),
        _e("route_intent", "guide_ss", "guide_ss", "查询指引"),
        _e("route_intent", "extract_huji", "extract_huji", "await_huji"),
        _e("route_intent", "extract_ga", "extract_ga", "await_gender_age"),
        _e("route_intent", "ask_retire", "retire", "退休时间"),
        _e("route_intent", "extract_retire", "extract_retire", "await_retire"),
        _e("route_intent", "ask_unemp", "unemp", "失业金"),
        _e("route_intent", "extract_unemp", "extract_unemp", "await_unemp"),
        # 户籍主链
        _e("ask_huji", "extract_huji", "", "答后"),
        _e("extract_huji", "fix_route", "", ""),
        _e("missing_huji", "classify_huji", "ok", "齐全"),
        _e("missing_huji", "huji_attempt", "miss", "缺失"),
        _e("missing_huji", "ask_huji", "ask", "首问"),
        _e("huji_attempt", "ask_huji_retry", "retry", "重试"),
        _e("huji_attempt", "fallback", "giveup", "放弃"),
        _e("ask_huji_retry", "extract_huji", "", "再答"),
        _e("classify_huji", "conclude_gz", "gz", "广州"),
        _e("classify_huji", "conclude_gd", "gd_other", "省内非广州"),
        _e("classify_huji", "check_ga", "other_prov", "省外"),
        # 性别年龄主链
        _e("ask_ga", "extract_ga", "", "答后"),
        _e("extract_ga", "fix_route", "", ""),
        _e("check_ga", "judge_age", "ok", "齐全"),
        _e("check_ga", "ask_ga", "ask", "首问"),
        _e("check_ga", "ga_attempt", "miss", "缺失"),
        _e("ga_attempt", "ask_ga_retry", "retry", "重试"),
        _e("ga_attempt", "fallback", "giveup", "放弃"),
        _e("ask_ga_retry", "extract_ga", "", "再答"),
        _e("judge_age", "conclude_q", "qualify", "符合"),
        _e("judge_age", "conclude_u", "unqualify", "超龄"),
        # 退休时间测算支线
        _e("ask_retire", "extract_retire", "", "答后"),
        _e("extract_retire", "fix_route", "", ""),
        _e("check_retire", "calc_retire", "ok", "齐全"),
        _e("check_retire", "ask_retire", "ask", "首问"),
        _e("check_retire", "retire_attempt", "miss", "缺失"),
        _e("retire_attempt", "ask_retire_retry", "retry", "重试"),
        _e("retire_attempt", "fallback", "giveup", "放弃"),
        _e("ask_retire_retry", "extract_retire", "", "再答"),
        # 失业保险金测算支线
        _e("ask_unemp", "extract_unemp", "", "答后"),
        _e("extract_unemp", "fix_route", "", ""),
        _e("check_unemp", "calc_unemp", "ok", "齐全"),
        _e("check_unemp", "ask_unemp", "ask", "首问"),
        _e("check_unemp", "unemp_attempt", "miss", "缺失"),
        _e("unemp_attempt", "ask_unemp_retry", "retry", "重试"),
        _e("unemp_attempt", "fallback", "giveup", "放弃"),
        _e("ask_unemp_retry", "extract_unemp", "", "再答"),
        _e("calc_unemp", "conclude_unemp", "", ""),
        _e("conclude_unemp", "end_node", "", ""),
        # 改口纠正回退：回到对应支线重跑（户籍优先——它决定走哪条支线）
        _e("fix_route", "missing_huji", "huji", "回户籍重判"),
        _e("fix_route", "check_ga", "ga", "回性别年龄"),
        _e("fix_route", "check_retire", "retire", "回退休"),
        _e("fix_route", "check_unemp", "unemp", "回失业金"),
        _e("calc_retire", "conclude_retire", "", ""),
        _e("conclude_retire", "end_node", "", ""),
        # 收尾 → 结束
        _e("conclude_gz", "end_node", "", ""),
        _e("conclude_gd", "end_node", "", ""),
        _e("conclude_q", "end_node", "", ""),
        _e("conclude_u", "end_node", "", ""),
        _e("fallback", "end_node", "", ""),
        _e("off_topic", "end_node", "", ""),
        _e("followup", "end_node", "", ""),
        _e("guide_ss", "end_node", "", ""),
    ],
}


def get_default_flow() -> dict:
    """返回深拷贝，避免调用方改到模块常量。"""
    return deepcopy(DEFAULT_FLOW)


# ============================================================
# 画布元数据：左侧工具条节点类型 / 判定 kind 的出口端口 / say action 清单
# ============================================================
FLOW_META = {
    "node_types": [
        {"type": "start",     "label": "开始",     "desc": "流程入口（每轮对话起点）",
         "color": "#2e7d32", "icon": "entry"},
        {"type": "say",       "label": "处理",     "desc": "LLM 生成回复并暂停等用户",
         "color": "#185fa5", "icon": "chat"},
        {"type": "extract",   "label": "抽取",     "desc": "LLM 槽位抽取（户籍/性别年龄）",
         "color": "#7f77dd", "icon": "extract"},
        {"type": "condition", "label": "条件",     "desc": "程序判定并按结果出口",
         "color": "#d85a30", "icon": "diamond"},
        {"type": "code",      "label": "代码",     "desc": "预置逻辑节点（同条件判定）",
         "color": "#a32d2d", "icon": "code"},
        {"type": "tool",      "label": "工具调用", "desc": "预留：外部工具（当前直通）",
         "color": "#d4537e", "icon": "tool"},
        {"type": "end",       "label": "结束",     "desc": "流程终点（置 done）",
         "color": "#5f5e5a", "icon": "exit"},
    ],
    "kinds": [
        {"kind": "route_intent", "label": "意图/阶段路由",
         "ports": ["ask_huji", "off_topic", "followup", "guide_ss", "extract_huji",
                   "extract_ga", "retire", "extract_retire", "unemp", "extract_unemp"],
         "default_port": "ask_huji", "group": "入口判定"},
        {"kind": "missing_huji", "label": "户籍是否齐全",
         "ports": ["ok", "miss", "ask"], "default_port": "ok", "group": "户籍"},
        {"kind": "classify_huji", "label": "户籍分类（省/市）",
         "ports": ["gz", "gd_other", "other_prov", "miss"], "default_port": "gz",
         "group": "户籍"},
        {"kind": "route_correction", "label": "改口纠正回退（按被纠正字段回支线）",
         "ports": ["huji", "ga", "retire", "unemp"], "default_port": "huji", "group": "公共"},
        {"kind": "attempt_left", "label": "重试次数是否用完",
         "ports": ["retry", "giveup"], "default_port": "retry", "group": "公共"},
        {"kind": "check_ga", "label": "性别年龄是否齐全",
         "ports": ["ok", "miss", "ask"], "default_port": "ok", "group": "性别年龄"},
        {"kind": "judge_age", "label": "年龄门槛判定",
         "ports": ["qualify", "unqualify", "miss"], "default_port": "qualify",
         "group": "性别年龄"},
        {"kind": "check_retire", "label": "退休信息是否齐全（出生年月/人员类别）",
         "ports": ["ok", "miss", "ask"], "default_port": "ok", "group": "退休时间"},
        {"kind": "check_unemp", "label": "失业金信息是否齐全（缴费/离职原因/失业登记）",
         "ports": ["ok", "miss", "ask"], "default_port": "ok", "group": "失业金"},
    ],
    "actions": [
        {"action": "greet", "label": "问候开场", "terminal": False},
        {"action": "ask_huji", "label": "反问1 · 问户籍", "terminal": False,
         "stage_after": "await_huji"},
        {"action": "ask_huji_retry", "label": "反问1 · 重试问户籍", "terminal": False,
         "stage_after": "await_huji", "attempt_field": "attempt_huji"},
        {"action": "ask_gender_age", "label": "反问2 · 问性别年龄", "terminal": False,
         "stage_after": "await_gender_age"},
        {"action": "ask_gender_age_retry", "label": "反问2 · 重试", "terminal": False,
         "stage_after": "await_gender_age", "attempt_field": "attempt_gender_age"},
        {"action": "conclusion", "label": "结论（选 branch）", "terminal": True},
        {"action": "ask_retire", "label": "反问3 · 问出生年月/人员类别", "terminal": False,
         "stage_after": "await_retire"},
        {"action": "ask_retire_retry", "label": "反问3 · 重试", "terminal": False,
         "stage_after": "await_retire", "attempt_field": "attempt_retire"},
        {"action": "conclusion_retire", "label": "结论 · 退休时间", "terminal": True},
        {"action": "ask_unemp", "label": "反问4 · 问失业金信息（缴费/离职/登记）", "terminal": False,
         "stage_after": "await_unemp"},
        {"action": "ask_unemp_retry", "label": "反问4 · 重试", "terminal": False,
         "stage_after": "await_unemp", "attempt_field": "attempt_unemp"},
        {"action": "conclusion_unemp", "label": "结论 · 失业保险金", "terminal": True},
        {"action": "fallback", "label": "兜底 · 转线下", "terminal": True},
        {"action": "off_topic", "label": "业务无关提示", "terminal": True},
        {"action": "followup", "label": "已完成 · 追问细节", "terminal": True},
        {"action": "guide_ss", "label": "查询指引 · 引导粤省事", "terminal": True},
    ],
    "branches": [
        {"branch": "gz", "label": "结论 · 广州户籍"},
        {"branch": "gd_other", "label": "结论 · 省内非广州"},
        {"branch": "other_qualify", "label": "结论 · 省外符合"},
        {"branch": "other_unqualify", "label": "结论 · 省外超龄"},
    ],
}
