"""P32：工具结果限量（分页/截断）+ 高风险动作人工审批旁路。

覆盖《My Agentix_架构优化方案》§9.1 B 组缺口：
- 「感知工具默认分页/截断/限量」（下游结果侧）
- 「高风险动作有独立 Sidecar 或人工审批旁路」

全部离线，零外部依赖。
"""

from __future__ import annotations

import operator  # 模块级：TypedDict 注解由 get_type_hints 解析，需在此命名空间可见
from typing import Annotated, List

import pytest

from agentix.tools.gateway.human_review import (
    DEFAULT_POLICY,
    HumanReviewGate,
    ReviewDecision,
    ReviewPolicy,
    RiskLevel,
    classify_risk,
    requires_human_review,
    reset_review_gate,
)
from agentix.tools.gateway.result_limiter import (
    TRUNCATION_SUFFIX,
    PageMeta,
    limit_rows,
    limit_value,
    make_cursor,
    parse_cursor,
    truncate_text,
)


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------


class TestCursor:
    def test_roundtrip(self):
        assert parse_cursor(make_cursor(42)) == 42

    def test_invalid_cursor_defaults_to_zero(self):
        """工具层要稳：坏游标不得抛异常。"""
        assert parse_cursor(None) == 0
        assert parse_cursor("garbage") == 0
        assert parse_cursor("") == 0
        assert parse_cursor("-5") == 0

    def test_negative_offset_clamped(self):
        assert make_cursor(-10) == "0"


# ---------------------------------------------------------------------------
# 行分页
# ---------------------------------------------------------------------------


class TestLimitRows:
    def test_first_page_and_has_more(self):
        rows = list(range(250))
        result = limit_rows(rows, max_rows=100)
        assert result.value == list(range(100))
        assert result.meta.total_rows == 250
        assert result.meta.returned_rows == 100
        assert result.meta.has_more is True
        assert result.meta.next_cursor == "100"
        assert result.meta.dropped_rows == 150

    def test_second_page_via_cursor(self):
        rows = list(range(250))
        result = limit_rows(rows, max_rows=100, cursor="100")
        assert result.value == list(range(100, 200))
        assert result.meta.offset == 100
        assert result.meta.has_more is True

    def test_last_page_has_no_more(self):
        rows = list(range(250))
        result = limit_rows(rows, max_rows=100, cursor="200")
        assert result.value == list(range(200, 250))
        assert result.meta.has_more is False
        assert result.meta.next_cursor is None

    def test_zero_max_rows_disables_limiting(self):
        rows = list(range(10))
        result = limit_rows(rows, max_rows=0)
        assert result.value == rows
        assert result.meta.has_more is False
        assert result.meta.limit == 0

    def test_cursor_beyond_end_returns_empty(self):
        result = limit_rows([1, 2, 3], max_rows=10, cursor="99")
        assert result.value == []
        assert result.meta.has_more is False

    def test_notice_mentions_remaining(self):
        result = limit_rows(list(range(250)), max_rows=100)
        notice = result.meta.notice()
        assert "250" in notice and "150" in notice and "next_cursor" in notice

    def test_no_notice_when_nothing_dropped(self):
        assert limit_rows([1, 2], max_rows=10).meta.notice() == ""


# ---------------------------------------------------------------------------
# 字符截断
# ---------------------------------------------------------------------------


class TestTruncateText:
    def test_short_text_untouched(self):
        result = truncate_text("hello", max_chars=100)
        assert result.value == "hello"
        assert result.meta.truncated is False

    def test_long_text_truncated_with_marker(self):
        result = truncate_text("x" * 1000, max_chars=100)
        assert result.value.startswith("x" * 100)
        assert result.value.endswith(TRUNCATION_SUFFIX)
        assert result.meta.truncated is True
        assert result.meta.truncated_chars == 900

    def test_zero_disables_truncation(self):
        result = truncate_text("y" * 5000, max_chars=0)
        assert result.value == "y" * 5000
        assert result.meta.truncated is False


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


