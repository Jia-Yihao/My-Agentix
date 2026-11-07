<div align="center">

<img src="./docs/assets/agentix-logo.png" alt="My Agentix Logo" width="320" />

# My Agentix

**一套面向生产的 Agent 平台内核：把规划、执行、治理、检索、评测与可观测做进同一套代码。**

FastAPI · LangGraph · Vue 3 · PostgreSQL · Redis · Neo4j · Milvus

</div>

> 本仓库尚未发布到公共远端（GitHub 仓库目前为私有）；下方命令按本地目录执行，不提供公开 clone 地址。

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 核心特性](#2-核心特性)
- [3. 架构总览](#3-架构总览)
- [4. 快速开始](#4-快速开始)
- [5. 配置](#5-配置)
- [6. 常用命令](#6-常用命令)
- [7. 检索与评测](#7-检索与评测)
- [8. 可观测性](#8-可观测性)
- [9. 安全](#9-安全)
- [10. 项目结构](#10-项目结构)
- [11. 技术栈](#11-技术栈)
- [12. 文档](#12-文档)
- [13. 已知限制](#13-已知限制)
- [14. 参与贡献](#14-参与贡献)
- [15. 许可证](#15-许可证)
- [16. 联系方式](#16-联系方式)

---

## 1. 项目简介

My Agentix 是一套**领域无关的 Agent 平台内核**。它不是某个垂直领域的问答机器人：Agent 循环、工具治理、运行预算、检索融合、可观测与评测这些平台能力写在同一套代码里，领域知识（图谱 Schema、提示词、语料、评测集）通过适配层接入，同一套内核可以迁移到咨询、客服、政务等场景。

对外它是一个 FastAPI 服务，提供两套问答域——**知识库检索**（多租户、带引用、可混合检索）与**通用助手**（规划 + 工具调用 + 多智能体）——共用同一份 Run/Event 真源、同一个工具网关和同一套轨迹。

典型的用法是把它当作 Agent 后端的起点：替换 `data/` 里的领域数据与 `agentix/application/agents/` 下的提示词，保留内核与前端。

<div align="center">
  <img src="./docs/assets/ui/knowledge-workspace.png" alt="知识库检索工作台" width="49%">
  <img src="./docs/assets/ui/assistant-workspace.png" alt="通用助手工作台" width="49%">
</div>

## 2. 核心特性

- **可控的执行**：每次运行有 `run_id`、六维预算（步数 / LLM / 工具 / token / 时延 / 成本）与超时，可查询、可取消，取消能真正打断 asyncio Task。
- **可恢复的失败**：错误分类 → 退避重试 → 模型与工具 Fallback 链 → 降级拒答，不悬挂。
- **统一的工具面**：所有工具经 Tool Gateway（权限 → 风险 → 超时 → 重试 → 轨迹），支持 MCP 远端工具与本机 stdio Server。
- **真隔离的执行位置**：`ToolSpec.sandbox` 声明执行位置，`subprocess` 档一次调用一个进程 + 子进程侧 `RLIMIT_AS/CPU/FSIZE` + 环境白名单 + 可杀进程组；拿不到的档位**拒绝执行**，不静默退回进程内。
- **混合检索**：知识库检索统一使用 Dense + BM25/z-score（Dense 权重 α=0.85），之后可接交叉编码器精排与融合后分数闸（默认关、fail-open、关 = 基线逐位等价）；上下文按 token 预算装配，引用与评测 contexts 只取模型真正读过的片段。图谱 Cypher、Text2SQL 与社区摘要提供专门的领域检索能力，来源回填成引用。
- **分层记忆**：user / knowledge / task 3 scope 的记忆写入与召回，写路径零 PII 留存，召回按 scope 过滤并按置信度排序。
- **可评估的质量**：确定性打分（路由 / 引用 / 拒答 / 引用正确性 / must_contain / pass^k）、轨迹前缀回归、延迟基准与每日性能巡检。
- **可观测的轨迹**：trace_id 全链路、步骤级轨迹落盘可回放、Prometheus 指标、OTel GenAI 语义 span。
- **可扩展的边界**：AST 分层依赖守卫 + 架构守卫测试，跨层引用即失败；注册表可插拔，新增工具 / Provider / 子图不改调用链。
- **灰度优先**：会改变主链路形态的开关默认关，flag-off 与基线逐位等价。

## 3. 架构总览

<div align="center">
  <img src="./docs/assets/project-architecture.png" alt="My Agentix 项目整体架构图" width="100%">
</div>

编排由 LangGraph **单层主图**承载：主图上**平铺 10 个节点**，靠条件边路由，重业务分支的子图收在节点内部，不再叠一层。`agentix/runtime/` 是运行时真源，SSE、Run API、轨迹与评测消费同一份 Run/Event。

```text
API / SSE / Run API（只做协议转换）
  → 应用层创建 Run（准入排队 · 单 owner · 取消打断 Task）
  → 执行循环（LangGraph 主图 或 AgentLoop，AGENT_LOOP_ENABLED 灰度）
      Context → Plan → Execute → Observe → Verify（Replan 受预算约束）
  → Runtime（状态 / 步骤 / 六维预算 / 生命周期 / 统一退出契约）
  → Tool Gateway（权限 / 风险 / 超时 / 重试 / HITL / 审计）
  → Capabilities（tools · retrieval · kg 子图 · MCP · memory）
  → Infrastructure（Milvus / Neo4j / Redis / LLM / 外部服务）
```

| 层 | 职责 | 代码位置 |
|---|---|---|
| Domain | 纯契约：错误词汇表、Run/Step/Budget、ToolSpec、端口 Protocol | `agentix/domain/` |
| 编排 | 主循环、执行器、主图、多智能体策略 | `agentix/orchestration/` |
| 策略件 | planner / verifier / HITL / guardrails | `agentix/policies/` |
| 工具 | 唯一执行入口与网关三闸 | `agentix/tools/` |
| 应用 | 业务规则、子图接线、会话与记忆 | `agentix/application/`、`agentix/agent/` |
| 基础设施 | 实现 Domain 端口：DB / 缓存 / LLM / Checkpointer | `agentix/infrastructure/` |
| 接口 | HTTP 路由、请求响应模型、中间件 | `agentix/interfaces/` |

依赖方向由 AST 守卫强制：`infrastructure` 不得 import `interfaces`，`domain` 零第三方依赖，守卫在 CI 中阻断违规。

## 4. 快速开始

### 4.1 最小 Demo（不用 Docker、不用云端 key）

模型全部走本机 LM Studio（`qwen/qwen3-8b` 生成 + `text-embedding-bge-m3` 嵌入），存储是 SQLite，向量索引是 FAISS，BM25 是进程内纯 Python——路由 → 检索 → 带引用回答整条链路在一个进程里跑完。

```bash
lms server start                                                     # 前置：LM Studio 开 HTTP 服务（端口 1234）
python scripts/demo/local_kb_demo.py seed                            # 4,865 条语料 → SQLite + FAISS
python scripts/demo/local_kb_demo.py ask "合同欺诈的认定依据是哪一条"   # 路由 → 检索 → 带引用回答
```

解释器用仓库 `.venv`（Python 3.12）；`seed` 冷缓存约 90s，命中本地嵌入缓存后约 4s。

### 4.2 完整平台

```bash
# 1. 虚拟环境与依赖
python -m venv .venv
.venv\Scripts\activate                 # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt    # 运行时 + pytest / ruff / black / mypy / ragas
pip install -e ".[dev,test]"

# 2. 环境变量（至少填 LLM_API_KEY / EMBEDDING_API_KEY / SECRET_KEY）
cp .env.example .env

# 3. 基础设施：compose 共 14 个 service，web 是第 14 个
docker compose up -d postgres redis neo4j mysql milvus kb_ingest prometheus alertmanager grafana
# 可选：AMQP broker（死信 / ack 语义），只在 --profile mq 下启动
# docker compose --profile mq up -d rabbitmq

# 4. 后端（本机进程，故意不放进 compose，避免先占住 8000）
export DATABASE_URL="postgresql+psycopg://postgres:postgrespass@localhost:15432/agentix"
export INGEST_SERVICE_URL="http://localhost:8100"
python -m uvicorn agentix.main:application --reload --port 8000

# 5. 前端（另开终端）
cd web && npm install && npm run dev
```

访问 `http://localhost:5173`（开发服务器，走 Vite proxy）；容器化部署时由 nginx 同源反代到 `http://localhost:8080`。

## 5. 配置

配置只有一处真源：`agentix/config/settings.py` 的 `Settings`（模块级单例，按精确大写名读环境变量、大小写敏感、读根目录 `.env`）。完整键表与回滚方式见 [配置管理](docs/architecture/配置管理.md)。

```bash
# .env —— 起服务最少要这几项
LLM_API_KEY=...
EMBEDDING_API_KEY=...
SECRET_KEY=...
DATABASE_URL=postgresql+psycopg://postgres:postgrespass@localhost:15432/agentix
REDIS_CHECKPOINT_URL=redis://localhost:6379/0
```

会改变主链路形态的开关默认关闭：`AGENT_LOOP_ENABLED`、`TASK_EXEC_ENABLED`、`AGENTIX_ABLATION_MULTI_AGENT`、`HUMAN_REVIEW_ENABLED`、`TOOL_GATEWAY_ENFORCED`、`CODE_EXEC_ENABLED`，逐项含义与回滚方式见配置管理文档的开关回滚表。

**会话持久化的库号有硬约束**：LangGraph checkpoint 走 `REDIS_CHECKPOINT_URL`，默认 db 0。RediSearch 只允许在 db 0 建索引，改成 `6379/2` 会让索引建立失败并报 `Cannot create index on db != 0`，进程不报错、会话却静默失去持久化——要分片请分 Redis 实例，不要分库号。

## 6. 常用命令

```bash
make install        # pip install -r requirements-dev.txt + cd web && npm install
make dev            # 同时起后端（8000）与前端（5173）
make run-server     # 只起后端
make run-web        # 只起前端
make lint           # ruff check + black --check + mypy agentix/domain（与 CI 同口径）
make test           # pytest tests/ -v
make init-data      # 建运行时数据目录
```

**当前测试基线（2026-11-06 实测）**：`env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -m "not integration" -q` → **6089 passed / 40 skipped / 6 deselected / 0 failed**（Python 3.12.13，单机离线，未开并行）；`ruff check agentix scripts evals kb_ingest` 全绿。

> `PYTHONPATH` 必须先清空，否则沙箱相关用例会假失败。

## 7. 检索与评测

检索质量统一以公开数据集 **STARD 的 8,000 文档受控子集**和精确 docid qrels 为准。Recall@K 是每题检出的相关法条占该题全部相关法条的比例，再对题目宏平均；不是查询命中率或分类准确率。融合参数只由 train 内固定 300 题 validation 选择，官方 `dev` 308 题留作最终盲测；首页只展示项目当前唯一默认方案的 dev 指标。

<!-- STARD_TABLE_START -->
> 本表由 `evals/stard_summary_governance.py` 从唯一基准报告自动生成；手工改动会被守卫拦下。

| 数据集 | 默认方案 | 通道候选深度 | dev Recall@1 | dev Recall@5 | dev Recall@10 | dev Recall@20 | dev MRR | dev NDCG@5 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| STARD 8,000 文档受控子集 | Dense + BM25/z-score (α=0.85) | 80 | 0.3757 | 0.5976 | 0.6899 | 0.7689 | 0.5972 | 0.5443 |
<!-- STARD_TABLE_END -->

默认检索为 Dense + BM25 原始分数 z-score 融合（Dense 权重 0.85、每通道候选深度 80、输出 Top-20），融合之后接三段可选处理。池深、预算、超时这些数值就是下面写的那一套，写在 `agentix/retrieval/settings.py` 里——克隆下来不配 `.env` 也跑调优档位；两个质量闸的总闸默认关，关掉时与上表逐位等价：

- **交叉编码器精排**（`RETRIEVAL_RERANK_ENABLED`）：把融合池前 50 条交给交叉编码器重打分（本地参考实现用 bge-reranker-v2-m3），取前 20 条进回答。端点可以是按量计费的云端 text-rerank，也可以是自建的 `/v1/rerank` 服务——仓库附一个本地 CUDA 实现 `scripts/local_rerank_server.py`，输出 `sigmoid(logit)` 与云端 relevance_score 同量纲，`MIN_SCORE` 两边可直接互换。超时、协议异常、无凭据一律 fail-open 回融合原序：精排是加分项，不许把检索链打挂或打空。
- **融合后分数闸**（`RETRIEVAL_FUSION_MIN_SCORE`）：仅在双通道真融合时按 z-score 下限裁掉尾部；裁空了回退 Top-1，不制造空答。单通道保留的是距离原分，量纲与方向都不同，这道闸对它一律不生效。默认不设：同一套 qrels 上「关闸 + 池 50」与「开闸且池深按 z-score 压到 40 上下」的全量 Recall@20 差异不足千分之一，落在噪声内，出厂档位取更简单的那个。
- **预算对齐装配**（`CONTEXT_BUDGET_TOKENS`）：按 token 预算贪心装配，并把引用列表、命中片段与评测 contexts 一起收窄到真正进了 Prompt 的那几条，避免「引用一段模型没读过的文本」、faithfulness 拿全量片段评估。本批片段确实经过精排时，`CONTEXT_AUTHORITY_RANKING_SKIP_ON_RERANK` 让查询相关的精排顺序直接进预算，不再被与问题无关的全局档位重排挤出窗口；默认预算 4,000 token，`0` 表示不限额。

精排的收益全在排序前端：dev 308 题上 Recall@20 抬高 1.7 个百分点、MRR 1.4 个、NDCG@5 1.7 个；全量 1,543 题复跑同向，三项分别 +3.0 / +2.9 / +2.5 个百分点。精排只重排不增召回——候选池里本来没有的文档仍然找不回来，所以收益随首发召回率见顶。验证集参数扫描与锁定后的 dev 结果、指标口径和复现方式见[评测说明](evals/README.md)。

```bash
python evals/stard_default_retrieval.py --help # 唯一检索基准与调参评测
python evals/run_eval.py --help              # 离线打分（需一份题集，缺输入会明确报错）
python evals/trajectory_regression.py --help # 轨迹前缀回归（-b 指向已录制的轨迹包目录）
```

打分口径、覆盖矩阵、消融与回流见 [评测说明](evals/README.md)。

## 8. 可观测性

- **指标**：Prometheus 抓 `/metrics`。首 token 延迟取 `agent_ttft_seconds` 直方图；token 量按 `llm_tokens_total{type="prompt"}` 与 `llm_tokens_total{type="completion"}` 分别累加，另有 `type="cached"` 记命中缓存的输入；Grafana 看板随 compose 起，告警走 Alertmanager。
- **轨迹**：每次运行的步骤级轨迹落盘可回放，前端提供执行轨迹时间线。
- **追踪**：`trace_id` 中间件贯穿全链路，OTel GenAI 语义 span 记录 token 用量并按模型归属。
- **健康**：`/health`、`/liveness`、`/readiness` 分工明确，readiness 覆盖依赖可用性。

## 9. 安全

| 面 | 措施 |
|---|---|
| 认证与授权 | APIKey / JWT / RBAC；跨租户访问返回 404 或 403，不回显目标资源 |
| 限流与熔断 | 中间件级限流；熔断在检索域两处接线 |
| 提示注入 | 输入检测与入库拦截两档开关，检测默认开启 |
| 输出过滤 | 敏感信息与 PII 脱敏默认开启 |
| 执行隔离 | 工具按 `ToolSpec.sandbox` 声明选执行位置，`subprocess` 档带资源闸与环境白名单；**文件系统与网络不隔离**，属已登记残留 |
| 人工审批 | `HUMAN_REVIEW_ENABLED` 打开后高风险工具需审批 |

逐条风险与缓解见 [SECURITY.md](SECURITY.md) 与 [已知限制](docs/已知限制.md)。

## 10. 项目结构

```text
My-Agentix/
├── .github/                CI / 安全扫描 / Issue 与 PR 模板
├── agentix/                后端平台内核
│   ├── __init__.py
│   ├── main.py             FastAPI 装配入口
│   ├── config/             Pydantic Settings（配置唯一真源）
│   ├── domain/             纯契约层，零第三方依赖
│   ├── orchestration/      主循环 / LangGraph 主图 / 多智能体策略
│   ├── policies/           planner · verifier · HITL · guardrails
│   ├── tools/              工具唯一执行入口 + 网关三闸
│   ├── application/        业务规则与子图接线
│   ├── agent/              上下文、记忆、事件、技能、钩子
│   ├── runtime/            运行状态 / 步骤 / 预算 / 生命周期 / 控制平面
│   ├── capabilities/       MCP 客户端与 stdio 服务端
│   ├── infrastructure/     DB / 缓存 / LLM / Checkpointer / Text2SQL
│   ├── interfaces/         HTTP 路由与中间件
│   ├── retrieval/          检索域（多租户知识库）
│   ├── observability/      指标 · span · 轨迹
│   ├── reliability/        限流 · 熔断 · 并发
│   ├── security/           JWT · 输出过滤 · 注入防护
│   ├── crawler/            语料抓取与导入
│   └── performance/        性能与基准工具
├── bench/                  检索与性能对比脚本
├── data/                   领域数据（图谱种子与语料，由使用者填充）
├── docs/                   架构、ADR、Runbook、实现手册
├── docker/                 观测栈与 compose 覆盖文件
├── evals/                  评测脚本、门禁与消融
├── kb_ingest/              知识库入库独立服务
├── scripts/                数据构建、迁移、运维与本地 Demo
├── skills/                 仓库级 Agent 技能
├── web/                    Vue 3 + TypeScript 前端
├── .env.example            可配置环境变量示例
├── pyproject.toml          Python 项目与工具配置
└── README.md               项目说明
```

## 11. 技术栈

| 类别 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Uvicorn（SSE 流式） |
| 编排 | LangGraph · Agent Core 2.0（Harness + Capabilities） |
| 前端 | Vue 3 · TypeScript · Vite · Vitest |
| 存储 | PostgreSQL（结构化）· Redis Stack（缓存 / 任务队列 / 事件流 / Checkpointer / 限流，db0+db1 分片）· Neo4j（图谱）· Milvus（向量）· MySQL（统计） |
| 消息 | Celery（Redis broker 为默认；RabbitMQ 为 `mq` profile 下的可选 broker，切 URL 即换，DLX 死信拓扑按 scheme 自动声明） |
| 检索 | FAISS / Milvus 向量 · BM25 · Dense + BM25/z-score 混合召回 · 交叉编码器精排（可选）· token 预算装配 |
| 扩展 | MCP（stdio JSON-RPC）· 声明式 ToolRegistry |
| 可观测 | Prometheus · Grafana · Alertmanager · OTel |
| 质量 | pytest · ruff · black · mypy · 确定性打分 · 轨迹前缀回归 |
| 交付 | Docker / docker compose · GitHub Actions（CI + CodeQL + 依赖巡检） |

## 12. 文档

| 文档 | 内容 |
|---|---|
| [架构总览](docs/architecture/README.md) | 设计原则与运行时架构图 |
| [运行时护栏](docs/architecture/运行时护栏.md) | 上下文隔离、控制平面、工具限量、人工审批 |
| [配置管理](docs/architecture/配置管理.md) | 配置真源与开关回滚表 |
| [中间件选型](docs/architecture/中间件选型.md) | Redis 六类用途与分片、Celery broker 的两种失败语义、RabbitMQ 切换与死信验证 |
| [ADR-0007 执行沙箱端口](docs/architecture/adr/0007-执行沙箱端口.md) | 执行位置做成可声明端口的决策与残留 |
| [预发环境与配置矩阵](docs/runbooks/预发环境与配置矩阵.md) | 环境差异清单、降权与授权收紧的切换步骤、发布前验收 |
| [subprocess 沙箱取证](docs/runbooks/子进程沙箱取证.md) | 资源闸、环境白名单、降权与延迟数字的复现方式 |
| [本体 Schema](docs/design/测试数据域-本体Schema.md) | Neo4j 节点 Label、关系类型与属性契约 |
| [已知限制](docs/已知限制.md) | 沙箱、门禁、覆盖缺口、路由兜底与边界 |
| [评测说明](evals/README.md) | 打分口径、质量门禁、消融实验 |

## 13. 已知限制

- **检索结论只在 STARD 的法律咨询场景成立**，换语料需重测；受控子集只覆盖 8,000 条候选。
- **精排依赖外部端点**：云端按量计费、本地要自备 GPU 与权重，端点不可用时静默回退融合原序——收益归零但不报错，排查看阶段帧里的 `rerank.skipped`。
- **执行沙箱不隔离文件系统与网络**：`subprocess` 档挡住资源与环境污染，但进程仍可读宿主可读的文件、仍可出网。
- **`in_process` / `thread` 两档不是隔离边界**：同进程、同 GIL、同环境变量。
- **评测门禁未武装**：仓库内的题集是公开 MCQ 集，旧的人工金标已下线，门禁代码保留但不阻塞合入。
- **消息链路有两条语义，切换时不互通**：默认 Redis broker 靠 `visibility_timeout` 重投，RabbitMQ 靠 ack/nack 与 DLX；两个 broker 里在飞的消息不会互相迁移，切换前须先跑完。死信链路需要真实 RabbitMQ，CI 不覆盖，验证方式见中间件选型文档 §5。
- **前端测试不进 CI**：CI 只做 lint 与构建。
- 逐条展开见 [已知限制](docs/已知限制.md)。

## 14. 参与贡献

欢迎 issue 与 PR。流程与代码规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)，安全问题的报告方式见 [SECURITY.md](SECURITY.md)。

合入门槛是 CI 全绿：`ci.yml` 的汇总门 `ci-gate`（`if: always()`）要求四个 job 全绿：`version-consistency`（四处版本号一致）· `lint`（ruff 阻断 + black 仅报告 + mypy 只覆盖 domain 层）· `import-integrity`（跨层引用与命名装配）· `frontend`（`npm ci` + lint + build）。

性能门在 `perf-baseline.yml` 按日巡检、不阻塞 PR。提交前请跑 `make lint` 与相关测试。

## 15. 许可证

Apache License 2.0，见 [LICENSE](LICENSE)；第三方组件与原创内容归属见 [NOTICE](NOTICE)。

## 16. 联系方式

- 问题与建议：走仓库 issue
- 安全问题：见 [SECURITY.md](SECURITY.md) 的报告渠道，请不要公开提 issue
