# Phase 0 · dependency-map.md — import 依赖与风险点

- 生成时间：2026-09-23
- 方法：静态 import 检索 + `tests/test_p17_domain_layer.py` / `docs/architecture/layers.md` 规则对照
- 目的：为 Agent Core 2.0 迁移标记**可安全抽取点**与**高耦合点**；不在本文件实施改动

## 1. 允许的分层方向（现状守卫）

```text
interfaces ──► application ──► domain.ports / domain.policies
                  │                ▲
                  │ 注入            │ 实现
                  ▼                │
             infrastructure ───────┘

domain 禁止 ──► infrastructure / interfaces（零豁免）
domain 禁止 第三方：sqlalchemy, langchain*, langgraph, fastapi, pydantic,
                 loguru, aiohttp, redis, pymilvus, openai, ...
```

新增计划约束（写入 layers.md，P0 Phase 1+）：

```text
agent/ ──► domain, capabilities接口, runtime接口
agent/graph/ ──► langgraph（仅此）
runtime/ ──► 不依赖 planner 实现 / 业务 agent / UI / HTTP
```

## 2. Runtime 簇（Phase 14 后：`agentix/runtime/` 为真源；`application/runtime/` shim 已在 A4 直删；M4 起 `control_plane` 亦迁入）

| 模块 | 关键符号 | 内部依赖 | 外部被谁 import |
|------|----------|----------|-----------------|
| `agentix/runtime/state.py` | `AgentState`, `AgentStatus`, `StepStatus`, `Task`, `ErrorInfo`, `BudgetState`, `PolicyContext` | 仅 TYPE_CHECKING 回指 `ExecutionStep` | runtime 包内；`chat_service`；`tests/test_r3_agent_runtime.py` |
| `agentix/runtime/steps.py` | `ExecutionStep`, `StepType` | `state.StepStatus` | runtime 包内 |
| `agentix/runtime/budget.py` | `Budget`, `BudgetManager` | `state.BudgetState` | runtime 包内 |
| `agentix/runtime/lifecycle.py` | `LifecycleManager` | `AgentState`, `AgentStatus` | runtime 包内 |
| `agentix/runtime/runtime.py` | `AgentRuntime` | budget/lifecycle/state/steps + **运行时** `observability.trajectory.current_recorder` | `agentix/runtime/__init__` 再导出 |
| `agentix/runtime/control_plane.py`（M4 迁入，自 `application/services/control_plane.py`） | `ControlPlane`, `AgentRun`, `AdmissionDenied`, `get_control_plane`, `reset_control_plane`, `RunState`/`TERMINAL_STATES`（M0 再导出） | `domain.runtime.runs`（模块级）+ `config.settings`（`get_control_plane` 内延迟）+ `application.services.token_budget.TokenBudget`（**仅 TYPE_CHECKING 注解边**，运行期零引用） | `chat_service`×2、retrieval `chat_service`（旧路径 shim）；`tests/test_p31_control_plane.py` 等 |
| `agentix/runtime/__init__.py` | 聚合再导出（**不含** control_plane——子模块直取） | 包内 | `chat_service`（延迟 import）等（原 `application/runtime/*` shim 再导出，A4 已删） |

**结论**：Runtime 簇**运行期自包含**（模块级 agentix 边只允许 `runtime`/`domain`/`config`，p73 AST 断言钉死），跨簇边两处皆惰性：`runtime.py` 对 `observability.trajectory` 的函数内延迟 import、`control_plane` 对 `token_budget` 的 `TYPE_CHECKING` 注解边（先例 `state.py` 回指 `steps`）—— Phase 1 Protocol 桥接、Phase 14 物理搬迁、M4 控制平面迁入均已完成；`application/runtime/**` 原为 Strangler shim（PEP 562 再导出），A4 已直删；`application/services/control_plane.py` 已于 M6 删除（迁移期为 `sys.modules` 别名 shim）。

### 2.1 LangGraph 出现点（orchestration 边界现状）

| 位置 | 用途 | P0 是否改动 |
|------|------|-------------|
| `orchestration/langgraph/lg_builder.py` | 主图 `StateGraph` + `MemorySaver` | 否（L2 主图保持） |
| `application/agents/text2sql/workflow.py` | text2sql 子图 | 否 |
| `kg_sub_graph/agentic_rag_agents/workflows/**` | single/multi agent 子图 | 否 |
| `infrastructure/persistence/checkpointer.py` | MemorySaver / RedisSaver | 否（Runtime Checkpoint 边界） |
| `domain/ports/graph.py` | **零** langgraph（`Any` 返回） | 保持 |
| **`agent/graph/`（新建）** | Agent Core 编排 | Phase 2 起 |

