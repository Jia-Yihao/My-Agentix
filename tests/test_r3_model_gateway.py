"""R3-7 Model Gateway 测试（架构文档 §42-44）。

覆盖：
① resolve_model：分档显式配置优先 / 空回退 LLM_MODEL / 未知分档 fail-fast
② fallback_chain：空配置 → []；逗号解析、去空格、去重、剔除 primary
③ ainvoke_with_fallback：primary 成功 / fallback 兜底 / 全部失败上抛 /
   attempts 上限硬约束
④ GatewayResult 协议归因字段
⑤ 接线面：query_knowledge_base 默认零行为变化（fallback 链空走原路径）
⑥ selfcheck / settings 默认值
"""

from __future__ import annotations

import pytest


# ---------- ① resolve_model（Registry/Router） ----------

def test_resolve_model_explicit_tier_config(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import get_model_gateway

    monkeypatch.setattr(settings, "LLM_MODEL_FAST", "qwen-turbo", raising=False)
    monkeypatch.setattr(settings, "LLM_MODEL", "deepseek-v4-flash", raising=False)
    gateway = get_model_gateway()
    assert gateway.resolve_model("fast") == "qwen-turbo"
    assert gateway.resolve_model("standard") == "deepseek-v4-flash"  # 空档回退主模型


def test_resolve_model_empty_falls_back(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_MODEL_FAST", "  ", raising=False)
    monkeypatch.setattr(settings, "LLM_MODEL", "deepseek-v4-flash", raising=False)
    gateway = ModelGateway()
    assert gateway.resolve_model("fast") == "deepseek-v4-flash"


def test_resolve_model_unknown_tier_fails_fast():
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    with pytest.raises(KeyError):
        ModelGateway().resolve_model("vision")  # type: ignore[arg-type]


# ---------- ② fallback_chain ----------

def test_fallback_chain_empty_by_default(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "", raising=False)
    assert ModelGateway().fallback_chain(primary_model="m1") == []


def test_fallback_chain_parse_dedupe_exclude_primary(monkeypatch):
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(
        settings, "LLM_FALLBACK_MODELS", " m2 , m3, m2 , m1, ", raising=False
    )
    chain = ModelGateway().fallback_chain(primary_model="m1")
    # 去空格、去重、剔除与 primary 相同项
    assert chain == ["m2", "m3"]


# ---------- ③ ainvoke_with_fallback ----------

@pytest.mark.asyncio
async def test_fallback_primary_success():
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    built: list[str] = []

    async def invoke(client):
        return f"resp-from-{client}"

    result = await ModelGateway().ainvoke_with_fallback(
        "prompt",
        primary_model="m1",
        fallback_models=["m2"],
        build_client=lambda m: (built.append(m), m)[1],
        invoke=invoke,
    )
    assert result.response == "resp-from-m1"
    assert result.model_used == "m1" and result.attempts == 1 and result.errors == []
    assert built == ["m1"]  # 不预建 fallback 客户端


@pytest.mark.asyncio
async def test_fallback_secondary_takes_over():
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    calls: list[str] = []

    async def invoke(client):
        calls.append(client)
        if client == "m1":
            raise RuntimeError("primary down")
        return f"resp-from-{client}"

    result = await ModelGateway().ainvoke_with_fallback(
        "prompt",
        primary_model="m1",
        fallback_models=["m2", "m3"],
        build_client=lambda m: m,
        invoke=invoke,
    )
    assert result.response == "resp-from-m2"
    assert result.model_used == "m2" and result.attempts == 2
    assert len(result.errors) == 1 and "m1" in result.errors[0]
    assert calls == ["m1", "m2"]  # m3 未尝试


@pytest.mark.asyncio
async def test_fallback_all_fail_raises_last_error():
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    async def invoke(client):
        raise ValueError(f"down-{client}")

    with pytest.raises(ValueError, match="down-m3"):
        await ModelGateway().ainvoke_with_fallback(
            "prompt",
            primary_model="m1",
            fallback_models=["m2", "m3"],
            build_client=lambda m: m,
            invoke=invoke,
            max_attempts=3,  # 显式放宽，验证"最后一个模型的错误原样上抛"
        )


@pytest.mark.asyncio
async def test_fallback_max_attempts_hard_cap(monkeypatch):
    """链长超过上限时被硬截断（§44 防成本爆炸）。"""
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MAX_ATTEMPTS", 2, raising=False)
    tried: list[str] = []

    async def invoke(client):
        tried.append(client)
        raise ValueError("down")

    with pytest.raises(ValueError):
        await ModelGateway().ainvoke_with_fallback(
            "prompt",
            primary_model="m1",
            fallback_models=["m2", "m3", "m4"],
            build_client=lambda m: m,
            invoke=invoke,
        )
    assert tried == ["m1", "m2"]


@pytest.mark.asyncio
async def test_fallback_chain_feeds_executor(monkeypatch):
    """fallback_chain → ainvoke_with_fallback 串联（§43+§44 组合）。"""
    from agentix.config import settings
    from agentix.infrastructure.llm.model_gateway import ModelGateway

    monkeypatch.setattr(settings, "LLM_FALLBACK_MODELS", "m2", raising=False)
    gateway = ModelGateway()
    chain = gateway.fallback_chain(primary_model="m1")

    async def invoke(client):
        if client == "m1":
            raise RuntimeError("down")
        return "ok"

    result = await gateway.ainvoke_with_fallback(
        "prompt",
        primary_model="m1",
        fallback_models=chain,
        build_client=lambda m: m,
        invoke=invoke,
    )
    assert result.response == "ok"


# ---------- ④ GatewayResult 协议 ----------

def test_gateway_result_shape():
    from agentix.infrastructure.llm.model_gateway import GatewayResult

    result = GatewayResult(response="r", model_used="m", attempts=2, errors=["e1"])
    assert result.response == "r" and result.attempts == 2


# ---------- ⑤ 接线面 ----------

def test_query_knowledge_base_gateway_wired():
    """接线确认：chat_agent_tools 引用了 model_gateway（默认链空零行为变化）。"""
    import inspect

    from agentix.retrieval.usecases import chat_agent_tools

    source = inspect.getsource(chat_agent_tools)
    assert "get_model_gateway" in source
    assert "ainvoke_with_fallback" in source


def test_settings_defaults_zero_change():
    from agentix.config import settings

    assert settings.LLM_MODEL_FAST == ""
    assert settings.LLM_MODEL_STANDARD == ""
    assert settings.LLM_MODEL_REASONING == ""
    assert settings.LLM_FALLBACK_MODELS == ""
    assert settings.LLM_FALLBACK_MAX_ATTEMPTS == 2
