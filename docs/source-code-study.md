# Claw Code 与 Hermes Agent 源码研读说明书

> 本文档对应岗位要求「阅读并研究过以下源码优先：Claude Code（开源版本 Claw Code）、Hermes Agent 框架」，
> 按《My-Agentix 项目说明书》同一规格编写：编号章节、表格化对照、关键结论带 `文件:行号` 引用。
>
> **事实基准**：全部内容来自对两个仓库**真实源码**的研读（2026-10-01 调研批次，
> 浅克隆至本机临时目录，不随仓库分发）；未读到的不写。每个仓库的研读锚定一个 commit，
> 见 §1.1——引用行号以该 commit 为准，上游更新后行号会漂移。
>
> **引用约定**：外部路径一律以仓库别名开头——`claw-code/…` 与 `hermes/…`
> （分别为两个克隆仓库的根目录）；本仓路径保持 `agentix/…`、`tests/…` 等原前缀，
> 受一致性守卫 `tests/test_p41_doc_consistency.py` 自动校验。
>
> **对照口径**：§4 的 My-Agentix 现状列是**研读时点（2026-10-01）的实测事实**；
> §5 借鉴动作清单是后续优化批次（P1–P6）的输入，落地后 §4 需同步更新。

## 0. 阅读指南

### 0.1 章节地图

| 章 | 主题 | 关键源码位置 |
|---|---|---|
| 1 | 研读对象与方法 | 两个仓库的 commit 锚点 |
| 2 | Claw Code（Claude Code 开源重写） | `claw-code/rust/crates/runtime/src/conversation.rs` |
| 3 | Hermes Agent（Nous Research 自进化智能体） | `hermes/agent/conversation_loop.py` |
| 4 | 岗位要求逐项对照矩阵 | 职责 7 条 + 要求 8 条 |
| 5 | 借鉴动作清单（P1–P6 输入） | 每条注明源出处与本仓落点 |

### 0.2 三个先读结论

1. **两条不同的设计路线**：Claw Code 是「Claude Code 架构的静态编译重写」——
   厚处在**权限规则引擎**（5 级模式 + allow/deny/ask + 沙箱能力探测）与**工具规格表**（55 个静态 ToolSpec）；
   Hermes 是「自进化通用智能体」——厚处在**技能/记忆/调度闭环**（技能自创建、后台评审、cron 自愈）与**上下文工程**
   （缓存断点纪律、可插拔上下文引擎、压缩生命周期）。两者共同的骨架是
   「同步单轮循环：LLM → tool_use → 串行执行 → 回填 → 直到无工具调用」。
2. **与 My-Agentix 的关系**：三者解决同一组问题域（工具注册与治理、上下文压缩、多智能体、技能），
   但重心不同——My-Agentix 强在**治理链与测试纪律**（风险分级/HITL/flag 字节等价/p41 文档守卫），
   弱在**任务执行生产接线、技能可执行语义、主动式能力**（详见 §4 满足度列与 §5 动作清单）。
3. **本文档的验证方式**：本仓路径引用全部经 p41 守卫校验存在；
   外部结论均可按 §1.1 的 commit 在对应仓库复核。

### 0.3 建议阅读顺序

- **只想看岗位对照**：§4 → §5。
- **想懂一个 CLI Agent 怎么写**：§2.2（Claw Code 主循环）→ §3.2（Hermes 阶段管线）→ §2.3/§3.3（工具系统）。
- **想找可直接搬的设计**：§5 借鉴动作清单（已按 P1–P6 批次归组）。

## 1. 研读对象与方法

### 1.1 对象表

| 别名 | 仓库 | commit（研读锚点） | 语言/规模 | 定位（自述） |
|---|---|---|---|---|
| `claw-code/` | [instructkr/claw-code](https://github.com/instructkr/claw-code) | `08106b0c3771ef5b4a5aa176acccd460e88b7325`（2026-08-16） | Rust 为主（`rust/` workspace，11 crate）+ Python 参考区（`src/`） | 「Claw Code is the public Rust implementation of the `claw` CLI agent harness」（`claw-code/README.md:96`）；Claude Code 泄露事件后的架构重写，**不含 Anthropic 专有代码** |
| `hermes/` | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) | `af92ea8e5b852518c523c3e8b2cbd95f0f3d79dc`（2026-09-30） | Python（顶层 ~60 个 facade/模块 + `agent/`、`tools/`、`gateway/`、`cron/`、`plugins/`、`skills/`；测试约 5600+ 文件） | 「The self-improving AI agent built by Nous Research」——学习闭环、技能自创建、cron 调度、多平台网关；MIT |

### 1.2 方法与分工声明

- 研读方式：浅克隆（`--depth 1`）→ 按主题通读真实源码文件 → 摘取 `文件:行号` 证据；
  README/架构文档只作导航，结论一律回到代码。
- Claw Code 的 **canonical 实现是 Rust 侧**：其 Python `src/` 自述为
  「porting workspace, not production code」（`claw-code/src/AGENTS.md:4`），
  工具清单读 JSON 快照（`claw-code/src/tools.py:12`）、查询引擎不调 LLM
  （`claw-code/src/query_engine.py:61`）——故 §2 结论除特别标注外全部取自 `rust/`。
- Hermes 的官方开发指南 `hermes/AGENTS.md` 与子目录 AGENTS.md 提供架构地图，
  §3 在其导航下逐文件核对了主循环、工具、上下文、记忆、技能、调度六条主线。

## 2. Claw Code（Claude Code 开源重写）

### 2.1 定位与内部分工

- Rust 是唯一真实运行时：`claw-code/rust/` 为 Cargo workspace，二进制 `claw`；
  crate 分工见 `claw-code/rust/README.md:209`——`api`（供应商客户端/SSE）、`runtime`（会话/权限/提示词/配置）、
  `tools`（工具规格与执行）、`commands`（斜杠命令注册表）、`rusty-claude-cli`（REPL 主二进制）。
- 自我声明是「agent-managed exhibit」而非生产产品（`claw-code/README.md:54`），
  真正的研究对象是「产生代码的协调系统」（`claw-code/PHILOSOPHY.md:7`）——
  本研读取其**架构**，不取其运营形态。
