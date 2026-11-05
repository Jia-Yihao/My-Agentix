"""P170（T22）：checkpointer 降级后按可配节奏重试升级，以及降级/升级切换的可观测面。

T22 的**剩余**三条验收
（①「待恢复线程由 checkpointer 派生」已由 LG-2-1 落地，本文件不复测 ①）：

② Redis 不可用降级 `MemorySaver` 后，按可配节奏**重试升级**（不是单例粘死）；
③ 降级 / 升级切换可观测（日志 + `checkpointer_upgrade_total{result}`）；
④ 两条语义在文档上分家——文档在 `docs/项目实现手册.md` §7.2，
   本文件只钉住「口径存在且被引用」，不复制正文。

覆盖面（逐条对应验收口径）：

1. 默认（`CHECKPOINTER_RETRY_INTERVAL_S=0`）：起停函数返回 `False`、**不创建任务**、
   模块级句柄仍为 `None`；`init_checkpointer` 在默认配置下的行为与改动前逐字一致
   （Redis 失败 ⇒ `MemorySaver` + 降级原因含异常类型）；
2. **只有「Redis 连接/asetup 失败」可重试**：显式 `CHECKPOINTER_TYPE=memory` 与
   「包未安装」都不置 `_upgrade_retryable`，即使间隔 > 0 也不起重试任务；
3. 升级成功：`_saver` 换成新的 `AsyncRedisSaver`、`get_degraded_reason()` 变 `None`、
   图重编译函数被调用（与启动期同一装配点）、指标 `result="ok"` +1、**成功一次即退出**
   （不再多试）；图重编译抛错 ⇒ 本轮按**失败**处理（`_saver` 不提交、原因写清「Redis 已
   就绪 + 装配失败」两条事实、`result="fail"`、仍可重试）——装配是成功判定的一环；
4. 连续失败达上限：恰好尝试 `CHECKPOINTER_RETRY_MAX_ATTEMPTS` 次即停、最后一次失败
   原因被记录、指标 `result="fail"` 计数等于尝试次数、标记复位（复位后不会被再次起任务，
   也就不刷屏）；
5. 关停取消：`_shutdown` 取消重试任务并把句柄置回 `None`；
6. 单一源守卫：`_startup` 经 `_wire_graph_checkpointer` 装配图（不内联 build_graph），
   并在装配块之外调用 `_start_checkpointer_upgrade()`；「要不要重试」只由
   `checkpointer.is_upgrade_retryable()` 回答；
7. 失败路径资源收口（台账 §8.2 表 D G2）：三条失败臂各自丢弃的 Redis client 必须
   被恰好关闭一次且显式断开池（`TestFailurePathClosesClient` A1–A3）；升级成功路径
   不许关闭在用 client（A4，防回归钉）；关闭动作自身失败只留 warning、不改原失败
   语义（A5，约定 2）。

**不连真 Redis、只用毫秒级真实 sleep（不注入时钟，理由已述）**：`redis.asyncio.Redis` 与
`_try_import_async_redis_saver` 两个既有接缝（`tests/test_p132_checkpointer_fail_closed.py`
同款）换成桩，间隔取毫秒级，`asetup` 成功/失败由桩的类属性切换。
刻意**不注入时钟**：间隔只喂给 `asyncio.sleep`，用毫秒级真实 sleep 足以覆盖「等待后
重试」的语义，而注入时钟要改生产签名，为一条被测行为引入生产参数不划算。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from typing import Any, List

import pytest
from langgraph.checkpoint.memory import MemorySaver

from agentix.config import settings as settings_instance
from agentix.config.settings import Settings
from agentix.infrastructure.persistence import checkpointer as ckpt_module
from agentix.observability.metrics import CHECKPOINTER_UPGRADE

#: 桩间隔：毫秒级，够让 `asyncio.sleep` 真的让出一次事件循环。
TINY_INTERVAL_S = 0.001


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后重置 checkpointer 单例与「可重试」标记，避免跨用例粘死。"""
    ckpt_module.reset_checkpointer()
    yield
    ckpt_module.reset_checkpointer()


