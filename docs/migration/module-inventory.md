# Phase 0 · module-inventory.md — 现有模块 → Agent Core 2.0 映射

- 生成时间：2026-09-23
- 规划：`docs/migration/Agent_Core_2.0_规划.md` v1.1
- 状态图例：`reuse` 复用 · `adapt` 适配 · `move` 物理迁移 · `new` 新建 · `defer` 延后 · `merge` 收敛双实现
- **存档语义**：§1 各表与 §5 文件清单为 Phase 0/14 期计划快照（「现有模块/源路径」列记录当时路径）；终态下 `application/runtime/**`、`infrastructure/tools/**`、`capabilities/tools/**`、`agentix/mcp/**`、`agent/planner|replanner|verifier|hitl|executor|core|multiagent/`、`domain/agent_types.py`、`domain/ports/agent_core.py` 等已随 A4/M3/M6 删除（见 §6 终态收口段与 `current-tree.md` §8）；现行模块清单以 `current-tree.md` 与本文件 §2 速查表为准

## 1. 总表

### 1.1 目标组件 ← 现有模块

| 目标组件（Agent Core） | 现有模块 | 关键符号 | 策略 | Phase |
|------------------------|----------|----------|------|-------|
| 类型：TaskSpec | `application/services/task_plan.py` | `TaskSpec`, `PlanValidation`, `PlanError` | adapt：抽 Domain/agent 类型，旧类变兼容别名 | 1 |
| 类型：Observation / VerificationResult 等 | —（散落在 steps/trajectory/grounding） | `ExecutionStep`, grounding report | new 定义 + 从旧字段映射 | 1 |
| `PlannerPort` | 雏形：`kg_sub_graph/planner/planner_node.py`；`services/task_plan.py` | — | new Protocol；子图 planner 不重写 | 1 |
| `VerifierPort` | `grounding_verify.py`、`validate_final_answer`、`domain/policies/completion.py` | `validate_answer_grounding` | adapt 分层接入 | 1 / 7 |
| `ToolSelectorPort` | `infrastructure/tools/selection.py` | `ToolSelectionPolicy`, `select_tool_schemas` | adapt | 1 / 6 |
| `MemoryPort` | `long_term_memory.py`, `semantic_memory.py`, `memory_consolidation.py`, `redis_cache.py` | `LongTermMemory`, `SemanticMemoryStore` | adapt | 4 |
| `ContextPort` | `retrieval/domain/services/context_engine.py` | `assemble_context`, `build_status_bar` | adapt → 上提 | 5 |
| `EventPort` | —（checkpoint 有雏形） | — | new | 9 |
| `RollbackPort` | `breakpoint_resume.py` | `CheckpointRecovery` | adapt | 9 |
| `agent/core/` 循环 | — | — | new | 2 |
| `agent/planner/` | 无通用 Plan 步进器（仅子图） | — | new | 2–3 |
| `agent/executor/`（M2 后为 shim，实体在 `agentix/orchestration/executor.py`） | `agentix/tools/gateway/executor.py`（原 `infrastructure/tools/executor.py` shim 已随 A6 死代码批删除）+ `services/agent_executors.py` | `ToolExecutor`（`AgentExecutor` 死代码 2026-09-27 已移除） | adapt 包装 | 2 |
| `agent/observer/` | `services/trajectory.py`（真源候选） | `TrajectoryRecorder`, sinks | defer 收敛 · reuse 写入 | 2 写 / 10 merge |
| `agent/verifier/` | `grounding_verify.py` 等 | — | adapt | 2 骨架 / 7 满配 |
| `agent/replanner/` | `domain/policies/retry.py`, `error_classification.py` | `is_retryable` | adapt + new 熔断 | 3 |
| `agent/context/` | `context_engine.py` + `artifact_store.py` | `assemble_context`, `InMemoryArtifactStore` | adapt | 5 |
| `agent/memory/` | 见 MemoryPort 行 | 五类 + PII | adapt + new 裁决 | 4 |
| `agent/policy/` | `domain/policies/*`, `runtime.state.PolicyContext` | `max_risk_level` | reuse | 2 |
| `agent/graph/` | —（LangGraph 散落） | — | new；旧图 defer | 2 |
| `runtime/`（最终） | `application/runtime/**` | `AgentRuntime`, `AgentState`, `Budget` | move（桥接先行） | 1 桥 / 14 迁 |
| `capabilities/tools` | `infrastructure/tools/**` | `ToolSpec`, `registry` | move | 6 |
| `capabilities/mcp` | `mcp/**` | `MCPStdioClient`, `MCPServer` | move | 6 |
| HITL 状态机 | `human_review.py` + `breakpoint_resume.py` + `approval_service` | `HumanReviewGate` 等 | adapt + `WAITING_APPROVAL` | 8 |
| Event Adapter | — | — | new | 9 |
| Eval 归因/消融 | `evals/*`, `agentix/config/ablation.py` | `compare_trajectory_prefix` | adapt + merge trajectory | 10 |
| Multi-Agent Peer/Manager | `kg.../workflows/multi_agent/**` | 现有 kb 多 Agent | defer 参考 · new 对等协议 | 11 |
| Continuous Evolution | `observability/*` 只读轨迹 | — | new 双循环 | 12 |
| README/架构文档 | `README.md`, `docs/architecture/*` | 2000 / 102 守卫 | adapt 增补 | 13 |

