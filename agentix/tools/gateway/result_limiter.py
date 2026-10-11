"""感知工具下游结果的分页 / 截断 / 限量。

《My Agentix_架构优化方案》§9.1 B 组缺口：「感知工具默认分页/截断/限量」——
现状：读侧已规范化 ``LIMIT``（``predefined_cypher/utils.py``、
``cypher_tools/utils.py``），但**工具返回给模型的结果**没有统一上限。

**为什么必须在「下游」再加一道**：数据库侧 ``LIMIT 100`` 只约束**行数**，
但单行仍可能是 50KB 的正文；而 Cypher 的写语句、以及 ``RETURN *`` 之类的
查询根本不会被注入 ``LIMIT``。结果直接进 state 会击穿上下文窗口，
且**模型看不到自己被截断**时会基于不完整数据编造结论。

因此本模块统一提供：

1. :func:`limit_rows` —— 行级分页（稳定游标 + ``has_more``），
   把「还有更多」**显式告诉模型**（而不是静默丢弃）。
2. :func:`truncate_text` —— 字符级截断（保留尾部提示）。
3. :func:`limit_value` —— 对任意工具返回形态（list / str / dict）统一施加
   上述两种限制，返回 :class:`LimitedResult`（值 + 元信息）。

**不静默**：所有截断都写入 ``PageMeta``，调用方可把它作为「系统提示」附在
结果里，让模型知道「这不是全部」。静默截断是幻觉的温床。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "PageMeta",
    "LimitedResult",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_MAX_CHARS",
    "make_cursor",
    "parse_cursor",
    "limit_rows",
    "truncate_text",
    "limit_value",
    "ResultDeduplicator",
    "get_result_deduplicator",
    "reset_result_deduplicator",
]

#: 单次返回给模型的最大行数
DEFAULT_MAX_ROWS = 100

#: 单次返回给模型的最大字符数
DEFAULT_MAX_CHARS = 8000

#: 截断标记（便于测试与模型识别）
TRUNCATION_SUFFIX = "\n…[结果已截断，如需完整内容请缩小查询范围]"

#: 常见的结果容器键（按优先级）
_ROWS_KEYS = ("rows", "data", "items", "records", "results")


@dataclass
class PageMeta:
    """分页/截断元信息（**必须随结果一起给模型看**）。"""

    total_rows: int = 0
    returned_rows: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False
    next_cursor: Optional[str] = None
    truncated_chars: int = 0
    truncated: bool = False

    @property
    def dropped_rows(self) -> int:
        return max(0, self.total_rows - self.returned_rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "returned_rows": self.returned_rows,
            "offset": self.offset,
            "limit": self.limit,
            "has_more": self.has_more,
            "next_cursor": self.next_cursor,
            "truncated_chars": self.truncated_chars,
            "truncated": self.truncated,
            "dropped_rows": self.dropped_rows,
        }

    def notice(self) -> str:
        """给模型看的一行说明；无需提示时返回空串。"""
        parts: List[str] = []
        if self.has_more:
            parts.append(
                f"共 {self.total_rows} 行，本次返回第 {self.offset + 1}-"
                f"{self.offset + self.returned_rows} 行；还有 {self.dropped_rows} 行未返回"
                f"（可用 next_cursor={self.next_cursor} 继续）"
            )
        if self.truncated:
            parts.append(f"内容被截断，省略 {self.truncated_chars} 字符")
        return "；".join(parts)


@dataclass
class LimitedResult:
    """限量后的结果 + 元信息。"""

    value: Any
    meta: PageMeta = field(default_factory=PageMeta)

    def with_notice(self) -> Any:
        """把提示并入返回值（list → dict 包裹；str → 追加）。"""
        text = self.meta.notice()
        if not text:
            return self.value
        if isinstance(self.value, str):
            return self.value + "\n\n[系统提示] " + text
        if isinstance(self.value, list):
            return {"rows": self.value, "page": self.meta.to_dict(), "notice": text}
        if isinstance(self.value, dict):
            merged = dict(self.value)
            merged.setdefault("page", self.meta.to_dict())
            merged.setdefault("notice", text)
            return merged
        return self.value


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------


def make_cursor(offset: int) -> str:
    """把偏移量编码为不透明游标（当前为十进制字符串；换实现不影响调用方）。"""
    return str(max(0, int(offset)))


def parse_cursor(cursor: Optional[str]) -> int:
    """解析游标；非法/缺省一律按 0 处理（**不抛异常**——工具层要稳）。"""
    if cursor is None:
        return 0
    try:
        return max(0, int(str(cursor).strip()))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 行级分页
# ---------------------------------------------------------------------------


def limit_rows(
    rows: Sequence[Any],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    cursor: Optional[str] = None,
) -> LimitedResult:
    """对行序列分页，返回当页 + 元信息。

    ``max_rows <= 0`` 表示不限制行数（但仍会如实报告 ``total_rows``）。
    """
    items = list(rows)
    total = len(items)
    offset = parse_cursor(cursor)

    if max_rows <= 0:
        meta = PageMeta(
            total_rows=total, returned_rows=total, offset=0, limit=0, has_more=False
        )
        return LimitedResult(value=items, meta=meta)

    page = items[offset : offset + max_rows]
    has_more = offset + len(page) < total
    meta = PageMeta(
        total_rows=total,
        returned_rows=len(page),
        offset=offset,
        limit=max_rows,
        has_more=has_more,
        next_cursor=make_cursor(offset + len(page)) if has_more else None,
    )
    return LimitedResult(value=page, meta=meta)


# ---------------------------------------------------------------------------
# 字符级截断
# ---------------------------------------------------------------------------


def truncate_text(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> LimitedResult:
    """按字符截断文本；``max_chars <= 0`` 表示不截断。"""
    raw = text if isinstance(text, str) else str(text)
    if max_chars <= 0 or len(raw) <= max_chars:
        return LimitedResult(value=raw, meta=PageMeta(truncated=False))
    kept = raw[:max_chars]
    dropped = len(raw) - max_chars
    return LimitedResult(
        value=kept + TRUNCATION_SUFFIX,
        meta=PageMeta(truncated=True, truncated_chars=dropped),
    )


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


def _find_rows_container(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key in _ROWS_KEYS:
            if isinstance(value.get(key), list):
                return key
    return None


def limit_value(
    value: Any,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_chars: int = DEFAULT_MAX_CHARS,
    cursor: Optional[str] = None,
) -> LimitedResult:
    """对任意工具返回形态统一施加「行分页 + 字符截断」。

    形态分派（确定性）：

    - ``list`` → 行分页；若当页序列化后仍超 ``max_chars``，**继续缩减行数**
      直到装得下（至少保留 1 行），装不下时退化为文本截断。
    - ``dict`` 且含 ``rows`` / ``data`` / ``items`` / ``records`` / ``results``
      之一的 **list** → 对该键分页 + 同上字符约束，其余键原样保留。
    - ``str`` → 仅字符截断。
    - 其他 → 先 ``str()`` 再字符截断。
    """
    container = _find_rows_container(value)
    if isinstance(value, list):
        payload, meta = _page_and_fit(value, max_rows=max_rows, max_chars=max_chars, cursor=cursor)
    elif container is not None:
        fitted, meta = _page_and_fit(
            value[container], max_rows=max_rows, max_chars=max_chars, cursor=cursor
        )
        payload = dict(value)
        payload[container] = fitted
    elif isinstance(value, str):
        trimmed = truncate_text(value, max_chars=max_chars)
        return LimitedResult(value=trimmed.value, meta=trimmed.meta)
    else:
        text = _serialize(value)
        trimmed = truncate_text(text, max_chars=max_chars)
        return LimitedResult(value=trimmed.value, meta=trimmed.meta)

    return LimitedResult(value=payload, meta=meta)


def _page_and_fit(
    rows: Sequence[Any],
    *,
    max_rows: int,
    max_chars: int,
    cursor: Optional[str],
) -> tuple:
    """先按行分页，再按字符预算缩减行数（保证 ``truncated`` 名副其实）。"""
    paged = limit_rows(rows, max_rows=max_rows, cursor=cursor)
    page = list(paged.value)
    meta = paged.meta

    if max_chars > 0:
        original_size = len(_serialize(page))
        if original_size > max_chars:
            # 逐步缩减行数，直到装得下；至少保留 1 行（宁可截断也不要空结果）
            while len(page) > 1 and len(_serialize(page)) > max_chars:
                page = page[: max(1, len(page) // 2)]
            new_size = len(_serialize(page))
            if new_size > max_chars:
                # 单行都装不下 → 退化为文本截断（保持结果是字符串，可读）
                trimmed = truncate_text(_serialize(page), max_chars=max_chars)
                return trimmed.value, PageMeta(
                    total_rows=meta.total_rows,
                    returned_rows=0,
                    offset=meta.offset,
                    limit=meta.limit,
                    has_more=True,
                    next_cursor=meta.next_cursor or make_cursor(meta.offset),
                    truncated=True,
                    truncated_chars=trimmed.meta.truncated_chars,
                )
            meta.truncated = True
            meta.truncated_chars = original_size - new_size
            meta.returned_rows = len(page)
            meta.has_more = True
            meta.next_cursor = make_cursor(meta.offset + len(page))

    return page, meta


def _serialize(value: Any) -> str:
    try:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# 重复结果 stub（P4-4.2，借鉴 hermes tool_guardrails.py:438）
# ---------------------------------------------------------------------------


class ResultDeduplicator:
    """按工具记录上次结果的字节级哈希，重复时产出 **stub**（折叠表示）。

    源语义（source-code-study §5 4.2）：

    - **第 2 次字节级重复起**替换为 stub —— 工具**仍真实执行**，只压缩
      进入上下文的表示（本类不拦截执行，只产替身文本）；
    - **轮询不豁免**：没有任何白名单——不断轮询同一状态正是要被折叠、
      并用 stub 显式告知「与上次相同」的信号；
    - 不静默：stub 内含工具名、连续相同次数与结果哈希前缀，模型能区分
      「折叠了」与「结果为空」。

    有界：只跟踪 ``max_tools`` 个工具（LRU 淘汰），不存结果正文。
    **默认不启用**——由接线点读 ``settings.TOOL_GUARDRAILS_ENABLED``
    决定是否调用（flag-off 一次都不调，与基线逐位等价）。
    """

    def __init__(self, *, max_tools: int = 256) -> None:
        self._max_tools = max(1, int(max_tools))
        # tool -> (last_hash, repeats_since_first, first_seen_count)
        self._last: "OrderedDict[str, tuple]" = OrderedDict()

    def observe(self, tool: str, value: Any) -> Optional[str]:
        """记录一次结果；重复 ⇒ 返回 stub 文本，首次/变化 ⇒ ``None``。"""
        name = str(tool)
        blob = _serialize(value)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        entry = self._last.get(name)
        if entry is not None:
            self._last.move_to_end(name)
            last_hash, repeats, total_hits = entry
            if digest == last_hash:
                repeats += 1
                total_hits += 1
                self._last[name] = (last_hash, repeats, total_hits)
                while len(self._last) > self._max_tools:
                    self._last.popitem(last=False)
                if repeats >= 2:
                    # 第 2 次字节级重复起折叠（首次记录 + 第 1 次重复 = 保留原文）
                    return (
                        f"[重复结果已折叠] tool={name}：与前次结果字节级相同"
                        f"（连续第 {repeats} 次，共 {total_hits} 次命中）；"
                        f"工具已真实执行，结果哈希={digest[:12]}。"
                        "如需新信息请更换参数或缩小查询范围。"
                    )
                return None
        self._last[name] = (digest, 0, 1)
        while len(self._last) > self._max_tools:
            self._last.popitem(last=False)
        return None

    def reset(self, tool: Optional[str] = None) -> None:
        if tool is None:
            self._last.clear()
        else:
            self._last.pop(str(tool), None)


_deduplicator: Optional[ResultDeduplicator] = None


def get_result_deduplicator() -> ResultDeduplicator:
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = ResultDeduplicator()
    return _deduplicator


def reset_result_deduplicator() -> None:
    global _deduplicator
    _deduplicator = None
