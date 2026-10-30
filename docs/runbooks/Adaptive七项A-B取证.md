# Runbook：Adaptive 七项与 token 窗口 A/B 取证（T17）

> 文档状态：**现行**。对应任务卡：`docs/design/审批与中断恢复优化规划.md:585-599`（T17）。
> **本卡不推默认值**：本文只登记「某天、某台机器、某个模型」的实测读数，不改任何 flag 的
> 默认值，也不把读数换算成"默认值该改成什么"的建议——推默认属灰度类，另行裁决
> （卡内边界原文：**不推默认值**；触发纪律 1 验证义务）。
> 数据真源是 `bench/results/adaptive_ab.*.json`（gitignored）；本文只做誊录与归因。
> 架构分层、默认开关清单、测试基线数、目录地图都不在本文复述（纪律 8），需要时看 `README.md`。

## 1. 测量日期与环境

| 项 | 值 |
| --- | --- |
| 测量日期 | 2026-10-28（UTC `11:30:34`–`11:34:00`，本机 19:30–19:34） |
| 平台 | `Windows-11-10.0.26300-SP0`，hostname `Yihao`，32 逻辑核（Intel64 Family 6 Model 183 Stepping 1） |
| 解释器 | CPython 3.12.13（仓库 `.venv`） |
| LLM 端点（回环） | `http://127.0.0.1:1234/v1`（LM Studio） |
| 嵌入端点（回环） | `http://127.0.0.1:1234/v1`，请求模型 `text-embedding-bge-m3` |
| 实际服务的聊天模型 | **`qwen/qwen3-8b`**（判定手段见 §1.1） |
| SDK 指纹 | agentix 0.2.0 / openai 2.54.0 / anthropic 0.18.1 / langchain 1.4.7 / langchain-openai 1.7.3 / langgraph 1.2.18 / fastapi 0.142.2 |
| 分位尺子 | `evals.perf_stats.nearest_rank_quantile`，口径 `nearest-rank sorted[round(q*(n-1))]`；池化入口 `bench.aggregate.pooled_summary` |
| 凭据 | 本轮以 `LLM_API_KEY=local-noauth` 覆盖，商用端点真实 key 不出本机 |

环境闸：`bench/adaptive_ab.py` 在任何测量之前校验 `LLM_BASE_URL` / `EMBEDDING_BASE_URL` 解析到回环，
否则拒绝出数。本轮两处均为 `127.0.0.1`，闸放行（重跑命令见 §2.1，原始读数见 §6）。

### 1.1 必须写进去的坑：端点不校验 model 名

- `/v1/models` 列出 9 个 id：`qwen/qwen3-8b`、`deepseek/deepseek-r1-0528-qwen3-8b`、
  `text-embedding-qwen3-embedding-4b`、`text-embedding-bge-m3`、`text-embedding-nomic-embed-text-v1.5`、
  `bert-base-chinese`、`bert-base-uncased`、`master`、`unimernet_hf_small_2503`。
- `/v1/chat/completions` **不校验请求里的 `model` 字段**：实测把请求写成不存在的 `deepseek-chat`，
  仍正常返回，响应体 `model` 与 `system_fingerprint` 都是 `qwen/qwen3-8b`。
- 因此指纹里的 `llm_model_requested` 是**请求名**，不是**实际服务的模型**。本轮请求名
  `qwen/qwen3-8b` 与实际服务一致（两条判据同源：响应体 `model` + `system_fingerprint`）。
- 该模型是**推理模型**：探活里 completion 219 tokens 中 180 是 `reasoning_tokens`，可见正文几乎全落在
  `reasoning_content`。⇒ 模型侧墙钟主要由推理 token 量决定，同一问题跨批次波动可达数秒（见 §3.2、§5.2）。

## 2. 方法与臂定义

### 2.1 入口与执行纪律

```bash
# 一条臂一个进程（admission / bulkhead / singleflight / load_shedder / retry_budget /
# control_plane 都是进程级单例，event-loop lag 是全局量；同进程连跑两臂 = 污染读数）
LLM_BASE_URL=http://127.0.0.1:1234/v1 LLM_MODEL=qwen/qwen3-8b \
LLM_API_KEY=local-noauth \
EMBEDDING_BASE_URL=http://127.0.0.1:1234/v1 EMBEDDING_MODEL=text-embedding-bge-m3 \
.venv/Scripts/python.exe -m bench.adaptive_ab --arm <arm> --repeats 5 --temperature 0.0
```

本轮九条臂按序各跑一次，`--arm` 依次为：`probe`、`admission`、`retry_budget`、`load_shedding`、
`bulkhead`、`singleflight`、`tier_routing`、`token_window`、`fastpath`。

### 2.2 重复次数与样本数

| 项 | 取值 | 依据 |
| --- | --- | --- |
| 重复轮数 `--repeats` | **5**（每条臂都是 5） | `bench/fingerprint.py::MIN_REPEATS = 5` 是硬下限，低于下限 `argparse` 直接报错、`collect_fingerprint` 抛 `ValueError` |
| 每轮每场景样本 `--samples` | 5（缺省） | 时延类臂用；⇒ admission / bulkhead / singleflight / retry_budget 池 = 5×5 = **25** |
| 每臂实际重复数的可核查位点 | 结果文件 `fingerprint.repeats`；**每轮一条**的 `per_repeat` / `per_repeat_levels` 长度 | 结论：**每臂 `repeats=5`**。核查方法：`fingerprint.repeats==5`，且该臂的 `per_repeat`（`token_window`）或 `per_repeat_levels`（`load_shedding`）长度也是 5 |