- 工具数量以代码为准：`mvp_tool_specs()` 实测 55 个（`claw-code/rust/crates/tools/src/lib.rs:484`），
  与 `claw-code/AGENTS.md:28` 一致；`PARITY.md` 声称 40 个属文档漂移（其仓库自身也标注了该漂移）。

### 2.2 主循环

- 入口链：`main()`（`claw-code/rust/crates/rusty-claude-cli/src/main.rs:330`）→ 交互态 `run_repl`（`:7048`）
  → 每行 `cli.run_turn(&trimmed)`（`:7103`）。
- 真正的 agent loop 是 `ConversationRuntime::run_turn`
  （`claw-code/rust/crates/runtime/src/conversation.rs:325`），单轮流程：
  1. push 用户消息 → `loop`：组装 `ApiRequest` 并流式调用 LLM（`:364`）；
  2. 解析流事件为 assistant 消息，收集 `ContentBlock::ToolUse`（`:375`–`:396`）；
  3. 无条件做一次自动压缩检查（`:408`）；
  4. **终止判定：无 `tool_use` 即 `break`（最终回答）**（`:414`）；
  5. 否则对同轮多个 tool_use **串行**执行：PreToolUse hook（`:419`）→ 权限裁决（`:428`）→
     `tool_executor.execute`（`:468`）→ Post 系列 hook（`:475`）→ push tool_result（`:505`）→ 回到第 1 步。
- 其余终止条件：迭代上限（`:356`；子代理上限 32，`claw-code/rust/crates/tools/src/lib.rs:4089`）、
  API 错误返回 `RuntimeError`（上下文超限触发压缩重试，`claw-code/rust/crates/rusty-claude-cli/src/main.rs:7794`）、
  中断（REPL 退出 `:7106`、Ctrl-C 经 HookAbortMonitor `:7594`）。
- 关键状态：`ConversationRuntime` 字段集中在 `conversation.rs:130`；单轮产出 `TurnSummary`（`:114`）；
  会话 `Session` 支持 JSONL 持久化与 fork（`claw-code/rust/crates/runtime/src/session.rs:117`）。

### 2.3 工具系统

| 要素 | 设计 | 出处 |
|---|---|---|
| 定义 | 静态 `ToolSpec{name, description, input_schema, required_permission}` 集中一张大表 | `claw-code/rust/crates/tools/src/lib.rs:484-1346` |
| 注册表 | `GlobalToolRegistry` = 内置 spec + 插件工具（重名拒绝）+ 运行时 MCP 工具；发给模型前按 `--allowed-tools` 过滤 | 同文件 `:113/:141/:160`，过滤 `:5378` |
| 分发 | `execute_tool` 巨型 `match name`；每支先做**动态权限分级**再进 enforcer | `:1363/:1369`，分级 `:2190/:2554` |
| 并发 | 同轮 tool_use 严格串行；唯一的「并发」是 Agent 工具把子代理丢后台线程 | `conversation.rs:418`，`tools/src/lib.rs:4175` |
| 渐进加载 | 仅 6 个核心工具常驻，其余为 deferred，经 `ToolSearch` 按需召回（支持 `select:` 语法） | `tools/src/lib.rs:5550/:381` |
| 输出治理 | bash 16 KiB、读文件 10 MiB、Web 8192 B、会话字段 16 KiB + 脱敏 | `runtime/bash.rs:449`、`file_ops.rs:14`、`tools/src/lib.rs:1979`、`session.rs:16` |

### 2.4 权限与安全（该仓库最厚的一层）

- **5 级模式**：`ReadOnly < WorkspaceWrite < DangerFullAccess`（+ Prompt/Allow 两特殊态），
  `claw-code/rust/crates/runtime/src/permissions.rs:9`；每个工具声明 `required_permission`
  （如 `bash` 要求 DangerFullAccess，`tools/src/lib.rs:505`）。
- **固定裁决顺序**（`permissions.rs:186-312`）：`denied_tools` 无条件拒绝 → deny 规则 →
  hook 覆盖（Deny/Ask/Allow）→ **ask 规则强制弹窗（即使当前模式已允许）** → allow 规则或模式满足 →
  模式升级需审批 → 否则拒绝；无 prompter 一律 deny（`:314`）。
- **第二道闸** `PermissionEnforcer`（`claw-code/rust/crates/tools/src/permission_enforcer.rs:27`）：
  文件写的 workspace 边界（`:108`）、bash 只读启发式（`:145/:241`，拒绝命令链/解释器/git 写子命令/重定向）。
- **沙箱是能力探测**：`unshare_probe` 探测内核能力而非看 binary 是否存在
  （`claw-code/rust/crates/runtime/src/sandbox.rs:334`）。
  对照本项目：My-Agentix **没有这一层**——工具全部在主进程内执行，隔离只到
  「闸门 + 容器配额」，`unshare`/namespace/`seccomp` 一律未接线。
  实测口径与逐项缺口见 `docs/PROJECT_SPEC.md` §24（含 T0 已落地的执行边界加固）。
- 规则语法 `tool:pattern`（Any/Exact/Prefix 三 matcher，`permissions.rs:355`）。

### 2.5 上下文与记忆

- **系统提示词分段组装**：intro → 系统段 → environment → 项目上下文（git status/diff）→
  项目指令文件 → 运行时配置段（`claw-code/rust/crates/runtime/src/prompt.rs:211-233`）。
- **指令文件发现**：从 cwd 沿目录向上直到 git root，每层尝试 `CLAUDE.md/CLAW.md/AGENTS.md/…`
  多来源合并（`prompt.rs:289-314`）；**预算与去重**：单文件 4000 字符、总 12000 字符、
  超预算截断附提示、按内容 hash 去重（`prompt.rs:43/:519/:542`）。
- **三层压缩**：① 阈值自动压缩——累计输入 ≥100k token 触发 `maybe_auto_compact`（`conversation.rs:18/:571`）；
  ② `compact_session` 默认保留最近 4 条、预算 10k token，把更早消息总结为系统摘要，
  **绝不拆散 tool_use/tool_result 配对**（`compact.rs:96/:129/:15`）；
  ③ 上下文超限时**渐进重试 4→2→1→0 条、最多 4 轮**，并按服务端报的窗口把阈值自适应设为 70%（`main.rs:7838-7903`）。
