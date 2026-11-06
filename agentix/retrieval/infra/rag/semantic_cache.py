"""E7 · 召回级语义结果缓存（路线图 ``docs/design/性能与优化路线图.md`` §2 E7）。

一句话：把"换了问法问同一件事"的检索结果接住。今天检索链路上已有两层缓存都只认
**逐字相同**的键——Redis 精确键（``chat_service._retrieval_cache_key``）与 embedding
内容缓存（§1.4 / E4）——所以近义问法照样全链路重跑一遍。本模块加的是第三层：
**归一化问法的 embedding 相似度 ≥ τ** ⇒ 直接复用同一作用域内近期检索到的文档。

口径六条（每条都对应一处不肯妥协的实现选择，逐条有守卫用例）
----------------------------------------------------------------
1. **默认关**（工程约定 1）：``SEMANTIC_CACHE_ENABLED=false`` 时
   :func:`semantic_recall_peek` 在**构造 embeddings 之前**就返回 ``None`` ⇒ 不建客户端、
   不发 embedding。等价性不是措辞：守卫把 ``create_embeddings`` 换成"一调用就 raise"的桩。
2. **τ 只有一个源**（约定 6 ④）：相似度与 τ 的比较只发生在 :func:`best_match` 里，
   τ 只从 ``settings.SEMANTIC_CACHE_TAU`` 读一次。调用方拿到 :class:`SemanticPeek`
   之后不许再自己比分数——再判一次就会出现"命中"与"看起来命中"两本账。
3. **权限/租户隔离是结构性的**：条目按 bucket 分桶，bucket 由调用方从**与精确键同一个**
   键构造函数派生（租户 / kb / artifact / search_k / scope_token / hybrid / 改写后缀都在里面）。
   空 bucket 一律不读写。语义相似度**不能**替代权限隔离——这与
   ``agentix/application/services/redis_cache.py`` 中"接入前必读"那条护栏说的是同一件事，
   也是路线图 E7 风险段"拒绝缓存高风险域"的落地形态。
4. **向量空间必须同源**：bucket 末尾带 embedding 模型名（``|m:<model>``）。换模型后新旧
   向量不在同一空间，共池就是拿余弦的**数值**去比**两把不同的尺**——最坏的形态是静默命中
   错答案，而不是报错。
5. **fail-open**（约定 2）：embedding 失败 / 维度不符 / 载荷坏 / 打点炸，全部按"未命中"
   处理并留一条 ``logger.warning``；缓存是加速件，没有任何资格让一次查询失败。
6. **有界**：每个 bucket 至多 :data:`MAX_ENTRIES_PER_BUCKET` 条（常量，不占配置面，理由见
   该常量注释），并按 ``SEMANTIC_CACHE_TTL_SECONDS`` 过期。进程内、每 worker 各持一份 ⇒
   多进程之间不共享，这是刻意的代价（换 Redis 共享只需替换 :class:`SemanticRecallCache`
   的存储面，判定面 :func:`best_match` 不动——这正是路线图说的"命中判定与存储解耦"）。

为什么复用 ``agentix.domain.policies.memory_recall.cosine_similarity`` 而不是自己写一份：
全仓已经有两处余弦实现（memory_recall 版把负值截 0、维度不符返回 0.0；
``redis_cache._cosine_similarity`` 版不截负值，且维度不符时被 ``zip`` 静默按短边算）。
第三份实现就是约定 6 ④ 说的"同一个判定两个源"。memory_recall 版的两条语义恰好都是本模块
要的：**维度不符永不命中**（换模型/被截断的向量不该命中），**负相似度不命中**（τ 接近 1，
截 0 无副作用）。那处既有的重复登记在路线图 §2 E7 的残余项里，不在本批顺手合并——
``redis_cache`` 那个类至今没接线，改它的数值口径没有可验证的收益。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)

#: 默认相似度阈值。取路线图 E7 风险段建议的保守值 0.95，而不是既有
#: ``REDIS_CACHE_THRESHOLD`` 出厂的 0.92：召回级命中的代价是"用别人那次检索的结果"，
#: 串答比省一次检索严重得多。
DEFAULT_TAU = 0.95

#: 默认 TTL（秒）。刻意比精确键的 ``RETRIEVAL_CACHE_TTL_SECONDS``（600）短一个量级：
#: 精确键复用的是"同一句话"的结果，语义键复用的是"另一句话"的结果，后者必须更快失效。
DEFAULT_TTL_SECONDS = 120

#: 每个 bucket 的条目上限。**刻意不是配置键**：它防的是"长尾问法把内存吃光"，
#: 与运维想调的行为无关；开一个键就多一处要五处同步的面（约定 4）。
#: 量级：1024 维 float 向量 × 64 条 ≈ 512KB/bucket（不含文档载荷）。
MAX_ENTRIES_PER_BUCKET = 64

#: 指标 ``cache_type`` 的取值。刻意与既有值 ``"semantic"`` 分开——那个值属于
#: ``RedisSemanticCache``（**答案级**，至今未接线）；把两种缓存打进同一条时间序列，
#: 命中率曲线就再也说不清是谁的。值必须是**有界常量**（不掺 bucket / 问法文本 / 租户）。
CACHE_TYPE = "semantic_recall"

#: 降级打点的 ``component`` 取值（``degraded_total{component}``，label 名既有且已进白名单）。
DEGRADED_COMPONENT = "semantic_cache"


@dataclass(frozen=True)
class SemanticMatch:
    """一次语义命中的可观测载荷。

    ``score`` 是**留给日志**的，不是留给调用方再判一次的（口径 2）。
    """

    payload: str
    score: float
    age_seconds: float


@dataclass(frozen=True)
class SemanticPeek:
    """``semantic_recall_peek()`` 的结果：本次问法的向量 + 命中（若有）。

    ``vector`` 必须带出来给 :func:`semantic_remember` 复用——回写时重新嵌入一次，
    等于让"命中判定用的向量"与"缓存里存的向量"分两次取，中间换了端点或模型就会写出
    自己对不上号的条目。
    """

    bucket: str
    query: str
    vector: tuple[float, ...]
    match: SemanticMatch | None

    @property
    def hit(self) -> bool:
        return self.match is not None


def best_match(
    vector: Sequence[float],
    candidates: Sequence[tuple[Sequence[float], float]],
    *,
    tau: float,
    now: float | None = None,
) -> tuple[int, float] | None:
    """纯判定：返回 ``(候选下标, 相似度)``，都不到 τ 就返回 ``None``。

    ``candidates`` 是 ``[(向量, 写入时刻), ...]``。同分时取**更新鲜**的那条：同分意味着
    两条问法在向量空间里一样近，新鲜的那条更可能仍反映当前索引。不读 settings、不碰锁，
    所以能直接单测 τ 的边界（命中/反例都在这一个函数上）。
    """
    if not vector or not candidates or tau <= 0.0:
        return None
    from agentix.domain.policies.memory_recall import cosine_similarity

    clock = time.monotonic() if now is None else float(now)
    best_index: int | None = None
    best_score = 0.0
    best_age = float("inf")
    for index, (candidate_vector, stored_at) in enumerate(candidates):
        score = cosine_similarity(list(vector), list(candidate_vector))
        if score < tau:
            continue
        age = max(0.0, clock - float(stored_at))
        if best_index is None or score > best_score or (score == best_score and age < best_age):
            best_index, best_score, best_age = index, score, age
    if best_index is None:
        return None
    return best_index, best_score


class SemanticRecallCache:
    """进程内、按 bucket 分桶的语义结果缓存（只做存储与过期，判定委托 :func:`best_match`）。"""

    def __init__(self, *, max_entries_per_bucket: int = MAX_ENTRIES_PER_BUCKET) -> None:
        self._lock = threading.Lock()
        self._max_entries = max(1, int(max_entries_per_bucket))
        # bucket → [(向量, 载荷, 写入时刻)]
        self._buckets: dict[str, list[tuple[tuple[float, ...], str, float]]] = {}

    def lookup(
        self,
        bucket: str,
        vector: Sequence[float],
        *,
        tau: float,
        ttl_seconds: int,
        now: float | None = None,
    ) -> SemanticMatch | None:
        """命中返回 :class:`SemanticMatch`，否则 ``None``；过期条目在读取时就地清掉。

        TTL 在**读取时**用当前配置算（条目只存写入时刻），所以把 120 改成 30 立即生效，
        不需要等旧条目按旧窗口漂完——运维改 TTL 想要的就是"从现在起更短"。
        """
        if not bucket or not vector or tau <= 0.0 or ttl_seconds <= 0:
            return None
        clock = time.monotonic() if now is None else float(now)
        with self._lock:
            entries = self._buckets.get(bucket)
            if not entries:
                return None
            fresh = [row for row in entries if clock - float(row[2]) <= float(ttl_seconds)]
            if len(fresh) != len(entries):
                if fresh:
                    self._buckets[bucket] = fresh
                else:
                    self._buckets.pop(bucket, None)
                entries = fresh
                if not entries:
                    return None
            matched = best_match(vector, [(row[0], row[2]) for row in entries], tau=tau, now=clock)
            if matched is None:
                return None
            index, score = matched
            row = entries[index]
            return SemanticMatch(
                payload=row[1], score=float(score), age_seconds=max(0.0, clock - float(row[2]))
            )

    def store(
        self,
        bucket: str,
        vector: Sequence[float],
        payload: str,
        *,
        ttl_seconds: int,
        now: float | None = None,
    ) -> bool:
        """写入一条；空 bucket / 空向量 / 空载荷 / 非正 TTL 一律拒写并返回 ``False``。

        同一问法（向量逐位相同）重复写入会**刷新时间戳**而不是堆重复条目：重复条目挤掉的
        是真正不同的问法，纯粹是亏。近似重复（τ 以上但不逐位相同）刻意各自留一条——
        它们是"不同问法的答案"，合并就等于提前宣布二者等价。

        ``payload.strip()`` 为空也拒写：全空白与空串是同一个东西的两种伪装，而"空白载荷"
        被命中时会给调用方一个解析不出文档的字符串，比不命中更难查。
        """
        if not bucket or not vector or not payload.strip() or ttl_seconds <= 0:
            return False
        moment = time.monotonic() if now is None else float(now)
        row = (tuple(float(x) for x in vector), payload, moment)
        with self._lock:
            entries = self._buckets.setdefault(bucket, [])
            same = _index_of_identical(row[0], [item[0] for item in entries])
            if same is not None:
                entries[same] = row
                return True
            entries.append(row)
            while len(entries) > self._max_entries:
                entries.pop(0)
            return True

    def stats(self) -> dict[str, int]:
        """只读观测（诊断与用例用，**不**进 Prometheus 指标面）。"""
        with self._lock:
            return {
                "buckets": len(self._buckets),
                "entries": sum(len(rows) for rows in self._buckets.values()),
                "max_entries_per_bucket": self._max_entries,
            }

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


def _index_of_identical(
    vector: tuple[float, ...], existing: Sequence[tuple[float, ...]]
) -> int | None:
    """找出与 ``vector`` 逐位相同的条目下标（重复写入去重）。不做近似比较。"""
    for index, candidate in enumerate(existing):
        if len(candidate) == len(vector) and all(a == b for a, b in zip(candidate, vector)):
            return index
    return None


_CACHE = SemanticRecallCache()


def get_semantic_recall_cache() -> SemanticRecallCache:
    return _CACHE


def reset_semantic_recall_cache() -> None:
    """清空进程内缓存。测试隔离与运维复位用；生产接线不依赖它。"""
    _CACHE.clear()


def semantic_recall_stats() -> dict[str, int]:
    return _CACHE.stats()


def _config() -> dict[str, Any]:
    """读取本模块需要的配置值——**只在这里读 settings**（约定 6 ④）。"""
    from agentix.retrieval.settings import settings

    return {
        "enabled": bool(getattr(settings, "SEMANTIC_CACHE_ENABLED", False)),
        "tau": float(getattr(settings, "SEMANTIC_CACHE_TAU", DEFAULT_TAU) or DEFAULT_TAU),
        "ttl_seconds": int(
            getattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)
            or DEFAULT_TTL_SECONDS
        ),
        "model": str(getattr(settings, "EMBEDDING_MODEL", "") or ""),
    }


def _count(outcome: str) -> None:
    """hit/miss 打点：复用既有 ``cache_hits_total{cache_type}``，失败绝不影响主链路。"""
    try:
        from agentix.observability.metrics import record_cache_hit, record_cache_miss

        if outcome == "hit":
            record_cache_hit(CACHE_TYPE)
        else:
            record_cache_miss(CACHE_TYPE)
    except Exception:  # noqa: BLE001 - 埋点失败不许打挂检索（R-P0-5 同口径）
        logger.debug("semantic cache metric %s failed", outcome, exc_info=True)


def _degraded(reason: str) -> None:
    try:
        from agentix.observability.metrics import record_degraded

        record_degraded(DEGRADED_COMPONENT)
    except Exception:  # noqa: BLE001 - 计数上报失败不再二次抛出
        logger.debug("semantic cache degraded counter failed", exc_info=True)
    logger.warning("semantic recall degraded: %s", reason)


_EMBEDDINGS_LOCK = threading.Lock()
_EMBEDDINGS_SIGNATURE: tuple[str, str] | None = None
_EMBEDDINGS_INSTANCE: Any = None


def _embeddings_or_none():
    """取进程内复用的 embeddings 实例（按 ``(model, base_url)`` 签名重建）。

    为什么要自己攒一份，而不是每次问一次构造一次：``create_embeddings()`` 会新建一个
    OpenAI 兼容客户端，docs/已知限制.md 登记的实测结论是这段构造在仓库 ``.venv`` 上是
    **16–24ms**（另一套 SDK 栈上 2–4s）。一个"省延迟"的缓存若每次查询先付一次构造费，
    就只在命中时才划算——而命中率本身要靠它跑出来。签名里带模型名与 base_url ⇒
    换端点/换模型即重建实例，不会继续拿旧客户端算新向量（那正是口径 4 要避免的分家）。

    **运行期导入**：既是避免 import 环（client → cache → settings），也是让守卫能把
    ``create_embeddings`` 换成"一调用就 raise"的桩，从而证伪 flag-off 逐位等价这句话
    ——与 E4 预热同口径。
    """
    global _EMBEDDINGS_SIGNATURE, _EMBEDDINGS_INSTANCE

    from agentix.retrieval.config import get_default_config
    from agentix.retrieval.infra.embedding.client import create_embeddings

    cfg = get_default_config()
    signature = (
        str(cfg.get("embedding_model") or ""),
        str(cfg.get("embedding_base_url") or ""),
    )
    if _EMBEDDINGS_INSTANCE is not None and _EMBEDDINGS_SIGNATURE == signature:
        return _EMBEDDINGS_INSTANCE
    with _EMBEDDINGS_LOCK:
        if _EMBEDDINGS_INSTANCE is None or _EMBEDDINGS_SIGNATURE != signature:
            _EMBEDDINGS_INSTANCE = create_embeddings(cfg)
            _EMBEDDINGS_SIGNATURE = signature
        return _EMBEDDINGS_INSTANCE


def reset_embeddings_holder() -> None:
    """丢掉复用的 embeddings 实例（测试隔离用；生产没有调用点）。"""
    global _EMBEDDINGS_SIGNATURE, _EMBEDDINGS_INSTANCE
    with _EMBEDDINGS_LOCK:
        _EMBEDDINGS_INSTANCE = None
        _EMBEDDINGS_SIGNATURE = None


def semantic_recall_peek(bucket_scope: str, query: str) -> SemanticPeek | None:
    """查一次语义缓存。返回 ``None`` 表示**这次没得判**（关闭 / 空输入 / 拿不到向量）。

    与"判过但未命中"（返回 ``SemanticPeek(match=None)``）刻意分开：前者不该记 miss——
    分母里混进"压根没配置"会把命中率曲线打成噪声；后者才是一次真 miss。
    """
    config = _config()
    if not config["enabled"] or not bucket_scope or not (query or "").strip():
        return None
    try:
        embeddings = _embeddings_or_none()
        vector = [float(x) for x in embeddings.embed_query(query)]
    except Exception as exc:  # noqa: BLE001 - 拿不到向量就当这层缓存不存在（约定 2）
        _degraded(f"embedding unavailable: {type(exc).__name__}: {exc}")
        return None
    if not vector:
        _degraded("embedding returned an empty vector")
        return None
    model = str(getattr(embeddings, "model", "") or config["model"])
    bucket = f"{bucket_scope}|m:{model}"
    match = _CACHE.lookup(bucket, vector, tau=config["tau"], ttl_seconds=config["ttl_seconds"])
    if match is None:
        _count("miss")
        return SemanticPeek(bucket=bucket, query=query, vector=tuple(vector), match=None)
    _count("hit")
    logger.info(
        "semantic recall hit bucket=%s score=%.4f age_s=%.1f payload_bytes=%d",
        bucket,
        match.score,
        match.age_seconds,
        len(match.payload),
    )
    return SemanticPeek(bucket=bucket, query=query, vector=tuple(vector), match=match)


def semantic_remember(peek: SemanticPeek | None, payload: str) -> bool:
    """把刚算出来的检索结果写回缓存。

    ``peek is None`` ⇒ 这次没得判（没向量就没法语义复用），**不写**；
    空载荷 / ``"[]"`` 也不写——把"检索为空"钉进缓存等于让空结果在 TTL 内反复命中，
    而灌库完成后的正常收敛恰恰是"从空变成有"。
    """
    if peek is None or not payload or payload.strip() in ("[]", "null"):
        return False
    config = _config()
    if not config["enabled"]:
        return False
    try:
        return _CACHE.store(peek.bucket, peek.vector, payload, ttl_seconds=config["ttl_seconds"])
    except Exception as exc:  # noqa: BLE001 - 写失败只影响下次能不能省（约定 2）
        _degraded(f"store failed: {type(exc).__name__}: {exc}")
        return False
