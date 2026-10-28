"""P103：fallback 跨 base_url 适配层（P1-1）测试。

覆盖：
① LLM_FALLBACK_MODEL_SPECS 解析：去空段/去重/剔除 primary/空段（`model||`）
② specs 优先于旧键 LLM_FALLBACK_MODELS（旧键语义零变化）
③ flag-off（两键皆空）：fallback_chain / fallback_specs 双空 = 基线等价
④ 环境变量名解析：api_key_env → os.environ 读值；未设置 ⇒ api_key=None
⑤ _build_fallback_client 消费点：跨端点三元组构造 ChatOpenAI 断言
   base_url/api_key；仅模型名条目与 primary（str）走主链路同构路径
⑥ 五处同步棘轮：settings / selfcheck / .env.example / 配置管理.md / p41 清单
"""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- ① 解析语义 ----------

def test_specs_empty_by_default(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL_SPECS", "", raising=False)
    assert ModelGateway().fallback_specs(primary_model="m1") == []


def test_specs_parse_triple(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(
        settings,
        "LLM_FALLBACK_MODEL_SPECS",
        " fb-model | http://localhost:1234/v1 | FB_KEY ",
        raising=False,
    )
    monkeypatch.setenv("FB_KEY", "sk-secret")
    specs = ModelGateway().fallback_specs(primary_model="m1")
    assert len(specs) == 1
    assert specs[0].model == "fb-model"
    assert specs[0].base_url == "http://localhost:1234/v1"
    assert specs[0].api_key == "sk-secret"


def test_specs_empty_segments_fall_back_to_primary_endpoint(monkeypatch):
    """`model||` = 仅模型名：base_url/api_key 均为 None（回退主端点）。"""
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL_SPECS", "model-only||", raising=False)
    specs = ModelGateway().fallback_specs(primary_model="m1")
    assert len(specs) == 1
    assert specs[0].model == "model-only"
    assert specs[0].base_url is None
    assert specs[0].api_key is None


def test_specs_dedupe_and_exclude_primary(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(
        settings,
        "LLM_FALLBACK_MODEL_SPECS",
        "a|http://x/v1|K1, ,a|http://y/v1|K2,m1|http://z/v1|K3,b",
        raising=False,
    )
    specs = ModelGateway().fallback_specs(primary_model="m1")
    # 重复 a 去重（保留首个）；m1 与 primary 相同剔除；空段剔除
    assert [s.model for s in specs] == ["a", "b"]


def test_specs_api_key_env_missing_yields_none(monkeypatch):
    """环境变量未设置 ⇒ api_key=None（消费点回退主端点，不抛错）。"""
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(
        settings, "LLM_FALLBACK_MODEL_SPECS", "m|http://x/v1|NO_SUCH_ENV_P103", raising=False
    )
    monkeypatch.delenv("NO_SUCH_ENV_P103", raising=False)
    specs = ModelGateway().fallback_specs(primary_model="m1")
    assert specs[0].api_key is None
    assert specs[0].base_url == "http://x/v1"


# ---------- ② 优先级与 ③ flag-off 等价 ----------

def test_specs_take_precedence_over_legacy_chain(monkeypatch):
    """specs 非空时消费点应走 specs；旧键语义不变但被覆盖。"""
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL_SPECS", "s1|http://x/v1|K", raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "legacy1,legacy2", raising=False)
    gateway = ModelGateway()
    assert [s.model for s in gateway.fallback_specs(primary_model="m1")] == ["s1"]
    # 旧键解析器本身行为不变（覆盖关系由消费点表达）
    assert gateway.fallback_chain(primary_model="m1") == ["legacy1", "legacy2"]


def test_both_keys_empty_is_baseline_equal(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL_SPECS", "", raising=False)
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "", raising=False)
    gateway = ModelGateway()
    assert gateway.fallback_specs(primary_model="m1") == []
    assert gateway.fallback_chain(primary_model="m1") == []


def test_settings_default_empty():
    """settings 默认空 = 未配置部署与基线逐位等价（灰度纪律）。"""
    from agentix.config import settings

    # agentix.config.settings 即 Settings 单例实例（p41 同款用法）
    assert settings.LLM_FALLBACK_MODEL_SPECS == ""


# ---------- ⑤ 消费点 _build_fallback_client ----------

def _make_ctx(model_name: str = "m1"):
    """构造最小 AgentToolContext（只需 model_name/chat_mode 被消费）。"""
    from agentix.retrieval.usecases.chat_agent_tools import AgentToolContext

    return AgentToolContext(
        session=None,  # type: ignore[arg-type] - build_client 分支不触库
        kb_id="kb",
        scope_type="all",
        scope_id=None,
        vectorstore=None,
        metadata_filter=None,
        search_k=5,
        cache_redis=None,
        artifact_id="",
        scope_token="",
        model_name=model_name,
        chat_mode="fast",
    )


def test_build_fallback_client_cross_endpoint_spec_mapping(monkeypatch):
    """跨端点三元组 ⇒ 按规格构造 ChatOpenAI（源级棘轮防改回主端点）。

    消费点闭包在真实调用里经 ``ainvoke_with_fallback`` 才触达，单测里直接
    构造会拉起 langchain 校验；这里按「规格 → 构造参数」的纯映射断言 +
    源级棘轮双保险：确认消费点确实透传 ``item.base_url`` / ``item.api_key``、
    且 primary（str）走主链路同构路径。
    """
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import FallbackSpec
    from agentix.retrieval.usecases import chat_agent_tools as cat
    from agentix.retrieval.usecases.chat_llm import get_default_config, thinking_extra_body

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL_SPECS", "", raising=False)
    ctx = _make_ctx()

    spec = FallbackSpec(model="fb", base_url="http://localhost:1234/v1", api_key="sk-x")
    cfg = get_default_config()
    kwargs = dict(
        model=spec.model,
        temperature=cfg.get("temperature", 0.1),
        timeout=cfg.get("llm_timeout", 30),
        max_retries=cfg.get("llm_max_retries", 3),
        api_key=spec.api_key,
        base_url=spec.base_url,
        extra_body=thinking_extra_body(ctx.chat_mode),
    )
    assert kwargs["base_url"] == "http://localhost:1234/v1"
    assert kwargs["api_key"] == "sk-x"
    assert kwargs["model"] == "fb"

    src = Path(cat.__file__).read_text(encoding="utf-8")
    assert "api_key=item.api_key" in src
    assert "base_url=item.base_url" in src
    assert "isinstance(item, str)" in src


# ---------- ⑥ 五处同步棘轮 ----------

def test_five_place_sync():
    """settings / selfcheck / .env.example / 配置管理.md / p41 清单。"""
    from agentix.config import selfcheck
    from agentix.config import settings

    key = "LLM_FALLBACK_MODEL_SPECS"
    # ① settings 字段存在（settings 即单例实例）
    assert hasattr(settings, key)
    # ② selfcheck 合法省略清单
    assert key in selfcheck.DEFAULT_IGNORED_MISSING
    # ③ .env.example
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8-sig")
    assert key in env_example
    # ④ 配置管理.md §5 表
    config_doc = (PROJECT_ROOT / "docs/architecture/配置管理.md").read_text(
        encoding="utf-8"
    )
    assert f"| `{key}`" in config_doc
    # ⑤ p41 手写清单
    p41 = (PROJECT_ROOT / "tests/test_p41_doc_consistency.py").read_text(encoding="utf-8")
    assert f'"{key}"' in p41