- **会话持久化**：JSONL 增量追加，存于 `<cwd>/.claw/sessions/<workspace_fingerprint>/`
  避免并行实例互踩（`session_control.rs:33`），字段级截断与 `[redacted]` 脱敏（`session.rs:16`）。
- **渐进式体现**：常驻 = 系统提示词 + 指令文件（受预算约束）；按需 = Skill 工具读 SKILL.md 全文
  （`tools/src/lib.rs:3790`）、ToolSearch 召回延迟工具、子代理/任务按需触发。

### 2.6 多智能体与计划

- **Agent 工具（后台子代理）**：按类型归一（`general-purpose/Explore/Plan/Verification`，`tools/src/lib.rs:5705`）→
  **按类型给工具白名单**（Explore 无写权限，`:4257`）→ 独立 system prompt（`:4233`）→
  写任务文件 `.clawd-agents/<id>.md/.json` → 后台线程跑独立 runtime（迭代上限 32、独立权限策略，`:4175/:4338`）→
  终态回写清单；父会话只拿「running」清单轮询。
- **任务/工人/团队/定时**：`TaskCreate/Get/List/…` 走内存 `TaskRegistry`（`runtime/task_registry.rs`），
  `Worker*` 8 工具由 `WorkerRegistry` 支撑——Worker 是独立 claw 进程/tmux pane（`worker_boot.rs:290`）。
- **计划模式 = 可回滚的配置级权限切换**：`EnterPlanMode` 保存 `PlanModeState` 并把
  `permissions.defaultMode` 置为 `plan`，`ExitPlanMode` 恢复（`tools/src/lib.rs:6013-6081`）——
  不是独立规划器模块。
- **TodoWrite**：schema 强制 `status∈{pending,in_progress,completed}`，持久化 `.clawd-todos.json`，
  全部完成即清空（`tools/src/lib.rs:628/:3744/:3773`）。

### 2.7 异常处理

- 工具失败/权限拒绝**一律降级为 `is_error=true` 的 tool_result 回喂模型**，不打断循环
  （`conversation.rs:468-511`），并触发 `PostToolUseFailure` hook（`:475`）。
- API 失败：上下文超限/无内容/解码失败被识别为可恢复，走 4 轮渐进压缩重试（`main.rs:7818-7903`）；
  流超时重发一次（`main.rs:12653`）。
- **静态错误分类表**：`classify_error_kind` 把消息映射为稳定 kind（`api_auth_error`/`api_rate_limit_error`/…）
  供上层与 JSON 输出契约使用（`main.rs:437`），`split_error_hint` 分离短原因与 hint（`:580`）。

### 2.8 工程实践

- 测试主体是 crate 内联 `#[cfg(test)]`（runtime 625、CLI 347、api 169、tools 118……）；
  体系亮点是 **mock parity harness**——确定性 Anthropic mock + 场景清单 + 子进程 CLI 对拍
  （`claw-code/rust/crates/rusty-claude-cli/tests/mock_parity_harness.rs`）。
- CI 5 job：文档一致性 / `cargo fmt` / `cargo test` / `cargo clippy` / Windows 冒烟
  （`claw-code/.github/workflows/rust-ci.yml:64-124`）。
- **PRD 即验收契约**：`claw-code/prd.json` 结构化 story + acceptanceCriteria + `passes` 标志，
  由 `progress.txt:95` 记录全数通过。
- 配置合并优先级固定且逐层 deep merge（`runtime/config.rs:436-462`）。

## 3. Hermes Agent（Nous Research 自进化智能体）

### 3.1 定位与架构原则

- 自述「self-improving AI agent」——从经验创建技能、使用中改进、跨会话积累用户模型
  （`hermes/README.md` 起始段）。
- 根 `hermes/AGENTS.md` 写下两条不变量：**(1) per-conversation prompt caching is sacred**
  （唯一例外是压缩）；**(2) core is a narrow waist**——核心循环只做编排，
  工具/平台/技能/供应商全部经边缘扩展点挂载。
- 规则按目录注入：根 AGENTS.md 只写不变量，`agent/`、`tools/`、`cron/`、`gateway/`、`plugins/`
  各有 AGENTS.md，由 `hermes/agent/subdirectory_hints.py` 按路径分发——规则跟着代码走且只在相关时出现。
- 依赖策略罕见地把供应链爆炸半径写进表头：所有直接依赖 `==X.Y.Z` 精确锁定，
  「只有每个会话都会用到的包才能进核心 dependencies」（`hermes/pyproject.toml:14`）。

### 3.2 主循环

- 入口 `AIAgent`（`hermes/run_agent.py:241`）本身近空壳，行为由 mixin 链提供（`:137`）；
  `max_iterations` 默认 `sys.maxsize`（`:268`），转由 iteration_budget 限流。
- 主循环是**同步单线程的阶段管线**（`hermes/agent/conversation_loop.py:1636-1678`）：
  `begin_iteration → prepare_iteration → assemble_api_request → run_preflight_gate →
  announce_api_call → _run_api_retry_loop → apply_retry_restarts → normalize_model_response →
  （有 tool_calls ? run_tool_round : finish_text_response）`。
- 每阶段由 `_run_phase`（`conversation_loop.py:1480`）包裹，返回
  `action ∈ {continue, break, return}` 的**结论对象**——把控制流从深层 return 里抽离；
  异常统一交 `handle_outer_loop_error`（`:1680`）；循环状态装 `_LoopState`（`:1615`）。
- 工具轮 `run_tool_round`（`hermes/agent/turn_tool_round.py:46`）三条顺序：
  先持久化 assistant 的 tool_calls（`:224`）→ 再执行 → 最后做工具后压缩；
  `memory/todo_list/skill_manage/session_search` 列为**家政工具**（`_HOUSEKEEPING_TOOLS`，`:23`）
  不计实质进度。
- 执行器 `hermes/agent/tool_executor.py`：并发批调度 + 授权门
  `_ConcurrentToolAuthorizationGate`（`:481`）+ 结果落库（`:1051`）。

### 3.3 工具系统

