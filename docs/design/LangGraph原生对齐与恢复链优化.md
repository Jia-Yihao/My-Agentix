# LangGraph 原生对齐与恢复链优化（执行指导）

> **文档状态**：已完成（2026-10-28 交付）· 21 项全部落地；逐项提交与守卫见 §4.6，交付前验收读数见 §8。
> **读者**：执行本方案的 AI 团队（下称「执行方」）
> **层级**：README（现行总览）→ `docs/项目实现手册.md`（实现手册）→ 本文（专题执行指导）→ 各 `docs/design/*` 规划（互不重叠）
> **配套事实快照**：`.workbuddy/deliverables/项目现状-2026-10-27.md`（未进版本库；若拿不到，以本文 §2 的基线读数为准，但**必须自己复跑一遍**）

---

## 0. 五分钟读完

### 0.1 一句话目标

不新增自研能力，先把「框架本来就有、项目却自己写了一遍」的能力换回 LangGraph 原生 API；换不动的 5 类（框架确实不覆盖）才写代码，并且必须挂在原生钩子上。

### 0.2 三条铁律

1. **langchain 家族整族一起走**：1.2 线要求 `langchain-core>=1.4.7`，因此 `langchain` / `langchain-core` / `langchain-community` / `langchain-openai` / `langchain-neo4j` 五个 pin 必须同时升，**不允许只升一半**（半升必然 `ResolutionImpossible`，且 `langchain-neo4j 0.11.0` 反过来要求 `langgraph>=1.0.2`）。完整清单见 §3.2。
2. **不改业务节点内部逻辑**：只给节点加策略（`retry_policy`），不动节点里做什么。
3. **不推翻既有裁决**：审批 pending 以 Postgres 为真源（Q7，守卫为 `tests/test_p136_hitl_pending_persistence.py`）、`AGENTS.md` 纪律 1–8、`docs/项目实现手册.md` §24 的执行边界口径。

### 0.3 本轮改动的总量

| 阶段 | 主题 | 任务数 | 碰依赖 | 大致文件数 |
|---|---|---|---|---|
| **P0** | 复核 + 兼容性 spike + 升到 1.2.18 + 策略集中 | 4（LG-0-0 ~ LG-0-3） | 是（**27 个包，含 4 个大版本跳跃**） | 8–12 |
| **P1** | 重试归位 | 4（LG-1-1 ~ LG-1-4） | 否 | 6–8 |
| **P2** | 恢复归位 | 7（LG-2-1 ~ LG-2-7，含 durability） | 否 | 6–9 |
| **P3** | 原生不覆盖的薄层 | 6（LG-3-1 ~ LG-3-6，超时/补偿改原生） | 否 | 5–8 |
| **D** | `docs` 文档中文化 | 1 批 6 步 | 否 | 278（机械替换）· **已完成** |

### 0.4 执行顺序（不可跳步）

```
LG-0-0 复核 1.2.18 落点（半个班，只读 + 隔离环境）
   ├─ 通过 → LG-0-1 spike（隔离 venv，验证三件事）→ LG-0-2 改 pin → LG-0-3 策略集中
   └─ 不通过 → 立刻停，回报失败项原文，不许自行降级或绕过
                              ↓
                     P1（重试）→ P2（恢复）→ P3（薄层）
                              ↓
                     D（文档中文化）—— 已完成，不占用执行方工期，见 §4.5
```

**每条任务独立提交、独立跑全量回归。** 一条没绿不许开下一条。

---

## 1. 范围

### 1.1 在范围内

- 依赖迁移（`langgraph` 0.2.60 → **1.2.18**，连带 `langchain` 家族整族升到 1.x，共 27 个包，见 §3.2/§3.3）；
- 节点级 `retry_policy` 接入（只读节点）；
- 恢复链：待恢复线程改由 `checkpointer.alist()` 派生、`resume_thread` 的 409 判据、`recursion_limit` 统一、`update_state` 定点修正、`get_state_history` 回看、启动恢复加墙钟；
- 原生不覆盖的薄层：deadline 预算感知、节点超时壳、四个业务指标、恢复期幂等；
- `docs/` 文件名中文化。

### 1.2 明确不做（越界即返工）

| 项 | 理由 |
|---|---|
| **评测体系一切改动** | 本轮不做。`evals/` 目录下任何文件（题集、基线 json、打分脚本、质量门禁）一律不动；`tests/` 里与评测基线绑定的用例不改不删。**注意：LG-3-3 的四个业务指标属可观测性，不是评测。** |
| `compile(store=...)` 长期记忆 | 属 memory 域，`docs/design/记忆域深度优化.md` 已交付，不并行 |
| `DeltaChannel`（1.2 新增） | 1.2 里仍标记为 beta，且要求 reducer 满足「批不变性」；本项目 channel 结构简单，收益不明，登记为后续评估 |
| `version="v2"` 流式契约（1.1 引入） | 会改变 `astream` 的输出形状与 `invoke` 的返回类型，项目 SSE 双链路依赖现有 `stream_mode` 语义，本轮不动 |
| 主图改 `Send` 并行 | 收益不明，改变 checkpoint 语义 |
| `CachePolicy` 节点缓存 | 与现有检索级语义缓存不同层，无需求 |
| 删除自建 `InterruptRecord` | Q7 已裁决 Postgres 为真源，删自建＝推翻既有裁决 |
| `compile(interrupt_before=/after=)` 静态断点 | 与动态中断并存会打架，只在文档写清为什么不用 |

---

## 2. 基线读数（2026-10-27 实测，执行前必须自己复跑）

> 下列数字是**快照**，不是承诺。执行方动手前按 §2.5 的命令各跑一遍，读数不一致就以你跑出来的为准，并在报告里写明差异。

### 2.1 版本

| 包 | 当前 |
|---|---|
| `langgraph` | 0.2.60 |
| `langgraph-checkpoint` | 2.0.26 |
| `langgraph-checkpoint-redis` | 0.0.8 |
| `langgraph-prebuilt` | 0.2.3 |
| `langgraph-sdk` | 0.1.74 |
| `langchain-core` | 0.3.86 |
| `redis` | 5.0.1 |
| `xxhash` | 4.0.1（升级清单未涉及，保持不动） |

### 2.2 主图结构

- 节点 11 个（含 `__start__`，业务节点 10）：`message_window` / `analyze_and_route_query` / `respond_to_general_query` / `get_additional_info` / `create_research_plan` / `create_kb_query` / `create_image_query` / `create_file_query` / `execute_task` / `code_agent`。
- 静态边 2 条（`START → message_window → analyze_and_route_query`），条件边 1 条（router 分派），**无回边**。
- 循环发生在 `execute_task` 内部 agent loop 与 `code_agent` 挂的 kg 子图 —— 所以断点续跑的价值点集中在 `execute_task`（in-loop HITL 的中断点注入在它的包装器里）。

### 2.3 原生能力使用面

| 能力 | 现状 |
|---|---|
| `add_node(..., retry=)` | 0 / 10 节点 |
| `update_state` | 0 |
| `get_state_history` | 0 |
| `durability=` | 0 |
| `compile(store=)` | 0 |
| `get_state` | 2（CLI 与恢复侧） |
| `Send()` | 11（全在 kg 子图） |
| `interrupt()` | 1（`execute_task` 包装器，四条件门控） |

### 2.4 三条恢复路径与五层重试

| 恢复路径 | 入口 | 实际可用性 |
|---|---|---|
| ① 图内 HITL 挂起/恢复 | `POST /ops/hitl/in-loop` | 机制完整，但 `HITL_IN_LOOP_ENABLED` + `HUMAN_REVIEW_ENABLED` 默认都 `False` |
| ② 手工崩溃续跑 | `POST /ops/threads/resume` | 恒 404：依赖的 Redis 集合 `checkpoint_recovery:active_threads` 恒空 |
| ③ 启动期自动恢复 | `main.py` lifespan | 恒不触发（同上） |

