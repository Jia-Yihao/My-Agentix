"""多智能体通信压缩测试。

覆盖：① 子代理结果契约（派生 / 序列化往返 / 有界提取 / 容错反序列化）；
② 契约面单缝（flag-off 逐位等价、flag-on 黑板压缩、过限外置可取回）；
③ 共享证据面（evidence→obs:// 外置与解析缺省回退）；④ 证据充分早停
（_boom 哨兵省调用 + 判据两条件缺一不可）；⑤ 五处同步守卫。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import pytest

from agentix.config import settings
from agentix.orchestration.strategies.multi_agent import (
    AgentRole,
    Manager,
    ManagerOutcome,
    PeerResult,
    SubAgentResult,
    evidence_sufficient,
    peer_face,
    resolve_evidence_ref,
)
from agentix.orchestration.strategies.multi_agent.contract import (
    try_offload_face,
)
from agentix.orchestration.strategies.multi_agent.manager import (
    FailureMode,
    bound_result_face,
    derive_exit_contract,
)


@pytest.fixture(autouse=True)
def _clean_artifact_store():
    """外置写入全局单例——每例前重置，防跨例内容串扰。"""
    from agentix.application.services.artifact_store import reset_artifact_store

    reset_artifact_store()
    yield
    reset_artifact_store()


def _prop_result(
    *,
    output: str = "新事实：合同约定违约金 30 万，甲方已构成根本违约。",
    meta: Optional[Dict[str, Any]] = None,
    ok: bool = True,
) -> PeerResult:
    return PeerResult(
        agent_id="proposer",
        role=AgentRole.PROPOSER,
        output=output,
        ok=ok,
        meta=meta if meta is not None else {"decision": "accept"},
    )


# ===========================================================================
# 1. 契约派生与序列化往返
# ===========================================================================


class TestContractDerivation:
    def test_derivation_full_fields(self):
        evidence_body = "证据正文" * 700  # 2800 字 > 外置阈值 2000
        pr = _prop_result(
            output="第一要点。\n第二要点。\n第三要点。",
            meta={
                "decision": "accept",
                "evidence": [{"content": evidence_body, "source": "tool"}],
                "artifacts": {"contract_id": "C-2026-001"},
                "confidence": 0.9,
                "tokens": 123,
                "latency_ms": 45.6,
            },
        )
        c = SubAgentResult.from_peer_result(pr)
        assert c.status == "ok"
        assert c.claims == ["第一要点。", "第二要点。", "第三要点。"]
        assert len(c.evidence_refs) == 1
        assert c.evidence_refs[0].startswith("obs://")
        assert "contract_id=C-2026-001" in c.artifact_refs
        assert c.confidence == pytest.approx(0.9)
        assert c.cost["tokens"] == 123
        assert c.cost["latency_ms"] == pytest.approx(45.6)

    def test_derivation_error_status(self):
        c = SubAgentResult.from_peer_result(_prop_result(ok=False, meta={}))
        assert c.status == "error"

    def test_derivation_defaults_without_meta(self):
        c = SubAgentResult.from_peer_result(_prop_result(meta={}))
        assert c.evidence_refs == []
        assert c.confidence is None
        assert c.cost == {"tokens": 0, "latency_ms": 0.0}

    def test_derivation_obs_handles_passthrough(self):
        pr = _prop_result(output="已取回 obs://abc123-def456 的内容", meta={})
        c = SubAgentResult.from_peer_result(pr)
        assert "obs://abc123-def456" in c.artifact_refs

    def test_claims_bounded_and_deduped(self):
        lines = "\n".join(f"要点{i}：" + "长" * 300 for i in range(8))
        c = SubAgentResult.from_peer_result(_prop_result(output=lines, meta={}))
        assert len(c.claims) == 5
        assert all(len(claim) <= 200 for claim in c.claims)
        # 重复行去重：全部相同的行只保留一条
        c2 = SubAgentResult.from_peer_result(
            _prop_result(output="同句。\n同句。\n异句。", meta={})
        )
        assert c2.claims == ["同句。", "异句。"]

    def test_roundtrip_json(self):
        c = SubAgentResult.from_peer_result(
            _prop_result(
                meta={
                    "evidence": [{"content": "证" * 2500, "source": "t"}],
                    "confidence": 0.42,
                    "tokens": 7,
                }
            )
        )
        payload = json.dumps(c.to_dict(), ensure_ascii=False)
        back = SubAgentResult.from_dict(json.loads(payload))
        assert back.to_dict() == c.to_dict()

    def test_from_dict_tolerates_bad_input(self):
        assert SubAgentResult.from_dict(None).to_dict() == SubAgentResult().to_dict()
        bad = SubAgentResult.from_dict(
            {"status": 123, "confidence": "oops", "cost": {"tokens": "x"}}
        )
        assert bad.status == "123"
        assert bad.confidence is None
        assert bad.cost == {"tokens": 0, "latency_ms": 0.0}


# ===========================================================================
# 2. 契约面单缝（flag-off 逐位等价 / flag-on 压缩 / 过限外置）
# ===========================================================================


class TestPeerFaceSeam:
    def test_flag_off_is_baseline(self):
        assert settings.SUBAGENT_RESULT_CONTRACT_ENABLED is False
        pr = _prop_result(output="全文" * 1000)
        face = peer_face(pr, max_chars=4000)
        assert face == bound_result_face(pr.output, max_chars=4000)

    def test_flag_on_is_compact_json(self, monkeypatch):
        monkeypatch.setattr(settings, "SUBAGENT_RESULT_CONTRACT_ENABLED", True)
        raw = "全文" * 4000
        pr = _prop_result(output=raw)
        face = peer_face(pr, max_chars=4000)
        parsed = json.loads(face)
        assert set(parsed) == {
            "status",
            "claims",
            "evidence_refs",
            "artifact_refs",
            "confidence",
            "cost",
        }
        assert len(face) < len(raw) // 2

    def test_flag_on_bare_string_stays_baseline(self, monkeypatch):
        monkeypatch.setattr(settings, "SUBAGENT_RESULT_CONTRACT_ENABLED", True)
        assert peer_face("裸字符串结果", max_chars=4000) == bound_result_face(
            "裸字符串结果", max_chars=4000
        )

    def test_overlimit_face_offloads_and_resolves_back(self, monkeypatch):
        monkeypatch.setattr(settings, "SUBAGENT_RESULT_CONTRACT_ENABLED", True)
        evidence_body = "证" * 2500
        pr = _prop_result(
            output="要点甲。\n要点乙。",
            meta={"evidence": [{"content": evidence_body, "source": "t"}]},
        )
        face = peer_face(pr, max_chars=50)  # 契约 JSON 必超 50
        assert "已外置" in face and "obs://" in face
        handle = re.search(r"obs://[A-Za-z0-9_\-]+", face).group(0)
        body = resolve_evidence_ref(handle)
        assert body is not None
        assert json.loads(body)["status"] == "ok"  # 可取回：不是丢字符

    def test_offload_face_empty_payload_none(self):
        assert try_offload_face("") is None
        assert try_offload_face(None) is None


# ===========================================================================
# 3. Manager 集成：黑板压缩 + result_contracts
# ===========================================================================


async def _run_manager(
    *,
    prop_output: str,
    prop_meta: Dict[str, Any],
    reviewer: Any,
) -> Any:
    async def prop(**kw: Any) -> PeerResult:
        return _prop_result(output=prop_output, meta=prop_meta)

    mgr = Manager(proposer=prop, reviewer=reviewer)
    return mgr, await mgr.run("证明违约")


class TestManagerContractIntegration:
    @pytest.mark.asyncio
    async def test_flag_on_blackboard_compressed_and_contracts(self, monkeypatch):
        monkeypatch.setattr(settings, "SUBAGENT_RESULT_CONTRACT_ENABLED", True)

        async def rev(**kw: Any) -> PeerResult:
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="approve 结构齐全",
                meta={"decision": "approve"},
            )

        raw = "论述内容" * 2000  # 8000 字
        mgr, out = await _run_manager(
            prop_output=raw,
            prop_meta={
                "decision": "accept",
                "evidence": [{"content": "证据" * 1300, "source": "t"}],
            },
            reviewer=rev,
        )
        assert out.ok is True
        assert out.result_contracts, "flag-on 必须携带结果契约"
        assert out.result_contracts[0]["status"] == "ok"
        assert out.result_contracts[0]["evidence_refs"][0].startswith("obs://")
        # 黑板首条 = 紧凑契约面（或过限外置句柄），不再承载全文
        board_face = mgr.blackboard[0]
        assert len(board_face) < len(raw) // 2
        # claims 有界提取（≤5×200 字），绝不出现原文级别的重复堆叠
        assert board_face.count("论述内容") < 80
        # 决策仍基于完整 PeerResult（meta 读取路径零变化）
        assert out.reason == "converged"

    @pytest.mark.asyncio
    async def test_flag_off_blackboard_verbatim_and_no_contracts(self):
        assert settings.SUBAGENT_RESULT_CONTRACT_ENABLED is False

        async def rev(**kw: Any) -> PeerResult:
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="approve 结构齐全",
                meta={"decision": "approve"},
            )

        raw = "论述内容" * 1000
        mgr, out = await _run_manager(
            prop_output=raw, prop_meta={"decision": "accept"}, reviewer=rev
        )
        assert out.ok is True
        assert out.result_contracts == []
        assert mgr.blackboard[0] == bound_result_face(raw, max_chars=4000)
        assert "论述内容" * 10 in mgr.blackboard[0]  # 全文仍在（基线不压缩）

    def test_outcome_default_contracts_empty(self):
        out = ManagerOutcome(
            ok=True,
            final_output="x",
            failure_mode=FailureMode.NONE,
            rounds=1,
            reason="converged",
        )
        assert out.result_contracts == []
        assert out.exit_status == "completed"
        assert out.truncated is False


# ===========================================================================
# 4. 证据充分早停（_boom 哨兵）
# ===========================================================================


class TestEarlyExit:
    @pytest.mark.asyncio
    async def test_flag_on_skips_reviewer_boom(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTIAGENT_EARLY_EXIT_ENABLED", True)
        calls: List[str] = []

        def boom(**kw: Any) -> PeerResult:
            calls.append("reviewer")
            raise RuntimeError("reviewer must not be called (early exit)")

        evidence_body = "证据" * 1300  # meta["evidence"] 非空即表征带证据
        mgr, out = await _run_manager(
            prop_output="新证据表明违约事实成立，结论唯一。",
            prop_meta={
                "decision": "accept",
                "evidence": [{"content": evidence_body, "source": "t"}],
            },
            reviewer=boom,
        )
        assert calls == [], "证据充分时 reviewer 不得被调用（早停省调用）"
        assert out.ok is True
        assert out.reason == "evidence_sufficient"
        assert out.exit_status == "completed"
        assert out.exit_reason == "completed"
        assert out.truncated is False
        assert out.rounds == 1

    @pytest.mark.asyncio
    async def test_flag_off_still_calls_reviewer(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTIAGENT_EARLY_EXIT_ENABLED", False)
        calls: List[str] = []

        async def rev(**kw: Any) -> PeerResult:
            calls.append("reviewer")
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="approve 结构齐全",
                meta={"decision": "approve"},
            )

        _, out = await _run_manager(
            prop_output="新证据表明违约事实成立，结论唯一。",
            prop_meta={
                "decision": "accept",
                "evidence": [{"content": "证据" * 1300, "source": "t"}],
            },
            reviewer=rev,
        )
        assert calls == ["reviewer"]  # 基线：每 peer 照常调用
        assert out.ok is True
        assert out.reason == "converged"

    @pytest.mark.asyncio
    async def test_flag_on_without_evidence_calls_reviewer(self, monkeypatch):
        monkeypatch.setattr(settings, "MULTIAGENT_EARLY_EXIT_ENABLED", True)
        calls: List[str] = []

        async def rev(**kw: Any) -> PeerResult:
            calls.append("reviewer")
            return PeerResult(
                agent_id="reviewer",
                role=AgentRole.REVIEWER,
                output="approve 结构齐全",
                meta={"decision": "approve"},
            )

        _, out = await _run_manager(
            prop_output="无证据的新表述，仅文字推进。",
            prop_meta={"decision": "accept"},  # 无 meta["evidence"]
            reviewer=rev,
        )
        assert calls == ["reviewer"]  # 无证据 ⇒ 不早停
        assert out.reason == "converged"


class TestEvidenceSufficientJudgement:
    def test_evidence_plus_increment(self):
        pr = _prop_result(output="全新事实甲。", meta={"evidence": [{"content": "x"}]})
        assert evidence_sufficient(pr, "") is True

    def test_no_increment_echo_is_not_sufficient(self):
        text = "既有事实重复一遍。"
        pr = _prop_result(output=text, meta={"evidence": [{"content": "x"}]})
        assert evidence_sufficient(pr, f"前文\n{text}") is False  # 纯复读不早停

    def test_increment_without_evidence_not_sufficient(self):
        pr = _prop_result(output="全新事实乙。", meta={})
        assert evidence_sufficient(pr, "") is False

    def test_obs_handle_in_output_counts_as_evidence(self):
        pr = _prop_result(output="参见 obs://deadbeef-0001 的取证结果", meta={})
        assert evidence_sufficient(pr, "") is True

    @pytest.mark.parametrize(
        "candidate",
        [
            PeerResult(
                agent_id="p",
                role=AgentRole.PROPOSER,
                output="x",
                ok=False,
                meta={"evidence": [{"content": "x"}]},
            ),
            "裸字符串",
            None,
        ],
    )
    def test_non_ok_or_non_peerresult_never_sufficient(self, candidate):
        assert evidence_sufficient(candidate, "") is False


class TestResolveFallback:
    def test_unknown_handle_returns_none(self):
        assert resolve_evidence_ref("obs://does-not-exist-42") is None

    def test_non_handle_returns_none(self):
        assert resolve_evidence_ref("普通文本") is None
        assert resolve_evidence_ref("obs://") is None
        assert resolve_evidence_ref(None) is None
        assert resolve_evidence_ref("") is None

    def test_store_exception_returns_none(self):
        class BoomStore:
            def get(self, artifact_id: str) -> Optional[str]:
                raise RuntimeError("store down")

        assert resolve_evidence_ref("obs://x1", store=BoomStore()) is None

    def test_injected_store_hit(self):
        class FakeStore:
            def get(self, artifact_id: str) -> Optional[str]:
                return f"content-of-{artifact_id}"

        assert resolve_evidence_ref("obs://art-9", store=FakeStore()) == (
            "content-of-art-9"
        )


class TestExitContract:
    def test_evidence_sufficient_reason_maps_to_completed(self):
        assert derive_exit_contract(True, "evidence_sufficient") == (
            "completed",
            "completed",
        )
        out = ManagerOutcome(
            ok=True,
            final_output="x",
            failure_mode=FailureMode.NONE,
            rounds=1,
            reason="evidence_sufficient",
        )
        assert out.exit_reason == "completed"
        assert out.truncated is False  # 契约等式：非 max_iterations ⇒ 不截断


# ===========================================================================
# 5. 五处同步守卫（p41 主检 配置管理.md + 手写清单，这里补其余）
# ===========================================================================


class TestW5FlagSync:
    W5_KEYS = (
        "SUBAGENT_RESULT_CONTRACT_ENABLED",
        "MULTIAGENT_EARLY_EXIT_ENABLED",
    )

    def test_defaults_are_off(self):
        assert settings.SUBAGENT_RESULT_CONTRACT_ENABLED is False
        assert settings.MULTIAGENT_EARLY_EXIT_ENABLED is False

    def test_selfcheck_allows_omission(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        for key in self.W5_KEYS:
            assert key in DEFAULT_IGNORED_MISSING, f"{key} 不在 selfcheck 白名单"

    def test_env_example_documents_all_keys(self):
        from pathlib import Path

        env_text = Path(".env.example").read_text(encoding="utf-8")
        for key in self.W5_KEYS:
            assert key in env_text, f"{key} 不在 .env.example"

    def test_configuration_md_documents_all_keys(self):
        from pathlib import Path

        doc = Path("docs/architecture/配置管理.md").read_text(encoding="utf-8")
        for key in self.W5_KEYS:
            assert key in doc, f"{key} 不在 配置管理.md"