三层：**发现 → 注册/分发 → 编排**。

| 层 | 设计 | 出处 |
|---|---|---|
| 发现 | AST 扫描 `tools/` 目录里的 `registry.register(...)` 调用（磁盘 memo 键 = mtime_ns+size） | `hermes/tools/registry.py:96` |
| 注册 | `ToolRegistry.register` 做 schema 校验与 override 策略；`check_fn` 可用性检查带 TTL | `registry.py:666/:398` |
| 分发 | `dispatch`：未知工具返回 `tool_error`；参数按签名过滤（窄签名不被撑爆）；**任何异常捕获转 `{"error":…}` 字符串**；返回值强制为「字符串或多模态信封」 | `registry.py:893-921/:904/:876` |
| 错误卫生 | `_sanitize_tool_error` 清围栏/CDATA、`_bound_error_text` 截断（防异常信息成为结构性噪声/注入面） | `registry.py:32` |
| 编排 | `model_tools.py` 提供 `get_tool_definitions()/handle_function_call()`；`toolsets.py` 核心白名单 + `get_toolset` 聚合 | `hermes/model_tools.py`、`hermes/toolsets.py:12/:286` |
| 容量 | 单结果 100k 字符、turn 预算 200k、预览 1.5k、MCP 结果 50k | `hermes/tools/budget_config.py:13-20` |

- **`execute_code` 程序化调用**：在持久 kernel 跑 Python 并经 RPC 白名单回调少量工具
  （`hermes/tools/code_execution_tool.py:703/:43`），护栏 = terminal-policy fail-closed（`:719`）+
  网关生命周期命令硬拦截且**探针超时失败即拒绝**（`:738`）+ 审批门（`:772`）；
  `project|strict` 两模式（`:839`）。

### 3.4 上下文工程（该仓库的立身之本）

- **缓存断点纪律**：默认 4 个 `cache_control` 断点 = 静态 system 前缀 + 尾部 3 条消息，共享 TTL；
  打点逻辑拒绝给空内容打点（`hermes/agent/prompt_caching.py:3/:16/:323`）。
- **上下文引擎可插拔**：`ContextEngine` ABC 定义完整生命周期
  （`update_from_response / should_compress / compress / prune_tool_results_only / select_context /
  on_session_start/end/reset / should_compress_preflight`，`hermes/agent/context_engine.py:48`）；
  默认 `ContextCompressor`（`hermes/agent/context_compressor.py:2054`）策略 =
  剪枝工具结果 + 保护头尾 + 中段 LLM 摘要 + 迭代式摘要更新（压缩入口 `:5439`）。
- **组装**：`prompt_builder.py` 负责记忆指引/环境提示/**技能 manifest（带快照比对命中缓存）**/上下文文件
  （`hermes/agent/prompt_builder.py:193/:1118/:1203/:1805`）。
- **每 turn 状态强制复位 + 热凭据刷新**：网关跨 turn 缓存 agent 对象，故压缩类 turn 级状态
  每轮开头强制复位（`conversation_loop.py:1563-1574`）。
- **诊断只读契约**：`/context` 输出 5×20 字形网格 + 分类估算表，明确
  「只读、不发 provider 请求、不影响缓存」（`hermes/hermes_cli/cli_info_mixin.py:657`）。
- 回溯原语 `history_before_user_originated_turn`（`context_compressor.py:5677`）
  是 `/retry`、`/undo` 共用基础。

### 3.5 记忆系统

- **多 provider 扇出**：`MemoryManager` 聚合多个 provider 并可预取
  （`hermes/agent/memory_manager.py:336`），provider 必须实现 ABC
  （`hermes/agent/memory_provider.py`）且受上下文边界约束；具体实现在 `hermes/plugins/memory/`
  （honcho/mem0/supermemory 等）——「插件永不触碰 core」的落点。
- **检索**：SQLite FTS5 + CJK 分词/trigram 回退，**全部 fail-open**
  （索引坏了也要能查，`hermes/hermes_state_fts.py`）。
- **双存储路由**（记忆哲学）：`USER.md`（用户画像，长期稳定）vs `MEMORY.md`（工作记忆，会话相关），
  由后台评审 prompt 决定写哪份（`hermes/agent/background_review.py:340/:353`）；
  评审在 turn 结束后**异步线程**触发，不阻塞用户（`:1308`）。

### 3.6 Skills 系统（程序性记忆）

- 技能 = 目录 + `SKILL.md`（frontmatter 含 name/description…）：`hermes/skills/` 内置 +
  `hermes/optional-skills/` 按需安装（Skills Hub 适配，`hermes/tools/skills_hub_official.py`）；
  description ≤60 字符等规范由 `hermes/tests/skills/test_authoring_standards.py` 强制——**规范即测试**。
- 加载与热更新：`scan_skill_commands` 把技能描述编译成斜杠命令候选（`hermes/agent/skill_commands.py:448`），
  `reload_skills` 运行时重载（`:528`）；技能增删改由 `skill_manage` 工具完成
  （`hermes/tools/skill_manager_tool.py`）。
- **自我改进闭环分两步**：`/learn` 由用户显式把一次成功经验沉淀成技能草稿
  （`hermes/agent/learn_prompt.py`）；后台评审 prompt 在无人值守时改进已有技能
  （`hermes/agent/background_review.py:418`，与记忆评审合并的 `:504`）。
- 技能提示词注入依赖 manifest 快照缓存（`prompt_builder.py:1203/:1241`），避免每轮重扫磁盘。

### 3.7 Proactive / cron（主动式智能体）

`cron/AGENTS.md` 把架构拆成两半：**`jobs.py` 是存储/计划，`scheduler.py` 是执行**。

- job 字段：cron 表达式、`script`（`no_agent` 不走模型）、`context_from`、`workdir`、`deliver` 投递目标
  （`hermes/cron/job_definition.py`）；持久化 `cron/jobs.py:75`（`jobs.json`，flock + fire-fence 防重复触发）。