集合恒空的根因：唯一的 `sadd` 在 `save_checkpoint_metadata(status="in_progress")` 里，而该方法全仓只有 2 个调用者，都在 `resume_thread` 内部、传的是 `completed` / `failed`（即 `srem`）。

五层重试栈（含首跑次数）：LLM SDK 2~3 → 工具 3 → 任务 3 → 检索 2 → 事件总线 3；图节点 1（不重试）。最坏放大 **18 次**同一下游调用。`RETRY_BUDGET_ENABLED=False`、`RETRY_BUDGET_TOKENS=6`、`AGENT_TIMEOUT=300`、`MAX_ITERATIONS=10`、框架默认 `DEFAULT_RECURSION_LIMIT=25`。

### 2.5 复跑命令

```bash
# 全量基线（必须清 PYTHONPATH，否则 test_p125 假红）
env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -m "not integration" -q
# 期望：5242 passed / 40 skipped / 4 deselected / 0 failed（约 421s）

# 图结构
.venv/Scripts/python.exe -c "import warnings;warnings.filterwarnings('ignore');\
from agentix.orchestration.langgraph import lg_builder as b;print(sorted(b.graph.nodes))"

# 原生 API 使用面
for k in update_state get_state_history durability "Send(" "retry="; do \
  echo -n "$k -> "; grep -rn "$k" --include=*.py agentix | wc -l; done

# 待恢复集合的写入点（应为 0 个业务写入）
grep -rn "save_checkpoint_metadata\|active_threads" --include=*.py agentix

# 已装版本
.venv/Scripts/python.exe -c "import importlib.metadata as m;\
print([(p,m.version(p)) for p in ['langgraph','langgraph-checkpoint','langgraph-checkpoint-redis','langgraph-prebuilt','langgraph-sdk']])"
```

---

## 3. 版本落点：1.2 线（`langgraph==1.2.18`）

### 3.1 选择依据：不是「最新」，是「最稳且最有用」

**已定，不再讨论：落点 `langgraph==1.2.18`。** 判定口径就一条——**最稳定、风评最好**，不按迁移代价选。所以不走 0.5.4 的最小改动路线，也不停在 1.0 / 1.1 这类「先上去再说」的中间档。

两条复核事实（2026-10-27 现场核对）：

- `pip index versions langgraph` → `LATEST: 1.2.18`，往上没有 1.3 / 2.0 这样的新大版本线。也就是说本次「最新」与「最稳」不冲突，1.2.18 既是 1.x 稳定线的最新补丁，也是当前唯一没有更新版本的稳定线。
- 1.2 线全部包的 `Requires-Python` 下界都是 3.10（见 §3.2 末段），本仓 `.venv` 是 **Python 3.12.13**，不存在版本墙。

| 版本 | 发布 | 定位 | 对本项目的价值 |
|---|---|---|---|
| 0.2.60（现状） | — | 起点 | `add_node` 形参叫 `retry=`，与手册不一致 |
| 0.5.4 | — | 命名对齐的最小代价档 | 只买到命名，买不到 1.x 的加固能力 |
| 0.6.11 | — | 0.x 收官版（`durability` 首次出现） | 拿 `durability` 要多付一次迁移，却仍无 1.x 的兼容承诺与加固件，属于「花两次钱买一件」 |
| 1.0 | 2025-10-17 | **第一个稳定大版本**，按 SemVer 主版本内不破 API（下一个破坏点才是 2.0） | 意味着落在 1.x 内的补丁升级不需要再改业务代码 |
| 1.1 | 2026-03-10 | 类型安全（`version="v2"` 流式契约、`GraphOutput`），修 replay / time-travel 边界 | 让 `interrupt` 与子图 checkpoint 的重放语义更可靠 |
| **1.2** | 2026-05-12 | **生产加固线**：`DeltaChannel`、per-node `TimeoutPolicy`、node-level `error_handler`、`RunControl` 优雅关闭 | 四处正好命中本项目痛点 |
| **1.2.18** | 2026-10-26 | 1.2 线第 18 次补丁 | **本轮落点** |

选 1.2.18 而不是 1.0 或 1.2.0 的理由：1.2 是 1.x 里唯一把「节点超时 / 错误补偿 / 优雅关闭 / checkpoint 膨胀」四件事做成原生 API 的版本，而这四件恰好是本方案 P2/P3 里原本要靠自建薄层顶替的部分；1.2.18 是这条线最新补丁，1.2.2 修过「`id=None` 消息在 checkpoint 写入前的稳定 ID」这类一致性问题，继续拿补丁只有收益。

**一条必须说清的不利事实**：1.2.18 发布于 **2026-10-26**，上线仅一天，现场暴露时间最短。它相对 1.2.17 只是补丁级差异（1.x 承诺 2.0 前不破 API），所以选它风险可控；但执行方若在 LG-0-1 里遇到**可归因到补丁本身**的失败，回退顺序是 **1.2.17（同线上一补丁）→ 停**，**不许回 0.x**（0.x 没有本方案要的原生能力，回去等于整个方案作废）。


### 3.2 生态自洽性（整组 dry-run 实测，无冲突）

`langchain` 家族必须整族跟着走，因为 1.2 线要求 `langchain-core>=1.4.7`，而现在的 0.3.86 差一个大版本。

| 包 | 现在 | 目标 | 硬约束（来自 wheel METADATA） |
|---|---|---|---|
| `langgraph` | 0.2.60 | **1.2.18** | — |
| `langgraph-checkpoint` | 2.0.26 | **4.3.1** | 1.2.18 要求 |
| `langgraph-prebuilt` | 0.2.3 | **1.1.1** | 项目未使用 `langgraph.prebuilt` 任何符号，仅为闭包完整性 |
| `langgraph-sdk` | 0.1.74 | **0.4.10** | 1.2.18 要求 |
| `langgraph-checkpoint-redis` | 0.0.8 | **0.5.2** | 要 `checkpoint>=4.1.1,<5`、`redis>=5.2.1`、`redisvl>=0.15,<1` |
| `langchain` | 0.3.30 | **1.4.7** | — |
| `langchain-core` | 0.3.86 | **1.7.0** | community 要 `>=1.4.0`、neo4j 要 `>=1.4.7` |
| `langchain-community` | 0.3.27 | **0.4.2** | **无 1.x**，`0.4.2` 是最后一版，已改为依赖 `langchain-classic` |
| `langchain-neo4j` | 0.3.0 | **0.11.0** | 要 `core>=1.4.7` 且 **`langgraph>=1.0.2`**——这一条反向锁死了「必须升到 1.x」 |
| `langchain-openai` | 0.2.14 | **1.7.3** | — |
| `redis` | 5.0.1 | **>=5.2.1,<6.0.0** | saver 0.5.2 只给下限；**上界必须自己钉**，见 §3.4 |
| `redisvl` | 0.26.0 | 0.26.0 | **不动**（`>=0.15,<1.0` 已满足） |

**Python 版本墙（wheel 的 `Requires-Python` 实测，全部通过）**：`langgraph 1.2.18` / `langgraph-checkpoint 4.3.1` / `langchain-core 1.7.0` / `langchain-openai 1.7.3` 均要求 `>=3.10`；`langchain-neo4j 0.11.0` 与 `langgraph-checkpoint-redis 0.5.2` 要求 `>=3.10,<3.15`。本仓 `.venv` 实测 **Python 3.12.13**，落在区间内。

### 3.3 完整安装清单（27 个包）

`pip install --dry-run` 整组结果（未安装，无冲突、无 `ResolutionImpossible`）：

