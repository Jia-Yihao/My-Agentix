<div align="center">
  <img
    src="./docs/assets/agentix-logo.png"
    alt="My Agentix Logo"
    width="360"
  />

  <h3>My Agentix</h3>
  <p>Production-oriented Agent Platform</p>

  <p>三层 Multi-Agent Runtime · Agent Core 2.0 · 多路知识检索 · Control Plane · 评测回归 · 可观测</p>

  ![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12-3776AB?style=flat&logo=python&logoColor=white)
  ![FastAPI](https://img.shields.io/badge/FastAPI-0.142-005571?style=flat&logo=fastapi&logoColor=white)
  ![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=ffffff)
  ![LangGraph](https://img.shields.io/badge/LangGraph-0.2.60-1A5F4F?style=flat&logo=databricks&logoColor=white)
  ![GraphRAG](https://img.shields.io/badge/GraphRAG-KG-blueviolet?style=flat)
  ![Agent](https://img.shields.io/badge/Agent-System-orange?style=flat)
  ![MCP](https://img.shields.io/badge/MCP-Client%20%2B%20Server-8A2BE2?style=flat)
  ![Vue](https://img.shields.io/badge/Vue-3-4FC08D?style=flat&logo=vuedotjs&logoColor=white)
  ![Neo4j](https://img.shields.io/badge/Neo4j-5.27-008CC1?style=flat&logo=neo4j&logoColor=white)
  ![Milvus](https://img.shields.io/badge/Milvus-2.3-00A9E0?style=flat&logo=milvus&logoColor=white)
  [![License](https://img.shields.io/badge/License-Apache%202.0-yellow?style=flat)](LICENSE)
  [![CI](https://github.com/Jia-Yihao/Legelbot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Jia-Yihao/Legelbot/actions/workflows/ci.yml)
  [![CodeQL](https://github.com/Jia-Yihao/Legelbot/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/Jia-Yihao/Legelbot/actions/workflows/codeql.yml)
  ![Version](https://img.shields.io/badge/version-0.2.0-blue?style=flat)
  ![Ruff](https://img.shields.io/badge/lint-ruff-261230?style=flat&logo=ruff&logoColor=white)
  [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat)](CONTRIBUTING.md)
</div>

---

## 目录

- [1. 平台概览](#1-平台概览)
- [2. 为什么选择 Agentix](#2-为什么选择-agentix)
- [3. 架构](#3-架构)
- [4. Agent Core 2.0](#4-agent-core-20)
- [5. Agent Runtime](#5-agent-runtime)
- [6. 能力模块](#6-能力模块)
- [7. 可靠性](#7-可靠性)
- [8. 评测](#8-评测)
- [9. 可观测](#9-可观测)
- [10. 快速开始](#10-快速开始)
- [11. 工程实践](#11-工程实践)
- [12. 项目结构](#12-项目结构)
- [13. 技术栈](#13-技术栈)
- [14. 文档](#14-文档)
- [15. 性能与成本（待实测）](#15-性能与成本待实测)
  - [15.1 本地法律语料检索对照](#151-本地法律语料检索对照2026-10-11)
- [16. 已知限制](#16-已知限制)
- [参与贡献](#参与贡献)
- [许可证](#许可证)
- [联系方式](#联系方式)

---

## 1. 平台概览

My Agentix 是一套**生产导向的 Agent 平台**——以 Agent 内核为核心、集成 RAG 等多路检索的综合服务平台，把构建、运行、治理智能体所需的内核能力做在同一套代码里：

| 能力域 | 内容 |
|--------|------|
| **Agent Core 2.0** | 规划 / 执行 / 观测 / 验证 / 纠偏 / 记忆 / HITL / 事件 / 多智能体 / 演化 |
| **Agent Runtime** | 运行状态 / 步骤轨迹 / 四维预算 / 生命周期 |
| **Control Plane** | spawn · status · cancel，取消可真正打断 asyncio Task |
| **ToolExecutor** | 权限 → 风险 → 超时 → 重试 → 轨迹，统一治理 |
| **MCP** | Client + Server，可挂载任意 MCP 工具进程 |
| **知识系统** | 图谱 / 向量 / SQL / 社区摘要多路检索与兜底 |
| **Adaptive Execution** | 档位路由 / fast-path / 准入排队 / 重试预算 / 负载甩载 / 隔离舱 / 单飞合流（全部 flag 灰度、默认基线等价） |
| **评测与门禁** | 黄金集回归、质量门禁、轨迹前缀回归、延迟基准与 CI 阻塞性能冒烟 |
| **可观测** | 步骤级轨迹、Explanation API、SLO 与告警、Prometheus 指标、OTel GenAI 语义 span |

**平台定位**：它不是「某个垂直领域的问答机器人」，而是一套**领域无关的 Agent 平台内核**——领域知识（图谱 Schema、提示词、评测集、数据）通过适配层接入，同一套内核可迁移到咨询 / 客服 / 政务等垂直领域，平台本身与具体业务域解耦。

**运行形态**：对外暴露 FastAPI 服务（SSE 流式对话 + 检索/任务级 Run API），LLM 编排由 LangGraph 三层 Multi-Agent 承载，Agent Core 2.0 提供可独立复用的内核（Harness + Capabilities），并以 `agentix/runtime` 收敛运行时真源。

---

## 2. 为什么选择 Agentix

- **可控制的执行**：每个 Agent 运行有 `run_id`、步数 / LLM / 耗时预算与超时，可查询、可取消
- **可恢复的失败**：错误分类 → 退避重试 → 模型 / 工具 Fallback 链 → 降级拒答，绝不悬挂
- **可承受的过载**：准入排队（超限 429）→ 重试预算 → 负载甩载 → 隔离舱，过载时礼貌拒绝而非雪崩（Phase H，全 flag 灰度）
- **可评估的质量**：102 条分层 golden 回归 + 声称一致性 / 提示敏感性 / 隐私标记（Phase 31）+ 质量门禁（不低于基线 -2%）+ 轨迹前缀回归 + 延迟基准（CI 阻塞性能冒烟）
- **可观测的轨迹**：步骤级轨迹落盘可回放、前端执行轨迹时间线、Explanation API、SLO 与告警、OTel GenAI 语义属性（token 用量归模型）
- **可感知的流式**：50ms 批窗渲染、智能吸底、Markdown 消毒即时渲染、流式阶段步进与工具卡、停止/重新生成全链（Phase H W6）
- **可扩展的边界**：AST 分层依赖守卫 + 架构守卫测试，违反即 CI 失败；注册表可插拔，新增工具 / Provider / 子图不改调用链
- **平台与领域解耦**：图谱 Schema / 提示词 / 评测集 / 数据全部走适配层，换领域不换内核

---
## 3. 架构

<div align="center">
  <img src="./docs/assets/architecture.png" alt="My Agentix 平台整体架构图" width="100%">
  <p><i>三层 Multi-Agent 编排（主路由层 → 多工具子图层 → 原子工具层），Agent Core 2.0 / Agent Runtime / Control Plane / MCP / 可观测构成运行时内核与横切能力</i></p>
</div>

### 三层 Multi-Agent 请求链路

| 层级 | 职责 | 核心技术 |
|-----|------|---------|
| **L1 主路由层** | 意图识别与任务分发 | 启发式 + LLM 双保险路由 |
| **L2 多工具子图层** | 任务分解与工具编排 | LangGraph Subgraph + Map-Reduce |
| **L3 原子工具层** | 具体工具执行 | Neo4j / Milvus / PostgreSQL / MySQL / LightRAG / 外部搜索 / MCP Server |

### 运行时内核与横切能力

| 组成 | 职责 | 落地 |
|------|------|------|
| **Agent Core 2.0** | 主循环 / 规划 / 纠偏 / 验证 / 记忆 / HITL / 事件 / 多智能体 / 演化 | `agentix/orchestration/` + `agentix/policies/` + `agentix/agent/` + `agentix/tools/` |
| **Agent Runtime** | 运行状态 / 步骤轨迹 / 四维预算 / 生命周期 | `agentix/runtime/` |
| **Control Plane** | 运行登记（有界 FIFO）+ 取消（打断 asyncio Task） | `agentix/runtime/control_plane.py` |
| **性能治理** | 分段延迟 / 预算 / 准入排队 / 过载降级 | `agentix/performance/` |
| **MCP** | 任意 MCP 工具进程接入 + 零依赖 stdio Server | `agentix/capabilities/mcp/` |
| **可观测** | trace_id 全链路 · 步骤轨迹 · Prometheus 指标 · SLO | `agentix/observability/` |
| **评测与门禁** | 黄金集回归 · 质量门禁 · 轨迹前缀回归 | `evals/` |

### 终态统一执行链（Stage A/B/M 收敛后）

```text
API / SSE / Run API（仅协议转换）
→ 应用层唯一入口创建 Run（ControlPlane 准入 · 单 owner，取消真正打断 asyncio Task）
→ 唯一执行循环（L2 LangGraph 主图 或 L3 AgentLoop，AGENT_LOOP_ENABLED 灰度）
   Context → Plan → Execute → Observe → Verify（Replan 受 Runtime 预算约束）
→ Runtime（agentix/runtime/：状态 / 步骤 / 四维预算 / 生命周期 / 统一退出契约）
→ Tool Gateway（agentix/tools/gateway/：权限 / 风险 / 超时 / 重试 / HITL / 审计）
→ Capabilities（tools/ · retrieval/ · kg 子图 · capabilities/mcp · memory-tools）
→ Infrastructure（infrastructure/：Milvus / Neo4j / Redis · LLM / 外部服务）
```

LangGraph 只是编排实现（状态图 / 条件边 / 并行节点 / 恢复点），SSE / Run API /
轨迹 / 评测消费同一条 Run/Event 真源。

### 一次请求的执行路径

1. 用户提问 → 中间件链（限流 → trace_id → 请求日志 → APIKey 鉴权 → CORS）→ ChatService
2. LangGraph 主图 `lg_builder.build_graph` → `message_window` 历史窗口化（条数窗口 + `LG_TOKEN_HISTORY_BUDGET` token 级预算，默认 0=关；ChatService 侧可注入级联指令块 `LG_INSTRUCTIONS_ENABLED` 与状态尾注 `LG_STATUS_TAIL_ENABLED`，均默认关）
3. **L1 主路由** `analyze_and_route_query`（启发式 + LLM 双保险）分发意图：`general / additional / graphrag / kb / image / file / text2sql`（`TASK_EXEC_ENABLED` 开启后另追加 `task-query` 任务型路由）
4. **L2 子图**（`graphrag` / `kb`）内执行：`guardrails → planner → tool_selection → 并行工具 → summarize / finalize`
5. **L3 原子工具**并行检索：Neo4j Cypher / Milvus 向量 / LightRAG 社区摘要 / Text2SQL / 外部搜索 / MCP 远端工具
6. 结果融合并附来源引用，经 Agent Core 主循环记账（AgentState / Steps / Budget），轨迹落盘可回放

完整架构图、设计特色与逐节点流程见 **[架构详解](docs/architecture/ARCHITECTURE_DETAIL.md)**；分层与依赖规则见 **[layers.md](docs/architecture/layers.md)**；运行时职责五层见 **[agent-core-layers.md](docs/architecture/agent-core-layers.md)**；逐环节「为何用自主 Agent 而非 Workflow」的论证见 **[orchestration-rationale.md](docs/design/orchestration-rationale.md)**。

---

## 4. Agent Core 2.0

`agentix/orchestration/`（orchestrator 主循环 + executor + langgraph 编排适配 + strategies/multi_agent）与 `agentix/policies/`（planner / verifier / hitl / guardrails 策略件）构成可独立复用的 Agent 内核，`agentix/agent/` 为 Context Provider 家（context / memory / events / observer / skills / hooks / evolution），`agentix/tools/` 是工具唯一执行入口；LangGraph 编排集中在 `agentix/orchestration/langgraph/`（lg_builder 主图 + lg_supervisor 拓扑）与 `agentix/application/agents/`（kg 子图 / text2sql / 业务 prompt），受 p66 导入白名单棘轮（仅登记在册 10 文件）约束，其余模块零 LangGraph 依赖。

<div align="center">
  <img src="./docs/assets/agent-core-architecture.png" alt="My Agentix Agent Core 2.0 与运行时内核架构图" width="100%">
  <p><i>主循环 Task → Context → Plan → Execute → Observe → Verify，失败由 Replanner 产出 Append-only 新 Plan；Runtime 提供状态 / 步骤 / 预算 / 控制平面治理</i></p>
</div>

| 组件 | 路径 | 说明 |
|------|------|------|
| **主循环** | `agentix/orchestration/orchestrator.py` | Task → Context → Plan → Execute → Observe → Verify；步数预算与四层故障 |
| **规划 / 纠偏** | `agentix/policies/planner/`（simple · retry，M3 归并 replanner） | 单步 / 声明式规划；失败产出 Append-only 新 Plan，连续同因熔断 |
| **执行 / 观测** | `agentix/orchestration/executor.py` · `observer/` | Protocol 注入工具编排；Append-only Observation |
| **验证** | `agentix/policies/verifier/` | L1 结构 + L2 completion（L3 可选）分层验收 |
| **上下文** | `context/` | Static Prefix + 轨迹、状态栏白名单、确定性截断与隔离引用；`CONTEXT_AUTOCOMPACT` 开启后按五要素保留（trace_id / 错误记录 / 约束 / 引用 / 任务状态）做 L4 自动压缩（`context/compaction.py`，默认关） |
| **记忆** | `memory/` | 3 scope × 5 kind，读时冲突裁决（全 tags 键 + 30 天时间窗，窗内标记进 `meta.conflicts` 不静默），写入 PII 脱敏（中英姓名/地址，读路径二次兜底）；`MEMORY_PROFILE_CARD_ENABLED` 开启后增量折入结构化画像卡（`profile_card`：派生幂等、同键冲突只标记不覆写） |
| **HITL Sidecar** | `agentix/policies/hitl/sidecar.py` | Reviewer 只见结构化参数；Approve / Reject / Edit / Resume；无 approver 高风险 fail-closed |
| **事件总线** | `events/adapter.py` | 去重、双时刻、重试交付、虚拟身份边界；`EVENT_PORT_BACKEND` 三后端：`memory`（默认）/ `jsonl`（重启恢复）/ `redis_stream`（Phase 23，Redis Stream，XADD 投递 + SET NX 跨进程去重），`GET /api/v1/ops/events` 可查询 |
| **多智能体** | `orchestration/strategies/multi_agent/` | Peer + Manager；六失败模式检测（deadlock / livelock / echo chamber / budget bleed / infinite escalation / contradiction） |
| **演化回路** | `evolution/` | 在线录制提案轨迹供离线回放与回归；`KNOWLEDGE`/`PROGRAM` 两载体（payload 白名单 fail-closed）+ 提案三状态 append-only 落盘（`EVOLUTION_PROPOSAL_LOG`）+ 睡眠学习（`EVOLUTION_SLEEP_ENABLED`，只产提案不晋升） |
| **端口契约** | `agentix/domain/agent/ports.py` | Context / Planner / ToolSelector / Verifier / Memory / Event / Rollback Protocol（M0 拆入；graph / repository / retrieval Protocol 留 `domain/ports/`） |

五层职责与依赖硬规则见 **[Agent Core 五层架构](docs/architecture/agent-core-layers.md)**；离线走查见 **[Research Agent Demo](docs/guides/research-agent-demo.md)**；迁移清单与勾选状态见 `docs/migration/module-inventory.md`。

**依赖边界（CI 强制）**：`domain/` 零第三方；LangGraph 导入受 p66 白名单棘轮（equality 断言，仅登记在册 10 文件，新增即红）；`capabilities/` 不反向依赖业务循环；Runtime 真源在 `agentix/runtime/`（Strangler shim `application/runtime/` 已随 A4 删除）。

---

## 5. Agent Runtime

超越 Chatbot 的运行时内核（`agentix/runtime/`，接线在 `agentix/application/services/`）：

| 组件 | 说明 |
|------|------|
| **AgentState / ExecutionStep** | 统一运行状态与步骤轨迹（PLAN / EXECUTING / VERIFYING…），自动计时与预算记账 |
| **BudgetManager** | 步数 / LLM 调用 / 工具调用 / 耗时四维预算，超限抛 `BudgetExceededError` 由调用方决策 STOP 或降级 |
| **Control Plane** | 运行登记（有界 FIFO）+ 取消（真正打断 asyncio Task）+ 预算归属，终态幂等 |
| **任务级 Run API** | `POST /api/v1/agents/runs` 派生 → `GET .../runs/{id}` 查状态 → `POST .../runs/{id}/cancel` 取消，超时统一 `wait_for` 施加（A1c 上移自 `/api/v1/retrieval/agents/runs`） |
| **MCP Client** | spawn 任意 MCP server 子进程，远端工具注册进统一 ToolExecutor（**默认不信任**：未声明即 `L2` + `side_effect=True`，需用 `risk_level_by_tool` 显式降级；`mcp_` 前缀防撞名） |
| **MCP Server** | `python -m agentix.capabilities.mcp.server`——零依赖 stdio JSON-RPC（initialize / tools/list / tools/call），内置 calculator / text_stats，可桥接声明式工具注册表的 L0 工具 |

> 设计边界：Control Plane 只做**运行登记 + 取消 + 预算归属**（不做调度）；工具实现暂留原地，经 `register_handler` 即纳入统一执行治理（Strangler 迁移纪律）。

**MCP 宿主挂载**（`pip install -e .` 后也可用 `agentix-mcp-server` 命令）：

```json
{
  "mcpServers": {
    "agentix": {
      "command": "python",
      "args": ["-m", "agentix.capabilities.mcp.server"]
    }
  }
}
```

---

## 6. 能力模块

| 模块 | 说明 | 技术实现 |
|------|------|---------|
| **智能路由** | 自动识别问题类型并分发；意图模糊先澄清再执行（确定性启发 + 单轮约束） | 启发式路由 + LLM 结构化输出（`CLARIFY_ROUTING_ENABLED` 默认关） |
| **知识库查询** | 语义检索、术语/条号词法召回、释义与案例式问答 | Dense + BM25 + RRF + 可选 Cross-encoder 重排；混合链按活动索引产物隔离缓存 |
| **图谱推理** | 实体关系推理与路径查询 | Cypher 动态查询 + 预定义模板 + GraphRAG 社区摘要 |
| **统计查询** | 数量统计、排行榜、聚合分析 | Text2SQL（自然语言转 SQL → MySQL） |
| **多模态处理** | 文档识别、文件上传分析 | Vision Model + 文档管理接口（PDF）/ 外部入库服务（Excel） |
| **对话管理** | 完整会话历史（最近 N 条窗口回放）；会话列表跨标签页实时同步 | LangGraph Checkpointer + Redis + BroadcastChannel |
| **运行性能治理** | 延迟与预算观测、运行准入排队、过载时分级降级 | `agentix/performance/`（准入与降级按配置启用） |
| **MCP 工具扩展** | 挂载任意 MCP 工具进程 | MCP Client（stdio JSON-RPC）+ 统一 ToolExecutor |

知识库填充（条目 / 语料块数量依领域数据而定，见 **[数据准备](docs/guides/DATA_PREPARATION.md)**）。

### 统一 Web 工作台

登录后默认进入统一聊天工作台，不再把「知识库问答」和「智能助手」作为两个页面模式。输入框左侧的「知识库」开关控制当前提问是否使用检索域：开启后选择知识库并检索其内容，关闭后调用 Agent 助手链路。知识库开启时，PDF 上传会写入所选知识库并提交索引；关闭时，上传文件作为助手本轮附件，支持的格式由服务端上传接口约束。

两种能力目前复用聊天界面，但后端仍是两套独立 API 与会话存储：检索使用 `/api/v1/retrieval/chat/stream` 和检索会话，助手使用 `/api/v1/chat/stream` 和 Agent `session_id`。因此两类消息不会自动合并成一个服务端历史，也不能把助手回答直接保存为知识库文档；后者需要单独的文本入库接口与索引流程，当前尚未实现。

**工作台交互**：

1. 在输入框点击「知识库」开关，开启后选择目标知识库，提问将携带所选 `kb_id` 走检索链路；
2. 关闭开关即可不选知识库直接使用 Agent 助手；此时附件作为助手本轮文件，不会写入知识库；
3. 知识库开启且未选择知识库时，发送与 PDF 上传都会禁用，并提示先选择/创建知识库。

**工作台界面（知识库检索模式）**——侧栏会话历史 + 建议卡 + 知识库开关与目标库选择：

![统一工作台 - 知识库检索模式](docs/assets/ui/knowledge-workspace.png)

**工作台界面（Agent 助手模式）**——关闭知识库开关后走 Agent 链路，回答附复制 / 重新生成操作：

![统一工作台 - Agent 助手模式](docs/assets/ui/assistant-workspace.png)

> 上述界面截图来自本地隔离测试账号与测试数据库（2026-10-11 实机采集），不包含真实用户数据。

**RAG 质量与时延优化路线**：先用人工核验的检索金标建立 Recall@5 / MRR 与引用正确率基线，再逐项对照 BM25 / Dense / RRF / Rerank（`python evals/hybrid_eval.py --kb-id <KB>`）；当前检索评估依赖真实索引和知识库数据，模板数据不能证明法条正确性。混合检索的 Dense 与 BM25 候选现已并发获取；BM25 使用倒排 posting 只扫描命中查询词的 chunk，并把阿拉伯数字与中文数字法条号规范到同一词项。索引快照仍按活动 `artifact_id` 隔离，语料只取该版本 manifest 内 `ready` 文档的 chunk。LM Studio 的 embedding 接口不能代替 Cross-encoder `/rerank` 接口；未成功精排会明确记为降级，不冒报成功。优化效果须在真实知识库上测量并同时检查召回质量。后续阶段与验收指标见 **[RAG 优化路线](docs/design/rag-optimization.md#8-后续质量提升计划)**。

**上传通道与能力边界**（三条通道职责不同）：

| 通道 | 接受格式 | 处理方 | 说明 |
|---|---|---|---|
| 聊天上传 | `.xlsx` / `.xls` | 外部入库服务（`kb_ingest`） | 需服务端配置 `INGEST_SERVICE_URL`；否则该通道不可用 |
| 「文档管理」上传 | `.pdf` | 本仓文档管理接口 | 切分 / 向量化 / 索引由该接口负责，是 PDF 的唯一入口 |
| 文本直传（`.txt` / `.md` / `.json` / `.csv` / `.log`） | — | — | **当前未实现**，且不是「稍后再试」 |

> 文本直传此前会落到通用异常分支，把**永久不可用**说成**瞬时故障**。
> 现已改为显式永久性提示（`FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE`），
> 并加了守卫测试防止「文案承诺」再次超出实现（`tests/test_p43_path_safety.py`）。
> 补齐该能力需在检索域新增一条非 PDF 的入库链路（含切分 / 索引改造），属产品决策。

---

## 7. 可靠性

- **多级检索兜底**：知识图谱 / 结构化优先 → 向量语义兜底 → 外部搜索兜底
- **工具执行治理链**：Permission → Risk(L0-L3) → Timeout → Retry → Execution → Result → Trace；**L1 及以上**（MEDIUM+）高风险动作走人工审批旁路 / HITL Sidecar（fail-closed）
- **模型 Fallback 链**：Model Gateway 分档路由，主模型失败自动兜底并打点 `gateway_fallback_total`
- **Guardrails**：每个查询检查服务范围 + 图谱 Schema 验证，越界问题明确拒绝
- **服务间鉴权**：`kb_ingest` 的写入 / 检索端点需 `X-API-Key`
- **Token 预算**：三档判定 + 两级台账，默认关闭、按需开启
- **可靠性原语**：限流（全局 + 路由级）· 熔断（circuit breaker）· 并发限制，见 `agentix/reliability/`（发布门禁 release gate 已于 A6 删除，现由 `.github/workflows/release.yml` preflight + `evals/quality_gate.py` 承担）
- **出站通知**（Phase 23）：`NOTIFY_USER_ENABLED=true` 注册 `notify_user` 工具——通知落 EventPort（type=`user_notification`）并经 `GET /api/v1/ops/events` 可查；声明 L1 → MEDIUM，开启 `HUMAN_REVIEW_ENABLED` 时**先审批后执行**（出站即副作用，不得绕闸）；本批不接真实邮件/短信通道
- **配置漂移告警**（Phase 22）：会话中途检测配置哈希变化 → 结构化 `system_notice`（仅键名不含值），实现见 `application/services/config_watch.py`
- **流式防挂起**（批次 B1）：SSE 逐次取事件包 `STREAM_IDLE_TIMEOUT`（默认 60s）看门狗——上游静默挂起即 error + done 兜底收口并归还并发槽位，连接绝不悬挂
- **预算意识**（Phase 28）：步数预算观测，轨迹 append `budget_notice`（剩余 ≤2 可见、不动静态前缀）+ 状态栏 `max_iter` 常量键（`BUDGET_NOTICE_ENABLED` 默认关）

**`kb_ingest` 服务间鉴权**：该服务是**写入语义**（入库直接改 Milvus 集合），且 compose 以 `ports: 8100:8000` 发布、`uvicorn --host 0.0.0.0` 启动。现已于 **router 级**挂载 `X-API-Key` 校验（router 级而非逐端点，避免新端点静默裸奔）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `KB_REQUIRE_API_KEY` | 是否强制校验；`false` 时启动期打 **WARNING** 并点名暴露的端口 | `false` |
| `KB_API_KEY` | 共享密钥。开启校验后必填，留空则**全部请求 401**（fail-closed） | 空 |

> ⚠️ 密钥**刻意不复用**主应用的 `API_KEY`（避免权限抬升），改用服务作用域的 `KB_*`，两支密钥可独立轮换。客户端侧由 `agentix/domain/policies/ingest_auth.py` 统一出站鉴权头，且**只对指向本仓 `kb_ingest` 的同源地址**附带密钥（按 origin 比较）。

详见 [运行时护栏](docs/architecture/runtime-guardrails.md) 与 [配置管理](docs/architecture/configuration.md)。

---
## 8. 评测

```bash
# 离线跑分（用已录制的基线预测打分，CI 同款）
./.venv/Scripts/python.exe evals/run_eval.py --offline

# 在线跑分 + Pass^k 可靠性（需 LM Studio / LLM 服务）
./.venv/Scripts/python.exe evals/run_eval.py --live --repeat 3

# 轨迹前缀回归（基线 vs 候选，逐位置比对，退出码可进 CI）
./.venv/Scripts/python.exe evals/trajectory_regression.py -b evals/baselines -c out/traces

# 消融实验（A 裸 LLM → G 全能力，七变体对比出 Benchmark 表）
./.venv/Scripts/python.exe evals/ablations/run_ablation.py --list
./.venv/Scripts/python.exe evals/ablations/run_ablation.py --aggregate-only
```

| 能力 | 说明 |
|------|------|
| 黄金评估集 | `evals/legal_golden.jsonl`——102 条 golden，覆盖全部 7 类路由 + 拒答 / 引用 / 效力感知；按 `difficulty`/`source` 分层汇总（Phase 31 只增列不增删行） |
| 确定性指标 | 路由准确率 / 引用命中率 / 拒答正确率 / 引用正确性（空真口径显式可见） |
| 质量门禁 | `quality_gate.py`——三态 **PASS / FAIL / NOT-ENFORCED**，不低于基线 -2% |
| 消融基准表 | `evals/ablations/`——A-G 七变体（LLM only → Full Agentix）对比，聚合出 BENCHMARK.md |
| 失败回流 | `feedback_ingest.py`——生产失败样本 → 候选 golden 行（人工评审后合入） |
| 评估二期（Phase 31） | 声称一致性 `claim_check` · 提示敏感性 `--sensitivity` 四变体极差 · PII 离线 `privacy_flagged`（applicable 分母、原文零留存）· Best@k——全部为报告键，只增不进门禁 |

> ⚠️ **质量门禁当前为 NOT-ENFORCED（未生效），而不是「已达标」**。
> 门禁需要 `evals/baseline_predictions.jsonl`（已录制的基线预测），该文件**尚未提交**。
> 缺失时语义是显式第三态：CI 的 `eval-offline` 会打 `::warning` 并写入 step summary，
> **绿灯不代表评估达标，只代表「没跑」**。
> 补齐：在可用 LLM 服务上跑 `evals/run_eval.py --record` 录制基线后提交。

详见 [evals/README.md](evals/README.md) 与 [消融实验](evals/ablations/variants.json)。

**如何复现评测（promptfoo 回归主入口，P1-5）**：自建体系保留轨迹回归 / Pass^k / 质量门禁；
promptfoo 承担声明式回归用例与断言入口（打分函数直接 import `run_eval`，口径单源）。
前置：`.venv` 依赖 + 可用 LLM 端点（CI 全离线，常驻 job 只做契约守卫，不跑在线 eval）。

```powershell
# 1. 从 golden 机械转换试点用例（默认 25 条；--limit 0 = 全量 102 条）
.\.venv\Scripts\python.exe evals/golden_to_promptfoo.py --limit 25
# 2. 本地在线评测（结果落本地 promptfoooutput/，纯 CLI 不上云）
npx promptfoo@latest eval --ci --no-cache
# 3.（可选红队）promptfoo redteam 生成注入/越狱 probes，作 LLM01 活体证据
```

---

## 9. 可观测

- **全链路 trace_id**：中间件最外层注入，请求日志 / 鉴权 / Agent / LLM 埋点共享同一 trace_id
- **步骤级轨迹**：`trajectory.py` 落盘可回放 / 前缀回归 / 首错定位
- **执行轨迹时间线**：前端消息内「执行轨迹」折叠面板，实时呈现工具调用 → 生成 → 完成事件序列与间隔
- **Explanation API**：`GET /api/v1/retrieval/answers/{id}/explanation` 聚合证据 / 引用 / 模型，未持久化块显式 `not_recorded`
- **业务指标**：grounding 低分率、网关兜底级联等 Prometheus 指标 + 告警规则（compose 内置 prometheus / alertmanager / grafana）
- **事件查询**：`GET /api/v1/ops/events?event_type=user_notification&limit=50` 列出 EventPort 最近事件（鸭子面 `list_recent`，memory / jsonl / redis_stream 三后端一致）
- **SLO 目录**：可用性 / 首 token P95 / 拒答正确率等 5 项 SLI + 错误预算 burn-rate 分级
- **LangSmith tracing 看板（可选，默认关）**：langchain 0.3.x 栈下经环境变量零代码接入——`LANGCHAIN_TRACING_V2=true` + `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT`（见 `.env.example`）后，LangGraph 编排与 LangChain runnable 调用链自动上报，先 10 条验证再全量。接入边界：自建 httpx 直调与 OTel/Prometheus 体系**不上报**；自建指标与轨迹落盘全保留（轨迹回归真源在本地），LangSmith 只接管看板与在线实验层。免费档 1 席 / 5000 traces / 月、轨迹保留 14 天，只接开发/评测流量，上报失败 fail-open（丢遥测不丢请求）。注册：[smith.langchain.com](https://smith.langchain.com) 拿 API Key 填入 `.env` 即可。

详见 [SLO 目录](docs/runbooks/slo.md) 与 [发布回滚 Runbook](docs/runbooks/release-and-rollback.md)。

---

## 10. 快速开始

```bash
# 1. 克隆项目（若仓库更名，请将 URL 同步替换）
git clone https://github.com/Jia-Yihao/Legelbot.git My-Agentix
cd My-Agentix

# 2. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate

# 3. 安装后端依赖
pip install -r requirements-dev.txt
pip install -e .

# 4. 配置环境变量
cp .env.example .env              # 至少配置：LLM_API_KEY / EMBEDDING_API_KEY / SECRET_KEY

# 5. 启动基础设施（Postgres / Redis / Neo4j / Milvus / MinIO / MySQL / kb_ingest / 观测栈）
#    注意：这里【刻意不启动 backend】——后端由第 6 步在本机运行，
#    若直接 `docker compose up -d`，backend 容器会先占住 8000，第 6 步就会端口冲突。
docker compose up -d postgres redis neo4j mysql milvus kb_ingest prometheus alertmanager grafana

# 可选：启动 BGE Cross-Encoder reranker（首次会下载 BAAI/bge-reranker-v2-m3）
# Windows 可在项目 .env 中设置 RERANKER_CACHE_DIR=E:/My-Agentix/reranker-cache，
# 将模型权重缓存到 E 盘；Docker 镜像层位置由 Docker Desktop 的数据盘设置决定。
# 若 Hugging Face CDN 无法访问，可从 ModelScope 下载到 E:/My-Agentix/reranker-cache/model，
# 并在 .env 设置 RERANKER_MODEL_SOURCE=/app/.cache/model。
docker compose --profile reranker up -d reranker
# 本机后端将 RERANKER_BASE_URL 设为 http://127.0.0.1:7997；
# Compose 内 backend 使用 http://reranker:7997。服务就绪后显式设置：
# RERANKER_MODEL=text-embedding-bge-reranker-v2-m3
# 普通检索需 RERANKER_ENABLED=true；混合检索另需 RETRIEVAL_HYBRID_ENABLED=true、
# RETRIEVAL_SPARSE_ENABLED=true（hybrid 的精排由 RETRIEVAL_HYBRID_RERANK 控制）。

# 6. 启动后端（本机进程）
#    基础设施在容器里，本机后端要连容器内的服务，先覆盖两个地址：
#      - Postgres 宿主端口默认 15432（不是 5432），库名用 .env.example 的 agentix
#      - kb_ingest 宿主端口默认 8100
# Windows PowerShell：
$env:DATABASE_URL="postgresql+psycopg://postgres:postgrespass@localhost:15432/agentix"
$env:INGEST_SERVICE_URL="http://localhost:8100"
python -m uvicorn agentix.main:application --reload --port 8000
# macOS/Linux（bash）：
# export DATABASE_URL="postgresql+psycopg://postgres:postgrespass@localhost:15432/agentix"
# export INGEST_SERVICE_URL="http://localhost:8100"
# python -m uvicorn agentix.main:application --reload --port 8000

# 7. 启动前端（另开终端）
cd web && npm install && npm run dev
```

> **⚠️ `VITE_API_BASE_URL` 仅限本地开发直连场景**：dev 模式不配它即走 Vite proxy
> （`/api → localhost:8000`），这是默认且推荐路径。若配成绝对地址（如
> `http://localhost:8000`）会被打进**构建产物**——容器部署（web/nginx 同源反代）
> 下会绕过 nginx 直连该地址，**必然连接失败**。生产构建前务必确保该变量为空。
> 同理 `useKbPersonalUpload` 的「代理失败→直连回退」路径仅对本地开发有意义。

> **想让后端也跑在容器里？** 直接 `docker compose up -d`（全部服务，backend 已映射宿主 8000），
> 此时**跳过第 6 步**——`.env` 里的 `DATABASE_URL` 指向容器内网主机名 `postgres:5432`，由 compose 注入 backend 容器。

| 服务 | 地址 |
|------|------|
| 前端 | <http://localhost:5173> |
| 后端 API | <http://localhost:8000> |
| API 文档 | <http://localhost:8000/docs> |
| Grafana 观测 | <http://localhost:3000>（compose） |

```bash
# API 调用示例
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，请介绍一下你的能力", "session_id": "demo-session"}'
```

> 知识库入库依赖 `kb_ingest` 服务：需配置 `INGEST_SERVICE_URL` 与 `KB_API_KEY`（见 §7），否则聊天上传通道不可用。

**全容器一键路径（P2-3，可选）**：除上述"基础设施容器 + 本机后端/前端"的开发模式外，
`web` 前端已提供容器化 service（`profile: full` 隔离——默认 `docker compose up` 不启动，
开发模式叙述不变）。全容器模式：

```powershell
docker compose --profile full up -d       # 基础设施 + backend + web（第 13 个 service）
# 浏览器访问 http://localhost:8080 —— nginx 同源反代 /api → backend:8000（零跨域，SSE 已配）
```

常见问题（端口占用 / 连接超时 / LLM 空返回等）见 [QUICK_START.md](docs/guides/QUICK_START.md)，技术细节见 [TECH_STACK_GUIDE.md](docs/guides/TECH_STACK_GUIDE.md)。

---
## 11. 工程实践

**测试真源**（唯一声明处，其他文档只应指向本节）：

**前端套件当前验证值**：2026-10-11 界面实测与按钮调试后运行 `npm run test -- --reporter=dot`，**26 个测试文件、217 个测试全部通过**（测试规模较 README 既有基线增加 2 个 Composer 上传控件用例与侧栏折叠/账户菜单/发送键语义三组组件用例）。一口径 +55 = 统一执行链 Stage F2c-a（A-P2-2 余项）——Multi-Agent 统一退出契约 + 六失败模式打点（`ManagerOutcome.exit_status/exit_reason/truncated` 复用 `domain/runtime/lifecycle` 的 `ExitStatus/ExitReason` 词表单源，`derive_exit_contract` 按 reason 推导：converged→completed、预算与轮次耗尽→failed/max_iterations+truncated、peer 取消→interrupted、未接线/无进展/矛盾→failed/error，并强制 `truncated == (exit_reason == max_iterations)` 契约等式；`lg_supervisor` meta 透传三键，Manager 异常路径诚实 `failed/error`；`multiagent_failure_mode_total{mode}` 失败模式恰打一次、NONE 不计；`_aggregate_multi_agent` 评测面零接触）；`tests/test_p88_multiagent_exit_contract.py` 55 条，3736→3791，2026-10-03；较上一口径 +38 = 统一执行链 Stage F2b-e（R-P2-5）——evals 混合检索归因报告（`evals/hybrid_eval.py` 独立脚本：`Recall@5/MRR × {BM25, Dense, RRF, +Rerank}` 四列归因表，同问题集/同 `--search-k`/同匹配函数，唯一变量是通道；默认 `legal_golden.jsonl` 23 问 `kb-query` 小集（`--limit`/`--route` 可调，10–50 问带）；匹配复用 `run_eval` 解析器——法规名归一互相包含 + `第X条` 三形（`第148条` ≡ `第一百四十八条`）；分母仅金标非空题，单题通道失败剔出分母、整列失败 = 表内 `n/a` + `unavailable` 诚实 fail-open；报告键固定 `{columns, questions, table, unavailable}` 只增不进门禁——`evaluate()` summary / `CSV_FIELDS` / CI `METRICS` 五键门 / `quality_gate` 零接触；缺 `--kb-id`/索引未就绪 exit 2 不出假表）；`tests/test_p87_hybrid_eval_report.py` 38 条，3698→3736，2026-10-03；较上一口径 +27 = 统一执行链 Stage F2b-b（R-P2-1）——改写/HyDE（两独立键 `RETRIEVAL_QUERY_REWRITE_ENABLED`/`RETRIEVAL_HYDE_ENABLED` 默认关 = 基线逐位等价：多轮改写意图补全 + HyDE 假设性文档扩展，`usecases/query_rewrite.py` 纯函数归一（改写取首行去引号截 512、HyDE 截 1200、空/等价输出回退原 query，LLM 失败/超时 8s 一律 fail-open）；接线单缝 `retrieve_scoped_docs_cached`——生效才改 effective_norm（dense/hybrid 检索文本）与缓存池后缀 `|r1`/`|y1`（沿 hybrid `|h1` 先例），rerank 仍按原查询打分；`history` 参数供意图补全（预检路径传 `history_text`，工具/端口适配缺省单轮））；`tests/test_p84_rag_p2_query_rewrite.py` 27 条，3617→3644，2026-10-03；较上一口径 +25 = 统一执行链 Stage F2b-a（R-P2-2）——证据门禁（`EVIDENCE_GATE_MODE` observe 默认=与基线逐位等价 / warn 答案尾附一条「证据核验提示」/ refuse 答案替换为拒答文案且 citations 清空；触发信号 = grounding 低分（`<GROUNDING_LOW_SCORE_WARN`，0 关）∪ R1-2 法条未核验 ∪ 引用无法溯源被剥离，判定纯函数 `agentix/domain/policies/evidence_gate.py`，未知模式 fail-safe 回 observe、门禁自身故障 fail-open 回基线交付；审计 `tool_ctx.last_evidence_gate` + Prometheus `evidence_gate_total{action}`；键沿 GROUNDING_* 检索域三处同步（rset 声明 + .env.example + selfcheck 白名单））；`tests/test_p83_rag_p2_evidence_gate.py` 25 条，3592→3617，2026-10-03；较上一口径 +43 = 统一执行链 Stage F2a（M-P2-2/4）——PreCompact 重注（`CONTEXT_PRECOMPACT_REINJECT_ENABLED` 默认关：超预算压缩前把关键规则+五要素硬约束重注上下文尾部，约束优先按出现序去重 + 规则声明序补满、1500 字符有界，`[precompact:<source>]` 行级审计标记 + snapshot meta 来源计数，块不在轨迹行内 ⇒ 丢弃循环必幸存）+ 画像作用域（`MEMORY_PROFILE_SCOPE_ENABLED` 默认关：画像字段分级 stable/session/meta 单源、未知字段 fail-closed；稳定层跨会话沿用走 `GET/POST /ops/memory/profile-scope` 授权、读取异常 fail-closed；`POST /ops/memory/clear-user` 分段计数清除画像+语义卡+摘要全链路，errors 非空显式 500）；`tests/test_p82_memory_p2.py` 43 条，3549→3592，2026-10-03；较上一口径 +34 = 统一执行链 Stage F1e（A-P1-4）——HITL 进环（`HITL_IN_LOOP_ENABLED` 默认关：图内 `execute_task` 遇 `review_required` 挂起进图（langgraph interrupt + checkpointer，approve 落批准重试一次、reject/未知 fail-closed 终止后续波次回 failed）；闸开+无 approver+无决策面 ⇒ REJECT `no_approver_wired_fail_closed`（沿 sidecar 语义，不留无人批的 PENDING）；裁决序 deny→hook→ask强制→allow；py3.12+ 才注入、py3.10 降级 review_required；恢复面 `POST /ops/hitl/in-loop`）；`tests/test_p81_hitl_in_loop.py` 35 条，3515→3549，2026-10-03；较上一口径 +33 = 统一执行链 Stage F1d（A-P1-3）——并发工具轮（`AGENT_TOOL_ROUND_CONCURRENCY` 默认 1 = 串行逐位等价：L2 检索聊天 `_execute_tool_calls` 与 L3 `AgentLoop` 批路径一把旋钮，派发前逐调用授权门 fail-closed、危险工具有效风险 MEDIUM+ 强制串行、批宽按剩余步数裁剪、观测声明序落位、每批至多一次 replan；「家政工具不计进度」留口 F2）；`tests/test_p80_multiagent_p1_concurrent_round.py` 33 条，3482→3515，2026-10-03；较上一口径 +36 = 统一执行链 Stage F1c（A-P1-2）——LLM 规划器（`AGENT_LLM_PLANNER_ENABLED` 默认关：`LLMPlanner` 结构化输出、执行表白名单整步过滤、失败/空计划 fail-open 回单步；`AGENT_MAX_REPLANS>0` 时注入 `RetryReplanner` 内层）+ TodoWrite（`todo_write` 整体替换、至多一条 in_progress、`TodoStore` JSON 落盘可恢复、回执即读通道；L2+control 五处同步）；另修 `test_notice_never_contains_config_values` 随机哈希撞字串假阳性；`tests/test_p79_multiagent_p1_llm_plan.py` 36 条，3446→3482，2026-10-03；较上一口径 +35 = 统一执行链 Stage F1b2——M-P1-3 MEMORY 索引制（记忆注入转「段落头 + obs:// 指针索引」，正文外置 ArtifactStore，预算 1500 封顶截尾）+ M-P1-4 AGENTS 级联（全局 ~/.agentix → 项目根 → 消息触达子目录三层 + read_agents_file 按需取回）+ M-P1-5 obs:// 隔离接线（ContextEngine 隔离引用真正可取回，read_artifact 认 obs:// 前缀），3 新旋钮默认关；`tests/test_p78_index_cascade_isolate.py` 35 条，3411→3446，2026-10-03；较上一口径 +51 = 统一执行链 Stage F1b——Memory P1 接线（M-P1-1 向量召回 fail-open、M-P1-2 后台评审 LLM 路由 + turn-end 生产者三接线点、M-P1-5 真分词计数 7 处估点，2 新旋钮默认关；`tests/test_p77_memory_p1_wiring.py` 51 条，3360→3411，2026-10-03）；较上一口径 +27 = 统一执行链 Stage F1a——RAG P1 真 Hybrid（R-P1-1..5 全落地：后端元组修复使距离阈值实际生效、rank_fusion 纯函数、进程内 BM25 稀疏通道、retrieve_hybrid_docs 四阶段与 SSE retrieval 进度帧、Channel B 融合 flag 门控，12 新旋钮默认关；`tests/test_p76_rag_p1_hybrid.py` 27 条，3333→3360，2026-10-03）；较上一口径 +28 = 统一执行链 Stage M·M6——删三簇 shim（`infrastructure/tools`、`mcp`、`capabilities/tools`）+ M0–M5 全部旧路径清尾（56 文件 + 空目录删除、`_TARGET` 归零、20 个旧 dotted 前缀静态 import 面归零、p66 `SHIM_DIRS` 空集终态、p69 `COMPLETED_STAGES` 含 M6、p70–p74 守卫断言翻转、生产源零旧斜杠串）（2026-10-03，新增 `tests/test_p75_m6_shim_removal.py` 30 条 + p69/p70/p72/p73 守卫改写净 −2，3305→3333）；再上一口径 +27 = 统一执行链 Stage M·M5——检索域收编（`chat_agent_runner` 剥离 langgraph 换 capability 内部工具循环、首轮无工具单 delta 修二次调模型 bug、Port 装配单门收敛 `knowledge_gateway.get_retrieval_port`、`legal_effectiveness`/`grounding_verify` 迁 `agentix/domain/policies/` 旧路径 alias shim、p66 LangGraph 白名单 11→10、`PROTECTED_VERIFIER_PATHS` 补真实路径）（2026-10-03，新增 `tests/test_p74_m5_retrieval_unification.py` 25 条 + p69 manifest 参数化 +2，3278→3305）；再上一口径 +21 = 统一执行链 Stage M·M4——控制平面迁 `agentix/runtime/control_plane.py`（Run registry/取消/准入归位 Runtime 簇；`TokenBudget` 收进 TYPE_CHECKING 保簇自包含，旧路径 alias shim，`runtime/__init__` 刻意不汇总导出）（2026-10-02，新增 `tests/test_p73_m4_runtime_control_plane.py` 21 条，3257→3278）；再上一口径 +54 = 统一执行链 Stage M·M3——策略件迁顶层 policies/（planner 归并 replanner、verifier、hitl+hitl_bridge、guardrails 自 capabilities 迁入；executor 保留旧路径 guardrails import 避 p66 #3），旧路径 7 模块 sys.modules 别名 shim + 4 惰性包门面，p66 SHIM_DIRS +5、p70 guardrails 断言翻转（2026-10-02，新增 `tests/test_p72_m3_policies_migration.py` 53 条 + p70 净 +1，3203→3257）；再上一口径 +55 = 统一执行链 Stage M·M2——agent 编排件迁顶层 `orchestration/`（orchestrator/executor/langgraph + strategies/multi_agent，lg_prompts 业务 prompt 留 application），旧路径 8 模块 `sys.modules` 别名 shim + multiagent 惰性门面，`agentix.agent.__init__` 改惰性防加载序回拉（2026-10-02，新增 `tests/test_p71_m2_orchestration_migration.py` 55 条，3148→3203）；再上一口径 +66 = Stage M·M1——capabilities/tools 迁顶层 tools/（gateway/adapters 子包 + 顶层注册面；注册三件套因 `registry` 单例与子包名冲突落顶层），旧路径 16 文件 `_TARGET` shim + PEP 562 惰性门面，guardrails 留驻待 M3（2026-10-02，新增 `tests/test_p70_m1_tools_migration.py` 66 条，3082→3148）；再上一口径 +54 = 统一执行链 Stage M·M0——契约拆分（`agentix/domain/agent`、`agentix/domain/runtime`、`agentix/domain/tool` 三个纯契约子包）+ M1–M6 逐文件迁移清单冻结（2026-10-02，新增 `tests/test_p69_stage_m_manifest.py` 51 条 + phase1/p17/p22 随迁守卫 3 条，3028→3082）；再上一口径 +33 = 统一执行链 Stage B·B2b——33-P0 RAG 收敛后三项（R-P0-3 rerank 三态收敛 / R-P0-4 Cypher 只读闸 + Neo4j schema TTL / R-P0-5 打点不吞错，2026-10-02，`tests/test_p68_rag_p0_convergence.py` 14→46 条 + `tests/test_p02_wiring.py` 打点契约 22→23 条，2995→3028）；再上一口径 +14 = 统一执行链 Stage B·B2a——33-P0 RAG 收敛前两项（LightRAG 单实现去重门面 / 租户 fail-closed，2026-10-02，新增 `tests/test_p68_rag_p0_convergence.py` 14 条，2981→2995）；再上一口径 +30 = 统一执行链 Stage B·B1——32-P0 记忆收敛（双卡合并 card_schema 单源 / 脱敏补齐中英姓名地址写读双向 / 冲突键全 tags + 30 天时间窗标记 / 3 scope 收口，2026-10-02，新增 `tests/test_p67_memory_p0_convergence.py` 30 条，2951→2981）；再上一口径 +9 = 统一执行链 Stage A 批次（A6 死代码批 51 文件 + 分层守卫定稿 p66——可达性扫描零引用直删、文档反引号与现时态陈述同步、12 守卫测试化收敛，2026-10-02，新增 `tests/test_p66_arch_guards.py` 9 条，2942→2951）；再上一口径 +13 = 统一执行链 Stage A 批次（A5 SSE 全量统一 R5a–R5d——run-scoped emitter / 词汇归一保留 `type` 删 `event` / 前端共 parser / 流式轨迹桥 + 持久化评估门，2026-10-02，新增 `tests/test_p65_unified_sse.py` 13 条，2929→2942，前端 vitest 121→126）；再上一口径 +11 = 统一执行链 Stage A 批次（A4 HITL 生产审批决策面 / application/runtime 直删 / 死转移处置，2026-10-02，新增 `tests/test_p64_hitl_ops_and_dead_transitions.py` 11 条，2918→2929）；再上一口径 +22 = 统一执行链 Stage A 批次（A3 图内规划收敛影子对拍/切流 + TEXT2SQL 死 flag 处置，2026-10-01，新增 `tests/test_p63_planner_convergence.py` 22 条，2896→2918）；再上一口径 +18 = 统一执行链 Stage A 批次（A2a–A2c 三闸有效化 / 五项旁路收编 / 记忆三工具守卫用例，2026-10-01，新增 `tests/test_p62_unified_gateway.py` 18 条，2878→2896）；再上一口径 +31 = 统一执行链 Stage A 批次（A1a–A1e 运行登记/退出契约/Run API 迁移/元数据减负/守卫用例，2026-10-01，新增 `tests/test_p61_unified_run_chain.py` 31 条，2847→2878）；再上一口径 +384 = 优化批次 P1–P6（任务执行 / Skills / 渐进上下文 / 异常闭环 / Proactive / 长时记忆，2026-10-01，新增 `test_p49`–`test_p60` 十二个测试模块共 384 条，逐批 2463→2496→2542→2594→2668→2761→2847）；再上一口径 +5 = 穿插批次 B/C（B1 流式 idle 看门狗 4 条 + B2 SSE 词汇双写 1 条；C2 为 lint 清理不增后端计数；前端 vitest 113 → 121）；再上一口径 +20 = Phase 29 记忆结构化 `test_agent_phase29_memory_cards.py` profile_card/派生幂等/冲突标记 20 条；再上一口径 +41 = Phase 30 进化闭环 41 条；再上一口径 2397 = Phase 31 评估二期 40 条；再上一口径 2357 = Phase 28 自主护栏 28 条；再上一口径 2329 = 审计批次 A 归属校验 `test_p48_ownership.py` 21 条 + 限流 A3 扩 4 条 + 上传读鉴权契约 1 条）。

14 条 skip 均为显式声明：4 条集成/端到端用例（2 条需 `LEGELBOT_RUN_INTEGRATION_TESTS=1`，2 条 LightRAG 端到端需 `LEGELBOT_RUN_LIGHTRAG_E2E=1`）+ 10 条 Redis Stream 契约用例（本机无 Redis 服务时按设计诚实跳过，`RedisStreamEventBus` 懒连接、连接错误向上传播不伪造）。真图 HITL 进环 E2E（`test_p81` `TestRealGraphE2E`）在本地 Python 3.12.13 执行；CI Python 3.10 按设计跳过，**不存在无原因静默跳过**。

本地统一使用 **Python 3.12.13 + 标准 venv `.venv`**；CI 仍按工作流配置验证受支持的 Python 版本，不需要本地维护多套环境。

```powershell
# 创建环境（需先安装 Python 3.12）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev,test]"

# 全量回归、架构守卫、lint 与类型检查
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests/test_p17_domain_layer.py tests/test_p22_obs_scope.py -q
.\.venv\Scripts\python.exe -m ruff check agentix scripts evals kb_ingest tests
.\.venv\Scripts\python.exe -m mypy agentix/domain --config-file=pyproject.toml
```

> 全量测试不要并行运行：少数用例写仓库相对路径，并行执行可能互相踩文件产生**假红**。
> **`--basetemp` 必须指向仓库之外、且每次用新建的空目录**。
> Linux/macOS 的解释器路径为 `./.venv/bin/python`。

**工具链口径**：`make lint` 与 CI 的 lint job 使用同一套命令与范围（`ruff` 全范围含 `tests`、`black --check` 非阻断、`mypy agentix/domain`，`follow_imports = "silent"` 渐进式策略），由 `tests/test_p46_toolchain_parity.py` 守卫。Windows 无 `make` 时直接跑上述三条命令。

| 测试类别 | 覆盖内容 |
|---------|---------|
| 单元 / API 集成 | 路由、Reranker、上传安全、认证、LLM 工厂 |
| **架构守卫** | 分层依赖规则（AST 扫描 + 白名单集合相等断言）+ 文档路径 / 配置键 / 数字一致性 |
| 运行时护栏 | 上下文隔离、控制平面、token 预算、目标仲裁、工具限量、人工审批、任务分解 |
| Agent Core 2.0 | 主循环 / 规划纠偏 / HITL / 事件 / 多智能体失败模式 / 演化 / 分层验证 |
| 可观测性 | 轨迹回放 / 首错定位 / Pass^k / 轨迹前缀回归 |
| Agent Runtime / MCP | Run API 语义（超时 / 取消 / 预算）、MCP 协议与桥接 |

**CI/CD**：GitHub Actions（pytest + ruff + eval-offline 质量门禁 + **阻塞性能冒烟**（Phase H W7 起 perf-smoke 进 `ci-gate` 汇总门）+ CodeQL）+ Dependabot 四生态。

**安全**：CORS 白名单、API Key 认证、会话/历史归属校验（跨用户 404）、上传读写同口径鉴权（owner sidecar）、限流 per-params 口径、路径遍历防护、XSS 防护、错误信息不泄露、文件大小限制。
⚠️ **默认不强制鉴权**：`REQUIRE_API_KEY` 默认 `false`，启动期打 `WARNING`；生产必须显式配置 `SECRET_KEY` / `API_KEY` 并置 `REQUIRE_API_KEY=true`（见 [SECURITY.md](SECURITY.md)）。生产启动期执行配置强校验，**不合格直接拒绝启动**。

---

## 12. 项目结构

```
agentix/                        后端主包（FastAPI 应用 + 平台内核）
├── main.py                     FastAPI 入口：中间件链 / 路由挂载 / 静态托管
├── config/                     Pydantic Settings 统一配置（单一事实来源）
├── domain/                     领域层：零第三方依赖
│   ├── errors.py               错误词汇表（ErrorCode / AgentError）
│   ├── evals.py / status.py / tracing.py
│   │                           评测统计、请求状态与 trace_id 契约
│   ├── agent/                  Task/Plan/Action 契约 + 八 Protocol（M0 拆入）
│   ├── runtime/                Run/Step/Budget 契约 + 统一退出契约（lifecycle.py）
│   ├── tool/                   ToolSpec / Risk / Permission 纯契约（M0 拆入）
│   ├── ports/                  graph / repository / retrieval Protocol + Payload TypedDict
│   └── policies/               错误分类 / 重试策略 / 冲突仲裁判定函数
├── orchestration/              编排件（M2）：orchestrator 主循环 · executor 执行器
│   ├── langgraph/              lg_builder 主图 / lg_states / lg_supervisor 拓扑
│   └── strategies/             multi_agent（Peer + Manager · 退出契约 · 子代理隔离）
├── policies/                   策略件（M3）：planner/ verifier/ hitl/ guardrails/
├── tools/                      工具唯一执行入口（M1）：gateway/（权限/风险/超时/重试三闸）
│                               definitions.py（registry 单例）/ registry_init.py / selection.py / adapters/
├── application/                应用层：业务规则与图接线
│   ├── agents/                 kg 子图 / main 图 / text2sql / kb_tools / 业务 prompt / LLM 工厂
│   └── services/               会话 / 记忆 / 观测 / 任务队列 / agent_loop_adapter 粘合
├── agent/                      Context Provider 家（M2/M3 收缩后）：context / memory /
│                               events / observer / policy / skills / hooks / evolution
│                               + graph（占位包）
├── runtime/                    Agent Runtime 真源：state / steps / budget / lifecycle
│                               control_plane（Run 登记 + 取消打断 Task）
├── performance/                运行性能治理：延迟 / 预算 / 准入排队 / 负载降级
├── capabilities/               MCP Client（mcp/：protocol / client / 零依赖 stdio server）
├── infrastructure/             基础设施层：实现 domain.ports
│   ├── core/                   日志 / 哈希 / DB / 中间件 / 限流（横切基建）
│   ├── cache/ / http/           缓存与 HTTP 客户端基础设施
│   ├── llm/                    LLM 调用加固（超时 / 重试 / 兼容）
│   ├── persistence/            DB / 仓储 / Redis Checkpointer
│   ├── text2sql/               Wren 文生 SQL 适配
│   └── registry.py             基础设施注册表
├── interfaces/                 HTTP 路由 / 请求响应模型 / 中间件
├── retrieval/                  检索域（多租户知识库）：api / domain / infra / usecases
│                               （自带 ReAct 控制流已随 M5 剥离）
├── crawler/                    数据采集与导入 CLI（当前支持 Wikipedia → 知识库）
├── observability/              trace_id / span / 指标 / 轨迹落盘
├── reliability/                限流 / 熔断 / 并发限制
└── security/                   JWT / 输出过滤 / 提示注入防护

tests/                          按主题组织的后端回归与架构守卫测试（基线见 §11）
skills/                         Agent Skills 目录（`SKILLS_ENABLED` 默认关）
kb_ingest/                      知识库入库独立服务（API / clients / core / db / services）
web/                            Vue3 + TypeScript 前端（Vite / Vitest）
evals/                          黄金评估集 / 质量门禁 / 消融实验 / 轨迹回归
docs/                           架构 / 指南 / Runbook / ADR（`architecture/adr/`）/ 项目说明书
                                （`PROJECT_SPEC.md` + 图文版 docx）
scripts/                        数据构建 / 迁移 / 运维脚本
data/                           领域数据（图谱种子 / 语料，由用户填充）
docker/                         观测栈（prometheus / alertmanager / grafana）与 compose 覆盖（Dockerfile 在仓库根）
```

---

## 13. 技术栈

| 类别 | 技术 |
|------|------|
| 后端 | Python 3.10 / 3.12 · FastAPI · Uvicorn（SSE 流式） |
| Agent 编排 | LangGraph 0.2（Subgraph + Map-Reduce）· Agent Core 2.0（Harness + Capabilities） |
| 前端 | Vue 3 · TypeScript · Vite · Vitest |
| 知识存储 | Neo4j（知识图谱）· Milvus（向量检索）· PostgreSQL（结构化 / 编排元数据）· MySQL（统计）· Redis（缓存 / Checkpointer） |
| 检索增强 | LightRAG（社区摘要）· Reranker（Cohere / Jina / Voyage / BGE）· 多路检索兜底 |
| 工具扩展 | MCP Client + Server（stdio JSON-RPC）· ToolRegistry 声明式注册 |
| 可观测 | Prometheus · Grafana · Alertmanager · 步骤级轨迹回放 · SLO |
| 质量与评测 | pytest · ruff（门禁含 `tests`）· mypy · 102 条分层 golden 回归 · 声称一致性 · 质量门禁 · 轨迹前缀回归 |
| 安全 | APIKey / JWT / RBAC · 输出过滤 · 提示注入防护 · 限流熔断 |
| 交付 | Docker / docker-compose · GitHub Actions（CI + CodeQL）· Dependabot |

---

## 14. 文档

| 文档 | 内容 |
|---|---|
| [项目说明书](docs/PROJECT_SPEC.md) | **新人向总指南**：23 章实现级全量说明书（技术栈与分层、两套编排、SSE 双链路词汇已归一、入库与门禁）；Word 图文版见 `docs/My-Agentix项目说明书.docx`（9 张架构图） |
| [架构总览](docs/architecture/README.md) | 一句话架构、五条设计原则、运行时架构图 |
| [架构详解](docs/architecture/ARCHITECTURE_DETAIL.md) | 设计特色、完整架构图、三层流程逐节点说明 |
| [分层与依赖规则](docs/architecture/layers.md) | 各层职责、**4 条强制依赖规则**及其守卫 |
| [Agent Core 五层架构](docs/architecture/agent-core-layers.md) | Harness / Capabilities / Application / Domain / Interfaces 运行时职责与端口映射 |
| [接口契约](docs/architecture/interfaces.md) | 端口 Protocol、Payload TypedDict、错误码四层分类 |
| [扩展指南](docs/architecture/extension.md) | 新增工具 / Provider / Agent 子图 / 领域适配 |
| [运行时护栏](docs/architecture/runtime-guardrails.md) | 上下文隔离 · 控制平面 · 感知工具限量 · 人工审批 |
| [请求执行图](docs/architecture/request-execution-graph.md) | 请求链路与节点依赖 |
| [配置管理](docs/architecture/configuration.md) | 单一事实来源、特性开关回滚表 |
| [架构决策记录](docs/architecture/adr/) | 分层与端口 / 注册表可插拔 / 声明式配置 |
| [架构审计 2026-09-28](docs/architecture/audit-2026-09-28.md) | 遗留可优化点分期方案；批次 A/B/C **已修复清单**（§1，随批移项）+ 验证基线 |
| [技术栈指南](docs/guides/TECH_STACK_GUIDE.md) | 全量技术选型、版本与集成细节 |
| [Quick Start 常见问题](docs/guides/QUICK_START.md) | 端口占用 / 连接超时 / LLM 空返回排查 |
| [Agent 路由速查表](docs/guides/agent_routing_quick_reference.md) | 路由意图、出口节点、关键词兜底 |
| [LightRAG 服务指南](docs/guides/lightrag_service_guide.md) | LightRAG 服务部署、检索模式与调试 |
| [Research Agent Demo](docs/guides/research-agent-demo.md) | Phase 13 离线主循环走查、组件映射与验收对照 |
| [测试分层策略](docs/guides/testing.md) | 五层测试体系、marker 约定、改动→测试对照表（测试总数见 §11） |
| [数据准备](docs/guides/DATA_PREPARATION.md) | 三类数据文件、覆盖范围、真实数据填充指引 |
| [发布与回滚 Runbook](docs/runbooks/release-and-rollback.md) | 部署步骤、五层健康验收链、四级回滚 |
| [SLO 目录](docs/runbooks/slo.md) | 服务等级目标、错误预算、告警分级 |
| [失败恢复 Runbook](docs/runbooks/failure-recovery.md) | 四层失败恢复机制与现场演示命令 |
| [迁移进度](docs/migration/module-inventory.md) | Agent Core / Runtime 物理迁移清单与勾选状态（Phase 0–31 + 批次 A/B/C 勾选真源） |
| [优化迭代 Phase 28–31](docs/migration/Phase28-31_优化规划.md) | 四主题（自主护栏 / 记忆结构化 / 进化闭环 / 评估二期）+ 审计批次 A/B/C 执行状态（§8）与验证命令 |
| [评测说明](evals/README.md) | golden 集、确定性打分、质量门禁、消融实验 |
| [OWASP LLM Top 10 自评](docs/security/owasp-llm-self-assessment.md) | LLM01–LLM10 逐条：风险 → 现有缓解（含代码位置）→ 残余风险 |

---

## 参与贡献

欢迎任何形式的贡献——代码、文档、评测集扩充、问题反馈。

1. **Fork** 并从 `main` 创建分支 → 2. 遵循分层架构约束 → 3. `ruff` / `black` / `mypy` / `pytest` 全过 → 4. Conventional Commits 格式提交 → 5. 按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 提交

| 文件 | 内容 |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、**架构约束**、代码与提交规范、PR 流程 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 社区行为准则 |
| [SECURITY.md](SECURITY.md) | 漏洞报告流程（请勿公开 Issue） |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |

## 15. 性能与成本（待实测）

> ⚠️ **端到端对话性能与成本仍为占位（`<待实测>`）**——采集方法见
> [docs/runbooks/perf-cost-measure.md](docs/runbooks/perf-cost-measure.md)，
> 环境就绪后实测回填。本节 §15.1 单独记录了已完成的检索子链路实验，
> 不代表端到端 SLO，也不外推到其他语料或模型。

| 指标 | 数值 | 口径 |
|---|---|---|
| 首 token P50 | `<待实测>` | `agent_ttft_seconds` 直方图 P50，10m 窗口 |
| 首 token P95 | `<待实测>` | 同上 P95；SLO 目标 ≤5s（见 SLO 目录） |
| 流总时长 P95 | `<待实测>` | 终帧 `duration_ms` 统计 |
| 单请求平均 token（输入） | `<待实测>` | `llm_tokens_total{direction="prompt"}` 增量 / 请求数 |
| 单请求平均 token（输出） | `<待实测>` | `llm_tokens_total{direction="completion"}` 增量 / 请求数 |
| 单会话平均成本 | `<待实测>` | `UsageRecord` 按 branch 聚合 cost |

**测量方法**：全栈环境（LM Studio 加载固定模型快照 + 基础设施容器）下，
对 102 条 golden 跑一轮 `evals/run_eval.py --record`，期间从 Prometheus
（`http://localhost:9090`）读取 TTFT 直方图分位数与 token 计数器增量，
token 明细从 `UsageRecord` 聚合。数字与模型快照/温度绑定，换模型须重测。
已知限制：本机显存与云服务器测量结果不可互比，回填时须标注测量环境。

### 15.1 本地法律语料检索对照（2026-10-11）

在隔离评测库中对 `data/kb/legal_corpus.txt` 的 4,865 条法条运行真实检索；
语料 SHA-256 为
`1ca58564b1f3a58c212c0fc23e9e5da317576ce524fc58cd4f6d0ec6f3aea2e5`。
使用 `evals/legal_golden.jsonl` 中 23 条适用的 `kb-query` 金标问题，
由同一匹配器统计 Recall@5 与 MRR。Dense 向量使用本地 LM Studio
`text-embedding-bge-m3`，Cross-Encoder 通过 Infinity CPU 服务
`text-embedding-bge-reranker-v2-m3` 实际推理。

| 检索阶段 | Recall@5 | MRR | 参数 |
|---|---:|---:|---|
| BM25 | 0.6957 | 0.5065 | `search_k=5` |
| Dense | 0.7826 | 0.5978 | `search_k=5` |
| RRF | 0.7826 | 0.6304 | `search_k=5`，K=60 |
| RRF + Cross-Encoder | 0.7826 | 0.6775 | `search_k=20`，精排 10 个候选 |
| RRF + Cross-Encoder | 0.7826 | 0.6971 | `search_k=20`，精排 20 个候选 |
| RRF + Cross-Encoder | 0.7826 | 0.7043 | `search_k=5`，精排 10 个候选 |
| Z-score | 0.7826 | 0.6884 | `search_k=5`，α=0.75 |
| Z-score + Cross-Encoder | 0.7826 | **0.7283** | `search_k=5`，α=0.75，精排 10 个候选 |

在同一小样本上的 α 扫描结果：0.25 得到 Recall@5/MRR `0.7391/0.5399`，
0.50 为 `0.7826/0.6413`，0.75 为 `0.7826/0.6884`。RRF K 扫描
（10/20/40/60/100）没有观察到优于默认 K=60 的稳定收益。
相比 Dense，当前最佳组合的 MRR 提高约 21.8%，但 Recall@5 仍是
18/23 题，**没有增加命中题数**；目前观察到的是相关法条排序前移，
不是召回覆盖提升。样本量小且结果用于调参，不能视作独立泛化验证。

延迟方面，仅记录整批脚本墙钟时间（23 题，包含多通道调用及评测开销）：
RRF + 10 候选在 `search_k=20` 时为 65.1 秒，在 `search_k=5` 时为
55.6 秒；后者约低 14.6%，且 MRR 从 0.6775 升至 0.7043，Recall@5
不变。Z-score + 10 候选的另一次整批运行用时 53.1 秒。以上均为单次
批量运行，不是单请求延迟或 P95；未控制重复轮次/缓存冷热，不能据此宣称
生产延迟 SLO 已达成。

本机复现实验需要可用的 LM Studio Embedding 与 Infinity `/rerank` 服务，
并将 `DATABASE_URL`、`INDEX_DIR` 指向隔离评测库/索引。以下参数也可用于
显式启用本机应用的实验链路（Compose 内运行时把 reranker 地址改为
`http://reranker:7997`）；混合检索总闸的代码默认值仍为关闭：

```powershell
$env:DATABASE_URL="sqlite:///E:/My-Agentix/retrieval-eval-20261011/agentix_eval.db"
$env:INDEX_DIR="E:/My-Agentix/retrieval-eval-20261011/indexes"
$env:EMBEDDING_BASE_URL="http://127.0.0.1:1234/v1"
$env:EMBEDDING_MODEL="text-embedding-bge-m3"
$env:EMBEDDING_API_KEY="lm-studio"
$env:RERANKER_BASE_URL="http://127.0.0.1:7997"
$env:RERANKER_MODEL="text-embedding-bge-reranker-v2-m3"
$env:RETRIEVAL_HYBRID_ENABLED="true"
$env:RETRIEVAL_SPARSE_ENABLED="true"
$env:RETRIEVAL_HYBRID_RERANK="true"
$env:RETRIEVAL_FUSION_METHOD="zscore"
$env:RETRIEVAL_FUSION_ALPHA="0.75"
$env:RETRIEVAL_RERANK_CANDIDATES="10"
python evals/hybrid_eval.py --kb-id legal-recall-eval-20261011 --route kb-query --limit 40 --search-k 5 --json-out E:\My-Agentix\retrieval-eval-20261011\hybrid-zscore075-c10-k5.json
```

这组值仅作为本语料/模型下的**显式 opt-in 实验配置**；代码默认的混合检索总闸仍关闭，
不要仅凭 23 题结果把此配置推广为所有知识库的默认值。Recall@5 未提升时，
后续应先分析未命中题的候选覆盖与金标匹配，再决定是否改召回通道或查询解析。

## 16. 已知限制

集中收录各处散落的诚实标注（随实现演进同步修订）：

- **质量门禁尚未武装**：`evals/baseline_predictions.jsonl` 未录制，
  `evals/baselines/quality.json` 为空 ⇒ CI `eval-offline` 处于
  NOT-ENFORCED 三态的"未生效"分支——绿灯只代表"没跑"。
  录制步骤见 [docs/runbooks/eval-recording.md](docs/runbooks/eval-recording.md)。
- **文本直传文件未实现**：`.txt` / `.md` / `.json` / `.csv` / `.log`
  等纯文本上传当前未实现，且不是"稍后再试"（见 §6 上传支持表）。
- **端到端性能与成本数字未实测**：§15.1 只有检索子链路小样本测量，
  不能替代端到端 TTFT、P95 或成本基准。
- **LLM03 供应链 / LLM07 系统提示泄露未专项治理**：
  见 [OWASP 自评](docs/security/owasp-llm-self-assessment.md) 残余风险栏。
- **judge 类指标默认关**：LLM-as-a-Judge 与 ragas 报告（如启用）均不进门禁，
  判官未校准前无资格参与评分（README §4.4 / evals/README 口径）。
- **`graph` 为占位包**（§12 项目结构）：图相关能力按需填充。

---

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

---

## 联系方式

- 📚 **项目主页**：[GitHub Repository](https://github.com/Jia-Yihao/Legelbot)
- 💬 **提问与讨论**：[GitHub Discussions](https://github.com/Jia-Yihao/Legelbot/discussions)
- 🐛 **问题反馈**：[Issue Tracker](https://github.com/Jia-Yihao/Legelbot/issues)
- 🔒 **安全漏洞**：请按 [SECURITY.md](SECURITY.md) 流程私下报告
- 🤝 **贡献指南**：见 [CONTRIBUTING.md](CONTRIBUTING.md)

---

<div align="center">

**My Agentix** — 可控制、可恢复、可评估、可观测的 Agent Platform

[回到顶部](#my-agentix)

</div>