`per_sample` **不是**重复轮数：它逐样本一条，长度是池内样本总数，因而各臂不同——
本轮 `admission` 75（3 场景 × 25）、`retry_budget` 75（3 故障 × 25）、`bulkhead` 25、`singleflight` 25、
`fastpath` 10（2 题 × 5 轮）。实测两例：`adaptive_ab.admission.flag_on-20261028T113107Z.json`
的 `per_sample` 长度 75、`fingerprint.repeats` 5；`adaptive_ab.token_window.budget_800_on-20261028T113219Z.json`
的 `per_repeat` 长度 5、`fingerprint.repeats` 5。另有三个可核查位点按臂而异，逐份实测：
`tier_routing` 与 `token_window` 用 `per_repeat`（长度 5），`load_shedding` 用 `per_repeat_levels`（长度 5），
`probe` 既无 `per_sample` 也无 `per_repeat`（不产时延样本，只有能力探针结果）。

### 2.3 题集

| 臂 | 输入 | 说明 |
| --- | --- | --- |
| `fastpath` | 命中面 2 题：`你好`、`刑法第264条规定了什么`；未命中面 2 题：`刑法第264条为什么和第265条区别`、`帮我写一份房屋租赁合同` | 命中/未命中面由规则表决定，脚本内有「命中面与预期不符 ⇒ 读数作废」的前置断言 |
| `probe` | 三问固定探针：结构化输出 `刑法第264条规定了什么`；`tools` 通道同题；多轮 = system + 3 条历史 | 只探能力，不出时延声明 |
| `token_window` | 确定性历史 24 条（human/ai 交替，每条 400 字，无随机、无时间戳） | 24 > `MAX_HISTORY_MESSAGES`(20) ⇒ 条数窗口本身裁 4 条 |
| 其余七项（admission / retry_budget / load_shedding / bulkhead / singleflight / tier_routing） | 无题集 | 决策面 / 纯函数 / 进程内计时；不调用模型 |

### 2.4 flag 注入与复位

所有 flag 取值由脚本按臂注入（`_patched_settings` 上下文管理器，退出即还原），**默认值未被改动**。
各 flag 的默认值与含义见 `.env.example`；下文只写**本轮注入值**与**对照臂取值**。
复位策略逐臂写进了结果文件的 `results.notes.reset_strategy`（进程级单例一律 `reset_*`，
lag 归零，信号量按尺寸重建）。

**七项实际注入的配置键：19 个**（逐项清单见下表）。取数方法：对 §6 点名的 18 份读数取
`results.flag_values` 的键并集（25 个），排除 `ablation_router_enabled`（ablation 开关，不是配置键）
与 4 个 `RetryPolicy` 形状参数（`max_attempts` / `base_delay_s` / `max_delay_s` / `jitter`）后剩
**20** 个；其中 `LG_TOKEN_HISTORY_BUDGET` 属第 8 条臂 `token_window`（token 窗口，不是「七项」之一）
⇒ **七项恰为 19 个**，18 份读数全体并集为 20 个。逐臂键数实测：`admission` 4、`retry_budget` 2
（另 4 个形状参数）、`load_shedding` 1、`bulkhead` 4、`singleflight` 1、`tier_routing` 5、`fastpath` 2
（`flag_on` 另加 `ablation_router_enabled`）、`token_window` 1、`probe` 0。

| 项 | 配置键 | 本轮注入值（臂内） | 定义位置 |
| --- | --- | --- | --- |
| 档位路由（5） | `LLM_TIER_ROUTING_ENABLED` | `false` / `true` | `agentix/config/settings.py` |
| 档位路由（5） | `LLM_ROLE_TIER_MAP` | `router=fast,planner=reasoning` | 同上 |
| 档位路由（5） | `LLM_MODEL_FAST` | `bench-tier-fast`（哨兵名） | 同上 |
| 档位路由（5） | `LLM_MODEL_STANDARD` | `bench-tier-standard`（哨兵名） | 同上 |
| 档位路由（5） | `LLM_MODEL_REASONING` | `bench-tier-reasoning`（哨兵名） | 同上 |
| fast-path（2） | `ROUTING_FASTPATH_ENABLED` | `false` / `true` | 同上 |
| fast-path（2） | `ROUTING_CACHE_ENABLED` | **固定 `false`**（两臂都关，作前置：不许缓存掩盖路由耗时） | 同上 |
| 准入排队（4） | `ADMISSION_CONTROL_ENABLED` | `false` / `true` | 同上 |
| 准入排队（4） | `ADMISSION_MAX_CONCURRENT_RUNS` | `1`；`limit_unset` 臂 **`0`**（= 不限，回落 `CONTROL_PLANE_MAX_CONCURRENCY=0`） | 同上 |
| 准入排队（4） | `ADMISSION_MAX_QUEUE` | `4`（三臂都是 `4`；`limit_unset` 臂并发不限 ⇒ 队列容量不生效） | 同上 |
| 准入排队（4） | `CONTROL_PLANE_MAX_CONCURRENCY` | `0`（不限） | 同上 |
| 重试预算（2） | `RETRY_BUDGET_ENABLED` | `false` / `true` | 同上 |
| 重试预算（2） | `RETRY_BUDGET_TOKENS` | `2` | 同上 |
| 负载甩载（1） | `LOAD_SHEDDING_ENABLED` | `false` / `true` | 同上 |
| 隔离舱（4） | `BULKHEAD_MCP_SIZE` | 同值：`0`（不限）/ `1`（串行） | 同上 |
| 隔离舱（4） | `BULKHEAD_NEO4J_SIZE` | 同上 | 同上 |
| 隔离舱（4） | `BULKHEAD_RETRIEVAL_SIZE` | 同上 | 同上 |
| 隔离舱（4） | `BULKHEAD_WEB_SEARCH_SIZE` | 同上 | 同上 |
| 单飞合流（1） | `RETRIEVAL_SINGLEFLIGHT_ENABLED` | `false` / `true` | **`agentix/retrieval/settings.py:250`**（不在 `agentix/config/settings.py`） |