```
Would install PyYAML-6.0.3 httpcore2-2.16.0 httpx2-2.16.0 jiter-0.18.0
  langchain-1.4.7 langchain-classic-1.0.8 langchain-community-0.4.2
  langchain-core-1.7.0 langchain-neo4j-0.11.0 langchain-openai-1.7.3
  langchain-protocol-0.0.20 langchain-text-splitters-1.1.3
  langgraph-1.2.18 langgraph-checkpoint-4.3.1 langgraph-checkpoint-redis-0.5.2
  langgraph-prebuilt-1.1.1 langgraph-sdk-0.4.10 langsmith-0.14.12
  neo4j-6.4.1 neo4j-graphrag-1.22.0 numpy-2.5.4 openai-3.39.0
  pydantic-settings-2.16.1 pypdf-6.21.2 redis-7.4.1 truststore-0.10.5 websockets-17.3
```

`langchain-classic` / `langchain-protocol` / `httpcore2` / `httpx2` / `truststore` / `neo4j-graphrag` 是 1.x 生态新增的包，不是可选项。

补丁号会随时间漂（2026-10-27 实测 `langsmith` 已从 `0.14.12` 漂到 `0.14.13`），**以你当次 dry-run 的实际输出为准**；上表钉的是主版本与关键包，不追补丁。

### 3.4 真正的风险：四个大版本跳跃

「装得上」不等于「跑得对」。下面四个跨大版本，且都在运行路径上：

| 包 | 现在 → 目标 | 风险面 | 怎么证伪 |
|---|---|---|---|
| `openai` | 1.109.1 → **3.39.0** | `ChatOpenAI` 内部适配；项目 11 处 `langchain_openai` 导入 | LG-0-1 里构造 client + 一次 mock 调用 |
| `neo4j` | 5.27.0 → **6.4.1** | 驱动 API；`Neo4jGraph` 16 处导入，kg 子图核心 | LG-0-1 里 import + 实例化参数校验 |
| `numpy` | 1.26.4 → **2.5.4** | numpy 2.x 改了 `np.float_` 等别名与 copy 语义 | 全量回归 + 检查项目 numpy 用法 |
| `redis` | 5.0.1 → **7.4.1** | redis-py 7.x 的 asyncio 客户端行为 | LG-0-1 的 `asetup` / `put` / `get_tuple` 往返 |

**`redis` 上界必须自己钉住**：整组 dry-run 给出 `redis-7.4.1`，是因为 saver 只写了下限没写上界，pip 就取了最高版。方案要求写 `redis>=5.2.1,<6.0.0`（或经 spike 验证后再放宽），**不许让 pip 自己决定**。

### 3.5 import 路径不用改（实测）

这是这次迁移最重要的好消息：升级不会被 import 路径打断。

- `langchain-core 1.7.0` 保留了项目用到的全部 11 个子模块：`messages` / `language_models` / `prompts` / `documents` / `runnables` / `output_parsers` / `embeddings` / `tools` / `retrievers` / `outputs` / `vectorstores`。
- `langchain-community 0.4.2` 仍有 `vectorstores`（97 个文件），项目用的是 `langchain_community.vectorstores`。
- 项目导入面：`langchain*` 涉及 **118 个文件**（`agentix` 74 / `tests` 41 / `scripts` 2 / `evals` 1），共 17 个模块；`langgraph*` 只涉及 **10 个符号**（`StateGraph` / `START` / `END` / `Command` / `Send` / `MemorySaver` / `CompiledStateGraph` / `AsyncRedisSaver` / `interrupt` / `add_messages` / `RemoveMessage`）。
- 项目**完全没有**使用 `langgraph.prebuilt`，所以 1.0 对 `prebuilt` 的废弃对本仓无影响。

⇒ 迁移的工作量集中在「签名与行为差异」，不在「改 import」。

### 3.6 1.2 线一次性解锁的四件事

| 能力 | 1.2 原生 API | 对应本方案 | 原计划怎么顶的 |
|---|---|---|---|
| `durability` 三档（`sync` / `async` / `exit`） | `astream(..., durability=)` | LG-2-7 | 原方案删除（0.5.4 没有），现恢复 |
| 节点级超时 | `add_node(..., timeout=TimeoutPolicy(run_timeout=, idle_timeout=))` | LG-3-2 | 原用 `asyncio.wait_for` 壳顶替，**改为原生** |
| 节点级错误补偿 | `error_handler`（重试耗尽后触发） | 新增 LG-3-5 | 无 |
| 优雅关闭（可恢复） | `astream(..., control=RunControl())` → `GraphDrained` | 新增 LG-3-6 | 无（启动恢复加墙钟只是缓解） |

`durability` 的默认值是 `"async"`（每步后台写、下一步不等），与现状 0.2.60 的行为一致；要「每步强制等落盘」必须显式写 `durability="sync"`。**默认值不是可靠性升级，别误读。**

1.2.18 的 `add_node` 完整形参（wheel 里 `langgraph/graph/state.py` 原文）：
`defer` / `metadata` / `input_schema` / `retry_policy` / `cache_policy` / `error_handler` / `destinations` / `timeout` / `trace_policy`。**本轮只用 `retry_policy`、`error_handler`、`timeout`**，其余留白。

三个容易踩的语义细节（已在 wheel 源码里确认，执行方别再猜）：

- `NodeTimeoutError` **故意不继承内置 `TimeoutError`**，就是为了让默认 `RetryPolicy` 把它当可重试异常 ⇒ 一个节点同时挂 `timeout` 和 `retry_policy` 时，超时会触发重试并成倍放大耗时。LG-3-2 必须二选一或显式限定 `max_attempts`。
- `RunControl` 是**调用级**参数：`astream(..., control=ctl)` 传入，`ctl.request_drain()` 在**超步边界**生效并抛 `GraphDrained`（`langgraph.errors`，继承 `GraphBubbleUp`，与 `interrupt` 同族，会穿透到流式外层）。源码注释明确要求**每次运行新建一个**，复用已 drain 的实例等于永久 drain。
- `GraphDrained` 抛出时 checkpoint 已保存，线程可以被 `alist()` 重新列出——这是 LG-3-6 与 LG-2-1 的连接点。


### 3.7 残余风险（LG-0-1 必须逐项证伪）

1. `MemorySaver` 与 `CompiledStateGraph` 在 1.x 是否仍从原路径导出（1.x 引入了 `InMemorySaver` 命名）。
2. `AsyncRedisSaver 0.5.2` 的 `asetup` / `put` / `get_tuple` / `alist(None)` 是否与 `checkpoint 4.3.1` 行为一致（结构兼容已由方法集核对，行为只能跑）。
3. `interrupt()` 在 1.2 收紧过语义（原先 race 条件下可能漏触发，现在准确触发）——项目 `multi_tool.py` 记录的 async 子图老坑是否消失，以及**是否有代码依赖过旧的「漏触发」行为**。
4. `Neo4jGraph` 在 `langchain-neo4j 0.11.0` 的构造参数是否变（16 处调用点）。
5. `ChatOpenAI` 在 `langchain-openai 1.7.3` 下 `max_retries` / `timeout` 参数名是否变（`model_gateway` 与 `provider_registry` 依赖）。

## 4. 任务清单

编号 `LG-<阶段>-<序号>`。每条含「做什么 / 改哪里 / 怎么算过」；规模栏是相对量，不是工时承诺。

### 4.1 P0 · 地基

#### LG-0-0 · 复核 1.2.18 落点（插在 LG-0-1 之前，只读）

