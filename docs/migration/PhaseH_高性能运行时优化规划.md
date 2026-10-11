# Phase H — 高性能运行时优化规划（执行版）

> **项目**：My-Agentix / Agentix
> **目标定位升级**：Production-oriented Agent Platform → High-Performance Production Agent Runtime
> **依据源**：《My Agentix 下一阶段高性能与深度优化规划.md》（134 节 / H0–H7，2026-10）+ 四路代码库 ground-truth 勘察
> **规划日期**：2026-10-05
> **开工基线**：全量 3908 passed / 15 skipped / 0 failed（2026-10-04 实测，真源 README §11）
> **版本规划**：0.1.3 → **0.2.0**（VERSIONING 判定表「新增功能 / 新配置项 → MINOR」；0 阶段 MINOR，CHANGELOG 显著标注「全部新能力默认关、flag-off 零行为变化」）

---

## 0. 本文档的地位与引用纪律

- 本文档是 Phase H **唯一的编码指导真源**：每个波次开工前读本节任务清单，完成后回写 §9 勾选区与实测数据。
- **p41 引用纪律**：`tests/test_p41_doc_consistency.py` 会校验本文档中反引号路径的真实存在性。**拟建文件在落地前一律以普通文本（不加反引号）书写**；文件落地的同一波次内回写本文档补上反引号。
- **计数纪律**：本文档属 docs/migration，测试计数真源仍是 README §11；本文档引用计数时注明口径日期。

---

## 1. Ground Truth 十项发现（规划依据，勘实于 2026-10-05）

| # | 事实 | 对规划的影响 |
|---|---|---|
| 1 | agentix/observability/metrics.py 中 record_request 与 HTTP 延迟直方图**存在但从未被中间件调用** | W0 第一件事：接线即得端到端延迟 |
| 2 | 全仓**零 TTFT、零百分位、零 SLO**基建（源文档称「已存在 SLO」不实） | W0 新建 agent_ttft_seconds，挂钩点 = RunEventEmitter 首个内容帧 |
| 3 | 检索四阶段计时已存在（stage report + record_retrieval + span） | 延迟拆解复用既有脊柱，不另起炉灶 |
| 4 | 无 singleflight / adaptive retrieval / cascade；检索缓存键**缺 tenant_id 与调参指纹**（已知缺口） | W2 核心新建项 + 一处正确性修复（无 flag） |
| 5 | Model Gateway 已有 fast/standard/reasoning 三档与 resolve_model，角色→档位**未接线** | W1 只接线，不重建网关 |
| 6 | PromptEnvelope 静态前缀纪律已存在，prompt 无时间戳/随机 ID | W1 前缀缓存 = 守卫测试 + 指纹指标 |
| 7 | LayeredVerifier 已是零 LLM 级联；KG LLM 校验层有 flag（默认关）但**无预算约束** | W1 补 verification budget |
| 8 | SSE 零合并零背压；前端零批处理（每 token 同步改响应式状态 + 无条件吸底滚动） | W4 后端 SSE + W6 前端 |
| 9 | flag 治理 = 五处同步 + 本地 .env 约束；新 flag 必须默认关、flag-off 与基线等价 | 每波机械成本，见 §5 |
| 10 | 无性能基准；scripts/load_test.py + perf-baseline.yml 夜跑仅测 liveness；**CI 前端 job 调用不存在的 npm run lint** | W0 建基准；W7 交付卫生修复 |

---

## 2. 范围界定

### 2.1 纳入（源文档 P0/P1 + 仓库已规划 E1–E8 + 前端）

H0 性能真值、H1 LLM 降本、H2 检索优化、H3 运行时治理、H4 持久化/SSE、H5 多智能体通信压缩、**W6 前端感知性能与实时动态 UI（用户强调项）**、H7 基准门禁与全文档同步。

### 2.2 明确排除（附理由，防止执行期发散）

| 排除项 | 理由 |
|---|---|
| H6 自部署推理（vLLM / SGLang / TensorRT-LLM） | 无 GPU 与自部署基础设施；仅在规划文档保留方向说明 |
| Rust / Cython 加速 | 源文档自评 P3 收益低难度极高；Python 侧优化未穷尽前不做 |
| Redis 实例拆分 / client-side caching | 运维复杂度高；先完成 pipeline 与批写 |
| GC tuning、对象池 | 仅 profiling 证明是瓶颈后做（源文档 §105 同立场） |
| PostgreSQL COPY 批写 | 轨迹主存储为 jsonl/memory，PG 写入路径非热区 |
| Python 3.12 生产切换 | 双环境保留兼容；CI 矩阵扩展留待后续 |
| 前端跨标签页消息级直播 | 需服务端推送通道，无基建；BroadcastChannel 现状（列表级）已够 |
| 前端消息虚拟列表 | >200 中段折叠已覆盖；先测渲染瓶颈再决定 |
| 重构 legacy ChatWidget | `/` 路由旧件，转义优先已安全；不投入 |

---

## 3. 波次总览（严格串行，每波全绿 → 提交 → 推送 → 下一波）

```text
W0 性能真值 (p92/p93)   → W1 LLM优化 (p94) → W2 检索优化 (p95/p96)
        ↓
W3 运行时治理 (p97)     → W4 持久化+SSE后端 (p98) → W5 多智能体 (p99)
        ↓
W6 前端感知性能与实时动态 UI（web/，vitest 增量）
        ↓
W7 观测收口 + 全文档同步 + 版本 0.2.0 + 交付全量测试
```

---

## 4. 各波任务清单（文件级）

### W0 · H0 性能真值（测试编号 p92 / p93）

**交付物**

1. HTTP 延迟接线：在 agentix/main.py 既有中间件链中接 record_request（激活 http_request_duration_seconds；现状 = 指标存在但零调用），endpoint 归一化（数字/UUID/长十六进制 → {id}，段数封顶控基数）。
2. TTFT：`agentix/observability/metrics.py` 新增 agent_ttft_seconds 直方图；`agentix/application/services/run_event_stream.py` 的 RunEventEmitter 自记 t0，首个内容帧（answer_chunk / content / message）触发 observe；终帧 attach 时**增量注入** duration_ms / ttft_ms 字段（纯增量字段，适配 `tests/test_p65_unified_sse.py` 词汇守卫）。
3. 延迟拆解：新建 `agentix/performance/` 包 —— `agentix/performance/latency.py`（LatencyBreakdown：phase() 上下文管理器 + contextvar + snapshot）、`agentix/performance/budget.py`（LATENCY_TOTAL_BUDGET_MS=0 关，超限仅告警 + record_degraded，不中断响应）。
4. LLM 账本：agent_llm_call_total{role} / agent_llm_tokens_total —— 挂接 `agentix/application/agents/llm_factory.py` 既有 _TrackedChatOpenAI 单点（role 有界枚举，遵守指标基数纪律）+ 检索域 `agentix/retrieval/usecases/chat_llm.py` record_llm_usage 补 role=retrieval。
5. 基准三件套（已落地）：
   - `tests/test_perf_smoke.py` —— 进程内、假件、**宽松软断言**（防抖动，PERF_SMOKE_* 环境变量可放宽），同时断言 TTFT/拆解/中间件埋点存在性（硬断言不放宽）；
   - `evals/performance/` —— `evals/performance/queries.jsonl`（九类离线负载：liveness/metrics/docs/openapi/root/api 六个免依赖 HTTP 端点 + 流×2 + 发射器 CPU 地板）、`evals/performance/fakes.py`（进程内假件，复用 p65 流夹具范式）、`evals/performance/runner.py`（TestClient + 假件，产出 p50/p90/p95/p99/max/mean/qps + 门禁）、`evals/performance/perf_gate.py`（对 `evals/performance/baseline.json` 的 p50×3 软比对，首版非阻塞）、`evals/performance/baseline.json`（首录入库）；
   - CI：perf-smoke job 跑 `tests/test_perf_smoke.py` + runner + gate，continue-on-error: true（首版非阻塞）。
6. event_loop_lag_seconds 采样指标（EVENT_LOOP_LAG_METRIC_ENABLED，默认关）。