另：`results.flag_values` 里还记着 4 个 **`RetryPolicy` 形状参数**（`max_attempts=4`、`base_delay_s=0.01`、
`max_delay_s=0.01`、`jitter=false`）与 `fastpath` on 臂的 `ablation_router_enabled`——它们**不是配置键**，
计入会让「七项几个键」这个数失真，故与上面 19 个分开记。
第 8 条臂 `token_window` 的 `LG_TOKEN_HISTORY_BUDGET`（`0` / `800`）同样经 `_patched_settings` 注入，
见 §3.8。

### 2.5 池化口径

跨轮**池化**分位数：把 5 轮的全部原始样本并成一个池后取分位，**不是**各轮分位再平均
（"平均的百分位"对偏态延迟是系统性失真）。空池返回 `None`，不返回 `0.0`。
分位本体委托 `evals.perf_stats.nearest_rank_quantile`（全仓唯一实现）。

## 3. 逐项读数

> 单位：毫秒。表内分位数全部来自 `results.pooled_quantiles_ms`；`count` 是池内样本数。
> 每张表后的一行是**该臂的读数边界**，引用数字时必须连着这句一起引。

### 3.1 档位路由 `tier_routing`

注入：`LLM_ROLE_TIER_MAP=router=fast,planner=reasoning`，`LLM_MODEL_FAST/STANDARD/REASONING=bench-tier-*`
（`bench-tier-*` 是**哨兵名**，不对应任何真实模型——只为让「选错没选错」可判定）。

| 臂 | router | planner | general | 5 轮是否逐点相同 |
| --- | --- | --- | --- | --- |
| `flag_off`（`LLM_TIER_ROUTING_ENABLED=false`） | `qwen/qwen3-8b` | `qwen/qwen3-8b` | `qwen/qwen3-8b` | `repeat_identical=true` |
| `flag_on`（`=true`） | `bench-tier-fast` | `bench-tier-reasoning` | `qwen/qwen3-8b` | `repeat_identical=true` |

边界：本臂**零网络**（只读 `get_llm_client(role=...).model_name`，不 `ainvoke`）⇒ 只证明"角色 → 档位模型名"
的选择生效；不证明时延、token、质量任何一项。`general` 无映射 ⇒ 回退 `LLM_MODEL`（本轮两臂一致，说明回退路径未被 flag 干扰）。

### 3.2 fast-path `fastpath`

| 臂 | 池化 p50 | p90 | p95 | p99 | max | count | 模型构造次数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `flag_on`（`ROUTING_FASTPATH_ENABLED=true`，`ROUTING_CACHE_ENABLED=false`） | **0.675** | 1.272 | 15.548 | 15.548 | 15.548 | 10（命中面 5 轮 × 2 题） | **0**（哨兵未被触发） |
| `flag_off`（`=false` + 缓存关） | **7908.217** | 11659.621 | 11660.231 | 11660.231 | 11660.231 | 10（5 轮 × 2 题，全部成功分类） | 10（真分类器，`with_structured_output`） |

- 命中面路由结果：`你好` → `general-query`；`刑法第264条规定了什么` → `graphrag-query`。未命中面 10/10 触发哨兵
  ⇒ 证明未命中面确实仍走分类器（本臂不对未命中面主张任何零成本）。
- off 臂：`classifier_constructions=10`、`classified_count=10`、`fallback_or_error_count=0`。
- 实测事实（本卡不判对错）：同一问题 `刑法第264条规定了什么`，on 臂规则表给 `graphrag-query`，
  off 臂分类器给 `kb-query`。⇒ flag 打开会改变该题的路由结果，属行为差异，另行裁决。

边界：on 臂的 0.675ms 是**纯函数短路**的代价，不是线上路由延迟；off 臂是**本机 8B 推理模型**的
分类器往返，也不是线上路由延迟（缺网关卡与真实流量）。两臂都不可读成"线上快多少"。
另：off 臂 p50 **跨批不稳定**——本行 7908.217 只在「本批 10/10 成功分类」的前提下成立；
更早两批同题集是 `classified=5 / fallback=5`，p50 只覆盖那 5 条（`count=5`，6634.113 / 6848.258），
是样本选择偏倚后的读数，与本批的 10 条池不同量。**跨批引用这个数就是把两种样本混在一起**，
依据与证据文件见 §5.2。

### 3.3 准入排队 `admission`

注入：`ADMISSION_CONTROL_ENABLED` 开/关；`ADMISSION_MAX_CONCURRENT_RUNS=1`、`ADMISSION_MAX_QUEUE=4`、
`CONTROL_PLANE_MAX_CONCURRENCY=0`；占位者持有 0.10s。三个场景：`under_capacity`、
`at_capacity_queue_enabled`、`at_capacity_queue_disabled`。

