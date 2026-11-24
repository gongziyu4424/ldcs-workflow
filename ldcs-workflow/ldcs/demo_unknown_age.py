"""验证「用户不知道首次参保时间」分支：
- 明确说不记得 → 应给「粤省事查询」指引 + 说明需确认才能出结论（不再重复追问）
- 随后补上年龄 → 应正常出结论（证明没有卡死）

跑法：python -m ldcs.demo_unknown_age   （需本地服务在 8787）
"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8787"


def chat(msg: str, sid: str):
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps({"message": msg, "session_id": sid}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    text, stage, branch, fallback = [], None, None, 0
    with urllib.request.urlopen(req, timeout=60) as r:
        cur_event = None
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                cur_event = line[7:]
            elif line.startswith("data: "):
                payload = line[6:]
                if cur_event == "token":
                    try:
                        d = json.loads(payload)
                        text.append(d if isinstance(d, str) else d.get("t", ""))
                    except Exception:
                        text.append(payload)
                elif cur_event == "meta":
                    try:
                        d = json.loads(payload)
                        stage, branch = d.get("stage"), d.get("branch")
                    except Exception:
                        pass
                elif cur_event == "fallback":
                    fallback += 1
    return "".join(text), stage, branch, fallback


def run(title: str, turns: list, sid: str):
    print(f"\n=== {title} ===")
    for i, t in enumerate(turns, 1):
        txt, stage, branch, fb = chat(t, sid)
        print(f"[第{i}轮] 用户：{t}")
        print(f"        stage={stage} branch={branch} fallback={fb}")
        print(f"        AI：{txt.strip()}")
    return txt


if __name__ == "__main__":
    rid = uuid.uuid4().hex[:6]
    run(
        "省外 + 不记得参保时间（应给粤省事指引）",
        ["我想领广州退休金", "江西九江", "男的，我不记得什么时候参保的"],
        f"unk-male-{rid}",
    )
    time.sleep(0.5)
    run(
        "省外 + 不知道（应给粤省事指引）",
        ["我想领广州退休金", "湖南", "女的，不知道首次参保时间"],
        f"unk-female-{rid}",
    )
    time.sleep(0.5)
    run(
        "给完指引后补年龄（应出结论，不卡死）",
        ["我想领广州退休金", "江西九江", "不记得了", "女，38 岁首次参保"],
        f"unk-then-answer-{rid}",
    )
