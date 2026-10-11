# Phase 21–26 · Agent Core 下一版优化迭代规划（v1.0）

- 版本：v1.0（依据《深入理解 AI Agent》全书笔记分章版 11 文件对照修订）
- 日期：2026-09-27
- 前置：Phase 0–20 收口 + 接口审查三批完成（勾选真源 `module-inventory.md` §6 / `Phase15-20_深化规划.md` §11）；现基线 **2463 passed / 14 skipped / 0 deselected**（`pytest -m "not integration"`，2026-09-28 审计批次 A + Phase 28 自主护栏 + Phase 31 评估二期 + Phase 30 进化闭环 + Phase 29 记忆结构化收口后；skip 含 10 条 Redis Stream 契约用例无 Redis 服务时诚实跳过）
- 模式：Strangler 不变；flag 默认关、flag-off 字节等价；domain 零第三方；LangGraph 仅 `agentix/agent/graph/`；不新增数据库 / 新框架 / 新中间件
- 范围决策（已拍板）：**六主题全做 = Phase 21–26**；分批执行，**批一 = Phase 21+22**，批二 = 23+24，批三 = 25+26；每批验收后拍板下一批

---

## 0. 对照结论（书 × 现状 × 缺口）

> 上一版（Phase 15–20）主题是「点亮已建成的 Harness」；本版主题是「**补书中有、代码里完全没有的子系统**」+「评估门解阻」。

| 主题 | 书依据 | 现状证据（2026-09-27 核实） | 缺口 |
|------|--------|------------------------------|------|
| Skills 渐进披露 + Hooks 生命周期 | Ch2 §2.6 / Ch4 §4.7 / Ch1 §1.11.2 | `agentix/` 全树 grep `skill\|hook`：**零命中**（hook 机制先例仅 `hitl_bridge.install_executor_hook:630`） | 零覆盖 |
| 工具三层按需化（层次化组织→主动发现→Skills 查阅） | Ch4 §4.7 | grep `能力检索\|按需加载\|progressive`：零命中；`ToolRegistry`（`capabilities/tools/definitions.py:130`，M6 已删，现见 `agentix/tools/definitions.py`）全量注册 | 零覆盖（并入 Phase 21） |
| 上下文二期：缓存纪律 + L4 | Ch2 §2.4（稳定前缀）/ §2.7（分层压缩） | Phase 18 已交付 L1–L3；明标另期：「工具序稳定 / 配置变更 append 消息」「L4 Auto-compact」（`Phase15-20_深化规划.md:211-213`） | 建成未排期 |
| 交互二期：出站通知 + Redis Stream | Ch6 §6.2（用户沟通是显式工具，不假设同步会话） | `capabilities/` 无任何出站通知工具（仅 MCP 内部 `_notify` 与风险词表 `notify`）；入站 webhook 有（`interfaces/http/v1/events.py:38`）；`EVENT_PORT_BACKEND` 仅 memory/jsonl（`agent/events/port.py:39`） | 零覆盖 + 另期 |
| 记忆/知识深化：整理遗忘生产化 + Agentic RAG | Ch3 §3.8（遗忘≠删除）/ §3.11（Agentic RAG） | 纯函数在（`memory_consolidation.py:104/155/216/265`）、`long_term_memory.consolidate:338` 有入口；**无生产触发调度**；`route_query`（`application/agents/router.py:191`）为固定路由，无「是否再搜/改写查询」决策权 | 弱覆盖 |
| multi-agent 运行时防线（六失败模式） | Ch10 §10.17 | `ControlPlane`（`control_plane.py:106`）有 spawn/status/cancel/预算雏形；`ManagerConfig.budget_for:158` peer 预算；**无消息总线、无最大并发/深度、无逻辑级冲突检测** | 部分覆盖 |
| 评估三期：judge 去 stub + 统计比较 + 解阻 | Ch7 §7.7 / §7.14 / §7.17 | `judge_similarity`（`evals/run_eval.py:390`）诚实 stub（raise 未实现）；pass@k/pass^k 已有（`:542-611`）；**无配对比较/置信区间**；Phase 17 全栈 record **blocked**（无 Docker/LM Studio，拒伪造） | 部分覆盖 + 硬件阻塞 |

**跨章确认已达标（本版不动）**：Harness 五职责与五模式（Ch1/附录 B）、L1–L3 上下文分层与状态栏白名单（Ch2）、Memory 五类/读取时裁决/PII（Ch3）、ACI/参数保真/MCP 审查（Ch4）、四层故障与恢复（Ch5）、HITL 三栈桥与误拒可观测（Ch4/Ch1）、首错归因与前缀回归（Ch7）、双循环与三道防线（Ch9）、supervisor 接线与信息增量离线指标（Ch10）。

---

## 1. 设计原则（继承 + 本版新增）

继承 `Agent_Core_2.0_规划.md` §1 五模式与 `Phase15-20_深化规划.md` §1 七条纪律，本版新增：

