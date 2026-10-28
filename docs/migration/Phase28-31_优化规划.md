# Phase 28–31 · 下一版优化迭代规划（v1.0）

> **文档状态**：历史 · 这是**当时**的规划或快照，**不代表当前实现**，不要按它判断现状。
> 文档分层：唯一现行总览是根 [README](../../README.md)，实现细节见 [PROJECT_SPEC](../项目实现手册.md)；在办的优化见 [优化路线图台账](../design/性能与优化路线图.md)。

- 版本：v1.0（依据《深入理解 AI Agent》全书笔记分章版 11 文件对照修订 + `docs/architecture/架构审计-2026-09-28.md` 分期方案）
- 日期：2026-09-28
- 前置：Phase 0–27 收口 + 接口审查批次完成（勾选真源 `模块清单.md` §6 / `Phase15-20_深化规划.md` §11 / `Phase21-26_规划.md` §7）；现基线 **2463 passed / 14 skipped / 0 deselected** + 前端 vitest **121**（2026-09-28 审计批次 A 安全收口 + Phase 28 自主护栏 + Phase 31 评估二期 + Phase 30 进化闭环 + Phase 29 记忆结构化 + 穿插批次 B（B1/B3/B4/B2）+ 批次 C（C1/C2/C3）完成后；套件规模唯一真源见 README §11）
- 模式：Strangler 不变；flag 默认关、flag-off 字节等价；domain 零第三方；LangGraph 仅 `agentix/agent/graph/`；不新增数据库 / 新框架 / 新中间件
- 范围决策（已拍板 2026-09-28）：**四主题 = Phase 28–31**；**审计批次 A（P0 安全）前置，B/C 随批穿插**；批一 = 审计 A + Phase 28 ∥ Phase 31，批二 = Phase 30，批三 = Phase 29；缺口 10–12 另期单列

---

## 0. 对照结论（书 × 现状 × 缺口）

> 上一版（Phase 21–26）主题是「补书中有、代码里没有的子系统」+ 评估门解阻；本版主题是「**补控制回路里的决策与评估能力**」+ 安全前置。

| # | 主题 | 书依据 | 现状证据（2026-09-28 核实） | 缺口 |
|---|------|--------|------------------------------|------|
| 1 | 需求澄清决策 | Ch9 §9.3.2 / Ch4 | `agentix/` + `evals/` grep `澄清\|clarif`：**零命中**；`route_query`（`application/agents/router.py:191`）固定分流 | 零覆盖 |
| 2 | 预算意识 | Ch10 §10.6 | `MAX_ITERATIONS=10`（`settings.py:603`）与 `AgentLoop.max_steps`（`agent/core/loop.py:89/145`）存在，但轨迹/前缀对剩余步数**不可见**；`STATUS_BAR_TRUSTED_KEYS`（`context/engine.py:17`）无预算键 | 弱覆盖 |
| 3 | 演化载体 KNOWLEDGE/PROGRAM | Ch9 §9.6–9.9 | `EvolutionCarrier`（`evolution/loop.py:34`）仅 PROMPT / TOOL_POLICY / MEMORY_CONSOLIDATION / VERIFIER_META 四值 | 缺 2 载体 |
| 4 | 提案落盘（保留失败） | Ch9 §9.8.2 | 提案仅内存 `self.proposals`（`loop.py:153`，写点 :230/251/253/273/292）+ `evolution_run.py:132-143` stdout 报告；staged/applied/rejected **无持久档案** | 部分覆盖 |
| 5 | 记忆结构化：JSON Cards / User as Code | Ch3 §3.5 / §3.6 | grep `json_card`：零命中；`UserProfile`（`long_term_memory.py:97`）为非结构化摘要式存储（`json.dumps(to_dict())` 键值） | 零覆盖 |
| 6 | golden 难度分层 + 来源字段 | Ch7 §7.17 | `legal_golden.jsonl`〔已于 2026-10-26 随评测集下线删除；以下 8 字段为当时结构，在用题集是 `evals/ceval_golden.jsonl`〕 8 字段（id/question/route_type/expected_sources/expected_citations/must_contain/expect_reject/notes），**无 difficulty / source**；`feedback_ingest.py:110` 回流不写来源（2026-10-27 实测；原引 `:82` 为行号腐烂） | 零覆盖 |
| 7 | 「说对做错」声称 vs 工具日志校验 | Ch7 §7.9 | evals grep `claim_consistency\|claims`：零命中 | 零覆盖 |
| 8 | Prompt Sensitivity + Privacy-aware 评估 | Ch7 §7.5 / §7.17 | evals 无 `sensitivity`（仅 ablation 框架 variant）；无 PII/privacy 处理；grep `privacy\|pii` 于 evals：零命中 | 零覆盖 |
| 9 | Best@k | Ch7 §7.14 | `pass_at_k` / `pass^k` 已有（`run_eval.py:634` 可靠性指标组，`repeat` 采样 :1344）；grep `best_at_k\|best@k`：零命中 | 缺 |
| 10 | Debate / 专家小组对等变体 | Ch10 §10.13 | grep 零命中；Manager 仅单形态 | 另期 |
| 11 | 主动工具发现层 2 | Ch4 §4.7 | Phase 21 落第 1 层（`describe_groups()`）；主动发现/第 2 层明标另期 | 另期 |
| 12 | 用户模拟器 | Ch7 §7.17 | grep 零命中 | 另期（端点前置已于 2026-10-25 解除，实现未做——见 §5） |