@pytest.fixture()
def warning_logs():
    """捕获 loguru WARNING+ 级消息（升级失败 / 次数用尽的可观测面）。"""
    from loguru import logger as loguru_logger

    records: List[str] = []
    handler_id = loguru_logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        loguru_logger.remove(handler_id)


class _SwitchableSaver:
    """可切换成功/失败的假 `AsyncRedisSaver`（不建真连、不碰网络）。

    `fail=True` 时 `asetup()` 抛 `ConnectionError`——这正是三条降级原因里**唯一**
    允许重试的那一条（Redis 连接/asetup 失败）。
    """

    fail = True
    instances: List["_SwitchableSaver"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.asetup_called = False
        type(self).instances.append(self)

    async def asetup(self) -> None:
        self.asetup_called = True
        if type(self).fail:
            raise ConnectionError("failed to connect to redis")


def _patch_redis(monkeypatch, saver_cls: Any = _SwitchableSaver) -> Any:
    """钉住 Redis 路径的既有接缝：包导入 + `Redis.from_url`（同 p132 范式）。

    返回 `_FakeRedis` 类本身：`TestFailurePathClosesClient`（台账 §8.2 表 D G2）
    要断言每次尝试新建 client 的 `aclose` 计数与参数，A5 还要把 `aclose`
    换成自身抛异常的版本。既有调用点不取返回值，形状只增不改。
    """
    monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: saver_cls)

    import redis.asyncio as aioredis

    class _FakeRedis:
        instances: List["_FakeRedis"] = []

        def __init__(self) -> None:
            # G2 测试面：逐实例记录 aclose 调用（含 kwargs），断言「恰好一次且显式关池」。
            self.aclose_calls: List[dict] = []
            type(self).instances.append(self)

        @classmethod
        def from_url(cls, *args: Any, **kwargs: Any) -> "_FakeRedis":
            return cls()

        async def aclose(self, close_connection_pool: Any = None) -> None:
            self.aclose_calls.append({"close_connection_pool": close_connection_pool})

    monkeypatch.setattr(aioredis, "Redis", _FakeRedis)
    return _FakeRedis


def _reset_switchable(fail: bool) -> None:
    _SwitchableSaver.fail = fail
    _SwitchableSaver.instances = []


def _upgrade_count(result: str) -> float:
    """读指标当前值（与 p105 / p81 同款读法：`Counter.labels(...)._value.get()`）。"""
    return CHECKPOINTER_UPGRADE.labels(result=result)._value.get()


@pytest.fixture(autouse=True)
def _clean_retry_handle(monkeypatch):
    """重试任务句柄归零：避免上一个用例留下的句柄让断言假绿。"""
    from agentix import main

    monkeypatch.setattr(main, "_checkpointer_upgrade_task", None)


class TestDefaultsAreBaseline:
    """验收①（约定 1）：默认配置下不建任务、不加日志，与基线逐位等价。"""

    def test_field_defaults(self):
        """两个新键的**字段默认值**（读 model_fields，不吃宿主 .env 污染）。"""
        interval = Settings.model_fields["CHECKPOINTER_RETRY_INTERVAL_S"].default
        attempts = Settings.model_fields["CHECKPOINTER_RETRY_MAX_ATTEMPTS"].default
        assert interval == 0.0, "默认必须关闭重试循环（0 = 关，否则等于默认改了行为）"
        assert attempts == 3, "每进程尝试上限默认 3"

    @pytest.mark.asyncio
    async def test_interval_zero_creates_no_task_even_when_retryable(self, monkeypatch):
        """间隔 0 ⇒ 即使降级可重试，也不建任务（事件循环存在时同样不建）。"""
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 0.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 3)
        # 造出「可重试」的降级态：Redis 连接失败
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)
        await ckpt_module.init_checkpointer()
        assert ckpt_module.is_upgrade_retryable() is True, "前置：Redis 失败态必须可重试"

        assert main._start_checkpointer_upgrade() is False
        assert main._checkpointer_upgrade_task is None, "默认路径不许创建后台任务"

    @pytest.mark.asyncio
    async def test_init_behaviour_unchanged_on_redis_failure(self, monkeypatch):
        """`init_checkpointer` 的降级结果与改动前逐字一致（单例仍是 MemorySaver）。"""
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)

        saver = await ckpt_module.init_checkpointer()

        assert isinstance(saver, MemorySaver)
        assert "ConnectionError" in (ckpt_module.get_degraded_reason() or "")
        assert ckpt_module.get_checkpointer() is saver
        # 单例复用语义不变（init 自己仍然不再试 Redis：「重试」是独立入口的事）
        assert await ckpt_module.init_checkpointer() is saver


