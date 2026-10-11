# 性能与优化路线图（总纲）

- 日期：2026-10-04
- 定位：跨层**性能功能盘点**（§1，已实现、每条锚定真实代码）+ **可扩展优化点清单**（§2，新提议）+ **深入优化索引**（§3，指向分域深钻文档）+ 分期路线（§4）与守卫口径（§5）。
- 分工：分域方案仍在各自文档深耕，本文件**不重复其内容**，只做总纲与接入点索引——

| 想看什么 | 去哪 |
|---|---|
| 检索域深度优化（hybrid 召回、查询智能） | `docs/design/rag-optimization.md` |
| 记忆域深度优化（在库能力灰度接线） | `docs/design/memory-optimization.md` |
| 多智能体域深度优化（任务中心协作） | `docs/design/multiagent-optimization.md` |
| 预算/限流/熔断的运行时口径 | `docs/architecture/runtime-guardrails.md` |
| 配置键全景 | `docs/architecture/configuration.md` |

- 守卫：本文引用的仓库内路径受 `tests/test_p41_doc_consistency.py` 守卫（2026-10-04 起根目录当前态文档与说明书 docx 已并入扫描集）；**行号锚点**不被自动校验，随代码演进需人工复核——引用时以符号名为准、行号为参考。

**落地四原则**（每个优化点都要过一遍）：

1. **flag 灰度**：新能力默认关闭，或默认值即「串行/不限额」基线等价（`AGENT_TOOL_ROUND_CONCURRENCY=1`、`TOKEN_BUDGET_PER_REQUEST=0`、`LLM_MAX_CONCURRENCY=0` 是三种既有范式）；
2. **回退零成本**：关掉 flag 一步回到旧行为，不留双路径；
3. **行为等价先于速度**：先有回归测试与 golden，再谈快慢；
4. **新配置键五处同步**：`agentix/config/settings.py` / `agentix/config/selfcheck.py` / `.env.example` / `docs/architecture/configuration.md` / p41 反向清单（口径见 `docs/PROJECT_SPEC.md` 五处同步原则）。

---

## 1. 现有性能功能盘点（已实现 · 方法实现锚点）

### 1.1 LLM 调用层

| 功能 | 方法实现（代码锚点） | 配置键（默认值） | 关闭/回退行为 |
|---|---|---|---|
| 角色分级超时与重试 | 角色 profile 表 `agentix/application/agents/llm_factory.py`（:43-71，router 10s/1 次、tool 20s/2 次、summarizer/general 45s/2 次；构造入口 :284，支持逐调用覆盖 timeout/max_retries） | 无键（代码内 profile） | 参数即分级本身，无开关 |
| 全局 LLM 并发闸门 | `agentix/reliability/concurrency_limiter.py`（GlobalConcurrencyLimiter :44，`_max>0` 才建信号量；acquire :69 未启用零开销直通、排队超时转 LLMConcurrencyLimitError；build_llm_limiter :103 + 进程级单例 get_llm_limiter :117、测试用 reset :125） | `LLM_MAX_CONCURRENCY`（settings :114，默认 **0 = 不限**）、`LLM_CONCURRENCY_QUEUE_TIMEOUT`（:119，默认 5.0s） | 0 → 不建信号量，零开销 |
| 入口令牌桶限流 | `agentix/infrastructure/core/ratelimit.py`（进程级令牌桶单例）；挂载点 `agentix/main.py` :165 | `RATE_LIMIT_ENABLED`（:90，默认 **False**）、`RATE_LIMIT_GLOBAL_RPM`（:94，120）、`RATE_LIMIT_GLOBAL_BURST`（:98，240） | 默认关 → 依赖直通零开销 |
| LLM profile 内建重试 | `agentix/application/agents/llm_factory.py` :43-71；检索侧对应 `agentix/retrieval/usecases/chat_llm.py` :52（llm_max_retries=3） | 无键（profile/retrieval config） | 失败即失败，不重试则一次 |

### 1.2 编排层

