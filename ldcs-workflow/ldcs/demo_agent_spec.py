"""演示 / 验证：话务员规范版（agent_spec）四条主路径的真实回复。

跑法（项目根目录）：
    python -m ldcs.demo_agent_spec

会临时切到 agent_spec 预设，跑完自动切回 default。
"""
import os
import sys

sys.path.insert(0, '/Users/gongziyu/WorkBuddy/工作流制作/ldcs-workflow')
os.environ.setdefault('LLM_MODE', 'openai')

from ldcs import config as cfg_mod, graph, llm as llm_mod  # noqa: E402
from ldcs.state import new_state  # noqa: E402

CASES = [
    ("分支一 · 广州户籍", ["我想领广州退休金", "广州"]),
    ("分支二 · 省内非广州", ["我想领广州退休金", "广东肇庆"]),
    ("分支三 · 省外符合（男 23）", ["我想领广州退休金", "江西九江", "男，23 岁首次参保"]),
    ("分支三 · 省外超龄（男 53）", ["我想领广州退休金", "江西九江", "男，53 岁首次参保"]),
    ("边界 · 户籍只说「广东」", ["我想领广州退休金", "广东"]),
    ("边界 · 误报当前年龄", ["我想领广州退休金", "江西九江", "男，我今年 30 岁"]),
    ("边界 · 两次答不全 → 兜底", ["我想领广州退休金", "江西九江", "男", "女", "不知道"]),
]


def run_case(title, turns):
    print("=" * 78)
    print("■ " + title)
    st = dict(new_state())
    for t in turns:
        st = graph.run_once(st, t)
        sc = st.get("_stream_scaffold") or {}
        reply = llm_mod.generate_reply(sc) if sc else ""
        print("  用户：" + t)
        print("  客服：" + reply.replace("\n", " "))
        print("        [stage=%s action=%s branch=%s]" % (
            st.get("stage"), sc.get("action"), st.get("branch")))
    print()


if __name__ == "__main__":
    print("当前预设：", cfg_mod.get_preset_name())
    cfg_mod.apply_preset("agent_spec")
    print("切换后：", cfg_mod.get_preset_name(), "\n")
    for title, turns in CASES:
        run_case(title, turns)
    cfg_mod.apply_preset("default")
    print("已恢复：", cfg_mod.get_preset_name())