1. **Skills = 元数据常驻、正文按需**（Progressive Disclosure）：常驻仅「名字 + 一句话适用条件」，正文经 `read_skill` 工具惰性进上下文；Skill 是知识/流程（「怎么做」），不替代工具（「能做」）。
2. **Hook 只旁路、不改道**：生命周期钩子异常一律吞掉记日志，不得改变主路径结果（与 HITL executor hook 同先例）。
3. **缓存纪律是架构约束**：工具 schema 顺序与静态前缀在同配置下必须字节稳定；配置变更走轨迹 append 消息，不改前缀。
4. **遗忘不销毁**：记忆整理只产摘要/合并/淘汰计划并保留可回溯证据；裁决仍走读取时（Ch3 §3.8 反对写入时销毁历史）。
5. **多 Agent 只加防线不加数量**：Ch10 准入判据——本版不增加任何新 agent 实例/拓扑，只补预算、并发、冲突、总线防线。
6. **judge 可用但不进门禁**：LLM-judge 默认关、需端点；`quality.json` 五 metric 门结构不变；拒伪造 baseline 纪律延续。

---

## 2. Phase 总览与依赖

| Phase | 主题 | 状态 | 依赖 |
|-------|------|------|------|
| **21** | Skills 渐进披露 + Hooks 生命周期 + 工具按需化 | 批一（已拍板执行） | 指令分层/ToolRegistry（已有） |
| **22** | 上下文二期：缓存纪律 + L4 auto-compact | 批一 | Phase 21（Skill 注入属动态轨迹） |
| **23** | 交互二期：出站通知 + Redis Stream + 事件矩阵 | 批二 | Phase 20 EventPort（已有） |
| **24** | 记忆/知识：整理遗忘生产化 + Agentic RAG 决策权 | 批二 | Phase 4/7（已有） |
| **25** | multi-agent 运行时防线（六失败模式） | 批三 | Phase 19 supervisor（已有） |
| **26** | 评估三期：judge 去 stub + 配对统计 + 解阻清单 | 批三 | Phase 17 离线部分（已有）；record 仍 blocked |

```
批一  Phase 21 Skills/Hooks ──► Phase 22 上下文二期
批二  Phase 23 交互二期 ∥ Phase 24 记忆/知识   （互相独立）
批三  Phase 25 multi-agent 防线 ∥ Phase 26 评估三期+解阻
附录  Phase 17 全栈 record 解阻（独立，仅受硬件约束，随时可执行）
```

---

## 3. Phase 详节

### Phase 21 — Skills 渐进披露 + Hooks 生命周期（批一）

**目标**：补全书最大零覆盖缺口。Skills 让专业知识/流程不再常驻上下文；Hooks 给生命周期提供可插拔扩展点（不新增行为，只开放挂点）；工具注册引入按需元数据索引（Ch4 §4.7 层次化组织第一层）。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Skill 存放 | 仓库根 `skills/`（内置，只读，示例 `skills/release-checklist/`）+ `SKILLS_DIR` 可指外部目录；SKILL.md = frontmatter（name/description/anti_when 三键必填，description 即「何时用」一句话）+ 正文 | 对齐书「常驻元数据 → 命中读正文」；不新建数据库 |
| 注入方式 | **两段式**：元数据（name + 适用条件一句话）经 `build_instructions` 注入静态区（flag 关=零注入）；正文经新工具 `read_skill(name)` 惰性读入轨迹 | 元数据极小、正文不预载；保护稳定前缀 |
| 负面条件 | frontmatter 强制 `anti_when`（何时**不**用） | Ch4 §4.3 负面描述原则 |
| flag | `SKILLS_ENABLED` / `HOOKS_ENABLED` / `SKILLS_DIR` 三 key（前两默认 false） | 独立开关 + 目录可外指；p35 三处同步 + p41 反向清单 |
| Hook 形态 | Python callable 注册表（同 `install_executor_hook` 先例），事件 5 类：`on_tool_before` / `on_tool_after` / `on_step_end` / `on_run_end` / `on_error`；**不实现 shell 型 hooks**（Ch4/Ch5 外延，另期） | 最小可用；异常吞掉只记日志 |
| 工具按需索引 | `ToolRegistry` 增加按类别分组的元数据目录（`describe_groups()`），供 planner/文档使用；**本 Phase 不改默认全量注册行为** | 第一层先落，主动发现/二级按需（第 2、3 层）另期 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 21.1 | `agentix/config/settings.py` + `selfcheck.py` + `.env.example` | `SKILLS_ENABLED: bool = False`、`HOOKS_ENABLED: bool = False`、`SKILLS_DIR: str = "skills"`（三处同步 + configuration.md 表行 + p41 清单） |
| 21.2 | **新** agentix/agent/skills/loader.py（已建） | 扫描 `skills/**/SKILL.md` frontmatter → `SkillMeta`；校验必填字段；失败条目告警跳过 |
| 21.3 | **新** agentix/agent/skills/registry.py（已建） | 元数据表 + `read_skill(name)` 正文读取（路径白名单防穿越，p43 风格） |
| 21.4 | `agentix/agent/context/instructions.py:96` `build_instructions` | flag 开：注入元数据清单（名字 + 何时用 + anti_when 压缩为一行/条）；关：字节不变；参数 `skills_enabled` 供测试覆盖 |
| 21.5 | agentix/capabilities/tools/registry_init.py（M6 已删，现见 `agentix/tools/registry_init.py`） + `definitions.py:130` | 声明 `read_skill`（flag 开才进清单；handler 经 `agent/skills/install.py` 组合根注册）；`ToolRegistry.describe_groups()` 分组元数据目录 |
| 21.6 | **新** `agentix/agent/hooks/`（registry.py + events.py） | 5 类事件注册/分发；`emit()` 捕获全部异常记 `logger.warning` |
| 21.7 | `agentix/capabilities/tools/executor.py:75` `ToolExecutor`（M6 已删，现见 `agentix/tools/gateway/executor.py`） | `on_tool_before/after` 旁路 emit（HOOKS_ENABLED 关 ⇒ 零调用） |
| 21.8 | agentix/agent/core/loop.py（M6 已删，现见 `agentix/orchestration/orchestrator.py`） | `on_step_end` / `on_run_end` / `on_error` 旁路 emit（AgentLoop 支路） |
| 21.9 | `agentix/config/selfcheck.py` + `tests/test_p41_doc_consistency.py` | 三 key 进 `DEFAULT_IGNORED_MISSING`、configuration.md 表行与 p41 反向清单 |
| 21.10 | tests（新 tests/test_agent_phase21_skills_hooks.py，已建） | 见 DoD（17 用例） |

