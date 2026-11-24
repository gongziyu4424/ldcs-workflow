#!/usr/bin/env python3
"""ldcs-workflow MCP server —— 把「待遇领取地 / 退休时间 / 失业保险金」三项测算暴露成 MCP 工具。

形态：stdio 传输，只用 Python 标准库，不占端口、不依赖线上沙箱、不走 HTTP。
业务逻辑直接复用 ldcs 引擎（与线上演示台同一份代码），行为一致。

启动：
    python ldcs_mcp.py            # 一般由 MCP 客户端拉起，不用手工跑

客户端配置（~/.workbuddy/mcp.json 或 Claude Desktop）：
    "ldcs": {
      "command": "/Users/gongziyu/.workbuddy/binaries/python/envs/default/bin/python",
      "args": ["/Users/gongziyu/WorkBuddy/工作流制作/ldcs-workflow/ldcs_mcp.py"]
    }

⚠️ 铁律：stdout 是 JSON-RPC 协议通道，任何日志/调试只能用 stderr。
   一行 stdout 混进半句 print，客户端立刻协议错误、工具全废。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "ldcs-pension", "version": "1.0.0"}
SESSION_FILE = ROOT / ".mcp_sessions.json"
MAX_SESSIONS = 40

INSTRUCTIONS = """\
本 server 提供广州社保三项测算：待遇领取地、退休时间、失业保险金。

两种用法，按需选：
1) 多轮问答式（pension_chat）—— 模拟话务员，信息不全时一次只追问一句。
   必须复用返回的 session_id，否则每轮都是新会话、永远问不完。
   一次只问一个缺项是设计如此，不要替用户一次填完。
2) 无状态精确计算（calc_retirement_time / calc_unemployment_benefit）——
   信息已齐时直接算，不必走追问流程。

