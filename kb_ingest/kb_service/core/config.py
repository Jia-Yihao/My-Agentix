
import os
from dataclasses import dataclass, field, fields
from typing import Dict, Optional


@dataclass(slots=True)
class MilvusConfig:
    """Connection parameters for the Milvus ``legal`` collection (R3.7)."""

    host: str = "localhost"
    port: str = "19530"
    collection: str = "legal"

    @classmethod
    def from_env(cls) -> "MilvusConfig":
        # 注意：slots=True 的 dataclass 类属性（cls.host）是 member descriptor，
        # 不能直接作为 os.getenv 的 default；须先构造实例取字段默认值。
        base = cls()
        return cls(
            host=os.getenv("MILVUS_HOST") or base.host,
            port=os.getenv("MILVUS_PORT") or base.port,
            collection=os.getenv("MILVUS_COLLECTION") or base.collection,
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "host": self.host,
            "port": self.port,
            "collection": self.collection,
        }


@dataclass(slots=True)
class Config:
    """Global configuration shared across services."""

    # data IO
    excel_file_path: str = field(default_factory=lambda: os.getenv("EXCEL_FILE_PATH", "data.xlsx"))
    output_csv_path: str = field(default_factory=lambda: os.getenv("OUTPUT_CSV_PATH", "processed_data.csv"))
    output_txt_path: str = field(default_factory=lambda: os.getenv("OUTPUT_TXT_PATH", "processed_data.txt"))

    # rewriting
    use_llm: bool = field(default_factory=lambda: os.getenv("USE_LLM", "true").lower() != "false")
    flat_sep: str = field(default_factory=lambda: os.getenv("FLAT_SEPARATOR", " | "))
    flat_kv_sep: str = field(default_factory=lambda: os.getenv("FLAT_KV_SEPARATOR", ": "))
    flat_max_len: int = field(default_factory=lambda: int(os.getenv("FLAT_MAX_LEN", "4000")))

    # LLM settings
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen/qwen3-next-80b-a3b-instruct"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_base_url: Optional[str] = field(default_factory=lambda: os.getenv("LLM_BASE_URL"))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.3")))
    llm_fallback_to_flatten: bool = field(
        default_factory=lambda: os.getenv("LLM_FALLBACK_TO_FLATTEN", "false").lower() == "true"
    )

    # embedding settings
    embedding_provider: str = field(default_factory=lambda: os.getenv("EMBEDDING_PROVIDER", "openai"))
    embedding_model: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "qwen/qwen3-embedding-8b"))
    embedding_api_key: str = field(default_factory=lambda: os.getenv("EMBEDDING_API_KEY", ""))
    embedding_base_url: Optional[str] = field(default_factory=lambda: os.getenv("EMBEDDING_BASE_URL"))
    embedding_dimension: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIMENSION", "4096")))

    # processing behaviour
    batch_size: int = field(default_factory=lambda: int(os.getenv("BATCH_SIZE", "16")))
    retry_times: int = field(default_factory=lambda: int(os.getenv("RETRY_TIMES", "3")))
    retry_delay: int = field(default_factory=lambda: int(os.getenv("RETRY_DELAY", "5")))

    # reranker configuration
    rerank_enabled: bool = field(default_factory=lambda: os.getenv("RERANK_ENABLED", "false").lower() == "true")
    rerank_provider: str = field(default_factory=lambda: os.getenv("RERANK_PROVIDER", "cohere"))

    # Cohere API 配置
    cohere_api_key: Optional[str] = field(default_factory=lambda: os.getenv("COHERE_API_KEY"))
    cohere_rerank_model: str = field(default_factory=lambda: os.getenv("COHERE_RERANK_MODEL", "rerank-multilingual-v3.0"))
    cohere_rerank_url: str = field(default_factory=lambda: os.getenv("COHERE_RERANK_URL", "https://api.cohere.com/v2/rerank"))

    # 自建服务配置
    rerank_base_url: Optional[str] = field(default_factory=lambda: os.getenv("RERANK_BASE_URL"))
    rerank_endpoint: str = field(default_factory=lambda: os.getenv("RERANK_ENDPOINT", "/rerank"))
    rerank_api_key: Optional[str] = field(default_factory=lambda: os.getenv("RERANK_API_KEY"))
    rerank_model: Optional[str] = field(default_factory=lambda: os.getenv("RERANK_MODEL"))

    # 通用配置
    rerank_timeout: float = field(default_factory=lambda: float(os.getenv("RERANK_TIMEOUT", "30")))
    rerank_max_candidates: int = field(default_factory=lambda: int(os.getenv("RERANK_MAX_CANDIDATES", "20")))
    rerank_top_n: int = field(default_factory=lambda: int(os.getenv("RERANK_TOP_N", "10")))
    rerank_score_fusion_alpha: Optional[float] = field(
        default_factory=lambda: float(os.getenv("RERANK_SCORE_FUSION_ALPHA", "0.3"))
        if os.getenv("RERANK_SCORE_FUSION_ALPHA") else None
    )

    milvus: MilvusConfig = field(default_factory=MilvusConfig.from_env)

    # API 鉴权（2026-09 新增）。
    #
    # ⚠️ 变量名是 ``KB_`` 前缀，**刻意不复用主应用的 ``API_KEY`` / ``REQUIRE_API_KEY``**。
    # 首版我正是复用了主应用的名字，那是个**安全设计错误**：compose 的
    # ``env_file: - .env`` 会把主应用的 ``API_KEY`` 一并注入本容器，于是
    # "任何拿到对外 API Key 的外部客户端"都能直接调本服务的**写入**端点
    # （改 Milvus 集合）——对外凭证变成了对内写权限，属权限抬升。
    # 两者信任边界不同：主应用的 Key 面向外部调用方，本服务的 Key 面向
    # 内部调用方（agentix）。因此各用各的。
    #
    # 这也与 ``kb_ingest/.env.example`` 的既有边界声明一致：
    # 「kb_ingest 是独立部署的容器……主应用的配置见仓库根目录 .env.example，
    # 两者不共享」。命名沿用该服务既有的 ``KB_*`` 约定
    # （``KB_MILVUS_HOST`` / ``KB_LLM_API_KEY`` / ``KB_INGEST_PORT`` …）。
    #
    # 默认 false = 保持既有部署行为不变；开启后所有 ``/api`` 端点要求 ``X-API-Key``。
    require_api_key: bool = field(
        default_factory=lambda: os.getenv("KB_REQUIRE_API_KEY", "false").lower() in ("1", "true", "yes")
    )
    api_key: str = field(default_factory=lambda: os.getenv("KB_API_KEY", ""))

    def __post_init__(self) -> None:
        if self.embedding_provider.lower() == "openai" and not self.embedding_api_key:
            self.embedding_api_key = self.llm_api_key

        if self.embedding_provider.lower() == "ollama" and not self.embedding_base_url:
            self.embedding_base_url = self.llm_base_url

    @property
    def milvus_config(self) -> Dict[str, str]:
        return self.milvus.as_dict()


def load_config() -> Config:
    """Factory to create config objects; entrypoint for DI."""
    return Config()


def clone_config(config: Config) -> Config:
    """Create a new Config instance copying all field values."""
    data = {}
    for field_def in fields(Config):
        value = getattr(config, field_def.name)
        if field_def.name == "milvus" and isinstance(value, MilvusConfig):
            value = MilvusConfig(
                host=value.host,
                port=value.port,
                collection=value.collection,
            )
        data[field_def.name] = value
    return Config(**data)