class TestOnlyRedisFailureIsRetryable:
    """验收②：显式 memory 与「包未安装」都是不可重试的降级原因。"""

    @pytest.mark.asyncio
    async def test_explicit_memory_is_not_retryable(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "memory")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 3)

        saver = await ckpt_module.init_checkpointer()

        assert isinstance(saver, MemorySaver)
        assert ckpt_module.is_upgrade_retryable() is False, "人配的 memory 不是故障，不许重试"
        assert main._start_checkpointer_upgrade() is False
        assert main._checkpointer_upgrade_task is None

    @pytest.mark.asyncio
    async def test_package_not_installed_is_not_retryable(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(ckpt_module, "_try_import_async_redis_saver", lambda: None)

        await ckpt_module.init_checkpointer()

        assert ckpt_module.is_upgrade_retryable() is False, "运行期不会凭空装上，重试是空转"
        assert main._start_checkpointer_upgrade() is False
        assert main._checkpointer_upgrade_task is None

    @pytest.mark.asyncio
    async def test_retry_refuses_when_not_retryable(self):
        """升级入口自己也读同一个标记（单一源）：不可重试时立刻 `False`，不建连。"""
        assert await ckpt_module.retry_upgrade_checkpointer() is False
        assert ckpt_module.get_checkpointer() is None

    @pytest.mark.asyncio
    async def test_fail_closed_leaves_nothing_to_retry(self, monkeypatch):
        """fail-closed 抛出后没有降级态可升级：标记必须为假，起停函数也起不了任务。

        若 fail-closed 也把标记置真，重试循环会对着一个 `_saver is None` 的空单例空转，
        「最后一次失败原因」恒为 `None`，次数用尽的告警说不出任何原因。
        """
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", True)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 3)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)

        with pytest.raises(RuntimeError):
            await ckpt_module.init_checkpointer()

        assert ckpt_module.get_checkpointer() is None, "fail-closed 不许把单例粘成 MemorySaver"
        assert ckpt_module.is_upgrade_retryable() is False
        assert await ckpt_module.retry_upgrade_checkpointer() is False
        assert main._start_checkpointer_upgrade() is False
        assert main._checkpointer_upgrade_task is None


class TestUpgradeSucceeds:
    """验收③：升级成功后状态、日志、图装配与指标四面同时到位。"""

    @pytest.mark.asyncio
    async def test_retry_upgrades_saver_and_rewires_graph(self, monkeypatch, warning_logs):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)

        degraded = await ckpt_module.init_checkpointer()
        assert isinstance(degraded, MemorySaver)
        assert ckpt_module.is_upgrade_retryable() is True

        # 图重编译的**唯一装配点**用一个记录器替代（验证"被调用且只被调用一次"）。
        wired: List[Any] = []
        monkeypatch.setattr(main, "_wire_graph_checkpointer", lambda ckpt: wired.append(ckpt))

        # Redis 恢复：第二次建连会成功
        _SwitchableSaver.fail = False
        ok_before = _upgrade_count("ok")

        await main._checkpointer_upgrade_loop(TINY_INTERVAL_S, max_attempts=5)

        upgraded = ckpt_module.get_checkpointer()
        assert upgraded is not degraded, "升级后必须是新的 Redis saver，不是原地复用降级单例"
        assert upgraded is _SwitchableSaver.instances[-1]
        assert ckpt_module.get_degraded_reason() is None
        assert ckpt_module.is_upgrade_retryable() is False, "成功一次即终止重试"
        assert wired == [upgraded], "必须经同一装配点重编译图，且只做一次"
        assert _upgrade_count("ok") == ok_before + 1
        assert len(_SwitchableSaver.instances) == 2, "成功一次后不许再多试（含首次降级那次建连）"
        assert not any("exhausted" in r for r in warning_logs)


