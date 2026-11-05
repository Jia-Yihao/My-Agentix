"""T6：拆开 TTFT 混采并给兜底路径单独计量。

验收口径逐条对应：
① 主口径（``agent_ttft_seconds``）与兜底路径（``agent_ttft_fallback_seconds``）可分别查询、
   互不污染——采用**独立指标**方案 ⇒ 主直方图定义与记录助手逐字不动；
② 白名单（``tests/test_p100_genai_observability.py::ALLOWED_METRIC_LABELS``）：本方案
   **零新增 label**，p100 全局守卫照常绿（本文件另钉死两直方图除桶位 ``le`` 外无标签）；
③ 桶边界零改动（Q2 未决，不细分）：``EXPECTED_TTFT_LE`` 钉死既有 le 取值集合，改桶即红；
④ 无高基数标签（约定 3）：同上，两个 TTFT 直方图均为零自定义标签。

判定同源（约定 6 ④）：「是否走了兜底」由回退链**自己**导出——
``ModelGateway.ainvoke_with_fallback`` 在它的成败判定点调用 ``record_gateway_fallback``
（model_gateway.py 既有分支），该调用顺带置位本流事实；发射器只读事实，不改写、不再判定。
兜底事实的全部驱动都来自真实回退链（假 build_client/invoke），用例不依赖真实模型调用。

fail-open（约定 2）：record/事实槽异常 ⇒ 帧流照常交付，仅 ``logger.warning`` 留痕。
"""
from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from agentix.application.services.run_event_stream import RunEventEmitter

PRIMARY = "agent_ttft_seconds"
FALLBACK = "agent_ttft_fallback_seconds"

# 验收③：既有主直方图桶边界的钉死取值（prometheus_client 的 le 字符串形态）。
# 改桶（含 Q2 判"细分"后的任何边界变化）都会让本用例红——这正是"桶边界零改动，
# diff 可证"的守卫：兜底直方图刻意逐字复制同一组桶，两口径才可互比。
EXPECTED_TTFT_LE = frozenset(
    {"0.05", "0.1", "0.25", "0.5", "1.0", "2.0", "5.0", "10.0", "30.0", "+Inf"}
)


def _count(metric: str) -> float:
    return REGISTRY.get_sample_value(f"{metric}_count") or 0.0


def _bucket_le(hist) -> frozenset:
    les: set = set()
    for metric in hist.collect():
        for sample in metric.samples:
            if sample.name.endswith("_bucket"):
                les.add(sample.labels["le"])
    return frozenset(les)


def _sample_label_keys(hist) -> frozenset:
    keys: set = set()
    for metric in hist.collect():
        for sample in metric.samples:
            keys.update(sample.labels.keys())
    return frozenset(keys)


def _emit_first_content(emitter: RunEventEmitter) -> dict:
    return emitter.attach({"type": "answer_chunk", "content": "x", "finished": False})


class TestAttributionSplit:
    """验收①：主/兜底两口径分别记账、互不污染。"""

    def test_primary_frame_records_primary_only(self):
        p0, f0 = _count(PRIMARY), _count(FALLBACK)
        em = RunEventEmitter("run-p142-primary")
        _emit_first_content(em)
        assert _count(PRIMARY) == p0 + 1
        assert _count(FALLBACK) == f0  # 主路径样本不得混进兜底口径

    def test_fallback_fact_frame_records_fallback_only(self):
        # record_gateway_fallback 是回退链在自身判定点的既有导出通道——
        # 本用例走的就是这条唯一通道（不直接摆弄内部状态）。
        from agentix.observability.metrics import record_gateway_fallback

        em = RunEventEmitter("run-p142-fallback")
        record_gateway_fallback("p142-direct-primary", "p142-direct-fallback")
        p0, f0 = _count(PRIMARY), _count(FALLBACK)
        _emit_first_content(em)
        assert _count(FALLBACK) == f0 + 1
        assert _count(PRIMARY) == p0  # 兜底样本不得再混进主口径

    def test_fallback_fact_scoped_to_its_own_stream(self):
        # 上一流残留的兜底事实不得污染下一流（构造发射器 = 新流事实槽开位）。
        from agentix.observability.metrics import record_gateway_fallback

        record_gateway_fallback("p142-old-primary", "p142-old-fallback")
        em = RunEventEmitter("run-p142-clean")
        p0, f0 = _count(PRIMARY), _count(FALLBACK)
        _emit_first_content(em)
        assert _count(PRIMARY) == p0 + 1
        assert _count(FALLBACK) == f0

    def test_only_first_content_frame_records(self):
        # 每流恰一次：首内容帧之后的兜底事实不追加记账（既有"每流一次"语义保持）。
        from agentix.observability.metrics import record_gateway_fallback

        em = RunEventEmitter("run-p142-once")
        _emit_first_content(em)
        f0, p0 = _count(FALLBACK), _count(PRIMARY)
        record_gateway_fallback("p142-late-primary", "p142-late-fallback")
        _emit_first_content(em)
        assert _count(FALLBACK) == f0
        assert _count(PRIMARY) == p0


