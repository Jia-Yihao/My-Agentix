# Phase 32–34 · Memory / RAG / Multi-Agent 深度优化总纲（v1.1）

> **文档状态**：历史 · 这是**当时**的规划或快照，**不代表当前实现**，不要按它判断现状。
> 文档分层：唯一现行总览是根 [README](../../README.md)，实现细节见 [PROJECT_SPEC](../项目实现手册.md)；在办的优化见 [优化路线图台账](../design/性能与优化路线图.md)。

- 版本：v1.1（2026-10-01）；v1.0 → v1.1：上位约束落到正式文档，守卫清单并入唯一真源，执行路线并入 v2 六段式
- 前置：Phase 0–31 收口 + 优化批次 P1–P6 完成（勾选真源见 `docs/migration/模块清单.md`）
- 上位约束：`docs/migration/架构重构_统一执行链方案_v2.md`（唯一执行蓝图）；本纲及其三分文档为其下位分册
- 模式：Strangler 不变；flag 默认关、flag-off 字节等价；domain 零第三方；LangGraph 仅 `agentix/agent/graph/`；不新增数据库 / 新框架 / 新中间件
- 分文档：记忆详见 `docs/design/记忆域深度优化.md`；检索详见 `docs/design/检索增强深度优化.md`；多智能体详见 `docs/design/多智能体深度优化.md`
- 对标基线：OpenAI Codex harness（AGENTS.md 级联 + Guardian + 沙箱）、Claude Code（7 法：CLAUDE.md / rules / skills / subagents / hooks / output styles / prompt append）、Hermes Agent（ContextEngine / MemoryManager / delegate 隔离 / cron 分半，研读见 `docs/源码研读笔记.md`）、智谱系 Agent（企业知识挂载 + 工具分级审批 + 多智能体分工）、生产 RAG SOTA（Dense + Sparse → RRF → Cross-encoder）

---

## 0. 为什么先收敛、再深优

重构案已判定：当前最大风险不是“能力不够”，而是“控制流双重真源”（LangGraph 三层 Multi-Agent 与 Agent Core 主循环并列、状态多处、工具双入口、SSE 双轨）。

因此三方向深优统一服从一条铁律（分文档 P0 均为收敛项）：

1. 一个 Run 只有一个 Orchestrator；
2. 一个 Run 只有一个 Runtime 真源（`agentix/runtime/`）；
3. 一个 Tool 只有一个执行入口（Tool Gateway：Permission → Risk → Timeout → Retry → Execute → Trace）；
4. SSE / Run API / 轨迹 / 评测消费同一条 Run/Event 流；
5. Memory 是 Context Provider（不拥有控制流）；HITL 是 Policy Gate（只暂停 Run）；Evolution 只离线（在线只采集）。

深优分期：**P0 收敛（去重/统一入口/强制租户与取消语义）→ P1 接线（灰度打开已在库能力）→ P2 增强（新增召回/规划/记忆智能）**。P1/P2 全部 flag 门控，默认关时与现基线字节等价。

---

## 1. 三方向目标架构（一句话版）

用户请求进入应用层唯一入口后创建一个 Run；Orchestrator 驱动唯一的 Plan → Execute → Observe → Verify 循环；Runtime 负责状态、预算和生命周期；所有工具（含记忆读写、检索、图谱、SQL、MCP）统一经过 Tool Gateway；RAG / Graph / SQL / Search / MCP 都是可插拔 Capability；LangGraph 只是编排适配实现；记忆只读写上下文，HITL 只做门控，多智能体只是 Orchestrator 的一种 Strategy。

```
API / SSE / Run API（仅协议转换）
→ 应用层 AgentService / RunService（唯一入口，创建 Run）
→ Orchestrator（唯一控制流：Context → Plan → Execute → Observe → Verify → Replan/Finish）
→ Runtime（唯一状态 / Step / Budget / Lifecycle / Cancellation）
→ Tool Gateway（唯一工具入口：权限 / 风险 / 超时 / 重试 / HITL / 审计）
→ Capabilities（Knowledge / Graph / SQL / Web / File / MCP / Memory-tools）
→ Infrastructure（Milvus / Neo4j / Redis / LLM / 外部服务）
```

LangGraph 定位：Orchestration Adapter（状态图/条件边/并行节点/恢复点机制），不定义任务语义；业务代码不得绕过 Orchestrator 直接建图执行工具。

