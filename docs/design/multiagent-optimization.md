# Multi-Agent 深度优化设计（Phase 34）

> 总纲约束见 `docs/migration/Phase32-34_Memory-RAG-MultiAgent优化总纲.md` §0–§5。上位定位：**Multi-Agent 是 Orchestrator 的一种 Strategy，不是第二套 Runtime；LangGraph 是编排适配实现**。论证基座见 `docs/design/orchestration-rationale.md`、分层见 `docs/architecture/agent-core-layers.md`、研读见 `docs/source-code-study.md` §2–§5。
>
> **状态（2026-10-01 代码核对，2026-10-03 Stage F 执行更新；执行蓝图见 `docs/migration/架构重构_统一执行链方案_v2.md` §4）**：
> 已完成 ✓：A-P1-1（任务型路由）、A-P1-5（自愈三计数器，落 guardrails.py）、A-P2-4（计划模式）、A-P1-4（F1e 已落 HITL 进环，见 §5 注记）、A-P2-2（**F2c-a 已落**：退出契约 `ManagerOutcome.exit_status/exit_reason/truncated` 复用 `domain/runtime/lifecycle` 词表 + 六失败模式打点 `multiagent_failure_mode_total`，黑板语义既有，见 §6 注记）、A-P2-1（**F2c-b 已落**：隔离三件套 flag `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` 默认关，见 §6 注记）、A-P0-1/2/3/4/5（v2 Stage A1/A2/A3 已落：单 owner+流式登记+cancel+退出契约 / Tool Gateway 三闸+旁路收编 / 图内规划收敛）、cron 另期（P5）。部分完成：A-P1-2（F1c 已落 LLM 规划 + TodoWrite，见 §5 注记；余「按任务类型」白名单分型细化）、A-P1-3（task waves 并行已有，AgentLoop 轮内仍串行——F1d 已落 L2/L3 并发工具轮，见 §5 注记）。待实施：无（A-P2-3 看板/A2A 按 D5 另期留口）。

---

## 1. 现状实测（2026-10-01 代码级）

- Harness 主循环：`agentix/orchestration/orchestrator.py`（`AgentLoop`：Task→Context→Plan→Execute→Observe→Verify→Final，全终态）+ `agentix/policies/planner/simple.py`（`SingleStep/DeclaredSteps` 零 LLM）+ `agentix/policies/planner/retry.py`（`RetryReplanner`，streak 熔断）+ `agentix/policies/verifier/layered.py`（L1 结构→L2 完成→L3 接地短路）组件齐全，但生产 `agentix/application/services/agent_loop_adapter.py` 硬连 `SingleStep` 回显工具、且流式问答旁路整条 Harness（`agentix/application/services/chat_service.py` 中 `AGENT_LOOP_ENABLED` 默认关、`AGENT_MAX_REPLANS=0`）。
- 生产编排：`agentix/orchestration/langgraph/lg_builder.py`（扁平 9 节点）+ `agentix/orchestration/langgraph/lg_supervisor.py`（supervisor 拓扑，`_SUPERVISOR_MAX_ROUNDS=3`）+ 顶层路由 `agentix/application/agents/router.py`（LLM + 启发式双保险，`route_query` 白名单全问答/检索）；KG/检索子图 `agentix/application/agents/kg_sub_graph/...`（planner / tool_selection / multi_tool / kb_workflow）各自带规划。
- 多智能体件：`agentix/orchestration/strategies/multi_agent/peer.py`（角色 + 隔离轨迹 + 结构化消息）+ `agentix/orchestration/strategies/multi_agent/manager.py`（六失败模式检测优先级 + `PeerBudget` + 黑板冲突标记不合并）+ `agentix/orchestration/strategies/multi_agent/bus.py`（`InProcessBus`，`MULTIAGENT_BUS_ENABLED` 默认关）；`Runtime` 真源 `agentix/runtime/`（state/lifecycle/budget/steps）+ `agentix/application/runtime/` Strangler shim 已删除（A4 直删）；HITL `agentix/policies/hitl/sidecar.py`（结构化参数评审 + fail-closed）为实例化门，无 `interrupt()` 中途拦截。
- 六缺口（vs Codex/Claude/Hermes/智谱）：① 任务执行无 C 位（工具全只读 L0，路由无任务型）；② 规划器 stub（无 LLM 规划/TodoWrite——F1c 已补，见 §5 A-P1-2 注记）；③ 工具轮严格串行（`_next_pending`，无并发批——F1d 已补，见 §5 A-P1-3 注记）；④ 无退出契约（缺 `status/exit_reason/truncated`）；⑤ 安全门在实例化不在环内（无中途拦截——F1e 已补，见 §5 A-P1-4 注记）；⑥ 多智能体灰度（supervisor + bus 默认关，bus 启用路径从未生产验证）。