class TestJudgmentSingleSource:
    """判定同源：归因开关完全由真实回退链的成败判定的返回值驱动，无第二判定点。"""

    @pytest.mark.asyncio
    async def test_chain_fallback_drives_ttft_to_fallback_metric(self):
        from agentix.infrastructure.llm.model_gateway import ModelGateway

        em = RunEventEmitter("run-p142-gw-fb")

        async def invoke(client):
            if client == "p142-primary":
                raise RuntimeError("primary down")
            return f"ok-from-{client}"

        result = await ModelGateway().ainvoke_with_fallback(
            "prompt",
            primary_model="p142-primary",
            fallback_models=["p142-fallback"],
            build_client=lambda model: model,
            invoke=invoke,
            max_attempts=2,
        )
        assert result.model_used == "p142-fallback"
        p0, f0 = _count(PRIMARY), _count(FALLBACK)
        _emit_first_content(em)
        assert _count(FALLBACK) == f0 + 1
        assert _count(PRIMARY) == p0

    @pytest.mark.asyncio
    async def test_chain_primary_success_stays_primary(self):
        from agentix.infrastructure.llm.model_gateway import ModelGateway

        em = RunEventEmitter("run-p142-gw-pri")

        async def invoke(client):
            return f"ok-from-{client}"

        result = await ModelGateway().ainvoke_with_fallback(
            "prompt",
            primary_model="p142-primary",
            fallback_models=["p142-fallback"],
            build_client=lambda model: model,
            invoke=invoke,
            max_attempts=2,
        )
        assert result.model_used == "p142-primary"
        p0, f0 = _count(PRIMARY), _count(FALLBACK)
        _emit_first_content(em)
        assert _count(PRIMARY) == p0 + 1
        assert _count(FALLBACK) == f0


class TestBucketAndCardinalityGuards:
    """验收③④：桶边界零改动 + 零新增标签。"""

    def test_primary_ttft_buckets_unchanged(self):
        from agentix.observability.metrics import TTFT_LATENCY

        assert _bucket_le(TTFT_LATENCY) == EXPECTED_TTFT_LE

    def test_fallback_ttft_shares_identical_buckets(self):
        # Q2 未决 ⇒ 不细分：兜底直方图与主直方图共用同一组边界，两口径互比。
        from agentix.observability.metrics import TTFT_LATENCY, TTFT_LATENCY_FALLBACK

        assert _bucket_le(TTFT_LATENCY_FALLBACK) == _bucket_le(TTFT_LATENCY)
        assert _bucket_le(TTFT_LATENCY_FALLBACK) == EXPECTED_TTFT_LE

    def test_both_ttft_histograms_add_no_labels(self):
        # 约定 3 / 验收②④：独立指标方案零新增 label ⇒ ALLOWED_METRIC_LABELS 无需扩容，
        # p100 全局守卫（含 FORBIDDEN 高基数名单）不受影响。
        from agentix.observability.metrics import TTFT_LATENCY, TTFT_LATENCY_FALLBACK

        for hist in (TTFT_LATENCY, TTFT_LATENCY_FALLBACK):
            keys = _sample_label_keys(hist)
            assert keys <= {"le"}, f"TTFT 直方图只允许桶位 le，实际：{sorted(keys)}"


class TestFailOpen:
    """约定 2：埋点异常绝不影响主链（帧流照常交付）。"""

    def test_record_ttft_failure_does_not_break_frame_stream(self, monkeypatch):
        import agentix.observability.metrics as m

        def boom(_d):
            raise RuntimeError("metrics exploded")

        monkeypatch.setattr(m, "record_ttft", boom)
        em = RunEventEmitter("run-p142-boom")
        f1 = em.attach({"type": "answer_chunk", "content": "x"})
        f2 = em.attach({"type": "finished"})
        assert f1["seq"] == 1 and f2["seq"] == 2
        assert "ttft_ms" in f2  # 戳记本身不依赖指标后端

    def test_record_ttft_fallback_failure_does_not_break_frame_stream(self, monkeypatch):
        import agentix.observability.metrics as m

        def boom(_d):
            raise RuntimeError("fallback metric exploded")

        monkeypatch.setattr(m, "record_ttft_fallback", boom)
        m.record_gateway_fallback("p142-boom-primary", "p142-boom-fallback")
        em = RunEventEmitter("run-p142-boom-fb")
        m.record_gateway_fallback("p142-boom-primary", "p142-boom-fallback")
        f1 = em.attach({"type": "answer_chunk", "content": "x"})
        assert f1["seq"] == 1

    def test_fact_read_failure_does_not_break_frame_stream(self, monkeypatch):
        import agentix.observability.metrics as m

        def explode():
            raise RuntimeError("context broken")

        monkeypatch.setattr(m, "gateway_fallback_observed", explode)
        recorded: list = []
        monkeypatch.setattr(m, "record_ttft", lambda d: recorded.append(d))
        em = RunEventEmitter("run-p142-read-boom")
        f1 = em.attach({"type": "answer_chunk", "content": "x"})
        assert f1["seq"] == 1
        assert recorded == []  # 事实读取失败 ⇒ 本帧放弃记账（降级可观察，不静默换口径）

    def test_fact_channel_open_failure_does_not_break_emitter(self, monkeypatch):
        import agentix.observability.metrics as m

        def boom():
            raise RuntimeError("cannot open fact channel")

        monkeypatch.setattr(m, "begin_gateway_fallback_fact", boom)
        em = RunEventEmitter("run-p142-init-boom")  # 构造不得抛
        f1 = em.attach({"type": "answer_chunk", "content": "x"})
        assert f1["seq"] == 1
