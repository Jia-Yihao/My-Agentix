"""P2 ``code_agent`` 图节点（代码型路由的落点，flag ``CODE_EXEC_ENABLED`` 门控）。

节点**无条件注册**进扁平图（phase19 绊线断言 ``route_query`` 的 Literal、
``ROUTE_HANDLERS``、图节点集三者一致），可达性由路由侧 flag 门控——flag-off 时
``route_query`` 不会把任何请求送到这里，行为与基线等价；本函数首行再做一次
防御性降级（belt-and-suspenders，同 ``task_handler`` 风格）。

职责链：取 message → 起草代码（LLM 结构化输出）→ 经 ``ToolExecutor`` 执行
``code_execute``（治理链：权限 → 风险 → HITL → 沙箱档位 → 超时）→ 失败时把
stdout/stderr 回灌下一轮自愈 → 汇总成中文答复 → ``AIMessage`` + ``CodeRouter``
状态回写。

三条边界（都是"不这样写就会出事故"的形状，不是风格选择）：

1. **``result.success is False`` 是终局**。它意味着**治理链**把这次调用挡下了
   （沙箱档位不可用 / 风险被拒 / 未获人工批准 / 参数非法）——这些都不是"代码写错了"，
   再起草一版也不会变好，自愈只会把一次拒绝变成 N 次拒绝。
2. **退出码非 0 / 超时才是"该自愈"**。这两种是代码本身的问题（抛异常 / 死循环），
   把真实输出回灌给模型才有意义。
3. **汇总环节 fail-open**：LLM 汇总失败时回落到原始 stdout（带 warning）——
   汇总是"锦上添花"，而用户真正要的答案（代码的输出）已经在手上了。

提示词常量在本文件（不碰 ``lg_prompts.py``）：它们是**这个节点的局部契约**，
跟着节点走比跟着全局提示词库走更好维护。
"""

from __future__ import annotations

from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from loguru import logger
from pydantic import BaseModel, Field

from agentix.orchestration.langgraph.lg_states import AgentState, CodeRouter, Router
from agentix.tools.adapters.code_exec import (
    CODE_EXEC_TOOL_NAME,
    code_exec_enabled,
    code_exec_max_rounds,
    code_exec_timeout_s,
    validate_code_execute_args,
)

__all__ = ["CodeDraft", "create_code_query"]


#: 起草阶段的系统提示词：把"运行环境的硬约束"写在**给模型看**的地方，而不是只写在
#: 代码里——模型看不到墙内环境，只能靠这段提示词知道它能做什么、不能做什么。
CODE_DRAFT_SYSTEM_PROMPT = """你是代码执行助手：把用户的问题转成一段可直接运行的 Python 代码。

硬性要求（违反任何一条，这次执行都会失败）：
1. 只能使用 Python 标准库——运行环境不安装任何第三方依赖，也不允许联网；
2. 结果必须用 print 输出——工具只取 stdout/stderr，不取函数返回值；
3. 代码在**独立进程**里运行，超时会被连整个进程组一起 SIGKILL 掉，所以不要写死循环、
   不要等待外部输入（stdin 是关闭的）；
4. 用 try/except 包住可能失败的语句，把异常信息 print 出来，别让整段代码直接崩掉；
5. 只输出一段可运行的代码，不要 Markdown 代码块、不要额外解释文字。
"""

#: 自愈轮次的追加提示词：把上一版的真实输出回灌给模型（这是自愈的全部依据）。
CODE_REPAIR_PROMPT = """上一版代码没有跑成功。请根据下面的真实运行结果修正，并重新给出**完整的**代码。

要求同前：只用标准库、必须用 print 输出、try/except 包住异常、只输出一段代码。

【用户问题】
{question}

【上一版代码】
{code}

【运行结果】
exit_code={exit_code} timed_out={timed_out}

stdout:
{stdout}

stderr:
{stderr}
"""