### 1.2 现有模块 → 归宿（反向）

| 现有路径 | 归宿 | 策略 | Phase |
|----------|------|------|-------|
| `application/runtime/state.py` | agentix/runtime/state.py | move | 14 |
| `application/runtime/steps.py` | agentix/runtime/steps.py | move | 14 |
| `application/runtime/budget.py` | agentix/runtime/budget.py | move | 14 |
| `application/runtime/lifecycle.py` | agentix/runtime/lifecycle.py | move | 14 |
| `application/runtime/runtime.py` | agentix/runtime/runtime.py | move | 14 |
| `application/services/task_plan.py` | 类型 → `agent/core`；校验留 services 或 domain.policies | split/adapt | 1 / 14 |
| `application/services/task_queue.py` `task_retry.py` `task_handlers.py` | Task Manager 面 | defer（非 Harness 核心） | — |
| `application/services/token_budget.py` | 与 `runtime/budget` 语义重叠面 | 对齐文档 · 不 P0 合并 | defer |
| `application/services/control_plane.py` | Platform/Task 层 | defer | — |
| `application/services/trajectory.py` | eval/导出真源 | merge 目标侧 | 10 |
| `observability/trajectory.py` | 运行时旁路 | merge → 薄适配 | 10 |
| `application/services/long_term_memory.py` | `agent/memory` | adapt | 4 |
| `application/services/semantic_memory.py` | `agent/memory` | adapt | 4 |
| `application/services/memory_consolidation.py` | `agent/memory` | adapt | 4 |
| `application/services/redis_cache.py` | infra 实现 | keep · 经 Port | 4 |
| `application/services/breakpoint_resume.py` | runtime/HITL/Event | adapt | 8–9 |
| `application/services/artifact_store.py` | context 隔离通道 | reuse | 5 |
| `application/services/chat_service.py` | 用例编排 | defer 体积 | — |
| `application/services/approval*`（retrieval usecases） | HITL | adapt | 8 |
| `retrieval/domain/services/context_engine.py` | `agent/context` | adapt → move 可选 | 5 |
| `retrieval/domain/services/grounding_verify.py` | `agent/verifier` L3 | adapt | 7 |
| `retrieval/api/routers/approvals.py` | HTTP 面 | keep | 8 |
| `infrastructure/tools/definitions.py` | capabilities registry | move | 6 |
| ~~`infrastructure/tools/executor.py`~~ | capabilities 执行 | **已删**（A6 死代码批 2026-10-02：零 importer 死 shim） | 6 |
| `infrastructure/tools/human_review.py` | HITL 门禁 + **唯一**风险映射 | keep 语义 · 可 re-export | 6–8 |
| `infrastructure/tools/selection.py` | 能力检索（选择层） | move/enhance | 6 |
| `infrastructure/tools/registry_init.py` | 组装 | move | 6 |
| `infrastructure/tools/search.py` `result_limiter.py` | 工具实现 | move | 6 |
| `mcp/client.py` `server.py` `protocol.py` `tool_source.py` | capabilities/mcp | move | 6 |
| `infrastructure/persistence/checkpointer.py` | Runtime checkpoint | keep · 非 Memory | — |
| `domain/ports/*` | 扩展 agent ports | extend | 1 |
| `domain/policies/retry.py` `error_classification.py` `completion.py` `goal_arbitration.py` | Replanner/Verifier | reuse | 3 / 7 |
| `application/agents/lg_builder.py` 等（M2 已迁 `agentix/orchestration/langgraph/`，旧路径为 shim） | L2 主图 | M2 迁移完成（原「不迁入 agent/graph 本批」作废） | — |
| `application/agents/kg_sub_graph/planner/` | 子图规划 | defer 参考实现 | 11 参考 |
| `application/agents/kg_sub_graph/.../workflows/multi_agent/` | 业务 Multi-Agent | defer | 11 |
| `interfaces/http/**` | 协议转换 | keep · 新状态码映射 | 8 |
| `agentix/config/settings.py` `agentix/config/selfcheck.py` | Platform 配置 | extend 新键需进 selfcheck | 各 Phase |
| `evals/**` | Phase 10 基准扩展 | adapt | 10 |
| `security/**` | 输出过滤/注入 | reuse 挂 Policy | 2+ |
| `reliability/**` | 熔断 | reuse | 3 |
| `crawler/**`, `text2sql/wren_*` | 领域能力工具 | defer → capabilities 注册 | 6+ |

### 1.3 明确新建（2026-09-23 时点代码中不存在；下表路径均已按 §6 建成）

| 路径 | 最早 Phase |
|------|------------|
| `agentix/agent/core/` | 2 |
| `agentix/agent/planner/` | 2 |
| `agentix/agent/executor/` | 2 |
| `agentix/agent/observer/` | 2 |
| `agentix/agent/verifier/` | 2 骨架 |
| `agentix/agent/replanner/` | 3 |
| `agentix/agent/context/` | 5 |
| `agentix/agent/memory/` | 4 |
| `agentix/agent/policy/` | 2 |
| `agentix/agent/graph/` | 2 |
| `agentix/runtime/` | 14（物理） |
| `agentix/capabilities/` | 6 |
| `domain/ports/agent_core.py`（或等价拆分） | 1 |
| `docs/migration/Agent_Core_2.0_规划.md` 等四件 | 0（本批） |