class TestLimitValue:
    def test_list_is_paginated(self):
        result = limit_value(list(range(300)), max_rows=50, max_chars=0)
        assert len(result.value) == 50
        assert result.meta.has_more is True

    def test_dict_rows_key_is_paginated(self):
        payload = {"rows": list(range(300)), "query_id": "q1"}
        result = limit_value(payload, max_rows=50, max_chars=0)
        assert len(result.value["rows"]) == 50
        assert result.value["query_id"] == "q1"   # 其余键原样保留
        assert result.meta.has_more is True

    def test_data_key_is_recognized(self):
        result = limit_value({"data": list(range(10))}, max_rows=5, max_chars=0)
        assert len(result.value["data"]) == 5

    def test_string_is_truncated(self):
        result = limit_value("z" * 20000, max_chars=500)
        assert result.meta.truncated is True
        assert len(result.value) < 20000

    def test_other_types_are_stringified(self):
        result = limit_value(12345, max_chars=3)
        assert isinstance(result.value, str)

    def test_char_budget_shrinks_rows_not_just_flags(self):
        """关键：字符超限必须**真的减少行数**，而不是只打个 truncated 标记。"""
        fat_rows = [{"body": "a" * 500} for _ in range(100)]
        result = limit_value(fat_rows, max_rows=100, max_chars=2000)
        assert result.meta.truncated is True
        assert len(result.value) < 100
        assert result.meta.returned_rows == len(result.value)
        assert result.meta.truncated_chars > 0

    def test_single_oversized_row_degrades_to_text(self):
        rows = [{"body": "b" * 10000}]
        result = limit_value(rows, max_rows=10, max_chars=200)
        assert isinstance(result.value, str)
        assert result.meta.truncated is True

    def test_with_notice_wraps_list(self):
        result = limit_value(list(range(300)), max_rows=50, max_chars=0)
        wrapped = result.with_notice()
        assert isinstance(wrapped, dict)
        assert "notice" in wrapped and "page" in wrapped
        assert len(wrapped["rows"]) == 50

    def test_with_notice_passthrough_when_clean(self):
        result = limit_value([1, 2, 3], max_rows=10, max_chars=0)
        assert result.with_notice() == [1, 2, 3]

    def test_page_meta_to_dict(self):
        meta = PageMeta(total_rows=10, returned_rows=5, offset=0, limit=5, has_more=True)
        data = meta.to_dict()
        assert data["dropped_rows"] == 5
        assert data["has_more"] is True


# ---------------------------------------------------------------------------
# 风险分级
# ---------------------------------------------------------------------------


class TestRiskClassification:
    def test_explicit_mapping_wins(self):
        assert classify_risk("cypher_query") == RiskLevel.LOW

    def test_delete_pattern_is_critical(self):
        assert classify_risk("drop_table") == RiskLevel.CRITICAL
        assert classify_risk("delete_all_records") == RiskLevel.CRITICAL

    def test_write_pattern_is_high(self):
        assert classify_risk("update_user_profile") == RiskLevel.HIGH
        assert classify_risk("create_document") == RiskLevel.HIGH

    def test_execute_pattern_is_high(self):
        assert classify_risk("execute_shell_command") == RiskLevel.HIGH

    def test_import_pattern_is_medium(self):
        assert classify_risk("import_history_data") == RiskLevel.MEDIUM

    def test_unknown_defaults_to_low(self):
        assert classify_risk("totally_unknown_tool") == RiskLevel.LOW

    def test_args_can_escalate_never_deescalate(self):
        assert classify_risk("search_documents", {"force": True}) == RiskLevel.CRITICAL
        assert classify_risk("update_user_profile", {"all": True}) == RiskLevel.HIGH
        # 低风险参数不得把高风险降下来
        assert classify_risk("drop_table", {"force": False}) == RiskLevel.CRITICAL

    def test_truthy_variants(self):
        for value in (True, 1, "true", "yes", "on"):
            assert classify_risk("x", {"force": value}) == RiskLevel.CRITICAL
        for value in (False, 0, "false", "", None):
            assert classify_risk("x", {"force": value}) == RiskLevel.LOW

    def test_custom_policy(self):
        policy = ReviewPolicy(explicit={"my_tool": RiskLevel.CRITICAL}, use_name_patterns=False)
        assert classify_risk("my_tool", policy=policy) == RiskLevel.CRITICAL
        assert classify_risk("drop_table", policy=policy) == RiskLevel.LOW


# ---------------------------------------------------------------------------
# 审批旁路
# ---------------------------------------------------------------------------


class TestHumanReviewGateDisabled:
    def test_disabled_gate_is_passthrough(self):
        gate = HumanReviewGate(enabled=False)
        assert gate.requires_review("drop_table") is False

    @pytest.mark.asyncio
    async def test_disabled_gate_approves_everything(self):
        gate = HumanReviewGate(enabled=False)
        decision, request = await gate.decide("drop_table", {"force": True})
        assert decision == ReviewDecision.APPROVED
        assert request is None


