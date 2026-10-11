"""Phase H·W7：OTel GenAI 语义属性 + Prometheus 指标标签基数守卫。

- GenAI：``chat {model}`` span 命名、``gen_ai.*`` 请求/用量属性（两种
  usage 键名兼容、缺失不臆造）、span 网关与 invoke_with_breaker 接线；
- 基数守卫：全部已注册指标的 label 名 ⊆ 有界白名单（新标签必须先在此
  登记），且高基数身份类标签（user/session/request/trace/问答内容）恒不出现。
"""
from __future__ import annotations

import pytest

from agentix.observability.spans import apply_genai_usage, genai_span
from agentix.observability.tracing import (
    GENAI_OPERATION_NAME,
    GENAI_REQUEST_MODEL,
    GENAI_SYSTEM,
    GENAI_USAGE_INPUT_TOKENS,
    GENAI_USAGE_OUTPUT_TOKENS,
    apply_genai_request,
    genai_span_name,
    infer_genai_system,
)


class FakeSpan:
    """最小 span 替身：记录 set_attribute 载荷。"""

    def __init__(self, *, explode: bool = False):
        self.attrs: dict = {}
        self._explode = explode

    def set_attribute(self, key, value):
        if self._explode:
            raise RuntimeError("span broken")
        self.attrs[key] = value


# ---------------------------------------------------------------------------
# GenAI span 命名与 provider 推断
# ---------------------------------------------------------------------------


def test_genai_span_name_follows_semconv():
    assert genai_span_name("deepseek-v4-flash") == "chat deepseek-v4-flash"
    assert genai_span_name("gpt-4o", operation="chat") == "chat gpt-4o"


def test_genai_span_name_without_model_falls_back_to_operation():
    assert genai_span_name(None) == "chat"
    assert genai_span_name("", operation="embed") == "embed"


def test_infer_genai_system_by_model_name():
    assert infer_genai_system("deepseek-v4-flash") == "deepseek"
    assert infer_genai_system("DeepSeek-R1") == "deepseek"
    assert infer_genai_system("gpt-4o-mini") == "openai"
    assert infer_genai_system(None) == "openai"


# ---------------------------------------------------------------------------
# gen_ai.* 属性写入
# ---------------------------------------------------------------------------


def test_apply_genai_request_sets_semconv_keys():
    span = FakeSpan()
    apply_genai_request(span, model="deepseek-v4-flash", operation="chat")
    assert span.attrs[GENAI_OPERATION_NAME] == "chat"
    assert span.attrs[GENAI_REQUEST_MODEL] == "deepseek-v4-flash"
    assert span.attrs[GENAI_SYSTEM] == "deepseek"


def test_apply_genai_request_system_override_and_missing_model():
    span = FakeSpan()
    apply_genai_request(span, model=None, system="azure-openai")
    assert span.attrs[GENAI_SYSTEM] == "azure-openai"
    assert span.attrs[GENAI_OPERATION_NAME] == "chat"
    assert GENAI_REQUEST_MODEL not in span.attrs


def test_apply_genai_request_fail_open_on_broken_span():
    span = FakeSpan(explode=True)
    apply_genai_request(span, model="m")  # 不抛


def test_apply_genai_usage_langchain_key_style():
    span = FakeSpan()
    apply_genai_usage(
        span, usage={"input_tokens": 120, "output_tokens": 45, "total_tokens": 165}
    )
    assert span.attrs[GENAI_USAGE_INPUT_TOKENS] == 120
    assert span.attrs[GENAI_USAGE_OUTPUT_TOKENS] == 45
    # total 非 semconv 标准键，不写
    assert "gen_ai.usage.total_tokens" not in span.attrs


def test_apply_genai_usage_openai_token_usage_key_style():
    span = FakeSpan()
    apply_genai_usage(span, usage={"prompt_tokens": 9, "completion_tokens": 3})
    assert span.attrs[GENAI_USAGE_INPUT_TOKENS] == 9
    assert span.attrs[GENAI_USAGE_OUTPUT_TOKENS] == 3


def test_apply_genai_usage_missing_writes_nothing():
    span = FakeSpan()
    apply_genai_usage(span, usage=None)
    apply_genai_usage(span, usage={})
    assert span.attrs == {}


def test_apply_genai_usage_garbage_fails_open_without_partial_write():
    span = FakeSpan()
    apply_genai_usage(span, usage={"input_tokens": "not-a-number", "output_tokens": 5})
    assert span.attrs == {}


def test_apply_genai_usage_explicit_params_win_over_usage_dict():
    span = FakeSpan()
    apply_genai_usage(span, usage={"input_tokens": 1}, input_tokens=7)
    assert span.attrs[GENAI_USAGE_INPUT_TOKENS] == 7