## 2. 关键类名速查（迁移时符号级锚点）

### Executor 对照（审查 2026-09-27：多处同名易混，先看这张表）
| 名称 | 路径 | 职责 | 调用面 |
|------|------|------|--------|
| `MappingStepExecutor` | `agentix/orchestration/executor.py`（M2 迁入，原 agentix/agent/executor/executor.py（M6 已删，现见 `agentix/orchestration/executor.py`） 为 shim） | AgentLoop 步执行：`PlanStep.tool` → 注入 callable，异常归一 `Observation`/`FailureLayer`（`classify_exception` 也定义在此，**不在 capabilities 侧**） | `application/services/agent_loop_adapter.py` → `AgentLoop(executor=...)` |
| `ToolExecutor` | `agentix/tools/gateway/executor.py`（原兼容 shim `infrastructure/tools/executor.py` 已随 A6 死代码批删除——该 shim 零 importer，仅余 capabilities 一条路径） | 工具执行治理链：风险 → 权限/审批 → 超时/重试 → 轨迹镜像 | MCP、`hitl_bridge.install_executor_hook` |
| `Executor`（类型别名） | `application/services/agent_executors.py` | 任务级 Agent Run 执行后端注册表（`register_executor`/`get_executor`；内置 `probe` + `chat`） | `interfaces/http/v1/runs.py`（A1c 上移） |
| `StepExecutorPort` | `agentix/orchestration/orchestrator.py`（M2 迁入，原 agentix/agent/core/loop.py（M6 已删，现见 `agentix/orchestration/orchestrator.py`） 为 shim） | `MappingStepExecutor` 的协议端口（`AgentLoop.__init__(executor=...)`） | `orchestration/` |
| ~~`AgentExecutor`~~ | **已移除**（原 `services/breakpoint_resume.py`，零调用死代码，2026-09-27） | 曾与上两者同名、签名不同，混淆源之一 | — |

### Runtime
- `AgentState` / `AgentStatus` / `StepStatus` / `BudgetState` / `PolicyContext` — `agentix/runtime/state.py`（原 `application/runtime/state.py` shim 已在 A4 删除）
- `ExecutionStep` / `StepType` — `steps.py`
- `Budget` / `BudgetManager` / `BudgetExceededError` — `budget.py`
- `LifecycleManager` / `InvalidTransitionError` — `lifecycle.py`
- `AgentRuntime` — `runtime.py`

### Memory
- `LongTermMemory` / `UserProfile` / `MemorySummary`
- `SemanticMemoryStore` / `MemoryCard` / `MergeReport`
- `EvictionPlan` / `ConsolidationPlan`
- `RedisSemanticCache` / `RedisConversationHistory`

### Context / Verifier
- `assemble_context` / `build_status_bar` — `context_engine.py`
- `validate_answer_grounding` — `grounding_verify.py`
- `ArtifactStore` / `InMemoryArtifactStore` / `ArtifactRef`

### Tools / HITL
- `ToolSpec` / `ToolRegistry` / `RISK_LEVELS` / `FORBIDDEN_RISK_LEVEL`（L3 禁注册）
- `ToolExecutor` / `ToolResult`
- `ToolSelectionPolicy` / `select_tool_schemas`
- `HumanReviewGate` / `ReviewPolicy` / `ReviewRequired` / `RiskLevel` / `ReviewDecision`
- `BreakpointManager` / `InterruptRecord` / `CheckpointRecovery` / `ResumeAction`
- MCP：`MCPStdioClient`（default_risk_level=L2）、`MCPServer`、仅导出 L0 的 `RegistryToolSource`

### Trajectory / Eval
- services：`TrajectoryStep` / `TrajectorySink` / `InMemoryTrajectorySink` / `JsonlTrajectorySink` / `TrajectoryRecorder` / `compare_trajectory_prefix` / `export_trajectory_packages`
- observability：`TrajectoryRecorder` / `current_recorder` / `set_recorder` / `reset_recorder`（**同名，勿混用**；Phase 10 后已收敛为 services 薄适配，`as_service_steps` 桥）

### Domain ports（已有）
- `GraphProviderPort` / `SessionRepositoryPort` / `MessageRepositoryPort` / `RetrievalPort`
- payloads：`SessionCreatePayload` / `MessageCreatePayload` / `SessionSnapshotPayload` / `ConversationSnapshotPayload`

## 3. 现有状态机 vs 目标状态

| `AgentStatus`（现状，state.py） | 目标增量（Phase 8/9） |
|----------------------------------|------------------------|
| 既有枚举值（见源码 33 行起） | **+ `WAITING_APPROVAL`**（HITL） |
| — | **+ `PAUSED`**（Long-running） |
| — | **+ `WAITING_EXTERNAL_EVENT`**（Event Adapter） |

扩枚举时：全量回归 + HTTP 错误映射 + 前端可能显示旧值 → 进 Phase 8/9 DoD，不进 Phase 1。

## 4. 测试模块 → 相关 Phase（防误伤）