**跨章确认已达标（本版不重复立项）**：三道防线（`evolution/loop.py` 证据隔离 / staging / `touches_protected_verifier:194` + `regression_gate:157`）、Prompt injection 三阶段检测、轨迹回流（`evals/feedback_ingest.py`）、KB 索引版本（`INDEX_VERSIONS_TO_KEEP`）、Harness 五职责、L1–L4 上下文分层、Skills/Hooks、Agentic RAG（Phase 24）、六失败模式（Phase 25）、judge + 配对统计（Phase 26）、淘汰归档红线（Phase 24）。

**维持不做**：Ch8 后训练、Ch5 coding-centric 架构中心、Ch6 模态、Ch10 A2A / Decentralized / Agent 社会、Ch3 §3.15 知识发现 / §3.16 多模态（依据不变，见 `Phase21-26_规划.md` §4）。

---

## 1. 设计原则（继承 + 本版新增）

继承 `Agent_Core_2.0_规划.md` §1 五模式与 `Phase15-20_深化规划.md` §1 七条纪律，本版新增：

1. **澄清是正常控制流，不是异常**（Ch9 §9.3.2）：澄清应答不计失败、不入 HITL、不改路由终态；flag-off 时澄清路径零调用（字节等价）。
2. **预算信息只读、走轨迹 append**：harness 写（模型不可自写，沿状态栏可信边界）；**动态剩余量不进状态栏**——状态栏位于静态前缀，动态值 = 每步换前缀 = 打穿 KV Cache（违反 Phase 22 原则 3「缓存纪律是架构约束」）；状态栏只允许加**常量**键且需 flag。
3. **记忆结构化只增不改、不毁原始**（Ch3 §3.8）：新卡片字段只增不改旧序列化；叙事情节保留 Simple Notes 双轨；淘汰仍走 Phase 24 归档红线。
4. **评估新指标只增不进门禁**：`evals/baselines/quality.json`〔已于 2026-10-26 随评测集下线删除，CI 侧已无 `eval-offline` 挂载；本条门键口径保留为评测复活后仍适用的口径〕五 metric 门键与 `tests/test_ci_gate_contract.py` 断言零变化；新报告键（`difficulty_*` / `claim_consistency` / `sensitivity_*` / `privacy_*` / `best_at_k`）全部只增、仅进摘要。

---

## 2. Phase 总览与依赖

| 项 | 主题 | 批次 | 状态 | 依赖 |
|----|------|------|------|------|
| **前置** | 审计批次 A（P0 安全）+ B/C 穿插 | 前置/穿插 | 审计 A **已完成**（2026-09-28）；B/C **已完成**（2026-09-28，见 §8 与 `架构审计-2026-09-28.md` §1） | `架构审计-2026-09-28.md` §3 |
| **28** | 自主护栏：需求澄清决策 + 预算意识 | 批一 | **已完成**（2026-09-28，§8） | Phase 18 状态栏、Phase 24 挂点先例 |
| **31** | 评估二期：分层 golden + 声称一致性 + 敏感性/隐私 + Best@k | 批一 | **已完成**（2026-09-28，§8） | Phase 26 judge/`--compare`、`feedback_ingest`（已有） |
| **30** | 进化闭环：载体扩展 + 提案落盘 + 睡眠学习 | 批二 | **已完成**（2026-09-28，§8） | Phase 12/20 进化链、Phase 24 task handler 先例 |
| **29** | 记忆结构化二期：JSON Cards + User as Code | 批三 | **已完成**（2026-09-28，§8） | Phase 24 归档红线（已有） |
| 另期 | 缺口 10 / 11 / 12（对等变体 + 虚拟 FS / 主动工具发现层 2 / 用户模拟器） | — | 另期 | 缺口 12 端点前置已解除，实现未做（§5） |

```
前置   审计批次 A（P0 安全，~1 天）；B1/B3/B4 随批穿插，B2 单列小批，C 随批尾
批一   Phase 28 自主护栏 ∥ Phase 31 评估二期      （互相独立）
批二   Phase 30 进化闭环
批三   Phase 29 记忆结构化
另期   缺口 10 对等变体 / 11 主动工具发现 / 12 用户模拟器（端点已具备、实现未做）
```

---

## 3. 前置批次与穿插批次（源自 `docs/architecture/架构审计-2026-09-28.md` §3）

> 执行细则、改法与验收以审计文档为准；完成后项移入审计文档 §1 表格并在本文 §8 勾选。**纪律：不引入新 auth 框架/中间件；安全修复默认开（语义收紧不属行为分叉，无需 flag）。**

