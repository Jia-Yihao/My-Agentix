# 运行时护栏：上下文隔离 · 控制平面 · 感知工具限量

本文档覆盖《My Agentix_架构优化方案》§9.1 中 **C 组（上下文）**、**D 组（控制平面）**
与 **B 组（感知工具）** 的运行时护栏。这些能力共同回答一个问题：

> **当模型自信地做错事时，系统里还有什么能兜住？**

| 能力 | 模块 | 默认 | 失败语义 |
|---|---|---|---|
| 上下文隔离通道 | `application/services/artifact_store.py` | 开启（阈值 2000 字符） | 降级为内联 |
| 记忆整理（consolidation） | `application/services/memory_consolidation.py` | 纯函数，由调用方驱动 | — |
| 可重建性淘汰闸门 | 同上 + `long_term_memory.is_rebuildable` | **保守：无证据即保留** | fail-safe |
| Token 预算 | `application/services/token_budget.py` | `TOKEN_BUDGET_PER_REQUEST=0`（不限额） | 可观测 |
| 控制平面 | `runtime/control_plane.py` | 开启（登记失败降级） | 降级为直接 await |
| 目标优先级与冲突裁决 | `domain/policies/goal_arbitration.py` | 纯规则 | — |
| 感知工具结果限量 | `tools/gateway/result_limiter.py`（原 infrastructure/tools，M6 后实体归顶层 tools/） | 100 行 / 8000 字符 | 截断 + warning |
| 人工审批旁路 | `tools/gateway/human_review.py`（原 infrastructure/tools，M6 后实体归顶层 tools/） | `HUMAN_REVIEW_ENABLED=False` | **fail-closed** |
| 声明式任务依赖 | `application/services/task_plan.py` | 纯函数 | 结构非法即报错 |
| 子任务局部重试 | `application/services/task_retry.py` | 3 次尝试 | 失败转数据（不抛） |
| 终态完成度校验 | `domain/policies/completion.py` | `COMPLETION_VALIDATOR_STRICT=False`（只记录） | 校验器自身异常即跳过 |
| Prompt 静态前缀保证 | `domain/policies/prompt_envelope.py` | 类型层强制 + 测试期断言 | 违反即断言失败 |
| 角色差异化 | `application/agents/role_capabilities.py` | 声明表 | 未知角色保守返回空 |
| 工具超时真截断 | `tools/sandbox.py::invoke_in_process`（`executor.py::_invoke` 复用它） | `TOOL_SYNC_OFFLOAD_ENABLED=False` | **默认不生效**（§15.1） |
| 执行位置端口 | `domain/tool/sandbox.py` + `tools/sandbox.py::resolve_sandbox` | `EXECUTION_SANDBOX_ENABLED=False` | 默认不读声明；开则未实现的档位**拒绝执行**，不退回进程内（§15.10） |
| handler 类型契约告警 | `tools/gateway/executor.py::handler_shape` | `TOOL_HANDLER_CONTRACT_ENABLED=False` | 默认静默；开则注册期点名非协程 handler（§15.6） |
| MCP 子进程环境裁剪 | `capabilities/mcp/client.py::scrub_subprocess_env` | `MCP_SUBPROCESS_ENV_SCRUB=False` | 白名单外不进子进程（§15.2） |
| 表达式资源护栏 | `capabilities/mcp/tool_source.py::_guard` | 常开 | 超界即报错 |
| 只读 SQL 词表（两闸共用） | `domain/policies/sql_read_only.py` | 常开 | **fail-closed** |
| 日志凭据脱敏 | `tools/adapters/search.py::redact_secrets` | 常开 | 保留键名、值打码 |
| 显式生产身份 | `retrieval/settings.py::ENVIRONMENT` + `main.py::_warn_insecure_posture` | `APP_ENV` 空 ⇒ 沿用 `DEBUG` 推断 | 未声明则生产校验整段静默（§15.4） |
| 跨服务连接串闸门 | `kb_ingest/kb_service/core/db_url_guard.py` | `KB_MYSQL_URL_GUARD=False` | 拒绝时只说原因、不回放输入（§15.5） |
| 导入 SQL 参数化闸门 | `kb_ingest/kb_service/core/sql_filters.py` | `KB_MYSQL_WHERE_PARAMETRIZED=False` | 开则原始 `where` 一律拒、值只走绑定（§15.9） |
| 设置接口租户归属 + admin | `retrieval/api/routers/settings.py` | `SETTINGS_AUTHZ_ENABLED=False` | **默认不校验归属**（§15.8） |

---

## 1. 上下文隔离优于压缩

**原则**（方案第 2 章）：大中间产物应当**外置**，主状态只回收结论与句柄。

```
压缩：  [大结果] ──有损摘要──> [短文本] ──进 state──> 原始内容永久丢失
隔离：  [大结果] ──外置──────> [artifact_id + preview] ──进 state──> 原文可按需取回
```

`offload_content()` 按阈值二选一：

```python
from agentix.application.services.artifact_store import offload_content

result = offload_content(big_rows_text, kind="cypher_rows", summary="命中 137 条")
if result.is_offloaded:
    state["evidence"] = result.ref.to_dict()   # 主状态只留句柄
else:
    state["evidence"] = result.inline          # 小内容内联更划算
```

**关键性质**：

* **无损**：`get(artifact_id)` 返回原文（区别于摘要）。
* **降级不阻断**：`put` 抛异常时 `offload_content` 回退为内联，
  并把原因写进 `degraded_reason`（可选增强语义，见 ADR-0002）。
* **有界**：`InMemoryArtifactStore` 同时按条数与字节数封顶、LRU 淘汰；
  `stats()["evicted_unrebuildable"]` 单独统计「不可重建产物的真实丢失」。
* **可回滚**：`ARTIFACT_INLINE_THRESHOLD=0` ⇒ 永不外置，回到旧行为。

---

## 2. 遗忘与整理（consolidation）

`memory_consolidation.py` 把「整理」拆成 4 个**纯函数**，全部离线可测：

| 函数 | 作用 |
|---|---|
| `select_consolidation_groups` | 选出「同主题 + 时间相邻（`max_gap_days`）」的摘要组 |
| `merge_summaries` | 合并一组：topics/key_facts 去重并集、`source_message_ids` **并集**、时间取最早 |
| `plan_message_eviction` | 原始消息淘汰计划（**受可重建判据保护**） |
| `plan_summary_eviction` | 摘要索引淘汰（TTL + 容量，保留访问过的） |

**为什么合并要保持溯源并集**：合并后仍需能证明「覆盖了哪些原始消息」，
否则可重建判据会在整理之后失效——`merge_summaries` 的
`test_merged_sources_still_satisfy_rebuildability` 守着这一点。

**为什么整理不做 LLM 再摘要**：LLM 摘要是不可逆变换。若整理本身再引入一次
不可逆变换，可审计性就断了。合并只做**确定性拼接 + 并集**。

### 执行侧：`LongTermMemory.consolidate()`

纯函数此前**没有任何调用方**（等于没接进链路）。执行侧放在 `LongTermMemory`，
只负责「读出来 → 交给纯函数 → 写回去」，判定规则仍留在纯函数里以便离线单测。

```python
report = await memory.consolidate("u1", dry_run=True)   # 先观察（零副作用）
report = await memory.consolidate("u1")                 # 再执行
```

`dry_run=True`：整理会**改写历史**，故默认建议先 dry-run —— 计划照算，但不写一个字节。

实测：4 条摘要（3 条「租赁」+ 1 条「劳动」）→ 整理后 2 条；
合并结果 `topics=['租赁','押金','异常金']`、**溯源 `['m0','m1','m2']`（并集）**。

---

## 3. 可重建性接入淘汰路径

方案 §10.10.2 定义：被压缩内容 `C` 若存在确定性过程使 `P(原始数据) = C`，
则 `C` 可重建，原始数据可安全淘汰。