| 项 | 内容 |
|---|---|
| **做什么** | 用你自己的命令复核 §3 的七条结论，尤其「整组 27 个包无冲突」与「`redis` 上界必须自己钉」 |
| **改哪里** | 不改仓库。只允许 `%TEMP%` 下建临时目录、`pip download` / `pip install --dry-run` |
| **怎么算过** | ① §3.3 的整组 dry-run（12 个 spec）无 `ResolutionImpossible`；② `Would install` 含 `langgraph-1.2.18`、`langchain-core-1.7.0`、`langgraph-checkpoint-redis-0.5.2`；③ `langchain-neo4j 0.11.0` 的 METADATA 里确有 `langgraph>=1.0.2`（这是「必须升到 1.x」的硬证据）；④ `langchain-core 1.7.0` 的 wheel 里 11 个子模块齐全；⑤ `langchain-community` 可用版本最高确为 `0.4.2`（无 1.x）；⑥ **只**把 `langgraph-checkpoint-redis==0.5.2` 加进来做 dry-run 时，pip 会把 `redis` 拉到 7.4.1（saver 只写 `redis>=5.2.1`、没写上界）；`langgraph` 自身不带 `redis` 依赖，别把两件事混谈；⑦ 各 wheel 的 `Requires-Python` 下界不高于本机 Python（本仓 `.venv` 实测 3.12.13，下界均为 3.10），`langchain-neo4j` / saver 的 `<3.15` 上界也不卡住 |
| **规模** | 小（纯只读） |

**回报要求**：把命令原始输出贴回来，不许只写「已验证」。

#### LG-0-1 · 兼容性 spike（隔离 venv，7 项）

| 项 | 内容 |
|---|---|
| **做什么** | 仓库外临时 venv 装 §3.2 的整组，跑最小脚本验证 7 件事。**不许动仓库的 `.venv`** |
| **改哪里** | 临时 venv + 临时脚本；仓库零改动 |
| **怎么算过** | 逐项给「通过/失败 + 报错原文」：① `AsyncRedisSaver 0.5.2` 的 `asetup` / `put` / `get_tuple` / `alist(None)` 四项往返成功；② `from langgraph.checkpoint.memory import MemorySaver` 与 `from langgraph.graph.state import CompiledStateGraph` 仍可导入；③ async 节点内 `interrupt()` 不再抛 `Called get_configurable outside of a runnable context`；④ `Neo4jGraph` 可导入，构造参数与现在一致；⑤ `ChatOpenAI` 可构造，`max_retries` / `timeout` 仍被接受；⑥ `compile(checkpointer=...).astream(...)` 跑通一轮，且 `durability="sync"` 被接受；⑦ `add_node(..., timeout=TimeoutPolicy(run_timeout=...))` 被接受且能抛 `NodeTimeoutError`。任一项失败 ⇒ 停，回报原文；**若失败可归因到 1.2.18 这个补丁本身，只允许回退到 1.2.17 再验一次，不许回 0.x、不许删方案** |
| **规模** | 中 |

#### LG-0-2 · 依赖迁移落地

| 项 | 内容 |
|---|---|
| **做什么** | 按 §3.2 改 `requirements.txt`：11 个 pin 改写，`redis` 写成 `>=5.2.1,<6.0.0`（**上界自己钉，不许交给 pip**） |
| **改哪里** | `requirements.txt`；若 `Neo4jGraph` / `ChatOpenAI` 构造签名有变，按 LG-0-1 的结论在 `agentix/application/agents/kg_sub_graph/kg_neo4j_conn.py` 与 `agentix/infrastructure/llm/provider_registry.py` 做最小适配 |
| **怎么算过** | ① 全量回归 **5242 条 0 failed**（本轮最难绿的一条，允许分阶段归因，但最终必须 0 failed）；② `tests/test_p132_checkpointer_fail_closed.py` 全绿；③ `ruff check agentix tests` 干净；④ `python scripts/gen_staging_matrix.py --check` 退出 0 |
| **规模** | 大（风险最高的一条） |

#### LG-0-3 · 策略集中

| 项 | 内容 |
|---|---|
| **做什么** | 在 `agentix/orchestration/langgraph/` 下新建 `policies.py`（本轮待创建），集中产出 `readonly_retry_policy()` / `node_timeout()` 等；形参名统一 `retry_policy` / `timeout`，禁止在节点注册处内联构造策略 |
| **改哪里** | 新文件 + `agentix/orchestration/langgraph/lg_builder.py` 的引用 |
| **怎么算过** | 新增守卫用例断言：全仓 `add_node(` 调用不出现裸 `RetryPolicy(` / `TimeoutPolicy(` 字面构造；策略参数只从 `policies.py` 出。既有用例不改 |
| **规模** | 小 |

### 4.2 P1 · 重试归位

#### LG-1-1 · 只读节点接 `retry_policy`

| 项 | 内容 |
|---|---|
| **做什么** | 给 6 个只读节点接原生重试：`analyze_and_route_query` / `respond_to_general_query` / `create_research_plan` / `create_kb_query` / `create_image_query` / `create_file_query`。**`execute_task` 与 `code_agent` 明确不给**（整节点重跑会重放工具副作用） |
| **改哪里** | `lg_builder.py` 节点注册段 |
| **怎么算过** | ① 注入 429 后节点内重试一次成功；② 断言 `execute_task` / `code_agent` 的注册里没有 `retry_policy`；③ 全量回归 0 failed |
| **规模** | 中 |

#### LG-1-2 · `is_retryable` → `retry_on`

| 项 | 内容 |
|---|---|
| **做什么** | 把现有业务分类函数适配成 `RetryPolicy(retry_on=...)` 的回调形态，作为唯一分类真源 |
| **改哪里** | `agentix/domain/policies/retry.py` → 由 `policies.py` 转出 |
| **怎么算过** | 参数化用例逐条同值（同一批异常/状态码，新旧判定一致）；断言实际生效的是自定义 `retry_on`、不是框架默认 `default_retry_on` |
| **规模** | 小 |

#### LG-1-3 · 三层自研退避与原生默认同源

| 项 | 内容 |
|---|---|
| **做什么** | 工具层补 `max_delay` 与抖动；任务层、检索层的 `initial/max/jitter` 统一从 `policies.py` 取，与 `RetryPolicy` 默认（`initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, max_attempts=3, jitter=True`）对齐 |
| **改哪里** | `agentix/tools/gateway/executor.py`、`agentix/application/services/task_retry.py`、检索侧 `kb_retrieval.py` |
| **怎么算过** | 参数快照用例：三处读到的值与 `RetryPolicy` 默认逐项相同；工具层不再出现无上限的 `0.2·2^n` |
| **规模** | 中 |

#### LG-1-4 · 打开预算桶

| 项 | 内容 |
|---|---|
| **做什么** | `RETRY_BUDGET_ENABLED` 代码里**仍默认 False**；在 `.env.example` 与 staging override 里写推荐值 `true` 并注明理由 |
| **改哪里** | `agentix/config/settings.py`（若需注释）、`.env.example`、`docker/compose.override.staging.yml`、配置文档 §5 表、`docs/runbooks/预发环境与配置矩阵.md` 生成物 |
| **怎么算过** | ① flag-off 与基线逐位等价；② flag-on 下三层共享 token，耗尽即停；③ 五处同步齐全（纪律 4）；④ `gen_staging_matrix.py --check` 退出 0 |
| **规模** | 中 |

### 4.3 P2 · 恢复归位

> 这里 7 条。1.2 线自带 `durability`，原方案删掉的 LG-2-6 已恢复（见 §3.6）。

#### LG-2-1 · 待恢复线程改由 `alist()` 派生（零依赖，可提前做）

| 项 | 内容 |
|---|---|
| **做什么** | `/ops/active-threads` 与启动恢复不再读 Redis 集合，改为 `checkpointer.alist(None, limit=...)` 派生「未完成」的线程；删除 `active_threads` 集合及 `save_checkpoint_metadata` 里的 `sadd`/`srem` |
| **改哪里** | `agentix/application/services/breakpoint_resume.py` |
| **怎么算过** | ① 走一轮未完成请求后该线程能被列出；② 完成后不再列出；③ `alist` 抛异常时**明确报错或降级并 `logger.warning`**，不许静默返空；④ 新增用例覆盖「有 1 个未完成 + 1 个已完成」的场景 |
| **规模** | 中 |

> 这条不依赖升级：现装 checkpoint 2.0.26 的 `alist` 已支持 `config=None`。可以排在 P0 之前先做，作为「断点续跑先能用」的快赢；若这么做，必须先跑一次全量回归建立对照。