| 批次 | 项 | 触点 | 验收 |
|------|----|------|------|
| **A（前置，P0）** ✅ | A1 会话/历史归属校验（IDOR） | `v1/chat.py::get_chat_history`、`v1/sessions.py` 详情/删除/重命名 | ✅ `test_p48_ownership.py`：跨 user 读 → 404；同 user → 200 |
| A ✅ | A2 上传读鉴权 | `v1/upload.py:220/234`（GET 无 `require_v1_user`） | ✅ 无凭证读 → 401；他人文件 → 404 |
| A ✅ | A3 限流口径 | `ratelimit.py:242` 共享 config 写、retrieval `_chat_limiter` import 期绑定 | ✅ `test_rate_limit_middleware` 扩 4 用例 |
| **B（穿插，P1）** ✅ | B1 流式 idle 看门狗 | `chat_service.py:1108`（`asyncio.wait_for(__anext__)`） | ✅ 新 key `STREAM_IDLE_TIMEOUT`（默认 60s，五处同步）；静默挂起 → error 帧 + 槽位归还（test_p14 新 4 条） |
| B ✅ | B3 auth 解耦 | 新建 `web/src/api/token.ts`；chat 去反向依赖 knowledge | ✅ `vue-tsc` 0 错 + 依赖方向扫描 |
| B ✅ | B4 跨标签页同步 | `useConversationChat.ts` BroadcastChannel | ✅ vitest：假 channel 往返等 4 用例 |
| B′（单列小批）✅ | B2 SSE 词汇双写 | chat + retrieval 两处 yield 加 `event` 字段并行 `type`；前端回退读取 | ✅ 双写断言 + 前端回退用例；**不合并解析框架**；双周期后弃 `type`（另立项） |
| **C（穿插，P2）** ✅ | C2/C3 tests lint 清理 + 死导出 | `ruff --fix` tests + 纳入严格 select | ✅ 55 处清零 + 删 tests 豁免 + 独占批次；死导出已删（重复组件评估不立项，audit §1 C3） |
| C ✅ | C1 消息列表窗口化 | `ChatMessageList` >200 条折叠中段 | ✅ vitest 窗口渲染 2 用例 |

---

## 4. Phase 详节

### Phase 28 — 自主护栏：需求澄清决策 + 预算意识（批一）

**目标**：补缺口 1/2。歧义输入先澄清再执行（Ch9 §9.3.2：澄清是自主性控制流）；预算对模型只读可见（Ch10 §10.6：预算意识影响收敛行为），两条都默认关、字节等价。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 澄清信号 | **确定性启发**：词表命中（法域冲突/主体实体缺失/口语过短）+ 路由 `Router.confidence` 低于阈值；零新增 LLM 调用 | 离线可测、可解释、零成本 |
| 澄清动作 | 返回结构化澄清应答（AIMessage + 候选项列表），**不新增图节点**（不动 L2 拓扑，避 `test_p24_deadcode` 8 节点绊线）；不入 HITL、不计失败 | 澄清是正常控制流；HITL 是危险动作语义 |
| 挂点 | 各 handler 函数体入口共享前置 `_maybe_clarify(state)`（先例：Phase 24 Agentic RAG 收敛于 `general_handler.create_kb_query` 函数体，双拓扑同点） | Strangler；`route_query` 返回 `Literal[节点名]`，加节点即改拓扑 |
| 单轮约束 | 每会话至多连续澄清 **1 次**，第二轮必须给出路由结果（会话态 `clarify_armed` 标记） | 防澄清死循环（Ch5 禁再入循环） |
| flag | `CLARIFY_ROUTING_ENABLED: bool = False` + `BUDGET_NOTICE_ENABLED: bool = False`（各五处同步 + p41；执行时拆为**两枚独立开关**——澄清与预算正交，避免实验开关互相污染） | 各自独立开关 |
| 预算·静态 | 状态栏白名单 `STATUS_BAR_TRUSTED_KEYS` **+1 常量键** `max_iter`（`BUDGET_NOTICE_ENABLED` 开才由构造侧写入） | Ch2 状态栏可信区；常量 = 前缀稳定 |
| 预算·动态 | 轨迹 **append** `budget_notice`（`BUDGET_NOTICE_ENABLED` 开且剩余 ≤ 2 步时由 harness 写，append-only）；**拒绝方案：状态栏写 `remaining_iterations`**（状态栏在静态前缀，动态值每步换前缀 = 打穿 KV Cache） | 缓存纪律（Phase 22 原则 3） |
| 耗尽语义 | 不改既有 `budget_exceeded`（`loop.py:153`）与 `MAX_ITERATIONS` 终止行为 | 只增可见性，不改行为分叉 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 28.1 | `agentix/config/settings.py` + `selfcheck.py` + `.env.example` + `docs/architecture/配置管理.md` 表行 | `CLARIFY_ROUTING_ENABLED: bool = False` **+ `BUDGET_NOTICE_ENABLED: bool = False`**（各五处同步 + p41 反向清单） |
| 28.2 | **新** agentix/application/agents/clarify.py | `should_clarify(...) -> ClarifyDecision` 纯函数（词表 + confidence 阈值）+ `clarify_reply()` 构造；词表常量模块级 |
| 28.3 | `agentix/application/agents/general_handler.py`（`respond_to_general_query` / `create_kb_query` 函数体入口） | `_maybe_clarify` 前置；flag 关零调用；`clarify_armed` 单轮约束 |
| 28.4 | `agentix/agent/context/engine.py:17`（`STATUS_BAR_TRUSTED_KEYS`）+ `simple.py` 同步 | +`max_iter`（构造侧 flag-gated 写入；白名单外键既有拒收逻辑复用） |
| 28.5 | agentix/agent/core/loop.py（M6 已删，现见 `agentix/orchestration/orchestrator.py`）（`max_steps`/`steps_used` 已知处 :145 附近）+ LangGraph 主循环 `MAX_ITERATIONS` 检查点（执行时核对取实际生效点） | `BUDGET_NOTICE_ENABLED` 开且剩余 ≤ 2 → 轨迹 append `budget_notice`（trace 观测通道，不动前缀）；**执行核对结论**：AgentLoop 支路经注入的 `budget_observer`（adapter 只在 flag 开时挂）；图主循环流式支路按 `langgraph_step`（子图按 `langgraph_checkpoint_ns` 含 `\|` 过滤）恰一次；非流式 `graph.ainvoke` 步数由 LangGraph 内部强制、无观测点 ⇒ 不插（每路径恰一处，防双写） |
| 28.6 | tests/test_agent_phase28_autonomy_guards.py | 见 DoD |

