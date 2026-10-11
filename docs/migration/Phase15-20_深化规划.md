# Phase 15–20 · Agent Core 深化规划（v1.0）

- 日期：2026-09-24
- 前置：Phase 0–14 已收口（勾选真源 `module-inventory.md` §6）；立项时基线 **1982 passed / 4 skipped** → 现 **2463 passed / 14 skipped**（2026-09-28，含接口审查收尾批次 + 接口审查批次（前后端同步）+ Phase 21 + Phase 22 + Phase 23 + Phase 24 + Phase 25 + Phase 26 + Phase 27 死代码清理 + 审计批次 A 安全收口 + Phase 28 自主护栏 + Phase 31 评估二期 + Phase 30 进化闭环 + Phase 29 记忆结构化）
- 依据：《Agent 全书笔记》10 章 + Claude Code 公开架构分析（arxiv 2604.14228）+ Codex agent loop 公开资料
- 模式：Strangler 不变；LangGraph 仅 `agentix/agent/graph/`；domain 零第三方
- 本批执行范围（已拍板）：**Phase 15 全 + Phase 17 离线可做部分**；16/18/19/20 挂起待批

---

## 0. 对照结论（为何需要 Phase 15+）

| 维度 | 书 / Claude Code / Codex | My-Agentix 现状（Phase 0–14 后） |
|------|--------------------------|----------------------------------|
| 核心循环 | 简单 while-loop；复杂度在环外 | `AgentLoop` 完整且测试绿，**生产零调用** |
| 上下文 | 分层降级压缩；稳定前缀 = 架构约束 | 仅尾部截断；无 Snip/Auto-compact 分层 |
| 权限 / HITL | deny-first 多模式；审批 = 正常控制流 | **三套 HITL 互不连通**（Gate / Approval / Sidecar） |
| 扩展 | Skills 渐进披露 + Hooks 生命周期 | 有 MCP/工具选择；无 Skills/Hooks 等价物 |
| 子代理 | 隔离上下文，只回摘要给父 | Manager/Peer 测通，无生产拓扑 |
| 评估 | 确定性验证器优先；baseline 可阻断 | 机制齐，**`baseline_predictions.jsonl` 不存在、`quality.json={}`** |
| 进化 | 双循环 + 三防线 | 双循环测通，**无生产信号源**（`OnlineLoopRecorder` 为孤岛） |
| multi_agent 消融 | — | E/F/G 被 `run_ablation` 诚实拒绝（UNWIRED） |

**一句话结论**：Capabilities/Runtime 已生产化；Harness 与评估/进化处于「已建成、未点亮」。

---

## 1. 设计原则（三源交叉，执行约束）

1. **Strangler 不变**：先挂支路，不推翻 L2 LangGraph 主图（`lg_builder`）。
2. **点亮优先于扩张**：接线已测通模块 > 再写新子系统。
3. **确定性验证器优先**；最终文本不作唯一事实源。
4. **隔离优于压缩**；子代理只回摘要。
5. **稳定前缀 + Append-only** 作缓存与审计硬约束。
6. **HITL = 正常控制流**，非异常。
7. **评估可阻断才算完成**；未跑 baseline 的评估不算交付；**拒绝手工伪造 baseline**。

---

## 2. Phase 总览与依赖

| Phase | 主题 | 状态 | 依赖 |
|-------|------|------|------|
| **15** | 点亮 Harness（AgentLoop 接生产 ask 支路） | **本批执行** | — |
| **16** | HITL 三栈统一 | 挂起待批 | 建议 15 后 |
| **17** | 评估武装 + 轨迹/在线信号回流 | **本批执行离线部分** | record 需全栈机 |
| **18** | 上下文分层（对齐 Claude Code/Codex） | 挂起待批 | 可与 15–17 并行 |
| **19** | multi_agent 接线（supervisor 拓扑） | 挂起待批 | **依赖 15** |
| **20** | 进化 + 事件点亮 | 挂起待批 | 依赖 17 回归门 + 15 轨迹 |

