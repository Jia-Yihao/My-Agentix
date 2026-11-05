"""LG-1-3 验收：三层自研退避与 LangGraph 原生默认同源。

## 验收口径（指导文档 §4.2 LG-1-3「怎么算过」）

> 参数快照用例：三处读到的值与 ``RetryPolicy`` 默认逐项相同；
> 工具层不再出现无上限的 ``0.2·2^n``。

三处 = 工具层 ``tool_retry_backoff()`` / 任务层 ``task_retry_backoff()`` /
检索层 ``retrieval_retry_backoff()``。真源在 ``agentix/domain/policies/retry.py``
（domain 零第三方依赖，不能反向 import LangGraph），由
``agentix/orchestration/langgraph/policies.py`` 转出；数值抄本的失真由本文件的
逐项比对钉住——框架改默认值时这里先红。

## 两条必须说清的口径

**flag-off 逐位等价**（``CONTRIBUTING.md` 的工程约定` 约定 1）：``RETRY_BACKOFF_ALIGNED`` 默认关，
三处返回各自的历史数值（工具 0.2s 起步无抖动、task 上界 8s、检索用
``KB_RETRIEVAL_*`` 配置），行为与基线逐位相同。

**工具层的上限在两种状态下都生效**，但 ``ToolSpec.max_retries`` 默认 2 ⇒ 可及
等待 ≤ 0.8s，128s 上限**不绑定**。flag 关时实得的 sleep 序列仍是 ``0.2 / 0.4``，
与旧公式 ``0.2·2^n`` 逐位相同——本文件用真实 ``ToolExecutor`` + 记录 sleep 证明，
不只靠读参数。
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import pathlib
from typing import Any, Dict, List

import pytest

from agentix.application.services import task_retry
from agentix.application.services.task_plan import TaskSpec
from agentix.config import settings
from agentix.domain.policies import retry as domain_retry
from agentix.domain.policies.retry import RetryBackoff
from agentix.tools.gateway.executor import ToolExecutor, tool_retry_backoff
from agentix.tools.definitions import ToolSpec

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
KB_RETRIEVAL_PATH = (
    REPO_ROOT
    / "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/multi_agent/kb_retrieval.py"
)
EXECUTOR_PATH = REPO_ROOT / "agentix/tools/gateway/executor.py"


def _native_fields() -> Dict[str, Any]:
    """``RetryPolicy()`` 的退避字段（去掉回调 ``retry_on``——它不是数值）。"""
    from langgraph.types import RetryPolicy

    fields = RetryPolicy()._asdict()
    fields.pop("retry_on")
    return fields


def _shape_fields(shape: RetryBackoff) -> Dict[str, Any]:
    return dataclasses.asdict(shape)


@pytest.fixture
def aligned_on():
    """临时打开 ``RETRY_BACKOFF_ALIGNED``（p148 同款：显式保存/恢复）。"""
    original = settings.RETRY_BACKOFF_ALIGNED
    settings.RETRY_BACKOFF_ALIGNED = True
    try:
        yield
    finally:
        settings.RETRY_BACKOFF_ALIGNED = original


# ---------------------------------------------------------------------------
# 真源：常量与原生默认逐项相同
# ---------------------------------------------------------------------------


class TestCanonicalSourceMatchesNative:
    def test_switch_defaults_off(self):
        """约定 1：新开关默认关；若确有理由改成 True，须先改本用例。"""
        assert settings.RETRY_BACKOFF_ALIGNED is False

    def test_constants_match_retry_policy_defaults(self):
        native = _native_fields()
        assert domain_retry.RETRY_INITIAL_INTERVAL == native["initial_interval"]
        assert domain_retry.RETRY_BACKOFF_FACTOR == native["backoff_factor"]
        assert domain_retry.RETRY_MAX_INTERVAL == native["max_interval"]
        assert domain_retry.RETRY_MAX_ATTEMPTS == native["max_attempts"]
        assert domain_retry.RETRY_JITTER == native["jitter"]

    def test_default_shape_is_the_native_default_shape(self):
        assert _shape_fields(RetryBackoff()) == _native_fields()

    def test_orchestration_policies_reexport_the_same_objects(self):
        """「从 policies.py 取」必须是转出、不是另抄一份。"""
        from agentix.orchestration.langgraph import policies

        for name in (
            "RETRY_INITIAL_INTERVAL",
            "RETRY_BACKOFF_FACTOR",
            "RETRY_MAX_INTERVAL",
            "RETRY_MAX_ATTEMPTS",
            "RETRY_JITTER",
            "RetryBackoff",
        ):
            assert getattr(policies, name) is getattr(domain_retry, name), (
                f"policies.{name} 与领域层不是同一个对象——出现了第二份真源"
            )
            assert name in policies.__all__, f"policies.__all__ 缺 {name}"

    def test_aretry_defaults_follow_the_same_constants(self):
        import inspect

        params = inspect.signature(domain_retry.aretry).parameters
        assert params["max_attempts"].default == domain_retry.RETRY_MAX_ATTEMPTS
        assert params["base_delay"].default == domain_retry.RETRY_INITIAL_INTERVAL
        assert params["max_delay"].default == domain_retry.RETRY_MAX_INTERVAL
        assert params["jitter"].default == domain_retry.RETRY_JITTER


# ---------------------------------------------------------------------------
# 三处参数快照
# ---------------------------------------------------------------------------


class TestThreeLayerSnapshot:
    def test_aligned_snapshot_equals_native_item_by_item(self, aligned_on):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_retrieval,
        )

        native = _native_fields()
        assert _shape_fields(tool_retry_backoff()) == native
        assert _shape_fields(task_retry.task_retry_backoff()) == native
        assert _shape_fields(kb_retrieval.retrieval_retry_backoff()) == native

    def test_flag_off_keeps_legacy_values(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_retrieval,
        )

        monkeypatch.setattr(settings, "KB_RETRIEVAL_MAX_ATTEMPTS", 2)
        monkeypatch.setattr(settings, "KB_RETRIEVAL_RETRY_BASE_DELAY", 0.25)

        assert _shape_fields(tool_retry_backoff()) == {
            "initial_interval": 0.2,
            "backoff_factor": 2.0,
            "max_interval": 128.0,
            "max_attempts": 3,
            "jitter": False,
        }, "工具层 flag 关必须还是 0.2s 起步、无抖动（基线）"
        assert _shape_fields(task_retry.task_retry_backoff()) == {
            "initial_interval": 0.5,
            "backoff_factor": 2.0,
            "max_interval": 8.0,
            "max_attempts": 3,
            "jitter": True,
        }, "task 层 flag 关必须还是 8s 上界（基线）"
        assert _shape_fields(kb_retrieval.retrieval_retry_backoff()) == {
            "initial_interval": 0.25,
            "backoff_factor": 2.0,
            "max_interval": 8.0,
            "max_attempts": 2,
            "jitter": True,
        }, "检索层 flag 关必须还是 KB_RETRIEVAL_* 配置驱动（基线）"

    def test_switch_actually_changes_all_three_layers(self, monkeypatch):
        """反空转：开关若对某层无效，上面的「逐项相同」就是假绿。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_retrieval,
        )

        off = (
            _shape_fields(tool_retry_backoff()),
            _shape_fields(task_retry.task_retry_backoff()),
            _shape_fields(kb_retrieval.retrieval_retry_backoff()),
        )
        monkeypatch.setattr(settings, "RETRY_BACKOFF_ALIGNED", True)
        on = (
            _shape_fields(tool_retry_backoff()),
            _shape_fields(task_retry.task_retry_backoff()),
            _shape_fields(kb_retrieval.retrieval_retry_backoff()),
        )
        assert all(a != b for a, b in zip(off, on)), (
            f"开关至少有一层没生效：off={off} on={on}"
        )

    def test_retrieval_params_dict_matches_shape(self, aligned_on):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_retrieval,
        )

        assert kb_retrieval.retrieval_retry_params() == {
            "max_attempts": 3,
            "base_delay": 0.5,
            "max_delay": 128.0,
            "jitter": True,
        }


