"""LLM 优化验收（p94）。

覆盖规划 §4 W1 五项：
1. 路由 fast-path：flag-off 仍走 LLM（基线）、命中分支（寒暄/单条法条）、
   复杂问题不短路、``_boom`` 哨兵钉死零 LLM 构造、命中记 llm_calls_saved；
2. 角色→档位接线：解析/命中替换/关闸与空表逐位等价/坏配置 fail-open 回退；
3. KG 验证预算：默认值、按本次答案的调用次数、超时、token 台账——任一维度
   超预算均 fail-open 降级放行；
4. ContextPort 段级输出预算：默认 0、flag-off 与基线逐字节等价、flag-on
   超配额观测整体外置为 obs:// 指针（非截断）；
5. 前缀稳定性：PromptEnvelope 静态段字节稳定 + 真实路由提示词公共前缀 +
   prompt_prefix_fingerprint 指标与 system 前缀提取。

运行：./.venv/Scripts/python.exe -m pytest tests/test_p94_llm_optimization.py -q
"""
import asyncio
import zlib
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables.base import RunnableLambda

from agentix.application.agents import router as router_module
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.final_answer_guard import (
    create_final_answer_guard,
)
from agentix.application.agents.kg_sub_graph.agentic_rag_agents.components.validate_final_answer.models import (
    ValidateFinalAnswerResponse,
)
from agentix.application.agents.llm_factory import (
    DEFAULT_PROFILES,
    LLMProfile,
    _apply_tier_routing,
    _parse_role_tier_map,
    _system_prompt_prefix,
    _STATIC_PROMPT_ROLES,
    get_llm_client,
)
from agentix.config import settings as settings_instance
from agentix.domain.policies.prompt_envelope import (
    PromptEnvelope,
    assert_static_prefix_stable,
    assemble,
)
from agentix.orchestration.langgraph.lg_states import AgentState


def _state(question: str) -> AgentState:
    return AgentState(messages=[HumanMessage(content=question)])


def _boom(**_kwargs):
    raise AssertionError("fast-path 应短路：不得构造/调用 LLM 分类器")


# ===========================================================================
# 1. 路由 fast-path
# ===========================================================================


class TestRouterFastPath:
    def test_defaults_off(self):
        assert settings_instance.ROUTING_FASTPATH_ENABLED is False

    @pytest.mark.asyncio
    async def test_flag_off_greeting_still_builds_llm(self, monkeypatch):
        """flag-off（默认）⇒ 基线行为不变：寒暄照样构造分类 LLM。"""
        monkeypatch.setattr(router_module, "get_llm_client", _boom)
        with pytest.raises(AssertionError):
            await router_module.analyze_and_route_query(_state("你好"), config={})

    @pytest.mark.asyncio
    async def test_flag_on_chit_chat_skips_llm(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_module, "get_llm_client", _boom)

        result = await router_module.analyze_and_route_query(_state("你好"), config={})

        router = result["router"]
        assert router.type == "general-query"
        assert "fastpath" in router.logic

    @pytest.mark.asyncio
    async def test_flag_on_statute_direct_short_circuits(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_module, "get_llm_client", _boom)

        result = await router_module.analyze_and_route_query(
            _state("刑法第264条规定了什么"), config={}
        )

        assert result["router"].type == "graphrag-query"
        assert "fastpath" in result["router"].logic

    @pytest.mark.asyncio
    async def test_reasoning_question_falls_through_to_llm(self, monkeypatch):
        """含推理词的问题绝不短路——照常走 LLM 分类（哨兵被触发即证明）。"""
        monkeypatch.setattr(settings_instance, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_module, "get_llm_client", _boom)

        with pytest.raises(AssertionError):
            await router_module.analyze_and_route_query(
                _state("刑法第264条为什么和第265条区别"), config={}
            )

    @pytest.mark.asyncio
    async def test_hit_records_llm_calls_saved_metric(self, monkeypatch):
        from prometheus_client import REGISTRY

        before = (
            REGISTRY.get_sample_value(
                "llm_calls_saved_total", {"reason": "chit_chat"}
            )
            or 0.0
        )
        monkeypatch.setattr(settings_instance, "ROUTING_FASTPATH_ENABLED", True)
        monkeypatch.setattr(router_module, "get_llm_client", _boom)

        await router_module.analyze_and_route_query(_state("谢谢！"), config={})

        after = REGISTRY.get_sample_value(
            "llm_calls_saved_total", {"reason": "chit_chat"}
        )
        if after is None:
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        assert after >= before + 1

    def test_fastpath_rejects_empty_long_and_ambiguous(self):
        assert router_module._fastpath_router("") is None
        assert router_module._fastpath_router(None) is None  # type: ignore[arg-type]
        assert router_module._fastpath_router("你好" + "x" * 70) is None
        # 寒暄 + 额外实质内容 ⇒ 不再是高置信，宁可走 LLM
        assert router_module._fastpath_router("你好，帮我看看这个合同的风险点") is None
        # 多条法条/含「和」⇒ 需要推理，不短路
        assert router_module._fastpath_router("刑法第264条和第265条") is None