| 功能 | 方法实现（代码锚点） | 配置键（默认值） | 关闭/回退行为 |
|---|---|---|---|
| 工具轮内并发 | 授权门 `agentix/tools/gateway/round_gate.py`（flag-on 时并发批才进 gather）；消费点 `agentix/application/services/agent_loop_adapter.py` :312、`agentix/retrieval/usecases/chat_agent_runner.py` :125；纯编排同款约束 `agentix/orchestration/orchestrator.py` :107-113 | `AGENT_TOOL_ROUND_CONCURRENCY`（:812，默认 **1 = 串行基线等价**） | 1 → 串行，与基线逐字节等价 |
| 工具级超时/重试 | 工具契约 `agentix/domain/tool/spec.py` :75（max_retries=2，仅 retryable=True 生效）；执行计次 `agentix/tools/gateway/executor.py` :293 | 每工具 spec 字段（timeout_ms / retryable / max_retries） | retryable=false → 单次即过 |
| 事件递送指数退避 | `agentix/agent/events/adapter.py` :113（deliver 失败按指数退避，超 max_retries 转 FAILED，不阻塞主链路） | 代码默认 max_retries=3 | 退避耗尽 → FAILED，主链路无感 |

### 1.3 上下文层

| 功能 | 方法实现（代码锚点） | 配置键（默认值） | 关闭/回退行为 |
|---|---|---|---|
| 请求级 token 预算台账 | `agentix/application/services/token_budget.py` :15（0 = 不限额仅计量）；消费 `agentix/application/services/chat_service.py` :227/:711 | `TOKEN_BUDGET_PER_REQUEST`（:476，默认 **0 = 不执行强制**） | 0 → 台账恒 None，零行为变化（`agentix/application/agents/llm_factory.py` :99 注释明示） |
| 每步上下文重建预算 | `agentix/application/services/agent_loop_adapter.py` :308；编排注释 `agentix/orchestration/orchestrator.py` :107 | `AGENT_CONTEXT_BUDGET_TOKENS`（:790，默认 **0 = one-shot 构建，基线等价**） | 0 → 一次性构建 |
| 自动压缩 + keep-last 阶梯 | `agentix/agent/context/compaction.py`（autocompact_enabled :91、阶梯压缩 :192、L4 入口 :242）；门在 `agentix/agent/context/engine.py` :175 | `CONTEXT_COMPACT_LADDER`（:235，默认 **False = 单次尝试**）与 `CONTEXT_AUTOCOMPACT` | ladder 关 → 单次压缩，失败不升级 |

### 1.4 检索层

| 功能 | 方法实现（代码锚点） | 配置键（默认值） | 关闭/回退行为 |
|---|---|---|---|
| embedding 持久缓存 | `agentix/retrieval/infra/embedding/cache.py`（cache_key :44、路径解析 :233、build :243、包装 :259）；接线 `agentix/retrieval/infra/embedding/client.py` :57 | `EMBEDDING_CACHE_ENABLED`（`agentix/retrieval/settings.py` :169，默认 **true**）、`EMBEDDING_CACHE_MAX_ENTRIES`（:171，200000）、`EMBEDDING_CACHE_PATH`（:170，空 = DATA_DIR） | 关 → 原样直连；初始化失败自动回退无缓存（cache.py :254） |
| 秩融合可切换 | `agentix/domain/policies/rank_fusion.py`（rrf_fuse :27、zscore_fuse :63）；消费 `agentix/retrieval/usecases/retrieval_hybrid.py` :193/:199、KB 本地 `agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/kb_nodes/local_search_node.py` :49 | `KB_LOCAL_FUSION_METHOD`（:1086，默认 rrf，可选 zscore） | 非法值回退 rrf |
| rerank 单端点 + 熔断降级 | `agentix/retrieval/infra/rag/rerank.py`（httpx 单端点 :23；breaker 接入 :43-61，走 `agentix/reliability/circuit_breaker.py` 注册表；熔断打开/探测期原序返回 :63-66） | RERANKER_TIMEOUT_SECONDS 与 breaker 注册参数 | 服务不可用 → 保留原召回顺序（降级非失败） |
| schema 双 TTL 缓存 | text2sql 侧 `agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/text2sql/schema_retrieval/node.py` :63/:98；Neo4j 单例 TTL 刷新（R-P0-4 防持旧 schema）`agentix/application/agents/kg_sub_graph/kg_neo4j_conn.py` :78/:96 | `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS`（:428，默认 **0 = 关**）、`NEO4J_SCHEMA_REFRESH_SECONDS`（:753，默认 300.0s） | text2sql 0 = 每次现查；Neo4j >0 才走 TTL |

### 1.5 负载帽与可观测