铁律：政策口径只在计算程序里，不要自行推断或改写政策数字。
「还没办失业登记」只是差一道手续，不等于不符合领取条件。
"""

# 与 server.py 的 PUBLIC_KEYS 对齐：只对外透出业务字段，不透内部运行态
PUBLIC_KEYS = [
    "stage", "ask_topic", "province", "city", "gender", "first_ins_age", "age_ambiguous",
    "birth_year", "first_insured_year", "gd_years",
    "branch", "judge", "judge_reason", "attempt_huji", "attempt_gender_age", "attempt_retire",
    "ga_progress", "retire_progress", "huji_progress",
    "ga_in_context", "retire_in_context", "huji_in_context",
    "retire_birth_year", "retire_birth_month", "retire_category",
    "retire_missing", "retire_result",
    "unemp_paid_months", "unemp_reason", "unemp_registered",
    "unemp_missing", "unemp_result",
    "attempt_unemp", "unemp_progress", "unemp_in_context",
]

LABELS = {
    "province": "参保省份",
    "city": "参保城市",
    "gender": "性别",
    "first_ins_age": "首次参保年龄",
    "age_ambiguous": "年龄存疑",
    "birth_year": "出生年份",
    "first_insured_year": "首次参保年份",
    "gd_years": "省内累计缴费年限",
    "branch": "判定分支",
    "judge": "领取地判定",
    "judge_reason": "判定原因",
    "retire_birth_year": "退休测算·出生年",
    "retire_birth_month": "退休测算·出生月",
    "retire_category": "退休测算·人员类别",
    "retire_result": "退休测算结果",
    "unemp_paid_months": "失业金·累计缴费月数",
    "unemp_reason": "失业金·离职情形",
    "unemp_registered": "失业金·已办失业登记",
    "unemp_result": "失业金测算结果",
}

# 只透出「事实类」字段；进度/内部标记不进摘要
SKIP_PREFIX = ("attempt_",)
SKIP_SUFFIX = ("_progress", "_in_context")

STAGE_CN = {
    "ask_huji": "正在追问参保地 / 户籍",
    "await_huji": "等待参保地 / 户籍信息",
    "ask_gender_age": "正在追问性别与年龄",
    "await_gender_age": "等待性别与年龄信息",
    "ask_retire": "正在追问退休测算所需信息",
    "ask_unemp": "正在追问失业保险金所需信息",
    "done": "当前支线已出结论",
}

GENDER_CN = {"male": "男", "female": "女", "男": "男", "女": "女"}

# 摘要里把内部码换成中文，避免模型把 "guangdong" 当成人话复述出去
CODE_CN = {
    "guangdong": "广东", "guangzhou": "广州", "shenzhen": "深圳",
    "other": "非广东", "unknown": "未知",
    "contract_end": "合同期满终止", "laid_off": "单位裁员",
    "dismissed": "被开除辞退", "mutual": "协商一致解除",
    "employer_fault": "单位违法被迫离职", "other_lawful": "其他法定情形",
    "voluntary": "本人主动辞职",
}

# ============================================================
# 环境
# ============================================================

def _load_dotenv() -> None:
    """极简 .env 载入：不引入 python-dotenv，避免 MCP 客户端启动时依赖缺失。

    LDCS_SKIP_DOTENV=1 时不读（测试用 mock 模式就这么来）。
    """
    if os.environ.get("LDCS_SKIP_DOTENV") == "1":
        return
    p = ROOT / ".env"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        _log("读取 .env 失败，继续用当前环境变量")


def _log(msg: str) -> None:
    """日志只能进 stderr。"""
    sys.stderr.write(f"[ldcs-mcp] {msg}\n")
    sys.stderr.flush()


# ============================================================
# 会话存储（内存 + 落盘，跨客户端重启也能接上）
# ============================================================

def _load_sessions() -> dict:
    """读会话存档。

    为什么落盘而不是只放内存：MCP 客户端（尤其桌面端）随时重启进程，
    只放内存的话用户昨天说到一半的信息全丢，第二天得从头再讲一遍。
    读失败一律当空——存档坏了不该让整个服务起不来。
    """
    if not SESSION_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_sessions(sessions: dict) -> None:
    try:
        if len(sessions) > MAX_SESSIONS:
            # 保留最近写入的一批（dict 保序，最早的在前）
            for k in list(sessions.keys())[: len(sessions) - MAX_SESSIONS]:
                sessions.pop(k, None)
        SESSION_FILE.write_text(
            json.dumps(sessions, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception as e:
        _log(f"会话落盘失败（不影响本轮）: {e}")


# ============================================================
# 状态摘要
# ============================================================

def _blank(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def summarize(state: dict) -> dict:
    """把内部 state 压成模型能直接读的进度摘要。"""
    facts: dict[str, object] = {}
    for k in PUBLIC_KEYS:
        if any(k.startswith(p) for p in SKIP_PREFIX) or any(k.endswith(s) for s in SKIP_SUFFIX):
            continue
        if k in ("stage", "ask_topic", "branch", "judge_reason", "retire_missing", "unemp_missing"):
            continue
        v = state.get(k)
        if _blank(v) or isinstance(v, (dict, list)):
            continue
        label = LABELS.get(k, k)
        if isinstance(v, str):
            v = GENDER_CN.get(v, v) if k == "gender" else CODE_CN.get(v, v)
        facts[label] = v

    missing: list[str] = []
    for key in ("retire_missing", "unemp_missing"):
        got = state.get(key)
        if isinstance(got, list):
            missing.extend(str(x) for x in got if x)

    stage = state.get("stage") or ""
    stage_cn = STAGE_CN.get(stage, stage)

    out: dict = {
        "stage": stage,
        "stage_cn": stage_cn,
        "facts": facts,
        "asking": state.get("ask_topic") or None,
    }
    if missing:
        out["missing"] = missing
    for key, label in (("retire_result", "退休时间结论"), ("unemp_result", "失业保险金结论")):
        r = state.get(key)
        if isinstance(r, dict) and r:
            out[label] = r
    if state.get("judge"):
        out["待遇领取地结论"] = state.get("judge")
    if state.get("branch"):
        out["branch"] = state.get("branch")
    return out


def _fmt_summary_text(s: dict) -> str:
    lines: list[str] = []
    if s.get("stage_cn"):
        lines.append(f"阶段：{s['stage_cn']}")
    if s.get("facts"):
        kv = "；".join(f"{k}={v}" for k, v in s["facts"].items())
        lines.append(f"已知：{kv}")
    if s.get("missing"):
        lines.append(f"仍缺：{'、'.join(s['missing'])}")
    for k in ("待遇领取地结论", "退休时间结论", "失业保险金结论"):
        if s.get(k):
            lines.append(f"{k}：{_compact(s[k])}")
    return "\n".join(lines)


def _compact(obj) -> str:
    """把结果 dict 压成一行可读文本，去掉冗余数字字段。"""
    if not isinstance(obj, dict):
        return str(obj)
    prefer = [
        "category_cn", "birth_cn", "retire_age_cn", "retire_cn", "delay_months",
        "min_contribution_cn", "paid_cn", "months", "total_cn", "formula_cn",
        "fail_cn", "advice_cn", "min_wage",
    ]
    parts = [f"{k}={obj[k]}" for k in prefer if k in obj and not _blank(obj[k])]
    if not parts:
        parts = [f"{k}={v}" for k, v in obj.items()
                 if isinstance(v, (str, int, float, bool)) and not _blank(v)][:6]
    return "，".join(parts)


# ============================================================
# 业务调用
# ============================================================

def _engine():
    """延迟导入引擎：让 initialize 秒回，import 出错也能给出人话。"""
    from ldcs import graph, llm as llm_mod
    return graph, llm_mod


def do_chat(message: str, session_id: str | None) -> dict:
    """多轮对话一轮：跑一次流程 → 让模型把 scaffold 说成人话 → 存档。

    三个容易踩的点：
    ① session_id 由客户端回传。MCP 的工具调用本身无状态，客户端不记就等
       于每轮都开新会话，用户会被反复问同一个问题。
    ② stage 只在「本轮已给出结论 / 兜底 / 离题 / 追问」时置 done，
       ask_huji 这类追问必须保留原 stage，否则流程会以为已经结束。
    ③ _stream_scaffold 是运行态，落盘前必须 pop 掉，否则下次读到脏 scaffold
       会把上一轮的结论再播一遍。
    """
    graph, llm_mod = _engine()

    sid = (session_id or "").strip() or uuid.uuid4().hex[:12]
    sessions = _load_sessions()
    state = sessions.get(sid)
    if not isinstance(state, dict):
        from ldcs.state import new_state
        state = dict(new_state())

    state = graph.run_once(state, message)
    scaffold = state.get("_stream_scaffold")

    reply, degraded = "", False
    if scaffold:
        try:
            reply = (llm_mod.generate_reply(scaffold) or "").strip()
        except Exception as e:
            degraded = True
            _log(f"模型生成失败，回退模板: {e}")
            reply = _fallback_reply(scaffold)
        if scaffold.get("action") in ("conclusion", "fallback", "off_topic", "followup",
                                      "conclusion_retire", "conclusion_unemp"):
            state["stage"] = "done"
    else:
        reply = (state.get("reply") or "").strip()

    # _stream_scaffold 是运行态，不落盘
    state.pop("_stream_scaffold", None)
    state["reply"] = reply
    sessions[sid] = state
    _save_sessions(sessions)

    summary = summarize(state)
    text = reply or "（本轮没有生成回复，请检查模型配置）"
    extra = _fmt_summary_text(summary)
    if extra:
        text = f"{text}\n\n---\n{extra}"
    if degraded:
        text += "\n\n[提示] 模型调用失败，以上为模板兜底文案，政策数字仍准确。"
    return {
        "session_id": sid,
        "reply": reply,
        "summary": summary,
        "degraded": degraded,
        "_text": text,
    }


def _fallback_reply(scaffold: dict) -> str:
    """模型不可用时的降级：用 scaffold 里已有的事实拼一句人话。"""
    facts = scaffold.get("facts") or {}
    parts = [str(v) for v in facts.values() if not _blank(v)]
    if parts:
        return "，".join(parts) + "。"
    return (scaffold.get("fallback_text") or "").strip() or "请补充信息后我再为您测算。"


def do_status(session_id: str) -> dict:
    """只读查进度。不改任何状态，可以随便重复调。"""
    sessions = _load_sessions()
    state = sessions.get(session_id)
    if not isinstance(state, dict):
        raise ValueError(f"会话不存在或已过期: {session_id}")
    summary = summarize(state)
    return {
        "session_id": session_id,
        "reply": state.get("reply") or "",
        "summary": summary,
        "_text": _fmt_summary_text(summary) or "（暂无进展）",
    }


def do_reset(session_id: str) -> dict:
    """清空会话。**这是唯一的重置方式**，业务层没有别的清空手段。

    已收集的槽位、追问配额、支线进度全部归零；下次 pension_chat 从头问起。
    """
    sessions = _load_sessions()
    existed = session_id in sessions
    sessions.pop(session_id, None)
    _save_sessions(sessions)
    return {
        "session_id": session_id,
        "reset": True,
        "existed": existed,
        "_text": ("会话已清空，下次 pension_chat 将从头开始。" if existed
                  else f"该会话本就不存在: {session_id}"),
    }


def do_calc_retire(birth_year: int, birth_month: int, category: str) -> dict:
    """无状态退休测算。政策口径全在 retire_calc，这里不做任何二次解释。"""
    from ldcs.retire_calc import calc_retirement, format_result_cn
    res = calc_retirement(birth_year, birth_month, category)
    return {**res, "_text": format_result_cn(res)}


def do_calc_unemp(paid_months: int, reason_code: str | None,
                  registered: bool | None, min_wage: int | None) -> dict:
    """无状态失业金测算。

    ⚠️ 业务铁律：`registered=False`（没办失业登记）**只是差一道手续**，
    不是资格不符。程序照常把月数和金额算出来，由 _unemp_text 补一句「办完登记即可申领」。
    谁的代码把这种情况判成「不符合领取条件」，就是把能领的钱说成了不能领。
    """
    from ldcs.unemp_calc import calc_unemp, DEFAULT_MIN_WAGE
    try:
        res = calc_unemp(paid_months, reason_code, registered,
                         min_wage if min_wage else DEFAULT_MIN_WAGE)
    except TypeError:
        # 老签名兼容
        res = calc_unemp(paid_months, reason_code, registered)
    res["_text"] = _unemp_text(res)
    return res


def _unemp_text(res: dict) -> str:
    """把计算结果写成人话。**措辞有业务后果，不是随便排排版**。

    规则：
      · 有月数金额就一定要报出来，哪怕资格还有一项没满足（差手续 ≠ 不符合）
      · registered=False 必须带一句「办完登记即可申领」，不能只报数字
      · 真不符合（缴费不满 1 年 / 本人主动辞职）才说「不满足领取条件」，
        并给出程序算的 fail_cn / advice_cn，别自己编政策
    """
    lines = [f"累计缴费：{res.get('paid_cn') or res.get('paid_months')}"]
    if res.get("months") is not None:
        lines.append(f"可领月数：{res.get('months')}个月（{res.get('months_cn') or ''}）")
    if res.get("total") is not None:
        lines.append(f"总金额：{res.get('total_cn') or res.get('total')}")
        if res.get("formula_cn"):
            lines.append(f"算法：{res['formula_cn']}")
    if res.get("fail_cn"):
        lines.append(f"不符合项：{res['fail_cn']}")
    if res.get("advice_cn"):
        lines.append(f"下步建议：{res['advice_cn']}")
    if not res.get("qualified"):
        lines.append("资格结论：不满足领取条件（见上）")
    elif res.get("registered") is False:
        lines.append("提醒：资格本身符合，只差办理失业登记，办完即可申领。")
    return "\n".join(lines)


# ============================================================
# 工具定义
# ============================================================

TOOLS: list[dict] = [
    {
        "name": "pension_chat",
        "description": (
            "待遇测算对话（多轮）。用户用大白话说情况，本工具负责追问缺项、判定、给结论。"
            "覆盖待遇领取地、退休时间、失业保险金三条支线。"
            "信息不全时一次只追问一句，属正常行为，不要替用户一次填完。"
            "⚠️ 后续轮次必须带上返回的 session_id，否则等于重开新会话。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "用户这一轮说的话（原话即可）"},
                "session_id": {
                    "type": "string",
                    "description": "上一轮返回的会话号；首次对话留空，由服务端生成",
                },
            },
            "required": ["message"],
        },
        "_handler": lambda a: do_chat(str(a.get("message") or "").strip(), a.get("session_id")),
    },
    {
        "name": "pension_status",
        "description": "只读查看某个会话的进度：已收集哪些信息、还缺什么、是否已出结论。不改状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话号"}},
            "required": ["session_id"],
        },
        "_handler": lambda a: do_status(str(a.get("session_id") or "").strip()),
    },
    {
        "name": "pension_reset",
        "description": "清空指定会话的全部已收集信息，从头开始。用户改主意、换人咨询时用。",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "会话号"}},
            "required": ["session_id"],
        },
        "_handler": lambda a: do_reset(str(a.get("session_id") or "").strip()),
    },
    {
        "name": "calc_retirement_time",
        "description": (
            "精确计算法定退休时间与最低缴费年限（无状态，不走追问）。"
            "依据 2025-01-01 起施行的渐进式延迟退休决定；最低缴费年限按实际退休时点动态计算"
            "（2029 年底前 15 年，2030 起每年加 6 个月，2039 年封顶 20 年）。"
            "已知出生年月和人员类别时优先用这个，比多轮对话快。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "birth_year": {"type": "integer", "description": "出生年份，如 1975"},
                "birth_month": {"type": "integer", "minimum": 1, "maximum": 12,
                                "description": "出生月份 1-12"},
                "category": {
                    "type": "string",
                    "enum": ["male", "female_cadre", "female_worker"],
                    "description": "male=男职工；female_cadre=原55岁退休女职工（女干部/管理技术岗）；female_worker=原50岁退休女职工（女工人）",
                },
            },
            "required": ["birth_year", "birth_month", "category"],
        },
        "_handler": lambda a: do_calc_retire(a["birth_year"], a["birth_month"], a["category"]),
    },
    {
        "name": "calc_unemployment_benefit",
        "description": (
            "计算失业保险金领取资格、可领月数与总金额（无状态，不走追问）。"
            "口径：累计缴费满 1 年才有资格；1-4 年部分每满 12 个月折 1 个月失业金，"
            "4 年以上每满 6 个月折 1 个月，封顶 24 个月；金额=月数×最低工资×90%。"
            "⚠️ 未办失业登记只是差手续，仍要照常算出金额并提醒去办登记，不能说成不符合条件。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paid_months": {"type": "integer", "minimum": 0,
                                "description": "失业保险累计缴费月数"},
                "reason_code": {
                    "type": "string",
                    "enum": ["contract_end", "laid_off", "dismissed", "mutual",
                             "employer_fault", "other_lawful", "voluntary"],
                    "description": "离职情形：contract_end 合同期满/单位注销；laid_off 裁员或单位单方解除；dismissed 开除辞退；mutual 协商一致；employer_fault 单位违法被迫离职；other_lawful 其他法定情形；voluntary 本人主动辞职（不符合条件）",
                },
                "registered": {"type": "boolean", "description": "是否已办理失业登记"},
                "min_wage": {"type": "integer", "description": "当地最低工资标准（元/月），默认 2680"},
            },
            "required": ["paid_months"],
        },
        "_handler": lambda a: do_calc_unemp(
            a["paid_months"], a.get("reason_code"), a.get("registered"), a.get("min_wage")
        ),
    },
]


# ============================================================
# JSON-RPC over stdio
# ============================================================

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, payload: dict) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": payload})


def _error(req_id, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _call_tool(name: str, args: dict) -> dict:
    """执行工具并封装成 MCP 的返回结构。

    _text 是给人和模型看的可读版，structuredContent 是给程序用的完整数据，
    两个都要给：只给 JSON 模型读起来费劲，只给文本客户端没法二次处理。
    """
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if spec is None:
        raise ValueError(f"未知工具: {name}")
    data = spec["_handler"](args or {})
    text = data.pop("_text", None) or json.dumps(data, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": False,
    }


def handle(msg: dict) -> None:
    """单条 JSON-RPC 消息分发。

    协议层的两条硬规矩，破了客户端会直接报协议错误、工具全消失：
      ① 没有 id 的是通知（notifications/*），**一律不回复**，回了就错。
      ② 任何异常都必须转成 JSON-RPC error 或 isError 结果，绝不能让进程崩。
    """
    method = msg.get("method") or ""
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # 通知类（无 id）一律不回复
    if req_id is None and method.startswith("notifications/"):
        if method == "notifications/initialized":
            _log("客户端握手完成")
        return

    try:
        if method == "initialize":
            client = (params.get("clientInfo") or {}).get("name") or "unknown"
            _log(f"initialize ← {client} protocol={params.get('protocolVersion')}")
            _result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
            return

        if method == "ping":
            _result(req_id, {})
            return

        if method == "tools/list":
            _result(req_id, {
                "tools": [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]
            })
            return

        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            _log(f"call {name}")
            try:
                _result(req_id, _call_tool(name, arguments))
            except Exception as e:
                _log(f"工具 {name} 出错: {e}\n{traceback.format_exc()}")
                _result(req_id, {
                    "content": [{"type": "text", "text": f"{name} 执行失败：{e}"}],
                    "isError": True,
                })
            return

        if method in ("resources/list", "resources/templates/list"):
            _result(req_id, {"resources": []} if method == "resources/list" else {"resourceTemplates": []})
            return

        if method == "prompts/list":
            _result(req_id, {"prompts": []})
            return

        _error(req_id, -32601, f"Method not found: {method}")

    except Exception as e:
        _log(f"处理 {method} 时崩了: {e}\n{traceback.format_exc()}")
        if req_id is not None:
            _error(req_id, -32603, f"Internal error: {e}")


def main() -> None:
    """主循环：从 stdin 逐行读 JSON-RPC，处理完继续等。

    ⚠️ 铁律：**stdout 是协议通道**。这里或任何被调用的代码只要往 stdout 打一行
    非 JSON 的东西（哪怕是个 print("debug")），客户端立刻解析失败、全部工具消失，
    而且报错信息完全指不到真正的原因。日志一律走 stderr。
    """
    _load_dotenv()
    _log(f"启动，root={ROOT}，LLM_MODE={os.environ.get('LLM_MODE') or 'mock(默认)'}")
    stdin = getattr(sys.stdin, "buffer", sys.stdin)
    for raw in stdin:
        try:
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        except Exception:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            _log(f"收到非 JSON 行，已忽略: {e}")
            continue
        if isinstance(msg, list):
            for m in msg:
                handle(m)
        elif isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