| 测试 | 针对 | 改动敏感期 |
|------|------|------------|
| `test_r3_agent_runtime.py` | application.runtime API | Phase 14 搬迁时整文件跟随 |
| `test_observability_trajectory.py` | **services** trajectory | Phase 10 merge 时 |
| `test_p32_tool_guards.py` / `test_p33_executor_review_gate.py` | human_review / executor | Phase 6–8 |
| `test_p33_task_planning.py` | TaskSpec / trajectory 导出 | Phase 1 类型抽取时 |
| `test_r1_tool_risk_levels.py` / `test_p13_registry.py` | RISK_LEVELS / registry | Phase 6 |
| `test_mcp_client.py` / `test_mcp_server.py` | mcp 包 | Phase 6 搬迁须改 import 路径（re-export 可减痛） |
| `test_r3_grounding.py` | grounding_verify | Phase 7 |
| `test_r3_tool_selection.py` | selection | Phase 6 |
| `test_p17_domain_layer.py` | domain 守卫 | Phase 1 加 ports 时 |
| `test_p41_doc_consistency.py` | README 数字 | 全程 · Phase 13 |

## 5. P0 最小增量文件清单（Phase 1–5 预期触碰）

```text
新增：
  domain/ports/agent_core.py          # 或拆 planner.py / verifier.py / memory.py / context.py
  domain/agent_types.py               # 若不用 application.task_plan 上提则新建
  agent/__init__.py
  agent/core/{__init__.py, loop.py, types.py}
  agent/planner/{...}
  agent/executor/{...}
  agent/observer/{...}
  agent/verifier/{...}
  agent/replanner/{...}
  agent/context/{...}
  agent/memory/{...}
  agent/policy/{...}
  agent/graph/{...}
  tests/test_agent_core_loop.py
  tests/test_agent_replanner.py
  tests/test_agent_memory_scope.py
  tests/test_agent_context_budget.py
  （文档）docs/migration/Agent_Core_2.0_规划.md + 本目录三件套

谨慎修改：
  domain/ports/__init__.py           # 再导出
  docs/architecture/interfaces.md    # Harness 对照
  docs/architecture/layers.md        # 新分层规则 + test_p17 同步
  tests/test_p17_domain_layer.py     # 仅增断言，不放松
  application/runtime/**             # P0 只加桥接，不改行为
  application/services/task_plan.py  # 兼容别名

禁止（P0）：
  observability/trajectory ↔ services/trajectory 合并
  application/runtime 物理搬迁
  lg_builder 主图重构
  README 的 N passed / golden 数字
```

## 6. 验收勾选（每 Phase 结束更新）

- [x] Phase 0 — 四文档落盘（规划 + tree + dependency + inventory）
- [x] Phase 1 — Protocol + 类型 + interfaces.md；守卫全绿（p17/p40/p41/p22/phase1）
- [x] Phase 2 — Agent Core 循环单测；全量绿
- [x] Phase 3 — 四层故障注入；全量绿
- [x] Phase 4 — Memory scope/裁决/PII；全量绿
- [x] Phase 5 — Context 前缀/状态栏/预算；全量绿
- [x] Phase 6 — capabilities/ 迁入 + Strangler shim + Tool Intelligence AC（负面描述/参数保真/单风险词表/MCP 防投毒）；全量绿
- [x] Phase 7 — 分层 Verifier L1/L2/L3 + Proposer–Reviewer 隔离；纯函数可测
- [x] Phase 8 — HITL Sidecar（Reviewer 独立于被审 Agent）+ Approve/Reject/Edit/Resume + 重启恢复
- [x] Phase 9 — Long-running Event Adapter（双时钟/去重/超时/重试/虚拟身份边界）+ WAITING_APPROVAL/PAUSED/WAITING_EXTERNAL_EVENT 枚举扩展
- [x] Phase 10 — Evaluation 2.0：domain.evals 显著性(1–2pp 规则)+成本全式；observability/trajectory 收敛为 services 薄适配（as_service_steps 桥）；首错/Pass^k 以 services+evals 为真源
- [x] Phase 11 — Multi-Agent：信息增量判据 + 六失败模式 + Peer/Manager 独立预算/深度/取消
- [x] Phase 12 — Continuous Evolution：在线零写 + staging/回归/灰度 + 三道防线 + 四载体
- [x] Phase 13 — Docs：五层架构（agent-core-layers）+ Research Agent Demo + 面试 10 问（README 数字未动；后续 README 深化已单独勾在本节末）
- [x] Phase 14 — Runtime 物理迁移：`agentix/runtime/**` 为真源；`application/runtime/**` 为 Strangler shim；全量回归 1981 passed
- [x] 最终审查纠错 — BOM/`__all__` 修复；滞后路径引用（inventory/dependency-map/current-tree/agent-core-layers/budget docstring）对齐 Phase 14；守卫 p17/p40/p41/code_hygiene/p46 全绿
- [x] README 深化 + 回归测试补强（2026-09-24）— Event 持久化 / HITL EDIT×approver / contradiction 终态 / custom planner step 重映射 / Event identity 往返；全量 1982

### 深化规划 Phase 15–20（详见 `Phase15-20_深化规划.md`，2026-09-24 立项）