**DoD**（2026-09-27 全项达成，见 tests/test_agent_phase21_skills_hooks.py 17 用例）

- [x] flag-off：指令区/工具表/执行路径与基线字节等价（回归断言）
- [x] flag-on：元数据注入 → `read_skill` 命中读正文 → 正文只进轨迹不进前缀
- [x] frontmatter 缺 `anti_when` → 条目跳过并告警（结构校验）
- [x] `read_skill` 路径穿越（`../`、绝对路径）被拒
- [x] hook：注册→触发顺序正确；hook 抛异常主路径不受影响；未注册时零开销
- [x] `describe_groups()` 分组完整、同配置两次输出序稳定
- [x] 守卫 p17/p22/p35/p41/code_hygiene + ruff + mypy + 全量 pytest 绿

**Gotchas**

1. 元数据注入静态区 = 改前缀 → **首次开启 flag 的那次请求前缀会变**，flag-off 不变即满足等价；文档写明「开 flag 即换前缀一次」。
2. `read_skill` 属能力面 → 注册过 `ToolRegistry` 才会被 executor 看见；勿直接塞进 prompt。
3. hook 挂点不得与 HITL hook 语义冲突（同为 executor 层旁路，顺序：HITL gate 在前、观测 hook 在后，执行时以 `install_executor_hook` 现状为准核对）。

---

### Phase 22 — 上下文二期：缓存纪律 + L4（批一）

**目标**：兑现 Phase 18 明标另期两项：工具序稳定 / 配置变更 append 消息（KV Cache 纪律，Ch2 §2.4）；L4 auto-compact 语义压缩（Ch2 §2.7 分层压缩最后一档，默认关）。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 工具序稳定 | `ToolRegistry` 序列化输出按「注册声明序」冻结快照（首次构建后不可重排）；同配置两次请求 schema 序列哈希一致 | 稳定前缀 = 架构约束；不做字典序重排（会与现状不符引发一次前缀变更，须一次到位并测试锁定） |
| 配置变更 append | 运行中检测到影响行为的配置变更（记录 config hash）→ 向**轨迹 append** 一条 `system_notice`，不改静态前缀 | Ch2：配置变更 append 消息；KV 不打穿 |
| L4 触发条件 | 仅当 L1–L3 后仍超预算的**最后手段**；默认关（`CONTEXT_AUTOCOMPACT=false`） | 书定位即如此 |
| L4 压缩器 | 抽象 `CompactorPort`（domain Protocol，零第三方）；生产实现走 LLM Gateway；**离线测试用 FakeCompactor** | domain 不碰第三方；离线可测 |
| L4 保留五要素 | 压缩 prompt 硬约束 + **机制断言**（trace_id、错误记录、约束 key、来源引用、任务状态字段必须原样保留；正文可缩写） | Ch2 §2.7 保留五要素；机制断言离线可跑 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 22.1 | settings + selfcheck + `.env.example` | `CONTEXT_AUTOCOMPACT: bool = False`（三处同步 + p41） |
| 22.2 | `agentix/capabilities/tools/definitions.py:130`（M6 已删，现见 `agentix/tools/definitions.py:130`） | 注册序冻结快照；`schema_prefix_hash()` 测试辅助 |
| 22.3 | **新**（application 组合根侧）config hash 监测 → append `system_notice` | 变更仅通知，不重建前缀 |
| 22.4 | `agentix/agent/context/engine.py:53`（`trim_policy` 同层） | 接 `CompactorPort`：L1–L3 后仍超限且 flag 开 ⇒ 调压缩；关 ⇒ 行为与 Phase 18 完全一致 |
| 22.5 | **新** `domain/ports/` 增 `CompactorPort`；生产实现在 application 侧接 LLM Gateway | domain 零第三方 |
| 22.6 | tests：tests/test_agent_phase22_context_cache.py（待建） | 见 DoD |

> 落地注（2026-09-27）：`freeze()/unfreeze()/schema_prefix_hash()` 已在 `definitions.py` 实现并测试锁定（本地 registry 实例验证告警拒绝，不触碰全局）；**生产自动触发放延后**——`build_graph`/`_startup` 两类锚点都会在会话中途冻结全局 registry（多处 `with TestClient` 走 lifespan、测试间存在动态注册），先以显式 API + 测试锁定纪律，待 MCP/运行期注册范围摸清后另批接线。

**DoD**

