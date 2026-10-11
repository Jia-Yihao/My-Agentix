"""
Logging utilities centralised for the application.
"""

import sys
from typing import Optional

from loguru import logger

from agentix.domain.tracing import get_trace_id

# P2-2：默认格式显式输出 ``service``（bind 的服务名）与 ``trace_id``（当前请求
# 的追踪 id），否则这两项在 ``serialize=False`` 下会被整段丢弃，日志无法按请求
# 关联。占位符统一写成 ``{extra[...]}``——由 ``_inject_context`` patcher 在**每条
# 记录发出时**注入。
DEFAULT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[service]}</cyan> | "
    "<cyan>{extra[trace_id]}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _inject_context(record) -> None:
    """loguru patcher：在**每条记录发出时**读取当前上下文并注入 extra。

    关键约束：trace_id 必须在 record 发出那一刻读取，而不是在
    ``logger.add`` 构造期绑定一次——构造期绑定会导致 trace_id 在请求/线程
    之间串味（后一个请求的 id 覆盖前一个）。patcher 对每个 record 单独求值，
    因此每条日志携带的是「产生它的那个请求」的 id。

    ``service`` 用 ``setdefault``：保留 ``get_logger(...).bind(service=...)``
    已绑定的值；未绑定的普通 ``logger.info`` 回退为 ``"-"``，避免 KeyError。
    """
    record["extra"]["trace_id"] = get_trace_id()
    record["extra"].setdefault("service", "-")


def configure_logging(
    *,
    level: Optional[str] = None,
    debug: bool = False,
    log_format: Optional[str] = None,
    serialize: bool = False,
) -> None:
    """
    Configure the global Loguru logger with a consistent format.
    """
    resolved_level = level or ("DEBUG" if debug else "INFO")
    logger.remove()
    # 注意：loguru 0.7.x 的 ``logger.add`` **不接受** patcher 关键字（它只转发
    # sink 专属参数）；全局 patcher 必须经 ``logger.configure(patcher=...)`` 设置，
    # 之后对所有 handler 生效。patcher 在**每条记录发出时**被调用。
    logger.configure(patcher=_inject_context)
    logger.add(
        sys.stderr,
        format=log_format or DEFAULT_FORMAT,
        level=resolved_level,
        diagnose=debug,
        serialize=serialize,
    )


def get_logger(*, service: str, level: Optional[str] = None):
    """
    Return a logger bound to a specific service name.
    """
    bound_logger = logger.bind(service=service)
    if level:
        bound_logger = bound_logger.opt()
    return bound_logger