- [x] Phase 15 — 点亮 Harness：`AGENT_LOOP_ENABLED` 默认关；`agent_loop_adapter` 工具/轨迹/结果桥；`chat_service._ask_impl` 支路；stream 仍 L2（决策 A）；flag-off 等价 + flag-on smoke（2026-09-24）
- [x] Phase 16 — HITL 三栈统一（`hitl_bridge` 库级桥：Gate → Sidecar → WAITING_APPROVAL/breakpoint；`HUMAN_REVIEW_ENABLED` flag 接线 + executor hook；误拒 `stats()` 可观测）（2026-09-26）
- [x] Phase 17（离线部分）— OnlineLoopRecorder 三漏斗接线；轨迹导出 `evals/baselines/traces/` 子目录约定；evals/guardrails/testing 武装顺序文档（2026-09-24）
- [ ] Phase 17（全栈部分）— `run_eval.py --record` 真 baseline + 播种 `quality.json` — **blocked（2026-09-26）：本机无 Docker/LM Studio，另期；拒绝伪造 baseline**
- [x] Phase 18 — 上下文分层（L1 limiter / L2 trim_policy / L3 bound_result_face；状态栏白名单；指令分层 settings→AGENTS.md/CLAUDE.md→会话；L4/Skills/Hooks/缓存纪律另期可选）（2026-09-26）
- [x] Phase 19 — multi_agent 接线（依赖 15；默认扁平，仅 flag 开 supervisor；wiring 翻转 + 指标流）（2026-09-26）
- [x] Phase 20 — 进化 + 事件点亮（`EVENT_PORT_BACKEND` memory/jsonl + webhook `POST /api/v1/events` + `wait_external_event`；`evals/evolution_run.py` 离线提案接 17 门 + 门对齐；Redis Stream 另期）（2026-09-26）

### 深化规划 Phase 21–26（详见 `Phase21-26_规划.md`，2026-09-27 立项）

- [x] Phase 21 — Skills 渐进披露 + Hooks 生命周期（`SKILLS_ENABLED`/`HOOKS_ENABLED`/`SKILLS_DIR` 三 key 默认关；两段式：元数据目录注入 `build_instructions` + `read_skill` 惰性正文；hooks 5 事件同步 callable + `ToolExecutor` 闸后观测对；`describe_groups()`；新包 `agentix/agent/skills/` + `agentix/agent/hooks/` + 示例 `skills/release-checklist/`）（2026-09-27）
- [x] Phase 22 — 上下文二期（缓存纪律 + L4）（`CONTEXT_AUTOCOMPACT` 默认关五处同步；`ToolRegistry.freeze()/unfreeze()/schema_prefix_hash()` 注册序冻结快照 + 告警拒绝；`config_watch` 非 secret 配置 hash → 轨迹 append `system_notice`（键名/哈希无值无 PII）；`domain/ports` 新 `CompactorPort` + 五要素机制断言 `agentix/agent/context/compaction.py`；L4 接入 `ContextEngine`/`SimpleContextBuilder`（flag off 逐字节等价，异常/断言失败回退丢弃循环）；生产 `application/agents/compactor.py::LLMCompactor` 经组合根 flag-gated 装配）（2026-09-27）
- [x] Phase 23 — 交互二期（出站通知 + Redis Stream + 事件矩阵）（`NOTIFY_USER_ENABLED` 默认关五处同步；`capabilities/tools/notify_user.py`（L1→MEDIUM、HITL 闸、`side_effect=True`）+ 组合根 `application/services/notify_bridge.py::install_notify_user_tool` 注入；`EVENT_PORT_BACKEND=redis_stream` 第三实现 `agentix/agent/events/redis_stream.py`（懒连接、SET NX 防投递竞态、分层偏离落地注）；`GET /ops/events` 鸭子扩展 `list_recent`；矩阵测试 dedup/timeout/cancel/concurrent/cross-restart；新 `tests/test_agent_phase23_interaction.py` 47 用例）（2026-09-27）
- [x] Phase 24 — 记忆/知识深化（整理遗忘生产化 + Agentic RAG）（`MEMORY_CONSOLIDATION_ENABLED` / `AGENTIC_RAG_ENABLED` 两 key 默认关五处同步；`consolidate` 淘汰红线改造——先归档 `ltm:archived:summary:{sid}`（同 TTL、含 `source_message_ids` 证据链）再删原键 + `report.archived_session_ids`；重要性三因素纯函数 `access_frequency_score`/`recency_decay_score`（`MEMORY_RECENCY_HALF_LIFE_DAYS` 半衰期）/`uniqueness_score` + `importance_sort_key` 字典序接 `plan_summary_eviction`（一/二键与旧语义兼容）；task handler `memory_consolidation` 经新 `try_claim_consolidation`（SET NX EX 幂等窗，dry_run 不占窗，force/window<=0 旁路）；Agentic RAG 挂 `general_handler.create_kb_query` 函数体（双拓扑收敛点，空检索改写重试 ≤2、轨迹 `kind=retrieval/name=agentic_rag_rewrite`、prompt 外置 `lg_prompts.KB_QUERY_REWRITE_PROMPT` 过 p23）；新 `tests/test_agent_phase24_memory_rag.py` 25 用例）（2026-09-27）
- [x] Phase 25 — multi-agent 运行时防线（批三）（`CONTROL_PLANE_MAX_CONCURRENCY/DEPTH` + `MULTIAGENT_PEER_MAX_STEPS/MESSAGES` + `MULTIAGENT_BUS_ENABLED` 五 key 默认关/0 五处同步；`ControlPlane.check_admission` 并发/深度准入 + `spawn` depth meta + `chat_service` `AdmissionDenied` fail-closed（注册故障仍降级）；新 `agentix/agent/multiagent/bus.py::InProcessBus` 纯 asyncio 进程内总线（send/recv/cancel/resume/discover/status/stats + `BusStats`，零 Redis/Docker）；`Manager` 注入 `budget_factory`/`bus` 默认 None 直连等价 + `detect_artifact_conflicts` 纯函数（同 key 不同值 + 极性并存，只标记不改写）+ `_outcome` meta（conflicts/conflict_marked/bus status）+ `_peer_cancelled` 早返回 + run 起点建信箱；`lg_supervisor` `_budget_factory`/`_make_bus`（`MULTIAGENT_BUS_ENABLED` 关）+ defense_meta 合入 `multi_agent`；修复最终 `detect_failure_mode` 漏传 `echo_overlap` 致 echo_chamber 恒退化 livelock；新 `tests/test_agent_phase25_multiaagent_guards.py` 26 用例）（2026-09-27）
- [x] Phase 26 — 评估三期 + Phase 17 解阻清单（批三）（`evals/run_eval.py` judge 去 stub：`judge_similarity(question, answer, model=None)` 惰性接 `LLMClient`（温度 0）+ `ModelGateway.resolve_model("standard")`、`_judge_preflight` 端点预检（无端点 `NotImplementedError` 退出码 3）、`JudgeError` 输出不可解析退出码 3、空回答 0.0 不触网、`evaluate` fail-fast + summary `judge_model/judge_count/judge_mean`（`judge_*` 只增不改、五 metric 门零变化）+ `format_summary_line` 辅助行；`--compare A B` 按 id 配对 + 五指标 percentile bootstrap 95% CI（纯 stdlib `random` 固定 seed，CI 含 0 →「噪声区间」、Δ=A−B、独有条目如实报告、无配对/缺文件退出 2）；`evals/README.md` §4.4 校准四步 + §4.4.1 比较口径 + §9 状态；`test_p21` 两处桩测试改端点语义（本地 `.env` 真端点下原测试会触网）；新 `tests/test_agent_phase26_eval_judge.py` 19 用例；附录 A 解阻清单待真机勾选）（2026-09-27）
- [x] Phase 27 — 死代码清理 + 文档收尾（批外收口）（`search_service.py` + `search_prompts.py` + 空包 `application/prompts/` 整体删除——`SearchService` 零 importer、`generate_stream` 零调用、5 类事件零引用，只删方法会留 `_call_with_tool`/prompt 孤儿故整体删除；p40 防复活守卫 `test_deleted_search_service_stays_deleted` + `services.__init__`/`settings.py` DISPLAY_TZ 注释/`.pre-commit` 排除表/`extension.md`/`current-tree`/`dependency-map` 引用同步；evolution-assessment §4 处置列 + §8 更新；Phase15-20 陈旧勾选同步（L4/缓存纪律/SkillsHooks 已由 Phase 21–22 完成、LLM-judge 已由 Phase 26 完成））（2026-09-27）

