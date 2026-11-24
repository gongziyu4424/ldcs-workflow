"""真机演示：①「查询指引」意图（→ 引导粤省事）；②「在语境里回应」判定。

用法：
    cd ldcs-workflow && LLM_MODE=openai python -m ldcs.demo_query_context

两组用例说明两件事：

1) 意图判定分出一类 query —— 用户问「怎么查自己的参保/缴费记录」，
   正确回应是引导「粤省事」自查，而不是推 12333 / 窗口。

2) 抽取节点输出 in_context —— 判断用户本轮**是不是在回应本流程的提问**。
   「不记得了」「不清楚」「回头查一下」这类**答不上来但在语境里**的回应同样算 true，
   绝不能被当成「答非所问」（那会白白消耗追问配额、把话术变生硬）；
   只有闲聊（问天气、问吃饭）才算 false。
"""
from __future__ import annotations

from . import prompts
from .llm import classify_intent, get_llm, parse_json, query_intent_hit

# (用户原话, 期望意图, 是否已出过结论)
INTENT_CASES = [
    ("我要怎么才能知道自己缴费满没满呢", "query", True),
    ("我怎么查自己的参保记录", "query", True),
    ("在哪能看到我缴了多少年", "query", True),
    ("怎么看自己够不够10年", "query", False),
    ("我什么时候能退休", "retire", False),
    ("养老金到底该在哪个城市领", "location", False),
    ("谢谢，明白了", "followup", True),
    ("今天天气怎么样", "off_topic", False),
]

# (用户原话, 期望 in_context)
CONTEXT_CASES = [
    ("不记得了", True),
    ("不知道", True),
    ("想不起来了", True),
    ("回头我查一下", True),
    ("今天天气怎么样", False),
    ("你吃饭了吗", False),
]


def main():
    print("== ① 意图判定（模型） ==")
    print(f"{'用户原话':<34}{'判定':<10}{'期望':<10}{'规则兜底':<10}结果")
    print("-" * 76)
    ok = 0
    for text, expect, done in INTENT_CASES:
        got = classify_intent(text, stage_done=done)
        rule = "query" if query_intent_hit(text) else "-"
        ok += got == expect
        print(f"{text:<34}{got:<10}{expect:<10}{rule:<10}"
              f"{'OK' if got == expect else 'FAIL'}")
    print(f"意图判定 {ok}/{len(INTENT_CASES)}")

    print("\n== ② 「是否在语境里回应」判定（抽取节点输出 in_context） ==")
    print(f"{'用户原话':<34}{'in_context':<12}{'期望':<8}结果")
    print("-" * 66)
    ok2 = 0
    for text, expect in CONTEXT_CASES:
        out = parse_json(get_llm().chat(prompts.extract_gender_age_messages(text)))
        got = bool(out.get("in_context"))
        ok2 += got == expect
        print(f"{text:<34}{str(got):<12}{str(expect):<8}"
              f"{'OK' if got == expect else 'FAIL'}")
    print(f"in_context 判定 {ok2}/{len(CONTEXT_CASES)}")


if __name__ == "__main__":
    main()
