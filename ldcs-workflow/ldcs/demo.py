"""命令行多轮会话演示。

用法（默认 mock 模式，无需任何 key）：
    python -m ldcs.demo
真实 LLM 模式（阿里云百炼 Token Plan）：
    LLM_MODE=openai LLM_BASE_URL=https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 \
    LLM_API_KEY=sk-sp-xxx LLM_MODEL=qwen3.6-plus python -m ldcs.demo
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ldcs import graph
from ldcs.state import new_state


def main():
    state = new_state()
    print("AI话务员：您好，请问有什么可以帮您？\n")
    while True:
        text = input("用户> ").strip()
        if text in ("exit", "quit"):
            break
        state = graph.run_once(state, text)
        print(f"AI    > {state.get('reply')}\n")


if __name__ == "__main__":
    main()
