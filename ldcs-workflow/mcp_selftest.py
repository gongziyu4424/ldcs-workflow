#!/usr/bin/env python3
"""ldcs_mcp.py 全链路自测 —— 真起子进程，走 stdio JSON-RPC，不花模型钱（mock 模式）。

用法：
    python mcp_selftest.py
    python mcp_selftest.py --real     # 用真模型（.env 里的 key），会真实产生调用
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

ok_all = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok_all
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  ← {detail}" if detail and not cond else ""))
    if not cond:
        ok_all = False


class Client:
    def __init__(self, env: dict):
        self.p = subprocess.Popen(
            [PY, str(ROOT / "ldcs_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(ROOT), env=env, text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        return json.loads(line)

    def call(self, name: str, args: dict):
        r = self.send("tools/call", {"name": name, "arguments": args})
        return r.get("result") or {"isError": True, "content": [{"type": "text", "text": str(r)}]}

    def stderr_tail(self) -> str:
        self.p.stdin.close()
        try:
            return self.p.stderr.read() or ""
        except Exception:
            return ""


def main() -> int:
    real = "--real" in sys.argv
    env = dict(os.environ)
    if not real:
        env["LDCS_SKIP_DOTENV"] = "1"
        env["LLM_MODE"] = "mock"
    print(f"=== 模式：{'真模型' if real else 'mock（不花钱）'} ===\n")

    c = Client(env)

    r = c.send("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "selftest", "version": "1.0"},
    })
    check("initialize 有 serverInfo", bool((r.get("result") or {}).get("serverInfo")),
          json.dumps(r, ensure_ascii=False)[:200])
    check("initialize 带 instructions", bool((r.get("result") or {}).get("instructions")))
    c.send("notifications/initialized", notify=True)

    r = c.send("tools/list")
    tools = (r.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    check("tools/list 返回 5 个工具", len(tools) == 5, f"实际 {len(tools)}: {names}")
    check("每个工具都有 inputSchema",
          all(t.get("inputSchema", {}).get("type") == "object" for t in tools))
    check("工具名齐全", set(names) >= {"pension_chat", "pension_status", "pension_reset",
                                    "calc_retirement_time", "calc_unemployment_benefit"}, str(names))

    r = c.send("ping")
    check("ping 正常", "result" in r)

    print("\n--- 无状态计算 ---")
    res = c.call("calc_retirement_time", {"birth_year": 1975, "birth_month": 5,
                                          "category": "female_worker"})
    sc = res.get("structuredContent") or {}
    check("退休测算·有结论", bool(sc.get("retire_cn")), res["content"][0]["text"][:200])
    check("退休测算·有最低缴费年限", bool(sc.get("min_contribution_cn")),
          sc.get("min_contribution_cn"))
    print(f"    {sc.get('birth_cn')} {sc.get('category_cn')} → {sc.get('retire_cn')} "
          f"（{sc.get('retire_age_cn')}），最低缴费 {sc.get('min_contribution_cn')}")

    res = c.call("calc_unemployment_benefit", {"paid_months": 60,
                                               "reason_code": "contract_end",
                                               "registered": False})
    sc = res.get("structuredContent") or {}
    check("失业金·算出月数", sc.get("months") is not None, str(sc)[:200])
    check("失业金·算出金额", sc.get("total") is not None, str(sc)[:200])
    check("未登记仍给金额（不是不符合资格）",
          sc.get("months") is not None and sc.get("qualified") is not False, str(sc)[:200])
    print(f"    缴费60个月 → {sc.get('months')}个月 / {sc.get('total_cn')}；qualified={sc.get('qualified')}")

    res = c.call("calc_unemployment_benefit", {"paid_months": 6, "reason_code": "voluntary"})
    sc = res.get("structuredContent") or {}
    check("缴费不满1年·判不符合", sc.get("qualified") is False, str(sc)[:200])

    print("\n--- 多轮对话 ---")
    res = c.call("pension_chat", {"message": "我在广州交了12年社保，1980年出生的"})
    sc = res.get("structuredContent") or {}
    sid = sc.get("session_id")
    reply1 = sc.get("reply") or ""
    check("首轮有 session_id", bool(sid), str(sc)[:200])
    check("首轮有回复", bool(reply1.strip()), str(sc)[:200])
    print(f"    sid={sid}\n    回复：{reply1[:100]}")

    res = c.call("pension_chat", {"message": "女的", "session_id": sid})
    sc2 = res.get("structuredContent") or {}
    check("次轮沿用同一 session_id", sc2.get("session_id") == sid, str(sc2)[:200])
    reply2 = sc2.get("reply") or ""
    print(f"    回复：{reply2[:100]}")
    if real:
        check("多轮不重问已答过的（非逐字相同）", reply1 != reply2, "两轮回复完全一样")

    res = c.call("pension_status", {"session_id": sid})
    sc3 = res.get("structuredContent") or {}
    check("status 能读到进度", bool(sc3.get("summary")), str(sc3)[:200])
    print(f"    进度：{json.dumps(sc3.get('summary', {}).get('facts'), ensure_ascii=False)}")

    res = c.call("pension_status", {"session_id": "不存在的号"})
    check("未知 session 报错而非崩", res.get("isError") is True, str(res)[:200])

    res = c.call("pension_reset", {"session_id": sid})
    check("reset 成功", (res.get("structuredContent") or {}).get("reset") is True, str(res)[:200])

    res = c.call("不存在的工具", {})
    check("未知工具返回 isError，不崩进程", res.get("isError") is True, str(res)[:200])

    err = c.stderr_tail()
    rc = c.p.wait(timeout=10)
    check("进程正常退出", rc == 0, f"returncode={rc}")
    check("stderr 有启动日志", "启动" in err, err[:300])

    print("\n" + ("=" * 46))
    print("全部通过 🎉" if ok_all else "有失败项，见上方 ❌")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