> **M5（2026-10-03）**：`retrieval/usecases/chat_agent_runner.py` 已剥离 langgraph
> （capability 内部工具循环，p66 白名单 11→10）——不再出现在本表。

**风险**：`ops_router`、`application/agents/main.py` 直接 `from ...lg_builder import graph` —— 与「值绑定」注释约定一致，迁移时**禁止**在 `agent/core` 复刻该模式（必须经 `GraphProviderPort` 注入）。

## 3. Memory 簇（适配不重写）

| 模块 | 符号 | 依赖 | 计划 |
|------|------|------|------|
| `services/long_term_memory.py` | `LongTermMemory`, `UserProfile`, `MemorySummary` | 服务层 | Phase 4 → `MemoryPort` 适配 |
| `services/semantic_memory.py` | `SemanticMemoryStore`, `MemoryCard`, `MergeReport` | 服务层 | Phase 4（读取时裁决） |
| `services/memory_consolidation.py` | `EvictionPlan`, `ConsolidationPlan` | 服务层 | Phase 4 合并策略 |
| `services/redis_cache.py` | `RedisSemanticCache`, `RedisConversationHistory` | redis（infra 合法） | 保持 infra；经 Port 暴露 |

**不**把 Redis 符号抬进 domain/agent 纯类型层（会踩 p17 FORBIDDEN）。

## 4. Context 簇

| 模块 | 符号 | 被谁用 |
|------|------|--------|
| `retrieval/domain/services/context_engine.py` | `assemble_context`, `build_status_bar`（状态栏） | `usecases/chat_agent_tools.py`, `usecases/chat_service.py` |

**结论**：已在 retrieval domain（零第三方倾向），Phase 5 上提为 `agent/context` 时优先 **import 复用 → 抽公共类型**，避免复制两份逻辑。

## 5. Verifier 簇

| 模块 | 符号 | 特征 |
|------|------|------|
| `domain/policies/grounding_verify.py`（M5 迁出；旧 `retrieval/domain/services/` 路径已于 M6 删除） | `validate_answer_grounding`, OO 封装 | 纯函数、零 LLM、bigram 包含度 |
| `usecases/chat_service.py` | 延迟 import grounding | 检索通道最终答案 |
| `kg.../components/validate_final_answer/` | 图内最终答案校验 | LangGraph 节点（`node.py` 曾删，模块仍在演进） |
| `kg.../components/final_answer*` | final answer guard | 图内 |

Phase 7：分层 Verifier —— L1 结构 / L2 规则（`domain/policies`）/ L3 grounding，**Proposer–Reviewer 隔离**。

## 6. Tool / MCP 簇

```text
infrastructure.tools.definitions (ToolSpec, RISK_LEVELS)
    ▲
    ├── executor (ToolExecutor) ──► human_review (HumanReviewGate, RiskLevel 映射)
    ├── selection (ToolSelectionPolicy) 
    ├── registry_init ──► application.agents.lg_builder / kg tool_selection
    ├── mcp.tool_source / mcp.client (默认 L2；仅导出 L0)
    └── kb_tools / evals & tests
```

| 边 | 说明 | Phase 6 注意 |
|----|------|--------------|
| executor → human_review | 执行前 preflight | 扩展 `WAITING_APPROVAL` 时保持 ReviewRequired 语义 |
| mcp → definitions/executor | 远端工具并入同一 Registry | 描述当不可信输入；shadowing 测试 |
| selection → definitions | 暴露面 `max_risk_level` | 与 human_review **唯一** L0–L3↔RiskLevel 映射不重复造 |
| `PolicyContext.max_risk_level`（runtime.state） | 运行时上限 | 与 selection 默认 L1 对齐，双处配置需单测钉住 |

**双词表结论**：声明级字符串 `L0–L3` 与枚举 `RiskLevel` 映射**只允许一处**（已在 `human_review.py`）；Phase 6 文档化 + 禁止第二处映射表。

## 7. Trajectory 双实现（P0 不合并）