class TestRewireIsPartOfSuccess:
    """验收③（第 2 轮返工）：图装配是「升级成功」的判定条件之一。

    反过来（先换 `_saver` 再装配）在装配抛错时会留下「自称已恢复、服务图仍挂旧
    saver」的中间态：`/ops/active-threads` 的线程 id 来自新 saver、「未完成」判据
    来自旧图 ⇒ 静默返回空列表，而指标已记 `result="ok"`。
    """

    @pytest.mark.asyncio
    async def test_rewire_failure_is_not_a_successful_upgrade(self, monkeypatch, warning_logs):
        """复现输入：Redis 恢复（`asetup` 成功）+ 装配点抛 `RuntimeError`。

        期望输出：指标 `result="ok"` 不增、`result="fail"` 每次尝试各 +1、
        `_saver` 仍是降级时的 MemorySaver（不提交新 saver）、`get_degraded_reason()`
        写明「Redis 已就绪但图重编译失败」、标记仍可重试直到次数用尽（用尽后复位）。

        这条用例在旧实现下会红：旧实现先把 `_saver` 换新、把原因置 `None`、记 ok，
        再把装配异常收成一条 ERROR 日志后 `return` ⇒ ok 计数 +1 且原因变 `None`。
        """
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)

        degraded = await ckpt_module.init_checkpointer()
        assert isinstance(degraded, MemorySaver)
        assert ckpt_module.is_upgrade_retryable() is True

        def _raise_(_ckpt):
            raise RuntimeError("graph recompile exploded")

        monkeypatch.setattr(main, "_wire_graph_checkpointer", _raise_)
        # Redis 侧恢复：建连 + asetup 都会成功，失败只发生在装配这一步
        _SwitchableSaver.fail = False
        ok_before = _upgrade_count("ok")
        fail_before = _upgrade_count("fail")

        await main._checkpointer_upgrade_loop(TINY_INTERVAL_S, max_attempts=3)

        assert _upgrade_count("ok") == ok_before, "装配失败不许记升级成功"
        assert _upgrade_count("fail") == fail_before + 3, "每次装配失败都要记一次 fail"
        assert ckpt_module.get_checkpointer() is degraded, "装配失败不许提交新 saver"
        reason = ckpt_module.get_degraded_reason()
        assert reason is not None, "装配失败后不得自称已恢复（原因不许置 None）"
        assert (
            "rewire" in reason and "RuntimeError" in reason
        ), f"降级原因必须写清「Redis 已就绪但图重编译失败」两条事实：{reason!r}"
        exhausted = [r for r in warning_logs if "exhausted after 3 attempt(s)" in r]
        assert len(exhausted) == 1, f"持续装配失败最终要打次数用尽告警：{warning_logs!r}"
        assert "rewire" in exhausted[0], "次数用尽告警必须带上最后一次失败原因"
        assert ckpt_module.is_upgrade_retryable() is False, "次数用尽后复位标记"