- [x] 同配置连续两次请求：工具 schema 前缀哈希一致；`CONTEXT_AUTOCOMPACT` 开关不进前缀内容
- [x] 运行中改配置 → 轨迹出现 append 通知，静态前缀字节不变
- [x] L4 off：与 Phase 18 行为逐字节等价（回归）
- [x] L4 on + FakeCompactor：五要素机制断言通过；超预算仍 deterministic 终止
- [x] 守卫 + ruff + mypy + 全量绿

**Gotchas**

1. 冻结快照与「新增工具注册」冲突：进程内运行期动态注册工具属违例 → 快照后注册告警拒绝（写测试）。
2. L4 需 LLM → 离线 record/eval 不得依赖；`--judge`/eval 路径永不触发 L4。
3. `system_notice` 内容不得含 PII（沿用 Phase 4 脱敏通道）。

---

### Phase 23 — 交互二期：出站通知 + Redis Stream + 事件矩阵（批二）

**目标**：Ch6 §6.2「用户沟通是显式工具、不假设用户在同步会话」零覆盖补齐；`EVENT_PORT_BACKEND` 补第三实现（Redis Stream，Phase 20 明标另期）；事件去重/超时/重试/取消矩阵补测。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 出站通知形态 | 新 capability 工具 `notify_user(channel, message, priority)`；**本批默认实现 = 落 EventPort（type=`user_notification`）+ ops 可查**，不接真实邮件/短信 | 不引新外部依赖；通道后续逐个挂 |
| 通知风险评级 | 命中 `human_review.py:124` 词表（notify/send/post → MEDIUM）⇒ 走既有 HITL 闸门 | 复用三栈桥；出站即动作 |
| Redis Stream 实现层 | 执行时按 p17/p40 守卫定（倾向 agentix/infrastructure/events/redis_stream.py，待建）+ 组合根工厂分派；`agent/events/port.py:39` 工厂加第三值 | 不臆测分层，守卫为准；复用既有 Redis，不算新中间件 |
| 去重/超时 | `adapter.py` `EventDedupKey` 已有去重；本批补 **超时唤醒 + 取消 + 并发投递** 测试与缺口 | Ch6 §6.2 必备件清单 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 23.1 | **新** agentix/capabilities/tools/notify_user.py（待建；M6 后实体 `agentix/tools/adapters/notify_user.py`） + 注册进 `definitions.py` | EventPort 落盘式通知；flag `NOTIFY_USER_ENABLED`（三处同步 + p41） |
| 23.2 | `agentix/agent/events/port.py:38` | `redis_stream` backend 分派（未知值仍回退 memory + 告警） |
| 23.3 | **新** redis stream EventBus（层别执行时定） | 语义与 jsonl 对齐：event_key 幂等、重启语义文档化 |
| 23.4 | `agentix/agent/events/adapter.py` | 超时/取消路径补缺口（如缺）+ 矩阵测试 |
| 23.5 | settings + selfcheck + `.env.example` | `EVENT_PORT_BACKEND` 增允许值 `redis_stream`；`NOTIFY_USER_ENABLED` |
| 23.6 | tests：tests/test_agent_phase23_interaction.py（待建） | 见 DoD |

**DoD**

- [x] `notify_user`：flag-off 零注册；flag-on 落事件、ops 可查、MEDIUM 风险触发 HITL 闸
- [x] `redis_stream` backend：与 memory/jsonl 通过同一套 EventBus 契约测试（复用参数化）；无 Redis 时诚实 skip 非伪造
- [x] 事件矩阵：去重（同 event_key 重发）/ 超时唤醒 / 取消 / 并发投递 各 ≥1 用例
- [x] 守卫 + ruff + mypy + 全量绿

**Gotchas**

1. 出站即副作用 → 必须过 HITL 词表与预算，不得绕过 `ToolExecutor`。
2. Redis Stream 的 XADD/XREAD 语义不得污染 domain（Protocol 在 `domain/ports/agent_core.py:118` 不动——M6 已删该路径，现见 `domain/agent/ports.py` 的 `EventPort`）。
3. `wait_external_event`（`agent/events/port.py:61`）在三种 backend 下行为一致（契约测试参数化）。

**落地注（2026-09-27）**：

- **23.3 分层偏离**：`RedisStreamEventBus` 落 `agentix/agent/events/redis_stream.py` 而非决策表倾向的 `infrastructure/events/`——与 memory/jsonl 后端同居一个包；仓库无 `agent → infrastructure` 先例，反向 import 会开先例；懒 import 防 p40。理由写入模块 docstring，p17/p40 守卫绿。
- **23.1 注册面**：`notify_user` 未进 `definitions.py`（该文件属 infrastructure 侧），而是经 `registry_init._build_declarations` flag-gated 声明 + `application/services/notify_bridge.py::install_notify_user_tool` 组合根注入（capabilities 不反向 import agent）。
- **23.4 扩展**：`list_recent` 为鸭子扩展（memory 按插入序倒序 / jsonl 委托 / redis async 面），新端点 `GET /ops/events` 惰性 import + `inspect.isawaitable` 兼容；ops **UI** 仍按 §4 另期。
- **事件列表 seq**：`tests/` 新增 `test_agent_phase23_interaction.py`（47 用例，37 过 / 10 redis 诚实 skip）。

---

### Phase 24 — 记忆/知识深化：整理遗忘生产化 + Agentic RAG（批二）