**测试**：p92（延迟/TTFT/预算/账本指标断言，`tests/test_p92_perf_observability.py`）、p93（基准 schema、gate 判定语义、queries 分层，`tests/test_p93_perf_benchmark.py`）。

**验收**：全绿 + /metrics 可见新指标 + baseline.json 产出。

### W1 · H1 LLM 优化（p94）

1. 复杂度感知 fast-path：agentix/application/agents/router.py 前置确定性规则分类（寒暄/单条法条直取等高置信 → 跳过 LLM 分类器），ROUTING_FASTPATH_ENABLED=false；命中记 llm_calls_saved_total；`_boom` 哨兵测试钉死零 LLM 调用。
2. 模型档位接线：llm_factory.get_llm_client(role) 按 LLM_ROLE_TIER_MAP（默认空=全回退 LLM_MODEL，等价基线）经既有 gateway.resolve_model 取模，LLM_TIER_ROUTING_ENABLED=false。
3. 验证预算：KG final_answer_guard 增 VERIFICATION_MAX_LLM_CALLS / VERIFICATION_TIMEOUT_MS / VERIFICATION_TOKEN_BUDGET（超预算 fail-open 降级，与既有降级语义一致）。
4. 工具输出预算：context 装配段级配额 TOOL_OUTPUT_BUDGET_TOKENS=0 关（执行收口 = ContextPort.build 轨迹段（工具/检索观测），ContextEngine 与 SimpleContextBuilder 同语义；history 条数窗口 / memory 索引预算 / retrieval 域 CONTEXT_BUDGET_TOKENS 已各司其职，不重复造四分区），超限走既有 obs:// ArtifactStore 外置而非截断。
5. 前缀稳定性：守卫测试钉死 PromptEnvelope 静态段字节稳定（两次构建逐字节相等）+ prompt_prefix_fingerprint 指标。

**测试**：p94（fast-path 分支/零调用哨兵、tier 解析与回退、预算降级、段配额 flag-off 等价、前缀稳定性）。

### W2 · H2 检索优化（p95 / p96）

1. SingleFlight：agentix/infrastructure/cache/singleflight.py（纯 asyncio 在飞合并，带 per-key future + 异常传播），接入 retrieve_scoped_docs_cached，RETRIEVAL_SINGLEFLIGHT_ENABLED=false。
2. 自适应检索与级联：确定性查询分类（「第X条」→ exact/BM25 优先；多跳→graph 优先）+ 低分升级的六级 cascade（exact/metadata → bm25+dense → rrf → rerank → hyde/rewrite → graph/external），RETRIEVAL_ADAPTIVE_ENABLED=false + RETRIEVAL_CASCADE_MIN_SCORE。
3. Rerank 动态跳过：候选数/分数达标即 skip，RERANK_DYNAMIC_SKIP_ENABLED=false。
4. 缓存键修复（**无 flag，正确性优先**）：retrieval/usecases/chat_service.py 的 _retrieval_cache_key 升级 |k2 后缀 = +tenant_id + fusion 配置指纹（一次性冷却是可接受代价，TTL 600s）。
5. Neo4j 成本护栏：GraphCostEstimator（纯字符串/AST 预检：无界变长遍历、OPTIONAL MATCH 堆叠、RETURN * → 拒绝/加 LIMIT），NEO4J_COST_GUARD_ENABLED=false。
6. MILVUS_NPROBE 配置化（默认 16 = 现硬编码值，行为等价）。

**测试**：p95（singleflight 并发合并与异常传播、cascade 分支、键含 tenant/指纹）、p96（成本护栏纯离线、nprobe 默认等价）。

**落地注记（2026-10-06，按实）**：六级级联按既有原语折叠为分级升级——hyde/rewrite 前置于查询智能段（`|r1`/`|y1` 缓存后缀已占位）、rrf/rerank 内嵌 hybrid 级（`RETRIEVAL_HYBRID_ENABLED` 总闸内）、graph/external 级留可注入缝（本波无生产注入方，诚实缺席）；实际级序 statute/general = `dense_exact → bm25_rrf_rerank`，multi_hop = 融合前置 → dense 兜底，`RETRIEVAL_CASCADE_MIN_SCORE=0`（默认）= 空命中才升级。检索域 4 键沿 R-P1 纪律只进 `.env.example` 注释示例（不入 configuration.md/p41），统一域 2 键五处全同步。成本护栏为纯字符串预检（未引入 AST）。

### W3 · H3 运行时治理（p97）

1. 准入控制：agentix/performance/admission.py（全局并发 + 队列深度三态：放行/排队/拒绝 429），挂 Control Plane 入口，ADMISSION_CONTROL_ENABLED=false。
2. 重试预算：agentix/reliability/retry_budget.py（run 级共享 token 桶，跨层防 3×3×3 放大），RETRY_BUDGET_ENABLED=false；补重试分类审计测试（仅 timeout/429/503/reset 可重试）。
3. 自适应超时：tool timeout = min(spec, 剩余 run 预算)，ADAPTIVE_TOOL_TIMEOUT_ENABLED=false。
4. 隔离舱 + 出口连接池（roadmap E3）：按下游分池 semaphore（LLM/检索/Neo4j/MCP/外搜）+ 应用级共享 httpx.AsyncClient 生命周期（消灭 per-request 创建）；池尺寸走既有 settings 模式。
5. 降级阶梯 + 负载甩脱：agentix/performance/load_shedder.py 七级（L0 全量 … L6 礼貌拒绝），输入 event-loop lag / 队列深度 / 下游延迟，优先甩后台（evolution/eval），LOAD_SHEDDING_ENABLED=false，降级经既有 record_degraded 留痕。

**测试**：p97（准入三态、预算桶跨层共享、超时裁剪、隔离舱故障不串扰、阶梯转移）。

**落地注记（2026-10-06，按实）**：准入挂点 = 三个 chat 入口（app `_invoke_tracked`、app 流式 spawn、检索流式 spawn）+ runs 入口（`plane.spawn` 前），并发账本复用 `plane.active_count()`（与平面同生命周期，无独立释放句柄），拒绝经既有 `global_exception_handler` 映射 429（Plain error 仍 500）；LLM 不另设池（复用 W1 `LLM_MAX_CONCURRENCY` limiter），隔离舱只设检索/Neo4j/MCP/外搜 4 池（0=不限直通），executor 按 `TOOL_POOL_MAP` 归池（search→web_search、cypher_query/predefined_cypher→neo4j，未声明不入池）；排队为 30s 有界轮询（模块常量 `ENQUEUE_TIMEOUT_S`，非 settings），队列深度/容量经 `queue_depth()/queue_capacity()` 喂降级阶梯；重试分类复用既有 `is_retryable`（NON_RETRIABLE 先判，timeout/connection/5xx/reset 命中），task 层闸在退避 sleep 前、tool 层在退避前并保留 `last_exc`、检索层在 sleep 前（无 RequestScope 时为每调用独立 fallback 桶，诚实降级）；自适应超时在 attempt 循环内取 `min(spec, scope.remaining_ms())`，flag-off 返回 `int(spec.timeout_ms)`（`timeout after Nms` 文案字节等价）；共享 httpx `trust_env=False` 统一 limits，flag-off = per-request 新建（owns=True，`post` 零额外参数=基线调用形状）；降级输入 = W0 lag 采样器 + admission 队列 + breaker 非 CLOSED 数，上行迁移经 `record_degraded("load_shed_lN")` 留痕，甩脱仅五类后台任务（evolution/eval/memory/summary/import，未知类型保守不甩），L6 不在消费侧甩（交准入礼貌拒绝）。统一域 12 键五处全同步。

### W4 · H4 持久化 + SSE 后端（p98）

1. 事件批写：agentix/agent/events/redis_stream.py 改 pipeline 批量 XADD，EVENT_BATCH_MAX_SIZE=50 / EVENT_BATCH_FLUSH_MS=20（0=关）；memory/jsonl 后端不变。
2. 轨迹异步写：内存队列 + 后台 writer（TRAJECTORY_ASYNC_WRITE_ENABLED=false），热路径仅留 minimal event。
3. SSE 合并：token 帧 50ms 窗口合并为 delta（SSE_COALESCE_ENABLED=false）。
4. SSE 背压：有界队列（SSE_QUEUE_SIZE=0 关），满则丢非关键帧、保 finished/error/status/tool_result 终态帧。