`plan_message_eviction` 把该判据接进**物理删除**决策：

```
待裁剪消息 ──> 找一条摘要，其 source_message_ids 完整覆盖该消息 id？
                 ├── 是 ──> drop（允许物理删除）
                 └── 否 ──> retain + blocked++（保留）
```

**保守默认（这是设计意图，不是缺陷）**：

* 缺 `id` 的消息**永远不可判定为可重建** → 保留。
* 没有任何摘要 → 全部保留。
* 这正是吸取 evals「空真地板」教训：**「没有证据说可以删」不等于「可以删」**。

**可观测**：`summarize_coverage()` 输出覆盖率，用于判断何时可以把
「只记录不阻断」升级为更激进的策略。

---

## 4. Token 预算（把「只计量」升级为「可限额」）

`request_scope` 负责**计量与上报**（用了多少），`TokenBudget` 负责**限额与判定**
（还能用多少、超了怎么办）。两者互补不重叠。

**两级预算**：请求级 `per_request` + 子 Agent 级 `per_agent`（请求额度的切片）。

**三档判定**（而非二值）：

| 判定 | 条件 | 调用方应做什么 |
|---|---|---|
| `OK` | `< warn_ratio × limit` | 正常 |
| `WARN` | `≥ 80%` | 收紧上下文 / 减少检索 |
| `EXCEEDED` | `≥ limit` | 降级（截断 / 收尾 / 拒答） |

**默认关闭的语义**：`limit <= 0` 表示不限额，此时 `remaining` 返回 `None`
（**不是 0**——0 表示已用尽）。这是刻意的：接线不得改变既有行为。

**当前接线程度**：`chat_service._ask_impl` 在请求结束后调用
`_settle_token_budget()`，把 `scope.total_tokens` 记入台账并在 `WARN`/`EXCEEDED`
时打日志，快照写入响应 `metadata.budget`。**硬中断**（跑到一半就掐断）需要
在 LLM 调用层挂钩子，属后续接线。

---

## 5. 控制平面（spawn / status / cancel / 预算）

**与 `task_queue` 的边界**（避免职责重叠）：

| 模块 | 管什么 | 粒度 |
|---|---|---|
| `task_queue` | 后台**作业**（file / embed / summary） | 作业队列，可持久化 |
| `control_plane` | **Agent 图运行**（本进程内） | 运行实例，生命周期短 |

控制平面**不做调度**（不决定先跑谁），只做**运行登记 + 取消 + 预算归属**——
这是「控制平面」与「调度器」的分工。

**cancel 为什么必须绑定 Task**：只改状态字段的「取消」是假的——执行还在跑。
`ControlPlane.bind(run_id, task)` 把真实 `asyncio.Task` 绑进运行实例，
`await cancel(run_id)` 才会真正打断并**等待其退出**（避免「已报取消但仍在跑」）。

**有界策略**：超容时只淘汰**已终态**的旧记录；若全是运行中，宁可超容也不丢
活动运行（`test_never_evicts_active_runs` 守着）。

---

## 6. 目标优先级与冲突裁决

Agent 同时被多个目标拉扯，而目标**天然冲突**。没有显式规则时，冲突由
**代码的偶然执行顺序**决定——同一类问题在不同路径得到不同处置，且无人能解释。

**两层规则**（先硬后软）：

1. **硬优先级**（`PRECEDENCE`，不可被配置让路）：
   `安全 > 规范 > 准确 > 有用 > 时延 > 成本`
2. **显式覆盖**（`Goal.priority`）：同一层内可用更小数值提权。
   **只在同层内生效**——安全不可被配置让路
   （`test_explicit_priority_cannot_cross_hard_precedence` 守着）。

**冲突表**（`CONFLICTING_PAIRS`）每条都带人类可读的裁决依据，可直接进审计日志。
未登记的目标对会被报进 `undefined_pairs`——**提示「这里还没定义规则」**，
而不是静默通过。

---

## 7. 感知工具下游结果限量

数据库侧 `LIMIT 100` 只约束**行数**，管不到单行体量（一行可能是 50KB 正文），
也管不到写语句与 `RETURN *`。因此必须在**下游**再加一道：

```python
from agentix.tools.gateway.result_limiter import limit_value

limited = limit_value(tool_output, max_rows=100, max_chars=8000)
payload = limited.with_notice()   # 把「还有更多 / 被截断」显式告诉模型
```

**为什么必须显式告知**：静默截断是幻觉的温床——模型看不到自己被截断，
就会基于不完整数据编造结论。`PageMeta.notice()` 生成一句可读说明，
`with_notice()` 把它并入返回值。

**已接线**：`tools/adapters/search.py` 的 `SearchTool._bounded()`
（`search` 是本仓库自有的感知工具；原 infrastructure/tools 路径 M6 后已删）。截断时打 `warning`。

---

## 8. 人工审批旁路（fail-closed）

读操作出错可以重试；**写操作出错不可逆**。审批旁路是唯一能兜住
「模型自信地做错事」的机制。

**风险分级**（确定性）：显式映射 → 名称模式（`drop/delete` → CRITICAL，
`write/update/execute` → HIGH，`import/sync` → MEDIUM）→ 默认 LOW。
参数可**提权不可降权**（`force=True` → CRITICAL）。

**两条通道**：

| 通道 | API | 用途 |
|---|---|---|
| 异步 | `await gate.decide(tool, args)` | 交互式审批：返回 `PENDING` + 请求，等人工裁决 |
| 同步 | `gate.preflight(tool, args)` | 硬闸门：未获批准直接抛 `ReviewRequired` |

**fail-closed 的两处体现**：

1. 高风险动作**没有可用的审批人** → `PENDING`（**不放行**）。
   审批通道抛异常 → 同样 `PENDING`。「没人能批」不等于「可以放行」。
2. `preflight` 找不到**未消费**的批准记录 → 抛 `ReviewRequired`。
   一次批准只放行一次，防止「一次批准长期有效」。

**已接线**：`agentix/tools/__init__.py` 的 `get_tool()`——工具实例化的
**唯一入口**（原 infrastructure/tools，M6 后实体归顶层 tools/）。默认关闭时 `preflight_review` 恒返回 `None`，行为与接线前逐位一致。

**已接线（图内，默认关）**：`HITL_IN_LOOP_ENABLED`（settings.py:828，默认 `False`）
开启且四项条件齐备时，`lg_builder._wrap_execute_task_in_loop` 把
`langgraph.types.interrupt` 经 config 注入 `execute_task` 节点；
裁决回写走 `POST /api/v1/ops/hitl/in-loop` → `lg_builder.resume_in_loop()`。
flag-off 与旧行为逐位等价（「实例化前硬闸门 + 异步审批通道」两层始终在位）。

---

## 9. 配置项速查