```
Phase 15 ──┬──► Phase 16 HITL 统一
           ├──► Phase 17 评估武装（离线部分可并行）
           └──► Phase 19 multi_agent（依赖 15）
Phase 18 上下文工程（相对独立）
Phase 20 进化+事件（依赖 17 门 + 15 轨迹）
```

---

## 3. Phase 15 — 点亮 Harness（本批全量）

### 3.1 目标

消灭「测通但死代码」：让 `agentix/agent/` 至少一条真实生产支路存在；**flag 默认关，生产默认仍 L2，flag-off 行为字节等价**。

### 3.2 已拍板决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| stream 分支（`chat_service.py:1006`） | **A：stream 仍走 L2，flag 只 gate `ask`** | AgentLoop 无流式 API；合成伪事件 = 假 token 流，不诚实 |
| flag 名 / 默认值 | `AGENT_LOOP_ENABLED: bool = False` | 照抄 `TEXT2SQL_ENABLED` 模式（settings + selfcheck + `.env.example` 三处同步，p35 守卫） |
| L3 流量类 | 仅非流式 `ask`；生产默认 L2 | Strangler 最小扰动 |
| 作用域 | 同一 `_AGENT_SEM` / `AGENT_TIMEOUT` / `runtime.astep` 包裹 L3 分支 | 并发核算与控制面可见性不回退 |
| 预算 | 复用 `MAX_ITERATIONS` / `AGENT_TIMEOUT` | 不发明平行预算 |

### 3.3 改动清单

| # | 文件 | 改动 |
|---|------|------|
| 15.1 | `agentix/config/settings.py`（~Agent behaviour 块） | `AGENT_LOOP_ENABLED: bool = Field(default=False, ...)` |
| 15.2 | `agentix/config/selfcheck.py`（flag 列表） | 注册 `"AGENT_LOOP_ENABLED"` |
| 15.3 | `.env.example` | `AGENT_LOOP_ENABLED=false` |
| 15.4 | **新** `agentix/application/services/agent_loop_adapter.py` | ① 工具桥：KG/KB 可调用 → `fn(task=, step=, context=)` kwargs 适配；② `TaskSpec` 构建器；③ `LoopResult` → 响应 dict（`messages`/`router`/`sources`/`degraded`）；④ `result.observations` → `TrajectoryRecorder.record` + `record_tool` |
| 15.5 | `chat_service.py` `_ask_impl`（~533–546） | flag 开：同包裹内 `loop.run(task)` + 结果映射；关：原路径零变化 |
| 15.6 | `chat_service.py` `_stream_events_impl`（~1006） | **不改行为**（决策 A）；仅注释说明 stream 仍 L2 |
| 15.7 | verifier route | 复用 `router.route_query` 先取 route 喂 `LayeredVerifier`；失败则 `route=None` 降级 |
| 15.8 | `agentix/agent/graph/` | 最小适配器（仅此子包可碰 langgraph）；本批若 ask 支路不直接依赖 LangGraph 可保持占位 |
| 15.9 | tests | flag-off 等价 + flag-on smoke（`SingleStepPlanner` + stub 工具）+ LangGraph 隔离守卫仍绿 |

### 3.4 构造与调用锚点（调研已核实）

```python
# agentix/agent/core/loop.py
AgentLoop(planner=..., context=..., executor=MappingStepExecutor(tools=...),
          verifier=LayeredVerifier(...), max_steps=settings.MAX_ITERATIONS, ...)
result: LoopResult = await loop.run(task)  # 永不抛，终态 completed|failed|budget_exceeded|verify_failed

# TaskSpec — domain/agent/types.py（frozen，零第三方；M6 后旧路径 domain/agent_types.py 已删）
# MappingStepExecutor — tools 注册为 fn(task=, step=, context=)
# TrajectoryRecorder.record — application/services/trajectory.py（自吞异常）
```