class TestHumanReviewGateEnabled:
    def test_low_risk_needs_no_review(self):
        gate = HumanReviewGate(enabled=True)
        assert gate.requires_review("cypher_query") is False

    def test_high_risk_needs_review(self):
        gate = HumanReviewGate(enabled=True)
        assert gate.requires_review("drop_table") is True

    @pytest.mark.asyncio
    async def test_low_risk_auto_approved(self):
        gate = HumanReviewGate(enabled=True)
        decision, request = await gate.decide("cypher_query")
        assert decision == ReviewDecision.APPROVED
        assert request is None

    @pytest.mark.asyncio
    async def test_no_approver_fails_closed(self):
        """「没人能批」不等于「可以放行」——必须 PENDING。"""
        gate = HumanReviewGate(enabled=True)
        decision, request = await gate.decide("drop_table")
        assert decision == ReviewDecision.PENDING
        assert request is not None
        assert request.risk_level == RiskLevel.CRITICAL
        assert gate.stats()["pending"] == 1

    @pytest.mark.asyncio
    async def test_approver_can_approve(self):
        async def approver(request):
            return ReviewDecision.APPROVED

        gate = HumanReviewGate(enabled=True, approver=approver)
        decision, request = await gate.decide("update_user_profile")
        assert decision == ReviewDecision.APPROVED
        assert request.decided_at is not None

    @pytest.mark.asyncio
    async def test_approver_can_reject(self):
        async def approver(request):
            return ReviewDecision.REJECTED

        gate = HumanReviewGate(enabled=True, approver=approver)
        decision, _ = await gate.decide("delete_records")
        assert decision == ReviewDecision.REJECTED

    @pytest.mark.asyncio
    async def test_approver_exception_fails_closed(self):
        async def broken(request):
            raise RuntimeError("approval channel down")

        gate = HumanReviewGate(enabled=True, approver=broken)
        decision, request = await gate.decide("drop_table")
        assert decision == ReviewDecision.PENDING
        assert request is not None

    @pytest.mark.asyncio
    async def test_manual_approve_flow(self):
        gate = HumanReviewGate(enabled=True)
        _, request = await gate.decide("drop_table")
        assert gate.approve(request.request_id, by="alice", note="已核对") is True
        stored = gate.get(request.request_id)
        assert stored.decision == ReviewDecision.APPROVED
        assert stored.decided_by == "alice"
        assert gate.pending() == []

    @pytest.mark.asyncio
    async def test_manual_reject_flow(self):
        gate = HumanReviewGate(enabled=True)
        _, request = await gate.decide("drop_table")
        assert gate.reject(request.request_id) is True
        assert gate.get(request.request_id).decision == ReviewDecision.REJECTED

    @pytest.mark.asyncio
    async def test_cannot_decide_twice(self):
        gate = HumanReviewGate(enabled=True)
        _, request = await gate.decide("drop_table")
        gate.approve(request.request_id)
        assert gate.reject(request.request_id) is False

    @pytest.mark.asyncio
    async def test_unknown_request_id(self):
        gate = HumanReviewGate(enabled=True)
        assert gate.approve("nope") is False
        assert gate.get("nope") is None

    @pytest.mark.asyncio
    async def test_args_digest_is_bounded(self):
        gate = HumanReviewGate(enabled=True)
        _, request = await gate.decide("drop_table", {"payload": "x" * 5000})
        assert len(request.args_digest["payload"]) < 400

    @pytest.mark.asyncio
    async def test_stats_and_bounds(self):
        gate = HumanReviewGate(enabled=True, max_requests=3)
        for _ in range(5):
            await gate.decide("drop_table")
        assert gate.stats()["total"] == 3
        assert gate.stats()["pending"] == 3


class TestReviewGateSingleton:
    def test_module_level_helper_and_reset(self):
        reset_review_gate()
        # 默认关闭（settings.HUMAN_REVIEW_ENABLED=False）⇒ 直通
        assert requires_human_review("drop_table") is False
        reset_review_gate()

    def test_default_policy_marks_readonly_tools_low(self):
        for name in ("cypher_query", "text2sql_query", "search_documents"):
            assert DEFAULT_POLICY.resolve(name) == RiskLevel.LOW


