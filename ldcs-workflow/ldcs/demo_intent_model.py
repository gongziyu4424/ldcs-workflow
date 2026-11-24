"""真机演示：模型判意图 vs 正则判意图（LLM_MODE=openai 下运行）。

用法：
    cd ldcs-workflow && LLM_MODE=openai python -m ldcs.demo_intent_model

用例特意选「正则抓不到的口语说法」，用来证明意图应由模型语义判断：
    「我啥时候能不上班了」「哪年才能领上钱」——正则里没有「退休」二字就漏判。
"""
from __future__ import annotations

from .flow import intent_hit, retire_intent_hit
from .llm import classify_intent

CASES = [
    # (用户原话, 期望意图)
    ("我啥时候能不上班了", "retire"),
    ("还要干到哪年啊", "retire"),
    ("哪年才能领上钱", "retire"),
    ("我多大年纪能领养老金", "retire"),
    ("我这种情况能不能在广州办退休", "location"),
    ("养老金到底该在哪个城市领", "location"),
    ("帮我查下医保报销的事", "off_topic"),
]


def main():
    print(f"{'用户原话':<22}{'模型判定':<12}{'期望':<10}{'正则判定':<10}结果")
    print("-" * 70)
    ok = rule_ok = 0
    for text, expect in CASES:
        got = classify_intent(text, stage_done=False)
        rule = ("retire" if retire_intent_hit(text)
                else ("location" if intent_hit(text) else "off_topic"))
        ok += got == expect
        rule_ok += rule == expect
        print(f"{text:<22}{got:<12}{expect:<10}{rule:<10}"
              f"{'OK' if got == expect else 'FAIL'}")
    print("-" * 70)
    print(f"模型判意图 {ok}/{len(CASES)}    （对照）正则判意图 {rule_ok}/{len(CASES)}")


if __name__ == "__main__":
    main()