### 3.5 Gotchas（执行时必须遵守）

1. **工具签名不匹配**：KG/KB 可调用吃 LangGraph state dict，必须 adapter 转换。
2. **`get_active_tools()` 返回 schema 不是 callable**；可调用在 node factory 闭包里 → adapter 需显式注册表或惰性封装。
3. **LangGraph import 禁令**：`agentix/agent/**` 仅 `agent/graph/` 允许（`test_agent_core_loop` 静态守卫）；application 侧可用。
4. **结果形状**：下游 `chat_service.py:566–608` 期望 dict；adapter 必须映射。
5. **flag 三处同步**（settings/selfcheck/`.env.example`）否则 p35 红。
6. **DI 惰性**：AgentLoop 构造不得进 import-time；仿 `graph_getter` 惰性。
7. **ablation twin（可选）**：仅当分支合并后把 `agent_loop` 加进 `FLAG_DEFAULTS` 并标 WIRED；本批若加必须默认 False 且读侧存在。

### 3.6 Phase 15 DoD

- [x] flag 默认 False；flag-off 与基线行为等价（回归测试）
- [x] flag-on `ask` smoke：SingleStepPlanner + stub 工具端到端终态
- [x] 轨迹桥：`LoopResult.observations` 进 `TrajectoryRecorder`
- [x] p35 / p17 / p22 / code_hygiene + ruff + mypy + 全量 pytest 绿
- [x] LangGraph 隔离守卫绿
- [x] stream 路径零行为变化（决策 A，注释已标）
- [x] 文档：本文件 + module-inventory §6 勾选 + CHANGELOG

---

## 4. Phase 17 — 评估武装（本批仅离线可做部分）

### 4.1 机制结论（调研已核实）

- 门禁是 **数据门**，非开关门：`quality_gate.py` 按 baseline 是否含 metric key 决定 NOT-ENFORCED / 建档 / PASS / FAIL。
- CI `eval-offline`：缺 `evals/baseline_predictions.jsonl` → 整步跳过（大声 warning）。
- CI 自己的建档写回 **随 runner 丢失**；必须**本地播种并 commit** `quality.json`（5 个 key：`route_accuracy`, `citation_hit_rate_applicable`, `citation_correctness_applicable`, `must_contain_applicable`, `reject_accuracy_applicable`）。
- 真实 record **需全栈机**（LM Studio/OpenAI 兼容端点 + Docker Redis/Neo4j/Milvus + keys）；**无 fixture 路径**；**拒绝手工伪造**。
- 轨迹：`TRAJECTORY_SINK=jsonl` 机制已完备但默认 `memory` 未开；导出到 `evals/baselines/` 会与 `quality.json` 碰撞（`load_packages` 见无 `steps` 即 raise）→ 导出须用 **`evals/baselines/traces/` 子目录**。
- `OnlineLoopRecorder`（`agent/evolution/loop.py`）零写契约，生产零调用 → 接 `chat_service` 三处 `record_request` 漏斗。

### 4.2 改动清单（本批）

| # | 文件 | 改动 | DoD |
|---|------|------|-----|
| 17.1 | `chat_service.py` 三处漏斗（success ~627 / agent error ~681 / internal error ~712） | 调 `OnlineLoopRecorder.record(trace_id, success, first_error_stage, cost_usd, tags)` | 岛变有源；零写不变量保持（不传 patch/payload/apply） |
| 17.2 | `evals/trajectory_regression.py` 或文档/辅助 | 导出目标改为 `evals/baselines/traces/` 子目录约定；避免 quality.json 碰撞 | 机制测试绿 |
| 17.3 | `evals/README.md` §9、`docs/architecture/runtime-guardrails.md` §12.5、`docs/guides/testing.md` §4 | 写明武装顺序：全栈 `--record` → offline score → 本地五次 `quality_gate.py` 播种 → **手动 commit** `quality.json`；CI 不会替你持久化建档 | 文档与实况一致 |
| 17.4 | （可选，时间允许） | CI 增 trajectory 步骤（合成包，明示不守真值） | 不强制本批 |