| 配置 | 默认 | 说明 |
|---|---|---|
| `ARTIFACT_INLINE_THRESHOLD` | `2000` | 超过则外置；`<=0` 永不外置 |
| `ARTIFACT_STORE_MAX_ITEMS` | `200` | 产物条数上限 |
| `ARTIFACT_STORE_MAX_BYTES` | `8388608` | 产物总字节上限 |
| `MEMORY_CONSOLIDATION_MAX_GAP_DAYS` | `7.0` | 整理的时间相邻阈值；`<=0` 只按主题 |
| `MEMORY_CONSOLIDATION_MAX_GROUPS` | `10` | 单次整理组数上限 |
| `TOKEN_BUDGET_PER_REQUEST` | `0` | 请求级 token 硬上限；`0` 不限额 |
| `TOKEN_BUDGET_PER_AGENT` | `0` | 子 Agent 切片；`0` 均分 |
| `CONTROL_PLANE_MAX_RUNS` | `500` | 运行登记表容量 |
| `TOOL_RESULT_MAX_ROWS` | `100` | 工具返回行数上限；`<=0` 不限 |
| `TOOL_RESULT_MAX_CHARS` | `8000` | 工具返回字符上限；`<=0` 不截断 |
| `HUMAN_REVIEW_ENABLED` | `False` | 高风险动作人工审批（fail-closed） |
| `COMPLETION_VALIDATOR_STRICT` | `False` | 终态校验是否把 `incomplete` 升级为 `degraded` |
| `TRAJECTORY_SINK` | `memory` | 轨迹后端：`memory`（默认）或 `jsonl`（落盘持久化） |
| `TRAJECTORY_PATH` | 空 | JSONL 轨迹文件路径（`TRAJECTORY_SINK=jsonl` 时必需） |
| `TOOL_SYNC_OFFLOAD_ENABLED` | `False` | §15：非协程工具 handler 下放线程，让 `timeout_ms` 真能截断 |
| `TOOL_HANDLER_CONTRACT_ENABLED` | `False` | §15.6：注册期点名非协程 handler（把「超时为什么不生效」前移到启动日志，告警不拒绝注册） |
| `EXECUTION_SANDBOX_ENABLED` | `False` | §15.10：按 `ToolSpec.sandbox` 声明选执行位置（默认档 `in_process` 与关着逐位等价；**开 ≠ 已隔离**，未实现的档位拒绝执行） |
| `MCP_SUBPROCESS_ENV_SCRUB` | `False` | §15：MCP 子进程只拿白名单环境，不继承主进程密钥 |
| `DB_ECHO` | 空（=沿用 `DEBUG`） | §15：显式覆盖 `engine.echo`，防止 SQL 连参数绕过 loguru |
| `APP_ENV` | 空（=沿用 `DEBUG` 推断） | §15：显式生产身份，优先级高于 legacy `ENVIRONMENT` |
| `SETTINGS_AUTHZ_ENABLED` | `False` | §15.8：`/settings/{kb_id}` 读要求同租户、写再要求 admin（默认关 = 登录即可读写任意 kb） |

本表的键都是 `agentix` 侧的 Settings 字段。`kb_ingest` 是独立进程、独立配置
（`kb_ingest/kb_service/core/config.py` 直接读 env，键名前缀同为 `KB_` 但不进本表，
否则会被 `tests/test_p41_doc_consistency.py` 当成主应用字段索要）：
执行边界相关的键是 `KB_MYSQL_URL_GUARD`、`KB_MYSQL_ALLOWED_HOSTS`（§15.5）与
`KB_MYSQL_WHERE_PARAMETRIZED`（§15.9），口径见各自小节，契约由
`tests/test_p35_config_governance.py` 守
（`config.py` 读到的键必须出现在 `kb_ingest/.env.example`，反向亦然）。

---

## RAG 补充：混合检索的活动索引快照一致性

Dense 与 BM25 只有检索同一知识库版本时才能融合。活动向量索引由
`IndexArtifact` 与 `IndexArtifactManifest` 确定；BM25 使用同一个 `artifact_id`
作为进程缓存键，并经 `get_chunks_for_index_artifact()` 限定语料：

```text
retrieve_scoped_docs_cached
  └─ active artifact_id
      ├─ Dense：该 artifact 的向量索引
      └─ BM25：manifest ∩ Document.ready ∩ indexed_artifact_id
          └─ RRF 按 chunk_id 融合
```

这避免了 BM25 按 `kb_id` 缓存时，索引重建切换后仍在 TTL 内读取旧语料；也避免新 chunk 尚未随 manifest 激活就提前进入稀疏通道。检索范围 filter 与数据条目有效性过滤仍在各自通道应用。BM25 是补充通道，数据库读取或构建失败继续 fail-open 到 Dense。

守卫位于 `tests/test_p76_rag_p1_hybrid.py`：同 artifact 缓存复用、不同 artifact 重新加载，并验证 inactive manifest / 非 ready 文档不会进入 sparse 语料。召回质量比较需用同一人工 gold 集，命令与口径见 `evals/README.md` 和 `docs/design/rag-optimization.md` §8。

---

## 10. 测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_p30_context_artifacts.py` | 产物存储、外置阈值、降级、整理分组/合并、可重建淘汰、摘要淘汰 |
| `tests/test_p31_control_plane.py` | token 预算三档判定、台账均分、运行登记/取消（含真实 Task 打断）、目标裁定流程 |
| `tests/test_p32_tool_guards.py` | 游标、行分页、字符截断、统一限量入口、风险分级、审批两条通道、`get_tool` 接线 |
| `tests/test_p33_task_planning.py` | 依赖声明与波次调度、局部重试与失败隔离、终态校验、静态前缀不变性、角色差异化（含真实站点集成） |

全部离线，不依赖 Redis / Neo4j / LLM / Docker。

---

## 11. 任务分解的显式化（声明式依赖 + 局部重试）

**问题**：依赖藏在 `await` 的顺序里时——环形依赖只能靠运行期死锁暴露、
调度顺序无人能一眼看懂、失败后不知道该重跑哪一段。

```
隐含依赖：  await a; results = await gather(b(), c()); await d()
            ↑ 依赖关系只存在于控制流里，无法校验、无法解释

显式声明：  TaskSpec("d", depends_on=("b", "c"))  ──plan_waves──>  [[a], [b, c], [d]]
            ↑ 串行/并行成为声明的**推论**，且可在结构校验期发现环
```

`plan_waves()` 用 Kahn 拓扑排序产出**执行波次**：同波次内可安全并行，
波次之间必须串行。`summarize_plan()` 给出 `max_parallelism`（最宽波次）
与 `serial_depth`（波次数），可据此预分配并发额度。

**局部重试与失败隔离**：一次 KG 问答会 fan-out 出多个工具 task。
其中一路瞬时失败若直接上抛，用户得到**整体失败**，而另外两路其实已成功——
**已付出的算力被丢弃**。`task_retry.run_task()` 把失败从「异常」变成「数据」
（返回 `TaskOutcome`），`run_batch()` 因此具备**部分成功**语义：

```python
batch = await run_batch(specs, runner)
if batch.partial_success:      # 一路失败不影响其余
    logger.warning("partial: {}/{}", len(batch.succeeded), batch.total)
```

**与 `aretry` 的分工**：`aretry` 是**装饰器**（重试后仍失败就抛）；
本模块是**边界**（失败转结果对象）。重试策略复用，不重写。

---

## 12. 终态完成度校验（不让考生自己判卷）

**问题**：图是否「跑完」原本只由 **LLM 不再发起工具调用**决定——
等价于**让考生自己判卷**。模型可以在证据为空时自信收尾。

`domain/policies/completion.py` 提供一层**确定性**对照（零 LLM 调用），
只看环境事实：

| 判定 | 触发条件 |
|---|---|
| `incomplete` | 终态为 error / 有未消解错误 / 答案实质为空（< 6 字符，为中文校准） |
| `degraded` | 引用类路由（graphrag / kb-query / …）但 `sources` **确认为空**；或有工具失败 |
| `complete` | 其余 |

**与 LLM 侧 `final_answer_guard` 互补而非替代**：

| | 判据来源 | 成本 | 默认 |
|---|---|---|---|
| `final_answer_guard` | 语义一致性 | 一次 LLM 调用 | 关 |
| 本模块 | 环境终态事实 | 零 | 开（仅记录） |

**默认只记录、不改判定**（`COMPLETION_VALIDATOR_STRICT=False`）：
先把「模型自认完成 vs 环境事实」的差异**观测**出来，再决定是否升级为强制。

**空真防护**：`sources=None`（未知）与 `[]`（确认为空）**区别对待**——
未知不判罚，避免把「没传」当「没有」。

---

