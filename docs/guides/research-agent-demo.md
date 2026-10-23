# Research Agent Demo（Phase 13）

离线可跑的「研究型任务」走查：不依赖外部 LLM/Redis，展示 Agent Core 主循环
各组件如何协作。生产入口仍是既有 HTTP / LangGraph 链路；本 Demo 用**确定性
桩**替换 Planner/Executor 输出，便于本地验证接线与验收断言。

## 1. 组件映射

| 步骤 | 模块 | Demo 行为 |
|------|------|-----------|
| 任务 | `agentix/domain/agent/types.py::TaskSpec` | 固定 goal + success_criteria |
| 规划 | `agentix/policies/planner/simple.py` | 三步固定 Plan（检索→综合→自检） |
| 执行 | `agentix/orchestration/executor.py` | 调用注入的假工具（返回固定证据） |
| 观测 | `agentix/agent/observer/observer.py` | Append-only Observation |
| 验证 | `agentix/policies/verifier/layered.py` | L1 结构 + L2 completion（L3 可选） |
| 记忆 | `agentix/agent/memory/service.py` | 写入 task scope，读时脱敏 |
| HITL | `agentix/policies/hitl/sidecar.py` | 高风险工具走 Approve/Reject |
| 事件 | `agentix/agent/events/adapter.py` | 模拟外部事件唤醒 |
| 轨迹 | `agentix/application/services/trajectory.py` | 首错步归因 |

## 2. 最小可运行片段（概念）

```python
from agentix.domain.agent.types import TaskSpec
from agentix.policies.planner.simple import DeclaredStepsPlanner  # 或 SingleStepPlanner
from agentix.policies.verifier.layered import LayeredVerifier

task = TaskSpec(
    goal="梳理数据条目分类并引用依据",
    task_id="demo-research-1",
    success_criteria=("contains_structure", "has_sources"),
)
# planner/executor 由测试桩注入；详见 tests/test_agent_core_loop.py
```

## 3. 验收对照

- 主循环与四层故障：`tests/test_agent_core_loop.py`、`tests/test_agent_replanner.py`
- 分层验证：`tests/test_agent_phase7_verifier.py`
- HITL 隔离：`tests/test_agent_phase8_hitl.py`
- 事件去重/双时钟：`tests/test_agent_phase9_events.py`
- 信息增量与失败模式：`tests/test_agent_phase11_multiagent.py`

## 4. 与线上关系

Research Demo **不**新增 HTTP 路由；线上等价路径为既有
`interfaces` → `application.services` → 检索/图 链路。Agent Core 新循环
通过 Protocol（`agentix/domain/agent/ports.py`）与 runtime 桥接，
物理收口见 Phase 14。
