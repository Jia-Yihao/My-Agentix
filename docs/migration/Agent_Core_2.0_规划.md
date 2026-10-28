# My-Agentix · Agent Core 2.0 迁移规划（v1.1）

> **文档状态**：历史 · 这是**当时**的规划或快照，**不代表当前实现**，不要按它判断现状。
> 文档分层：唯一现行总览是根 [README](../../README.md)，实现细节见 [PROJECT_SPEC](../项目实现手册.md)；在办的优化见 [优化路线图台账](../design/性能与优化路线图.md)。

- 版本：v1.1（在 v1.0 基础上按《Agent 全书笔记》10 章 + 附录对照修订）
- 日期：2026-09-23
- 模式：Strangler Migration + 接口先行 + 测试保护
- 基线：**2463 passed, 14 skipped, 0 failed**（`pytest -m "not integration"`，2026-09-28 审计批次 A（会话/上传归属校验 + 限流口径收口，见 `架构审计-2026-09-28.md` §1）+ Phase 28 自主护栏 + Phase 31 评估二期 + Phase 30 进化闭环 + Phase 29 记忆结构化（`profile_card` JSON Cards + `derive_profile` 派生幂等，见 `Phase28-31_优化规划.md` §4）后实测；规划起点 1814；skip 含 10 条 Redis Stream 契约用例无 Redis 时诚实跳过）；`ruff` / `mypy` / `vue-tsc` 全绿〔历史读数，现值只认根 `README.md` §11「测试真源」；本文件不重复抄数（纪律 7）〕
- 范围决策：**先 P0 = Phase 0–5**，P1 = 6–9，P2 = 10–14，分批验收

> **终态注记（2026-10-04）**：本文为 v1.1 蓝图存档（Phase 0–14 期规划）。行文中 `infrastructure/tools/**`、`mcp/*`、`capabilities/tools/**`、`application/runtime/**`、`domain/ports/agent_core.py` 等路径已随统一执行链 A4/M1/M3/M6 删除或迁移，「现状落点」列不代表当前树——现状以 `docs/migration/现有目录树.md` 与 `docs/migration/架构重构_统一执行链方案_v2.md` §5 终态对照矩阵为准；测试基线以 README §11 唯一真源为准（L6 的 2463 为 2026-09-28 历史数字）。

---

## 0. 总纲：Agent = Model + Harness

Agent Core 2.0 的定位是 **Harness**（脚手架/执行环境），不是又一个业务 Agent。

| Harness 职责（书 §1.7） | Agent Core 组件 | 现状落点 |
|-------------------------|-----------------|----------|
| Context（管上下文） | Context Manager + Memory | `retrieval/domain/services/context_engine.py`（底座）、`application/services/*memory*`（半成品） |
| Tool（管工具） | Tool Selector / Executor / Capability Registry | `infrastructure/tools/*`、`mcp/*` |
| Constrain（管约束） | Policy + Budget + Risk Level | `application/runtime/budget.py`、`domain/policies/*`、`infrastructure/tools/definitions.py` |
| Verify（管验证） | Verifier（分层） | `retrieval/domain/services/grounding_verify.py`、`kg.../validate_final_answer/` |
| Correct（管纠偏） | Replanner + HITL + Failure Recovery | `application/services/breakpoint_resume.py`、`infrastructure/tools/human_review.py`、`domain/policies/error_classification.py` |

**演进纪律（书 §1.9）**：保留 L1（路由）/ L2（固定工作流）/ L3（开放 Agent）三级；Agent Core 只接管**开放任务分支**；单次 LLM → Workflow → Autonomous 递进选用，不为用 Workflow 解的问题引入自主循环。

**工程总则**：

- 不破坏现有全量基线 / README 守卫（`tests/test_p41_doc_consistency.py`）/ 架构零豁免（`tests/test_p17_domain_layer.py`）。
- 不新增数据库、不引入新框架；LangGraph 仅作 orchestration，放 `agentix/agent/graph/`。
- Memory ≠ Checkpoint：记忆走 Agent Core，断点走 Runtime checkpoint。
- 每阶段 DoD：`pytest -m "not integration"` + `ruff check` + `mypy` + 架构守卫全绿。

---

## 1. 设计原则（五模式横切，书 §1.11 / 附录 B）