## 12.5 轨迹持久化（让 F1 / F5 真正可用）

默认 `InMemoryTrajectorySink` 进程一重启就丢，这直接卡住两件事：
**F1 完整回放**留不下生产轨迹，**F5 轨迹前缀回归**拿不到真实基线（只能人工构造）。

`JsonlTrajectorySink` 补上这一环——追加式 JSONL，**每行一个步骤**
（追加写是原子的，不会截断整条 trace），写后即关，按大小轮转。

典型工作流：:

```bash
# 1) 生产/预发开启落盘（TRAJECTORY_SINK=jsonl TRAJECTORY_PATH=var/trajectories.jsonl）
# 2) 跑一批请求，轨迹自动落盘
# 3) 从落盘轨迹导出回归基线
#    ⚠️ 导出到子目录 evals/baselines/traces/ —— 与 quality.json 同级会被
#    load_packages 当「缺 steps 的轨迹包」拒绝（quality.json 不是轨迹包）
python evals/trajectory_regression.py --export-from var/trajectories.jsonl \
                                      --export-to evals/baselines/traces
# 4) 改造后重跑并导出候选，做前缀回归
python evals/trajectory_regression.py -b evals/baselines/traces -c out/candidate
```

实测端到端：落盘 2 条 trace → 导出 2 个轨迹包 → 自比较 exit 0；
注入一步漂移后 exit 1 并精确报出 `@step 2 (step_mismatch)` 与两侧签名。

---

## 13. Prompt 静态前缀的框架级保证

**为什么逐点守卫不够**：每新增一个 prompt builder，就新增一个把动态内容
（用户输入、检索结果、当前时间）插到静态区之前的机会。逐点守卫**只覆盖已写过的点**。

`domain/policies/prompt_envelope.py` 给两层保证：

1. **类型层**：`PromptEnvelope(static_prefix, dynamic_suffix)` 的 `render()`
   唯一决定拼接顺序（`static + dynamic`），因此**写不出**「动态在前」的代码。
2. **不变性层**：`assert_static_prefix_stable(build, samples, ...)` —— 用多组
   **不同的**动态输入渲染同一 builder，断言公共前缀 ≥ 声明的静态常量。

第 2 点是关键：它检查的不是「代码长什么样」，而是「**输出是否真的不变**」——
即使有人用很隐蔽的方式（条件分支、隐式拼接）把动态内容混进静态区，
只要输出随输入变化，断言就会失败。**防空真**：不同输入产出相同输出 ⇒
判为「动态部分未生效，验证无意义」并报错，而不是静默通过。

**已用于真实站点**：`tests/test_p33::TestRealPromptStaticPrefix` 对真实的
`create_guardrails_context` 断言「静态前缀在前、动态 scope 在后」，
并断言 `schema_utils` 不得再内联前缀字面量。

**生产链路扩展（上下文工程 A，2026-10-09）**：general-query 的 system 前缀
含 memory/指令动态注入，不满足「逐字节不变」，其漂移信号改由
`llm_factory._TEMPLATE_PREFIX_ROLES` 承接——指纹取**模板常量**
（`GENERAL_QUERY_SYSTEM_PROMPT`）而非本次请求正文：模板变更仍触发
`prompt_prefix_fingerprint{builder=general}` 突变，记忆/指令/logic 注入不再
打成逐请求抖动噪声。配套守护测试 `tests/test_p102_lg_context_engineering.py::TestStaticPrefixDiscipline`
用 `assert_static_prefix_stable` 对 general 组装序（模板 → logic → 指令/记忆）
断言公共前缀恰好停在 `<logic>\n` 静态/动态边界——只进测试，不进热路径。
状态尾注（`LG_STATUS_TAIL_ENABLED`）与指令块（`LG_INSTRUCTIONS_ENABLED`）
两个新增动态注入面均走「本轮 human 消息尾部 / system 后置块」，绝不写静态前缀。

---

## 14. 角色差异化（工具集 + 上下文）

**问题**：给「路由」角色挂上 `cypher_query` 是**纯噪声**——它只做意图分类，
多出来的工具 schema 既占 token（每轮都进 prompt），又制造误选机会。
给路由灌入 20 轮历史同样既无收益又抬高延迟与成本。

`application/agents/role_capabilities.py` 用一张**声明表**表达差异：

| 角色 | 工具集 | 历史 | 记忆 | 证据 | 工具清单 |
|---|---|---|---|---|---|
| `router` | **零** | 4 | ✗ | ✗ | ✗ |
| `hallucination` | **零**（不得重新检索） | 6 | ✗ | ✓（8） | ✗ |
| `planner` | 全集 | 10 | ✓ | ✗ | ✓ |
| `tool` | 全集 | 10 | ✗ | ✓（5） | ✓ |
| `summarizer` | **零**（工具阶段已结束） | 8 | ✗ | ✓（20） | ✗ |
| `general` | 全集 | 20 | ✓ | ✓（5） | ✓ |

`tools_for_role(role, available)` 与真实可用集**求交**（尊重 `DISABLED_TOOLS`）；
未知角色**保守**返回空工具 + 最保守上下文（不认识就别给能力）。

**测试守着「真差异化」**：断言不同角色的上下文策略签名 **≥ 4 种**——
防止「改了个字段名但其实全表一样」的假差异化；并断言
`ALL_TOOLS` 与真实 `registry.tool_names()` 一致（防声明漂移）。

---

## 15. 执行边界加固（Phase S，§24.3 T0）

前面 14 节管的是「**该不该做**」——权限、审批、预算、限量。本节管的是
「**决定做了之后，它跑到哪一步为止**」：一次工具调用失控，能不能被真的切断。
这两类闸门的失效方向**必须相反**：审批闸门 fail-closed（§8），
边界闸门也得 fail-closed——但它的「失败」不是拒绝，而是**截断**
（例外见 §15.10：执行位置"跑不了"时只能拒绝，因为静默降级正好把那条声明作废）。

审计全文与逐项实测见 [docs/PROJECT_SPEC.md](../PROJECT_SPEC.md) §24；
本节只留在架构文档里必须被遵守的**十一条口径**（§15.1–15.10，§15.11 是未收口清单）。

### 15.1 超时只在事件循环让出来时成立

`ToolExecutor` 用 `asyncio.wait_for` 截断工具，但**非协程 handler 在自己的
栈帧里同步执行时，循环根本没被让出**——`wait_for` 无从取消。实测声明
`timeout_ms=100` 的同步 handler 跑了 1201ms 且 `success=True`：
超时配置形同虚设（G1）。

`TOOL_SYNC_OFFLOAD_ENABLED=True` 时 `_invoke` 改走 `asyncio.to_thread`，
循环空得出来，超时才真生效。**默认关**：线程内不再持有原事件循环，
loop 亲和的 handler（直接 `asyncio.get_event_loop()`、绑定循环的客户端）
会坏，必须由部署方按工具逐个确认后才能打开。

### 15.2 子进程环境是信任边界，不是便利通道

MCP server 是**任意第三方命令**。`create_subprocess_exec` 不传 `env` 等于
把父进程全部环境——`LLM_API_KEY` / `SERPAPI_KEY` / `DATABASE_URL` —
交给它（G2）。`MCP_SUBPROCESS_ENV_SCRUB=True` 改传白名单
（`client.py::OS_ENV_ALLOWLIST`：起进程、找包、正确编码、证书、代理所需的键），
第三方需要自己的凭据时用 `MCP_SUBPROCESS_ENV_EXTRA_KEYS` **显式加名**，
而不是退回继承。裁掉 `PYTHONPATH` 时自动补回仓库根，
默认 `python -m agentix.capabilities.mcp.server` 在开与关两种状态下都能起来
（开状态下实测 `calculator 6*7` → 42）。

### 15.3 白名单挡语法，不挡资源