**DoD**

- [x] flag-off：`respond_to_general_query` / `create_kb_query` 入口行为与基线字节等价（回归断言）；状态栏无 `max_iter`
- [x] flag-on：歧义输入（短查询 / 低 confidence / 词表命中）→ 澄清应答含候选项；明确输入 → 正常路由
- [x] 连续第二次澄清被强制路由（单轮约束测试）
- [x] 澄清应答不入 HITL、不计失败、不改路由终态
- [x] 状态栏 `max_iter` 仅 flag 开写入且为常量（同配置两次前缀哈希一致）；白名单拒未知键
- [x] 剩余 ≤ 2 → `budget_notice` 进轨迹 append 区，**静态前缀哈希不变**
- [x] 耗尽行为与基线一致（`budget_exceeded` 语义回归）
- [x] 守卫 p17/p22/p35/p41/code_hygiene + ruff + mypy + 全量绿

**Gotchas**

1. 澄清应答若进状态栏/前缀会换前缀 → 必须走轨迹/消息区（原则 2）。
2. `route_query` 返回 `Literal[...]` 节点名 + `test_p24_deadcode` / `ROUTE_HANDLERS`（`tests/test_agent_phase19_multiagent.py`）收口 → **禁止新增 clarify 图节点**。
3. 词表与澄清文案不得含 PII；沿既有脱敏通道。
4. `budget_notice` 触发点在两个预算体系（AgentLoop `max_steps` / 图主循环 `MAX_ITERATIONS`）——执行时先核对实际生效路径，勿双写。（已核对：AgentLoop 支路 = loop.py:145 附近注入 observer；图流式支路 = chat_service 事件循环按 `langgraph_step` 恰一次；非流式 ainvoke 无观测点不插。）

---

### Phase 29 — 记忆结构化二期：JSON Cards + User as Code（批三）

**目标**：补缺口 5。稳定属性结构化为 JSON Cards（Ch3 §3.5），派生事实可从事件日志确定性重放（Ch3 §3.6 User as Code）；叙事情节保留 Simple Notes 双轨。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 卡片范围 | `UserProfile` 稳定属性 → `profile_card`（`schema_version` + 显式字段：legal_domain 偏好 / 使用习惯 / 用户约束）；叙事情节保留 Simple Notes | Ch3 §3.5：卡片 = 稳定可校验，笔记 = 易变 |
| 兼容 | `to_dict()` **只增** `profile_card` 键、旧键不动；读侧兼容无 card 旧数据（缺省回退旧字段） | 只增不改（原则 3） |
| User as Code | `derive_profile(events) -> UserProfile` 纯函数：从会话事件日志重派生卡片字段 + 确定性约束检查（偏好冲突 → **标记**而非覆写）；不引规则引擎/新库 | Ch3 §3.6；domain 零第三方 |
| 重派生时机 | 仅在触发器（`memory_consolidation` handler）时重派生；读取时**不**隐式重放 | 读路径零成本 |
| 记忆评估 | 离线 fixtures：偏好追踪 / 冲突更新 / 多会话连续性 / 派生幂等（replay 结果 ≡ 增量结果） | Ch7 记忆质量维度 |
| flag | `MEMORY_PROFILE_CARD_ENABLED: bool = False`（五处同步 + p41）。**执行注记**：规划原案名 `MEMORY_CARDS_ENABLED` 已被 R3-8 语义卡片占用（默认 `true`、不同特性）⇒ 按 Phase 28 先例（两枚正交开关）改用独立新名，两特性互不污染 | 独立开关 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 29.1 | settings + selfcheck + `.env.example` + 配置管理.md | `MEMORY_PROFILE_CARD_ENABLED: bool = False`（原案 `MEMORY_CARDS_ENABLED` 与 R3-8 撞名，见决策表执行注记） |
| 29.2 | `agentix/application/services/long_term_memory.py`（`UserProfile:97` / `to_dict` :326 区） | `profile_card` 结构 + 双向兼容 + roundtrip |
| 29.3 | **新** 派生纯函数（`long_term_memory.py` 同模块或 `memory_consolidation.py` 侧） | `derive_profile` + 约束检查（只标记不覆写） |
| 29.4 | `agentix/application/services/task_handlers.py::handle_memory_consolidation`（:125 先例） | flag 开时追加重派生（幂等） |
| 29.5 | tests/test_agent_phase29_memory_cards.py | 见 DoD |

**DoD**

- [x] flag-off：`to_dict()` 序列化与基线字节等价（回归断言：7 键精确相等，存储里已有 card 也不写盘；prompt 无卡片区段）
- [x] flag-on：卡片写入/读回 roundtrip；旧数据（无 card）读侧正常（`from_dict` 恒读、None 不写空卡）
- [x] 派生幂等：replay 事件 → 卡片 ≡ 增量维护结果（增量/重放共用 `fold_profile_card`；`derive_profile` 排序重放两次确定性一致）
- [x] 约束冲突被标记而非覆写（原值保留 + `conflicts` 只增不覆写；R3-8 读取时裁决回归 `test_r3_memory_layers` 不退化）
- [x] 淘汰仍走归档红线（Phase 24 断言不回归 + flag 开时新增归档红线用例）
- [x] 守卫 + ruff + mypy + 全量绿（**2458 passed / 14 skipped**，新增 `test_agent_phase29_memory_cards.py` 20 条；附带修复 `tests/fakes/redis.py` 负 stop 区间语义对齐真实 Redis）

