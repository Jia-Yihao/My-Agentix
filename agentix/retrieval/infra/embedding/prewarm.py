"""E4 · embedding 缓存启动预热（路线图 `docs/design/性能与优化路线图.md` §2 E4 的后半段）。

动机
----
``EMBEDDING_CACHE_ENABLED`` 出厂就是 **true**，但它只在"某段文本被嵌入过之后"才有内容：
进程冷启动时缓存里躺着的是上一代进程写过的条目（同一 sqlite 文件），**新起来的查询问法**
第一次照样 miss，而 miss 的代价是一次 embedding 端点往返（本机 LM Studio 上量级几十到几百毫秒，
远端端点更高）。E4 的前半段（hit/miss 计数）早已落在
``agentix/retrieval/infra/embedding/cache.py:192-202``，缺的就是这一句"首刷"。

口径四条（每条都对应一处不肯妥协的实现选择）
------------------------------------------------
1. **默认关**（工程约定 1）：三个键全部 `False` / 空串 / 上限起步。开关关着时本模块
   **一个 embeddings 实例都不构造、一次网络都不发**——不是"构造了但不用"，因为构造本身
   就会新建 OpenAI 兼容客户端并读凭据，那已经越出"与基线逐位等价"这条线了。
2. **fail-open**（约定 2）：预热的作用是"让第一次查询更快"，它自己没有"必须成功"的资格。
   任何异常（缺 API key、端点未起、超时）都只进报告的 ``error`` 字段 + 一条
   ``logger.warning``，绝不外抛给启动路径。
3. **没有缓存就不预热**：``EMBEDDING_CACHE_ENABLED=false`` 时打出来的向量无处可存，
   等于每启动一次就白烧一批端点调用 ⇒ 响亮跳过（``skipped="cache_disabled"``）。
4. **不新造指标 label**（约定 3）：hit/miss 计数由缓存层既有接线负责，本模块再打一遍就是
   "同一个事实两个源"（约定 6 ④）；要新增指标先登记 ``ALLOWED_METRIC_LABELS``。
   守卫见 ``tests/test_p127_embedding_cache_prewarm.py``。

为什么预热走 ``embed_documents`` 而不是自己写 sqlite
   键计算（``sha256(model + "\\x00" + text)``）、批内去重、float32 落盘格式、FIFO 裁剪
   全都属于缓存层的内部事实。绕开它自己拼一份，等于给同一套键逻辑造第二个源——
   日后换模型或改键格式，预热这份会静默写出对不上号的条目。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from langchain_core.embeddings import Embeddings

logger = logging.getLogger(__name__)

#: 默认首刷上限。刻意是个"小"数字：预热的收益是第一条查询的时延，
#: 而代价是启动后台的一次批量请求；刷上千条会把冷启动变成一次小型灌库。
DEFAULT_PREWARM_MAX = 32

#: 清单分隔符。半角逗号**不在**其列——英文问句里 "contract, not Tort" 这种逗号
#: 会凭空造出一条不存在的"问法"来（守卫用例专门钉这一条）。
_SEPARATORS = (";", "；", "\r\n", "\n", "\r")

_REPORT_KEYS = ("requested", "cached", "embedded", "elapsed_ms", "error")


def parse_prewarm_queries(raw: str, *, limit: int = DEFAULT_PREWARM_MAX) -> list[str]:
    """把配置串拆成预热清单：切分 → 去空 → **保序**去重 → 按上限截断。

    去重先于截断：上限是"最多刷几条"的预算，让重复项吃掉配额等于用预算换零收益。
    保序是为了让运维在 `.env` 里看到的顺序与实际请求顺序一致（排查启动抖动时要用）。
    """
    if limit <= 0:
        return []
    pieces: list[str] = [raw or ""]
    for sep in _SEPARATORS:
        nxt: list[str] = []
        for piece in pieces:
            nxt.extend(piece.split(sep))
        pieces = nxt
    out: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        text = piece.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _coverage(embeddings: Embeddings, texts: Sequence[str]) -> tuple[int, int]:
    """返回 ``(去重后的条数, 已在缓存中的条数)``；未套缓存的实例一律算 0 命中。"""
    distinct = list(dict.fromkeys(texts))
    counter = getattr(embeddings, "cached_count_for", None)
    cached = 0
    if callable(counter):
        try:
            cached = int(counter(distinct))
        except Exception:
            # 只读探测失败不影响预热本身：退化成"全部未命中"，最多是多打一次端点
            logger.warning(
                "embedding prewarm coverage probe failed; assuming cold cache", exc_info=True
            )
            cached = 0
    return len(distinct), min(max(0, cached), len(distinct))


def prewarm_embeddings(embeddings: Embeddings, texts: Iterable[str]) -> dict[str, Any]:
    """按清单首刷一次；返回报告，**任何异常都不外抛**。

    报告字段（口径与 ``tests/test_p127`` 逐字一致）：

    - ``requested``：清单条数（**未去重**，就是运维配了几条）
    - ``cached``：其中已在缓存里的条数（按内容去重后计）
    - ``embedded``：本次真正向后端请求的条数（去重后的未命中数；成功才填）
    - ``elapsed_ms``：本次预热耗时
    - ``error``：失败原因（``"异常类型: 消息"``），成功为 ``None``
    """
    manifest = list(texts)
    report: dict[str, Any] = {
        "requested": len(manifest),
        "cached": 0,
        "embedded": 0,
        "elapsed_ms": 0.0,
        "error": None,
    }
    started = time.perf_counter()
    try:
        distinct, cached = _coverage(embeddings, manifest)
        report["cached"] = cached
        if manifest:
            embeddings.embed_documents(list(manifest))
        report["embedded"] = max(0, distinct - cached)
    except Exception as exc:  # noqa: BLE001 - 预热失败只留痕，不影响启动（约定 2）
        report["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "embedding cache prewarm failed after %d queries: %s",
            report["requested"],
            exc,
            exc_info=True,
        )
    report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
    return report


def _base_report() -> dict[str, Any]:
    report: dict[str, Any] = {
        key: 0 if key in ("requested", "cached", "embedded") else None for key in _REPORT_KEYS
    }
    report["elapsed_ms"] = 0.0
    report["skipped"] = None
    return report


def run_prewarm_from_settings() -> dict[str, Any]:
    """配置驱动的预热入口（启动装配只调这一个函数）。

    三种"响亮跳过"各有独立原因码，因为修法不同：``disabled`` 是没开 flag（正常态），
    ``cache_disabled`` 是开了 flag 却关着缓存（配置矛盾，运维要二选一），
    ``empty_list`` 是开了 flag 但清单没填（最常见的手顺错误：先开 flag 再补清单）。
    """
    from agentix.retrieval.settings import settings

    report = _base_report()
    if not getattr(settings, "EMBEDDING_CACHE_PREWARM_ENABLED", False):
        report["skipped"] = "disabled"
        return report
    if not getattr(settings, "EMBEDDING_CACHE_ENABLED", False):
        report["skipped"] = "cache_disabled"
        logger.warning(
            "embedding prewarm switched on but EMBEDDING_CACHE_ENABLED is false; "
            "skipping (warmed vectors would have nowhere to be stored)"
        )
        return report
    limit = int(getattr(settings, "EMBEDDING_CACHE_PREWARM_MAX", DEFAULT_PREWARM_MAX))
    raw = str(getattr(settings, "EMBEDDING_CACHE_PREWARM_QUERIES", "") or "")
    manifest = parse_prewarm_queries(raw, limit=limit)
    if not manifest:
        report["skipped"] = "empty_list"
        logger.warning(
            "embedding prewarm switched on but EMBEDDING_CACHE_PREWARM_QUERIES resolves to 0 entries; "
            "nothing to warm (configured raw length=%d, max=%d)",
            len(raw),
            limit,
        )
        return report
    report["requested"] = len(manifest)
    started = time.perf_counter()
    try:
        # 运行期导入：既是避免 import 环（client → cache → settings），也是让守卫能把这个
        # 名字换成"一调用就红"的桩，从而证伪"flag-off 逐位等价"这句话。
        from agentix.retrieval.config import get_default_config
        from agentix.retrieval.infra.embedding.client import create_embeddings

        embeddings = create_embeddings(get_default_config())
        result = prewarm_embeddings(embeddings, manifest)
    except Exception as exc:  # noqa: BLE001 - 同上：启动路径不许被预热打断（约定 2）
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        logger.warning(
            "embedding cache prewarm aborted before the first request: %s", exc, exc_info=True
        )
        return report
    for key in _REPORT_KEYS:
        report[key] = result[key]
    if result["error"]:
        logger.warning("embedding cache prewarm finished with error: %s", result)
    else:
        logger.info(
            "embedding cache prewarm done: requested=%s cached=%s embedded=%s elapsed_ms=%s",
            report["requested"],
            report["cached"],
            report["embedded"],
            report["elapsed_ms"],
        )
    return report