计算器的 AST 节点白名单已经足够严，但**合法语法一样能算到天荒地老**：
`9**9**9` 的中间结果能把内存吃光（G3）。因此语法白名单之外另有一层
**值域护栏**（`agentix/capabilities/mcp/tool_source.py`）：表达式长度 ≤200 字符、
`**` 指数模 ≤64、整数结果位宽 ≤512、浮点必须有限且 ≤1e308。
同理，只读 SQL 的词法表放在 `agentix/domain/policies/sql_read_only.py`——
**先在等长处抹掉字面量/注释/反引号标识符再按词边界匹配**，
两条 text2sql 闸门共用同一份词表（G5 修的是两份黑名单互相不一致，
以及 `SELECT 'it''s'` 这类转义引号误判）。

推论：**任何「先解析再执行」的入口，语法闸与资源闸都要独立存在**。
只写其一等于只防了攻击者最不感兴趣的那一半。

硬失败面（配置有错就拒绝启动）判的是 `STRICT_VALIDATION` =
`prod`/`production`/`staging`，**不是** `PRODUCTION`——staging 的价值正是「用生产的失败语义
提前跑一遍」，只判生产会让显式声明 `APP_ENV=staging` 的部署静默丢掉这道 raise
（G6 修复过程中自己踩出来的，见 PROJECT_SPEC §24.2 G6 的「连带发现」）。

### 15.4 生产身份必须由人声明，不能由 debug 开关反推

`ENVIRONMENT` 原先由 `DEBUG` 反推（G6），而 `DEBUG` 出厂默认 `True`：
忘设 `DEBUG=False` 的容器既在生产开热重载、又让
`validate_app_settings()` 的 PRODUCTION 分支**整段静默不生效**，
出厂默认关闭的鉴权/限流/审批闸门也就没有任何一处会响。
现在优先级是 `APP_ENV` > legacy `ENVIRONMENT` > `DEBUG` 推断（末项仅为兼容保留），
且 `main.py::_warn_insecure_posture()` 在未显式声明时**点名当前关闭的闸门**
打一条 `SECURITY POSTURE` 告警；`UVICORN_RELOAD` / `DB_ECHO` 同样从 `DEBUG`
解耦成独立三态开关（留空 = 旧行为）。

同一原则的另外两处：`engine.echo` 默认开时完整 SQL 连参数走标准库 logging，
绕过 PROJECT_SPEC §15.5 的 loguru 出口与脱敏（G11）；设置接口 `GET` 原样回显模型密钥，
任何已认证用户可读（G9，现按 `_api_key`/`_secret`/`_token` 后缀掩码、
PUT 拒收掩码回写，未知键 422）。

### 15.5 跨服务输入：连接串不是可透传的参数

`kb_ingest` 的 `/ingest/mysql` 把请求体里的 `connection_url` 直接交给
`create_engine()`。持 API key 的调用方因此能让**服务端**去连任意 `host:port`：
`sqlite:////app/secrets.db` 读容器内文件、`mysql://u:p@ attacker:3306/` 外带凭据并
探测内网（G12 / §24.3 ⑫）。现在由 `kb_ingest/kb_service/core/db_url_guard.py` 拦，
`KB_MYSQL_URL_GUARD` 默认关（关 = 与加固前逐位等价，连 `sqlite:` 也照放）。

两个反直觉的实现口径，都是**真跑出来的**：

- **校验点不在 pydantic 层。** 初版写成 `@field_validator("connection_url")`，
  实测被拒请求的响应体是
  `{"detail":[{"msg":"Value error, host 'evil.example' is not in …","input":"mysql://u:Tr3m0d1a@evil.example/kb"}]}`
  ——pydantic 把被拒输入放进 `errors()["input"]`，FastAPI 又默认把 `input` 写进 422，
  于是**闸门本身成了凭据回显器**，且只在错误路径上回显。闸门因此下移到
  `MySQLIngestor.ingest()`（HTTP 与 CLI 的唯一汇合点）与路由的**入队前**同步判定，
  detail 由我们自己构造：只说为什么被拒，不回放输入。
  通用口径：**承载凭据的字段不要过会回显给调用方的 validator**。
- **两条闸正交：主机白名单管「连到谁」，选项闸管「连过去之后客户端愿意干什么」。**
  `?local_infile=1` 让**对端服务器**在握手后要求本客户端上传本地文件——实测
  `create_engine("mysql+pymysql://u:p@h/db?local_infile=1")` 的
  `dialect.create_connect_args()` 产出含 `{'local_infile': 1, 'client_flag': 2}`，
  SQLAlchemy 确实把它交给驱动并置上 `CLIENT_LOCAL_FILES` 位。所以连一台陌生 MySQL
  的危险度不止「端口探测」，还含「读走服务主机上的文件」。闸门因此额外拒绝
  `local_infile` / `allow_local_infile` / `read_default_file` / `read_default_group`
  与带 LOCAL_FILES 位的 `client_flag`（显式 `local_infile=0` 放行；detail 只回显选项名）。
  这与 §15.3 是同一课：**「白名单式校验」只覆盖它声明过的那个维度**，
  scheme 白名单覆盖协议、主机白名单覆盖目的地，客户端能力位谁都不覆盖。
- **不做 DNS 解析。** 解析出的 IP 与随后真正连接的 IP 之间没有绑定关系
  （DNS 重绑定照样能翻），却把外网延迟引进每一次导入。所以本闸门只声明
  「scheme 必须 mysql + 主机在配置白名单内」，**文档与测试都不许暗示它做了内网段判定**。
  白名单空 = 只校验 scheme，这是开关已开但尚未收紧的过渡态，必须在文档里显式说明。

### 15.6 handler 类型契约：先让「谁不可超时」可 grep

§15.1 的那份「生产在案的同步 handler」清单是人肉 grep 出来的。一个工具注册进来时
到底是同步还是异步，运行期没有任何信号——只有等一次「超时没生效」的真实排障才知道。
`TOOL_HANDLER_CONTRACT_ENABLED`（默认关）把这条口径搬到注册期：`register_handler`
归类 handler 形状，非协程就带**声明超时**点名工具名并指向 `TOOL_SYNC_OFFLOAD_ENABLED`。
开 flag 后装上真实技能工具即打印
`TOOL HANDLER CONTRACT: tool 'read_skill' handler is not a coroutine function; … (declared timeout: 30000ms)`
——打中的是生产里真实存在的 handler，不是测试夹具。

三条实现口径：

- **判定看「调用后能否拿到可 await 的结果」，不是 `iscoroutinefunction` 的字面返回值。**
  类的 `async def __call__` 对 `iscoroutinefunction(实例)` 是 False，若照字面判，
  每个类形式的工具都会吃一条假告警；`functools.partial` 包住的协程要仍算 async。
  口径与 `_invoke` 一致，否则契约告警和实际执行行为会各说一套。
- **异步生成器单列一档、给不同文案。** 它返回 async_generator，执行链 await 不了它；
  把「下放线程」的修法推荐给它是把一个错换成另一个错，所以这条告警里**不许**出现
  `TOOL_SYNC_OFFLOAD_ENABLED`。
- **告警，不拒绝注册。** 仓里最重的几个工具（`search`、`read_skill`、`cypher_query`）
  本来就以同步 I/O 形式注册，契约若做成 fail-closed 会在开 flag 的那一刻打断全部功能。
  契约的产物是一条可 grep、可进 CI 的信号，不是新增的 break；要治阻塞本身，仍然只有 §15.1 的下放线程。

### 15.7 compose 的插值发生在宿主机，不在容器里

`docs/runbooks/staging-environment.md` 的配置矩阵是那份 runbook 验收清单第 5 条的 diff 基准，
所以「表里写 staging=true」必须等于「`docker/compose.override.staging.yml` 真的设了它」。
这条起初只核对三个执行边界 flag，本轮推广成**整表通则**（凡加粗声明具体值的 staging
单元格都逐行核对 override），随即抓出一处不只是纸面漂移的问题：