**目标**：Ch3 遗忘三动作（保留重要/合并重复/抽象低层）从纯函数变生产触发；Ch3 Agentic RAG 把「是否再搜/改写查询」决策权交给路由（当前固定）。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 触发方式 | 复用 `task_queue`（先例 `handle_conversation_summary:88`）调度 `long_term_memory.consolidate:338`；无新调度器 | 现有基建 |
| 遗忘红线 | `plan_message_eviction/plan_summary_eviction` 仅作用于**上下文窗口消息与可重建摘要**；长期记忆淘汰必须走「摘要保留 + 证据可回溯」，**禁止写入时销毁** | Ch3 §3.8；执行时先核对 `consolidate` 现状落盘行为 |
| 重要性四因素 | 核对 `select_consolidation_groups:104` 是否覆盖访问频率/时间衰减/独特性；缺项补纯函数（情感强度对本项目弱相关，可标注不适用） | 书清单对照补差 |
| Agentic RAG 范围 | 仅知识分支：检索空/低分 → 查询改写重试 **≤2 次** → 仍无则诚实声明；flag `AGENTIC_RAG_ENABLED=false` | 有界；防死循环（Ch5 错误处理禁再入无限循环） |
| 挂点 | `application/agents/router.py:191` `route_query` 后的知识分支（或其下游），不动 L2 主图结构 | Strangler |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 24.1 | `agentix/application/services/task_handlers.py` | 新 handler：触发 `consolidate`（幂等、可关） |
| 24.2 | `memory_consolidation.py:104/155/216/265` | 核对 + 补重要性因素纯函数与测试（若缺） |
| 24.3 | `long_term_memory.py:338` `consolidate` | 落盘路径审计：确保淘汰保留摘要与证据（读取时裁决不回写）；违规则改 |
| 24.4 | settings + selfcheck + `.env.example` | `AGENTIC_RAG_ENABLED`、（如需）`MEMORY_CONSOLIDATION_ENABLED`（三处同步 + p41） |
| 24.5 | `application/agents/router.py:191` 知识分支 | 空/低分 → 改写重试 ≤2 → 诚实声明；每次尝试进 trajectory |
| 24.6 | tests：tests/test_agent_phase24_memory_rag.py（待建） | 见 DoD |

**DoD**

- [x] 整理触发：幂等（重复触发不重复合并）；淘汰路径保留摘要 + 证据可回溯；纯函数覆盖率不降
- [x] 读取时裁决回归：同事实冲突新旧并存时检索可裁决，历史不被覆写
- [x] Agentic RAG：flag-off 等价；flag-on 空检索改写 ≤2 次且总步数有界；诚实声明路径可断言
- [x] 守卫 + ruff + mypy + 全量绿

**Gotchas**

1. `consolidate` 若含直接删写，先改后接调度——**先核对再动**（执行第一步是审计）。
2. 改写查询的 prompt 走 `prompts/` 外置（p23 纪律），不得内联硬编码。
3. 重试计数进 trajectory（评估三期归因要用）。

**落地注（2026-09-27）：**

- 审计确认 `consolidate` 原状**直接 DEL 淘汰摘要**（违规）→ 已改为「先归档正文 `ltm:archived:summary:{sid}`（同 TTL，含 `source_message_ids` 证据链）再删原键」，report 新增 `archived_session_ids`；被合并掉的条目不另归档（并集语义已全文并入合并结果）；dry-run 报计划零写入。
- 24.2 补三纯函数 `access_frequency_score` / `recency_decay_score`（半衰期指数，`MEMORY_RECENCY_HALF_LIFE_DAYS` 接线）/ `uniqueness_score`（主题稀有度）+ `importance_sort_key` 字典序 `(频率, 衰减, 独特性)`——第一/二键与旧 `(access_count, last_accessed)` 同序严格兼容，独特性仅三级 tie-break，接进 `plan_summary_eviction` 超容淘汰。
- 挂点落地偏离决策表 `router.py:191`：实际收敛于 `general_handler.create_kb_query` **函数体**（扁平图与 supervisor 双拓扑同点，不动 L2 图结构——`test_p24_deadcode` 8 节点绊线 + `ROUTE_HANDLERS` 收口测试约束）；改写 prompt 外置 `lg_prompts.KB_QUERY_REWRITE_PROMPT` 过 p23 三断言。
- handler 幂等走新 `try_claim_consolidation`（`SET NX EX`；`dry_run` 不占窗、`force`/`window<=0` 旁路）；两新 key（`MEMORY_CONSOLIDATION_ENABLED` / `AGENTIC_RAG_ENABLED`，默认关）五处同步。
- 新增 `tests/test_agent_phase24_memory_rag.py` 25 用例；全量 **2245 passed / 14 skipped / 2 deselected**。

---

### Phase 25 — multi-agent 运行时防线（批三）