# ---------------------------------------------------------------------------
# genai_span 网关（spans.py 是业务唯一 span 入口，硬规则 12）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genai_span_gateway_applies_request_attrs(monkeypatch):
    import agentix.observability.spans as spans_mod

    captured: dict = {}

    class _FakeCM:
        def __init__(self, name, attributes=None):
            captured["name"] = name
            captured["init_attrs"] = attributes

        async def __aenter__(self):
            captured["span"] = FakeSpan()
            return captured["span"]

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(spans_mod, "trace_span", lambda name, attributes=None: _FakeCM(name, attributes))

    async with genai_span("deepseek-v4-flash") as span:
        assert isinstance(span, FakeSpan)
        apply_genai_usage(span, usage={"input_tokens": 11, "output_tokens": 7})

    assert captured["name"] == "chat deepseek-v4-flash"
    assert captured["span"].attrs[GENAI_OPERATION_NAME] == "chat"
    assert captured["span"].attrs[GENAI_SYSTEM] == "deepseek"
    assert captured["span"].attrs[GENAI_USAGE_INPUT_TOKENS] == 11
    assert captured["span"].attrs[GENAI_USAGE_OUTPUT_TOKENS] == 7


# ---------------------------------------------------------------------------
# 接线：invoke_with_breaker（检索域工具/改写/压缩共用入口）
# ---------------------------------------------------------------------------


class _FakeLLM:
    model_name = "deepseek-v4-flash"

    def invoke(self, prompt):
        class _Resp:
            usage_metadata = {"input_tokens": 33, "output_tokens": 12}

        return _Resp()


@pytest.mark.asyncio
async def test_invoke_with_breaker_wires_genai_span(monkeypatch):
    import agentix.observability.spans as spans_mod
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.usecases import chat_llm

    monkeypatch.setattr(retrieval_settings, "LLM_CIRCUIT_BREAKER_ENABLED", False)

    captured: dict = {}

    class _FakeCM:
        def __init__(self, model, *, operation="chat", system=None):
            captured["model"] = model
            captured["span"] = FakeSpan()

        async def __aenter__(self):
            return captured["span"]

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(spans_mod, "genai_span", lambda model, **kw: _FakeCM(model, **kw))

    resp = await chat_llm.invoke_with_breaker(_FakeLLM(), "hi")
    assert resp.usage_metadata["input_tokens"] == 33
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["span"].attrs[GENAI_USAGE_INPUT_TOKENS] == 33
    assert captured["span"].attrs[GENAI_USAGE_OUTPUT_TOKENS] == 12


def test_llm_factory_ainvoke_wires_genai_span():
    """编排域 LLM 出口接线（源级守卫——完整 ainvoke 需 ChatOpenAI 网络栈）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "agentix" / "application" / "agents" / "llm_factory.py").read_text(
        encoding="utf-8"
    )
    assert "genai_span(self.model_name)" in src
    assert "apply_genai_usage(gen_span" in src


# ---------------------------------------------------------------------------
# Prometheus 指标标签基数守卫
# ---------------------------------------------------------------------------

# 有界标签白名单（新指标若引入新 label，必须先在此登记并说明有界理由）：
#   身份/枚举类：method status scope stage reason action decision state mode
#              role type from_state to_state engine provider step_type
#              target_type agent_type error_code component cache_type
#   配置态有界串：model primary_model fallback_model builder name tool_name
#   资源/归一化：endpoint（main._ENDPOINT_SEGMENT_RE 归一化 + 段数封顶，p92 钉死）
#               kb_id（租户 KB，业务主键非用户输入自由串）
ALLOWED_METRIC_LABELS = frozenset(
    {
        "action",
        "agent_type",
        "builder",
        "cache_type",
        "component",
        "decision",
        "endpoint",
        "engine",
        "error_code",
        "fallback_model",
        "from_state",
        "kb_id",
        "method",
        "mode",
        "model",
        "name",
        "primary_model",
        "provider",
        "rating",   # P2-1 feedback_total{rating}：有界枚举 up|down
        "reason",
        "role",
        "scope",
        "stage",
        "state",
        "status",
        "step_type",
        "target_type",
        "to_state",
        "tool_name",
        "type",
    }
)

# 身份/问答内容类标签：任何情况下不得进入指标（高基数 + 隐私）。
FORBIDDEN_METRIC_LABELS = frozenset(
    {
        "user_id",
        "session_id",
        "request_id",
        "trace_id",
        "span_id",
        "conversation_id",
        "question",
        "prompt",
        "content",
        "answer",
        "query",
        "ip",
        "user_agent",
        "path",
        "url",
    }
)


def _registered_labelnames() -> set[str]:
    from prometheus_client import REGISTRY

    labels: set[str] = set()
    for collector, _names in REGISTRY._collector_to_names.items():
        labs = getattr(collector, "_labelnames", None)
        if labs:
            labels.update(labs)
            continue
        try:
            for desc in collector.describe():
                labels.update(tuple(getattr(desc, "labelnames", None) or ()))
        except Exception:  # noqa: BLE001 - 内建 collector（gc/process）无 describe
            continue
    return labels


def test_metric_labels_within_bounded_whitelist():
    labels = _registered_labelnames()
    assert labels, "应能枚举已注册指标标签（prometheus_client 可用）"
    extra = labels - ALLOWED_METRIC_LABELS
    assert not extra, f"发现未登记的指标标签（新标签必须入白名单并注明有界理由）：{sorted(extra)}"


def test_metric_labels_never_include_identity_or_content():
    labels = _registered_labelnames()
    bad = labels & FORBIDDEN_METRIC_LABELS
    assert not bad, f"高基数/隐私标签不得进指标：{sorted(bad)}"


def test_genai_semconv_keys_are_span_only_not_metric_labels():
    labels = _registered_labelnames()
    genai_keys = {
        "gen_ai.system",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    }
    assert not (labels & genai_keys)
