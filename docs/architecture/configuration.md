# 配置管理

My Agentix 的配置遵循「**单一事实来源 + 分层覆盖 + 启动自检**」三原则。

---

## 1. 单一事实来源

**所有运行时配置都定义在 `agentix/config/settings.py`**（`pydantic-settings` 的 `Settings` 类）。

```python
# 禁止在业务代码里散落 os.getenv
import os
key = os.getenv("OPENAI_API_KEY")        # ❌ 绕过集中配置

from agentix.config import settings
key = settings.OPENAI_API_KEY            # ✅
```

### 配置来源优先级（高 → 低）

```
1. 进程环境变量            （生产 / 容器注入）
2. .env 文件              （本地开发，由 model_config.env_file 指定）
3. Settings 类中的 default （代码内的合理默认值）
```

> `model_config`：`{"env_file": ".env", "case_sensitive": True, "extra": "ignore"}`
> —— 未知的环境变量会被忽略（不报错），便于共用宿主环境。

---

## 2. 配置分组

`settings.py` 按功能分组（新增配置项请归入相应分组）：

| 分组 | 代表配置 | 说明 |
|------|---------|------|
| **应用元信息** | `APP_NAME` `APP_VERSION` `DEBUG` | 版本号与 `pyproject.toml` 同步（`scripts/bump_version.py --check`） |
| **API** | `API_V1_PREFIX` `HOST` `PORT` | 服务监听 |
| **安全** | `SECRET_KEY` `API_KEY` `REQUIRE_API_KEY` `CORS_ORIGINS` | 见 [§4 敏感配置](#4-敏感配置与生产检查) |
| **LLM** | `LLM_PROVIDER` `LLM_MODEL` `LLM_API_KEY` `LLM_BASE_URL` `LLM_TIER_ROUTING_ENABLED` `LLM_ROLE_TIER_MAP` | 模型服务；`OPENAI_*` 为其兼容别名 |
| **成本** | `LLM_INPUT_PRICE_PER_1K` `LLM_OUTPUT_PRICE_PER_1K` | 默认 0.0（本地模型免费，不假装有价） |
| **Embedding** | `EMBEDDING_PROVIDER` `EMBEDDING_MODEL` `EMBEDDING_DIMENSION` | 检索向量化 |
| **知识库** | `KB_CHUNK_SIZE` `MILVUS_*` `KB_POSTGRES_*` `RERANK_*` | 检索链路 |
| **图谱** | `NEO4J_URI` `NEO4J_USER` `NEO4J_PASSWORD` | GraphRAG |
| **持久化** | `REDIS_URL` `CHECKPOINTER_TYPE` `DATABASE_URL` | 会话状态 |
| **Agent 行为** | `AGENT_TIMEOUT` `STREAM_IDLE_TIMEOUT` `AGENT_MAX_CONCURRENCY` `MAX_ITERATIONS` `MAX_HISTORY_MESSAGES` `AGENT_LOOP_ENABLED` `AGENT_MAX_REPLANS` `AGENT_CONTEXT_BUDGET_TOKENS` `TOOL_OUTPUT_BUDGET_TOKENS` `AGENT_LLM_PLANNER_ENABLED` `AGENT_TOOL_ROUND_CONCURRENCY` `HITL_IN_LOOP_ENABLED` `TASK_EXEC_ENABLED` `AGENT_ROLLBACK_ENABLED` `FAILURE_BOUNDARY_ENABLED` `CRON_ENABLED` `CRON_TICK_SECONDS` | 硬边界与执行支路 |
| **记忆** | `MEMORY_CONTEXT_ENABLED` `MEMORY_RECALL_LIMIT` `MEMORY_RECENCY_HALF_LIFE_DAYS` `MEMORY_FANOUT_ENABLED` `MEMORY_REVIEW_ENABLED` `MEMORY_TOOLS_ENABLED` `MEMORY_VECTOR_RECALL_ENABLED` `MEMORY_INDEX_ENABLED` `MEMORY_PROFILE_SCOPE_ENABLED` | 长期记忆 |
| **工具** | `DISABLED_TOOLS` `TOOL_SELECTION_ENABLED` `TOOL_SELECTION_MAX_RISK` `TOOL_DEFERRED_TOOLS` `TOOL_RESULT_CONTRACT_ENABLED` `TOOL_GATEWAY_ENFORCED` `TOOL_GUARDRAILS_ENABLED` | 工具集裁剪与工具面灰度（逗号分隔） |
| **特性开关** | `KG_FINAL_ANSWER_VALIDATION` `KG_PLANNER_SHADOW_ENABLED` `KG_PLANNER_UNIFIED_ENABLED` `KG_SCHEMA_AWARE_GENERATION_ENABLED` `EVIDENCE_POOL_ENABLED` `VERIFIER_GROUNDING_ENABLED` `LEGACY_ERROR_MODE` `TEXT2SQL_DRY_RUN_ENABLED` `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` `LATENCY_BREAKDOWN_ENABLED` `LATENCY_TOTAL_BUDGET_MS` `EVENT_LOOP_LAG_METRIC_ENABLED` `ROUTING_FASTPATH_ENABLED` `VERIFICATION_MAX_LLM_CALLS` `VERIFICATION_TIMEOUT_MS` `VERIFICATION_TOKEN_BUDGET` `NEO4J_COST_GUARD_ENABLED` `ADMISSION_CONTROL_ENABLED` `RETRY_BUDGET_ENABLED` `ADAPTIVE_TOOL_TIMEOUT_ENABLED` `LOAD_SHEDDING_ENABLED` `TRAJECTORY_ASYNC_WRITE_ENABLED` `SSE_COALESCE_ENABLED` `SSE_QUEUE_SIZE` `SUBAGENT_RESULT_CONTRACT_ENABLED` `MULTIAGENT_EARLY_EXIT_ENABLED` | 默认关，见 [§5 特性开关](#5-特性开关回滚机制) |

> 完整列表见 `.env.example`（每一项都有注释）。

---

## 3. 别名与向后兼容

部分配置有历史别名，使用 `AliasChoices` 兼容：

```python
LLM_MODEL: str = Field(
    default="gpt-4-turbo-preview",
    validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"),
)
```

同时提供**只读 property** 作为兼容视图：

```python
@property
def OPENAI_API_KEY(self) -> Optional[str]:
    return self.LLM_API_KEY
```

> ⚠️ 这些 property **只读**。在测试中 monkeypatch 时请改底层字段
> （`LLM_API_KEY`），而非 property（会抛 `AttributeError`）。

---

## 4. 敏感配置与生产检查

| 配置 | 生产要求 | 行为 |
|------|---------|------|
| `SECRET_KEY` | **必须显式配置** | 缺失时：dev 临时生成 + 强告警；**prod/staging 直接启动失败**（`resolved_secret_key` 抛 `RuntimeError`） |
| `API_KEY` | 必须显式配置 | 默认空串；`REQUIRE_API_KEY=True` 且为空时启动报错 |
| `REQUIRE_API_KEY` | 建议 `true` | 默认 `false`（warn 模式）——开启前请先确认调用方已带 `X-API-Key`；同时管辖机器面端点级依赖（events/ops 的 `verify_api_key`） |
| `V1_REQUIRE_JWT` | 按需 | 默认 `false`；`true` 时 v1 用户面端点（chat/sessions/upload 写操作）逐端点要求有效 Bearer JWT（`v1/deps.py`，只验签不查库）；与 `REQUIRE_API_KEY` 正交 |

### 启动期自检

`main.py` 的 `startup_event` 会：

1. 消费 `settings.resolved_secret_key`（触发生产检查）
2. 打印 `secret_key: explicit | temporary(dev only)`
3. 打印 `auth enabled: True/False`
4. 若 `REQUIRE_API_KEY=True` 但 `API_KEY` 为空 → 记录 error

```bash
# 生产环境变量缺失时的表现
SECRET_KEY= DEBUG=false python -m uvicorn agentix.main:application
# → RuntimeError: SECRET_KEY must be explicitly configured in production/staging.
```

### 密钥管理建议

- **不要**把 `.env` 提交到版本库（已在 `.gitignore`）
- 生产使用 KMS / Vault / 容器 secret 注入
- 轮换密钥后需重启服务（会话状态与密钥绑定）

---

## 5. 特性开关（回滚机制）

所有架构改造项都通过配置提供**关闭开关**，回滚**不改代码**：

| 开关 | 默认 | 作用 |
|------|------|------|
| `CHECKPOINTER_TYPE` | `redis` | 设 `memory` 回退进程内记忆（Redis 不可用时） |
| `LEGACY_ERROR_MODE` | `false` | `true` 恢复「吞异常返回 200」旧行为 |
| `KG_FINAL_ANSWER_VALIDATION` | `false` | KG 终态 LLM 校验（开启增加一次 LLM 调用） |
| `KG_PLANNER_SHADOW_ENABLED` | `false` | A3 图内规划影子对拍：KG 子图 planner 节点图内链继续驱动输出，统一 Planner 并行影子运行并落对拍报告（不一致/影子侧异常只记报告，绝不影响主链路输出）；报告先行，golden 回归零抖动才允许切流 |
| `KG_PLANNER_UNIFIED_ENABLED` | `false` | A3 图内规划切流：`true` = planner 节点改由统一 Planner（AgentLoop 同款单步规划，零 LLM）驱动任务分解，图内私有 LLM 链不再执行；与 `KG_PLANNER_SHADOW_ENABLED` 同开 = 切流驱动 + 图内链反向对拍（同样只记报告）；flag-off（默认）图内链逐位不变 |
| `MEMORY_CONTEXT_ENABLED` | `false` | 是否把长期记忆注入 general 问答 |
| `DISABLED_TOOLS` | 空 | 运行期裁剪工具集 |
| `MAX_HISTORY_MESSAGES` | `20` | 历史窗口上限；`<=0` 关闭窗口化 |
| `MEMORY_RECENCY_HALF_LIFE_DAYS` | `14.0` | 记忆时效半衰期；`<=0` 关闭衰减 |
| `OBSERVABILITY_PAYLOAD_CAPTURE` | `false` | 落 prompt/response/工具入参摘要（脱敏 + 截断）以支持完整回放 |
| `ARTIFACT_INLINE_THRESHOLD` | `2000` | 超过该字符数的大产物外置；`<=0` 永不外置（回旧行为） |
| `TOKEN_BUDGET_PER_REQUEST` | `0` | 请求级 token 硬上限；`0` 不限额（仅计量） |
| `TOKEN_BUDGET_PER_AGENT` | `0` | 子 Agent 预算切片；`0` 按已有子 Agent 数均分 |
| `TOOL_RESULT_MAX_ROWS` | `100` | 工具返回行数上限；`<=0` 不限 |
| `TOOL_RESULT_MAX_CHARS` | `8000` | 工具返回字符上限；`<=0` 不截断 |
| `HUMAN_REVIEW_ENABLED` | `false` | 高风险动作人工审批（**fail-closed**） |
| `COMPLETION_VALIDATOR_STRICT` | `false` | 终态校验是否把 `incomplete` 升级为 `degraded` |
| `AGENT_LOOP_ENABLED` | `false` | 非流式 ask 走 AgentLoop（L3）支路；stream 恒 L2 |
| `AGENT_MAX_REPLANS` | `0` | `AGENT_LOOP_ENABLED=true` 支路接入 `RetryReplanner` 的重规划上限；`0` = 不接 replanner（与基线逐位等价），`>0` = 失败步后最多重规划 N 次（连续同因熔断由 `same_failure_threshold` 把守） |
| `AGENT_LLM_PLANNER_ENABLED` | `false` | F1c·A-P1-2（LLM 规划 + TodoWrite）：AgentLoop 支路 planner 换 `LLMPlanner`（`with_structured_output` 结构化输出、按执行表白名单整步过滤、LLM 故障/空计划 fail-open 回 `SingleStepPlanner` 语义；`AGENT_MAX_REPLANS>0` 时同时注入 `RetryReplanner` 内层），并声明+装配 `todo_write`（整体替换、至多一条 `in_progress`、JSON 落盘可恢复；回执即读通道）。父闸 `AGENT_LOOP_ENABLED` |
| `AGENT_CONTEXT_BUDGET_TOKENS` | `0` | P3 渐进上下文（补「主循环一次性构建」缺口）：AgentLoop 每步按累计观测重建上下文的 token 预算；`0` = 只在启动时构建一次（trajectory=()、budget=0，与基线逐位等价），`>0` = 每步执行前重建 + 确定性裁剪（重建异常 ⇒ `context_build_failed` 终态不 hang）；值在组合根读 settings，`AgentLoop` 保持不 import config 的纯编排约束 |
| `AGENT_TOOL_ROUND_CONCURRENCY` | `1` | F1d·A-P1-3（并发工具轮）：L2 检索聊天 `_execute_tool_calls` 与 L3 `AgentLoop` 批路径共用的同轮并发度；`1`（默认）= 串行逐位等价，`>1` = ready 批发并发——派发前逐调用授权门（`HUMAN_REVIEW` 通道，未获批 fail-closed 落 error 不派发）、危险工具（有效风险 MEDIUM+ = L1+）强制串行、批宽按剩余步数预算裁剪、观测按声明序落位、每批至多一次 replan |
| `HITL_IN_LOOP_ENABLED` | `false` | F1e·A-P1-4（HITL 进环）：图内 `execute_task` 遇 `review_required` 挂起进图（langgraph `interrupt` + checkpointer，恢复值 `approve` ⇒ 落批准后重试一次，`reject`/未知值 fail-closed 快速失败）；`HUMAN_REVIEW` 开 + 无 approver + 无决策面 ⇒ 直接 REJECT(`no_approver_wired_fail_closed`，沿 sidecar 语义，不留无人批的 PENDING)；裁决顺序 deny→hook→ask强制→allow 契约锁定。仅 py3.12+ 注入（3.10 async interrupt 死锁 ⇒ 降级回 `review_required`）、非 stream ask 才声明进环意图（stream 恒 L2 不挂起）；父闸 `HUMAN_REVIEW_ENABLED`，只对图内任务链生效（L2/L3 维持 F1d 派发门 fail-closed） |
| `TASK_EXEC_ENABLED` | `false` | 任务型路由与任务执行管线（P1，岗位对照）：router 意图 `task-query`（flag-on 才追加提示词段/白名单）→ `execute_task` 节点（拆解 → `plan_waves` 波次并行 → `ToolExecutor` 治理执行 → `LayeredVerifier` 分层校验 → completed/failed/budget_exceeded/verify_failed 四终态）+ `append_task_note`/`write_task_artifact` 两个 side-effect 写工具与 `read_artifact` 只读取回工具（P3，写→读闭环）；flag-off 路由/提示词/工具声明/图可达性与基线等价 |
| `SKILLS_ENABLED` | `false` | Skills 渐进披露（Phase 21 + P6-6.3）：元数据目录注入静态前缀 + `read_skill` 惰性读正文 + `create_skill_draft` 经验沉淀草稿工具（写 `data/skills_drafts/`，隔离于 `skills/`，人工评审晋升后生效；同名拒绝覆盖）；flag-off 指令区与基线字节等价，**开启即换一次前缀** |
| `SKILLS_DIR` | `skills` | Skill 根目录（相对 CWD，可指向仓库外）；递归扫描其下 `SKILL.md` |
| `AGENTS_CASCADE_ENABLED` | `false` | F1b2·M-P1-4（AGENTS 级联）：指令分层升级为全局（`~/.agentix/AGENTS.md`，常驻）→ 项目根（常驻）→ 子目录（消息触达才加载）三层，hash 去重与单 4k/总 12k 预算沿既有机制；配套 `read_agents_file` 按需取回工具（L0 只读，向上查找最近约定文件，项目边界内）；flag-off `build_instructions` 输出与基线逐字节一致 |
| `LG_INSTRUCTIONS_ENABLED` | `false` | 上下文工程 B（指令分层接入 L2 生产链路）：把级联指令块（AGENTS.md/CLAUDE.md，经 `build_instructions` 组装）注入 general-query 的 system 消息，顺序 = 静态 system 常量 → 指令块 → memory_context → 历史；块内容经 chat_service 进程级**指纹缓存**（文件 stat / 触达目录 / 相关 flag 任一变化才重读盘，读盘经 `asyncio.to_thread`），故障 fail-open 返回空串；flag-off 不写 `configurable["instructions"]`，config 与基线逐位等价 |
| `LG_TOKEN_HISTORY_BUDGET` | `0` | 上下文工程 C（token 级历史窗口）：`>0` 时 message_window 在条数窗口（`MAX_HISTORY_MESSAGES`）之内再按 token 预算从最新消息**向前一次性累加** `count_tokens`，累到预算即停（每条只计一次，无逐条重算的 O(n²)），至少保留最新一条；`0`（默认）= 该分支完全不执行，与条数窗口逐位等价 |
| `LG_STATUS_TAIL_ENABLED` | `false` | 上下文工程 D（状态尾注，Manus 式）：把「对话轮次 / 历史条数 / 预算余量（`BudgetLedger` 快照，仅非流式 ask 有 ledger 时）」追加到**本轮 human 消息尾部**（`model_copy` 副本，瞬态只进本次 ainvoke，不写回 checkpointer 历史、不进静态前缀 ⇒ 不打穿 KV Cache）；flag-off 不追加任何内容 |
| `TOOL_SELECTION_ENABLED` | `false` | P2 双层闸 L1：把 `tool_policy` 注入图 `configurable`（tool_selection 节点声明级过滤链的注入方，原先有读取点无注入方）；flag-off 不注入该键，config 与基线逐位等价 |
| `TOOL_SELECTION_MAX_RISK` | `L1` | 注入策略的声明级风险上限（L0–L3）；只影响 LLM **可见面**，执行层 `ToolExecutor` 的参数感知有效级判定不受影响 |
| `TOOL_DEFERRED_TOOLS` | 空 | 逗号分隔的延迟工具名：非空时退出常驻 `get_active_tools()`（注册表 spec 保留），并把 `tool_search` 召回工具挂进声明清单（核心常驻、长尾按需召回）；空 = 不延迟不声明，声明清单与基线逐位等价 |
| `TOOL_RESULT_CONTRACT_ENABLED` | `false` | 返回值类型契约：handler 成功返回值必须 JSON 可序列化，否则执行点立即落 failed（契约违规不重试）；flag-off 沿用 data 原样透传（与基线逐位等价） |
| `TOOL_GATEWAY_ENFORCED` | `false` | A2b Tool Gateway 收编闸：`true` = 五项署名旁路（`search`/`cypher_query`/`predefined_cypher`/`microsoft_graphrag_query`/`text2sql_query`）的节点执行缝改经 `ToolExecutor` 治理链（有效风险→权限→HITL→超时→重试→轨迹）；flag-off（默认）节点直连，与基线逐位等价 |
| `HOOKS_ENABLED` | `false` | 生命周期 hooks（Phase 21：on_tool_before/after、on_step_end、on_run_end、on_error）；默认关 = 组合根零接线 |
| `CONTEXT_AUTOCOMPACT` | `false` | L4 语义压缩（Phase 22，Ch2 §2.7 分层压缩最后一档）：L1–L3 后仍超预算时压缩轨迹；五要素机制断言失败 ⇒ 丢弃回退 Phase 18；默认关 |
| `CONTEXT_COMPACT_LADDER` | `false` | P3 渐进压缩重试梯（借鉴 claw-code）：`CONTEXT_AUTOCOMPACT=true` 时把 L4 单次尝试升级为 keep-last 窗口阶梯 4→2→1→0（每轮只压缩头部、尾部原样保留；空输出/五要素被拒换更小窗口再试；压缩器异常立即回退；四轮耗尽 ⇒ 丢弃循环）；`false` = Phase 22 单次语义逐字节不变 |
| `CONTEXT_PRECOMPACT_REINJECT_ENABLED` | `false` | F2a·M-P2-2（memory-optimization §6 PreCompact 重注）：压缩/丢弃循环触发**之前**把关键规则（静态前缀规则行，按声明序截断）与用户硬约束 token（五要素 `constraint_key`，从原轨迹采集去重）重注到上下文尾部高注意力区——行带 `[precompact:<source>]` 审计标记、snapshot meta 记来源计数（规则来源可审计）；挂 `CONTEXT_AUTOCOMPACT` 通道；默认关 ⇒ 组装与基线逐字节等价 |
| `CONTEXT_REAL_TOKEN_COUNT_ENABLED` | `false` | F1b·M-P1-5（真分词/真计数）：token 估算换 LLM 侧 tokenizer（tiktoken cl100k_base，进程内懒加载一次，不新增服务）；flag-off ≡ `len//4` 与全部既有估点逐字节一致，flag-on 且 tokenizer 缺失/失败自动退 CJK 感知启发式（永不打断上下文构建）；接线点 = context 的 engine/simple/compaction 共 7 处估点 |
| `CONTEXT_ISOLATE_PERSIST_ENABLED` | `false` | F1b2·M-P1-5（obs:// 接线）：`ContextEngine` 超长观测隔离时把正文真正外置 ArtifactStore（`offload_content`，强制外置），`isolated_refs` 落 `obs://{artifact_id}`——经 `read_artifact` 可取回（与记忆索引指针共用同一条取回通道）；默认关 ⇒ 隔离引用与基线逐位一致（仍是 `obs://{step_id}` 裸引用）；外置失败 fail-open 回裸引用 |
| `EVENT_PORT_BACKEND` | `memory` | EventPort 后端：`memory`（进程内）/ `jsonl`（append-only 落盘 `events/event_log.jsonl`，重启恢复 + 跨重启 event_key 去重）/ `redis_stream`（Phase 23：Redis Stream，XADD/XREAD，需 Redis 运行时，无 Redis 诚实 skip） |
| `NOTIFY_USER_ENABLED` | `false` | `notify_user` 出站通知工具（Phase 23，Ch6 §6.2）：本批落 EventPort（type=`user_notification`）+ `GET /api/v1/ops/events` 可查，不接真实邮件/短信；MEDIUM 风险走 HITL 闸 |
| `MEMORY_CONSOLIDATION_ENABLED` | `false` | 记忆整理触发 handler（Phase 24，Ch3 遗忘生产化）：task_queue `memory_consolidation` 才执行 consolidate（幂等时间窗 + dry_run）；flag-off 直接 skipped 零存储触碰 |
| `AGENTIC_RAG_ENABLED` | `false` | Agentic RAG（Phase 24，Ch3）：知识分支空检索时查询改写重试 ≤2 次后诚实声明；每次尝试进 trajectory；默认关 = 与基线行为等价 |
| `CLARIFY_ROUTING_ENABLED` | `false` | 需求澄清决策（Phase 28，Ch9 §9.3.2）：歧义输入（词表法域冲突/主体缺失/口语过短 + 路由 confidence 低）先返回结构化澄清应答再执行；零新增 LLM；不入 HITL、不计失败、不改路由终态；会话态 `clarify_armed` 单轮约束；默认关 = handler 入口与基线字节等价 |
| `BUDGET_NOTICE_ENABLED` | `false` | 预算意识（Phase 28，Ch10 §10.6）：状态栏常量键 `max_iter`（构造侧写入，前缀稳定）+ 剩余 ≤2 步时轨迹 append `budget_notice`（append-only；动态剩余量拒绝进状态栏——KV Cache 纪律）；不改 `budget_exceeded`/`MAX_ITERATIONS` 终止语义；默认关 |
| `EVOLUTION_PROPOSAL_LOG` | `data/evolution/proposals.jsonl` | 进化提案档案（Phase 30，Ch9 §9.8.2）：`OfflineEvolutionLoop` 经 `jsonl_proposal_sink` 把 staged/applied/rejected 三状态 append-only 落盘（`content_hash` 去重、保留失败）；**不落 `evals/baselines/`**（与 `quality.json` 碰撞，sink 构造时 fail-closed 拒绝） |
| `EVOLUTION_SLEEP_ENABLED` | `false` | 睡眠学习触发 handler（Phase 30，Ch9 §9.9）：task_queue `evolution_sleep` 门槛达标（近窗信号数 ≥ N 且错误率 ≥ T）→ LLM 合成提案（端点预检 fail-closed，无端点 skipped 非伪造）→ **只产 staged 提案、不晋升**；flag-off 直接 skipped 零副作用 |
| `MEMORY_PROFILE_CARD_ENABLED` | `false` | 结构化画像卡 `profile_card`（Phase 29，Ch3 §3.5/§3.6 JSON Cards + User as Code）：`UserProfile.to_dict()` 只增 `profile_card` 键（schema_version/legal_domain_prefs/usage_habits/user_constraints/conflicts），`derive_profile` 从会话事件日志确定性重派生（memory_consolidation 触发、读路径不隐式重放）；偏好冲突**只标记不覆写**；默认关 = 序列化与基线字节等价（与 R3-8 语义卡片 `MEMORY_CARDS_ENABLED` 为不同特性） |
| `STREAM_IDLE_TIMEOUT` | `60` | 流式单事件间隔看门狗（批次 B1，audit-2026-09-28 §2 B1）：`astream_events.__anext__()` 超过该秒数无新事件 ⇒ 按既有 error+done 兜底契约收口并归还并发槽位；与整流墙钟 `AGENT_TIMEOUT` 解耦（覆盖工具执行/检索等非 LLM 节点静默挂起） |
| `MEMORY_CONSOLIDATION_MAX_GAP_DAYS` | `7.0` | 记忆整理的时间相邻阈值；`<=0` 只按主题分组 |
| `CONTROL_PLANE_MAX_CONCURRENCY` | `0` | 控制平面准入：同时非终态运行数上限（Phase 25）；超限 `spawn` 抛 `AdmissionDenied`（fail-closed）；`0` = 不限（默认 = 接线前行为） |
| `CONTROL_PLANE_MAX_DEPTH` | `0` | 控制平面准入：委托深度上限（Phase 25）；`depth` 超限拒入；`0` = 不限 |
| `MULTIAGENT_PEER_MAX_STEPS` | `10` | Peer 独立预算（Phase 25，Ch10 §10.17 反预算失控）：经 `lg_supervisor` budget_factory 注入 Manager；镜像 `PeerBudget.max_steps` 默认值（两处相等有测试断言） |
| `MULTIAGENT_PEER_MAX_MESSAGES` | `20` | Peer 独立预算：单 Peer 消息数上限；镜像 `PeerBudget.max_messages` 默认值 |
| `MULTIAGENT_BUS_ENABLED` | `false` | 进程内 asyncio 消息总线（Phase 25，书 §10.3 Message Bus）：Peer 消息经 bus 收发 + peer 粒度取消；默认关 = 点对点直连现行为逐位等价 |
| `MULTIAGENT_SUBAGENT_ISOLATION_ENABLED` | `false` | 子代理隔离三件套（Stage F2c·A-P2-1，multiagent-optimization §6）：flag-on ⇒ 子代理只见任务面 prompt（ephemeral，父对话轨迹不进）、父记忆活上下文从 config 摘除（skip_memory，唯一记忆入口 = `MemorySnapshot` 只读拷贝，Mapping 深拷贝后 `MappingProxyType` 冻结）、每 actor 独立 `SubAgentSession`（session_id + 独立 `PeerBudget` 经 Manager `budgets` 单源注入）+ 心跳保活 + live registry（kill 终态短路不执行内层 / pause 有界等待超时 fail-open / steer 指令一次性注入下轮 context）；默认关 = supervisor 现行为逐位等价 |
| `SUBAGENT_RESULT_CONTRACT_ENABLED` | `false` | Phase H·W5（PhaseH 规划 §4）：Peer 结果进黑板换结构化契约面 `{status, claims[], evidence_refs[], artifact_refs[], confidence, cost{}}`（`multi_agent/contract.py::SubAgentResult`）——claims 为有界要点（5 行 × 200 字符）、`meta["evidence"]` 逐条经 `offload_content` 外置成 `obs://` 句柄、契约面过 `max_result_chars` 整体外置可取回；完整输出仍在 `ManagerOutcome.peer_results` 不丢信息；默认关 = 黑板仍走全文 `bound_result_face` 逐位等价 |
| `MULTIAGENT_EARLY_EXIT_ENABLED` | `false` | Phase H·W5：proposer 结果**带证据**（`meta["evidence"]` 或输出内嵌 `obs://`）且相对入板前黑板**有信息增量**（复用既有 `information_increment` 判据，两条件缺一不可）⇒ 提前收敛（跳过 reviewer 调用与后续轮次，`exit_reason=completed`）；顺序拓扑下「停其余 peer」= 跳过调用，无在途消息需取消；默认关 = 每 peer 照常调用逐位等价 |
| `TOOL_GUARDRAILS_ENABLED` | `false` | P4 异常闭环 4.1/4.2（借鉴 hermes `tool_guardrails.py`）：工具失败三计数器分级（同参连续失败 2/3/5 → warn/block、工具总失败 2/5/10 → warn/halt、同结果幂等 2 次 → warn）+ 恢复建议经 metadata/`task_retry` 终局/`RetryReplanner` rationale 回喂 + 重复结果字节级折叠 stub（第 2 次重复起，轮询不豁免）；flag-off 检查/记账/折叠零调用（与基线逐位等价） |
| `AGENT_ROLLBACK_ENABLED` | `false` | P4 异常闭环 4.3（借鉴 claw-code 计划态）：`RollbackPort` 真实现（`InMemoryRollbackStore`，deepcopy 隔离 + per-run LRU + 缺失恢复抛异常）+ `enter_plan_mode`/`exit_plan_mode` 工具（enter 快照 `executor.policy` → 收紧 L0 只读、exit 按 checkpoint 恢复原策略；幂等不二次快照）；flag-off 不声明不装配（与基线逐位等价） |
| `FAILURE_BOUNDARY_ENABLED` | `false` | P4 异常闭环 4.4（借鉴 hermes 失败 turn durable 边界）：ask 抛 HTTP 前 / 流式 error 帧发布前**先持久化**一条 assistant 边界消息（`[对话边界] 本轮请求失败（{code}）：…`），消除失败 turn 的 user-user 相邻；`context_overflow` 类显式排除；flag-off 不补写（与基线逐位等价） |
| `CRON_ENABLED` | `false` | P5 Proactive 5.1/5.2（借鉴 hermes `cron/scheduler.py` 四件套）：后台 tick 循环（每 `CRON_TICK_SECONDS` 跑一轮 `CronScheduler.tick`：代码版本让位 → 卡死探测（`CRON_STALE_MS` 窗口）→ **先推进 next_run_at 再执行**（崩溃不 tight-loop）→ inflight 簿记防双跑 → 交付账本重投）+ `cron_create`/`cron_list`/`cron_delete`/`cron_run` 工具挂进声明清单（L1 → `HUMAN_REVIEW_ENABLED` 时人工审批半；不提供任意 shell——任务仅 `agent` 提示词或已注册工具两型）；flag-off 不启动循环不注册工具不建 `data/cron/jobs.json`（与基线逐项一致） |
| `CRON_TICK_SECONDS` | `60` | P5 Proactive：cron 调度 tick 间隔秒数（≥1，仅 `CRON_ENABLED=true` 时使用）；单任务执行超时 `CRON_JOB_TIMEOUT_S=120s` 与卡死判定 `CRON_STALE_MS=600000ms` 为代码内常量不进配置面 |
| `MEMORY_FANOUT_ENABLED` | `false` | P6-6.1（source-code-study §5 表 6.1，借鉴 hermes MemoryManager 多 provider 扇出）：`MemoryManager` 把 `MemoryPort` recall/remember 扇出到 Redis LTM 适配器（独立键空间 `{prefix}:port:{subject}`，scope 隔离 + PII 脱敏 + 确定性 id 幂等覆盖）+ 进程内 provider——每 provider 独立超时、失败跳过（recall fail-open 永不抛，全败返回 `[]`；remember 全败才抛）；挂在 `MEMORY_CONTEXT_ENABLED` 通道之下，flag-on 才在 chat 记忆上下文后追加「长期记忆片段」段落；flag-off 读路径与基线逐位等价 |
| `MEMORY_REVIEW_ENABLED` | `false` | P6-6.2（§5 表 6.2，借鉴 §3.5 后台评审线程）：`conversation_summary` handler 在摘要落库后把抽出的稳定语义卡片**异步**（fire-and-forget，done-callback 只记日志）经纯规则路由晋升为长期画像条目——`kind=user` 或 SEMANTIC + 置信 ≥0.6 + 标签 preference/instruction 进画像，低置信/情节/过程性条目不晋升（reason 诚实进报告）；flag-off handler 报告与基线完全一致 |
| `MEMORY_TOOLS_ENABLED` | `false` | A2c 记忆三工具：`remember`/`recall`/`forget` 进声明清单（`tools/adapters/memory_tools.py`）并在组合根安装 handler（`memory_tools_bridge`，经 `ToolExecutor` 治理链执行；remember/forget=L1 side-effect）；`forget` 经 `MemoryManager.forget` duck-typed 扇出到声明了 forget 的 provider（`MemoryPort` Protocol 不扩容）；flag-off 不声明不注册，声明清单与基线逐项一致 |
| `MEMORY_VECTOR_RECALL_ENABLED` | `false` | F1b·M-P1-1（向量召回）：进程内记忆 provider 的 `recall` 升级为关键词 × 向量双路 + recency 衰减（半衰期沿 `MEMORY_RECENCY_HALF_LIFE_DAYS`，TopK 沿 `MEMORY_RECALL_LIMIT`）；embedding 复用检索域向量栈（`embed_texts_sync`，不另建框架），embedding 失败/超预算 fail-open 回纯关键词路径（断向量服务仍可答）；flag-off 召回与基线逐位等价 |
| `MEMORY_INDEX_ENABLED` | `false` | F1b2·M-P1-3（MEMORY 索引制）：记忆注入段升级为「索引 + `obs://` 指针」——正文经 `offload_content` 外置 ArtifactStore（kind=`memory_topic`），入口只留 `- 摘要 → obs://{artifact_id}` 索引行（常驻预算封顶 `MEMORY_INDEX_MAX_CHARS=1500`，超限截索引尾部），取回走 `read_artifact`（`obs://` 前缀自动剥离）；挂在 `MEMORY_CONTEXT_ENABLED` 通道之下，外置降级时该行保留原文（fail-open 不丢内容）；flag-off 注入与基线逐位等价 |
| `MEMORY_PROFILE_SCOPE_ENABLED` | `false` | F2a·M-P2-4（memory-optimization §6 画像作用域）：画像字段分级（stable vs session，`profile_scope.PROFILE_FIELD_TIERS` 单源，未知字段 fail-closed 按 stable）+ 跨会话沿用授权边界——flag-on 时稳定层（`user_constraints`/`legal_domain_prefs`/`conflicts`、频繁领域、语义卡）仅在 `/ops/memory/profile-scope` 授权后注入，未授权/读取异常 fail-closed 只注会话层；配套 `POST /ops/memory/clear-user` 一键清除画像+语义卡+摘要全链路；默认关 ⇒ 注入与基线逐位等价 |
| `NEO4J_SCHEMA_REFRESH_SECONDS` | `300` | Neo4j 单例 schema 周期刷新（R-P0-4 图谱硬化）：`get_neo4j_graph()` 到期后在锁内 `refresh_schema()`——刷新失败**保留旧 schema + warning**（不打断请求，按 TTL 重试）；`<=0` = 仅首连刷一次（接线前行为） |
| `NEO4J_QUERY_TIMEOUT_SECONDS` | `0` | Cypher 执行只读闸 + 超时（R-P0-4）：五处执行缝经 `query_readonly()` 前置 `WRITE_CLAUSES` 只读校验（命中写子句 fail-closed 拒绝执行，不触库）；该键 `>0` 时经 wait_for 限时执行（超时抛错进既有失败分支），`0` = 不限时（与基线逐位等价） |
| `KB_LOCAL_FUSION_ENABLED` | `false` | R-P1-2 Channel B（F1a）：kb local_search 双路跨通道 rank 融合，替代「postgres 非空即丢掉 milvus」的丢分逻辑；默认关 = 旧行为逐位不变 |
| `KB_LOCAL_FUSION_METHOD` | `rrf` | Channel B 融合算法：`rrf`（免调参）\| `zscore`（两通道 z 归一后各 0.5 加权）；检索主链 Channel A 的同名旋钮见 `.env.example` 注释示例（`RETRIEVAL_FUSION_METHOD`，retrieval 域专属键不入本表） |
| `TEXT2SQL_DRY_RUN_ENABLED` | `false` | F2b-c·R-P2-4（rag-optimization §6 Text2SQL 加固）：`sql_validation` 节点对词法通过的 SQL 跑 `EXPLAIN` 试跑（先例 = Cypher `EXPLAIN` 试跑），失败喂既有重试环；基础设施故障 fail-open 不误杀合法查询；默认关 = 校验行为与基线逐位等价 |
| `TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS` | `0` | F2b-c·R-P2-4：按 `(DATABASE_URL, 归一问题)` 缓存 INFORMATION_SCHEMA 拉取（`schema_retrieval` 节点，失败不入缓存，128 条上限过期剪除），打点 `record_cache_hit/miss("text2sql_schema")`；`0` = 关闭（每问现拉，与基线逐位等价） |
| `KG_SCHEMA_AWARE_GENERATION_ENABLED` | `false` | F2b-d·R-P2-3（rag-optimization §6 Graph 动态化）：text2cypher 生成提示 `{schema}` 改用与校验同源的「法律域文档摘要 + 发现态 `graph.schema`」渲染（`components/utils/utils.py::resolve_generation_schema`，替代裸格式/静态中文标签为权威源；孪生链静态冗余块同步置空）；flag-on 渲染异常 fail-open 回裸 `graph.schema`；flag-off = 基线裸 `graph.schema` 逐位等价 |
| `EVIDENCE_POOL_ENABLED` | `false` | F2b-d·R-P2-3：任务管线成功执行结果统一进观测 `meta["evidence"]`（`content` ≤4000 字符 + `source`，取去重 stub 前的原始正文），供分层验证器 L2 `sources` / L3 grounding 消费；flag-off = meta 无 `evidence` 键与基线逐位等价（L3 维持无证据 fail-open） |
| `VERIFIER_GROUNDING_ENABLED` | `false` | F2b-d·R-P2-3：分层验证器 L3 grounding 总闸——`task_execution` 与 `agent_loop_adapter` 两处生产调用点原硬编码 `require_grounding=False`，改读本键；flag-off = 基线逐位等价；与 `EVIDENCE_POOL_ENABLED` 同开 = 图/工具执行结果真正进 L3 支撑度核验 |
| `LATENCY_BREAKDOWN_ENABLED` | `false` | Phase H·W0（PhaseH_高性能运行时优化规划 §4）：SSE 终帧（finished/done/error）增量注入 `breakdown_ms`（ttft / retrieval 四阶段和 / stream），前端耗时芯片与回放消费；默认关 ⇒ 帧与基线逐位等价（p65 守卫通过） |
| `LATENCY_TOTAL_BUDGET_MS` | `0` | Phase H·W0：端到端流延迟总预算（毫秒）。`0` = 不限额；`>0` 超限仅 warning + `record_degraded("latency_budget")` 计数——fail-open，绝不中断响应、绝不重试 |
| `EVENT_LOOP_LAG_METRIC_ENABLED` | `false` | Phase H·W0：事件循环调度滞后后台采样（0.5s 间隔 → `event_loop_lag_seconds` 直方图）；默认关 = 不起采样任务，零开销 |
| `ROUTING_FASTPATH_ENABLED` | `false` | Phase H·W1（PhaseH_高性能运行时优化规划 §4）：复杂度感知 fast-path——高置信确定性输入（寒暄、单条法条直取）前置短路，跳过 LLM 意图分类器（零模型构造、零分类调用，命中记 `llm_calls_saved_total`）；默认关 = 路由链与基线逐位等价 |
| `LLM_TIER_ROUTING_ENABLED` | `false` | Phase H·W1：角色→档位接线——`LLM_ROLE_TIER_MAP`（逗号分隔 `role=tier`，tier ∈ fast/standard/reasoning）命中时经既有 `ModelGateway.resolve_model` 取该档模型；关/空表/未知角色或档位 ⇒ profile 与基线逐位等价 |
| `LLM_ROLE_TIER_MAP` | 空 | 配 `LLM_TIER_ROUTING_ENABLED` 使用的角色→档位映射（如 `router=fast,planner=reasoning`）；空 = 全回退 `LLM_MODEL`（单模型部署零行为变化） |
| `LLM_FALLBACK_MODEL_SPECS` | 空 | P1-1 跨端点 fallback：逗号分隔的 `model\|base_url\|api_key_env` 三元组（`api_key_env` 是环境变量名，密钥不落配置值、不与主 `LLM_API_KEY` 复用；`base_url`/`api_key_env` 段可空 = 仅换模型名回退主端点）。非空时**覆盖** `LLM_FALLBACK_MODELS`（旧键语义零变化）；两键皆空 = 无 fallback。生效面诚实边界：fallback 只接线在检索域 `query_knowledge_base` 工具链路，编排域不消费 |
| `FEEDBACK_ENABLED` | `false` | P2-1 满意度反馈闭环：`true` 才注册 `POST /api/v1/feedback`（用户面鉴权 + 会话归属校验跨用户 404，事件落 EventPort `message_feedback`，指标 `feedback_total{rating}`）；flag-off 路由不注册 = 与基线等价 |
| `FEEDBACK_PII_REDACT` | `true` | 反馈 `reason` 落盘前 PII 脱敏（复用 `agentix.agent.memory.service.redact_pii`；脱敏是安全项，**开为安全默认**） |
| `VERIFICATION_MAX_LLM_CALLS` | `1` | Phase H·W1 验证预算：KG 答案校验的 LLM 调用次数硬上限；`0` = 跳过校验，超限 ⇒ fail-open 降级放行（与既有校验失败降级同语义，steps 留痕） |
| `VERIFICATION_TIMEOUT_MS` | `1000` | Phase H·W1 验证预算：校验 LLM 调用超时（毫秒）；超时 ⇒ fail-open 降级放行，绝不扣下答案 |
| `VERIFICATION_TOKEN_BUDGET` | `0` | Phase H·W1 验证预算：校验累计 token 预算；`0` = 不限额，累计超限 ⇒ fail-open 降级放行 |
| `TOOL_OUTPUT_BUDGET_TOKENS` | `0` | Phase H·W1：`ContextPort.build`（`ContextEngine` + `SimpleContextBuilder`）轨迹段（工具/检索观测）的 token 段级配额；`0` = 不设段配额（与基线逐位等价），`>0` = 累计超配额的观测**整体外置**为 `obs://` 可取回引用（复用既有 `ArtifactStore` offload 通道）而非截断丢数据 |
| `NEO4J_COST_GUARD_ENABLED` | `false` | Phase H·W2（PhaseH_高性能运行时优化规划 §4）：Cypher 成本护栏——`query_readonly()` 只读闸之后跑 `GraphCostEstimator` 纯字符串预检：`RETURN *` / 无界变长遍历（`*..`、`*2..`、`[r*]`、`[:REL*]`）/ `OPTIONAL MATCH` 堆叠（>4 层）→ 拒绝（`ValueError` fail-closed 不触库）；简单单 `RETURN` 尾无 `LIMIT` → 追加 `LIMIT 1000`（UNION/多 RETURN/分号结尾不改写）；默认关 = 零预检零改写，与基线逐位等价 |
| `MILVUS_NPROBE` | `16` | Phase H·W2：Milvus IVF 搜索探针数（`milvus_impl.similarity_search` 原硬编码 16 配置化）；`>=1`（越界回落下限 1），默认值与基线逐位等价；调大召回↑耗时↑ |
| `ADMISSION_CONTROL_ENABLED` | `false` | Phase H·W3（PhaseH_高性能运行时优化规划 §4）：运行准入三态——`ControlPlane.spawn` 前 `admission_wait()`（放行 / 排队等位 / 拒绝抛 `AdmissionDenied` → HTTP 429）；并发账本复用平面 `active_count()`（终态自动释放）；默认关 = 直连 spawn 与基线逐位等价 |
| `ADMISSION_MAX_CONCURRENT_RUNS` | `0` | Phase H·W3：准入全局并发上限；`0` = 回落 `CONTROL_PLANE_MAX_CONCURRENCY`（两者皆 `0` = 不限）；`>0` 且到顶时按队列策略排队或拒绝 |
| `ADMISSION_MAX_QUEUE` | `0` | Phase H·W3：准入排队深度；`0` = 超限即拒；`>0` = 入队等位（等位上限 30s 模块常量，超时仍拒），等位深度同时是负载阶梯输入 |
| `RETRY_BUDGET_ENABLED` | `false` | Phase H·W3：run 级共享重试预算——task/tool/retrieval 三层重试前扣同一 token 桶（防 3×3×3 放大），并给工具/检索层补可重试性分类审计闸（仅 timeout/429/503/reset 等瞬时错误可重试）；默认关 = 各层现行为逐位等价 |
| `RETRY_BUDGET_TOKENS` | `6` | Phase H·W3：`RETRY_BUDGET_ENABLED` 开启时每 run 的重试 token 总额（三层共用，耗尽即停；首 attempt 不占额度） |
| `ADAPTIVE_TOOL_TIMEOUT_ENABLED` | `false` | Phase H·W3：工具超时 = `min(spec.timeout_ms, 剩余 run 预算)`（deadline 经 `RequestScope` 跨 gather 子任务传播，预算耗尽 → 1ms 近速失败）；默认关 = spec 原值，超时文案字节等价 |
| `LOAD_SHEDDING_ENABLED` | `false` | Phase H·W3：七级降级阶梯（L0 全量 … L6 礼貌拒绝）——输入 event-loop lag / 准入队列深度 / 下游熔断态，L1 起甩 evolution 后台，L6 经准入合流 429；消费侧甩任务（`task_queue._process_message`），降级经 `record_degraded` 留痕；默认关 = 零采样零甩负荷 |
| `BULKHEAD_RETRIEVAL_SIZE` | `0` | Phase H·W3：检索下游隔离舱池位（`retrieve_scoped_docs_cached` miss 计算缝，SingleFlight 等待方不占池）；`0` = 不限（基线），`>0` = 池满等待（故障不串扰其他下游） |
| `BULKHEAD_NEO4J_SIZE` | `0` | Phase H·W3：Neo4j/Cypher 工具隔离舱（executor 按工具名 `cypher_query`/`predefined_cypher` 归池）；`0` = 不限 |
| `BULKHEAD_MCP_SIZE` | `0` | Phase H·W3：MCP stdio 下游隔离舱（`MCPStdioClient.call_tool` 缝）；`0` = 不限 |
| `BULKHEAD_WEB_SEARCH_SIZE` | `0` | Phase H·W3：外部搜索工具隔离舱（executor 按工具名 `search` 归池）；`0` = 不限 |
| `SHARED_HTTP_MAX_CONNECTIONS` | `0` | Phase H·W3：应用级共享 `httpx.AsyncClient` 连接池上限（rerank / embedding 两缝复用，消灭 per-request 创建；超时经请求级 `timeout=` 传入，关停经 lifespan 关闭）；`0` = 每次请求新建（与基线逐位等价），`>0` 时共享实例 `trust_env=False`（出口行为确定性优先，rerank 侧为 flag 开启后的有意变化） |
| `EVENT_BATCH_MAX_SIZE` | `0` | Phase H·W4：Redis Stream 事件批写容量（满批即 pipeline flush；与 `EVENT_BATCH_FLUSH_MS` 任一为 `0` = 关，回到逐条写；推荐 `50`）。开启后 publish→flush 前不可见（可见性延迟 ≤ 间隔），故默认关 |
| `EVENT_BATCH_FLUSH_MS` | `0` | Phase H·W4：事件批写定时 flush 间隔（毫秒，推荐 `20`）；批写仅改提交节奏，flush 后事件内容与读回语义不变 |
| `TRAJECTORY_ASYNC_WRITE_ENABLED` | `false` | Phase H·W4：轨迹记录入队 + 后台 writer 落 sink（关停/测试经 `flush_trajectory_writes` 兜底落盘）；默认关 = 同步直写现行为 |
| `SSE_COALESCE_ENABLED` | `false` | Phase H·W4：SSE 出口 token 帧（`message`/`answer_chunk`/`thinking_chunk`）50ms 窗口合并为 delta，终态/非 token 帧前必 flush；默认关 = 逐帧直通 |
| `SSE_QUEUE_SIZE` | `0` | Phase H·W4：SSE 有界队列背压；满则丢非关键帧（丢帧计数 `sse_dropped_frames_total`），终态帧（`done`/`error`/`finished` 等）阻塞等待绝不丢；`0` = 关 |

> 新增特性开关时，请**默认关闭**（或保持既有行为），并在 `.env.example` 中注释说明。

---

## 5.1 法律法规领域数据路径

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `NEO4J_LEGAL_JSON_PATH` | `data/legal/legal_seed.json` | 法律法规领域种子（Neo4j 法律本体），由 `scripts/legal_kg_to_csv.py` 转换灌库 |
| `LEGAL_LIGHTRAG_JSON_PATH` | `data/legal/legal_lightrag_corpus.json` | 法律法规 LightRAG 语料（法条全文 + 司法解释全文） |

---

## 5.2 服务间鉴权：调 `kb_ingest` 的共享密钥

`kb_ingest`（知识入库服务）的端点是**写入语义**（Excel / MySQL 入库直接改 Milvus 集合），
且 compose 把它发布到宿主机（`ports: 8100:8000`）、`uvicorn --host 0.0.0.0` 监听。
因此它自带一套 `X-API-Key` 校验。本应用作为**调用方**只需持有密钥：

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `KB_API_KEY` | 空 | 调 `kb_ingest` 时作为 `X-API-Key` 头发出；空 = 不带该头（适用于对端未开启校验） |

对端的**开关**叫 `KB_REQUIRE_API_KEY`（`kb_ingest` 自己的配置，本应用**不读它**——
本应用只需知道"要不要带钥匙"，而这件事由 `KB_API_KEY` 是否为空决定；
多读一个开关只会多一处可能不一致的配置面）。完整契约见 `kb_ingest/.env.example`。

> ⚠️ **不要复用 `API_KEY`。** compose 里 `kb_ingest` 容器用 `env_file: .env` 加载
> 仓库根 `.env`，复用会把主应用密钥原样注入该容器；而 `API_KEY` 是**对外发布**的
> 凭证（任何外部客户端都持有）⇒ **一支对外凭证同时成了对内写权限**（权限抬升）。
> 服务作用域的 `KB_*` 使两支密钥可独立轮换与吊销。
>
> 出站请求头的构造收在 `agentix/domain/policies/ingest_auth.py`（纯 stdlib；
> 放在 domain 层是为满足"`application` 不得引用 `infrastructure`"这条规则），
> 且**只对与本仓 `kb_ingest` 同源的目标**附带密钥（按 origin 比较，
> 不用 `str.startswith`——后者会放过 `http://kb_ingest:8000.evil.example`）。

---

## 6. 新增配置项

**步骤**：

1. 在 `settings.py` 的相应分组中添加字段（含 `description` 与合理默认值）：

```python
MY_FEATURE_ENABLED: bool = Field(
    default=False,
    description="Enable my feature (default off for safe rollout)",
)
```

2. 在 `.env.example` 中补一行（含注释）：

```dotenv
# 我的特性开关（默认 false）
MY_FEATURE_ENABLED=false
```

3. 在代码中通过 `settings.MY_FEATURE_ENABLED` 读取（禁止 `os.getenv`）

4. 若为特性开关，同步更新本文档 §5 的表格

**约定**：
- 布尔开关默认值一律「安全侧」（关）
- 数值边界用 `Field(..., ge=, le=)` 声明
- 涉及敏感信息（密钥、口令）的字段**不得**给非空默认值

---

## 7. 配置一致性检查

| 检查 | 工具 | 频率 |
|------|------|------|
| 版本号三处一致 | `python scripts/bump_version.py --check` | CI（每次 PR） |
| `.env.example` 与 `settings.py` 字段对应 | 人工 Review（PR 检查清单） | 每次配置改动 |
| 生产密钥就位 | 启动期 `resolved_secret_key` 检查 | 每次启动 |

---

## 8. 相关文档

- [extension.md](extension.md) — 配置驱动的组件装配（Provider 选择）
- [layers.md](layers.md) — 配置读取的层级归属
- [`.env.example`](../../.env.example) — 带注释的完整配置样例
