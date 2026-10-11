"""
capability 内部工具循环：模型 → 工具 → 模型，流式输出最终助手回复。
（M5：剥离 langgraph ReAct 编排，事件时序与原 create_react_agent 路径对齐。）
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Literal, TypedDict

# NotRequired 需要 Python 3.11+ 的 typing；3.10 回退 typing_extensions
try:
    from typing import NotRequired  # type: ignore[attr-defined]  # Python 3.11+
except ImportError:  # Python 3.10
    from typing_extensions import NotRequired  # type: ignore[assignment]

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agentix.retrieval.settings import settings
from agentix.retrieval.usecases.chat_agent_tools import AgentToolContext, build_agent_system_prompt, build_chat_agent_tools

logger = logging.getLogger(__name__)

# 预算超限时的用户提示文案（不暴露内部模型/成本明细）
_BUDGET_INPUT_HINT = "已达到本次对话的输入资源上限，回答可能不完整，请尝试更具体的问题或稍后重试。"
_BUDGET_OUTPUT_HINT = "已达到本次对话的输出资源上限，回答已截断。"
_RECURSION_HINT = "本次问题的工具调用轮次已达上限，请尝试更具体的问题或稍后重试。"


class AgentStreamEvent(TypedDict):
    kind: Literal["tool_running", "answer_delta", "model_usage", "terminated"]
    content: str
    tokens_in: int
    tokens_out: int
    terminate_reason: NotRequired[str]


def _usage_from(value) -> tuple[int, int]:
    usage = getattr(value, "usage_metadata", None) or getattr(value, "response_metadata", {}).get("token_usage", {})
    if not isinstance(usage, dict):
        return 0, 0
    return (
        int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0),
        int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0),
    )


_TOOL_LABELS: dict[str, str] = {
    "count_scope_documents": "正在统计文档数量…",
    "list_scope_documents": "正在列出文档…",
    "lookup_scope_document": "正在查找文档…",
    "query_knowledge_base": "正在检索知识库…",
    "search_documents": "正在搜索文档片段…",
}


def _message_content(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(str(block))
        return "".join(parts)
    return ""


def _tool_call_name(tool_call) -> str:
    if isinstance(tool_call, dict):
        fn = tool_call.get("function") or tool_call.get("name")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(fn or tool_call.get("name") or "")
    fn = getattr(tool_call, "name", None) or getattr(tool_call, "function", None)
    if fn is not None and not isinstance(fn, str):
        return str(getattr(fn, "name", "") or "")
    return str(fn or "")


def _tool_running_label(tool_name: str) -> str:
    return _TOOL_LABELS.get(tool_name, f"正在执行 {tool_name}…")


def _as_chunk(msg) -> AIMessageChunk | None:
    if isinstance(msg, AIMessageChunk):
        return msg
    if not isinstance(msg, AIMessage):
        return None
    kwargs: dict = {
        "content": msg.content,
        "id": msg.id,
        "additional_kwargs": msg.additional_kwargs,
        "response_metadata": msg.response_metadata,
        "usage_metadata": getattr(msg, "usage_metadata", None),
    }
    if getattr(msg, "tool_calls", None):
        kwargs["tool_calls"] = msg.tool_calls
    return AIMessageChunk(**kwargs)


def _tool_running_events(tool_calls, *, announced: set[str]) -> list[AgentStreamEvent]:
    events: list[AgentStreamEvent] = []
    for tc in tool_calls:
        name = _tool_call_name(tc)
        if not name or name in announced:
            continue
        announced.add(name)
        events.append({"kind": "tool_running", "content": _tool_running_label(name)})
    return events


async def _execute_tool_calls(tool_calls, tools_by_name: dict) -> list[ToolMessage]:
    # F1d·A-P1-3（并发工具轮）：>1 走批路径（授权门 → 危险串行 → 输入序
    # 组装）；1（默认）走下方串行原路径，与基线逐位等价。
    concurrency = settings.AGENT_TOOL_ROUND_CONCURRENCY
    if concurrency > 1:
        return await _execute_tool_calls_parallel(tool_calls, tools_by_name, concurrency)
    outputs: list[ToolMessage] = []
    for tc in tool_calls:
        name = _tool_call_name(tc)
        call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
        tool = tools_by_name.get(name)
        if tool is None:
            outputs.append(
                ToolMessage(content=f"工具 {name} 不存在", tool_call_id=call_id, status="error")
            )
            continue
        try:
            result = await tool.ainvoke(tc)
        except Exception as e:
            logger.warning("tool %s failed: %s", name, e)
            outputs.append(
                ToolMessage(content=f"工具执行失败：{e}", tool_call_id=call_id, status="error")
            )
            continue
        if isinstance(result, ToolMessage):
            outputs.append(result)
        else:
            outputs.append(ToolMessage(content=_message_content(result), tool_call_id=call_id))
    return outputs


def _tool_call_args(tool_call) -> dict:
    """解析 tool_call 的入参（供授权门/危险判定用；解析失败回空 dict）。"""
    if not isinstance(tool_call, dict):
        return {}
    raw = None
    fn = tool_call.get("function")
    if isinstance(fn, dict):
        raw = fn.get("arguments")
    if raw is None:
        raw = tool_call.get("arguments") or tool_call.get("args")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json

            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - 解析失败按空参走名称判定
            return {}
        return data if isinstance(data, dict) else {}
    return {}


async def _invoke_tool_call(tc, tools_by_name: dict) -> ToolMessage:
    """单次工具调用（与串行路径同款 invoke/异常/封装语义）。"""
    name = _tool_call_name(tc)
    call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
    tool = tools_by_name.get(name)
    if tool is None:
        return ToolMessage(content=f"工具 {name} 不存在", tool_call_id=call_id, status="error")
    try:
        result = await tool.ainvoke(tc)
    except Exception as e:
        logger.warning("tool %s failed: %s", name, e)
        return ToolMessage(content=f"工具执行失败：{e}", tool_call_id=call_id, status="error")
    if isinstance(result, ToolMessage):
        return result
    return ToolMessage(content=_message_content(result), tool_call_id=call_id)


async def _execute_tool_calls_parallel(
    tool_calls, tools_by_name: dict, concurrency: int
) -> list[ToolMessage]:
    """F1d·A-P1-3 并发轮：派发前逐调用授权门 → 危险强制串行 → safe 并发。

    结果按**输入序**组装（ToolMessage 落位顺序 = 输入顺序，事件序不变）；
    授权门 = ``round_gate.preflight_block``（HUMAN_REVIEW 关闭时零副作用
    恒放行；未获批 fail-closed 落 error ToolMessage、不派发）。
    """
    import asyncio

    # 惰性 import（retrieval→tools 新边，R6 已知；按 result_limiter 先例函数内）
    from agentix.tools.gateway.round_gate import is_dangerous, preflight_block

    outputs: list[ToolMessage | None] = [None] * len(tool_calls)
    dangerous_idx: list[int] = []
    safe_idx: list[int] = []
    for i, tc in enumerate(tool_calls):
        name = _tool_call_name(tc)
        call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
        if tools_by_name.get(name) is None:
            outputs[i] = ToolMessage(
                content=f"工具 {name} 不存在", tool_call_id=call_id, status="error"
            )
            continue
        args = _tool_call_args(tc)
        block = preflight_block(name, args)
        if block:
            outputs[i] = ToolMessage(content=block, tool_call_id=call_id, status="error")
            continue
        (dangerous_idx if is_dangerous(name, args) else safe_idx).append(i)

    # 危险强制串行（输入序；绝不与同批任何调用并发）
    for i in dangerous_idx:
        outputs[i] = await _invoke_tool_call(tool_calls[i], tools_by_name)

    # safe 集按输入序限并发执行
    if safe_idx:
        sem = asyncio.Semaphore(concurrency)

        async def _one(idx: int) -> ToolMessage:
            async with sem:
                return await _invoke_tool_call(tool_calls[idx], tools_by_name)

        done = await asyncio.gather(*(_one(i) for i in safe_idx))
        for idx, out in zip(safe_idx, done):
            outputs[idx] = out

    final: list[ToolMessage] = []
    for i, out in enumerate(outputs):
        if out is None:  # 理论不可达（分派覆盖每个下标）；防御兜底
            tc = tool_calls[i]
            call_id = str(tc.get("id") or "") if isinstance(tc, dict) else ""
            out = ToolMessage(
                content="工具执行失败：并发分派未返回结果",
                tool_call_id=call_id,
                status="error",
            )
        final.append(out)
    return final


def _recursion_events() -> list[AgentStreamEvent]:
    logger.warning(
        "agent hit recursion limit (AGENT_RECURSION_LIMIT=%s), aborting",
        settings.AGENT_RECURSION_LIMIT,
    )
    return [
        {
            "kind": "answer_delta",
            "content": _RECURSION_HINT,
            "tokens_in": 0,
            "tokens_out": 0,
        },
        {
            "kind": "terminated",
            "content": "",
            "tokens_in": 0,
            "tokens_out": 0,
            "terminate_reason": "recursion_limit",
        },
    ]


async def _stream_native_rounds(
    *,
    llm,
    tools: list,
    system_prompt: str,
    user_text: str,
    emit_tool_running: bool,
) -> AsyncGenerator[AgentStreamEvent, None]:
    """
    capability 内部工具循环（复刻原 langgraph ReAct 事件时序）：

    - 尚未跑过工具的轮次：整轮缓冲；无工具时单个 answer_delta 收尾
      （消除旧路径「首轮无工具 → ainvoke 二次调模型」的双调 bug）；
    - 已跑过工具的轮次：文本增量实时流出，工具调用碎片不外发；
    - model_usage 时序对齐旧 updates 滞后语义（工具执行前 / 流式 delta 之后）；
    - 步数（模型步 + 工具步）对照 AGENT_RECURSION_LIMIT，复刻 langgraph 粒度。
    """
    bound = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}
    messages: list = [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
    announced: set[str] = set()
    tools_have_run = False
    steps = 0
    limit = settings.AGENT_RECURSION_LIMIT

    while True:
        if steps + 1 > limit:
            for event in _recursion_events():
                yield event
            return
        steps += 1

        full: AIMessageChunk | None = None
        live = tools_have_run
        async for chunk in bound.astream(messages):
            norm = _as_chunk(chunk)
            if norm is None:
                continue
            full = norm if full is None else full + norm
            if not live or getattr(norm, "tool_call_chunks", None):
                continue
            delta = _message_content(norm)
            if delta:
                tin, tout = _usage_from(norm)
                yield {
                    "kind": "answer_delta",
                    "content": delta,
                    "tokens_in": tin,
                    "tokens_out": tout,
                }

        if full is None:
            logger.warning("model stream produced no chunks")
            return

        tool_calls = list(getattr(full, "tool_calls", None) or [])
        tin, tout = _usage_from(full)

        if tool_calls:
            kwargs: dict = {"content": full.content, "id": full.id, "tool_calls": tool_calls}
            messages.append(AIMessage(**kwargs))
            if tin or tout:
                yield {"kind": "model_usage", "content": "", "tokens_in": tin, "tokens_out": tout}
            if emit_tool_running:
                for event in _tool_running_events(tool_calls, announced=announced):
                    yield event
            if steps + 1 > limit:
                for event in _recursion_events():
                    yield event
                return
            steps += 1
            messages.extend(await _execute_tool_calls(tool_calls, tools_by_name))
            tools_have_run = True
            continue

        if tin or tout:
            yield {"kind": "model_usage", "content": "", "tokens_in": tin, "tokens_out": tout}
        if not live:
            # 缓冲轮收尾：全文单 delta（usage 已单独发出，delta 归零对齐旧 fallback 语义）
            text = _message_content(full)
            if text:
                yield {"kind": "answer_delta", "content": text, "tokens_in": 0, "tokens_out": 0}
        return


async def _ainvoke_rounds(
    *,
    llm,
    tools: list,
    system_prompt: str,
    user_text: str,
) -> tuple[str, int, int]:
    """流失败时的非流式多轮 fallback：跑完工具后返回最终文本与 usage。"""
    bound = llm.bind_tools(tools)
    tools_by_name = {t.name: t for t in tools}
    messages: list = [SystemMessage(content=system_prompt), HumanMessage(content=user_text)]
    steps = 0
    limit = settings.AGENT_RECURSION_LIMIT

    while True:
        steps += 1
        if steps > limit:
            raise RuntimeError(f"fallback hit recursion limit (AGENT_RECURSION_LIMIT={limit})")
        msg = await bound.ainvoke(messages)
        norm = _as_chunk(msg)
        if norm is None:
            return "", 0, 0
        tool_calls = list(getattr(norm, "tool_calls", None) or [])
        tin, tout = _usage_from(norm)
        if not tool_calls:
            return _message_content(norm), tin, tout
        kwargs: dict = {"content": norm.content, "id": norm.id, "tool_calls": tool_calls}
        messages.append(AIMessage(**kwargs))
        messages.extend(await _execute_tool_calls(tool_calls, tools_by_name))


async def run_agent_stream(
    *,
    llm,
    ctx: AgentToolContext,
    question: str,
    history_text: str,
    chat_mode: str = "fast",
    scope_label: str = "",
) -> AsyncGenerator[AgentStreamEvent, None]:
    """
    运行 Agent（capability 内部工具循环，无 langgraph）：
    deep 模式可推 tool_running；answer_delta 流最终答案。
    流失败时 fallback 非流式多轮，整段答案作为单个 answer_delta。
    scope_label 经用户消息独立通道注入（M3.2），不内嵌 system prompt。
    """
    tools = build_chat_agent_tools(ctx)
    system_prompt = build_agent_system_prompt(ctx)

    user_text = question.strip()
    prefix_parts: list[str] = []
    if scope_label:
        prefix_parts.append(f"【当前范围】{scope_label}")
    if history_text:
        prefix_parts.append(f"【历史对话】\n{history_text}")
    if prefix_parts:
        prefix_parts.append(f"【当前问题】\n{user_text}")
        user_text = "\n\n".join(prefix_parts)

    max_in = settings.AGENT_MAX_INPUT_TOKENS
    max_out = settings.AGENT_MAX_OUTPUT_TOKENS
    accumulated_in = 0
    accumulated_out = 0
    terminated_reason: str | None = None

    streamed_answer = False
    try:
        async for event in _stream_native_rounds(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            user_text=user_text,
            emit_tool_running=(chat_mode == "deep"),
        ):
            kind = event.get("kind")
            if kind == "model_usage":
                accumulated_in += int(event.get("tokens_in", 0) or 0)
                accumulated_out += int(event.get("tokens_out", 0) or 0)
                # 输入预算检查（发生在工具执行前/下一轮 model 调用前）
                if max_in > 0 and accumulated_in > max_in:
                    terminated_reason = "budget_exceeded"
                    logger.warning(
                        "agent input budget exceeded: accumulated_in=%s > max=%s",
                        accumulated_in,
                        max_in,
                    )
                    yield {
                        "kind": "terminated",
                        "content": _BUDGET_INPUT_HINT,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "terminate_reason": "budget_exceeded",
                    }
                    break
                continue
            if kind == "answer_delta":
                streamed_answer = True
                accumulated_in += int(event.get("tokens_in", 0) or 0)
                accumulated_out += int(event.get("tokens_out", 0) or 0)
                yield event
                # 输出预算检查（截断保护，已生成内容已发出）
                if max_out > 0 and accumulated_out > max_out:
                    terminated_reason = "budget_exceeded"
                    logger.warning(
                        "agent output budget exceeded: accumulated_out=%s > max=%s",
                        accumulated_out,
                        max_out,
                    )
                    yield {
                        "kind": "terminated",
                        "content": _BUDGET_OUTPUT_HINT,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "terminate_reason": "budget_exceeded",
                    }
                    break
                continue
            yield event
    except Exception as e:
        logger.warning("agent stream failed, fallback to ainvoke: %s", e)

    if not streamed_answer and terminated_reason is None:
        try:
            text, tokens_in, tokens_out = await _ainvoke_rounds(
                llm=llm,
                tools=tools,
                system_prompt=system_prompt,
                user_text=user_text,
            )
            if text:
                yield {
                    "kind": "answer_delta",
                    "content": text,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                }
        except Exception as e:
            logger.exception("agent ainvoke failed: %s", e)
            yield {
                "kind": "answer_delta",
                "content": "处理问题时出错，请稍后重试。",
                "tokens_in": 0,
                "tokens_out": 0,
            }