| 实现 | 符号 | 使用方 |
|------|------|--------|
| `observability/trajectory.py` | `TrajectoryRecorder`, `current_recorder` | runtime、spans、executor、`retrieval/usecases/chat_service`（运行时旁路） |
| `application/services/trajectory.py` | `TrajectoryStep`, Sinks, `TrajectoryRecorder`, `compare_trajectory_prefix` | **evals**、`application.services.chat_service`、`llm_factory`、大量 `test_observability_trajectory` |

**风险**：同名 `TrajectoryRecorder` 双定义；前缀回归工具依赖 **services 版**。  
**计划**：Phase 10 收敛为 services 版为导出/评测真源，observability 收薄适配；**禁止**在 Phase 0 改 import（会打爆既有 trajectory 测试套件）。

## 8. HITL / 断点簇（Phase 8 输入）

| 模块 | 符号 |
|------|------|
| `agentix/tools/gateway/human_review.py`（M1 迁入，原 `infrastructure/tools/human_review.py` 已随 M6 删除） | `HumanReviewGate`, `ReviewPolicy`, `ReviewRequired` |
| `retrieval/usecases/approval_service.py` | 列表/详情/approve/reject |
| `retrieval/api/routers/approvals.py` | HTTP 审批 |
| `application/services/breakpoint_resume.py` | `BreakpointManager`, `InterruptRecord`, `CheckpointRecovery`, `ResumeAction` |

**现状 gap**：审批链路偏「工具 L2」与检索侧 approvals；**Runtime 状态机尚无 `WAITING_APPROVAL`**（`AgentStatus` 需在 Phase 8 增补，属枚举扩展——注意向前兼容测试）。

## 9. 高耦合 / 延后处理点

| 点 | 原因 | 建议 Phase |
|----|------|------------|
| `application/services/chat_service.py`（千行级） | 事件过滤、langgraph_node 启发式、多延迟 import | 不进 P0；随 10/14 |
| `orchestration/langgraph/lg_builder.py` + 全图 | L2 主图与业务节点纠缠 | P0 不动；Agent Core 只新增 L3 分支 |
| `kg_sub_graph/agentic_rag_agents/**` | 体量大、子图成熟 | 不重写；工具面经 capabilities 汇聚 |
| `interfaces/http/ops_router.py` → `lg_builder.graph` | 启动期 graph 引用 | 保持；新包禁止仿此模式 |
| Control plane ↔ retrieval HTTP | 服务发现耦合 | 规划遗留；非本批 |

## 10. 测试基线与守卫依赖（每 Phase 回归清单）

```powershell
& ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests
& ".\.venv\python.exe" -m mypy agentix/domain --config-file=pyproject.toml
```

关键守卫文件（勿破坏）：

| 文件 | 保护什么 |
|------|----------|
| `test_p17_domain_layer.py` | 分层 + domain 零第三方 |
| `test_p41_doc_consistency.py` | README 数字/Python 版本/`N passed` |
| `test_p35_config_governance.py` | selfcheck |
| `test_p47_wiring_gaps.py` | 熔断器配置键（注释态保持） |
| `test_p46_toolchain_parity.py` | 工具链参数一致 |
| `test_dependency_consistency.py` | pyproject ⊆ requirements 分层 |
| `test_p13_registry.py` / `test_p32_tool_guards.py` / `test_p33_*` | 工具与 HITL |
| `test_observability_trajectory.py` | **services 版** trajectory |
| `test_r3_agent_runtime.py` | runtime 公共 API（`agentix.runtime` 真源 + `application.runtime` shim） |

本地统一使用 `.venv`（Python 3.12.13）；CI 按 workflow 配置独立验证；全量测试不要并行，临时目录使用 pytest 默认的仓库外位置。

## 11. Phase 0 结论（给 Phase 1 的输入）

1. **可立即新建** `agentix/agent/**`、`domain/ports` 新 Protocol —— 无目录冲突。  
2. **Runtime** 走桥接不搬树；唯一外依是 trajectory。  
3. **Memory/Context/Verifier** 以适配与上提为主，不重写。  
4. **禁止** Phase 0 改 trajectory 双实现、改 lg_builder 图结构、改 README 数字。  
5. 新增分层规则写入 layers.md 时，必须与 `test_p17` 同步扩展（同 PR），避免守卫漂移。