---

## 2. 三方向依赖矩阵

| 方向 | 新定位 | 依赖上游 | 被谁依赖 | 与另两方向的关系 |
|---|---|---|---|---|
| Memory | Context Provider + Writer | Runtime（budget）、Tool Gateway（读写工具化后） | Orchestrator 的 Context 阶段、压缩器 | 只给 RAG 提供个性化改写信号（如用户偏好）；不直接指挥检索；多智能体子代理继承父记忆快照只读拷贝 |
| RAG | Capability + Tool（search / retrieve / rerank / evidence） | Tool Gateway、Runtime（取消/预算）、记忆（偏好） | Planner（选能力）、Executor、Verifier（引用校验） | 不拥有 Agent loop；多智能体场景下作为共享检索工具被各角色调用，结果归黑板统一融合 |
| Multi-Agent | Orchestrator 的 Strategy 之一 | Runtime、Tool Gateway、Memory（快照）、RAG（共享工具） | 无（顶层策略） | Manager/Worker 复用单智能体同一套 Planner/Verifier/预算/HITL；子代理退出契约统一 |

---

## 3. Phase 路线总览

| Phase | 主题 | 批次 | 核心交付 | 状态 |
|---|---|---|---|---|
| 32 | Memory 深优 | P0 收敛 → P1 接线 → P2 智能 | 五层统一模型 + 读写工具化 + AGENTS 级联 + 向量召回 + 后台评审 | ✅ 全部完成（P0=B1/p67，P1=F1/p77+p78，P2=F2a/p82；2026-10-04 收口） |
| 33 | RAG 深优 | P0 收敛 → P1 真 hybrid → P2 查询智能 | Capability 化 + 去重（LightRAG 双实现/双集合）+ Dense+Sparse RRF + Rerank 默认开 + 改写/HyDE | ✅ 全部完成（P0=B2/p68，P1=F1/p76，P2=F2b/p83–p87 含归因报告；2026-10-04 收口） |
| 34 | Multi-Agent 深优 | P0 单流 → P1 规划并发 → P2 协作 | 退出契约 + AgentLoop 灰度 + LLM 规划 + 并发工具轮 + HITL 中途拦截 | ✅ 全部落地（P0=Stage A1–A3/p61–p63，P1=F1/p79–p81，P2=F2c/p88+p89；A-P2-3 按 D5 另期；2026-10-04 收口） |

执行顺序（以 v2 §4 为准）：34-P0 = Stage A 行为收敛（A1–A6）→ 32-P0 = B1、33-P0 = B2 → Stage M 结构大迁移 → 三方向 P1/P2 = Stage F（功能增量，迁移完成后做，见 v2 决策 D3）。已完成项核对（2026-10-01 代码级）：34 的 A-P1-1 任务型路由、A-P1-5 自愈三计数器、A-P2-4 计划模式与 32 的 M-P2-1 /learn、M-P2-3 /context 已在优化批次落地，不再重复立项。cron / proactive（Hermes 式调度器）明确不在 32–34 范围内（v2 决策 D5，只留口）。终态（2026-10-04）：三方向 P0/P1/P2 随 Stage A/B/F 全部落地（唯 A-P2-3 按 D5 另期），验收用例 p61–p89 逐批落位。

---

## 4. 统一契约（冻结口，不再各自发明）

- Run / Step / Budget / Lifecycle：以 `agentix/runtime/` 为唯一真源；LangGraph checkpoint 只做编排恢复，不做业务状态读取源。
- Plan / Action / Observation / Verification：以 agentix/domain/agent_types.py（M6 已删，现见 `agentix/domain/agent/types.py`） 与 agentix/domain/ports/agent_core.py（M6 已删，现见 `agentix/domain/agent/ports.py`） 为准；分文档不得新增并列模型，只做字段只增。
- ToolDescriptor / Risk(L0–L3) / Permission：以 agentix/capabilities/tools/definitions.py（M6 已删，现见 `agentix/tools/definitions.py`） 与 agentix/capabilities/tools/executor.py（M6 已删，现见 `agentix/tools/gateway/executor.py`） 为准；记忆工具、检索工具、子代理派发全部注册为 ToolSpec。
- Event / Trace：轨迹落盘 `agentix/observability/trajectory.py` + EventPort（memory / jsonl / redis_stream 三后端一致语义）；SSE 只是 Run Event Stream 的投影。