class TestFailurePathClosesClient:
    """台账 §8.2 表 D G2：失败路径丢弃的 Redis client 必须被关闭（池断开）恰好一次。

    「建池即负责，谁丢弃谁关闭」——每条把 client 丢弃掉的臂都要有钉：

    - A1 `retry_upgrade_checkpointer` 的建连/`asetup` 失败臂；
    - A2 `retry_upgrade_checkpointer` 的 rewire 失败臂（saver 已建连并 asetup 成功但整只被丢弃）；
    - A3 `init_checkpointer` 降级臂（client 在 `_connect_redis_saver` 内部抛出点已关）；
    - A4 升级成功路径 ⇒ **不**关闭在用 client（所有权移交 saver；防止把
      「关掉在用 client」这种新 bug 当成修复——本条在改前即绿，是防回归钉）；
    - A5 关闭动作自身抛异常 ⇒ 原失败语义不变（仍降级 / 仍 `return False`），
      只多一条 warning 留痕（约定 2：关闭失败不得升格成异常或改写原失败）。
    """

    @pytest.mark.asyncio
    async def test_a1_failed_connect_attempt_closes_client_once(self, monkeypatch):
        """A1：重试的建连/asetup 失败臂 ⇒ 该次尝试新建的 client 被关闭恰好一次。"""
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        fake = _patch_redis(monkeypatch)

        await ckpt_module.init_checkpointer()  # 造降级态（init 自身的关闭由 A3 负责钉）
        assert ckpt_module.is_upgrade_retryable() is True

        assert await ckpt_module.retry_upgrade_checkpointer() is False

        assert len(fake.instances) == 2, "init 一次 + retry 一次建连"
        retried = fake.instances[-1]
        assert retried.aclose_calls == [{"close_connection_pool": True}], (
            f"重试失败臂新建的 client 必须恰好关闭一次且显式关池：{retried.aclose_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_a2_rewire_failure_closes_client_once(self, monkeypatch):
        """A2：rewire 失败臂 ⇒ saver 连同其 client 整只被丢弃，client 必须被关闭恰好一次。"""
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        fake = _patch_redis(monkeypatch)

        degraded = await ckpt_module.init_checkpointer()
        assert ckpt_module.is_upgrade_retryable() is True

        # Redis 侧恢复：建连 + asetup 成功；失败只发生在装配 ⇒ 丢弃点在此臂
        _SwitchableSaver.fail = False

        def _raise_(_ckpt: Any) -> None:
            raise RuntimeError("graph recompile exploded")

        assert await ckpt_module.retry_upgrade_checkpointer(_raise_) is False
        assert ckpt_module.get_checkpointer() is degraded, "前置：失败不提交新 saver"

        assert len(fake.instances) == 2, "init 一次 + retry 一次建连"
        rewired = fake.instances[-1]
        assert rewired.aclose_calls == [{"close_connection_pool": True}], (
            f"rewire 失败臂丢弃的 client 必须恰好关闭一次且显式关池：{rewired.aclose_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_a3_init_degradation_closes_client_once(self, monkeypatch):
        """A3：init 降级臂 ⇒ client 在 `_connect_redis_saver` 内部抛出点已被关闭恰好一次。"""
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        fake = _patch_redis(monkeypatch)

        saver = await ckpt_module.init_checkpointer()

        assert isinstance(saver, MemorySaver), "前置：降级语义不变"
        assert len(fake.instances) == 1
        assert fake.instances[0].aclose_calls == [{"close_connection_pool": True}], (
            f"init 降级臂的 client 必须恰好关闭一次且显式关池：{fake.instances[0].aclose_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_a4_successful_upgrade_does_not_close_client(self, monkeypatch):
        """A4：升级成功 ⇒ 在用 client 不许关（所有权移交 saver）。防回归钉，改前即绿。"""
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        fake = _patch_redis(monkeypatch)

        await ckpt_module.init_checkpointer()  # 前置降级（其 client 的关闭归 A3 判，不算本条）
        _SwitchableSaver.fail = False
        wired: List[Any] = []

        assert await ckpt_module.retry_upgrade_checkpointer(wired.append) is True
        assert ckpt_module.get_checkpointer() is _SwitchableSaver.instances[-1]

        assert len(fake.instances) == 2, "init 一次 + retry 一次建连"
        success_client = fake.instances[-1]
        assert success_client.aclose_calls == [], (
            f"成功路径所有权已移交 saver，关闭在用 client 是新 bug：{success_client.aclose_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_a5_close_failure_keeps_original_semantics(self, monkeypatch, warning_logs):
        """A5：aclose 自身抛 ⇒ 原失败语义逐位不变，只多一条 warning 留痕（约定 2）。"""

        async def _boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("aclose exploded")

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        fake = _patch_redis(monkeypatch)
        fake.aclose = _boom  # 在建 client 之前替换：本用例所有 client 的关闭都自身抛

        saver = await ckpt_module.init_checkpointer()
        assert isinstance(saver, MemorySaver), "关闭失败不得改变降级语义"
        assert "ConnectionError" in (ckpt_module.get_degraded_reason() or ""), (
            "降级原因必须仍是原异常，不被关闭失败改写"
        )
        assert ckpt_module.is_upgrade_retryable() is True
        close_warnings = [r for r in warning_logs if "closing discarded redis client" in r]
        assert len(close_warnings) == 1, f"关闭失败要有恰好一条 warning 留痕：{warning_logs!r}"

        assert await ckpt_module.retry_upgrade_checkpointer() is False, (
            "关闭失败不得把 False 升格成异常"
        )
        assert "ConnectionError" in (ckpt_module.get_degraded_reason() or "")
        close_warnings = [r for r in warning_logs if "closing discarded redis client" in r]
        assert len(close_warnings) == 2, "重试臂同样：恰好再多一条，原语义不变"


class TestRetryExhaustion:
    """验收②/③：连续失败到上限即停，且次数与最后原因说得清。"""

    @pytest.mark.asyncio
    async def test_exhaustion_stops_at_cap_and_disarms(self, monkeypatch, warning_logs):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)

        await ckpt_module.init_checkpointer()
        assert ckpt_module.is_upgrade_retryable() is True
        fail_before = _upgrade_count("fail")

        await main._checkpointer_upgrade_loop(TINY_INTERVAL_S, max_attempts=3)

        assert len(_SwitchableSaver.instances) == 1 + 3, "恰好尝试上限次数（1 次 init + 3 次重试）"
        assert _upgrade_count("fail") == fail_before + 3
        assert isinstance(ckpt_module.get_checkpointer(), MemorySaver), "失败不许把降级态改坏"
        assert "ConnectionError" in (
            ckpt_module.get_degraded_reason() or ""
        ), "最后一次失败原因必须被记录（次数用尽的告警要读它）"
        exhausted = [r for r in warning_logs if "exhausted after 3 attempt(s)" in r]
        assert len(exhausted) == 1, f"次数用尽必须打恰好一条明确 warning：{warning_logs!r}"
        assert "ConnectionError" in exhausted[0], "告警必须带上最后一次失败原因"
        assert ckpt_module.is_upgrade_retryable() is False, "用尽后复位标记，避免日志刷屏"

        # 复位后再起：没有任务可起（也就不会重复刷同一条告警）
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 3)
        assert main._start_checkpointer_upgrade() is False
        assert len([r for r in warning_logs if "exhausted after" in r]) == 1


class TestStartAndShutdown:
    """验收①：起停形状照 `_start_hitl_deadline_sweeper`——句柄唯一写点、关停取消。"""

    @pytest.mark.asyncio
    async def test_start_creates_cancellable_task(self, monkeypatch):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 3)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)
        await ckpt_module.init_checkpointer()

        assert main._start_checkpointer_upgrade() is True
        task = main._checkpointer_upgrade_task
        assert task is not None and not task.done()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        monkeypatch.setattr(main, "_checkpointer_upgrade_task", None)

    @pytest.mark.asyncio
    async def test_max_attempts_zero_warns_and_creates_no_task(self, monkeypatch, warning_logs):
        from agentix import main

        monkeypatch.setattr(settings_instance, "CHECKPOINTER_TYPE", "redis")
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_FAIL_CLOSED", False)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_INTERVAL_S", 30.0)
        monkeypatch.setattr(settings_instance, "CHECKPOINTER_RETRY_MAX_ATTEMPTS", 0)
        _reset_switchable(fail=True)
        _patch_redis(monkeypatch)
        await ckpt_module.init_checkpointer()

        assert main._start_checkpointer_upgrade() is False
        assert main._checkpointer_upgrade_task is None
        assert any(
            "CHECKPOINTER_RETRY_MAX_ATTEMPTS" in r for r in warning_logs
        ), "配了节奏却给零次尝试必须显式可见，不能静默假装在重试"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_task_and_clears_handle(self, monkeypatch):
        from agentix import main

        task = asyncio.create_task(asyncio.sleep(3600))
        monkeypatch.setattr(main, "_checkpointer_upgrade_task", task)

        class _NoopService:
            async def cleanup(self) -> None:
                return None

        monkeypatch.setattr(main, "get_lightrag_service", lambda: _NoopService())

        await main._shutdown()

        assert task.cancelled()
        assert main._checkpointer_upgrade_task is None