| 臂 | 池化 p50 | p90 | p95 | p99 | max | count | 三场景决策（各 25 样本） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `flag_off` | 0.001 | 0.002 | 0.002 | 0.003 | 0.003 | 25 | 全 `no_decision_recorded`（准入完全旁路，零指标增量） |
| `flag_on` | **109.369** | 110.448 | 110.832 | 110.839 | 110.839 | 25 | `admitted` 25 / `queued` 25 / `rejected` 25 |
| `limit_unset`（flag 开、`MAX_CONCURRENT_RUNS=0`） | 0.025 | 0.041 | 0.056 | 0.060 | 0.060 | 25 | 三场景全 `admitted` |

`flag_on` 分场景：

| 场景 | 决策 | p50 | p95 | 备注 |
| --- | --- | --- | --- | --- |
| `under_capacity` | `admitted` 25 | 0.021 | 0.039 | 有 1 个 150.033ms 离群（p99 即它），未剔除 |
| `at_capacity_queue_enabled` | `queued` 25 | 109.369 | 110.832 | ≈ 占位者 100ms 持有 + 9ms 开销 |
| `at_capacity_queue_disabled` | `rejected` 25 | 0.179 | 0.398 | 拒绝路径最便宜 |

边界：单进程、单等位者、占位者固定持有 0.10s。排队代价读的是"等占位者释放"，
不含真实任务时长分布；`limit_unset` 只说明"开 flag 不设上限"时不产生排队。

### 3.4 重试预算 `retry_budget`

注入：`RETRY_BUDGET_ENABLED` 开/关；`RETRY_BUDGET_TOKENS=2`；`max_attempts=4`；
退避 `base=max=0.01s`、`jitter=false`。注入三类故障：`transient_connection`、`transient_timeout`、
`non_transient_401`。

| 臂 | 故障 | 尝试次数 | 是否截断 | 预算首个读数 | p50 | p95 | count |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `flag_off` | `transient_connection` | 4 | `truncated=true` | `null` | 46.727 | 47.902 | 25 |
| `flag_off` | `transient_timeout` | 4 | `truncated=true` | `null` | 46.771 | 47.818 | 25 |
| `flag_off` | `non_transient_401` | 1 | `false` | `null` | 0.028 | 0.035 | 25 |
| `flag_on` | `transient_connection` | **3** | `truncated=true` | `{consumed:2, remaining:0, layer_task:2}` | 30.650 | 32.878 | 25 |
| `flag_on` | `transient_timeout` | **3** | `truncated=true` | 同上 | 30.977 | 32.493 | 25 |
| `flag_on` | `non_transient_401` | 1 | `false` | `null` | 0.023 | 0.044 | 25 |

边界：退避被压到 0.01s（等比缩小模型），所以 46.7 → 30.7ms（约 −34%）只说明"少一次尝试的墙钟差"，
真实网络退避下的绝对值不由此推出。非瞬态 401 两臂都是 1 次尝试（不进预算）。

### 3.5 负载甩载 `load_shedding`

注入：`LOAD_SHEDDING_ENABLED` 开/关。两条注入轴：`record_event_loop_lag()`（与线上同一入口）、
受损下游计数（`load_shedder._impaired_downstreams`，与钉住测试同一注入点）。

lag 阶梯（`current_level()`，两臂各跑满 5 轮，`repeat_deterministic=true`）：

| 注入 lag(s) | 0.0 | 0.05 | 0.1 | 0.24 | 0.25 | 0.49 | 0.5 | 0.99 | 1.0 | 1.99 | 2.0 | 5.0 | 10.0 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `flag_off` | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 | L0 |
| `flag_on` | L0 | L0 | L1 | L1 | L2 | L2 | L3 | L3 | L4 | L4 | L5 | L6 | L6 |

- `should_shed` 决策面（数据真源 `results.lag_sweep`，13 个 lag 采样点逐格取自该段）。
  `flag_on` 的实测阶梯是**按级别分档加人**，不是按任务类型一条道走到黑：
  **L1 起**甩 `evolution_sleep`；**L3 起**加 `memory_consolidation`；**L4 起**加 `conversation_summary`；
  **L5 起**加 `file_import`（即 L5 后四个已登记后台类型全甩）。**L0（lag 0.0 / 0.05s）一格不甩**，
  **L6（lag 5.0s / 10.0s 两格）也是全 `false`**——L6 只拒绝、不甩，见下一条。未登记类型
  `bench_unknown_type` 在 13 格上**始终不甩**（不在任何类型的甩脱集合里）。
  `flag_off` 全程 `false`：13 格 × 5 类型 × 5 轮逐格核过（`bench/results/` 下 5 份 `flag_off` 读数的
  `results.per_repeat_levels` 里没有任何一格为 `true`），且 `repeat_deterministic=true`
  ⇒ 上面这张阶梯 5 轮逐格相同。
- 受损下游轴（**单次探针，n=1，不进 5 轮池**）：`flag_on` 下 0 → L0、1 → L2、2 → L4、3 → L4；`flag_off` 恒 L0。
- 活体队列（**单次探针，n=1，不进 5 轮池**）：真起一个等位者（`queue_depth=1`、`queue_capacity=8`）后读级别：
  `flag_off` L0、`flag_on` **L1**。
