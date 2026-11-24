"""验证「只判断是否广东 / 是否广州」的新口径：
- 潮汕人 → 应直接给「省内非广州」结论，不再追问地市
- 只答「广东」→ 追问一次，且问的是「是不是广州户籍」
- 广州 → 广州户籍结论
跑法：python -m ldcs.demo_huji_alias   （需本地服务在 8787）
"""
from __future__ import annotations

import json
import sys
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
                        stage = d.get("stage")
                        branch = d.get("branch")
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
        print(f"        AI：{txt.strip()[:160]}")


if __name__ == "__main__":
    rid = uuid.uuid4().hex[:6]
    run("潮汕人（俗称，应直接出结论）", ["我想领广州退休金", "潮汕人"], f"alias-chaoshan-{rid}")
    time.sleep(0.5)
    run("只说广东（应追问是否广州）", ["我想领广州退休金", "广东"], f"alias-gd-{rid}")
    time.sleep(0.5)
    run("广州（应直接出广州结论）", ["我想领广州退休金", "广州"], f"alias-gz-{rid}")
    time.sleep(0.5)
    run("粤西（俗称）", ["我想领广州退休金", "粤西的"], f"alias-yuexi-{rid}")