| 功能 | 方法实现（代码锚点） | 配置键（默认值） | 关闭/回退行为 |
|---|---|---|---|
| 工具结果行列帽 | 工具网关执行链截断 `agentix/tools/gateway/executor.py` | `TOOL_RESULT_MAX_ROWS`（:540，100 行）、`TOOL_RESULT_MAX_CHARS`（:543，8000 字符） | <=0 → 不限 |
| 大输出转存 artifact | 超阈值的工具/子图输出落 artifact store（观测 payload 不进主链路） | `ARTIFACT_INLINE_THRESHOLD`（:441，2000） | <=0 → 全量 inline（关转存） |
| 轨迹 payload 回放采集 | 脱敏后的 prompt/response/工具参数预览进轨迹 | `OBSERVABILITY_PAYLOAD_CAPTURE`（:191，默认 **False**） | 关 → 不采集，零 IO |
| 轨迹 sink 故障中立 | `agentix/application/services/trajectory.py` build_trajectory_sink :351（jsonl 无路径→内存回退、未知 kind→回退、任何异常→InMemoryTrajectorySink；**可观测设施不得影响主链路**） | `TRAJECTORY_SINK`（:198，memory）、`TRAJECTORY_PATH`（:202，空） | 默认 memory = 不落盘 |

---

## 2. 可扩展优化点（2026-10-04 新增清单）

> 每条含：**现状 → 方案与方法实现 → 可扩展方式 → 验收 → 风险与回退**。标「拟增」的键/文件是提案，落地时按四原则五处同步。

### P0（配置或小改即可，先做）

**E1 · 工具轮内并发灰度推广**（类型：纯配置）
- 现状：机制与授权门已齐（§1.2），默认 1 = 串行基线。
- 方案：对低风险工具集（只读检索/查询类）在联调与压测环境将 `AGENT_TOOL_ROUND_CONCURRENCY` 提到 2-4，观察 p95 与错误率后再动生产默认值。
- 可扩展方式：round_gate 的授权语义即扩展点——后续可按工具风险分级（高风险工具强制串行）细化。
- 验收：golden 离线评估零回归；同题串行/并发各跑一轮对比 p95。
- 风险与回退：并发共享副作用的工具（写库/发通知）须靠既有风险门拦住；回退 = 置回 1。

**E2 · schema 缓存 TTL 生产化**（类型：配置 + 小改）
- 现状：`TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` 默认 0（关），`NEO4J_SCHEMA_REFRESH_SECONDS` 默认 300s——两套 schema 缓存节奏不一致。
- 方案：生产将 text2sql TTL 对齐 300s；补一个**主动失效**入口（schema 变更后清缓存而非等 TTL），接入点 `agentix/application/agents/kg_sub_graph/agentic_rag_agents/components/text2sql/schema_retrieval/node.py`（拟增清缓存函数）。
- 可扩展方式：失效入口同时服务两套缓存（text2sql + Neo4j 单例），后续可挂 schema 变更事件。
- 验收：golden 零回归；「改表 → 主动清 → 立即可见」用例。
- 风险与回退：TTL 内读到旧 schema（最长 300s 窗口）——文档写明窗口与主动失效办法；回退 = TTL 置 0。

**E3 · HTTP 出口连接复用（连接池化）**（类型：代码 + 拟增 flag）
- 现状：rerank 与 Redis 侧每次调用新建客户端——`agentix/retrieval/infra/rag/rerank.py` :23 与 `agentix/application/services/redis_cache.py` :294 均为 `async with httpx.AsyncClient(...)` 现建现弃，TCP/TLS 握手每请求重复付。
- 方案：进程级懒加载单例 `AsyncClient`（保留各自 timeout），由 `agentix/main.py` lifespan 托管关闭；拟增 `HTTP_CLIENT_POOL_ENABLED`（默认 false 灰度）。
- 可扩展方式：单例工厂落在 `agentix/infrastructure/core/` 下，后续其它出口（webhook、外部搜索）统一挂同一工厂。
- 验收：既有 rerank/redis 测试全绿 + 新增「两次调用复用同一 client 实例」单测；开/关 flag 行为等价断言。
- 风险与回退：连接泄漏/跨事件循环 → lifespan 统一关；回退 = flag 置 false 走现建现弃。

### P1（需要新测试/新口径）

**E4 · embedding 缓存命中率观测与启动预热**（类型：代码）
- 现状：缓存默认开（§1.4）但**命中率不可见**，冷启动全量 miss。
- 方案：`agentix/retrieval/infra/embedding/cache.py` 内加 hit/miss 计数并入 `agentix/application/services/observability.py` 指标；可选启动预热清单（高频查询向量首刷）。拟增 `EMBEDDING_CACHE_METRICS_ENABLED`（默认 true，观测类默认开）。
- 可扩展方式：计数器走观测设施，后续语义缓存（E7）复用同一套 hit/miss 口径。
- 验收：单测断言计数；预热开关 off/on 行为等价。
- 风险与回退：计数本身零成本；预热失败仅告警。