**目标**：Ch10 §10.17 六失败模式的运行时防线。**不新增任何 agent 数量/拓扑**（信息增量判据：本 Phase 零新增实例 ⇒ 防线本身不违反准入）。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 预算/并发/深度 | `ControlPlane`（`control_plane.py:106`）补 `max_concurrency` / `max_depth` / per-peer 独立预算上限；与 `ManagerConfig.budget_for:158` 统一为单处映射 | 反循环失控；预算单源 |
| 消息总线 | **进程内 asyncio 消息总线**（新 agent/multiagent/bus.py，待建）替代点对点直连；**不引入 Redis Stream 复用为 bus**（职责分离：事件≠agent 通信） | 不加中间件；Ch10 §10.3 Message Bus 本义 |
| 逻辑级冲突检测 | peer 共享产物 key 冲突 + 关键字段矛盾报告（离线可测的启发式：同 key 不同值、结论极性相反）→ 汇总进 meta | 文件层看不见的语义冲突 |
| 错误级联 | peer 结果回传强制走 `bound_result_face`（`manager.py:88`，已有）+ 独立交叉验证钩子（二 peer 结论不一致时标记而非沿链转述） | Ch10 反级联 |
| 独立取消 | 现有 cancel 扩展到 peer 粒度（ControlPlane 已有 cancel，补 fan-out） | 六模式之五 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 25.1 | `application/services/control_plane.py:106`（M4 迁 `agentix/runtime/control_plane.py`，M6 已删旧路径） | max_concurrency / max_depth / per-peer budget（配置进 settings 三处同步 + p41） |
| 25.2 | **新** agentix/agent/multiagent/bus.py（待建） | 进程内消息总线（send/status/cancel/discover 最小原语） |
| 25.3 | agentix/agent/multiagent/manager.py（M6 已删，现见 `agentix/orchestration/strategies/multi_agent/manager.py`） | 经 bus 通信（flag 或构造注入，默认关=现直连行为等价）；冲突/级联检测 meta |
| 25.4 | tests：tests/test_agent_phase25_multiaagent_guards.py（待建） | 六失败模式各 ≥1 用例（离线 fake peer） |

**DoD**

- [x] 六失败模式各 ≥1 用例：并发越限拒绝 / 深度越限拒绝 / 预算越限熔断 / 取消 fan-out / 逻辑冲突被标记 / 级联信息被独立验证标记
- [x] flag-off：manager 行为与基线等价（直连路径字节不变）
- [x] bus 不依赖 Redis/Docker（进程内），离线全测
- [x] 守卫 + ruff + mypy + 全量绿

**落地注（2026-09-27）：**

- 25.1 `ControlPlane` 补 `check_admission(depth=)` / `max_concurrency`（0=不限=接线前行为）/ `spawn(..., depth=0)` 写 `meta["depth"]`；`get_control_plane()` 每次读 settings 五新 key（`CONTROL_PLANE_MAX_CONCURRENCY/DEPTH`=0、`MULTIAGENT_PEER_MAX_STEPS/MESSAGES`=10/20、`MULTIAGENT_BUS_ENABLED`=False，五处同步）；`chat_service._invoke_tracked` 对 `AdmissionDenied` **fail-closed**（`coro.close()` 后上抛），注册故障仍降级 `return await coro`。
- 25.2 `agentix/agent/multiagent/bus.py::InProcessBus`：纯 asyncio 进程内总线（subscribe/unsubscribe/discover/send 同步 `put_nowait` 未注册→False+dropped/recv/cancel 清在途/resume/status/stats；`BusStats` 导出）——零 Redis/Docker。
- 25.3 `Manager` 构造注入 `budget_factory` / `bus`（默认 None=直连字节等价）；`detect_artifact_conflicts` 纯函数（同 key 不同值 + 极性 accept/reject 并存）；`_record` 先 `bus.send` + run() 起点为双方建信箱（幂等）；`_peer_cancelled` 早返回 `reason=peer_cancelled:{id}`；所有返回经 `_outcome` 收 `meta`（conflicts/conflict_marked/bus status）；`lg_supervisor._budget_factory`/`_make_bus`（flag 默认关）接线 + `defense_meta` 合入两处 `multi_agent` meta。
- **修复真缺口**：轮次耗尽的最终 `detect_failure_mode` 此前**漏传 `echo_overlap`（恒 0）**——echo_chamber 永远退化为 livelock，六模式之三形同虚设；现传末轮 `echo` 并预置 `echo = 0.0` 防空轮 NameError（`test_echo_chamber_high_overlap` 驱动发现）。
- 新增 `tests/test_agent_phase25_multiaagent_guards.py` **26 用例**（六模式运行时、冲突标记不合并、bus 原语、Manager×bus 注入、ControlPlane 准入、settings 镜像）；全量 **2271 passed / 14 skipped / 2 deselected**。

**Gotchas**

1. `budget_for` 与 ControlPlane 预算合并时先画清「谁裁决」，双源必漂移。
2. 冲突检测只**标记**不自动改写（自动裁决属进化范畴，走 Phase 12/20 离线链）。
3. manager 文件内禁 import LangGraph（守卫现状保持）。

---

### Phase 26 — 评估三期：judge 去 stub + 配对统计 + 解阻清单（批三）

**目标**：Ch7 §7.7 judge 可用化（诚实、默认关）、§7.14 统计显著性进摘要、附录交付 Phase 17 真机解阻清单。

**决策表**

| 决策点 | 选择 | 理由 |
|--------|------|------|
| judge 实现 | `judge_similarity`（`run_eval.py:390`）接 LLM Gateway 客户端；无端点 → **诚实报错/跳过，绝不给假分**；默认 `--no-judge` | 需人工抽样校准后才可常开（书同旨） |
| judge 不进门禁 | `quality.json` 五 metric 门结构不变；judge 结果仅进摘要附注 | 门禁稳定性 |
| 统计显著性 | `run_eval` 摘要加：配对比较 `--compare a.jsonl b.jsonl` + bootstrap 置信区间；<显著 则标注「噪声区间」 | Ch7 §7.14（差 1–2pp 可能是波动） |
| 解阻 | 仅文档/脚本清单，**不跑 record、不造 baseline** | 硬件到位前 blocked 不变 |

