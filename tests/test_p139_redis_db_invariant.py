"""P139：Redis db0 不变式的运行时校验（规划文档 T7）。

规格真源：docs/design/审批与中断恢复优化规划.md T7 卡。验收标准逐条对应：
① 运行时读到非约定 db ⇒ error 日志 + 可见告警（TestDbInvariantAlert 前两条）；
② **不新增拒绝路径**——告警照响，执行链照常走（TestNoNewRejectionPath 全部）；
③ 守卫用例覆盖"注入错误 db 能看到告警"（test_wrong_db_emits_error_alert）。

判定同源纪律（AGENTS.md 硬性纪律 6 ④）：告警判据必须取**执行链建连时实际生效的
db 号**（来自 init_checkpointer 构造好的 Redis client 连接池参数），不得另解析一遍
settings.REDIS_CHECKPOINT_URL 立第二个判点——test_alert_reads_effective_db_not_url
把这条钉死：URL 与 client 池参数故意不一致时，告警报的是 client 里的值。

用例不建真 Redis：``Redis.from_url`` 是惰性的，构造连接池对象而不开 socket
（沿用 tests/test_r32_redis_sharding.py 的纯形状断言与 test_p132 的桩范式）；
loguru 断言沿用 test_agent_phase22_context_cache.py 的 ``logger.add(sink)`` 范式。
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from loguru import logger as loguru_logger

from agentix.config import settings as settings_instance
from agentix.infrastructure.persistence import checkpointer as ckpt_module

# 与实现约定的告警锚点文本一致：断言只认这个标记，避免被无关 ERROR 日志误绿
ALERT_MARK = "checkpointer db0 不变式违例"


class _SuccessSaver:
    """假 AsyncRedisSaver：asetup 成功。用于证明告警与否都不阻断执行链（验收②）。"""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs

    async def asetup(self):
        return None


class _FailingSaver:
    """模拟真 Redis Stack 在 db!=0 上 asetup 的实测报错（checkpointer 模块 docstring 约束 2）。"""

    def __init__(self, **kwargs):
        pass

    async def asetup(self):
        raise RuntimeError(
            "Failed to create index 'checkpoints' on Redis: Cannot create index on db != 0"
        )


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后重置单例，避免跨用例粘死污染（同 test_p132 范式）。"""
    ckpt_module.reset_checkpointer()
    yield
    ckpt_module.reset_checkpointer()


def _install(monkeypatch, url: str, saver_cls=_SuccessSaver) -> None:
    """钉住 redis 路径配置与假 saver；不建真连（from_url 惰性，桩 asetup 不碰网络）。"""
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
    monkeypatch.setattr(settings_instance, "REDIS_CHECKPOINT_URL", url)
    # raising=False：宿主 .env 若显式开了 fail-closed 会干扰"不新增拒绝路径"的断言，这里钉回默认
    monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False, raising=False)
    monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: saver_cls)


def _capture_error_logs():
    """返回 (捕获文本列表, sink id)：只收 ERROR 级，断言时按 ALERT_MARK 过滤。"""
    captured: list[str] = []
    sink_id = loguru_logger.add(captured.append, level="ERROR")
    return captured, sink_id


def _alerts(captured: list[str]) -> list[str]:
    return [m for m in captured if ALERT_MARK in m]


