"""性能基准的进程内假件（零 LLM / 零网络 / 零 Docker）。

思路源自 p65 流夹具：把 ``_stream_answer_impl`` 的四个外部依赖换成确定性
假实现，使检索域 SSE 流（含 ControlPlane Run 登记 + W0 性能戳记）能在纯
离线进程内被完整压测与冒烟。

用法（pytest 与直跑 CLI 通吃，基于 ``unittest.mock`` 而非 monkeypatch fixture）::

    from evals.performance.fakes import collect_stream, patched_stream

    with patched_stream(include_progress=True):
        frames = asyncio.run(collect_stream())
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Any, Dict, Iterator, List, Optional


class _FakeMsg:
    id = 42
    content = "q"


class _FakeSession:
    """SQLAlchemy Session 的哑元（usage 记录等 best-effort 写路径不炸不连库）。"""

    def add(self, *_a, **_kw) -> None:  # noqa: D102
        return None

    def commit(self) -> None:  # noqa: D102
        return None

    def rollback(self) -> None:  # noqa: D102
        return None

    def flush(self, *_a, **_kw) -> None:  # noqa: D102
        return None

    def refresh(self, *_a, **_kw) -> None:  # noqa: D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


def default_final_frames(chunks: int = 2) -> List[Dict[str, Any]]:
    """确定性终帧序列：N 个内容 chunk + 1 个带 finished 元数据的收尾帧。"""
    frames: List[Dict[str, Any]] = []
    for i in range(max(1, chunks)):
        frames.append(
            {
                "type": "answer_chunk",
                "content": f"这是性能基准回答内容 chunk-{i} ",
                "request_id": "req-perf",
                "finished": False,
            }
        )
    frames.append(
        {
            "type": "answer_chunk",
            "content": "",
            "request_id": "req-perf",
            "finished": True,
            "conversation_id": "cid-perf",
            "message_id": 1,
            "answer_id": 2,
            "citations": [],
        }
    )
    return frames


def default_progress_frames() -> List[Dict[str, Any]]:
    """带四阶段 detail.stages 的 progress 帧（喂 W0 的 retrieval_ms stash）。"""
    return [
        {
            "type": "progress",
            "stage": "retrieve",
            "request_id": "req-perf",
            "detail": {
                "stages": [
                    {"stage": "embed", "ms": 12},
                    {"stage": "search", "ms": 9},
                    {"stage": "rerank", "ms": 5},
                    {"stage": "graph", "ms": 3},
                ]
            },
        }
    ]


@contextmanager
def patched_stream(
    *,
    final_frames: Optional[List[Dict[str, Any]]] = None,
    include_progress: bool = False,
    trajectory_off: bool = True,
) -> Iterator[None]:
    """在上下文内把检索域流的外部依赖全部替换为假实现。"""
    from agentix.retrieval.settings import settings as retrieval_settings
    from agentix.retrieval.usecases import chat_service as cs
    from agentix.retrieval.usecases import chat_service_helpers as helpers

    frames: List[Dict[str, Any]] = []
    if include_progress:
        frames.extend(default_progress_frames())
    frames.extend(final_frames if final_frames is not None else default_final_frames())

    async def fake_prepare_context(*_a, **_kw):
        return {"history_text": "", "scope_label": ""}

    async def fake_preflight(*_a, **_kw):
        return None  # None = 继续主链路

    async def fake_final(*_a, **_kw):
        for frame in frames:
            yield frame

    with ExitStack() as stack:
        if trajectory_off:
            # 关流式轨迹观测：失败路径会落盘，压测不该写文件
            stack.enter_context(
                _patch_attr(retrieval_settings, "TRAJECTORY_ENABLED", False)
            )
        stack.enter_context(
            _patch_attr(
                cs,
                "_prepare_conversation_and_scope",
                lambda *a, **kw: ("cid-perf", _FakeMsg(), "kb", "kb-1"),
            )
        )
        stack.enter_context(
            _patch_attr(cs, "_ensure_kb_ready_or_reject", lambda *a, **kw: (object(), None, None, None))
        )
        stack.enter_context(_patch_attr(cs, "count_documents_in_scope", lambda *a, **kw: 3))
        stack.enter_context(_patch_attr(helpers, "_prepare_context", fake_prepare_context))
        stack.enter_context(
            _patch_attr(helpers, "_stream_retrieval_preflight", fake_preflight)
        )
        stack.enter_context(_patch_attr(helpers, "_stream_final_answer", fake_final))
        yield


class _patch_attr:
    """``unittest.mock.patch.object`` 的轻量替身（可 patch pydantic settings 实例属性）。"""

    def __init__(self, target: Any, name: str, value: Any) -> None:
        self._target = target
        self._name = name
        self._value = value

    def __enter__(self) -> Any:
        self._had = hasattr(self._target, self._name)
        self._old = getattr(self._target, self._name, None)
        setattr(self._target, self._name, self._value)
        return self._value

    def __exit__(self, *exc: Any) -> bool:
        if self._had:
            setattr(self._target, self._name, self._old)
        else:
            try:
                delattr(self._target, self._name)
            except AttributeError:
                pass
        return False


async def collect_stream(**kwargs: Any) -> List[Dict[str, Any]]:
    """跑一遍 ``_stream_answer_impl`` 并收齐全部帧（与 p65 同参数形态）。"""
    from agentix.retrieval.usecases import chat_service as cs

    return [
        frame
        async for frame in cs._stream_answer_impl(
            _FakeSession(),
            kb_id="kb-1",
            question="q",
            conversation_id=None,
            request_id="req-perf",
            user_id="u-1",
            cache_redis=None,
            **kwargs,
        )
    ]


def reset_state() -> None:
    """重置 ControlPlane / 轨迹记录器单例（压测防跨批串扰，best-effort）。"""
    try:
        from agentix.runtime.control_plane import reset_control_plane

        reset_control_plane()
    except Exception:  # noqa: BLE001
        pass
    try:
        from agentix.application.services.trajectory import reset_trajectory_recorder

        reset_trajectory_recorder()
    except Exception:  # noqa: BLE001
        pass