**改动清单（锚点已核实）**

| # | 文件 | 改动 |
|---|------|------|
| 26.1 | `evals/run_eval.py:390` | judge 去 stub（接 gateway；不可用诚实降级） |
| 26.2 | `evals/run_eval.py` 摘要区（`:931` 附近） | `--compare` 配对 + bootstrap CI；显著性标注 |
| 26.3 | `evals/README.md` | judge 校准流程（人工抽样 → 双 Judge 对照）说明 |
| 26.4 | **附录文档**（本文件附录 A） | Phase 17 真机解阻清单（已含，执行时勾选） |

**DoD**

- [x] judge：有端点出分且带 model 标注；无端点诚实失败；默认路径不触发
- [x] `--compare`：同分布不显著、构造差异分布显著（离线构造数据，非伪造 baseline）
- [x] 守卫 + ruff + mypy + 全量绿；`quality.json` 门行为零变化

**落地注（2026-09-27）：**

- 26.1 `judge_similarity(question, answer, *, model=None)` 去 stub：惰性接 `LLMClient`（温度 0）+ `ModelGateway.resolve_model("standard")`；`_judge_preflight` 端点预检（`OPENAI_API_KEY`/`OPENAI_API_BASE` 任一空 → `NotImplementedError`，退出码 3，含「校准」提示）+ 分档解析；评委输出解析不出 0–1 → `JudgeError`（同样退出码 3，`main` 补捕获）；空回答确定性 0.0 不触网；`evaluate` 开头预检 fail-fast（无半截结果）；summary 仅增 `judge_model`/`judge_count`/`judge_mean`（`judge_*` 前缀只增不改，五 metric 门结构零变化）；`format_summary_line` 追加辅助指标行。**惰性导入**：离线默认路径零 agentix 依赖触碰。
- 26.2 `--compare A B`（nargs=2，新参数组）：两份预测各自 `evaluate` 离线打分 → 按 id 配对（交集排序、同 id 多行取首行、各边独有数如实报告）→ 五指标 percentile bootstrap 95% CI（**纯 stdlib** `random.Random` 固定 seed=1729，不引 numpy，可复现）→ CI 含 0 标注「不显著（噪声区间）」；Δ = A − B；纯离线不写结果文件；golden 缺失/无配对/与 `--live` 同用 → 退出码 2。Gotcha 1 落地：`test_ci_gate_contract.py` 门键集合断言原样全过 + 新增 judge_* 键集断言（只增不改）。
- 26.3 `evals/README.md` §4.4 重写（真实端点出分/诚实失败/校准四步流程：≥30 条抽样 → 双人背对背人工标注 → 一致性阈值 → 双 Judge 对照）+ 新增 §4.4.1（`--compare` 口径）+ §9 状态更新。
- **本地 `.env` 带真实端点凭据**（Gotcha 级）：所有 judge 测试显式 monkeypatch `LLM_API_KEY`/`LLM_BASE_URL` + 伪造 `LLMClient`——**禁止触网**；伪造 client 零构造断言守住默认路径。
- 新增 `tests/test_agent_phase26_eval_judge.py` **19 用例**；`test_p21_eval_harness.py` 两处桩函数测试改写为端点语义（`.env` 真端点下原测试会触网）。全量 **2290 passed / 14 skipped / 2 deselected**。

**Gotchas**

1. judge 输出进 `quality.json` 即污染门禁 → 硬约束写测试（gate keys 集合断言）。
2. `--compare` 输入必须是真实 predictions 文件形状；示例数据放 `evals/fixtures/` 而非 baselines/。

---

## 4. 明确不做（另期 / 不适用）

| 项 | 理由 |
|----|------|
| Ch8 模型后训练（Mid-training/SFT/RL/RLVP） | 无训练基建，超出本项目边界 |
| Ch5「Coding Agent 为通用架构中心」 | 本项目是**以 Agent 内核 + RAG 等多路检索为核心的综合服务平台**，任务空间跨领域、非以开放编码为核心；代码仅工具之一 |
| Ch6 模态扩展（语音/Computer Use/机器人） | 非本项目场景 |
| Ch10 A2A 跨组织协议、Decentralized 拓扑 | 延续 Phase 11 决策（先 Peer+Manager） |
| 图内 LangGraph `interrupt()` | async 节点实测否决（`multi_tool.py:151` 注释），单列评估 |
| 前端 Skills/Hooks 管理 UI | 本版纯后端/evals；UI 另期 |
| shell 型 Hooks、主动工具发现（按需化第 2/3 层） | Phase 21 落第 1 层，其余另期 |
| 新数据库 / 新框架 / 新中间件 | 工程总则；Redis Stream 复用既有 Redis |
| 手工伪造任何 baseline / quality.json | 拒伪造纪律（延续） |

---

