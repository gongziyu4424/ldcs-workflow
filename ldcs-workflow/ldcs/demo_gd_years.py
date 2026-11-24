"""验证「省外户籍 + 省内参保满 10 年」新口径：
- 问性别/首次参保年龄/省内参保年数 三项
- 不知道参保了多少年 → 粤省事指引
- 满 10 年 → 结论 C（三条件）；不满 → 结论 D·年限不足

跑法：python -m ldcs.demo_gd_years   （需本地服务在 8787）
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


if __name__ == "__main__":
    rid = uuid.uuid4().hex[:6]
    run(
        "不知道参保了多少年 → 粤省事指引 → 补年数出结论",
        ["我想领广州退休金", "江西九江", "男的，25 岁参保，不知道参保了多少年", "查到了，参保了 12 年"],
        f"yr-unknown-{rid}",
    )
    time.sleep(0.5)
    run(
        "一次给全（25 岁参保 12 年）→ 结论 C",
        ["我想领广州退休金", "江西九江", "男，25 岁首次参保，在广东参保 12 年了"],
        f"yr-ok-{rid}",
    )
    time.sleep(0.5)
    run(
        "年数不足（25 岁参保 6 年）→ 结论 D·年限不足",
        ["我想领广州退休金", "湖南", "男的，25 岁参保，才参保 6 年"],
        f"yr-short-{rid}",
    )