# ===========================================================================
# 2. 角色 → 档位接线
# ===========================================================================


class TestTierRouting:
    def test_defaults_off_and_empty_map(self):
        assert settings_instance.LLM_TIER_ROUTING_ENABLED is False
        assert settings_instance.LLM_ROLE_TIER_MAP == ""

    def test_flag_off_returns_same_profile_object(self):
        monkeypatch_profile = DEFAULT_PROFILES["router"]
        assert _apply_tier_routing(monkeypatch_profile) is monkeypatch_profile

    def test_flag_on_empty_map_returns_same_profile(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "")
        profile = DEFAULT_PROFILES["router"]
        assert _apply_tier_routing(profile) is profile

    def test_flag_on_hit_replaces_model_keeps_profile(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "router=fast")
        monkeypatch.setattr(settings_instance, "LLM_MODEL_FAST", "tier-flash-x")
        profile = DEFAULT_PROFILES["router"]

        routed = _apply_tier_routing(profile)

        assert routed is not profile
        assert routed.model == "tier-flash-x"
        assert routed.temperature == profile.temperature
        assert routed.timeout == profile.timeout
        assert routed.max_retries == profile.max_retries
        assert routed.tags == profile.tags
        assert routed.role == profile.role

    def test_tier_model_empty_falls_back_to_baseline(self, monkeypatch):
        """档位模型空 = 回退 LLM_MODEL = profile.model ⇒ 与基线逐位等价。"""
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "router=fast")
        monkeypatch.setattr(settings_instance, "LLM_MODEL_FAST", "")
        profile = DEFAULT_PROFILES["router"]
        assert _apply_tier_routing(profile) is profile

    def test_unknown_tier_fails_open_to_baseline(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "router=bogus")
        profile = DEFAULT_PROFILES["router"]
        assert _apply_tier_routing(profile) is profile

    def test_unmapped_role_returns_same_profile(self, monkeypatch):
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "planner=fast")
        profile = DEFAULT_PROFILES["router"]
        assert _apply_tier_routing(profile) is profile

    def test_parse_role_tier_map(self):
        assert _parse_role_tier_map("") == {}
        assert _parse_role_tier_map("router=fast, planner=reasoning") == {
            "router": "fast",
            "planner": "reasoning",
        }
        # 坏项丢弃（缺等号 / 空侧），合法项保留
        assert _parse_role_tier_map("bad,=x,router=,router=FAST") == {
            "router": "fast"
        }

    def test_get_llm_client_uses_tier_model_end_to_end(self, monkeypatch):
        """组合根端到端：flag 开 + 档位配置 ⇒ 真实 client 的 model_name 换档。"""
        monkeypatch.setattr(settings_instance, "LLM_API_KEY", "sk-test-p94")
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", True)
        monkeypatch.setattr(settings_instance, "LLM_ROLE_TIER_MAP", "router=fast")
        monkeypatch.setattr(settings_instance, "LLM_MODEL_FAST", "tier-flash-x")

        client = get_llm_client(role="router")
        assert client.model_name == "tier-flash-x"

        # flag 关 ⇒ 回基线 profile 模型（逐位等价）
        monkeypatch.setattr(settings_instance, "LLM_TIER_ROUTING_ENABLED", False)
        base_client = get_llm_client(role="router")
        assert base_client.model_name == settings_instance.LLM_MODEL


# ===========================================================================
# 3. KG 验证预算
# ===========================================================================


