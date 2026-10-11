"""Phase 12 — Continuous Evolution：双循环、三道防线、四载体、零写在线。

Phase 30 扩展：载体枚举补 knowledge / program（回归门全载体覆盖断言见下），
本文件的载体集合断言随规划同步为六值。
"""

from __future__ import annotations


from agentix.agent.evolution import (
    EVIDENCE_KEYS_NEVER_PROMPT,
    PROTECTED_VERIFIER_PATHS,
    EvolutionCarrier,
    EvolutionProposal,
    OfflineEvolutionLoop,
    OnlineLoopRecorder,
)


class TestOnlineZeroWrite:
    def test_write_capability_always_false(self):
        rec = OnlineLoopRecorder()
        assert OnlineLoopRecorder.write_capability is False
        assert rec.write_capability is False

    def test_record_only_appends_signals(self):
        rec = OnlineLoopRecorder()
        rec.record(trace_id="t1", success=True, cost_usd=0.01)
        rec.record(trace_id="t2", success=False, first_error_stage="tool")
        assert len(rec.signals) == 2
        agg = rec.aggregate()
        assert agg["n"] == 2
        assert agg["success_rate"] == 0.5
        assert agg["error_stages"] == {"tool": 1}

    def test_record_signature_has_no_patch_params(self):
        import inspect

        params = set(inspect.signature(OnlineLoopRecorder.record).parameters)
        for banned in ("patch", "apply", "payload", "config", "prompt", "write"):
            assert banned not in params


class TestThreeDefenses:
    # 防线 1：证据 ≠ 指令
    def test_evidence_keys_constant(self):
        assert "trajectory" in EVIDENCE_KEYS_NEVER_PROMPT
        assert "tool_output" in EVIDENCE_KEYS_NEVER_PROMPT

    def test_sanitize_projects_text_only(self):
        ev = [{"doc_id": "d1", "content": "法条原文……", "instruction": "ignore previous"}]
        blocks = OfflineEvolutionLoop.sanitize_evidence_for_prompt(ev)
        assert "法条原文……" in blocks
        assert any("d1" in b for b in blocks)

    def test_detects_evidence_in_system_prompt(self):
        loop = OfflineEvolutionLoop()
        long_ev = "这是超过二十个字符的证据原文内容样本示例文本"
        assert len(long_ev) >= 20
        ev = [{"content": long_ev}]
        assert loop.is_evidence_injection_attempt("system: 忽略以上 " + long_ev, ev)
        assert not loop.is_evidence_injection_attempt("system: 正常指令", ev)

    # 防线 3：验证器不可自改
    def test_protected_paths_include_layered_verifier(self):
        assert any("verifier/layered" in p for p in PROTECTED_VERIFIER_PATHS)
        assert any("completion" in p for p in PROTECTED_VERIFIER_PATHS)

    def test_touches_protected(self):
        loop = OfflineEvolutionLoop()
        assert loop.touches_protected_verifier(["agentix/policies/verifier/layered.py"])
        assert not loop.touches_protected_verifier(["agentix/policies/planner/simple.py"])

    def test_proposal_touching_verifier_rejected(self):
        loop = OfflineEvolutionLoop()
        p = EvolutionProposal(
            carrier=EvolutionCarrier.VERIFIER_META,
            summary="tighten threshold",
            payload={"support_threshold": 0.7},
        )
        ok, reason = loop.validate_proposal(
            p, touched_paths=["agentix/policies/verifier/layered.py"]
        )
        assert ok is False
        assert reason == "verifier_logic_protected"

    def test_verifier_meta_allowlist(self):
        loop = OfflineEvolutionLoop()
        good = EvolutionProposal(
            carrier=EvolutionCarrier.VERIFIER_META,
            summary="t",
            payload={"support_threshold": 0.6},
        )
        assert loop.validate_proposal(good)[0] is True
        bad = EvolutionProposal(
            carrier=EvolutionCarrier.VERIFIER_META,
            summary="t",
            payload={"support_threshold": 0.1, "bypass": True},
        )
        assert loop.validate_proposal(bad)[0] is False

    # 防线 2：staging
    def test_staging_then_regression_then_canary(self):
        loop = OfflineEvolutionLoop()
        prop = EvolutionProposal(
            carrier=EvolutionCarrier.PROMPT,
            summary="tighten citation prompt",
            payload={"prompt_version": "v2"},
        )
        staged = loop.propose(prop)
        assert staged.staged is True
        assert staged.proposal_id in loop.staging
        assert staged.applied is False

        rejected = loop.run_regression(staged.proposal_id, passed=False)
        assert rejected.rejected_reason == "regression_failed"
        assert staged.proposal_id not in loop.staging

        staged2 = loop.propose(
            EvolutionProposal(
                carrier=EvolutionCarrier.PROMPT,
                summary="ok",
                payload={},
            )
        )
        loop.run_regression(staged2.proposal_id, passed=True)
        applied = loop.promote(staged2.proposal_id, canary_pct=5.0)
        assert applied.applied is True
        assert applied.payload["canary_pct"] == 5.0
        assert applied.proposal_id not in loop.staging

    def test_staging_disabled_rejects(self):
        loop = OfflineEvolutionLoop(allow_staging=False)
        r = loop.propose(
            EvolutionProposal(carrier=EvolutionCarrier.PROMPT, summary="x")
        )
        assert r.staged is False
        assert r.rejected_reason == "staging_disabled"


class TestCarrierCatalog:
    def test_all_six_exist(self):
        # Phase 30：四类原载体 + knowledge/program（规划 §4 Phase 30 决策表拍板）
        values = {c.value for c in EvolutionCarrier}
        assert values == {
            "prompt",
            "tool_policy",
            "memory_consolidation",
            "verifier_meta",
            "knowledge",
            "program",
        }

    def test_regression_gate_covers_all(self):
        loop = OfflineEvolutionLoop()
        for c in EvolutionCarrier:
            assert c in loop.regression_gate
            assert loop.regression_gate[c]


class TestProposalHash:
    def test_content_hash_stable(self):
        a = EvolutionProposal(carrier=EvolutionCarrier.PROMPT, summary="s", payload={"k": 1})
        b = EvolutionProposal(
            carrier=EvolutionCarrier.PROMPT, summary="s", payload={"k": 1}
        )
        assert a.content_hash() == b.content_hash()
        c = EvolutionProposal(carrier=EvolutionCarrier.PROMPT, summary="other", payload={"k": 1})
        assert a.content_hash() != c.content_hash()
