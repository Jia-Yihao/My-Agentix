"""Phase 8 — HITL Sidecar：上下文隔离、四裁决、fail-closed、重启恢复。"""

from __future__ import annotations

import asyncio

import pytest

from agentix.policies.hitl import (
    ReviewDecisionAction,
    ReviewerSidecar,
    SidecarReviewRequest,
)


def _req(**kw) -> SidecarReviewRequest:
    tool = kw.pop("tool_name", "send_notice")
    return SidecarReviewRequest.create(tool, kw.pop("arguments", {"to": "x"}), **kw)


class TestContextIsolation:
    """Reviewer 只见结构化面，不见 Agent 自述/trajectory。"""

    def test_public_dict_has_no_narrative_fields(self):
        req = _req()
        pub = req.to_public_dict()
        for banned in ("trajectory", "prompt", "narrative", "rationale", "messages"):
            assert banned not in pub
        assert set(pub.keys()) == {
            "request_id",
            "tool_name",
            "arguments",
            "declared_risk_level",
            "evidence_ids",
            "artifact_refs",
            "created_at",
        }

    @pytest.mark.asyncio
    async def test_reviewer_receives_only_request(self):
        seen: dict = {}

        async def approver(payload: dict):
            seen.update(payload)
            return True

        side = ReviewerSidecar(approver=approver)
        req = _req(declared_risk_level="L2")
        await side.submit(req)
        result = await side.review(req, action=ReviewDecisionAction.APPROVE)
        assert result.action is ReviewDecisionAction.APPROVE
        # approver 只拿到 public dict
        assert "trajectory" not in seen
        assert seen["tool_name"] == "send_notice"


class TestFourActions:
    @pytest.mark.asyncio
    async def test_approve(self):
        side = ReviewerSidecar()
        req = _req()
        r = await side.review(req, action=ReviewDecisionAction.APPROVE)
        assert r.action is ReviewDecisionAction.APPROVE
        assert side.get_result(req.request_id) is r
        assert req.request_id not in side.pending_ids

    @pytest.mark.asyncio
    async def test_reject(self):
        side = ReviewerSidecar()
        req = _req()
        r = await side.review(
            req, action=ReviewDecisionAction.REJECT, reason="too_broad"
        )
        assert r.action is ReviewDecisionAction.REJECT
        assert r.reason == "too_broad"

    @pytest.mark.asyncio
    async def test_edit_filters_keys(self):
        side = ReviewerSidecar(edit_allowed_keys=frozenset({"num_results"}))
        req = _req(arguments={"query": "a", "num_results": 10})
        r = await side.review(
            req,
            action=ReviewDecisionAction.EDIT,
            edited_arguments={"num_results": 3, "hacked": True},
        )
        assert r.action is ReviewDecisionAction.EDIT
        assert r.edited_arguments == {"num_results": 3}

    @pytest.mark.asyncio
    async def test_edit_requires_payload(self):
        side = ReviewerSidecar()
        req = _req()
        with pytest.raises(ValueError):
            await side.review(req, action=ReviewDecisionAction.EDIT)

    @pytest.mark.asyncio
    async def test_resume_action(self):
        side = ReviewerSidecar()
        req = _req()
        r = await side.review(req, action=ReviewDecisionAction.RESUME)
        assert r.action is ReviewDecisionAction.RESUME


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_high_risk_no_approver_rejects(self):
        side = ReviewerSidecar()
        req = _req(declared_risk_level="L2")
        r = await side.auto_review_high_risk(req)
        assert r.action is ReviewDecisionAction.REJECT
        assert "fail_closed" in r.reason

    @pytest.mark.asyncio
    async def test_force_arg_triggers_gate(self):
        side = ReviewerSidecar()
        req = _req(arguments={"force": True})
        r = await side.auto_review_high_risk(req)
        assert r.action is ReviewDecisionAction.REJECT

    @pytest.mark.asyncio
    async def test_approver_denial_becomes_reject(self):
        async def deny(_):
            return False

        side = ReviewerSidecar(approver=deny)
        req = _req(declared_risk_level="L2")
        r = await side.auto_review_high_risk(req)
        assert r.action is ReviewDecisionAction.REJECT
        assert r.reason == "approver_denied"

    @pytest.mark.asyncio
    async def test_edit_also_asks_approver_fail_closed(self):
        """EDIT 是「改参后放行」，注入 approver 后不得绕过审批。"""
        async def deny(_):
            return False

        side = ReviewerSidecar(approver=deny)
        req = _req(arguments={"query": "a", "n": 1})
        r = await side.review(
            req,
            action=ReviewDecisionAction.EDIT,
            edited_arguments={"n": 2},
            reason="tighten",
        )
        assert r.action is ReviewDecisionAction.REJECT
        assert r.reason == "approver_denied"
        assert r.edited_arguments is None

    @pytest.mark.asyncio
    async def test_edit_allowed_when_approver_accepts(self):
        async def allow(_):
            return True

        side = ReviewerSidecar(approver=allow, edit_allowed_keys=frozenset({"n"}))
        req = _req(arguments={"query": "a", "n": 10})
        r = await side.review(
            req, action=ReviewDecisionAction.EDIT, edited_arguments={"n": 3, "x": 1}
        )
        assert r.action is ReviewDecisionAction.EDIT
        assert r.edited_arguments == {"n": 3}

    @pytest.mark.asyncio
    async def test_low_risk_auto_approves(self):
        side = ReviewerSidecar()
        req = _req(declared_risk_level="L0")
        r = await side.auto_review_high_risk(req)
        assert r.action is ReviewDecisionAction.APPROVE


class TestRestartRecovery:
    def test_roundtrip_preserves_pending_and_results(self, tmp_path):
        side = ReviewerSidecar(reviewer_id="r-A", persist_dir=str(tmp_path))
        pending = _req(tool_name="slow_tool")
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            side.submit(pending)
        )
        decided = _req(tool_name="done_tool")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                side.review(decided, action=ReviewDecisionAction.APPROVE, reason="ok")
            )
        finally:
            loop.close()

        blob = side.to_persistence()
        restored = ReviewerSidecar.from_persistence(blob)
        assert pending.request_id in restored.pending_ids
        assert decided.request_id not in restored.pending_ids
        res = restored.get_result(decided.request_id)
        assert res is not None
        assert res.action is ReviewDecisionAction.APPROVE
        assert res.reason == "ok"
        assert restored.reviewer_id == "r-A"

    def test_persist_file_written(self, tmp_path):
        side = ReviewerSidecar(reviewer_id="r-file", persist_dir=str(tmp_path))
        loop = asyncio.new_event_loop()
        try:
            req = _req()
            loop.run_until_complete(side.submit(req))
        finally:
            loop.close()
        files = list(tmp_path.glob("sidecar_*.json"))
        assert len(files) == 1
        assert "pending" in files[0].read_text(encoding="utf-8")
