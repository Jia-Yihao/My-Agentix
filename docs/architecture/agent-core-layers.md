# Agent Core 五层架构（v1.1）

对应 `docs/migration/Agent_Core_2.0_规划.md` §2 与本仓库分层守卫
（`docs/architecture/layers.md`、`tests/test_p17_domain_layer.py`）。

> 五层 = 既有四层（interfaces / application / domain / infrastructure）之上，
> 由 **agent/**（Harness 核心）与 **capabilities/**（工具与能力）在
> application 侧承担的「Agent 运行面」拆分；物理目录仍是四层包，
> 下表按**运行时职责**叙述，不改 `layers.md` 的 import 规则。

## 1. 职责总览

| 层（职责） | 包 / 目录 | 代表模块 | 依赖 |
|-----------|-----------|----------|------|
| **Harness** 循环与策略 | `agentix/agent/` + `orchestration/` + `policies/` | `agent/core/loop`（M2 后实体 `orchestration/orchestrator.py`）、`orchestration/executor.py`、`strategies/multi_agent/`、`policies/{planner,verifier,hitl,guardrails}/`、`agent/{observer,context,memory,events,evolution}/` | domain, capabilities 接口, runtime 接口 |
| **Capabilities** 工具与能力 | `agentix/capabilities/` + 顶层 `tools/` | `capabilities/mcp/`、`tools/`（M6 后 capabilities/tools 已删） | domain, infrastructure 实现 |
| **Application** 用例编排 | `agentix/application/` | `services/`, `agents/`（runtime/ 已随 A4 直删，真源 `agentix/runtime/`） | domain.ports, agent, capabilities |
| **Domain** 规则与端口 | `agentix/domain/` | `agent/`, `runtime/`, `tool/`（M0 三子包）、`ports/`, `policies/`, `evals.py`（agent_types.py 已随 M6 删除） | 零第三方、零上游层 |
| **Interfaces** 协议入口 | `agentix/interfaces/` | `http/` | application |

## 2. Harness 五职责 ↔ 端口

见 `agentix/domain/agent/ports.py`：

| 职责 | 端口 | Agent 实现 |
|------|------|------------|
| Context | `ContextPort` | `agentix/agent/context/engine.py` |
| 规划 | `PlannerPort` | `agentix/policies/planner/simple.py` |
| 工具 | `ToolSelectorPort` | capabilities 工具选择 |
| 验证 | `VerifierPort` | `agentix/policies/verifier/layered.py` |
| 纠偏 | `RollbackPort` | runtime 断点 / HITL Resume |
| 记忆 | `MemoryPort` | `agentix/agent/memory/service.py` |
| 事件 | `EventPort` | `agentix/agent/events/adapter.py` |

## 3. 依赖硬规则（与 CI 一致）

1. `domain/` 零第三方、不依赖 infrastructure/interfaces。
2. `agent/` 除 `agentix/agent/graph/` 外禁 LangGraph；禁依赖 `interfaces`。
3. `capabilities/` 不反向依赖 `agent/` 业务循环。
4. Runtime 真源在 `agentix/runtime/`；原 `application/runtime/` Strangler shim 已在 A4 直删。

## 4. 相关文档

- 分层 import 规则：`docs/architecture/layers.md`
- 接口冻结：`docs/architecture/interfaces.md`
- 迁移进度：`docs/migration/module-inventory.md`
