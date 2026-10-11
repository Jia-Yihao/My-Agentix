"""步骤级轨迹记录与「首个错误步」定位（可观测性补齐）。

对应《My Agentix_架构优化方案》§9.1 F 组缺口：

* **F1 完整回放**：记录每次 LLM 调用的 prompt / response 摘要（脱敏 + 截断），
  以及工具调用入参摘要，使单次运行可按 ``trace_id`` 回放。
* **F6 失败自动定位首个错误步**：按 ``trace_id`` 顺序记录步骤，提供
  :func:`locate_first_error` 定位**第一个**失败步骤（而非只给最终失败）。
* **F5 轨迹前缀回归**：把步骤序列压成「前缀签名」，用
  :func:`compare_trajectory_prefix` 逐位置比对基线与候选，输出**首个分歧点**。
  评测集的整题打分（route/citation/reject）只能回答「答对了没」，轨迹前缀
  回归回答「**走的还是不是同一条路**」——重构时上游拓扑漂移会被立刻抓住。

设计约束：

- **零侵入**：记录失败绝不影响主链路（所有写入包 try/except + warning）。
- **有界**：每 trace 步骤数封顶（``max_steps``），超出丢弃最早步骤，
  防止长会话无界增长。
- **脱敏**：payload 捕获默认关闭（``settings.OBSERVABILITY_PAYLOAD_CAPTURE``），
  开启后仍对疑似密钥做遮蔽并截断到 ``PAYLOAD_PREVIEW_CHARS``。
- **可测试**：默认使用内存 sink，测试无需 Redis。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from loguru import logger

from agentix.config import settings
from agentix.domain.tracing import get_trace_id

__all__ = [
    "TrajectoryStep",
    "TrajectorySink",
    "InMemoryTrajectorySink",
    "JsonlTrajectorySink",
    "build_trajectory_sink",
    "export_trajectory_packages",
    "TrajectoryRecorder",
    "get_trajectory_recorder",
    "reset_trajectory_recorder",
    "flush_trajectory_writes",
    "redact_and_truncate",
    "locate_first_error",
    "trajectory_prefix_signature",
    "compare_trajectory_prefix",
    "compare_trajectory_packages",
]

#: 前缀签名的默认字段（顺序即比较顺序）
PREFIX_FIELDS: Tuple[str, ...] = ("kind", "name", "status")

#: payload 预览的最大字符数（超出截断并标注）
PAYLOAD_PREVIEW_CHARS = 500

#: 疑似密钥的模式（脱敏为 ``***``）
_SECRET_PATTERNS = (
    re.compile(r"(sk-[A-Za-z0-9]{8,})"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)([^\s,;'\"]{6,})"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;'\"]{6,})"),
    re.compile(r"(?i)(password\s*[:=]\s*)([^\s,;'\"]{3,})"),
)


def redact_and_truncate(text: Any, *, max_chars: int = PAYLOAD_PREVIEW_CHARS) -> str:
    """把任意内容转为「脱敏 + 截断」的字符串预览。

    - 非字符串先 ``str()``；
    - 命中疑似密钥模式 → 替换为 ``***``；
    - 超出 ``max_chars`` → 截断并追加 ``…[truncated]``。
    """
    try:
        raw = text if isinstance(text, str) else str(text)
    except Exception:  # noqa: BLE001 - 预览失败不应抛
        return "<unprintable>"
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            raw = pattern.sub(lambda m: m.group(1) + "***", raw)
        else:
            raw = pattern.sub("***", raw)
    if len(raw) > max_chars:
        raw = raw[:max_chars] + "…[truncated]"
    return raw


@dataclass
class TrajectoryStep:
    """单条轨迹步骤。"""

    step_index: int
    kind: str  # llm | tool | node | retrieval | error
    name: str
    status: str = "ok"  # ok | error | start | end | skipped
    trace_id: str = "-"
    latency_ms: float = 0.0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_index": self.step_index,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "trace_id": self.trace_id,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
            "error": self.error,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TrajectoryStep:
        return cls(
            step_index=int(data.get("step_index", 0)),
            kind=str(data.get("kind", "")),
            name=str(data.get("name", "")),
            status=str(data.get("status", "ok")),
            trace_id=str(data.get("trace_id", "-")),
            latency_ms=float(data.get("latency_ms", 0.0)),
            detail=dict(data.get("detail") or {}),
            error=data.get("error"),
            started_at=float(data.get("started_at", 0.0)),
        )


class TrajectorySink(Protocol):
    """轨迹存储后端契约。"""

    def append(self, trace_id: str, step: TrajectoryStep) -> None:
        ...

    def list(self, trace_id: str) -> List[TrajectoryStep]:
        ...


class InMemoryTrajectorySink:
    """内存 sink（默认）：有界、零依赖、测试友好。

    注意：进程重启即丢失——生产若需跨进程回放，可替换为 Redis sink
    （实现同样的 ``append`` / ``list`` 契约即可）。
    """

    def __init__(self, *, max_traces: int = 500, max_steps_per_trace: int = 200) -> None:
        self._data: Dict[str, List[TrajectoryStep]] = {}
        self._order: List[str] = []
        self._max_traces = max_traces
        self._max_steps = max_steps_per_trace

    def append(self, trace_id: str, step: TrajectoryStep) -> None:
        bucket = self._data.get(trace_id)
        if bucket is None:
            bucket = []
            self._data[trace_id] = bucket
            self._order.append(trace_id)
            while len(self._order) > self._max_traces:
                evicted = self._order.pop(0)
                self._data.pop(evicted, None)
        bucket.append(step)
        if len(bucket) > self._max_steps:
            del bucket[: len(bucket) - self._max_steps]

    def list(self, trace_id: str) -> List[TrajectoryStep]:
        return list(self._data.get(trace_id, []))

    def clear(self) -> None:
        self._data.clear()
        self._order.clear()


class JsonlTrajectorySink:
    """追加式 **JSONL** sink：每行一个步骤，跨进程 / 重启保留。

    **为什么需要它**：默认内存 sink 进程一重启就丢失，这直接卡住了两件事——

    - **F1 完整回放**：生产上想事后回溯某次请求的 prompt / 工具入参，
      内存 sink 根本留不下；
    - **F5 轨迹前缀回归**：回归 CLI 需要**基线轨迹文件**，
      没有持久化就只能人工构造基线（等于没接真实链路）。

    设计取舍：

    - **每行一个步骤**（而非每 trace 一个 JSON）：追加写是原子的，
      多进程/多线程同时写不会截断整条 trace；读时按 ``trace_id`` 过滤组装。
    - **写后即关**：崩溃时最多丢最后一行，不用维护长驻文件句柄
      （长句柄在容器热重启 / 日志轮转场景下反而更脆）。
    - **有界**：按文件大小封顶，超出则轮转一份旧文件（只保留一份）。

    ``list()`` 需扫描文件，故**不**适合高频调用；正常用法是
    「写时 append，分析时一次性 :func:`export_trajectory_packages`」。
    """

    def __init__(
        self,
        path: "str | Path",
        *,
        max_bytes: int = 32 * 1024 * 1024,
        max_steps_per_trace: int = 200,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._max_steps = max(1, int(max_steps_per_trace))
        # 首次使用时才建目录：构造 sink 不应产生副作用（利于单测与只读环境）
        self._ensured = False

    # -- 写入 ---------------------------------------------------------------

    def _ensure_parent(self) -> None:
        if self._ensured:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 目录建不出来时让 append 自然失败（由调用方降级），此处不抛
            pass
        self._ensured = True

    def append(self, trace_id: str, step: TrajectoryStep) -> None:
        if self._should_rotate():
            self._rotate()
        self._ensure_parent()
        payload = step.to_dict()
        payload["trace_id"] = trace_id
        line = json.dumps(payload, ensure_ascii=False, default=str)
        # 追加 + 换行：单行写入，尽量保持原子
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- 读取 ---------------------------------------------------------------

    def _iter_lines(self):
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # 损坏行（如进程被强杀写了一半）跳过，绝不影响其余
                        continue
        except OSError:
            return

    def list(self, trace_id: str) -> List[TrajectoryStep]:
        steps: List[TrajectoryStep] = []
        for item in self._iter_lines():
            if str(item.get("trace_id", "")) == str(trace_id):
                steps.append(TrajectoryStep.from_dict(item))
        steps.sort(key=lambda s: s.step_index)
        if len(steps) > self._max_steps:
            steps = steps[-self._max_steps :]
        return steps

    def all_traces(self) -> Dict[str, List[TrajectoryStep]]:
        """一次性读出全部 trace（分析用，成本 = 扫一遍文件）。"""
        grouped: Dict[str, List[TrajectoryStep]] = {}
        for item in self._iter_lines():
            tid = str(item.get("trace_id", "-"))
            grouped.setdefault(tid, []).append(TrajectoryStep.from_dict(item))
        for tid, steps in grouped.items():
            steps.sort(key=lambda s: s.step_index)
        return grouped

    # -- 维护 ---------------------------------------------------------------

    def _should_rotate(self) -> bool:
        try:
            return self._path.exists() and self._path.stat().st_size >= self._max_bytes
        except OSError:
            return False

    def _rotate(self) -> None:
        """轮转：当前文件改名为 ``.1``（只保留一份历史）。"""
        try:
            backup = self._path.with_suffix(self._path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            self._path.rename(backup)
        except OSError as exc:
            logger.warning("trajectory rotation failed (non-fatal): {}", exc)

    def clear(self) -> None:
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            return

    def stats(self) -> Dict[str, Any]:
        size = self._path.stat().st_size if self._path.exists() else 0
        return {
            "path": str(self._path),
            "bytes": size,
            "max_bytes": self._max_bytes,
            "exists": self._path.exists(),
        }


def export_trajectory_packages(
    sink: "TrajectorySink | TrajectoryRecorder",
    out_dir: "str | Path",
) -> int:
    """把 sink 里的轨迹导出为 `evals/trajectory_regression.py` 可直接消费的轨迹包。

    每个 trace 写成 ``<out_dir>/<trace_id>.json``，内容为
    ``{"trace_id": ..., "step_count": ..., "first_error_step_index": ..., "steps": [...]}``
    —— 与 :meth:`TrajectoryRecorder.replay` 的输出结构一致。

    :return: 导出的 trace 数量。
    """
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    if isinstance(sink, TrajectoryRecorder):
        traces = {tid: sink.get(tid) for tid in sink.trace_ids()}
    elif hasattr(sink, "all_traces"):
        traces = sink.all_traces()  # type: ignore[attr-defined]
    else:
        raise TypeError("sink 需为 TrajectoryRecorder 或提供 all_traces() 的 sink")

    count = 0
    for trace_id, steps in traces.items():
        if not steps:
            continue
        first_error = locate_first_error(steps)
        package = {
            "trace_id": trace_id,
            "step_count": len(steps),
            "first_error_step_index": first_error.step_index if first_error else None,
            "first_error": first_error.to_dict() if first_error else None,
            "steps": [s.to_dict() for s in steps],
        }
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(trace_id)) or "unknown"
        (target / f"{safe_name}.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        count += 1
    return count


def build_trajectory_sink() -> TrajectorySink:
    """按 settings 选择 sink 后端（默认 memory，保持既有行为）。

    ``TRAJECTORY_SINK=jsonl`` 时落盘到 ``TRAJECTORY_PATH``。
    任何异常都降级为内存 sink —— **可观测性设施不得影响主链路**。
    """
    try:
        kind = str(getattr(settings, "TRAJECTORY_SINK", "memory") or "memory").strip().lower()
        if kind == "jsonl":
            path = str(getattr(settings, "TRAJECTORY_PATH", "") or "").strip()
            if path:
                return JsonlTrajectorySink(Path(path))
            logger.warning("TRAJECTORY_SINK=jsonl 但未配置 TRAJECTORY_PATH，回退内存 sink")
        elif kind != "memory":
            logger.warning("未知的 TRAJECTORY_SINK={}，回退内存 sink", kind)
    except Exception as exc:  # noqa: BLE001
        logger.debug("trajectory sink selection failed: {}", exc)
    return InMemoryTrajectorySink()


class TrajectoryRecorder:
    """按 ``trace_id`` 记录步骤序列，并支持「首个错误步」定位。"""

    def __init__(self, *, sink: Optional[TrajectorySink] = None) -> None:
        self._sink: TrajectorySink = sink or InMemoryTrajectorySink()
        self._counters: Dict[str, int] = {}
        # Phase H · W4 异步写：flag-on 时 record 只入队，后台 writer 落 sink。
        self._async_queue: Optional[asyncio.Queue] = None
        self._writer_task: Optional[asyncio.Task] = None

    # -- 写入 ---------------------------------------------------------------

    def record(
        self,
        *,
        kind: str,
        name: str,
        status: str = "ok",
        latency_ms: float = 0.0,
        detail: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Optional[TrajectoryStep]:
        """记录一条步骤；任何异常都被吞掉并 warning（绝不影响主链路）。

        Phase H · W4：``TRAJECTORY_ASYNC_WRITE_ENABLED`` 且处于 event loop
        时改为入队 + 后台 writer 落 sink（顺序 = 队列 FIFO；关停/测试经
        ``flush_trajectory_writes`` 兜底）；flag 关或无 loop（同步上下文）
        保持同步直写现行为。
        """
        try:
            tid = trace_id or get_trace_id()
            index = self._counters.get(tid, 0)
            self._counters[tid] = index + 1
            step = TrajectoryStep(
                step_index=index,
                kind=kind,
                name=name,
                status=status,
                trace_id=tid,
                latency_ms=latency_ms,
                detail=detail or {},
                error=error,
            )
            if self._async_enabled():
                self._enqueue(tid, step)
            else:
                self._sink.append(tid, step)
            return step
        except Exception as exc:  # noqa: BLE001 - 可观测性不得影响主链路
            logger.warning("trajectory record failed (non-fatal): {}", exc)
            return None

    def _async_enabled(self) -> bool:
        if not bool(getattr(settings, "TRAJECTORY_ASYNC_WRITE_ENABLED", False)):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无 event loop（同步上下文）：降级同步直写——与在途队列交错的
            # 乱序在本仓不可能（record 调用方全部在 async 上下文）。
            return False
        return True

    def _enqueue(self, tid: str, step: TrajectoryStep) -> None:
        if self._async_queue is None:
            self._async_queue = asyncio.Queue()
        self._async_queue.put_nowait((tid, step))
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.get_running_loop().create_task(
                self._writer_loop()
            )

    async def _writer_loop(self) -> None:
        """后台 writer：FIFO drain 队列落 sink（异常吞 warning 不中断）。"""
        queue = self._async_queue
        if queue is None:
            return
        while True:
            tid, step = await queue.get()
            try:
                self._sink.append(tid, step)
            except Exception as exc:  # noqa: BLE001
                logger.warning("trajectory async write failed (non-fatal): {}", exc)

    async def flush_async(self) -> int:
        """兜底 flush：把在队条目全部落 sink（与 writer 竞争 get_nowait 安全——
        单线程事件循环、append 无 await 点 ⇒ 无中间态无重复写）。"""
        queue = self._async_queue
        if queue is None:
            return 0
        flushed = 0
        while True:
            try:
                tid, step = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                self._sink.append(tid, step)
                flushed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("trajectory flush failed (non-fatal): {}", exc)
        return flushed

    def _cancel_writer(self) -> None:
        task = self._writer_task
        self._writer_task = None
        if task is None or task.done():
            return
        try:
            task.cancel()
        except RuntimeError:
            # 事件循环已关闭（测试 teardown 时序）：task 随 loop 一并销毁
            pass

    # -- 读取 ---------------------------------------------------------------

    def get(self, trace_id: Optional[str] = None) -> List[TrajectoryStep]:
        """取某 trace 的完整步骤序列（按 step_index 升序）。"""
        tid = trace_id or get_trace_id()
        steps = self._sink.list(tid)
        return sorted(steps, key=lambda s: s.step_index)

    def first_error(self, trace_id: Optional[str] = None) -> Optional[TrajectoryStep]:
        """返回**第一个**状态为 ``error`` 的步骤（失败定位入口）。"""
        return locate_first_error(self.get(trace_id))

    def trace_ids(self) -> List[str]:
        """列出已知的全部 trace_id（导出/遍历用）。

        内存 sink 直接返回内部键；JSONL sink 走 ``all_traces()``。
        """
        all_traces = getattr(self._sink, "all_traces", None)
        if callable(all_traces):
            return sorted(all_traces().keys())
        keys = getattr(self._sink, "_data", None)
        if isinstance(keys, dict):
            return sorted(keys.keys())
        return sorted(self._counters.keys())

    def replay(self, trace_id: Optional[str] = None) -> Dict[str, Any]:
        """返回可直接落盘/回放的完整轨迹包。"""
        tid = trace_id or get_trace_id()
        steps = self.get(tid)
        first_error = locate_first_error(steps)
        return {
            "trace_id": tid,
            "step_count": len(steps),
            "first_error_step_index": (
                first_error.step_index if first_error else None
            ),
            "first_error": first_error.to_dict() if first_error else None,
            "steps": [s.to_dict() for s in steps],
        }

    def reset(self) -> None:
        """清空记录（测试隔离）。"""
        self._counters.clear()
        self._cancel_writer()
        if self._async_queue is not None:
            self._async_queue = asyncio.Queue()
        clear = getattr(self._sink, "clear", None)
        if callable(clear):
            clear()


def locate_first_error(steps: List[TrajectoryStep]) -> Optional[TrajectoryStep]:
    """在步骤序列中定位第一个失败步骤（纯函数，便于单测）。"""
    for step in sorted(steps, key=lambda s: s.step_index):
        if step.status == "error":
            return step
    return None


# ---------------------------------------------------------------------------
# F5 轨迹前缀回归
# ---------------------------------------------------------------------------


def trajectory_prefix_signature(
    steps: Sequence[TrajectoryStep],
    *,
    length: Optional[int] = None,
    fields: Sequence[str] = PREFIX_FIELDS,
) -> List[List[str]]:
    """把步骤序列压成可逐位置比较的「前缀签名」。

    签名是**字符串二维数组**（而非对象引用），因此可直接序列化进基线文件、
    跨进程/跨版本比较，且不受 ``latency_ms`` / ``started_at`` 等易变字段干扰。

    :param steps: 步骤序列（内部会按 ``step_index`` 排序，入参顺序无关）。
    :param length: 只取前 N 步；``None`` 表示全部。
    :param fields: 参与比较的字段名，顺序即比较顺序；缺字段按空串处理。
    """
    ordered = sorted(steps, key=lambda s: s.step_index)
    if length is not None:
        ordered = ordered[: max(0, int(length))]
    return [[str(getattr(s, f, "") or "") for f in fields] for s in ordered]


def compare_trajectory_prefix(
    baseline: Sequence[TrajectoryStep],
    candidate: Sequence[TrajectoryStep],
    *,
    length: Optional[int] = None,
    fields: Sequence[str] = PREFIX_FIELDS,
) -> Dict[str, Any]:
    """逐位置比较两条轨迹的**前缀**，返回首个分歧点。

    语义（这是「前缀回归」而非「全等回归」的关键）：

    - 基线被当作**参考前缀**；候选必须**以基线前缀开头**。
    - 候选**更长**是允许的（下游多走了几步不算回归）；候选**更短**、
      或在比较区内任一步不一致 → 不匹配。
    - ``length`` 可把基线进一步截短（只锁定最上游的 N 步），
      便于在已知下游会变动时仍能守住上游拓扑。

    返回字典字段：

    ``match``
        布尔。True 表示候选以基线前缀开头。
    ``compared_length``
        实际参与比较的步数。
    ``baseline_length`` / ``candidate_length``
        两侧原始步数。
    ``first_divergence_index``
        首个分歧位置（0-based）；无分歧为 ``None``。
    ``baseline_step`` / ``candidate_step``
        分歧处的两个签名；某一侧越界则为 ``None``。
    ``reason``
        ``"ok"`` / ``"step_mismatch"`` / ``"candidate_shorter"`` / ``"empty_baseline"``。
    """
    base_sig = trajectory_prefix_signature(baseline, length=length, fields=fields)
    cand_sig = trajectory_prefix_signature(candidate, fields=fields)
    compared = len(base_sig)

    result: Dict[str, Any] = {
        "match": True,
        "compared_length": compared,
        "baseline_length": len(sorted(baseline, key=lambda s: s.step_index)),
        "candidate_length": len(cand_sig),
        "first_divergence_index": None,
        "baseline_step": None,
        "candidate_step": None,
        "reason": "ok",
    }

    if compared == 0:
        # 空基线无可比较内容 —— 显式标注，避免「空真通过」被当成真回归保障。
        result["reason"] = "empty_baseline"
        return result

    for idx in range(compared):
        if idx >= len(cand_sig):
            result.update(
                match=False,
                first_divergence_index=idx,
                baseline_step=base_sig[idx],
                candidate_step=None,
                reason="candidate_shorter",
            )
            return result
        if base_sig[idx] != cand_sig[idx]:
            result.update(
                match=False,
                first_divergence_index=idx,
                baseline_step=base_sig[idx],
                candidate_step=cand_sig[idx],
                reason="step_mismatch",
            )
            return result

    return result


def compare_trajectory_packages(
    baseline_package: Dict[str, Any],
    candidate_package: Dict[str, Any],
    *,
    length: Optional[int] = None,
    fields: Sequence[str] = PREFIX_FIELDS,
) -> Dict[str, Any]:
    """对 ``TrajectoryRecorder.replay()`` 产出的轨迹包做前缀比较。

    轨迹包形如 ``{"trace_id": ..., "steps": [...]}``；步骤项可为 ``dict``
    或 :class:`TrajectoryStep`。
    """

    def _coerce(package: Dict[str, Any]) -> List[TrajectoryStep]:
        raw = (package or {}).get("steps") or []
        out: List[TrajectoryStep] = []
        for item in raw:
            if isinstance(item, TrajectoryStep):
                out.append(item)
            elif isinstance(item, dict):
                out.append(TrajectoryStep.from_dict(item))
        return out

    return compare_trajectory_prefix(
        _coerce(baseline_package),
        _coerce(candidate_package),
        length=length,
        fields=fields,
    )


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_recorder: Optional[TrajectoryRecorder] = None


def get_trajectory_recorder() -> TrajectoryRecorder:
    """获取全局轨迹记录器（首次调用时创建）。

    sink 后端由 :func:`build_trajectory_sink` 按 settings 选择
    （默认 memory；``TRAJECTORY_SINK=jsonl`` 时落盘）。
    """
    global _recorder
    if _recorder is None:
        _recorder = TrajectoryRecorder(sink=build_trajectory_sink())
    return _recorder


def reset_trajectory_recorder() -> None:
    """重置全局记录器（测试隔离）；在途 writer 一并取消。"""
    global _recorder
    if _recorder is not None:
        _recorder._cancel_writer()
    _recorder = None


async def flush_trajectory_writes() -> int:
    """Phase H · W4 兜底：把全局记录器在队的异步轨迹全部落 sink。

    关停（lifespan shutdown）与测试在读回前调用；无记录器 / 队列为空 /
    flag 关均为 no-op（返回 0，零开销）。
    """
    global _recorder
    if _recorder is None:
        return 0
    return await _recorder.flush_async()


def payload_capture_enabled() -> bool:
    """payload（prompt / response / 工具入参）捕获是否开启。"""
    return bool(getattr(settings, "OBSERVABILITY_PAYLOAD_CAPTURE", False))
