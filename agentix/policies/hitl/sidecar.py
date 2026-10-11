"""HITL Sidecar（Phase 8）：Reviewer 独立于被审 Agent 的上下文。

书 §4.10 / 规划修订 7：Reviewer **不得**读被审 Agent 的自然语言自述；
只看结构化参数、产物引用、风险等级。Sidecar 形态：
- 独立 ReviewSession（不共享 Agent 的 trajectory / prompt 缓存）；
- Approve / Reject / Edit / Resume 四裁决；
- 断点持久化 → 进程重启可恢复（与 BreakpointManager 的 InterruptRecord 对齐）。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "ReviewDecisionAction",
    "SidecarReviewRequest",
    "SidecarReviewResult",
    "ReviewerSidecar",
]


class ReviewDecisionAction(str, Enum):
    """四裁决（规划 Phase 8）。"""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"      # 修正参数后放行
    RESUME = "resume"  # 从断点继续（不重跑已完成步）


@dataclass(frozen=True)
class SidecarReviewRequest:
    """送审面：**只含结构化字段**，禁止携带 Proposer 自述。"""

    request_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    declared_risk_level: str = "L0"
    evidence_ids: tuple = ()
    #: 产物引用（路径/ID），Reviewer 按需只读拉取，不嵌入 Agent prompt
    artifact_refs: tuple = ()
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        declared_risk_level: str = "L0",
        evidence_ids: tuple = (),
        artifact_refs: tuple = (),
    ) -> "SidecarReviewRequest":
        return cls(
            request_id=uuid.uuid4().hex,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            declared_risk_level=declared_risk_level,
            evidence_ids=tuple(evidence_ids),
            artifact_refs=tuple(artifact_refs),
        )

    def to_public_dict(self) -> Dict[str, Any]:
        """给 Reviewer UI / 持久化的投影——**无** Agent trajectory / narrative。"""
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "declared_risk_level": self.declared_risk_level,
            "evidence_ids": list(self.evidence_ids),
            "artifact_refs": list(self.artifact_refs),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SidecarReviewResult:
    request_id: str
    action: ReviewDecisionAction
    reviewer_id: str
    edited_arguments: Optional[Dict[str, Any]] = None
    reason: str = ""
    decided_at: float = field(default_factory=time.time)


class ReviewerSidecar:
    """独立 Reviewer 进程面（逻辑隔离即可；物理进程部署属运维）。

    - **上下文隔离**：`review()` 只收 :class:`SidecarReviewRequest`，
      不收 Agent 的 prompt / trajectory / rationale；
    - **fail-closed**：未注入 approver 时，高风险一律 REJECT；
    - **Edit**：裁决可附带修正后的 arguments（白名单 key 覆盖）。
    """

    def __init__(
        self,
        *,
        reviewer_id: str = "sidecar-1",
        approver: Optional[Any] = None,
        edit_allowed_keys: Optional[frozenset] = None,
        persist_dir: Optional[str] = None,
    ) -> None:
        self.reviewer_id = reviewer_id
        self._approver = approver  # async (request_dict) -> bool | dict | str
        self._edit_keys = edit_allowed_keys  # None = 允许任意 key
        self._persist_dir = persist_dir
        self._results: Dict[str, SidecarReviewResult] = {}
        self._pending: Dict[str, SidecarReviewRequest] = {}

    # -- 提交 / 裁决 -----------------------------------------------------

    async def submit(self, request: SidecarReviewRequest) -> str:
        """挂起待审（进入 WAITING_APPROVAL 面）。返回 request_id。"""
        self._pending[request.request_id] = request
        self._persist()
        return request.request_id

    async def review(
        self,
        request: SidecarReviewRequest,
        *,
        action: ReviewDecisionAction,
        edited_arguments: Optional[Dict[str, Any]] = None,
        reason: str = "",
    ) -> SidecarReviewResult:
        """执行四裁决。Approve/Edit 前若注入了 approver 则先问它（fail-closed）。"""
        if action in (ReviewDecisionAction.APPROVE, ReviewDecisionAction.EDIT) and self._approver is not None:
            ok = await self._ask_approver(request)
            if not ok:
                result = SidecarReviewResult(
                    request_id=request.request_id,
                    action=ReviewDecisionAction.REJECT,
                    reviewer_id=self.reviewer_id,
                    reason="approver_denied",
                )
                self._finalize(request, result)
                return result

        final_args: Optional[Dict[str, Any]] = None
        if action is ReviewDecisionAction.EDIT:
            if edited_arguments is None:
                raise ValueError("EDIT action requires edited_arguments")
            final_args = self._filter_edits(request.arguments, edited_arguments)

        result = SidecarReviewResult(
            request_id=request.request_id,
            action=action,
            reviewer_id=self.reviewer_id,
            edited_arguments=final_args,
            reason=reason,
        )
        self._finalize(request, result)
        return result

    async def auto_review_high_risk(
        self, request: SidecarReviewRequest
    ) -> SidecarReviewResult:
        """高风险默认路径：无 approver → fail-closed REJECT。"""
        needs_human = request.declared_risk_level in {"L1", "L2", "L3"} or bool(
            request.arguments.get("force")
        ) or bool(request.arguments.get("all"))
        if not needs_human:
            return await self.review(request, action=ReviewDecisionAction.APPROVE)
        if self._approver is None:
            return await self.review(
                request,
                action=ReviewDecisionAction.REJECT,
                reason="no_approver_wired_fail_closed",
            )
        return await self.review(request, action=ReviewDecisionAction.APPROVE)

    # -- 隔离断言面（测试用） --------------------------------------------

    @property
    def pending_ids(self) -> List[str]:
        return list(self._pending)

    def get_result(self, request_id: str) -> Optional[SidecarReviewResult]:
        return self._results.get(request_id)

    def to_persistence(self) -> Dict[str, Any]:
        """序列化挂起 + 已决（重启恢复用）。**不含** Agent 内部状态。"""
        return {
            "pending": {k: v.to_public_dict() for k, v in self._pending.items()},
            "results": {
                k: {
                    "request_id": r.request_id,
                    "action": r.action.value,
                    "reviewer_id": r.reviewer_id,
                    "edited_arguments": r.edited_arguments,
                    "reason": r.reason,
                    "decided_at": r.decided_at,
                }
                for k, r in self._results.items()
            },
            "reviewer_id": self.reviewer_id,
        }

    @classmethod
    def from_persistence(
        cls, data: Dict[str, Any], *, reviewer_id: Optional[str] = None, **kw
    ) -> "ReviewerSidecar":
        side = cls(reviewer_id=reviewer_id or data.get("reviewer_id", "sidecar-restored"), **kw)
        for rid, pub in (data.get("pending") or {}).items():
            side._pending[rid] = SidecarReviewRequest(
                request_id=pub["request_id"],
                tool_name=pub["tool_name"],
                arguments=dict(pub.get("arguments") or {}),
                declared_risk_level=pub.get("declared_risk_level", "L0"),
                evidence_ids=tuple(pub.get("evidence_ids") or ()),
                artifact_refs=tuple(pub.get("artifact_refs") or ()),
                created_at=pub.get("created_at", time.time()),
            )
        for rid, rd in (data.get("results") or {}).items():
            side._results[rid] = SidecarReviewResult(
                request_id=rd["request_id"],
                action=ReviewDecisionAction(rd["action"]),
                reviewer_id=rd.get("reviewer_id", side.reviewer_id),
                edited_arguments=rd.get("edited_arguments"),
                reason=rd.get("reason", ""),
                decided_at=rd.get("decided_at", time.time()),
            )
        return side

    # -- internals --------------------------------------------------------

    def _finalize(self, request: SidecarReviewRequest, result: SidecarReviewResult) -> None:
        self._pending.pop(request.request_id, None)
        self._results[request.request_id] = result
        self._persist()

    def _filter_edits(
        self, original: Dict[str, Any], edits: Dict[str, Any]
    ) -> Dict[str, Any]:
        """返回**增量 edits**（调用方与 original 合并）；白名单时只留允许 key。"""
        if self._edit_keys is None:
            return dict(edits)
        return {k: v for k, v in edits.items() if k in self._edit_keys}

    async def _ask_approver(self, request: SidecarReviewRequest) -> bool:
        approver = self._approver
        if approver is None:
            return True  # type: ignore[unreachable]
        out = approver(request.to_public_dict())
        if hasattr(out, "__await__"):
            out = await out
        if isinstance(out, dict):
            decision = out.get("decision") or out.get("action") or out.get("result")
            return str(decision).lower() in {"approve", "approved", "allow", "yes", "true", "ok", "放行", "批准"}
        if isinstance(out, bool):
            return out
        return str(out or "").strip().lower() in {
            "approve", "approved", "allow", "yes", "true", "ok", "放行", "批准",
        }

    def _persist(self) -> None:
        if not self._persist_dir:
            return
        import json
        from pathlib import Path

        path = Path(self._persist_dir) / f"sidecar_{self.reviewer_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_persistence(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