---

## 2. 对标结论（搬什么）

| 产品 | 可搬设计 | 落点 |
|---|---|---|
| Codex | Agent 按类型工具白名单（Explore 无写）+ 独立 system prompt + 后台线程 + 迭代上限 32；`Guardian` LLM 安全分类 + OS 沙箱；工具失败降级为 `is_error` tool_result 回喂不打断循环；计划模式 = 可回滚权限切换；TodoWrite 持久化 | 子代理白名单 + Guardian 门 + 失败回喂 + Plan 模式 + Todo |
| Claude Code | Subagent 独立上下文、主会话只回终讯息 + metadata；可嵌套 5 层、动态工作流可编排数十上百后台 agent；Hooks（PreToolUse exit 2 拦截是强制）；Skills 按需加载零常驻成本 | 隔离 + 只回摘要 + Hook 强制 + 按需技能 |
| Hermes | `delegate` 完整隔离（`ephemeral_prompt/skip_memory/独立 session_db+budget/weakref/心跳`）+ 明确契约 `status/exit_reason/truncated` + live registry 可 kill/pause/steer；阶段管线决策对象（continue/break/return）；三计数器自愈 + 结果 stub 化；`jobs.py/scheduler.py` 分半（本期只留口） | 隔离三件套 + 退出契约 + 决策对象 + 自愈 |
| 智谱系 | 多智能体分工（检索/分析/执行角色）+ 协作看板 + 分级审批 | Manager 分工 + 看板（P2）+ L1+ 审批 |

---

## 3. 目标架构（Strategy 化）

```
Orchestrator（唯一控制流，见总纲 §1）
├── Strategy: SingleStep（简单问答 fast-path，保留）
├── Strategy: Retrieval / Research / SQL（原 L2 subgraph 降格，不再拥有生命周期）
└── Strategy: MultiAgent（真需多自主角色时启用）
     ├── Manager（任务分解→派发→黑板融合→冲突标记→验收）
     ├── Worker = 完整子 Agent 实例（隔离三件套 + 独立 budget + 白名单工具）
     ├── 共享：同一 Planner / Verifier / Tool Gateway / Memory 快照 / RAG 工具
     └── 退出契约：status / exit_reason / truncated（全链统一，不止子代理）
```

约束：Capability 不得反向依赖 Orchestrator；子代理只见任务面（黑板 + 结构化消息），不见父轨迹全文；主上下文只收子代理终讯息 + metadata（沿 Claude 隔离语义）。

---

## 4. P0 单流收敛（先消双推进）

- A-P0-1 Owner 唯一：同一 run_id 只允许一个 Orchestrator（Core Loop 与 LangGraph 图二选一，另者只做 adapter 机制）；并行推进同一 Run 必须拒绝并打点（总纲守卫 1/4 的测试落点）。
- A-P0-2 路由瘦身：L1 只做能力域选择不执行工具；KG/检索子图内的私自规划收敛为统一 Planner 调用（子图 planner 节点改为 Strategy 内步骤，不再各自发明 Plan 模型）。
- A-P0-3 退出契约先行：所有执行单元统一 `status ∈ {completed, interrupted, failed}` / `exit_reason ∈ {completed, max_iterations, interrupted, error}` / `truncated == (exit_reason == max_iterations)`（沿 Hermes 语义；先契约后功能，保证可 killed、可恢复、可评测）。
- A-P0-4 取消语义：API cancel → Runtime → Orchestrator → Tool Gateway 全链打断，终态必达 CANCELLED/明确终态；`ControlPlane` 登记 + `InProcessBus.cancel` 语义对齐（总纲守卫 8）。
- A-P0-5 生产工具调用归位：问答/任务生产路径一律经 Tool Gateway 治理链（收敛 `agent_loop_adapter` 旁路，为 P1 并发轮铺路）。