- 调度入口 `tick()`（`hermes/cron/scheduler_tick.py:7`），自愈机制四件套：
  1. **代码版本让位**：比较 boot/disk revision，发现旧代码在跑就抛 `CronTickYielded`
     让新 gateway 接管（`hermes/cron/scheduler.py:175-196`）；
  2. **inflight 簿记**：`try_register_running_job/is_job_running` + 卡死任务探测（`:766/:752/:713`）；
  3. **失败事故单**：建单/自动销案/重复告警节流（`:388/:412/:361`）；
  4. **tick 先推进 `next_run_at` 再执行**——崩溃也不会 tight-loop。
- 投递走 durable ledger：`gateway/delivery_ledger.py:556`（默认开启），
  「受理 ≠ 模型 turn 完成」需回执（`gateway/AGENTS.md`）。

### 3.8 多智能体

- **子 agent 即完整的 `AIAgent` 实例**：`_build_child_agent` 在**主线程**构建（构造非线程安全），
  运行时才进线程（`hermes/tools/delegate_tool.py:156`）；深度派生
  `child_depth = parent._delegate_depth + 1`（`:189`），超限即 `leaf`。
- **隔离手段**（`:236-249`）：`ephemeral_system_prompt`、`skip_context_files`、`skip_memory`、
  独立 `session_db` 与 `parent_session_id`、全新 `iteration_budget`、weakref 父引用、心跳保活。
- **明确的子 agent 契约**（`:299-314`）：`status ∈ {completed, interrupted, failed}`、
  `exit_reason ∈ {completed, max_iterations, interrupted, error}`、
  `truncated == (exit_reason == "max_iterations")`；运行期注册 live registry 供 kill/pause/steer（`:323`）。
- 另有 **kanban**（`hermes/plugins/kanban/`，SQLite 看板）与 **A2A**（gateway 平台适配器，默认关）。

### 3.9 异常与恢复（五级）

1. **工具级自愈提示**：三套计数——相同参数重复失败 / 同工具不同参数失败 / 幂等结果哈希不变
   （`hermes/agent/tool_guardrails.py:357/:375`），判级 warn/block/halt 并生成**工具专属恢复建议**
   回喂模型（`:577`）；**中间发生过一次成功 mutation 即清零**（`:418-422`）。
2. **结果去重**：从第 2 次字节级重复起替换为 stub（工具仍真实执行，只压缩上下文表示，
   轮询类不豁免，`:438`）。
3. **API 退避梯**：15s 起步、60s 封顶，仅对 overloaded/server_error/timeout 生效
   （`hermes/agent/turn_recovery_autorecover.py:26-29`）。
4. **中断的可恢复表述**：被截断的响应转成「[interrupted scaffold] + 可见残留 + 用户纠正」
   插回对话而非丢弃（`conversation_loop.py:70/:324-338`），并反复强调「工具仍完全可用」（`:921`）。
5. **持久层 turn 收口**：失败 turn 补写 Hermes-authored 边界避免 user-user 相邻
   （`conversation_loop.py:1745`），幂等按 durable 尾巴角色且**显式排除 compression/context_overflow 类**；
   `/retry` `/undo` 走 `rewind_user_turn` **先持久化、后发布**（`hermes/hermes_cli/cli_session_mixin.py:753-813`）。

### 3.10 工程实践

- 测试约 5600+ 文件、38 个子目录，覆盖 `conformance`/`perf_guards`/`security`/`compat`/`e2e`/`evals`
  等非功能维度；契约测试用**变异测试**守「血泪规则」（`hermes/tests/gateway/test_stream_final_contract.py`）。
- **注释即事故档案**：设计决策普遍挂 issue 号（如 `code_execution_tool.py:733-737` 记录
  曾绕过 terminal 守卫 SIGTERM 掉 gateway 的事故）。
- 缓存失效全部显式化：工具发现磁盘 memo、skills manifest 快照、`check_fn` TTL、配置 mtime 失效。
- 诊断只读、文案走 `t("key")` 国际化、失败预算写成常量
  （`hermes/tools/budget_config.py:13`）并按上下文窗口动态收敛（`:92`）。

## 4. 岗位要求逐项对照矩阵

> 满足度口径：✅ 满足｜⚠️ 部分（组件在库/已实现但未接线或默认关）｜❌ 缺失。
> My-Agentix 列为 2026-10-01 实测（P1–P6 批次落地后终态；逐批记录见 `CHANGELOG.md`，当前目录归属见 `docs/architecture/overview.md`）。

### 4.1 职责对照（7 条）