- L6 准入探针（**单次探针，n=1，不进 5 轮池**）：L6 的"礼貌拒绝"落在准入侧。注入 lag 5s 后调 `admission_wait`：
  `flag_off` `admission_denied=false`（无理由）；`flag_on` `admission_denied=true`，
  理由 `admission denied: load shed L6 (bench_l6)`。
  L6 下 `should_shed` 恒 `false`（与上面阶梯里 lag 5.0s / 10.0s 两格全 `false` 是同一事实），
  所以这一格只能用真准入闸观测，不能用 `should_shed` 代替。

边界：本臂是**决策面读数，不含任何时延主张**。阈值是实现的内部常量，本文不复述其数值来源与取值理由
（改动阈值属行为变更，另有卡）。

### 3.6 隔离舱 `bulkhead`

注入：四个 `BULKHEAD_*_SIZE` 同值（0 = 不限 / 1 = 串行）；并发 8 路，每调用 0.05s。

| 臂 | p50 | p90 | p95 | p99 | max | count | 完成数 | 超时后池位归还 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `size_0_unlimited`（0，对照） | **62.163** | 62.968 | 63.473 | 63.550 | 63.550 | 25 | 8/8 | `None`（本臂不测） |
| `size_1_serialized`（1，注入） | **507.708** | 509.801 | 510.728 | 511.589 | 511.589 | 25 | 8/8 | `True`（0.01s 超时后池位确实归还，后续调用仍拿到结果） |

- 期望 vs 实测：8 路 × 0.05s 串行 ≈ 400ms，实测 p50 507.7ms（约 8.2×），多出约 107ms 是信号量排队与调度开销。
- 工具 → 池映射（`pool_for_tool`）：`search` → `web_search`、`cypher_query` / `predefined_cypher` → `neo4j`、
  `not_a_tool` → `null`（未映射工具不占池）。

边界：只有 `pooled_wait_for` **原语级**读数（同一 semaphore 语义）；Neo4j / MCP stdio / 外搜三条下游的
线上延迟**本机不可测**（见 §5.1），不得读成那三个下游的延迟。

### 3.7 单飞合流 `singleflight`

注入：`RETRIEVAL_SINGLEFLIGHT_ENABLED` 开/关；8 路同参并发；后端 `faiss`，索引
`data/demo/indexes/legal-demo`。

| 臂 | p50 | p90 | p95 | p99 | max | count | 下游检索次数 | 嵌入调用次数 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `flag_off` | **5.204** | 7.323 | 13.324 | 66.595 | 66.595 | 25 | **8** | **8** |
| `flag_on` | **2.345** | 3.505 | 3.567 | 3.706 | 3.706 | 25 | **1** | **1** |

- 两臂 `all_callers_same_doc_count=true`：8 路拿到同一文档集（前置断言成立，合流没有改变结果）。
- `flag_off` 尾部明显更重（p99 66.6ms vs 3.7ms）：8 次独立嵌入往返的抖动。
- 嵌入口径：`embedding_calls` 是嵌入函数被调次数（本臂走本机嵌入端点）。

边界：本机 faiss demo 索引 + 本机嵌入端点。"8 → 1"是**调用次数**事实；真实后端（远端向量库 / 远端嵌入）
的收益幅度不由此推出——那里单次往返更贵，方向不变但幅度不同，本卡不量化。

### 3.8 token 窗口 `token_window`

注入：`LG_TOKEN_HISTORY_BUDGET=0`（对照）/ `=800`（臂内 `_patched_settings` 注入，退出即还原；
生产接线同源：`agentix/orchestration/langgraph/lg_builder.py:481-484`）（2026-10-30 实测更正：该接线块现为同文件 `agentix/orchestration/langgraph/lg_builder.py`:428-435，`481-484` 系登记时点的旧行号快照；取数 `grep -n "^    builder.add_node(" agentix/orchestration/langgraph/lg_builder.py`）。窗口节点拿到的两个实参
（`MAX_HISTORY_MESSAGES`、`LG_TOKEN_HISTORY_BUDGET`）**都从 settings 读**，脚本不往里传字面量预算；
`results.flag_values` 记的是 settings 实际被读到的值。历史 24 条、每条 400 字；
`count_tokens` 默认口径是 `len(text)//4`（非真分词器）。

| 臂 | 裁掉条数 | 剩余条数 | 剩余 token（len//4 单位） | 全历史（同单位） | 模型 `usage.prompt_tokens` | 模型调用成功 |
| --- | --- | --- | --- | --- | --- | --- |
| `budget_0_off` | 4 | 20 | 2020 | 2424 | **5072**（5 轮同值） | 5/5 |
| `budget_800_on` | **17** | **7** | **707** | 2424 | **1788**（5 轮同值） | 5/5 |

- 两个数**不可互换**：窗口内是 `len//4` 单位，模型报的是自家分词器口径的 prompt_tokens
  （含 system 消息与模板），故分栏列，不合并、不换算。
- 800 的取值理由：落在"条数窗口（20 条 ≈ 2000 单位）之下、单条之上"，于是两个窗口的作用不会互相掩盖
  ——本轮实测 800 比条数窗口更紧（裁 17 条 vs 裁 4 条），归因可分离。

边界：`window` 段是**纯函数路径**（真节点 + 真 `add_messages` reducer，但跳过图编排与真实会话历史），
端到端只由 model 子臂的 `prompt_tokens` 承担。本臂测的是"窗口裁切是否生效"，**不是**"答案质量是否不受影响"。

### 3.9 探活 `probe`（能力定点，非性能读数）