**E5 · 最小延迟基准与门槛**（类型：基准设施）
- 现状：仓库**没有**专用性能基准（2026-10-04 实测无 perf/bench 测试文件）——速度类改动目前只有行为回归兜底，「更快」无法被证明。
- 方案：建最小计时 harness（拟建「tests/test_perf_smoke.py」：固定 3-5 个代表性调用计时，只断言数量级与不回退，不断言绝对值）+ 轨迹时长字段口径（复用 `evals/trajectory_regression.py` 的轨迹产物）。
- 可扩展方式：基准用例可逐个追加，门槛先「软」（报告不阻断）后「硬」（超阈值红）。
- 验收：harness 自身稳定（本地连跑 3 次方差可接受）。
- 风险与回退：CI 抖动 → 初期只在本地/夜间跑，不进门禁。

**E6 · prompt 静态前缀稳定化（提示缓存友好）**（类型：prompt 布局）
- 现状：prompt 已外置（`agentix/application/agents/lg_prompts.py` 与各子图 prompts），但未按「静态前缀在前、动态内容在后」的布局纪律组织，provider 侧前缀级 prompt cache 利用率低。
- 方案：梳理各角色 system prompt 为「不变段（角色/工具说明/规则） + 可变段（上下文/检索结果）」，静态段放最前且逐字稳定。
- 可扩展方式：布局纪律写进 prompt 评审清单，新 prompt 按同模板扩展。
- 验收：golden 零回归（prompt 重排是行为变更，必须过 `evals/run_eval.py` 的 `--offline` 模式）。
- 风险与回退：顺序敏感的 prompt（few-shot 位置）逐个评审；回退 = git revert 单文件。

### P2（深度/新能力，需设计评审）

**E7 · 语义结果缓存**（类型：新能力，拟增 flag）
- 现状：有 embedding 精确缓存（§1.4），无「问题语义相近 → 复用近期答案」能力；重复问法仍全链路重跑。
- 方案：答案缓存键 = 归一化问题 embedding 相似度 ≥ τ + 时间窗；接入点 `agentix/retrieval/usecases/retrieval_hybrid.py`（召回级）与 `agentix/retrieval/usecases/chat_service.py`（答案级）二选一先做召回级。拟增 `SEMANTIC_CACHE_ENABLED`（默认 false）、`SEMANTIC_CACHE_TAU`、`SEMANTIC_CACHE_TTL_SECONDS`。
- 可扩展方式：命中判定与存储解耦（存储可换内存/Redis），复用 E4 的 hit/miss 口径。
- 验收：相似问题命中用例 + 「不相似必须不命中」反例；golden 零回归；默认关下零行为变化。
- 风险与回退：陈旧/串答 → τ 保守（如 0.95）+ 短 TTL + 拒绝缓存高风险域（涉权限/时效）；回退 = flag 关。

**E8 · 出口连接池全景审计**（类型：调查 → 实施）
- 现状：已确认 rerank/Redis 两处现建现弃（E3）；pg / milvus / neo4j / 其余外部出口的客户端生命周期**尚未核验**，不排除同类问题。
- 方法实现：审计法——全仓搜客户端构造点（httpx/aiohttp/驱动 connect），逐个判定「单例/池化 vs 每请求新建」，产出表落入本文 §2 附录；坏点并入 E3 的工厂。
- 可扩展方式：审计结论表挂在本节，新增出口必须先过表。
- 验收：审计表覆盖全部出口清单；每个坏点有归属期号。
- 风险与回退：审计不改行为，零风险。

---

## 3. 深入优化（架构级 · 索引到分域深钻）

跨层盘点到此为止；真正「深」的优化在分域文档里已成体系，本节只做索引与接入点：