- 基线 compose 写 `UVICORN_RELOAD=${UVICORN_RELOAD:-${DEBUG:-True}}`，而 compose 的插值发生在
  **渲染期**、取的是宿主机环境 / `.env`——与同一份文件里 override 给容器设的 `DEBUG` 无关。
  实测：`DEBUG=true` 的工作站启动 staging，渲染出的容器环境是 `UVICORN_RELOAD: "true"`，
  **预发以 `--reload` 起**（文件监听 + 单 worker 语义），而矩阵写着 staging=false。
- `DB_ECHO` 那条当时**恰好**也是 false（空值 ⇒ 由容器内 `DEBUG=false` 推断），但那是巧合
  不是声明；两处一并由 override 显式钉成 `"false"`（backend 与 retrieval-worker）。

**口径**：「容器取值 = 文档声明值」在 compose 里不是自动成立的事实。用了
`${X:-${Y:-…}}` 这类嵌套默认、又指望某个 override 覆盖它，就是在赌操作者的主机环境干净。
要么 override 显式写值，要么文档别写具体值。通则的残余面：它验的是 override **写了什么**，
不是容器里**真正生效什么**，也不覆盖「按需」「显式注入」这类描述行。

**终态（同日落地）：这张表改由部署物生成**，`scripts/gen_staging_matrix.py` 既是生成器也是
校验器（`--check` 漂移即退出 1）。三条口径从这里开始是机器的，不是人的：

- **覆盖面被定义，不再被挑选**：表的行 = staging override 的 backend 里设了的键 ∪
  `MANUAL_KEYS` 里署名登记的手填行。加一行只有两条路，两条都留下名字——
  「文档作者觉得这项重要」不再是进表的理由。
- **加粗 ⇔ override 真设了**，且加粗那格必须**等于** override 的字面值。这一条把 §24.2 G13
  两例（表里加粗、override 里没设）变成机械冲突，而不是要靠人记得去对。
- **事实源是两份 YAML 的声明，不是 `docker compose config` 的渲染输出**——这一处是刻意的
  落点偏差，理由两条都跟本节的教训同源：渲染会把宿主机真实 `.env` 展开到 stdout（终端与
  CI 日志因此成为密钥出口），而渲染结果恰恰又被 `.env` 污染，用它校验文档等于让被校验的
  对象决定校验基准。因此 staging 列逐字等于容器值（override 写的是不参与插值的带引号字面量），
  dev 列则**明写**它是「宿主机没设该变量时」的基线取值。
- 残余：production 列没有第三份部署物，脚本不校验它；表外的键（compose 转发、override 不管）
  的跨环境差异仍不在射程内。回归 `tests/test_p115_staging_matrix_generated.py`（44 项，
  八种漂移形态各有反向用例，18 处变异全抓）。

### 15.8 掩码不是授权：可写字段就是一条出站通道

G9 把 `/settings/{kb_id}` 响应里的 `*_api_key` 打成 `***`，解决的是「读回显」。
它没有解决「谁能写」，而这两件事经常被当成一件事。

- 该接口原先只挂在 `Depends(get_current_user)` 上：**登录即可**读写任意 `kb_id`，
  租户归属一概不看（同仓的 `authz.py::require_kb_in_tenant` 早已存在，只是这条路没接）。
- 可写字段里有 `llm_base_url` / `embedding_base_url`。把它们改成攻击者自己的端点，
  之后该知识库的每一条提问、每一段召回上下文都会发过去——**不需要拿到任何密钥**。
  掩码在这里帮不上忙：掩码管的是「值不外流」，而这一步利用的是「值可以被换掉」。

**口径**：`*_api_key` 的掩码与「谁能改这条配置」是两个独立的闸门，任何一扇开了都不算
另一扇可以省。凡是**可写**且会**影响出站目标**（base_url / webhook / 连接串 / 代理）的字段，
授权面必须按资源归属收口，而不是按「接口只读不读密钥」判风险。
落地由 `SETTINGS_AUTHZ_ENABLED`（§24.3 T1 ⑪，默认关）承担：读要求 kb 与调用者同租户，
写再要求 `role=admin`，判定完全复用 `authz.py` + `rbac.py` 的既有口径（404/403 语义、
不泄露存在性），没有新造一套授权语义。

**两个前置不是形式**：① `run_migrations()` 会把 `knowledgebase.tenant_id` 空值回填到默认租户，
没跑过迁移的存量行开 flag 后一律 403；② 全仓**没有**自助升 admin 的接口，注册只会产出
member——一个 admin 都没有的实例打开这一位，设置页读写同时失效。这也是 staging override
把它显式钉成 `false` 而不是跟生产一起 true 的原因。

### 15.9 拼接式过滤：值不许进 SQL 文本

§15.5 管的是「连到谁」，这一节是同一条导入接口的另一半输入——「连过去之后跑什么 SQL」。
`MySQLIngestRequest.where` 是一段**原始 SQL 片段**，原实现写的是
`base += f" WHERE {request.where}"`，前面只有一道黑名单（拒 `;` 与注释）。

- **黑名单拦得住堆叠，拦不住越界**：`1=1 OR 1=1`、`id IN (SELECT … FROM mysql.user)`
  都是合法片段。对拼出来的 SQL 而言，调用方给的就是语法——所以「再补几个黑名单词」
  这条路本质上收不了口，§15.3 那条教训在这里重演一次。
- **参数化换的是判据，不是写法**：列名进不了 bindparam，于是只接受纯标识符
  （`^[A-Za-z0-9_]{1,64}$`，与 `table` 同口径）；运算符取封闭枚举，展开成写死的词，
  不参与拼接；值全部走 `bindparams`；条件之间只允许 `AND`（要布尔或关系就分多次导入，
  别让本服务拼一段自己看不懂的 SQL）。产出文本里唯一来自调用方的字节是标识符。
- **闸门形态是硬切，不是"多一个可选字段"**：`KB_MYSQL_WHERE_PARAMETRIZED=true` 之后
  原始片段一律 400。只加字段、旧路照走，注入面一条也没关——那是加功能，不是收口。
  反过来 flag 关时**拒绝** `filters`：静默忽略一个调用方以为生效的过滤条件，
  等于把整张表灌进向量库，比报错更坏。
- **代价写在配置面上**：这一位是破坏性变更，开当天所有仍传 `where` / `--where` 的
  作业全部 400。所以默认关，两个 `.env.example` 都写明「先让调用方迁完再开」；
  CLI 给的是 JSON 形态的 `--filters`，**不发明 `col=op=val` 这类迷你语法**——
  那会长出第二套需要被绕过的语言。`MAX_FILTERS = 32` 让「拿过滤器打探针」在请求期现形。
- **错误消息不回显值**（§15.5 那条 422 回显凭据的教训在这里同样成立）：
  报错只说违反了哪条规则、第几个条件，被拒的列名与值都不进消息。
- **口径同源**：`enforce_where_policy` 是路由（入队前，回 400）与服务层
  （HTTP/CLI 汇合点，排在 `create_engine` 之前）唯一的入口。两处各写一份判据，
  迟早只关一条——这与 §15.5 的闸门落点是同一个理由。

### 15.10 执行位置是声明出来的：但"有端口"不等于"有墙"

§24.3 T2 ⑭ 落的是**词汇表**，不是**隔离**。`ToolSpec.sandbox` 让「这条工具在哪儿跑」
第一次成为声明式、可 grep、可测的事实，而 `EXECUTION_SANDBOX_ENABLED`（默认关）决定
executor 是否去读它。口径有六条：

