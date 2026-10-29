"""Trajectory 采集与落盘（增量设计 §3.6）——Phase 10 收敛为**薄适配层**。

真源：``agentix.application.services.trajectory``（evals / 首错归因 / 前缀回归）。
本模块只保留运行时旁路需要的 **ContextVar + begin_step/end_step** span 面；
落盘结构与 ``services.TrajectoryStep.to_dict`` 对齐，失败步语义与
``locate_first_error`` 一致（首个 status!=ok 的 step name）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentix.observability.context import current_request_id, current_trace_id

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_recorder_var: ContextVar["TrajectoryRecorder | None"] = ContextVar(
    "retrieval_trajectory_recorder", default=None
)


class TrajectoryRecorder:
    """一次请求的步骤轨迹（start → step* → end → dump）。"""

    def __init__(
        self,
        name: str,
        *,
        kb_id: str = "",
        conversation_id: str = "",
        question: str = "",
        prompt_version: str = "",
        model_version: str = "",
        knowledge_version: str = "",
    ):
        self.name = name
        self.kb_id = kb_id
        self.conversation_id = conversation_id
        self.question = question
        # These values are deliberately plain strings so traces remain portable
        # between the JSON recorder and OpenTelemetry exporters.
        self.prompt_version = prompt_version
        self.model_version = model_version
        self.knowledge_version = knowledge_version
        self.trace_id = current_trace_id()
        if not self.trace_id or self.trace_id == "-":
            self.trace_id = uuid.uuid4().hex
        self.request_id = current_request_id()
        self.started_at = datetime.now(timezone.utc)
        self.status = "ok"
        self.failure_stage: str | None = None
        self.terminate_reason: str | None = None
        self.steps: list[dict] = []
        self._t0 = time.perf_counter()

    # -- 步骤采集（由 spans.span / spans.sync_span 调用） ------------------

    def begin_step(self, name: str) -> dict:
        return {
            "name": name,
            "started_ms": int((time.perf_counter() - self._t0) * 1000),
            "_t0": time.perf_counter(),
        }

    def end_step(
        self,
        step: dict,
        *,
        status: str = "ok",
        attributes: dict[str, Any] | None = None,
    ) -> None:
        duration_ms = int((time.perf_counter() - step.pop("_t0", time.perf_counter())) * 1000)
        step["duration_ms"] = duration_ms
        step["status"] = status
        step["attributes"] = _jsonable(attributes or {})
        step["seq"] = len(self.steps) + 1
        self.steps.append(step)
        if status != "ok" and self.failure_stage is None:
            self.failure_stage = step["name"]

    def fail_step(self, step: dict, *, attributes: dict[str, Any] | None = None) -> None:
        self.end_step(step, status="error", attributes=attributes)

    def first_error_step(self) -> dict | None:
        """首个失败步（与 services.locate_first_error 同语义）。"""
        for s in self.steps:
            if s.get("status") != "ok":
                return s
        return None

    def as_service_steps(self) -> list[dict]:
        """导出为 services.TrajectoryStep.from_dict 兼容的 dict 列表（收敛桥）。"""
        out: list[dict] = []
        for i, s in enumerate(self.steps):
            out.append(
                {
                    "step_index": i,
                    "kind": str(s.get("kind", "node") or "node"),
                    "name": str(s.get("name", "")),
                    "status": str(s.get("status", "ok")),
                    "trace_id": self.trace_id,
                    "latency_ms": float(s.get("duration_ms", 0) or 0),
                    "error": s.get("error") or s.get("attributes", {}).get("error")
                    if isinstance(s.get("attributes"), dict)
                    else s.get("error"),
                    "payload_preview": "",
                }
            )
        return out

    # -- 生命周期 ----------------------------------------------------------

    def end(self, status: str = "ok") -> None:
        self.status = status
        if status != "ok" and self.failure_stage is None:
            # 整体失败但无失败 step（如准备阶段异常）
            self.failure_stage = self.name

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def set_versions(
        self,
        *,
        prompt_version: str | None = None,
        model_version: str | None = None,
        knowledge_version: str | None = None,
    ) -> None:
        """Attach resolved artifact versions when they become known mid-request."""
        if prompt_version is not None:
            self.prompt_version = prompt_version
        if model_version is not None:
            self.model_version = model_version
        if knowledge_version is not None:
            self.knowledge_version = knowledge_version

    # -- 落盘 ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "name": self.name,
            "kb_id": self.kb_id,
            "conversation_id": self.conversation_id,
            "question": self.question,
            "prompt_version": self.prompt_version,
            "model_version": self.model_version,
            "knowledge_version": self.knowledge_version,
            "versions": {
                "prompt": self.prompt_version,
                "model": self.model_version,
                "knowledge": self.knowledge_version,
            },
            "started_at": self.started_at.isoformat(),
            "status": self.status,
            "failure_stage": self.failure_stage,
            "terminate_reason": self.terminate_reason,
            "total_ms": self.total_ms(),
            "steps": self.steps,
        }

    def dump(self) -> None:
        """落盘 JSON；任何异常只 logger.debug，不影响主流程。"""
        try:
            directory = _dump_dir()
            path = directory / f"{self.trace_id}.json"
            path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001 — 落盘绝不抛出
            logger.debug("trajectory dump failed: %s", e)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _dump_dir() -> Path:
    from agentix.config.settings import settings

    root = Path(settings.TRAJECTORY_DIR)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- ContextVar 单例 -------------------------------------------------------


def set_recorder(recorder: TrajectoryRecorder) -> Token:
    return _recorder_var.set(recorder)


def current_recorder() -> "TrajectoryRecorder | None":
    return _recorder_var.get()


def reset_recorder(token: Token) -> None:
    _recorder_var.reset(token)