| 模式 | 含义 | 落地约束 |
|------|------|----------|
| Proposer–Reviewer | 提案与审查分离 | Verifier 不得读 Proposer 自述；HITL Reviewer 用独立上下文只看结构化参数与结果 |
| Progressive Disclosure | 逐层披露 | 工具/Skills/记忆按需注入，不一次性塞满 |
| Append-only | 轨迹只追加 | Context 轨迹与 Trajectory 日志不可原地改写；裁剪靠“可重建”判据而非改历史 |
| Boundary + Retention Set | 边界用例 + 保留集回归 | 每阶段金标边界用例进 `evals/`；Retention Set 防前缀回归 |
| Minimal Diff + Rollback | 最小差异可回滚 | Strangler：旧实现 → Adapter → 新 Core → 验证 → 删旧；任一 Phase 可独立回滚 |

---

## 2. 目标架构

```text
Application  (Chat / Research / Knowledge / Data Agent)
     │
Task Manager  (Create / Resume / Cancel / Retry / Schedule / Approval)
     │
┌────▼─────────────────────────────────────────────┐
│  AGENT CORE  = Harness 五职责                     │
│  ContextMgr │ Planner │ ToolSelector │ Executor   │
│  Observer   │ Verifier│ Replanner    │ Policy     │
│  Memory (Working/Episodic/Semantic/Procedural/User)│
│  graph/ ← LangGraph 仅 orchestration              │
└────┬──────────────┬──────────────┬────────────────┘
     │              │              │
 Memory Layer  Capability    Knowledge
 (scope+format) (tools/mcp)  (vector/kg/sql)
     │              │              │
┌────▼──────────────▼──────────────▼────────────────┐
│  Agent Runtime  (现 application/runtime → agentix/runtime) │
│  Run/State/Step/Event/Budget/Checkpoint/Cancel/Control     │
│  新状态: WAITING_APPROVAL / PAUSED / WAITING_EXTERNAL_EVENT │
└────┬──────────────────────────────────────────────┘
     │
 Platform  (LLM Gateway / Auth / Config / Obs / Eval / CI)
```

### 2.1 目标目录（新建，当前均不存在）

```text
agentix/
  agent/                 # 新建（P0 Phase 2 起）
    core/                # 循环骨架 Task→Context→Plan→Execute→Observe→Verify→Final
    planner/             # Plan / TaskSpec
    executor/            # 工具执行编排（调 capability 接口）
    observer/            # 观测/轨迹写入（统一 trajectory）
    verifier/            # 分层 Verifier（L1 结构 / L2 规则 / L3 grounding）
    replanner/           # 四层故障后的再规划
    context/             # ContextMgr：budget/compression/稳定前缀/状态栏
    memory/              # 五类记忆 + 两 scope + 读取时裁决 + PII 脱敏
    policy/              # 运行时策略（风险、预算、取消）
    graph/               # LangGraph 仅在此（可替换）
  runtime/               # 物理迁移在 Phase 14，P0 期间由 application/runtime 原地 + Protocol 桥接
  capabilities/          # Phase 6 起：tools 汇聚、mcp/ 迁入、能力检索
```

### 2.2 分层守卫增量（进 `test_p17` + `docs/architecture/分层架构与依赖规则.md`）

在现有规则（domain 零第三方、domain 不依赖 infrastructure/interfaces、application 依赖 domain.ports）之上：

- `agent/` → 可依赖 `domain`、`capabilities` **接口**、`runtime` **接口**；除 `agent/graph/` 外禁依赖 LangGraph。
- `runtime/` → 禁依赖 planner 实现、业务 agent、UI、HTTP。
- `domain/` → 维持零例外（LangGraph / Redis / Milvus / Neo4j 等均在 `FORBIDDEN_THIRD_PARTY`）。
- Framework must remain replaceable：`GraphProviderPort`（已有）继续作为唯一图后端接缝。

---

## 3. 书章节 → 相对 v1.0 的 12 处修订