| 探针 | 结果 | 耗时 |
| --- | --- | --- |
| `/v1/models` | ok，9 个 id（见 §1.1） | — |
| `tools` 通道能否产 `tool_calls` | **supported=true**，`finish_reason=tool_calls`，返回 `route` 工具调用 | 5376.9ms |
| 多轮（system + 3 条历史） | **supported=true**，`finish_reason=stop` | 5111.0ms |
| 真实 router 节点 `with_structured_output` | **supported=true**，`router_type=kb-query`，`fell_back=false` | 12834.2ms |

### 3.9.1 旧探活为何判成"不支持"（能力口径以本节为准）

**现象**：Phase A 旧探活两次「带 `tools` 无 `tool_calls`」，记于规划文档 §8 台账 T17 行。`bench/results/`
下的 probe 读数**只有三份，且都是同一天由本脚本产出的**：`adaptive_ab.probe.local_endpoint-20261028T111317Z`
/ `...T112245Z`（更早批次，未被覆盖，§6 已说明旧批次保留未删）/ `...T113034Z`（本轮）。
**Phase A 的探活输出没有落盘**：Phase A 是**只读盘点**（台账 T17 行原文「零文件变更，`git status
--porcelain` 前后同为 38 行」），`bench/adaptive_ab.py` 是 B1 才建的 ⇒ 当时那次探活没有结果文件、
也没有本脚本可复现的请求体，`bench/results/` 里找不到它的产物 ⇒ 旧 `finish_reason` 无原文可查、
本文不补数字。
**原因（推断，不是事实）**：以下按本轮读数推断，**没有旧口径的原文可比**。`qwen/qwen3-8b` 是推理模型
（§1.1），`reasoning_tokens` 先占预算；本轮 probe 臂给了显式预算 `max_tokens=512`
（`bench/adaptive_ab.py::_probe_tool_calls`），而**旧口径没有给 `max_tokens` 留参数**（旧调用里没有这个入参，
也没有留下请求体原文）。本轮 tools 探针 `completion_tokens=219` 中 `reasoning_tokens=180`（§5.3 原文）
⇒ 推理段占绝大部分，若预算更紧，推理段先耗尽、`finish_reason` 落到 `length`，自然轮不到 `tool_calls`。
这条链条**只能按本轮 `reasoning_tokens` 占比推断**，本卡没有旧请求体/旧响应体可对。
**结论**：`tools` 能力以 §3.9 本轮读数（`supported=true`、`finish_reason=tool_calls`）为准，Phase A 的
「不支持」结论作废；本节判的是**能力**，不是时延或质量声明。

这三项能力探针（tools / 多轮 / 结构化输出）决定"模型依赖臂可测与否"：本轮三项全 true ⇒ fastpath off 臂与 token 窗口 model 子臂都测得出来。
本臂不产出任何时延声明（`repeats` 只用于指纹合规）。

## 4. 结论（不外推）

1. **准入排队**：flag 开时"到顶"变成可观测三态（admitted / queued / rejected）；排队代价 ≈ 占位者持有时间
   （109.4ms ≈ 100ms 持有 + 9ms 开销），拒绝最便宜（0.18ms）。只开 flag 不设上限时不产生排队。
   ——单进程、单等位者、固定 0.10s 持有，不外推到多副本或真实任务时长分布。
2. **重试预算**：2 个 token 的预算把"最多 4 次尝试"压到 3 次，瞬态路径墙钟 46.7 → 30.7ms；非瞬态 401
   两次都一样（attempts 1）。——退避被压到 0.01s 的等比模型，绝对值不外推。
3. **负载甩载**：lag 与受损下游两条注入轴都给出确定性阶梯（5 轮逐点相同）；L6 的拒绝落在准入侧
   而不是 `should_shed`。——决策面读数，无时延主张。
4. **隔离舱**：size=1 把 8 并发串行化，p50 62.2 → 507.7ms（≈8.2×），与"8 × 0.05s"同量级；超时后池位归还。
   ——原语级读数，不是三条下游的线上延迟。
5. **单飞合流**：8 路同参并发从 8 次下游检索 + 8 次嵌入降到 1 + 1，p50 5.2 → 2.3ms，尾部 66.6 → 3.7ms，
   且 8 路拿到同一文档集。——本机 faiss + 本机嵌入端点，别的后端幅度不外推。
6. **档位路由**："角色 → 档位模型名"的选择生效（router/planner 换名、无映射角色回退 `LLM_MODEL`）；
   5 轮逐点相同。——零网络，不证明时延 / token / 质量。
7. **fast-path**：命中面零模型构造（p50 0.675ms）vs off 臂分类器往返（p50 7908ms）；命中面确实短路，
   未命中面确实仍走分类器。——两个端点值都不是线上路由延迟。
8. **token 窗口**：800 预算把 20 条裁到 7 条（2020 → 707 个 `len//4` 单位），模型侧 prompt_tokens
   5072 → 1788（约 −65%）。——证明"裁切生效且比条数窗口更紧"，不证明答案质量不受影响。
9. **七项 + token 窗口的默认值全程未改**：所有 flag 取值由脚本在臂内注入并还原；flag-off 行为由
   `tests/test_p94_llm_optimization.py`、`tests/test_p95_retrieval_cascade.py`、
   `tests/test_p97_runtime_control.py`、`tests/test_p102_lg_context_engineering.py` 钉住，本轮这四份全绿
   （读数见 §7）。