**Gotchas**

1. 卡片化不得删除叙事情节（弃 Simple Notes 即违反 §3.5 双轨）。
2. 事件源含 PII → 派生沿既有脱敏通道；卡片字段白名单化。
3. `profile_card` 新键若进 prompt 拼装 = 前缀变更 → flag-off 不拼装即等价，开启时点文档化。

---

### Phase 30 — 进化闭环：载体扩展 + 提案落盘 + 睡眠学习（批二）

**目标**：补缺口 3/4。演化载体补 KNOWLEDGE / PROGRAM（Ch9 §9.6 载体谱系）；提案状态全量 append-only 落盘（Ch9 §9.8.2：保留失败）；离线睡眠学习触发（Ch9 §9.9：无人值守时聚合信号产提案）。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| KNOWLEDGE 载体 | 变更对象 = KB/检索提示片段与检索参数提案（`KB_QUERY_REWRITE_PROMPT` 片段、top_k/threshold 提案值）；回归门 `("citation", "scope_isolation")` | 检索知识不越权、引用不降 |
| PROGRAM 载体 | 变更对象 = **声明式**工具编排/策略规则（结构化 JSON 规则，**绝不写代码文件/执行任意代码**，payload 过 allowlist 校验）；回归门 `("param_fidelity", "verifier_logic_unchanged")`；`touches_protected_verifier`（`:194`）fail-closed | 程序 = 可执行规则；三道防线不动 |
| 提案落盘 | 新 key `EVOLUTION_PROPOSAL_LOG: str = "data/evolution/proposals.jsonl"`；`EvolutionLoop` 注入可选 `proposal_sink`，staged/applied/rejected **全记**（append-only + `content_hash()` 去重）；**不进 `evals/baselines/`**（与 `quality.json` 碰撞 raise 的既有守卫） | Ch9 §9.8.2 保留失败；无 sink = 基线等价 |
| 睡眠学习 | `task_handlers.py` 新 `@queue.register("evolution_sleep")`：门槛（近窗轨迹数 ≥ N 且错误率 ≥ T）→ `aggregate_signals` → `propose`（**只产提案、不晋升**）；flag `EVOLUTION_SLEEP_ENABLED: bool = False`（handler 内读 settings，先例 `MEMORY_CONSOLIDATION_ENABLED:129`） | 复用 task_queue，无新调度器 |
| 端点纪律 | 提案合成需 LLM → handler 端点预检 fail-closed（先例 Phase 26 `_judge_preflight`），无端点 → skipped 非伪造 | 拒伪造纪律 |
| 晋升边界 | 睡眠产出仍停 staging；晋升仍走 evals 门 + 回归（Ch9 双循环：提案可自动、**晋升不可自动**） | |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 30.1 | `agentix/agent/evolution/loop.py:34`（enum）+ `:157`（`regression_gate`）+ `:194`（保护路径核对） | +`KNOWLEDGE` / `PROGRAM` 两值 + 各自门元组；PROGRAM/VERIFIER 触保护路径 fail-closed |
| 30.2 | `agentix/agent/evolution/loop.py`（`EvolutionProposal` + `self.proposals` 写点 :230/251/253/273/292） | 可选 `proposal_sink` 注入 + 三状态 append-only 落盘（含 `content_hash`、`rejected_reason`、时间戳） |
| 30.3 | settings + selfcheck + `.env.example` + 配置管理.md | `EVOLUTION_PROPOSAL_LOG`、`EVOLUTION_SLEEP_ENABLED`（五处同步 + p41） |
| 30.4 | `agentix/application/services/task_handlers.py`（先例 :19/:87/:125） | `evolution_sleep` handler：幂等 claim（沿 `try_claim_consolidation` 模式）+ 门槛 + 端点预检 |
| 30.5 | `evals/evolution_run.py`（stdout 报告 :132-143 旁） | 接 `proposal_sink`；报告键指向档案路径（stdout 语义不变） |
| 30.6 | tests/test_agent_phase30_evolution_carriers.py | 见 DoD |

**DoD**

- [x] 新载体两值 + 门元组；PROGRAM 触碰 `PROTECTED_VERIFIER_PATHS` → fail-closed 拒绝
- [x] 落盘：staged/applied/rejected 三状态全记；同 `content_hash` 重放 append 幂等（去重）；**无 sink 时行为与基线字节等价**（sink 有无双跑场景内部状态序列一致）
- [x] `evolution_sleep`：flag 关 → skipped；门槛未达 → no-op；门槛达 → 只产 staged 提案、不晋升、不触真实端点（预检 fail-closed；测试钉死 `promote`/`run_regression` 被调即失败）
- [x] `proposals.jsonl` 不落 `evals/baselines/`（路径测试：正/反斜杠与绝对路径全拒）
- [x] 守卫 + ruff + mypy + 全量绿（**2438 passed / 14 skipped**，新增 `test_agent_phase30_evolution_carriers.py` 41 条）

**Gotchas**

1. PROGRAM 载体绝不引入任意代码执行面——payload 声明式 + allowlist，越界即拒。
2. 提案档案路径守卫：`evals/baselines/` 有碰撞 raise——新路径必须在 `data/evolution/`。
3. 睡眠学习在进程内触发 LLM 属触网 → 与 record/eval 同纪律：无端点诚实 skipped。
4. 门元组只从既有 gate 键派生，不发明新门（五 metric 门外的 golden 门键集只增不改口径）。

