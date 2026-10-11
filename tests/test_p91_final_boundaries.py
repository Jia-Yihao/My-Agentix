"""P91 终验边界测试：安全护栏 + 低覆盖模块的边界行为钉死。

最终交付批次补齐的边界（对应"安全隐患排除 + 边界测试"）：
1. MySQL ingest 注入护栏：表名标识符白名单、where 拒绝堆叠语句/注释、limit 下界、
   校验通过后的 SQL 形态（`MySQLIngestor._build_query` 此前无任何测试）。
2. ``agentix/security/output_filter.py``（覆盖率 32%）：PII 脱敏边界、有害内容拦截、
   幻觉检测的空上下文/阈值边界。
3. ``agentix/retrieval/infra/embedding/cache.py``（覆盖率 26%）：sqlite 读写往返、
   FIFO 裁剪边界、模型隔离键、缓存/内嵌失败降级。
4. ``agentix/infrastructure/core/ratelimit.py``（覆盖率 44%）：CircuitBreaker 状态机
   与 TokenBucket 容量/补充/突发上限边界（此前仅测 dataclass 字段）。

约定：异步入口一律 ``asyncio.run``（与既有套件一致，不引入 pytest-asyncio）。
"""

import asyncio
import time
from datetime import date

import pydantic
import pytest
from fastapi import HTTPException
from langchain_core.embeddings import Embeddings

from agentix.infrastructure.core.ratelimit import (
    CircuitBreaker,
    CircuitBreakerConfig,
    RateLimitConfig,
    TokenBucketRateLimiter,
)
from agentix.security.output_filter import (
    HallucinationDetector,
    HarmCategory,
    HarmfulContentDetector,
    OutputFilter,
    PIIMasker,
)
from kb_service.schemas.ingest import MySQLIngestRequest
from kb_service.services.mysql_ingest import MySQLIngestor
from fakes.redis import FakeRedis  # type: ignore[import-not-found]
from agentix.retrieval.infra.embedding.cache import (
    CachedEmbeddings,
    EmbeddingCache,
    embedding_cache_key,
)


def _mysql_request(**overrides) -> MySQLIngestRequest:
    payload = {"connection_url": "mysql+pymysql://user:pw@host/db", "table": "laws"}
    payload.update(overrides)
    return MySQLIngestRequest(**payload)


# ============================================================
# 1. MySQL ingest SQL 注入护栏
# ============================================================


class TestMySQLIngestSQLGuard:
    def test_valid_table_identifiers_pass(self):
        for table in ("laws", "a", "law_2024_v2", "A" * 64):
            assert _mysql_request(table=table).table == table

    @pytest.mark.parametrize(
        "bad_table",
        [
            "law table",      # 空格
            "laws;DROP TABLE users",  # 分号堆叠
            "db.laws",        # 点号（反引号内会被当作字面表名，本就不支持）
            "la`ws",          # 反引号逃逸
            "la'ws",          # 单引号
            "la-ws",          # 连字符
            "",               # 空串
            "a" * 65,         # 超长
            "法律",            # 非 ASCII 标识符
        ],
    )
    def test_bad_table_identifiers_rejected(self, bad_table):
        with pytest.raises(pydantic.ValidationError, match="table"):
            _mysql_request(table=bad_table)

    @pytest.mark.parametrize("bad_where", ["id=1; DROP TABLE users", "id=1 -- x", "id=1 /* x */"])
    def test_where_stacked_statements_and_comments_rejected(self, bad_where):
        with pytest.raises(pydantic.ValidationError, match="where"):
            _mysql_request(where=bad_where)

    def test_where_normal_predicate_and_none_allowed(self):
        assert _mysql_request(where="status = 'in_force' AND year > 2020").where
        assert _mysql_request(where=None).where is None

    @pytest.mark.parametrize("limit,ok", [(1, True), (0, False), (-1, False)])
    def test_limit_lower_bound(self, limit, ok):
        if ok:
            assert _mysql_request(limit=limit).limit == limit
        else:
            with pytest.raises(pydantic.ValidationError):
                _mysql_request(limit=limit)

    def test_build_query_shape_after_validation(self):
        request = _mysql_request(where="source = 'npc'", limit=5)
        sql = str(MySQLIngestor._build_query(None, request))
        assert sql == "SELECT * FROM `laws` WHERE source = 'npc' LIMIT 5"

    def test_build_query_without_where_or_limit(self):
        sql = str(MySQLIngestor._build_query(None, _mysql_request()))
        assert sql == "SELECT * FROM `laws`"

    def test_legal_date_fields_still_accepted(self):
        request = _mysql_request(
            legal_effective_from=date(2024, 1, 1),
            legal_effective_to=date(2099, 12, 31),
        )
        assert request.legal_effective_from.year == 2024