#: 汇总阶段的系统提示词：只做"把输出翻译成人话"，不许编造输出里没有的数值。
CODE_SUMMARY_SYSTEM_PROMPT = """你在把一次代码执行的输出总结成中文答复。

要求：
- 直接给结论，不要复述"我运行了代码"这类过程；
- 输出为空就如实说"没有拿到输出"，不要编；
- 不要写输出里没有的数值或结论；
- 有报错就把报错要点说清楚。
"""

#: 汇总阶段喂给模型的用户消息模板。
CODE_SUMMARY_PROMPT = """【用户问题】
{question}

【代码运行结果】
exit_code={exit_code} timed_out={timed_out}

stdout:
{stdout}

stderr:
{stderr}
"""

#: 回灌给模型前，单段输出的尾部长度上限：整段贴回上下文既贵又容易把模型带偏，
#: 而报错信息的关键部分总在尾部。
FEEDBACK_TAIL_CHARS = 4000


class CodeDraft(BaseModel):
    """起草阶段的结构化输出（``with_structured_output`` 的 schema）。"""

    code: str = Field(description="要执行的 Python 代码正文（只用标准库，必须 print 输出）")
    note: str = Field(default="", description="给这段代码的一句话说明（可选）")


def _message_text(message: Any) -> str:
    """末条用户消息 → 纯文本（多模态 part 列表时拼接 text 部分）。"""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(message or "")