---

### Phase 31 — 评估二期：分层 golden + 声称一致性 + 敏感性/隐私 + Best@k（批一）

**目标**：补缺口 6/7/8/9。数据分层（Ch7 §7.17）、说对做错检测（Ch7 §7.9）、提示敏感性与隐私感知评估（Ch7 §7.5/§7.17）、Best@k 汇总（Ch7 §7.14）——全部离线、确定性、不进门禁。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| golden 分层字段 | **只增** `difficulty`（easy/medium/hard/unknown）+ `source`（manual/feedback/derived）；旧数据缺省 `unknown`/`legacy`，**不改旧行** | 只增不改；p41 golden 行数/分布断言不破 |
| 分层汇总 | `run_eval` summary 增 `difficulty_easy_*` 等分组键（报告只增）；不进门禁 | 门禁纪律 |
| 回流写来源 | `feedback_ingest.build_candidate:82` 增 `source="feedback"` | 闭环可追溯 |
| 声称一致性 | **新** evals/claim_check.py：从答案提取引用声称（法条条号正则族）↔ trajectory tool sources / golden `expected_sources` 比对 → 报告键 `claim_consistency`（不符样本列表；**只标记不判分**、不进门禁） | Ch7 §7.9；正则误伤靠人工复核口径兜底 |
| Prompt Sensitivity | `run_eval` 新 CLI `--sensitivity`：对**指令包装**做确定性微扰（语序/语气词替换表，不动问题本体与 `must_contain` 断言）→ N 变体间指标极差报告键 `sensitivity_*`；缺省关 = 现状字节等价 | Ch7 §7.5 |
| Privacy-aware | PII 正则族（手机/身份证/银行卡，**纯 stdlib 离线**）命中 → 样本标 `privacy_flagged`、单列报告，**applicable 类指标分母排除**；不进门禁 | Ch7 §7.17；离线不外发 |
| Best@k | 在既有 `repeat`（`run_eval.py:1344`）结果上补 `best_at_k`（rubric 类指标取 k 次 max；`pass_at_k`/`pass^k` 不动）；`repeat=1` 时标注 n/a | 只增；复用既有采样结构 |
| 门禁 | `evals/baselines/quality.json`〔已于 2026-10-26 随评测集下线删除〕门键 + `test_ci_gate_contract` 断言**零变化**（复活后仍适用） | 原则 4 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 31.1 | `evals/legal_golden.jsonl`（8 字段，该集已于 2026-10-26 删除） | 新行写 `difficulty`/`source`；旧行不动（缺省由 runner 兜底） |
| 31.2 | `evals/run_eval.py`（分组汇总 / `compute_pass_k` :634 旁 / CLI :1480 可靠性参数组） | `by_difficulty` 汇总 + `--sensitivity` + `best_at_k` + `privacy_flagged` 口径（全部报告键只增） |
| 31.3 | **新** evals/claim_check.py | 条号正则族 + 声称↔来源比对 + 报告输出；无 trajectory 诚实 skip |
| 31.4 | `evals/feedback_ingest.py:110`（2026-10-27 实测；原引 `:82` 是空行，行号早已腐烂） | `source="feedback"` 写入 |
| 31.5 | tests/test_agent_phase31_eval_layering.py | 见 DoD |

**DoD**

- [x] 旧 golden（无新字段）全量可跑，输出与基线字节等价（2026-09-28 验证：改前快照 `phase31_pre_snapshot.json` 对照，37 个旧键**逐位相等**；新键只增不改）
- [x] 新字段分层汇总出数；summary 新键全部 `difficulty_*` / `sensitivity_*` / `claim_consistency` / `privacy_*` / `best_at_k`（只增）
- [x] `claim_check`：构造两例（说对做对 / 说对做错）检出正确；无 trajectory 诚实 skip
- [x] `--sensitivity` 缺省关 = 字节等价；变体不改 `must_contain` 语义断言（离线带 flag → 提示 + exit 2；run_offline 自动检测出键）
- [x] PII 检测纯离线；命中样本单列且从 applicable 分母排除（口径测试）
- [x] `best_at_k` 在 `repeat≥2` 出数；`repeat=1` 标注 n/a（reason `n_a_single_run`）
- [x] `test_ci_gate_contract` 门键断言**原样通过**（零变化）
- [x] 守卫 + ruff + mypy + 全量绿（**2397 passed / 14 skipped**，新增 `test_agent_phase31_eval_layering.py` 40 条）

**Gotchas**

1. 条号正则误伤（合同「第 11 条」vs 法条）→ 只标记不判分，报告口径写明人工复核。
2. PII 正则不外发、不进日志原文（只记命中布尔/类型）。
3. sensitivity 扰动只作用于指令包装，**不得**改写问题本体——否则分层指标不可比。
4. golden 加字段不得触碰 p41 `test_golden_size_claims_match_data` / `test_golden_distribution_line_matches_data` 的行数与分布声明（只增字段不增删行）。

---

## 5. 明确不做（另期 / 不适用）