**测试**：p98（合并窗口内容恒等、丢帧保终帧、批写批读一致、异步写排序与兜底落盘）。

**落地注记（2026-10-06，按实）**：① 事件批写默认 **0/0=关**（规划初版 50/20 按实修订：批写使 publish→flush 前不可见，破坏 phase23 `wait(timeout=0)` 即时可见契约与「连接错误在 publish 内传播」的诚实失败语义；`.env.example` 给推荐值 50/20 供显式启用）——flag-on 时 dedup SET NX 仍即时（跨进程去重语义不变），xadd/save/rpush/ltrim 四件入缓冲，满批即 flush、否则 `EVENT_BATCH_FLUSH_MS` 定时 flush，单次 `pipeline(transaction=True)` 提交，失败回滚全批 dedup 并上抛（per-publish 原子放宽为批次原子），`aclose()` 关停兜底挂 `main._shutdown`；memory/jsonl 后端零改动。② 轨迹异步写收口在 `TrajectoryRecorder.record→sink.append`（应用层唯一收口；executor/runtime 的 mirror 是 observability 侧另一套，不在本波范围）：flag-on 且在 event loop 时入队 + 懒启动后台 writer（FIFO 保序），无 loop 降级同步直写（本仓 record 调用方全 async，无交错乱序），`flush_trajectory_writes()` 关停/测试兜底（与 writer 竞争 get_nowait 安全：单线程、append 无 await 点），`reset` 取消 writer。③ SSE 门控挂 **HTTP 出口两缝**（编排域 `interfaces/http/v1/chat.py::event_stream`、检索域 `retrieval/api/routers/chat.py::sse_generator`）而非 service 层：service 帧语义/seq 戳记/落库/终态收口零感知，非流式聚合不经背压（丢帧只作用于真 SSE）；合并为**到期检查式拉驱动**（帧到达时判窗口过期，绝不取消源；尾帧仅在源正常耗尽后 flush，GeneratorExit 路径不 yield）；token 类型 = `message`/`answer_chunk`/`thinking_chunk` 且非 finished；关键帧集合 = 规划 `finished/error/status/tool_result`（实际词汇映射 `done/error/tool_end`）∪ `meta`（流首帧会话锚定，丢了前端无法关联会话，按实扩展）；背压 producer 主动拉源（满丢非关键+`sse_dropped_frames_total` 计数，关键帧 await put 阻塞等位），源异常经信封传播、消费者关闭 cancel producer。④ 新指标 `sse_dropped_frames_total{reason}`（唯一新计数器）。

### W5 · H5 多智能体通信压缩（p99）

1. 子代理结果契约：{status, claims[], evidence_refs[], artifact_refs[], confidence, cost{tokens,latency_ms}} 替代 giant string，SUBAGENT_RESULT_CONTRACT_ENABLED=false。
2. 共享证据面：manager/peer 经既有 obs:// ArtifactStore 交换 evidence 引用（不再传整段父轨迹）。
3. 早停：evidence 充分即停其余 peer（复用既有 information_increment 判据），MULTIAGENT_EARLY_EXIT_ENABLED=false。

**测试**：p99（契约序列化往返、早停省调用 `_boom` 哨兵、证据引用解析与缺省回退）。

**落地注记（2026-10-06，按实）**：① 契约面挂 manager **单缝** `peer_face`（原 proposer/reviewer 两处 `str()+bound_result_face` 统一收口）：flag-off/裸字符串结果 → 基线全文截断逐位等价；flag-on → 紧凑 JSON 契约面 `{status,claims,evidence_refs,artifact_refs,confidence,cost}`（`multi_agent/contract.py::SubAgentResult`）——claims = 非空行有界提取（5 条 × 200 字符）、`meta["evidence"]` 逐条经 `offload_content` 外置成 `obs://` 句柄（阈值 2000，过短内联降级 ≤120 字）、`meta["artifacts"]` 键值 + 输出/元数据中既有 `obs://` 原样透传；cost 取 peer 自报 `meta["tokens"]/["latency_ms"]`（缺省 0——**不臆造测量值**）；完整输出仍留 `ManagerOutcome.peer_results`，契约是**面替换不是数据丢失**；`_extract_decision` 决策仍读完整 `PeerResult`（meta/output 路径零变化）。② 共享证据面本波范围 = 证据条目外置 + 契约面过 `max_result_chars` **整体外置可取回**（`try_offload_face`：从「丢字符」变 `…[结果面已外置，可取回 obs://…]`，降级回基线截断）；「父轨迹四传递点」（supervisor state 直通 / 隔离截断 / 黑板 `_joined()` / 父记忆快照）**不在本波动**——黑板承载契约面已达成「peer 间不再传整段」的等效，state 直通归 isolation 域。③ 早停插 proposer 后 / reviewer 前：`MULTIAGENT_EARLY_EXIT_ENABLED` 且 `evidence_sufficient(p_out, board_before)`（两条件缺一不可：带证据 = `meta["evidence"]` 或输出内嵌 `obs://`；有增量 = 复用 `has_information_increment`，上下文取**入板前**黑板防自比）⇒ `reason=evidence_sufficient` 提前收敛；顺序拓扑下「停其余 peer」= 跳过 reviewer 调用与后续轮次（peer 内联 await 无 task 可 cancel、无在途消息，`bus.cancel` 不适用——诚实记档）；`derive_exit_contract` 对该未知 reason 走 `ok=True` 兜底 → `completed/completed`、`truncated=False`，**不改推导表**（p88 推导表测试不破）。④ `ManagerOutcome.result_contracts` 新字段（缺省空，`__post_init__` 退出契约零接触）；`lg_supervisor` 仅 flag-on 非空时挂 `defense_meta["result_contracts"]`（空不挂键 = flag-off meta 逐位等价；增键不破 p88 聚合——只读 4 键）。⑤ 新文件 `multi_agent/contract.py`：`agentix.application` 依赖全部**函数内延迟 import**（orchestration 不顶层反向依赖 application）、不 import manager（manager import 它，防环）；导出进 `multi_agent/__init__`。⑥ 测试 34 例（契约派生/往返/容错反序列化、单缝等价、黑板压缩、过限外置可取回、`_boom` 早停省调用 + 三反例、判据纯函数 5 态、`resolve` 四回退、退出契约映射、五处同步）。

### W6 · 前端感知性能与实时动态 UI（web/）

**A. 流式渲染性能**

1. 批渲染：新建 useStreamBuffer composable —— token 先入缓冲，50ms 窗口 flush（终帧/abort 立即 flush）；渲染次数从每 token 降至约 20/s。配套修 useConversationChat 的 displayMessages 恒定性（仅流式消息原地更新，映射引用稳定）。
2. 智能吸底：纯函数 shouldStickToBottom（近底阈值 80px）+ rAF 节流 + 「↓ 回到底部」浮动胶囊；progress 帧同样触发；用户上翻不打扰。
3. markdown 流式渲染：引入 marked + dompurify（转义/消毒后渲染；flush 解析、终帧全量）；助手消息富文本化，用户消息保持纯文本。降级链：npm 安装失败 → 转义优先的轻量子集渲染器，绝不裸 v-html。

**B. 实时可见动态**

4. 阶段步骤器：准备会话 → 检索知识库 → 生成回答 三态 stepper，生成期间保持紧凑显示（修「首 token 即消失」）；消费现被丢弃的 detail.stages：四阶段子步骤 + 各阶段 ms 徽标。
5. 工具活动卡片：tool_running / tool_start / tool_end → 可折叠卡片行（不再污染思考区）。
6. 流式光标：生成行尾闪烁 caret。
7. 停止按钮：流式期间发送键变「停止」（接既有 abort()），保留部分回答 + toast「已停止生成」。
8. 耗时芯片：finished 后显示「耗时 Xs · 首字 Ys」（消费 W0 注入的 duration_ms / ttft_ms）。
9. 执行轨迹实时化：流式期间默认展开 + 高亮最新步骤，完成后收起。
10. Toast 体系：零依赖 ToastHost + useToast，替换 useKbModalChat 的 alert()。
11. 消息操作：复制按钮 + 重新生成（复用 sendMessage 重发末轮问题）。

