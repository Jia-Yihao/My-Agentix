"""Continuous Evolution 双循环（Phase 12，书 §9.6–9.9）。

- **在线循环**：只记轨迹/信号，**零写能力**（不得改 prompt/工具/验证器）；
- **离线循环**：聚合 → 提案 → 回归 → 灰度；
- **三道可信根防线**：
  1. 证据 ≠ 指令（trajectory payload 当数据不当 prompt 注入源）；
  2. staging 隔离（提案先落 staging，不直写生产配置）；
  3. 验证器不可被进化流程修改（verifier 路径黑名单）。

四类进化载体：prompt / tool_policy / memory_consolidation / verifier_meta
（verifier **逻辑**不可改，仅允许元数据/阈值白名单字段）。

Phase 30 补两载体（书 §9.6 载体谱系）：knowledge（KB 提示片段与检索参数提案，
回归门 citation + scope_isolation——检索知识不越权、引用不降）/ program
（**声明式**工具编排与策略规则，payload 过 allowlist、绝不执行代码，
回归门 param_fidelity + verifier_logic_unchanged）。另：可选 ``proposal_sink``
把 staged/applied/rejected 三状态 append-only 落盘（Ch9 §9.8.2 保留失败；
无 sink = 行为与基线字节等价）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

__all__ = [
    "EvolutionCarrier",
    "EvolutionProposal",
    "OnlineLoopRecorder",
    "OfflineEvolutionLoop",
    "get_online_loop_recorder",
    "reset_online_loop_recorder",
    "jsonl_proposal_sink",
    "PROTECTED_VERIFIER_PATHS",
    "EVIDENCE_KEYS_NEVER_PROMPT",
    "KNOWLEDGE_PAYLOAD_ALLOWLIST",
    "PROGRAM_PAYLOAD_ALLOWLIST",
    "PROGRAM_RULE_KEYS",
]


class EvolutionCarrier(str, Enum):
    """六类进化载体（书 §9.6–9.9；Phase 30 补 knowledge / program）。"""

    PROMPT = "prompt"
    TOOL_POLICY = "tool_policy"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    VERIFIER_META = "verifier_meta"
    KNOWLEDGE = "knowledge"
    PROGRAM = "program"


#: 防线 3：验证器**逻辑**路径——进化流程禁止写入。
#: M3 实体迁 ``agentix/policies/verifier/``、M5 grounding 实体迁
#: ``agentix/domain/policies/``；M6 清尾拆 shim 后仅保留实体路径（4 条）。
PROTECTED_VERIFIER_PATHS: FrozenSet[str] = frozenset(
    {
        "agentix/policies/verifier/layered.py",
        "agentix/policies/verifier/structural.py",
        "agentix/domain/policies/grounding_verify.py",
        "agentix/domain/policies/completion.py",
    }
)

#: 防线 1：证据字段永不当指令注入
EVIDENCE_KEYS_NEVER_PROMPT: FrozenSet[str] = frozenset(
    {
        "content",
        "snippet",
        "text",
        "raw",
        "tool_output",
        "observation",
        "trajectory",
        "payload",
    }
)

#: verifier_meta 允许碰的白名单键（阈值/标签，非判定逻辑）
VERIFIER_META_ALLOWLIST: FrozenSet[str] = frozenset(
    {"support_threshold", "max_evidence", "prompt_version_label", "notes"}
)

#: knowledge 载体允许变更的 payload 键：KB 提示片段与检索参数（不越权到代码/验证器）
KNOWLEDGE_PAYLOAD_ALLOWLIST: FrozenSet[str] = frozenset(
    {"prompt_fragment", "top_k", "threshold", "notes"}
)

#: program 载体 payload 键：只收声明式规则集（结构化 JSON，**绝不执行**）
PROGRAM_PAYLOAD_ALLOWLIST: FrozenSet[str] = frozenset({"rules", "notes"})

#: program 单条规则的键（when/then 声明式形状；任意代码字段一律拒）
PROGRAM_RULE_KEYS: FrozenSet[str] = frozenset({"id", "when", "then"})


def _is_declarative_value(value: Any) -> bool:
    """声明式值检查：标量 / 标量列表 / 标量字典（递归），无对象、无可执行结构。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_declarative_value(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_declarative_value(v) for k, v in value.items())
    return False


