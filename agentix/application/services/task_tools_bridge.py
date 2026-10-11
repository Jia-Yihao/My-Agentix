"""P1 任务写工具组合根装配（岗位对照·任务执行）。

职责切分（与 Phase 23 ``notify_bridge`` 同款）：

- **声明**（spec + LLM schema）在 ``capabilities/tools/task_writes.py``（写）
  与 ``capabilities/tools/artifact_read.py``（读，P3），由
  ``settings.TASK_EXEC_ENABLED`` gate（flag-off 不进声明清单）；
- **纯参数校验**在上述 capabilities 模块；
- 本模块只做接线：``ensure_registered()`` + ``register_handler``——
  写 handler 把内容落 ``InMemoryArtifactStore``（kind=``task_note`` /
  ``task_artifact``，``rebuildable=False``），读 handler（``read_artifact``）
  按 ID 只读取回并按 ``max_chars`` 截断，``main.py::_startup`` 仅在
  flag-on 时调用。

写入即副作用：handler 经 ``ToolExecutor.execute`` 执行 ⇒ 有效风险
（L1→MEDIUM / 词表 write→HIGH）/ 权限 / HITL 审批闸门全部在前，
本模块不提供任何绕过路径（Gotcha 1）。``register_handler`` 覆盖同名
handler，装配幂等。
"""

from __future__ import annotations

from agentix.config.settings import settings

__all__ = ["install_task_write_tools", "install_artifact_read_tool"]


def install_task_write_tools() -> None:
    """注册任务写工具处理器（幂等；依赖 flag-on 声明清单已有 spec）。"""
    if not settings.TASK_EXEC_ENABLED:
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.application.services.artifact_store import get_artifact_store
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered
    from agentix.tools.adapters.task_writes import (
    validate_append_note_args,
    validate_artifact_args,
)
    from agentix.domain.tracing import get_trace_id

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    async def _append_note(args: dict) -> dict:
        # 非法输入抛 ValueError → ToolResult 失败（fail-fast，不落半份笔记）
        normalized = validate_append_note_args(args)
        ref = get_artifact_store().put(
            normalized["note"],
            kind="task_note",
            summary=f"task note ({normalized['key']})",
            trace_id=get_trace_id(),
            rebuildable=False,  # 内存态：诚实声明不可重建
            meta={"key": normalized["key"]},
        )
        return {
            "status": "stored",
            "kind": "task_note",
            "artifact_id": ref.artifact_id,
            "size_bytes": ref.size_bytes,
            **normalized,
        }

    async def _write_artifact(args: dict) -> dict:
        normalized = validate_artifact_args(args)
        ref = get_artifact_store().put(
            normalized["content"],
            kind=normalized["kind"],
            summary=normalized["summary"] or normalized["name"],
            trace_id=get_trace_id(),
            rebuildable=False,
            meta={"name": normalized["name"]},
        )
        return {
            "status": "stored",
            "kind": normalized["kind"],
            "artifact_id": ref.artifact_id,
            "name": normalized["name"],
            "summary": normalized["summary"],
            "size_bytes": ref.size_bytes,
        }

    executor = get_tool_executor()
    executor.register_handler("append_task_note", _append_note)
    executor.register_handler("write_task_artifact", _write_artifact)
    # read_artifact 与写工具一体装（同 TASK_EXEC 开关）；MEMORY_INDEX_ENABLED
    # 单开时由 main.py 直接调 install_artifact_read_tool()。
    install_artifact_read_tool()


def install_artifact_read_tool() -> None:
    """注册 ``read_artifact`` 取回处理器（幂等；两个 flag 任一开即装）。

    - ``TASK_EXEC_ENABLED``：写→读闭环（与写工具一体装）；
    - ``MEMORY_INDEX_ENABLED``（F1b2·M-P1-3）：记忆索引 / 上下文隔离的
      ``obs://`` 指针取回端。
    """
    if not (
        settings.TASK_EXEC_ENABLED
        or bool(getattr(settings, "MEMORY_INDEX_ENABLED", False))
    ):
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.application.services.artifact_store import get_artifact_store
    from agentix.tools.adapters.artifact_read import validate_read_artifact_args
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    async def _read_artifact(args: dict) -> dict:
        # P3（渐进上下文）：写→读闭环的取回端。只读零副作用（L0，免审批）；
        # 未知 ID fail-fast（ValueError → ToolResult 失败，不静默空串）；
        # 正文按 max_chars 确定性截断并提示（防整库倾倒进上下文）。
        # F1b2·M-P1-5：接受 `obs://{artifact_id}` 隔离/索引指针（自动剥离
        # 前缀——记忆索引与 ContextEngine 隔离引用共用本取回端）。
        normalized = validate_read_artifact_args(args)
        artifact_id = normalized["artifact_id"]
        if artifact_id.startswith("obs://"):
            artifact_id = artifact_id[len("obs://") :]
        store = get_artifact_store()
        content = store.get(artifact_id)
        if content is None:
            raise ValueError(
                f"artifact not found: {normalized['artifact_id']}"
            )
        ref = store.ref(artifact_id)
        limit = normalized["max_chars"]
        truncated = len(content) > limit
        body = content[:limit] + ("\n…[artifact truncated]" if truncated else "")
        return {
            "status": "ok",
            "artifact_id": artifact_id,
            "content": body,
            "truncated": truncated,
            "size_bytes": ref.size_bytes if ref else len(content),
            "kind": ref.kind if ref else "",
            "summary": ref.summary if ref else "",
        }

    get_tool_executor().register_handler("read_artifact", _read_artifact)