| # | 书章节 | v1.0 缺口 | v1.1 修订（并入对应 Phase） |
|---|--------|-----------|---------------------------|
| 1 | §1.7 | 未把 Agent Core 定性为 Harness | §0 总纲五职责对照表 |
| 2 | §1.11 / 附录 B | 五设计模式未横切 | §1 设计原则；各 DoD 引用 |
| 3 | §1.9 | 演进递进 | L1/L2/L3 保留；Agent Core 只接管开放任务 |
| 4 | §2.4 / §2.7 / §2.9 | Context 缺稳定前缀 | `Context = Static Prefix + Trajectory`；Append-only；状态栏**状态不得模型自写**；可重建可裁剪；隔离优于压缩（Phase 5） |
| 5 | §3.1–3.5 | Memory 缺正交框架 | 两 scope × 三维度；四存储格式；**Procedural**；冲突**读取时裁决**；PII 脱敏；LoCoMo 指标（Phase 4） |
| 6 | §4.3–4.7 | Tool 只有 ranking | ACI；**负面描述（何时不能用）**；**参数保真（禁静默改参）**；能力检索三层；MCP 投毒/shadowing（Phase 6） |
| 7 | §4.10 | HITL 未强调上下文隔离 | Reviewer **独立于被审 Agent 上下文**（Sidecar）（Phase 8） |
| 8 | §5.7 | 失败测试未分层 | 四层故障（API/工具/上下文/控制流）；错误处理不得再无限调 LLM（Phase 3） |
| 9 | §6.2 | Long-running 缺事件模型 | Event Adapter；事件去重/超时/重试；双时刻事件工具；虚拟身份边界（Phase 9） |
| 10 | §7.3 / §7.8 / §7.10 | 评估缺归因与统计 | **首错步归因**；最终文本≠唯一事实源；Pass@k vs Pass^k；1–2pp 不显著；成本全式（Phase 10） |
| 11 | §9.6–9.9 | 持续进化只有指标扩展 | **双循环**；三道可信根防线；四类进化载体（Phase 12） |
| 12 | §10.1–10.17 | Multi-Agent 只有 Supervisor | 信息增量判据；共享/隔离两轴；先 Peer+Manager；六失败模式；独立预算（Phase 11） |

---

## 4. Phase 清单

### P0（本批实施）

#### Phase 0 — Inventory（只读产物）
产出三件套（已执行落盘）：
- `docs/migration/现有目录树.md` — 目录树与新建缺口
- `docs/migration/依赖关系图.md` — import 依赖与违规风险点
- `docs/migration/模块清单.md` — 现有模块 → 新包映射

**DoD**：三文件存在且与代码一致；不改任何 `.py`。

#### Phase 1 — Interface Freeze
- 扩展 `agentix/domain/`（或新建 agentix/agent/core/types.py 若 domain 语义冲突）：`TaskSpec`（已有雏形 `application/services/task_plan.py`）、`Plan`、`Observation`、`VerificationResult`、`MemoryItem`、`ContextSnapshot`。
- 七个 Protocol：`PlannerPort`、`VerifierPort`、`ToolSelectorPort`、`MemoryPort`、`ContextPort`、`EventPort`、`RollbackPort`（落 `domain/ports/agent_core.py` 等，零第三方）。
- **不动** `application/runtime/` 实现；加 Adapter 桥接。
- `docs/architecture/接口与契约.md` 补 Harness 五职责对照。

**DoD**：新类型/Protocol 有单测；全量仍绿；domain 零第三方守卫仍绿。

#### Phase 2 — Agent Core Loop
- 新建 `agentix/agent/`：`core/ planner/ executor/ observer/ verifier/ context/ policy/ graph/`。
- 主循环：`Task → Context → Plan → Execute → Observe → Verify → Final`。
- Fake Tool / Fake Planner / Fake Verifier 单测覆盖 happy path、预算耗尽、验证失败退出。
- Graph 节点**只调 service**，不写业务。

**DoD**：Agent Core 单测 ≥ 约定集合；全量回归绿；LangGraph import 仅允许出现在 `agent/graph/`（新增守卫可选，先文档约束）。

#### Phase 3 — Replanning + 四层故障
- `max_replans=3` + `same_failure_count`（连续同因失败熔断）。
- 故障矩阵（书 §5.7）：API / 工具 / 上下文 / 控制流 各至少 1 注入用例。
- 断言：never hang、never duplicate side effect、always terminal。
- **错误处理路径禁止再无限调 LLM**（有限重试 + 降级）。

**DoD**：四层故障测试全过；原有 reliability 测试不回归。

