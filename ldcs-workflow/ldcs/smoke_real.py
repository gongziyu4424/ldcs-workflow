"""真实 LLM（DeepSeek V4 Flash）冒烟验证（v4 模型问答版）。

跑关键路径，逐轮打印对话，并用 fact_regression 校验必含事实。
mock 模式下跑不通本脚本（要接真模型）；test_cases 才是离线验收。

运行：python -m ldcs.smoke_real
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldcs import graph
from ldcs import llm as llm_mod
from ldcs.llm import get_llm, reset_llm
from ldcs.state import new_state


# (描述, 输入序列, 期望 stage, 必含事实)
SMOKE = [
    ("用例1 广州户籍",
     ["我想领广州退休金", "广州"], "done",
     ["广州户籍", "最后参保地"]),
    ("用例2 广东肇庆(满5年)",
     ["我想领广州退休金", "广东肇庆"], "done",
     ["广东省内", "5年", "最后参保地"]),
    ("用例3 江西男23岁符合",
     ["我想领广州退休金", "江西九江", "男，23岁参保"], "done",
     ["首次参保年龄", "广州", "最后参保地"]),
    ("用例6 广西女40岁不符合",
     ["我想领广州退休金", "广西", "女，40岁参保"], "done",
     ["40", "年龄门槛", "广州"]),
    ("用例8 连续答非所问→兜底",
     ["我想领广州退休金", "不知道", "不知道", "还是不知道"], "done",
     ["社保经办窗口"]),
]


def run(desc, turns, expect_stage, required_facts):
    state = dict(new_state())
    print(f"\n---------- {desc} ----------")
    for i, t in enumerate(turns, 1):
        state = graph.run_once(state, t)
        scaffold = state.get("_stream_scaffold")
        if scaffold:
            # 流式模拟（mock 也按字符推一遍，真实模型同理）
            chunks = []
            for c in llm_mod.stream_generate_reply(scaffold):
                if c.startswith("__FALLBACK_MARKER__"):
                    chunks.append(c[len("__FALLBACK_MARKER__"):])
                else:
                    chunks.append(c)
            state["reply"] = "".join(chunks).strip() or scaffold.get("fallback_text", "")
        print(f"[轮{i}] 用户> {t}\n       AI> {(state.get('reply') or '')[:160]}")

    stage_ok = state.get("stage") == expect_stage
    fact_ok = llm_mod.fact_regression(required_facts, state.get("reply") or "")
    ok = stage_ok and fact_ok
    print(f"  结果: stage={'OK' if stage_ok else 'FAIL('+str(state.get('stage'))+')'}"
          f"  必含事实={'OK' if fact_ok else 'FAIL'}")
    return ok


def main():
    reset_llm()
    llm = get_llm()
    print(f"已接入模型: {type(llm).__name__}  "
          f"model={os.environ.get('LLM_MODEL', '?')}  "
          f"base={os.environ.get('LLM_BASE_URL', '?')}")
    results = [run(*c) for c in SMOKE]
    print("\n" + "=" * 60)
    print(f"真实模型冒烟: 通过 {sum(results)}/{len(results)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
