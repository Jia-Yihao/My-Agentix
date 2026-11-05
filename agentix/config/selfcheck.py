"""配置自检：在启动时比对 `.env` 与 `.env.example` 的字段集合（P2-4）。

P2-4 要求：".env 与 .env.example 不一致 → 加启动自检：比对字段集合，缺失告警"。

**为什么需要**：`settings.py` 用 Pydantic Settings 读 `.env`，缺项会**静默回落到
默认值**。这导致的典型事故是：本地跑得好好的，换台机器少配一项就行为异常，
而日志里没有任何提示——因为"用了默认值"在语义上完全合法。

本模块只看**字段集合**（不看值），因为：

- 值含密钥，**绝不能**进日志；
- 缺项才是要抓的问题（值对不对是运行时的事）。

**失败语义：只告警，绝不抛异常。** 自检本身是可选的诊断手段，
不能因为"`.env` 少了个可选字段"就让服务起不来——那会把诊断工具变成新的故障源。

用法::

    from agentix.config.selfcheck import run_startup_selfcheck
    run_startup_selfcheck()          # 记录 warning，无异常

    python -m agentix.config.selfcheck   # 也可单独运行做 CI/本地检查
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from loguru import logger

__all__ = [
    "EnvDiff",
    "parse_env_keys",
    "compare_env_files",
    "run_startup_selfcheck",
]

#: 默认文件名
ENV_FILE = ".env"
EXAMPLE_FILE = ".env.example"

#: 允许"示例里有、本地没有"的字段：这些是有意留空的**可选**项，
#: 缺失不值得告警（否则自检会被噪声淹没，进而被忽略）。
DEFAULT_IGNORED_MISSING: frozenset = frozenset(
    {
        # 由运维按部署环境注入，本地通常不设
        "SECRET_KEY",
        "API_KEY",
        "SERPAPI_KEY",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "TAVILY_API_KEY",
        "GEMINI_API_KEY",
        "LLM_API_KEY",
        "KB_RERANK_API_KEY",
        # 2026-09：kb_ingest 的服务间共享密钥与开关。本地通常不设
        # （kb_ingest 未开启鉴权时本应用不带 X-API-Key 头即可）。
        "KB_API_KEY",
        "KB_REQUIRE_API_KEY",
        # 2026-10-23（§24.2 G12）：kb_ingest 的 MySQL 连接串闸门。默认关，
        # 本地 .env 不设即与加固前逐位等价；容器侧的缺省由 docker-compose.yml 给。
        "KB_MYSQL_URL_GUARD",
        "KB_MYSQL_ALLOWED_HOSTS",
        # 2026-10-23（§24.3 T1 ⑫）：同服务的 WHERE 参数化闸门。同样不是本应用的字段。
        "KB_MYSQL_WHERE_PARAMETRIZED",
        # 2026-10-09（P1-1）：跨端点 fallback 规格——可选能力，本地通常不配
        "LLM_FALLBACK_MODEL_SPECS",
        # 2026-10-09（P2-1）：反馈闭环 flag——默认关，本地不配即基线等价
        "FEEDBACK_ENABLED",
        "FEEDBACK_PII_REDACT",
        "EMBEDDING_API_KEY",
        "RERANK_API_KEY",
        # 开关类：不设 = 用默认值，属合法省略
        "REQUIRE_API_KEY",
        "V1_REQUIRE_JWT",
        "ENABLE_EXTERNAL_SEARCH",
        "LEGACY_ERROR_MODE",
        "CHECKPOINTER_TYPE",
        # fail-closed 开关不设 = 默认 false（维持降级基线），属合法省略
        "CHECKPOINTER_FAIL_CLOSED",
        # T22：升级重试节奏不设 = 默认 0（关闭重试循环）= 基线等价，属合法省略
        "CHECKPOINTER_RETRY_INTERVAL_S",
        "CHECKPOINTER_RETRY_MAX_ATTEMPTS",
        # LG-2-6：图执行 durability 不设 = 框架默认 async（与升级前逐位等价）
        "GRAPH_DURABILITY",
        # Phase 20：EventPort 后端不设 = 默认 memory（进程内），属合法省略
        "EVENT_PORT_BACKEND",
        "APP_ENV",
        "DEBUG",
        "LOG_LEVEL",
        # M2-1（拆 observability→retrieval 反向边）：`LOG_JSON_ENABLED` / `LOG_LEVEL` /
        # `TRAJECTORY_DIR` 三键的真源上提到 config.settings，前两个同时写进
        # `.env.example`（约定 4 ③）。不设 = 用代码默认值（JSON 日志开、根 logger
        # INFO），属合法省略 ⇒ 不登记进来就会被 `compare_env_files` 判成"示例有、
        # 本地缺"的缺项，启动自检刷 warning（`LOG_LEVEL` 本就在表内，见上一行）。
        # `TRAJECTORY_DIR` **刻意不登记**：它在 `.env.example` 里是**注释行**——默认值是按
        # 仓库根推导的绝对路径，写一行活值（尤其空值）会把落盘根变成"当前工作目录"，
        # 与代码默认不等价。而 `parse_env_keys` 跳过 `#` 行 ⇒ 它压根不进 example_keys
        # ⇒ 登记进来就是僵尸条目（`CONTRIBUTING.md` 工程约定 1「登记不许腐化」）。
        "LOG_JSON_ENABLED",
        # 企业化 P0-3：限流开关与参数不设 = 用默认值（关闭/120rpm/240burst）
        "RATE_LIMIT_ENABLED",
        "RATE_LIMIT_GLOBAL_RPM",
        "RATE_LIMIT_GLOBAL_BURST",
        # R1-4（技术变更指南 §2.1.5②）：上传单独配额不设 = 20rpm/30burst
        "UPLOAD_RATE_LIMIT_RPM",
        "UPLOAD_RATE_LIMIT_BURST",
        # 企业化 P0-2：监控栈端口为 compose 可选项，有内置默认值
        "PROMETHEUS_PORT",
        "GRAFANA_PORT",
        "ALERTMANAGER_PORT",
        "GRAFANA_ADMIN_PASSWORD",
        # 企业化 P0-4：OTel 为可选导出配置，不设 = 不上送 OTLP / 不打印 console
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_CONSOLE_EXPORT",
        # §24.3 T0（沙箱加固）：全部默认关 / 默认沿用旧推断，不设 = 与加固前逐位等价
        "TOOL_SYNC_OFFLOAD_ENABLED",
        "TOOL_HANDLER_CONTRACT_ENABLED",
        # §24.2 G14：同名重复注册严格模式。不设 = 默认关 = 只告警、仍按首次声明执行
        "TOOL_DUPLICATE_REGISTRATION_STRICT",
        # §24.3 T2 ⑭：执行位置端口。不设 = 默认关 = 不查 `ToolSpec.sandbox`
        "EXECUTION_SANDBOX_ENABLED",
        # §24.3 T2 ⑮：阶梯第三档。不设 = 默认关 = `subprocess` 仍在"词汇表−已实现"差集里
        "TOOL_SANDBOX_SUBPROCESS_ENABLED",
        "TOOL_SANDBOX_SUBPROCESS_UID",
        "TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS",
        # code exec（运行时写代码执行任务）：总闸默认关、轮数/超时用默认值，
        # 不设 = 与基线逐位等价，属合法省略
        "CODE_EXEC_ENABLED",
        "CODE_EXEC_MAX_ROUNDS",
        "CODE_EXEC_TIMEOUT_S",
        "MCP_SUBPROCESS_ENV_SCRUB",
        "MCP_SUBPROCESS_ENV_EXTRA_KEYS",
        "DB_ECHO",
        "UVICORN_RELOAD",
        # §24.3 T1 ⑪：设置接口授权收紧，默认关 = 与加固前逐位等价
        "SETTINGS_AUTHZ_ENABLED",
        # 企业化 P1-5.3：LLM 并发上限不设 = 不限额（0）+ 默认排队超时
        "LLM_MAX_CONCURRENCY",
        "LLM_CONCURRENCY_QUEUE_TIMEOUT",
        # 企业化 P1-4.2：注入检测默认开（不设=true）、入库阻断默认关
        "PROMPT_INJECTION_DETECTION_ENABLED",
        "PROMPT_INJECTION_INGEST_BLOCK",
        # 企业化 P2-4.5：输入长度上限不设 = 默认 5000 字符
        "QUERY_MAX_LENGTH",
        # R2-2（技术变更指南 §2.2.7①）：有效性过滤默认开、检索截至标注默认开
        "EFFECTIVENESS_FILTER_ENABLED",
        "EFFECTIVENESS_ASOF_ANNOTATION",
        # R3-5（架构文档 §24-28）：authority 排序默认开；Budget/隔离默认关（行为不变）
        "CONTEXT_AUTHORITY_RANKING_ENABLED",
        "CONTEXT_BUDGET_TOKENS",
        "CONTEXT_ISOLATE_THRESHOLD",
        # R3-6（架构文档 §104）：grounding 观测模式默认开，只报告不拦截
        "GROUNDING_VALIDATOR_ENABLED",
        "GROUNDING_SUPPORT_THRESHOLD",
        "GROUNDING_LOW_SCORE_WARN",
        # R-P2-2 证据门禁（rag-optimization §6）：检索域 env 键（observe 默认）
        "EVIDENCE_GATE_MODE",
        # R3-7（架构文档 §42-44）：分档/回退模型空 = 回退主模型（单模型部署零行为变化）
        "LLM_MODEL_FAST",
        "LLM_MODEL_STANDARD",
        "LLM_MODEL_REASONING",
        "LLM_FALLBACK_MODELS",
        "LLM_FALLBACK_MAX_ATTEMPTS",
        # R3-8（架构文档 §32）：语义卡片提取默认开，但挂在 MEMORY_CONTEXT_ENABLED 通道之下
        "MEMORY_CARDS_ENABLED",
        # Phase 29：profile_card（JSON Cards）默认关，本地 .env 省略属合法
        "MEMORY_PROFILE_CARD_ENABLED",
        # P6（source-code-study §5 表 6.1/6.2）：记忆扇出/后台评审默认关
        "MEMORY_FANOUT_ENABLED",
        "MEMORY_REVIEW_ENABLED",
        # A2c（统一执行链）：记忆三工具默认关，属合法省略
        "MEMORY_TOOLS_ENABLED",
        # F1b（统一执行链 Stage F）：向量召回 / 真分词计数默认关，属合法省略
        "MEMORY_VECTOR_RECALL_ENABLED",
        "CONTEXT_REAL_TOKEN_COUNT_ENABLED",
        # F1b2（统一执行链 Stage F）：记忆索引制 / 指令级联 / 隔离外置默认关
        "MEMORY_INDEX_ENABLED",
        "AGENTS_CASCADE_ENABLED",
        "CONTEXT_ISOLATE_PERSIST_ENABLED",
        # F1c（统一执行链 Stage F）：LLM 规划 + TodoWrite 默认关，属合法省略
        "AGENT_LLM_PLANNER_ENABLED",
        # F1d（统一执行链 Stage F）：并发工具轮默认 1（串行），属合法省略
        "AGENT_TOOL_ROUND_CONCURRENCY",
        # F1e（统一执行链 Stage F）：HITL 进环默认关，属合法省略
        "HITL_IN_LOOP_ENABLED",
        # T23（审批恢复规划）：流内审批默认关 + 心跳间隔有默认值，属合法省略
        "STREAM_HITL_ENABLED",
        "STREAM_HITL_HEARTBEAT_S",
        # T18（审批恢复规划）：HITL pending 落库默认关（纯内存基线），属合法省略
        "HITL_PENDING_PERSIST_ENABLED",
        # T19（审批恢复规划）：审批等待时限默认 0（无独立 deadline），属合法省略
        "HITL_APPROVAL_DEADLINE_S",
        # T21（审批恢复规划）：控制平面运行态落库默认关（纯内存基线），属合法省略
        "RUN_STATE_PERSIST_ENABLED",
        # F2a（统一执行链 Stage F2）：PreCompact 重注 / 画像作用域默认关
        "CONTEXT_PRECOMPACT_REINJECT_ENABLED",
        "MEMORY_PROFILE_SCOPE_ENABLED",
        # 批次 B1：流式 idle 看门狗超时不设 = 默认 60s，属合法省略
        "STREAM_IDLE_TIMEOUT",
        # 2026-09：Settings.model_fields 全量补进 .env.example 后，
        # 下列均为带默认值的可选项——本地 .env 省略属合法，不必告警。
        # （键集合自检只关心「.env 比示例少了什么」，不关心 Settings 是否全覆盖。）
        "ALGORITHM",
        "ACCESS_TOKEN_EXPIRE_SECONDS",
        "AUTH_COOKIE_SESSION_FALLBACK",
        "LLM_INPUT_PRICE_PER_1K",
        "LLM_OUTPUT_PRICE_PER_1K",
        "OBSERVABILITY_PAYLOAD_CAPTURE",
        "TRAJECTORY_SINK",
        "TRAJECTORY_PATH",
        "MEMORY_RECENCY_HALF_LIFE_DAYS",
        "MAX_HISTORY_MESSAGES",
        "MEMORY_CONTEXT_ENABLED",
        "MEMORY_RECALL_LIMIT",
        "KG_FINAL_ANSWER_VALIDATION",
        # A3（统一执行链·图内规划收敛）：影子对拍 / 切流双闸默认关，属合法省略
        "KG_PLANNER_SHADOW_ENABLED",
        "KG_PLANNER_UNIFIED_ENABLED",
        # Stage F2b-d（R-P2-3 Graph 动态化）：三旋钮默认关 = 合法省略
        "KG_SCHEMA_AWARE_GENERATION_ENABLED",
        "EVIDENCE_POOL_ENABLED",
        "VERIFIER_GROUNDING_ENABLED",
        "TEXT2SQL_BACKEND",
        # 空 = 回退应用自身 DATABASE_URL（接线前行为），属合法省略
        "TEXT2SQL_DATABASE_URL",
        "WREN_PROJECT_PATH",
        "WREN_DATA_SOURCE",
        "WREN_CONNECTION_INFO",
        "WREN_QUERY_LIMIT",
        # R-P2-4（F2b-c）Text2SQL 加固：两旋钮默认关/0 = 合法省略
        "TEXT2SQL_DRY_RUN_ENABLED",
        "TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS",
        "ARTIFACT_INLINE_THRESHOLD",
        "ARTIFACT_STORE_MAX_ITEMS",
        "ARTIFACT_STORE_MAX_BYTES",
        "MEMORY_CONSOLIDATION_MAX_GAP_DAYS",
        "MEMORY_CONSOLIDATION_MAX_GROUPS",
        "MEMORY_CONSOLIDATION_ENABLED",
        "AGENTIC_RAG_ENABLED",
        # Phase 28：自主护栏双开关不设 = 默认关，属合法省略
        "CLARIFY_ROUTING_ENABLED",
        "BUDGET_NOTICE_ENABLED",
        # Phase 30：进化闭环——睡眠学习默认关、提案档案路径有默认值，属合法省略
        "EVOLUTION_SLEEP_ENABLED",
        "EVOLUTION_PROPOSAL_LOG",
        "TOKEN_BUDGET_PER_REQUEST",
        "TOKEN_BUDGET_PER_AGENT",
        "TOOL_CALLS_PER_REQUEST",
        "CONTROL_PLANE_MAX_RUNS",
        "CONTROL_PLANE_MAX_CONCURRENCY",
        "CONTROL_PLANE_MAX_DEPTH",
        "MULTIAGENT_PEER_MAX_STEPS",
        "MULTIAGENT_PEER_MAX_MESSAGES",
        "MULTIAGENT_BUS_ENABLED",
        "MULTIAGENT_SUBAGENT_ISOLATION_ENABLED",
        # 性能真值三旋钮默认关/0，
        # 属合法省略
        "LATENCY_BREAKDOWN_ENABLED",
        "LATENCY_TOTAL_BUDGET_MS",
        "EVENT_LOOP_LAG_METRIC_ENABLED",
        # LLM 优化七键默认关/空/
        # 默认值，属合法省略
        "ROUTING_FASTPATH_ENABLED",
        # T8：路由结果跨请求缓存（进程内 LRU+TTL，默认关，属合法省略）
        "ROUTING_CACHE_ENABLED",
        # T30（审批恢复规划）：单层路由业务总闸默认关，属合法省略
        "ROUTING_SINGLE_LAYER_ENABLED",
        "LLM_TIER_ROUTING_ENABLED",
        "LLM_ROLE_TIER_MAP",
        "VERIFICATION_MAX_LLM_CALLS",
        "VERIFICATION_TIMEOUT_MS",
        "VERIFICATION_TOKEN_BUDGET",
        # 成本护栏默认关、
        # nprobe 默认 16（= 原硬编码值），属合法省略
        "NEO4J_COST_GUARD_ENABLED",
        "MILVUS_NPROBE",
        "TOOL_OUTPUT_BUDGET_TOKENS",
        # 运行时治理默认关/0，
        # RETRY_BUDGET_TOKENS 默认 6，属合法省略
        "ADMISSION_CONTROL_ENABLED",
        "ADMISSION_MAX_CONCURRENT_RUNS",
        "ADMISSION_MAX_QUEUE",
        # LG-1-1：只读节点原生重试开关，不设 = 默认 false（不传 retry_policy），属合法省略
        "NODE_RETRY_ENABLED",
        # LG-3-2：节点级原生超时，不设 = 0（不传 timeout 形参），属合法省略
        "NODE_TIMEOUT_SECONDS",
        # LG-3-5：节点级错误补偿，不设 = false（不传 error_handler），属合法省略
        "NODE_ERROR_HANDLER_ENABLED",
        "RETRY_BUDGET_ENABLED",
        "RETRY_BUDGET_TOKENS",
        "RETRY_BACKOFF_ALIGNED",
        "ADAPTIVE_TOOL_TIMEOUT_ENABLED",
        "LOAD_SHEDDING_ENABLED",
        "BULKHEAD_RETRIEVAL_SIZE",
        "BULKHEAD_NEO4J_SIZE",
        "BULKHEAD_MCP_SIZE",
        "BULKHEAD_WEB_SEARCH_SIZE",
        "SHARED_HTTP_MAX_CONNECTIONS",
        # 持久化与 SSE 批处理，
        # 默认 0/false = 关（与基线逐位等价），属合法省略
        "EVENT_BATCH_MAX_SIZE",
        "EVENT_BATCH_FLUSH_MS",
        "TRAJECTORY_ASYNC_WRITE_ENABLED",
        "SSE_COALESCE_ENABLED",
        "SSE_QUEUE_SIZE",
        # 多智能体通信压缩，
        # 默认 false = 关（与基线逐位等价），属合法省略
        "SUBAGENT_RESULT_CONTRACT_ENABLED",
        "MULTIAGENT_EARLY_EXIT_ENABLED",
        "TOOL_RESULT_MAX_ROWS",
        "TOOL_RESULT_MAX_CHARS",
        "HUMAN_REVIEW_ENABLED",
        "COMPLETION_VALIDATOR_STRICT",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "SERPAPI_BASE_URL",
        "SERPAPI_TIMEOUT",
        "SEARCH_RESULT_COUNT",
        "LIGHTRAG_INIT_LIMIT",
        "NEO4J_DATABASE",
        "NEO4J_GRAPH_CACHE_PATH",
        "NEO4J_MAX_CONNECTION_LIFETIME",
        "NEO4J_BOOTSTRAP_JSON",
        "NEO4J_BOOTSTRAP_FORCE",
        # R-P0-4：schema TTL 刷新 / Cypher 执行超时默认值安全（300s / 0=off），
        # 不设 = 回落字段默认，属合法省略
        "NEO4J_SCHEMA_REFRESH_SECONDS",
        "NEO4J_QUERY_TIMEOUT_SECONDS",
        "NEO4J_LEGAL_JSON_PATH",
        "LEGAL_LIGHTRAG_JSON_PATH",
        "AGENT_MAX_CONCURRENCY",
        "AGENT_LOOP_ENABLED",
        # P1（任务执行）：TASK_EXEC/重规划默认关或 0，属合法省略
        "TASK_EXEC_ENABLED",
        "AGENT_MAX_REPLANS",
        # LG-3-4：恢复期步骤清单默认关，不设 = 不建表、零读写，属合法省略
        "RECOVERY_STEP_LEDGER_ENABLED",
        # LG-3-6：优雅关闭默认关，不设 = 不装信号处理器、不传 control 形参，属合法省略
        "RUN_CONTROL_ENABLED",
        "RUN_DRAIN_TIMEOUT_S",
        # P2（Skills/Registry/动态调度）：工具面灰度开关默认关/空，属合法省略
        "TOOL_SELECTION_ENABLED",
        "TOOL_SELECTION_MAX_RISK",
        "TOOL_DEFERRED_TOOLS",
        "TOOL_RESULT_CONTRACT_ENABLED",
        # A2b（统一执行链）：Tool Gateway 收编默认关，属合法省略
        "TOOL_GATEWAY_ENFORCED",
        # P3（渐进上下文）：压缩阶梯/逐步重建预算默认关或 0，属合法省略
        "CONTEXT_COMPACT_LADDER",
        "AGENT_CONTEXT_BUDGET_TOKENS",
        # P4（异常闭环）：三开关默认关（fail-fast flag 治理），属合法省略
        "TOOL_GUARDRAILS_ENABLED",
        "AGENT_ROLLBACK_ENABLED",
        "FAILURE_BOUNDARY_ENABLED",
        # P5（Proactive）：cron 调度两开关默认关/60s，属合法省略
        "CRON_ENABLED",
        "CRON_TICK_SECONDS",
        # T26（队列 PEL 认领 + 优雅停机）：队列 worker 默认关，不设 = 不建任务、不建连，
        # 属合法省略
        "TASK_QUEUE_WORKER_ENABLED",
        # Phase 21：Skills/Hooks 默认关，不设 = 合法省略（skills 目录用默认 "skills"）
        "SKILLS_ENABLED",
        "SKILLS_DIR",
        "HOOKS_ENABLED",
        "CONTEXT_AUTOCOMPACT",
        # 上下文工程（A/B/C/D）三项默认关/0，不设 = 合法省略
        "LG_INSTRUCTIONS_ENABLED",
        "LG_TOKEN_HISTORY_BUDGET",
        "LG_STATUS_TAIL_ENABLED",
        "NOTIFY_USER_ENABLED",
        "DISABLED_TOOLS",
        "KB_DATA_FILE",
        # 空 = 沿用子图内置的测试数据集范围文案，属合法省略
        "KB_SCOPE_DESCRIPTION",
        "KB_RERANK_MAX_CANDIDATES",
        "KB_RERANK_TOP_N",
        "KB_RERANK_TIMEOUT",
        "KB_RERANK_SCORE_FUSION_ALPHA",
        "UPLOAD_DIR",
        "KB_EXTERNAL_SEARCH_URL",
        "KB_EXTERNAL_SEARCH_TIMEOUT",
        "KB_RETRIEVAL_MAX_ATTEMPTS",
        "KB_RETRIEVAL_RETRY_BASE_DELAY",
    }
)


@dataclass
class EnvDiff:
    """字段集合比对结果。"""

    #: 示例里有、本地 `.env` 里没有（**需要关注**）
    missing: Set[str] = field(default_factory=set)
    #: 本地有、示例里没有（示例可能过期）
    extra: Set[str] = field(default_factory=set)
    #: 被忽略的缺失项（可选字段，见 `DEFAULT_IGNORED_MISSING`）
    ignored: Set[str] = field(default_factory=set)
    present: bool = True

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        if not self.present:
            return "未找到 .env（使用环境变量或默认值）"
        if self.ok:
            return "字段集合一致"
        return f"缺 {len(self.missing)} 项：{', '.join(sorted(self.missing))}"


def parse_env_keys(path: Path) -> Set[str]:
    """解析 env 文件的**键集合**（绝不读取值）。

    跳过空行与 `#` 注释行；`export FOO=bar` 形式也兼容（去掉 `export` 前缀）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()

    keys: Set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def compare_env_files(
    env_path: Optional[Path] = None,
    example_path: Optional[Path] = None,
    *,
    ignored_missing: Optional[Set[str]] = None,
) -> EnvDiff:
    """比对 `.env` 与 `.env.example` 的字段集合。

    :param ignored_missing: 视作"合法省略"的缺失字段；默认 `DEFAULT_IGNORED_MISSING`。
    """
    env_path = Path(env_path) if env_path is not None else Path(ENV_FILE)
    example_path = Path(example_path) if example_path is not None else Path(EXAMPLE_FILE)
    ignore = set(
        ignored_missing if ignored_missing is not None else DEFAULT_IGNORED_MISSING
    )

    if not env_path.exists():
        return EnvDiff(present=False)
    if not example_path.exists():
        # 没有示例文件就无从比对——不算错误（自检是尽力而为的）
        return EnvDiff(present=True)

    env_keys = parse_env_keys(env_path)
    example_keys = parse_env_keys(example_path)

    raw_missing = example_keys - env_keys
    return EnvDiff(
        missing=raw_missing - ignore,
        extra=env_keys - example_keys,
        ignored=raw_missing & ignore,
        present=True,
    )