#### Phase 4 — Memory 完整化
- 五类：Working / Episodic / Semantic / **Procedural** / User。
- 两 scope：user memory vs knowledge base；三维度：在哪 / 怎么存 / 存什么。
- 冲突：**读取时裁决（read-time resolution）** 为默认；写入只做去重与合并提示。
- PII 脱敏在写入路径；普通日志不含 PII。
- **适配不重写**：`long_term_memory.py`、`semantic_memory.py`、`memory_consolidation.py`、`redis_cache.py` → 经 `MemoryPort`。
- LoCoMo 风格维度进 evals 指标说明。

**DoD**：scope 隔离测试；冲突裁决测试；PII 测试；全量仍绿。

#### Phase 5 — Context Engineering
- budget + compression + tool schema filter + memory selection。
- **Static Prefix + Trajectory** 结构；稳定前缀不变性测试（同任务两次请求前缀哈希一致，或文档化可变字段白名单）。
- **状态栏（status bar）**：复用 `context_engine.build_status_bar`；状态来源白名单，**状态不得由模型自写**。
- 可重建信息可裁剪；**隔离优于压缩**（大对象走 artifact 通道，已有 artifact_store / 大文档隔离）。

**DoD**：超预算 deterministic；状态栏来源校验；稳定前缀测试；全量绿。

### P1（P0 验收后）

| Phase | 核心增量 | 现状复用 | DoD 要点 |
|-------|----------|----------|----------|
| 6 Tool Intelligence | ACI、负面描述、参数保真、MCP 投毒审查、能力检索三层；**双风险词表收敛**（工具 L0–L3 与 `RiskLevel` 映射已有 `human_review.py` 单处映射，保持唯一） | `infrastructure/tools/*`、`mcp/*` → `capabilities/` | 参数零静默变换；描述含「何时不能用」；MCP 描述当不可信输入 |
| 7 Agentic RAG | KnowledgeRouter + EvidenceFusion + GroundingVerifier | `grounding_verify.py`、`kg.../text2cypher|text2sql|validate_final_answer` | 分层 Verifier；纯函数 L1/L3 可测 |
| 8 HITL | `WAITING_APPROVAL` + Approve/Reject/Edit/Resume；**Reviewer 独立上下文**；进程重启持久化 | `human_review.py`、`approval_service`、`breakpoint_resume.py`、`approvals` 路由 | Sidecar 隔离测试；重启恢复测试 |
| 9 Long-running | Event Adapter、双时刻事件工具、虚拟身份与凭证边界 | `breakpoint_resume.py`、checkpointer | 事件去重/超时/重试；PAUSED/WAITING_EXTERNAL_EVENT |

### P2

| Phase | 核心增量 | 收敛动作 |
|-------|----------|----------|
| 10 Evaluation 2.0 | 6 类任务基准、A–G 消融（`evals/ablations/variants.json` 为唯一事实源；规划原文 A–H 的 H 无定义，按 A–G 落地）、**首错步归因**、统计显著性、成本全式 | **双 trajectory 收敛**（`observability/trajectory.py` vs `application/services/trajectory.py`：evals 依赖后者，运行时旁路用前者 → 统一以 services 版为真源，observability 收薄壳或合并） |
| 11 Multi-Agent | 先 Peer（Proposer–Reviewer）+ Manager；信息增量判据；六失败模式；独立预算/深度/取消 | 不做 Decentralized 首期 |
| 12 Continuous Evolution | 双循环（在线只记轨迹；离线聚合→提案→回归→灰度）；Boundary+Retention；三道防线（证据≠指令、staging、验证器不可自改） | 在线循环零写能力 |
| 13 Docs/README | 五层架构说明、Research Agent Demo、面试 10 问 | 不破坏 README 数字守卫 |
| 14 物理迁移 | `application/runtime/` → `agentix/runtime/`；删 Adapter | 单独 Phase，import 扰动集中处理；全量回归后删旧路径 |

---

## 5. 旧路径 → 新包映射（摘要，详见 模块清单.md）