### 批二收尾（非 Phase，2026-09-27）

- [x] 前后端契约显示同步（knowledge progress/answer_id/trace/terminate_reason + chat 流式主路径 + citation `effective_status/replaced_by` 链）
- [x] 旧版文档删除 8 份 + ADR `0003-infrastructure-criticality` 重编 `0006` + 6 处路径 rot 修复（全仓引用扫描清零后删除）
- [x] `docs/migration/evolution-assessment.md` 架构/代码框架演进评估（依赖方向 / 分层偏离 / 双 SSE / 零调用面 / 框架候选均不动）

### 接口审查批次（非 Phase，2026-09-28）

- [x] 前后端同步审查（并行产出：前端 S1–S4/M1–M8/L1–L8 + 后端 P1–P3 清单）→ 修复：流式主路径助手落库（`event_stream` finally + 独立 Session）、历史最近 N 条窗口（新 `get_recent_by_session`）+ `limit ge=1 le=100`、`_AGENT_SEM` 流式限流对齐、`count_by_user` 软删口径、`_public_metadata` 过滤 `agent_state`、retrieval SSE 异常收口、`agent_runs` 日志格式
- [x] 前端同步/渲染修复：SSE 终态解析器（`terminated` 双向丢弃）、会话切换竞态守卫 + abort + 草稿统一 + `openConversationById` 回源、进度条优先于 pending 占位、`serverResponded` 不回退、截断返回 `failed`、maxlength=5000 对齐、共享 API 配置层 `web/src/api/config.ts`、`readChatSse` 残留帧/CRLF/`releaseLock`
- [x] 测试补齐：`test_chat_api.py` 零断言脚本重写为 12 条离线契约测试（`integration` 标记清零）+ 前端 4 文件 20 用例；全量 **2303 passed / 14 skipped / 0 deselected** + vitest **113**（2026-09-28）
- [x] 文档：README §11 基线 + 七处基线同步 + CHANGELOG + 新增 `docs/architecture/audit-2026-09-28.md`（架构审计：可优化点 + 下一步优化方案）

### 优化迭代 Phase 28–31（详见 `Phase28-31_优化规划.md`，2026-09-28 立项）

