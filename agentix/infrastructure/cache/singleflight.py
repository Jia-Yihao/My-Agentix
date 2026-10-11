"""Phase H·W2：纯 asyncio 在飞合并（SingleFlight）。

同一 key 的并发 miss 只让第一个调用者（leader）真正执行 ``factory``，
其余调用者（waiter）等在同一个 future 上共享其结果——检索 Redis 缓存
击穿收敛的进程内补位（不引入分布式锁；跨进程仍由缓存 TTL/nx 语义兜底）。

实现要点：
- 结果以 ``("ok", value) / ("err", exc)`` 信封写入 future：waiter 按信封
  还原（重抛原异常），避免 ``Future.set_exception`` 下同一异常对象被多个
  waiter 反复持有引发 ``never retrieved`` 告警；
- waiter 经 ``asyncio.shield`` 等待：waiter 被取消不打断 leader 执行，
  leader 被取消（``CancelledError`` 也是 ``BaseException``）以 err 信封
  收场，waiter 侧改抛 ``RuntimeError``（waiter 本身并未被取消，直接重抛
  CancelledError 会让调用方误判自身取消态）；
- ``finally`` 只弹出**自己注册**的 future（同一性校验 = ABA 防护）：
  在飞表不留残项，后续同 key 请求重新起跑。

接线：``retrieve_scoped_docs_cached`` miss 分支在
``RETRIEVAL_SINGLEFLIGHT_ENABLED=true``（默认关）时包一层；flag-off 时
本模块不被导入，检索路径与基线逐位等价。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Tuple, TypeVar

T = TypeVar("T")

_Envelope = Tuple[str, object]


class SingleFlight:
    """按 key 合并并发执行的进程内原语（单事件循环内）。"""

    def __init__(self) -> None:
        self._inflight: Dict[str, "asyncio.Future[_Envelope]"] = {}

    async def do(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        """同 key 并发调用共享一次 ``factory`` 执行（成功值或异常）。"""
        existing = self._inflight.get(key)
        if existing is not None:
            kind, payload = await asyncio.shield(existing)
            if kind == "err":
                if isinstance(payload, asyncio.CancelledError):
                    raise RuntimeError(
                        "singleflight leader was cancelled"
                    ) from payload
                raise payload  # type: ignore[misc]
            return payload  # type: ignore[return-value]

        fut: "asyncio.Future[_Envelope]" = (
            asyncio.get_running_loop().create_future()
        )
        self._inflight[key] = fut
        try:
            value = await factory()
        except BaseException as exc:
            if not fut.done():
                fut.set_result(("err", exc))
            raise
        else:
            if not fut.done():
                fut.set_result(("ok", value))
            return value
        finally:
            if self._inflight.get(key) is fut:
                del self._inflight[key]

    @property
    def inflight_count(self) -> int:
        """当前在飞 key 数（观测/测试用）。"""
        return len(self._inflight)


_default_singleflight = SingleFlight()


def get_singleflight() -> SingleFlight:
    """进程内单例（asyncio 上下文使用）。"""
    return _default_singleflight


def reset_singleflight() -> None:
    """清空在飞表（仅供测试隔离）。"""
    _default_singleflight._inflight.clear()
