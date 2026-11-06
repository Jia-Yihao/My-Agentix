<div align="center">
  <img
    src="./docs/assets/agentix-logo.png"
    alt="My Agentix Logo"
    width="360"
  />

  <h3>My Agentix</h3>
  <p>Production-oriented Agent Platform</p>

  <p>单层 Multi-Agent 编排 · Agent Core 2.0 · 多路知识检索 · Control Plane · 评测回归 · 可观测</p>
</div>

> **一个可控制、可取消、可回放的 Agent 运行时。**
>
> 不是又一个 RAG demo——每个 Agent 运行有预算、可取消（真正打断 asyncio Task）、
> 失败可恢复、质量门禁四态可判（见 §8）。
> 3 条命令跑通一次带引用的 RAG 问答，见下方 [最小 Demo](#最小-demo3-条命令跑通一次带引用的-rag-问答)。

<div align="center">

  ![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12-3776AB?style=flat&logo=python&logoColor=white)
  ![FastAPI](https://img.shields.io/badge/FastAPI-0.142-005571?style=flat&logo=fastapi&logoColor=white)
  ![LangGraph](https://img.shields.io/badge/LangGraph-1.2.18-1A5F4F?style=flat&logo=databricks&logoColor=white)
  [![License](https://img.shields.io/badge/License-Apache%202.0-yellow?style=flat)](LICENSE)
  ![Version](https://img.shields.io/badge/version-0.2.0-blue?style=flat)
</div>

---

## 目录

- [最小 Demo：3 条命令跑通一次带引用的 RAG 问答](#最小-demo3-条命令跑通一次带引用的-rag-问答)
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
- [15. 性能与成本](#15-性能与成本)
  - [15.1 本地测试数据语料检索对照](#151-本地测试数据语料检索对照2026-10-11)
  - [15.2 最小切片端到端小样本（2026-10-25）](#152-最小切片端到端小样本本地-lm-studio--sqlite2026-10-25)
- [16. 已知限制](#16-已知限制)
- [参与贡献](#参与贡献)
- [许可证](#许可证)
- [联系方式](#联系方式)

---

## 最小 Demo：3 条命令跑通一次带引用的 RAG 问答

> **不依赖 Neo4j / Milvus / DeepSeek，也不需要 Docker**：模型全部走本机 LM Studio
> （`qwen/qwen3-8b` 生成 + `text-embedding-bge-m3` 嵌入），存储是 SQLite，向量索引是
> FAISS（`VECTOR_STORE_BACKEND` 的出厂默认值），BM25 是进程内纯 Python——
> 路由 → kb 检索 → 带引用回答整条链路在同一个 Python 进程里跑完。

```bash
python scripts/demo/local_kb_demo.py seed                        # ① 4,865 条语料 → SQLite + FAISS（冷缓存约 90s；重跑命中本地嵌入缓存约 4s）
python scripts/demo/local_kb_demo.py ask "合同欺诈的认定依据是哪一条"    # ② 路由 → 检索 → 带引用回答
python scripts/demo/local_kb_demo.py eval --golden <kb-query 金标集>.jsonl --limit 23 --recall-k 5   # ③ 可选：需带 expected_sources 的 kb-query 金标；仓内只有 C-Eval MCQ 集，此命令暂无可跑题集（见 §8）
```

前置只有一句：`lms server start`（或界面里开 HTTP 服务，端口 1234）；三条命令都不需要
`.env` 里的云端 key。解释器要用**仓库 `.venv`**（Python 3.12），不要用系统 conda——
理由与完整前置见 docs/runbooks/性能与成本采数.md「宿主直跑」。

> **跑不通怎么办**：
> ① `ask` 报 `Failed to load model "qwen/qwen3-8b": Engine protocol startup was aborted` ⇒
> 先 `lms load qwen/qwen3-8b` 再跑（8GB 显存冷加载不稳，实测重试第二次即通）；
> ② `seed` 慢是正常的（冷缓存约 90s，重跑命中本地嵌入缓存约 4s）；
> ③ 解释器必须是仓库 `.venv`（3.12）；换栈会换结构化输出形态（能力探测自动选，见脚本顶部）。
> 完整口径与复现命令见 docs/runbooks/性能与成本采数.md。

②的真实终端输出（`.venv`，2026-10-25，未剪辑，仅省略正文）：

```text
本地适配   : 环境指纹: python 3.12.13 / openai 1.109.1 / langchain-openai 0.2.14 / langchain-core 0.3.86
本地适配   : with_structured_output ⇒ method=json_schema
本地适配   : router 角色超时 10s ⇒ 60s
预热     : 模型驻留 206ms（不计入端到端延迟）
路由     : kb-query  confidence=0.95
判定依据 : 该问题涉及法律条文的认定依据，属于知识库中关于规则与观点的查询范畴。需要从相关法律法规或司法解释中检索具体条款内容。
检索状态 : ok  命中=5  search_k=5  525ms
  [1] 《民法典总则编司法解释》第21条
  [2] 《民法典合同编通则司法解释》第5条
  [3] 《刑法》第224条
  [4] 《民法典合同编通则司法解释》第17条
  [5] 《刑法》第167条
回答     : 合同欺诈的认定依据主要涉及……（逐条引用上面 3 个条目，末尾带免责声明）
生成=6934ms 端到端=21445ms token prompt=942/completion=266
```

「判定依据」那一行现在写的是**模型自己给的理由**；如果它写成 `kb-query` 而路由行没有
confidence，说明路由结构化输出走了兜底（见 §16 的 `router_fallback_total`）。

问一句寒暄（`ask "你好，你是谁"`）会看到路由节点判成 `general-query` 并以退出码 3 停下——
这个切片只跑 `kb-query` 一条链路，其余分支属于下面的可选能力。加 `--hybrid` 会显式打开
BM25+Dense 融合（代码默认关，见 工程约定 1），同一条问题的 top-5 里多出
《民法典》第148/149条 两条欺诈本条（实测日志
`hybrid retrieval kb=legal-demo dense=20 sparse=on fused=20 rerank=closed docs=5`，
末项 `rerank=closed` 是 **rerank 熔断器的状态字符串**（closed＝熔断器闭合、失败数未达阈值），
不是"精排是否启用/执行"的开关读数——本机没有 Cross-Encoder 服务时精排按 fail-open 降级，
这两处口径不得互引）。

**三条实现口径**（细节写在脚本顶部与 §15.2，这里只留读者需要知道的）：

1. 脚本自建隔离环境（`data/demo/`），**不碰仓库既有库与索引**；`seed` 可重复执行，并有 `index.doc_count == chunks` 守卫。
2. 两处仅作用于本进程的本地适配（结构化输出形态、路由超时 60s）会打印环境指纹；按能力探测选择，不改生产代码。
3. 端到端含路由 + 检索 + 生成；本机 8B 上路由占 64%，命中判定是 silver，数字对 Python 栈敏感——换栈检索质量逐题一致，但**延迟差 23 倍**（§15.2 口径③）。

**这只是「看它能跑」**：要跑完整平台（含 Neo4j / Milvus / MCP / 前端），见 [§10 快速开始](#10-快速开始)。

**可选能力**（需要额外基础设施，不在三条命令范围内，逐项见 §3/§6 与 [`docs/`](docs/)）：
GraphRAG（Neo4j + Infinity）· Text2SQL（MySQL）· MCP 工具进程 · 多智能体协作与任务执行 ·
Milvus 向量后端 · 云端 LLM（DeepSeek/OpenAI 兼容）· 全栈 `docker compose` 起法（§10）。

---

## 1. 平台概览

My Agentix 是一套**生产导向的 Agent 平台**——以 Agent 内核为核心、集成 RAG 等多路检索的综合服务平台，把构建、运行、治理智能体所需的内核能力做在同一套代码里：

| 能力域 | 内容 |
|--------|------|
| **Agent Core 2.0** | 规划 / 执行 / 观测 / 验证 / 纠偏 / 记忆 / HITL / 事件 / 多智能体 / 演化 |
| **Agent Runtime** | 运行状态 / 步骤轨迹 / 六维预算 / 生命周期 |
| **Control Plane** | spawn · status · cancel，取消可真正打断 asyncio Task |
| **ToolExecutor** | 权限 → 风险 → 超时 → 重试 → 轨迹，统一治理 |
| **MCP** | Client + Server，可挂载任意 MCP 工具进程 |
| **知识系统** | 图谱 / 向量 / SQL / 社区摘要多路检索与兜底 |
| **Adaptive Execution** | 档位路由 / fast-path / 准入排队 / 重试预算 / 负载甩载 / 隔离舱 / 单飞合流（全部 flag 灰度、默认基线等价） |
| **评测与门禁** | 质量门禁四态（评测集换代中、基线未录，暂未武装，见 §8）、轨迹前缀回归、延迟基准与每日性能冒烟（`perf-baseline.yml`，不阻塞 PR，见 §11） |
| **可观测** | 步骤级轨迹、Explanation API、SLO 与告警、Prometheus 指标、OTel GenAI 语义 span |

**平台定位**：它不是「某个垂直领域的问答机器人」，而是一套**领域无关的 Agent 平台内核**——领域知识（图谱 Schema、提示词、评测集、数据）通过适配层接入，同一套内核可迁移到咨询 / 客服 / 政务等垂直领域，平台本身与具体业务域解耦。

**运行形态**：对外暴露 FastAPI 服务（SSE 流式对话 + 检索/任务级 Run API），LLM 编排由 LangGraph **单层主图**承载（10 个节点平铺 + 条件边路由），Agent Core 2.0 提供可独立复用的内核（Harness + Capabilities），并以 `agentix/runtime` 收敛运行时真源。

---

## 2. 为什么选择 Agentix

与顶部三句话（可控、可取消、可回归）对齐，这里只留五条主线；其余能力归入对应章节，不在这里铺开。

- **可控制的执行**：每个 Agent 运行有 `run_id`、六维预算（步数 / LLM / 工具 / token / 时延 / 成本）与超时，可查询、可取消
- **可恢复的失败**：错误分类 → 退避重试 → 模型 / 工具 Fallback 链 → 降级拒答，绝不悬挂
- **可评估的质量**：确定性打分口径（路由 / 引用 / 拒答 / 引用正确性 / must_contain / pass^k）+ 声称一致性 / 提示敏感性 / 隐私标记（Phase 31）+ 质量门禁四态（**评测集已于 2026-10-26 下线，门禁当前未武装**，见 §8）+ 轨迹前缀回归 + 延迟基准（性能冒烟跑在 `perf-baseline.yml`，见 §11）
- **可观测的轨迹**：步骤级轨迹落盘可回放、前端执行轨迹时间线、Explanation API、SLO 与告警、OTel GenAI 语义属性（token 用量归模型）
- **可扩展的边界**：AST 分层依赖守卫 + 架构守卫测试，违反即 CI 失败；注册表可插拔，新增工具 / Provider / 子图不改调用链

**另有准入排队与过载甩载、流式渲染、领域适配层**三项，见 §1 能力表、§6 能力模块与 [扩展指南](docs/architecture/扩展机制.md)。

---
## 3. 架构

> **这一章证明什么**：我能把多 Agent 编排拆成可独立复用的层，并让依赖规则由守卫而不是口头约定来维持。

<div align="center">
  <img src="./docs/assets/project-architecture.png" alt="My Agentix 项目整体架构图" width="100%">
  <p><i>两套问答域（测试数据编排 / 检索）+ 知识入库流；默认主链路为 Vue3 → Nginx → FastAPI → ChatService → LangGraph 主图 → Tool / Retrieval → 数据层，橙色虚线为默认关闭/可选能力。</i></p>
</div>

> **文档层级（三层，按「你此刻要回答什么问题」选）**
>
> | 你要回答 | 看哪 | 定位 |
> |---|---|---|
> | 这个项目**现在是什么样** | **本文档** | 唯一「现行总览」 |
> | 某处**具体怎么实现** | [`docs/项目实现手册.md`](docs/项目实现手册.md) | 实现细节手册（按符号定位，不写行号） |
> | 出事/上线**要做什么动作** | `docs/runbooks/` | 操作手册 |
>

### 三条端到端数据流

系统里跑的不是一条链路，而是三条互不依赖的流。判断一段代码属于哪条流，比判断它在哪个目录更有用。

```mermaid
flowchart LR
    subgraph Domain1["测试数据编排域 /api/v1"]
        L1["Web 对话 / 外部 API"] --> L2["Nginx"] --> L3["FastAPI + 中间件"]
        L3 --> L4["ChatService"]
        L4 --> L5["LangGraph 主图"]
        L5 --> L6["KB 子图 / KG 子图 / 直接出口"]
        L6 --> L7["工具与数据层"]
        L7 --> L8["答案 + SSE"]
    end

    subgraph Domain2["检索域 /api/v1/retrieval"]
        R1["Web 知识库"] --> R2["FastAPI"] --> R3["JWT + RBAC + scope"]
        R3 --> R4["Retrieval ChatService"]
        R4 --> R5["ReAct Runner（无 LangGraph）"]
        R5 --> R6["混合检索 / 工具"]
        R6 --> R7["引用核验 + SSE"]
    end

    subgraph Ingest["知识入库流"]
        I1["Excel / MySQL"] --> I2["kb_ingest API"]
        I2 --> I3["Celery retrieval-worker"]
        I3 --> I4["解析 / 切块 / 向量化"]
        I4 --> I5[("Milvus / MinIO / PostgreSQL")]
    end
```

| 维度 | 测试数据编排域 | 检索域 | 入库流 |
|---|---|---|---|
| 是否用 LangGraph | 是（单层主图；可选 AgentLoop 执行循环） | 否（自建 ReAct 循环） | 否 |
| 会话标识 | `session_id` → `thread_id` | 会话 + 用户 + 知识库作用域 | 任务/文档 ID |
| 流式协议 | `metadata` / `message` / `tool_start` / `tool_end` / `done` / `error` | `meta` / `progress` / `tool_running` / `answer_chunk` / `error` | 任务状态轮询/回调 |
| 主要鉴权 | API Key（默认关闭） | JWT 强制 + RBAC | 服务配置 |
| 典型存储 | PostgreSQL / Redis / Neo4j / Milvus | PostgreSQL / Milvus / MinIO | MySQL / Milvus / MinIO / PostgreSQL |

### 单层主图与请求链路

编排只有**一张主图**：`lg_builder.build_graph` 用 `StateGraph` 平铺 10 个节点，路由用条件边
（`route_query`）完成，没有「层 → 层」的嵌套调用。重业务的两个分支（`graphrag` / `kb`）各自在
**节点内部**再嵌一个多角色子图——子图是节点的实现细节，不是架构层级。

| 组成 | 职责 | 落地 |
|-----|------|---------|
| 唯一主图 | 节点平铺 + 条件边路由，`START` → `message_window` → `analyze_and_route_query` | `agentix/orchestration/langgraph/lg_builder.py` |
| 路由节点 | 意图识别（启发式 + LLM 双保险），产出条件边的分支键 | `analyze_and_route_query` + `route_query` |
| 业务节点 | 一次问答 / 检索 / 规划：`general` / `additional` / `research_plan` / `image` / `file` / `kb` | `agentix/application/agents/` |
| 执行节点 | 任务执行与代码执行，由 `TASK_EXEC_ENABLED` / `CODE_EXEC_ENABLED` 在路由侧门控 | `execute_task` / `code_agent` |
| 节点内子图（仅两处） | `graphrag` 走 `create_multi_tool_workflow`；`kb` 走 `create_kb_multi_tool_workflow` | `.../workflows/multi_agent/multi_tool.py` |
| 工具出口 | 所有原子工具经 Tool Gateway 统一出入（权限 / 风险 / 超时 / 重试 / HITL / 审计） | `agentix/tools/gateway/` |

「单层路由」是**可选开关**而非当前默认：`ROUTING_SINGLE_LAYER_ENABLED` 默认 `false`，开启后
路由节点一次调用同时产出意图与执行参数，`kb` 子图内的 A3 节点不再做第二次检索路由（kb-query 跳数
4→3）。默认关时路由链、提示词与图可达性与基线逐位等价。

### 运行时内核与横切能力

| 组成 | 职责 | 落地 |
|------|------|------|
| **Agent Core 2.0** | 主循环 / 规划 / 纠偏 / 验证 / 记忆 / HITL / 事件 / 多智能体 / 演化 | `agentix/orchestration/` + `agentix/policies/` + `agentix/agent/` + `agentix/tools/` |
| **Agent Runtime** | 运行状态 / 步骤轨迹 / 六维预算 / 生命周期 | `agentix/runtime/` |
| **Control Plane** | 运行登记（有界 FIFO）+ 取消（打断 asyncio Task） | `agentix/runtime/control_plane.py` |
| **性能治理** | 分段延迟 / 预算 / 准入排队 / 过载降级 | `agentix/performance/` |
| **MCP** | 任意 MCP 工具进程接入 + 零依赖 stdio Server | `agentix/capabilities/mcp/` |
| **执行沙箱（⑮ 起有墙）** | 按 `ToolSpec.sandbox` 声明选执行位置：`in_process` / `thread` 不隔离（同进程同 GIL）；`subprocess` = 一次调用一个进程 + 子进程侧 `RLIMIT_AS/CPU/FSIZE` + 进程数闸 `RLIMIT_NPROC`（**只在配了独立且非 0 的 uid 时落**）+ 环境白名单 + 可杀进程组 + **应答按 nonce 归属且 `ok` 严格布尔**（**POSIX 专属，默认关**）；`container` 未实现 ⇒ 声明即在执行点被拒 | `agentix/tools/sandbox.py` · `agentix/tools/subprocess_worker.py` · `agentix/domain/tool/exec_env.py` |
| **运行时写代码执行（code exec）** | 模型在运行时写 Python 代码完成任务：经 `code_execute` 工具走 `subprocess` 沙箱档（一次调用一个进程 + `RLIMIT_AS/CPU/FSIZE` 三道闸 + 环境白名单 + 可杀进程组）；**沙箱隔离但 POSIX 专属，默认关**（`CODE_EXEC_ENABLED=false`，flag-off 与基线逐位等价）；**不隔离文件系统与网络** | `docs/architecture/adr/0007-执行沙箱端口.md` |
| **可观测** | trace_id 全链路 · 步骤轨迹 · Prometheus 指标 · SLO | `agentix/observability/` |
| **评测与门禁** | 确定性打分 · 质量门禁 · 轨迹前缀回归（评测集换代中、基线未录，代码保留） | `evals/` |

### 终态统一执行链（Stage A/B/M 收敛后）

```text
API / SSE / Run API（仅协议转换）
→ 应用层唯一入口创建 Run（ControlPlane 准入 · 单 owner，取消真正打断 asyncio Task）
→ 唯一执行循环（LangGraph 主图 或 AgentLoop，AGENT_LOOP_ENABLED 灰度）
   Context → Plan → Execute → Observe → Verify（Replan 受 Runtime 预算约束）
→ Runtime（agentix/runtime/：状态 / 步骤 / 六维预算 / 生命周期 / 统一退出契约）
→ Tool Gateway（agentix/tools/gateway/：权限 / 风险 / 超时 / 重试 / HITL / 审计）
→ Capabilities（tools/ · retrieval/ · kg 子图 · capabilities/mcp · memory-tools）
→ Infrastructure（infrastructure/：Milvus / Neo4j / Redis · LLM / 外部服务）
```

LangGraph 只是编排实现（状态图 / 条件边 / 并行节点 / 恢复点），SSE / Run API /
轨迹 / 评测消费同一条 Run/Event 真源。

### 一次请求的执行路径

1. 用户提问 → 中间件链（限流 → trace_id → 请求日志 → APIKey 鉴权 → CORS）→ ChatService
2. LangGraph 主图 `lg_builder.build_graph` → `message_window` 历史窗口化（条数窗口 + `LG_TOKEN_HISTORY_BUDGET` token 级预算，默认 0=关；ChatService 侧可注入级联指令块 `LG_INSTRUCTIONS_ENABLED` 与状态尾注 `LG_STATUS_TAIL_ENABLED`，均默认关）
3. **路由节点** `analyze_and_route_query`（启发式 + LLM 双保险）判定意图，条件边据此选分支：`general / additional / graphrag / kb / image / file / text2sql`（`TASK_EXEC_ENABLED` 开启后另追加 `task-query` 任务型路由，`CODE_EXEC_ENABLED` 开启后另追加 `code-query` 代码型路由）
4. 分支落到**主图上的一个业务节点**（节点之间平级，不是上下层）：`create_research_plan` / `create_kb_query` / `execute_task` / `code_agent` 等。只有 `graphrag` 与 `kb` 两个节点**内部**再跑子图：`guardrails → planner → tool_selection → 并行工具 → summarize / finalize`
5. 原子工具经 Tool Gateway 并行检索：Neo4j Cypher / Milvus 向量 / LightRAG 社区摘要 / Text2SQL / 外部搜索 / MCP 远端工具
6. 结果融合并附来源引用，经 Agent Core 主循环记账（AgentState / Steps / Budget），轨迹落盘可回放

### 默认开关与能力矩阵

下表只列「会改变主链路形态」的开关；完整键表、默认值与回滚方式见 [配置管理.md](docs/architecture/配置管理.md)。**所有默认值以 `Settings` 代码为准**，本表只是索引。

**默认开启**（改了会变更主链路形态，慎动）：

| 开关 | 默认 | 影响 |
|---|---|---|
| `MEMORY_CARDS_ENABLED` | `True` | 记忆卡能力 |
| `PROMPT_INJECTION_DETECTION_ENABLED` | `True` | 提示注入检测 |
| `RERANK_ENABLED` | `True` | 检索结果重排 |
| 检索域 `OUTPUT_FILTER_ENABLED` | `True` | 输出过滤 |
| 检索域 `OUTPUT_FILTER_PII_ENABLED` | `True` | PII 脱敏 |
| 检索域 `VECTOR_STORE_BACKEND` | `faiss` | 默认向量后端 |

**默认关闭**（打开即换形态，属灰度面）：

| 开关 | 默认 | 打开后的效果 |
|---|---|---|
| `AGENT_LOOP_ENABLED` | `False` | `ask` 走 AgentLoop 执行循环 |
| `TASK_EXEC_ENABLED` | `False` | `execute_task` 可达 |
| `AGENTIX_ABLATION_MULTI_AGENT` | `False` | 切换 Supervisor 多智能体拓扑 |
| `HUMAN_REVIEW_ENABLED` | `False` | 工具执行接入人工审批 |
| `TOOL_GATEWAY_ENFORCED` | `False` | KG 工具也强制走网关 |
| `SKILLS_ENABLED` / `HOOKS_ENABLED` | `False` | 装配 Skills / 生命周期 Hooks |
| `MEMORY_CONTEXT_ENABLED` | `False` | 长期记忆注入上下文 |
| `CONTEXT_AUTOCOMPACT` | `False` | 上下文自动压缩 |
| `RATE_LIMIT_ENABLED` / `ADMISSION_CONTROL_ENABLED` / `LOAD_SHEDDING_ENABLED` | `False` | 全局限流 / 准入排队 / 过载甩载 |
| `RETRY_BUDGET_ENABLED` / `FEEDBACK_ENABLED` / `NOTIFY_USER_ENABLED` | `False` | 重试预算 / 用户反馈 / 用户通知 |
| `MULTIAGENT_BUS_ENABLED` / `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` / `SUBAGENT_RESULT_CONTRACT_ENABLED` | `False` | 事件总线 / 子代理隔离 / 子代理结果契约 |
| `AGENTIC_RAG_ENABLED` / `KG_PLANNER_UNIFIED_ENABLED` | `False` | Agentic RAG 路径 / KG Planner 统一路径 |
| `V1_REQUIRE_JWT` | `False` | 测试数据域 V1 强制 JWT（检索域不受此开关影响） |
| `SSE_COALESCE_ENABLED` | `False` | SSE 合流/背压 |

**怎么判断一个能力是否真在默认链路里**（不要只看文档标题，按顺序查五处）：

1. **`Settings` 默认值**——`agentix/config/settings.py` 与 `agentix/retrieval/settings.py`；
2. **组合根是否接线**——`agentix/main.py` 的 `_startup` 在 flag 开启时是否装配；
3. **图里是否注册**——`lg_builder.py` 是否 `add_node` / `add_edge`；
4. **调用点是否可达**——静态搜引用；未注册、未调用的实现属于「已实现但未接线」；
5. **守卫与测试**——`tests/test_p41_doc_consistency.py` 防文档漂移，分层守卫防依赖回潮。

完整架构图、设计特色与逐节点流程见 **架构详解**；分层与依赖规则见 **[分层架构与依赖规则.md](docs/architecture/分层架构与依赖规则.md)**；运行时职责五层见 **AgentCore五层架构.md**；逐环节「为何用自主 Agent 而非 Workflow」的论证见 **编排方案选型论证.md**。

---

## 4. Agent Core 2.0

`agentix/orchestration/`（orchestrator 主循环 + executor + langgraph 编排适配 + strategies/multi_agent）与 `agentix/policies/`（planner / verifier / hitl / guardrails 策略件）构成可独立复用的 Agent 内核，`agentix/agent/` 为 Context Provider 家（context / memory / events / observer / skills / hooks / evolution），`agentix/tools/` 是工具唯一执行入口；LangGraph 编排集中在 `agentix/orchestration/langgraph/`（lg_builder 主图 + lg_supervisor 拓扑）与 `agentix/application/agents/`（kg 子图 / text2sql / 业务 prompt），受 p66 导入白名单棘轮（仅登记在册 11 文件）约束，其余模块零 LangGraph 依赖。

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

五层职责与依赖硬规则见 **Agent Core 五层架构**；离线走查见 **Research Agent Demo**；当前目录结构与模块职责见 **[§12 项目结构](#12-项目结构)**。

**依赖边界（CI 强制）**：`domain/` 零第三方；LangGraph 导入受 p66 白名单棘轮（equality 断言，仅登记在册 11 文件，新增须署名理由）；`capabilities/` 不反向依赖业务循环；Runtime 真源在 `agentix/runtime/`（Strangler shim `application/runtime/` 已随 A4 删除）。

---

## 5. Agent Runtime

> **这一章证明什么**：我知道 Agent 执行不可控在哪（预算、取消、超时、状态），以及怎么把它们收敛成运行时契约。

超越 Chatbot 的运行时内核（`agentix/runtime/`，接线在 `agentix/application/services/`）：

| 组件 | 说明 |
|------|------|
| **AgentState / ExecutionStep** | 统一运行状态与步骤轨迹（PLAN / EXECUTING / VERIFYING…），自动计时与预算记账 |
| **BudgetManager** | 步数 / LLM 调用 / 工具调用 / token / 时延 / 成本六维预算，超限抛 `BudgetExceededError` 由调用方决策 STOP 或降级 |
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

知识库填充（条目 / 语料块数量依领域数据而定，见 **数据准备**）。

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

> 上述界面截图来自本地隔离测试账号与测试数据库（2026-10-22 实机重拍），不包含真实用户数据；截图文案与当前实现一致（见
> `web/src/modules/knowledge/components/chat/HomeWelcomeHero.vue`、`web/src/modules/knowledge/views/HomeView.vue`）。

**RAG 检索质量口径**：当前以 STARD 公开集（1,543 题、精确 docid 金标）为准，复现入口是 `evals/stard_eval.py`，指标与方法消融见 README §15.3。该集上的结论是：口语咨询问法下 Dense 单通道召回高于 BM25，词法查询改写 / 扩展 / 多查询融合不提升召回，把 BM25 用 RRF 并入 Dense 反而稀释语义信号。生产默认因此保持 Dense 单通道、混合总闸默认关；`RETRIEVAL_DENSE_RERANK_ENABLED` 可在不打开混合总闸的情况下对放大的 dense 候选池做 Cross-Encoder 精排，服务不可用时按原召回顺序 fail-open。混合检索的 Dense 与 BM25 候选并发获取，BM25 倒排 posting 只扫描命中查询词的 chunk，并把阿拉伯数字与中文数字条号规范到同一词项；索引快照按活动 `artifact_id` 隔离，语料只取该版本 manifest 内 `ready` 文档的 chunk。LM Studio 的 embedding 接口不能代替 `/rerank` 接口，未成功精排明确记为降级。检索开关与调优口径见 `docs/architecture/配置管理.md`。

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
- **可靠性原语**：限流（全局 + 路由级）· 熔断（circuit breaker）· 并发限制，见 `agentix/reliability/`。**熔断的接线范围分开说（两个概念不互相援引）**：检索域两处**已接线**——① 检索 rerank 熔断（`agentix/retrieval/infra/rag/rerank.py`，熔断 OPEN / 半开探测额度用尽 ⇒ 快速降级、保留原召回顺序），② 检索链 LLM 调用熔断（`agentix/retrieval/usecases/chat_llm.py::invoke_with_breaker`，`LLM_CIRCUIT_BREAKER_ENABLED` 默认开）；**通用 CircuitBreaker 原语**（`agentix/reliability/circuit_breaker.py` 的装饰器与类直接用法）实现存在、未接线进检索域以外的生产链路（主编排 / 工具执行等无熔断闸），对外不声称"通用熔断已在生产链路生效"（发布门禁 release gate 已于 A6 删除，现由 `.github/workflows/release.yml` preflight + `evals/quality_gate.py` 承担）
- **出站通知**（Phase 23）：`NOTIFY_USER_ENABLED=true` 注册 `notify_user` 工具——通知落 EventPort（type=`user_notification`）并经 `GET /api/v1/ops/events` 可查；声明 L1 → MEDIUM，开启 `HUMAN_REVIEW_ENABLED` 时**先审批后执行**（出站即副作用，不得绕闸）；当前未接真实邮件/短信通道
- **配置漂移告警**（Phase 22）：会话中途检测配置哈希变化 → 结构化 `system_notice`（仅键名不含值），实现见 `application/services/config_watch.py`
- **流式防挂起**：SSE 逐次取事件包 `STREAM_IDLE_TIMEOUT`（默认 60s）看门狗——上游静默挂起即 error + done 兜底收口并归还并发槽位，连接绝不悬挂
- **预算意识**（Phase 28）：步数预算观测，轨迹 append `budget_notice`（剩余 ≤2 可见、不动静态前缀）+ 状态栏 `max_iter` 常量键（`BUDGET_NOTICE_ENABLED` 默认关）
- **执行边界加固**（§24.3 T0）：治理链回答「该不该做」，这一层回答「跑出去之后能不能切断」——非协程工具 handler 下放线程后 `timeout_ms` 才真能截断（`TOOL_SYNC_OFFLOAD_ENABLED`，默认关）、MCP 子进程只拿白名单环境不再继承全部密钥（`MCP_SUBPROCESS_ENV_SCRUB`）、表达式资源护栏（长度/指数/位宽/浮点有限性，语法白名单挡不住 `9**9**9`）、text2sql 两条闸门共用同一份只读词表（先等长抹除字面量再按词边界匹配）、日志凭据脱敏、`engine.echo` 与生产身份从 `DEBUG` 解耦（`DB_ECHO`/`APP_ENV`/`UVICORN_RELOAD`）、compose backend 加 `no-new-privileges` + CPU/内存/pids 配额；后续补上 T1 ⑫ 提前落地的 `kb_ingest` 连接串闸门（`KB_MYSQL_URL_GUARD`，默认关）与 T1 ⑧ 的 handler 类型契约告警（`TOOL_HANDLER_CONTRACT_ENABLED`，默认关：注册期点名非协程 handler，把「这条工具的超时为什么不生效」从排障现场前移到启动日志）、以及 T1 ⑬ 的数据库 engine 惰性化（`agentix/infrastructure/core/database.py` 不再在模块导入期建池，`import agentix.capabilities.mcp` 连带把整个数据面拉起来的副作用已切断；`engine` / `SessionLocal` 旧写法仍可用，只是取用才建）、以及 T0 ⑤ 的容器运行身份旋钮（`AGENTIX_CONTAINER_USER`，默认 `0:0` 仍是 root——降权要先准备宿主目录属主，否则失败形态是 restart 崩溃循环而不是拒绝启动，步骤见 staging Runbook）、以及 T1 ⑪ 的设置接口授权闸门（`SETTINGS_AUTHZ_ENABLED`，默认关：打开后 `GET /settings/{kb_id}` 要求该 kb 属于调用者租户、`PUT` 再要求 admin。**为什么掩码不够**——`llm_base_url` 这类字段本身可写，改指外部端点不需要密钥却足以让该租户的提问整批外送；「掩码」堵的是读、「授权」才堵得住写。开启有两个前置：迁移已回填 `knowledgebase.tenant_id`（NULL 走 fail-closed 一律 403）、且该租户内确有 admin（全仓暂无自助升 admin 的接口））、以及 T1 ⑫ 的第二半——导入过滤参数化（`KB_MYSQL_WHERE_PARAMETRIZED`，默认关：开则原始 `where` 片段一律拒，过滤只能走 `{column, op, value}` 结构化条件，列名过标识符白名单、运算符取封闭枚举、值全部走 bindparams。**为什么不是"再补几个黑名单词"**——黑名单拦得住 `;` 堆叠，拦不住 `1=1 OR 1=1` 这类合法但越界的片段，对拼出来的 SQL 而言调用方给的就是语法；也因此这一位是硬切而不是加字段，调用方要先迁完）、以及 T2 ⑭ 的执行位置端口（`EXECUTION_SANDBOX_ENABLED`，默认关：`ToolSpec.sandbox` 把「这条工具在哪儿跑」从写死在 `_invoke` 里变成声明，能跑的 `in_process` / `thread` 两档中后者让**单条工具**不必为它打开全局 `TOOL_SYNC_OFFLOAD_ENABLED` 就能下放线程；`container` 仍未实现 ⇒ 声明它在执行点被拒而不是静默退回进程内。**注意这两档都不是隔离**：同进程、同 GIL、同环境变量，端口的价值是让 ⑮ 的墙有地方接）、以及 T2 ⑮ 的子进程沙箱档（`TOOL_SANDBOX_SUBPROCESS_ENABLED`，默认关：开了、且某条工具**逐条声明** `sandbox="subprocess"`，才真的走墙。**一次调用一个进程**，子进程侧先落资源闸 `RLIMIT_AS=512MiB` / `RLIMIT_CPU=30s` / `RLIMIT_FSIZE=32MiB`（可选 `TOOL_SANDBOX_SUBPROCESS_UID=-1` 不降权；root 容器实测降到 65534）**再 import 业务代码**，环境变量走与 G2 **同一份**白名单 `agentix/domain/tool/exec_env.py`（实测父进程里的 `DATABASE_URL`/`LLM_API_KEY` 不过界），协议是 stdin 一条 JSON 请求 + stdout 一行带哨兵的 JSON 应答、两侧各有体积闸，**且成功信封必须回显本次调用的 nonce**（墙内代码自己 `print` 一条"成功信封"换不到它——旧口径下那条会被父进程当成成功结果返回，已实测复现；**且 `ok` 必须是严格布尔**，`"ok": 1` 这种伪造件同样会被真值判定当成成功，归属凭证在那条路上压根没执行），取消经 `start_new_session` + `os.killpg` 真杀进程组；⑯ 的能力探测不过（Windows 缺 `resource` 与进程组 kill）⇒ 执行点响亮拒绝，一次 handler 都不调、一个进程都不起。**它是墙，不是笼子**：挡住地址空间 / CPU 秒 / 文件大小 / 环境密钥 / 可杀性，另买到**带前置的**进程数闸（`RLIMIT_NPROC`，上限 32 个**任务**——线程也算：实测 soft=8 时先起 7 个线程、fork 预算就归 0；只在 `TOOL_SANDBOX_SUBPROCESS_UID` 配了**独立且非 0** 的 uid 时落，共用父 uid 会被假拦或形同虚设，uid 0 因持 `CAP_SYS_RESOURCE` 被内核豁免 ⇒ 明确不落并告警一次），**仍没挡住**文件权限与出网（只读根、指定可写目录、出网默认拒绝三件属 ⑰/T3）。代价实测：单次调用 p50 **≈50ms**（容器本机 FS），同一条 handler 走 `in_process` 档 p50 **0.001ms**、`thread` 档 **0.074ms** ⇒ 默认档必须留在进程内。取证步骤与数字表见 subprocess 沙箱验证 Runbook）。T2 G14 的同名重复注册点名告警也在这层（无开关、默认打印；`TOOL_DUPLICATE_REGISTRATION_STRICT` 可选、默认关）。口径见 [运行时护栏 §15](docs/architecture/运行时护栏.md)、选型与被拒方案见 [项目实现手册 §23.8](docs/项目实现手册.md)、逐项实测见 [项目实现手册 §24](docs/项目实现手册.md)
- **运行时写代码执行**（默认关）：模型在运行时**自己写 Python 代码**完成任务，而不是把每一步翻译成既有工具调用（`code-query` 意图 → `code_agent` 节点 → `code_execute` 工具 → `subprocess` 沙箱档）。失败语义分两层：**治理层**失败（沙箱档位不可用 / 风险被拒 / HITL 未批 / 参数非法）**终局**——重试换不来一个实现，不进自愈循环；**运行层**失败（退出码非零 / 超时）才把真实 stdout/stderr 回灌让模型改码，最多 `CODE_EXEC_MAX_ROUNDS` 轮**尝试（含首次执行）**，轮数用尽产出**含最后一次输出**的失败回执、不假装成功。**POSIX 专属**，且**不隔离文件系统与网络**。口径见 `docs/项目实现手册.md` 里 code exec 的配置段

**`kb_ingest` 服务间鉴权**：该服务是**写入语义**（入库直接改 Milvus 集合），且 compose 以 `ports: 8100:8000` 发布、`uvicorn --host 0.0.0.0` 启动。现已于 **router 级**挂载 `X-API-Key` 校验（router 级而非逐端点，避免新端点静默裸奔）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `KB_REQUIRE_API_KEY` | 是否强制校验；`false` 时启动期打 **WARNING** 并点名暴露的端口 | `false` |
| `KB_API_KEY` | 共享密钥。开启校验后必填，留空则**全部请求 401**（fail-closed） | 空 |

> ⚠️ 密钥**刻意不复用**主应用的 `API_KEY`（避免权限抬升），改用服务作用域的 `KB_*`，两支密钥可独立轮换。客户端侧由 `agentix/domain/policies/ingest_auth.py` 统一出站鉴权头，且**只对指向本仓 `kb_ingest` 的同源地址**附带密钥（按 origin 比较）。

详见 [运行时护栏](docs/architecture/运行时护栏.md) 与 [配置管理](docs/architecture/配置管理.md)。

---
## 8. 评测

> **这一章证明什么**：我知道怎么判断一个 Agent 是变好还是变坏，以及怎么防止“门禁被悄悄放宽”。

> **评测集已完成换代**：旧的 KG/RAG 法律金标（102 条）与录制基线已于 2026-10-26 停用删除，
> CI 的 `eval-offline` 门禁机位随之暂停。评测代码全部保留（确定性打分口径、门禁四态、
> 轨迹前缀回归、消融框架），题集改为公开 C-Eval MCQ（156 行 = 52 学科 × 3 题，**非商用许可**）。
>
> ✅ **全量实测读数（2026-11-05，本机 LM Studio qwen3-8b，扁平拓扑 156 题全量）**：
> MCQ 正确率 **0.7097**（110/155 可判分，1 题抽不出字母）、路由准确率 **1.0**、
> 单题墙钟 p50 **5923 ms** / p90 **6931 ms**、整轮 **16 分 18 秒**、零拒答零缺失。
> 多 agent 拓扑在同题集上跑 22 题后中止（单题 p50 **9628 ms**，约为扁平拓扑的 1.6 倍），
> 部分样本正确率 0.7273（16/22），样本过小不作结论。
>
> 评测线不产出 `quality.json` 质量基线（C-Eval 无引用要求样本），因此门禁四态仍处于未武装状态；
> 打分与门禁代码全部保留，接入带引用要求的题集即可重新武装。跑法见
> [evals/README.md §10](evals/README.md#10-公开-mcq-评测集-c-eval)。

```bash
# 离线跑分（用已录制的基线预测打分，CI 同款）——需要评测集，缺输入会明确报错
./.venv/Scripts/python.exe evals/run_eval.py --offline

# 在线跑分 + Pass^k 可靠性（需 LM Studio / LLM 服务）——需要评测集
./.venv/Scripts/python.exe evals/run_eval.py --live --repeat 3

# 轨迹前缀回归（基线 vs 候选，逐位置比对，退出码可进 CI）
#   -b 必须指向**已录制的轨迹包目录**：脚本把该目录下每个 *.json 都当轨迹包读，
#   而 `evals/baselines/` 是门禁基线目录（它不是轨迹包，没有 `steps`），
#   照 `-b evals/baselines` 直跑会以「[错误] …缺少 'steps' 字段」返回码 2 退出。
#   这是 fail-loud 的预期行为。⚠️ 别把两件事读成一件：**打分基线**已于 2026-10-26 随旧集停用删除，
#   新题集的基线尚未录制；**轨迹基线**从未录制——两者都不是现成的入口。
./.venv/Scripts/python.exe evals/trajectory_regression.py -b <已录制轨迹目录> -c out/traces

# 消融实验（A 裸 LLM → G 全能力，七变体对比出 Benchmark 表）
./.venv/Scripts/python.exe evals/ablations/run_ablation.py --list
./.venv/Scripts/python.exe evals/ablations/run_ablation.py --aggregate-only
```

| 能力 | 状态 | 说明 |
|------|------|------|
| 确定性打分 | 保留 | 路由准确率 / 引用命中率 / 拒答正确率 / 引用正确性 / must_contain / pass^k（空真口径显式可见，`*_applicable` 才是真实分母） |
| 质量门禁 | 保留，未武装 | `quality_gate.py`——四态 **PASS / FAIL / NOT-ARMED / NOT-ENFORCED**，缺基线或缺键默认 **NOT-ARMED 硬失败**（退出码 2，不改动基线），首跑建档要显式 `--allow-bootstrap`；C-Eval 题集无引用要求样本、不产出 `quality.json`，门禁仍处于未武装状态，CI 暂不跑这道门 |
| 消融基准表 | 保留 | `evals/ablations/`——A-G 七变体（LLM only → Full Agentix）对比，聚合出 BENCHMARK.md |
| 失败回流 | 保留 | `feedback_ingest.py`——生产失败样本 → 候选评测行（人工评审后合入） |
| 评估二期（Phase 31） | 保留 | 声称一致性 `claim_check` · 提示敏感性 `--sensitivity` 四变体极差 · PII 离线 `privacy_flagged`（applicable 分母、原文零留存）· Best@k——全部为报告键，只增不进门禁 |
| 评测集 | **已换代并实测** | 旧 KG/RAG 法律金标（102 条）与录制基线已于 2026-10-26 删除；新题集为 C-Eval 公开 MCQ（156 题），2026-11-05 本机扁平拓扑全量实测 MCQ 正确率 **0.7097**（110/155 可判分）。历史五指标（`route_accuracy=0.6275` 等）是历史测量记录，不是当前门禁基准 |

> 门禁被悄悄放宽这件事怎么防：口径在 `evals/quality_gate.py` 一处定义，CI 里不写死
> 默认值；「缺基线」是硬失败而不是静默放行；`evals/` 的守卫测试（`test_p21` harness
> 契约、`test_p104` promptfoo 契约、`test_ci_gate_contract` 门禁语义）不依赖评测集，
> 评测集换代不会把它们一起废掉。

详见 [evals/README.md](evals/README.md) 与 [消融实验](evals/ablations/variants.json)。

**如何复现评测（promptfoo 回归主入口，P1-5）**：自建体系保留轨迹回归 / Pass^k / 质量门禁；
promptfoo 承担声明式回归用例与断言入口（打分函数直接 import `run_eval`，口径单源）。
前置：`.venv` 依赖 + 可用 LLM 端点 + **一份评测集**（新题集 `evals/ceval_golden.jsonl` 已就位，为 C-Eval MCQ；旧 KG/RAG 金标已停用删除，转换器对不存在的 `--golden` 会明确报错）。

```powershell
# 1. 从评测集机械转换试点用例（--golden 指向一份存在的题集）
.\.venv\Scripts\python.exe evals/golden_to_promptfoo.py --golden evals/ceval_golden.jsonl --limit 25
# 2. 本地在线评测（结果落本地 promptfoooutput/，纯 CLI 不上云）
npx promptfoo@latest eval --ci --no-cache
# 3.（可选红队）promptfoo redteam 生成注入/越狱 probes，作 LLM01 活体证据
```

## 9. 可观测

- **全链路 trace_id**：中间件最外层注入，请求日志 / 鉴权 / Agent / LLM 埋点共享同一 trace_id
- **步骤级轨迹**：`trajectory.py` 落盘可回放 / 前缀回归 / 首错定位
- **执行轨迹时间线**：前端消息内「执行轨迹」折叠面板，实时呈现工具调用 → 生成 → 完成事件序列与间隔
- **Explanation API**：`GET /api/v1/retrieval/answers/{id}/explanation` 聚合证据 / 引用 / 模型，未持久化块显式 `not_recorded`
- **业务指标**：grounding 低分率、网关兜底级联等 Prometheus 指标 + 告警规则（compose 内置 prometheus / alertmanager / grafana）
- **事件查询**：`GET /api/v1/ops/events?event_type=user_notification&limit=50` 列出 EventPort 最近事件（鸭子面 `list_recent`，memory / jsonl / redis_stream 三后端一致）
- **SLO 目录**：可用性 / 首 token P95 / 拒答正确率等 5 项 SLI + 错误预算 burn-rate 分级
- **LangSmith tracing 看板（可选，默认关）**：langchain 1.x 栈下经环境变量零代码接入——`LANGCHAIN_TRACING_V2=true` + `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT`（见 `.env.example`）后，LangGraph 编排与 LangChain runnable 调用链自动上报，先 10 条验证再全量。接入边界：自建 httpx 直调与 OTel/Prometheus 体系**不上报**；自建指标与轨迹落盘全保留（轨迹回归真源在本地），LangSmith 只接管看板与在线实验层。免费档 1 席 / 5000 traces / 月、轨迹保留 14 天，只接开发/评测流量，上报失败 fail-open（丢遥测不丢请求）。注册：[smith.langchain.com](https://smith.langchain.com) 拿 API Key 填入 `.env` 即可。

详见 SLO 目录 与 发布回滚 Runbook。

---

## 10. 快速开始

> 只想先确认「它能不能跑」：用 [最小 Demo](#最小-demo3-条命令跑通一次带引用的-rag-问答) 的 3 条命令，不需要 Docker、不需要云端 key；本节是完整平台起法。

```bash
# 1. 获取源码
#    本仓库当前未发布到公共远端，不提供公开 clone 入口；
#    发布后把下面的 URL 换成实际地址（含 CI 徽章一并回填）。
git clone <远端仓库 URL> My-Agentix
cd My-Agentix

# 2. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate

# 3. 安装依赖（与 CONTRIBUTING.md / §11 同一条配方）
pip install -r requirements-dev.txt   # 含 -r requirements.txt + pytest/ruff/black/mypy/ragas
pip install -e ".[dev,test]"          # 可编辑安装并声明 dev/test extras（其包已在上一行装齐）

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
#   ⚠️ 模型名有两个旋钮、两个默认值，别改错：精排真正用的是 `RERANKER_MODEL`
#   （`agentix/retrieval/settings.py` 的 `_RetrievalSettingsCompat.RERANKER_MODEL`，默认即上面这个串；
#   `rerank.py` 与 `chat_agent_tools.py` 都读它）。平台配置里的 `RERANK_MODEL`
#   （`agentix/config/settings.py`，默认 `bge-reranker-large`，`.env.example` 现为 `qwen3-rerank`）
#   在检索侧没有消费者，改它不影响精排模型名——它只被 `kb_ingest` 独立按同名环境变量读取。
# 普通检索需 RERANKER_ENABLED=true；混合检索另需 RETRIEVAL_HYBRID_ENABLED=true、
# RETRIEVAL_SPARSE_ENABLED=true。混合链路的精排开关 `RETRIEVAL_HYBRID_RERANK`
# **代码默认开**（仅总闸打开后生效）——这是 §11 F1a「12 新旋钮默认关」里唯一的例外。

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

> **第 6 步只覆盖了两条地址，其余按设计降级——知道边界再怪功能不工作。**
> `.env.example`（以及照它复制的 `.env`）里的 `redis` / `postgres` / `minio` 是**容器网主机名**，
> 本机进程解析不到。逐条后果：
> ① `CHECKPOINTER_TYPE=redis` 连接失败 ⇒ **自动降级 `MemorySaver`**，会话状态不跨进程
> 持久化（`agentix/infrastructure/persistence/checkpointer.py`，设计上不阻塞启动，只记原因）。
> 但**主机名解析不到只是其中一种触发方式**：2026-10-23 接真 Redis Stack 实测发现，`REDIS_CHECKPOINT_URL`
> 只要指向 db0 以外的库也必降级——RediSearch 只能在 db0 建索引，db2 时 `asetup()` 抛
> `RedisSearchError: Failed to create index 'checkpoints' on Redis: Cannot create index on db != 0`，
> 应用照常启动、只留一条 ERROR 日志，持久化其实从未生效。R3.2 曾把 checkpoint 分片放在 db2，
> 正是这种情况；现已改到 db0，与 cache 同库、靠 key 前缀隔离（`checkpoint:` / `semantic:` / `history:`）。
> 守卫见 `tests/test_r32_redis_sharding.py`
> 的 db0 不变式 + 那条 `-m integration` 的真连接往返测试（形状守卫绿过一次，真 Redis 上一次都没激活）。
> 顺带两条实测细节：不带 RediSearch 的 `redis:7-alpine` 会报 `unknown command 'FT.INFO'` 后降级（不是启动失败）；
> 降级单例是粘的，同一进程内 Redis 恢复了也不会重试。
> ② `CACHE_REDIS_URL`（留空时**回落 `REDIS_CACHE_URL`**，不是关掉缓存）连不上 ⇒ 检索缓存退化为不缓存，
> 主链路仍出答案；探活失败只花 `CACHE_REDIS_CONNECT_TIMEOUT_SECONDS`（默认 2s，实测 2.010s 返回 None）。
> ③ `CELERY_BROKER_URL` **非空即改走 Celery 投递**（`agentix/retrieval/usecases/index_service.py`
> 的 `enqueue_index_job`）⇒ 本机没有 worker 时 `apply_async` 照样返回成功（实测 0.812s），
> 任务静静躺在 Redis 的 `celery` 列表里没人消费——索引不会建，但接口不报错。
> 要吃这三条能力，把 `REDIS_CHECKPOINT_URL`、`REDIS_CACHE_URL`（或 `CACHE_REDIS_URL`）、
> `CELERY_BROKER_URL` 改指宿主地址 `redis://127.0.0.1:6379/…`（Redis 宿主端口即 6379，
> 但 **checkpoint 与 cache 那两条的库号必须是 `/0`**），
> 或直接走下面的全容器路径。向量检索侧不用改：`VECTOR_STORE_BACKEND` 的代码默认是
> `faiss`（`agentix/retrieval/settings.py` 的 `_RetrievalSettingsCompat`），
> 本机走本地索引文件，不依赖 Milvus 主机名。
>
> 顺带说明 compose 里那个本机 quick start 不会启动的 service：**`retrieval-worker`**
> （第 3 个 service，`celery -A agentix.retrieval.infra.tasks.celery_app.celery_app worker`，
> 与 backend 共享 `./data`、`./uploads` 挂载，副本数与配额由 `RETRIEVAL_WORKER_REPLICAS`
> / `_CPUS` / `_MEMORY` 控制）。它是 ③ 那条投递的消费者——只在容器路径里有意义。

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
docker compose --profile full up -d       # 基础设施 + backend + web（compose 共 15 个 service，web 是第 15 个）
# 浏览器访问 http://localhost:8080 —— nginx 同源反代 /api → backend:8000（零跨域，SSE 已配）
```

常见问题（端口占用 / 连接超时 / LLM 空返回等）见 快速启动.md，技术细节见 技术栈详解.md。

---
## 11. 工程实践

**测试真源**（唯一声明处，其他文档只应指向本节）：

> **当前测试基线（2026-11-02 实测）**：`.venv/Scripts/python.exe -m pytest -m "not integration" -q` → **6052 passed / 40 skipped / 6 deselected / 0 failed**（514.95s，Python 3.12.13，单机离线，未开并行）；`ruff check agentix tests scripts evals kb_ingest` 全绿；`mypy agentix/domain` 40 文件 0 错；`python scripts/gen_staging_matrix.py --check` 退出 0；`pip check` 无 broken requirement。
>
> 更早口径（2026-10-27：5239 passed / 40 skipped / 4 deselected / 1 failed）与 5084 → 5061 → … → 2329 已移入历史链；README 只保留当前值，避免同一事实写两处。
>
> 历史口径链（5084 → 5061 → … → 2329）、逐项加法、变异验证与独立审查留痕已移出本节；README 只保留当前值，避免同一事实写两处。

> **教训（写进口径链，不只写进这一次的说明）**：⑫ 落地时只跑了定向守卫（p113 及其邻域），而它改的是 `_build_query` 的调用契约，破坏面在**没人想到要跑**的旧文件里。定向回归证明"新行为对"，全量套件才证明"旧行为没被撞坏"——两者不可互替。

**前端套件当前验证值**：2026-10-23 运行 `npm run test -- --reporter=dot`，**26 个测试文件、223 个测试全部通过**（5.77s）。

40 条 skip 均为显式声明：22 条 p125 的 POSIX 专属用例（Windows 上按设计跳过，由 Linux 容器真跑取证，见 subprocess 沙箱取证）+ 4 条 p131 的 POSIX 专属用例（code exec 在墙内真跑一段代码 / 非零退出如实上报 / 超时被杀且秒级返回 / 墙被杀时孙进程随之被杀（`PR_SET_PDEATHSIG`）——最后那条在 Windows 上是**按设计跳过**：口径 §5 写明这条承诺只在 Linux 成立（macOS 没有 `prctl`），它测的不是"这台机器测不了"，而是"这个平台兑现不了那条承诺"；4 条的跳过理由都取自与执行点同源的能力探测，第 4 条取自 `parent_death_signal_supported()`）+ 4 条集成/端到端用例（2 条需 `LEGELBOT_RUN_INTEGRATION_TESTS=1`，2 条 LightRAG 端到端需 `LEGELBOT_RUN_LIGHTRAG_E2E=1`）+ 10 条 Redis Stream 契约用例（本机无 Redis 服务时按设计诚实跳过，`RedisStreamEventBus` 懒连接、连接错误向上传播不伪造）。真图 HITL 进环 E2E（`test_p81` `TestRealGraphE2E`）在本地 Python 3.12.13 执行；CI Python 3.10 按设计跳过，**不存在无原因静默跳过**。

p131 共 39 条（本次真实采集，不是手抄：这条由 `tests/test_p131_code_exec.py::test_docs_declaring_this_files_case_count_match_the_live_collection` 现算再核，改用例就要同改 README / CHANGELOG / runbook / 项目实现手册 里这句短语，漏一处当场红）。这 4 条 skip 已由 **Linux 容器真跑取证**：root 与非 root 两轮各 **39 passed / 0 skipped**（`-rs` 一条 skip 都没打印 ⇒ 没有出现"墙自己的故障"类 skip），步骤与原始输出见 subprocess 沙箱取证 第 3.5 步。

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
| 运行时护栏 | 上下文隔离、控制平面、token 预算、目标裁定流程、工具限量、人工审批、任务分解 |
| Agent Core 2.0 | 主循环 / 规划纠偏 / HITL / 事件 / 多智能体失败模式 / 演化 / 分层验证 |
| 可观测性 | 轨迹回放 / 首错定位 / Pass^k / 轨迹前缀回归 |
| Agent Runtime / MCP | Run API 语义（超时 / 取消 / 预算）、MCP 协议与桥接 |

**CI/CD**：五条工作流（`ci.yml` / `codeql.yml` / `perf-baseline.yml` / `release.yml` / `secrets-scan.yml`）+ Dependabot 安全更新（pip / npm / github-actions / docker 四生态；版本升级手动执行）。

`ci.yml` 的汇总门 `ci-gate`（`if: always()`）要求七个 job 全绿：`version-consistency`（`scripts/bump_version.py --check`：pyproject / `agentix.__version__` / `web/package.json` / `.env.example` 四处一致）· `lint`（`ruff check agentix scripts evals kb_ingest tests` + `black --check` 非阻断 + `mypy agentix/domain`）· `test`（`pytest -m "not integration" -q`，矩阵只有 **Python 3.10**）· `architecture-guards`（`test_p17_domain_layer.py` + `test_p22_obs_scope.py`）· `eval-smoke`（`test_p21_eval_harness.py` harness 契约，只跑单测、不需要评测集）· `retrieval-gate`（检索与租户契约 8 个测试文件 + 三条 import 装配验）· `frontend`（`npm ci` + `npm run lint` + `npm run build`，**不含 vitest**）。

> **性能门禁不在这道门里。** p95 延迟由独立的 `perf-baseline.yml` 承担——每日 UTC 18:00 与手动触发，起后端压 `/liveness`（200 请求 / 10 worker / 20 warmup），与上一次基线比对、劣化 >20% 阻断**该次运行**，基线靠 `actions/cache` 跨运行持久化。**PR 不会因为性能劣化变红**，这条线是每日巡检而非合入阻塞。前端 vitest 同理只在本地实测（见上方验证值），CI 不跑。

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
│   └── policies/               错误分类 / 重试策略 / 冲突裁决判定函数
├── orchestration/              编排件（M2）：orchestrator 主循环 · executor 执行器
│   ├── langgraph/              lg_builder 主图 / lg_states / lg_supervisor 拓扑 / policies 策略集中
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
docs/                           架构 / 指南 / Runbook / ADR（`architecture/adr/`）/ 实现细节手册
                                （`项目实现手册.md` + 图文版 docx）/ 归档（`archive/`）
scripts/                        数据构建 / 迁移 / 运维脚本
data/                           领域数据（图谱种子 / 语料，由用户填充）
docker/                         观测栈（prometheus / alertmanager / grafana）与 compose 覆盖（Dockerfile 在仓库根）
```

### 关键代码索引

| 想找 | 文件 | 符号/说明 |
|---|---|---|
| 应用入口 | `agentix/main.py` | `application`、`lifespan`、`_startup` |
| 主配置 | `agentix/config/settings.py` | `Settings`、`settings` |
| 对话业务 | `agentix/application/services/chat_service.py` | `ChatService.ask`、`stream_events` |
| 图构建 | `agentix/orchestration/langgraph/lg_builder.py` | `build_graph`、`get_active_graph` |
| 图状态 | `agentix/orchestration/langgraph/lg_states.py` | `AgentState` |
| 路由 | `agentix/application/agents/router.py` | `route_query` |
| KB 子图 | `agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/kb_workflow.py` | `create_kb_multi_tool_workflow` |
| KG 子图 | `agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/multi_tool.py` | `create_multi_tool_workflow` |
| Checkpointer | `agentix/infrastructure/persistence/checkpointer.py` | `init_checkpointer` |
| 工具执行 | `agentix/tools/gateway/executor.py` | `ToolExecutor` |
| 工具注册 | `agentix/tools/registry_init.py` | 内置工具注册 |
| 检索聊天路由 | `agentix/retrieval/api/routers/chat.py` | `/chat`、`/chat/stream` |
| 检索 ReAct | `agentix/retrieval/usecases/chat_agent_runner.py` | `run_agent_stream` |
| 入库路由 | `kb_ingest/kb_service/api/routes.py` | `/ingest/*`、`/search*` |
| 前置 SSE | `web/src/api/sse.ts` | 统一 SSE 解析 |
| 测试数据前端 | `web/src/modules/chat/App.vue` | 对话工作台 |
| 检索前端 | `web/src/modules/knowledge/` | 知识库管理 |

---

## 13. 技术栈

| 类别 | 技术 |
|------|------|
| 后端 | Python 3.10 / 3.12 · FastAPI · Uvicorn（SSE 流式） |
| Agent 编排 | LangGraph 1.2（Subgraph + Map-Reduce）· Agent Core 2.0（Harness + Capabilities） |
| 前端 | Vue 3 · TypeScript · Vite · Vitest |
| 知识存储 | Neo4j（知识图谱）· Milvus（向量检索）· PostgreSQL（结构化 / 编排元数据）· MySQL（统计）· Redis（缓存 / Checkpointer） |
| 检索增强 | LightRAG（社区摘要）· Reranker（Cohere / Jina / Voyage / BGE）· 多路检索兜底 |
| 工具扩展 | MCP Client + Server（stdio JSON-RPC）· ToolRegistry 声明式注册 |
| 可观测 | Prometheus · Grafana · Alertmanager · 步骤级轨迹回放 · SLO |
| 质量与评测 | pytest · ruff（门禁含 `tests`）· mypy · 确定性打分契约 · 声称一致性 · 质量门禁语义 · 轨迹前缀回归（评测集换代中、基线未录，代码保留） |
| 安全 | APIKey / JWT / RBAC · 输出过滤 · 提示注入防护 · 限流 · 熔断（仅检索域两处接线，口径见 §7；通用 CircuitBreaker 原语未接线进其他生产链路） |
| 交付 | Docker / docker-compose · GitHub Actions（CI + CodeQL）· Dependabot |

---

## 14. 文档

文档分三层，按「你此刻要回答什么问题」选，不要按目录名猜：

| 层级 | 回答什么 | 入口 |
|---|---|---|
| 现行总览 | 项目**现在是什么样** | 本文档（唯一） |
| 实现细节手册 | 某处**具体怎么实现** | [项目实现手册.md](docs/项目实现手册.md) |
| 操作手册 / 设计论证 | 出事**做什么动作** / 为什么是这么设计的 | `docs/runbooks/`、`docs/design/` |

下表是完整文档目录：

| 文档 | 内容 |
|---|---|
| [实现细节手册](docs/项目实现手册.md) | **实现级细节**：共 25 章（§0–§24），按「一次请求怎么走 → 分层怎么落地 → 各子系统怎么实现」展开；按**符号**定位、不写行号。Word 图文版 `docs/My-Agentix项目说明书.docx`（含整体架构图）为 2026-10-22 快照，**以 md 为准** |
| [架构总览](docs/architecture/README.md) | 一句话架构、五条设计原则、运行时架构图 |
| 架构详解 | 设计特色、完整架构图、主图逐节点说明 |
| [分层与依赖规则](docs/architecture/分层架构与依赖规则.md) | 各层职责、**4 条强制依赖规则**及其守卫 |
| Agent Core 五层架构 | Harness / Capabilities / Application / Domain / Interfaces 运行时职责与端口映射 |
| [接口契约](docs/architecture/接口与契约.md) | 端口 Protocol、Payload TypedDict、错误码四层分类 |
| [扩展指南](docs/architecture/扩展机制.md) | 新增工具 / Provider / Agent 子图 / 领域适配 |
| [运行时护栏](docs/architecture/运行时护栏.md) | 上下文隔离 · 控制平面 · 感知工具限量 · 人工审批 |
| 请求执行图 | 请求链路与节点依赖 |
| [配置管理](docs/architecture/配置管理.md) | 单一事实来源、特性开关回滚表 |
| [架构决策记录](docs/architecture/adr/) | 分层与端口 / 注册表可插拔 / 声明式配置 |
| 技术栈指南 | 全量技术选型、版本与集成细节 |
| Quick Start 常见问题 | 端口占用 / 连接超时 / LLM 空返回排查 |
| Agent 路由速查表 | 路由意图、出口节点、关键词兜底 |
| LightRAG 服务指南 | LightRAG 服务部署、检索模式与调试 |
| Research Agent Demo | Phase 13 离线主循环走查、组件映射与验收对照 |
| 测试分层策略 | 五层测试体系、marker 约定、改动→测试对照表（测试总数见 §11） |
| 数据准备 | 三类数据文件、覆盖范围、真实数据填充指引 |
| 发布与回滚 Runbook | 部署步骤、五层健康验收链、四级回滚 |
| SLO 目录 | 服务等级目标、错误预算、告警分级 |
| 失败恢复 Runbook | 四层失败恢复机制与现场演示命令 |
| subprocess 沙箱取证 | ⑮/⑯ 在 Linux 容器里的资源闸、环境白名单、降权与延迟数字怎么复现 |
| 变更记录 | 按版本记录功能、修复、迁移与验证口径；历史迁移文档已移除，当前结构以 [§3 架构](#3-架构) 为准 |
| [已知限制清单](docs/已知限制.md) | §16 的逐条展开：沙箱 / 门禁 / 覆盖缺口 / 路由兜底 / `kb_ingest` 边界 |
| 版本管理 | 版本号语义、发布分支与兼容策略 |
| [评测说明](evals/README.md) | 确定性打分、质量门禁、消融实验（评测集换代中；新题集为公开 MCQ，见其 §10） |
| OWASP LLM Top 10 自评 | LLM01–LLM10 逐条：风险 → 现有缓解（含代码位置）→ 残余风险 |

---

## 15. 性能与成本

> ⚠️ **全栈服务口径（FastAPI + 基础设施容器 + Prometheus TTFT 直方图）仍为占位（`<待实测>`）**——
> 采集方法见 docs/runbooks/性能与成本采数.md。
> **当前检索质量口径是 §15.3**（STARD 公开集，1,543 题、精确 docid 金标）。
> §15.1 与 §15.2 是 2026-10 的历史小样本留档：题集已停用、命中判定为 silver 文本匹配，
> 其中的 Recall@5 / MRR 与 §15.3 不是同一个量，不作现状读数，也不参与任何门禁。
> 下面的 2026-10-23 段落是服务端指标口径（桶分辨率受限）。全章数字都不构成 SLO 结论。

| 指标 | 数值 | 口径 |
|---|---|---|
| 首 token P50 | `<待实测>`（小样本观测见下） | `agent_ttft_seconds` 直方图 P50，10m 窗口 |
| 首 token P95 | `<待实测>`（小样本观测见下） | 同上 P95；SLO 目标 ≤5s（见 SLO 目录） |
| 流总时长 P95 | `<待实测>`（小样本观测见下） | 终帧 `duration_ms` 统计 |
| 单请求平均 token（输入） | `<待实测>`（小样本观测见下） | `llm_tokens_total{type="prompt"}` 增量 / 请求数 |
| 单请求平均 token（输出） | `<待实测>`（小样本观测见下） | `llm_tokens_total{type="completion"}` 增量 / 请求数 |
| 单会话平均成本 | `<待实测>` | `UsageRecord` 按 branch 聚合 cost |

**2026-10-23 小样本实测（宿主后端 + DeepSeek API，非 LM Studio 固定快照；不构成 SLO 基线）**：
`POST /api/v1/chat/stream` 同一条单轮通用问题（不查知识库）跑 18 次（两个后端进程合并计数：
修复前的 8010 进程 15 次、修复后的 8011 进程 3 次；其中 1 次走的是下面②那条检索为空兜底路径），
服务端 `agent_ttft_seconds` 合并 `count=18 / sum=36.84s` ⇒ **均值 2.047s**；直方图桶边界是
`… 1.0 / 2.0 / 5.0 …`，所以 P50 只能落在 `1,2)`、P95 只能落在 `[2,5)`（桶内插值得
1.57s / 3.8s，精度受桶分辨率限制，n 再大也不会更准）。客户端逐帧观测：首内容帧
1.78–2.51s，与服务端值互差 <0.3s；每条响应 **54–95 帧**（`metadata` → 52–93 × `message`
→ `done`），内容帧铺开 0.23–0.79s——**流式是真的在流**，不是整段一次性下发。
`llm_tokens_total` 增量：prompt 5277/3=1759、completion 609/3=203、cached 4224/3=1408
（标签名是 `type`，另有第三种取值 `cached`；此前文档写的 `direction=` 不存在）。
按 DeepSeek 现行价目折算，单条约 ¥0.0008，102 条 golden 一轮约 ¥0.08。
两个必须一并记住的实测结论：① 终帧的 `duration_ms`/`ttft_ms` **今天是才开始真正上 wire 的**——
`RunEventEmitter` 早就注入了这两个字段，但 `_to_sse` 走 DTO 字段白名单，把它们整个丢掉，
前端 `chatStreamHandlers.ts` 的耗时芯片因此永远拿不到数据（其单测喂手写假帧，掩盖了断链）；
已修复并补 `tests/test_p65_unified_sse.py` 两条守卫（DTO 单测 + stub 图整链对拍）。
② 知识分支在检索为空时返回 `finalize_node.py` 的固定兜底文案，实测该路径 **不消耗 LLM token**
且不会自动回退到通用回答——本机 embedding 服务（LM Studio `127.0.0.1:1234`）未启动时，
`kb-query` 类问题全部走这条兜底，所以 golden 基线（#6）与检索指标（#7）在此环境下测了也不算数。
③ 上面 18 次是**两个 regime 合并**的：8010 的 15 次是真实生成，8011 的 3 次落在 ②那条兜底路径上
（其中一次实测 `ttft_ms=1210`、`duration_ms=1211`、终帧 0 条 LLM token）。所以 **2.047s 这个均值
不能当生成延迟基线读**——它只是"这一天宿主上观测到的均值"，兜底样本把它的方向拉低了；桶边界
（`1s/2s/5s`）与 regime 混采是两个独立的精度损失，谁也不能拿它去判 SLO 达标。
④ 同一天 DeepSeek 侧出现 **402 Insufficient Balance**（宿主后端日志 18 次命中），router 与多工具
分支全部退到默认 `kb-query`，实测该 regime 下**单请求 0 LLM token**、正文是 29 字固定文案——
这既解释了为什么修复后的样本"看起来更快"，也是 #6/#7 决定暂不录基线的直接原因（详见 §11 第三轮）。

**测量方法**：全栈环境（LM Studio 加载固定模型快照 + 基础设施容器）下，
对 102 条 golden（历史评测集，2026-10-26 停用）跑一轮 `evals/run_eval.py --record`，期间从 Prometheus
（`http://localhost:9090`）读取 TTFT 直方图分位数与 token 计数器增量，
token 明细从 `UsageRecord` 聚合。数字与模型快照/温度绑定，换模型须重测。
已知限制：本机显存与云服务器测量结果不可互比，回填时须标注测量环境。

### 15.1 本地测试数据语料检索对照（2026-10-11，历史读数）

在隔离评测库中对 `data/kb/legal_corpus.txt` 的 4,865 条数据条目运行真实检索；
语料 SHA-256 为
`1ca58564b1f3a58c212c0fc23e9e5da317576ce524fc58cd4f6d0ec6f3aea2e5`。
使用 `evals/legal_golden.jsonl`（历史评测集，2026-10-26 停用）中 23 条适用的 `kb-query` 金标问题，
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
18/23 题，**没有增加命中题数**；目前观察到的是相关数据条目排序前移，
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

### 15.2 最小切片端到端小样本（本地 LM Studio + SQLite，2026-10-25，历史读数）

**先看结论**（细节与 caveat 在下面，按顺序读）：

| 指标 | 数值 | 一句话口径 |
|---|---|---|
| 检索 Recall@5 | **0.8696**（20/23） | silver 匹配；严格口径 0.6957；这份语料的严格天花板 0.7826 |
| 检索 MRR | **0.7029** | 与 `evals/hybrid_eval.py` 同口径 |
| 检索 P50 / P95 | **100.3 / 284.5 ms** | 只含检索段；对 Python 栈敏感（口径③） |
| 端到端 P50 | **20546.8 ms** | 本机 8B 上路由占 64%；这是最小切片的形状，不是生产基线（口径④） |

本节是**第一条端到端（路由 + 检索 + 生成）实测数字**，跑在「顶部最小 Demo」那条链路上。
**环境指纹是数字的一部分**：仓库 `.venv` = `python 3.12.13 / openai 1.109.1 /
langchain-openai 0.2.14 / langchain-core 0.3.86`（**这是 2026-10-25 采数当日的读数**——
LG-0-2 起 `.venv` 已迁到 `openai 2.54.0 / langchain-openai 1.7.3 / langchain-core 1.7.0`，
重跑本节要按新指纹重新登记，不许沿用旧数字；另与
[docs/runbooks/性能与成本采数.md「宿主直跑」前置 ①
是同一条纪律）；路由的结构化输出走 `method="json_schema"`——那是脚本按能力探测为这套栈选的
本地形态（见顶部口径 ②），不改生产默认。其余条件：RTX 5060 Laptop 8GB 上的 LM Studio
（`qwen/qwen3-8b` + `text-embedding-bge-m3`）、SQLite、FAISS 索引（`doc_count=4,865`，语料
SHA-256 与 §15.1 同为 `1ca58564b1f3a58c…`）、Dense-only（`hybrid=false`）、温度 0.0、
`/no_think` 关思考链、单进程串行一轮、无并发。
明细：`data/demo/eval_e2e_venv_py312_openai1109.json`
（`data/demo/` 已进 `.gitignore`，需要时自行重跑生成）。

| 指标 | 数值 | 口径 |
|---|---|---|
| 样本量 | 23 | `evals/legal_golden.jsonl` 中 `route_type=kb-query` 且 `expected_sources` 非空（102 条里取这 23 条，`--limit 23`） |
| Recall@5 | **0.8696**（20/23） | top-1 命中 14 题、2–5 位命中 6 题、未命中 3 题（L049/L055/L056）；匹配器见下方口径① |
| MRR | **0.7029** | 首命中位次取倒数、位次不截断到 k（与 `evals/hybrid_eval.py` 同口径；本次两者相等，因为没有 6 位以后的命中） |
| 检索 P50 / P95 | 100.3 / 284.5 ms | 只含 `_retrieve_docs` 一段（mean 125.8，min 51.7 / max 511.5）；**这段对 Python 栈敏感**，见口径③ |
| 生成 P50 / P95 | 7108.2 / 13739.9 ms | 只含生成那次 LLM 调用（mean 7596.5，min 1831.8 / max 14749.4） |
| 路由 P50 / P95（残差） | 13462.3 / 17682.1 ms | 端到端减检索减生成逐题算得（mean 13836.1，min 10608.8，max 19243.9）——**端到端里最大的一段**，见口径④ |
| 端到端 P50 / P95 | **20546.8 / 31543.1 ms** | 路由 + 检索 + 生成三段串行（mean 21558.3，min 12869.6，max 32546.3） |
| 单请求 token 均值 | prompt 718.7 / completion 283.3 / 合计 1002.0 | **只计生成调用**（prompt 逐题 526–941、completion 69–580）；路由那次 LLM 分类的 token 未计入，真实每请求成本高于此数 |
| 路由一致率 | 23/23 | 金标 `kb-query` ⇔ 路由节点判 `kb-query`，且 23 题都带回模型自己的 `decision` 正文（答案正文均值 465.5 字，104–885） |

**口径必须逐条读，否则这组数会被读坏**：

1. **命中判定是 silver**（复用 `evals/hybrid_eval.py::doc_matches_gold`）：把 chunk 正文 +
   `metadata` 全部值当干草堆，出现「法规名 + 第X条（中文/阿拉伯数字写法）」即算命中，
   金标无条号时只比法规名。换成「**金标文档本身**进 top-5」的严格口径，本次是
   **16/23 = 0.6957**——20 个 silver 命中里有 4 题是靠"别的条文引用了它"得分：
   L053/L054 的金标法规（《食品安全法》148、《消费者权益保护法》55）**根本不在这份语料**，
   命中的是正文里引用了该条文的司法解释 chunk；而 L060（《民法典》311）与 L062（《民法典》188）
   金标条目**在库里**，只是排在 top-5 之外，这两题是真·排序失败。三条 silver 未命中
   （L049/L055/L056）全部落在覆盖缺口上，**不是排序失败**。覆盖缺口共 5 条：《劳动法》44、
   《食品安全法》148、《消费者权益保护法》55、《工伤保险条例》14、《刑法》253条**之一**
   （库里只有《刑法》第253条，没有"之一"那款）。⇒ 这份语料对任何严格口径的天花板都是
   **18/23 = 0.7826**；报 Recall 必须同时说明是 silver 还是「金标文档本身」。
2. **分位数是最近邻秩**：`sorted[round(q×(n−1))]`，n=23 时 P95 就是第 22 小 ⇒ 最慢那一条
   （本次端到端 max 32.5s / 路由残差 max 19.2s）被 P95 排除在外。这与 §15 主表的服务端
   Prometheus 直方图（桶边界 1s/2s/5s）是两套口径，**不可互读**；本节是逐请求客户端实测毫秒，
   但 n=23、单轮、单机，不构成任何 SLO 结论，也不外推到其他语料/模型快照。
3. **延迟绝对值对 Python 栈敏感，质量结论不敏感**——这条是本次踩出来的，值得单独讲。
   同一份代码、同一份语料 SHA，另一套栈（Anaconda `python 3.13.9 / openai 1.47.0 /
   langchain-openai 1.1.6`，脚本为它自动改用 `function_calling + tool_choice="auto"`）把 23 题
   重跑一遍（明细留档到 `data/demo/eval_e2e_anaconda_py313_openai147.json`，
   该目录已进 `.gitignore`）：
   **逐题位次与检索集合 0 处差异**（Recall@5、MRR 逐位相同），但**检索 P50 从 100.3ms 变成
   2310.8ms**（mean 2304.1）。归因不在 FAISS：每次搜索都会重建 embedding 客户端——
   `RetrievalPortAdapter.search()`（`agentix/retrieval/usecases/retrieval_port_adapter.py:73`）
   → `build_retriever`（`agentix/retrieval/usecases/chat_service.py:206`）→
   `create_vector_store` → `FAISSVectorStore.load()`
   （`agentix/retrieval/infra/vectorstore/faiss_impl.py:213`）→ `create_embeddings()`
   （`faiss_impl.py:116-118`）。实测构造耗时：`.venv` 首次 259ms、后续 16–24ms；Anaconda 栈
   2053–4025ms，cProfile 指向 `_ssl._SSLContext.load_verify_locations` 4 次共 3926ms
   （≈每个 httpx client 装一次系统 CA，而 LM Studio 是明文 HTTP，这笔开销用不上）。
   量级对照：FAISS 本地 `similarity_search(k=5)` 1–16ms、query embedding 的 HTTP 往返 43–111ms。
   ⇒ **「每请求重建」是生产链路的形状问题，但它的代价是栈相关的**：在仓库登记的
   环境里检索 P95 也只有 284.5ms，把"检索要 2 秒"写进结论就是把 SDK 的问题当产品的性能。
   这一处**已在 2026-10-25 补上开关**：`RETRIEVAL_RETRIEVER_CACHE_ENABLED`（默认 **关**，
   flag-off 与接线前逐位等价）开启后进程内复用 `(retriever, vectorstore)`，
   签名含 `index.faiss` 的 mtime+size ⇒ 索引换代自动失效
   （实现 `agentix/retrieval/infra/vectorstore/retriever_cache.py`，
   守卫 `tests/test_p117_retriever_cache.py`）。
   开关实测（同日、本机 `.venv`、`eval --retrieve-only --skip-route --no-prewarm --limit 23`，
   冷进程、单轮、只测检索层）：**逐题检索集合与位次 0 处差异**，Recall@5 与 MRR 逐位相同
   （0.8696 / 0.7029）；检索 **P50 93.9→4.4ms（21.3×）、P95 341.5→5.5ms**，
   均值 229.4→38.5ms（开关侧均值被首题冷启 789.9ms 拉高，其后中位 4.3ms）。
   ⇒ 省掉的正是"每次检索重建客户端 + 反序列化索引"这段；**检索结果一位没变**。
   这条 A/B 是本机单轮小样本，不外推到其他语料/栈；它也不是端到端结论
   （端到端仍以路由为最大段，见口径④）。
   `seed` 是这轮的**栈不敏感对照**：批大小 200 ⇒ 4,865 条 = 25 次嵌入请求，冷缓存整轮实测
   `.venv` 89.5s / Anaconda 85.1s（≈18ms/条，瓶颈在嵌入服务吞吐——这条链只建一次客户端，
   不经过上面那个"每请求重建"的路径），重跑命中本地 embedding 缓存
   （`data/embedding_cache.db`，键 `sha256(model+text)`，实现见
   `agentix/retrieval/infra/embedding/cache.py`）后 4–6s。
   尾部同样不稳：Anaconda 轮曾出现单条端到端 86.6s（路由残差 73.2s），`.venv` 轮最大 32.5s
   ——**n=23 的 P95 连自己这一轮都稳不住**，更要连环境指纹一起报。同一份问题集重复跑的
   分位数区间见口径⑥（排序结论一题不变，动的正是分位数）。
4. **端到端里最大的一段是路由，不是检索也不是生成**：路由残差 mean 13836.1ms，占端到端
   mean 21558.3ms 的 **64%**——本机 8B 单条分类就要 10.6–19.2s。所以这张表的"端到端"只能当
   **这条最小切片**的形状看，不能当生产端到端延迟基线（生产走云端模型，路由段成本结构不同）。
5. **切片口径**：一条数据条目 = 一个 chunk，正文含 `# 《X》第N条` 标题行，**未走生产入库切片器**。
   标题行不是装饰：去掉它 BM25 实测从 0.6957 掉到 0.3478（本 Demo 第一版就是这么算错的）。
   同一条问题加 `--hybrid` 的实测形状：日志
   `hybrid retrieval kb=legal-demo dense=20 sparse=on fused=20 rerank=closed docs=5`，top-5
   多出《民法典》第148/149条；那一次检索段 2766ms 的大头是**进程内 BM25 首次建索引**
   （`BM25 index built for kb=legal-demo docs=4865`），不是融合算法的成本。
6. **同一份问题集重复跑：排序结论逐轮一致，分位数不逐轮一致**（2026-10-25，`eval --repeat` 实跑）：
   质量指标跨轮**完全相同**（Recall@5 恒 0.8696、MRR 恒 0.7029——检索是确定性的，routed 轮的
   `route` 也 23/23 一致），但分位数会动，幅度与 n 成反比：
   - 检索层 ×5（`--retrieve-only --skip-route`）：P50 49.2–53.6ms、**P95 119.3–144.4ms**（≈±20%）；
   - 检索+生成 ×3（`--skip-route`）：生成 P50 8818–9537ms、生成 P95 14273–14856ms、e2e P95 14454–15026ms；
   - 全链路路由 ×2（默认）：e2e **P50 24854.6 / 25620.8ms、P95 31740.3 / 35666.5ms**。
   两条可读的结论：① 上表那次录到的 e2e P95 **31543.1ms 落在重复跑的区间内**（31740ms 那一轮几乎逐位复现），
   所以那不是手气好，但它周围的抖动有 ≈±12%；② **路由段自身在漂**——同样 23 题，路由残差 P50 两轮
   16174 / 18019ms，而上表录的是 13462ms（差 2–4s，随机器负载/热状态），**路由既是最大段也是最不稳的一段**。
   所以"连自己这一轮都稳不住"（见上一条）不是措辞谨慎，是实测：报分位数要么连重复轮次一起报、要么改用
   重复池化（`--repeat` 的 `pooled_latency_ms`）再取分位数；`--repeat` 默认 1，summary 键集与历史一致
   （不新增任何键，已与 `data/demo/eval_retrieve_only_cacheOFF.json` 比对 key set，Recall/MRR 也逐位相同；
   变的只是重跑本就不同的毫秒数）。

**口径⑦：50 题扩样集（2026-10-25，只测检索层）——历史测量，评测集已于 2026-10-26 下线**

主表的 23 题是历史基线，**一个字节都没动**；扩样另立 `evals/legal_golden_ext.jsonl`（新增 27 题，L103–L129，**2026-10-26 随评测集删除**），由 `--golden` 把两份拼起来跑，分母 23→50。新题只挑**金标条文确实在语料里**
的条款（`# 《X》第N条` 标题与 `must_contain` 关键词均用脚本逐条比对过原文），
所以这组数字回答的是"多 27 道可答题后形状如何"，不是"检索器变强了多少"。扩样集只测检索层
（路由/生成与样本量无关，仍以主表为准）；生成物是
[`docs/evals/golden50-summary.json`](docs/evals/golden50-summary.json)（留档，不再由守卫重算）。

| 指标 | 数值 | 口径 |
|---|---|---|
| 样本量 | 50（合并 baseline + ext） | 23 基线 + 27 扩样；两份评测集均已于 2026-10-26 下线 |
| Recall@5（silver） | **0.9400（47/50）** | 匹配器与主表口径①同一套 |
| 严格 Recall@5 | **43/50 = 0.8600** | 金标文档本身进 top-5 |
| MRR | 0.7933 | 首命中位次取倒数、不截断到 k |
| top-1 命中 | 35/50 | 23 题基线的 top-1 是 14/23 |
| 漏召回题号 | L049/L055/L056 | 全部落在覆盖缺口上（三条都是原 23 题集里的题），扩样 27 题零漏召回 |
| 覆盖缺口天花板（严格口径） | **45/50 = 0.9000** | 5 条金标条文不在语料（主表口径①列的那 5 条）；**扩样 27 题没有新增缺口**，缺口行全部来自基线 |
| 扩样组命中 | 27/27（21 题 top-1，6 题 2–5 位） | silver 与严格同值：27 题的金标条文都在库里、且金标文档本身都进了 top-5 |

读法三条：

- **47/50 (0.9400) 与 45/50 (0.9000) 不是一回事**：前者是 silver 命中，后者是"金标条文在库里"的严格口径
  天花板。silver 侧的天花板同为 47/50——3 条 silver 漏召回恰好就是 3 条覆盖缺口行，不是排序失败。
  4 条"命中但金标文档不在 top-5"里，L053/L054 是命中了引用该条文的司法解释 chunk（金标法规不在语料），
  L060/L062 是金标条目在库里但排到 top-5 之外——与主表是同 4 题、同一归因。
- **Recall 的抬升首先反映样本构成**：新增 27 题的取样条件是"金标条文在语料里"，
  这本身就是有偏的；它可以和 23 题主表并排读，**不能**当成同分布样本上的提升量，
  也不能据此说检索质量提高了若干个百分点。
- **延迟不在这个口径里发布**：cache OFF、只测检索段的同一天三次单轮 50 题分位数是
  P50 **115.4 / 47.8 / 49.5ms**、P95 **226.8 / 116.5 / 119.2ms**（首轮冷态离群）；
  3 轮重复是 P50 50.8 / 51.8 / 51.4ms、P95 126.1 / 122.0 / 117.2ms（池化 51.6 / 122.0ms）。
  所以口径⑦只发质量数字；延迟仍看口径③（缓存开关 A/B）与口径⑥（重复区间）——
  **不发布 ≠ 没测**，但单轮分位数确实不配进基线表。

复现（先按顶部最小 Demo 起好 LM Studio；`--repeat 3` 可顺带看跨轮抖动）。⚠️ 限定：下面两条命令里 `--golden evals/legal_golden.jsonl evals/legal_golden_ext.jsonl` / `--ext-golden` 指向的集**已于 2026-10-26 随旧 KG/RAG 金标集停用删除**，照抄会命中 `evals/golden_source.py` 的响亮退出 2（并列扫描出的可用题集清单）——本块保留为**历史读数复现路径**（改动 bash 块内任何一行都对不上当时读数），要重跑须显式换成在用题集（题数真源见 `evals/ceval_golden.manifest.json` 的 `rows`）：

```bash
.venv/Scripts/python.exe scripts/demo/local_kb_demo.py eval \
  --golden evals/legal_golden.jsonl evals/legal_golden_ext.jsonl --limit 50 \
  --retrieve-only --skip-route --no-prewarm --repeat 3 \
  --json-out data/demo/eval_ext50_retrieve_only.json
.venv/Scripts/python.exe scripts/demo/report_eval.py \
  --eval data/demo/eval_ext50_retrieve_only.json --corpus data/kb/legal_corpus.txt \
  --ext-golden evals/legal_golden_ext.jsonl --retrieval-only \
  --generated-for "README §15.2 口径⑦（50 题扩样集，只测检索层）" \
  --write-summary docs/evals/golden50-summary.json
```

**与 §15.1 的对照已做到逐题对拍**（同一套 harness、同一份语料 SHA，把 `DATABASE_URL`/`INDEX_DIR`
指回 2026-10-11 那套隔离评测库）：Dense 精确复现 **18/23 = 0.7826**，逐题只有 4 个位次变化
（L044 3→4、L046 None→1、L047 None→1、L052 2→1）。差异原因查明：那份评测库有
**429 条 chunk 被截断在第一行**（例：《劳动合同法》第19条 的 `content` 是
`'# 《劳动合同法》第19条\n第十九条'`，18 字，正文全丢；本次同条 220 字），
两侧总字符 637,041 vs 696,224（+59,183）——L046/L047 问的正是试用期条款，正文缺失时不可召回。
**截断是那一次灌库脚本的产物，今天的生产入库链路是否同样截断未经验证**。
（那次对拍跑在 Anaconda 栈上；口径③已证逐题位次对栈不敏感，所以仍可与 §15.1 并排读。）
BM25 侧本次 0.6957/0.5486 vs §15.1 的 0.6957/0.5176：Recall 完全一致，MRR 略升。
（单位提醒：§15.1 的 0.7826 与这里的 BM25 0.6957 都是 **silver** 口径实测值；0.7826 恰好等于
上面算出的严格口径天花板 **18/23**，数值撞在一起，不是同一个量——别把"复现了旧数字"读成
"旧数字是严格口径"。）

四通道交叉对照（同一 `data/demo` 索引，`evals/hybrid_eval.py` 原样跑；
**两套栈各跑一遍，四行数字逐位相同**——产物分别留档 `data/demo/hybrid_venv_py312.json`
与 `data/demo/hybrid_anaconda_py313.json`）：

| 通道 | Recall@5 | MRR |
|---|---:|---:|
| BM25 | 0.6957 | 0.5486 |
| Dense | 0.8696 | 0.7029 |
| RRF | 0.8696 | 0.7065 |
| +Rerank | 0.8696 | 0.7065 |

`+Rerank` 与 `RRF` 数字相同，是因为本机没有 Cross-Encoder 服务、精排调用 fail-open 保留融合
原序；日志里的 `rerank=closed`（`hybrid retrieval … dense=20 sparse=on fused=20 rerank=closed docs=5`）
是 rerank **熔断器的状态字符串**（closed＝熔断器闭合、失败数未达阈值），不能读成"精排无效"，
也不等于"精排未执行"——精排是否实际执行以阶段报告的 `applied` 字段为准，两个口径不得互引；
§15.1 的 +Rerank 列走的是 Infinity CPU 服务真推理。

复现命令（bash；解释器用 `.venv`；模型侧前置见顶部最小 Demo）：

```bash
.venv/Scripts/python.exe scripts/demo/local_kb_demo.py seed
.venv/Scripts/python.exe scripts/demo/local_kb_demo.py eval --route kb-query --limit 23 --top-k 5 \
  --recall-k 5 --json-out data/demo/eval_e2e_venv_py312_openai1109.json   # 端到端（23 题串行约 9 分钟）

PYTHONPATH=. DATABASE_URL="sqlite:///D:/Code/My-Agentix/data/demo/agentix_demo.db" \
  INDEX_DIR="D:/Code/My-Agentix/data/demo/indexes" \
  EMBEDDING_BASE_URL="http://127.0.0.1:1234/v1" EMBEDDING_MODEL="text-embedding-bge-m3" \
  EMBEDDING_API_KEY="lm-studio" \
  .venv/Scripts/python.exe evals/hybrid_eval.py --kb-id legal-demo --route kb-query --limit 40 \
    --search-k 5 --json-out data/demo/hybrid_venv_py312.json              # 四通道对照
```

**这组数字当前不能用来做什么**：不能判 SLO（n=23、单轮、无重复采样、冷热缓存未控制）；
不能与 §15 主表的服务端 TTFT 口径互换；不能当生成延迟基线（路由占了两段里的绝大多数）；
不能推广为"混合检索/精排的收益结论"（Recall 未变、MRR 差异 0.0036 在 23 题上不到一题）；
**不能把两套 Python 栈的延迟数字互读**，也不能脱离环境指纹引用其中任何一个毫秒数；
换模型快照、换机器、换语料都要重测。

### 15.3 STARD 公开集检索评测（当前口径）

检索质量以公开数据集 **STARD**（Su et al., *Findings of EMNLP 2024*，MIT，
`github.com/oneal2000/STARD`）为准：1,543 条真实非专业用户法律咨询、2,717 个条款级相关对、
55,348 条候选法条，官方 `train` 1,235 / `dev` 308 划分零重叠。命中判定按 qrels 的
**精确 docid**，定义为「至少一条相关法条进入 top-k」，不使用 silver 文本匹配。
`dev` 是盲测集（权重与参数在 `train` 上定），下表 dev 列是唯一对外读数。

<!-- STARD_TABLE_START -->
> 本表由 `evals/stard_summary_governance.py` 从两份进仓 JSON 自动生成；每个数字都可由 `docs/evals/stard-lexical-summary.json` 与 `docs/evals/stard-semantic-summary.json` 回算，手工改动会被守卫拦下。

| 语料 | 通道 | dev R@5 | dev R@10 | dev MRR | dev NDCG@5 | 全量 R@5 |
|---|---|---:|---:|---:|---:|---:|
| 全库 55,348 | BM25 | 0.4708 | 0.5422 | 0.3942 | 0.3327 | 0.5217 |
| 受控子集 8,000 | BM25 | 0.5812 | 0.6494 | 0.4761 | 0.4192 | 0.6060 |
| 受控子集 8,000 | **Dense** | **0.7110** | **0.7760** | **0.6014** | **0.5455** | **0.7207** |
| 受控子集 8,000 | RRF(BM25+Dense, k=60) | 0.6591 | 0.7565 | 0.5584 | 0.4994 | 0.6889 |
<!-- STARD_TABLE_END -->

以下手写转述为 **历史 / 方向性** 读数，各有自己的样本口径、不与上表混算，也不作门禁数字：RRF(k=10) 在受控子集 dev 上 R@5 0.6916 / R@10 0.7792 / MRR 0.5698、全量 R@5 0.7084，来自早期调参记录，无进仓生成物。

**受控子集**＝全部 1,445 个金标法条 + `seed=42` 抽样的 6,555 条干扰文档，共 8,000 条。
本机 embedding 吞吐约 33 ms/条，55,348 条全库向量约需 30 分钟，故语义通道在子集上测；
子集保留全部金标、覆盖率为 100%，其绝对值高于全库，**只用于同一子集内的方法间比较**。
全库 BM25 与子集 BM25 语料规模不同，两行不可互读。生成物：
[`docs/evals/stard-lexical-summary.json`](docs/evals/stard-lexical-summary.json)、
[`docs/evals/stard-semantic-summary.json`](docs/evals/stard-semantic-summary.json)。

复现（数据落 `.tmp/stard/`，已 gitignore；`--mode semantic` 需要本机 embedding 服务）：

```bash
.venv/Scripts/python.exe evals/stard_eval.py --fetch-only
.venv/Scripts/python.exe evals/stard_eval.py --mode lexical \
  --write-summary docs/evals/stard-lexical-summary.json
.venv/Scripts/python.exe evals/stard_eval.py --mode semantic --subset 8000 \
  --write-summary docs/evals/stard-semantic-summary.json
```

**查询侧方法的消融**（**历史 / 方向性** 读数：每项都在真实口语 query 上跑过，数字各按自己的样本口径，不与上表混算，也不作门禁；其中 CE 单行是名次覆盖形态，方向性小样本）：

| 方法 | 样本口径 | R@5 变化 | 判定 |
|---|---|---|---|
| 查询改写 / 槽位扩展 / 关键词视图 / 法规名视图 | 全库 200 题 | 0.5550 → 0.5550 | 持平，仅 MRR 0.4389→0.4433 |
| 词法多查询 Fan-out + RRF（四视图） | 全库 200 题 | 0.5550 → 0.5550 | 持平 |
| 子查询分解（`/` 分隔的复合问句） | 全库 300 题 | 0.5400 → 0.5400 | 持平 |
| 伪相关反馈 PRF（top-5 词项扩展） | 全库 300 题 | 0.5400 → 0.4730 | 劣化 |
| 语义多查询 Fan-out + RRF（Q1+Q2，k=10/20/60） | 子集 1,543 题 | 0.7110 → 0.6851–0.6916 | 全部低于原句单路 |
| 语义 PRF（原句 + 首轮法规名） | 子集 1,543 题 | 0.7110 → 0.6558 | 劣化 |
| 线性 Learning-to-Rank（10 个 query-doc 特征，train 学权重 / dev 盲测） | 全库 1,543 题 | 0.4708 → 0.4675 | 无增益，词面覆盖率权重为负 |
| Cross-Encoder 精排（dense top-20 → top-5） | 子集 60 题方向性验证 | 0.7000 → 0.7333 | 正向，但未做全量（单题约 3 s） |

诊断口径：全库 300 题里 138 题未进 top-5，其中 80 题（58%）的金标已在前 100 名候选内，
属排序失败而非召回失败；但把金标从 20–100 位拉到前 5 需要语义信号，词法特征做不到——
STARD 论文指出的「生活事实 ↔ 法律事实」语义鸿沟下，口语咨询里的词在正确法条中本就不出现，
所以词面覆盖率、标题命中这类特征学出来是负权重。能稳定提升召回的是语义通道本身：
受控子集 dev R@5 从 BM25 的 0.5812 提到 Dense 的 0.7110（**+12.98 个百分点，相对 +22.3%**），
MRR 0.4761 → 0.6014；把 BM25 与 Dense 做 RRF 融合反而稀释语义信号（0.6591，k=10 时 0.6916，
仍低于单路 Dense）。因此**默认检索保持 Dense 单通道**，混合总闸只在有独立词法证据时开启，
查询改写 / 槽位扩展 / HyDE 保持默认关。

生产侧对应配置：`RETRIEVAL_HYBRID_ENABLED=false`（默认）即 Dense 单通道；
`RETRIEVAL_DENSE_RERANK_ENABLED`（默认关）把 dense 候选池放大到
`RETRIEVAL_RERANK_CANDIDATES` 后走 Cross-Encoder 精排，再截回 `search_k`，
服务不可用或熔断时按原召回顺序 fail-open；生效时缓存池加 `|d1` 后缀隔离。
精排每次检索增加一次上游调用，开启前按实际 QPS 与预算评估。

### 15.4 运行时基线与联合实验生成物

以下每个结论都有进仓 JSON + 复现命令 + 守卫（`tests/test_stard_governance_guard.py`）。
本节不复述读数，只给生成物与口径边界；读数一律以 JSON 为准，避免再产生「只有 README 文字」的数字。

| 生成物 | 内容 | 复现命令 |
|---|---|---|
| `docs/evals/stard-runtime-default-baseline.json` | 默认链路端到端基线：质量双口径、六层延迟、调用面、配置指纹 | `.venv/Scripts/python.exe evals/runtime_default_baseline.py --split dev --write-summary docs/evals/stard-runtime-default-baseline.json` |
| `docs/evals/stard-ce-fusion-summary.json` | blend + 同候选集 z(CE)：val 定参、dev 单次盲测、硬上限归因、CE 分数缓存 | `.venv/Scripts/python.exe evals/stard_ce_fusion.py --phase ce` 与 `--phase report --write-summary ...` |
| `docs/evals/stard-zfusion-mu-selection.json` | 第 1 件 A/B 的 z 融合 μ 在 val 300 上的定参网格 | `.venv/Scripts/python.exe evals/stard_zfusion_val_select.py --write-summary ...` |
| `docs/evals/stard-rerank-ab-summary.json` | 候选池 20/50/100 × 精排 off / 名次覆盖 / z 融合分列 | `.venv/Scripts/python.exe evals/stard_recall_lab.py --split dev60 --ce-cache .tmp/stard/ce_ab_dev60.json ...` |
| `docs/evals/stard-fusion-sweep-summary.json` | 34 组融合在 dev 308 与全量 1,543 并列验证 | `.venv/Scripts/python.exe evals/stard_fusion_sweep.py --split both --write-summary ...` |
| `docs/evals/stard-latency-probe-summary.json` | SingleFlight / 引用还原批量化 / Redis 连接池真实读取点 | `.venv/Scripts/python.exe evals/latency_probe.py --probe all --write-summary ...` |
| `docs/evals/stard-recall-lab-summary.json` | query memory 各方法小批次 / val / 全量读数 | `.venv/Scripts/python.exe evals/stard_recall_lab.py --split dev --write-summary ... --append` |

口径边界（写进生成物，不在正文另作转述）：

- 运行时基线是**默认链路的离线等价复刻**，不是线上真实读数或 SLO；`served_top5` 与
  `canonical_pool100` 两套读数不可互读，「既有数字不退步」只锚定发布口径。
- 六层延迟里的 `rerank` 是 `dense_rerank=false` 下的 no-op 占位，不是精排成本；
  开启在线 Cross-Encoder 的真实量级另见 A/B 生成物里的 `ce_latency`，
  且该 P95 是**离线 CE 缓存逐题 seconds，不是在线真实调用墙钟**。
- `call_surface.cold.db_sql_statements` 是引用还原**按 document 聚合批量化之后**的 SQL 计数。
- 离线实验形态（query memory、CE z 融合）不进生产默认；默认值一律未改，
  `RETRIEVAL_HYBRID_ENABLED=false` / `RETRIEVAL_DENSE_RERANK_ENABLED=false` 保持。
- 每份生成物含 `generated_at` / `config_fingerprint` / `corpus` / `split` /
  `metric_definition` / `reproduce`；`evals/stard_summary_governance.py --check` 反向校验全表与键一致性。

## 16. 已知限制

这是演示项目，不是可直接上线的生产系统。**四条最需要知道的边界**：

1. 执行沙箱默认关（`in_process` / `thread` 同进程同 GIL，不构成隔离）；未打开文中点名的闸门前，不应把实例暴露给不可信流量。
   - `subprocess` 档也**不是笼子**：它**不隔离文件系统与网络**（仍可出网、仍可读宿主可读的文件），且**仅 POSIX 可用**——非 POSIX 平台 fail-closed 拒绝执行，不做无隔离降级。
   - **运行时写代码执行（code exec）继承同一组边界**：`CODE_EXEC_ENABLED` 默认关；关着时路由不产 `code-query`、节点不可达、工具零注册，**与基线逐位等价**。开着的它也**不隔离文件系统与网络**、**仅 POSIX 可用**，且**不进 LLM 可见工具面**（由 `code_agent` 节点直接调用，不让模型自己挑）。它要真跑起来还得同时开 `EXECUTION_SANDBOX_ENABLED` + `TOOL_SANDBOX_SUBPROCESS_ENABLED`——**只开业务总闸跑不起来**，缺哪一闸会**响亮失败**而不是退回进程内。
2. 质量门禁把关的是“打分口径没被放宽”，不是“这次改动有没有让答案变差”——后者必须重新录制基线才看得见。
3. 端到端只有最小切片的小样本（n=23）；全栈服务口径（FastAPI + 容器 + Prometheus TTFT、成本聚合）仍是 `<待实测>`。
4. 流式审批（`STREAM_HITL_ENABLED`，默认关）**只交付了后端帧**：`web/` 零改动，前端 `chatStream.ts` 的 `ChatStreamChunk.type` 仍是六值、`dispatchChunk` 对 `approval` / `heartbeat` 走 `default` 直接丢弃——浏览器端审批卡片另立排期。另外它**跨 worker** 才完整：裁决真源是 T18 的库行（本地闸门只是快路径），等流靠「本地闸门优先、未决时回捞库」看见别的 worker 上的裁决，因此要求 `HITL_PENDING_PERSIST_ENABLED=true`；关着时该流只看得到本进程的裁决，跨 worker 的裁决要等到调用墙钟超时。

逐条边界（含 `kb_ingest` 三道默认关、golden 覆盖缺口、路由兜底计数、子进程沙箱的已知空洞、checkpointer 静默降级等）见 [docs/已知限制.md](docs/已知限制.md)。

---

## 参与贡献

欢迎任何形式的贡献——代码、文档、评测集扩充、问题反馈。

1. 从 `main` 创建分支（本仓库当前**未发布公共远端**，Fork 流程等发布后启用）→ 2. 遵循分层架构约束 → 3. `ruff` / `black` / `mypy` / `pytest` 全过 → 4. Conventional Commits 格式提交 → 5. 按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 提交

| 文件 | 内容 |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | 开发环境、**架构约束**、代码与提交规范、PR 流程 |
| [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) | 社区行为准则 |
| [SECURITY.md](SECURITY.md) | 漏洞报告流程（请勿公开 Issue） |
| CHANGELOG.md | 版本变更记录 |

---

## 许可证

本项目采用 [Apache License 2.0](LICENSE) 许可证。

---

## 联系方式

- 📦 **托管状态**：本仓库当前**未发布到公共远端**，因此不提供 GitHub 主页 / Discussions / Issue 入口，页首也不挂 CI 状态徽章；工作流的真实形态与本地实测口径见 [§11 工程实践](#11-工程实践)。发布后再在此处与页首回填链接。
- 🔒 **安全漏洞**：请按 [SECURITY.md](SECURITY.md) 流程私下报告
- 🤝 **贡献指南**：见 [CONTRIBUTING.md](CONTRIBUTING.md)

---

<div align="center">

**My Agentix** — 可控制、可恢复、可评估、可观测的 Agent Platform

[回到顶部](#my-agentix)

</div>