class FakeGuardLLM:
    """与 p28 同构的假 LLM（calls 计数证明预算跳过时零调用）。"""

    def __init__(self, response=None, exc=None, delay: float = 0.0):
        self.response = response
        self.exc = exc
        self.delay = delay
        self.calls = 0

    def with_structured_output(self, schema):
        async def _respond(_inputs):
            self.calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.exc is not None:
                raise self.exc
            return self.response

        return RunnableLambda(_respond)


@pytest.fixture()
def guard_graph():
    return MagicMock()


@pytest.fixture()
def force_validation(monkeypatch):
    monkeypatch.setattr(settings_instance, "KG_FINAL_ANSWER_VALIDATION", True)


class TestVerificationBudget:
    def test_defaults(self):
        assert settings_instance.VERIFICATION_MAX_LLM_CALLS == 1
        assert settings_instance.VERIFICATION_TIMEOUT_MS == 1000
        assert settings_instance.VERIFICATION_TOKEN_BUDGET == 0

    @pytest.mark.asyncio
    async def test_defaults_validate_normally(
        self, guard_graph, force_validation, monkeypatch
    ):
        """预算默认值下：首次校验照常执行（与现行行为一致）。"""
        monkeypatch.setattr(settings_instance, "VERIFICATION_MAX_LLM_CALLS", 1)
        monkeypatch.setattr(settings_instance, "VERIFICATION_TIMEOUT_MS", 1000)
        monkeypatch.setattr(settings_instance, "VERIFICATION_TOKEN_BUDGET", 0)
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validated"]}
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_max_calls_zero_skips_without_llm(
        self, guard_graph, force_validation, monkeypatch
    ):
        monkeypatch.setattr(settings_instance, "VERIFICATION_MAX_LLM_CALLS", 0)
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validation_budget"]}
        assert llm.calls == 0

    @pytest.mark.asyncio
    async def test_budget_hit_on_second_attempt_within_same_answer(
        self, guard_graph, force_validation, monkeypatch
    ):
        """本次答案已尝试 1 次（state.steps 留痕）+ 上限 1 ⇒ 第二次 fail-open 跳过。"""
        monkeypatch.setattr(settings_instance, "VERIFICATION_MAX_LLM_CALLS", 1)
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard(
            {"question": "q", "summary": "s", "steps": ["final_answer_validated"]}
        )

        assert result == {"steps": ["final_answer_validation_budget"]}
        assert llm.calls == 0

    @pytest.mark.asyncio
    async def test_timeout_degrades_open(
        self, guard_graph, force_validation, monkeypatch
    ):
        """超时窗包含模板格式化（毫秒级）但远小于假 LLM 休眠 ⇒ 必然打断在调用中。

        注：10ms 这类极窄窗会被**模板步**（非 LLM 步）吃掉，属测试口径问题；
        生产默认 1000ms 对格式化留有充分余量，这里用 300ms vs 1.0s 保证
        超时必然发生在 LLM 调用进行时（calls==1 可断言）。
        """
        monkeypatch.setattr(settings_instance, "VERIFICATION_TIMEOUT_MS", 300)
        llm = FakeGuardLLM(
            response=ValidateFinalAnswerResponse(valid=True), delay=1.0
        )
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert result == {"steps": ["final_answer_validation_timeout"]}
        assert llm.calls == 1  # 调用真的发生过（超时被打断而非没发起）

    @pytest.mark.asyncio
    async def test_token_budget_blocks_later_attempts(
        self, guard_graph, force_validation, monkeypatch
    ):
        monkeypatch.setattr(settings_instance, "VERIFICATION_TOKEN_BUDGET", 1)
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        first = await guard({"question": "q", "summary": "s"})
        second = await guard({"question": "q2", "summary": "s2"})

        assert first == {"steps": ["final_answer_validated"]}
        assert second == {"steps": ["final_answer_validation_token_budget"]}
        assert llm.calls == 1

    @pytest.mark.asyncio
    async def test_budget_never_blocks_answer_fields(
        self, guard_graph, force_validation, monkeypatch
    ):
        """预算降级也只留痕：增量里不得出现 summary/answer。"""
        monkeypatch.setattr(settings_instance, "VERIFICATION_MAX_LLM_CALLS", 0)
        llm = FakeGuardLLM(response=ValidateFinalAnswerResponse(valid=True))
        guard = create_final_answer_guard(llm=llm, graph=guard_graph)

        result = await guard({"question": "q", "summary": "s"})

        assert set(result.keys()) == {"steps"}