**测试（vitest，现 126 → 预计 +35~50）**：useStreamBuffer（fake timers 断言 flush 次数与内容恒等）、shouldStickToBottom 纯函数、displayMessages 恒等性、stepper/工具卡片/停止/复制组件、markdown 消毒（XSS 样本断言净化）、toast 生命周期、ChatMessageList 进度持续显示回归。

**配套后端小改**（随 W0/W4 落）：finished/meta 帧增量字段 duration_ms/ttft_ms/breakdown_ms（增量字段不破坏 p65 词汇）；其余前端数据源均已在既有帧内。

**落地注记（2026-10-06，按实）**：① 依赖：`marked@18.1.0` + `dompurify@3.4.16`（仅 web/package.json，pip_audit 零影响）；另增 devDependency `jsdom`——DOMPurify 依赖真实 DOM 语义，happy-dom 的节点突变与 iframe 自动加载同消毒器不兼容（实测 h1/script 消毒结果不稳定 + NetworkError），XSS 断言用 `// @vitest-environment jsdom` docblock 隔离（`markdown.test.ts`/`ChatMessageList.test.ts`），其余测试仍走全局 happy-dom。② 批渲染：`useStreamBuffer` 50ms 窗（可缓冲帧 = progress/thinking_chunk/tool_running/非空 answer_chunk；终帧 `pushNow` 先落缓冲再应用、abort 走 `try/finally flush` 兜底、`onFlush` 一窗最多一次）；吸底接线拆双回调——`useConversationChat` 新增 `onChunkScroll`（流式增量走 smartScroll，贴底才跟随），`onScroll` 保留 force（切会话/发送），缺省回落保兼容；`ChatPanel` rAF 节流 + 「↓ 回到底部」胶囊（`showJumpBottom`）。③ 三态 stepper：`StreamStepper` 消费 progress + `detail.stages`（SUB_STEP_LABELS：rewrite 查询重写/sparse 稠密前稀疏/dense 稠密检索/rrf 融合排序/rerank 重排序，未知 stage 原样回退），progress 不再被 answer_chunk 清除、生成期间 `compact=!!content`；**测试暴露真实样式缺陷**——模板 `:class="stateOf(i)"` 与 CSS `.is-active/.is-done` 不匹配（样式从未生效）→ 修为 `is-${stateOf(i)}`。④ 工具活动：`tool_running` → `msg.toolActivity`（不再写 thinking），`finalizeAssistantStream` 折叠 `toolHistory`（去重）+ `timing` 落位；**`tool_start`/`tool_end` 属编排域 chat 模块（不经 chatStreamHandlers 链），不在本波范围，诚实缺席**。⑤ `StreamableMessage`（chatTypes.ts）补 `toolActivity/toolHistory/timing` 可选字段（与 `conversation.ChatMessage` 同构；chatStreamHandlers ↔ chatTypes 互相 import 为 type-only，无运行时环）。⑥ markdown：`marked`(gfm+breaks) → `DOMPurify.sanitize` → v-html，异常降级 `escapeHtml`；`memoRenderMarkdown` Map 缓存 >200 整体清空；流式光标 span 拼在 sanitize **之后**（受信常量不进消毒器）。⑦ 停止/重生成：`useConversationChat.stopGeneration/regenerate` + `useKbModalChat.stopModalGeneration/regenerateModal`，接线面 = Home/History/KbChatModal/PdfReaderWithChat/PersonalKbView（modal 链经 `PersonalKbBrowsePane` 转发 stop/regenerate 事件）；停止保留部分回答 + toast「已停止生成」，重生成仅末条助手 + 非流式可点。⑧ Toast：`useToast` 模块级单例 + `ToastHost` 挂根 `App.vue`（两模块共用）；`useKbModalChat` 3 处 alert→toast（error 型）；**`useKbBrowseActions`/`useKbPersonalUpload` 的 alert 不在 B10 字面范围未动**（诚实记档，W7 可评估）。⑨ 测试：126 → **198**（+72，超出 +35~50 预期）——新增 `scrollBehavior.test.ts`7 / `useStreamBuffer.test.ts`10 / `markdown.test.ts`11 / `useToast.test.ts`6 / `StreamStepper.test.ts`8 / `chatStreamHandlers.test.ts`13，扩展 `ChatMessageList.test.ts` +14、`ComposerBox.test.ts` +3；既有「rag-progress-indicator」断言随 B4 替换为 `.stream-stepper`。⑩ 门禁：vitest **198 passed**（22 文件）、`vue-tsc` 零错、`vite build` 通过（369.63 kB / gzip 127.28 kB）。

### W7 · 观测收口 + 全文档同步 + 交付

1. OTel GenAI 语义属性补齐（gen_ai.usage.input_tokens 等，otel 为既有硬依赖，零新增包）；指标标签基数守卫测试。
2. CI 修复：web/package.json 补 lint 脚本（现 ci-gate 必红），alias 至 typecheck。
3. perf_gate 转阻塞评估（baseline 已入库则接入）。
4. **全文档同步**（见 §7）。
5. 版本 0.1.3 → 0.2.0（bump_version.py 四处 + 文档字面量）。
6. **交付前完整测试**（见 §8 清单，一项不省）。

---

## 5. Flag 登记表（规划态；执行中增补，全部默认关 + 五处同步）

五处同步 = ① agentix/config/settings.py 字段 ② agentix/config/selfcheck.py DEFAULT_IGNORED_MISSING（或本地 .env 补键）③ .env.example 活行 ④ docs/architecture/configuration.md §5 表行 ⑤ p41 测试清单核验；检索域 flag 按既有惯例放 agentix/retrieval/settings.py。

| Flag | 类型/默认 | 波次 |
|---|---|---|
| LATENCY_BREAKDOWN_ENABLED | bool / false | W0 |
| LATENCY_TOTAL_BUDGET_MS | int / 0=关 | W0 |
| EVENT_LOOP_LAG_METRIC_ENABLED | bool / false | W0 |
| ROUTING_FASTPATH_ENABLED | bool / false | W1 |
| LLM_TIER_ROUTING_ENABLED | bool / false | W1 |
| LLM_ROLE_TIER_MAP | str / 空=等价基线 | W1 |
| VERIFICATION_MAX_LLM_CALLS | int / 1 | W1 |
| VERIFICATION_TIMEOUT_MS | int / 1000 | W1 |
| VERIFICATION_TOKEN_BUDGET | int / 0=关（执行中增补，任务 3 文本提及而初版表遗漏） | W1 |
| TOOL_OUTPUT_BUDGET_TOKENS | int / 0=关 | W1 |
| RETRIEVAL_SINGLEFLIGHT_ENABLED | bool / false | W2 |
| RETRIEVAL_ADAPTIVE_ENABLED | bool / false | W2 |
| RETRIEVAL_CASCADE_MIN_SCORE | float / 0=关 | W2 |
| RERANK_DYNAMIC_SKIP_ENABLED | bool / false | W2 |
| NEO4J_COST_GUARD_ENABLED | bool / false | W2 |
| MILVUS_NPROBE | int / 16（等价） | W2 |
| ADMISSION_CONTROL_ENABLED | bool / false | W3 |
| ADMISSION_MAX_CONCURRENT_RUNS | int / 0=用 CONTROL_PLANE 上限 | W3 |
| ADMISSION_MAX_QUEUE | int / 0=超限即拒（执行中增补） | W3 |
| RETRY_BUDGET_ENABLED | bool / false | W3 |
| RETRY_BUDGET_TOKENS | int / 6（执行中增补） | W3 |
| ADAPTIVE_TOOL_TIMEOUT_ENABLED | bool / false | W3 |
| LOAD_SHEDDING_ENABLED | bool / false | W3 |
| BULKHEAD_RETRIEVAL_SIZE / BULKHEAD_NEO4J_SIZE / BULKHEAD_MCP_SIZE / BULKHEAD_WEB_SEARCH_SIZE | int / 0=不限（执行中增补×4） | W3 |
| SHARED_HTTP_MAX_CONNECTIONS | int / 0=关（执行中增补） | W3 |
| EVENT_BATCH_MAX_SIZE / EVENT_BATCH_FLUSH_MS | int / 0 / 0=关（初版 50/20，按实修订：破坏即时可见契约 ⇒ 默认关，.env 给推荐 50/20） | W4 |
| TRAJECTORY_ASYNC_WRITE_ENABLED | bool / false | W4 |
| SSE_COALESCE_ENABLED | bool / false | W4 |
| SSE_QUEUE_SIZE | int / 0=关 | W4 |
| SUBAGENT_RESULT_CONTRACT_ENABLED | bool / false | W5 |
| MULTIAGENT_EARLY_EXIT_ENABLED | bool / false | W5 |

