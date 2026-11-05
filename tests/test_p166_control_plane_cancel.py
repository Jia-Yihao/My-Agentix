"""P166（T11）：取消传播 / Control Plane FIFO 边界固化 + ``BudgetExceededError`` 处置口径。

验收②③：
同卡验收①（取消传播 p95 的 bench 读数）落在 ``bench/cancel_propagation.py``，
本文件只对它做入口可跑通的冒烟守卫，**不**在任何用例里断言时延阈值——
时延没有门禁，门禁是拿读数的人的事。

覆盖面：

A. **乱序取消**：取消顺序不影响登记顺序（``list_runs`` 是 spawn 序，不是取消序）、
   已终态运行不被第二次取消覆盖、取消一个运行不波及无关运行与其绑定的 Task。
B. **并发取消**：同一 run 被 N 个并发 cancel 打 ⇒ 恰好一个 ``True``（判定与置
   终态之间没有 await，胜者是确定性的"第一个"，不靠时序运气）；不同 run 并发
   取消互不影响；cancel 与 finish 竞争 ⇒ 恰好一方成功。
C. **``max_*`` 溢出**：``max_runs`` 的 FIFO 只淘汰最旧**终态**、活动运行宁可超容
   也不淘汰、被淘汰的 run_id 不可被 cancel/finish/bind 复活、钳位下界 1；
   ``max_concurrency`` 槽位随取消释放；``max_depth`` 边界含等号。
D. **``BudgetExceededError`` 处置口径（事实陈述，不是建议）**：见下节。

---

``BudgetExceededError`` 处置口径（与**现状**逐条对齐，改代码必须同步改这里）

1. **定义与抛点**（``agentix/domain/runtime/budget.py:46``）：``class
   BudgetExceededError(Exception)``，携带 ``dimension`` / ``used`` / ``limit``。
   **不是** ``AgentError`` 子类 ⇒ 不受 ``chat_service.ask`` 里
   ``except AgentError``（可降级、200 + 礼貌拒答）那条分支保护。
   六个抛点全在 ``agentix/runtime/budget.py``，每个都带 ``limit > 0`` 前置：
   ``check_step`` / ``check_tool_call`` / ``check_llm_call`` / ``check_latency``
   与 ``record_llm_call`` 里的 tokens / cost 两个量维度。
2. **catch 点**：生产代码（``agentix/**``）**零**处 ``except
   BudgetExceededError``（本条由 ``TestBudgetErrorDisposition::
   test_production_has_no_dedicated_catch`` 静态钉住）。它唯一的落点是
   ``chat_service.ask`` 末尾的兜底 ``except Exception``（``chat_service.py``
   末段），与"未预期内部错误"同一条路。
3. **回执形态**（由 2 推出，无需实测新行为）：
   - 默认：``HTTPException(500, {"error_code": "internal_error", ...})``；
   - ``LEGACY_ERROR_MODE=True``：200 + 兜底文案 + ``error_code="internal_error"``；
   - **例外**：兜底分支先问 ``is_retryable(e)``，而该判据是**对异常文本做子串
     匹配**（``used=500`` / ``used=429`` / ``cost_usd=0.502`` 这类数字串会命中
     ``500``/``429``/``502`` 等瞬时特征）⇒ 这类用量数字会把一次预算超限误判成
     "上游瞬断"，回执变成 503 ``LLM_UNAVAILABLE``。这是**现状缺陷**，本节只
     如实记录，不修（修 catch 点不属 T11）。
4. **可达性**：生产装配的 Runtime 预算六维**全 0**（``chat_service.py`` 的
   ``RuntimeBudget(max_steps=0, max_llm_calls=0, ..., max_cost_usd=0.0)``，由
   ``test_production_runtime_budget_is_all_zero`` 静态钉住），而每个抛点都有
   ``> 0`` 前置 ⇒ **生产链路上该异常当前不可达**。
5. **「不限额」≠「超限」**：``0`` 是"该维度不限额"（``BudgetManager`` 的
   ``check_*`` 直接短路），不是"额度已用完"。``Budget()`` 的**默认值**是有限额
   （steps 20 / llm 15 / tool 20 / tokens 30000 / latency 60000 / cost 0.5），
   但生产装配显式传了全 0 ⇒ 生效的是"不限额"。把这两件事写成一句
   "预算超限了"会让失败归因失真（T11 卡风险行）。
6. **别与 ``ErrorCode.TOKEN_BUDGET_EXCEEDED`` 混为一谈**：那是
   ``application/services/token_budget.py`` 的**编排域**判定，抛/返回的是
   ``AgentError``（``ErrorCode.TOKEN_BUDGET_EXCEEDED`` 在 ``DEGRADABLE`` 里 ⇒
   200 + 礼貌拒答）。两个"预算"分属不同层、不同回执，用同一个词描述它们
   就是口径漂移。

D 节用例把 1/2/4/5/6 的**现状**逐条钉住：新增任何专用 catch、或把生产预算从
全 0 改成有限额 ⇒ 用例红 ⇒ 必须先改本节口径再改代码（同一个判定不许两个源）。
"""