---

## 5. P1 规划与执行（任务 C 位）

- A-P1-1 任务型路由：新增任务执行路由（与问答/检索并列，灰度 flag），任务终态建模（沿用 A-P0-3 契约 + Todo 持久化）；问答链保持 L2 图不变，互不干扰。
- A-P1-2 LLM 规划器：Plan 工具 + TodoWrite（`pending/in_progress/completed`，持久化可恢复）+ 按任务类型白名单；`DeclaredSteps` 保留为确定性 fast-path；`RetryReplanner` 生产接线（`AGENT_MAX_REPLANS>0` 灰度）+ 失败回喂（`is_error` tool_result 不打断循环，沿 Codex 语义）。【F1c 已落地（2026-10-03，flag `AGENT_LLM_PLANNER_ENABLED` 默认关）：`LLMPlanner`（`agentix/policies/planner/llm.py`，`with_structured_output` temperature=0 + 执行表白名单整步过滤 + 步数封顶 `MAX_ITERATIONS` + 任何失败 fail-open 回 `SingleStepPlanner` 语义）经 `agent_loop_adapter._select_planner` 注入，`AGENT_MAX_REPLANS>0` 时同时进 `RetryReplanner` 内层（flag-off 分支与基线逐位等价）；`todo_write` 工具（整体替换、至多一条 `in_progress`、`TodoStore` JSON 落盘可恢复、回执即读通道；声明 L2+control，`registry_init` 与 `main.py` 成对 flag 门控）；「Plan 工具」= 既有 `enter/exit_plan_mode`（A-P2-4 已完成，无需重复立项）；白名单现为执行表键交集注入（`allowed_tools` 入口），按任务类型分型细化待 F2；规划提示可携带当前 Todo 快照（`todo_provider` 注入）；守卫 `tests/test_p79_multiagent_p1_llm_plan.py`】
- A-P1-3 并发工具轮：同轮多 tool_use budgeted batch 并发（授权门 + 结果落库 + 家政工具不计进度，沿 Hermes `turn_tool_round/tool_executor` 语义）；串行保持为可配（危险工具强制串行）。【F1d 已落地（2026-10-03，flag `AGENT_TOOL_ROUND_CONCURRENCY` 默认 1 = 串行逐位等价）：一把旋钮共管两面——L2 检索聊天 `chat_agent_runner._execute_tool_calls` 与 L3 `AgentLoop` 批路径（`round_concurrency` 组合根注入，`orchestrator` 保持不 import config）；派发前逐调用授权门 `tools/gateway/round_gate.py::preflight_block`（执行点语义 `consume=True`，HUMAN_REVIEW 关闭零副作用恒放行、未获批 fail-closed 落 error 不派发）+ 危险判定 `is_dangerous`（有效风险 MEDIUM+ = L1+ ⇒ 强制串行，绝不与同批任何调用并发）；批路径顺序 = 授权门 → 危险串行 → 剩余步数预算裁剪（G7 并发不放宽 `max_steps`）→ 批前一次上下文重建 → `Semaphore(N)` gather 异常按单步同款分类 → 观测按声明序落位 + `steps_used` 单调 + `ON_STEP_END` → 每批至多一次 replan；L2 结果按输入序组装（ToolMessage 落位顺序 = 输入顺序，事件序不变）；「家政工具不计进度」留口未写码（当前两面在途工具全为检索/act，无家政在途；机制归 F2）；`retrieval→tools` 惰性 import 新边（R6 已知，沿 `orchestration/executor.py` result_limiter 先例）；flag-off 单步路径代码逐字不动（守 `test_p52` G6 / p74 事件序）；守卫 `tests/test_p80_*.py`】
- A-P1-4 HITL 进环：高风险（L1+）走 `interrupt()` 中途拦截 + checkpointer 恢复（替代纯实例化门）；无 approver fail-closed（沿 `sidecar.py` 语义）；固定裁决 `deny → hook → ask强制 → allow`（沿 Codex 顺序）。【F1e 已落地（2026-10-03，flag `HITL_IN_LOOP_ENABLED` 默认 False = 与基线逐位等价，父闸 `HUMAN_REVIEW_ENABLED`）】：三子句全落——① 进环挂起：`lg_builder` 在 `add_node("execute_task")` 处包注入壳（langgraph 依赖留 p66 白名单文件；四条件 = py3.12+ × 本 flag × 父闸 × 非 stream ask 声明的 `configurable.hitl_in_loop` 意图，stream 恒 L2 永不声明），把 `langgraph.types.interrupt` 经 config 传 `task_handler → run_task_execution(review_interrupt=…)`；`task_execution` 在 `run_batch` **之外** 消费首个 `review_required`（`task_retry.run_task` 把 BaseException 转 outcome 会吞掉 GraphInterrupt，故中断必须在节点内、批外抛出），恢复值 `approve` ⇒ `gate.approve` 落批准后重试一次并把该步 outcome 换成成功，`reject`/未知值/批准失败 ⇒ 终止后续波次回 failed 终态（保持本函数「只返回 LoopResult 不抛」契约，fail-closed）；py3.10 async `interrupt` 必死锁（test_p32:429-451 特征化）⇒ 版本门控，3.10 诚实降级回 `review_required`（绝不注入绝不调用）；已知边界：LangGraph 节点重入 ⇒ 挂起点之前的步骤恢复后重执行（非幂等写步骤须调用方幂等化或规划为单步）。② 无 approver fail-closed 沿 sidecar 语义：闸开 + 无 approver + 无决策面 ⇒ executor `_blocked` 直接 `gate.reject` + `reason=no_approver_wired_fail_closed`（不留永远没人批的 PENDING）；有 HITL 桥决策面（`decision_surface_active()`）或 flag-off ⇒ 照旧 `review_required`。③ 裁决顺序 `deny → hook → ask强制 → allow` 契约锁定（前置风险/guardrail 闸 = deny，hook 可代批但代批仍须复过 ask，allow = 执行点 `consume` 消费批准；flag-off 路径与基线逐字未动，`_blocked` 对 flag-off 返回与原面量相同的结果）。恢复面 = `POST /ops/hitl/in-loop`（惰性调 `lg_builder.resume_in_loop` 的 `Command(resume=…)`，langgraph import 不出白名单）+ 单例 `decision_surface_active()` 读取点；守卫 `tests/test_p81_hitl_in_loop.py`（py3.10 = 34 passed + 1 skipped 真图 e2e，py3.12 = 35 passed 含挂起→Command 恢复真图 e2e）。】
- A-P1-5 自愈闭环：三计数器（同参重复/同工具异参/幂等哈希不变）分级 warn/block/halt + 工具专属恢复建议回喂；重复结果 stub 化（第 2 次字节级重复起，轮询不豁免）；mutation 成功清零（沿 Hermes `tool_guardrails` 语义）。