# ============================================================
# 2. output_filter：PII / 有害内容 / 幻觉检测边界
# ============================================================


class TestPIIMaskerBoundary:
    def test_phone_length_boundary(self):
        masker = PIIMasker()
        # 恰好 11 位且符合 1[3-9] 开头 → 命中
        text, detected = masker.mask("联系电话13812345678")
        assert detected == ["phone"]
        assert "1**********" in text
        # 10 位 → 不命中
        text, detected = masker.mask("短号1381234567")
        assert detected == []
        assert text == "短号1381234567"
        # 12 位 → 未锚定正则吃掉前 11 位（钉死当前语义）
        text, detected = masker.mask("138123456789")
        assert detected == ["phone"]
        assert text == "1**********9"

    def test_id_card_with_trailing_x_masked(self):
        masker = PIIMasker()
        # 2005 年出生（避免 19xx 年份让 phone 正则先行吃掉片段——钉死顺序语义）
        text, detected = masker.mask("身份证号11010120050101123X")
        assert detected == ["id_card"]
        assert "11010120050101123X" not in text
        assert "*******************" in text

    def test_bank_card_16_matches_15_does_not(self):
        masker = PIIMasker()
        text, detected = masker.mask("卡号6222021234567890")
        assert detected == ["bank_card"]
        assert "6222021234567890" not in text
        text, detected = masker.mask("卡号622202123456789")
        assert detected == []
        assert text.endswith("622202123456789")

    def test_email_ip_and_clean_text(self):
        masker = PIIMasker()
        text, detected = masker.mask("联系 a.b+tag@example.com 或 10.0.0.1")
        assert set(detected) == {"email", "ip_address"}
        assert "a.b+tag@example.com" not in text
        assert "***.***.***.***" in text
        text, detected = masker.mask("纯文本没有敏感信息")
        assert detected == []
        assert text == "纯文本没有敏感信息"


class TestHarmfulContentBoundary:
    def test_harmful_keyword_blocks_output_filter(self):
        filt = OutputFilter(enable_hallucination_detection=False)
        result = asyncio.run(filt.filter("警告：有人在网上出售爆炸物"))
        assert result.blocked is True
        assert "violence" in result.reason
        assert result.filtered_text == ""
        assert "violence" in result.harm_detected

    def test_pii_only_output_is_masked_not_blocked(self):
        filt = OutputFilter(enable_hallucination_detection=False)
        result = asyncio.run(filt.filter("请拨打13812345678咨询"))
        assert result.blocked is False
        assert "phone" in result.pii_detected
        assert "13812345678" not in result.filtered_text
        assert result.filtered_text == "请拨打1**********咨询"

    def test_custom_keywords_do_not_leak_across_instances(self):
        # 注：keywords 以 HarmCategory 枚举为键；用枚举键才能并入既有类别，
        # 用字符串键会落成并列的新键（模块现状，另有 test 固化其可检出性）。
        detector_a = HarmfulContentDetector({HarmCategory.VIOLENCE: ["drill_rifle"]})
        detector_b = HarmfulContentDetector()
        assert "drill_rifle" in detector_a.keywords[HarmCategory.VIOLENCE]
        assert "drill_rifle" not in detector_b.keywords[HarmCategory.VIOLENCE]
        # 类属性共享库不允许被实例污染（代码注释里钉死过的坑）
        assert "drill_rifle" not in HarmfulContentDetector.KEYWORDS[HarmCategory.VIOLENCE]

    def test_string_category_key_lands_as_parallel_key(self):
        # 字符串类名不在枚举键集合里 → 落成并列 str 键，detect 仍可检出
        detector = HarmfulContentDetector({"corruption": ["bribe"]})
        assert "bribe" not in detector.keywords[HarmCategory.ILLEGAL]
        is_harmful, categories, keywords = detector.detect("accept a bribe")
        assert is_harmful is True
        assert "corruption" in categories
        assert "bribe" in keywords

    def test_custom_keyword_matches_case_insensitively(self):
        detector = HarmfulContentDetector({HarmCategory.ILLEGAL: ["launder"]})
        is_harmful, categories, keywords = detector.detect("They LAUNDER dirty money")
        assert is_harmful is True
        assert HarmCategory.ILLEGAL in categories
        assert "launder" in keywords

    def test_all_features_disabled_passes_through(self):
        filt = OutputFilter(
            enable_pii_masking=False,
            enable_harm_detection=False,
            enable_hallucination_detection=False,
        )
        result = asyncio.run(filt.filter("含爆炸与手机号13812345678的原文"))
        assert result.blocked is False
        assert result.filtered_text == "含爆炸与手机号13812345678的原文"
        assert result.pii_detected == []
        assert result.hallucination_score == 1.0