def run_startup_selfcheck(
    env_path: Optional[Path] = None,
    example_path: Optional[Path] = None,
    *,
    root: Optional[Path] = None,
) -> Optional[EnvDiff]:
    """执行启动自检并记录结果；**任何异常都被吞掉**。

    :return: 比对结果；`.env` 不存在或自检失败时返回 ``None``。
    """
    try:
        base = Path(root) if root is not None else Path(os.getcwd())
        env = Path(env_path) if env_path is not None else base / ENV_FILE
        example = Path(example_path) if example_path is not None else base / EXAMPLE_FILE

        diff = compare_env_files(env, example)
        if not diff.present:
            return None
        if diff.missing:
            logger.warning(
                "配置自检：.env 缺少 {} 个 .env.example 中声明的字段（将回落到默认值）：{}",
                len(diff.missing),
                ", ".join(sorted(diff.missing)),
            )
        if diff.extra:
            logger.info(
                "配置自检：.env 有 {} 个字段未出现在 .env.example（请同步更新示例文件）：{}",
                len(diff.extra),
                ", ".join(sorted(diff.extra)),
            )
        return diff
    except Exception as exc:  # noqa: BLE001 - 自检不得影响启动
        logger.debug("env self-check skipped: {}", exc)
        return None


def _main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI 薄壳
    import argparse

    parser = argparse.ArgumentParser(description="比对 .env 与 .env.example 的字段集合")
    parser.add_argument("--env", default=ENV_FILE)
    parser.add_argument("--example", default=EXAMPLE_FILE)
    args = parser.parse_args(argv)

    diff = compare_env_files(Path(args.env), Path(args.example))
    print(f".env vs {args.example}: {diff.summary()}")
    if diff.extra:
        print(f"  示例文件待补：{', '.join(sorted(diff.extra))}")
    if diff.ignored:
        print(f"  已忽略（可选项）：{', '.join(sorted(diff.ignored))}")
    return 0 if diff.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
