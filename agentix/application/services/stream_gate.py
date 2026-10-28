"""Phase H · W4：SSE 出口门控——token 帧合并 + 有界队列背压。

挂载点是 **HTTP 出口**（``interfaces/http/v1/chat.py::event_stream`` 与
``retrieval/api/routers/chat.py::sse_generator``），service 层帧语义、
seq/run_id 戳记、落库/终态收口逻辑全部不感知（flag 关时逐位等价）。

- ``SSE_COALESCE_ENABLED``：token 帧（``message``/``answer_chunk``/
  ``thinking_chunk`` 且非终态）按 50ms 窗口合并为一帧 delta（content 拼接，
  其余字段取尾帧——seq/run_id/会话元数据天然保序）。**到期检查式（拉驱动）**：
  只在下一帧到达时判断窗口过期，绝不取消源 generator；终态/非 token 帧
  到达时先 flush 缓冲再放行（顺序恒等），流结束 finally flush 尾巴。
  代价：首 token 与窗口尾 token 的展示延迟 ≤ 窗口 + 帧间隔（规划取舍，
  渲染次数从每 token 降至约 20/s）。

- ``SSE_QUEUE_SIZE>0``：有界中间队列 + producer task 主动拉源（慢消费者
  场景下内存有界）。满时**丢非关键帧**（``sse_dropped_frames_total`` 计数），
  终态帧（``done``/``error``/``tool_end``/``status``/``tool_result``/``meta``
  或 ``finished`` 为真）``await put`` 阻塞等位**绝不丢**；源异常以信封传给
  消费者原样传播；消费者提前关闭（断连）→ finally cancel producer（源
  generator 收到取消走自身 finally 收口）。

两 flag 均默认关/0 = ``gate_events`` 直接返回原 source（零包装零开销）。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from loguru import logger

from agentix.config import settings
from agentix.observability import metrics

__all__ = [
    "COALESCE_WINDOW_S",
    "gate_events",
    "is_critical_frame",
    "is_token_frame",
    "merge_token_frames",
]

#: 合并窗口（规划 §4 W4：50ms）——模块常量（热路径键，YAGNI 不入 settings）。
COALESCE_WINDOW_S = 0.05

#: 可合并的 token 帧类型（两域流式词汇；终态 answer_chunk 带 finished 不合）。
_TOKEN_TYPES = frozenset({"message", "answer_chunk", "thinking_chunk"})

#: 关键帧（背压满时绝不丢）：终态 + 流首帧会话锚定（meta 丢了前端无法关联会话）
#: + T23 审批帧（丢了这场运行就没人可批，只能干等到 AGENT_TIMEOUT——决策面不是
#: 可丢弃的观测数据；心跳帧刻意**不在**此列：它本来就是可丢的占位帧）。
_CRITICAL_TYPES = frozenset(
    {"done", "error", "tool_end", "status", "tool_result", "meta", "approval"}
)


def is_token_frame(event: Dict[str, Any]) -> bool:
    """token 帧判定：类型在合并集且非终态。"""
    return event.get("type") in _TOKEN_TYPES and not event.get("finished")


def is_critical_frame(event: Dict[str, Any]) -> bool:
    """关键帧判定：终态/锚定帧，背压时阻塞等位绝不丢。"""
    return event.get("type") in _CRITICAL_TYPES or bool(event.get("finished"))


def merge_token_frames(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把窗口内 token 帧合并为一帧 delta：content 拼接，其余字段取尾帧。

    尾帧携带最新的 seq/run_id/会话元数据（emitter attach 后单调），中间帧
    只贡献 content——内容恒等由调用方跨帧拼接验证。
    """
    if len(frames) == 1:
        return frames[0]
    merged = dict(frames[-1])
    merged["content"] = "".join(
        str(f.get("content") or "") for f in frames
    )
    return merged


async def _coalesce_source(
    source: AsyncGenerator[Dict[str, Any], None],
    *,
    window_s: float = COALESCE_WINDOW_S,
    time_fn: Optional[Callable[[], float]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """到期检查式合并（拉驱动）：帧到达时判断窗口过期，不取消源。

    尾巴 flush 只在**源正常耗尽**后执行（顺序上位于流终止之前）；异常 /
    消费者提前关闭路径不 flush（连接已死，窗口尾巴无消费者——GeneratorExit
    期间禁止 yield）。
    """
    loop = asyncio.get_running_loop()
    now = time_fn or loop.time
    pending: List[Dict[str, Any]] = []
    window_start = 0.0
    async for event in source:
        if is_token_frame(event):
            t = now()
            if not pending:
                window_start = t
                pending.append(event)
            elif t - window_start >= window_s:
                # 窗口已过期：flush 旧的，新帧开窗
                yield merge_token_frames(pending)
                pending = [event]
                window_start = t
            else:
                pending.append(event)
            continue
        # 非 token/终态帧：先 flush 缓冲（顺序恒等），再放行原帧
        if pending:
            yield merge_token_frames(pending)
            pending = []
        yield event
    if pending:
        yield merge_token_frames(pending)


async def _bounded_source(
    source: AsyncGenerator[Dict[str, Any], None],
    *,
    maxsize: int,
) -> AsyncGenerator[Dict[str, Any], None]:
    """有界队列背压：producer 主动拉源，满则丢非关键帧，终态阻塞等位。"""
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    async def producer() -> None:
        try:
            async for event in source:
                if is_critical_frame(event):
                    await queue.put(("frame", event))  # 阻塞等位，绝不丢
                    continue
                try:
                    queue.put_nowait(("frame", event))
                except asyncio.QueueFull:
                    metrics.record_sse_dropped("backpressure")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — 源异常经信封传播
            try:
                await queue.put(("error", exc))
            except asyncio.CancelledError:
                raise
            return
        try:
            await queue.put(("end", None))
        except asyncio.CancelledError:
            raise

    task = asyncio.get_running_loop().create_task(producer())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                return
            if kind == "error":
                raise payload  # 源异常原样传播（基线行为等价）
            yield payload  # type: ignore[misc]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — producer 收尾异常不外抛
            logger.debug("sse bounded producer cleanup: {}", exc)


def gate_events(
    source: AsyncGenerator[Dict[str, Any], None],
    *,
    window_s: float = COALESCE_WINDOW_S,
) -> AsyncGenerator[Dict[str, Any], None]:
    """按 settings 组合出口门控；两 flag 均关 = 返回原 source（零开销）。

    顺序：source → 合并（内层，近源，减少入队帧数）→ 有界队列 → 消费者。
    """
    if not settings.SSE_COALESCE_ENABLED and settings.SSE_QUEUE_SIZE <= 0:
        return source
    gated: AsyncGenerator[Dict[str, Any], None] = source
    if settings.SSE_COALESCE_ENABLED:
        gated = _coalesce_source(gated, window_s=window_s)
    if settings.SSE_QUEUE_SIZE > 0:
        gated = _bounded_source(gated, maxsize=settings.SSE_QUEUE_SIZE)
    return gated