---

## 6. P2 协作（可选扩展）

- A-P2-1 子代理隔离三件套：`ephemeral_prompt / skip_memory / 独立 session + budget` + 父记忆快照只读拷贝 + 心跳保活 + live registry（kill/pause/steer）。**【F2c-b 已落地（2026-10-03，flag `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` 默认关 = supervisor 现行为逐位等价）】** 新模块 `orchestration/strategies/multi_agent/isolation.py`（纯基础设施零 I/O）：① `IsolationPolicy`（frozen）+ `build_task_face_prompt`——ephemeral 恒不拼父轨迹，只读快照段按 `memory_snapshot_readonly` 注入；② `take_memory_snapshot`——str 原样冻结、Mapping 深拷贝后 `MappingProxyType`（写入 TypeError，真只读）；③ `SubAgentSession`（session_id + 独立 `PeerBudget` 副本，`session_budgets` 经 Manager `budgets` 单源注入）；④ `LiveSubagentRegistry`——心跳保活（TTL 外 `is_alive=False`）、kill 终态短路（`session_gate` 返回 killed PeerResult，内层零执行，审计记录保留）、pause 有界等待（`wait_while_paused`：resume 继续 / kill 短路 / 超时 fail-open 放行绝不悬挂）、steer 一次性注入下轮 context 并落 `steered` 标记；⑤ `ephemeral_task_face_state`（messages 收敛最后一条，router 等任务态字段共享）+ `isolated_config`（摘除活 `memory_context` = skip_memory，唯一记忆入口 `memory_snapshot` 键）。生产接线 `lg_supervisor`（flag-on）：handler 子 agent 收任务面 state/config、Proposer/Reviewer 各持会话包装（`wrap_peer`：心跳/steer/快照段/kill 短路）、meta 仅 flag-on 追加 `isolation`（policy/sessions/snapshot_chars/registry）；flag-off 全部旁路逐位等价；配置键五处同步；守卫 `tests/test_p89_subagent_isolation.py` 47 条。
- A-P2-2 黑板融合：零增量消息 drop（仍审计留痕）、结果面截断显式标记、冲突只标记不合并（沿用 `manager.py` 现有语义并受 Runtime Budget 约束）；六失败模式（deadlock/livelock/echo/bleed/escalation/contradiction）检测保留并打点。**【F2c-a 已落地（2026-10-03，无 flag——正确性契约）】** ①退出契约：`ManagerOutcome` 增 `exit_status/exit_reason/truncated` 三字段，`__post_init__` 按 `reason` 推导（`derive_exit_contract`：converged→completed/completed，预算与轮次耗尽→failed/max_iterations+truncated，peer 取消→interrupted/interrupted，未接线/无进展/矛盾→failed/error），词表单源复用 `domain/runtime/lifecycle.py` 的 `ExitStatus/ExitReason`，并强制 `truncated == (exit_reason == max_iterations)` 契约等式；`lg_supervisor` meta 透传三键，Manager 异常路径诚实 `failed/error`；②打点：`multiagent_failure_mode_total{mode}` 计数（`failure_mode != NONE` 恰一次，NONE 不计），`metrics.record_multiagent_failure` 双分支（prometheus/noop）；③评测面零接触：`_aggregate_multi_agent` 只读既有键，meta 新增键不改聚合（守卫 `tests/test_p88_multiagent_exit_contract.py`，词表/推导表/等式/打点/透传/聚合六面钉死）。
- A-P2-3 看板/A2A：kanban（SQLite 看板）与 A2A 网关适配列为可选（默认关；智谱式协作看板需求出现时再启用）。
- A-P2-4 计划模式：可回滚的配置级权限切换（保存/恢复，沿 Codex `Enter/ExitPlanMode` 语义），与 `RollbackPort` 真实域实现联动。
- 另期（不在 34 内）：cron/proactive 调度器（jobs 存储 + tick 执行分半 + 自愈四件套 + delivery ledger，沿 Hermes cron 语义），只留接口口不展开。

---

## 7. 验收

- 架构守卫：单 owner 断言；ToolCall 必经 Gateway 断言；Capability 禁 import Orchestrator/Planner（AST 扫描沿现有分层守卫扩展）；在线禁 Evolution promotion；SSE 同源断言。
- 行为：任务完成率；`exit_reason` 分布；预算耗尽不悬挂；取消必达终态；高风险无 approver 必 REJECT（F1e 已落：flag-on 且无 approver 且无决策面 ⇒ REJECT o_approver_wired_fail_closed\，见 §5 A-P1-4 注记）；六失败模式各至少 1 条触发用例（沿用 `test_agent_phase11/19/25_multiagent` 扩展）。
- 评测（只增报告键）：`task_success / exit_reason_dist / budget_exhaust_rate / cancel_terminal_rate`；进 `evals/` 摘要，不进质量门禁。
- 灰度顺序：契约 → 单流 → 任务路由（影子）→ LLM 规划 → 并发轮 → HITL 中途（F1e ✓）→ 子代理隔离；每步 flag 独立，可单独回滚。