# ---------------------------------------------------------------------------
# 工具层：不再有无上限的 0.2·2^n
# ---------------------------------------------------------------------------


class TestToolLayerHasBoundedBackoff:
    def test_source_has_no_unbounded_formula(self):
        source = EXECUTOR_PATH.read_text(encoding="utf-8")
        assert "0.2 * (2**attempt)" not in source, (
            "工具层仍是旧的 0.2·2^n 无上限公式——LG-1-3 要求换成带 max_interval 的形状"
        )
        assert "backoff.delay(attempt)" in source, (
            "执行循环没有走 tool_retry_backoff() 的 delay()——参数快照与真实行为脱节"
        )

    def test_delay_is_capped_in_both_states(self):
        for shape in (tool_retry_backoff(), RetryBackoff(initial_interval=0.2, jitter=False)):
            assert shape.delay(50) <= domain_retry.RETRY_MAX_INTERVAL

    def test_legacy_first_delay_is_exactly_the_old_formula(self):
        assert tool_retry_backoff().delay(0) == 0.2
        assert tool_retry_backoff().delay(1) == 0.4

    @pytest.mark.asyncio
    async def test_flag_off_sleep_sequence_is_bit_identical_to_baseline(self, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        result = await _run_flaky_tool(max_retries=2)
        assert result.success is False
        assert sleeps == [0.2, 0.4], (
            f"flag 关的 sleep 序列必须仍是 0.2/0.4（旧公式），实得 {sleeps}"
        )

    @pytest.mark.asyncio
    async def test_flag_on_sleep_sequence_uses_native_initial_and_jitter(self, aligned_on, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        result = await _run_flaky_tool(max_retries=2)
        assert result.success is False
        assert len(sleeps) == 2
        assert 0.25 <= sleeps[0] <= 0.5, f"首次退避应落在原生 0.5s 的抖动区间，实得 {sleeps[0]}"
        assert 0.5 <= sleeps[1] <= 1.0, f"第二次退避应落在原生 1.0s 的抖动区间，实得 {sleeps[1]}"


def _record_sleeps(monkeypatch) -> List[float]:
    sleeps: List[float] = []

    async def _record(delay, *args, **kwargs):
        sleeps.append(delay)
        return None

    monkeypatch.setattr(asyncio, "sleep", _record)
    return sleeps


async def _run_flaky_tool(*, max_retries: int):
    async def always_fails(args):
        raise RuntimeError("down")

    executor = ToolExecutor()
    executor.register_handler(
        "lg13_flaky",
        always_fails,
        spec=ToolSpec(name="lg13_flaky", description="d", retryable=True, max_retries=max_retries),
    )
    return await executor.execute("lg13_flaky", {})


# ---------------------------------------------------------------------------
# 任务层：真实 sleep 序列听从形状（不只读参数）
# ---------------------------------------------------------------------------


class TestTaskLayerBehaviour:
    @pytest.mark.asyncio
    async def test_aligned_delays_go_past_the_old_8s_ceiling(self, aligned_on, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        outcome = await _run_failing_task(max_attempts=7)
        assert outcome.attempts == 7
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0], (
            f"对齐后上界应是 128s（第 6 次退避 16s 不被 8s 截断），实得 {sleeps}"
        )

    @pytest.mark.asyncio
    async def test_flag_off_delays_stay_at_the_old_8s_ceiling(self, monkeypatch):
        sleeps = _record_sleeps(monkeypatch)
        outcome = await _run_failing_task(max_attempts=7)
        assert outcome.attempts == 7
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0], (
            f"flag 关必须保留旧 8s 上界（基线），实得 {sleeps}"
        )

    @pytest.mark.asyncio
    async def test_explicit_arguments_still_win(self, monkeypatch):
        """调用方显式传参时不得被开关覆盖（run_batch 会透传）。"""
        sleeps = _record_sleeps(monkeypatch)

        async def runner(spec):
            raise ConnectionError("boom")

        await task_retry.run_task(
            TaskSpec("t"),
            runner,
            max_attempts=2,
            base_delay=0.01,
            jitter=False,
        )
        assert sleeps == [0.01]


async def _run_failing_task(*, max_attempts: int):
    async def runner(spec):
        raise ConnectionError("boom")

    return await task_retry.run_task(TaskSpec("t"), runner, max_attempts=max_attempts, jitter=False)


# ---------------------------------------------------------------------------
# 检索层：装饰器真的展开同一份参数
# ---------------------------------------------------------------------------


class TestRetrievalWiring:
    def test_both_decorators_spread_the_module_snapshot(self):
        tree = ast.parse(KB_RETRIEVAL_PATH.read_text(encoding="utf-8"))
        wired = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name not in ("_postgres_search", "_milvus_search"):
                continue
            assert len(node.decorator_list) == 1, f"{node.name} 的装饰器数量变了"
            deco = node.decorator_list[0]
            assert isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "aretry"
            (kw,) = deco.keywords
            assert kw.arg is None, f"{node.name} 没有用 ** 展开参数"
            assert getattr(kw.value, "id", "") == "_RETRIEVAL_RETRY_PARAMS", (
                f"{node.name} 的 @aretry 实参不是模块级快照——参数快照与接线脱节"
            )
            wired[node.name] = True
        assert set(wired) == {"_postgres_search", "_milvus_search"}, (
            f"没找到两个检索通道的装饰器：{sorted(wired)}"
        )

    def test_reload_under_flag_recomputes_the_snapshot(self, monkeypatch):
        """真实接线证据：打开开关后重新导入，装饰期快照变成原生默认。"""
        module = _kb_retrieval_module()
        original = settings.RETRY_BACKOFF_ALIGNED
        try:
            monkeypatch.setattr(settings, "RETRY_BACKOFF_ALIGNED", True)
            module = importlib.reload(module)
            assert module._RETRIEVAL_RETRY_PARAMS == {
                "max_attempts": 3,
                "base_delay": 0.5,
                "max_delay": 128.0,
                "jitter": True,
            }, f"开开关后装饰期实参没跟着变：{module._RETRIEVAL_RETRY_PARAMS}"
        finally:
            settings.RETRY_BACKOFF_ALIGNED = original
            monkeypatch.undo()
            importlib.reload(module)


def _kb_retrieval_module():
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
        kb_retrieval,
    )

    return kb_retrieval