#### LG-2-2 · `resume_thread` 的 409 判据

| 项 | 内容 |
|---|---|
| **做什么** | 续跑前先 `get_state()`，`snapshot.interrupts` 非空 ⇒ 返回 409 并指向 `/ops/hitl/in-loop`；不再用 `ainvoke(None)` 制造「200 但图又挂回去」 |
| **改哪里** | `breakpoint_resume.py` + `ops_router.py` 的状态码 |
| **怎么算过** | 有挂起时端点返回 409 且图状态不变；无挂起时行为与现状一致 |
| **规模** | 小 |

#### LG-2-3 · `recursion_limit` 统一

| 项 | 内容 |
|---|---|
| **做什么** | 三处 config 构造走同一个 helper：`{"configurable": {"thread_id": ...}, "recursion_limit": MAX_ITERATIONS}`，消掉恢复路径回落 25 的问题 |
| **改哪里** | `lg_builder.resume_in_loop`、`breakpoint_resume`、`chat_service` |
| **怎么算过** | 三处构造出的 config 里 `recursion_limit == 10`（参数化断言）；恢复请求不再触发 `GraphRecursionError` 的行为差异 |
| **规模** | 小 |

#### LG-2-4 · 定点修正端点

| 项 | 内容 |
|---|---|
| **做什么** | 新增 `GET /ops/threads/{tid}/tasks`（列出 task）与 `POST /ops/threads/{tid}/retry-task`（用 `update_state(task_id=...)` 只重跑失败的那个 task） |
| **改哪里** | `ops_router.py` + 恢复服务 |
| **怎么算过** | 计数桩证明：只重跑失败的 task，同节点其他 task 不重放；越权/不存在的 thread 返回 404 |
| **规模** | 大 |

#### LG-2-5 · 历史回看端点（只读）

| 项 | 内容 |
|---|---|
| **做什么** | 新增 `GET /ops/threads/{tid}/history`，用 `get_state_history` 返回步骤序列 |
| **改哪里** | `ops_router.py` |
| **怎么算过** | 返回步骤数与轨迹条数对齐；**断言不写任何状态**（前后 `get_state` 的 checkpoint_id 不变） |
| **规模** | 中 |

#### LG-2-6 · `durability` 参数化（1.2 原生）

| 项 | 内容 |
|---|---|
| **做什么** | 新增 Settings 字段 `GRAPH_DURABILITY`，取值 `"sync"` / `"async"` / `"exit"`，**默认 `"async"`**——这是 1.2 的框架默认值，且与现状 0.2.60「每步后台写、下一步不等」的行为一致。在 `astream` 调用处透传；代码注释写清三档语义，并注明「默认值不是可靠性升级」 |
| **改哪里** | `agentix/config/settings.py`（新字段 + 五处同步）、`agentix/orchestration/langgraph/lg_builder.py` 或 `chat_service.py` 的 `astream` 调用点 |
| **怎么算过** | ① 默认 `"async"` 时与基线逐位等价（纪律 1）；② 显式 `"sync"` 时，节点执行完立刻 `get_state` 能读到该步 checkpoint（`async` 下可能读不到）；③ 非法取值启动即报错，不许静默回落 |
| **规模** | 小 |

#### LG-2-7 · 启动恢复加墙钟与并发上限

| 项 | 内容 |
|---|---|
| **做什么** | lifespan 里的串行恢复改成 `asyncio.wait_for(AGENT_TIMEOUT)` + `Semaphore(2)` + 总预算 |
| **改哪里** | `agentix/main.py` |
| **怎么算过** | 注入 3 个待恢复线程，启动耗时不超过总预算且不随 N 线性增长；单个卡死不会拖垮启动 |
| **规模** | 中 |

### 4.4 P3 · 原生不覆盖的薄层

#### LG-3-1 · deadline 预算感知

| 项 | 内容 |
|---|---|
| **做什么** | 三处退避的 `sleep` 改为 `min(delay, remaining)`，余量不足直接放弃并把该次尝试标记为 `truncated` |
| **改哪里** | `agentix/domain/policies/retry.py`、`task_retry.py`、`executor.py` |
| **怎么算过** | 剩余 1s 时不进入第 3 次尝试；`truncated` 计数可见；无 deadline 时行为与基线逐位等价 |
| **规模** | 中 |

#### LG-3-2 · 节点级原生超时（1.2 `TimeoutPolicy`）

| 项 | 内容 |
|---|---|
| **做什么** | 用 `add_node(..., timeout=TimeoutPolicy(run_timeout=..., idle_timeout=...))` 取代原计划的 `asyncio.wait_for` 壳。开关 `NODE_TIMEOUT_SECONDS` 默认 `0`（关）；为 `0` 时不传 `timeout` 参数，保持逐位等价 |
| **改哪里** | `agentix/orchestration/langgraph/` 下的 `policies.py`（策略集中）+ `lg_builder.py` 的 `add_node` 注册处 |
| **怎么算过** | ① 关时逐位等价（完全不传 `timeout`）；② 开时超时节点抛 `NodeTimeoutError`，并能被 `retry_policy` / `error_handler` 接住，不让整轮静默失败；③ **保留原方案的边界说明**：`run_timeout` 只对「把事件循环让出来」的调用有效，同步阻塞节点仍然杀不掉（`AGENTS.md` 纪律 6 ②）；④ 同节点若已有 `retry_policy`，必须显式限定重试次数或从 `retry_on` 排除 `NodeTimeoutError`（它被框架设计成「可重试」，不排除就会超时→重试→再超时） |
| **规模** | 中 |

#### LG-3-3 · 四个业务指标

| 项 | 内容 |
|---|---|
| **做什么** | `retry_attempts_total{layer}` / `retry_sleep_seconds_total{layer}` / `retry_exhausted_total{layer}` / `graph_resume_total{kind}` |
| **改哪里** | 观测指标模块 + `tests/test_p100_genai_observability.py` 的 `ALLOWED_METRIC_LABELS` 白名单 |
| **怎么算过** | 每层都能观测到；label 值全部有界（`layer` 是枚举、`kind` 是枚举）；**不得引入 thread_id / user_id 等高基数 label** |
| **规模** | 中 |

#### LG-3-4 · 恢复期幂等

| 项 | 内容 |
|---|---|
| **做什么** | 已完成 step 落持久清单（键含 thread + step），恢复时跳过 |
| **改哪里** | 节点壳 + 持久层 |
| **怎么算过** | 恢复一次后副作用只发生一次（计数桩）；与 `tests/test_p143_run_idempotency.py` 的**客户端**幂等分层，互不援引 |
| **规模** | 大 |

#### LG-3-5 · 节点级错误补偿（1.2 `error_handler`）

| 项 | 内容 |
|---|---|
| **做什么** | 给 `execute_task` 等有副作用的节点注册 `error_handler`：重试耗尽后写补偿记录并路由到明确的失败出口，而不是整轮 500。开关默认关 |
| **改哪里** | `policies.py` + `lg_builder.py` |
| **怎么算过** | ① 关时逐位等价；② 开时重试耗尽进入 `error_handler`，返回 `Command` 能改 state 并完成路由；③ 补偿记录复用 Postgres 既有 `hitl_pending` 口径（Q7 真源），**不新建存储** |
| **规模** | 中 |

#### LG-3-6 · 优雅关闭（1.2 `RunControl`）

| 项 | 内容 |
|---|---|
| **做什么** | 收到 SIGTERM 时 `ctl.request_drain()`，等当前超步结束、留下可恢复 checkpoint 再退出，替代「直接杀进程、靠下次启动猜」。API 是**调用级**的：`from langgraph.runtime import RunControl`，每次运行新建实例，`astream(..., control=ctl)` 传入，drain 在超步边界抛 `GraphDrained` |
| **改哪里** | `agentix/main.py` 的 lifespan / 信号处理 + `chat_service` 的 `astream` 调用点；`GraphDrained` 的捕获在流式外层（与 `interrupt` 同级处理） |
| **怎么算过** | ① 开关默认关；② 开时发 SIGTERM 后能观察到 `GraphDrained`，且该线程可被 `alist` 列出；③ drain 有超时上限，超时仍强制退出（不无限等）；④ 断言 `RunControl` 是每请求新建（复用已 drain 的实例会永久 drain） |
| **规模** | 中 |

