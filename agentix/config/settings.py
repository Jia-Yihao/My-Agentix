"""
Centralised application settings for My Agentix.
"""

import logging
import secrets
from typing import List, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _default_app_version() -> str:
    """读取包版本号作为 APP_VERSION 的默认值（单一事实来源）。

    用 ``default_factory``（惰性）而非模块级常量：既保证
    ``agentix/__init__.py`` 是版本号的唯一权威，也避免 settings 模块
    在导入期反向依赖 ``agentix`` 包而引发循环导入。
    兜底值仅用于包元数据不可用的极端情况（如源码直接运行且未安装）。
    """
    try:
        from agentix import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - 版本读取失败不应阻止启动
        return "0.0.0"


class Settings(BaseSettings):
    """Application wide configuration loaded from environment variables."""

    # Application metadata
    APP_NAME: str = "My Agentix"
    # 版本号的**唯一事实来源**是 `agentix.__version__`（由 `scripts/bump_version.py`
    # 与 pyproject.toml / web/package.json 同步维护）。此处用 `default_factory`
    # 惰性读取，避免 settings.py 成为第 4 处需要手工维护的版本声明——
    # 历史事故：settings 写 0.1.2，而 .env/.env.example 里写 0.1.1，
    # 运行期被 .env 覆盖后健康检查对外报 0.1.1，且旧版 `--check` 完全看不到该漂移。
    APP_VERSION: str = Field(
        default_factory=_default_app_version,
        description="Application version; defaults to the package version (single source of truth)",
    )
    DEBUG: bool = True

    # API configuration
    API_V1_PREFIX: str = "/api/v1"
    HOST: str = "0.0.0.0"  # nosec B104 - 容器/开发默认监听全部网关，生产边界由反代与鉴权控制
    PORT: int = 8000

    # Security
    # SECRET_KEY 不再默认随机生成：运行期随机密钥会在重启后失效所有会话/token。
    # 显式配置走环境变量/.env；缺失时由 resolved_secret_key 按环境决定（dev 临时生成+强告警，生产拒绝启动）。
    SECRET_KEY: str = Field(
        default="",
        description="Secret key for JWT signing (must be explicitly configured in production)",
    )
    ALGORITHM: str = Field(default="HS256", description="JWT algorithm")
    ACCESS_TOKEN_EXPIRE_SECONDS: int = Field(
        default=86400,
        description="JWT access token expiry in seconds (default 24h)",
    )
    AUTH_COOKIE_SESSION_FALLBACK: bool = Field(
        default=False,
        description="Enable Cookie Session fallback auth (rollback switch for R2.3)",
    )
    REQUIRE_API_KEY: bool = Field(
        default=False,
        description="Require API key authentication for all endpoints",
    )
    # v1 逐端点鉴权（接口审查 2026-09-27）：用户面 chat/sessions/upload 写操作
    # 要求有效 Bearer JWT；机器面 events/ops 走既有 REQUIRE_API_KEY。
    # 默认关 = 轻量 v1 匿名域保持原状（strangler 纪律，flag-off 行为不变）。
    V1_REQUIRE_JWT: bool = Field(
        default=False,
        description="Require valid Bearer JWT on v1 user-plane endpoints (chat/sessions/upload mutations)",
    )
    # API_KEY 不再默认随机生成：随机值从不打印，一旦开启鉴权调用方无从获知，形同锁死。
    # 显式配置走环境变量/.env；缺失时开启鉴权应在启动期告警（见 main.py startup）。
    API_KEY: str = Field(
        default="",
        description="API key for authentication (must be explicitly configured to enable auth)",
    )

    # Global rate limiting（企业化 P0-3）
    # 主开关与检索域 chat 路由限流（retrieval/settings.py）共用同一环境变量：
    # 开启后「全局中间件 + 知识域聊天路由」双层同时生效。
    RATE_LIMIT_ENABLED: bool = Field(
        default=False,
        description="Enable global rate limiting middleware (per-client token bucket)",
    )
    RATE_LIMIT_GLOBAL_RPM: int = Field(
        default=120,
        description="Global rate limit: steady requests per minute per client IP",
    )
    RATE_LIMIT_GLOBAL_BURST: int = Field(
        default=240,
        description="Global rate limit: token bucket burst capacity per client IP",
    )
    # R1-4（技术变更指南 §2.1.5②）：上传接口单独配额（重 IO + 触发索引任务）
    UPLOAD_RATE_LIMIT_RPM: int = Field(
        default=20,
        description="Per-client uploads per minute (upload paths get a stricter separate bucket)",
        gt=0,
    )
    UPLOAD_RATE_LIMIT_BURST: int = Field(
        default=30,
        description="Token bucket burst capacity for upload paths per client IP",
        gt=0,
    )
    # 企业化 P1-5.3：LLM 全局并发上限（防高峰期打满上游配额后全站雪崩）
    LLM_MAX_CONCURRENCY: int = Field(
        default=0,
        description="Global max in-flight LLM calls; 0 = unlimited (no semaphore)",
        ge=0,
    )
    LLM_CONCURRENCY_QUEUE_TIMEOUT: float = Field(
        default=5.0,
        description="Seconds an LLM call may wait in the concurrency queue before failing fast",
        gt=0,
    )
    # 企业化 P1-4.2：prompt injection 防线开关
    PROMPT_INJECTION_DETECTION_ENABLED: bool = Field(
        default=True,
        description="Enable prompt injection pattern detection (ingest/output observe + boundary wrapping)",
    )
    PROMPT_INJECTION_INGEST_BLOCK: bool = Field(
        default=False,
        description="Block document ingestion when injection patterns detected (default observe-only; legal KB false-positive cost is high)",
    )
    # 企业化 P2-4.5：长文本输入统一上限（防 token 放大攻击；各路由文本输入 DTO 统一引用）
    QUERY_MAX_LENGTH: int = Field(
        default=5000,
        description="Max length for free-text query/message fields across HTTP DTOs",
        gt=0,
    )

    # LLM configuration
    LLM_PROVIDER: str = Field(default="openai", description="LLM provider: openai, anthropic, etc.")
    LLM_MODEL: str = Field(
        default="gpt-4-turbo-preview",
        description="LLM model name",
        validation_alias=AliasChoices("LLM_MODEL", "OPENAI_MODEL"),
    )
    # R3-7 Model Gateway（架构文档 §42-44）：任务分档路由 + Fallback 链
    # 分档模型：空 = 回退 LLM_MODEL（单模型部署零行为变化）
    LLM_MODEL_FAST: str = Field(
        default="", description="Fast tier model for simple classification tasks (empty = fallback to LLM_MODEL)"
    )
    LLM_MODEL_STANDARD: str = Field(
        default="", description="Standard tier model for normal answers (empty = fallback to LLM_MODEL)"
    )
    LLM_MODEL_REASONING: str = Field(
        default="", description="Strong reasoning tier model for complex legal reasoning (empty = fallback to LLM_MODEL)"
    )
    # Phase H·W1：角色→档位接线（网关既有 fast/standard/reasoning 只接线不重建）。
    # 关（默认）⇒ get_llm_client 档位解析不执行，profile 与基线逐位等价；
    # 开 + LLM_ROLE_TIER_MAP 命中 ⇒ 经 ModelGateway.resolve_model 取该档模型
    # （档位模型空/未知角色/未知档位 ⇒ 静默回退 LLM_MODEL 基线）。
    LLM_TIER_ROUTING_ENABLED: bool = Field(
        default=False,
        description="Route per-role LLM model choice through the ModelGateway tier map (default off = baseline profile)",
    )
    LLM_ROLE_TIER_MAP: str = Field(
        default="",
        description="Comma-separated role=tier pairs (tier in fast/standard/reasoning; empty = baseline-equal)",
    )
    # Fallback 链（§44）：逗号分隔；空 = 无 fallback（主链路零行为变化）。
    # 硬上限 LLM_FALLBACK_MAX_ATTEMPTS 防「模型故障→无限重试→成本爆炸」。
    LLM_FALLBACK_MODELS: str = Field(
        default="", description="Comma-separated fallback model chain (empty = no fallback)"
    )
    LLM_FALLBACK_MAX_ATTEMPTS: int = Field(
        default=2, description="Max total attempts including primary (hard cap against cost explosion)", gt=0
    )
    # 跨端点 fallback 规格（P1-1）：逗号分隔的 `model|base_url|api_key_env` 三元组序列。
    # 用 `|` 分隔（base_url 含 `://`，不能用 `:`）；api_key_env 是**环境变量名**
    # （密钥不落配置值，且不与主 API_KEY 复用——密钥隔离纪律）。
    # base_url / api_key_env 段可空（`model||` = 仅模型名，回退主端点）。
    # 非空时**覆盖** LLM_FALLBACK_MODELS（旧键语义零变化，保持向后兼容）。
    LLM_FALLBACK_MODEL_SPECS: str = Field(
        default="",
        description="Comma-separated model|base_url|api_key_env triples for cross-endpoint fallback (empty = use LLM_FALLBACK_MODELS)",
    )
    LLM_API_KEY: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    LLM_BASE_URL: Optional[str] = Field(
        default=None,
        description="LLM API base URL",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_API_BASE"),
    )
    # P2-2：成本估算单价（每 1K token）。默认 0.0 是诚实默认——本地 LM Studio
    # 端点免费，不假装有价格；接商用 API 时通过环境变量覆盖即可。
    LLM_INPUT_PRICE_PER_1K: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated cost per 1K input (prompt) tokens, in currency units",
    )
    LLM_OUTPUT_PRICE_PER_1K: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated cost per 1K output (completion) tokens, in currency units",
    )

    # 可观测性：payload（prompt / response / 工具入参）捕获开关。
    # 默认 **关**——完整回放需要落盘用户输入与模型输出，涉及隐私与存储成本，
    # 应由部署方显式开启。开启后内容仍经脱敏（疑似密钥遮蔽）+ 截断（500 字符）。
    OBSERVABILITY_PAYLOAD_CAPTURE: bool = Field(
        default=False,
        description="Capture redacted prompt/response/tool-arg previews into the trajectory for replay",
    )

    # 轨迹 sink 后端：`memory`（默认，进程内、重启即失）或 `jsonl`（落盘、可跨进程回放）。
    # 选 jsonl 时必须同时给 TRAJECTORY_PATH，否则自动回退 memory 并告警。
    TRAJECTORY_SINK: str = Field(
        default="memory",
        description="Trajectory sink backend: 'memory' (default) or 'jsonl' (persisted)",
    )
    TRAJECTORY_PATH: str = Field(
        default="",
        description="File path for the JSONL trajectory sink (required when TRAJECTORY_SINK=jsonl)",
    )

    # 批次 7：长期记忆 recall 的时效衰减半衰期（天）。
    # 相关性分 = 关键词命中数 × 0.5^(age_days / half_life)；<=0 表示关闭衰减
    # （退化为纯关键词打分，即旧行为）。
    MEMORY_RECENCY_HALF_LIFE_DAYS: float = Field(
        default=14.0,
        description="Half-life in days for recency decay in long-term memory recall; <=0 disables decay",
    )

    # 批次 7：主图（lg_builder）历史消息窗口上限。会话经 checkpointer 跨请求
    # 持久化，旧消息无界累积会推高每轮上下文体量；入口节点将其裁剪到最近
    # N 条（含本轮输入）。<=0 表示关闭窗口化（行为与旧版一致）。
    MAX_HISTORY_MESSAGES: int = Field(
        default=20,
        description="Max chat messages kept in the checkpointer-backed graph state per session; <=0 disables windowing",
    )

    # Phase 22（L4 auto-compact，Ch2 §2.7 分层压缩最后一档，**默认关**）：
    # L1–L3 组装后仍超预算的最后手段——语义压缩轨迹（经 CompactorPort，
    # 生产走 LLM、离线测试用 Fake）；压缩失败/五要素缺失 ⇒ 丢弃回退
    # Phase 18 行为。flag 关 ⇒ 构建路径与 Phase 18 逐字节一致。
    CONTEXT_AUTOCOMPACT: bool = Field(
        default=False,
        description="Enable L4 semantic auto-compaction of trajectory when over budget (last resort; default off)",
    )
    # P3（岗位对照·渐进上下文，借鉴 claw-code 渐进压缩重试梯）：L4 压缩的
    # keep-last 窗口阶梯 4→2→1→0（每轮只压缩头部、尾部原样保留，五要素
    # 断言逐轮把关；异常立即回退）。默认 False = 单次尝试（Phase 22 语义，
    # TestL4On 契约不变）；True 且 CONTEXT_AUTOCOMPACT=true 时阶梯生效。
    CONTEXT_COMPACT_LADDER: bool = Field(
        default=False,
        description="Retry L4 compaction with a shrinking keep-last window ladder (4→2→1→0); default off = single attempt",
    )
    # F2a·M-P2-2（memory-optimization §6 PreCompact 重注）：压缩/丢弃循环
    # 触发**之前**，把关键规则（静态前缀 RULE 行/规则行，按声明序截断）与
    # 用户硬约束 token（五要素 constraint_key，从**原轨迹**采集去重）预采集，
    # 重注到上下文尾部高注意力区——行携带 `[precompact:<source>]` 审计标记、
    # snapshot meta 记来源计数（规则来源可审计），解决长会话规则丢失。
    # 挂 CONTEXT_AUTOCOMPACT 通道（只在超预算将触发压缩时采集）；默认关 ⇒
    # 组装与基线逐字节等价（重注块不进 prompt）。
    CONTEXT_PRECOMPACT_REINJECT_ENABLED: bool = Field(
        default=False,
        description="Re-inject key rules and hard-constraint tokens at the context tail before compaction (auditable [precompact:*] markers + snapshot meta)",
    )

    # F1b·M-P1-5（memory-optimization §5 真分词/真计数）：token 估算换 LLM 侧
    # tokenizer（tiktoken cl100k_base，进程内懒加载一次，不新增服务）。
    # 默认关 ⇒ count_tokens ≡ len//4，与全部既有估点逐字节一致；开启后
    # tokenizer 缺失/失败自动退 CJK 感知启发式（永不打断上下文构建）。
    CONTEXT_REAL_TOKEN_COUNT_ENABLED: bool = Field(
        default=False,
        description="Use the real LLM-side tokenizer (tiktoken) for token estimates instead of len//4; default off = byte-equal estimates",
    )

    # 批次 7：长期记忆读回接线（默认**关**——开启后每次 general-query 问答
    # 会先检索该用户的历史摘要并注入 system 上下文，属上下文语义变更）。
    MEMORY_CONTEXT_ENABLED: bool = Field(
        default=False,
        description="Inject long-term memory context into general-query prompts",
    )
    MEMORY_RECALL_LIMIT: int = Field(
        default=3,
        ge=1,
        description="Max memory summaries recalled per query",
    )
    # R3-8 Semantic Memory 卡片（架构文档 §32）：从会话摘要提取稳定偏好卡
    # （Extractor→Dedup→Conflict→Store）；挂在 MEMORY_CONTEXT_ENABLED 通道之下
    # （后者默认关 ⇒ 整体默认零行为变化）。
    MEMORY_CARDS_ENABLED: bool = Field(
        default=True,
        description="Extract and recall semantic memory cards from session summaries",
    )
    # Phase 29（Ch3 §3.5/§3.6）：UserProfile 的结构化 `profile_card`
    # （JSON Cards + User as Code 派生）——与上面 R3-8 语义卡片
    # （MEMORY_CARDS_ENABLED，自由文本偏好卡）是**两个不同特性**，
    # 故独立开关（先例：Phase 28 澄清/预算两枚正交开关）。
    # 默认关 = `to_dict()` 序列化与基线字节等价（只增不改，原则 3）。
    MEMORY_PROFILE_CARD_ENABLED: bool = Field(
        default=False,
        description="Serialize/derive the structured profile_card on UserProfile (Phase 29 JSON Cards)",
    )
    # P6-6.1（source-code-study §5 表 6.1）：MemoryPort↔Redis 扇出召回——
    # 挂在 MEMORY_CONTEXT_ENABLED 通道之下（与 MEMORY_CARDS_ENABLED 同族）：
    # 默认关 ⇒ _maybe_memory_context 与基线逐位等价（零接线零开销）。
    MEMORY_FANOUT_ENABLED: bool = Field(
        default=False,
        description="Fan out memory recall across MemoryPort providers (Redis LTM adapter + in-process) into chat context",
    )
    # P6-6.2（§5 表 6.2）：摘要后台评审——flag-on 时 conversation summary
    # handler 异步把稳定语义卡片晋升为长期画像条目（fire-and-forget）；
    # 默认关 ⇒ handler 报告与基线完全一致。
    MEMORY_REVIEW_ENABLED: bool = Field(
        default=False,
        description="Schedule asynchronous background review promoting stable summary cards into long-term profile items",
    )
    # A2c（统一执行链 Stage A）：记忆三工具——true = remember/recall/forget
    # 进声明清单并在组合根安装 handler（经 ToolExecutor 治理链执行）；
    # flag-off（默认）不声明不注册，声明清单与基线逐项一致。
    MEMORY_TOOLS_ENABLED: bool = Field(
        default=False,
        description="Expose remember/recall/forget long-term-memory tools (declarations + ToolExecutor handlers)",
    )
    # F1b·M-P1-1（memory-optimization §5 向量召回）：进程内 provider 的 recall
    # 升级为"关键词 × 向量"双路 + recency 衰减（半衰期沿
    # MEMORY_RECENCY_HALF_LIFE_DAYS；TopK 沿 MEMORY_RECALL_LIMIT）。
    # 默认关 ⇒ 召回与基线逐位等价；开启后 embedding 走检索域向量栈（复用，
    # 不另建框架），embedding 失败/超时 fail-open 回纯关键词路径。
    MEMORY_VECTOR_RECALL_ENABLED: bool = Field(
        default=False,
        description="Enable keyword×vector dual-path recall with recency decay in the in-process memory provider (fail-open to keyword)",
    )
    # F1b2·M-P1-3（memory-optimization §5 MEMORY 索引制）：记忆注入段升级为
    # 「索引 + obs:// 指针」——正文经 offload_content 外置到 ArtifactStore，
    # 入口只留 `- 摘要 → obs://{artifact_id}` 索引行（常驻预算封顶，超限截
    # 索引尾部），取回走 read_artifact（obs:// 前缀自动剥离）。挂在
    # MEMORY_CONTEXT_ENABLED 通道之下；默认关 ⇒ 注入与基线逐位等价。
    MEMORY_INDEX_ENABLED: bool = Field(
        default=False,
        description="Index long-term memory context into obs:// pointers (fetch via read_artifact) instead of inlining summaries",
    )
    # F2a·M-P2-4（memory-optimization §6 画像作用域）：画像字段分级
    # （长期稳定 stable vs 会话偏好 session，profile_scope.PROFILE_FIELD_TIERS
    # 单源）+ 跨会话沿用授权边界——flag-on 时稳定层字段（user_constraints/
    # legal_domain_prefs/conflicts、频繁领域、语义卡）仅在用户已授权跨会话
    # 沿用（/ops/memory/profile-scope）才注入，未授权 fail-closed 只注会话层；
    # 授权读取异常同样 fail-closed。默认关 ⇒ 注入与基线逐位等价。
    MEMORY_PROFILE_SCOPE_ENABLED: bool = Field(
        default=False,
        description="Tier profile fields (stable vs session) and gate stable cross-session reuse behind explicit user consent (fail-closed)",
    )

    # 批次 10（§10.10.1）：KG 子图终态验证器（final_answer_guard），默认**关**。
    # 开启后每次 KG 问答在 summarize 与 final_answer 之间多一次 LLM 结构化校验
    # 调用（latency +）；校验失败**降级放行**（答案照常下发，仅 steps 留痕 +
    # warning），绝不因校验失败扣下答案——校验器不能成为主链路单点故障。
    KG_FINAL_ANSWER_VALIDATION: bool = Field(
        default=False,
        description="Validate final KG answer via LLM structured check between summarize and final_answer nodes",
    )
    # Phase H·W1 验证预算：校验器本身受硬预算约束（默认值 = 单次、1 秒、无
    # token 上限——校验只在 KG_FINAL_ANSWER_VALIDATION 开时才有意义，预算
    # 进一步保证它不会放大延迟/成本）。任一维度超预算 ⇒ **fail-open 降级放行**
    # （与既有校验失败降级同语义，steps 留痕），绝不扣下答案。
    VERIFICATION_MAX_LLM_CALLS: int = Field(
        default=1,
        ge=0,
        description="Max validation LLM calls per KG answer before fail-open degrade (0 = skip validation entirely)",
    )
    VERIFICATION_TIMEOUT_MS: int = Field(
        default=1000,
        ge=0,
        description="Timeout for the validation LLM call in ms; exceeded => fail-open degrade",
    )
    VERIFICATION_TOKEN_BUDGET: int = Field(
        default=0,
        ge=0,
        description="Cumulative validation token budget (0 = unlimited); exceeded => fail-open degrade",
    )

    # A3（统一执行链·图内规划收敛）：KG 子图 planner 节点的影子对拍 / 切流双闸，
    # 默认**双关**（flag-off 图内私有 LLM 规划链逐位不变）。
    # SHADOW：图内链继续驱动输出，统一 Planner 并行影子运行并落对拍报告
    #   （不一致或影子侧异常只记报告，绝不影响主链路输出——报告先行，
    #    golden 回归零抖动才允许切流，见方案 v2 §8 风险对策）；
    # UNIFIED：planner 节点改由统一 Planner（AgentLoop 同款单步规划，零 LLM）
    #   驱动任务分解（切流），图内私有 LLM 链不再执行；
    # 两 flag 同开 = 切流驱动 + 图内链反向对拍（同样只记报告）。
    KG_PLANNER_SHADOW_ENABLED: bool = Field(
        default=False,
        description="Shadow-compare the KG in-graph planner against the unified planner (report-only; output unchanged)",
    )
    KG_PLANNER_UNIFIED_ENABLED: bool = Field(
        default=False,
        description="Cut KG graph planning over to the unified planner (skip the private in-graph LLM planner chain)",
    )

    # Stage F2b-d（R-P2-3 Graph 动态化·schema 感知生成）：text2cypher 生成
    # 提示的 {schema} 改用与校验同源的「测试数据域文档摘要 + 发现态 graph.schema」
    # 渲染（替代裸格式/静态中文标签描述为权威源）；关闭（默认）= 基线裸
    # graph.schema 逐位等价；渲染/读取异常 fail-open 回裸 graph.schema。
    KG_SCHEMA_AWARE_GENERATION_ENABLED: bool = Field(
        default=False,
        description="Use the discovered+curated schema render (same source as validation) in text2cypher generation prompts",
    )

    # Stage F2b-d（R-P2-3·证据池）：任务管线成功执行结果统一进观测
    # meta["evidence"]（content ≤4000 字符 + source），供分层验证器
    # L2 sources / L3 grounding 消费；关闭（默认）= meta 无 evidence 键，
    # 与基线逐位等价（L3 维持无证据 fail-open）。
    EVIDENCE_POOL_ENABLED: bool = Field(
        default=False,
        description="Attach successful tool execution results to observation meta['evidence'] for verifier L2/L3 consumption",
    )

    # Stage F2b-d（R-P2-3·L3 接通）：分层验证器 L3 grounding 总闸——两处
    # 生产调用点原硬编码 require_grounding=False，改读本键；关闭（默认）
    # = 基线逐位等价。
    VERIFIER_GROUNDING_ENABLED: bool = Field(
        default=False,
        description="Enable LayeredVerifier L3 grounding (require_grounding) at production call sites",
    )

    # ------------------------------------------------------------------
    # Text2SQL 执行后端（WrenAI 语义层集成）
    # ------------------------------------------------------------------
    # 默认 mysql：直连 DATABASE_URL（既有行为，零变化）。
    # "wren"：SQL 交给 Wren 语义引擎执行（MDL 语义建模/指标定义/RLAC 生效）；
    # wrenai 未安装或项目路径无效时**降级回 MySQL**（steps 留痕），绝不因
    # 后端缺失打断问答主链路。WrenAI 项目需先经 CLI 准备
    # （wren_project.yml + target/mdl.json），参考 WrenAI 上游仓
    # github.com/WrenAI/WrenAI（sdk/wren-langchain）。
    TEXT2SQL_BACKEND: str = Field(
        default="mysql",
        description="Text2SQL execution backend: mysql (default) | wren",
    )
    # Text2SQL 的**目标库**。空 = 用应用自身的 ``DATABASE_URL``（接线前的既有行为，
    # 逐位等价）。之所以要这个键：法律库由 ``data/init_mysql.sql`` 单独建
    # （compose 里的 ``legal_db`` + ``MYSQL_*``），而会话/产物这些应用元数据在
    # 另一个库里——两处都读 DATABASE_URL 时，text2sql 只能看见元数据表，
    # 「民法典有多少条」这类问题结构上就答不对（2026-10-25 录制 evals 基线时实测）。
    # 单源见 ``components/text2sql/target_db.py``（schema / 执行 / dry-run / 缓存键共用）。
    TEXT2SQL_DATABASE_URL: str = Field(
        default="",
        description="Target DB URL for Text2SQL schema retrieval and execution; empty falls back to DATABASE_URL",
    )
    WREN_PROJECT_PATH: str = Field(
        default="",
        description="Wren project directory containing wren_project.yml and target/mdl.json",
    )
    WREN_DATA_SOURCE: str = Field(
        default="duckdb",
        description="WrenEngine data_source for direct-engine path (wren_langchain absent)",
    )
    WREN_CONNECTION_INFO: str = Field(
        default="{}",
        description="WrenEngine connection_info JSON for direct-engine path",
    )
    WREN_QUERY_LIMIT: int = Field(
        default=1000,
        ge=1,
        description="Default max rows returned by Wren-backed Text2SQL execution",
    )
    # R-P2-4（F2b-c）Text2SQL 加固（rag-optimization §6）：
    # dry-run = 校验节点对词法通过的 SQL 跑 `EXPLAIN` 试跑，失败喂既有重试环；
    # 基础设施故障（连不上库等）fail-open 不误杀 SQL。默认关 = 基线逐位等价。
    TEXT2SQL_DRY_RUN_ENABLED: bool = Field(
        default=False,
        description="EXPLAIN dry-run in sql_validation node (R-P2-4); infra errors fail-open",
    )
    # schema 缓存：按 (DATABASE_URL, 问题) 缓存 INFORMATION_SCHEMA 拉取结果；
    # 0 = 关闭 = 基线逐位等价（每问现拉）。
    TEXT2SQL_SCHEMA_CACHE_TTL_SECONDS: int = Field(
        default=0,
        ge=0,
        description="TTL for cached MySQL schema retrieval per (DATABASE_URL, question); 0 disables (R-P2-4)",
    )

    # ------------------------------------------------------------------
    # 上下文隔离 / 记忆整理（§9.1 C 组）
    # ------------------------------------------------------------------

    # 大中间产物的**内联阈值**（字符）。超过则外置到 artifact store，
    # 主状态只留句柄（artifact_id + preview + 摘要）。<=0 表示永不外置
    # （退化为旧行为：内容原样进 state）。
    ARTIFACT_INLINE_THRESHOLD: int = Field(
        default=2000,
        description="Char threshold above which tool/subgraph output is offloaded to the artifact store; <=0 disables",
    )
    # artifact store 的有界容量（条数 / 字节数），LRU 淘汰。
    ARTIFACT_STORE_MAX_ITEMS: int = Field(
        default=200, description="Max artifacts kept in the in-memory artifact store"
    )
    ARTIFACT_STORE_MAX_BYTES: int = Field(
        default=8 * 1024 * 1024,
        description="Max total bytes kept in the in-memory artifact store",
    )
    # F1b2·M-P1-5（obs:// 接线，与渐进上下文共用）：ContextEngine 超长观测
    # 隔离时把正文真正外置到 ArtifactStore，isolated_refs 落
    # `obs://{artifact_id}`（经 read_artifact 可取回，与记忆索引指针共用同
    # 一条取回通道）；默认关 ⇒ 隔离引用与基线逐位一致（仍是
    # `obs://{step_id}` 裸引用）；外置失败 fail-open 回裸引用。
    CONTEXT_ISOLATE_PERSIST_ENABLED: bool = Field(
        default=False,
        description="Persist oversize observation isolation into the artifact store so obs:// refs are fetchable via read_artifact",
    )
    # 记忆整理：同主题摘要的「时间相邻」阈值（天）。<=0 表示只按主题分组。
    MEMORY_CONSOLIDATION_MAX_GAP_DAYS: float = Field(
        default=7.0,
        description="Max gap (days) between summaries merged into one during consolidation; <=0 ignores time",
    )
    MEMORY_CONSOLIDATION_MAX_GROUPS: int = Field(
        default=10, ge=1, description="Max consolidation groups produced per run"
    )

    # ------------------------------------------------------------------
    # 控制平面 / 预算（§9.1 D 组）
    # ------------------------------------------------------------------

    # Token 预算：请求级硬上限。**0 = 不限额**（默认，行为与接线前一致）。
    TOKEN_BUDGET_PER_REQUEST: int = Field(
        default=0,
        description="Hard token budget per request; 0 disables enforcement (meter-only)",
    )
    # 子 Agent 的默认预算切片；0 表示按「已有子 Agent 数」均分请求额度。
    TOKEN_BUDGET_PER_AGENT: int = Field(
        default=0,
        description="Default token budget per sub-agent; 0 means fair-share of the request budget",
    )
    TOOL_CALLS_PER_REQUEST: int = Field(
        default=0,
        ge=0,
        description="Hard tool-call budget per request; 0 disables enforcement",
    )
    # 控制平面运行登记表容量（有界，FIFO 淘汰已终态记录）。
    CONTROL_PLANE_MAX_RUNS: int = Field(
        default=500, ge=1, description="Max agent runs tracked by the in-process control plane"
    )
    # Phase 25（Ch10 §10.17 六失败模式运行时防线）：控制平面准入。
    # 0 = 不限（默认 = 接线前行为，逐位等价）；>0 时超限 spawn 抛
    # AdmissionDenied（fail-closed：调用方不得静默绕过）。
    CONTROL_PLANE_MAX_CONCURRENCY: int = Field(
        default=0,
        ge=0,
        description="Max concurrent non-terminal agent runs (admission); 0 disables the limit",
    )
    CONTROL_PLANE_MAX_DEPTH: int = Field(
        default=0,
        ge=0,
        description="Max delegation depth for agent runs (admission); 0 disables the limit",
    )
    # T21（规划 approval-recovery · 运行态落库）：控制平面运行态的持久化。
    # 默认**关**——关 = 运行登记/状态迁移只存进程内存，不 import 持久层、不建表、
    # 不读写、响应体形状不变（GET 重启后仍 404，与基线逐位等价，纪律 1）；
    # 开 = spawn 与每次状态迁移经**有界后台写队列 + 单写线程**落 `agent_run_states`
    # （游标在事件的什么位置都不会被同步 SQL 卡住，纪律 6 ②），GET 单查与列表在内存
    # miss 时回落到库（重启后仍可查历史终态）。写库失败 **fail-open**（纪律 2）：
    # 只 warning + `stats().persist_errors` 计数，运行照跑、内存态不回滚。
    # 装配判据唯一源 = `agentix/runtime/control_plane.py::resolve_run_state_store`。
    RUN_STATE_PERSIST_ENABLED: bool = Field(
        default=False,
        description=(
            "Persist control-plane agent run states (spawn + every transition) to the "
            "database so GET /agents/runs survives restarts; default off = in-process "
            "memory only, baseline-equal (no persistence import, no table, no reads/"
            "writes, unchanged response shape); writes go through a bounded background "
            "queue and failures are fail-open (warning + stats().persist_errors)"
        ),
    )
    # Phase H · W3（PhaseH_高性能运行时优化规划 §4）：运行时治理五件套。
    # ADMISSION_CONTROL_ENABLED：准入三态（放行/排队/拒绝 429）挂 Control
    # Plane 入口（spawn 前 await 排队，运行计数复用平面 active_count，
    # 终态自动释放）；默认关 = 直连 spawn 现行为逐位等价。
    ADMISSION_CONTROL_ENABLED: bool = Field(
        default=False,
        description="Enable three-state run admission (admit/queue/429) before Control Plane spawn; default off",
    )
    # 准入全局并发上限：0 = 回落 CONTROL_PLANE_MAX_CONCURRENCY（两者皆 0 = 不限）。
    ADMISSION_MAX_CONCURRENT_RUNS: int = Field(
        default=0,
        ge=0,
        description="Global admission concurrency; 0 falls back to CONTROL_PLANE_MAX_CONCURRENCY (both 0 = unlimited)",
    )
    # 排队深度：0 = 不排队（超限即拒）；>0 时入队等位，等待超时（模块
    # 常量 30s）仍拒绝。
    ADMISSION_MAX_QUEUE: int = Field(
        default=0,
        ge=0,
        description="Admission wait-queue depth; 0 = reject immediately at capacity",
    )
    # LG-1-1：只读节点的原生节点级重试（langgraph 1.x 的 `add_node(retry_policy=)`）。
    # 默认关 ⇒ 注册处**完全不传** `retry_policy`，图执行与基线逐位等价（纪律 1）。
    # 覆盖面只有 6 个只读节点；`execute_task` / `code_agent` 永不挂——整节点重跑会
    # 重放工具副作用（指导文档铁律，LG-1-1 验收第 ② 条把它钉成用例）。
    NODE_RETRY_ENABLED: bool = Field(
        default=False,
        description="Attach LangGraph native retry_policy to the 6 read-only graph nodes; default off",
    )
    # LG-3-2：主图节点的原生超时（langgraph 1.x 的 `add_node(timeout=)`），单位秒，
    # 作用在**每次节点尝试**上。`0`（默认）= 关 ⇒ 注册处**完全不传** `timeout`
    # 形参，与基线逐位等价（纪律 1）；策略对象由
    # `orchestration/langgraph/policies.py::node_timeout` 集中构造（LG-0-3）。
    # 边界（AGENTS.md 纪律 6 ②）：超时依赖 asyncio 协作式取消，只在节点**让出
    # 事件循环**的时刻生效——同步阻塞/整段不让出的节点杀不掉；同步节点更是
    # 编不过（`compile().validate()` 直接抛 ValueError）。
    NODE_TIMEOUT_SECONDS: float = Field(
        default=0.0,
        ge=0,
        description="Per-attempt wall-clock cap for main-graph nodes (seconds); 0 = off (timeout kwarg not passed)",
    )
    # LG-3-5：有副作用节点（execute_task / code_agent）的原生错误补偿
    # （langgraph 1.x 的 `add_node(error_handler=)`）。默认关 ⇒ 注册处**完全不传**
    # `error_handler`，与基线逐位等价（纪律 1）。开 = 节点重试耗尽（无 retry_policy
    # 时即首次失败）后不再是整轮 500：handler 往 hitl_pending_approvals 写一条
    # 已闭合的补偿记录（decided_by=system:node_error_handler），并把一条如实的
    # 失败回执写进 messages 后路由到 END。handler **不重跑节点**（副作用铁律）。
    # 记录写失败按纪律 2 fail-open：只告警，失败收尾照常。
    NODE_ERROR_HANDLER_ENABLED: bool = Field(
        default=False,
        description="Attach native error_handler compensation to side-effecting nodes (execute_task/code_agent); default off",
    )
    # RETRY_BUDGET_ENABLED：run 级共享重试预算桶（task/tool/retrieval 三层
    # 共用 token，防 3×3×3 放大）+ 工具/检索层可重试性分类审计闸；
    # 默认关 = 各层现行为逐位等价。LG-1-4 只在部署面写推荐值 true
    # （.env.example / staging override），代码默认仍为 false。
    RETRY_BUDGET_ENABLED: bool = Field(
        default=False,
        description="Share a per-run retry token budget across task/tool/retrieval layers; default off",
    )
    RETRY_BUDGET_TOKENS: int = Field(
        default=6,
        ge=1,
        description="Retry tokens per run when RETRY_BUDGET_ENABLED is on",
    )
    # RETRY_BACKOFF_ALIGNED：三层（tool/task/retrieval）自研退避统一换成
    # LangGraph ``RetryPolicy`` 的默认形状（0.5 / 2.0 / 128.0 / 3 / jitter=True）；
    # 默认关 = 各层保留历史数值，flag-off 逐位等价（纪律 1）。
    RETRY_BACKOFF_ALIGNED: bool = Field(
        default=False,
        description="Align task/tool/retrieval retry backoff with LangGraph RetryPolicy defaults; default off",
    )
    # ADAPTIVE_TOOL_TIMEOUT_ENABLED：工具超时 = min(spec.timeout, 剩余 run
    # 预算)（run deadline 经 RequestScope 传播）；默认关 = spec 原值。
    ADAPTIVE_TOOL_TIMEOUT_ENABLED: bool = Field(
        default=False,
        description="Clip per-tool timeout to remaining run budget (RequestScope deadline); default off",
    )
    # LOAD_SHEDDING_ENABLED：七级降级阶梯（L0 全量 … L6 礼貌拒绝），输入
    # event-loop lag / 准入队列深度 / 下游熔断态，优先甩后台任务；L6 与
    # 准入拒绝合流（429）。默认关 = 零采样零甩负荷。
    LOAD_SHEDDING_ENABLED: bool = Field(
        default=False,
        description="Enable seven-level load shedding (lag/queue/downstream inputs; background first, L6 polite reject); default off",
    )
    # 隔离舱池尺寸（roadmap E3）：0 = 不限（默认 = 接线前逐位等价）。
    # LLM 池复用既有 LLM_MAX_CONCURRENCY 全局限流，不另设新池。
    BULKHEAD_RETRIEVAL_SIZE: int = Field(
        default=0,
        ge=0,
        description="Bulkhead pool size for retrieval downstream; 0 = unlimited",
    )
    BULKHEAD_NEO4J_SIZE: int = Field(
        default=0,
        ge=0,
        description="Bulkhead pool size for Neo4j/Cypher tools; 0 = unlimited",
    )
    BULKHEAD_MCP_SIZE: int = Field(
        default=0,
        ge=0,
        description="Bulkhead pool size for MCP stdio downstream; 0 = unlimited",
    )
    BULKHEAD_WEB_SEARCH_SIZE: int = Field(
        default=0,
        ge=0,
        description="Bulkhead pool size for external web search; 0 = unlimited",
    )
    # 应用级共享 httpx.AsyncClient（消灭 rerank/embedding per-request 创建）：
    # 0 = 关（默认，每次请求新建与基线逐位等价）；>0 = 共享池连接上限。
    SHARED_HTTP_MAX_CONNECTIONS: int = Field(
        default=0,
        ge=0,
        description="Process-shared httpx.AsyncClient pool limit; 0 disables sharing (per-request clients)",
    )
    # Phase H · W4（PhaseH_高性能运行时优化规划 §4）：持久化与 SSE 出口批处理。
    # 事件批写：redis_stream 后端把 xadd/save/rpush 按批经 pipeline 提交
    # （满批即 flush，否则 FLUSH_MS 定时 flush）；两键任一为 0 = 关（逐条
    # 写 = 基线）。memory/jsonl 后端不受影响。
    # 执行中按实修订：规划初版默认 50/20，但批写使 publish 后 flush 前
    # 不可见（破坏 phase23 `wait(timeout=0)` 即时可见契约与「连接错误在
    # publish 内传播」语义）⇒ 默认 0/0=关，推荐启用值 50/20（.env.example）。
    EVENT_BATCH_MAX_SIZE: int = Field(
        default=0,
        ge=0,
        description="Batch size for Redis Stream event writes (with EVENT_BATCH_FLUSH_MS; 50/20 recommended); 0 disables batching",
    )
    EVENT_BATCH_FLUSH_MS: int = Field(
        default=0,
        ge=0,
        description="Flush interval (ms) for pending batched event writes; 0 disables batching",
    )
    # 轨迹异步写：record() 只入队，后台 writer 落 sink（关停/测试经
    # flush_trajectory_writes 兜底）；默认关 = 同步直写现行为。
    TRAJECTORY_ASYNC_WRITE_ENABLED: bool = Field(
        default=False,
        description="Queue trajectory records to a background writer instead of synchronous sink append; default off",
    )
    # SSE 出口门控（HTTP 层，service 层帧语义不变）：token 帧 50ms 窗口合并
    # 为 delta + 有界队列背压（满则丢非关键帧，保终态帧）；默认关/0 = 逐帧
    # 直通。
    SSE_COALESCE_ENABLED: bool = Field(
        default=False,
        description="Coalesce streaming token frames into 50ms delta batches at the SSE egress; default off",
    )
    SSE_QUEUE_SIZE: int = Field(
        default=0,
        ge=0,
        description="Bounded SSE egress queue; full drops non-critical frames (terminal frames never dropped); 0 disables",
    )
    # Phase 25：Peer 独立预算单源（与 manager.PeerBudget 默认值一致，
    # tests 断言两处相等防漂移；经 lg_supervisor 的 budget_factory 注入）。
    MULTIAGENT_PEER_MAX_STEPS: int = Field(
        default=10,
        ge=0,
        description="Per-peer max steps budget injected into Manager; mirrors PeerBudget.max_steps",
    )
    MULTIAGENT_PEER_MAX_MESSAGES: int = Field(
        default=20,
        ge=0,
        description="Per-peer max messages budget injected into Manager; mirrors PeerBudget.max_messages",
    )
    # Phase 25：进程内 asyncio 消息总线（书 §10.3 Message Bus）。
    # 默认关 = 点对点直连现行为逐位等价；开启后 Peer 消息经 bus 发送，
    # 支持 peer 粒度取消（bus.cancel）与 discover/status 观测。
    MULTIAGENT_BUS_ENABLED: bool = Field(
        default=False,
        description="Route multi-agent peer messages through the in-process asyncio bus; default off",
    )
    # Stage F2c·A-P2-1：子代理隔离三件套（ephemeral_prompt / skip_memory /
    # 独立 session+budget + 父记忆只读快照 + 心跳保活 + live registry）。
    # 默认关 = supervisor 现行为逐位等价；开启后子代理只见任务面、
    # 父记忆活上下文被摘除（唯一记忆入口 = 只读快照）。
    MULTIAGENT_SUBAGENT_ISOLATION_ENABLED: bool = Field(
        default=False,
        description="Enable subagent isolation (ephemeral task-face prompt, skip live memory, per-session budget+registry); default off",
    )
    # Phase H · W5（PhaseH_高性能运行时优化规划 §4）：多智能体通信压缩。
    # SUBAGENT_RESULT_CONTRACT_ENABLED：Peer 结果进黑板换结构化契约面
    # {status,claims,evidence_refs,artifact_refs,confidence,cost}，证据条目
    # 经 obs:// 外置成引用（完整输出仍在 ManagerOutcome.peer_results）；
    # 默认关 = 黑板仍走全文 bound_result_face 逐位等价。
    SUBAGENT_RESULT_CONTRACT_ENABLED: bool = Field(
        default=False,
        description="Replace giant peer result strings on the blackboard with structured SubAgentResult contracts (evidence via obs:// refs); default off",
    )
    # MULTIAGENT_EARLY_EXIT_ENABLED：proposer 带证据且有信息增量即提前收敛
    # （跳过 reviewer 与后续轮次，复用 information_increment 判据）；
    # 默认关 = 每 peer 照常调用逐位等价。
    MULTIAGENT_EARLY_EXIT_ENABLED: bool = Field(
        default=False,
        description="Exit early once proposer evidence is sufficient (has evidence + information increment, reuses information_increment); default off",
    )
    # Phase H · W0（PhaseH_高性能运行时优化规划 §4）：性能真值三旋钮。
    # LATENCY_BREAKDOWN_ENABLED：SSE 终帧注入 breakdown_ms（ttft/retrieval/stream），
    # 供前端耗时芯片与回放消费；默认关 ⇒ 帧与基线逐位等价（守卫 p65 通过）。
    LATENCY_BREAKDOWN_ENABLED: bool = Field(
        default=False,
        description="Attach breakdown_ms (ttft/retrieval/stream) to terminal SSE frames; default off",
    )
    # LATENCY_TOTAL_BUDGET_MS：延迟总预算（毫秒）。0 = 不限额（默认）；
    # >0 超限仅 warning + record_degraded("latency_budget")，fail-open 不中断流。
    LATENCY_TOTAL_BUDGET_MS: int = Field(
        default=0,
        ge=0,
        description="End-to-end stream latency budget in ms; 0 disables (over = warn + degraded counter only)",
    )
    # EVENT_LOOP_LAG_METRIC_ENABLED：后台采样事件循环调度滞后（0.5s 间隔，
    # 上报 event_loop_lag_seconds 直方图）。默认关 = 不起任务零开销。
    EVENT_LOOP_LAG_METRIC_ENABLED: bool = Field(
        default=False,
        description="Enable background event-loop lag sampler (event_loop_lag_seconds); default off",
    )

    # ------------------------------------------------------------------
    # 感知工具结果限量 / 人工审批（§9.1 B 组）
    # ------------------------------------------------------------------

    # 工具返回给模型的**下游**结果上限（数据库侧 LIMIT 管不到单行体量与写语句）。
    TOOL_RESULT_MAX_ROWS: int = Field(
        default=100, description="Max rows returned to the model from a tool; <=0 disables row limiting"
    )
    TOOL_RESULT_MAX_CHARS: int = Field(
        default=8000,
        description="Max chars returned to the model from a tool; <=0 disables truncation",
    )
    # 高风险动作的人工审批旁路。默认**关**（未接线时审批门为直通）。
    HUMAN_REVIEW_ENABLED: bool = Field(
        default=False,
        description="Require human approval for medium/high/critical risk tool calls (fail-closed)",
    )

    # A 组：终态完成度校验（**确定性**，零 LLM 调用）。
    # 默认**关**：只把校验结论记录进 metadata / 轨迹，**不改变**响应状态。
    # 开启后，「环境事实判定为未完成」会把响应标为 degraded。
    # 演进顺序：先观测「模型自认完成 vs 环境事实」的差异，再决定是否强制。
    COMPLETION_VALIDATOR_STRICT: bool = Field(
        default=False,
        description="Let the deterministic completion validator mark responses degraded when incomplete",
    )

    # ------------------------------------------------------------------
    # OpenAI compatibility (使用 LLM 配置)
    # ------------------------------------------------------------------

    @property
    def OPENAI_API_KEY(self) -> Optional[str]:
        return self.LLM_API_KEY

    @property
    def OPENAI_API_BASE(self) -> Optional[str]:
        return self.LLM_BASE_URL

    @property
    def OPENAI_MODEL(self) -> str:
        return self.LLM_MODEL

    # Anthropic models
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-3-opus-20240229"

    # Vision models (for image analysis)
    VISION_API_KEY: Optional[str] = None
    VISION_BASE_URL: Optional[str] = None
    VISION_MODEL: str = "gpt-4-vision-preview"

    # Image generation models (for creating images)
    IMAGE_GENERATION_API_KEY: Optional[str] = None
    IMAGE_GENERATION_BASE_URL: Optional[str] = Field(
        default="https://open.bigmodel.cn/api/paas/v4",
        description="Image generation API base URL"
    )
    IMAGE_GENERATION_MODEL: str = Field(
        default="cogView-4-250304",
        description="Image generation model name"
    )
    IMAGE_GENERATION_SIZE: str = Field(
        default="1024x1024",
        description="Default image generation size"
    )

    # Milvus vector database
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    # 测试数据集领域：向量集合默认改为 legal（旧集合遗留，本期不使用）
    MILVUS_COLLECTION: str = "legal"
    MILVUS_INDEX_TYPE: str = "IVF_FLAT"
    MILVUS_METRIC_TYPE: str = "IP"
    # Phase H·W2：IVF 搜索探针数（原 milvus_impl 硬编码 16 配置化；默认值
    # 与基线逐位等价，>=1）
    MILVUS_NPROBE: int = 16

    # Embeddings
    EMBEDDING_PROVIDER: str = Field(default="openai", description="Embedding provider")
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_BASE_URL: Optional[str] = Field(default=None, description="Embedding API base URL")
    EMBEDDING_DIMENSION: int = 1536

    # Reranker configuration
    RERANK_ENABLED: bool = Field(default=True, description="Enable reranking")
    RERANK_PROVIDER: str = Field(default="custom", description="Rerank provider: cohere, jina, voyage, custom")
    RERANK_BASE_URL: Optional[str] = Field(default=None, description="Rerank API base URL")
    RERANK_ENDPOINT: str = Field(default="/rerank", description="Rerank endpoint path")
    RERANK_MODEL: str = Field(default="bge-reranker-large", description="Rerank model name")
    RERANK_API_KEY: Optional[str] = None
    RERANK_MAX_CANDIDATES: int = Field(default=20, description="Max candidates for reranking")
    RERANK_TOP_N: int = Field(default=6, description="Top N results after reranking")
    RERANK_TIMEOUT: int = Field(default=30, description="Rerank API timeout in seconds")
    RERANK_SCORE_FUSION_ALPHA: Optional[float] = Field(default=None, description="Score fusion alpha parameter")

    # Legacy reranker compatibility
    @property
    def RERANKER_PROVIDER(self) -> str:
        return self.RERANK_PROVIDER

    @property
    def RERANKER_API_KEY(self) -> Optional[str]:
        return self.RERANK_API_KEY

    @property
    def RERANKER_MODEL(self) -> Optional[str]:
        return self.RERANK_MODEL

    @property
    def RERANKER_API_URL(self) -> Optional[str]:
        return self.RERANK_BASE_URL

    @property
    def RERANKER_TOP_K(self) -> int:
        return self.RERANK_TOP_N

    # Web search tooling
    SERPAPI_KEY: Optional[str] = None
    SERPAPI_BASE_URL: str = Field(
        default="https://serpapi.com/search",
        description="Base URL for SerpAPI requests",
    )
    SERPAPI_TIMEOUT: float = Field(
        default=15.0,
        description="SerpAPI request timeout in seconds",
    )
    SEARCH_RESULT_COUNT: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Default number of search results to return",
    )

    # Redis cache
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URI",
    )
    # R3.2 单 Redis-Stack 实例 + DB 分片：db0 = cache + checkpoint，db1 = Celery。
    # checkpoint 必须与 cache 同库：RediSearch 只能在 db0 建索引，db!=0 时
    # `asetup()` 直接报 "Cannot create index on db != 0"，checkpointer 会静默降级
    # MemorySaver（见 agentix/infrastructure/persistence/checkpointer.py）。
    # 因此这里牺牲「FLUSH 级别隔离」换「key 前缀隔离」：checkpoint 键统一 `checkpoint:`
    # 前缀，cache 键是 `semantic:` / `history:`，不重叠。要拿回 FLUSH 隔离只能给
    # checkpoint 单开一个 Redis 实例（改这个 URL 即可）。
    REDIS_CACHE_URL: str = Field(
        default="redis://localhost:6379/0",
        description="R3.2: cache 分片（db0）",
    )
    REDIS_CELERY_URL: str = Field(
        default="redis://localhost:6379/1",
        description="R3.2: Celery broker/backend 分片（db1）",
    )
    REDIS_CHECKPOINT_URL: str = Field(
        default="redis://localhost:6379/0",
        description="R3.2: LangGraph checkpoint 分片（必须 db0，RediSearch 只能在 db0 建索引）",
    )
    REDIS_CACHE_EXPIRE: int = Field(
        default=60 * 60 * 12,
        description="Redis cache expiry in seconds",
    )
    REDIS_CACHE_THRESHOLD: float = Field(
        default=0.92,
        description="Semantic cache similarity threshold",
    )
    REDIS_CACHE_MAX_SIZE: int = Field(
        default=1000,
        description="Maximum number of cached items per namespace",
    )

    # Relational database
    DATABASE_URL: str = "sqlite:///./data/agentix.db"

    # LightRAG configuration
    LIGHTRAG_WORKING_DIR: str = Field(
        default="./data/lightrag",
        description="LightRAG 工作目录，用于存储索引和缓存"
    )
    LIGHTRAG_RETRIEVAL_MODE: str = Field(
        default="hybrid",
        description="LightRAG 检索模式: local, global, hybrid, naive, mix, bypass"
    )
    LIGHTRAG_TOP_K: int = Field(
        default=10,
        description="LightRAG 检索返回的top-k结果数量"
    )
    LIGHTRAG_MAX_TOKEN_SIZE: int = Field(
        default=4096,
        description="LightRAG 文本单元的最大token数量"
    )
    LIGHTRAG_ENABLE_NEO4J: bool = Field(
        default=True,
        description="是否使用Neo4j作为LightRAG的图存储后端"
    )
    LIGHTRAG_ENABLE_MILVUS: bool = Field(
        default=False,
        description="是否使用Milvus作为LightRAG的向量存储后端（默认使用内置向量存储）"
    )
    INIT_LIGHTRAG_ON_BUILD: bool = Field(
        default=True,
        description="Docker构建时是否初始化LightRAG"
    )
    LIGHTRAG_INIT_LIMIT: Optional[int] = Field(
        default=None,
        description="LightRAG初始化时的文档数量限制，None为全部"
    )

    # Neo4j configuration
    NEO4J_URI: str = "bolt://neo4j:7687"
    NEO4J_USER: Optional[str] = None
    NEO4J_PASSWORD: Optional[str] = None
    NEO4J_DATABASE: str = "neo4j"
    NEO4J_DEFAULT_GRAPH_QUERY: str = "MATCH (a)-[r]-(b) RETURN a, r, b LIMIT 100"
    NEO4J_GRAPH_CACHE_PATH: str = "data/neo4j/graph.json"
    NEO4J_MAX_CONNECTION_LIFETIME: Optional[int] = None
    NEO4J_BOOTSTRAP_JSON: bool = True
    NEO4J_BOOTSTRAP_FORCE: bool = False
    # R-P0-4：单例 schema 周期刷新秒数（>0 时下次取用在锁内 refresh_schema，
    # 刷新失败保留旧 schema + warning；<=0 回退首连刷一次的旧行为）
    NEO4J_SCHEMA_REFRESH_SECONDS: float = 300.0
    # R-P0-4：Cypher 执行超时秒数（>0 经 query_readonly 限时执行；
    # 0 = 不限时，与基线逐位等价）
    NEO4J_QUERY_TIMEOUT_SECONDS: float = 0.0
    # Phase H·W2：Cypher 成本护栏（GraphCostEstimator 纯字符串预检：
    # RETURN */无界变长/OPTIONAL MATCH 堆叠 → 拒绝；简单单 RETURN 尾追加
    # LIMIT；默认关 = 零预检零改写，与基线逐位等价）
    NEO4J_COST_GUARD_ENABLED: bool = False
    # 测试数据集领域种子（Neo4j 测试数据本体），由 scripts/legal_kg_to_csv.py 转换灌库
    NEO4J_LEGAL_JSON_PATH: str = "data/legal/legal_seed.json"
    # 测试数据集 LightRAG 语料（数据条目全文 + 示例说明全文），供 scripts/init_lightrag.py --source json 读取
    LEGAL_LIGHTRAG_JSON_PATH: str = "data/legal/legal_lightrag_corpus.json"

    # Agent behaviour
    MAX_ITERATIONS: int = 10
    # LG-2-6：图执行落盘时机（LangGraph 1.2 原生 `durability` 三档）。
    # - sync：每步写完 checkpoint 才开始下一步（崩溃最多丢当前步）；
    # - async：每步后台写、下一步不等（**框架默认**，与升级前行为一致）；
    # - exit：只在整轮退出 / 中断 / 报错时写（步骤历史会稀疏）。
    # Literal 让非法取值在 Settings 实例化（启动）即报错，不许静默回落（纪律 1）。
    GRAPH_DURABILITY: Literal["sync", "async", "exit"] = "async"
    AGENT_TIMEOUT: int = 300
    # 批次 B1（audit-2026-09-28 §2 B1）：流式**单事件间隔**看门狗——与
    # AGENT_TIMEOUT（整流墙钟）解耦。`astream_events.__anext__()` 静默挂起
    # （非 LLM 节点卡死 / 网络黑洞）时墙钟检查在循环体内永不执行 ⇒ 用
    # `asyncio.wait_for` 给每次取事件包 idle 超时，超时走既有 error+done
    # 兜底契约并归还并发槽位。默认 60s；`>0`。
    STREAM_IDLE_TIMEOUT: float = Field(
        default=60.0,
        gt=0,
        description="Max seconds of silence between stream events before emitting error (watchdog)",
    )
    # Phase 15：非流式 ask 走 agentix.agent.AgentLoop（L3 harness）支路。
    # 默认 False = 生产仍走 L2 LangGraph；True 时仅影响 ask（stream 恒 L2）。
    AGENT_LOOP_ENABLED: bool = False
    # Phase 15 配套：run_agent_loop_ask 的 Replanner 接线（RetryReplanner 上限）。
    # 0（默认）= 不接 replanner（与基线逐位等价）；>0 = 失败步后最多重规划
    # N 次（连续同因熔断仍由 RetryReplanner.same_failure_threshold 把守）。
    AGENT_MAX_REPLANS: int = Field(
        default=0,
        ge=0,
        description="RetryReplanner budget for the AGENT_LOOP ask branch (0 = replanner not wired)",
    )
    # P3（岗位对照·渐进上下文，主循环一次性构建缺口）：AgentLoop 每步重建
    # 上下文的 token 预算。0（默认）= 只在启动时构建一次（trajectory=(),
    # budget=0，与基线逐位等价）；>0 = 每步执行前按累计观测重建上下文并按
    # 该预算确定性裁剪（重建异常 ⇒ context_build_failed 终态，不 hang）。
    AGENT_CONTEXT_BUDGET_TOKENS: int = Field(
        default=0,
        ge=0,
        description="Per-step context rebuild token budget for AgentLoop (0 = one-shot build, baseline-equal)",
    )
    # Phase H·W1 段级输出预算：ContextPort.build（ContextEngine +
    # SimpleContextBuilder）对轨迹段（工具/检索观测）的 token 配额。0（默认）
    # = 不设段配额（与基线逐位等价）；>0 = 累计超过配额的观测**整体外置**
    # （obs:// 可取回引用，复用既有 ArtifactStore offload 通道）而非截断丢数据。
    TOOL_OUTPUT_BUDGET_TOKENS: int = Field(
        default=0,
        ge=0,
        description="Segment token budget for trajectory tool/retrieval observations in ContextPort.build (0 = off; over-budget rows externalize to obs:// refs instead of truncating)",
    )
    # F1c·A-P1-2（统一执行链 Stage F）：LLM 规划器 + TodoWrite 工具。默认
    # False = planner 仍为 SingleStepPlanner（与基线逐位等价）、不声明不装配
    # todo_write；True = AgentLoop 支路 planner 换 LLMPlanner（结构化输出、
    # 执行表白名单过滤、失败 fail-open 回单步语义），并把 todo_write（整体
    # 替换、单 in_progress、JSON 落盘可恢复）挂进声明与装配。父闸
    # AGENT_LOOP_ENABLED（默认 False）——本键仅在 AgentLoop 支路生效。
    AGENT_LLM_PLANNER_ENABLED: bool = Field(
        default=False,
        description="Enable LLM planner (structured plan output, whitelisted tools) for AgentLoop + todo_write tool (default off = SingleStepPlanner, no todo_write)",
    )
    # F1d·A-P1-3（统一执行链 Stage F）：并发工具轮。1（默认）= 串行逐位等价；
    # >1 = 同轮批量并发——L2 检索聊天 `_execute_tool_calls` 与 L3 AgentLoop
    # 批路径共用一把旋钮：派发前逐调用授权门（HUMAN_REVIEW 通道，未获批
    # fail-closed）、危险工具（有效风险 MEDIUM+ = L1+）强制串行、批宽按剩余
    # 步数预算裁剪、观测按声明序落位。「家政工具不计进度」：当前两面在途
    # 工具全为检索/act（无家政在途），机制留口见 multiagent-optimization
    # A-P1-3 注记。父闸 = 本键自身（>1 即开）。
    AGENT_TOOL_ROUND_CONCURRENCY: int = Field(
        default=1,
        ge=1,
        description="Within-round tool batch concurrency for chat tool loop + AgentLoop (1 = serial baseline-equal)",
    )
    # F1e·A-P1-4（统一执行链 Stage F）：HITL 进环。默认 False = 高风险工具
    # 拦下后走既有 review_required 快速失败（与基线逐位等价）。True 时三子句
    # 生效：① 图内 execute_task 遇 review_required 挂起进图（langgraph
    # interrupt + checkpointer 快照，恢复值 approve ⇒ 落批准后重试一次、
    # 其余 fail-closed 快速失败）——仅 py3.12+ 注入，py3.10 诚实降级回
    # review_required（3.10 async interrupt 必死锁，见 test_p32 特征化）；
    # ② 闸开 + 无 approver + 无决策面 ⇒ REJECT(no_approver_wired_fail_closed)
    # 沿 sidecar 语义，不留永远没人批的 PENDING；③ 裁决顺序
    # deny→hook→ask强制→allow 契约锁定（source-code-study Codex 序）。只对
    # 图内任务链生效（L2/L3 维持 F1d 派发门 fail-closed）。父闸 =
    # HUMAN_REVIEW_ENABLED；非 stream ask 才声明进环意图（stream 恒 L2）。
    HITL_IN_LOOP_ENABLED: bool = Field(
        default=False,
        description="F1e A-P1-4 in-loop HITL: suspend review_required in execute_task via langgraph interrupt (approve resumes + one retry) + no-approver REJECT + deny/hook/ask/allow order lock (py3.12+ only, py3.10 degrades; parent HUMAN_REVIEW_ENABLED)",
    )
    # T18（规划 approval-recovery §4 · Q7 已裁决：Postgres 为真源）：HITL pending
    # 落库。默认**关**——关 = pending 维持进程内存（FIFO 封顶 200），不建表、不
    # 读写新表，行为与基线逐位等价（纪律 1）；开 = 挂起写 `hitl_pending_approvals`
    # （库侧未决同样封顶 200，溢出**显式拒绝**不静默挤行——口径见
    # `agentix/infrastructure/persistence/hitl_pending_store.py` 模块注释）、
    # 裁决同库闭合、启动期回捞未决 pending 进内存可裁决、跨实例（本实例内存没有
    # 该 pending）从库判定不再 404。落库失败显式可观测（error 日志 + 回执
    # persist_error 标记 + persist_errors 计数），绝不静默降级成纯内存宣称成功。
    # 治理层拒绝语义不动（fail-closed，`.qoder/rules/code-exec.md` §7）；
    # 装配判据唯一源 = `agentix/policies/hitl/hitl_bridge.py::_resolve_pending_store`。
    HITL_PENDING_PERSIST_ENABLED: bool = Field(
        default=False,
        description="Persist HITL pending approvals to the database (source of truth per Q7): suspend writes the row, decision closes it, boot-time restore pulls unresolved rows back, cross-instance decide falls back to the store (no more 404); default off = in-process memory only, baseline-equal (parent HUMAN_REVIEW_ENABLED at wiring time)",
    )
    # T19（规划 approval-recovery · Q9 已裁决：到期自动拒绝）：审批等待时限。
    # **0（默认）= 关 = 现行为**：不建扫描任务、不读库、`stats()` 键集合不变
    # （纪律 1 flag-off 逐位等价）；>0 才启用扫描。量的是「pending 存活时长」
    # （挂起时刻起算），与 `AGENT_TIMEOUT` 的「单次图调用墙钟」是两把尺子：
    # 两者同时可达时**先到者生效**，且归因不同（超时拒 = `decided_by=system:deadline`）。
    # 扫描的库面来自 T18（`HITL_PENDING_PERSIST_ENABLED`），因此本键 >0 而落库
    # 未开时不会生效，启动期显式告警一次（不静默假装在管）。
    HITL_APPROVAL_DEADLINE_S: float = Field(
        default=0.0,
        ge=0.0,
        description="Approval wait deadline in seconds: 0 = off (baseline-equal, no sweep task, no store read); >0 = pending older than this is auto-rejected (reason=auto_reject_timeout, decided_by=system:deadline) by a background sweep over the pending store (requires HITL_PENDING_PERSIST_ENABLED)",
    )
    # P4（岗位对照·异常闭环 4.3）：RollbackPort 生产实现 + 计划态权限切换。
    # 默认 False = 不声明不装配 enter/exit_plan_mode（与基线逐位等价）；True =
    # 注册「计划态」工具：enter 先把 executor.policy 快照进 RollbackStore 再切到
    # 只读（max_risk_level=L0），exit 按 checkpoint 恢复原策略（保存/恢复语义）。
    AGENT_ROLLBACK_ENABLED: bool = Field(
        default=False,
        description="Register enter/exit_plan_mode tools backed by the RollbackPort snapshot store (plan-mode permission switch; default off)",
    )
    # P4（岗位对照·异常闭环 4.4）：失败 turn durable 边界补写 + 先持久化后发布。
    # 默认 False = 行为与基线逐位等价（ask 失败时历史可能 user-user 相邻）；
    # True = ask 抛 HTTP 前 / 流式 error 帧发布前，先补写一条 assistant 边界消息
    # （context_overflow 类显式排除，见 breakpoint_resume.failure_boundary_content）。
    FAILURE_BOUNDARY_ENABLED: bool = Field(
        default=False,
        description="Persist a durable assistant boundary message before publishing a failed turn (excludes context_overflow; default off)",
    )
    # P5（岗位对照·Proactive 5.1-5.4）：cron 定时任务调度——默认**关**（strangler）
    # flag-on = 后台 tick 循环（CRON_TICK_SECONDS 周期跑 CronScheduler.tick）+
    # cron_create/cron_list/cron_delete/cron_run 工具挂进声明清单（L1 → 审批半）
    # flag-off = 零接线：不启动循环、不注册工具、不建 data/cron/jobs.json
    # （与基线逐项一致）。任务执行超时/卡死窗口是代码内常量（CRON_JOB_TIMEOUT_S
    # =120s、CRON_STALE_MS=600000ms）——不变量级上界，不进配置面。
    CRON_ENABLED: bool = Field(
        default=False,
        description="Enable the background cron scheduler loop and the cron_* tool family (default off)",
    )
    CRON_TICK_SECONDS: int = Field(
        default=60,
        ge=1,
        description="Background cron scheduler tick interval in seconds (used only when CRON_ENABLED)",
    )
    # T26（队列 PEL 认领 + 优雅停机）：Redis Stream 任务队列的消费 worker。
    # 此前 `TaskQueue.start_worker()` 全仓没有生产调用点——任务能入队但没人消费，
    # 未 ack 的消息永久卡在 PEL 里。默认**关** = 不建任务、不建连、不打日志、
    # 不 import 队列模块（纪律 1：flag-off 与基线逐位等价）；
    # 开 = `_startup` 末尾起一条消费循环（`XAUTOCLAIM` 接手死消费者遗留消息，
    # idle 阈值 / 周期 / 单轮条数是 `task_queue.py` 的模块常量，不进配置面），
    # 关停时由 `_shutdown` 显式收口（`stop_worker()` + cancel）。
    TASK_QUEUE_WORKER_ENABLED: bool = Field(
        default=False,
        description="Start the Redis Stream task-queue consumer worker (XAUTOCLAIM PEL claim included) at startup; default off",
    )
    # P1（岗位对照·任务执行）：任务型路由与任务执行管线（默认**关**）。
    # flag-on = router 意图 task-query（提示词段 + 白名单，换一次前缀）→
    # execute_task 节点（拆解 → plan_waves 波次并行 → ToolExecutor 治理执行 →
    # LayeredVerifier 分层校验 → completed/failed/budget_exceeded/verify_failed
    # 四终态）+ 两个 side-effect 写工具声明/装配；flag-off = 路由白名单、
    # 提示词、工具声明、图可达性与基线逐位等价（节点占位不改变任何路径）。
    TASK_EXEC_ENABLED: bool = Field(
        default=False,
        description="Enable task-type routing (task-query -> execute_task) and the task execution pipeline; default off",
    )
    # LG-3-4：恢复期幂等——`execute_task` 节点内挂起后续跑会重跑挂起点之前的
    # 步骤（LangGraph 节点重入），带副作用的步骤不能重放两遍。开 = 已完成步骤
    # 落 `run_step_ledger`（键：thread + 节点执行作用域 + 步骤指纹），同一作用域
    # 内恢复时跳过；默认关 = 不建表、零读写，与基线逐位等价（纪律 1）。
    RECOVERY_STEP_LEDGER_ENABLED: bool = Field(
        default=False,
        description="Persist completed task steps (run_step_ledger) and skip them when execute_task is re-run after an in-loop suspension; default off",
    )
    # LG-3-6：优雅关闭——SIGTERM/SIGINT 先对在跑的每次图执行请求 drain
    # （langgraph 1.2 原生 ``RunControl``，调用级，每次运行新建），等到超步
    # 边界留下可恢复 checkpoint 再退出；默认关 = 不装信号处理器、不新建控制面、
    # ``astream_events`` 不传 ``control`` 形参，与基线逐位等价（纪律 1）。
    RUN_CONTROL_ENABLED: bool = Field(
        default=False,
        description="Drain in-flight graph runs on SIGTERM/SIGINT via native RunControl (default off = no signal handler, no control kwarg)",
    )
    # drain 的**上限**（秒）：到点仍在跑的运行不再等——信号处理函数起一条
    # daemon 计时线程，超时 ``os._exit`` 强制退出（Docker 里 uvicorn 走 CLI，
    # `__main__` 里的 ``timeout_graceful_shutdown`` 管不到它，所以硬上限必须
    # 由进程自己持有）。只在 ``RUN_CONTROL_ENABLED`` 开时生效。
    RUN_DRAIN_TIMEOUT_S: float = Field(
        default=30.0,
        ge=1.0,
        description="Upper bound (seconds) for graceful drain; force exit if runs are still active at the deadline",
    )
    # Phase 21（Skills 渐进披露）：SKILL.md 元数据常驻注入 + read_skill 惰性读正文。
    # 默认 False = 零注入零注册（Strangler，flag-off 行为字节等价）。
    SKILLS_ENABLED: bool = False
    # Skills 根目录（相对 CWD）；可指到仓库外的自定义目录。
    SKILLS_DIR: str = "skills"
    # F1b2·M-P1-4（memory-optimization §5 AGENTS 级联）：指令分层升级为
    # 全局（~/.agentix/，常驻）→ 项目根（常驻）→ 子目录（消息触达才加载）
    # 三层，hash 去重与 4k/12k 预算沿既有机制；配套 read_agents_file
    # 按需取回工具（L0 只读）。默认关 ⇒ build_instructions 输出与基线
    # 逐字节一致（零全局层零子目录层）。
    AGENTS_CASCADE_ENABLED: bool = Field(
        default=False,
        description="Cascade instruction files global (~/.agentix) -> project root -> message-touched subdirs + read_agents_file on-demand tool",
    )
    # ── 上下文工程 L2 接线（A/B/C/D，默认全关 ⇒ flag-off 与基线逐位等价）──
    # B（⑥ 指令分层进生产链路）：把 AGENTS.md/CLAUDE.md 级联块注入 LangGraph
    # 主链路 general-query 的 system 消息（此前 build_instructions 仅 L3 支路
    # 在用）。flag-on 顺序：静态 system 常量 → 指令块 → memory_context →
    # 历史窗口 → 本轮；块内容经指纹缓存，文件不变不重读盘。
    LG_INSTRUCTIONS_ENABLED: bool = Field(
        default=False,
        description="Inject cascaded instruction blocks (AGENTS.md/CLAUDE.md) into the L2 general-query system prompt",
    )
    # C（⑤ token 级历史窗口）：>0 时 message_window 在条数窗口之内再按 token
    # 预算**从尾部向前**保留（0 = 关，仅 MAX_HISTORY_MESSAGES 条数裁剪，
    # 与基线逐字节等价）；无论如何至少保留本轮消息。
    LG_TOKEN_HISTORY_BUDGET: int = Field(
        default=0,
        description="Token budget for the L2 history window (0 = off, count-based window only)",
    )
    # D（⑤ 状态尾注 / 元信息注入）：把「轮次、历史量、预算余量」作为结构化
    # 状态追加到**本轮 human 消息尾部**（瞬态，只进本次 ainvoke，不写回
    # checkpointer 历史；动态量放尾部 ⇒ 不打穿静态前缀/KV Cache）。
    LG_STATUS_TAIL_ENABLED: bool = Field(
        default=False,
        description="Append structured status tail (turn/budget) to the latest human message; transient, never in the static prefix",
    )
    # P2（岗位对照·Skills/Registry/动态调度）：三个默认关的工具面灰度开关。
    # TOOL_SELECTION_ENABLED = 把 tool_policy 注入图 configurable（tool_selection
    # 节点的声明级过滤链从「有读取点无注入方」的死读变为生产可用；flag-off 不注入
    # 该键，config 与基线逐位等价）。TOOL_SELECTION_MAX_RISK = 注入策略的声明级
    # 风险上限（执行层有效级判定不受此影响，见 selection.py 分工说明）。
    # TOOL_DEFERRED_TOOLS = 逗号分隔的延迟工具名：非空时这些工具退出常驻
    # get_active_tools()（注册表仍保留 spec），并把 tool_search 召回工具挂进
    # 声明清单（核心常驻、长尾按需召回）；空（默认）= 不延迟、不声明，
    # 声明清单与基线逐位等价。
    TOOL_SELECTION_ENABLED: bool = Field(
        default=False,
        description="Inject configurable.tool_policy (declaration-layer tool visibility filter) into graph config",
    )
    TOOL_SELECTION_MAX_RISK: str = Field(
        default="L1",
        description="max_risk_level for the injected ToolSelectionPolicy (L0-L3)",
    )
    TOOL_DEFERRED_TOOLS: str = Field(
        default="",
        description="Comma-separated tool names deferred from resident schemas; non-empty declares the tool_search recall tool",
    )
    # TOOL_RESULT_CONTRACT_ENABLED = 返回值类型契约：handler 成功返回值必须
    # JSON 可序列化，否则立即落 failed（契约违规不重试）。flag-off 沿用现状
    # （data 原样透传，与基线逐位等价）——下游 SSE/落盘才炸改为执行点诚实失败。
    TOOL_RESULT_CONTRACT_ENABLED: bool = Field(
        default=False,
        description="Enforce JSON-serializable handler results in ToolExecutor.execute (0 = legacy passthrough)",
    )
    # A2b（统一执行链 Stage A）：Tool Gateway 收编闸——true = 五项署名旁路
    # （search/cypher_query/predefined_cypher/microsoft_graphrag_query/
    # text2sql_query）的节点执行缝（kg_gateway.run_governed）改经 ToolExecutor
    # 治理链；flag-off（默认）节点直连，与基线逐位等价。
    TOOL_GATEWAY_ENFORCED: bool = Field(
        default=False,
        description="Route the 5 bypassed KG/retrieval tool executions through the ToolExecutor governance chain",
    )
    # P4（岗位对照·异常闭环 4.1+4.2，借鉴 hermes tool_guardrails.py）：
    # 工具层异常闭环两件事。4.1 三计数器失败分级（同参连续失败 / 工具级失败 /
    # 幂等结果哈希不变）+ warn/block/halt 判级 + 工具专属恢复建议回喂 + mutation
    # 成功即清零；4.2 重复结果 stub（第 2 次字节级重复起只折叠上下文表示，
    # 工具仍真实执行，轮询不豁免）。默认 False = executor/replanner/task_retry/
    # 结果出层全部零调用（与基线逐位等价）。
    TOOL_GUARDRAILS_ENABLED: bool = Field(
        default=False,
        description="Enable tool-failure guardrails (three-counter warn/block/halt tiering + recovery advice + duplicate-result stub; default off)",
    )
    # Phase 21（Hooks 生命周期）：on_tool_before/after、on_step_end、on_run_end、
    # on_error 五类旁路钩子。默认 False = 组合根零接线（executor/loop 零调用）。
    HOOKS_ENABLED: bool = False
    # ------------------------------------------------------------------
    # Phase S（沙箱与执行边界加固，§24.3 T0）——全部默认关，flag-off 与基线逐位等价。
    # ------------------------------------------------------------------
    # G1：同步（非协程）工具 handler 会占住事件循环，asyncio.wait_for 对它无效。
    # 打开后 executor 用 asyncio.to_thread 下放，使 timeout_ms 真正可截断。
    TOOL_SYNC_OFFLOAD_ENABLED: bool = Field(
        default=False,
        description="Offload non-coroutine tool handlers to a worker thread so timeout_ms can actually truncate them (default off)",
    )
    # §24.3 T1 ⑧：handler 类型契约。注册期就点名「非协程 handler」，而不是等到
    # 一次超时不生效才在人肉排障里发现。默认关 = 不告警（逐位等价）。
    TOOL_HANDLER_CONTRACT_ENABLED: bool = Field(
        default=False,
        description="Warn at registration time when a tool handler is not a coroutine function (its timeout_ms binds only with TOOL_SYNC_OFFLOAD_ENABLED)",
    )
    # G14（§24.2）：同名工具重复注册。默认 False = **不拒绝注册，但照发点名告警**
    # （本批目的就是把"后一份 spec 静默丢弃"变成可 grep 的信号；默认语义仍是
    # 第一次赢 / 最后一次赢，注册结果逐位不变）。True = 在改动 handler 之前抛
    # ValueError，fail-closed。诊断默认开、行为开关默认关。
    TOOL_DUPLICATE_REGISTRATION_STRICT: bool = Field(
        default=False,
        description="Refuse (ValueError) a same-name tool re-registration whose ToolSpec differs from the first declaration, instead of only warning (default off = warn and keep first-wins)",
    )
    # §24.3 T2 ⑭（ADR-0007）：执行位置端口。打开后 ToolExecutor 按 `ToolSpec.sandbox`
    # 选执行位置（默认 in_process = 与现状逐位相同；thread = 下放线程；
    # subprocess/container 尚未实现 ⇒ 执行点**拒绝**，不静默退回进程内）。
    # 关 = 完全不查 `sandbox` 字段，等价于加固前。
    EXECUTION_SANDBOX_ENABLED: bool = Field(
        default=False,
        description="Honour the per-tool `ToolSpec.sandbox` declaration when picking where a handler runs (default off = in-process, byte-equal to pre-T2)",
    )
    # §24.3 T2 ⑮（ADR-0007）：阶梯第三档落地。关 = `subprocess` 仍留在
    # 「词汇表 − 已实现」的差集里，声明它的工具在执行点被拒（与 ⑭ 逐位等价）；
    # 开 = 这一档有实现并被解析到，但**本机能不能把资源闸落下去另说**——
    # POSIX 能力探测不过仍然在执行点拒绝，绝不静默退回进程内。
    TOOL_SANDBOX_SUBPROCESS_ENABLED: bool = Field(
        default=False,
        description="Register the `subprocess` execution sandbox tier (default off = the tier stays in the ladder-but-unimplemented gap; enabling it never downgrades a refusal to in-process)",
    )
    # 子进程侧降权到的 uid（−1 = 不降权，沿用父进程 uid）。刻意默认不降：
    # setuid 需要父进程有权限，且降权后连绑定监听端口都可能失败——把它设成非 −1
    # 是一次显式的部署决定，不是一个可以顺手打开的布尔。
    TOOL_SANDBOX_SUBPROCESS_UID: int = Field(
        default=-1,
        description="uid the subprocess sandbox child drops to before running a handler (-1 = keep the parent uid; only meaningful on POSIX)",
    )
    # 额外允许透传给沙箱子进程的环境变量名（逗号分隔），与内置 OS 白名单取并集。
    # 与 MCP 那一对键**刻意不共用**：判据同源（`domain/tool/exec_env.py`），受众不同——
    # 让 MCP 的加法自动放宽沙箱的墙，等于两个安全边界互相授权。
    TOOL_SANDBOX_SUBPROCESS_ENV_EXTRA_KEYS: str = Field(
        default="",
        description="Comma-separated extra environment variable names allowed into the subprocess sandbox child, unioned with the OS allowlist (independent of MCP_SUBPROCESS_ENV_EXTRA_KEYS by design)",
    )
    # code exec（运行时写代码执行任务）：总闸默认**关**。flag-off 时生产链路与
    # 基线**逐位等价**——路由不产 `code-query` 意图、图里 `code_agent` 节点不可达、
    # `code_execute` 工具零注册、提示词与基线逐字节相同。写 Python 意味着执行面
    # 第一次可能跑用户代码，所以它默认关：开启需要显式声明，而不是随上游开关
    # 一起被带起来。
    CODE_EXEC_ENABLED: bool = Field(
        default=False,
        description="Enable the code exec path (router intent code-query -> code_agent node -> code_execute tool); default off = byte-equal to baseline",
    )
    # 总尝试轮数上限（**含首次执行**）：运行层失败（退出码非零 / 超时）把输出
    # 喂回下一轮改码，最多尝试这么多轮（含第一次）；轮数用尽 ⇒ 产出失败回执
    # （含最后一次输出），不假装成功。治理层失败（沙箱不可用 / 风险被拒 /
    # HITL 未批）是**终局**，不进这个循环。
    CODE_EXEC_MAX_ROUNDS: int = Field(
        default=3,
        description=(
            "Maximum total attempt rounds for code exec, including the first execution "
            "(runtime failures feed stdout/stderr back for a rewrite); default 3"
        ),
    )
    # 单次代码执行超时（秒）。这是**一次执行**的上限，不是整轮自修的预算；
    # 工具契约里 `timeout_s` 的取值域是 1–120 秒。
    CODE_EXEC_TIMEOUT_S: float = Field(
        default=20.0,
        description="Per-execution timeout in seconds for code exec (tool contract clamps timeout_s to 1-120); default 20",
    )
    # G2：MCP stdio 子进程默认继承主进程全部环境变量，其中含 LLM/SERPAPI 等密钥。
    MCP_SUBPROCESS_ENV_SCRUB: bool = Field(
        default=False,
        description="Scrub MCP stdio subprocess environment down to an OS-critical allowlist (default off = inherit everything)",
    )
    # 额外允许透传给 MCP 子进程的环境变量名（逗号分隔），与内置白名单取并集。
    MCP_SUBPROCESS_ENV_EXTRA_KEYS: str = Field(
        default="",
        description="Comma-separated extra environment variable names additionally allowed into MCP stdio subprocesses when scrub is on",
    )
    # G11：SQLAlchemy echo 会把完整 SQL 连同参数打进标准库 logging，绕过 loguru 结构出口。
    # None = 沿用旧行为（由 DEBUG 推断）；显式设值即覆盖。
    DB_ECHO: Optional[bool] = Field(
        default=None,
        description="Override SQLAlchemy engine echo; None keeps the legacy DEBUG-derived behaviour",
    )
    # G6：「生产身份」不该由 debug 开关反推。空 = 沿用旧推断逻辑，部署应显式声明。
    APP_ENV: str = Field(
        default="",
        description="Explicit application environment (development|staging|production); empty keeps the legacy DEBUG-derived value",
    )
    # G6：热重载与 DEBUG 解耦。None = 旧行为（reload=DEBUG）。
    UVICORN_RELOAD: Optional[bool] = Field(
        default=None,
        description="Override uvicorn reload; None keeps the legacy DEBUG-derived value",
    )
    # §24.3 T1 ⑪：`/settings/{kb_id}` 的读写此前只要求「已登录」，任何认证用户都能
    # 读改任意 kb（含跨租户）。G9 把响应里的密钥掩成 ***，但掩码不是授权：
    # `llm_base_url` / `embedding_base_url` 仍是可写字段，改指攻击者的端点就等于把
    # 目标租户的提问内容整批外送。打开后：读要求 kb 与调用者同租户，写在此基础上再要求 admin。
    # 前置条件（缺一会把设置页变成 403）：迁移已回填 kb.tenant_id，且该租户内确有 admin 账号。
    SETTINGS_AUTHZ_ENABLED: bool = Field(
        default=False,
        description="Enforce tenant ownership on GET /settings/{kb_id} and admin role on PUT (default off = any authenticated user)",
    )
    # P0-2(c)：agent 图执行的进程内并发上限（防 LLM 上游雪崩），在 chat.process_agent_query 施加
    AGENT_MAX_CONCURRENCY: int = Field(
        default=16,
        ge=1,
        description="Max concurrent agent graph executions (semaphore in front of graph.ainvoke)",
    )
    # P0-1: LangGraph checkpointer 类型。redis = AsyncRedisSaver（需 RediSearch，
    # 初始化失败自动降级 memory）；memory = MemorySaver（进程内，重启即失）。
    CHECKPOINTER_TYPE: str = Field(
        default="redis",
        description="LangGraph checkpointer backend: 'redis' or 'memory'",
    )
    # Phase K 任务 1：Redis 路径初始化失败的 fail-closed 判据（默认关 = 维持既有降级基线）。
    CHECKPOINTER_FAIL_CLOSED: bool = Field(
        default=False,
        description="checkpointer 初始化失败时是否拒绝启动（true=fail-closed，绝不落 MemorySaver）；"
        "显式 `CHECKPOINTER_TYPE=memory` 不受影响",
    )
    # T22：Redis 不可用降级 MemorySaver 后按定间隔重试升级。0（默认）= 关闭重试循环
    # ⇒ 不建后台任务、不加日志、与基线逐位等价（纪律 1）。>0 才起任务，节奏就是这个值，
    # 刻意不做指数退避（重试目标同进程的一个 Redis 连接，退避换不来成功率）。
    CHECKPOINTER_RETRY_INTERVAL_S: float = Field(
        default=0.0,
        ge=0.0,
        description="checkpointer 降级 MemorySaver 后重试升级为 Redis 的间隔秒数（0=关闭，默认）",
    )
    # T22：每进程重试上限，防重试风暴；仅在 `CHECKPOINTER_RETRY_INTERVAL_S > 0` 时生效。
    CHECKPOINTER_RETRY_MAX_ATTEMPTS: int = Field(
        default=3,
        ge=0,
        description="checkpointer 升级重试的每进程尝试上限"
        "（仅 CHECKPOINTER_RETRY_INTERVAL_S>0 时生效；0=不尝试）",
    )
    # Phase 20: EventPort 生产后端。memory（默认）= 进程内事件总线（现状）；
    # jsonl = 追加式事件日志（events/event_log.jsonl，重启恢复 pending/dedup）；
    # redis_stream（Phase 23）= Redis Stream（XADD/XREAD，需 Redis 运行时；
    # 无 Redis 时诚实 skip 非伪造）。
    EVENT_PORT_BACKEND: str = Field(
        default="memory",
        description="EventPort backend: 'memory' (default), 'jsonl' (persisted event log), or 'redis_stream' (needs Redis)",
    )
    # Phase 23（Ch6 §6.2 出站通知）：notify_user 工具（默认**关**）。
    # flag-on 注册 notify_user——本批默认实现 = 落 EventPort
    # （type=user_notification）+ ops 可查，不接真实邮件/短信通道；
    # 出站即副作用，MEDIUM 风险走既有 HITL 闸门。
    NOTIFY_USER_ENABLED: bool = Field(
        default=False,
        description="Register the notify_user outbound-notification tool (records to EventPort; default off)",
    )
    # P2-1 满意度反馈闭环（默认**关**）：flag-on 才注册 POST /api/v1/feedback
    # （落 EventPort type=message_feedback），flag-off 路由不存在=与基线等价。
    FEEDBACK_ENABLED: bool = Field(
        default=False,
        description="Enable the user feedback endpoint (POST /api/v1/feedback -> EventPort message_feedback); default off",
    )
    # 反馈 reason 的 PII 脱敏开关（默认**开**——脱敏是安全项，开为安全默认）。
    FEEDBACK_PII_REDACT: bool = Field(
        default=True,
        description="Redact PII in feedback reason before persisting (default on = safe default)",
    )
    # Phase 24（Ch3 遗忘三动作生产化）：记忆整理触发 handler（默认**关**）。
    # flag-on 后 task_queue 的 memory_consolidation handler 才会真正执行
    # consolidate（幂等时间窗 + dry_run 支持）；flag-off 直接 skipped 零存储触碰。
    MEMORY_CONSOLIDATION_ENABLED: bool = Field(
        default=False,
        description="Enable the memory_consolidation task handler (consolidate trigger; default off)",
    )
    # Phase 24（Ch3 Agentic RAG）：知识分支空检索时的查询改写重试（默认**关**）。
    # flag-on = create_kb_query 内改写重试 ≤2 次仍空则诚实声明；
    # 每次尝试进 trajectory（kind=retrieval, name=agentic_rag_rewrite）。
    AGENTIC_RAG_ENABLED: bool = Field(
        default=False,
        description="Enable agentic RAG query-rewrite retries (<=2) on empty knowledge retrieval; default off",
    )
    # Phase H·W1：复杂度感知 fast-path——高置信确定性输入（寒暄、单条数据条目
    # 直取）前置短路，跳过 LLM 意图分类器（零模型构造、零分类调用）。
    # 关（默认）⇒ 路由链与基线逐位等价；命中记 llm_calls_saved_total。
    ROUTING_FASTPATH_ENABLED: bool = Field(
        default=False,
        description="Skip the LLM intent classifier on high-confidence deterministic inputs (greetings/single-statute); default off",
    )
    # T8：路由结果跨请求缓存（进程内、有界 LRU + TTL，见
    # ``agentix/application/agents/routing_cache.py``）。命中即跳过整次 LLM 意图
    # 分类（零客户端构造、零请求）。**默认关** ⇒ 缓存模块连 import 都不发生
    # （惰性 import 在 flag 判真之后），路由链与基线逐位等价。
    # 失效口径是 Q1 未裁决时的**保守版「换代失效」**：router 提示词实际生效文本、
    # 实际使用的 Router 类、主模型标识、``router_enabled`` 消融开关任一变化即换代；
    # 语料换代**不**失效，进程重启与 TTL 到期自然失效（该模块 docstring 写明了边界）。
    ROUTING_CACHE_ENABLED: bool = Field(
        default=False,
        description="Cache routing results in-process (bounded LRU + TTL) so repeated identical questions skip the LLM intent classifier; default off",
    )
    # Phase 28（Ch9 §9.3.2 需求澄清决策）：歧义输入先澄清再执行（默认**关**）。
    # flag-on = general_handler 各入口 _maybe_clarify 前置（词表/confidence 确定性
    # 启发，零新增 LLM）；澄清应答走消息区，不入 HITL、不计失败、不改路由终态；
    # 会话态 clarify_armed 单轮约束（连续澄清至多 1 次）。
    CLARIFY_ROUTING_ENABLED: bool = Field(
        default=False,
        description="Enable deterministic clarify gate before handlers (ambiguous input asks first; default off)",
    )
    # Phase 28（Ch10 §10.6 预算意识）：预算只读可见（默认**关**）。
    # flag-on = 状态栏常量键 max_iter（构造侧写入，同配置前缀稳定，KV Cache 纪律）
    # + 剩余 ≤2 步时轨迹 append budget_notice（append-only；动态剩余量**拒绝**
    # 进状态栏——状态栏位于静态前缀，动态值每步换前缀会打穿 KV Cache）。
    BUDGET_NOTICE_ENABLED: bool = Field(
        default=False,
        description="Expose read-only iteration budget (constant status-bar max_iter + budget_notice trail); default off",
    )
    # Phase 30（Ch9 §9.8.2 提案落盘）：进化提案三状态 append-only 档案路径。
    # staged/applied/rejected 全记（content_hash 去重）；**不落 evals/baselines/**
    # （与 quality.json 碰撞守卫，jsonl_proposal_sink 构造时 fail-closed 拒绝）。
    EVOLUTION_PROPOSAL_LOG: str = Field(
        default="data/evolution/proposals.jsonl",
        description="Append-only JSONL archive of evolution proposal states (staged/applied/rejected)",
    )
    # Phase 30（Ch9 §9.9 睡眠学习）：离线睡眠学习触发 handler（默认**关**）。
    # flag-on 后 task_queue 的 evolution_sleep 才聚合信号 → 只产 staged 提案
    # （不晋升、不触真实端点预检 fail-closed）；flag-off 直接 skipped 零副作用。
    EVOLUTION_SLEEP_ENABLED: bool = Field(
        default=False,
        description="Enable the evolution_sleep task handler (sleep learning proposes only; default off)",
    )
    # P0-3: 错误语义回滚开关。True = 旧版吞异常返 200 + 兜底文案（前端兼容期用）；
    # False（默认）= 结构化错误（4xx/5xx 或 200 + status=degraded + error_code）。
    LEGACY_ERROR_MODE: bool = Field(
        default=False,
        description="True restores legacy swallow-exception-200 behavior (rollback switch)",
    )
    # P1-3: 工具集裁剪。列出的工具名不会进入 tool_selection 的 bind_tools
    # 候选（注册表仍保留 spec，便于随时恢复）。默认空 = 全部启用。
    DISABLED_TOOLS: str = Field(
        default="",
        description="Comma-separated tool names to exclude from get_active_tools()",
    )

    # Knowledge base retrieval
    KB_TOP_K: int = 5
    KB_SIMILARITY_THRESHOLD: float = 0.2
    KB_CHUNK_SIZE: int = Field(
        default=512,
        description="Chunk size used when splitting documents for the knowledge base",
    )
    KB_CHUNK_OVERLAP: int = Field(
        default=80,
        description="Chunk overlap used when splitting documents",
    )
    # 测试数据集领域知识库语料文件（供 kb_ingest 导入）
    KB_DATA_FILE: str = "data/kb/legal_corpus.txt"
    # KB 子图 guardrails 的「本知识库处理什么」说明。空 = 沿用代码内置的
    # 测试数据集文案（逐位等价）；非空则由部署方声明领域边界——语料换域
    # （如法律）时必须同步换这句，否则 guardrails 会按旧域拒答。
    KB_SCOPE_DESCRIPTION: str = Field(
        default="",
        description=(
            "Domain scope text for the KB workflow guardrails; empty keeps the "
            "built-in test-dataset wording (bitwise legacy behavior)"
        ),
    )
    KB_LLM_PROVIDER: str = Field(default="openai", description="LLM provider used by kb_ingest")
    KB_LLM_MODEL: Optional[str] = Field(default=None, description="LLM model used by kb_ingest")
    KB_LLM_API_KEY: Optional[str] = Field(default=None, description="LLM API key for kb_ingest")
    KB_LLM_BASE_URL: Optional[str] = Field(default=None, description="LLM base URL for kb_ingest")
    KB_LLM_TEMPERATURE: float = Field(default=0.3, description="LLM temperature for kb_ingest")
    KB_USE_LLM: bool = Field(default=True, description="Whether kb_ingest uses an LLM pipeline")
    KB_EMBEDDING_PROVIDER: str = Field(default="openai", description="Embedding provider for kb_ingest")
    KB_EMBEDDING_MODEL: Optional[str] = Field(default=None, description="Embedding model for kb_ingest")
    KB_EMBEDDING_API_KEY: Optional[str] = Field(default=None, description="Embedding API key for kb_ingest")
    KB_EMBEDDING_BASE_URL: Optional[str] = Field(
        default=None, description="Embedding base URL for kb_ingest"
    )
    KB_EMBEDDING_DIMENSION: int = Field(default=1024, description="Embedding dimension for kb_ingest")
    KB_RERANK_ENABLED: bool = Field(default=False, description="Whether kb_ingest uses a reranker service")
    KB_RERANK_PROVIDER: Optional[str] = Field(default=None, description="Reranker provider for kb_ingest")
    KB_RERANK_BASE_URL: Optional[str] = Field(default=None, description="Reranker base URL for kb_ingest")
    KB_RERANK_ENDPOINT: Optional[str] = Field(default=None, description="Reranker endpoint for kb_ingest")
    KB_RERANK_API_KEY: Optional[str] = Field(default=None, description="Reranker API key for kb_ingest")
    KB_RERANK_MODEL: Optional[str] = Field(default=None, description="Reranker model for kb_ingest")
    KB_RERANK_MAX_CANDIDATES: int = Field(
        default=20, description="Max candidates for kb_ingest reranker when enabled"
    )
    KB_RERANK_TOP_N: int = Field(default=6, description="Top N results returned by kb_ingest reranker")
    KB_RERANK_TIMEOUT: int = Field(default=30, description="Timeout for kb_ingest reranker requests")
    KB_RERANK_SCORE_FUSION_ALPHA: float = Field(
        default=0.5, description="Score fusion alpha for kb_ingest reranker"
    )
    KB_POSTGRES_SIMILARITY_THRESHOLD: float = Field(
        default=0.5,
        description="Minimum similarity required for structured-KB channel (kb_ingest, Milvus-backed) results after rerank",
    )
    KB_POSTGRES_RERANK_THRESHOLD: float = Field(
        default=0.8,
        description="Minimum rerank score required for structured-KB channel (kb_ingest, Milvus-backed) results",
    )
    KB_RERANK_SCORE_THRESHOLD: float = Field(
        default=0.8,
        description=(
            "DEPRECATED (R-P0-3): no code consumer. Kept for env compatibility; "
            "when set it logs a WARNING and maps onto KB_POSTGRES_RERANK_THRESHOLD. "
            "Use KB_POSTGRES_RERANK_THRESHOLD instead."
        ),
    )
    # R-P1-2 Channel B（F1a）：kb local_search 两路跨通道 rank 融合，
    # 替代「postgres 非空则丢掉 milvus」的丢分逻辑；默认关 = 现行为逐位不变。
    KB_LOCAL_FUSION_ENABLED: bool = Field(
        default=False,
        description=(
            "Fuse postgres/milvus local-search channels via rank fusion "
            "(default off: postgres-first fallback semantics unchanged)"
        ),
    )
    KB_LOCAL_FUSION_METHOD: str = Field(
        default="rrf",
        description="Rank fusion method for KB local search: rrf | zscore (default rrf)",
    )

    # External ingestion service (test folder FastAPI) configuration
    INGEST_SERVICE_URL: Optional[str] = Field(
        default=None,
        description="Base URL for external ingestion service (e.g., http://localhost:8000)",
    )
    INGEST_INCREMENTAL_DEFAULT: bool = Field(
        default=True,
        description="Whether external Excel ingestion should default to incremental updates",
    )
    # 2026-09：调 kb_ingest 时使用的**共享密钥**（对应其 ``KB_API_KEY``）。
    #
    # 刻意**不复用**本服务的 ``API_KEY``：两者信任边界不同——
    # ``API_KEY`` 是**外部调用方**访问 agentix 的凭证，``KB_API_KEY`` 是
    # **内部调用方**（agentix）访问 kb_ingest 的凭证。若复用，任何拿到对外
    # Key 的客户端就能直接用同一把钥匙去调 kb_ingest 的**写入**端点
    # （改 Milvus 集合），属权限抬升。
    #
    # 取值来源：仓库根 ``.env``（该文件的 ``KB_*`` 段同时被 kb_ingest 容器的
    # ``env_file`` 读取，所以两边天然一致，无需额外同步机制）。
    # 留空 = 不带鉴权头，适用于 kb_ingest 未开启 ``KB_REQUIRE_API_KEY`` 的部署。
    KB_API_KEY: str = Field(
        default="",
        description="Shared secret sent as X-API-Key when calling the kb_ingest service",
    )
    FILE_UPLOAD_MAX_MB: int = Field(
        default=2,
        ge=1,
        description="Maximum accepted upload file size in MB for local ingestion",
    )
    # 2026-09（P43 路径安全）：上传根目录的**唯一真源**。
    # 此前 interfaces 层硬编码 ``Path("uploads")``（相对 CWD），而 main.py 的静态挂载
    # 用 ``os.path.dirname(__file__)/../../uploads``（绝对）——两者只在"进程 CWD 恰好是
    # 仓库根"时才指向同一处，属于隐性耦合。更关键的是：application 层的 file_handler /
    # image_handler 需要**同一个根**来判定"这个路径是不是上传目录里的"，
    # 而按分层它不能 import interfaces。故上提到 config。
    # 默认值保持 "uploads"（与旧行为逐位一致，仍按 CWD 解析）。
    UPLOAD_DIR: str = Field(
        default="uploads",
        description="Root directory for uploaded files/images (relative paths resolve against CWD)",
    )
    ENABLE_EXTERNAL_SEARCH: bool = Field(
        default=False,
        description="Whether tools are allowed to perform external network search",
    )
    KB_ENABLE_EXTERNAL_SEARCH: bool = Field(
        default=False,
        description="Augment KB answers with optional external web search",
    )
    KB_EXTERNAL_SEARCH_URL: Optional[str] = Field(
        default=None,
        description="External HTTP endpoint for KB fallback search (e.g., http://localhost:8000/api/search)",
    )
    KB_EXTERNAL_SEARCH_TIMEOUT: float = Field(
        default=20.0,
        description="Timeout in seconds for external KB search requests.",
    )
    # P0-2(a)：检索通道瞬时故障重试接线（kb_retrieval._postgres_search /
    # _milvus_search 施加 @aretry）。检索是只读 POST，重试安全；重试耗尽后
    # 异常照样向上抛，仍按「通道失败」处理（P1-5 语义不变）。
    KB_RETRIEVAL_MAX_ATTEMPTS: int = Field(
        default=2,
        ge=1,
        description="检索通道瞬时故障重试（P0-2(a) 接线）：单次检索最多尝试次数（含首次）",
    )
    KB_RETRIEVAL_RETRY_BASE_DELAY: float = Field(
        default=0.25,
        ge=0.0,
        description="检索通道瞬时故障重试（P0-2(a) 接线）：指数退避基础延迟（秒）",
    )

    # Conversation history retention
    CONVERSATION_HISTORY_TTL: int = Field(
        default=60 * 60 * 24 * 3,
        description="How long to retain chat history in seconds",
    )
    CONVERSATION_HISTORY_MAX_MESSAGES: int = Field(
        default=200,
        description="Maximum number of messages retained per conversation",
    )

    # CORS
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:5173",
        description="Comma-separated list of CORS origins"
    )

    # Ollama configuration
    OLLAMA_BASE_URL: str = Field(
        default="http://localhost:11434",
        description="Ollama service base URL"
    )
    OLLAMA_EMBEDDING_MODEL: str = Field(
        default="nomic-embed-text",
        description="Ollama embedding model for semantic caching"
    )

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS_ORIGINS string to list"""
        if isinstance(self.CORS_ORIGINS, str):
            return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]
        return list(self.CORS_ORIGINS)

    # dev 缺失 SECRET_KEY 时的进程级临时密钥缓存。
    # 必须缓存：create_access_token / decode_access_token 各自调用一次本
    # property，若每次返回新随机值，则签发与校验用的密钥必然不同，
    # dev 环境下 JWT 登录态 100% 校验失败。缓存后进程内稳定、重启仍失效。
    _dev_secret_key_cache: Optional[str] = PrivateAttr(default=None)

    @property
    def resolved_secret_key(self) -> str:
        """显式配置优先；缺失时：非生产环境临时生成（强告警），生产环境直接拒绝启动。"""
        if self.SECRET_KEY:
            return self.SECRET_KEY
        env = getattr(self, "APP_ENV", "") or getattr(self, "ENVIRONMENT", "") or ""
        if not env:
            # 项目未定义 APP_ENV/ENVIRONMENT 字段，用 DEBUG 反推：DEBUG=False 视为生产
            env = "development" if self.DEBUG else "production"
        if env.lower() in ("prod", "production", "staging"):
            raise RuntimeError(
                "SECRET_KEY must be explicitly configured in production/staging. "
                "Runtime random keys invalidate all sessions/tokens on restart."
            )
        if self._dev_secret_key_cache is None:
            self._dev_secret_key_cache = secrets.token_urlsafe(32)
            logger.warning(
                "SECRET_KEY not configured; generated a TEMPORARY value that will "
                "change on restart. Only acceptable in dev."
            )
        return self._dev_secret_key_cache

    @model_validator(mode="before")
    @classmethod
    def _empty_str_to_none_for_unset_optionals(cls, data):
        """``KEY=``（空串）对 ``default=None`` 的字段按「未设置」处理。

        pydantic 无法把 ``""`` 解析成 ``Optional[int]``——运维在 .env 里写了
        空的可选数值项（如 ``NEO4J_MAX_CONNECTION_LIFETIME=``）会直接炸启动。
        空串对这类字段的语义就是「未设置」。仅当字段 default is None 时改写，
        不影响 ``API_KEY=`` 这类 default 本就是 ``""`` 的字符串字段。
        """
        if not isinstance(data, dict):
            return data
        for name, value in list(data.items()):
            if value != "":
                continue
            field = cls.model_fields.get(name)
            if field is not None and field.default is None:
                data[name] = None
        return data

    @model_validator(mode="before")
    @classmethod
    def _deprecated_kb_rerank_score_threshold(cls, data):
        """R-P0-3 阈值键收敛：旧键 ``KB_RERANK_SCORE_THRESHOLD`` 兼容映射。

        rerank 分数阈值唯一现役键是 ``KB_POSTGRES_RERANK_THRESHOLD``
        （旧键无代码消费点，属文档/环境残留）。运维仍设置旧键时：

        - 新键**未**显式给值 ⇒ 发 WARNING 并把旧键值映射给新键；
        - 新键已显式给值 ⇒ 发 WARNING 且新键胜出（旧键被忽略）。
        """
        if not isinstance(data, dict):
            return data
        legacy = data.get("KB_RERANK_SCORE_THRESHOLD")
        if legacy in (None, ""):
            return data
        current = data.get("KB_POSTGRES_RERANK_THRESHOLD")
        if current not in (None, ""):
            logger.warning(
                "KB_RERANK_SCORE_THRESHOLD is deprecated and ignored: "
                "KB_POSTGRES_RERANK_THRESHOLD=%r is set explicitly and wins "
                "(R-P0-3 threshold key convergence)",
                current,
            )
        else:
            logger.warning(
                "KB_RERANK_SCORE_THRESHOLD is deprecated; mapping its value "
                "onto KB_POSTGRES_RERANK_THRESHOLD=%r (R-P0-3 threshold key "
                "convergence). Please migrate to the new key.",
                legacy,
            )
            data["KB_POSTGRES_RERANK_THRESHOLD"] = legacy
        return data

    model_config = {
        "env_file": ".env",
        "case_sensitive": True,
        "extra": "ignore"
    }

    @property
    def NEO4J_USERNAME(self) -> Optional[str]:  # pragma: no cover - convenience alias
        return self.NEO4J_USER


settings = Settings()


# ---------------------------------------------------------------------------
# 展示时区（**非** env 配置项的业务常量）
# ---------------------------------------------------------------------------

#: 用户可见时间/日期所用的时区——跨模块**唯一真源**。
#:
#: 为什么需要它：``datetime.now()`` / ``date.today()`` 取的是**进程本地时区**。
#: 本地开发机是 +08:00、容器部署时 ``TZ`` 通常未设置（=UTC），同一份代码在
#: 两处得出的"今天"相差 8 小时。表现：
#:
#: - 会话标题（``chat_service``）：北京时间 07:00 创建会话显示成"前一天 23:00"；
#: - 数据集有效性判定（``legal_effectiveness``）：``date.today()`` 少一天，
#:   导致"失效当天仍有效"的边界判定错位，"检索截至"标注日期也错。
#:
#: 为什么不做成 ``Settings`` 字段：这是**产品常量**（本服务面向中文测试数据场景），
#: 不是部署旋钮。放进 env 只会多出一个能被配错、且错了只表现为"日期诡异"的开关。
#:
#: 用法::
#:
#:     from agentix.config.settings import DISPLAY_TZ
#:     datetime.now(DISPLAY_TZ)
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