## 5. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Skill 元数据注入打穿前缀 | flag-off 零注入；开启时点文档化「一次前缀换代」；22.2 哈希测试锁定此后稳定 |
| Hook 异常污染主路径 | emit 全捕获只记日志；hook 抛错不影响结果断言测试 |
| 遗忘误销毁历史 | 先审计 `consolidate` 落盘；淘汰仅限可重建消息/摘要；读取时裁决回归测试 |
| Agentic RAG 重试失控 | ≤2 硬上限 + 诚实声明路径 + trajectory 计数 |
| Redis Stream 分层触守卫 | 执行时以 p17/p40 定层，Protocol 不动 |
| multi-agent 双预算源漂移 | 单处映射 + 冲突即测试红 |
| judge 污染门禁 | gate keys 集合断言 + 默认关 |
| record 仍 blocked | 附录 A 清单待真机；`not_enforced` fail-closed 不变 |

---

## 6. 交付物模板（每 Phase）

1. 代码 + 默认关 feature flag（如适用；flag 三处同步 settings/selfcheck/`.env.example` + p41 反向清单）
2. 新测试文件 ≥1（flag-off 等价 + flag-on 行为 + 负路径）
3. 文档：本规划勾选 / `module-inventory.md` §6（若产生新模块清单项）/ CHANGELOG Unreleased 条目
4. 全量验证：

```powershell
& ".\.venv\python.exe" -m pytest tests/ -m "not integration" -q
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests
& ".\.venv\python.exe" -m ruff check agentix scripts evals kb_ingest tests --select F401,F811,F841,E9
& ".\.venv\python.exe" -m mypy agentix/domain --config-file=pyproject.toml
# 触及前端时（本版预计不触及）
Set-Location web; npx vue-tsc --noEmit; npx vitest run
```

5. 基线数字若变：README §11 + `current-tree.md` + `Agent_Core_2.0_规划.md` §9 头 + `Phase15-20_深化规划.md:4` + 本文件头部，共 5 处同步（CHANGELOG 旧条目为历史记录不改）

---

## 7. 执行状态

| 项 | 状态 |
|----|------|
| 本规划文档落盘 | 2026-09-27 v1.0（六主题 + 分批已拍板） |
| Phase 21 Skills + Hooks | **已完成** 2026-09-27（批一；17 用例 DoD 全过，flag 三 key 默认关） |
| Phase 22 上下文二期 | **已完成** 2026-09-27（批一；19 用例 DoD 全过，`CONTEXT_AUTOCOMPACT` 默认关） |
| Phase 23 交互二期 | **已完成** 2026-09-27（批二启动项；47 用例 DoD 全过，`NOTIFY_USER_ENABLED` 默认关，`redis_stream` 第三 backend 落 `agent/events/`——分层偏离见 §3 落地注） |
| Phase 24 记忆/知识深化 | **已完成** 2026-09-27（批二；25 用例 DoD 全过，`MEMORY_CONSOLIDATION_ENABLED` / `AGENTIC_RAG_ENABLED` 默认关，淘汰归档红线落地——挂点偏离见 §5 落地注） |
| Phase 25 multi-agent 防线 | **已完成** 2026-09-27（批三；26 用例 DoD 全过，`MULTIAGENT_*` / `CONTROL_PLANE_*` 默认关，echo_overlap 修复见 §3 落地注） |
| Phase 26 评估三期 + 解阻清单 | **已完成** 2026-09-27（批三；19 用例 DoD 全过，judge 默认关不进门禁、`quality.json` 零变化，附录 A 解阻清单待真机） |
| Phase 17 全栈 record 解阻 | **blocked**（无 Docker/LM Studio/中间件；见附录 A；拒伪造 baseline） |

每完成一 Phase：更新本表 + 跑 §6 验证命令；数字变化按 §6.5 同步。

---

## 附录 A · Phase 17 全栈解阻清单（真机到位后执行，当前 blocked）

> 纪律：**拒绝手工伪造任何 baseline / quality.json**；runner 缺基线判 `not_enforced` 且 fail-closed（≠通过）；本清单只描述顺序，不预跑。

1. 环境：Docker（Redis/Neo4j/Milvus）+ LM Studio 或 OpenAI 兼容端点 + keys（对照 `docs/runbooks/` 与 `.env.example`）
2. 全栈冒烟：`pytest -m integration` 中相关标记用例通过
3. `evals/run_eval.py --record` → 生成并提交 `evals/baseline_predictions.jsonl`
4. offline score 跑通 → 本地五次 `quality_gate.py` 播种五个 metric（`route_accuracy` / `citation_hit_rate_applicable` / `citation_correctness_applicable` / `must_contain_applicable` / `reject_accuracy_applicable`）
5. **手动 commit** evals/quality.json（CI 写回随 runner 丢失，不会替你持久化建档）
6. 验证门状态 NOT-ENFORCED → 建档 → **ENFORCED**；`evolution_run.py` 由 `not_enforced` fail-closed 转真门
7. 生产轨迹回流：`TRAJECTORY_SINK=jsonl` 导出到 `evals/baselines/traces/` 子目录（**禁止**直接写 `evals/baselines/` 根——与 `quality.json` 碰撞会 raise）
8. 前缀回归 baseline 建档；Boundary Set + Retention Set 双重回归过门后，才允许把任何进化提案从 staging 晋升

> 详细武装顺序已文档化于 `evals/README.md` §9、`docs/architecture/runtime-guardrails.md` §12.5、`docs/guides/testing.md` §4（Phase 17.3 交付），本附录为入口索引。