# ---------------------------------------------------------------------------
# 框架限制刻画：LangGraph interrupt 的 sync / async 差异
# ---------------------------------------------------------------------------


class TestLangGraphInterruptCharacterization:
    """刻画当前 LangGraph 版本下 `interrupt()` 的能力边界。

    **为什么用测试来记录**（而不是只写在文档里）：这是一个**框架限制**，
    文档会腐、会被忽略；测试则在框架升级后**主动告诉你边界变了**。

    结论（批次 13 用最小复现实测）：

    - **sync 节点**：`interrupt()` 可用 —— 暂停 → `Command(resume=…)` 恢复并取回裁决值
    - **async 节点**：抛 `RuntimeError: Called get_configurable outside of a runnable context`

    由此推出：本仓库工具节点均为 async，故「包装现有工具节点」的接线方式不成立；
    可行做法是另插 **sync 审批节点**并改 `tool_selection` 的路由表（属图拓扑变更，需单独授权）。

    ⚠️ 若将来升级 LangGraph 使 async 也可用，**本测试会失败** —— 那正是它存在的意义：
    它提醒你重新评估上面那段结论，而不是继续按旧认知回避一个已经可行的方案。
    """

    def _graph(self, *, async_node: bool):
        from typing import TypedDict

        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        class S(TypedDict, total=False):
            steps: Annotated[List[str], operator.add]

        async def anode(state):
            decision = interrupt({"tool": "drop_all_tables"})
            return {"steps": [f"resumed:{decision}"]}

        def snode(state):
            decision = interrupt({"tool": "drop_all_tables"})
            return {"steps": [f"resumed:{decision}"]}

        builder = StateGraph(S)
        builder.add_node("n", anode if async_node else snode)
        builder.add_edge(START, "n")
        builder.add_edge("n", END)
        return builder.compile(checkpointer=MemorySaver())

    def test_sync_node_can_interrupt(self):
        from langgraph.types import Command

        app = self._graph(async_node=False)
        cfg = {"configurable": {"thread_id": "chr-sync"}}
        app.invoke({"steps": []}, config=cfg)
        resumed = app.invoke(Command(resume="approve"), config=cfg)
        assert "resumed:approve" in resumed.get("steps", []), (
            "sync 节点应能中断并恢复 —— 若失败，说明 LangGraph 版本行为已变，请重新评估审批方案"
        )

    @pytest.mark.asyncio
    async def test_async_node_cannot_interrupt(self):
        """async 节点 `interrupt()` 行为特征化（按解释器版本分支）。

        py3.10：抛 RuntimeError('runnable context')——当前版本在 py3.10 下的
        已知限制；
        py3.12+：限制解除——interrupt 正常挂起（首次返回原 state），恢复后
        完成节点（库行为随解释器变化，特征化按版本跟随；审批语义不变）。
        """
        import sys

        app = self._graph(async_node=True)
        cfg = {"configurable": {"thread_id": "chr-async"}}
        if sys.version_info >= (3, 12):
            from langgraph.types import Command

            first = await app.ainvoke({"steps": []}, config=cfg)
            assert first.get("steps") == []  # 挂起：节点未完成
            resumed = await app.ainvoke(Command(resume="approve"), config=cfg)
            assert "resumed:approve" in resumed.get("steps", [])
        else:
            with pytest.raises(RuntimeError, match="runnable context"):
                await app.ainvoke({"steps": []}, config=cfg)


# ---------------------------------------------------------------------------
# 人工裁决归一化（fail-closed）
# ---------------------------------------------------------------------------


class TestNormalizeDecision:
    """裁决归一化：看不懂的裁决**一律拒绝**，绝不默认放行。"""

    @pytest.mark.parametrize(
        "value", ["approve", "APPROVED", "yes", True, "放行", {"decision": "approve"}]
    )
    def test_approve_variants(self, value):
        from agentix.tools.gateway.human_review import (
    ReviewDecision,
    normalize_decision,
)

        assert normalize_decision(value) is ReviewDecision.APPROVED

    @pytest.mark.parametrize(
        "value", ["reject", "DENIED", "no", False, "拒绝", {"decision": "reject"}]
    )
    def test_reject_variants(self, value):
        from agentix.tools.gateway.human_review import (
    ReviewDecision,
    normalize_decision,
)

        assert normalize_decision(value) is ReviewDecision.REJECTED

    @pytest.mark.parametrize("value", [None, "", "不知道", 123, {"foo": "bar"}, []])
    def test_unrecognised_is_rejected_fail_closed(self, value):
        from agentix.tools.gateway.human_review import (
    ReviewDecision,
    normalize_decision,
)

        assert normalize_decision(value) is ReviewDecision.REJECTED

    def test_passthrough_enum(self):
        from agentix.tools.gateway.human_review import (
    ReviewDecision,
    normalize_decision,
)

        assert normalize_decision(ReviewDecision.APPROVED) is ReviewDecision.APPROVED


