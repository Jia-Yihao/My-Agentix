"""Phase 30 — 进化闭环：载体扩展（knowledge/program）+ 提案落盘 + 睡眠学习。

验收对应：

* 新载体两值 + 门元组；PROGRAM 触保护路径 fail-closed；
* 落盘三状态全记、同 content_hash 重放 append 幂等、无 sink 基线等价；
* `evolution_sleep`：flag 关 skipped / 门槛未达 no-op / 达标只产 staged、
  不晋升、端点预检 fail-closed；
* `proposals.jsonl` 不落 `evals/baselines/`（路径测试）；
* `evolution_run --proposal-log` 接线（默认关 = stdout 与基线逐键一致）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agentix.agent.evolution import (
    KNOWLEDGE_PAYLOAD_ALLOWLIST,
    PROGRAM_PAYLOAD_ALLOWLIST,
    PROGRAM_RULE_KEYS,
    EvolutionCarrier,
    EvolutionProposal,
    OfflineEvolutionLoop,
    get_online_loop_recorder,
    jsonl_proposal_sink,
    reset_online_loop_recorder,
)
from agentix.config import settings as app_settings
from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING
from evals.evolution_run import main as evolution_main


# ---------------------------------------------------------------------------
# 30.1 新载体 + 门元组
# ---------------------------------------------------------------------------


class TestNewCarriersAndGates:
    def test_knowledge_and_program_exist(self):
        assert EvolutionCarrier.KNOWLEDGE.value == "knowledge"
        assert EvolutionCarrier.PROGRAM.value == "program"

    def test_gate_tuples_are_exact(self):
        loop = OfflineEvolutionLoop()
        assert loop.regression_gate[EvolutionCarrier.KNOWLEDGE] == (
            "citation",
            "scope_isolation",
        )
        assert loop.regression_gate[EvolutionCarrier.PROGRAM] == (
            "param_fidelity",
            "verifier_logic_unchanged",
        )

    def test_gates_reuse_only_preexisting_gate_keys(self):
        """Gotcha 4：门元组只从既有 gate 键派生，不发明新门。"""
        loop = OfflineEvolutionLoop()
        original = (
            loop.regression_gate[EvolutionCarrier.PROMPT]
            + loop.regression_gate[EvolutionCarrier.TOOL_POLICY]
            + loop.regression_gate[EvolutionCarrier.MEMORY_CONSOLIDATION]
            + loop.regression_gate[EvolutionCarrier.VERIFIER_META]
        )
        original_keys = set(original)
        assert set(loop.regression_gate[EvolutionCarrier.KNOWLEDGE]) <= original_keys
        assert set(loop.regression_gate[EvolutionCarrier.PROGRAM]) <= original_keys

    def test_program_touching_protected_verifier_fail_closed(self):
        loop = OfflineEvolutionLoop()
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="tweak verifier orchestration",
            payload={"rules": [{"id": "r1", "when": {"x": 1}, "then": "y"}]},
        )
        ok, reason = loop.validate_proposal(
            p, touched_paths=["agentix/policies/verifier/layered.py"]
        )
        assert ok is False
        assert reason == "verifier_logic_protected"


class TestProgramPayloadAllowlist:
    def _loop(self) -> OfflineEvolutionLoop:
        return OfflineEvolutionLoop()

    def test_declarative_rules_accepted(self):
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="prefer rerank on low score",
            payload={
                "rules": [
                    {"id": "r1", "when": {"tool": "search", "top_k": 5}, "then": "rerank"},
                    {"id": "r2", "when": {"route": "kb-query"}, "then": "cite_required"},
                ],
                "notes": "declarative only",
            },
        )
        assert self._loop().validate_proposal(p) == (True, "ok")

    def test_unknown_top_level_key_rejected(self):
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="s",
            payload={"code": "exec(open('x.py'))"},
        )
        ok, reason = self._loop().validate_proposal(p)
        assert ok is False
        assert reason.startswith("program_payload_keys_not_allowed")

    def test_unknown_rule_key_rejected(self):
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="s",
            payload={"rules": [{"id": "r1", "command": "rm -rf /"}]},
        )
        ok, reason = self._loop().validate_proposal(p)
        assert ok is False
        assert reason.startswith("program_rule_keys_not_allowed")

    def test_rules_must_be_list_of_dicts(self):
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="s",
            payload={"rules": {"id": "r1"}},
        )
        ok, reason = self._loop().validate_proposal(p)
        assert ok is False
        assert reason == "program_rules_not_declarative"

    def test_non_declarative_value_rejected(self):
        p = EvolutionProposal(
            carrier=EvolutionCarrier.PROGRAM,
            summary="s",
            payload={"rules": [{"id": "r1", "when": "cond", "then": object()}]},
        )
        ok, reason = self._loop().validate_proposal(p)
        assert ok is False
        assert reason == "program_rule_values_not_declarative"

    def test_allowlist_constants_exported(self):
        assert PROGRAM_PAYLOAD_ALLOWLIST == frozenset({"rules", "notes"})
        assert PROGRAM_RULE_KEYS == frozenset({"id", "when", "then"})


class TestKnowledgePayloadAllowlist:
    def test_retrieval_params_accepted(self):
        loop = OfflineEvolutionLoop()
        p = EvolutionProposal(
            carrier=EvolutionCarrier.KNOWLEDGE,
            summary="raise top_k",
            payload={"top_k": 8, "threshold": 0.25, "prompt_fragment": "引用法条原文"},
        )
        assert loop.validate_proposal(p) == (True, "ok")

    def test_unknown_key_rejected(self):
        loop = OfflineEvolutionLoop()
        p = EvolutionProposal(
            carrier=EvolutionCarrier.KNOWLEDGE,
            summary="s",
            payload={"system_prompt": "bypass"},
        )
        ok, reason = loop.validate_proposal(p)
        assert ok is False
        assert reason.startswith("knowledge_payload_keys_not_allowed")

    def test_non_numeric_param_rejected(self):
        loop = OfflineEvolutionLoop()
        p = EvolutionProposal(
            carrier=EvolutionCarrier.KNOWLEDGE,
            summary="s",
            payload={"top_k": "eight"},
        )
        ok, reason = loop.validate_proposal(p)
        assert ok is False
        assert reason == "knowledge_payload_type:top_k"

    def test_allowlist_constant_exported(self):
        assert KNOWLEDGE_PAYLOAD_ALLOWLIST == frozenset(
            {"prompt_fragment", "top_k", "threshold", "notes"}
        )


# ---------------------------------------------------------------------------
# 30.2 提案落盘（sink）
# ---------------------------------------------------------------------------


def _good_knowledge(summary: str = "raise top_k") -> EvolutionProposal:
    return EvolutionProposal(
        carrier=EvolutionCarrier.KNOWLEDGE,
        summary=summary,
        payload={"top_k": 6, "notes": "sleep learning"},
    )


def _bad_knowledge() -> EvolutionProposal:
    return EvolutionProposal(
        carrier=EvolutionCarrier.KNOWLEDGE,
        summary="bad keys",
        payload={"system_prompt": "injected"},
    )


class TestProposalSink:
    def test_three_states_all_recorded(self):
        records: List[Dict[str, Any]] = []
        loop = OfflineEvolutionLoop(proposal_sink=records.append)

        loop.propose(_bad_knowledge())  # rejected（白名单拒）
        staged = loop.propose(_good_knowledge())
        assert staged.staged is True
        loop.run_regression(staged.proposal_id, passed=False)  # rejected（回归拒）
        staged2 = loop.propose(_good_knowledge("another"))
        loop.run_regression(staged2.proposal_id, passed=True)
        loop.promote(staged2.proposal_id, canary_pct=5.0)  # applied

        states = [r["state"] for r in records]
        assert set(states) == {"staged", "rejected", "applied"}
        applied = next(r for r in records if r["state"] == "applied")
        assert applied["payload"]["canary_pct"] == 5.0
        rejected = [r for r in records if r["state"] == "rejected"]
        assert {r["rejected_reason"] for r in rejected} == {
            "knowledge_payload_keys_not_allowed:['system_prompt']",
            "regression_failed",
        }
        for r in records:
            assert r["content_hash"] and r["proposal_id"] and r["carrier"] == "knowledge"
            assert "ts" in r

    def test_replay_same_content_hash_is_append_idempotent(self):
        records: List[Dict[str, Any]] = []
        loop = OfflineEvolutionLoop(proposal_sink=records.append)
        # 同 content（carrier/summary/payload 相同）重放两次：staged/rejected 各只记一次
        s1 = loop.propose(_good_knowledge())
        loop.run_regression(s1.proposal_id, passed=False)
        s2 = loop.propose(_good_knowledge())
        loop.run_regression(s2.proposal_id, passed=False)
        assert len(records) == 2
        assert [r["state"] for r in records] == ["staged", "rejected"]

    def test_no_sink_behavior_identical_to_sink_loop(self):
        """无 sink = 与基线字节等价（内部状态序列完全一致，零落盘）。"""

        def scenario(loop: OfflineEvolutionLoop) -> List[tuple]:
            loop.propose(_bad_knowledge())
            a = loop.propose(_good_knowledge("one"))
            loop.run_regression(a.proposal_id, passed=False)
            b = loop.propose(_good_knowledge("two"))
            loop.run_regression(b.proposal_id, passed=True)
            loop.promote(b.proposal_id, canary_pct=10.0)
            return [
                (p.carrier, p.summary, p.staged, p.applied, p.rejected_reason)
                for p in loop.proposals
            ]

        records: List[Dict[str, Any]] = []
        baseline = scenario(OfflineEvolutionLoop())
        with_sink = scenario(OfflineEvolutionLoop(proposal_sink=records.append))
        assert baseline == with_sink
        # rejected(白名单) + staged(one) + rejected(回归) + staged(two) + applied = 5
        assert len(records) == 5
        assert [r["state"] for r in records] == [
            "rejected",
            "staged",
            "rejected",
            "staged",
            "applied",
        ]


class TestJsonlProposalSinkPathGuard:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "evals/baselines/proposals.jsonl",
            "evals\\baselines\\proposals.jsonl",
            "C:/repo/evals/baselines/p.jsonl",
            "C:\\repo\\evals\\baselines\\p.jsonl",
        ],
    )
    def test_rejects_evals_baselines(self, bad_path: str):
        with pytest.raises(ValueError, match="evals/baselines"):
            jsonl_proposal_sink(bad_path)

    def test_rejects_empty_path(self):
        with pytest.raises(ValueError):
            jsonl_proposal_sink("")

    def test_default_settings_path_is_constructible(self):
        # 默认档案路径在 data/evolution/ 下：只构造不写（构造零副作用）
        sink = jsonl_proposal_sink(app_settings.EVOLUTION_PROPOSAL_LOG)
        assert callable(sink)

    def test_writes_append_only_jsonl(self, tmp_path: Path):
        target = tmp_path / "nested" / "proposals.jsonl"
        sink = jsonl_proposal_sink(str(target))
        sink({"state": "staged", "content_hash": "a"})
        sink({"state": "rejected", "content_hash": "a"})
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert [json.loads(x)["state"] for x in lines] == ["staged", "rejected"]


# ---------------------------------------------------------------------------
# 30.3 五处同步（settings / selfcheck 存在性）
# ---------------------------------------------------------------------------


class TestSettingsSync:
    def test_settings_defaults(self):
        assert app_settings.EVOLUTION_SLEEP_ENABLED is False
        assert app_settings.EVOLUTION_PROPOSAL_LOG == "data/evolution/proposals.jsonl"

    def test_keys_in_selfcheck_legal_omission(self):
        assert "EVOLUTION_SLEEP_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "EVOLUTION_PROPOSAL_LOG" in DEFAULT_IGNORED_MISSING


# ---------------------------------------------------------------------------
# 30.4 evolution_sleep handler
# ---------------------------------------------------------------------------


class _StubLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: List[Any] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)

        class _Resp:
            content: str = ""

        resp = _Resp()
        resp.content = self._content
        return resp


def _seed_signals(n: int = 20, failures: int = 5) -> None:
    rec = get_online_loop_recorder()
    for i in range(n):
        rec.record(trace_id=f"t{i}", success=(i >= failures), first_error_stage="tool" if i < failures else "")


@pytest.fixture(autouse=True)
def _isolate_sleep_state():
    reset_online_loop_recorder()
    from agentix.application.services import task_handlers

    task_handlers._EVOLUTION_SLEEP_CLAIM.clear()
    yield
    reset_online_loop_recorder()
    task_handlers._EVOLUTION_SLEEP_CLAIM.clear()


def _enable_sleep(monkeypatch, *, log_path: str = "") -> None:
    monkeypatch.setattr(app_settings, "EVOLUTION_SLEEP_ENABLED", True)
    monkeypatch.setattr(app_settings, "EVOLUTION_PROPOSAL_LOG", log_path)


def _set_endpoint(monkeypatch, key: str = "test-key", base: str = "http://127.0.0.1:1") -> None:
    # OPENAI_API_KEY / OPENAI_API_BASE 是只读 property（派生自 LLM_* 字段），
    # monkeypatch 需落在真实存储字段上。
    monkeypatch.setattr(app_settings, "LLM_API_KEY", key)
    monkeypatch.setattr(app_settings, "LLM_BASE_URL", base)


def _stub_llm(monkeypatch, content: str) -> _StubLLM:
    stub = _StubLLM(content)
    monkeypatch.setattr(
        "agentix.application.agents.llm_factory.get_llm_client",
        lambda **kwargs: stub,
    )
    return stub


def _forbid_promotion(monkeypatch) -> None:
    """睡眠学习绝不允许晋升/回归——调用即测试失败。"""

    def _boom(*args, **kwargs):
        raise AssertionError("睡眠学习不得调用 promote/run_regression")

    monkeypatch.setattr(OfflineEvolutionLoop, "promote", _boom)
    monkeypatch.setattr(OfflineEvolutionLoop, "run_regression", _boom)


class TestEvolutionSleepHandler:
    @pytest.mark.asyncio
    async def test_flag_off_skipped(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        monkeypatch.setattr(app_settings, "EVOLUTION_SLEEP_ENABLED", False)
        result = await handle_evolution_sleep({})
        assert result == {"status": "skipped", "reason": "disabled"}

    @pytest.mark.asyncio
    async def test_threshold_not_met_no_op(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch)
        # 无信号 → n=0 < min_signals
        result = await handle_evolution_sleep({})
        assert result["status"] == "no_op"
        assert result["reason"] == "threshold_not_met"
        assert result["n"] == 0

    @pytest.mark.asyncio
    async def test_threshold_error_rate_gate(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch)
        _seed_signals(n=20, failures=0)  # 错误率 0 < 0.1
        result = await handle_evolution_sleep({})
        assert result["status"] == "no_op"
        assert result["error_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_threshold_met_no_endpoint_skipped(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch)
        _set_endpoint(monkeypatch, key="", base="")
        _seed_signals()
        result = await handle_evolution_sleep({})
        assert result == {"status": "skipped", "reason": "no_endpoint"}

    @pytest.mark.asyncio
    async def test_dry_run_plans_without_claim(self, monkeypatch):
        from agentix.application.services import task_handlers
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch)
        _seed_signals()
        result = await handle_evolution_sleep({"dry_run": True})
        assert result["status"] == "planned"
        assert result["threshold_met"] is True
        assert task_handlers._EVOLUTION_SLEEP_CLAIM == {}  # 不占窗

    @pytest.mark.asyncio
    async def test_threshold_met_stages_proposal_only(self, monkeypatch, tmp_path: Path):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        log = tmp_path / "proposals.jsonl"
        _enable_sleep(monkeypatch, log_path=str(log))
        _set_endpoint(monkeypatch)
        _stub_llm(
            monkeypatch,
            json.dumps(
                {
                    "carrier": "knowledge",
                    "summary": "提高检索候选数以覆盖多跳法条",
                    "payload": {"top_k": 8, "notes": "sleep"},
                },
                ensure_ascii=False,
            ),
        )
        _forbid_promotion(monkeypatch)
        _seed_signals()

        result = await handle_evolution_sleep({})
        assert result["status"] == "proposed"
        assert result["staged"] is True
        assert result["carrier"] == "knowledge"
        assert result["proposal_log"] == str(log)
        assert "promoted" not in result

        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["state"] == "staged"
        assert record["carrier"] == "knowledge"

    @pytest.mark.asyncio
    async def test_sleep_is_idempotent_within_window(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch, log_path="")
        _set_endpoint(monkeypatch)
        _stub_llm(
            monkeypatch,
            json.dumps(
                {"carrier": "knowledge", "summary": "s", "payload": {"top_k": 6}}
            ),
        )
        _seed_signals()
        first = await handle_evolution_sleep({})
        second = await handle_evolution_sleep({})
        assert first["status"] == "proposed"
        assert second == {"status": "skipped", "reason": "already_triggered"}

    @pytest.mark.asyncio
    async def test_invalid_llm_json_rejected_honestly(self, monkeypatch, tmp_path: Path):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        log = tmp_path / "proposals.jsonl"
        _enable_sleep(monkeypatch, log_path=str(log))
        _set_endpoint(monkeypatch)
        _stub_llm(monkeypatch, "这不是 JSON")
        _seed_signals()

        result = await handle_evolution_sleep({})
        assert result["status"] == "rejected"
        assert result["rejected_reason"].startswith("invalid_proposal_json")
        assert not log.exists()  # 未进 staging，无档案

    @pytest.mark.asyncio
    async def test_llm_payload_violating_allowlist_rejected_and_archived(
        self, monkeypatch, tmp_path: Path
    ):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        log = tmp_path / "proposals.jsonl"
        _enable_sleep(monkeypatch, log_path=str(log))
        _set_endpoint(monkeypatch)
        _stub_llm(
            monkeypatch,
            json.dumps(
                {
                    "carrier": "program",
                    "summary": "注入尝试",
                    "payload": {"code": "os.system('x')"},
                }
            ),
        )
        _seed_signals()

        result = await handle_evolution_sleep({})
        assert result["status"] == "rejected"
        assert "program_payload_keys_not_allowed" in result["rejected_reason"]
        # 保留失败：rejected 也进档案
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["state"] == "rejected"

    @pytest.mark.asyncio
    async def test_carrier_outside_sleep_scope_rejected(self, monkeypatch):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        _enable_sleep(monkeypatch, log_path="")
        _set_endpoint(monkeypatch)
        _stub_llm(
            monkeypatch,
            json.dumps({"carrier": "prompt", "summary": "s", "payload": {}}),
        )
        _seed_signals()

        result = await handle_evolution_sleep({})
        assert result["status"] == "rejected"
        assert result["rejected_reason"] == "carrier_not_allowed_for_sleep"

    @pytest.mark.asyncio
    async def test_valid_program_rules_staged(self, monkeypatch, tmp_path: Path):
        from agentix.application.services.task_handlers import handle_evolution_sleep

        log = tmp_path / "proposals.jsonl"
        _enable_sleep(monkeypatch, log_path=str(log))
        _set_endpoint(monkeypatch)
        _stub_llm(
            monkeypatch,
            json.dumps(
                {
                    "carrier": "program",
                    "summary": "kb 查询强制引用校验",
                    "payload": {
                        "rules": [
                            {"id": "cite", "when": {"route": "kb-query"}, "then": "require_citation"}
                        ]
                    },
                }
            ),
        )
        _seed_signals()

        result = await handle_evolution_sleep({})
        assert result["status"] == "proposed"
        assert result["carrier"] == "program"
        lines = log.read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0])["state"] == "staged"

    def test_prompt_constant_externalized(self):
        from agentix.application.services.prompts import EVOLUTION_SLEEP_PROPOSAL_PROMPT

        assert EVOLUTION_SLEEP_PROPOSAL_PROMPT.strip()
        assert "{n}" in EVOLUTION_SLEEP_PROPOSAL_PROMPT
        assert "{error_rate}" in EVOLUTION_SLEEP_PROPOSAL_PROMPT
        # 静态指令在前、动态信号占位在后（静态前缀纪律）
        assert (
            EVOLUTION_SLEEP_PROPOSAL_PROMPT.index("聚合信号")
            > EVOLUTION_SLEEP_PROPOSAL_PROMPT.index("carrier 只能取")
        )


# ---------------------------------------------------------------------------
# 30.5 evolution_run --proposal-log
# ---------------------------------------------------------------------------


def _proposal_file(tmp_path: Path, **overrides) -> Path:
    data = {"carrier": "tool_policy", "summary": "tune", "payload": {"text": "x"}, "touched_paths": []}
    data.update(overrides)
    target = tmp_path / "proposal.json"
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


class TestEvolutionRunProposalLog:
    def test_default_off_stdout_keys_unchanged(self, tmp_path: Path, capsys):
        code = evolution_main(["--proposal", str(_proposal_file(tmp_path))])
        out = json.loads(capsys.readouterr().out)
        assert code == 1  # tool_policy 门 not_wired → 拒绝（既有语义）
        assert "proposal_log" not in out  # 默认不落盘 = 与基线逐键一致
        assert out["rejected_reason"] == "regression_failed"

    def test_with_log_records_staged_and_rejected(self, tmp_path: Path, capsys):
        log = tmp_path / "archive" / "proposals.jsonl"
        code = evolution_main(
            ["--proposal", str(_proposal_file(tmp_path)), "--proposal-log", str(log)]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 1
        assert out["proposal_log"] == str(log)
        lines = log.read_text(encoding="utf-8").splitlines()
        states = [json.loads(x)["state"] for x in lines]
        assert states == ["staged", "rejected"]

    def test_evals_baselines_path_rejected(self, tmp_path: Path, capsys):
        code = evolution_main(
            [
                "--proposal",
                str(_proposal_file(tmp_path)),
                "--proposal-log",
                "evals/baselines/proposals.jsonl",
            ]
        )
        err = capsys.readouterr().err
        assert code == 2
        assert "evals/baselines" in err