### 4.5 D 段 · `docs` 文档中文化（**已完成**）

2026-10-27 由维护者按下列 6 步执行完毕：**63 份文件改名**（`docs` 下 76 份 md 中，4 份 `README.md` 保留原名、9 份原本已是中文），全仓 158 个文件里的 1166 处引用同步替换，`My-Agentix项目说明书.docx` 内 24 处路径一并更新。**执行方不需要重做，也不许再改名**——附录 C 从此只作历史映射使用。

原始步骤留档如下（复现或回退时按此走）：

| 步 | 动作 | 验证 |
|---|---|---|
| 1 | 把 §附录 C 的映射转成 JSON | 人工核对映射 |
| 2 | Python 脚本 `os.rename` 逐份改名（**不走 shell**，Windows 中文路径会乱码） | `find docs -name "*.md" \| wc -l` 数量不变 |
| 3 | 同脚本替换引用：`docs/旧路径.md` → `docs/新路径.md`，含相对链接 `](旧名.md)` | — |
| 4 | 跑 `tests/test_p41_doc_consistency.py` + `tests/test_p101_integrity_guards.py` | 两条全绿即无漏改 |
| 5 | 全量回归 | 0 failed |
| 6 | 同步 `AGENTS.md` 的「文档同步面」与 README §14 的路径 | p41 的路径与章数断言全绿 |

**两个已定口径**：目录名**不改**（278 个文件带路径，风险翻倍）；4 份 `README.md` **保留原名**（GitHub/GitLab 只认这个名字做目录自动索引）。

**回退方式**：`git status` 里的重命名按反向映射 `os.rename` 回去，再 `git checkout -- <被改的引用文件>`；不要逐个手改引用。

### 4.6 交付台账（2026-10-28）

21 项全部落地，一条任务一次提交。新增守卫合计 **277 条**；起始基线 5242 + 277 = **5519**，与 2026-10-28 全量读数逐条对得上（见 §8）——这是「基线增量 = 新用例数」的自查，不是估算。

| 任务 | 提交 | 新增守卫（`tests/`） | 用例数 |
|---|---|---|---|
| LG-0-0 | 无（只读复核，仓库零改动） | — | 7 项结论逐条核对，原始输出见当轮报告 |
| LG-0-1 | 无（隔离 venv spike，仓库零改动） | — | 7 项兼容性逐项给「通过/失败 + 报错原文」 |
| LG-0-2 | `c244c69` | `test_dependency_consistency`（28）· `test_p43_path_safety`（29）· `test_p132_checkpointer_fail_closed` | 全量 5242 / 0 failed |
| LG-0-3 | `ac2436a` | `test_p147_policy_centralization.py` | 18 |
| LG-1-1 | `127883b` | `test_p148_node_retry_wiring.py` | 12 |
| LG-1-2 | `a2fa896` | `test_p149_retry_on_business.py` | 14 |
| LG-1-3 | `109483c` | `test_p150_retry_backoff_alignment.py` | 19 |
| LG-1-4 | `42ee24e` | `test_p151_retry_budget_deployment.py` | 4 |
| LG-2-1 | `54c9253` | `test_p152_pending_threads.py` | 11 |
| LG-2-2 | `54da09f` | `test_p153_resume_interrupted_conflict.py` | 5 |
| LG-2-3 | `43b072c` | `test_p154_recursion_limit_unified.py` | 10 |
| LG-2-4 | `f025b76` | `test_p155_task_point_retry.py` | 9 |
| LG-2-5 | `23ce625` | `test_p156_thread_history.py` | 11 |
| LG-2-6 | `b5958aa` | `test_p157_graph_durability.py` | 18 |
| LG-2-7 | `0674655` | `test_p158_startup_recovery_bounds.py` | 11 |
| LG-3-1 | `310c745` | `test_p159_retry_deadline_budget.py` | 16 |
| LG-3-2 | `a4f2fd8` | `test_p160_node_timeout.py` | 26 |
| LG-3-3 | `800f09b` | `test_p161_retry_resume_metrics.py` | 22 |
| LG-3-4 | `0b13b57` | `test_p162_recovery_step_ledger.py` | 26 |
| LG-3-5 | `7e5fab2` | `test_p163_node_error_handler.py` | 20 |
| LG-3-6 | `cd06293` | `test_p164_run_drain.py` | 25 |

---

## 5. 写代码的规范（执行方硬约束）

1. **新开关默认关**：新增 Settings flag 一律 `False` / `0` 起步，flag-off 路径与基线逐位等价。推荐值只写部署面（`.env.example` + staging override）。
2. **fail-open 边界**：指标 / 记忆 / 指令 / 预算等辅助子系统出错不影响主链路，捕获后降级并 `logger.warning` 留痕。宽 `except` 必须带 `# noqa: BLE001 - <理由>`，理由要写清降级去向。
3. **指标 label 有界**：新指标 label 必须先进 `ALLOWED_METRIC_LABELS` 白名单；禁止 user / session / trace / 问答内容进指标（只进 span）。
4. **配置键五处同步**：`settings.py` / `selfcheck.py` 的 `DEFAULT_IGNORED_MISSING` / `.env.example` / `docs/architecture/配置管理.md` §5 表 / p41 的反向清单，缺一处就是红。
5. **注释用简体中文**，说明「为什么」而非「是什么」。
6. **测试文件命名** `tests/test_p<NN>_<主题>.py`：执行前先 `ls tests` 占号，**不许复用已占用的编号**；模块 docstring 写清覆盖面与验收口径。
7. **异步规范**：CPU/IO 阻塞调用经 `asyncio.to_thread` 下放。

---

## 6. 执行协议（怎么干活）

### 6.1 节奏

- **一条任务一次提交**：改 → 跑全量 → 报告 → 等确认 → 下一条。
- 报告格式固定三行区：**改了什么**（文件清单 + 一句话）／**怎么验证的**（命令 + 原始读数）／**风险与未决**（不确定的地方必须写，不许写「应该没问题」）。
- 全量回归**必须** `env -u PYTHONPATH ...`，否则 `test_p125_subprocess_sandbox` 会假红（已知坑）。
- 任何一条任务让基线数字变化（passed 增减），必须在报告里逐条归因到具体用例。

### 6.2 禁止事项

| 禁止 | 原因 |
|---|---|
| 改 `evals/` 下任何文件、碰评测基线与质量门禁 | 本轮明确不做 |
| 用 `--no-verify` 跳过 hook、或改守卫让红变绿 | `AGENTS.md`；红点必须先解释再修 |
| 删除/弱化既有守卫来「通过」 | 同上；确需调整口径必须先报清单 |
| 动 `execute_task` / `code_agent` 的节点级重试 | 会重放工具副作用 |
| 把 `durability` 用自建方式「模拟」出来 | 违背「原生优先」主线 |
| 做文件级备份（`*.bak` / `*.orig` / 整目录副本） | 维护者明确要求；回退靠 git |
| 提交前不跑全量 | 本仓 5242 条用例是唯一门禁 |

### 6.3 回滚

改动都在 git 管理下，回滚用 `git checkout -- <path>` / `git revert`；**依赖回滚必须整文件回退**——`git checkout HEAD -- requirements.txt` 后重装，不要手改单个 pin（1.2 迁移里 11 个 pin 互为约束，留一个旧值就会 `ResolutionImpossible`）。

---

## 7. 风险与红线