10. **不外推**：以上全部是「2026-10-28 / Windows 工作站 / 单进程 / 本机 8B 推理模型 + 本机嵌入端点」的读数。
    换机器、换模型、多副本、真实流量都没有数据支持；本卡也不给"默认值应改成什么"的结论（卡内边界）。

## 5. 本机测不到的东西

### 5.1 环境不可测（缺环境，不是待办）

| 项 | 为什么本机测不到 |
| --- | --- |
| `bulkhead` 的端到端（Neo4j cypher / MCP stdio / 外搜） | 本机没有这三个下游服务 ⇒ 只有 `pooled_wait_for` 原语级读数。要测那三条得先有那三个下游（属别的卡的环境准备） |
| 线上 fast-path 端到端路由延迟 | 本机没有网关卡与真实流量 ⇒ 只有"纯函数短路"与"本机推理模型分类器往返"两个端点值，中间那段线上开销测不到 |
| 多副本 / 跨进程维度（admission 的 `CONTROL_PLANE_MAX_CONCURRENCY`、bulkhead 信号量、singleflight 合流表） | 这三个都是**进程内**对象（`agentix/runtime/control_plane.py` 文档字符串写明"本进程内"）⇒ 单进程工作站测不到跨副本的准入 / 隔离舱 / 合流行为 |

本轮 18 份读数的 `measurement_status` **全部为 `completed`，0 份 `skipped_no_environment`**
⇒ 没有整臂登记为"本机不可测"的条目。上表三行是**读数边界**，不是"跑失败了"。

### 5.2 读数边界（配置口径造成，不是环境缺失）

- **token 窗口的 800 是 `count_tokens` 单位**：默认 `CONTEXT_REAL_TOKEN_COUNT_ENABLED=false` ⇒
  `len(text)//4`。真分词器口径下"800 token 会剩几条"本机未测（该 flag 本轮未开）。
- **fastpath off 臂的读数有效性依赖"当轮全部成功分类"**：router 角色超时是 10.0s
  （`agentix/application/agents/llm_factory.py` 的 `DEFAULT_PROFILES["router"]`）。本轮 10/10 成功，
  但 `刑法第264条规定了什么` 单次墙钟 11.4–11.7s **已超过该超时却仍被成功分类** ⇒ 该超时不是这条路径的
  硬墙钟上界，机制本卡未归因。上一批同题集有 **5/10** 落到兜底
  （`adaptive_ab.fastpath.flag_off-20261028T112001Z.json`、`...T112602Z.json`，
  `fallback_or_error_count=5`），兜底分支是 `with_structured_output` 抛异常后的默认 `kb-query`
  （`agentix/application/agents/router.py:486-490`），本臂不采集异常原因。
  ⇒ 该臂命中率对机器负载敏感，本节 p50 只在"本轮 10/10 成功分类"的前提下成立。

### 5.3 支撑"可测性判定"的探活原始输出

```json
{
 "models_endpoint": {"ok": true, "served_model_ids": ["qwen/qwen3-8b", "bert-base-chinese",
   "bert-base-uncased", "master", "unimernet_hf_small_2503",
   "deepseek/deepseek-r1-0528-qwen3-8b", "text-embedding-qwen3-embedding-4b",
   "text-embedding-bge-m3", "text-embedding-nomic-embed-text-v1.5"], "error": null},
 "tool_calls": {"supported": true, "finish_reason": "tool_calls",
   "tool_calls": [{"type": "function", "id": "dXHdYaagZUUBjugYGo9nJNzS76MBOiQh",
     "function": {"name": "route",
       "arguments": "{\"type\":\"kb-query\",\"logic\":\"user_inquired_about_specific_article_of_criminal_code\"}"}}],
   "usage": {"prompt_tokens": 185, "completion_tokens": 219, "total_tokens": 404,
             "completion_tokens_details": {"reasoning_tokens": 180}},
   "elapsed_ms": 5376.9, "evidence": "POST /v1/chat/completions with tools(tool_choice=default auto)"},
 "multi_turn": {"supported": true, "finish_reason": "stop",
   "usage": {"prompt_tokens": 55, "completion_tokens": 212, "total_tokens": 267,
             "completion_tokens_details": {"reasoning_tokens": 202}}, "elapsed_ms": 5111.0},
 "structured_output_via_router_node": {"supported": true, "router_type": "kb-query",
   "logic": "kb-query", "fell_back": false, "elapsed_ms": 12834.2}
}
```

（原文见 `bench/results/adaptive_ab.probe.local_endpoint-20261028T113034Z.json` 的 `results.probe`；
`multi_turn.turn2_content_head` 在 GBK 控制台会乱码，文件里是正常 UTF-8，本文略去该字段。）

## 6. 原始读数（点名文件）

目录 `bench/results/`（gitignored）。本轮 18 份，全部以 `20261028T113`–`T1134` 结尾的批次：