def _tail(text: str, limit: int = FEEDBACK_TAIL_CHARS) -> str:
    """取输出尾部（有界）：回灌给模型的文本必须封顶，否则一次大输出就能把上下文顶爆。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


async def _draft_code(
    question: str,
    *,
    previous_code: str = "",
    stdout: str = "",
    stderr: str = "",
    exit_code: Any = None,
    timed_out: bool = False,
) -> CodeDraft:
    """起草（或按上一轮输出修好）一段代码。

    temperature=0.0 + role="tool"：起草要的是确定性——同一份问题反复起草得到
    同一段代码，自愈才有可比性。
    """
    from agentix.application.agents.llm_factory import get_llm_client

    model = get_llm_client(temperature=0.0, tags=["code"], role="tool")
    messages: List[Any] = [
        SystemMessage(content=CODE_DRAFT_SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]
    if previous_code:
        messages.append(
            HumanMessage(
                content=CODE_REPAIR_PROMPT.format(
                    question=question,
                    code=previous_code,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    stdout=_tail(stdout),
                    stderr=_tail(stderr),
                )
            )
        )
    structured = model.with_structured_output(CodeDraft)
    draft = await structured.ainvoke(messages)
    return draft


async def _summarize(
    question: str,
    *,
    stdout: str,
    stderr: str,
    exit_code: Any,
    timed_out: bool,
) -> str:
    """把一次执行结果汇总成中文答复；**LLM 失败时回落到原始 stdout**（fail-open）。"""
    from agentix.application.agents.llm_factory import get_llm_client

    try:
        model = get_llm_client(temperature=0.3, tags=["code"], role="summarizer")
        response = await model.ainvoke(
            [
                SystemMessage(content=CODE_SUMMARY_SYSTEM_PROMPT),
                HumanMessage(
                    content=CODE_SUMMARY_PROMPT.format(
                        question=question,
                        exit_code=exit_code,
                        timed_out=timed_out,
                        stdout=_tail(stdout),
                        stderr=_tail(stderr),
                    )
                ),
            ]
        )
        text = str(getattr(response, "content", response) or "").strip()
        if text:
            return text
        logger.warning("code summary LLM returned empty content; falling back to raw stdout")
    except Exception as exc:  # noqa: BLE001 - 汇总是辅助环节：失败回落原文，不打挂整条链
        logger.warning("code summary LLM failed; falling back to raw stdout: {}", exc)
    return _raw_output(stdout, stderr)


def _raw_output(stdout: str, stderr: str) -> str:
    """汇总失败时的回落答案：原始输出（stdout 优先，空则 stderr）。"""
    body = (stdout or "").strip()
    if not body:
        body = (stderr or "").strip()
    if not body:
        return "代码执行没有产生任何输出。"
    return body


async def create_code_query(state: AgentState, *, config: RunnableConfig) -> Dict[str, Any]:
    """代码执行节点：起草 → 执行 → 自愈 → 汇总 → 图状态更新（messages/router/sources）。"""
    if not code_exec_enabled():
        # 防御性降级：正常链路不可达（路由侧已门控）；返回 {} = 零状态变更。
        logger.warning("code_agent invoked while CODE_EXEC_ENABLED=false; no-op")
        return {}

    message = _message_text(state.messages[-1].content if state.messages else "")
    max_rounds = code_exec_max_rounds()
    timeout_s = code_exec_timeout_s()
    # 会话号只进日志（图节点的签名要求带 config，这里用它把一次执行钉到会话上，
    # 便于事后按 session 回查是哪一次跑崩了）。
    thread_id = str((config or {}).get("configurable", {}).get("thread_id") or "")

    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()

    code = ""
    stdout = ""
    stderr = ""
    exit_code: Any = None
    timed_out = False
    refusal = ""
    rounds_used = 0
    succeeded = False

    for _round in range(max_rounds):
        rounds_used += 1
        try:
            draft = await _draft_code(
                message,
                previous_code=code,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                timed_out=timed_out,
            )
        except Exception as exc:  # noqa: BLE001 - 起草失败即终止：没有代码就没有可执行的东西
            logger.warning("code draft LLM failed; aborting code execution: {}", exc)
            refusal = f"代码草稿生成失败：{exc}"
            break

        code = (getattr(draft, "code", "") or "").strip()
        if not code:
            refusal = "代码草稿为空，未执行任何代码。"
            break

        # 入参校验放在**执行点之前**：非法入参是"重写一版也救不了"的终局，
        # 在这里挡下来比让它走到执行点再失败更省（也更容易说清楚原因）。
        # 校验器的错误信息不回显代码正文（执行边界 ③）。
        try:
            normalised = validate_code_execute_args({"code": code, "timeout_s": timeout_s})
        except ValueError as exc:
            refusal = str(exc)
            logger.warning("code draft rejected by argument validation: {}", refusal)
            break

        result = await executor.execute(
            CODE_EXEC_TOOL_NAME,
            {"code": normalised["code"], "timeout_s": normalised["timeout_s"]},
        )
        if not result.success:
            # 治理拒绝 = 终局：沙箱不可用 / 风险被拒 / 未获人工批准 / 参数非法——
            # 这些都不是"代码写错了"，再起草一版也不会变好（自愈只会放大拒绝次数）。
            refusal = str(result.error or "未知原因")
            logger.warning("code execution refused by the governance chain: {}", refusal)
            break

        data = result.data if isinstance(result.data, dict) else {}
        exit_code = data.get("exit_code")
        stdout = str(data.get("stdout") or "")
        stderr = str(data.get("stderr") or "")
        timed_out = bool(data.get("timed_out"))
        if exit_code == 0 and not timed_out:
            succeeded = True
            break  # 跑通了：进汇总
        # 退出码非 0 / 超时 ⇒ 代码本身的问题：把真实输出回灌下一轮自愈。

    if refusal:
        answer = f"代码执行未完成：{refusal}"
    else:
        answer = await _summarize(
            message,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
        )

    prev = state.router if isinstance(state.router, Router) else None
    # 三种结局写进 logic：拒绝（治理链）/ 跑通 / 用完轮次仍未跑通——把它们混成
    # 同一个值，下游读状态的人就分不出"没跑成"和"跑成了"。
    outcome = "refused" if refusal else ("ok" if succeeded else "unresolved")
    router = CodeRouter(
        type="code-query",
        logic=f"code_exec:{outcome}:rounds={rounds_used}",
        question=(prev.question if prev else "") or message,
        decision=prev.decision if prev else None,
        confidence=prev.confidence if prev else None,
        reasoning=prev.reasoning if prev else None,
    )

    logger.info(
        "code_agent done: session={} rounds={} exit_code={} timed_out={} refused={}",
        thread_id,
        rounds_used,
        exit_code,
        timed_out,
        bool(refusal),
    )
    return {
        "messages": [AIMessage(content=answer)],
        "router": router,
        "sources": [],
    }