（前端无 flag 体系：W6 前端改动以测试保证内容恒等，直接生效。）

---

## 6. 测试登记表

| 编号 | 覆盖 | 波次 |
|---|---|---|
| `tests/test_p92_perf_observability.py` | 延迟接线/TTFT/拆解/预算/LLM账本 | W0 |
| `tests/test_p93_perf_benchmark.py` | 基准 schema/gate 语义/queries 分层 | W0 |
| `tests/test_p94_llm_optimization.py` | fast-path/档位/验证预算/输出预算/前缀 | W1 |
| `tests/test_p95_retrieval_cascade.py` | singleflight/级联/键升级/rerank跳过 | W2 |
| `tests/test_p96_graph_cost_guard.py` | Neo4j 护栏/nprobe/五处同步 | W2 |
| `tests/test_p97_runtime_control.py` | 准入/重试预算/超时/隔离舱/降级 | W3 |
| `tests/test_p98_persistence_sse.py` | 事件批写/轨迹异步/SSE 合并背压 | W4 |
| `tests/test_p99_multiagent_efficiency.py` | 契约/证据面/早停 | W5 |
| `tests/test_p100_genai_observability.py` | GenAI span 命名/gen_ai.* 属性/接线/指标标签基数白名单 | W7 |
| `tests/test_perf_smoke.py` | 进程内软基准 | W0 |
| web `scrollBehavior` / `useStreamBuffer` / `markdown` / `useToast` / `StreamStepper` / `chatStreamHandlers` 六新套件 + `ChatMessageList`/`ComposerBox` 扩展（198 例） | 批渲染/吸底/markdown/stepper/工具卡/toast/handlers/组件 | W6 |

复用既有 fake：tests/fakes/redis.py、tests/test_p84_rag_p2_query_rewrite.py 的同步 FakeRedis + FakeLLM、tests/test_r34_milvus_unification.py 的 _FakeCollection、LangChain GenericFakeChatModel。全部离线、零外部服务。

---

## 7. 文档同步机制

| 时机 | 文档 | 内容 |
|---|---|---|
| 每波完成 | 本文档 §9 | 勾选 + 实测数据回写 + 新文件路径补反引号 |
| W6 | docs/design/frontend-streaming-optimization.md（拟建） | 前后端帧流架构、flush/吸底/markdown 策略、**优化前后量化对照表** |
| W7 | CHANGELOG.md [Unreleased] Phase H 节 | 分波条目 + 每块「用户可感知变化」一句话 + 回归行 |
| W7 | README.md | 能力清单补 Adaptive Execution / 感知性能条目；§11 计数链按实测更新 |
| W7 | docs/architecture/configuration.md §5 | 全部新 flag 行（五处同步之一） |
| W7 | docs/design/optimization-roadmap.md | E1–E8 状态回写 |
| W7 | docs/migration/current-tree.md | tests 模块数更新 |
| W7 | docs/PROJECT_SPEC.md | 新增能力/flag/目录相关节 |
| W7 | skills/release-checklist/SKILL.md 五处计数同步 | 计数变化时同步 |

---

## 8. 交付纪律与全量门禁

