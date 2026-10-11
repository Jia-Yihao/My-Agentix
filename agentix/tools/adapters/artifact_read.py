"""P3 ``read_artifact`` 取回工具（岗位对照·渐进上下文，补「写→读闭环」）。

背景（对照矩阵行 3）：任务管线（P1）已有 ``append_task_note`` /
``write_task_artifact`` 两个写工具，产物落 ``InMemoryArtifactStore`` 后
**没有任何取回通道**——模型写完即失忆；隔离正文（``obs://`` 句柄、
offload 产物）同样无工具可取回正文。本工具补齐读环。

职责切分（与 notify_user / read_skill / task_writes 同款）：

- **声明**（spec + LLM schema）在本文件，由 ``registry_init`` 在
  ``TASK_EXEC_ENABLED=true`` 时挂进声明清单（flag-off 零注册，声明清单
  与基线逐项一致）；
- **实现**（handler → ``InMemoryArtifactStore.get``）在组合根
  ``application/services/task_tools_bridge.py``（capabilities 不反向依赖
  agent/application）。

治理语义：

- **只读**：``side_effect=False``、声明 L0；名称不命中
  ``human_review.py`` 词表（无 write/drop/send 等词）⇒ 有效风险 LOW，
  任何模式下免审批；
- schema 类名 ``read_artifact`` == 工具注册名（title 契约——否则
  ``selection._spec_for_schema`` 的 title 回查静默 miss，延迟检索与
  tool_policy 过滤都会漏掉它），沿用 kg_tools_list / tool_search 约定；
- 正文上限 ``max_chars``（默认 4000，≤16384）：取回不等于把 8MB 存储
  倾倒进上下文——超长确定性截断并提示（与指令层 4k 纪律同源）。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "read_artifact",
    "MAX_READ_CHARS",
    "DEFAULT_READ_CHARS",
    "validate_read_artifact_args",
    "build_read_artifact_spec",
]

#: 取回正文上限（防上下文倾倒；与指令层单块 4k 纪律同源）
MAX_READ_CHARS = 16384

#: 默认返回上限
DEFAULT_READ_CHARS = 4000


class read_artifact(BaseModel):
    """``read_artifact`` 入参（类名即工具名：schema title 必须等于注册名，
    否则 ``selection._spec_for_schema`` 的 title 回查会静默 miss——
    按 kg_tools_list / tool_search 的既有约定命名，而非 CamelCase）。"""

    artifact_id: str = Field(
        description="产物 ID（append_task_note / write_task_artifact 返回的 artifact_id）"
    )
    max_chars: int = Field(
        default=DEFAULT_READ_CHARS,
        ge=1,
        le=MAX_READ_CHARS,
        description=f"正文返回上限（1-{MAX_READ_CHARS}，默认 {DEFAULT_READ_CHARS}；超长确定性截断并提示）",
    )


def validate_read_artifact_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化 ``read_artifact`` 入参；非法输入抛 ``ValueError``。"""
    artifact_id = str(args.get("artifact_id") or "").strip()
    raw_max = args.get("max_chars", DEFAULT_READ_CHARS)
    try:
        max_chars = int(raw_max)
    except (TypeError, ValueError):
        raise ValueError("max_chars must be an integer") from None

    if not artifact_id or len(artifact_id) > 128:
        raise ValueError("artifact_id must be non-empty and <=128 chars")
    if not 1 <= max_chars <= MAX_READ_CHARS:
        raise ValueError(f"max_chars must be within 1..{MAX_READ_CHARS}")
    return {"artifact_id": artifact_id, "max_chars": max_chars}


def build_read_artifact_spec() -> ToolSpec:
    """构造 ``read_artifact`` 声明（L0 只读、零副作用；免审批）。"""
    return ToolSpec(
        name="read_artifact",
        risk_level="L0",  # 只读取回；词表（write/drop/...）不命中 → 默认 LOW
        description=(
            "按 artifact_id 取回一份已存产物的正文（内存 ArtifactStore 只读）。\n"
            "何时用：write_task_artifact / append_task_note 之后需要回读产物内容、"
            "复用先前写下的结论，或核对产物与当前任务是否一致；也可用 "
            "`obs://` 指针取回被隔离/索引化的正文（上下文隔离引用、记忆索引"
            "指针——前缀自动剥离）。\n"
            "何时不能用：artifact_id 为空或不在库中（本工具 fail-fast 拒绝，"
            "请先用写工具产出再取回）；需要按名称浏览全部产物（本工具按 ID 精确取回）。\n"
            "注意：只读、零副作用、免审批；正文超 max_chars 确定性截断并提示，"
            "不会把整库倾倒进上下文。"
        ),
        parameters=read_artifact.model_json_schema(),
        side_effect=False,
        capabilities=("persistence",),
        timeout_ms=10_000,
    )