| # | 岗位职责 | Claw Code 做法 | Hermes 做法 | My-Agentix 现状 | 满足度 |
|---|---|---|---|---|---|
| 1 | 任务执行 Task Execution（非仅问答） | 无预置任务路由；靠 55 工具 + 循环完成任意任务（`claw-code/rust/crates/tools/src/lib.rs:484`） | 通用任务执行是本体；`delegate_task` 并发派发（`hermes/tools/delegate_tool.py:440`） | 任务执行链路已接线：`lg_states.TaskRouter` 路由 `task-query` → `execute_task` 图节点（8→9）→ `agentix/application/services/task_execution.py` 分波管线 + `LayeredVerifier` 终态 + `task_note`/`task_artifact` 写工具 + `AGENT_MAX_REPLANS` 重规划注入（`agentix/application/services/agent_loop_adapter.py`）；`TASK_EXEC_ENABLED=False` 默认关灰度 | ✅（默认关） |
| 2 | Skills 体系 + Registry + 动态调度组合 | 55 工具静态 spec 表 + `GlobalToolRegistry`；Skill 按需读全文；`ToolSearch` 召回延迟工具（`claw-code/rust/crates/tools/src/lib.rs:5550`） | 导入期自注册（`hermes/tools/registry.py:96`）、toolset 聚合（`hermes/toolsets.py:286`）、技能 manifest 热更新（`hermes/agent/skill_commands.py:448`）、技能自创建 | 文档型 Skill 两段式披露扎实（`agentix/agent/skills/loader.py:51`、`agentix/agent/context/instructions.py:126`）；`ToolRegistry` 治理链完整（M6 后见 `agentix/tools/definitions.py:134`、`agentix/tools/gateway/executor.py:168`，旧路径 capabilities/tools 已删）。P2 补齐：`tool_policy` 注入方（ask/stream 双路径）、`tool_search` 延迟召回 + CJK bigram ranking、manifest 指纹热重载、规范即测试、三注册表对账与裁决序列守卫；P6 补 `create_skill_draft` 草稿沉淀（`agentix/agent/skills/learn.py` → `data/skills_drafts` 隔离目录）。**残余**：多技能组合编排仍为零、`SKILLS_ENABLED` 默认关 | ⚠️（组合编排未做，其余闭环） |
| 3 | 渐进式上下文 Progressive Context | 指令文件 4k/12k 预算 + hash 去重（`claw-code/rust/crates/runtime/src/prompt.rs:519`）、三层压缩 + 超限渐进重试（`main.rs:7838`）、Skill/ToolSearch 按需展开 | 缓存断点纪律（`hermes/agent/prompt_caching.py:323`）、可插拔 `ContextEngine` 生命周期（`hermes/agent/context_engine.py:48`）、压缩+回溯原语（`hermes/agent/context_compressor.py:5677`）、`/context` 只读可见性 | L1–L4 分层/预算/隔离/压缩在库（agentix/application/runtime/budget.py 已删除，A4 直删；`agentix/application/agents/compactor.py`、`tests/test_r3_context_engine.py`）；P3 补齐：指令 sha256 去重 + 12k 总预算、`CONTEXT_COMPACT_LADDER` keep-last 阶梯 4→2→1→0、`AgentLoop(context_budget_tokens)` 逐步重建（组合根注入）、`artifact_read` 隔离正文取回、`GET /ops/context` 只读诊断。**残余**：`ContextPolicy` 仍只声明不执行、无分阶段检索、`CONTEXT_COMPACT_LADDER` 默认关 | ⚠️（接线大半，默认关） |
| 4 | Planning/ReACT/Tool 调用/回滚重试 | 循环 + 迭代上限 + 工具失败降级回喂（`conversation.rs:468`）；计划=可回滚权限切换（`tools/src/lib.rs:6013`）；静态错误分类（`main.rs:437`） | 阶段管线结论对象（`hermes/agent/conversation_loop.py:1480`）、退避梯（`turn_recovery_autorecover.py:26`）、三计数器+恢复提示（`tool_guardrails.py:577`）、先持久化后发布（`cli_session_mixin.py:753`） | 组件齐全：planner/replanner `max_replans=3`（agentix/agent/replanner/retry.py（M6 已删，现见 `agentix/policies/planner/retry.py`））、分层 verifier（agentix/agent/verifier/layered.py（M6 已删，现见 `agentix/policies/verifier/layered.py`））、指数退避/DLQ（`agentix/application/services/task_queue.py`）、熔断（`agentix/reliability/circuit_breaker.py`）、断点续跑（`agentix/application/services/breakpoint_resume.py`）。P1 补 `AGENT_MAX_REPLANS` 生产注入（replanner 未传参缺口关闭），P4 补三计数器护栏 + 重复折叠 stub + `plan_mode` 权限快照恢复 + `InMemoryRollbackStore`（`RollbackPort` 首个生产实现）+ 失败 turn 先持久化边界。**残余**：L3 `AgentLoop` 直连工具表仍旁路 `ToolExecutor` 治理链（`agentix/application/services/agent_loop_adapter.py:104`） | ⚠️（旁路未清） |
| 5 | Multi-Agent 协作 | 子代理按类型工具白名单 + 独立 prompt + 32 上限 + 后台线程（`tools/src/lib.rs:4257/:4175`）；Task/Worker/Team 注册表 | 完整实例隔离三件套 + status/exit_reason/truncated 契约（`delegate_tool.py:236/:299`）+ kanban 看板 + A2A | 已实现：supervisor + `multi_agent/` 工作流 + 3 个 phase 测试（`tests/test_agent_phase11_multiagent.py` 等）；`multi_agent` ablation 默认 False（`agentix/config/ablation.py:67`）——灰度问题非能力问题 | ✅（默认关） |
| 6 | RAG/GraphRAG 作辅助而非核心 | 有独立 `claw-rag-service`（`claw-code/concept.md:13`）定位为旁挂服务 | 检索只是工具之一（FTS5、`session_search`），核心是循环与记忆 | 检索作为工具已接入；Channel A 有 Dense/BM25/RRF/Rerank、改写/HyDE 与证据门禁，均由配置灰度。2026-10-10 补齐 BM25 活动 `artifact_id` 隔离、倒排查询、Dense/Sparse 并发、数据条目号跨格式规范化，以及 rerank HTTP 200 错误的降级识别。**残余**：真实人工检索/答案金标与 live 基线尚未录制；标题/数据条目结构字段加权仍需消融验证，LM Studio 的 embedding API 不是 `/rerank` 交叉编码器接口。路线见 `docs/design/rag-optimization.md` §8 | ⚠️（质量证据待补） |
| 7 | AI Coding 高质量可维护代码 | mock parity harness + 5-job CI + PRD 验收契约（§2.8） | ~5600 测试文件 + 分层 AGENTS.md + 变异契约测试（§3.10） | ✅ 2847 tests/150 模块、CI 门禁、evals、p41 文档守卫 33 条（含 P1–P6 新键名单，见 `README.md` §11、`evals/README.md`） | ✅ |

### 4.2 岗位要求对照（8 条）