**提交节奏**：每波全绿 → 单行长中文 subject + 尾部 [#类型] → 小包直推（RST 则走既有分块推送脚本）；波间独立可回退。

**交付前完整测试（一项不省）**：

```text
1. 全量 pytest（真源口径回写 README §11）
2. ruff check .
3. mypy agentix/domain
4. 覆盖率 --cov=agentix 实测 ≥ 60
5. bandit medium+（LOW 77 项信息性保留）
6. pip_audit -r requirements.txt（记录残余，预期 21 项不变——本阶段零新增依赖除 marked/dompurify[web]）
7. web：npm run test / typecheck / build / lint（修复后）
8. 守卫：p41 p35 p36 p40 p17 p22 p66 p65 p24 依赖一致性 ablation
9. bump_version.py --check（0.2.0）
10. 分支状态：main 与 origin/main 同步、工作树干净
```

---

## 9. 进度回写区

- [x] W-1 规划文档落盘（本文件，2026-10-05，commit 6bcaf77）
- [x] W0 性能真值 —— 状态：**完成**（2026-10-05）
  - 新增测试：p92=35 / p93=17 / perf_smoke=3，合计 55 例；全量 pytest **3963 passed**（3908 → 3963）、ruff 全绿、mypy agentix/domain 全绿、p41/p35/p47/p65/p66/p17/p40/p46 守卫全绿。
  - 新增 flag（五处同步完成）：`LATENCY_BREAKDOWN_ENABLED`（bool/false）、`LATENCY_TOTAL_BUDGET_MS`（int/0=关）、`EVENT_LOOP_LAG_METRIC_ENABLED`（bool/false）。
  - 新增文件：`agentix/performance/__init__.py`、`agentix/performance/latency.py`、`agentix/performance/budget.py`、`evals/performance/__init__.py`、`evals/performance/fakes.py`、`evals/performance/runner.py`、`evals/performance/perf_gate.py`、`evals/performance/queries.jsonl`、`evals/performance/baseline.json`、`tests/test_p92_perf_observability.py`、`tests/test_p93_perf_benchmark.py`、`tests/test_perf_smoke.py`。
  - 接线改造：`agentix/main.py`（record_request + endpoint 归一化 + lag 采样启停）、`agentix/observability/metrics.py`（4 指标 + 4 助手 + noop）、`agentix/application/services/run_event_stream.py`（TTFT/终帧戳记/拆解/预算）、`agentix/application/agents/llm_factory.py` 与 `agentix/retrieval/usecases/chat_llm.py`（角色账本）；CI 增 perf-smoke job（continue-on-error: true）。
  - 基线首录（本地 iterations=3，顺序压测 p50 ms / OVERALL p50 **1.88ms**）：liveness 3.21、metrics 3.08、docs 1.33、openapi 2.56、root 3.04、api_probe 1.88、stream_answer 0.47、stream_progress 0.33、emitter_cpu 0.78。
  - 偏差与调整：① 九类负载由「问题难度分层」改为「传输面分层」（六免依赖 HTTP 端点 + 流×2 + 发射器 CPU 地板）——假件无法真实区分 easy/medium 问题难度，离线可测的是**代码路径**；② gate 阈值取 p50×3 而非 >10%（共享 CI 抖动下 10% 必红，首版非阻塞原则不变）；③ 检索域终帧实为 answer_chunk+finished=true（非 finished 词汇），戳记改为双识别；④ tokens/run 不入离线基准（假件无真实 token），留待 live 口径；⑤ budget 日志最初误用 loguru {} 插值（stdlib logging %-风格）被 fail-open 吞掉，已修并以测试钉死。
- [x] W1 LLM 优化 —— 状态：**完成**（2026-10-05）
  - 新增测试：p94=37 例；全量 pytest **4000 passed**（3963 → 4000）、ruff 全绿、mypy agentix/domain 全绿、p41/p46/p66/p23/p24/p17/p47/ablation/p49 守卫 215+ 例全绿。
  - 新增 flag（七枚，五处同步完成）：`ROUTING_FASTPATH_ENABLED`（bool/false）、`LLM_TIER_ROUTING_ENABLED`（bool/false）、`LLM_ROLE_TIER_MAP`（str/空）、`VERIFICATION_MAX_LLM_CALLS`（int/1）、`VERIFICATION_TIMEOUT_MS`（int/1000）、`VERIFICATION_TOKEN_BUDGET`（int/0，执行中增补入 §5）、`TOOL_OUTPUT_BUDGET_TOKENS`（int/0）。
  - 新增指标：`llm_calls_saved_total{reason}`（Counter，fast-path 省下的分类调用，reason 有界 chit_chat/statute_direct）、`prompt_prefix_fingerprint{builder}`（Gauge，CRC32 前缀指纹，仅静态模板角色 router/hallucination/tool/summarizer/planner；general 含动态记忆注入不上报）。
  - 接线改造：`agentix/application/agents/router.py`（`_fastpath_router` 确定性规则 + ablation 后前置短路）、`agentix/application/agents/llm_factory.py`（`_apply_tier_routing` 经 `get_model_gateway().resolve_model` + `_TrackedChatOpenAI.ainvoke` 前缀指纹）、`agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/final_answer_guard.py`（三旋钮预算 + 新 steps：budget/token_budget/timeout）、`agentix/agent/context/engine.py` 与 `agentix/agent/context/simple.py`（段级配额 → `_isolate_ref` obs:// 外置）、`agentix/observability/metrics.py`（2 指标 + 2 助手 + noop）。
  - 偏差与调整：① 段级预算收口定为 `ContextPort.build` 轨迹段（工具/检索观测）单缝——四分区统一结构不重复造（history 条数窗口 / memory 索引预算 / retrieval 域 CONTEXT_BUDGET_TOKENS 各司其职），§4 任务 4 已按实修订；② statute_direct 短路 → `graphrag-query`，与既有 `_heuristic_router` 法条关键词映射同向；寒暄正则**全句匹配**（加料即回 LLM）+ 推理词黑名单，宁保守不激进；③ VERIFICATION_TOKEN_BUDGET 按 `len//4` 粗估累计台账（非 provider 精确计数），按本次答案 state.steps 已尝试次数计 MAX_LLM_CALLS；④ 超时测试口径用 300ms 窗（10ms 会被模板格式化步吃掉，生产默认 1000ms 余量充分）；⑤ 档位解析任何异常 fail-open 回基线 profile（未知档位 KeyError 不外抛）。
- [x] W2 检索优化 —— 状态：**完成**（2026-10-06）
  - 新增测试：p95=42 / p96=32，合计 74 例；全量 pytest **4074 passed**（4000 → 4074，15 skipped，7 分 01 秒）、ruff 全绿、mypy agentix/domain 全绿、p41/p68/p76/p84/p35/p47/p12/p83/p86/p85/r1/r34/r2×2/p78 关联套件全绿。
  - 新增 flag（六枚）：统一域 2 枚五处同步——`NEO4J_COST_GUARD_ENABLED`（bool/false）、`MILVUS_NPROBE`（int/16，= 原硬编码值等价）；检索域 4 枚两处同步（retrieval/settings.py + `.env.example` 注释示例，沿 R-P1「retrieval 域专属键不入本表」纪律不入 configuration.md/p41）——`RETRIEVAL_SINGLEFLIGHT_ENABLED`（bool/false）、`RETRIEVAL_ADAPTIVE_ENABLED`（bool/false）、`RETRIEVAL_CASCADE_MIN_SCORE`（float/0=关）、`RERANK_DYNAMIC_SKIP_ENABLED`（bool/false）。
  - 新增文件：`agentix/infrastructure/cache/__init__.py`、`agentix/infrastructure/cache/singleflight.py`、`agentix/retrieval/domain/services/cascade.py`、`tests/test_p95_retrieval_cascade.py`、`tests/test_p96_graph_cost_guard.py`。
  - 接线改造：`agentix/retrieval/usecases/chat_service.py`（缓存键升级 tenant+融合指纹+`|k2`、`_resolve_cache_tenant` fail-open、miss 分支折叠 `_dense_miss`/`_hybrid_miss` 闭包 + cascade/singleflight 包装、`tenant_id` 可选参）、`retrieval_hybrid.py`（rerank stage 动态跳过留痕 `applied=false + skipped`）、`chat_agent_tools.py`/`retrieval_port_adapter.py`（跳过判定接线 + 端口显式 tenant 透传）、`kg_neo4j_conn.py`（`GraphCostEstimator` + `query_readonly` flag 门控，只读闸仍在最前）、`milvus_impl.py`（nprobe 配置化，越界回落下限 1）、`agentix/config/settings.py`/`selfcheck.py`/`.env.example`/`configuration.md`/`test_p41`（统一域 2 键五处同步）。
  - 偏差与调整：① 六级级联折叠为分级升级（hyde/rewrite 前置、rrf/rerank 内嵌 hybrid、graph/external 无注入方缺席），§4 已按实注记；② rerank 高分跳过仅相似度口径判定（距离/融合分不可比 ⇒ 保守只按候选数跳过）；③ 键结构 `prefix+tenant:kb:art:k:scope:digest|fp|k2`——`|h1`/`|r1` 追加后仍居末位（p76/p84 endswith 断言不破），旧键一次性冷却（TTL 600s）；④ 成本护栏纯字符串预检：括号内存在 `..n` 即有界（列表字面量乘法 `[2*3]` 保守误拒，flag-on 专属子串语义，与 WRITE_CLAUSES 同姿态）；⑤ SingleFlight 结果信封 `("ok"/"err", v)` + `asyncio.shield` 等待——leader 取消 ⇒ waiter 抛 RuntimeError（不误判自身取消态），`finally` 同一性校验防 ABA 残留。
- [x] W3 运行时治理 —— 状态：**完成**（2026-10-06）
  - 新增测试：p97=42 例；全量 pytest **4116 passed**（4074 → 4116，15 skipped，7 分 05 秒）、ruff 全绿、mypy agentix/domain 全绿、W3 关联回归 591 例（p41/p22/p31/p33×2/p38/p39/p54 + p68/p73/p76/p84/p95/r1_rerank + r3_agent_runtime/llm_retry/p1_llm_concurrency/unit_task_queue/agent_run_api/phase25）全绿。
  - 新增 flag（12 枚，五处同步完成，7 枚执行中增补入 §5）：`ADMISSION_CONTROL_ENABLED`（bool/false）、`ADMISSION_MAX_CONCURRENT_RUNS`（int/0=回落 CONTROL_PLANE 上限）、`ADMISSION_MAX_QUEUE`（int/0=超限即拒）、`RETRY_BUDGET_ENABLED`（bool/false）、`RETRY_BUDGET_TOKENS`（int/6）、`ADAPTIVE_TOOL_TIMEOUT_ENABLED`（bool/false）、`LOAD_SHEDDING_ENABLED`（bool/false）、`BULKHEAD_RETRIEVAL_SIZE`/`BULKHEAD_NEO4J_SIZE`/`BULKHEAD_MCP_SIZE`/`BULKHEAD_WEB_SEARCH_SIZE`（int/0=不限）、`SHARED_HTTP_MAX_CONNECTIONS`（int/0=关）。
  - 新增指标：`admission_decisions_total{state}`（admitted/queued/rejected 计数，经 `record_admission_decision`）。
  - 新增文件：`agentix/performance/admission.py`（三态准入 `admission_wait`/`AdmissionController` 30s 有界排队）、`agentix/performance/load_shedder.py`（七级阶梯 `classify_load`/`should_shed`/`TASK_TYPE_CATEGORY`）、`agentix/reliability/retry_budget.py`（`RetryBudget` 跨层共享桶 + `retryable_error` 分类闸）、`agentix/reliability/bulkhead.py`（4 池 `bulkhead_slot`/`pool_for_tool`/`pooled_wait_for`）、`agentix/infrastructure/http/__init__.py` + `shared_client.py`（`acquire_http_client` owns 语义）、`tests/test_p97_runtime_control.py`。
  - 接线改造：`application/services/request_scope.py`（+`deadline_at`/`retry_budget` 字段与 `set_deadline_seconds`/`remaining_ms`）、app `chat_service.py`（ask/流式 scope 设 run 截止期 + `_invoke_tracked`/流式 spawn 前 `admission_wait`）、`retrieval/usecases/chat_service.py`（流式 spawn 前准入 + `retrieve_docs_with_retry` 分类/预算闸 + miss 分支 `_pooled_runner` 检索隔离舱，SingleFlight 等待方不占池）、`interfaces/http/v1/runs.py`（spawn 前准入，拒绝→429）、`tools/gateway/executor.py`（`_effective_timeout_ms` min(spec,剩余预算) + `pooled_wait_for` 归池 + `last_exc` 分类/预算闸）、`application/services/task_retry.py`（sleep 前预算闸）、`application/services/task_queue.py`（`_process_message` 按级甩后台任务 FAILED+ack 不进重试）、`capabilities/mcp/client.py`（`call_tool` 隔离舱）、`main.py`（`AdmissionDenied`→429 全局映射 + `_shutdown` 关共享 client）、`observability/metrics.py`（新计数器 + `get_last_event_loop_lag`）、rerank `_call_rerank` 与 `redis_cache._get_embedding`（共享 client 接线，owns=True 时 `post` 零额外参数=基线形状）、`agentix/config/settings.py`/`selfcheck.py`/`.env.example`/`configuration.md`/`test_p41`（12 键五处同步）。
  - 偏差与调整：① LLM 不另设池——复用 W1 `LLM_MAX_CONCURRENCY` limiter（同为 LLM 出口，双池反而分薄限额）；② 准入账本复用 `plane.active_count()`（无独立释放句柄），挂点=三 chat 入口+runs 入口而非「所有 runtime 出口」（fail-closed 三站点已全覆盖）；③ 队列 30s/滞后阈值/轮询间隔为模块常量非 settings（热路径键，YAGNI）；④ 共享 client 统一 `trust_env=False`，flag-off per-request 新建且 `post` 不带额外参数（保 p39 fake 契约逐位等价）；⑤ 检索层预算无 RequestScope 时为每调用独立 fallback 桶（诚实降级，不跨调用串桶）；⑥ 降级输入用 breaker「非 CLOSED 数」而非具体延迟（breaker 状态即下游劣化的既有信号）；⑦ L6 不在消费侧甩脱——交准入礼貌拒绝 429，避免消费端与准入端双重计数。
- [x] W4 持久化 + SSE 后端 —— 状态：**完成**（2026-10-06）
  - 新增测试：p98=31 例；全量 pytest **4147 passed**（4116 → 4147，15 skipped，7 分 16 秒）、ruff 全绿、mypy agentix/domain 全绿、W4 关联回归（p41/p65/p14/chat_api/progress 126 + events 三件 134 + trajectory + lifespan/app_error/p37 18）全绿。
  - 新增 flag（5 枚，五处同步完成，规划 §5 EVENT_BATCH 行按实修订）：`EVENT_BATCH_MAX_SIZE`（int/0=关，初版 50/20 修订——批写破坏 publish→flush 即时可见契约与连接错误即时传播语义，`.env.example` 给推荐 50/20）、`EVENT_BATCH_FLUSH_MS`（int/0=关）、`TRAJECTORY_ASYNC_WRITE_ENABLED`（bool/false）、`SSE_COALESCE_ENABLED`（bool/false）、`SSE_QUEUE_SIZE`（int/0=关）。
  - 新增指标：`sse_dropped_frames_total{reason}`（背压丢帧计数，终态帧永不丢）。
  - 新增文件：`agentix/application/services/stream_gate.py`（`gate_events` 组合两门控、`_coalesce_source` 到期检查式拉驱动合并、`_bounded_source` producer 有界队列背压）、`tests/test_p98_persistence_sse.py`。
  - 接线改造：`interfaces/http/v1/chat.py::event_stream` 与 `retrieval/api/routers/chat.py::sse_generator`（SSE 出口包 `gate_events`，非流式聚合不经背压）、`agentix/agent/events/redis_stream.py`（批写缓冲 + `flush()` 单次 `pipeline(transaction=True)` 提交 + `_schedule_flush` 定时 + `aclose()` 关停兜底；flag-off 逐条路径逐位不变）、`application/services/trajectory.py`（record→入队/同步双路、懒启动 `_writer_loop`、`flush_async` 兜底、`flush_trajectory_writes()` 单例入口、`_cancel_writer` 防 closed-loop、reset 取消 writer）、`main.py::_shutdown`（trajectory flush + event port aclose）、`observability/metrics.py`（1 计数器 + `record_sse_dropped` + noop）、settings/selfcheck/.env.example/configuration.md（§2 特性行 +3、§5 表 +5）/p41（5 键五处同步）。
  - 偏差与调整：① **EVENT_BATCH 默认 50/20 → 0/0**（§5 行已按实修订）：phase23 契约 `wait(timeout=0)` 要求 publish 后立即可见、连接错误要求 publish 内传播，批写两者皆破——改为显式启用的性能选项而非开箱默认，「默认关=与基线逐位等价」纪律优先；② SSE 门控挂 HTTP 出口而非规划暗示的流内层：service 层 seq 戳记/落库/终态收口零感知，背压丢帧不污染非流式聚合的答案拼接；③ 合并窗口用到期检查式（拉驱动）而非独立定时器——不取消源 generator（GeneratorExit 期间 yield 会 RuntimeError，尾 flush 仅在源正常耗尽后），代价是窗口尾延迟 = 窗口 + 帧间隔；④ 关键帧集合在规划四词外扩 `meta`（流首帧会话锚定帧，丢失则前端无法关联会话）；⑤ 轨迹异步写只收口应用层 recorder（observability 侧 `dump()/end()` 另一套 mirror 不在本波范围，诚实缺席）；⑥ writer 与 flush 竞争安全性靠「单线程 + append 无 await 点」而非锁（记档以免后续在 append 引入 await 破坏此假设）。
- [x] W5 多智能体 —— 状态：**完成**（2026-10-06）
  - 新增测试：p99=34 例；全量 pytest **4181 passed**（4147 → 4181，15 skipped，7 分 06 秒）、ruff 全绿、mypy agentix/domain 全绿、多智能体关联回归（p11/p19/p25/p88/p89/p79/p80/p41/ablation_wiring）**313 passed** 全绿。
  - 新增 flag（2 枚，五处同步完成）：`SUBAGENT_RESULT_CONTRACT_ENABLED`（bool/false）、`MULTIAGENT_EARLY_EXIT_ENABLED`（bool/false）。
  - 新增文件：`agentix/orchestration/strategies/multi_agent/contract.py`（`SubAgentResult` 契约 + `contract_face`/`try_offload_face`/`resolve_evidence_ref`/`evidence_sufficient`/`contract_enabled`/`early_exit_enabled`）、`tests/test_p99_multiagent_efficiency.py`。
  - 接线改造：`multi_agent/manager.py`（`peer_face` 单缝统一原 proposer/reviewer 两处 `str()+bound_result_face`、`_outcome` flag-on 收集 `result_contracts`、proposer 后插早停分支、`ManagerOutcome.result_contracts` 新字段、`__all__` 增 `peer_face`）、`multi_agent/__init__.py`（导出 4 符号）、`langgraph/lg_supervisor.py`（`defense_meta` flag-on 非空挂 `result_contracts`）、settings（MULTIAGENT 段 :674 后两键）+ selfcheck + `.env.example`（MULTIAGENT 区尾）+ `configuration.md`（§2 特性行 +2、§5 表 +2 行）+ p41 手写清单（+2）五处同步。
  - 偏差与调整：①「停其余 peer」按**顺序拓扑**实现——本仓无 fan-out（peer 内联 `await` 无 task 可 cancel、无在途消息），早停 = 跳过 reviewer 调用与后续轮次，`bus.cancel` 不适用；②「共享证据面」本波 = 证据条目 `offload_content` 外置 + 契约面过限**整体外置可取回**，「父轨迹四传递点」不在本波（黑板承载契约面已达成 peer 间不传整段的等效，state 直通归 isolation 域，诚实缺席）；③ 早停判据两条件缺一不可（带证据 ∧ 有信息增量，复用 `has_information_increment`，上下文取入板前黑板防自比）——比「复用判据」字面更严，防「有证据但纯复读」误停；④ `derive_exit_contract` 不改推导表（未知 reason + ok=True 兜底 `completed/completed`，p88 推导表零接触）；⑤ cost 字段取 peer 自报 meta 缺省 0，**不臆造测量值**；⑥ `contract.py` 的 application 依赖全部函数内延迟 import（orchestration 不顶层反向依赖），不 import manager（防环）。
- [x] W6 前端感知性能与实时动态 UI —— 状态：**完成**（2026-10-06）
  - 新增测试：web vitest 126 → **198**（+72，六新文件 + 两扩展；超 +35~50 预期）；`vue-tsc` 零错、`vite build` 通过。新增 devDependency：`jsdom`（DOMPurify 消毒断言需真实 DOM，happy-dom 不兼容——详见 §4 W6 落地注记①）。
  - 新增文件：`web/src/modules/knowledge/composables/useStreamBuffer.ts`、`composables/useToast.ts`、`utils/scrollBehavior.ts`、`utils/markdown.ts`、`components/StreamStepper.vue`、`components/ToastHost.vue` + 六套件测试文件；依赖 `marked@18.1.0`/`dompurify@3.4.16`。
  - 接线改造：`chatStream.ts`（progress stages/timing 类型）、`chatStreamHandlers.ts`（StreamProgress 带 stages、tool_running→toolActivity、answer_chunk 不清 progress、`finalizeAssistantStream` 收口）、`useChatStream.ts`（buffer 接线 + finally flush + 截断清态）、`useConversationChat.ts`（displayMessages 投影恒定化 + stopGeneration/regenerate + onChunkScroll 选项）、`chat/ChatPanel.vue`（smartScroll/rAF/回底胶囊/regenerate 透传）、`ChatMessageList.vue`（stepper/工具卡/markdown+光标/耗时芯片/复制+重生成/trace 受控展开）、`ComposerBox.vue`（streaming 停止键）、`HomeView`/`HistoryView`/`PersonalKbView`/`PersonalKbBrowsePane`/`KbChatModal`/`PdfReaderWithChat`（stop/regenerate 全链）、`App.vue`（ToastHost 挂根）、`useKbModalChat.ts`（3 alert→toast + stopModalGeneration/regenerateModal）。
  - 偏差与调整：① DOMPurify + happy-dom 实测不兼容（消毒结果不稳定 + iframe 自动 fetch 网络错误）→ XSS 断言文件级 jsdom docblock，未改全局环境（其余 20 文件仍 happy-dom 快跑）；② `tool_start`/`tool_end` 事件属编排域 chat 模块（走 `modules/chat/api/chatStream` 独立链），本波工具卡只消费 knowledge 链的 `tool_running`，规划字面按帧源裁剪；③ progress/工具/进度帧全部可缓冲 ⇒ 「step/total 进度条 + 子步骤 ms」在 50ms 窗内批量呈现（用户感知无差，渲染次数降为 ~20/s）；④ `StreamStepper` 状态类名与 CSS 不匹配系既有样式缺陷（测试暴露后修复），非本波引入；⑤ `useKbBrowseActions`/`useKbPersonalUpload` 的 alert 不在 B10 字面范围未替换（诚实记档）；⑥ 重新生成未复用 `sendMessage` 字面（该函数含新建会话等 home 专用语义），按 `regenerate` 独立导出重发末轮 user 消息，modal 侧同构 `regenerateModal`。
- [x] W7 观测收口 + 全文档同步 + 0.2.0 + 交付 —— 状态：**完成**（2026-10-06）
  - 新增测试：p100=17 例（GenAI span 命名/provider 推断/gen_ai.* 属性双键名与 fail-open、`genai_span` 网关、`invoke_with_breaker` 接线运行时断言、`llm_factory` 接线源级守卫、指标标签 29 项有界白名单 + 高基数身份标签禁入 + GenAI 键只入 span 不入指标）；全量 pytest **4198 passed**（4181 → 4198，15 skipped，7 分 31 秒）、覆盖率 **74.78% ≥ 60**、ruff 全绿、mypy agentix/domain 全绿、bandit medium+ **0**、pip_audit 残余 **14 唯一漏洞 / 22 行明细 / 8 包**（全 langchain 生态，较 21 项口径为同一集合的行级展示差异，零新增依赖）、web lint/test（**198**）/typecheck/build 四件全绿、`bump_version.py --check` 0.2.0 一致。
  - OTel GenAI 接线：`agentix/observability/tracing.py` 新增 `genai_span_name`/`infer_genai_system`/`apply_genai_request`/`apply_genai_usage` + 五常量；`spans.py` 新增 `genai_span()` async CM（经唯一 span 入口网关，不写 trajectory——LLM 轨迹已由 `_record_llm_trajectory`/检索域 usage 覆盖防一步双记）+ 再导出 `apply_genai_usage`；两 LLM 出口接线 = `chat_llm.invoke_with_breaker`（包装 breaker/limiter 双分支单出口补用量）与 `llm_factory._TrackedChatOpenAI.ainvoke`（limiter 外包 genai_span、返回前补用量，既有 try/except/finally 结构不重排）。
  - CI：`web/package.json` 补 `lint`（= `vue-tsc --noEmit`，修 ci-gate 必红）；perf-smoke 摘 `continue-on-error` 转**阻塞**（job 改名「性能冒烟」+ ci-gate needs 增补 + `perf_gate.py` docstring 按实改写）。
  - 版本：0.1.3 → **0.2.0**（bump_version 四处 + web/package-lock.json 根版本 + README 徽章 + PROJECT_SPEC 三处 + VERSIONING 示意图 + TECH_STACK 示例；本地 `.env` APP_VERSION 同步——不入库）。
  - 全文档同步（§7 清单全勾）：CHANGELOG [Unreleased] Phase H 八波分条 + 每波「用户可感知变化」+ 汇总回归行；README §1 能力表 + Adaptive Execution 行、§2 新增「可承受的过载」「可感知的流式」、§11 真源 3908 → **4198**（+290 计数链）、CI/CD 行补阻塞性能冒烟；configuration.md §5 全 35 flag 核验（检索域 4 键沿 R-P1 纪律豁免）；optimization-roadmap §4 分期表增「状态」列（E3/E5 已落地、E4/E6/E8 部分、E1/E2/E7 未动）；current-tree（tests 191 模块、agentix 541 .py、.env.example 295 活行）；PROJECT_SPEC（§3.4 Phase H 35 flag 指针 + §21.6 链路/指标两行 GenAI 与标签守卫）；release-checklist 前端四命令改 npm scripts。
  - 偏差与调整：① GenAI 用量只补 span 不进 Prometheus 指标（semconv 键进指标属高基数维度之外的第三套命名空间，指标面沿既有 `llm_*` 家族，p100 白名单钉住两侧边界）；② `llm_factory.ainvoke` 的接线以源级断言 + `invoke_with_breaker` 运行时断言组合覆盖（完整 ainvoke 需 ChatOpenAI 网络栈，离线不建）；③ bandit High 1 为 W2 `_fusion_fingerprint` 的 `sha1` 未标 `usedforsecurity=False`（S2 批漏网，非 Phase H 新隐患面）——已补 `# nosec B324` + 非安全用途注释复归 0；④ perf_gate 转阻塞后 CI 首跑需观察（阈值 p50×3 已含 3 倍抖动余量，本地基线已入库）；⑤ pip_audit 计数口径本波按「唯一包+漏洞 ID」核验 = 14，与历史「21 项」同集合不同展示（行明细含重复 fix-version 行），零新增依赖结论不变；⑥ 本地 `.env` APP_VERSION 残留 0.1.3 会使 p35/p37 双红——bump 流程须同步本地 env（不入库，已修）。
  - 文档链接：W6 已建 `docs/design/frontend-streaming-optimization.md`；本波无新文件入 §6 反引号缺口。

（每波完成后在此回写：实测数字、新增 flag、新增测试数、发现的偏差与调整。）