| 臂 | 文件 |
| --- | --- |
| `probe` | `adaptive_ab.probe.local_endpoint-20261028T113034Z.json` |
| `admission` | `adaptive_ab.admission.flag_off-20261028T113107Z.json`、`adaptive_ab.admission.flag_on-20261028T113107Z.json`、`adaptive_ab.admission.limit_unset-20261028T113107Z.json` |
| `retry_budget` | `adaptive_ab.retry_budget.flag_off-20261028T113112Z.json`、`adaptive_ab.retry_budget.flag_on-20261028T113112Z.json` |
| `load_shedding` | `adaptive_ab.load_shedding.flag_off-20261028T113113Z.json`、`adaptive_ab.load_shedding.flag_on-20261028T113113Z.json` |
| `bulkhead` | `adaptive_ab.bulkhead.size_0_unlimited-20261028T113130Z.json`、`adaptive_ab.bulkhead.size_1_serialized-20261028T113130Z.json` |
| `singleflight` | `adaptive_ab.singleflight.flag_off-20261028T113133Z.json`、`adaptive_ab.singleflight.flag_on-20261028T113133Z.json` |
| `tier_routing` | `adaptive_ab.tier_routing.flag_off-20261028T113136Z.json`、`adaptive_ab.tier_routing.flag_on-20261028T113136Z.json` |
| `token_window` | `adaptive_ab.token_window.budget_0_off-20261028T113219Z.json`、`adaptive_ab.token_window.budget_800_on-20261028T113219Z.json` |
| `fastpath` | `adaptive_ab.fastpath.flag_on-20261028T113400Z.json`、`adaptive_ab.fastpath.flag_off-20261028T113400Z.json` |

同目录文件总量与批次分布（实测，命令与读数如下）：

```bash
ls bench/results/adaptive_ab.*.json | wc -l      # 67

# 按文件名切 arm.state 与时间戳，数出本轮/更早批次与去重后的组合数
.venv/Scripts/python.exe - <<'PY'
import glob, os
files = sorted(glob.glob("bench/results/adaptive_ab.*.json"))
def parts(b):
    body, ts = b[len("adaptive_ab."):-len(".json")].rsplit("-", 1)
    arm, state = body.split(".", 1)
    return arm, state, ts
rows = [parts(os.path.basename(f)) for f in files]
batch = [r for r in rows if r[2] >= "20261028T113" ]       # 本轮（含 T1134xx）
earlier = [r for r in rows if r not in batch]
print(len(files), len(batch), len(earlier),
      len({r[2] for r in earlier}), len({(r[0], r[1]) for r in earlier}))
PY
# 67 18 49 25 18
```

| 统计口径 | 实测值 |
| --- | --- |
| `bench/results/` 下 `adaptive_ab.*.json` 总数 | **67** |
| 本轮（§6 表内，`20261028T113034Z`–`T113400Z`） | **18** |
| 更早批次文件数（`20261028T111317Z`–`T112635Z`） | **49** |
| 更早批次的时间戳（= 批次）数 | **25** |
| 更早批次覆盖的 (arm, state) 组合数 | **18**（与本轮同一批组合：九条臂共 18 个 (arm, state) 组合，本轮每个组合各 1 份，更早批次是同一组组合反复重跑） |

「组」在本文的定义：**(arm, state) 组合**（`adaptive_ab.<arm>.<state>-<timestamp>Z.json` 里的前两段），
不是文件数也不是批次（时间戳）数——同一个组合在不同时间戳下会存在多份。上面「18」是本轮与更早批次
**都有的**组合数（全目录去重后同样是 18；本轮 18 份恰好覆盖这 18 个组合各一份）。

更早批次全部保留未删：本文每个数字都取自上面 18 份，只在 §5.2 引用了其中两份作为"兜底率跨批次不稳定"
的证据（`adaptive_ab.fastpath.flag_off-20261028T112001Z.json`、`...T112602Z.json`），
§3.9.1 另引了两份更早的 probe 读数。指纹字段（`measured_at` / `repeats` / `sdk_versions` /
`quantile_source`）逐份写在文件顶层 `fingerprint` 里，可机器读。

## 7. 复验命令与本轮读数

```bash
# ① flag-off 钉住测试（默认值未动）
.venv/Scripts/python.exe -m pytest tests/test_p94_llm_optimization.py \
  tests/test_p95_retrieval_cascade.py tests/test_p97_runtime_control.py \
  tests/test_p102_lg_context_engineering.py -q
# 本轮：150 条用例通过，1 warning，10.03s（全量基线真源是 README §11，本文不复述）

# ② lint（CI 同一把尺子，含 bench）
.venv/Scripts/ruff.exe check agentix tests bench
# 本轮：All checks passed!
```

重跑某条臂见 §2.1 的命令；`--repeats` 低于 5 会被直接拒绝（`bench/fingerprint.py::MIN_REPEATS`）。

**③ token 窗口臂的接线复跑（不改动 §3.8 的读数来源）**：把 `LG_TOKEN_HISTORY_BUDGET` 的注入方式
改成「臂内 `_patched_settings` 注入、节点实参从 settings 读」之后，用 `--out-dir bench/results/t17r2`
另跑一份（**不覆盖 §6 点名的 18 份**）：

```bash
.venv/Scripts/python.exe -m bench.adaptive_ab --arm token_window --repeats 5 \
  --temperature 0.0 --out-dir bench/results/t17r2
```

两臂逐字段与本批一致：`budget_0_off` 裁 4 / 剩 20 / 2020 单位 / `prompt_tokens` 5072（5 轮同值）；
`budget_800_on` 裁 17 / 剩 7 / 707 单位 / `prompt_tokens` 1788（5 轮同值）；
`status` 两臂 `completed`、`flag_values` 分别为 `{LG_TOKEN_HISTORY_BUDGET: 0}` 与 `{…: 800}`。
⇒ 接线方式改变没有改变读数。§3.8 表内数字仍取自
`adaptive_ab.token_window.*-20261028T113219Z.json` 两份。