- [x] 前置 · 审计批次 A（A1 会话/历史归属校验 IDOR / A2 上传读鉴权 / A3 限流口径；安全修复默认开无 flag；验收 `test_p48_ownership.py` 21 条 + `test_rate_limit_middleware` 扩 4 条）（2026-09-28 完成：`v1/deps.py` 新增 `resolve_v1_caller`/`session_visible_to`；chat/sessions/upload 全端点接归属校验，跨用户 404；上传 owner sidecar `.owners/`；ratelimit per-params 派生 + retrieval limiter 热重建；全量 **2329 passed**）
- [x] Phase 28 — 自主护栏（需求澄清决策 + 预算意识）（`CLARIFY_ROUTING_ENABLED` + `BUDGET_NOTICE_ENABLED` 默认关各五处同步；确定性澄清启发挂 handler 函数体不加图节点 + 单轮约束；状态栏 +`max_iter` 常量键 + 轨迹 append `budget_notice`（剩余 ≤2，不动前缀））（2026-09-28 完成：新 `clarify.py`/`budget_notice.py`；`general_handler` `_maybe_clarify` 前置 + `AgentState.clarify_armed`；`loop.py` 注入 `budget_observer` + chat_service 流式 `langgraph_step` 检查点（非流式 ainvoke 无观测点不插）；验收 `test_agent_phase28_autonomy_guards.py` 28 条；全量 **2357 passed**）
- [x] Phase 29 — 记忆结构化二期（JSON Cards + User as Code）（`MEMORY_PROFILE_CARD_ENABLED` 默认关（原案 `MEMORY_CARDS_ENABLED` 与 R3-8 撞名 ⇒ 独立开关）；`profile_card` 只增不改 + `derive_profile` 派生幂等 + 冲突只标记不覆写；离线记忆 fixtures）**（2026-09-28 完成，20 条验收，全量 2458 passed；附带修 `tests/fakes/redis.py` 负 stop 区间语义）**
- [x] Phase 30 — 进化闭环（载体扩展 + 提案落盘 + 睡眠学习）（`EvolutionCarrier` +`KNOWLEDGE`/`PROGRAM` + 各自回归门、PROGRAM 声明式 allowlist fail-closed；`EVOLUTION_PROPOSAL_LOG=data/evolution/proposals.jsonl` 三状态 append-only；`EVOLUTION_SLEEP_ENABLED` 默认关 + `evolution_sleep` handler 端点预检，只产提案不晋升）**（2026-09-28 完成，41 条验收，全量 2438 passed；Phase 12 载体断言同步六值）**
- [x] Phase 31 — 评估二期（分层 golden + 声称一致性 + 敏感性/隐私 + Best@k）（golden 只增 `difficulty`/`source` 不增删行；新 evals/claim_check.py + `--sensitivity` + `privacy_flagged` + `best_at_k` 全部报告键只增不进门禁；`test_ci_gate_contract` 门键零变化）**（2026-09-28 完成，40 条验收，全量 2397 passed）**
- [x] 穿插 · 审计批次 B（B1 `STREAM_IDLE_TIMEOUT` 看门狗 / B3 token 解耦 / B4 跨标签页；B2 SSE 词汇双写单列小批）与批次 C（C1 窗口化 + C2/C3 lint 清理独占批次）**（2026-09-28 完成：B1 `wait_for(__anext__)` + test_p14 新 4 条；B3 `web/src/api/token.ts` 共享层 + knowledge re-export；B4 `conversationSync.ts` BroadcastChannel 三事件 + vitest 4 条；B2 chat/retrieval 27 处 yield 双写 `event`+传输层透传 + 前端回退解析（双稳定两周期后弃 `type`）；C1 >200 折叠中段 2 条；C2 tests 清零 55 处 + 移入门禁命令 + 删 tests per-file 豁免；C3 删 `PROGRESS_STAGE_LABELS` 死导出（空态组件评估后不合并，理由见 audit §1）；全量 2463 passed + vitest 121）**
- [ ] 另期 · 缺口 10 对等变体 / 11 主动工具发现层 2 / 12 用户模拟器（需端点 blocked）

### 优化批次 P1–P6（2026-09-30 立项，2026-10-01 全量完成）

> 对标 AI Agent 开发岗招聘需求的六批次（计划与借鉴依据见 `docs/source-code-study.md` §4/§5，逐批变更见 `CHANGELOG.md`）。每批默认关 flag + 五处同步 + p49–p60 十二个新测试模块，全量基线只增不减。

