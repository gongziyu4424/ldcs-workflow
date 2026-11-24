"""待遇领取地测算 · 演示台 本地服务（v4 模型问答版）。

接口：
  GET  /                          前端页面
  POST /api/chat                  非流式；返回 {session_id, reply, 状态字段}
  POST /api/chat/stream           SSE 流式；先 meta、再 token*、再 done
  POST /api/chat/reset            清空会话状态

会话状态仅存进程内存。模型按 .env 配置：默认已接 DeepSeek V4 Flash。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ldcs import config as cfg_mod, graph, llm as llm_mod
from ldcs.state import new_state

WEB_DIR = ROOT / "web"
# 部署环境通过 PORT 注入端口，并要求绑定 0.0.0.0；本地运行默认同样监听全部网卡。
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8787"))

# 前端 HTML 动态读取（带 mtime 缓存）：改 web/index.html 后刷新即可，无需重启服务
_index_html_cache: str | None = None
_index_html_mtime: float = 0.0


def get_index_html() -> str:
    global _index_html_cache, _index_html_mtime
    p = WEB_DIR / "index.html"
    mtime = p.stat().st_mtime if p.exists() else 0.0
    if _index_html_cache is None or mtime != _index_html_mtime:
        _index_html_cache = p.read_text(encoding="utf-8")
        _index_html_mtime = mtime
    return _index_html_cache

PUBLIC_KEYS = [
    "stage", "ask_topic", "province", "city", "gender", "first_ins_age", "age_ambiguous",
    "birth_year", "first_insured_year", "gd_years",
    "branch", "judge", "attempt_huji", "attempt_gender_age", "attempt_retire",
    "ga_progress", "retire_progress", "huji_progress",
    "ga_in_context", "retire_in_context", "huji_in_context",
    # 退休时间测算支线（同一会话要记住，故一并透出）
    "retire_birth_year", "retire_birth_month", "retire_category",
    "retire_missing", "retire_result",
    # 失业保险金测算支线
    "unemp_paid_months", "unemp_reason", "unemp_registered",
    "unemp_missing", "unemp_result",
    "attempt_unemp", "unemp_progress", "unemp_in_context",
]

sessions: dict[str, dict] = {}


def _apply_fallback_after_stream(state: dict, scaffold: dict, accumulated_chunks: list[str]):
    """流式收尾时，若必含事实缺失，附加 fallback。"""
    required = scaffold.get("required_facts") or []
    fallback = (scaffold.get("fallback_text") or "").strip()
    full = "".join(accumulated_chunks).strip()
    if required and fallback and not llm_mod.fact_regression(required, full):
        # 标个特殊 marker 给前端识别
        return f"\n\n[[FALLBACK]]\n{fallback}"
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # 让客户端按 Content-Length 读取；避免 keep-alive 引用 1.0 buffer

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        # 写完后手动 close 套接字，绕过 BaseHTTPRequestHandler 内部 flush 在我们的关闭后再次引发的错误
        try:
            self.wfile.write(body)
        finally:
            try:
                self.connection.shutdown(1)  # SHUT_WR
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass

    # ---------- 绕过 BaseHTTPRequestHandler.rfile.read() 的方案 ----------
    # 经验：本环境内 rfile.read(n) 会 hang 收不全 POST body。
    # workaround：对路径 /api/config/[save|reset] 走自定义 handle，完全不读 request body。
    # /api/config/save 通过 query string 接收整段 JSON（urlencoded），
    # 客户端 base64 后放 ?p=... 参数；这样 BaseHTTPRequestHandler 只需要读 URL 不需要读 body。
    def _handle_config_via_query(self):
        """接受 URL ?p=<base64(json)> 的配置写入（在 sandbox 内 rfile.read hang 时的绕行）。

        客户端也可以用普通 POST 配合 query string（不读 body）。返回最终生效的 config。
        """
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        qs = parse_qs(u.query or "")
        payload_b64 = (qs.get("p") or [""])[0]
        import base64
        try:
            raw = base64.b64decode(payload_b64).decode("utf-8")
        except Exception as e:
            self._send_json(400, {"error": f"bad payload: {e}"})
            return
        try:
            payload = json.loads(raw)
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return
        final = cfg_mod.save_config(payload)
        self._send_json(200, final)

    def _send_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _emit(self, event: str, data):
        # 统一 JSON 序列化：字符串也包引号（data: "您"），前端统一 JSON.parse 无特判
        data = json.dumps(data, ensure_ascii=False)
        chunk = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except BrokenPipeError:
            raise

    def do_GET(self):
        from urllib.parse import urlparse
        path_only = urlparse(self.path).path
        if path_only in ("/", "/index.html"):
            body = get_index_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # 禁止缓存：否则重新部署后浏览器仍用旧的 index.html / config.js
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path_only in ("/config.js", "/canvas.js"):
            p = WEB_DIR / "config.js"
            body = p.read_bytes() if p.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path_only == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif path_only == "/api/config":
            self.do_GET_config()
            return
        elif path_only == "/api/flow/meta":
            self.do_GET_flow_meta()
            return
        elif path_only == "/api/presets":
            self.do_GET_presets()
            return
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        from urllib.parse import urlparse
        path_only = urlparse(self.path).path
        # body-绕过：先看路径，config 走 query string 不读 body
        if path_only == "/api/config":
            # 有 ?p= 参数时走 query 兼容路径；否则走普通 body POST
            if "p=" in (urlparse(self.path).query or ""):
                self._handle_config_via_query()
            else:
                self.do_POST_config_save()
            return
        if path_only == "/api/config/reset":
            self.do_POST_config_reset()
            return
        if path_only == "/api/config/preset":
            self.do_POST_config_preset()
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send_json(400, {"error": "bad json"})
            return

        if self.path == "/api/chat/reset":
            sid = payload.get("session_id")
            if sid:
                sessions.pop(sid, None)
            self._send_json(200, {"ok": True})
            return

        if self.path == "/api/chat":
            sid = payload.get("session_id") or uuid.uuid4().hex[:12]
            msg = str(payload.get("message") or "").strip()
            if not msg:
                self._send_json(400, {"error": "empty message"})
                return
            state = sessions.get(sid)
            if state is None:
                state = dict(new_state())
            state = graph.run_once(state, msg)

            # 拿到本轮的 _stream_scaffold → 同步调模型生成回复
            scaffold = state.get("_stream_scaffold")
            if scaffold:
                reply = llm_mod.generate_reply(scaffold).strip()
                # 必含事实缺失已自动回退到 fallback_text（generate_reply 内部处理）
                state["reply"] = reply
                if scaffold.get("action") in ("conclusion", "fallback", "off_topic", "followup"):
                    state["stage"] = "done"
            else:
                state["reply"] = state.get("reply") or ""
            sessions[sid] = state
            pub = {k: state.get(k) for k in PUBLIC_KEYS}
            self._send_json(200, {"session_id": sid, "reply": state.get("reply"), **pub})
            return

        if self.path == "/api/chat/stream":
            sid = payload.get("session_id") or uuid.uuid4().hex[:12]
            msg = str(payload.get("message") or "").strip()
            if not msg:
                self._send_json(400, {"error": "empty message"})
                return
            state = sessions.get(sid)
            if state is None:
                state = dict(new_state())
            state = graph.run_once(state, msg)

            self._send_sse()
            base_pub = {k: state.get(k) for k in PUBLIC_KEYS}
            meta = {**base_pub, "session_id": sid}

            try:
                scaffold = state.get("_stream_scaffold")
                self._emit("meta", meta)
                if scaffold:
                    # 真流式：调 LLM.stream_generate_reply，逐 token 推
                    chunks: list[str] = []
                    fallback_appended = False
                    for chunk in llm_mod.stream_generate_reply(scaffold):
                        if chunk.startswith("__FALLBACK_MARKER__") and not fallback_appended:
                            fb = chunk[len("__FALLBACK_MARKER__"):]
                            self._emit("fallback", fb)
                            fallback_appended = True
                            state["reply"] = ("".join(chunks).strip() + "\n" + fb).strip()
                        else:
                            chunks.append(chunk)
                            self._emit("token", chunk)
                    if not state.get("reply"):
                        state["reply"] = "".join(chunks).strip() or scaffold.get("fallback_text", "")
                    # 关键：仅当本轮是结论/兜底/off_topic/followup 时才把 stage 标 done
                    # 其他（ask_huji / ask_gender_age / retry）保留 n_xxx 写入的 stage
                    if scaffold.get("action") in ("conclusion", "conclusion_retire",
                                                  "conclusion_unemp",
                                                  "fallback", "off_topic", "followup"):
                        state["stage"] = "done"
                else:
                    # 极端情况：没有 _stream_scaffold（n_intent 第二次返 hit 时也可能）
                    reply = state.get("reply") or ""
                    state["reply"] = reply
                    if reply:
                        step = 10
                        for i in range(0, len(reply), step):
                            self._emit("token", reply[i:i + step])

                sessions[sid] = state
                done_pub = {k: state.get(k) for k in PUBLIC_KEYS}
                done_pub["session_id"] = sid
                self._emit("done", done_pub)
            except BrokenPipeError:
                pass
            except Exception as e:
                self._emit("error", {"message": str(e)})
            return

        self._send_json(404, {"error": "not found"})

    def do_GET_config(self):
        """GET /api/config —— 返回当前生效配置。"""
        self._send_json(200, cfg_mod.get_config())

    def do_GET_flow_meta(self):
        """GET /api/flow/meta —— 画布编辑器元数据（节点类型/端口/动作清单）。"""
        from ldcs.flow_default import FLOW_META
        self._send_json(200, FLOW_META)

    def do_GET_presets(self):
        """GET /api/presets —— 版本预设清单 + 当前生效项。"""
        from ldcs.presets import list_presets
        self._send_json(200, {
            "presets": list_presets(),
            "active": cfg_mod.get_preset_name(),
        })

    def _read_body(self) -> bytes:
        """读 POST body（循环读，绕开反向代理下 read1 只返单 chunk 的问题）。"""
        import time as _time
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        raw = b""
        if n > 0:
            deadline = _time.time() + 3.0
            while len(raw) < n:
                try:
                    want = min(n - len(raw), 65536)
                    chunk = (self.rfile.read1(want) if hasattr(self.rfile, "read1")
                             else self.rfile.read(want))
                except Exception:
                    break
                if not chunk:
                    if _time.time() > deadline:
                        break
                    _time.sleep(0.02)
                    continue
                raw += chunk
        return raw

    def do_POST_config_preset(self):
        """POST /api/config/preset —— 切换版本预设 {preset: 'default'|'agent_spec'}。"""
        try:
            payload = json.loads(self._read_body() or b"{}")
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return
        name = str((payload or {}).get("preset") or "").strip()
        if not name:
            self._send_json(400, {"error": "missing preset"})
            return
        try:
            final = cfg_mod.apply_preset(name)
            self._send_json(200, final)
        except KeyError:
            self._send_json(400, {"error": f"unknown preset: {name}"})
        except Exception as e:
            import traceback as _tb
            print("[config preset error]", _tb.format_exc(), flush=True)
            self._send_json(500, {"error": f"preset failed: {e}"})

    def do_POST_config_save(self):
        """POST /api/config —— 保存新配置；返回保存后生效的 config。"""
        import sys as _sys
        _sys.stderr.write(f"[config] POST hit, content-length={self.headers.get('Content-Length')}\n"); _sys.stderr.flush()
        try:
            raw = self._read_body()
            _sys.stderr.write(f"[config] read {len(raw)} bytes\n"); _sys.stderr.flush()
            payload = json.loads(raw or b"{}")
        except Exception as e:
            self._send_json(400, {"error": f"bad json: {e}"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "payload must be a JSON object"})
            return
        try:
            _sys.stderr.write("[config] calling save_config\n"); _sys.stderr.flush()
            final = cfg_mod.save_config(payload)
            _sys.stderr.write(f"[config] saved, returning {len(json.dumps(final))} bytes\n"); _sys.stderr.flush()
            self._send_json(200, final)
            _sys.stderr.write("[config] response sent\n"); _sys.stderr.flush()
        except Exception as e:
            import traceback as _tb
            print("[config save error]", _tb.format_exc(), flush=True)
            self._send_json(500, {"error": f"save failed: {e}"})

    def do_POST_config_reset(self):
        """POST /api/config/reset —— 恢复到 DEFAULT_CONFIG，返回恢复后的配置。"""
        try:
            final = cfg_mod.reset_config()
            self._send_json(200, final)
        except Exception as e:
            import traceback as _tb
            print("[config reset error]", _tb.format_exc(), flush=True)
            self._send_json(500, {"error": f"reset failed: {e}"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"待遇领取地测算演示台: http://127.0.0.1:{PORT}  (bind {HOST})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