class TestDbInvariantAlert:
    """验收①/③：注入错误 db 必须让告警可见；约定 db 必须无噪声（否则告警会被习惯性忽略）。"""

    @pytest.mark.asyncio
    async def test_wrong_db_emits_error_alert(self, monkeypatch):
        _install(monkeypatch, "redis://127.0.0.1:1/2")
        captured, sink_id = _capture_error_logs()
        try:
            await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        alerts = _alerts(captured)
        assert len(alerts) == 1, f"注入 db2 必须恰好产生一条不变式告警：{captured!r}"
        assert "实际生效 db 号为 2" in alerts[0], alerts[0]
        assert "REDIS_CHECKPOINT_URL" in alerts[0], "告警必须点名可修的注入来源，否则运维无从下手"

    @pytest.mark.asyncio
    async def test_expected_db_no_alert(self, monkeypatch):
        _install(monkeypatch, "redis://127.0.0.1:1/0")
        captured, sink_id = _capture_error_logs()
        try:
            saver = await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert _alerts(captured) == [], f"约定 db0 不该有不变式告警：{captured!r}"
        assert type(saver) is _SuccessSaver

    @pytest.mark.asyncio
    async def test_url_without_db_path_defaults_to_db0_no_alert(self, monkeypatch):
        """URL 无路径段时 redis-py 生效 db 就是 0——按 client 池参数判是"缺省⇒0"，不得误告警。"""
        _install(monkeypatch, "redis://127.0.0.1:1")
        captured, sink_id = _capture_error_logs()
        try:
            await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert _alerts(captured) == [], f"生效 db0（URL 缺省路径）不该告警：{captured!r}"

    @pytest.mark.asyncio
    async def test_alert_reads_effective_db_not_url(self, monkeypatch):
        """纪律 6 ④判同源钉桩：故意让 client 池参数与 URL 不一致，告警报的必须是池里的值。

        若实现另解析一遍 settings.REDIS_CHECKPOINT_URL，这里会报 0（URL 侧）而不是 7（生效侧）
        ——即两个判点漂移，正是纪律 6 ④ 禁止的形态。
        """
        import redis.asyncio as aioredis

        class _StubPool:
            def __init__(self, db):
                self.connection_kwargs = {"db": db}

        class _StubClient:
            def __init__(self, db):
                self.connection_pool = _StubPool(db)

        class _FakeRedis:
            @classmethod
            def from_url(cls, url, **kwargs):
                # URL 说 db0，池参数说 db7：以执行链真正使用的池参数为准
                return _StubClient(7)

        monkeypatch.setattr(aioredis, "Redis", _FakeRedis)
        _install(monkeypatch, "redis://127.0.0.1:1/0")
        captured, sink_id = _capture_error_logs()
        try:
            await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        alerts = _alerts(captured)
        assert len(alerts) == 1, f"生效 db7 必须告警（判据取自 client 而非 URL）：{captured!r}"
        assert "实际生效 db 号为 7" in alerts[0], alerts[0]

    @pytest.mark.asyncio
    async def test_unreadable_client_shape_skips_alert_without_breaking_chain(self, monkeypatch):
        """判据取不到（桩/异形 client）⇒ 跳过告警且绝不影响执行链——诊断不配让主链路买单（纪律 2）。"""
        import redis.asyncio as aioredis

        class _OpaqueClient:
            """没有 connection_pool 属性：无法判定生效 db。"""

        class _FakeRedis:
            @classmethod
            def from_url(cls, url, **kwargs):
                return _OpaqueClient()

        monkeypatch.setattr(aioredis, "Redis", _FakeRedis)
        _install(monkeypatch, "redis://127.0.0.1:1/0")
        captured, sink_id = _capture_error_logs()
        try:
            saver = await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert _alerts(captured) == [], f"无法判定时不得猜测告警：{captured!r}"
        assert type(saver) is _SuccessSaver


class TestNoNewRejectionPath:
    """验收②：不变式告警只做诊断，不新增拒绝路径；也不得与 PhaseK fail-closed 开关纠缠。"""

    @pytest.mark.asyncio
    async def test_wrong_db_chain_still_completes_when_redis_path_ok(self, monkeypatch):
        """错误 db + Redis 路径本身成功 ⇒ 照常返回 saver，不抛、不降级、单例照常用。"""
        _install(monkeypatch, "redis://127.0.0.1:1/2")
        captured, sink_id = _capture_error_logs()
        try:
            saver = await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert type(saver) is _SuccessSaver, "告警不得把成功的 Redis 路径掐成降级或异常"
        assert ckpt_module.get_degraded_reason() is None
        assert ckpt_module.get_checkpointer() is saver
        assert len(_alerts(captured)) == 1, "告警与阻断必须分离：只告警"

    @pytest.mark.asyncio
    async def test_wrong_db_degradation_behaviour_matches_baseline(self, monkeypatch):
        """错误 db 触发 asetup 实测报错时，降级行为必须与加校验之前逐位一致
        （MemorySaver + 原因入单例），校验只是提前留痕，不改结果。
        """
        _install(monkeypatch, "redis://127.0.0.1:1/2", _FailingSaver)
        captured, sink_id = _capture_error_logs()
        try:
            saver = await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert isinstance(saver, MemorySaver)
        reason = ckpt_module.get_degraded_reason()
        assert reason and "Cannot create index on db != 0" in reason
        assert len(_alerts(captured)) == 1

    @pytest.mark.asyncio
    async def test_fail_closed_on_does_not_turn_invariant_into_rejection(self, monkeypatch):
        """fail-closed 开关只治理"失败分支"（test_p132 的口径）：db 违例但路径成功
        不是失败，开了开关也不得因此抛——否则就是借开关新增拒绝路径。
        """
        _install(monkeypatch, "redis://127.0.0.1:1/2")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True, raising=False)
        captured, sink_id = _capture_error_logs()
        try:
            saver = await ckpt_module.init_checkpointer()
        finally:
            loguru_logger.remove(sink_id)

        assert type(saver) is _SuccessSaver, "不变式告警不得搭 fail-closed 的车变成拒绝启动"
        assert len(_alerts(captured)) == 1

    @pytest.mark.asyncio
    async def test_fail_closed_semantics_unchanged_on_real_failure(self, monkeypatch):
        """真失败 + 开关开 ⇒ 照旧 RuntimeError（PhaseK 任务 1 语义不因本校验而变，禁纠缠）。"""
        _install(monkeypatch, "redis://127.0.0.1:1/2", _FailingSaver)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True, raising=False)
        with pytest.raises(RuntimeError) as excinfo:
            await ckpt_module.init_checkpointer()
        assert str(excinfo.value).startswith("checkpointer fail-closed: ")
        assert ckpt_module.get_checkpointer() is None