- [x] P1 · 任务执行链路（`task-query` 路由三件套 + `execute_task` 图节点 8→9 + 声明式分波管线 `agentix/application/services/task_execution.py` + `task_note`/`task_artifact` 写工具 + `AGENT_MAX_REPLANS` 重规划注入；`TASK_EXEC_ENABLED`/`AGENT_MAX_REPLANS` 两 key；新 `test_p49` 33 条；全量 **2463 → 2496**）
- [x] P2 · Skills/Registry/动态调度（`tool_policy` 注入方 ask/stream 双路径 + `tool_search` 延迟召回 + CJK bigram ranking + skills manifest 指纹热重载 + 规范即测试 + `audit_tool_registries` 三注册表对账 + 裁决顺序全链序列守卫 + 执行契约三处修复；`TOOL_SELECTION_ENABLED`/`MAX_RISK`/`DEFERRED_TOOLS`/`RESULT_CONTRACT` 四 key；新 `test_p50/51` 共 46 条；**2496 → 2542**）
- [x] P3 · 渐进上下文（指令 sha256 去重 + 12k 总预算 + L4 keep-last 压缩阶梯 4→2→1→0 + `AgentLoop` 逐步重建 `context_budget_tokens` + `artifact_read` 写读闭环 + `GET /ops/context` 只读诊断；`CONTEXT_COMPACT_LADDER`/`AGENT_CONTEXT_BUDGET_TOKENS` 两 key；新 `test_p52/53` 共 52 条；**2542 → 2594**）
- [x] P4 · 异常闭环（`guardrails.py` 三计数器判级 + 恢复建议回喂 + 重复结果折叠 stub + `plan_mode`/`rollback` 权限快照恢复 + `RollbackPort` 首个生产实现 `InMemoryRollbackStore` + 失败 turn 先持久化边界；`TOOL_GUARDRAILS_ENABLED`/`AGENT_ROLLBACK_ENABLED`/`FAILURE_BOUNDARY_ENABLED` 三 key；新 `test_p54/55/56` 共 74 条；**2594 → 2668**）
- [x] P5 · Proactive（零依赖 cron 解析 + 原子 `JobStore`/advance 先行 + `cron_scheduler.tick` 五步契约（revision 让位/reap/推进/防双跑/回执）+ 交付账本与事故单 + `Cron*` 四工具族 + 组合根 `main._build_cron_runner` 接线；`CRON_ENABLED`/`CRON_TICK_SECONDS` 两 key；新 `test_p57/58` 共 93 条；**2668 → 2761**）
- [x] P6 · 长时记忆（`MemoryManager` 多 provider 扇出 fail-open + `LongTermMemoryProvider` Redis 适配器（独立键空间/确定性 id/上限裁剪）+ `memory_review` 后台评审路由 + `create_skill_draft` 草稿沉淀（`data/skills_drafts` 隔离）+ `get_context_for_query` 分段 fail-open；`MEMORY_FANOUT_ENABLED`/`MEMORY_REVIEW_ENABLED` 两 key（6.3 复用 `SKILLS_ENABLED`）；新 `test_p59/60` 共 86 条；**2761 → 2847**）

收口（2026-10-01）：全量 **2847 passed / 14 skipped / 0 deselected** + `ruff check agentix tests` 全绿 + p41（含 13 个新键名单）33 条；README §11 基线、`current-tree.md` 数字、`source-code-study.md` §4 对照矩阵已同步终态。

### 统一执行链 Stage A/B/M/F/T（详见 `架构重构_统一执行链方案_v2.md`，2026-10-01 立项，2026-10-04 收口）

- [x] Stage A 行为收敛（2026-10-02~03）— A1 Run 真源/单 owner/退出契约（p61）· A2 Tool Gateway 三闸有效化 + 记忆三工具（p62）· A3 图内规划收敛（p63）· A4 HITL 生产空转修复 + `application/runtime` 直删（p64）· A5 SSE 归一 R5a–d（p65）· A6 分层守卫定稿：LangGraph 白名单棘轮 + shim 空集断言（p66，12 条统一架构守卫全部测试化）
- [x] Stage B · P0 收敛（2026-10-03）— 32-P0 记忆双卡合并/脱敏/冲突键（p67）· 33-P0 LightRAG 去重/租户 fail-closed/rerank 三态/图谱硬化（p68）
- [x] Stage M 结构迁移（2026-10-02~03）— M0 契约拆分 + 逐文件清单冻结（p69）· M1 `tools/`（p70）· M2 `orchestration/`（p71）· M3 `policies/`（p72）· M4 `runtime/control_plane`（p73）· M5 retrieval 收编（p74）· M6 三簇 shim 清零（p75，旧路径一律 `ModuleNotFoundError`；对照矩阵见 v2 §5 终态列）
- [x] Stage F1 · 三方向 P1（2026-10-03）— 真 hybrid Dense+Sparse RRF（p76）· 记忆工具化/向量召回接线（p77）· 索引级联隔离（p78）· Multi-Agent LLM 规划（p79）· 并发工具轮（p80）· HITL 进环（p81）
- [x] Stage F2 · 三方向 P2（2026-10-03~04）— F2a Memory：画像卡/冲突标记/后台评审（p82）；F2b RAG：证据门（p83）· 查询改写/HyDE（p84）· Text2SQL 硬化（p85）· 图谱动态收敛（p86）· 混合检索四列归因报告 hybrid_eval（p87）；F2c Multi-Agent：统一退出契约 status/exit_reason/truncated + 失败模式打点（p88）· 子代理隔离三件套（p89）
- [x] Stage T 终态（2026-10-04）— README 架构段/项目结构树重绘 · PROJECT_SPEC 终态修订（SSE 词汇归一/M6 删除标注/M0 拆分标注）· v2 §5 对照矩阵终态化 · `current-tree.md` 全量刷新 · evals 退出契约报告键（p90，总纲 §6 Multi-Agent 四键）· 本文档收口 · CHANGELOG

终态收口（2026-10-04）：全量门禁见 README §11（唯一真源，带实测日期）；`ruff check .` 全绿；p41 33 条全绿；shim 目录断言为空集模式（p66），LangGraph 导入白名单 10 文件 equality 断言。
