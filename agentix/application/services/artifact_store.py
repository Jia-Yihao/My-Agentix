"""上下文隔离通道：承载大中间产物（Context Isolation over Compression）。

《My Agentix_架构优化方案》§9.1 C 组缺口：「存在上下文隔离通道承载大中间产物」。
原则依据（方案第 2 章）：**上下文隔离优于压缩**。

**隔离 vs 压缩**（这是本模块存在的理由）：

===========  ======================================  ==============================
维度         压缩（compression）                     隔离（isolation，本模块）
===========  ======================================  ==============================
处理方式     把大内容变短，**仍放进** state           把大内容**移出** state
state 里留下 一段有损摘要                               一个句柄（artifact_id）+ 摘要
可追溯性     不可逆（原始内容丢失）                     **无损**（原始内容可按需取回）
失败代价     摘要失真 → 后续推理基于错误前提           取回失败 → 明确报错，不静默失真
===========  ======================================  ==============================

**典型场景**：Neo4j/向量库返回 500 行、工具吐回 200KB 原文、子图产出长文档——
把它们塞进 LangGraph state 会迅速击穿上下文窗口；外置后主状态只留
``ArtifactRef``（约 200 字节），需要全文时再 ``get()``。

**失败语义（差异化）**：artifact store 属**可选增强**——
``offload_content`` 内部任何异常都降级为「内联返回原内容」（绝不阻断主链路），
与 LLM/Embedding 的 fail-fast 形成对照（见 ADR-0002）。

**有界**：``InMemoryArtifactStore`` 同时按**条数**与**字节数**封顶，LRU 淘汰；
淘汰计数可观测（``stats()``），便于判断阈值是否合理。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from loguru import logger

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "InMemoryArtifactStore",
    "OffloadResult",
    "offload_content",
    "get_artifact_store",
    "reset_artifact_store",
]

#: 默认内联阈值：内容超过该字符数才外置（低于此值内联更划算）
DEFAULT_INLINE_THRESHOLD = 2000

#: ``ArtifactRef.preview`` 的默认长度
DEFAULT_PREVIEW_CHARS = 200


@dataclass
class ArtifactRef:
    """大中间产物的**句柄**——主状态里只保留这个（而非内容本身）。

    ``preview`` 让模型/人无需取回全文即可判断「这个产物是否相关」；
    ``summary`` 是可选的一句话结论（调用方可传入，例如「命中 137 条法条」）。
    """

    artifact_id: str
    kind: str
    size_bytes: int
    preview: str
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    trace_id: str = "-"
    #: 该产物能否由原始数据确定性重建；True 时淘汰可安全丢弃
    rebuildable: bool = True
    #: 额外元数据（如 row_count / page_count / source）
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "preview": self.preview,
            "summary": self.summary,
            "created_at": self.created_at,
            "trace_id": self.trace_id,
            "rebuildable": self.rebuildable,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(data["artifact_id"]),
            kind=str(data.get("kind", "unknown")),
            size_bytes=int(data.get("size_bytes", 0)),
            preview=str(data.get("preview", "")),
            summary=str(data.get("summary", "")),
            created_at=float(data.get("created_at", time.time())),
            trace_id=str(data.get("trace_id", "-")),
            rebuildable=bool(data.get("rebuildable", True)),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class OffloadResult:
    """``offload_content`` 的返回：要么内联，要么外置（二选一）。

    用**同一个返回类型**而非 ``Union[str, ArtifactRef]``，是为了让调用方
    显式处理两条分支（``is_offloaded``），避免把句柄当字符串误用。
    """

    #: True = 内容已外置（``ref`` 有值、``inline`` 为 None）
    is_offloaded: bool
    #: 外置时为主状态应保留的句柄
    ref: Optional[ArtifactRef] = None
    #: 内联时为原始内容
    inline: Optional[str] = None
    #: 外置失败降级的原因（仅诊断用，不影响主链路）
    degraded_reason: Optional[str] = None

    def value(self, store: Optional["ArtifactStore"] = None) -> str:
        """取回「应当放进 state 的东西」——内联原文，或句柄的可读表示。

        ``is_offloaded=True`` 但 ``ref`` 缺失属不一致态；此时退化为
        ``(state 里不该出现未定义内容)`` 空串，绝不抛异常——
        调用方不应因一个优化设施的不一致而崩溃。
        """
        if not self.is_offloaded:
            return self.inline or ""
        return self.ref.preview if self.ref is not None else ""


class ArtifactStore(Protocol):
    """产物存储契约（默认内存实现；生产可换 Redis/对象存储）。"""

    def put(
        self,
        content: str,
        *,
        kind: str = "generic",
        summary: str = "",
        trace_id: str = "-",
        rebuildable: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> ArtifactRef:
        ...

    def get(self, artifact_id: str) -> Optional[str]:
        ...

    def ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        ...

    def delete(self, artifact_id: str) -> bool:
        ...


class InMemoryArtifactStore:
    """内存实现：有界（条数 + 字节数）、LRU 淘汰、零外部依赖。

    ⚠️ 进程重启即丢失——生产若需跨进程/跨请求回放，替换为 Redis 实现即可
    （实现同样的 ``put`` / ``get`` / ``ref`` / ``delete`` 契约）。
    """

    def __init__(self, *, max_items: int = 200, max_bytes: int = 8 * 1024 * 1024) -> None:
        self._max_items = max(1, int(max_items))
        self._max_bytes = max(1, int(max_bytes))
        self._items: Dict[str, Tuple[ArtifactRef, str]] = {}
        self._order: List[str] = []  # LRU：末尾为最近使用
        self._bytes = 0
        self._evicted = 0
        self._evicted_unrebuildable = 0

    # -- 写入 ---------------------------------------------------------------

    def put(
        self,
        content: str,
        *,
        kind: str = "generic",
        summary: str = "",
        trace_id: str = "-",
        rebuildable: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> ArtifactRef:
        text = content if isinstance(content, str) else str(content)
        artifact_id = uuid.uuid4().hex
        ref = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            size_bytes=len(text.encode("utf-8")),
            preview=_preview(text, preview_chars),
            summary=summary,
            trace_id=trace_id,
            rebuildable=rebuildable,
            meta=dict(meta or {}),
        )
        self._items[artifact_id] = (ref, text)
        self._order.append(artifact_id)
        self._bytes += ref.size_bytes
        self._evict_if_needed()
        return ref

    # -- 读取 ---------------------------------------------------------------

    def get(self, artifact_id: str) -> Optional[str]:
        entry = self._items.get(artifact_id)
        if entry is None:
            return None
        self._touch(artifact_id)
        return entry[1]

    def ref(self, artifact_id: str) -> Optional[ArtifactRef]:
        entry = self._items.get(artifact_id)
        if entry is None:
            return None
        self._touch(artifact_id)
        return entry[0]

    def delete(self, artifact_id: str) -> bool:
        entry = self._items.pop(artifact_id, None)
        if entry is None:
            return False
        self._bytes -= entry[0].size_bytes
        try:
            self._order.remove(artifact_id)
        except ValueError:
            pass
        return True

    # -- 维护 ---------------------------------------------------------------

    def _touch(self, artifact_id: str) -> None:
        try:
            self._order.remove(artifact_id)
        except ValueError:
            return
        self._order.append(artifact_id)

    def _evict_if_needed(self) -> None:
        while self._order and (
            len(self._items) > self._max_items or self._bytes > self._max_bytes
        ):
            victim = self._order.pop(0)
            entry = self._items.pop(victim, None)
            if entry is None:
                continue
            self._bytes -= entry[0].size_bytes
            self._evicted += 1
            if not entry[0].rebuildable:
                # 不可重建的产物被淘汰 = 真实信息损失，单独计数以便告警
                self._evicted_unrebuildable += 1

    def clear(self) -> None:
        self._items.clear()
        self._order.clear()
        self._bytes = 0

    def stats(self) -> Dict[str, Any]:
        """容量与淘汰统计（可观测：阈值是否过小一眼可见）。"""
        return {
            "items": len(self._items),
            "bytes": self._bytes,
            "max_items": self._max_items,
            "max_bytes": self._max_bytes,
            "evicted": self._evicted,
            "evicted_unrebuildable": self._evicted_unrebuildable,
        }

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, artifact_id: object) -> bool:
        return artifact_id in self._items


def _preview(text: str, preview_chars: int) -> str:
    limit = max(0, int(preview_chars))
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def offload_content(
    content: str,
    *,
    kind: str = "generic",
    store: Optional[ArtifactStore] = None,
    inline_threshold: int = DEFAULT_INLINE_THRESHOLD,
    summary: str = "",
    trace_id: str = "-",
    rebuildable: bool = True,
    meta: Optional[Dict[str, Any]] = None,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> OffloadResult:
    """按阈值决定「内联」还是「外置」，永不抛异常。

    内容长度 ≤ ``inline_threshold`` → 内联返回（外置反而增加一次取回开销）；
    超过阈值 → 外置到 ``store``，返回句柄；**外置过程任何异常都降级为内联**，
    并把原因记在 ``degraded_reason``（可选增强的降级语义）。

    ``inline_threshold <= 0`` 表示**永不外置**——用于一键回滚到
    「内容原样进 state」的旧行为（见 ``settings.ARTIFACT_INLINE_THRESHOLD``）。
    """
    text = content if isinstance(content, str) else str(content)
    threshold = int(inline_threshold)
    if threshold <= 0 or len(text) <= threshold:
        return OffloadResult(is_offloaded=False, inline=text)

    try:
        target = store if store is not None else get_artifact_store()
        ref = target.put(
            text,
            kind=kind,
            summary=summary,
            trace_id=trace_id,
            rebuildable=rebuildable,
            meta=meta,
            preview_chars=preview_chars,
        )
        # 存储后端返回空句柄 ⇒ 外置实际未成功（如自定义 store 的契约实现不完整）。
        # 此时**必须**降级为内联：否则内容既不在 state 也取不回来，等于静默丢失。
        if ref is None:
            logger.warning(
                "artifact store returned no handle, falling back to inline ({} chars)",
                len(text),
            )
            return OffloadResult(
                is_offloaded=False,
                inline=text,
                degraded_reason="store returned no handle",
            )
        return OffloadResult(is_offloaded=True, ref=ref)
    except Exception as exc:  # noqa: BLE001 - 可选增强，降级不阻断
        logger.warning("artifact offload failed, falling back to inline: {}", exc)
        return OffloadResult(is_offloaded=False, inline=text, degraded_reason=str(exc))


# ---------------------------------------------------------------------------
# 单例
# ---------------------------------------------------------------------------

_store: Optional[InMemoryArtifactStore] = None


def get_artifact_store() -> InMemoryArtifactStore:
    """获取全局产物存储（首次调用时创建）。"""
    global _store
    if _store is None:
        _store = InMemoryArtifactStore()
    return _store


def reset_artifact_store() -> None:
    """重置全局产物存储（测试隔离）。"""
    global _store
    _store = None
