"""R-P3 路由兜底计数守卫——把 README §16 记录的"静默兜底"变成可归因指标。

覆盖面与验收口径：

1. ``exception``：分类那次 LLM 调用抛错 ⇒ ``router_fallback_total{reason="exception"}``
   +1，返回兜底 Router 且 ``logic`` 文案与基线**逐字相同**；有启发式时结果换成启发式
   （更好），但"兜底发生过"这件事仍然记账——兜底 ≠ 放弃；
2. ``invalid_type`` / ``empty`` 两条防御分支各自计数。触达方式必须用
   ``Router().type = ...`` 赋值：pydantic v2 默认不在赋值时校验，而**构造期
   Literal 校验根本到不了这两条分支**（这一点单独用一条测试记录）；
3. **fail-open**（工程约定 2）：指标子系统抛错不得改写路由结果——这条路径
   本身就是降级路径，再抛异常等于把降级变成故障；
4. **标签值有界**（约定 3）：表外 reason 归 ``unknown``；
5. **已知空洞（characterization，非期望行为）**：结构化输出返回非 ``Router``
   实例且类型非法时，``router_cls.model_validate`` 在 try 之外 ⇒
   ``ValidationError`` 直接冒出节点，既不兜底也不计数。修它属行为变更，
   需产品决策，本批只把现状钉住。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from agentix.application.agents import router as router_module
from agentix.config import ablation
from agentix.orchestration.langgraph.lg_states import AgentState, Router

ABLATION_FLAG_NAMES = tuple(ablation.FLAG_DEFAULTS)


@pytest.fixture(autouse=True)
def _clean_ablation_env(monkeypatch):
    """消融开关必须处于默认（router_enabled 打开），否则会短路掉被测链路。"""
    for name in ABLATION_FLAG_NAMES:
        monkeypatch.delenv(f"{ablation.ENV_PREFIX}{name.upper()}", raising=False)


def _state(question: str = "随便聊两句") -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


def _counter(reason: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value("router_fallback_total", {"reason": reason}) or 0.0


class _Structured:
    """``with_structured_output(...)`` 的替身：返回固定结果或抛固定异常。"""

    def __init__(self, *, result=None, error: BaseException | None = None) -> None:
        self._result = result
        self._error = error

    async def ainvoke(self, messages):
        if self._error is not None:
            raise self._error
        return self._result


class _Model:
    def __init__(self, structured: _Structured) -> None:
        self._structured = structured

    def with_structured_output(self, schema, **kwargs):
        return self._structured


def _install_model(monkeypatch, structured: _Structured) -> None:
    monkeypatch.setattr(router_module, "get_llm_client", lambda **kw: _Model(structured))


# ---------------------------------------------------------------------------
# 1. exception：抛错兜底
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_path_counts_and_returns_fallback(monkeypatch):
    monkeypatch.setattr(router_module, "_heuristic_router", lambda q: None)
    _install_model(monkeypatch, _Structured(error=RuntimeError("402 Insufficient Balance")))
    before = _counter("exception")

    result = await router_module.analyze_and_route_query(_state(), config={})

    assert _counter("exception") == before + 1
    assert result["router"].type == "kb-query"
    assert result["router"].logic == "fallback: default to knowledge base routing"


@pytest.mark.asyncio
async def test_exception_with_heuristic_still_counts(monkeypatch):
    """有启发式时结果更好（text2sql），但兜底事实仍必须记账。"""
    _install_model(monkeypatch, _Structured(error=RuntimeError("timeout")))
    before = _counter("exception")

    result = await router_module.analyze_and_route_query(_state("有多少部法律"), config={})

    assert result["router"].type == "text2sql-query"
    assert _counter("exception") == before + 1


# ---------------------------------------------------------------------------
# 2. 防御分支：invalid_type / empty
# ---------------------------------------------------------------------------


def _mutated_router(type_value: str) -> Router:
    """构造合法 Router 后**赋值**改 type —— 唯一能触达这两条防御分支的方式。"""
    r = Router(type="kb-query")
    r.type = type_value
    return r


@pytest.mark.asyncio
async def test_invalid_type_branch_counts(monkeypatch):
    monkeypatch.setattr(router_module, "_heuristic_router", lambda q: None)
    _install_model(monkeypatch, _Structured(result=_mutated_router("bogus-type")))
    before = _counter("invalid_type")

    result = await router_module.analyze_and_route_query(_state(), config={})

    assert _counter("invalid_type") == before + 1
    assert result["router"].type == "kb-query"
    assert result["router"].logic == "fallback: invalid router output"


@pytest.mark.asyncio
async def test_empty_type_branch_counts(monkeypatch):
    monkeypatch.setattr(router_module, "_heuristic_router", lambda q: None)
    _install_model(monkeypatch, _Structured(result=_mutated_router("")))
    before = _counter("empty")

    result = await router_module.analyze_and_route_query(_state(), config={})

    assert _counter("empty") == before + 1
    assert result["router"].type == "kb-query"


def test_literal_schema_makes_invalid_type_unreachable_by_construction():
    """``Router.type`` 是 Literal ⇒ 构造期非法类型直接被 pydantic 拒掉。

    所以上两条测试只能靠赋值触达；这条记录的是"生产里这两条分支实际靠 schema
    兜住了"这一事实，避免把防御分支的覆盖度读成"线上真的出现过非法类型"。
    """
    with pytest.raises(ValidationError):
        Router(type="bogus-type")


# ---------------------------------------------------------------------------
# 3. fail-open：指标坏了不许改路由
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metric_failure_does_not_break_routing(monkeypatch):
    import agentix.observability.metrics as metrics_module

    monkeypatch.setattr(router_module, "_heuristic_router", lambda q: None)
    _install_model(monkeypatch, _Structured(error=RuntimeError("llm down")))

    def _boom(reason: str) -> None:
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(metrics_module, "record_router_fallback", _boom)

    result = await router_module.analyze_and_route_query(_state(), config={})

    assert result["router"].type == "kb-query", "指标抛错不得改写路由结果"
    assert result["router"].logic == "fallback: default to knowledge base routing"


# ---------------------------------------------------------------------------
# 4. 标签与取值有界
# ---------------------------------------------------------------------------


def test_counter_is_registered_with_bounded_reason_label():
    from agentix.observability import metrics as metrics_module

    # prometheus_client 的 Counter 内部只存基名（``_total`` 后缀在暴露时补上），
    # 所以这里断言基名 + 标签；暴露名由上面 _counter() 的取样名钉住。
    assert metrics_module.ROUTER_FALLBACK._name + "_total" == "router_fallback_total"
    assert tuple(metrics_module.ROUTER_FALLBACK._labelnames) == ("reason",)


def test_reason_values_are_bounded():
    from agentix.observability.metrics import record_router_fallback

    before = _counter("unknown")
    record_router_fallback("definitely-not-a-registered-reason")
    assert _counter("unknown") == before + 1, "表外 reason 必须归 unknown（基数有界）"


# ---------------------------------------------------------------------------
# 5. 已知空洞（characterization）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_payload_raises_instead_of_falling_back(monkeypatch):
    """已知空洞：非 Router 实例 + 非法类型 ⇒ 校验异常冒出节点，不兜底也不计数。

    这不是期望行为，是把现状钉住：``router_cls.model_validate`` 位于 try 之外，
    Literal 校验失败即抛。修它（把 validate 纳入 try 并计 ``invalid_type``）是
    行为变更，需产品决策，故本批不动。
    """
    monkeypatch.setattr(router_module, "_heuristic_router", lambda q: None)
    _install_model(monkeypatch, _Structured(result={"type": "bogus-type"}))
    before_exception = _counter("exception")
    before_invalid = _counter("invalid_type")

    with pytest.raises(ValidationError):
        await router_module.analyze_and_route_query(_state(), config={})

    assert _counter("exception") == before_exception
    assert _counter("invalid_type") == before_invalid