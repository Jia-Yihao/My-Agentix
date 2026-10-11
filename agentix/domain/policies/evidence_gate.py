"""R-P2-2 证据门禁（rag-optimization §6）：citation/grounding 由「仅观测」升级为可配门禁。

- ``observe``（默认）：与基线逐位等价——只跑既有验证器与 WARN 日志，不改答案、
  不拦截交付（Phase 31 只增不进门禁纪律）；
- ``warn``：触发时在答案尾部追加**一条**证据核验警告（原答案保留）；
- ``refuse``：触发时降级拒答——答案替换为拒答文案、citations 清空；
- 触发信号（任一即触发）：
  1. grounding 低分——报告未跳过且 ``score < warn_threshold``（``0`` = 关闭该信号）；
  2. 法条引用未通过核验（R1-2 ``unverified_refs`` 非空）；
  3. 引用无法溯源被剥离（``verify_citations_traceable`` stripped 非空）；
- 纯函数零 I/O：判定与文案均可离线单测；未知模式 fail-safe 回 ``observe``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "GATE_MODES",
    "GATE_MODE_OBSERVE",
    "GATE_MODE_WARN",
    "GATE_MODE_REFUSE",
    "ACTION_DELIVER",
    "ACTION_WARN",
    "ACTION_REFUSE",
    "EVIDENCE_GATE_REFUSAL_MSG",
    "EVIDENCE_GATE_WARN_HEADER",
    "GateDecision",
    "normalize_gate_mode",
    "decide_evidence_gate",
    "build_warn_footer",
]

GATE_MODE_OBSERVE = "observe"
GATE_MODE_WARN = "warn"
GATE_MODE_REFUSE = "refuse"
GATE_MODES: Tuple[str, str, str] = (GATE_MODE_OBSERVE, GATE_MODE_WARN, GATE_MODE_REFUSE)

ACTION_DELIVER = "deliver"
ACTION_WARN = "warn"
ACTION_REFUSE = "refuse"

#: 降级拒答文案（refuse 模式触发时替换答案正文）。
EVIDENCE_GATE_REFUSAL_MSG = "答案未通过证据核验，已拒绝交付。"
#: warn 模式追加的警告前缀（与 R1-2「引用核验提示」标注头刻意不同文案）。
EVIDENCE_GATE_WARN_HEADER = "证据核验提示"

TRIGGER_GROUNDING_LOW = "grounding_low_score"
TRIGGER_CITATION_UNVERIFIED = "citation_unverified"
TRIGGER_CITATION_UNTRACEABLE = "citation_untraceable"


def normalize_gate_mode(raw: Any) -> str:
    """模式归一化：未知/空值 fail-safe 回 ``observe``（门禁误配不得改变基线）。"""
    mode = str(raw or "").strip().lower()
    return mode if mode in GATE_MODES else GATE_MODE_OBSERVE


@dataclass(frozen=True)
class GateDecision:
    """门禁判定结果（进日志/tool_ctx 审计）。"""

    action: str  # deliver | warn | refuse
    mode: str
    triggers: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode, "action": self.action, "triggers": list(self.triggers)}


def decide_evidence_gate(
    *,
    mode: Any,
    grounding_score: Optional[float] = None,
    grounding_skipped_reason: Optional[str] = None,
    warn_threshold: float = 0.0,
    unverified_citations: int = 0,
    stripped_citations: int = 0,
) -> GateDecision:
    """收集触发信号并按模式给出动作（纯函数）。

    ``observe`` 或零触发 ⇒ ``deliver``；``warn`` ⇒ 附警告；``refuse`` ⇒ 拒答。
    触发信号在 ``observe`` 下同样计算（审计可见），但绝不改变交付。
    """
    mode = normalize_gate_mode(mode)
    triggers = []
    if (
        grounding_skipped_reason is None
        and grounding_score is not None
        and warn_threshold > 0
        and grounding_score < warn_threshold
    ):
        triggers.append(TRIGGER_GROUNDING_LOW)
    if unverified_citations > 0:
        triggers.append(TRIGGER_CITATION_UNVERIFIED)
    if stripped_citations > 0:
        triggers.append(TRIGGER_CITATION_UNTRACEABLE)

    if not triggers:
        return GateDecision(ACTION_DELIVER, mode, ())
    if mode == GATE_MODE_REFUSE:
        return GateDecision(ACTION_REFUSE, mode, tuple(triggers))
    if mode == GATE_MODE_WARN:
        return GateDecision(ACTION_WARN, mode, tuple(triggers))
    return GateDecision(ACTION_DELIVER, mode, tuple(triggers))


def build_warn_footer(
    decision: GateDecision,
    *,
    score: Optional[float] = None,
    unverified: int = 0,
    stripped: int = 0,
) -> str:
    """warn 动作的尾部警告文案（单行，触发项逐一枚举）。"""
    parts = []
    if TRIGGER_GROUNDING_LOW in decision.triggers and score is not None:
        parts.append(f"证据锚定分偏低（{score:.2f}）")
    if TRIGGER_CITATION_UNVERIFIED in decision.triggers:
        parts.append(f"{unverified} 条法条引用未通过核验")
    if TRIGGER_CITATION_UNTRACEABLE in decision.triggers:
        parts.append(f"{stripped} 条引用无法溯源已移除")
    if not parts:
        return ""
    return f"{EVIDENCE_GATE_WARN_HEADER}：{'；'.join(parts)}。"
