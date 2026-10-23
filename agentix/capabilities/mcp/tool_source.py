"""MCP 工具源：把「可被 MCP 暴露的工具」抽象成统一接口。

三档来源：
- :class:`StaticToolSource`——手工注册的最小工具集（内置离线工具基类）；
- :class:`OfflineToolSource`——零外部依赖的内置工具（calculator /
  text_stats），MCP server 的默认源，子进程/CI 均可跑；
- :class:`RegistryToolSource`——桥接既有 :class:`ToolRegistry` +
  :class:`ToolExecutor`：把声明式 registry 里的 **L0 只读**工具导出为
  MCP 工具，调用走 Executor 的统一治理（超时/风险/权限/轨迹）。

工具定义遵循 MCP 规范：``{name, description, inputSchema}``，
``inputSchema`` 为 JSON Schema——与 ToolSpec 的 OpenAI function parameters
同构，直接复用，不重复建模。
"""

from __future__ import annotations

import ast
import asyncio
from typing import Any, Awaitable, Callable, Dict, List

from agentix.tools.definitions import ToolSpec
from agentix.tools.gateway.executor import ToolExecutor
from agentix.capabilities.mcp.protocol import INVALID_PARAMS, MCPError

__all__ = [
    "ToolHandler",
    "StaticToolSource",
    "OfflineToolSource",
    "RegistryToolSource",
]

#: ``handler(args: dict) -> Any``（返回值须 JSON 可序列化）
ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]] | Callable[[Dict[str, Any]], Any]


class StaticToolSource:
    """手工注册工具集（name → 定义 + handler）。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, ToolHandler] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: ToolHandler,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"tool '{name}' already registered")
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
        }
        self._handlers[name] = handler

    def list_tools(self) -> List[Dict[str, Any]]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            raise MCPError(INVALID_PARAMS, f"unknown tool: {name}")
        result = handler(dict(arguments or {}))
        if asyncio.iscoroutine(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# 内置离线工具
# ---------------------------------------------------------------------------

_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)

MAX_EXPRESSION_CHARS = 200
MAX_POW_EXPONENT = 64
MAX_INT_BITS = 512
MAX_FLOAT_ABS = 1e308


def _guard(value: float, operation: str) -> float:
    """白名单挡语法不挡资源：字面量再大也只是内存，一次 ** 就能算到天荒地老。"""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{operation} produced a non-finite result")
        if abs(value) > MAX_FLOAT_ABS:
            raise ValueError(f"{operation} result out of range")
        return value
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise ValueError(f"{operation} result has too many bits")
    return value


def _safe_eval(node: ast.AST) -> float:
    """AST 白名单求值（只允许数字字面量与一元正负号；二元运算由 ev 处理）。"""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, float) and (
            node.value != node.value or node.value in (float("inf"), float("-inf"))
        ):
            raise ValueError("literal must be finite")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
        operand = _safe_eval(node.operand)
        return -operand if isinstance(node.op, ast.USub) else operand
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _calc(args: Dict[str, Any]) -> Dict[str, Any]:
    expr = str(args.get("expression", "")).strip()
    if not expr:
        raise ValueError("expression is required")
    if len(expr) > MAX_EXPRESSION_CHARS:
        raise ValueError(f"expression longer than {MAX_EXPRESSION_CHARS} chars")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression: {exc}") from exc

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, _ALLOWED_UNARYOPS):
            operand = ev(n.operand)
            return -operand if isinstance(n.op, ast.USub) else operand
        if isinstance(n, ast.BinOp):
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return _guard(left + right, "+")
            if isinstance(n.op, ast.Sub):
                return _guard(left - right, "-")
            if isinstance(n.op, ast.Mult):
                return _guard(left * right, "*")
            if isinstance(n.op, ast.Div):
                return _guard(left / right, "/")
            if isinstance(n.op, ast.FloorDiv):
                return _guard(left // right, "//")
            if isinstance(n.op, ast.Mod):
                return _guard(left % right, "%")
            if isinstance(n.op, ast.Pow):
                if abs(right) > MAX_POW_EXPONENT:
                    raise ValueError(f"exponent magnitude exceeds {MAX_POW_EXPONENT}")
                return _guard(left**right, "**")
        return _safe_eval(n)

    value = ev(tree)
    return {"expression": expr, "value": value}


def _text_stats(args: Dict[str, Any]) -> Dict[str, Any]:
    text = args.get("text")
    if not isinstance(text, str):
        raise ValueError("text (string) is required")
    return {
        "chars": len(text),
        "words": len(text.split()),
        "lines": len(text.splitlines()) or (1 if text else 0),
    }


class OfflineToolSource(StaticToolSource):
    """零外部依赖内置工具——MCP server 默认源（子进程/CI 可跑）。"""

    def __init__(self) -> None:
        super().__init__()
        self.register(
            "calculator",
            "Evaluate an arithmetic expression (numbers, + - * / // % ** and parentheses).",
            {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            _calc,
        )
        self.register(
            "text_stats",
            "Count characters / whitespace-split words / lines of a text.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            _text_stats,
        )


# ---------------------------------------------------------------------------
# 既有 ToolRegistry 桥接
# ---------------------------------------------------------------------------

class RegistryToolSource:
    """把 ToolRegistry 中 **L0 只读**工具经 ToolExecutor 导出为 MCP 工具。

    安全边界：
    - 只导出 ``risk_level == "L0"``——MCP 传输层不做人工审批，
      L1+（可预览/需确认）必须留在进程内 HITL 链路；
    - 调用走 :meth:`ToolExecutor.execute` 的完整治理链
      （超时/重试/权限/轨迹），MCP 不构成治理旁路；
    - 可传 ``allow`` 白名单进一步收窄暴露面（默认全量 L0）。
    """

    def __init__(self, executor: ToolExecutor, *, allow: List[str] | None = None) -> None:
        from agentix.tools.definitions import registry as tool_registry

        self._executor = executor
        self._allow = set(allow) if allow is not None else None
        self._registry = tool_registry

    def _exportable(self) -> List[ToolSpec]:
        out = []
        for spec in self._registry.get_specs():
            if spec.risk_level != "L0":
                continue
            if self._allow is not None and spec.name not in self._allow:
                continue
            out.append(spec)
        return out

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.parameters
                or {"type": "object", "properties": {}},
            }
            for spec in self._exportable()
        ]

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        result = await self._executor.execute(name, arguments)
        # ToolResult 统一协议直接映射为 MCP 载荷：成功 data / 失败 isError
        if result.success:
            return {"tool": result.tool_name, "data": result.data, "citations": result.citations}
        raise MCPError(
            INVALID_PARAMS if "unknown tool" in (result.error or "") else -32000,
            result.error or "tool execution failed",
            data={"retryable": result.retryable, "metadata": result.metadata},
        )
