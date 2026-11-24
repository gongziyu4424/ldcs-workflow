"""验证：① 已给过的信息不再重问；② 出生年份×参保年份 → 自动换算年龄。

跑法（需本地服务在 8787 运行）：
    python -m ldcs.demo_birth_year
"""
import json
import http.client
import uuid

HOST, PORT = "127.0.0.1", 8787


def chat(msg, sid):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=60)
    body = json.dumps({"message": msg, "session_id": sid})
    conn.request("POST", "/api/chat/stream", body, {"Content-Type": "application/json"})
    resp = conn.getresponse()
    stage, reply, fallback = None, [], 0
    cur = None
    while True:
        line = resp.readline()
        if not line:
            break
        s = line.decode("utf-8").rstrip("\n")
        if s.startswith("event: "):
            cur = s[7:].strip()
        elif s.startswith("data: "):
            payload = s[6:]
            if cur == "meta":
                try:
                    stage = json.loads(payload).get("stage")
                except Exception:
                    pass
            elif cur == "token":
                try:
                    reply.append(json.loads(payload).get("t", ""))
                except Exception:
                    reply.append(payload)
            elif cur == "fallback":
                fallback += 1
    conn.close()
    return stage, "".join(reply).strip(), fallback


def run(title, turns):
    sid = "by-" + uuid.uuid4().hex[:6]
    print("=" * 70)
    print(title)
    for i, t in enumerate(turns, 1):
        stage, reply, fb = chat(t, sid)
        print(f"  [第{i}轮] 用户：{t}")
        print(f"          AI（stage={stage}, fallback={fb}）：{reply}")
    print()


if __name__ == "__main__":
    run("场景0 · 截图复现：两位数年份跨轮补齐（不问参保年数）", [
        "我想领广州退休金",
        "江西九江",
        "男，不记得什么时候参保的了",
        "查到了，是15年的时候",
        "我是95年的",
    ])
    run("场景1 · 截图复现：先给性别，再说查到参保年份+出生年份", [
        "我想领广州退休金",
        "江西九江",
        "男，不记得什么时候参保的了",
        "查到了，是2020年，我是1995年的",
    ])
    run("场景2 · 只给参保年份、不给出生年份/年龄（应要求补出生年份或年龄）", [
        "我想领广州退休金",
        "湖南",
        "男，我2020年才第一次在广东参保",
    ])
    run("场景3 · 直接给周岁（原有路径不受影响）", [
        "我想领广州退休金",
        "广西",
        "女，38岁首次参保，参保12年",
    ])