### 4.3 本批明确不做（**blocked**：需全栈机器，另期）

> 2026-09-26 实况：本机无 Docker / LM Studio / Redis / Neo4j / Milvus → 全栈 record **blocked**，
> 诚实标注，**拒绝伪造任何 baseline**。Phase 20 进化 runner 对缺基线门判 `not_enforced` 且 **fail-closed（≠通过）**。

- [ ] `run_eval.py --record` → 提交 `baseline_predictions.jsonl`
- [ ] 本地播种并提交非空 `quality.json`（五个 metric 真值）
- [ ] 真实生产轨迹回流 + 真前缀回归 baseline
- [x] LLM-judge —— **已于 Phase 26 去 stub**（真实端点出分 + `judge_model` 标注 + 诚实失败，默认关不进门禁；见 `Phase21-26_规划.md` §3 Phase 26 落地注）
- [ ] 手工伪造任何 baseline 文件

### 4.4 Phase 17 离线部分 DoD

- [x] `OnlineLoopRecorder` 三漏斗有调用；单测证明 success/error 路径均 record
- [x] 导出路径约定无 quality.json 碰撞
- [x] 文档武装顺序与 CI 行为一致
- [x] 守卫 + ruff + mypy + 全量 pytest 绿

---

## 5. Phase 16 — HITL 三栈统一（**完成** 2026-09-26，库级桥）

**目标**：ReviewerSidecar / HumanReviewGate / approval+breakpoint 连成一条线。

```
ToolExecutor.execute
  → HumanReviewGate（风险有效级）
    → ReviewerSidecar（结构化审面 Approve/Reject/Edit/Resume）
      → 挂起 AgentStatus.WAITING_APPROVAL + breakpoint_resume
      → 恢复：Sidecar._pending；编辑后合并 arguments（approver fail-closed）
```

### 5.1 Phase 16 DoD（完成）

- [x] 默认关：`HUMAN_REVIEW_ENABLED=false` / hook=None ⇒ executor 零变化（flag-off 字节等价）
- [x] 开启 fail-closed：拦下必挂 `ReviewerSidecar`；approver 否决 ⇒ REJECT 落回闸门
- [x] EDIT×approver / 重启恢复 E2E：编辑合并 + digest/risk 同式重算；`to_persistence`→`from_persistence` 后裁决仍落闸门
- [x] 误拒可观测：`stats()` 拦截数 / 裁决分布 / `rejection_rate`
- [x] Permission modes（default / accept_edits / bypass）收敛到 Gate 配置面（`hitl_bridge.PermissionMode`）
- [x] 组合根接线：`main.py` 仅在 `HUMAN_REVIEW_ENABLED=true` 时 `install_executor_hook()`
- [x] `tests/test_agent_phase16_hitl_bridge.py` 27 用例 + 守卫（p17/p22/p35/p41/code_hygiene）+ ruff 全绿
- [ ] 图内 `interrupt()`：**另期**（单列评估，对照 runtime-guardrails §8）

## 6. Phase 18 — 上下文分层（**完成** 2026-09-26）

| 层 | 借鉴 | 落点 |
|----|------|------|
| L1 Budget | 大工具输出裁剪 | result_limiter → 主循环 |
| L2 Snip | 时间深度裁剪 | ContextEngine 轨迹按龄/重要性 |
| L3 Isolation | 子代理只回摘要 | Manager 结果面摘要协议 |
| L4 Auto-compact | 语义压缩（最后手段） | 可选，默认关 |
| 稳定前缀 | Codex 缓存纪律 | 静态 prefix + 工具序稳定；配置变更 append 消息 |

- 状态栏白名单补「不可模型自写」断言。
- 指令分层（CLAUDE.md/AGENTS.md 等价）：settings → 项目 → 会话。
- Skills/渐进披露、Hooks 生命周期（5–8 类）作增量可选项。

