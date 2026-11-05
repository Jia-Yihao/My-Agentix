"""检索域配置兼容层——委托 agentix.config.settings 统一配置入口。

R1.11 后本文件不再是独立 Settings 类，而是 agentix.config.settings 的
兼容包装：共享字段（SECRET_KEY/DEBUG/MILVUS_HOST 等）委托统一 settings，
检索特定字段（VECTOR_STORE_BACKEND/RETRIEVAL_MIN_SCORE 等）从 env 加载。

所有 ``from agentix.retrieval.settings import settings`` 导入无需改动。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 加载**仓库根** .env（本文件在 agentix/retrieval/ 下，需上溯三级）。
# 原先 parent.parent 指向 agentix/，该路径下没有 .env，dotenv 静默无效。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_REPO_ROOT / ".env", override=False)

from agentix.config.settings import settings as _unified_settings  # noqa: E402


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default).strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


class _RetrievalSettingsCompat:
    """兼容包装：委托统一 settings + 从 env 加载检索特定字段。"""

    _BASE_DIR = Path(__file__).resolve().parent.parent.parent

    def __getattr__(self, name: str):
        return getattr(_unified_settings, name)

    @property
    def APP_NAME(self) -> str:
        return "My Agentix"

    @property
    def ENVIRONMENT(self) -> str:
        # 优先级：显式 APP_ENV > legacy ENVIRONMENT > 由 DEBUG 推断（旧行为，仅为兼容保留）。
        # G6：生产身份不该由一个 debug 开关反推——忘设 DEBUG=False 会让下面的
        # validate_app_settings() 整段静默不生效。
        explicit = (_unified_settings.APP_ENV or "").strip().lower()
        if explicit:
            return explicit
        legacy = os.getenv("ENVIRONMENT", "").strip().lower()
        if legacy:
            return legacy
        return "development" if _unified_settings.DEBUG else "production"

    @property
    def PRODUCTION(self) -> bool:
        return self.ENVIRONMENT in ("prod", "production")

    @property
    def STRICT_VALIDATION(self) -> bool:
        """配置校验的**硬失败面**——比 PRODUCTION 多一个 staging。

        口径与 `config/settings.py::resolved_secret_key` 保持一致（prod/production/staging
        一律严格）。staging 的价值是「用生产的失败语义提前跑一遍」；把它降级成观察环境
        等于把这道校验从发布链路上摘掉。

        这条不是理论担心：`docker/compose.override.staging.yml` 显式写了 `APP_ENV: staging`，
        而 G6 之前 staging 是靠 `DEBUG=false` 反推出 "production" 才拿到 raise 的——
        只改 ENVIRONMENT 优先级而不同步这里，staging 的拒绝启动会**静默消失**。
        """
        return self.ENVIRONMENT in ("prod", "production", "staging")

    @property
    def database_url(self) -> str:
        """R3.1 起与编排域共用同一 DATABASE_URL（单 PG/SQLite 实例），不再回落 retrieval.db。"""
        return _unified_settings.DATABASE_URL

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    # 与仓库实际布局对齐（原先 "backend/data" 目录不存在，永远落在幽灵路径）。
    DATA_DIR = _BASE_DIR / "data"

    @property
    def UPLOAD_DIR(self) -> str:
        """与编排域共用同一上传根（动态委托，测试 monkeypatch 也生效）。

        local_fs 写入侧与 file_handler/image_handler 的 resolve_within_root
        校验侧必须同源，否则检索上传的文件会被判"越界"。
        """
        return str(_unified_settings.UPLOAD_DIR)
    INDEX_DIR = os.getenv("INDEX_DIR", "./data/indexes")

    SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(14 * 24 * 3600)))
    SESSION_HTTPS_ONLY = _bool_env("SESSION_HTTPS_ONLY", "false")

    TRAJECTORY_ENABLED = _bool_env("TRAJECTORY_ENABLED", "true")
    TRAJECTORY_DUMP_ALL = _bool_env("TRAJECTORY_DUMP_ALL", "false")

    # M2-1 拆反向边：原先印在上面那两行（`SESSION_HTTPS_ONLY` 之后）的
    # `LOG_JSON_ENABLED` / `LOG_LEVEL` / `TRAJECTORY_DIR` 已删——它们是通用可观测性配置、
    # 不属检索域，真源上提到 `agentix/config/settings.py`（口径见
    # docs/architecture/分层架构与依赖规则.md §7.4）。这里刻意**不留第二份定义**：
    # 留着就是同一个判定两个源（工程约定 6 ④，改一处漏一处必然漂移）。
    # 删后这三个键经 `_RetrievalSettingsCompat.__getattr__` 自动委托统一 settings
    # ⇒ 全仓任何 `from agentix.retrieval.settings import settings` 读它们仍然可用。

    OUTPUT_FILTER_ENABLED = _bool_env("OUTPUT_FILTER_ENABLED", "true")
    OUTPUT_FILTER_PII_ENABLED = _bool_env("OUTPUT_FILTER_PII_ENABLED", "true")
    OUTPUT_FILTER_HARM_ENABLED = _bool_env("OUTPUT_FILTER_HARM_ENABLED", "false")

    # R2-2 数据条目有效性过滤（指南 §2.2.7①）：默认开启后过滤；存量无字段数据视为现行有效
    EFFECTIVENESS_FILTER_ENABLED = _bool_env("EFFECTIVENESS_FILTER_ENABLED", "true")
    # 答案尾部"检索截至日期"标注
    EFFECTIVENESS_ASOF_ANNOTATION = _bool_env("EFFECTIVENESS_ASOF_ANNOTATION", "true")
    OUTPUT_FILTER_HALLUCINATION_ENABLED = _bool_env("OUTPUT_FILTER_HALLUCINATION_ENABLED", "false")

    # R3-5 Context Engine（架构文档 §24-28 + 指南 L1⑦）
    # authority 排序：缺失 source_authority_level 的存量数据档位 0，稳定排序顺序不变
    CONTEXT_AUTHORITY_RANKING_ENABLED = _bool_env("CONTEXT_AUTHORITY_RANKING_ENABLED", "true")
    # Context Budget（token 级）：0 = 不限额（默认，行为不变）
    CONTEXT_BUDGET_TOKENS = int(os.getenv("CONTEXT_BUDGET_TOKENS", "0"))
    # 大文档隔离（复用 artifact_store）：0 = 关闭（默认——启用需配合取回通道）
    CONTEXT_ISOLATE_THRESHOLD = int(os.getenv("CONTEXT_ISOLATE_THRESHOLD", "0"))

    # R3-6 GroundingValidator（架构文档 §104）：句子级证据锚定校验
    # 观测模式：只产出报告与低分 WARN，不改答案、不拦截（语义级 grounding 属后续演进）
    GROUNDING_VALIDATOR_ENABLED = _bool_env("GROUNDING_VALIDATOR_ENABLED", "true")
    # 句子锚定阈值：bigram 包含度 >= 视为有证据支撑
    GROUNDING_SUPPORT_THRESHOLD = float(os.getenv("GROUNDING_SUPPORT_THRESHOLD", "0.35"))
    # 低于该分数记 WARN（0 = 关闭 WARN）
    GROUNDING_LOW_SCORE_WARN = float(os.getenv("GROUNDING_LOW_SCORE_WARN", "0.6"))
    # R-P2-2 证据门禁（rag-optimization §6）：citation/grounding 由仅观测升级为
    # 可配门禁——observe（默认，与基线逐位等价）| warn（附警告）| refuse（降级拒答）；
    # 未知值 fail-safe 回 observe（门禁误配不得改变基线）。
    EVIDENCE_GATE_MODE = os.getenv("EVIDENCE_GATE_MODE", "observe").strip().lower()

    LLM_CIRCUIT_BREAKER_ENABLED = _bool_env("LLM_CIRCUIT_BREAKER_ENABLED", "true")
    LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(os.getenv("LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"))
    LLM_CIRCUIT_BREAKER_RECOVERY_SECONDS = float(os.getenv("LLM_CIRCUIT_BREAKER_RECOVERY_SECONDS", "30"))

    # 重排服务熔断器。此前 rerank.py 用 getattr(_rs, "RERANK_BREAKER_*", 默认值) 读取，
    # 但是这两个字段从未在此声明过 ⇒ getattr 永远走字面量兜底，环境变量也永远不会被读到。
    # 现在正式声明，默认值与原来兜底的 5 / 60 完全一致（行为零变化）。
    # 默认值为什么与 LLM 的 30s 恢复时间不同：重排模型冷启动/排队更慢，60s 避免频繁半开探测。
    RERANK_BREAKER_FAILURE_THRESHOLD = int(os.getenv("RERANK_BREAKER_FAILURE_THRESHOLD", "5"))
    RERANK_BREAKER_RECOVERY_SECONDS = float(os.getenv("RERANK_BREAKER_RECOVERY_SECONDS", "60"))

    RATE_LIMIT_ENABLED = _bool_env("RATE_LIMIT_ENABLED", "false")
    RATE_LIMIT_RATE = float(os.getenv("RATE_LIMIT_RATE", "2"))
    RATE_LIMIT_BURST = int(os.getenv("RATE_LIMIT_BURST", "20"))

    S3_ENDPOINT_INTERNAL = os.getenv("S3_ENDPOINT_INTERNAL", os.getenv("S3_ENDPOINT", "http://127.0.0.1:9000"))
    S3_ENDPOINT_EXTERNAL = os.getenv("S3_ENDPOINT_EXTERNAL", os.getenv("S3_ENDPOINT", "http://127.0.0.1:9000"))
    S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
    # R3.3：默认凭据与 docker-compose 的 MinIO 服务对齐（MINIO_ACCESS/SECRET_KEY=minioadmin）
    S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
    # R3.3：业务文档 bucket 统一命名 documents（kb-files 旧名废弃）；
    # milvus bucket 由 Milvus 内部使用同一 MinIO 实例的独立 bucket，互不混用。
    S3_BUCKET = os.getenv("S3_BUCKET", "documents")
    S3_REGION = os.getenv("S3_REGION", "us-east-1")

    # R3.2：Celery 分片为 db1（REDIS_CELERY_URL）。这里保持「显式配置才启用
    # Celery」的契约——默认空串走同步兜底，避免开发环境无 Redis 时上传索引直接崩；
    # 部署侧在 .env/compose 中设 CELERY_BROKER_URL=redis://redis:6379/1。
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "")
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "")

    # R3.2：默认指向统一 Redis-Stack 的 cache 分片（db0）；连接失败时
    # get_cache_redis() 优雅降级为不缓存，不影响主链路。
    CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "") or getattr(_unified_settings, "REDIS_CACHE_URL", "")
    CACHE_REDIS_PREFIX = os.getenv("CACHE_REDIS_PREFIX", "agentix_retrieval:retrieve:")
    RETRIEVAL_CACHE_TTL_SECONDS = int(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "600"))
    RETRIEVAL_MAX_DISTANCE = float(os.getenv("RETRIEVAL_MAX_DISTANCE", "1.2"))
    VECTOR_NORMALIZE = _bool_env("VECTOR_NORMALIZE", "true")
    RETRIEVAL_MIN_SCORE = float(os.getenv("RETRIEVAL_MIN_SCORE", "0.0"))
    INDEX_SCORE_IS_SIMILARITY = _bool_env("INDEX_SCORE_IS_SIMILARITY", "false")
    VERIFY_INDEX_INTEGRITY = _bool_env("VERIFY_INDEX_INTEGRITY", "false")
    INDEX_VERSIONS_TO_KEEP = int(os.getenv("INDEX_VERSIONS_TO_KEEP", "3"))
    INDEX_MAX_ATTEMPTS = int(os.getenv("INDEX_MAX_ATTEMPTS", "3"))
    INDEX_RETRY_BASE_DELAY_SECONDS = float(os.getenv("INDEX_RETRY_BASE_DELAY_SECONDS", "2"))
    INDEX_RETRY_MAX_DELAY_SECONDS = float(os.getenv("INDEX_RETRY_MAX_DELAY_SECONDS", "30"))
    INDEX_JOB_TIMEOUT_SECONDS = int(os.getenv("INDEX_JOB_TIMEOUT_SECONDS", "1800"))
    INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS = int(os.getenv("INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS", "7200"))
    INDEX_JOB_STALE_QUEUED_SECONDS = int(os.getenv("INDEX_JOB_STALE_QUEUED_SECONDS", "300"))
    INDEX_JOB_STALE_RUNNING_SECONDS = int(os.getenv("INDEX_JOB_STALE_RUNNING_SECONDS", "1800"))

    EMBEDDING_CACHE_ENABLED = _bool_env("EMBEDDING_CACHE_ENABLED", "true")
    EMBEDDING_CACHE_PATH = os.getenv("EMBEDDING_CACHE_PATH", "").strip()
    EMBEDDING_CACHE_MAX_ENTRIES = int(os.getenv("EMBEDDING_CACHE_MAX_ENTRIES", "200000"))
    # E4 · 启动预热（docs/design/性能与优化路线图.md §2 E4）。三个键默认关/空：
    # 开着它就是在每次启动打一批端点调用，属于行为变更，不作出厂形态（约定 1）。
    EMBEDDING_CACHE_PREWARM_ENABLED = _bool_env("EMBEDDING_CACHE_PREWARM_ENABLED", "false")
    # 清单分隔符：`;`／`；`／换行（**不含半角逗号**——英文问句里的逗号会造出假问法）。
    # 默认空串 ⇒ 即便 flag 开着也只是响亮跳过（reason=empty_list），不打端点。
    EMBEDDING_CACHE_PREWARM_QUERIES = os.getenv("EMBEDDING_CACHE_PREWARM_QUERIES", "")
    EMBEDDING_CACHE_PREWARM_MAX = int(os.getenv("EMBEDDING_CACHE_PREWARM_MAX", "32"))
    # E7 · 召回级语义结果缓存（docs/design/性能与优化路线图.md §2 E7）。默认关：
    # 命中的代价是"复用另一问法的检索结果"，属行为变更 ⇒ 新开关默认关（约定 1）。
    SEMANTIC_CACHE_ENABLED = _bool_env("SEMANTIC_CACHE_ENABLED", "false")
    # τ：余弦相似度下限。0.95 是路线图风险段给的保守值；τ 调到 0.5 就等于把"无关问法"
    # 也算命中——那是串答，不是缓存。
    SEMANTIC_CACHE_TAU = float(os.getenv("SEMANTIC_CACHE_TAU", "0.95"))
    # TTL：比精确键的 RETRIEVAL_CACHE_TTL_SECONDS（600）短一个量级——语义键复用的是
    # 另一句话的结果，陈旧窗口必须更短（时效性由这个窗口兜底，没有主动失效钩子）。
    SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "120"))
    RERANKER_ENABLED = _bool_env("RERANKER_ENABLED", "false")
    RERANKER_MODEL = os.getenv("RERANKER_MODEL", "text-embedding-bge-reranker-v2-m3")

    RETRIEVAL_FETCH_K_MULTIPLIER = int(os.getenv("RETRIEVAL_FETCH_K_MULTIPLIER", "20"))
    RETRIEVAL_FETCH_K_MIN = int(os.getenv("RETRIEVAL_FETCH_K_MIN", "200"))
    RETRIEVAL_MAX_FETCH_K = int(os.getenv("RETRIEVAL_MAX_FETCH_K", "0"))
    RETRIEVAL_FETCH_K_ESCALATE = _bool_env("RETRIEVAL_FETCH_K_ESCALATE", "true")
    RETRIEVAL_FETCH_K_MAX_ESCALATIONS = int(os.getenv("RETRIEVAL_FETCH_K_MAX_ESCALATIONS", "2"))

    # R-P1（F1a）混合检索：BM25 sparse + RRF/zscore 融合 + 精排 + 四阶段观测。
    # 总闸默认关 ⇒ flag-off 与现基线字节等价（总纲：P1/P2 全部 flag 门控，默认关）。
    RETRIEVAL_HYBRID_ENABLED = _bool_env("RETRIEVAL_HYBRID_ENABLED", "false")
    # sparse 子闸（需总闸开）：进程内 BM25 通道（语料 = PostgreSQL chunk 表）
    RETRIEVAL_SPARSE_ENABLED = _bool_env("RETRIEVAL_SPARSE_ENABLED", "false")
    RETRIEVAL_SPARSE_MIN_SCORE = float(os.getenv("RETRIEVAL_SPARSE_MIN_SCORE", "0.0"))
    RETRIEVAL_SPARSE_CACHE_TTL = int(os.getenv("RETRIEVAL_SPARSE_CACHE_TTL", "600"))
    # 融合：rrf（默认，免调参）| zscore；alpha = zscore 下首通道（dense）权重
    RETRIEVAL_FUSION_METHOD = os.getenv("RETRIEVAL_FUSION_METHOD", "rrf").strip().lower()
    RETRIEVAL_RRF_K = int(os.getenv("RETRIEVAL_RRF_K", "60"))
    RETRIEVAL_FUSION_ALPHA = float(os.getenv("RETRIEVAL_FUSION_ALPHA", "0.5"))
    RETRIEVAL_FUSION_TOP_N = int(os.getenv("RETRIEVAL_FUSION_TOP_N", "200"))
    # R-P1-3：hybrid 链默认精排（只在总闸开时生效）；候选上限 → 截断 TopK
    RETRIEVAL_HYBRID_RERANK = _bool_env("RETRIEVAL_HYBRID_RERANK", "true")
    RETRIEVAL_RERANK_CANDIDATES = int(os.getenv("RETRIEVAL_RERANK_CANDIDATES", "20"))
    # T9（approval-recovery-optimization §T9）：rerank 候选分批，降低单请求上游负载。
    # <=0 = 关（默认）⇒ 单次全候选调用，与基线逐位等价（约定 1）；>0 = 每批候选数，
    # 分批调用并按全局 relevance_score 降序融合（同分按原始下标升序，见 rerank.py）。
    # 键沿检索域惯例只同步字段本体 + .env.example 注释块（Q5 保守边，其余三处缺口
    # 已登记台账待追认），不入 配置管理.md / p41 名单 / selfcheck。
    RETRIEVAL_RERANK_BATCH_SIZE = int(os.getenv("RETRIEVAL_RERANK_BATCH_SIZE", "0"))

    # R-P2-1（F2b-b）查询改写/HyDE：双可选独立 flag、默认关 = 基线逐位等价；
    # 改写/扩展失败一律回退原 query（fail-open），键沿 GROUNDING_* 检索域三处同步。
    RETRIEVAL_QUERY_REWRITE_ENABLED = _bool_env("RETRIEVAL_QUERY_REWRITE_ENABLED", "false")
    RETRIEVAL_HYDE_ENABLED = _bool_env("RETRIEVAL_HYDE_ENABLED", "false")

    # 检索优化三闸 + 精排动态
    # 跳过，全部默认关 ⇒ flag-off 与基线逐位等价（检索域 env 键沿既有惯例
    # 只进 .env.example 注释示例块，不入 配置管理.md/p41 名单）。
    RETRIEVAL_SINGLEFLIGHT_ENABLED = _bool_env("RETRIEVAL_SINGLEFLIGHT_ENABLED", "false")
    RETRIEVAL_ADAPTIVE_ENABLED = _bool_env("RETRIEVAL_ADAPTIVE_ENABLED", "false")
    # 级联分数闸：0 = 关（空命中才升级）；>0 = 归一化分数低于该值升级下一级
    RETRIEVAL_CASCADE_MIN_SCORE = float(os.getenv("RETRIEVAL_CASCADE_MIN_SCORE", "0"))
    RERANK_DYNAMIC_SKIP_ENABLED = _bool_env("RERANK_DYNAMIC_SKIP_ENABLED", "false")

    # R-P3（README §15.2 口径③ 实测）：进程内检索器缓存——消灭「每次检索都
    # 重建 embedding 客户端 + 反序列化 FAISS 索引」。默认关 ⇒ 与接线前逐位等价
    # （每次照旧直调 build_retriever）；开启后按
    # backend|index_path|index.faiss(mtime_ns,size)|embedding model+base_url|k 组键，
    # 索引换代即自动失效。键名沿检索域惯例只进 .env.example 注释示例块。
    RETRIEVAL_RETRIEVER_CACHE_ENABLED = _bool_env("RETRIEVAL_RETRIEVER_CACHE_ENABLED", "false")
    RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES = int(
        os.getenv("RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES", "8")
    )

    AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "25"))
    AGENT_MAX_INPUT_TOKENS = int(os.getenv("AGENT_MAX_INPUT_TOKENS", "0"))
    AGENT_MAX_OUTPUT_TOKENS = int(os.getenv("AGENT_MAX_OUTPUT_TOKENS", "0"))

    CONVERSATION_SUMMARY_THRESHOLD = int(os.getenv("CONVERSATION_SUMMARY_THRESHOLD", "0"))
    CONVERSATION_KEEP_RECENT_ROUNDS = int(os.getenv("CONVERSATION_KEEP_RECENT_ROUNDS", "10"))

    LLM_PRICE_INPUT_PER_1M = float(os.getenv("LLM_PRICE_INPUT_PER_1M", "0.0"))
    LLM_PRICE_OUTPUT_PER_1M = float(os.getenv("LLM_PRICE_OUTPUT_PER_1M", "0.0"))
    LLM_ESTIMATE_CHARS_PER_TOKEN = float(os.getenv("LLM_ESTIMATE_CHARS_PER_TOKEN", "4.0"))

    DELETE_APPROVAL_ENABLED = _bool_env("DELETE_APPROVAL_ENABLED", "false")
    DELETE_APPROVAL_TIMEOUT_SECONDS = int(os.getenv("DELETE_APPROVAL_TIMEOUT_SECONDS", str(72 * 3600)))
    DELETE_APPROVERS = _csv_env("DELETE_APPROVERS", "")

    CACHE_REDIS_MAX_CONNECTIONS = int(os.getenv("CACHE_REDIS_MAX_CONNECTIONS", "50"))
    CACHE_REDIS_CONNECT_TIMEOUT_SECONDS = float(os.getenv("CACHE_REDIS_CONNECT_TIMEOUT_SECONDS", "2"))
    CACHE_REDIS_SOCKET_TIMEOUT_SECONDS = float(os.getenv("CACHE_REDIS_SOCKET_TIMEOUT_SECONDS", "2"))

    LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    EMBEDDING_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "15"))
    VECTOR_DB_TIMEOUT_SECONDS = float(os.getenv("VECTOR_DB_TIMEOUT_SECONDS", "5"))
    POSTGRES_TIMEOUT_SECONDS = float(os.getenv("POSTGRES_TIMEOUT_SECONDS", "5"))
    OBJECT_STORAGE_TIMEOUT_SECONDS = float(os.getenv("OBJECT_STORAGE_TIMEOUT_SECONDS", "10"))
    RERANKER_TIMEOUT_SECONDS = float(os.getenv("RERANKER_TIMEOUT_SECONDS", "10"))

    THUMBNAIL_MAX_WIDTH = int(os.getenv("THUMBNAIL_MAX_WIDTH", "400"))

    VECTOR_STORE_BACKEND = os.getenv("VECTOR_STORE_BACKEND", "faiss")
    QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
    QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
    QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "retrieval_kb")

    # 2026-10-06 完整性审查：此处原有 CORS_ORIGINS 属性，默认值含
    # http://localhost:23416 —— 23416 是历史遗留端口，全仓（含 web/vite.config.ts，
    # 实际 5173）无任何引用；且真正生效的 CORS 白名单是
    # agentix/config/settings.py::cors_origins_list（agentix/main.py:55 消费），
    # 本属性全仓无调用者，属死代码，故删除。


settings = _RetrievalSettingsCompat()


def validate_app_settings() -> tuple[list[str], list[str]]:
    """应用启动配置校验。"""
    errors: list[str] = []
    warnings: list[str] = []

    if settings.STRICT_VALIDATION:
        if not settings.RATE_LIMIT_ENABLED:
            errors.append("生产环境必须开启 RATE_LIMIT_ENABLED=true")
        if not settings.OUTPUT_FILTER_ENABLED or not settings.OUTPUT_FILTER_PII_ENABLED:
            errors.append("生产环境必须开启 OUTPUT_FILTER_ENABLED=true 和 OUTPUT_FILTER_PII_ENABLED=true")
        if settings.VECTOR_STORE_BACKEND.lower() == "faiss" and not settings.VERIFY_INDEX_INTEGRITY:
            errors.append("生产环境使用 FAISS 时必须开启 VERIFY_INDEX_INTEGRITY=true")
        if not settings.SESSION_HTTPS_ONLY:
            warnings.append("生产环境建议开启 SESSION_HTTPS_ONLY=true")
        if settings.VECTOR_STORE_BACKEND.lower() == "faiss":
            warnings.append("生产环境仍使用 FAISS：请确认数据规模与单机故障恢复方案")

    if settings.DELETE_APPROVAL_TIMEOUT_SECONDS <= 0:
        errors.append("DELETE_APPROVAL_TIMEOUT_SECONDS 必须为正整数")
    for name in (
        "LLM_TIMEOUT_SECONDS", "EMBEDDING_TIMEOUT_SECONDS", "VECTOR_DB_TIMEOUT_SECONDS",
        "POSTGRES_TIMEOUT_SECONDS", "OBJECT_STORAGE_TIMEOUT_SECONDS", "RERANKER_TIMEOUT_SECONDS",
    ):
        if float(getattr(settings, name)) <= 0:
            errors.append(f"{name} 必须为正数")
    if settings.AGENT_MAX_INPUT_TOKENS < 0:
        errors.append("AGENT_MAX_INPUT_TOKENS 不能为负（0 表示不限制）")
    if settings.AGENT_MAX_OUTPUT_TOKENS < 0:
        errors.append("AGENT_MAX_OUTPUT_TOKENS 不能为负（0 表示不限制）")
    if settings.DELETE_APPROVAL_ENABLED and not settings.DELETE_APPROVERS:
        warnings.append("DELETE_APPROVAL_ENABLED=true 但 DELETE_APPROVERS 为空")
    if settings.RETRIEVAL_MIN_SCORE < 0 or settings.RETRIEVAL_MIN_SCORE >= 1:
        errors.append("RETRIEVAL_MIN_SCORE 必须在 [0, 1) 范围内（0=不过滤）")
    if settings.INDEX_SCORE_IS_SIMILARITY and not settings.VECTOR_NORMALIZE:
        errors.append("INDEX_SCORE_IS_SIMILARITY=true 要求 VECTOR_NORMALIZE=true")
    if settings.VECTOR_STORE_BACKEND.lower() == "milvus" and not settings.VECTOR_NORMALIZE:
        # R3.4：Milvus 集合索引为 IP（内积），仅归一化向量下等价 cosine；
        # 关闭归一化会导致分数口径与 dense.py 漂移。
        errors.append("VECTOR_STORE_BACKEND=milvus 要求 VECTOR_NORMALIZE=true（IP metric ≡ cosine 的前提）")
    if settings.RETRIEVAL_MAX_DISTANCE > 0 and settings.INDEX_SCORE_IS_SIMILARITY:
        warnings.append("RETRIEVAL_MAX_DISTANCE 在相似度口径下不生效，请改用 RETRIEVAL_MIN_SCORE")
    if settings.INDEX_JOB_STALE_RUNNING_SECONDS < settings.INDEX_JOB_TIMEOUT_SECONDS:
        warnings.append(
            f"INDEX_JOB_STALE_RUNNING_SECONDS={settings.INDEX_JOB_STALE_RUNNING_SECONDS} "
            f"小于 INDEX_JOB_TIMEOUT_SECONDS={settings.INDEX_JOB_TIMEOUT_SECONDS}"
        )
    if settings.INDEX_JOB_STALE_QUEUED_SECONDS <= 0:
        errors.append("INDEX_JOB_STALE_QUEUED_SECONDS 必须为正整数")
    if settings.INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS <= 0:
        errors.append("INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS 必须为正整数")
    if (
        (settings.CELERY_BROKER_URL or "").strip()
        and settings.INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS <= settings.INDEX_JOB_TIMEOUT_SECONDS
    ):
        warnings.append("INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS 未大于 INDEX_JOB_TIMEOUT_SECONDS")
    if settings.EMBEDDING_CACHE_ENABLED and settings.EMBEDDING_CACHE_MAX_ENTRIES <= 0:
        errors.append("EMBEDDING_CACHE_MAX_ENTRIES 必须为正整数")
    # E4：上限 <=0 与"关掉预热"不是一回事——前者是配置错误（flag 开着却永远刷 0 条），
    # 后者由 EMBEDDING_CACHE_PREWARM_ENABLED 表达。这里刻意**不**以 flag 为前提：
    # 让非法值在配置期就红，而不是等开了 flag 那天变成一条安静的空跑。
    if settings.EMBEDDING_CACHE_PREWARM_MAX <= 0:
        errors.append("EMBEDDING_CACHE_PREWARM_MAX 必须为正整数（关掉预热请用 _ENABLED=false）")
    # E7：τ 与 TTL 同样**不以 flag 为前提**（与 E4 的 _MAX 同口径）。理由分两种失败形态：
    # τ<=0 是"任何问法都算命中"（串答事故），τ>1 是"永远不命中"（开了等于没开、且没人知道）；
    # TTL<=0 让写入被拒、读侧恒判过期——三者都是配错，不是"关"的表达方式。
    if not 0.0 < settings.SEMANTIC_CACHE_TAU <= 1.0:
        errors.append(f"SEMANTIC_CACHE_TAU 必须落在 (0, 1]（当前 {settings.SEMANTIC_CACHE_TAU}）")
    if settings.SEMANTIC_CACHE_TTL_SECONDS <= 0:
        errors.append("SEMANTIC_CACHE_TTL_SECONDS 必须为正整数（关掉缓存请用 SEMANTIC_CACHE_ENABLED=false）")

    return errors, warnings
