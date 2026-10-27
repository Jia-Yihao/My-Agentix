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
LG-1-2 把 ``retry_on`` 换成业务分类 ``is_retryable``（唯一真源）。接线仍归属：
LG-3-2（节点级 ``timeout``）、LG-3-5（``error_handler`` 补偿）。
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import RetryPolicy, TimeoutPolicy

from agentix.domain.policies.retry import is_retryable

__all__ = [
    "NATIVE_RETRY_DEFAULTS",
    "POLICY_KWARG_NAMES",
    "LEGACY_POLICY_KWARG_NAMES",
    "readonly_retry_policy",
    "node_timeout",
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