| # | 岗位要求 | 对照结论 | 证据 |
|---|---|---|---|
| 1 | AI Coding 能力（最高优先级） | ✅ 过程性满足（本仓库即 AI 协作开发产物，2847 用例 + p41 守卫管理大规模生成代码；P1–P6 六批次全程 CHANGELOG 可溯） | `README.md` §11、`tests/test_p41_doc_consistency.py` |
| 2 | 完整 Agent 系统开发经验（Task→Planning→Tool→输出） | ✅ P1–P6 完成生产接线（路由→节点→管线→护栏/回滚/边界→cron），默认关 flag 灰度（同职责 1） | §4.1 第 1 行 |
| 3 | 深入理解 Skills/Tool、渐进上下文、Planning/ReACT、多步骤执行 | ✅ P2/P3/P4 已接线可跑（注入方/召回/排名/对账；指令预算/阶梯/逐步重建/read_artifact；护栏/回滚/边界），flag-on 行为由 p50–p56 钉住 | §4.1 第 2/3/4 行 |
| 4 | Python + FastAPI 后端 + 全栈架构思维 | ✅ FastAPI 0.142 + Vue3 + LangGraph，分层架构与端口抽象 | `README.md` §1.2 技术栈表（`docs/PROJECT_SPEC.md` §1.2） |
| 5 | 系统设计：Agent/Skill/Memory 解耦、状态管理与执行链路 | ✅ Agent/Memory 端口解耦（P6 起 `MemoryPort` 有 Redis 生产实现 `LongTermMemoryProvider`）；Skill 侧三注册表以 `audit_tool_registries` 对账守卫收敛（物理三表未合并） | `agentix/domain/ports/`、`agentix/tools/definitions.py:134`（M6 后；旧路径 capabilities/tools 已删） |
| 6 | 工程习惯：结构/日志/调试/异常、管理 AI 生成大规模代码 | ✅ | `README.md` §11、`docs/PROJECT_SPEC.md` §20 |
| 7 | 加分：Proactive / 长期记忆 / Context Engineering / 多智能体 | Proactive ✅（cron 分半调度 + 自愈四件套 + 交付账本 + Cron* 工具族，`CRON_ENABLED=False` 默认关，对照 §3.7）；长期记忆 ✅（`MemoryPort` 扇出 + Redis LTM 适配器 + 语义卡片召回 + 后台评审 + 分段 fail-open，`MEMORY_FANOUT_ENABLED`/`MEMORY_REVIEW_ENABLED` 默认关；Procedural 以 `create_skill_draft` 草稿起步、人工评审晋升）；Context Eng. ⚠️（同职责 3，接线大半）；多智能体 ✅（职责 5） | §3.7、`agentix/application/services/long_term_memory.py`、`agentix/application/services/memory_manager.py` |
| 8 | 源码研读：Claude Code（Claw Code）+ Hermes Agent | ✅（本文档交付） | §1.1 commit 锚点、§2、§3 |

## 5. 借鉴动作清单（P1–P6 批次输入，2026-10-01 已全部落地）

> 每条 = 源出处 → My-Agentix 落点 → 所属批次。批次含义与实施顺序见优化计划：
> P1 任务执行、P2 Skills、P3 渐进上下文、P4 异常闭环、P5 Proactive、P6 长时记忆。

### P1 · 任务执行

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 1.1 | 工具失败/权限拒绝一律降级为可自纠消息回喂模型，不打断循环 | `claw-code/rust/crates/runtime/src/conversation.rs:468-511` | agentix/agent/core/loop.py（M6 已删，现见 `agentix/orchestration/orchestrator.py`） 与 replanner 协同 |
| 1.2 | 阶段管线 + 结论对象（continue/break/return），把控制流从深层 return 抽离 | `hermes/agent/conversation_loop.py:1480/:1636` | agentix/agent/core/loop.py（M6 已删，现见 `agentix/orchestration/orchestrator.py`） 重构骨架 |
| 1.3 | 静态错误分类表（稳定 error kind）供上层重试决策与输出契约 | `claw-code/rust/crates/rusty-claude-cli/src/main.rs:437` | `agentix/domain/policies/`、`agentix/application/services/task_retry.py` |
| 1.4 | 子代理/后台任务的明确退出契约（status/exit_reason/truncated） | `hermes/tools/delegate_tool.py:299-314` | 任务型路由 + 任务终态建模（P1 新增） |

### P2 · Skills 体系

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 2.1 | 导入期自注册 + dispatch 异常兜底 + 返回值类型契约 | `hermes/tools/registry.py:96/:893/:876` | `agentix/capabilities/`（CapabilityRegistry 统一三表） |
| 2.2 | 延迟加载工具面：核心常驻、其余 ToolSearch 按需召回 | `claw-code/rust/crates/tools/src/lib.rs:5550` | agentix/capabilities/tools/selection.py（M6 已删，现见 `agentix/tools/selection.py`） ranking 层 |
| 2.3 | 技能 manifest 快照缓存 + 热重载 + 规范即测试 | `hermes/agent/prompt_builder.py:1203`、`hermes/agent/skill_commands.py:448`、`hermes/tests/skills/test_authoring_standards.py` | `agentix/agent/skills/loader.py` + 作者规范测试 |
| 2.4 | 动态权限分级（按参数内容而非工具名静态分级）+ 双层闸 | `claw-code/rust/crates/tools/src/lib.rs:2190`、`permission_enforcer.rs:27` | agentix/capabilities/tools/executor.py（M6 已删，现见 `agentix/tools/gateway/executor.py`）、`agentix/security/` |
| 2.5 | 固定裁决顺序：deny → hook → **ask 强制** → 模式 | `claw-code/rust/crates/runtime/src/permissions.rs:186-312` | executor 前置策略 + HITL 顺序对齐 |

### P3 · 渐进式上下文

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 3.1 | 指令文件预算（单 4k/总 12k）+ 内容 hash 去重 | `claw-code/rust/crates/runtime/src/prompt.rs:519-557` | `agentix/agent/context/instructions.py` |
| 3.2 | 上下文超限「渐进压缩重试 4→2→1→0 + 按服务端窗口自适应阈值 70%」 | `claw-code/rust/crates/rusty-claude-cli/src/main.rs:7838-7903` | `agentix/application/agents/compactor.py` |
| 3.3 | 缓存断点纪律：静态前缀 + 尾部 N 条、空内容不打点、断点绑 TTL | `hermes/agent/prompt_caching.py:323` | ✅ 已接线（2026-10-09 上下文工程 A/B/C/D）：指纹可观测 `prompt_prefix_fingerprint{builder}`（general 改取模板常量 `llm_factory._TEMPLATE_PREFIX_ROLES`）+ 缓存命中量 `llm_tokens_total{type="cached"}` / `cache_{hits,misses}_total{cache_type="prompt_prefix"}`（`llm_factory._extract_cached_tokens`）+ token 级历史窗口 `LG_TOKEN_HISTORY_BUDGET`（尾部保留）+ 状态尾注 `LG_STATUS_TAIL_ENABLED`（动态量只走本轮 human 尾部，静态前缀逐字节不变）；provider `cache_control` 断点未做（无 anthropic 直连需求） |
| 3.4 | 可插拔 ContextEngine 完整生命周期（preflight/session 级钩子） | `hermes/agent/context_engine.py:48` | `agentix/agent/context/` 引擎抽象对齐 |
| 3.5 | 家政工具单列不计进度 | `hermes/agent/turn_tool_round.py:23` | agentix/capabilities/tools/selection.py（M6 已删，现见 `agentix/tools/selection.py`） 预算判定 |
| 3.6 | 诊断只读契约（不发 provider 请求、不影响缓存/计费） | `hermes/hermes_cli/cli_info_mixin.py:657` | `agentix/application/services/observability.py` |

