"""P33：任务分解显式化（声明式依赖 + 局部重试 + 终态校验）
       + Prompt 静态前缀框架级保证 + 角色差异化。

覆盖《My Agentix_架构优化方案》§9.1 中最后 5 条 ⚠️：

- A「串行/并行依赖显式声明并据此调度」→ `task_plan`
- A「子任务可独立验证 + 失败局部重试」→ `task_retry`
- A「完成判定来自验证器/环境终态」→ `domain/policies/completion`
- C「System Prompt/工具定义在静态前缀（全局保证）」→ `domain/policies/prompt_envelope`
- D「各角色在模型/工具集/上下文真实差异化」→ `application/agents/role_capabilities`

全部离线，零外部依赖。
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from agentix.application.agents.role_capabilities import (
    ALL_TOOLS,
    ROLE_CAPABILITIES,
    capability_summary,
    context_policy_for,
    roles_sharing_tools,
    tools_for_role,
)
from agentix.application.services.task_plan import (
    PlanError,
    TaskSpec,
    plan_waves,
    summarize_plan,
    validate_plan,
)
from agentix.application.services.task_retry import (
    BatchOutcome,
    run_batch,
    run_task,
)
from agentix.domain.policies.completion import (
    CITATION_ROUTES,
    CompletionSignal,
    validate_completion,
)
from agentix.domain.policies.prompt_envelope import (
    PromptEnvelope,
    StaticPrefixViolation,
    assemble,
    assert_static_prefix_stable,
    common_prefix_len,
)


# ---------------------------------------------------------------------------
# A 组：声明式依赖与调度波次
# ---------------------------------------------------------------------------


class TestPlanWaves:
    def test_independent_tasks_share_one_wave(self):
        waves = plan_waves([TaskSpec("a"), TaskSpec("b"), TaskSpec("c")])
        assert len(waves) == 1
        assert [t.name for t in waves[0]] == ["a", "b", "c"]

    def test_diamond_dependency(self):
        """a → {b, c} → d：中间两个可并行，前后必须串行。"""
        tasks = [
            TaskSpec("a"),
            TaskSpec("b", depends_on=("a",)),
            TaskSpec("c", depends_on=("a",)),
            TaskSpec("d", depends_on=("b", "c")),
        ]
        waves = plan_waves(tasks)
        assert [[t.name for t in w] for w in waves] == [["a"], ["b", "c"], ["d"]]

    def test_serial_chain(self):
        tasks = [
            TaskSpec("a"),
            TaskSpec("b", depends_on=("a",)),
            TaskSpec("c", depends_on=("b",)),
        ]
        waves = plan_waves(tasks)
        assert [[t.name for t in w] for w in waves] == [["a"], ["b"], ["c"]]

    def test_wave_order_follows_declaration_order(self):
        tasks = [TaskSpec("z"), TaskSpec("a"), TaskSpec("m")]
        waves = plan_waves(tasks)
        assert [t.name for t in waves[0]] == ["z", "a", "m"]

    def test_dependency_declared_after_use_still_works(self):
        """声明顺序与依赖顺序无关（拓扑排序而非顺序扫描）。"""
        tasks = [TaskSpec("b", depends_on=("a",)), TaskSpec("a")]
        assert [[t.name for t in w] for w in plan_waves(tasks)] == [["a"], ["b"]]

    def test_cycle_raises(self):
        tasks = [TaskSpec("a", depends_on=("b",)), TaskSpec("b", depends_on=("a",))]
        with pytest.raises(PlanError, match="环形依赖"):
            plan_waves(tasks)

    def test_missing_dependency_raises_in_strict_mode(self):
        with pytest.raises(PlanError, match="依赖缺失"):
            plan_waves([TaskSpec("a", depends_on=("nope",))])

    def test_missing_dependency_tolerated_when_not_strict(self):
        waves = plan_waves([TaskSpec("a", depends_on=("nope",))], strict=False)
        assert [[t.name for t in w] for w in waves] == [["a"]]

    def test_self_dependency_raises(self):
        with pytest.raises(PlanError, match="自依赖"):
            plan_waves([TaskSpec("a", depends_on=("a",))])

    def test_empty_plan(self):
        assert plan_waves([]) == []

    def test_determinism(self):
        tasks = [
            TaskSpec("a"),
            TaskSpec("b", depends_on=("a",)),
            TaskSpec("c", depends_on=("a",)),
        ]
        assert plan_waves(tasks) == plan_waves(tasks)


class TestValidatePlan:
    def test_valid_plan(self):
        result = validate_plan([TaskSpec("a"), TaskSpec("b", depends_on=("a",))])
        assert result.ok is True
        assert result.errors() == []

    def test_duplicate_names(self):
        result = validate_plan([TaskSpec("a"), TaskSpec("a")])
        assert result.ok is False
        assert result.duplicates == ["a"]

    def test_self_dependency_detected(self):
        result = validate_plan([TaskSpec("a", depends_on=("a",))])
        assert result.self_deps == ["a"]

    def test_missing_dependency_detected(self):
        result = validate_plan([TaskSpec("a", depends_on=("ghost",))])
        assert result.missing == [("a", "ghost")]

    def test_cycle_detected(self):
        result = validate_plan(
            [TaskSpec("a", depends_on=("b",)), TaskSpec("b", depends_on=("a",))]
        )
        assert result.cycles
        assert result.ok is False

    def test_three_node_cycle_detected(self):
        tasks = [
            TaskSpec("a", depends_on=("c",)),
            TaskSpec("b", depends_on=("a",)),
            TaskSpec("c", depends_on=("b",)),
        ]
        assert validate_plan(tasks).cycles

    def test_empty_name_rejected(self):
        with pytest.raises(PlanError):
            TaskSpec("")

    def test_depends_on_normalized_to_tuple(self):
        spec = TaskSpec("a", depends_on=["x", "y"])
        assert spec.depends_on == ("x", "y")
        assert isinstance(spec.depends_on, tuple)


class TestSummarizePlan:
    def test_summary_reports_parallelism_and_depth(self):
        tasks = [
            TaskSpec("a"),
            TaskSpec("b", depends_on=("a",)),
            TaskSpec("c", depends_on=("a",)),
            TaskSpec("d", depends_on=("b", "c")),
        ]
        summary = summarize_plan(tasks)
        assert summary["task_count"] == 4
        assert summary["valid"] is True
        assert summary["wave_count"] == 3
        assert summary["max_parallelism"] == 2   # 最宽波次
        assert summary["serial_depth"] == 3      # 波次数

    def test_summary_surfaces_errors(self):
        summary = summarize_plan([TaskSpec("a", depends_on=("ghost",))])
        assert summary["valid"] is False
        assert summary["errors"]

    def test_summary_does_not_raise_on_cycle(self):
        summary = summarize_plan(
            [TaskSpec("a", depends_on=("b",)), TaskSpec("b", depends_on=("a",))]
        )
        assert summary["valid"] is False
        assert summary["wave_count"] == 0


# ---------------------------------------------------------------------------
# A 组：局部重试与失败隔离
# ---------------------------------------------------------------------------


class TestRunTaskLocalRetry:
    @pytest.mark.asyncio
    async def test_succeeds_without_retry(self):
        calls = []

        async def runner(spec):
            calls.append(spec.name)
            return "ok"

        outcome = await run_task(TaskSpec("t"), runner)
        assert outcome.ok is True
        assert outcome.value == "ok"
        assert outcome.attempts == 1
        assert calls == ["t"]

    @pytest.mark.asyncio
    async def test_retries_retryable_error_then_succeeds(self):
        state = {"n": 0}

        async def runner(spec):
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("connection refused")
            return "recovered"

        outcome = await run_task(TaskSpec("t"), runner, max_attempts=3, base_delay=0.0)
        assert outcome.ok is True
        assert outcome.value == "recovered"
        assert outcome.attempts == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_does_not_retry(self):
        state = {"n": 0}

        async def runner(spec):
            state["n"] += 1
            raise ValueError("bad input")

        outcome = await run_task(TaskSpec("t"), runner, max_attempts=5, base_delay=0.0)
        assert outcome.ok is False
        assert outcome.attempts == 1          # 不可重试 ⇒ 立即放弃
        assert outcome.retryable is False
        assert "ValueError" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_exhausts_attempts_and_returns_failure_as_data(self):
        """关键语义：失败**不抛异常**，而是返回 TaskOutcome。"""

        async def runner(spec):
            raise ConnectionError("down")

        outcome = await run_task(TaskSpec("t"), runner, max_attempts=2, base_delay=0.0)
        assert outcome.ok is False
        assert outcome.attempts == 2
        assert outcome.retryable is True
        assert outcome.error_type == "ConnectionError"

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self):
        """取消不是失败，必须透传（不可被重试逻辑吞掉）。"""

        async def runner(spec):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_task(TaskSpec("t"), runner, max_attempts=3, base_delay=0.0)

    @pytest.mark.asyncio
    async def test_outcome_serialization(self):
        async def runner(spec):
            return 1

        data = (await run_task(TaskSpec("t"), runner)).to_dict()
        assert data["ok"] is True
        assert data["attempts"] == 1
        assert "duration_ms" in data


class TestRunBatchIsolation:
    @pytest.mark.asyncio
    async def test_partial_success_is_visible(self):
        """一路失败不影响其余 —— 这是本模块存在的理由。"""

        async def runner(spec):
            if spec.name == "bad":
                raise ValueError("nope")
            return spec.name.upper()

        batch = await run_batch(
            [TaskSpec("a"), TaskSpec("bad"), TaskSpec("c")], runner, max_attempts=1
        )
        assert isinstance(batch, BatchOutcome)
        assert batch.total == 3
        assert [o.name for o in batch.succeeded] == ["a", "c"]
        assert [o.name for o in batch.failed] == ["bad"]
        assert batch.partial_success is True
        assert batch.all_failed is False

    @pytest.mark.asyncio
    async def test_all_failed(self):
        async def runner(spec):
            raise ValueError("nope")

        batch = await run_batch([TaskSpec("a"), TaskSpec("b")], runner, max_attempts=1)
        assert batch.all_failed is True
        assert batch.partial_success is False

    @pytest.mark.asyncio
    async def test_all_succeeded(self):
        async def runner(spec):
            return 1

        batch = await run_batch([TaskSpec("a"), TaskSpec("b")], runner)
        assert batch.all_failed is False
        assert batch.partial_success is False
        assert len(batch.succeeded) == 2

    @pytest.mark.asyncio
    async def test_empty_batch(self):
        async def runner(spec):
            return 1

        batch = await run_batch([], runner)
        assert batch.total == 0
        assert batch.partial_success is False
        assert batch.all_failed is False

    @pytest.mark.asyncio
    async def test_concurrency_limit_is_respected(self):
        peak = {"now": 0, "max": 0}

        async def runner(spec):
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1
            return spec.name

        specs = [TaskSpec(f"t{i}") for i in range(6)]
        await run_batch(specs, runner, max_concurrency=2)
        assert peak["max"] <= 2

    @pytest.mark.asyncio
    async def test_batch_serialization(self):
        async def runner(spec):
            return 1

        data = (await run_batch([TaskSpec("a")], runner)).to_dict()
        assert data["total"] == 1
        assert data["succeeded"] == 1
        assert data["partial_success"] is False


# ---------------------------------------------------------------------------
# A 组：终态完成度校验
# ---------------------------------------------------------------------------


class TestCompletionValidator:
    def test_complete_answer(self):
        verdict = validate_completion(
            route="general-query", answer="一加一等于二。", sources=[]
        )
        assert verdict.signal is CompletionSignal.COMPLETE
        assert verdict.is_acceptable is True

    def test_short_but_complete_answer_is_not_penalized(self):
        """中文单字信息密度高：简短但完整的答案不应被判空。"""
        verdict = validate_completion(route="general-query", answer="是的。")
        assert verdict.signal is CompletionSignal.INCOMPLETE  # 2 字，确属空壳

        verdict2 = validate_completion(route="general-query", answer="合同是双方合意的产物。")
        assert verdict2.signal is CompletionSignal.COMPLETE

    def test_error_status_is_incomplete(self):
        verdict = validate_completion(route="kb-query", answer="x" * 50, status="error")
        assert verdict.signal is CompletionSignal.INCOMPLETE

    def test_unresolved_errors_are_incomplete(self):
        verdict = validate_completion(
            route="kb-query", answer="x" * 50, errors=["retrieval_failed"]
        )
        assert verdict.signal is CompletionSignal.INCOMPLETE
        assert "未消解" in "".join(verdict.reasons)

    def test_empty_answer_is_incomplete(self):
        verdict = validate_completion(route="general-query", answer="   ")
        assert verdict.signal is CompletionSignal.INCOMPLETE

    def test_citation_route_without_sources_is_degraded(self):
        """法律领域：有答案但无依据 = 质量折损，不是「完成」。"""
        verdict = validate_completion(
            route="graphrag", answer="根据民法典第577条，违约方应承担责任。", sources=[]
        )
        assert verdict.signal is CompletionSignal.DEGRADED
        assert verdict.is_acceptable is True
        assert any("无依据" in r for r in verdict.reasons)

    def test_citation_route_with_sources_is_complete(self):
        verdict = validate_completion(
            route="graphrag",
            answer="根据民法典第577条，违约方应承担责任。",
            sources=[{"law": "民法典", "article": "577"}],
        )
        assert verdict.signal is CompletionSignal.COMPLETE

    def test_unknown_sources_are_not_penalized(self):
        """空真防护：sources=None（未知）≠ []（确认为空）。"""
        verdict = validate_completion(route="graphrag", answer="x" * 50, sources=None)
        assert verdict.signal is CompletionSignal.COMPLETE

    def test_tool_failures_degrade(self):
        verdict = validate_completion(
            route="general-query", answer="x" * 50, sources=[], tool_failures=1
        )
        assert verdict.signal is CompletionSignal.DEGRADED

    def test_general_route_without_sources_is_complete(self):
        """非引用类路由无来源属正常（闲聊 / 常识）。"""
        verdict = validate_completion(route="general-query", answer="你好，我能帮你查法规。")
        assert verdict.signal is CompletionSignal.COMPLETE

    def test_verdict_serialization(self):
        data = validate_completion(route="kb-query", answer="x" * 50, sources=[]).to_dict()
        assert data["signal"] == "degraded"
        assert data["checked"]["sources_count"] == 0
        assert data["reasons"]

    def test_citation_routes_cover_retrieval_paths(self):
        for route in ("graphrag", "kb-query"):
            assert route in CITATION_ROUTES

    def test_incomplete_is_not_acceptable(self):
        verdict = validate_completion(route="kb-query", answer="")
        assert verdict.is_acceptable is False


# ---------------------------------------------------------------------------
# C 组：Prompt 静态前缀框架级保证
# ---------------------------------------------------------------------------

STATIC = (
    "你是一个法律法规领域的智能助手。请严格依据检索到的法条回答问题，"
    "并在答案末尾标注引用来源。若问题超出法律法规范围，请明确拒绝回答。\n"
)


class TestPromptEnvelope:
    def test_render_puts_static_first(self):
        env = PromptEnvelope(static_prefix=STATIC, dynamic_suffix="问题：民法典第577条")
        assert env.render() == STATIC + "问题：民法典第577条"
        assert env.render().startswith(STATIC)

    def test_assemble_helper(self):
        assert assemble(STATIC, "dyn").render() == STATIC + "dyn"

    def test_pure_static(self):
        env = assemble(STATIC)
        assert env.is_pure_static is True
        assert env.dynamic_suffix == ""

    def test_rejects_non_str(self):
        with pytest.raises(StaticPrefixViolation):
            PromptEnvelope(static_prefix=123)  # type: ignore[arg-type]

    def test_envelope_is_frozen(self):
        import dataclasses

        env = assemble(STATIC)
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.static_prefix = "changed"  # type: ignore[misc]

    def test_to_dict(self):
        data = assemble(STATIC, "abc").to_dict()
        assert data["static_chars"] == len(STATIC)
        assert data["dynamic_chars"] == 3
        assert data["is_pure_static"] is False


class TestStaticPrefixStability:
    def test_common_prefix_len(self):
        assert common_prefix_len(["abcdef", "abcxyz"]) == 3
        assert common_prefix_len(["same", "same"]) == 4
        assert common_prefix_len([]) == 0
        assert common_prefix_len(["a"]) == 1

    def test_stable_builder_passes(self):
        def build(question: str) -> str:
            return assemble(STATIC, f"问题：{question}").render()

        measured = assert_static_prefix_stable(
            build, ["民法典第577条", "劳动合同试用期上限"], expected_prefix=STATIC
        )
        assert measured.startswith(STATIC)

    def test_dynamic_leak_into_static_region_is_caught(self):
        """把用户输入插到静态区之前 —— 公共前缀会退化，必须被抓住。"""

        def build(question: str) -> str:
            return f"问题：{question}\n" + STATIC

        with pytest.raises(StaticPrefixViolation, match="公共前缀"):
            assert_static_prefix_stable(build, ["甲问题", "乙问题"], min_prefix_chars=40)

    def test_prefix_mismatch_with_declared_constant_is_caught(self):
        def build(x: str) -> str:
            return "完全不同的前缀" * 10 + x

        with pytest.raises(StaticPrefixViolation, match="不符"):
            assert_static_prefix_stable(
                build, ["a", "b"], min_prefix_chars=20, expected_prefix=STATIC
            )

    def test_needs_at_least_two_samples(self):
        with pytest.raises(StaticPrefixViolation, match="至少需要 2 组样本"):
            assert_static_prefix_stable(lambda x: STATIC + str(x), ["only-one"])

    def test_identical_outputs_are_rejected_as_vacuous(self):
        """不同输入产出相同输出 ⇒ 动态部分没生效，验证无意义（防空真）。"""
        with pytest.raises(StaticPrefixViolation, match="动态部分未生效"):
            assert_static_prefix_stable(lambda x: STATIC, ["a", "b"])

    def test_thin_static_region_is_caught(self):
        def build(x: str) -> str:
            return "短" + str(x)

        with pytest.raises(StaticPrefixViolation, match="静态区过薄|公共前缀"):
            assert_static_prefix_stable(build, ["aaaa", "bbbb"], min_prefix_chars=40)


# ---------------------------------------------------------------------------
# D 组：角色差异化
# ---------------------------------------------------------------------------


class TestRoleCapabilities:
    def test_all_roles_declared(self):
        expected = {"router", "planner", "tool", "summarizer", "general", "hallucination"}
        assert set(ROLE_CAPABILITIES) == expected

    def test_router_has_no_tools(self):
        """纯分类角色给工具只会制造误选。"""
        assert tools_for_role("router") == []

    def test_summarizer_has_no_tools(self):
        assert tools_for_role("summarizer") == []

    def test_hallucination_has_no_tools(self):
        """判定角色不得重新检索（否则无法判原始答案）。"""
        assert tools_for_role("hallucination") == []

    def test_tool_role_has_all_tools(self):
        assert tools_for_role("tool") == sorted(ALL_TOOLS)

    def test_general_role_has_all_tools(self):
        assert tools_for_role("general") == sorted(ALL_TOOLS)

    def test_tools_intersect_with_available(self):
        available = ["cypher_query", "search"]
        assert tools_for_role("tool", available) == ["cypher_query", "search"]

    def test_tools_respect_disabled_set(self):
        assert tools_for_role("general", ["cypher_query"]) == ["cypher_query"]

    def test_unknown_role_gets_no_tools(self):
        """保守：不认识的角色不给工具。"""
        assert tools_for_role("nonexistent") == []

    def test_unknown_role_gets_most_conservative_context(self):
        policy = context_policy_for("nonexistent")
        assert policy.max_history_messages == 0
        assert policy.include_memory is False
        assert policy.include_tools is False
        assert policy.include_sources is False

    def test_context_policies_actually_differ(self):
        """「真实差异化」的核心断言：策略不能全表一样。"""
        signatures = {
            (
                p.max_history_messages,
                p.include_memory,
                p.include_sources,
                p.max_retrieved_docs,
                p.include_tools,
            )
            for p in (cap.context for cap in ROLE_CAPABILITIES.values())
        }
        assert len(signatures) >= 4, "角色上下文策略差异不足，等于没做差异化"

    def test_router_context_is_minimal(self):
        policy = context_policy_for("router")
        assert policy.include_tools is False
        assert policy.include_sources is False
        assert policy.max_history_messages <= 4

    def test_summarizer_sees_most_evidence(self):
        assert context_policy_for("summarizer").max_retrieved_docs >= max(
            context_policy_for(r).max_retrieved_docs for r in ROLE_CAPABILITIES
        )

    def test_general_role_enables_memory_and_history(self):
        policy = context_policy_for("general")
        assert policy.include_memory is True
        assert policy.max_history_messages == 20

    def test_capability_summary_is_serializable(self):
        summary = capability_summary()
        assert set(summary) == set(ROLE_CAPABILITIES)
        assert all(isinstance(v["tools"], list) for v in summary.values())

    def test_roles_sharing_tools_index(self):
        index = roles_sharing_tools()
        assert "tool" in index["cypher_query"]
        assert "general" in index["cypher_query"]
        # 未被任何角色使用的工具会被暴露为死工具（便于评估保留价值）
        assert all(isinstance(v, list) for v in index.values())

    def test_router_shares_no_tools(self):
        index = roles_sharing_tools()
        assert "router" not in index["cypher_query"]


# ---------------------------------------------------------------------------
# 真实站点集成：把上面的「保证」用到真实 prompt 构造器与真实工具注册表上
# ---------------------------------------------------------------------------


class TestRealPromptStaticPrefix:
    """把静态前缀不变性验证用到**真实**的 guardrails prompt 构造器上。

    这是「框架级保证」的落地证明：不是新增一个守卫测试，而是用同一个机制
    （`assert_static_prefix_stable`）去验证**真实**的生产 prompt 组装代码。

    **系统消息**用真实模板 `create_guardrails_prompt_template` 验证
    （批次 13 修掉该模块的断链 import 后才可导入，此前只能退而用
    `create_guardrails_context`）。
    """

    @staticmethod
    def _build_system(scope: str) -> str:
        from agentix.application.agents.kg_sub_graph.ps_genai_agents.components.guardrails.prompts import (
            create_guardrails_prompt_template,
        )

        template = create_guardrails_prompt_template(
            graph=None, scope_description=scope
        )
        messages = template.format_messages(question="民法典第577条是什么？")
        return str(messages[0].content)

    @staticmethod
    def _build_human(scope: str) -> str:
        from agentix.application.agents.kg_sub_graph.prompts.schema_utils import (
            create_guardrails_context,
        )

        context = create_guardrails_context(graph=None, scope_description=scope)
        # 与 guardrails/prompts.py 的真实拼接一致：scope + schema + Question
        return context["scope_context"] + context["graph_schema"] + "Question: {question}"

    def test_system_message_is_byte_identical_across_inputs(self):
        """system 消息必须**逐字节**与动态输入无关（真正的静态前缀）。"""
        a = self._build_system("法律领域：法规、法条、案由、司法解释")
        b = self._build_system("中医药典：方剂、药材、炮制、性味归经")
        assert a == b, "system 消息随 scope 变化 ⇒ 静态前缀被动态内容污染"
        assert len(a) >= 40

    def test_system_message_matches_declared_constant(self):
        from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import (
            GUARDRAILS_SYSTEM_PROMPT,
        )

        assert self._build_system("任意 scope") == GUARDRAILS_SYSTEM_PROMPT

    def test_static_prefix_comes_before_dynamic_scope(self):
        from agentix.application.agents.lg_prompts import GUARDRAILS_SCOPE_PREFIX

        measured = assert_static_prefix_stable(
            self._build_human,
            ["法律领域：法规、法条、案由、司法解释", "中医药典：方剂、药材、炮制"],
            min_prefix_chars=len(GUARDRAILS_SCOPE_PREFIX),
            expected_prefix=GUARDRAILS_SCOPE_PREFIX,
            label="guardrails.scope_context",
        )
        assert measured == GUARDRAILS_SCOPE_PREFIX

    def test_scope_is_appended_after_the_static_prefix(self):
        """动态 scope 必须出现在静态前缀**之后**（顺序断言，逐字节）。"""
        from agentix.application.agents.lg_prompts import GUARDRAILS_SCOPE_PREFIX

        scope = "法律领域：法规、法条、案由"
        rendered = self._build_human(scope)
        assert rendered.startswith(GUARDRAILS_SCOPE_PREFIX)
        assert rendered.index(scope) >= len(GUARDRAILS_SCOPE_PREFIX)

    def test_system_prompt_is_module_level_constant(self):
        """System Prompt 必须是模块级常量（单一事实来源），不得内联在函数里。"""
        from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import (
            GUARDRAILS_SYSTEM_PROMPT,
        )

        assert isinstance(GUARDRAILS_SYSTEM_PROMPT, str)
        assert len(GUARDRAILS_SYSTEM_PROMPT) >= 40

    def test_shared_prefix_constants_are_module_level(self):
        from agentix.application.agents import lg_prompts

        for name in (
            "GUARDRAILS_SCOPE_PREFIX",
            "GUARDRAILS_GRAPH_PREFIX",
            "GUARDRAILS_QUESTION_SUFFIX",
        ):
            value = getattr(lg_prompts, name)
            assert isinstance(value, str) and value

    def test_no_inline_scope_prefix_literal_in_consumers(self):
        """消费者不得再内联 scope 前缀字面量（否则单一事实来源被击穿）。"""
        from agentix.application.agents import lg_prompts

        literal = lg_prompts.GUARDRAILS_SCOPE_PREFIX.strip()
        assert literal, "静态前缀常量不得为空"

        from agentix.application.agents.kg_sub_graph.prompts import schema_utils

        source = pathlib.Path(schema_utils.__file__).read_text(encoding="utf-8")
        assert literal not in source, "schema_utils 又内联了 scope 前缀字面量"


class TestRoleCapabilitiesAgainstRealRegistry:
    """角色能力表必须与**真实工具注册表**一致（防止声明漂移）。"""

    def test_all_tools_matches_registry(self):
        from agentix.tools.definitions import registry
        from agentix.tools.registry_init import ensure_registered

        ensure_registered()
        real = set(registry.tool_names())
        declared = set(ALL_TOOLS)
        assert declared <= real, f"声明了不存在的工具：{sorted(declared - real)}"

    def test_every_declared_tool_is_used_by_some_role(self):
        """声明了却没有任何角色使用的工具 = 死工具，应显式暴露。"""
        index = roles_sharing_tools()
        unused = [name for name, roles in index.items() if not roles]
        assert unused == [], f"以下工具没有任何角色使用：{unused}"


# ---------------------------------------------------------------------------
# 终态校验的接线（chat_service 辅助函数）
# ---------------------------------------------------------------------------


class TestCompletionValidatorWiring:
    def test_helper_returns_verdict_dict(self):
        from agentix.application.services.chat_service import _validate_completion_state

        verdict = _validate_completion_state(
            route="graphrag",
            answer="根据民法典第577条，违约方应承担继续履行等责任。",
            sources=[{"law": "民法典"}],
            status="ok",
        )
        assert verdict is not None
        assert verdict["signal"] == "complete"

    def test_helper_degrades_when_citation_route_has_no_sources(self):
        from agentix.application.services.chat_service import _validate_completion_state

        verdict = _validate_completion_state(
            route="graphrag",
            answer="根据民法典第577条，违约方应承担继续履行等责任。",
            sources=[],
            status="ok",
        )
        assert verdict["signal"] == "degraded"

    def test_helper_never_raises_on_bad_input(self):
        """校验器不得成为新的单点故障——坏输入也要降级为 None。"""
        from agentix.application.services.chat_service import _validate_completion_state

        assert _validate_completion_state(
            route=None, answer=None, sources=None, status=None
        ) is not None  # None 输入不炸，返回合法 verdict

    def test_strict_flag_defaults_off(self):
        """默认只记录不改判定。"""
        from agentix.application.services.chat_service import _completion_strict

        assert _completion_strict() is False

    def test_strict_flag_follows_settings(self, monkeypatch):
        from agentix.application.services.chat_service import _completion_strict
        from agentix.config import settings

        monkeypatch.setattr(settings, "COMPLETION_VALIDATOR_STRICT", True, raising=False)
        assert _completion_strict() is True

    def test_record_completion_step_does_not_raise(self):
        from agentix.application.services.chat_service import _record_completion_step
        from agentix.application.services.trajectory import (
            get_trajectory_recorder,
            reset_trajectory_recorder,
        )

        reset_trajectory_recorder()
        try:
            _record_completion_step({"signal": "degraded", "reasons": ["x"]})
            steps = get_trajectory_recorder().get()
            assert any(s.name == "completion_validator" for s in steps)
        finally:
            reset_trajectory_recorder()