from __future__ import annotations

import ast
import asyncio
import math
from pathlib import Path

import pytest

from agentix.domain.policies.error_classification import is_retryable
from agentix.domain.runtime.budget import Budget, BudgetExceededError
from agentix.runtime.budget import BudgetManager
from agentix.runtime.control_plane import (
    AdmissionDenied,
    ControlPlane,
    RunState,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# A. 乱序取消
# ---------------------------------------------------------------------------


class TestOutOfOrderCancel:
    def setup_method(self) -> None:
        self.plane = ControlPlane()

    @pytest.mark.asyncio
    async def test_cancel_order_does_not_reorder_registry(self) -> None:
        """``list_runs`` 是 spawn 序（FIFO），不是取消序——乱序取消不改登记顺序。"""
        a = self.plane.spawn("a")
        b = self.plane.spawn("b")
        c = self.plane.spawn("c")

        assert await self.plane.cancel(c.run_id, reason="third") is True
        assert await self.plane.cancel(a.run_id, reason="first") is True

        assert [r.run_id for r in self.plane.list_runs()] == [
            a.run_id,
            b.run_id,
            c.run_id,
        ]
        assert [r.state for r in self.plane.list_runs()] == [
            RunState.CANCELLED,
            RunState.PENDING,
            RunState.CANCELLED,
        ]
        # 未被打扰的那个既没进终态，也没有取消原因
        assert b.cancel_reason is None and b.ended_at is None

    @pytest.mark.asyncio
    async def test_cancel_after_finish_does_not_overwrite_terminal(self) -> None:
        """已成功收尾的运行再被取消 ⇒ ``False``，状态/原因/结束时间一律不动。"""
        run = self.plane.spawn("x")
        assert self.plane.finish(run.run_id, state=RunState.SUCCEEDED) is True
        ended_at = run.ended_at

        assert await self.plane.cancel(run.run_id, reason="late") is False
        assert run.state == RunState.SUCCEEDED
        assert run.cancel_reason is None
        assert run.ended_at == ended_at, "取消被拒时不得刷新 ended_at"

    @pytest.mark.asyncio
    async def test_cancel_unknown_run_is_false(self) -> None:
        assert await self.plane.cancel("no-such-run") is False

    @pytest.mark.asyncio
    async def test_cancel_leaves_unrelated_bound_task_alive(self) -> None:
        """取消 A 不许波及 B 的绑定 Task（cancel 只碰自己的 run._task）。"""
        gate = asyncio.Event()
        started = asyncio.Event()

        async def work() -> None:
            started.set()
            await gate.wait()

        a = self.plane.spawn("a")
        b = self.plane.spawn("b")
        task_b = asyncio.create_task(work())
        assert self.plane.bind(b.run_id, task_b) is True
        self.plane.mark_running(b.run_id)
        await started.wait()

        assert await self.plane.cancel(a.run_id) is True

        assert not task_b.done(), "取消 A 把 B 的 Task 一起打掉了"
        assert b.state == RunState.RUNNING

        gate.set()
        await task_b

    @pytest.mark.asyncio
    async def test_cancel_wins_over_pending_run_without_task(self) -> None:
        """尚无绑定 Task 的 PENDING 运行也能取消（只是没有可打断的对象）。"""
        run = self.plane.spawn("pending-only")
        assert await self.plane.cancel(run.run_id) is True
        assert run.state == RunState.CANCELLED
        assert self.plane.active_count() == 0


# ---------------------------------------------------------------------------
# B. 并发取消
# ---------------------------------------------------------------------------


class TestConcurrentCancel:
    @pytest.mark.asyncio
    async def test_same_run_concurrent_cancel_has_exactly_one_winner(self) -> None:
        """N 个并发取消同一 run ⇒ 恰好一个 ``True``，取消原因来自胜者。

        判定"是否终态"与"置终态"之间**没有 await**，所以胜者是确定性的第一个
        真正被调度到的协程，而不是靠事件循环时序碰运气。
        """
        plane = ControlPlane()
        run = plane.spawn("x")

        async def attempt(idx: int) -> tuple[str, bool]:
            reason = f"racer-{idx}"
            return reason, await plane.cancel(run.run_id, reason=reason)

        outcomes = await asyncio.gather(*(attempt(i) for i in range(5)))
        winners = [reason for reason, ok in outcomes if ok]

        assert len(winners) == 1, f"并发取消出现 {len(winners)} 个胜者：{outcomes}"
        assert run.state == RunState.CANCELLED
        assert run.cancel_reason == winners[0], "取消原因不是胜者写的那个"
        assert sum(1 for _, ok in outcomes if not ok) == 4

    @pytest.mark.asyncio
    async def test_distinct_runs_concurrent_cancel_all_succeed(self) -> None:
        plane = ControlPlane()
        runs = [plane.spawn(f"r{i}") for i in range(5)]

        outcomes = await asyncio.gather(
            *(plane.cancel(r.run_id, reason="bulk") for r in runs)
        )

        assert outcomes == [True] * 5
        assert all(r.state == RunState.CANCELLED for r in runs)
        assert all(r.cancel_reason == "bulk" for r in runs)
        assert plane.active_count() == 0

    @pytest.mark.asyncio
    async def test_cancel_and_finish_race_has_exactly_one_winner(self) -> None:
        """cancel 与 finish 同时打同一个 run：两个方向各测一次，恰好一方赢。"""
        plane = ControlPlane()

        first = plane.spawn("cancel-first")
        assert await plane.cancel(first.run_id) is True
        assert plane.finish(first.run_id, state=RunState.SUCCEEDED) is False
        assert first.state == RunState.CANCELLED, "取消语义被 finish 覆盖"

        second = plane.spawn("finish-first")
        assert plane.finish(second.run_id, state=RunState.SUCCEEDED) is True
        assert await plane.cancel(second.run_id) is False
        assert second.state == RunState.SUCCEEDED, "成功终态被取消覆盖"

    @pytest.mark.asyncio
    async def test_concurrent_cancel_interrupts_bound_tasks_once(self) -> None:
        """并发取消带真实 Task 的多个 run：全部 ``True``，且不留悬挂 Task。"""
        plane = ControlPlane()
        gates = [asyncio.Event() for _ in range(3)]
        started = [asyncio.Event() for _ in range(3)]
        tasks = []

        for idx, (gate, begun) in enumerate(zip(gates, started)):
            async def work(gate=gate, begun=begun) -> None:
                begun.set()
                await gate.wait()

            run = plane.spawn(f"t{idx}")
            task = asyncio.create_task(work())
            assert plane.bind(run.run_id, task) is True
            plane.mark_running(run.run_id)
            tasks.append(task)

        await asyncio.gather(*(s.wait() for s in started))
        run_ids = [r.run_id for r in plane.list_runs()]
        outcomes = await asyncio.gather(*(plane.cancel(rid) for rid in run_ids))

        assert outcomes == [True] * 3
        assert all(task.done() for task in tasks)
        assert all(task.cancelled() for task in tasks)


# ---------------------------------------------------------------------------
# C. max_* 溢出行为
# ---------------------------------------------------------------------------


class TestMaxRunsFifo:
    def test_overflow_only_evicts_oldest_terminal(self) -> None:
        plane = ControlPlane(max_runs=2)
        a = plane.spawn("a")
        plane.finish(a.run_id)
        b = plane.spawn("b")
        plane.finish(b.run_id)
        c = plane.spawn("c")

        assert plane.status(a.run_id) is None, "最旧的终态记录没被淘汰"
        assert [r.run_id for r in plane.list_runs()] == [b.run_id, c.run_id]

    def test_successive_overflow_keeps_newest_two(self) -> None:
        """连续溢出：终态 + 循环淘汰后，留下的必须是**最新的** max_runs 条。"""
        plane = ControlPlane(max_runs=2)
        runs = []
        for i in range(5):
            run = plane.spawn(f"r{i}")
            plane.finish(run.run_id)
            runs.append(run)

        assert plane.stats()["total"] == 2
        assert [r.run_id for r in plane.list_runs()] == [
            runs[3].run_id,
            runs[4].run_id,
        ]
        assert len(plane.list_runs()) == plane.stats()["total"], (
            "登记表与 _order 长度漂移（淘汰时没清干净）"
        )

    def test_all_active_overflows_instead_of_dropping_work(self) -> None:
        """全是活动运行 ⇒ 放弃淘汰、宁可超容（不丢正在跑的），且不抛异常。"""
        plane = ControlPlane(max_runs=1)
        active = [plane.spawn(f"a{i}") for i in range(3)]

        assert plane.stats()["total"] == 3 > plane.stats()["max_runs"]
        assert [r.run_id for r in plane.list_runs()] == [r.run_id for r in active]
        assert plane.active_count() == 3

        # 都转终态后再登记一次 ⇒ 超容部分才被消化到 max_runs
        for run in active:
            plane.finish(run.run_id)
        fresh = plane.spawn("fresh")
        assert plane.stats()["total"] == 1
        assert plane.list_runs()[0].run_id == fresh.run_id

    @pytest.mark.asyncio
    async def test_evicted_run_cannot_be_resurrected(self) -> None:
        """被淘汰的 run_id 是**不存在**，不是"能救回来"——三个入口都返回 False。"""
        plane = ControlPlane(max_runs=1)
        old = plane.spawn("old")
        plane.finish(old.run_id)
        plane.spawn("new")  # 超容 ⇒ 淘汰 old

        assert plane.status(old.run_id) is None
        assert plane.finish(old.run_id) is False
        assert await plane.cancel(old.run_id) is False
        assert plane.bind(old.run_id, None) is False
        assert plane.mark_running(old.run_id) is False

    def test_max_runs_is_clamped_to_at_least_one(self) -> None:
        """``max_runs<=0`` 钳到 1：不许出现"永远淘汰得只剩 0 条"的死循环形态。"""
        for raw in (0, -5):
            plane = ControlPlane(max_runs=raw)
            assert plane.stats()["max_runs"] == 1

    def test_max_runs_one_evicts_previous_terminal_immediately(self) -> None:
        plane = ControlPlane(max_runs=1)
        a = plane.spawn("a")
        plane.finish(a.run_id)
        b = plane.spawn("b")

        assert plane.stats()["total"] == 1
        assert plane.status(b.run_id) is not None


class TestAdmissionBoundaries:
    @pytest.mark.asyncio
    async def test_concurrency_slot_is_released_by_cancel(self) -> None:
        """``active_count`` 是准入判据 ⇒ 取消立刻让出槽位（下一次 spawn 放行）。"""
        plane = ControlPlane(max_concurrency=1)
        first = plane.spawn("first")

        with pytest.raises(AdmissionDenied, match="max_concurrency"):
            plane.spawn("second")

        assert await plane.cancel(first.run_id) is True
        admitted = plane.spawn("second")
        assert admitted is not None
        assert plane.active_count() == 1

    def test_concurrency_boundary_is_inclusive_at_limit(self) -> None:
        """``active_count() >= max`` 才拒 ⇒ 第 max 个放行、第 max+1 个被拒。"""
        plane = ControlPlane(max_concurrency=2)
        plane.spawn("a")
        plane.spawn("b")
        with pytest.raises(AdmissionDenied):
            plane.spawn("c")

    def test_depth_boundary_is_inclusive_at_limit(self) -> None:
        plane = ControlPlane(max_depth=2)
        assert plane.spawn("ok", depth=2) is not None
        with pytest.raises(AdmissionDenied, match="max_depth"):
            plane.spawn("too-deep", depth=3)

    def test_zero_limits_never_deny(self) -> None:
        """默认 0 = 不限：深度再大也放行（与接线前逐位等价）。"""
        plane = ControlPlane()
        assert plane.spawn("deep", depth=99) is not None

    def test_rejected_spawn_registers_nothing(self) -> None:
        """准入被拒 ⇒ 不登记（否则 cancel 会打到一个从未运行的幽灵）。"""
        plane = ControlPlane(max_concurrency=1)
        plane.spawn("a")
        with pytest.raises(AdmissionDenied):
            plane.spawn("b")
        assert plane.stats()["total"] == 1

    def test_duplicate_run_id_is_refused(self) -> None:
        """同 ID 双登记会让 cancel 打错对象 ⇒ 显式 ValueError，不静默覆盖。"""
        plane = ControlPlane()
        plane.spawn("a", run_id="fixed")
        with pytest.raises(ValueError, match="already registered"):
            plane.spawn("b", run_id="fixed")
        assert plane.stats()["total"] == 1


# ---------------------------------------------------------------------------
# D. BudgetExceededError 处置口径（钉现状）
# ---------------------------------------------------------------------------


def _agentix_py_sources() -> list[tuple[str, str]]:
    root = REPO_ROOT / "agentix"
    assert root.is_dir(), "agentix/ 目录不存在"
    return [
        (p.relative_to(REPO_ROOT).as_posix(), p.read_text(encoding="utf-8-sig"))
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def _handler_type_names(handler: ast.ExceptHandler) -> set[str]:
    node = handler.type
    if node is None:  # 裸 except
        return set()
    parts = list(node.elts) if isinstance(node, ast.Tuple) else [node]
    names: set[str] = set()
    for part in parts:
        if isinstance(part, ast.Name):
            names.add(part.id)
        elif isinstance(part, ast.Attribute):
            names.add(part.attr)
    return names


class TestBudgetErrorDisposition:
    """口径 D 节逐条钉住——改代码必须同步改模块 docstring 的口径表。"""

    def test_error_is_a_plain_exception_not_agent_error(self) -> None:
        """口径 1：只有 ``Exception`` 这一层身份 ⇒ 走 chat_service 的兜底 except。"""
        from agentix.domain.errors import AgentError

        assert issubclass(BudgetExceededError, Exception)
        assert not issubclass(BudgetExceededError, AgentError), (
            "若它变成了 AgentError，回执形态会从 500 internal_error 变成可降级 "
            "200 —— 口径 3 必须同步改写"
        )

    def test_error_carries_dimension_and_usage(self) -> None:
        err = BudgetExceededError("steps", 20, 20)
        assert (err.dimension, err.used, err.limit) == ("steps", 20, 20)
        assert "budget exceeded: steps used=20 limit=20" in str(err)

    def test_production_has_no_dedicated_catch(self) -> None:
        """口径 2：``agentix/**`` 零处 ``except BudgetExceededError``。

        新增专用 catch ⇒ 本条红 ⇒ 先改模块 docstring 的口径表（catch 点/回执形态），
        再改代码。这不是"反对加 catch"，而是不许口径与代码各说一套。
        """
        offenders: list[str] = []
        for rel, source in _agentix_py_sources():
            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover - 语法不过的文件不归本守卫管
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and (
                    "BudgetExceededError" in _handler_type_names(node)
                ):
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            "生产代码出现了 BudgetExceededError 专用 catch："
            f"{offenders}——模块 docstring 的口径 2/3 必须先改"
        )

    def test_retryable_classification_reads_numbers_in_message(self) -> None:
        """口径 3 的例外：兜底分支按**异常文本**判瞬时 ⇒ 用量数字会改写回执。

        ``used=500`` / ``used=429`` / ``cost_usd=0.502`` 命中
        ``RETRYABLE_HINTS``（500/429/502）⇒ 预算超限被当成上游瞬断（503）。
        这是现状缺陷，用例只钉住它的存在，避免被当成"已处理"。
        """
        assert is_retryable(BudgetExceededError("steps", 3, 3)) is False
        assert is_retryable(BudgetExceededError("tokens", 500, 500)) is True
        assert is_retryable(BudgetExceededError("tool_calls", 429, 429)) is True
        assert is_retryable(BudgetExceededError("cost_usd", 0.502, 0.5)) is True

    def test_all_zero_budget_never_trips(self) -> None:
        """口径 4/5：全 0 ⇒ 每个 check 都短路；用多少都不抛（不限额 ≠ 超限）。"""
        manager = BudgetManager(
            budget=Budget(
                max_steps=0,
                max_llm_calls=0,
                max_tool_calls=0,
                max_tokens=0,
                max_latency_ms=0,
                max_cost_usd=0.0,
            )
        )
        for _ in range(100):
            manager.check_step()
            manager.record_step()
            manager.check_tool_call()
            manager.record_tool_call()
            manager.check_llm_call()
        manager.record_llm_call(tokens=10**9, cost_usd=1e6)
        manager.check_latency()

        snapshot = manager.snapshot()
        assert snapshot.used_steps == 100
        assert snapshot.max_steps == 0, "0 必须原样出现在快照里（下游靠它判不限额）"

    def test_default_budget_does_raise(self) -> None:
        """同一套代码，预算非 0 时确实会抛——证明"不抛"是 0 的语义，不是死代码。"""
        manager = BudgetManager(budget=Budget(max_steps=3))
        for _ in range(3):
            manager.check_step()
            manager.record_step()
        with pytest.raises(BudgetExceededError) as excinfo:
            manager.check_step()
        assert excinfo.value.dimension == "steps"
        assert (excinfo.value.used, excinfo.value.limit) == (3, 3)

    @pytest.mark.asyncio
    async def test_astep_over_budget_raises_before_recording_step(self) -> None:
        """超预算时 ``astep`` 在**登记步骤之前**就抛 ⇒ 状态里不留幽灵步。"""
        from agentix.runtime import AgentRuntime, Budget as RuntimeBudget, StepType

        runtime = AgentRuntime(budget=RuntimeBudget(max_steps=1))
        state = runtime.begin()

        with runtime.step(StepType.EXECUTING, "first"):
            pass
        assert len(state.steps) == 1

        with pytest.raises(BudgetExceededError):
            async with runtime.astep(StepType.EXECUTING, "second"):
                pass
        assert len(state.steps) == 1, "超预算的步骤被登记进了轨迹"

    def test_production_runtime_budget_is_all_zero(self) -> None:
        """口径 4：生产装配点（chat_service）六维全 0 —— 静态钉住，不看注释。"""
        path = REPO_ROOT / "agentix/application/services/chat_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        runtime_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AgentRuntime"
        ]
        assert len(runtime_calls) == 1, (
            f"chat_service 里 AgentRuntime 装配点数量变了：{len(runtime_calls)}"
        )
        keywords = {kw.arg: kw.value for kw in runtime_calls[0].keywords}
        assert "budget" in keywords, "Runtime 装配没传 budget（会落到 Budget() 的有限额默认值）"

        budget_call = keywords["budget"]
        assert isinstance(budget_call, ast.Call)
        assert getattr(budget_call.func, "id", None) == "RuntimeBudget", (
            "预算构造被换成了别的类 —— 口径 5 的『默认值有限额』会立刻生效"
        )
        dimensions = {
            kw.arg: ast.literal_eval(kw.value) for kw in budget_call.keywords
        }
        assert set(dimensions) == {
            "max_steps",
            "max_llm_calls",
            "max_tool_calls",
            "max_tokens",
            "max_latency_ms",
            "max_cost_usd",
        }, f"预算维度集合变了：{sorted(dimensions)}"
        assert all(v == 0 for v in dimensions.values()), (
            f"生产预算已不再全 0：{dimensions} —— 口径 4『不可达』失效，"
            "必须重写模块 docstring 的处置口径并配 catch 点"
        )

    def test_token_budget_error_code_is_a_different_mechanism(self) -> None:
        """口径 6：编排域的 ``TOKEN_BUDGET_EXCEEDED`` 是 ``AgentError``（可降级）。"""
        from agentix.domain.errors import DEGRADABLE, ErrorCode

        assert ErrorCode.TOKEN_BUDGET_EXCEEDED in DEGRADABLE
        assert not hasattr(BudgetExceededError("steps", 1, 1), "code"), (
            "BudgetExceededError 不该带 ErrorCode —— 它不属于四层错误分类"
        )


