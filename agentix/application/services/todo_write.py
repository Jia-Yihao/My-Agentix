"""F1c·A-P1-2：TodoWrite 的 store + handler + 装配（application 组合根）。

- 声明在 ``tools/adapters/todo_write.py``（``registry_init`` 在
  ``AGENT_LLM_PLANNER_ENABLED=true`` 时挂进声明清单）；本模块负责
  ``TodoStore``（JSON 落盘、原子替换、损坏容忍）与 handler 装配。
- **单清单语义**（Codex/Claude TodoWrite 同款）：``todo_write`` 整体替换
  当前清单；按文件恢复（进程重启可读回，spec「持久化可恢复」）。
- **fail-open 纪律**：落盘失败只告警、内存态保留（更新绝不因磁盘故障
  丢失）；文件损坏时按空清单起步（不抛、不锁死）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from loguru import logger

from agentix.config.settings import settings

__all__ = [
    "TodoStore",
    "todo_write_handler",
    "install_todo_write_tool",
    "get_todo_store",
    "set_todo_store",
    "TODO_STATUSES",
    "MAX_TODO_ITEMS",
    "MAX_TODO_CONTENT",
    "DEFAULT_TODO_PATH",
]

#: 状态词表（与 adapters/todo_write.py 的 schema 同源——handler 对直连
#: 调用（绕过 LLM schema 校验）做同等把关）。
TODO_STATUSES = ("pending", "in_progress", "completed")

#: 清单条数上限（防无界膨胀进提示词）。
MAX_TODO_ITEMS = 50

#: 单条内容上限（与 schema MAX_TODO_CONTENT 同值）。
MAX_TODO_CONTENT = 500

#: 默认落盘位置（相对 CWD，与 ``data/agentix.db`` 同族；测试注入 tmp 路径）。
DEFAULT_TODO_PATH = Path("data") / "todos" / "current.json"

_store: Optional["TodoStore"] = None


class TodoStore:
    """单清单 JSON store：内存态 + 原子落盘 + 损坏容忍加载。"""

    def __init__(self, path: Optional[Union[Path, str]] = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_TODO_PATH
        self._items: List[Dict[str, str]] = []
        self._version = 0
        self._load()

    # ---------- 校验 ----------

    @staticmethod
    def _validate(items: Any) -> List[Dict[str, str]]:
        if items is None:
            items = []
        if not isinstance(items, (list, tuple)):
            raise ValueError("items must be a list of todo objects")
        if len(items) > MAX_TODO_ITEMS:
            raise ValueError(f"too many todo items ({len(items)} > {MAX_TODO_ITEMS})")
        cleaned: List[Dict[str, str]] = []
        in_progress_seen = False
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError("todo item must be an object with content/status")
            content = str(raw.get("content") or "").strip()
            status = str(raw.get("status") or "").strip()
            if not content:
                raise ValueError("todo item content must be non-empty")
            if len(content) > MAX_TODO_CONTENT:
                raise ValueError(
                    f"todo item content too long ({len(content)} > {MAX_TODO_CONTENT})"
                )
            if status not in TODO_STATUSES:
                raise ValueError(
                    f"invalid todo status {status!r}; "
                    f"must be one of {', '.join(TODO_STATUSES)}"
                )
            if status == "in_progress":
                if in_progress_seen:
                    raise ValueError("only one todo item may be in_progress")
                in_progress_seen = True
            cleaned.append({"content": content, "status": status})
        return cleaned

    # ---------- IO ----------

    def _load(self) -> None:
        """按文件恢复（损坏/缺失 ⇒ 空清单起步，fail-open）。"""
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw_items = data.get("items", [])
            if not isinstance(raw_items, list):
                raise ValueError("items is not a list")
            self._items = self._validate(raw_items)
            self._version = max(0, int(data.get("version") or 0))
        except Exception as exc:  # noqa: BLE001 - 损坏容忍：按空清单起步
            logger.warning("todo store load failed (start empty): {}", exc)
            self._items = []
            self._version = 0

    def _persist(self) -> None:
        """原子替换写盘（tmp + os.replace）；失败只告警、内存态保留。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"items": self._items, "version": self._version},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception as exc:  # noqa: BLE001 - 落盘失败绝不丢内存态
            logger.warning("todo store persist failed (memory kept): {}", exc)

    # ---------- API ----------

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> Dict[str, Any]:
        return {
            "items": [dict(item) for item in self._items],
            "version": self._version,
        }

    def replace(self, items: Any) -> Dict[str, Any]:
        """整体替换（校验失败抛 ``ValueError`` ⇒ executor 落诚实错误结果）。"""
        cleaned = self._validate(items)
        self._items = cleaned
        self._version += 1
        self._persist()
        return self.snapshot()


def get_todo_store() -> TodoStore:
    """进程级单例（首次访问懒加载默认路径）。"""
    global _store
    if _store is None:
        _store = TodoStore()
    return _store


def set_todo_store(store: Optional[TodoStore]) -> None:
    """替换/清空单例（测试与组合根注入用；``None`` = 下次访问重建默认）。"""
    global _store
    _store = store


async def todo_write_handler(args: dict) -> Dict[str, Any]:
    """``todo_write`` handler：整体替换 + 回执当前清单（回执即读通道）。"""
    if not isinstance(args, dict) or "items" not in args:
        raise ValueError("items is required (pass [] to clear the list)")
    snapshot = get_todo_store().replace(args.get("items"))
    return {
        "ok": True,
        "items": snapshot["items"],
        "version": snapshot["version"],
    }


def install_todo_write_tool() -> None:
    """把 ``todo_write`` handler 挂上 ToolExecutor（幂等；组合根装配）。"""
    if not getattr(settings, "AGENT_LLM_PLANNER_ENABLED", False):
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）
    executor = get_tool_executor()
    if not executor.has_handler("todo_write"):
        executor.register_handler("todo_write", todo_write_handler)