| 风险 | 触发条件 | 兜底 |
|---|---|---|
| checkpoint 4.3.1 与 saver 0.5.2 行为不兼容 | LG-0-1 的 `asetup` / `put` / `get_tuple` 失败 | 停，回报原文；1.2 迁移**没有「半升」的中间态**（`langchain-neo4j 0.11.0` 反向要求 `langgraph>=1.0.2`），兜底是整体回 0.2.60：`git checkout HEAD -- requirements.txt` + revert 本批代码 |
| checkpointer 静默降级 MemorySaver | 初始化异常被吞 | `CHECKPOINTER_FAIL_CLOSED=true` 下必须报错；`test_p132` 必须绿 |
| 升级后基线漂移 | 依赖变更 | 每批前后各跑一次全量；数字变化逐条归因 |
| 1.2.18 上线仅一天，暴露时间短 | LG-0-1 出现可归因到补丁的失败 | 回退到 1.2.17（同线上一补丁）重验；**不回 0.x**，因为 0.x 没有本方案要的原生能力 |
| `retry` / `retry_policy` 写混 | 文档与代码不同步 | LG-0-3 守卫禁裸策略；文档同时标注两种形参名（0.2 线叫 `retry`，1.x 叫 `retry_policy`） |
| `alist(None)` 全量列举变慢 | 线程数增长 | 必须带 `limit` 与分页；在 P2 用例里断言不无条件全表扫 |
| 改名漏引用 | D 段 | p41 + p101 两条守卫 |
| 新开关默认开的诱惑 | 预算桶 / 节点超时 | 纪律 1 |

---

## 8. 交付前的验收清单

- [x] `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -m "not integration" -q` → **0 failed**（2026-10-28：5519 passed / 40 skipped / 4 deselected / 0 failed，453.16s）
- [x] `ruff check agentix tests` 干净（`All checks passed!`）
- [x] `mypy agentix/domain` 0 错（`Success: no issues found in 38 source files`）
- [x] `python scripts/gen_staging_matrix.py --check` 退出 0
- [x] `tests/test_p41_doc_consistency.py` 与 `tests/test_p101_integrity_guards.py` 全绿（2026-10-28 与 p132 合并复跑 63 passed）
- [x] `tests/test_p132_checkpointer_fail_closed.py` 绿（升级后重跑：4 passed）
- [x] `pip check` 无 broken requirement（`No broken requirements found.`）
- [x] 新增端点有端点级用例（LG-2-4/2-5 的 p155/p156 走路由级 200/404/409/422）
- [x] 新增配置键五处同步齐全（`GRAPH_DURABILITY` / `NODE_TIMEOUT_SECONDS` / `NODE_ERROR_HANDLER_ENABLED` / `RECOVERY_STEP_LEDGER_ENABLED` / `RUN_CONTROL_ENABLED` / `RUN_DRAIN_TIMEOUT_S` 均逐处落位）
- [x] `CHANGELOG.md` 的 `[Unreleased]` 有条目；README 若动默认值/基线则同步（README §11 已 re-cite 至 5519）
- [x] 没有留下 `*.bak` / 临时脚本 / 调试打印（本批文件 grep `breakpoint()/pdb.set_trace/print("DEBUG")` 零命中；`.tmp/` 下旧备份早于本批且被 gitignore）
- [x] 报告里给出了每条任务的原始验证读数（交付台账 §4.6 + 各轮报告）

---

## 附录 A · 证据清单（本方案结论的复跑方式）

| 结论 | 命令 |
|---|---|
| 0.2.60 的 `add_node` 形参叫 `retry` | `python -c "import inspect;from langgraph.graph.state import StateGraph;print(list(inspect.signature(StateGraph.add_node).parameters))"` |
| 1.2.18 的形参叫 `retry_policy` + `cache_policy`，并新增 `timeout` | `pip download langgraph==1.2.18 --no-deps -d /tmp/x` 后读 wheel 内 `langgraph/graph/state.py` 的 `def add_node` |
| `RunControl` 的导入路径与 drain 语义 | 读 wheel 内 `langgraph/runtime.py`（`class RunControl`、`request_drain`、`drain_requested`）与 `langgraph/pregel/main.py`（`control:` 形参、超步边界抛 `GraphDrained`） |
| `NodeTimeoutError` 不是 `TimeoutError` 子类（所以默认可重试） | 读 wheel 内 `langgraph/errors.py` 的 `class NodeTimeoutError` docstring 原文 |
| `durability` 只接受 `sync` / `async` / `exit` | 读 wheel 内 `langgraph/types.py` 的 `Durability` 字面量类型 |
| `durability` 是 0.6.0 才引入、0.5.4 只有前身 `checkpoint_during` | 分别解压 0.5.4 / 0.6.0 wheel，全量 grep `durability` 与 `checkpoint_during`（0.5.4: durability 0 命中、checkpoint_during 28 命中） |
| `durability` 默认是 `"async"` | 读 0.6.x/1.2.x 的 `langgraph/pregel/main.py`：`durability = config.get(CONF, {}).get(CONFIG_KEY_DURABILITY, "async")` |
| 1.2 整组可解析、动 27 个包 | 整组 `pip install --dry-run`（§3.3 的 12 个 spec），看 `Would install` 行 |
| `langchain-neo4j 0.11.0` 反向要求 `langgraph>=1.0.2` | 读其 wheel 的 `METADATA` 的 `Requires-Dist` |
| 1.2.18 就是当前 `LATEST`，没有更新的稳定线 | `pip index versions langgraph`（输出末两行 `INSTALLED` / `LATEST`） |
| 1.2 线不卡 Python 版本 | 解压各 wheel 读 `METADATA` 的 `Requires-Python`（下界均 3.10，neo4j 与 saver 上界 `<3.15`），再对 `python -V` 比对 |
| `langchain-community` 最高只有 `0.4.2`（无 1.x） | `pip index versions langchain-community` |
| `langchain-core 1.7.0` 保留 11 个子模块 | 解压 wheel，检查 `langchain_core/{messages,prompts,runnables,documents,tools,retrievers,embeddings,output_parsers,outputs,language_models,vectorstores}/` |
| saver 0.5.2 要 `checkpoint>=4.1.1,<5` 与 `redis>=5.2.1`（无上界） | 读其 wheel 的 `METADATA` |
| `alist` 的 `config` 可为 None | `inspect.signature(BaseCheckpointSaver.alist)`；再看 saver 的 `alist` 实现里 `if config:` 分支 |
| 框架默认递归上限 25 | `python -c "from langchain_core.runnables.config import DEFAULT_RECURSION_LIMIT;print(DEFAULT_RECURSION_LIMIT)"` |
| `RetryPolicy` 默认值 | `python -c "from langgraph.types import RetryPolicy;print(RetryPolicy())"` |
| 全仓 `active_threads` 无业务写入点 | `grep -rn "save_checkpoint_metadata\|active_threads" --include=*.py agentix` |
| 当前基线 | `env -u PYTHONPATH .venv/Scripts/python.exe -m pytest -m "not integration" -q` → 5242 passed / 40 skipped / 4 deselected / 0 failed |

## 附录 B · 版本线地图

```
                                    retry 形参     cache_policy  durability  节点超时      checkpoint  sdk      redis
0.2.60（现状）                      retry=          ✗            ✗           ✗            2.0.26      0.1.x   5.0.1
0.4.10                              retry=          ✓            ✗           ✗            2.0.26+     0.1.x   5.0.1
0.5.4                               retry_policy    ✓            ✗（有前身）  ✗            2.1.x       0.1.x   5.0.1
0.6.11                              retry_policy    ✓            ✓           ✗            3.0.1       0.2.x   6.x
1.2.18（落点）                       retry_policy    ✓            ✓           ✓ TimeoutPolicy  4.3.1    0.4.10  7.4.1*
```

`*` `redis` 的实际 pin 写 `>=5.2.1,<6.0.0`：pip 之所以给 7.4.1，是因为 saver 只写了下限。

