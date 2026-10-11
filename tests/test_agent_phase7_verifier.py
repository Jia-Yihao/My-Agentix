"""Phase 7 — 分层 Verifier（L1/L2/L3）+ Proposer–Reviewer 隔离验收。"""

from __future__ import annotations


import pytest

from agentix.policies.verifier import LayeredVerifier, StructuralVerifier
from agentix.domain.agent.types import (
    Observation,
    Plan,
    PlanStep,
    TaskSpec,
)

_EVIDENCE = [
    {
        "content": "《中华人民共和国劳动合同法》第三十九条规定，劳动者严重违反用人单位的规章制度的，用人单位可以解除劳动合同。",
        "source": "劳动合同法",
    },
]

_GROUNDED = (
    "根据《中华人民共和国劳动合同法》第三十九条，劳动者严重违反用人单位的规章制度的，"
    "用人单位可以解除劳动合同。"
)


def _obs(ok: bool = True, content: str = "x" * 20, **kw) -> Observation:
    return Observation(step_id=kw.pop("step_id", "s1"), content=content, ok=ok, **kw)


def _plan(*ids: str) -> Plan:
    return Plan(steps=tuple(PlanStep(id=i, action="a") for i in ids or ("s1",)))


class TestProposerReviewerIsolation:
    """Verifier 不得读 Proposer 自述（plan.rationale / meta.narrative）。"""

    @pytest.mark.asyncio
    async def test_rationale_not_considered(self):
        """把成功条件只写在 rationale 里 → L1 仍 fail（说明没读它）。"""
        task = TaskSpec(goal="g", task_id="t", success_criteria=("NEEDLE",))
        plan = Plan(
            steps=(
                PlanStep(
                    id="s1",
                    action="a",
                    rationale="我会输出 NEEDLE 保证通过",  # Proposer 自述
                ),
            )
        )
        v = StructuralVerifier()
        result = await v.verify(task, plan, [_obs(content="无关内容，没有条件")])
        assert result.passed is False
        assert "missing_criteria" in result.reason

    @pytest.mark.asyncio
    async def test_verification_result_has_no_proposer_field(self):
        v = StructuralVerifier()
        task = TaskSpec(goal="g")
        result = await v.verify(task, _plan(), [_obs(ok=True, content="hello world")])
        # VerificationResult 字段白名单：无 proposer/narrative/self_report
        for banned in ("proposer", "narrative", "self_report", "rationale"):
            assert banned not in result.meta
            assert not hasattr(result, banned)

    @pytest.mark.asyncio
    async def test_meta_narrative_ignored_by_layered(self):
        """Observation.meta 携带 narrative 不改变判定。"""
        task = TaskSpec(goal="g", task_id="t", success_criteria=("ok_token",))
        with_narr = _obs(
            content="ok_token 出现在产物里",
            meta={"narrative": "我是 Proposer，我说一切都好", "confidence": 0.99},
        )
        without = _obs(content="ok_token 出现在产物里", meta={})
        v = LayeredVerifier()
        r1 = await v.verify(task, _plan(), [with_narr])
        r2 = await v.verify(task, _plan(), [without])
        assert r1.passed == r2.passed
        # narrative 不进 meta 结果面
        assert "narrative" not in r1.meta


class TestLayeredL1L2L3:
    @pytest.mark.asyncio
    async def test_l1_fail_short_circuits(self):
        task = TaskSpec(goal="g", task_id="t", success_criteria=("X",))
        v = LayeredVerifier()
        result = await v.verify(task, _plan(), [_obs(ok=False, content="boom")])
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_l2_incomplete_empty_answer(self):
        task = TaskSpec(goal="g", task_id="t")  # 无 success_criteria → L1 过
        v = LayeredVerifier()
        result = await v.verify(
            task, _plan(), [_obs(ok=True, content="")]  # 空答案 → L2 INCOMPLETE
        )
        assert result.passed is False
        assert "completion_incomplete" in result.reason

    @pytest.mark.asyncio
    async def test_l2_degraded_still_passes(self):
        task = TaskSpec(goal="g", task_id="t")
        # 有答案但有工具失败 → DEGRADED → 通过
        v = LayeredVerifier(route="graphrag")
        result = await v.verify(
            task,
            _plan(),
            [
                _obs(ok=True, content="答案内容足够长超过六个字符"),
                _obs(ok=False, content="tool failed", step_id="s2"),
            ],
        )
        assert result.passed is True
        assert result.reason == "layered_ok_degraded"
        assert result.meta["l2"]["signal"] == "degraded"

    @pytest.mark.asyncio
    async def test_l3_grounding_pass_when_grounded(self):
        task = TaskSpec(goal="g", task_id="t")
        v = LayeredVerifier(require_grounding=True, route="kb-query")
        result = await v.verify(
            task,
            _plan(),
            [
                _obs(
                    ok=True,
                    content=_GROUNDED,
                    meta={"evidence": _EVIDENCE},
                )
            ],
        )
        assert result.passed is True
        assert result.meta.get("l3", {}).get("score") is not None
        assert result.meta["l3"]["score"] >= 0.35

    @pytest.mark.asyncio
    async def test_l3_grounding_fail_when_hallucinated(self):
        task = TaskSpec(goal="g", task_id="t")
        halluc = "劳动者入职后即可享受每年五十天的带薪年假且无需任何条件限制。"
        v = LayeredVerifier(require_grounding=True, route="kb-query")
        result = await v.verify(
            task,
            _plan(),
            [_obs(ok=True, content=halluc, meta={"evidence": _EVIDENCE})],
        )
        assert result.passed is False
        assert "grounding_below_threshold" in result.reason

    @pytest.mark.asyncio
    async def test_l3_skipped_without_evidence_fail_open(self):
        task = TaskSpec(goal="g", task_id="t")
        v = LayeredVerifier(require_grounding=True, route="kb-query")
        result = await v.verify(
            task, _plan(), [_obs(ok=True, content="没有证据时的答案内容六个字")]
        )
        # fail-open：无证据不判 grounding
        assert result.passed is True
        assert result.meta.get("l3", {}).get("skipped_reason") == "no_evidence"

    @pytest.mark.asyncio
    async def test_layered_meta_layers_present(self):
        task = TaskSpec(goal="g", task_id="t")
        v = LayeredVerifier()
        result = await v.verify(task, _plan(), [_obs(ok=True, content="正常答案内容")])
        assert "l1" in result.meta and "l2" in result.meta
