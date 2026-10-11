"""A2c 记忆三工具装配（``MEMORY_TOOLS_ENABLED``）。

职责分工（同 ``notify_bridge`` / ``task_tools_bridge`` 模式）：

- **声明**：spec + LLM schema 在 ``capabilities/tools/memory_tools.py``，
  ``settings.MEMORY_TOOLS_ENABLED`` gate（flag-off 不进声明清单）；
- **参数校验**：schema 契约在本模块（缺 content / forget 无匹配键 →
  ValueError → executor 落 failed，诚实不静默）；
- **实现安装**：本模块只装 handler（``ensure_registered()`` +
  ``register_handler``），由 ``main.py::_startup`` 在 flag-on 时调用。

handler 即治理入口：经 ``ToolExecutor.execute`` ⇒ 风险（remember/forget=L1
side-effect）/ 权限 / HITL / 超时闸门全部在前，本模块不提供旁路。
"""

from __future__ import annotations

from agentix.config.settings import settings

__all__ = ["install_memory_tools"]


def _scope_kind(args: dict) -> tuple:
    from agentix.domain.agent.types import MemoryKind, MemoryScope

    scope = MemoryScope(str(args.get("scope") or "user"))
    kind = MemoryKind(str(args.get("kind") or "semantic"))
    return scope, kind


def _subject(args: dict) -> str:
    return str(args.get("subject") or "default").strip() or "default"


def install_memory_tools() -> None:
    """注册 remember/recall/forget handler（幂等；flag-off 直接返回）。"""
    if not bool(getattr(settings, "MEMORY_TOOLS_ENABLED", False)):
        return  # belt-and-suspenders：调用方已 gate，防御直调

    from agentix.application.services.memory_manager import get_memory_manager
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered
    from agentix.domain.agent.types import MemoryItem

    ensure_registered()  # spec 先入 registry（executor.execute 的前置）
    executor = get_tool_executor()

    async def _remember(args: dict) -> dict:
        content = str(args.get("content") or "").strip()
        if not content:
            raise ValueError("remember requires non-empty content")
        scope, kind = _scope_kind(args)
        item = MemoryItem(
            content=content,
            kind=kind,
            scope=scope,
            subject=_subject(args),
            confidence=float(args.get("confidence") or 1.0),
            tags=tuple(str(t) for t in (args.get("tags") or ())),
        )
        item_id = await get_memory_manager().remember(item)
        return {
            "status": "stored",
            "item_id": item_id,
            "scope": scope.value,
            "kind": kind.value,
            "subject": item.subject,
        }

    async def _recall(args: dict) -> dict:
        scope, _ = _scope_kind(args)
        items = await get_memory_manager().recall(
            scope=scope.value,
            subject=_subject(args),
            query=str(args.get("query") or ""),
            limit=int(args.get("limit") or 10),
        )
        payload = [
            {
                "item_id": it.item_id,
                "content": it.content,
                "scope": it.scope.value,
                "kind": it.kind.value,
                "subject": it.subject,
                "confidence": it.confidence,
                "tags": list(it.tags),
            }
            for it in items
        ]
        return {"count": len(payload), "items": payload}

    async def _forget(args: dict) -> dict:
        item_id = str(args.get("item_id") or "").strip()
        content = str(args.get("content") or "").strip()
        if not item_id and not content:
            raise ValueError("forget requires item_id or content (no wildcard delete)")
        scope, _ = _scope_kind(args)
        deleted = await get_memory_manager().forget(
            scope=scope.value,
            subject=_subject(args),
            item_id=item_id,
            content=content,
        )
        return {"status": "deleted" if deleted else "not_found", "deleted": deleted}

    executor.register_handler("remember", _remember)
    executor.register_handler("recall", _recall)
    executor.register_handler("forget", _forget)