| 现有路径 | 目标 | Phase |
|----------|------|-------|
| `application/runtime/{state,steps,budget,lifecycle,runtime}.py` | 原地 + Protocol 桥接 → Phase 14 `agentix/runtime/` | 1 桥 / 14 搬 |
| `application/services/task_plan.py` (`TaskSpec`) | `agent/core` 类型对齐，Phase 1 出 Domain 类型 | 1 |
| `application/services/{long_term,semantic,memory_consolidation,redis_cache}` | `agent/memory` 适配 `MemoryPort` | 4 |
| `retrieval/domain/services/context_engine.py` | `agent/context` 底座（可先 import 再抽） | 5 |
| `retrieval/domain/services/grounding_verify.py` | `agent/verifier` L3 层 | 7 |
| `infrastructure/tools/*`、`mcp/*` | `capabilities/` | 6 |
| `infrastructure/tools/human_review.py` + `breakpoint_resume.py` + `approval_service` | HITL + `WAITING_APPROVAL` | 8 |
| `application/agents/lg_builder.py` 等散落 LangGraph | `agent/graph/` 或保持 L2 工作流不动（非 P0） | 2+ |
| `observability/trajectory.py` vs `application/services/trajectory.py` | 收敛为一（services 为真源） | 10 |

**明确不存在、需新建**：`agentix/agent/`、`agentix/runtime/`、`agentix/capabilities/`。

---

## 6. 组件级 DoD 增补

| 组件 | 验收 |
|------|------|
| Planner | DAG cycle 检测；budget-aware；负面 capability 描述测试（Phase 6 后） |
| Verifier | Proposer–Reviewer：只看产物/证据/结构化参数，不看 Proposer 自述 |
| Memory | user/task scope 隔离；读取时冲突裁决；PII 不进普通日志 |
| Context | 超预算 deterministic；状态栏来源校验；稳定前缀不变性 |
| Tool | 参数零静默变换；描述含「何时不能用」；MCP 描述当不可信输入 |
| Failure | 四层故障全覆盖；never hang / never duplicate side effect / always terminal |
| Eval | 首错步归因；Retention Set 回归；统计显著性门槛 |
| Evolution | 在线循环零写能力；验证器不可被进化流程修改 |
| Multi-Agent | 信息增量断言；六失败模式各有测试 |

---

## 7. 迁移纪律（不可倒置的顺序前提）

1. 旧实现 → Adapter → 新 Interface → 新 Core → 验证 → 删旧路径（Minimal Diff + Rollback）。
2. 每阶段全量测试绿才允许进入下一 Phase；P0 内 Phase 0→5 顺序固定；P1/P2 在前一批验收后开始。
3. 不破坏 README/架构/依赖一致性守卫；新包遵守分层与零豁免。
4. 双实现（trajectory、风险词表）**不提前**在 Phase 0 强行合并——分别在 Phase 10 / Phase 6 处理。
5. Runtime **不**在 Phase 1 物理搬迁；最后 Phase 14 集中迁。

---

## 8. 附：原规划保留的优先级锚点

- **P0 必选**：Agent Core、Planner、Verifier、Replanner、Context、Memory、Runtime boundary。
- **P1**：Tool Intelligence、Agentic RAG、HITL、Long-running。
- **P2**：Evaluation 2.0、Multi-Agent、Continuous Evolution、Docs、物理迁移。

---

## 9. 执行状态

> 2026-09-24 收口：Phase 0–14 全部完成；勾选真源见 `模块清单.md` §6。
> 深化规划 Phase 15–20 见 `Phase15-20_深化规划.md`（Harness 点亮 / HITL 统一 / 评估武装 / 上下文分层 / multi_agent / 进化点亮）。
> 下一版优化迭代 Phase 21–26 见 `Phase21-26_规划.md`（2026-09-27 立项：Skills+Hooks / 上下文二期 / 交互二期 / 记忆知识 / multi-agent 防线 / 评估三期+解阻）。
> 当前优化迭代 Phase 28–31 见 `Phase28-31_优化规划.md`（2026-09-28 立项：自主护栏 / 记忆结构化 / 进化闭环 / 评估二期；审计批次 A 前置、B/C 穿插）。