### P4 · 异常闭环

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 4.1 | 三计数器失败分级 + 工具专属恢复建议回喂 + mutation 成功即清零 | `hermes/agent/tool_guardrails.py:357/:577/:418` | agentix/capabilities/tools/guardrails.py（M6 已删，现见 `agentix/policies/guardrails/guardrails.py`）、agentix/agent/replanner/retry.py（M6 已删，现见 `agentix/policies/planner/retry.py`）、`task_retry.py` |
| 4.2 | 重复结果 stub 化（第 2 次字节级重复起，轮询不豁免） | `hermes/agent/tool_guardrails.py:438` | agentix/capabilities/tools/result_limiter.py（M6 已删，现见 `agentix/tools/gateway/result_limiter.py`） |
| 4.3 | 计划态 = 可回滚的配置级权限切换（保存/恢复） | `claw-code/rust/crates/tools/src/lib.rs:6013-6081` | `agentix/application/services/rollback.py`（`InMemoryRollbackStore` 首个生产实现） |
| 4.4 | 失败 turn durable 边界补写 + **先持久化后发布**的回溯顺序 | `hermes/agent/conversation_loop.py:1745`、`hermes/hermes_cli/cli_session_mixin.py:753` | `agentix/application/services/breakpoint_resume.py`、回滚实现 |

### P5 · Proactive

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 5.1 | 存储/计划（jobs.py）与执行（scheduler.py）分半的 cron 架构 | `hermes/cron/AGENTS.md`、`hermes/cron/scheduler_tick.py:7` | `agentix/application/services/cron_jobs.py` + `agentix/application/services/cron_scheduler.py`（jobs 存储 + tick 执行分半） |
| 5.2 | 自愈四件套：代码版本让位 / inflight 簿记 / 事故单 / 先推进 next_run_at | `hermes/cron/scheduler.py:175/:766/:388` | 调度器实现内建 |
| 5.3 | durable delivery ledger（受理 ≠ turn 完成，需回执） | `hermes/gateway/delivery_ledger.py:556` | `agentix/application/services/notify_bridge.py` 投递语义 |
| 5.4 | Cron/Task 定时工具族（`Cron*`、TaskCreate/Get） | `claw-code/rust/crates/tools/src/lib.rs:484-1346` | agentix/capabilities/tools/cron_tools.py（M6 已删，现见 `agentix/tools/adapters/cron_tools.py`）（Cron* 四工具族，任务型工具注册 P1/P5 交叉） |

### P6 · 长时记忆

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| 6.1 | MemoryManager 多 provider 扇出 + ABC 边界约束 | `hermes/agent/memory_manager.py:336`、`hermes/agent/memory_provider.py` | `agentix/application/services/memory_manager.py`（`MemoryManager` 多 provider 扇出 + `LongTermMemoryProvider` 实现 `MemoryPort`） |
| 6.2 | USER.md（长期画像）vs MEMORY.md（工作记忆）双路由 + 后台异步评审 | `hermes/agent/background_review.py:340/:1308` | `agentix/application/services/memory_review.py`（后台评审路由 + 摘要调度，画像/摘要双路由为既有 `long_term_memory.py`） |
| 6.3 | `/learn` 式显式沉淀入口（成功经验 → 技能草稿） | `hermes/agent/learn_prompt.py` | `agentix/agent/skills/learn.py`（`create_skill_draft` 草稿沉淀，Procedural 起步，与 P2 交叉） |
| 6.4 | FTS5 fail-open 检索（索引损坏不阻断会话） | `hermes/hermes_state_fts.py` | 记忆召回容错语义 |

### 工程与协作（贯穿）

| # | 借鉴动作 | 源出处 | 本仓落点 |
|---|---|---|---|
| G.1 | 分层 AGENTS.md + 按目录路由注入规则 | `hermes/AGENTS.md`、`hermes/agent/subdirectory_hints.py` | `docs/` 与各子包开发规则组织 |
| G.2 | PRD 即验收契约（story + acceptanceCriteria + passes） | `claw-code/prd.json`、`progress.txt:95` | 优化批次 DoD 写法（`docs/migration/` 规划文档） |
| G.3 | 契约测试/变异测试守「血泪规则」 | `hermes/tests/gateway/test_stream_final_contract.py` | 关键不变量测试化（p41 已有同思路） |
| G.4 | mock 对拍 harness（确定性 mock + 场景清单 + 子进程对拍） | `claw-code/rust/crates/rusty-claude-cli/tests/mock_parity_harness.rs` | `evals/` 场景对拍扩展 |

### 未采纳项（明确记录，不是遗漏）

| # | 借鉴动作 | 源出处 | 未采纳原因与当前替代 |
|---|---|---|---|
| X.1 | 内核能力探测式沙箱（`unshare` / namespace / seccomp） | `claw-code/rust/crates/runtime/src/sandbox.rs:334` | 本仓工具全部在主进程内执行，且部署形态是 compose 单机——起 namespace 需要容器特权，与同一份 compose 里「不额外给能力」的降权方向冲突。2026-10-23 先落地**不依赖特权**的 T0（超时真截断 flag、MCP 子进程环境白名单、表达式资源护栏、只读 SQL 词表两闸合一、日志凭据脱敏、显式生产身份、容器配额 + `no-new-privileges`）；namespace 级隔离仍是 T2，路线与逐项实测见 `docs/PROJECT_SPEC.md` §24.3、行为口径见 `docs/architecture/runtime-guardrails.md` §15 |