# ===========================================================================
# 4. ContextPort 段级输出预算
# ===========================================================================


def _obs(step_id: str, content: str, ok: bool = True):
    from agentix.domain.agent.types import Observation

    return Observation(step_id=step_id, content=content, ok=ok)


class TestToolOutputBudget:
    def test_default_is_zero(self):
        assert settings_instance.TOOL_OUTPUT_BUDGET_TOKENS == 0

    @pytest.mark.asyncio
    async def test_flag_off_simple_builder_byte_equal_baseline(
        self, monkeypatch
    ):
        """flag-off：轨迹行与旧实现（简单推导式）逐字节等价，无隔离无 meta 增量。"""
        from agentix.agent.context.simple import SimpleContextBuilder
        from agentix.domain.agent.types import TaskSpec

        task = TaskSpec(goal="回答问题")
        trajectory = [_obs("s1", "A" * 40), _obs("s2", "B" * 400, ok=False)]
        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 0)
        builder = SimpleContextBuilder(rules=("RULE 静态规则",))
        snap = await builder.build(task, trajectory=trajectory, budget_tokens=0)

        expected = (
            "RULE 静态规则\n回答问题\n[s1] " + "A" * 40 + "\n[s2] " + "B" * 400
        )
        assert snap.prompt == expected
        assert "obs://" not in snap.prompt
        assert "isolated_refs" not in snap.meta

    @pytest.mark.asyncio
    async def test_flag_on_under_budget_no_isolation(self, monkeypatch):
        from agentix.agent.context.simple import SimpleContextBuilder
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 100_000)
        builder = SimpleContextBuilder()
        snap = await builder.build(
            TaskSpec(goal="g"), trajectory=[_obs("s1", "A" * 40)], budget_tokens=0
        )

        assert "A" * 40 in snap.prompt
        assert "obs://" not in snap.prompt
        assert "isolated_refs" not in snap.meta

    @pytest.mark.asyncio
    async def test_flag_on_over_budget_externalizes_row(
        self, monkeypatch
    ):
        """超配额观测整体外置：prompt 只留小头部 + obs:// 指针，非截断丢数据。"""
        from agentix.agent.context.simple import SimpleContextBuilder
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 50)
        monkeypatch.setattr(settings_instance, "CONTEXT_ISOLATE_PERSIST_ENABLED", False)
        builder = SimpleContextBuilder()
        trajectory = [_obs("s1", "A" * 40), _obs("s2", "B" * 400)]
        snap = await builder.build(
            TaskSpec(goal="g"), trajectory=trajectory, budget_tokens=0
        )

        assert "A" * 40 in snap.prompt  # 预算内的行原样内联
        assert "B" * 400 not in snap.prompt  # 超额行正文不进 prompt
        assert "obs://s2" in snap.prompt  # flag-off 关隔离 ⇒ 裸引用
        assert snap.meta["isolated_refs"] == ["obs://s2"]

    @pytest.mark.asyncio
    async def test_flag_on_persist_externalizes_to_artifact(
        self, monkeypatch
    ):
        """隔离持久化开时：预算外置走 ArtifactStore（引用可取回，非裸 step_id）。"""
        from agentix.agent.context.simple import SimpleContextBuilder
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 50)
        monkeypatch.setattr(settings_instance, "CONTEXT_ISOLATE_PERSIST_ENABLED", True)
        builder = SimpleContextBuilder()
        trajectory = [_obs("s1", "A" * 40), _obs("s2", "B" * 400)]
        snap = await builder.build(
            TaskSpec(goal="g"), trajectory=trajectory, budget_tokens=0
        )

        ref = snap.meta["isolated_refs"][0]
        assert ref.startswith("obs://")
        assert ref != "obs://s2"  # 已真正外置为 artifact_id（可经 read_artifact 取回）

    @pytest.mark.asyncio
    async def test_context_engine_flag_off_byte_equal(self, monkeypatch):
        from agentix.agent.context.engine import ContextEngine
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 0)
        engine = ContextEngine()
        trajectory = [_obs("s1", "A" * 40), _obs("s2", "B" * 400)]
        snap = await engine.build(TaskSpec(goal="g"), trajectory=trajectory)

        assert "A" * 40 in snap.prompt
        assert "B" * 400 in snap.prompt
        assert "obs://" not in snap.prompt
        assert "isolated_refs" not in snap.meta

    @pytest.mark.asyncio
    async def test_context_engine_flag_on_externalizes_row(self, monkeypatch):
        from agentix.agent.context.engine import ContextEngine
        from agentix.domain.agent.types import TaskSpec

        monkeypatch.setattr(settings_instance, "TOOL_OUTPUT_BUDGET_TOKENS", 50)
        monkeypatch.setattr(settings_instance, "CONTEXT_ISOLATE_PERSIST_ENABLED", False)
        engine = ContextEngine()
        trajectory = [_obs("s1", "A" * 40), _obs("s2", "B" * 400)]
        snap = await engine.build(TaskSpec(goal="g"), trajectory=trajectory)

        assert "A" * 40 in snap.prompt
        assert "B" * 400 not in snap.prompt
        assert snap.meta["isolated_refs"] == ["obs://s2"]