| Phase | 状态 |
|-------|------|
| 0 Inventory | **完成** — 四文档落盘（规划 + tree + dependency + inventory） |
| 1 Interface Freeze | **完成** — Protocol + 类型 + 接口与契约.md；守卫 p17/p40/p41 全绿 |
| 2–5 | **完成** — Agent Core 循环 / 四层故障 / Memory / Context |
| 6–9 | **完成** — Tool Intelligence / Verifier / HITL / Long-running |
| 10–12 | **完成** — Evaluation 2.0 / Multi-Agent / Continuous Evolution |
| 13–14 | **完成** — Docs + Runtime 物理迁移（`agentix/runtime/**` 真源） |
| 最终审查 + README 深化 | **完成**（2026-09-24）— 全量 1982 passed / 4 skipped（历史读数，现值见根 `README.md` §11） |
| 15–20 深化 | **完成**（除 Phase 17 全栈 **blocked**）— 规划见 `Phase15-20_深化规划.md`；Phase 15/16/18/19/20 + 17 离线部分完成 + 接口审查修复与收尾 |
| 21 Skills + Hooks | **完成**（2026-09-27）— 批一启动项，规划见 `Phase21-26_规划.md`；阶段终值 2164 passed / 4 skipped（历史读数，现值见根 `README.md` §11） |
| 22 上下文二期 | **完成**（2026-09-27）— 批一，规划见 `Phase21-26_规划.md`；缓存纪律（工具序冻结 / 配置变更 `system_notice` append）+ L4 auto-compact（`CONTEXT_AUTOCOMPACT` 默认关，五要素机制断言）；阶段终值 2183 passed / 4 skipped（历史读数，现值见根 `README.md` §11） |
| 23 交互二期 | **完成**（2026-09-27）— 批二启动项，规划见 `Phase21-26_规划.md`；`notify_user` 出站通知（`NOTIFY_USER_ENABLED` 默认关）+ `EVENT_PORT_BACKEND=redis_stream` 第三实现 + `GET /ops/events` + 事件矩阵；现 2220 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 24 记忆/知识深化 | **完成**（2026-09-27）— 批二，规划见 `Phase21-26_规划.md`；淘汰归档红线（摘要保留 + 证据可回溯）+ 重要性三因素纯函数 + `memory_consolidation` handler（幂等时间窗）+ Agentic RAG 空检索改写重试 ≤2（`MEMORY_CONSOLIDATION_ENABLED` / `AGENTIC_RAG_ENABLED` 默认关）；阶段终值 2245 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 25 multi-agent 运行时防线 | **完成**（2026-09-27）— 批三，规划见 `Phase21-26_规划.md`；ControlPlane 并发/深度准入 fail-closed + 进程内 `InProcessBus`（flag `MULTIAGENT_BUS_ENABLED` 默认关）+ Manager 冲突/级联只标记 meta + `lg_supervisor` 接线 + echo_overlap 修复（六模式防线）；阶段终值 2271 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 26 评估三期 | **完成**（2026-09-27）— 批三，规划见 `Phase21-26_规划.md`；judge 去 stub（真实端点出分 + `judge_model` 标注 + 无端点/不可解析诚实失败，默认关不进门禁）+ `--compare` 配对 percentile bootstrap 显著性（纯 stdlib 可复现）+ `evals/README.md` 校准四步流程；阶段终值 2290 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 27 死代码清理 + 文档收尾 | **完成**（2026-09-27）— 批外收口，见 `架构演进评估.md` §4/§8；`search_service`/`search_prompts`/空包 `application/prompts/` 整体删除（零调用两步流程 + 防复活守卫）+ Phase15-20 陈旧勾选同步；2291 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 接口审查批次（非 Phase） | **完成**（2026-09-28）— 前后端同步审查：流式助手落库 / 历史最近 N 条窗口 + limit 下界 / SSE 终态解析器 / 会话切换竞态守卫 / `serverResponded` 不回退等 30+ 修复；`test_chat_api.py` 零断言脚本重写为 12 条离线契约测试（integration 标记清零）；现 2303 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |
| 优化迭代 28–31 | **完成**（2026-09-28）— 规划见 `Phase28-31_优化规划.md` §8；批一 = 审计批次 A（IDOR 归属 / 上传读鉴权 / 限流口径）+ Phase 28 自主护栏 ∥ Phase 31 评估二期；批二 = Phase 30 进化闭环；批三 = Phase 29 记忆结构化；穿插批次 B（B1/B3/B4/B2）+ 批次 C（C1/C2/C3）；现 2463 passed / 14 skipped（历史读数，现值见根 `README.md` §11） |

每完成一 Phase，更新本表与 `模块清单.md` §6，并跑全量守卫命令：

```powershell
& ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests
& ".\.venv\python.exe" -m mypy agentix/domain --config-file=pyproject.toml
# web 前端（触及前端时）
cd web; npx vue-tsc --noEmit
```
