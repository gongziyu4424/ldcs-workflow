# v6 画布化工作流 — 设计

## 目标
在 v5（config 可配置话术/阈值/必含事实，保存即生效）基础上，把"流程编排"本身也做成画布可配置：
- 左侧节点工具条：开始 / 处理(LLM话术) / 抽取(LLM) / 条件 / 代码 / 工具 / 结束
- 画布拖拽节点、连线（条件/代码节点右侧按出口标签出端口）
- 节点点击弹出配置（hint 覆盖 / fallback / 必含事实 / schema / 判定 kind）
- 保存 -> 写回 config/workflow.json 的 flow 段 -> 运行时引擎按 flow 拓扑执行（真生效）
- 恢复初始按钮 -> 回到 DEFAULT flow

## flow 数据结构
flow = {
  nodes: [{id, type, label, x, y, config:{...}}],
  edges: [{from, to, port, label}]
}
node.type: start | say | extract | condition | code | tool | end
- start: 入口容器（每轮从 start 或其后续开始）
- say  : LLM 话术节点。config.action 决定 scaffold（hint/fallback/required_facts）；
        可含 config.overrides {hint, fallback, required_facts} 覆盖顶层配置；
        可含 attempt_field（执行时自增，如 attempt_huji）；
        执行后若非终结 action 则 stage 置 config.stage_after，at=出边目标（等用户下一句）；
        若终结（conclusion/fallback/off_topic/followup）则 stage=done，at=None。
- extract: LLM 槽位抽取。config.kind = huji | gender_age
- condition/code: 程序判定。config.kind 决定判定函数与出口标签集合；
        出口经 edges.port 连线。kind 注册表见 flow.py REGISTRY。
- tool: 预留工具调用（当前 no-op 直通）
- end : 终节点，stage=done，不产生话术

## 执行语义 run_flow(state, user_input)
1. merged = new_state + state；cursor = state.at or flow 中 start 的后继首节点
2. 循环 max_step：
   - start: cursor=后继，continue
   - extract: 调 LLM 抽槽位写 state；cursor=后继
   - condition/code: 判定得到 (label, update)；state+=update；cursor=label 对应边的 to；
     若无边则兜底 scaffold=fallback, stage=done, break
   - say: 组装 scaffold（含 overrides/hint/attempt 自增）；写 stage/stage_after、at/at=None；
         _stream_scaffold=scaffold; break（本轮对外说一句即停）
   - tool: cursor=后继
   - end: stage=done; at=None; break（无 scaffold，回复空）
3. 若中途无出口边/死循环(step>max) → scaffold=fallback 兜底结束

## 默认 flow（镜像 v5 行为，保证回归一致）
start → route_intent(condition kind=route_intent)
  route_intent 出口: ask_huji | off_topic | followup | extract_huji | extract_ga
  - idle+命中->ask_huji ; idle+未命中->off_topic ; await_huji->extract_huji
  - await_gender_age->extract_ga ; done->followup
ask_huji(say action=ask_huji stage_after=await_huji) -> extract_huji
off_topic(say action=off_topic) -> end
followup(say action=followup) -> end
extract_huji(extract kind=huji) -> missing_huji(condition kind=missing_huji)
  missing_huji 出口 ok/miss ; miss-> huji_attempt(condition kind=attempt_left field=attempt_huji)
  huji_attempt 出口 retry/giveup ; retry -> ask_huji_retry(say action=ask_huji_retry attempt_field=attempt_huji) -> extract_huji
  giveup -> fallback(say action=fallback) -> end ; ok -> classify_huji(condition kind=classify_huji)
  classify_huji 出口 gz/gd_other/other_prov
  gz -> conclude_gz(say action=conclusion branch=gz) -> end
  gd_other -> conclude_gd(say action=conclusion branch=gd_other) -> end
  other_prov -> ask_ga(say action=ask_gender_age stage_after=await_gender_age) -> extract_ga
extract_ga(extract kind=gender_age) -> check_ga(condition kind=check_ga)
  check_ga 出口 ok/miss ; miss -> ga_attempt(condition kind=attempt_left field=attempt_gender_age)
  retry -> ask_ga_retry(say action=ask_gender_age_retry attempt_field=attempt_gender_age) -> extract_ga
  giveup -> fallback -> end ; ok -> judge_age(condition kind=judge_age)
  judge_age 出口 qualify/unqualify
  qualify -> conclude_q(say conclusion other_qualify) -> end
  unqualify -> conclude_u(say conclusion other_unqualify) -> end

## 判定 kind 注册表（condition/code 共用）
route_intent : 依据 stage+user_input 返回出口（含意图正则）
missing_huji  : province&city 是否齐全 -> ok/miss
classify_huji : classify_branch -> gz/gd_other/other_prov
attempt_left  : config.field 对应 attempt 计数 < 对应上限 -> retry/giveup
check_ga      : gender/first_ins_age/非歧义 齐全 -> ok/miss
judge_age     : judge_qualify -> qualify/unqualify
（可扩展；新增判定 = 加一个 kind 函数）

## 落盘
config/workflow.json 顶层新增 "flow" 段；顶层 hints/fallback_texts/required_facts 保留作
action 默认；节点 overrides 覆盖单节点行为。
