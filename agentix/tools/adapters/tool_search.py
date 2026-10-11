"""P2 ``tool_search``：延迟工具召回（借鉴 claw-code ToolSearch，书 §4.7 第 2 层）。

机制：``TOOL_DEFERRED_TOOLS`` 非空时——
- ``registry_init`` 把本工具挂进声明清单（flag-off/空列表 = 不声明，与基线逐位等价）；
- ``main._startup`` 调 ``install_tool_search_tool()`` 装配 handler；
- ``get_active_tools()`` 把延迟工具剔出**常驻** schema（注册表 spec 保留）。

召回面 = 声明清单**全集（含延迟工具）**——召回的意义正是把不在常驻列表里的
工具找回来；排序 = ``selection.score_tool_match``（确定性、无 I/O）。
返回值是纯 JSON（含入参 schema），供 LLM 直接看参数签名再决定调用。
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from agentix.tools.definitions import ToolSpec

__all__ = [
    "tool_search",
    "build_tool_search_spec",
    "search_declarations",
    "install_tool_search_tool",
]


class tool_search(BaseModel):
    """``tool_search`` 入参（类名即工具名：schema title 必须等于注册名，
    否则 ``selection._spec_for_schema`` 的 title 回查会静默 miss——
    按 kg_tools_list 的既有约定命名，而非 CamelCase）。"""

    query: str = Field(description="查询（自然语言或关键词，如「法条查询」「统计报表」）")
    top_k: int = Field(default=8, ge=1, le=20, description="返回条数上限")


def build_tool_search_spec() -> ToolSpec:
    """声明 ``tool_search``（L0 只读、零副作用；仅延迟列表非空时入清单）。"""
    return ToolSpec(
        name="tool_search",
        risk_level="L0",
        description=(
            "按查询召回可用工具清单（按名字/描述相关度排序，返回工具名、描述、"
            "风险级与入参 schema）。\n"
            "何时用：需要发现或确认可用工具（尤其是未出现在常驻工具列表中的"
            "延迟工具）；不确定该用哪个工具时先检索再调用。\n"
            "何时不能用：目标工具已在常驻列表且 schema 已知（直接调用即可，"
            "不必再搜）；需要执行工具（本工具只检索不执行）。\n"
            "注意：只读、零副作用；检索范围是声明清单全集（含延迟工具）。"
        ),
        parameters=tool_search.model_json_schema(),
        capabilities=("discovery",),
    )


def search_declarations(query: str, *, top_k: int = 8) -> Dict[str, Any]:
    """对声明清单全集按 query 排序召回（含延迟工具；确定性、无 I/O）。

    空查询/零分按声明序返回前 ``top_k``（fail-open：召回层不静默丢工具，
    与 ``select_tool_schemas`` 的既有纪律一致）。
    """
    from agentix.tools.registry_init import (  # noqa: PLC0415
        deferred_tool_names,
        get_declarations,
    )
    from agentix.tools.selection import score_tool_match  # noqa: PLC0415

    scored: list[tuple[float, int, Any, type[BaseModel]]] = []
    for idx, (spec, schema_cls) in enumerate(get_declarations()):
        score = score_tool_match(spec.name, spec.description, query)
        scored.append((-score, idx, spec, schema_cls))
    scored.sort(key=lambda item: (item[0], item[1]))

    deferred = deferred_tool_names()
    results = [
        {
            "name": spec.name,
            "description": spec.description,
            "risk_level": spec.risk_level,
            "deferred": spec.name in deferred,
            "schema": schema_cls.model_json_schema(),
        }
        for _, _, spec, schema_cls in scored[: max(0, top_k)]
    ]
    return {"query": query, "count": len(results), "results": results}


def install_tool_search_tool() -> None:
    """组合根装配（``main._startup`` 在延迟列表非空时调用；幂等）。

    非空检查在此**再做一次**（双 flag gate 纪律）：声明清单与 handler 的
    可见性必须同源——延迟列表变空后本函数直接 no-op，不残留 handler。
    """
    from agentix.tools.registry_init import (  # noqa: PLC0415
        deferred_tool_names,
        ensure_registered,
    )

    if not deferred_tool_names():
        return
    ensure_registered()

    def _handler(args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or "")
        top_k = int(args.get("top_k") or 8)
        return search_declarations(query, top_k=max(1, min(top_k, 20)))

    from agentix.tools.gateway.executor import get_tool_executor  # noqa: PLC0415

    get_tool_executor().register_handler("tool_search", _handler)