---

## 5. 架构守卫（10 条，全部要求测试化，见各分文档 §验收）

> v1.1 注：唯一守卫清单为 v2 方案 §6 的 **12 条**（本纲 10 条为其子集，另两条为 LangGraph 导入白名单棘轮、shim 目录断言）。本节保留作分文档引用锚点，两处清单以 v2 §6 为准。

1. 同一 run_id 不允许同时存在两个 Orchestrator owner。
2. 所有 ToolCall（含记忆读写、检索、MCP）必须经过 Tool Gateway。
3. Capability 不得 import Orchestrator / Planner（分层单向）。
4. LangGraph adapter 不得直接写业务数据库状态；checkpoint 不得成为 Runtime 状态读取的唯一来源。
5. SSE event 必须来自统一 Run/Event stream（禁止 Chat SSE 与 Run API 各自拼事件）。
6. 应用层不得直接实例化 Neo4j / Milvus / MCP client（必须经 Gateway / Capability）。
7. 在线请求不得调用 Evolution promotion（只采集轨迹/反馈）。
8. 取消 Run 后必须最终进入 CANCELLED 或明确终态（取消真正打断 asyncio Task）。
9. Replan 次数受 Runtime Budget 控制；budget_exceeded 绝不悬挂。
10. 高风险（L1+）动作无 approver 时 fail-closed（HITL Sidecar 语义，见 agentix/agent/hitl/sidecar.py（M6 已删，现见 `agentix/policies/hitl/sidecar.py`））。

---

## 6. 评测与门禁（增量、不打破现有 CI）

- 沿用：102 条分层 golden（`evals/legal_golden.jsonl`）〔该集已于 2026-10-26 随评测集下线删除，"沿用"这一现在时不成立；102 条为历史读数〕+ 确定性指标 + 质量门禁三态 + 轨迹前缀回归（`evals/trajectory_regression.py`）+ 消融 A–G。
- 新增（只增报告键，不进门禁，沿 Phase 31 纪律）：
  - Memory：画像稳定性（同一用户多轮偏好一致率）、冲突标记准确率、PII 零留存抽查；
  - RAG：Recall@5 / MRR 分阶段归因（dense / sparse / RRF / rerank 四列，见分文档）；
  - Multi-Agent：任务完成率、exit_reason 分布、预算耗尽率、取消终态到达率。
- 文档守卫：新增文档中以反引号引用的 `agentix/...`、`tests/...`、`evals/...`、`scripts/...`、`kb_ingest/...`、`web/...` 路径必须真实存在（`tests/test_p41_doc_consistency.py` 全量扫描 docs/）；**规划新建文件一律不加反引号**（写 plain 文本 + “（规划新建）”标注，或用 `agentix/.../` 缩写豁免）。

---

## 7. 风险与回滚

| 风险 | 对策 |
|---|---|
| 检索链路大改导致 golden 回归抖动 | RRF/rerank/改写全 flag 门控；P1 先影子跑（shadow）只打点不生效，出报告后再切流 |
| 记忆工具化带来提示注入/PII 面 | 读写双向脱敏 + 引用隔离（超长进 artifact）；默认关；审计链必经 Tool Gateway |
| AgentLoop 灰度导致问答退化 | 问答路由保持 L2 图；任务型路由独立灰度；退出契约保证可 killed / 可恢复 |
| 文档腐烂（路径/配置键漂移） | p41 守卫 + 本纲 §6 写作纪律；配置键新增只在 `docs/architecture/配置管理.md` 落表（受守卫校验存在性） |

---

## 8. DoD（本纲完成标准）

- [x] 三分文档全部落盘且互相引用一致（总纲↔分文档章节号对齐）；（2026-10-04 复核）
- [x] 每分文档含“现状实测（文件行号级）→ 对标 → 目标 → P0/P1/P2 任务 → 旗标表 → 验收测试”完整链条；（F1/F2 期间状态行与 §6 注记随批更新）
- [x] `tests/test_p41_doc_consistency.py` 相关用例通过；
- [x] `docs/migration/模块清单.md` 与 README §14 文档表同步更新（2026-10-04 Stage T 收口：统一执行链 Stage A/B/M/F/T 全勾选，终态收口行入册）。
