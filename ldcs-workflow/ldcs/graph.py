"""每轮运行入口。

v6：运行时改走画布 flow（数据驱动拓扑）。下面保留 v5 的 LangGraph build_graph 作对照
参考 —— 但 langgraph 改为「按需惰性导入」，因此部署环境无需安装 langgraph 即可运行。
run_once 统一委托 ldcs.flow.run_flow，使「画布改拓扑/配置 → 对话行为」真生效。
"""
from . import nodes
from .flow import run_flow
from .state import LdcsState, new_state


# ============================================================
# v5 静态图（保留，供对照/回退。运行时默认走 flow）
# ============================================================
def _route_intent(state: dict) -> str:
    """如果 n_intent 已经写了 _stream_scaffold，说明本轮已对外开口，直接 END。
    否则依据 stage 决定下一节点。"""
    if state.get("_intent") == "miss":
        return "off_topic"
    if state.get("_intent") == "followup":
        return "followup"
    # hit：n_intent 没写 _stream_scaffold 才意味着"还要继续走流程"
    if state.get("_stream_scaffold"):
        return "end"
    # 没有 _stream_scaffold → 走流程节点
    if state.get("stage") == "await_gender_age":
        return "extract_gender_age"
    return "extract_huji"


def build_graph():
    # 惰性导入：仅 v5 对照/回退时才需要 langgraph，部署环境可不装
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(LdcsState)
    g.add_node("intent",          nodes.n_intent)
    g.add_node("off_topic",       nodes.n_off_topic)
    g.add_node("followup",        nodes.n_followup)
    g.add_node("extract_huji",    nodes.n_extract_huji)
    g.add_node("decide_huji",     nodes.n_decide_huji)
    g.add_node("extract_gender_age", nodes.n_extract_gender_age)
    g.add_node("decide_age",      nodes.n_decide_age)

    g.set_entry_point("intent")
    g.add_conditional_edges("intent", _route_intent, {
        "off_topic":          "off_topic",
        "followup":           "followup",
        "extract_huji":       "extract_huji",
        "extract_gender_age": "extract_gender_age",
        "end":                END,
    })
    g.add_edge("extract_huji", "decide_huji")
    g.add_edge("decide_huji", END)

    g.add_edge("extract_gender_age", "decide_age")
    g.add_edge("decide_age", END)

    g.add_edge("off_topic", END)
    g.add_edge("followup", END)
    return g.compile()


_app = None


def get_app():
    global _app
    if _app is None:
        _app = build_graph()
    return _app


def run_once(state: dict, user_input: str) -> dict:
    """v6 主入口：按 config.flow 拓扑执行一轮（画布可改，保存即生效）。"""
    return run_flow(state, user_input)