# ---------------------------------------------------------------------------
# E. bench 入口冒烟（验收①）——只证入口能跑、读数形态对，不断言时延数值
# ---------------------------------------------------------------------------


class TestCancelPropagationBenchEntry:
    def test_bench_sample_shape(self) -> None:
        from bench.cancel_propagation import collect_samples

        samples = collect_samples(repeats=1, samples_per_repeat=3)

        assert len(samples) == 3
        assert all(isinstance(v, float) and math.isfinite(v) and v >= 0.0 for v in samples)

    def test_payload_uses_skeleton_pooling_and_fingerprint(self, tmp_path: Path) -> None:
        from bench import cancel_propagation as bench_case
        from bench.fingerprint import REQUIRED_FINGERPRINT_KEYS
        from bench.results import write_result

        payload = bench_case.build_payload(
            ["--repeats", "5", "--samples", "2", "--out-dir", str(tmp_path)]
        )

        missing = set(REQUIRED_FINGERPRINT_KEYS) - set(payload["fingerprint"])
        assert not missing, f"指纹缺必需字段：{sorted(missing)}"
        assert payload["fingerprint"]["repeats"] == 5
        assert payload["fingerprint"]["temperature"] == "not_applicable"

        results = payload["results"]
        assert results["pooled_status"] == "pooled_from_raw_samples"
        assert results["quantile_source"] == "evals.perf_stats.nearest_rank_quantile"
        assert results["pooled_quantiles_ms"]["count"] == 10, "5 轮 × 每轮 2 个样本"
        assert len(results["per_repeat"]) == 5

        path = write_result(payload, out_dir=tmp_path)
        assert path.exists()

    def test_bench_rejects_fewer_than_five_repeats(self) -> None:
        from bench import cancel_propagation as bench_case

        with pytest.raises(SystemExit) as excinfo:
            bench_case.build_payload(["--repeats", "4", "--samples", "1"])
        assert excinfo.value.code != 0