| 域 | 深钻文档 | 关键深入项 | 主要接入点 |
|---|---|---|---|
| 检索 | `docs/design/rag-optimization.md` | §4 P0 去重收敛 → §5 真 Hybrid（召回质变）→ §6 查询智能 | `agentix/retrieval/usecases/retrieval_hybrid.py`、KB 本地节点（§1.4） |
| 记忆 | `docs/design/memory-optimization.md` | §4 消双轨 → §5 灰度打开在库能力 → §6 智能化 | `agentix/agent/context/` 与 memory 读写路径 |
| 多智能体 | `docs/design/multiagent-optimization.md` | §4 单流收敛 → §5 任务 C 位 → §6 协作扩展 | 编排策略与 `agentix/tools/gateway/round_gate.py` 协同 |
| 上下文经济 | 本文件 §1.3 + §2 | 阶梯压缩深化、结构化压缩（逐段裁剪而非整体截断） | `agentix/agent/context/compaction.py` |
| 任务执行 | `docs/architecture/runtime-guardrails.md` | 任务级超时/重试预算与并发上限的口径统一 | `agentix/application/services/task_queue.py` |

---

## 4. 分期路线

| 期 | 项 | 类型 | 关键依赖 | 验收方式 | 状态（2026-10-06 Phase H 后） |
|---|---|---|---|---|---|
| P0 | E1 轮内并发灰度 | 配置 | 无 | golden + p95 对比 | 未动（机制既有，Phase H W5 多智能体波仅消费同一授权门） |
| P0 | E2 schema TTL 生产化 | 配置+小改 | 主动失效入口（拟增） | golden + 失效可见用例 | 未动（超出 Phase H 范围，仍待生产化） |
| P0 | E3 出口连接池化 | 代码+flag | E8 审计先行一小步（已定位两处） | 复用单测 + 行为等价 | **已落地（Phase H W3）**：`SHARED_HTTP_MAX_CONNECTIONS`（默认 0=关）+ `agentix/infrastructure/http/shared_client.py`，rerank/`redis_cache` 两处原现建现弃出口已接线，flag-off 逐位等价，`tests/test_p97_runtime_control.py` 守卫 |
| P1 | E4 缓存观测与预热 | 代码 | 观测设施 | 计数单测 | 计数面已具备（`record_cache_hit/miss("embedding")` 既有接线，`cache_hits_total{cache_type}` 可见）；启动预热仍未做 |
| P1 | E5 延迟基准 | 基准 | 轨迹时长字段 | 自身稳定 | **已落地（Phase H W0 + W7）**：`evals/performance/`（runner/queries/baseline.json/perf_gate）+ `tests/test_perf_smoke.py`+`test_p93`；W7 起 perf-smoke 为 CI **阻塞**门禁（原 continue-on-error 摘除），gate 阈值 p50×3 |
| P1 | E6 prompt 前缀稳定 | prompt | golden 基线 | `evals/run_eval.py --offline` | 部分：W1 新增 `prompt_prefix_fingerprint{builder}` 观测（静态模板角色 CRC32 指纹）——可观测已补，布局纪律整改未做 |
| P2 | E7 语义缓存 | 新能力 | E4 口径、τ/TTL 设计 | 命中/反例 + flag 等价 | 未动（Phase H 范围明确排除，见 PhaseH 规划 §2.2） |
| P2 | E8 出口池化审计 | 调查 | 无 | 审计表全覆盖 | 部分：W3 顺带核实并修复 rerank/redis_cache 两处；pg/milvus/neo4j 其余出口仍未逐一核验 |

依赖顺序总结：**E8 → E3 →（E1 ∥ E2 ∥ E6）→ E4 → E5 → E7**；分域深钻（§3 各文档）与本路线并行推进、互不阻塞。E3/E5 已由 Phase H 落地，链上剩余关键路径为 E8 →（E1 ∥ E2 ∥ E6）→ E4 → E7。

---

## 5. 验收与守卫（性能改动怎么算过）

1. **行为等价优先**：flag 默认关/基线值时，新增断言「开与关输出等价」的测试必须先存在；
2. **golden 回归**：涉及 prompt/检索/编排的改动，`evals/run_eval.py` 的 `--offline` 模式零回归（预测基线未录制时该步是大声跳过——绿灯只代表没跑，见 `evals/README.md`）；轨迹类回归看 `evals/trajectory_regression.py`；
3. **质量门禁不是速度门禁**：`evals/quality_gate.py` 管质量五指标；速度口径由 E5 基准补位（建成前，性能声明一律标注「测量日期 + 环境」，同 README §11 测试数的口径纪律）；
4. **配置键五处同步**：任何拟增 flag 落地时按四原则第 4 条走，并把键加进 p41 反向清单；
5. **路径防腐**：本文件与所有被引路径受 `tests/test_p41_doc_consistency.py` 守卫，改名/搬迁会直接红；
6. **诚实标注**：§2 中「拟增/拟建/尚未核验」字样在落地时必须逐个替换为真实锚点，不允许长期滞留。
