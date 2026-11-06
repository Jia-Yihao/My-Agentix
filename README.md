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
- **多路检索**：BM25 / Dense / RRF 融合 / Cross-Encoder 精排 / 图谱 Cypher / Text2SQL / 社区摘要，缺一路有兜底，来源回填成引用。
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

# 3. 基础设施：compose 共 15 个 service，web 是第 15 个
docker compose up -d postgres redis neo4j mysql milvus kb_ingest prometheus alertmanager grafana

# 4. 后端（本机进程，故意不放进 compose，避免先占住 8000）
export DATABASE_URL="postgresql+psycopg://postgres:postgrespass@localhost:15432/agentix"
export INGEST_SERVICE_URL="http://localhost:8100"
python -m uvicorn agentix.main:application --reload --port 8000

# 5. 前端（另开终端）
cd web && npm install && npm run dev
```

访问 `http://localhost:5173`（开发服务器，走 Vite proxy）；容器化部署时由 nginx 同源反代到 `http://localhost:8080`。

可选精排服务：`docker compose --profile reranker up -d reranker`（首次会下载 `BAAI/bge-reranker-v2-m3`）。精排模型名由 `RERANKER_MODEL` 决定，平台配置里的 `RERANK_MODEL` 在检索侧没有消费者。

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

检索质量以公开数据集 **STARD**（真实非专业用户法律咨询，1,543 题、精确 docid 金标）为准，命中判定按 qrels 的精确 docid，不用文本匹配来猜。`dev` 是盲测集（权重与参数在 `train` 上定），下表 dev 列是唯一对外读数。

<!-- STARD_TABLE_START -->
> 本表由 `evals/stard_summary_governance.py` 从两份进仓 JSON 自动生成；每个数字都可由 `docs/evals/stard-lexical-summary.json` 与 `docs/evals/stard-semantic-summary.json` 回算，手工改动会被守卫拦下。

| 语料 | 通道 | dev R@5 | dev R@10 | dev MRR | dev NDCG@5 | 全量 R@5 |
|---|---|---:|---:|---:|---:|---:|
| 全库 55,348 | BM25 | 0.4708 | 0.5422 | 0.3942 | 0.3327 | 0.5217 |
| 受控子集 8,000 | BM25 | 0.5812 | 0.6494 | 0.4761 | 0.4192 | 0.6060 |
| 受控子集 8,000 | **Dense** | **0.7110** | **0.7760** | **0.6014** | **0.5455** | **0.7207** |
| 受控子集 8,000 | RRF(BM25+Dense, k=60) | 0.6591 | 0.7565 | 0.5584 | 0.4994 | 0.6889 |
<!-- STARD_TABLE_END -->

结论直接决定默认配置：口语咨询问法下 **Dense 单通道召回高于 BM25**；词法查询改写、扩展、多查询融合不提升召回；把 BM25 用 RRF 并入 Dense 反而稀释语义信号。因此出厂默认是 Dense 单通道、混合总闸关闭；`RETRIEVAL_DENSE_RERANK_ENABLED` 可在不打开混合的情况下对放大的候选池做精排，精排服务不可用时按原召回顺序 fail-open。

```bash
python evals/stard_eval.py --help            # 公开集评测
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
agentix/                   后端主包
├── config/                Pydantic Settings（配置唯一真源）
├── domain/                纯契约层，零第三方依赖
├── orchestration/         主循环 / 执行器 / LangGraph 主图 / 多智能体策略
├── policies/              planner · verifier · HITL · guardrails
├── tools/                 工具唯一执行入口 + 网关三闸
├── application/           业务规则与子图接线
├── agent/                 上下文、记忆、事件、技能、钩子
├── runtime/               运行状态 / 步骤 / 预算 / 生命周期 / 控制平面
├── capabilities/          MCP 客户端与 stdio 服务端
├── infrastructure/        DB / 缓存 / LLM / Checkpointer / Text2SQL
├── interfaces/            HTTP 路由与中间件
├── retrieval/             检索域（多租户知识库）
├── observability/         指标 · span · 轨迹
├── reliability/           限流 · 熔断 · 并发
└── security/              JWT · 输出过滤 · 注入防护

web/                       Vue 3 + TypeScript 前端
kb_ingest/                 知识库入库独立服务
evals/                     评测脚本、门禁与消融
scripts/                   数据构建、迁移、运维与本地 Demo
docs/                      架构、ADR、Runbook、实现手册
data/                      领域数据（图谱种子与语料，由使用者填充）
docker/                    观测栈与 compose 覆盖文件
bench/                     检索与性能对比脚本
```

## 11. 技术栈

| 类别 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · Uvicorn（SSE 流式） |
| 编排 | LangGraph · Agent Core 2.0（Harness + Capabilities） |
| 前端 | Vue 3 · TypeScript · Vite · Vitest |
| 存储 | PostgreSQL（结构化）· Redis（缓存 / Checkpointer）· Neo4j（图谱）· Milvus（向量）· MySQL（统计） |
| 检索 | FAISS / Milvus 向量 · BM25 · RRF 融合 · Cross-Encoder 精排（BGE / Cohere / Jina / Voyage） |
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
| [ADR-0007 执行沙箱端口](docs/architecture/adr/0007-执行沙箱端口.md) | 执行位置做成可声明端口的决策与残留 |
| [预发环境与配置矩阵](docs/runbooks/预发环境与配置矩阵.md) | 环境差异清单、降权与授权收紧的切换步骤、发布前验收 |
| [subprocess 沙箱取证](docs/runbooks/子进程沙箱取证.md) | 资源闸、环境白名单、降权与延迟数字的复现方式 |
| [本体 Schema](docs/design/测试数据域-本体Schema.md) | Neo4j 节点 Label、关系类型与属性契约 |
| [已知限制](docs/已知限制.md) | 沙箱、门禁、覆盖缺口、路由兜底与边界 |
| [评测说明](evals/README.md) | 打分口径、质量门禁、消融实验 |

## 13. 已知限制

- **检索结论只在 STARD 的法律咨询场景成立**，换语料需重测；受控子集只覆盖 8,000 条候选。
- **执行沙箱不隔离文件系统与网络**：`subprocess` 档挡住资源与环境污染，但进程仍可读宿主可读的文件、仍可出网。
- **`in_process` / `thread` 两档不是隔离边界**：同进程、同 GIL、同环境变量。
- **评测门禁未武装**：仓库内的题集是公开 MCQ 集，旧的人工金标已下线，门禁代码保留但不阻塞合入。
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