# ===========================================================================
# 5. 前缀稳定性 + 指纹
# ===========================================================================

_STATIC = "RULE 你是一名法律助手。\nRULE 引用必须给出条文出处。"


class TestPrefixStability:
    def test_envelope_static_segment_byte_equal_across_builds(self):
        first = assemble(_STATIC, "动态内容甲").render()
        second = assemble(_STATIC, "动态内容乙").render()
        assert first[: len(_STATIC)] == _STATIC
        assert second[: len(_STATIC)] == _STATIC
        # 同输入两次构建逐字节相等
        assert assemble(_STATIC, "同").render() == assemble(_STATIC, "同").render()

    def test_envelope_render_order_is_static_then_dynamic(self):
        envelope = PromptEnvelope(static_prefix=_STATIC, dynamic_suffix="|tail")
        assert envelope.render() == _STATIC + "|tail"

    def test_assert_static_prefix_stable_accepts_real_router_prompt(self):
        from agentix.application.agents.lg_prompts import ROUTER_SYSTEM_PROMPT

        # 动态样本首字符即不同 ⇒ 公共前缀恰为静态常量本身
        measured = assert_static_prefix_stable(
            lambda q: ROUTER_SYSTEM_PROMPT + q,
            ["甲", "乙", "丙"],
            expected_prefix=ROUTER_SYSTEM_PROMPT,
            label="router",
        )
        assert measured == ROUTER_SYSTEM_PROMPT

    def test_fingerprint_metric_set_and_deterministic(self):
        from prometheus_client import REGISTRY

        from agentix.observability.metrics import record_prompt_prefix_fingerprint

        record_prompt_prefix_fingerprint("p94-builder", _STATIC)
        expected = float(zlib.crc32(_STATIC.encode("utf-8")) & 0xFFFFFFFF)
        sample = REGISTRY.get_sample_value(
            "prompt_prefix_fingerprint", {"builder": "p94-builder"}
        )
        if sample is None:
            pytest.skip("prometheus_client 不可用，指标断言跳过")
        assert sample == expected
        # 同内容重写 = 同值（漂移才变）
        record_prompt_prefix_fingerprint("p94-builder", _STATIC)
        assert (
            REGISTRY.get_sample_value(
                "prompt_prefix_fingerprint", {"builder": "p94-builder"}
            )
            == expected
        )

    def test_system_prompt_prefix_extraction_forms(self):
        assert _system_prompt_prefix(([],), {}) is None
        assert _system_prompt_prefix(("纯字符串输入",), {}) is None
        assert (
            _system_prompt_prefix(
                ([{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],),
                {},
            )
            == "SYS"
        )
        assert (
            _system_prompt_prefix(
                ([SystemMessage(content="SYS-B"), HumanMessage(content="U")],), {}
            )
            == "SYS-B"
        )
        assert _system_prompt_prefix(([{"role": "user", "content": "U"}],), {}) is None

    def test_fingerprint_scope_excludes_dynamic_general_role(self):
        assert "router" in _STATIC_PROMPT_ROLES
        assert "hallucination" in _STATIC_PROMPT_ROLES
        assert "general" not in _STATIC_PROMPT_ROLES

    def test_profile_defaults_untouched(self):
        """档位接线不得篡改默认 profile 表（等价基线前提）。"""
        assert DEFAULT_PROFILES["router"].temperature == 0.0
        assert DEFAULT_PROFILES["general"].temperature == 0.7
        for role, profile in DEFAULT_PROFILES.items():
            assert isinstance(profile, LLMProfile)
            assert profile.role == role
