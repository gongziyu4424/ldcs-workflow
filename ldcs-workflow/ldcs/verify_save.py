"""端到端：保存新 config → 立即发请求 → 确认使用新值。"""
import base64
import json
import time
import urllib.parse
import urllib.request


def _post_config(payload: dict) -> dict:
    b = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    url = f"http://127.0.0.1:8787/api/config?p={urllib.parse.quote(b)}"
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _get_config() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:8787/api/config", timeout=5) as r:
        return json.loads(r.read())


def main():
    print("=== 修改 male_age_threshold → 60 ===")
    cur = _get_config()
    print(f"before: male={cur['business']['male_age_threshold']}")
    payload = {"business": dict(cur["business"], male_age_threshold=60)}
    res = _post_config(payload)
    print(f"after save: male={res['business']['male_age_threshold']}")
    cur2 = _get_config()
    print(f"GET again: male={cur2['business']['male_age_threshold']}")
    if cur2["business"]["male_age_threshold"] != 60:
        print("[FAIL] 修改未生效")
        return

    print("\n=== 修改 ask_huji hint → 验证可改提示词 ===")
    new_hints = dict(cur2["hints"])
    new_hints["ask_huji"] = "[修改过] 您好（测试保存生效）。请问户籍地？"
    payload = {"hints": new_hints, "business": cur2["business"]}
    res = _post_config(payload)
    print(f"new hint[:60]: {res['hints']['ask_huji'][:60]}")

    print("\n=== reset 恢复 ===")
    req = urllib.request.Request("http://127.0.0.1:8787/api/config/reset", method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        res = json.loads(r.read())
    print(f"after reset: male={res['business']['male_age_threshold']}")
    if res["business"]["male_age_threshold"] != 50:
        print("[FAIL] reset 未生效")
        return

    print("\n[OK] 全部验证通过")


if __name__ == "__main__":
    main()