class TestHallucinationDetectorBoundary:
    def test_empty_context_scores_half_and_ungrounded(self):
        detector = HallucinationDetector(threshold=0.7)
        result = asyncio.run(detector.detect("任何回答", []))
        assert result.score == 0.5
        assert result.is_grounded is False
        assert result.unsupported_claims == ["无上下文可验证"]

    def test_no_extractable_tokens_scores_full(self):
        detector = HallucinationDetector()
        result = asyncio.run(detector.detect("hello world", ["unrelated context"]))
        assert result.score == 1.0
        assert result.is_grounded is True
        assert result.unsupported_claims == []

    def test_number_mismatch_flagged(self):
        detector = HallucinationDetector()
        result = asyncio.run(
            detector.detect(
                "合同编号 ABCD 有 43 条记录",
                ["合同编号 ABCD 有 42 条记录"],
            )
        )
        assert result.details["keyword_score"] == 1.0
        assert result.details["number_score"] == 0.0
        assert result.unsupported_claims == ["数字 '43' 未在上下文中找到"]

    def test_threshold_comparison_is_inclusive(self):
        context = ["合同编号 ABCD 有 42 条记录"]
        answer = "合同编号 ABCD 有 43 条记录"
        first = asyncio.run(HallucinationDetector().detect(answer, context))
        at_score = asyncio.run(HallucinationDetector(threshold=first.score).detect(answer, context))
        assert at_score.is_grounded is True  # score >= threshold → 含等号
        above = asyncio.run(
            HallucinationDetector(threshold=first.score + 1e-9).detect(answer, context)
        )
        assert above.is_grounded is False


# ============================================================
# 3. embedding 本地缓存边界（sqlite）
# ============================================================