### 6.1 Phase 18 DoD（完成）

- [x] L1 Budget：executor `_limit_result` 接 `result_limiter`（仅 `truncated`/`has_more` 改写 content 并记 `meta.limited`；未触限字节不变；capabilities 不可用时降级无界）
- [x] L2 Snip：`ContextEngine(trim_policy=)` tail（默认，保前缀丢最新）/ head（按龄丢最旧）；非法值 `ValueError`
- [x] L3 Isolation：`ManagerConfig.max_result_chars=4000`（≤0 关闭）+ `bound_result_face()` 有界截断显式注明
- [x] 状态栏白名单：`STATUS_BAR_TRUSTED_KEYS` 过滤（保插入序）+ `rules` 注入
- [x] 指令分层：settings → 项目 `AGENTS.md`/`CLAUDE.md`（cwd 向上查找，`stop` 边界）→ 会话；`build_instructions` → `SimpleContextBuilder(rules=)` 组合根接线
- [x] `tests/test_agent_phase18_context_layering.py` 37 用例 + 守卫 + ruff 绿
- [x] L4 Auto-compact 语义压缩 —— **已于 Phase 22 完成**（`CONTEXT_AUTOCOMPACT` 默认关，五要素机制断言，见 `Phase21-26_规划.md` §3）
- [x] 工具序稳定 / 配置变更 append 消息（缓存纪律）—— **已于 Phase 22 完成**（`ToolRegistry.freeze()` + `config_watch.system_notice`）
- [x] Skills/渐进披露、Hooks 生命周期 —— **已于 Phase 21 完成**（`SKILLS_ENABLED` / `HOOKS_ENABLED` 默认关）

## 7. Phase 19 — multi_agent 接线（**完成** 2026-09-26，依赖 15）

1. [x] `agent/graph/` 可切换 supervisor 拓扑（planner + peers），包装既有 Manager/Peer。
2. [x] 图选择放惰性 `graph_getter`。
3. [x] `multi_agent`：UNWIRED → WIRED（写 file:line）。
4. [x] `variants.json` wiring_status + `test_ablation_wiring` 翻转。
5. [x] 默认拓扑仍扁平；flag 关 = 零变化（`AGENTIX_ABLATION_MULTI_AGENT` 消融开关）。
6. [x] 信息增量/六失败模式指标进离线聚合（meta → run_eval 摘要 → Benchmark 附录）。

> 验收：`tests/test_agent_phase19_multiagent.py`（全离线，无 LLM/Redis/Neo4j/Docker）。

## 8. Phase 20 — 进化 + 事件点亮（**完成** 2026-09-26；Redis Stream 另期）

1. [x] `chat_service` 结束时 success/cost/first_error_stage → `OnlineLoopRecorder`（**17.1 已做本批**）。
2. [x] `EventPort` 生产实现：**JSONL**（`EVENT_PORT_BACKEND`，默认 `memory`；三处同步 settings/selfcheck/.env.example）替代纯 in-memory；webhook `POST /api/v1/events` + `wait_external_event` 挂 `WAITING_EXTERNAL_EVENT`。**Redis Stream 另期**（不加 path 键，模块常量 `DEFAULT_EVENT_LOG_PATH`）。
3. [x] 离线：`evals/evolution_run.py` 轨迹聚合 → 提案 → 回归（接 Phase 17 门）→ staging → canary；三防线保持（verifier 受保护路径 propose 即拒 / OnlineLoopRecorder 零写 / 缺基线 `not_enforced` fail-closed）。
4. [x] Evolution 的 `golden_prefix`/`citation` 门与 evals 同名检查对齐（`GATE_CHECKERS`，未接入门 `not_wired` fail-closed）。