class TestSingleSourceWiring:
    """约定 6 ④：装配与「要不要重试」都只有一个源（静态守卫，不靠运行时碰运气）。"""

    def test_startup_wires_graph_through_single_helper(self):
        from agentix import main

        source = inspect.getsource(main._startup)
        assert "_wire_graph_checkpointer(ckpt)" in source, "启动必须经唯一装配点"
        assert (
            "build_supervisor_graph" not in source
        ), "启动段不许自己再拼一份图装配（那是第二个源，重试路径就漏了）"
        assert "_start_checkpointer_upgrade()" in source, "启动段必须调用起停函数"
        # 起停调用必须在装配 try 之外（装配失败恰恰最该把重试跑起来）。
        # 判据取 AST 父子关系而非字符串下标：下标只保证「文本上在前面」，把调用包进
        # 任何另一个 try / if / with 照样放行，而包进 try 恰好是这条守卫要防的形态
        # （try 里的异常会把随后的调用一起跳过去）。
        tree = ast.parse(textwrap.dedent(source))
        parents: dict = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_start_checkpointer_upgrade"
        ]
        assert len(calls) == 1, "启动段必须调用起停函数，且只调用一次"
        # 从调用节点往上走到函数定义，沿途的祖先就是「包住它的那些块」
        chain = []
        node: Any = calls[0]
        while node in parents:
            node = parents[node]
            chain.append(node)
        assert isinstance(chain[0], ast.Expr), "起停调用必须是独立语句（不是表达式的一部分）"
        assert isinstance(chain[1], (ast.FunctionDef, ast.AsyncFunctionDef)), (
            "起停调用的最近父级必须是函数体——被 try / if / with 包住时"
            f"装配失败会让自愈路径一起跳过（实际父级：{type(chain[1]).__name__}）"
        )
        # 台账表 D G3：把「必须经唯一装配点」从纯子串升级为 AST 检出——上面的纯子串判据
        # 只保证那行字面出现在源码任意位置，钉不住它确实在 _startup 体内、且先于后续关键
        # 语句执行。_startup 体内没有 return（读源码实测：函数体一路向下到末尾、无提前
        # 返回），故「先于后续关键语句」取紧随其后的起停调用 _start_checkpointer_upgrade
        # 作锚——装配必须在「安排升级重试」之前落定，否则重试跑起来时图还挂着旧 saver。
        wire_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_wire_graph_checkpointer"
        ]
        assert len(wire_calls) == 1, (
            f"启动段必须经唯一装配点且只调用一次，实际 {len(wire_calls)} 处"
        )
        assert wire_calls[0].lineno < calls[0].lineno, (
            "装配调用必须先于起停调用出现在 _startup 体的语句序里"
        )

    def test_retry_loop_reads_the_marker_not_config(self):
        from agentix import main

        source = inspect.getsource(main._checkpointer_upgrade_loop)
        assert "is_upgrade_retryable()" in source
        for forbidden in (
            "settings.CHECKPOINTER_TYPE",
            "CHECKPOINTER_FAIL_CLOSED",
            "get_degraded_reason().startswith",
            '"not installed"',
        ):
            assert (
                forbidden not in source
            ), f"重试判定不许在 main 里重判一遍（约定 6 ④）：{forbidden}"

    def test_saver_swap_and_reason_reset_live_in_checkpointer(self):
        """升级的三件写入（saver / 原因 / 标记）都在 checkpointer 模块内部完成。"""
        source = inspect.getsource(ckpt_module.retry_upgrade_checkpointer)
        assert "_saver = saver" in source
        assert "_degraded_reason = None" in source
        assert "_upgrade_retryable = False" in source
