"""P1 任务执行的两个写工具声明与参数校验（岗位对照·任务执行）。

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``TASK_EXEC_ENABLED=true`` 时挂进声明清单（flag-off 零注册，声明清单
  与基线逐项一致）；
- **实现**（handler → ``InMemoryArtifactStore.put``）在组合根
  ``application/services/task_tools_bridge.py``（capabilities 不反向依赖
  agent/application——与 notify_user / read_skill 同款职责切分）；
- 产物为**内存态**（进程内 store，重启即失）：本批先打通「写工具治理
  链路」（声明 → 风险 → HITL → 执行 → 轨迹），持久化通道后续再接——
  文档与工具描述均已注明，不假装持久化。

治理语义（Gotcha 1：出站即副作用）：

- 两工具均 ``side_effect=True``（validator 禁止 auto-retry，防重复写入）；
- ``append_task_note``：声明 L1（→ 审批级 MEDIUM）；名称不命中
  ``human_review.py`` 词表（无 write/drop/send 等词）→ 有效等级 MEDIUM；
- ``write_task_artifact``：名称命中 ``write`` 词表 → HIGH，声明 L2（→ HIGH）
  + 必填 ``control``——两层一致，``HUMAN_REVIEW_ENABLED=true`` 时须人工批准。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "AppendTaskNoteSchema",
    "WriteTaskArtifactSchema",
    "MAX_NOTE_CHARS",
    "MAX_ARTIFACT_NAME_CHARS",
    "validate_append_note_args",
    "validate_artifact_args",
    "build_append_task_note_spec",
    "build_write_task_artifact_spec",
]

#: 笔记正文上限（与 notify_user 同界，事件/产物日志有界）。
MAX_NOTE_CHARS = 4000

#: 产物名上限。
MAX_ARTIFACT_NAME_CHARS = 200


class AppendTaskNoteSchema(BaseModel):
    """``append_task_note`` 入参（仅 ``TASK_EXEC_ENABLED=true`` 进声明清单）。"""

    note: str = Field(description="要追加记录的笔记正文（必填，有界）")
    key: str = Field(
        default="default",
        description="笔记分组键（默认 default；同一 key 的笔记在 meta 中归组）",
    )


class WriteTaskArtifactSchema(BaseModel):
    """``write_task_artifact`` 入参（仅 ``TASK_EXEC_ENABLED=true`` 进声明清单）。"""

    name: str = Field(description="产物名（必填，≤200 字符，便于按名检索）")
    content: str = Field(description="产物正文（必填）")
    kind: str = Field(
        default="task_artifact",
        description="产物类型（默认 task_artifact，如 memo/report/summary）",
    )
    summary: str = Field(default="", description="一句话摘要（可选）")


def _strip_control(text: str) -> str:
    """剥离 C0/C1 控制字符（保留换行/制表）——正文会进产物与前端展示。"""
    out = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
        elif 32 <= ord(ch) < 127 or ord(ch) >= 160:
            out.append(ch)
    return "".join(out)


def validate_append_note_args(args: Dict[str, Any]) -> Dict[str, str]:
    """校验并规范化 ``append_task_note`` 入参；非法输入抛 ``ValueError``。"""
    note = _strip_control(str(args.get("note") or "")).strip()
    key = _strip_control(str(args.get("key") or "default")).strip() or "default"

    if not note:
        raise ValueError("note must be a non-empty string")
    if len(note) > MAX_NOTE_CHARS:
        raise ValueError(f"note exceeds {MAX_NOTE_CHARS} chars")
    if len(key) > 64:
        raise ValueError("key must be <=64 chars")
    return {"note": note, "key": key}


def validate_artifact_args(args: Dict[str, Any]) -> Dict[str, str]:
    """校验并规范化 ``write_task_artifact`` 入参；非法输入抛 ``ValueError``。"""
    name = _strip_control(str(args.get("name") or "")).strip()
    content = _strip_control(str(args.get("content") or "")).strip()
    kind = _strip_control(str(args.get("kind") or "task_artifact")).strip()
    summary = _strip_control(str(args.get("summary") or "")).strip()

    if not name or len(name) > MAX_ARTIFACT_NAME_CHARS:
        raise ValueError(
            f"name must be non-empty and <= {MAX_ARTIFACT_NAME_CHARS} chars"
        )
    if not content:
        raise ValueError("content must be a non-empty string")
    if not kind or len(kind) > 64:
        raise ValueError("kind must be non-empty and <=64 chars")
    if len(summary) > 400:
        raise ValueError("summary must be <=400 chars")
    return {"name": name, "content": content, "kind": kind, "summary": summary}


def build_append_task_note_spec() -> ToolSpec:
    """构造 ``append_task_note`` 声明（L1 → 审批级 MEDIUM；名称无写词）。"""
    return ToolSpec(
        name="append_task_note",
        risk_level="L1",  # L1 → MEDIUM；词表：append 不命中任何模式 → 默认 LOW → max=MEDIUM
        description=(
            "把一条笔记追加记录到会话任务产物（内存 ArtifactStore，kind=task_note）。\n"
            "何时用：任务需要留存过程性记录/要点/结论摘要，供后续步骤或会话回看。\n"
            "何时不能用：正文为空或超长（>4000 字符）时——先精简；"
            "需要正式产物文件请改用 write_task_artifact。\n"
            "注意：出站即副作用——MEDIUM 风险，开启 HUMAN_REVIEW_ENABLED 时须"
            "人工批准后才执行；产物仅存内存（重启即失），trace_id 可追溯。"
        ),
        parameters=AppendTaskNoteSchema.model_json_schema(),
        side_effect=True,  # 写入即副作用：不可按只读对待（Gotcha 1）
        capabilities=("interaction",),
        timeout_ms=10_000,
    )


def build_write_task_artifact_spec() -> ToolSpec:
    """构造 ``write_task_artifact`` 声明（L2 + control；词表 write → HIGH）。"""
    return ToolSpec(
        name="write_task_artifact",
        risk_level="L2",  # 词表 write → HIGH；L2 → HIGH，两层一致
        control=(
            "需人工审批（HITL）：HUMAN_REVIEW_ENABLED=true 时有效风险 HIGH，"
            "未获人工批准不得执行（fail-closed）；产物仅落内存 "
            "ArtifactStore（rebuildable=false，重启即失），按 trace_id/名称可追溯。"
        ),
        description=(
            "把一份产物（备忘/报告/整理稿等）写入会话任务产物库"
            "（内存 ArtifactStore，可按 name 检索）。\n"
            "何时用：任务目标是产出一份可复用的成果文档/结构化记录。\n"
            "何时不能用：只是过程笔记（用 append_task_note）；"
            "或 name/content 为空（本工具 fail-fast 拒绝）。\n"
            "注意：出站即副作用——HIGH 风险（名称含 write），声明级 L2，"
            "开启 HUMAN_REVIEW_ENABLED 时须人工批准后才执行；"
            "产物仅存内存（重启即失），trace_id 可追溯。"
        ),
        parameters=WriteTaskArtifactSchema.model_json_schema(),
        side_effect=True,
        capabilities=("interaction",),
        timeout_ms=10_000,
    )