| 项 | 理由 |
|----|------|
| 缺口 10：Debate / 专家小组对等变体 + 虚拟 FS | 另期单列（需先定对等拓扑与信息增量判据复用面） |
| 缺口 11：主动工具发现层 2（Ch4 §4.7） | Phase 21 明标另期；依赖层 1 元数据目录稳定 |
| 缺口 12：用户模拟器（Ch7 §7.17） | **前置已解除、实现未做（未做）**：LLM 端点已到位——judge 通路已于 Phase 26 去 stub（`evals/run_eval.py::judge_similarity` 真实端点出分，见 CHANGELOG「Phase 26 执行：评估三期——judge 去 stub + 配对显著性（2026-09-27）」），本机 LM Studio 自 2026-10-25 起实际用于 102 题基线录制（见 CHANGELOG「优化批次 P2-A」）；但用户模拟器本身全仓零实现（`grep -rni "simulator" agentix evals scripts tests` 零命中）。仍是另期 |
| Ch8 后训练 / Ch5 coding-centric / Ch6 模态 / Ch10 A2A·Decentralized | 继承 `Phase21-26_规划.md` §4，依据不变 |
| 完整 auth 体系 / 新中间件 | 审计批次 A 纪律：最小收口，不引框架 |
| 合并双 SSE 解析框架 | `架构演进评估.md` 拍板不动（B2 只做词汇双写） |
| 手工伪造 baseline / quality.json（二者已于 2026-10-26 随评测集下线删除，代码保留） / golden 分层数据 | 拒伪造纪律（分层字段只随真实新增行写入） |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 澄清死循环 | 单轮强制路由 + 连续澄清拒绝测试 |
| 澄清/预算打穿静态前缀（KV Cache） | 走轨迹 append 通道；状态栏只加常量键且 flag-gated；前缀哈希测试断言 |
| 新图节点触 p24/p19 绊线 | 澄清挂 handler 函数体，不加节点 |
| PROGRAM 载体绕过三道防线 | 声明式 payload + allowlist + `touches_protected_verifier` fail-closed + 回归门 |
| 提案档案与 `evals/baselines/` 碰撞 | 路径约定 `data/evolution/` + 专测 |
| 睡眠学习离线触网 | 端点预检 fail-closed（Phase 26 先例），无端点 skipped |
| 新报告键污染门禁 | gate keys 集合断言零变化（`test_ci_gate_contract`）+ 原则 4 |
| golden 加字段破坏 p41 行数/分布断言 | 只增字段、不增删行；旧行不动 |
| 安全收口（批次 A）破坏现有客户端 | 404 而非 500（防存在性泄露）；header 兼容 query 旧值过渡（执行时核对前端调用点） |

---

## 7. 交付物模板（每 Phase）

1. 代码 + 默认关 feature flag（如适用；五处同步：settings / selfcheck / `.env.example` / 配置管理.md 表行 / p41 反向清单）
2. 新测试文件 ≥1（flag-off 等价 + flag-on 行为 + 负路径）
3. 文档：本规划勾选 / `模块清单.md` §6 / `架构审计-2026-09-28.md` §1 移项（批次 A/B/C）/ CHANGELOG Unreleased 条目
4. 全量验证：

```powershell
& ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests --select F401,F811,F841,E9
& ".\.venv\python.exe" -m mypy agentix/domain --config-file=pyproject.toml
# 触及前端时（批次 A 鉴权头 / 批次 B、C 前端项）
Set-Location web; npx vue-tsc --noEmit; npx vitest run
```

5. 基线数字若变：README §11 + `现有目录树.md` + `Agent_Core_2.0_规划.md` §9 头 + `Phase15-20_深化规划.md:4` + `Phase21-26_规划.md:5` + 本文件头部，共 7 处同步（CHANGELOG 旧条目为历史记录不改）

---

## 8. 执行状态