@dataclass(frozen=True)
class EvolutionProposal:
    carrier: EvolutionCarrier
    summary: str
    payload: Dict[str, Any] = field(default_factory=dict)
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    staged: bool = False
    applied: bool = False
    rejected_reason: str = ""

    def content_hash(self) -> str:
        blob = f"{self.carrier.value}|{self.summary}|{sorted(self.payload.items())}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class OnlineLoopRecorder:
    """在线循环：**只读轨迹 → 只记信号**，零写能力。

    DoD：调用 :meth:`record` 不得接受任何会修改 prompt/tool/verifier 的参数；
    :attr:`write_capability` 恒为 False。
    """

    write_capability: bool = False

    def __init__(self) -> None:
        self._signals: List[Dict[str, Any]] = []

    def record(
        self,
        *,
        trace_id: str,
        success: bool,
        first_error_stage: str = "",
        cost_usd: float = 0.0,
        tags: Sequence[str] = (),
    ) -> None:
        """只追加观测信号——不接收 patch/payload/apply 回调。"""
        self._signals.append(
            {
                "trace_id": trace_id,
                "success": bool(success),
                "first_error_stage": first_error_stage,
                "cost_usd": float(cost_usd),
                "tags": list(tags),
            }
        )

    @property
    def signals(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(self._signals)

    def aggregate(self) -> Dict[str, Any]:
        n = len(self._signals)
        if n == 0:
            return {"n": 0, "success_rate": 0.0, "mean_cost_usd": 0.0, "error_stages": {}}
        ok = sum(1 for s in self._signals if s["success"])
        stages: Dict[str, int] = {}
        for s in self._signals:
            st = s["first_error_stage"]
            if st:
                stages[st] = stages.get(st, 0) + 1
        return {
            "n": n,
            "success_rate": round(ok / n, 4),
            "mean_cost_usd": round(sum(s["cost_usd"] for s in self._signals) / n, 6),
            "error_stages": stages,
        }


class OfflineEvolutionLoop:
    """离线循环：聚合 → 提案 → 回归门 → 灰度。三道防线在此强制执行。"""

    def __init__(
        self,
        *,
        protected_paths: Optional[FrozenSet[str]] = None,
        allow_staging: bool = True,
        proposal_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._protected = protected_paths if protected_paths is not None else PROTECTED_VERIFIER_PATHS
        self._allow_staging = allow_staging
        #: 可选提案档案 sink（Phase 30）：None = 不落盘（与基线行为字节等价）
        self._proposal_sink = proposal_sink
        #: 幂等去重：同 (content_hash, state) 的重放只 append 一次
        self._sunk_keys: set = set()
        self.proposals: List[EvolutionProposal] = []
        self.staging: Dict[str, EvolutionProposal] = {}
        self.applied: Dict[str, EvolutionProposal] = {}
        #: 回归门：carrier → 必须通过的检查名
        self.regression_gate: Dict[EvolutionCarrier, Tuple[str, ...]] = {
            EvolutionCarrier.PROMPT: ("golden_prefix", "citation"),
            EvolutionCarrier.TOOL_POLICY: ("risk_levels", "param_fidelity"),
            EvolutionCarrier.MEMORY_CONSOLIDATION: ("scope_isolation",),
            EvolutionCarrier.VERIFIER_META: ("verifier_logic_unchanged",),
            EvolutionCarrier.KNOWLEDGE: ("citation", "scope_isolation"),
            EvolutionCarrier.PROGRAM: ("param_fidelity", "verifier_logic_unchanged"),
        }

    # -- 提案档案落盘（Ch9 §9.8.2：三状态全记，保留失败） ----------------

    def _sink_record(self, proposal: EvolutionProposal, state: str) -> None:
        """向 sink 追加一条状态记录；无 sink 空操作（基线等价）。

        幂等：同 ``(content_hash, state)`` 的重放（重试/事件重复投递）只记一次。
        """
        if self._proposal_sink is None:
            return
        key = (proposal.content_hash(), state)
        if key in self._sunk_keys:
            return
        self._sunk_keys.add(key)
        self._proposal_sink(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "proposal_id": proposal.proposal_id,
                "carrier": proposal.carrier.value,
                "state": state,
                "content_hash": proposal.content_hash(),
                "summary": proposal.summary,
                "payload": dict(proposal.payload),
                "rejected_reason": proposal.rejected_reason,
            }
        )

    # -- 防线 1：证据 ≠ 指令 --------------------------------------------

    @staticmethod
    def sanitize_evidence_for_prompt(evidence: Sequence[Dict[str, Any]]) -> List[str]:
        """把证据投影为**纯文本块**，剥离可能被当指令的结构字段。

        返回的字符串应拼进 user/context 区，而非 system 指令区。
        """
        blocks: List[str] = []
        for item in evidence:
            for key, val in item.items():
                if key.lower() in EVIDENCE_KEYS_NEVER_PROMPT and isinstance(val, str):
                    # 仍可作为**引用文本**，但调用方必须放 evidence 区
                    blocks.append(val)
                elif key.lower() in ("doc_id", "source", "citation"):
                    blocks.append(f"[{key}={val}]")
        return blocks

    @staticmethod
    def is_evidence_injection_attempt(system_prompt: str, evidence: Sequence[Dict[str, Any]]) -> bool:
        """检测是否把 evidence 原文拼进了 system（简单启发，防御纵深）。"""
        for item in evidence:
            for key in EVIDENCE_KEYS_NEVER_PROMPT:
                val = item.get(key)
                if isinstance(val, str) and len(val) >= 20 and val in system_prompt:
                    return True
        return False

    # -- 防线 3：验证器不可自改 ------------------------------------------

    def touches_protected_verifier(self, paths: Sequence[str]) -> bool:
        norm = {p.replace("\\", "/") for p in paths}
        return bool(norm & self._protected)

    def validate_proposal(
        self, proposal: EvolutionProposal, *, touched_paths: Sequence[str] = ()
    ) -> Tuple[bool, str]:
        """提案有效性：载体白名单 + 验证器保护 + 各载体 payload 白名单。"""
        if not isinstance(proposal.carrier, EvolutionCarrier):
            return False, "unknown_carrier"
        if self.touches_protected_verifier(touched_paths):
            return False, "verifier_logic_protected"
        if proposal.carrier is EvolutionCarrier.VERIFIER_META:
            bad = set(proposal.payload) - VERIFIER_META_ALLOWLIST
            if bad:
                return False, f"verifier_meta_keys_not_allowed:{sorted(bad)}"
        if proposal.carrier is EvolutionCarrier.KNOWLEDGE:
            bad = set(proposal.payload) - KNOWLEDGE_PAYLOAD_ALLOWLIST
            if bad:
                return False, f"knowledge_payload_keys_not_allowed:{sorted(bad)}"
            for key in ("top_k", "threshold"):
                if key in proposal.payload and not isinstance(proposal.payload[key], (int, float)):
                    return False, f"knowledge_payload_type:{key}"
        if proposal.carrier is EvolutionCarrier.PROGRAM:
            # 防线延伸：声明式规则 + 键白名单，越界即拒（绝不引入可执行代码面）
            bad = set(proposal.payload) - PROGRAM_PAYLOAD_ALLOWLIST
            if bad:
                return False, f"program_payload_keys_not_allowed:{sorted(bad)}"
            rules = proposal.payload.get("rules")
            if rules is not None:
                if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
                    return False, "program_rules_not_declarative"
                for rule in rules:
                    bad_rule = set(rule) - PROGRAM_RULE_KEYS
                    if bad_rule:
                        return False, f"program_rule_keys_not_allowed:{sorted(bad_rule)}"
                    if not all(_is_declarative_value(v) for v in rule.values()):
                        return False, "program_rule_values_not_declarative"
        if not proposal.summary.strip():
            return False, "empty_summary"
        return True, "ok"

    # -- 提案 → staging → 回归 → 灰度 -----------------------------------

    def propose(
        self, proposal: EvolutionProposal, *, touched_paths: Sequence[str] = ()
    ) -> EvolutionProposal:
        ok, reason = self.validate_proposal(proposal, touched_paths=touched_paths)
        if not ok:
            rejected = EvolutionProposal(
                carrier=proposal.carrier,
                summary=proposal.summary,
                payload=proposal.payload,
                proposal_id=proposal.proposal_id,
                staged=False,
                applied=False,
                rejected_reason=reason,
            )
            self.proposals.append(rejected)
            self._sink_record(rejected, "rejected")
            return rejected
        staged = EvolutionProposal(
            carrier=proposal.carrier,
            summary=proposal.summary,
            payload=proposal.payload,
            proposal_id=proposal.proposal_id,
            staged=True,
            applied=False,
        )
        if not self._allow_staging:
            # staging 关闭 → 仍不直写生产，只拒
            rejected = EvolutionProposal(
                carrier=proposal.carrier,
                summary=proposal.summary,
                payload=proposal.payload,
                proposal_id=proposal.proposal_id,
                staged=False,
                applied=False,
                rejected_reason="staging_disabled",
            )
            self.proposals.append(rejected)
            self._sink_record(rejected, "rejected")
            return rejected
        self.proposals.append(staged)
        self.staging[staged.proposal_id] = staged
        self._sink_record(staged, "staged")
        return staged

    def run_regression(self, proposal_id: str, *, passed: bool) -> EvolutionProposal:
        """回归门：未通过则踢出 staging。"""
        prop = self.staging.get(proposal_id)
        if prop is None:
            raise KeyError(proposal_id)
        if not passed:
            del self.staging[proposal_id]
            rejected = EvolutionProposal(
                carrier=prop.carrier,
                summary=prop.summary,
                payload=prop.payload,
                proposal_id=prop.proposal_id,
                staged=False,
                applied=False,
                rejected_reason="regression_failed",
            )
            self.proposals.append(rejected)
            self._sink_record(rejected, "rejected")
            return rejected
        return prop

    def promote(self, proposal_id: str, *, canary_pct: float = 10.0) -> EvolutionProposal:
        """灰度晋升：只从 staging 出，记录 canary 比例。"""
        prop = self.staging.get(proposal_id)
        if prop is None:
            raise KeyError(proposal_id)
        applied = EvolutionProposal(
            carrier=prop.carrier,
            summary=prop.summary,
            payload={**prop.payload, "canary_pct": canary_pct},
            proposal_id=prop.proposal_id,
            staged=False,
            applied=True,
        )
        del self.staging[proposal_id]
        self.applied[applied.proposal_id] = applied
        self.proposals.append(applied)
        self._sink_record(applied, "applied")
        return applied


#: 进程内在线信号单例（Phase 17.1：chat_service 三处漏斗写入）。
_online_recorder: Optional[OnlineLoopRecorder] = None


def jsonl_proposal_sink(path: str) -> Callable[[Dict[str, Any]], None]:
    """提案档案 sink：append-only JSONL（staged/applied/rejected 三状态全记）。

    路径纪律（fail-closed）：**不得写入 ``evals/baselines/``**——该目录是
    quality.json / 轨迹包的门禁数据区，混入提案档案会被 ``load_packages``
    误读而 raise；建议 ``data/evolution/proposals.jsonl``
    （= ``settings.EVOLUTION_PROPOSAL_LOG`` 默认值）。

    :raises ValueError: 路径落在 ``evals/baselines/`` 之下。
    """
    norm = str(path).replace("\\", "/").strip()
    if not norm:
        raise ValueError("提案档案路径为空")
    parts = [p for p in norm.split("/") if p]
    for i in range(len(parts) - 1):
        if parts[i] == "evals" and parts[i + 1] == "baselines":
            raise ValueError(f"提案档案不得写入 evals/baselines/（与 quality.json 碰撞）：{path}")

    def _sink(record: Dict[str, Any]) -> None:
        target = Path(norm)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return _sink


def get_online_loop_recorder() -> OnlineLoopRecorder:
    """全局在线循环记录器（惰性创建；零写能力由类型保证）。"""
    global _online_recorder
    if _online_recorder is None:
        _online_recorder = OnlineLoopRecorder()
    return _online_recorder


def reset_online_loop_recorder() -> None:
    """重置在线记录器（测试隔离）。"""
    global _online_recorder
    _online_recorder = None