- **关着时声明完全惰性**：flag-off 不查 `spec.sandbox`，走的还是加固前那行进程内 await。
  这条不是"少做事"，是**逐位等价的证明**——回归里故意让一条工具声明跑不了的
  `subprocess`，flag 关时它照样执行成功（`tests/test_p114_execution_sandbox.py`）。
- **阶梯与实现集合是两个概念**：`SANDBOX_LADDER` 决定"允许声明什么"，
  `implemented_kinds()` 决定"真能跑什么"，差集 `{subprocess, container}` 就是 ⑮/⑰ 的余量，
  可以机械检查。合成一个集合的后果是 ⑮ 落地当天要回头改一批本来就写对了的声明。
- **声明了跑不了的档位 ⇒ 拒绝执行，不退回进程内**：`SandboxUnavailable` 在执行点落成
  `reason="sandbox_unavailable"` 的失败结果，handler **一次都不被调用**，也不重试
  （重试换不来一个实现），并且排在观测钩子之前——闸门拒绝不是一次执行。
  静默退回进程内等于把安全声明当装饰，而那正是这条档位存在的目的要否定的东西。
- **超时仍不归沙箱管**：截断由 executor 外层 `pooled_wait_for` 统一负责。每个实现自备
  超时的话，「这条工具的 `timeout_ms` 生不生效」又变回要读实现才知道的事（§15.1 的教训）。
- **`thread` 档买到的是"别占住循环"，不是隔离**：它把 §24.2 G1 的全局
  `TOOL_SYNC_OFFLOAD_ENABLED` 降级成**按工具表态**——一条会占循环的 handler 不再需要
  为了它把整实例的循环亲和语义改掉。同进程、同 GIL、同一套 `os.environ` 与文件权限
  仍然一切共享；`open(os.environ["DATABASE_URL"])` 在两种档位里都成功。
- **「键在文件里出现」要按整词判**：配置面同步检查原先写 `KEY in text`，变异测试立刻
  暴露它把 `KEY_TYPO` 也算成"已同步"——而自检查表与 p41 手写清单里真正登记的恰恰是那个
  拼错的键，于是"五处同步"的守卫在最常见的写错名场景下是空转的。现在用
  `(?<![A-Za-z0-9_])KEY(?![A-Za-z0-9_])`，并配一条反向钉子要求后缀形态必须不匹配。

落点偏差也要记一句：规划写的是「实现落 infrastructure」，本仓没有
`agentix/infrastructure/` 这个包（工具层规范位在 `agentix/tools/`），
所以实现落在 `tools/sandbox.py`，端口落在 `domain/tool/sandbox.py`——
分层依据是 [ADR-0001](adr/0001-layered-architecture-with-ports.md) 的依赖方向，
不是目录名的字面复刻。`tests/test_p114_execution_sandbox.py` 里那两条 AST 钉子
（域层不得 import `agentix.*`/第三方；实现层不得反向 import executor）盯的就是这件事。

### 15.11 未收口的部分

T0 已落齐 ①②③④⑥⑦⑩⑤，T1 落了 ⑫⑧⑬⑪，T2 落了 **⑭（端口 + `in_process`/`thread`
两档实现 + ADR-0007）**；仍待做的是
⑨（DB 层只读账号 + 视图白名单），它在 [docs/PROJECT_SPEC.md](../PROJECT_SPEC.md) §24.3
标为待做；⑧ 只做了告警、`_invoke` 的线程下放仍默认关；
T2 剩下的是 ⑮（`subprocess` + `rlimit` + 独立 uid + 只读根 + 环境白名单——G2 那条
"带什么出去"的教训在这里是**前置**而不是后补）与 ⑯（能力探测优先于二进制探测）。
**⑭ 在这里的含义是"位置可以声明了"，不是"执行被隔离了"**：能跑的两档都在本进程内，
所以 §24.1 那句「仍然没有墙」今天仍然成立，要等 ⑮ 才改写。
**⑫ 的两半都已收口**（连接串 §15.5 + 过滤参数化 §15.9），但两位都默认关，
所以「已落地」在这里的含义是**能拧过去**，不是**已经拧过去**——开任何一位都要先按
`kb_ingest/.env.example` 的迁移顺序确认调用方，否则当天就是导入作业整批 400。
**⑪ 的落点不是规划里那句「路由升 `get_current_admin`」**：整条路由升 admin 会把 member
彻底挡在设置页外（他们本来就要看自己库的切分/检索参数），而读回来的密钥已由 G9 掩码，
不构成新的外泄面。所以实际口径是「读收归属、写加 admin」（见 §15.8）。
**由此留下的真实残余**：全仓没有自助升 admin 的接口，注册只会产出 member，
所以 ⑪ 的写侧在「一个 admin 都没有」的实例上等于「谁都不能改」——
这与 `DELETE /knowledge-bases/{kb_id}` 上早已存在的同一枚空洞是一件事，
归到 T1 之后的身份/成员治理里收，不在本条假装解决。
**⑬ 落地的口径值得单独记一句**：`engine` / `SessionLocal` 现在是「取用才建」，
所以「import 了 DB 模块但没连过库」这种状态从此是存在的——启动自检、`create_all`
之类的建池动作不再由导入顺序保证，故障点统一落到第一次真正取用连接的地方。
**⑤ 的口径也一样**：`AGENTIX_CONTAINER_USER` 默认 `0:0` 就是「仍然以 root 跑」，
它买到的是「能拧过去」而不是「已经拧过去」。挑这条特别记一句的理由是它的**失败形态**：
非 root 写不进绑定挂载时，entrypoint 的 `set -euo pipefail` 让容器进入 restart 崩溃循环，
既不拒绝启动也没有明确日志——是所有 T0 项里唯一一个「配错不会响、只会一直重启」的。
容器侧仍然没有的还有 `cap_drop`（docker 默认 capability 集照旧全给）、`read_only` 根文件系统、
user namespace 映射（容器内 uid 1000 与宿主 uid 1000 是同一个身份）。
容器本来就各自拥有 pid/mount/net namespace，那不是本条买到的东西——所以 §24.1 的结论仍是
「闸门严、边界有了一格、但仍然没有墙」。
**G13 那张表已经改成生成物（§15.7），但它只兜住 dev / staging 两列**：production 列没有
第三份部署物、脚本刻意不校验它，「按需」「确认后开」那一类至今只有约定；表外的键
（compose 转发了、override 没管的）也不进表。这两块要收口，靠的是让 override 成为
跨环境差异的唯一声明源，而不是再补几行人眼检查项。

