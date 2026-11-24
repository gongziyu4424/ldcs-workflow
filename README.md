# ldcs-workflow
面向话务员/经办窗口的对话式测算工具，覆盖三个业务口径：
支线
说明
待遇领取地
按户籍、参保地、缴费年限判定在哪领养老金
退休时间
依据 2025-01-01 起施行的渐进式延迟退休决定，算退休时点与该时点的最低缴费年限
失业保险金
判定领取资格，算可领月数与总金额
形态是一套数据驱动的对话流程引擎：拓扑、话术、判定参数都可配置，改配置即时生效，不用改代码。
设计要点
- 识别与判定分层。意图与槽位识别交给模型；配额、门槛、户籍规则、必含事实由程序硬判定。排查问题时先分清是哪一层坏了。
- 业务口径只沉淀在程序与单测里。提示词和流程图都不写规则，避免两处维护、互相打架。
- 禁语表是硬的，不是靠提示词叮嘱。关键结论必须包含的事实、绝对不能出现的事实，都由 required_facts / forbidden_facts 校验，模型说错就换成程序兜底稿。
- 同一会话必须复用已收集信息，绝不重问已经问到的。
目录
server.py               HTTP + SSE 服务（演示台 + 配置页）
ldcs_mcp.py             MCP server（stdio），把三项测算暴露成工具
mcp_selftest.py         MCP 全链路自测（起子进程走 JSON-RPC）
ldcs/
  graph.py              每轮运行入口（委托 flow）
  flow.py               流程执行器：节点类型、抽取、工具调用、话术组装
  flow_default.py       默认拓扑（38 节点 / 64 边）与画布元数据
  config.py             配置集中管理（DEFAULT_CONFIG + config/workflow.json 覆盖）
  presets.py            话术覆盖层（default / agent_spec）
  state.py              会话状态结构与分支分类
  llm.py                模型接入（MockLLM 规则抽取 / OpenAI 兼容实现）
  retire_calc.py        退休时间测算程序
  unemp_calc.py         失业保险金测算程序
  nodes.py / prompts.py 节点实现与提示词组装
  test_cases.py         回归用例
web/                    演示台前端（单页 + 画布编辑器）
config/workflow.json    生效中的配置快照
快速开始
pip install -r requirements.txt     # 只有 python-dotenv
cp .env.example .env                # 不填也能跑，默认 mock 模式
python server.py                    # → http://127.0.0.1:8787
mock 模式用本地规则抽取，不联网不花钱，适合先跑通流程；真模型需要在 .env 里配 LLM_MODE=openai 与对应密钥。
测试
# 流程回归（mock 模式，不花钱）
LDCS_SKIP_DOTENV=1 LLM_MODE=mock python -m ldcs.test_cases

# MCP 全链路
python mcp_selftest.py
改过提示词或 required_facts 之后，必须再用真模型跑一遍，确认没有触发兜底。
MCP 接入
ldcs_mcp.py 是一个只用标准库的 stdio MCP server，暴露 5 个工具：
工具
用途
pension_chat
多轮问答，信息不全时一次只追问一句。后续轮次必须带 session_id
pension_status
只读查看某会话的进度与结论
pension_reset
清空会话
calc_retirement_time
无状态精确计算退休时间与最低缴费年限
calc_unemployment_benefit
无状态精确计算失业金资格与金额
客户端配置：
{
  "mcpServers": {
    "ldcs": {
      "command": "<python 绝对路径>",
      "args": ["<仓库绝对路径>/ldcs_mcp.py"]
    }
  }
}
会话状态落在 .mcp_sessions.json（已在 .gitignore 中排除），跨进程重启可续。
免责
测算结果依据公开政策口径实现，仅供经办辅助参考，最终以社保经办机构核定为准。政策调整时需同步更新 retire_calc.py / unemp_calc.py 及其单测。