| 项 | 状态 |
|----|------|
| 本规划文档落盘 | 2026-09-28 v1.0（四主题 + 审计 A 前置 / B/C 穿插已拍板） |
| 前置 · 审计批次 A（A1 归属校验 / A2 上传读鉴权 / A3 限流口径） | ✅ 完成（2026-09-28）：`v1/deps.py` `resolve_v1_caller`/`session_visible_to` + owner sidecar + per-params limiter；验收 `test_p48_ownership.py` 21 条 + `test_rate_limit_middleware` 扩 4 条；全量 2329 passed；已移入 `架构审计-2026-09-28.md` §1 |
| 批一 · Phase 28 自主护栏 | ✅ 完成（2026-09-28）：`CLARIFY_ROUTING_ENABLED` + `BUDGET_NOTICE_ENABLED` 双 flag 五处同步；新 `clarify.py`（`should_clarify`/`clarify_reply`）+ `general_handler` 澄清门（`clarify_armed` 单轮约束，`AgentState` 新通道）+ `engine.py` 白名单 `max_iter`（adapter 构造侧 flag-gated）+ 新 `budget_notice.py`（loop observer + 流式 `langgraph_step` 检查点）；验收 `test_agent_phase28_autonomy_guards.py` 28 条；全量 **2357 passed** |
| 批一 · Phase 31 评估二期 | ✅ 完成（2026-09-28）：新 `evals/claim_check.py`（条号正则 + 声称↔来源比对，纯 stdlib 无时钟）；`run_eval.py` 分层汇总 `difficulty_*` + `privacy_*`（PII 纯离线检测，applicable 分母排除、原文零留存）+ `claim_consistency` + `best_at_k` 5 分项 + `--sensitivity` 四变体 `sensitivity_range_*`（run_live 参数 / run_offline 自动检测，离线带 flag 拒绝 exit 2）+ `feedback_ingest` `source="feedback"`；golden 不动（缺省兜底，拒伪造）；验收 `test_agent_phase31_eval_layering.py` 40 条；全量 **2397 passed** |
| 批二 · Phase 30 进化闭环 | ✅ 完成（2026-09-28）：`EvolutionCarrier` +`KNOWLEDGE`/`PROGRAM` 两值与门元组（`citation+scope_isolation` / `param_fidelity+verifier_logic_unchanged`，全用既有门键）+ 各自 payload 白名单（KNOWLEDGE 键/类型、PROGRAM 声明式 rules `{id,when,then}` 越界即拒、触保护路径 fail-closed）；`proposal_sink` 注入（`jsonl_proposal_sink` 三状态 append-only + `content_hash`+state 去重、`evals/baselines/` 路径构造即 ValueError、无 sink 内部状态与基线一致）；`EVOLUTION_PROPOSAL_LOG`/`EVOLUTION_SLEEP_ENABLED` 两 key 五处同步；`evolution_sleep` handler（flag→门槛→dry_run→端点预检→进程内幂等 claim→LLM 合成→白名单校验只 stage，`promote`/`run_regression` 禁调）+ `EVOLUTION_SLEEP_PROPOSAL_PROMPT` 外置 services/prompts；`evolution_run --proposal-log`（默认关 stdout 逐键一致，非法路径 exit 2）；验收 `test_agent_phase30_evolution_carriers.py` 41 条（Phase 12 载体断言同步六值）；全量 **2438 passed** |
| 批三 · Phase 29 记忆结构化 | ✅ 完成（2026-09-28）：`MEMORY_PROFILE_CARD_ENABLED` 默认关（原案 `MEMORY_CARDS_ENABLED` 与 R3-8 撞名 ⇒ 独立开关）；`UserProfile` +`profile_card`（`to_dict` flag 闸只增键、旧 7 键不动；`from_dict` 恒读、无卡回退）；纯函数 `fold_profile_card`（增量/重放唯一核心：usage 极值/领域计数/事件内主题去重/`preferences` 观测——冲突**只标记不覆写**原值）+ `derive_profile`（`(created_at,session_id)` 排序重放、旧字段沿用首见序容量 50 语义、空日志不伪造卡）；`refresh_profile_card` 只更新卡不动旧字段（无事件/flag-off 零写入）；`memory_consolidation` handler flag 开且非 dry_run 才重派生入报告（flag-off 报告形状与基线一致）；prompt 卡片区段仅 flag 开拼装（Gotcha 3 已文档化）；`MEMORY_PROFILE_CARD_ENABLED` 五处同步；验收 `test_agent_phase29_memory_cards.py` 20 条；全量 **2458 passed** |
| 批三 Phase 后 · 穿插批次 B/C | ✅ 完成（2026-09-28）：B1 → B3/B4 → B2 → C1 → C2/C3 全部落地，全量 2463 passed + vitest 121 + 守卫 162 + ruff（含 tests 门禁）/mypy/vue-tsc 全绿 |
| 穿插 · 批次 B（B1/B3/B4，B2 单列） | ✅ 完成（2026-09-28）：B1 `STREAM_IDLE_TIMEOUT` 五处同步 + `asyncio.wait_for(__anext__)` 手动迭代（error+done 兜底/槽位归还，test_p14 新 4 条）；B3 `web/src/api/token.ts` 共享层（knowledge 原路径 re-export，chat 去反向依赖）；B4 `conversationSync.ts` BroadcastChannel `create/delete/rename` 三事件 + useConversationChat 合并接收 + useKbModalChat rename/delete 发送（vitest 4 条）；B2 单列：chat/retrieval 27 处 yield 双写 `event` + `_to_sse`/router 透传 + 前端两解析器「优先 event 回退 type」（双稳定两周期后弃 `type`，不合并解析框架）；已移入 `架构审计-2026-09-28.md` §1 |
| 穿插 · 批次 C（C1/C2/C3） | ✅ 完成（2026-09-28）：C1 `ChatMessageList` >200 折叠中段（首 50 + 尾 150 + 可展开折叠条，vitest 2 条）；C2 tests 一次性清零 55 处（49 F401 --fix + 6 F841 手工保调用）+ B017 noqa 修正 + **删 tests per-file-ignores 豁免** + 门禁纳入 tests（README/audit §4/本规划 §7 三处文档 + `make lint`/ci.yml ruff 范围）；C3 删 `PROGRESS_STAGE_LABELS` 死导出（空态组件评估后不合并——语义/样式差异 + 跨模块合并与 B3 分层方向相逆，理由见 audit §1）；已移入 `架构审计-2026-09-28.md` §1 |
| 另期 · 缺口 10/11/12 | 另期单列（12 需端点） |
| Phase 17 全栈 record | **blocked**（沿 `Phase21-26_规划.md` 附录 A；拒伪造 baseline） |

每完成一项：更新本表 + 跑 §7 验证命令；数字变化按 §7.5 同步。

---

## 附录 A · 批次排期与穿插说明

1. **批一启动前置**：审计批次 A 三项完成并过 `test_p48_ownership.py` + `test_rate_limit_middleware` 扩展后，Phase 28 / 31 才进入执行（安全面先收口）。
2. **穿插节奏**：B1（流式看门狗）在批一任意测试红/绿间隙插入（独立、可单测）；B3/B4 前端项合并一个前端批次；B2 单列小批（双写稳定两发布周期）；C 随批尾独占（防与功能改动混提交）。
3. **每批验收**：§7 验证命令全绿 + §8 状态表勾选 + `模块清单.md` §6 同步 + CHANGELOG 条目；批次 A/B/C 完成项移入 `架构审计-2026-09-28.md` §1。
4. **批间拍板**：批一验收后拍板批二（Phase 30），批二验收后拍板批三（Phase 29）。