> 验收：`tests/test_agent_phase20_events_evolution.py` 31 用例。

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 接线改变现网行为 | flag 默认关；flag-off 等价测试；Strangler 零行为变化 |
| 假流式误导用户 | 决策 A：stream 不碰 AgentLoop |
| 工具签名/state dict 撕裂 | 强制 adapter；参数保真测试 |
| 伪造 baseline 污染门禁 | 明确拒绝；文档只指向真机 record |
| quality.json 导出碰撞 | 强制 `traces/` 子目录 |
| OnlineLoopRecorder 违反零写 | 只 record 聚合信号，不传任何写能力参数 |
| 三套 HITL 复杂度 | Phase 16 先库级桥；图 interrupt 单列 |
| 改主拓扑 | multi_agent 仅 flag；默认扁平 |
| 缓存被配置打穿 | Phase 18：工具序稳定、配置 append 消息 |

---

## 10. 每 Phase 交付物模板

1. 代码 + 默认关的 feature flag（如适用）
2. 至少 1 条 E2E/守卫回归
3. 文档：本规划勾选 / `module-inventory.md` §6 / interfaces 或 layers 如触达
4. `CHANGELOG.md` Unreleased 条目
5. 全量 pytest + ruff + mypy（触及前端则 vue-tsc）
6. 数字若变：README §11 日期格式 `YYYY-MM-DD` + 同步 `current-tree` / `dependency-map` / 规划 §9 基线

### 本批验证命令

```powershell
& ".\.venv\python.exe" -m pytest tests/test_p35_config_governance.py tests/test_p17_domain_layer.py tests/test_p22_obs_scope.py tests/test_code_hygiene.py -q
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest --select F401,F811,F841
& ".\.venv\python.exe" -m mypy agentix/domain --config-file=pyproject.toml
& ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
```

---

## 11. 执行状态

| 项 | 状态 |
|----|------|
| 本规划文档落盘 | 2026-09-24 v1.0 |
| Phase 15 | **完成**（2026-09-24）— flag 三处同步 + adapter + ask 支路 + stream 注释 + 18 用例 |
| Phase 16 | **完成**（2026-09-26）— `hitl_bridge` 库级桥 + executor hook + `main.py` flag 接线 + 27 用例 |
| Phase 17 离线部分 | **完成**（2026-09-24）— OnlineLoopRecorder 三漏斗 + traces/ 导出约定 + 武装顺序文档 |
| Phase 17 全栈部分 | **blocked**（2026-09-26）— 本机无 Docker/LM Studio/中间件；需全栈 record 后手动播种 quality.json；**拒绝伪造 baseline**；runner 缺基线 `not_enforced` fail-closed |
| Phase 18 | **完成**（2026-09-26）— L1 limiter / L2 trim_policy / L3 bound_result_face / 白名单 + 指令分层 + 37 用例 |
| Phase 19 | **完成**（2026-09-26）— supervisor 图切换 + ablation 开关 + wiring 翻转 + 指标流 + 离线验收 |
| Phase 20 | **完成**（2026-09-26）— `EVENT_PORT_BACKEND`（memory/jsonl）+ webhook + `wait_external_event` + `evolution_run.py` + 门对齐 + 31 用例；Redis Stream 另期 |

本批验证（2026-09-28 接口审查批次后复跑）：全量 **2303 passed / 14 skipped / 0 deselected**（`-m "not integration"`；skip 含 10 条 Redis Stream 契约用例无 Redis 时诚实跳过；原 2 条 `integration` 用例已随 `test_chat_api.py` 重写清零）；守卫 p17/p22/p35/p41/code_hygiene 全绿；`ruff` 主检 + 严格 select 全过；`mypy agentix/domain` 0 错；`vue-tsc` 0 错 + vitest **113** 全绿（本批 +20 契约测试：chat SSE 解析 7 + knowledge SSE 读取 5 + parseHttpError 6 + ChatMessageList 进度条 2）。Phase 17 全栈 record **blocked**（无 Docker/LM Studio，拒伪造 baseline）。

（Phase 27 终值存档：2026-09-27 全量 2291 passed / 14 skipped / 2 deselected，vitest 93。）