另记两条与本轮无关但容易被误传的事实：
- `durability` 只影响「checkpoint 什么时候落盘」，**不是断点续跑的前提**；默认 `async` 与 0.2.60 行为一致。
- 1.2 线与本地旧 pin 并不冲突——**整族一起升就自洽**（§3.2 已实测），冲突只发生在「只升 langgraph 不升 langchain」这种半升姿势。

## 附录 C · docs 改名全量映射（76 份 + 本文 = 77）

「引用」列是全仓引用次数，用来标出改名前需要重点回归的文件。标「保留」＝不改名。

### C.1 根目录（5 份）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `PROJECT_SPEC.md` | `项目实现手册.md` | **149** |
| `VERSIONING.md` | `版本管理策略.md` | 35 |
| `ENGINEERING_LOG.md` | `工程口径日志.md` | 28 |
| `source-code-study.md` | `源码研读笔记.md` | 25 |
| `LIMITATIONS.md` | `已知限制.md` | 8 |

### C.2 `architecture/`（11 份）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `configuration.md` | `配置管理.md` | **111** |
| `runtime-guardrails.md` | `运行时护栏.md` | 63 |
| `extension.md` | `扩展机制.md` | 35 |
| `layers.md` | `分层架构与依赖规则.md` | 32 |
| `README.md` | 保留 | 26 |
| `interfaces.md` | `接口与契约.md` | 21 |
| `agent-core-layers.md` | `AgentCore五层架构.md` | 13 |
| `audit-2026-09-28.md` | `架构审计-2026-09-28.md` | 11 |
| `ARCHITECTURE_DETAIL.md` | `架构详解.md` | 10 |
| `request-execution-graph.md` | `请求执行图与关键路径.md` | 5 |
| `run-api-idempotency.md` | `接口幂等契约.md` | 4 |

### C.3 `architecture/adr/`（8 份 · 保留编号前缀）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `0001-layered-architecture-with-ports.md` | `0001-分层架构与端口抽象.md` | 2 |
| `0002-registry-based-plugin-mechanism.md` | `0002-注册表驱动的插件机制.md` | 3 |
| `0003-declarative-configuration-single-source.md` | `0003-配置声明单一事实来源.md` | 2 |
| `0004-dual-orm-coexistence.md` | `0004-双ORM长期共存.md` | 0 |
| `0005-dual-retrieval-channels.md` | `0005-双检索通道边界.md` | 2 |
| `0006-infrastructure-criticality.md` | `0006-基础设施关键性与降级.md` | 2 |
| `0007-execution-sandbox-port.md` | `0007-执行沙箱端口.md` | 17 |
| `README.md` | 保留 | 6 |

### C.4 `design/`（14 份 + 本文）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `approval-recovery-optimization.md` | `审批与中断恢复优化规划.md` | 28 |
| `legal_domain_schema.md` | `测试数据域-本体Schema.md` | 24 |
| `orchestration-rationale.md` | `编排方案选型论证.md` | 20 |
| `memory-optimization.md` | `记忆域深度优化.md` | 20 |
| `rag-optimization.md` | `检索增强深度优化.md` | 42 |
| `optimization-roadmap.md` | `性能与优化路线图.md` | 37 |
| `multiagent-optimization.md` | `多智能体深度优化.md` | 10 |
| `frontend-streaming-optimization.md` | `前端流式渲染优化.md` | 6 |
| `phase5_brand_switch.md` | `阶段五-品牌切换验证记录.md` | 5 |
| `design-legal-domain.md` | `测试数据域-架构设计.md` | 4 |
| `wren-text2sql-integration.md` | `Wren语义层集成.md` | 4 |
| `prd-legal-domain.md` | `测试数据域-产品需求.md` | 2 |
| `storage_convergence_r3.md` | `阶段三-存储收敛验证记录.md` | 2 |
| `phase4_frontend_merge.md` | `阶段四-前端合并验证记录.md` | 1 |

### C.5 `guides/`（9 份）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `testing.md` | `测试分层策略.md` | 18 |
| `DATA_PREPARATION.md` | `数据准备指南.md` | 18 |
| `QUICK_START.md` | `快速启动.md` | 12 |
| `research-agent-demo.md` | `研究型Agent演示.md` | 8 |
| `TECH_STACK_GUIDE.md` | `技术栈详解.md` | 8 |
| `interview-10-questions.md` | `面试十问.md` | 6 |
| `lightrag_service_guide.md` | `LightRAG服务使用指南.md` | 3 |
| `agent_routing_quick_reference.md` | `路由速查表.md` | 3 |
| `crawler_guide.md` | `数据批量导入指南.md` | 0 |

### C.6 `migration/`（13 份 · 9 份已是中文）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `current-tree.md` | `现有目录树.md` | 23 |
| `module-inventory.md` | `模块清单.md` | 15 |
| `evolution-assessment.md` | `架构演进评估.md` | 6 |
| `dependency-map.md` | `依赖关系图.md` | 2 |
| `Agent_Core_2.0_规划.md` | 已是中文（可选规范化） | 0 |
| `Phase15-20_深化规划.md` | 已是中文（可选规范化） | 0 |
| `Phase21-26_规划.md` | 已是中文（可选规范化） | 0 |
| `Phase28-31_优化规划.md` | 已是中文（可选规范化） | 0 |
| `Phase32-34_Memory-RAG-MultiAgent优化总纲.md` | 已是中文（可选规范化） | 0 |
| `PhaseH_高性能运行时优化规划.md` | 已是中文（可选规范化） | 0 |
| `PhaseJ_评测活证据与叙事收口规划.md` | 已是中文（可选规范化） | 0 |
| `PhaseK_HITL与恢复链断点收口实施计划.md` | 已是中文（可选规范化） | 0 |
| `架构重构_统一执行链方案_v2.md` | 已是中文（可选规范化） | 0 |

### C.7 `runbooks/`（12 份）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `staging-environment.md` | `预发环境与配置矩阵.md` | **74** |
| `eval-recording.md` | `评测基线录制.md` | 37 |
| `subprocess-sandbox-verification.md` | `子进程沙箱取证.md` | 34 |
| `perf-cost-measure.md` | `性能与成本采数.md` | 34 |
| `release-and-rollback.md` | `发布与回滚.md` | 14 |
| `slo.md` | `SLO目录.md` | 8 |
| `failure-recovery.md` | `失败恢复演练.md` | 5 |
| `oncall.md` | `值班响应手册.md` | 4 |
| `secrets-management.md` | `密钥管理.md` | 4 |
| `postmortems.md` | `故障复盘模板.md` | 4 |
| `fallback-drill.md` | `LLM降级演练.md` | 2 |
| `INGEST_RUNBOOK.md` | `入库操作手册.md` | 2 |

### C.8 其余（4 份）

| 原名 | 中文名 | 引用 |
|---|---|---|
| `archive/overview.md` | `项目全景-归档快照.md` | 6 |
| `archive/README.md` | 保留 | 2 |
| `notebooks/README.md` | 保留 | 6 |
| `security/owasp-llm-self-assessment.md` | `大模型安全风险自评.md` | 7 |

### C.9 命名规则

| 规则 | 说明 | 例 |
|---|---|---|
| 中文优先，技术专名保留原文 | `LangGraph` / `LightRAG` / `Wren` / `AgentCore` / `ADR` / `SLO` / `OWASP` / `RAG` 不译 | `LightRAG服务使用指南.md` |
| 用 `-` 分隔语义块，不用下划线 | 与现有文件名风格一致 | `测试数据域-本体Schema.md` |
| 时间与阶段用数字前缀 | ADR、阶段记录保留可排序前缀 | `0007-执行沙箱端口.md` |
| 名字里不重复目录含义 | `runbooks/发布与回滚.md` 而不是 `runbooks/发布与回滚运行手册.md` | — |
| 4 份 `README.md` 保留 | 目录索引靠这个名字自动渲染 | — |
