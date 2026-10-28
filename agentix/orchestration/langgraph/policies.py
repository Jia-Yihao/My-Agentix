"""LangGraph 节点策略的**唯一构造点**（LG-0-3 · 策略集中）。

## 为什么要有这个模块

``add_node`` 的策略形参在 1.x 叫 ``retry_policy`` / ``cache_policy`` /
``error_handler`` / ``timeout``（0.2 线的 ``retry=`` 已废）。一旦各注册点各自
``RetryPolicy(...)`` 内联构造，"哪个节点重试几次、退避多久"就散成 N 份字面量：
改一次要同步 N 处，而漏掉的那处不会报错，只会静默按旧值跑——这正是
``AGENTS.md`` 纪律 6 ④ 说的"同一个判定不许两个源"。集中到本模块后，
注册处只点名策略、不描述策略，守卫
（``tests/test_p147_policy_centralization.py``）据"实参来源"判定违规。

## 与仓内同名符号的区分

``agentix/retrieval/infra/tasks/reliable.py`` 里另有一个**自研**
``RetryPolicy``（检索索引批次的退避配置，与 LangGraph 无关，消费者是
``index_worker.py`` 与 ``scripts/migration/backfill_vectors.py``）。本模块只产出
LangGraph 的策略对象；守卫按"该名字是不是从 ``langgraph*`` 导入的"消歧，
不会把那个自研类算作违规。

## 本轮的边界

LG-0-3 只建集中点；LG-1-1 把 ``readonly_retry_policy`` 接到 6 个只读节点；
LG-1-2 把 ``retry_on`` 换成业务分类 ``is_retryable``（唯一真源）；LG-1-3 把三层
自研退避的数值与原生默认对齐（真源在 ``agentix.domain.policies.retry``，本模块
只转出——领域层不能反向依赖 LangGraph）。LG-3-2 已把 ``node_timeout`` 接到主图
全部 10 个节点（开关 ``NODE_TIMEOUT_SECONDS``，为 0 时完全不传 ``timeout``）。
LG-3-5 的 ``node_error_handler`` 只给**有副作用**的节点（``execute_task`` /
``code_agent``）注册：它对失败只做补偿与收尾，**不重跑节点**，因此与 LG-1-1
「这两个节点不挂 retry_policy」的铁律不冲突。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Awaitable, Callable, Literal

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import NodeError
from langgraph.graph import END
from langgraph.types import Command, RetryPolicy, TimeoutPolicy
from loguru import logger

from agentix.domain.policies.retry import (
    RETRY_BACKOFF_FACTOR,
    RETRY_INITIAL_INTERVAL,
    RETRY_JITTER,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_INTERVAL,
    RetryBackoff,
    is_retryable,
)

__all__ = [
    "NATIVE_RETRY_DEFAULTS",
    "POLICY_KWARG_NAMES",
    "LEGACY_POLICY_KWARG_NAMES",
    "RetryBackoff",
    "RETRY_INITIAL_INTERVAL",
    "RETRY_BACKOFF_FACTOR",
    "RETRY_MAX_INTERVAL",
    "RETRY_MAX_ATTEMPTS",
    "RETRY_JITTER",
    "readonly_retry_policy",
    "node_timeout",
    "node_error_handler",
    "NODE_ERROR_RECORD_DECIDED_BY",
]

#: 原生默认快照。LG-1-3 要"三层自研退避与原生默认同源"，比对基准就是它——
#: 直接从 ``RetryPolicy`` 的默认值取，不另抄一份数字（抄本会随框架升级失真）。
NATIVE_RETRY_DEFAULTS = RetryPolicy()

#: ``add_node`` 里属于"策略"的形参名（1.x 口径）。守卫只检查这些形参的实参来源。
POLICY_KWARG_NAMES = frozenset({"retry_policy", "cache_policy", "error_handler", "timeout"})

#: 0.2 线的旧形参名。1.x 里它们不再是 ``add_node`` 的形参，写出来会被
#: ``**kwargs`` 吞掉或报错，属"看着能跑其实没生效"的坑，守卫一律判违规。
LEGACY_POLICY_KWARG_NAMES = frozenset({"retry"})


def readonly_retry_policy(**overrides: Any) -> RetryPolicy:
    """只读节点用的重试策略。

    **数值**默认值全部来自 ``RetryPolicy`` 自身（``initial_interval=0.5`` /
    ``backoff_factor=2.0`` / ``max_interval=128.0`` / ``max_attempts=3`` /
    ``jitter=True``）——本函数刻意**不**填数字，这样"与原生默认同源"是由构造
    保证的，而不是靠注释里的约定。

    **唯一与原生默认不同的是 ``retry_on``**（LG-1-2）：接业务分类
    ``agentix.domain.policies.retry.is_retryable``，它是全仓"可重试"的唯一真源。
    原生 ``default_retry_on`` 与业务口径实测有三处漂移：`httpx.HTTPStatusError(429)`
    原生**不重试**而业务判可重试；`openai.AuthenticationError`（401）原生会重试
    3 次而业务判不可重试；未知异常原生"未知即重试"而业务判不可重试（重试换不来
    一个缺陷的修复）。漂移用例见 ``tests/test_p149_retry_on_business.py``。

    ⚠️ 只给**只读**节点。``execute_task`` 与 ``code_agent`` 不得接：整节点重跑会
    重放工具副作用（指导文档铁律，也是 LG-1-1 验收第 ② 条）。
    """
    overrides.setdefault("retry_on", is_retryable)
    return RetryPolicy(**overrides)


def node_timeout(
    run_timeout: float | None = None,
    idle_timeout: float | None = None,
    refresh_on: Literal["auto", "heartbeat"] = "auto",
) -> TimeoutPolicy | None:
    """节点级超时策略；**未启用时返回 ``None``**。

    返回 ``None`` 是接口的一部分：LG-3-2 的口径是"开关为 0 时**完全不传**
    ``timeout`` 形参"，而不是传 ``None``。调用方据此 ``if (policy := node_timeout(...))``
    决定是否带上实参，flag-off 路径才能与基线逐位等价（纪律 1）。

    非正值（``<= 0``）一律视为"关"⇒ ``None``：配置里的 ``0`` 是默认关的取值，
    不该在框架侧变成另一个语义。

    边界说明（``AGENTS.md`` 纪律 6 ②，LG-3-2 验收第 ③ 条会把它钉在用例里）：
    超时依赖 asyncio 协作式取消——同步阻塞、不释放事件循环的节点**杀不掉**。
    """
    if (run_timeout is None or run_timeout <= 0) and (idle_timeout is None or idle_timeout <= 0):
        return None
    return TimeoutPolicy(
        run_timeout=run_timeout if run_timeout and run_timeout > 0 else None,
        idle_timeout=idle_timeout if idle_timeout and idle_timeout > 0 else None,
        refresh_on=refresh_on,
    )


#: 补偿记录的 ``decided_by`` 署名。与人工裁决区分开：运维按这一列就能筛出
#: 「系统自动闭合的失败记录」，不会和真正被人拒掉的审批混在一起。
NODE_ERROR_RECORD_DECIDED_BY = "system:node_error_handler"


def _compensation_request_id(thread_id: str, node: str, exc: BaseException) -> str:
    """补偿记录的主键：同一 (线程, 节点, 异常) 收敛到同一行（``close_decision`` upsert）。

    不取随机 uuid：节点失败会被重入（恢复、重试、同一轮重放）重复触发，
    每次生成新行会把真源表变成事件流水，而本表的口径是**一条失败一条记录**。
    """
    seed = f"{thread_id}|{node}|{type(exc).__name__}|{exc}"
    return "nodeerr:" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]


def _write_compensation_record(store: Any, *, thread_id: str, node: str, exc: BaseException) -> str:
    """把失败写成 ``hitl_pending_approvals`` 里一条**已闭合**的记录（Q7 真源复用）。

    为什么用 ``close_decision(rejected)`` 而不是 ``save_pending``：``pending``
    行的语义是「等人裁决」，启动恢复会把它们重新装配成审批请求
    （``hitl_bridge._apply_loaded_pending``）——一条节点失败记录混进去就会变成
    幽灵审批。``rejected`` 是既有词表里唯一的「未执行即闭合」态，配上
    ``decided_by=system:node_error_handler`` 与 ``decision_reason`` 里的原因，
    既留在真源表里可查，又不会被恢复链复活。

    ``declared_risk_level`` 填 ``L0``：这一列描述的是工具声明级，节点失败没有
    工具声明可抄；拿它冒充风险等级会让审批统计失真。
    """
    sid = _compensation_request_id(thread_id, node, exc)
    store.close_decision(
        sid,
        status="rejected",
        decided_by=NODE_ERROR_RECORD_DECIDED_BY,
        reason=f"{node} failed: {type(exc).__name__}: {exc}",
        sidecar={
            "request_id": sid,
            "tool_name": node,
            "arguments": {
                "node": node,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
            "declared_risk_level": "L0",
            "created_at": time.time(),
        },
        thread_id=thread_id,
    )
    return sid


def node_error_handler(
    *,
    store: Any = None,
    exit_node: str = END,
) -> Callable[..., Awaitable[Command]]:
    """节点级错误补偿 handler 工厂（LG-3-5，langgraph 1.x ``add_node(error_handler=)``）。

    框架在节点**重试耗尽**（无 ``retry_policy`` 时即首次失败）且异常不是
    ``GraphBubbleUp`` 时把该异常交给这里：失败上下文以 ``NodeError`` 注入，
    入口 state 是失败节点拿到的那份输入。本 handler 只做两件事：

    1. 写补偿记录（复用 ``hitl_pending_approvals`` 真源，见
       ``_write_compensation_record``）；写库失败按纪律 2 fail-open——记
       ``logger.warning`` 后继续收尾，**绝不让补偿动作本身把整轮再打挂**
       （handler 自己抛错 = 整轮失败，框架不做二次兜底）。
    2. 返回 ``Command``：往 ``messages`` 追加一条如实的失败回执，并路由到
       ``exit_node``（默认 ``END``）——明确的失败出口，而不是整轮 500。

    ``store`` 注入只给测试（sqlite 内存）；生产缺省惰性解析共享 engine，
    构造 handler 不连库、不建表。

    **为什么不在这里重跑节点**：``execute_task`` / ``code_agent`` 整节点重跑
    会重放工具副作用（LG-1-1 铁律）。补偿 = 记录 + 收尾，不是「再试一次」。
    """
    resolved_store = store

    async def _handle(state: Any, *, error: NodeError, config: RunnableConfig) -> Command:
        node = str(getattr(error, "node", "") or "unknown")
        exc = getattr(error, "error", None)
        if not isinstance(exc, BaseException):
            exc = RuntimeError(f"{node} failed with non-exception payload {exc!r}")
        configurable = config.get("configurable") if isinstance(config, dict) else None
        thread_id = str((configurable or {}).get("thread_id") or "")
        recorded = True
        try:
            target_store = resolved_store
            if target_store is None:
                from agentix.infrastructure.persistence.hitl_pending_store import (
                    build_default_hitl_pending_store,
                )

                target_store = build_default_hitl_pending_store()
            # 同步 SQLAlchemy I/O 不下放线程就会卡住事件循环（AGENTS.md 代码风格）。
            await asyncio.to_thread(
                _write_compensation_record,
                target_store,
                thread_id=thread_id,
                node=node,
                exc=exc,
            )
        except Exception as store_exc:  # noqa: BLE001 - 纪律 2：补偿记录写失败只降级为告警，失败收尾照常
            recorded = False
            logger.warning(
                "node_error_handler: compensation record write failed "
                "(node={}, thread={}): {}: {}",
                node,
                thread_id or "-",
                type(store_exc).__name__,
                store_exc,
            )
        record_note = (
            "已记录补偿项，可稍后重试；若持续失败请联系管理员查看 nodeerr 记录。"
            if recorded
            else "补偿记录写入失败（已告警），请把本轮时间与节点一并告知管理员。"
        )
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=(
                            f"本轮未能完成：内部节点 {node} 执行失败"
                            f"（{type(exc).__name__}）。{record_note}"
                        )
                    )
                ]
            },
            goto=exit_node,
        )

    return _handle