回归覆盖：`tests/test_p107_sandbox_hardening.py`（66 项，逐条映射 G1/G2/G3/G4/G5/G6/G9/G11，
其中 MCP 子进程、日志脱敏、超时截断、`engine.echo` 都跑真实代码路径而非 mock）；
`tests/test_p108_kb_mysql_url_guard.py`（72 项，映射 G12/⑫，含真实 TestClient 的
400 响应体断言与「schema 上不得再出现 `connection_url` validator」的反向钉子）；
`tests/test_p109_tool_handler_contract.py`（33 项，映射 T1 ⑧，含 flag-off 静默等价、
`partial`/类 `async __call__` 不误报、异步生成器文案不得指向下放线程，
同一批 handler 在 flag 开/关 × offload 开/关下 `_invoke` 返回值逐位一致，
以及第 6 节把「runbook 配置矩阵 ↔ staging override ↔ 基线 compose 转发」做成
**整表通则** + 删键/改值两种篡改的反向验证（G13））。
`tests/test_p110_lazy_database_engine.py`（11 项，映射 T1 ⑬/G10，前三条各起一个真子进程数
`create_engine` 的调用次数——「import 期有没有建池」是进程级事实，在 pytest 进程内测会被别的
用例的导入污染；另含「缺驱动时 import 成功、取用才抛 `NoSuchModuleError`」与
「模块顶层不得再出现 `engine = create_engine(...)`」的反向钉子）。
`tests/test_p111_container_user_knob.py`（11 项，映射 T0 ⑤/G8：默认值 `0:0` 的等价性、
「镜像没有 `USER` 指令」这条前提本身、两个服务同源、runbook 是否真教了切换步骤，
以及一条「compose 改坏 / 加了 `USER` 就必须红」的反向钉子。全部走解析文件而非起容器，
所以本机没有 Docker 守护进程时 CI 也能跑——真起容器验 uid 属 T3 的验收面）。
`tests/test_p112_settings_authz.py`（22 项，映射 T1 ⑪，用真 HTTP 打路由而不是单测 helper：
flag-off 的逐位等价（跨租户读/写照旧 200、不存在的 kb 读也照旧 200 而不是 404）、
flag-on 的 403/404 分工与「归属校验排在负载校验之前，所以越权拿到 403 不是 422」、
`tenant_id IS NULL` 的存量 kb 走 fail-closed、响应体不回显 kb id，
外加两条口径同源钉子（路由必须复用 `require_kb_in_tenant`/`require_admin`、
不得再出现 `current_user.role != "admin"` 这类本地判断）和一条反向验证
「admin 在同租户不被锁在外面」。**顺带钉出一个真 bug**：JSON 列没挂 `MutableDict`，
原地 `row.config.update()` 不被变更检测看见 ⇒ 同一 kb 的第二次及以后 PUT 回 200
却一个键都不落库；改成整体重新赋值，并由「第二次 PUT 确实落库」这条用例守住）。
`tests/test_p113_where_parametrization.py`（75 项，映射 T1 ⑫ 的第二半，分七层：默认姿态、
flag-off 与加固前**逐字节**相同且 `_bindparams` 为空、flag-on 的收紧面（原始片段一律拒 /
模板与绑定分离 / 标识符白名单 / 封闭运算符枚举 / `eq NULL` 与空 `IN ()` 报错而不是静默恒假）、
两个入口 + CLI、口径同源反向钉子、部署面（两个 `.env.example` + compose 转发 +
`clone_config` 必须保住这一位、**真执行**——在内存 sqlite 上跑产出的语句（sqlite 认反引号
quoted 标识符，MySQL 形状 SQL 原样可跑），且走生产那条**一模一样**的调用形状
`pd.read_sql_query(stmt, conn, chunksize=…)`；布尔恒真探针必须匹配 0 行，并配一条
「同表不过滤确有 3 行」的对照组，免得那个 0 其实是"没连上库"）。「值不进 SQL 文本」用**真方言编译**验而不是只看
`str(text())`：pymysql / mysqldb / mysqlconnector 是 `format` 风格渲染 `%s`、sqlite 是
`qmark` 渲染 `?`，四种渲染文本里都没有哨兵值，「按名字传值、不靠顺序拼串」由
`compiled.params` 的键名证明。HTTP 面刻意用真 `Config()` 而不是哑配置——成功路径会
`clone_config(config)` 遍历 `fields(Config)`，换哑配置就压根跑不到那条代码。
另有一条反向钉子把 `_build_query` 的源码按 `elif request.where:` 切开，要求
`{request.where}` 这个**插值形状**只出现在 flag-off 那一侧。落地时对这 75 项做过 11 处变异
（值内联、两条分支各变死、两处策略调用各被中和、默认值翻转、白名单与枚举放宽、空 `IN` 放行、
`filters` 被静默忽略、原始片段被照收），**全部被抓**。
`tests/test_p114_execution_sandbox.py`（66 项，映射 T2 ⑭/ADR-0007，分七层：默认姿态、声明期校验
（拼写错与"还没实现"是两类错）、端口形状 + **两条 AST 分层钉子**（域层端口不得 import
`agentix.*`/第三方，实现层不得反向 import executor）、口径同源（executor 源码里不得再出现
`to_thread`，`_invoke`/`InProcessSandbox.run`/`invoke_in_process` 三入口对同一批 handler
逐位一致）、真跑 `ToolExecutor.execute`（**flag 关时故意声明跑不了的 `subprocess` 且照常
执行成功** = 声明惰性的反证；flag 开时 `calls == []` = 拒绝真的没执行；拒绝不触发观测钩子；
拒绝压过 retry）、**停摆时长**实测（0.2s 同步 handler × 四种 档位/全局下放 组合，
量事件循环相邻 tick 的最大间隙：占循环的两档 **200–312ms**、下放的两档 **20ms**——
`sandbox="thread"` 不碰全局 flag 就不占循环，而端口开 + 默认档仍然照旧占，
证明打开端口本身不改变语义）、配置五处 + 部署面
（compose 转发默认 `False`、staging override 钉 `true` 与 runbook 矩阵加粗声明逐字对齐 +
改声明就必须红的反向钉子、ADR-0007 存在且进了两处索引、台账没把 ⑭ 写成 T2 完成）。
落地时对这 66 项做过 **19 处变异**（默认值翻转、两处静默退回进程内、executor 不查声明、
声明被忽略、拒绝被吞掉、拒绝记成可重试、拒绝排到观测钩子之后、声明校验放宽、域层染第三方依赖、
实现层懒导入 executor、executor 重新自带下放分支、把 `subprocess` 假装已实现、`thread` 档偷回进程内、
selfcheck / `.env.example` / p41 清单各删键、compose 默认翻 True、staging override 改 false），
**19/19 全部被抓**——其中「selfcheck 与 p41 清单各删键」两条首版**没被抓到**，
根因是 `KEY in text` 把 `KEY_TYPO` 也算同步（见 §15.10 第六条口径），改成整词匹配并补一条
反向钉子后才归位；跑完逐一还原并复测基线绿）。
`tests/test_p115_staging_matrix_generated.py`（44 项，映射 G13 终态：compose 四种转发写法各自的
dev 取值推导、`environment:` 的 mapping 与 `- K=V` 两形等价、表的覆盖面**双向**等于
「override 设了的键 ∪ 署名的手填行」、`check()` 当前必须干净、加粗 ⇔ 被钉 的整表通则、
G13 两例（`UVICORN_RELOAD`/`DB_ECHO`）现在是由 override 算出来的而不是抄的、解析器受
小节边界约束、runbook 表旁必须写着生成器且验收清单第 5 条不再让人眼 diff。
**八种漂移形态各有反向用例**（删 override 的键 / 改 override 的值 / 改表的 staging 声明 /
改表的 dev 声明 / 删表一行 / 多一行未登记的键 / 多一行却加粗 / 把该加粗的抹平），
另对 `scripts/gen_staging_matrix.py` 本身做过 **18 处变异**（`check()` 整个中和、
四个比较分支各变死、两个覆盖面条目各被摘、加粗信号被丢、`environment` 只吃 mapping、
手填行不再参与 / 反而参与校验、`pinned` 恒 False、Settings 默认读不到、`${K:-}` 改由默认值填空、
解析不受小节约束、service 名写错、单元格不剥反引号、行首正则放宽），
**18/18 全部被抓**。最后一条一开始是**等价变异**——`runbook_matrix()` 里正则与
`startswith("| \`")` 两条判据同义，放宽正则被后者挡住，看不出现实改动；删掉那条冗余
startswith 后它才被抓。这类"两条判据描述同一件事"的形态与 §15.9 那句口径同源是同一个病。
前端面另有一组（`npm run test`，vitest + happy-dom，与 pytest 是两个独立入口）：`web/src/modules/knowledge/components/KbRagSettings.test.ts` 15 项，其中 6 项映射 ⑪ 的
可辨识度——403/404 显示服务端的**具体** `message` 而不是所有 FORBIDDEN 共用的
`user_hint`、GET 失败时保存按钮禁用（否则「读取失败仍点保存」会把一整份前端默认值
合并盖掉该 kb 的真实配置）。