class _CountingEmbeddings(Embeddings):
    """记录调用次数与入参的内层桩。"""

    def __init__(self, mismatch: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.mismatch = mismatch

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.mismatch:
            return [[0.0]]
        return [[float(len(t)), float(sum(map(ord, t)))] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class _BrokenCache:
    def get_many(self, keys: list[str]) -> dict[str, list[float]]:
        raise RuntimeError("read boom")

    def put_many(self, items: dict[str, list[float]], *, model: str) -> None:
        raise RuntimeError("write boom")


class TestEmbeddingCacheBoundary:
    def test_roundtrip_and_partial_hits(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"))
        try:
            cache.put_many({"k1": [0.5, -1.25, 3.0], "k2": [1.0]}, model="m")
            assert cache.count() == 2
            hits = cache.get_many(["k1", "missing", "k2"])
            assert set(hits) == {"k1", "k2"}
            assert hits["k1"] == [0.5, -1.25, 3.0]  # float32 精确可表示值
            assert cache.get_many([]) == {}
        finally:
            cache.close()

    def test_put_many_empty_is_noop(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"))
        try:
            cache.put_many({}, model="m")
            assert cache.count() == 0
        finally:
            cache.close()

    def test_fifo_prune_at_capacity_boundary(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"), max_entries=2)
        try:
            cache.put_many({"k1": [1.0]}, model="m")
            time.sleep(0.02)
            cache.put_many({"k2": [2.0]}, model="m")
            assert cache.count() == 2  # 恰好等于上限 → 不裁剪
            time.sleep(0.02)
            cache.put_many({"k3": [3.0]}, model="m")
            # 超上限 → FIFO 裁剪到 max(1, int(2*0.8))=1，最新写入保留
            assert cache.count() == 1
            assert "k3" in cache.get_many(["k1", "k2", "k3"])
        finally:
            cache.close()

    def test_cache_key_isolated_by_model(self):
        assert embedding_cache_key("同一文本", "model-a") != embedding_cache_key("同一文本", "model-b")
        assert embedding_cache_key("t", "m") == embedding_cache_key("t", "m")


class TestCachedEmbeddingsBoundary:
    def test_batch_dedup_and_order_preserved(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"))
        inner = _CountingEmbeddings()
        wrapped = CachedEmbeddings(inner, cache, model="m")
        try:
            result = wrapped.embed_documents(["a", "a", "b"])
            assert len(inner.calls) == 1
            assert inner.calls[0] == ["a", "b"]  # 批内去重，只请求一次
            assert result[0] == result[1]  # 返回顺序与输入一一对应（含重复）
            assert result[0] != result[2]
            # 二次调用全部命中缓存 → 内层不再被调用
            wrapped.embed_documents(["a", "b"])
            assert len(inner.calls) == 1
        finally:
            cache.close()

    def test_empty_input_skips_inner(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"))
        inner = _CountingEmbeddings()
        wrapped = CachedEmbeddings(inner, cache, model="m")
        try:
            assert wrapped.embed_documents([]) == []
            assert inner.calls == []
        finally:
            cache.close()

    def test_inner_count_mismatch_raises(self, tmp_path):
        cache = EmbeddingCache(str(tmp_path / "c.db"))
        wrapped = CachedEmbeddings(_CountingEmbeddings(mismatch=True), cache, model="m")
        try:
            with pytest.raises(ValueError, match="条数"):
                wrapped.embed_documents(["a", "b"])
        finally:
            cache.close()

    def test_cache_failures_degrade_gracefully(self):
        inner = _CountingEmbeddings()
        wrapped = CachedEmbeddings(inner, _BrokenCache(), model="m")
        result = wrapped.embed_documents(["a", "b"])
        assert len(result) == 2
        assert len(inner.calls) == 1  # 读失败→按未命中降级；写失败→吞掉不影响返回


# ============================================================
# 4. CircuitBreaker 状态机 + TokenBucket 容量边界
# ============================================================


class TestCircuitBreakerBoundary:
    def test_fresh_breaker_closed_and_success_keeps_closed(self):
        async def scenario():
            breaker = CircuitBreaker("p91", redis_client=FakeRedis())
            assert (await breaker.get_state()).value == "closed"

            async def ok():
                return 7

            assert await breaker.call(ok) == 7
            return await breaker.get_state()

        assert asyncio.run(scenario()).value == "closed"

    def test_trips_exactly_at_failure_threshold(self):
        async def scenario():
            breaker = CircuitBreaker(
                "p91_trip",
                redis_client=FakeRedis(),
                config=CircuitBreakerConfig(failure_threshold=3, recovery_timeout=30),
            )
            for i in range(2):  # threshold-1 次失败 → 仍 CLOSED
                with pytest.raises(RuntimeError):
                    await breaker.call(_async_boom)
            assert (await breaker.get_state()).value == "closed"
            with pytest.raises(RuntimeError):  # 第 3 次 → 立即 OPEN
                await breaker.call(_async_boom)
            assert (await breaker.get_state()).value == "open"
            with pytest.raises(HTTPException) as exc:  # OPEN 期间直接 503
                await breaker.call(_async_value, 1)
            assert exc.value.status_code == 503

        asyncio.run(scenario())

    def test_half_open_recovery_after_timeout(self):
        async def scenario():
            breaker = CircuitBreaker(
                "p91_recover",
                redis_client=FakeRedis(),
                config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.01),
            )
            with pytest.raises(RuntimeError):
                await breaker.call(_async_boom)
            assert (await breaker.get_state()).value == "open"
            await asyncio.sleep(0.05)  # 越过 recovery_timeout
            assert await breaker.call(_async_value, 1) == 1  # HALF_OPEN 放行并成功
            assert (await breaker.get_state()).value == "closed"  # 成功即恢复

        asyncio.run(scenario())

    def test_half_open_failure_reopens(self):
        async def scenario():
            breaker = CircuitBreaker(
                "p91_reopen",
                redis_client=FakeRedis(),
                config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.01),
            )
            with pytest.raises(RuntimeError):
                await breaker.call(_async_boom)
            await asyncio.sleep(0.05)
            with pytest.raises(RuntimeError):  # HALF_OPEN 下再失败
                await breaker.call(_async_boom)
            assert (await breaker.get_state()).value == "open"

        asyncio.run(scenario())

    def test_reset_returns_to_closed(self):
        async def scenario():
            breaker = CircuitBreaker(
                "p91_reset",
                redis_client=FakeRedis(),
                config=CircuitBreakerConfig(failure_threshold=1),
            )
            with pytest.raises(RuntimeError):
                await breaker.call(_async_boom)
            assert (await breaker.get_state()).value == "open"
            await breaker.reset()
            assert (await breaker.get_state()).value == "closed"

        asyncio.run(scenario())


async def _async_value(v):
    return v


async def _async_boom():
    raise RuntimeError("boom")


class TestTokenBucketBoundary:
    def _limiter(self, redis: FakeRedis, max_tokens: int = 2) -> TokenBucketRateLimiter:
        return TokenBucketRateLimiter(
            redis_client=redis,
            config=RateLimitConfig(
                max_tokens=max_tokens,
                refill_rate=1.0,
                refill_interval=1.0,
                burst_multiplier=1.5,
            ),
        )

    def test_exact_capacity_then_denied(self):
        async def scenario():
            redis = FakeRedis()
            limiter = self._limiter(redis, max_tokens=2)
            assert await limiter.acquire("user") is True
            assert await limiter.acquire("user") is True  # 恰好耗尽容量
            assert await limiter.acquire("user") is False  # 第 3 次拒绝
            assert await limiter.get_remaining("user") < 1.0  # 剩余不足以再取一枚
            await limiter.reset("user")
            assert await limiter.get_remaining("user") == 2.0  # reset 归还全量

        asyncio.run(scenario())

    def test_refill_restores_tokens(self):
        async def scenario():
            redis = FakeRedis()
            limiter = self._limiter(redis, max_tokens=2)
            assert await limiter.acquire("user") is True
            assert await limiter.acquire("user") is True
            assert await limiter.acquire("user") is False
            # 把 last_refill 拨回 2.5s 前 → 按 1 token/s 补充 2.5 枚
            redis.hashes["ratelimit:user"]["last_refill"] = str(time.time() - 2.5)
            assert await limiter.acquire("user") is True
            assert await limiter.acquire("user") is True
            assert await limiter.acquire("user") is False

        asyncio.run(scenario())

    def test_burst_cap_and_fresh_key_returns_full(self):
        async def scenario():
            redis = FakeRedis()
            limiter = self._limiter(redis, max_tokens=2)
            # 新 key（无桶）→ 视为满容量
            assert await limiter.get_remaining("fresh") == 2.0
            await limiter.acquire("user")
            redis.hashes["ratelimit:user"]["last_refill"] = str(time.time() - 10_000)
            # 搁置过期 → 补充量被 max_tokens * burst_multiplier 封顶（2*1.5=3）
            assert await limiter.get_remaining("user") == pytest.approx(3.0, abs=0.01)

        asyncio.run(scenario())
