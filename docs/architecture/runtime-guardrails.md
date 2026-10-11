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
| 目标优先级与冲突仲裁 | `domain/policies/goal_arbitration.py` | 纯规则 | — |
| 感知工具结果限量 | `tools/gateway/result_limiter.py`（原 infrastructure/tools，M6 后实体归顶层 tools/） | 100 行 / 8000 字符 | 截断 + warning |
| 人工审批旁路 | `tools/gateway/human_review.py`（原 infrastructure/tools，M6 后实体归顶层 tools/） | `HUMAN_REVIEW_ENABLED=False` | **fail-closed** |
| 声明式任务依赖 | `application/services/task_plan.py` | 纯函数 | 结构非法即报错 |
| 子任务局部重试 | `application/services/task_retry.py` | 3 次尝试 | 失败转数据（不抛） |
| 终态完成度校验 | `domain/policies/completion.py` | `COMPLETION_VALIDATOR_STRICT=False`（只记录） | 校验器自身异常即跳过 |
| Prompt 静态前缀保证 | `domain/policies/prompt_envelope.py` | 类型层强制 + 测试期断言 | 违反即断言失败 |
| 角色差异化 | `application/agents/role_capabilities.py` | 声明表 | 未知角色保守返回空 |

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
合并结果 `topics=['租赁','押金','违约金']`、**溯源 `['m0','m1','m2']`（并集）**。

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

## 6. 目标优先级与冲突仲裁

Agent 同时被多个目标拉扯，而目标**天然冲突**。没有显式规则时，冲突由
**代码的偶然执行顺序**决定——同一类问题在不同路径得到不同处置，且无人能解释。

**两层规则**（先硬后软）：

1. **硬优先级**（`PRECEDENCE`，不可被配置让路）：
   `安全 > 合规 > 准确 > 有用 > 时延 > 成本`
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

这避免了 BM25 按 `kb_id` 缓存时，索引重建切换后仍在 TTL 内读取旧语料；也避免新 chunk 尚未随 manifest 激活就提前进入稀疏通道。检索范围 filter 与法条效力过滤仍在各自通道应用。BM25 是补充通道，数据库读取或构建失败继续 fail-open 到 Dense。

守卫位于 `tests/test_p76_rag_p1_hybrid.py`：同 artifact 缓存复用、不同 artifact 重新加载，并验证 inactive manifest / 非 ready 文档不会进入 sparse 语料。召回质量比较需用同一人工 gold 集，命令与口径见 `evals/README.md` 和 `docs/design/rag-optimization.md` §8。

---

## 10. 测试

| 测试文件 | 覆盖 |
|---|---|
| `tests/test_p30_context_artifacts.py` | 产物存储、外置阈值、降级、整理分组/合并、可重建淘汰、摘要淘汰 |
| `tests/test_p31_control_plane.py` | token 预算三档判定、台账均分、运行登记/取消（含真实 Task 打断）、目标仲裁 |
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
