"""端到端：直接调 ldcs.config 写文件（模拟前端保存），再用 HTTP 跑对话看行为变化。

注意：保存路径不通过 HTTP（urllib 在 sandbox 下不稳定），改用 cfg_mod 直接写。
对话验证仍走 HTTP /api/chat/stream。"""
import sys, uuid
sys.path.insert(0, '/Users/gongziyu/WorkBuddy/工作流制作/ldcs-workflow')
from ldcs import config as cfg_mod
import json, http.client

HOST, PORT = '127.0.0.1', 8787
# 每次运行用随机 session 前缀，避免服务器内存里残留上一轮同 id 会话（stage=done）污染基线
RUN_ID = uuid.uuid4().hex[:6]


def chat(message, sid):
    conn = http.client.HTTPConnection(HOST, PORT)
    body = json.dumps({'message': message, 'session_id': sid}).encode()
    conn.request('POST', '/api/chat/stream', body, headers={'Content-Type': 'application/json'})
    r = conn.getresponse()
    state, full = {}, []
    ev = None
    for raw in r:
        line = raw.decode('utf-8', 'ignore').rstrip()
        if line.startswith('event:'):
            ev = line[6:].strip()
        elif line.startswith('data:'):
            d = line[5:].strip()
            try:
                v = json.loads(d)
            except Exception:
                continue
            if ev == 'meta':
                state = v
            elif ev == 'token':
                full.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
            elif ev == 'done':
                state.update(v)
    return state, ''.join(full)


def set_flow_edit(each_node_id='', port='', new_to=''):
    cfg = cfg_mod.get_config()
    flow = cfg.get('flow') or cfg_mod.get_default_flow()
    changed = False
    for e in flow['edges']:
        if e.get('from') == each_node_id and (e.get('port') or '') == port:
            e['to'] = new_to
            changed = True
    cfg['flow'] = flow
    cfg_mod.save_config(cfg)
    return changed


# 1) 基线
s1a, _ = chat('我想领广州退休金', f'{RUN_ID}-t1')
s2, r2 = chat('广州', f'{RUN_ID}-t1')
print(f"[基线] 第1轮 stage={s1a.get('stage')} 第2轮 stage={s2.get('stage')} reply头={r2[:50]}")

# 2) 改线：gz 出口接 ask_huji_retry
set_flow_edit('classify_huji', 'gz', 'ask_huji_retry')
chat('我想领广州退休金', f'{RUN_ID}-t2')
s2b, r2b = chat('广州', f'{RUN_ID}-t2')
print(f"[改线后] 第2轮 stage={s2b.get('stage')} reply头={r2b[:60]}")

# 3) 恢复
cfg_mod.reset_config()
chat('我想领广州退休金', f'{RUN_ID}-t3')
s2c, r2c = chat('广州', f'{RUN_ID}-t3')
print(f"[reset后] 第2轮 stage={s2c.get('stage')} reply头={r2c[:50]}")