# ---------------------------------------------------------------------------
# 同步前置检查（fail-closed 硬闸门）
# ---------------------------------------------------------------------------


class TestPreflightReview:
    def test_disabled_gate_is_noop(self):
        gate = HumanReviewGate(enabled=False)
        assert gate.preflight("drop_table") is None

    def test_low_risk_passes(self):
        gate = HumanReviewGate(enabled=True)
        assert gate.preflight("cypher_query") is None

    def test_high_risk_without_approval_raises(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table")
        request = exc_info.value.request
        assert request.tool_name == "drop_table"
        assert request.risk_level == RiskLevel.CRITICAL
        # 被拦下的请求进入待审队列
        assert gate.stats()["pending"] == 1

    def test_approval_lets_it_through_once(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table")
        request_id = exc_info.value.request.request_id

        assert gate.approve(request_id, by="alice") is True
        # 第一次：批准被消费，放行
        consumed = gate.preflight("drop_table")
        assert consumed is not None and consumed.consumed is True
        # 第二次：批准已用掉 ⇒ 再次 fail-closed（一次批准只放行一次）
        with pytest.raises(ReviewRequired):
            gate.preflight("drop_table")

    def test_rejection_does_not_let_it_through(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table")
        gate.reject(exc_info.value.request.request_id)
        with pytest.raises(ReviewRequired):
            gate.preflight("drop_table")

    def test_approval_is_tool_scoped(self):
        """批准 drop_table 不应放行 delete_other。"""
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table")
        gate.approve(exc_info.value.request.request_id)
        gate.preflight("drop_table")  # 消费掉
        with pytest.raises(ReviewRequired):
            gate.preflight("delete_other")

    def test_get_tool_wiring_is_default_off(self, monkeypatch):
        """默认关闭时 get_tool 行为不变（可正常实例化低风险与高风险工具）。"""
        from agentix.tools import get_tool

        reset_review_gate()
        assert get_tool("search") is not None

    def test_get_tool_blocks_when_enabled(self, monkeypatch):
        """开启审批后，高风险工具实例化被硬闸门拦下。"""
        from agentix.config import settings
        from agentix.tools import get_tool
        from agentix.tools.gateway.human_review import (
    ReviewRequired,
    reset_review_gate,
)

        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        get_tool.cache_clear()
        try:
            with pytest.raises(ReviewRequired):
                get_tool("drop_all_tables")
        finally:
            get_tool.cache_clear()
            reset_review_gate()


# ---------------------------------------------------------------------------
# 修正记录（安全判定不可依赖 dict 顺序 / 批准范围必须绑定调用）
# ---------------------------------------------------------------------------


class TestArgEscalationIsOrderInsensitive:
    """参数提权必须取"最高等级"，不能"先命中者胜"。

    原实现 `for key, value in args.items(): ... return ...` 会在第一个命中键
    处返回，于是 `{'all': True, 'force': True}`（字典序 all 在前）被判为 HIGH，
    而 `{'force': True, 'all': True}` 被判为 CRITICAL —— **同一组参数**仅因
    书写顺序不同就得到不同风险等级。安全判定不允许这种不确定性。
    """

    def test_both_orders_yield_critical(self):
        from agentix.tools.gateway.human_review import _arg_escalation

        assert _arg_escalation({"all": True, "force": True}) is RiskLevel.CRITICAL
        assert _arg_escalation({"force": True, "all": True}) is RiskLevel.CRITICAL

    def test_classify_risk_escalates_low_risk_tool_by_args(self):
        """只读工具名 + 高危参数 ⇒ 必须提权（当前 get_tool 路径传不到 args，
        但真调用点若传 args，判定逻辑本身必须正确）。"""
        assert classify_risk("cypher_query", {"force": True}) is RiskLevel.CRITICAL
        assert classify_risk("cypher_query", {"all": True}) is RiskLevel.HIGH

    def test_args_never_downgrade(self):
        """参数只能提权、不能降权。"""
        assert classify_risk("drop_table", {"all": False}) is RiskLevel.CRITICAL

    def test_non_trigger_keys_ignored(self):
        from agentix.tools.gateway.human_review import _arg_escalation

        assert _arg_escalation({"limit": 10, "offset": 0}) is None
        assert _arg_escalation({"all": False, "force": 0}) is None


class TestPreflightApprovalScope:
    """一次批准的放行范围必须绑定「工具名 + 风险等级（CRITICAL 还需参数）」，
    否则"批了一次小操作"会变成"放行一次更危险的操作"。"""

    def test_medium_approval_does_not_release_critical_call(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        # 1) 批一次 MEDIUM（import_data 命中 import 模式，参数无提权）
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("import_data", {"path": "small.csv"})
        assert exc_info.value.request.risk_level is RiskLevel.MEDIUM
        gate.approve(exc_info.value.request.request_id)

        # 2) 同工具名 + CRITICAL 参数 ⇒ 不得被上面那次 MEDIUM 批准放行
        with pytest.raises(ReviewRequired) as exc_info2:
            gate.preflight("import_data", {"all": True, "force": True})
        assert exc_info2.value.request.risk_level is RiskLevel.CRITICAL

    def test_critical_approval_requires_identical_args(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table", {"table": "users"})
        gate.approve(exc_info.value.request.request_id)

        # 参数不同 ⇒ 不匹配（CRITICAL 要求逐位相同）
        with pytest.raises(ReviewRequired):
            gate.preflight("drop_table", {"table": "orders"})

    def test_same_tool_same_level_same_args_is_released_once(self):
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table", {"table": "users"})
        gate.approve(exc_info.value.request.request_id)

        consumed = gate.preflight("drop_table", {"table": "users"})
        assert consumed is not None and consumed.consumed is True
        with pytest.raises(ReviewRequired):
            gate.preflight("drop_table", {"table": "users"})

    def test_digest_equality_ignores_key_order(self):
        """参数摘要比对按内容而非插入顺序。"""
        from agentix.tools.gateway.human_review import ReviewRequired

        gate = HumanReviewGate(enabled=True)
        with pytest.raises(ReviewRequired) as exc_info:
            gate.preflight("drop_table", {"table": "users", "cascade": False})
        gate.approve(exc_info.value.request.request_id)

        consumed = gate.preflight("drop_table", {"cascade": False, "table": "users"})
        assert consumed is not None


class TestReviewGateStartupVisibility:
    """开启审批但未接审批人 ⇒ 高风险工具全线阻断，必须显式告警。

    注意：本项目用 **loguru**（不是 stdlib ``logging``），因此 pytest 的
    ``caplog`` 抓不到这类日志——必须挂一个 loguru sink。用 ``caplog`` 会静默
    拿到空字符串，是"看错通道导致假红/假绿"的典型坑。
    """

    @staticmethod
    def _capture_loguru(level: str = "WARNING"):
        """挂一个临时 loguru sink，返回 (sink_list, handle_id)。"""
        from loguru import logger

        sink: List[str] = []
        handle_id = logger.add(lambda msg: sink.append(str(msg)), level=level)
        return sink, handle_id

    @staticmethod
    def _detach_loguru(handle_id) -> None:
        from loguru import logger

        logger.remove(handle_id)

    def test_enabled_without_approver_logs_warning(self, monkeypatch):
        from agentix.config import settings
        from agentix.tools.gateway.human_review import get_review_gate

        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", True, raising=False)
        reset_review_gate()
        sink, handle_id = self._capture_loguru()
        try:
            gate = get_review_gate()
        finally:
            self._detach_loguru(handle_id)
            reset_review_gate()

        assert gate.enabled is True
        assert any("no approver is wired" in line for line in sink), sink

    def test_disabled_gate_logs_nothing(self, monkeypatch):
        from agentix.config import settings
        from agentix.tools.gateway.human_review import get_review_gate

        monkeypatch.setattr(settings, "HUMAN_REVIEW_ENABLED", False, raising=False)
        reset_review_gate()
        sink, handle_id = self._capture_loguru()
        try:
            get_review_gate()
        finally:
            self._detach_loguru(handle_id)
            reset_review_gate()

        assert not any("no approver is wired" in line for line in sink), sink
