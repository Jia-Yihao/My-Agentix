"""P4（岗位对照·异常闭环 4.3）：``RollbackPort`` 生产实现 + 计划态权限切换。

借鉴 claw-code ``tools/src/lib.rs:6013-6081``「计划态 = 可回滚的**配置级**
权限切换（保存/恢复）」，把仓库里「仅协议 + 测试 fake」的 ``RollbackPort``
落成真实可用的域实现：

- **存储**：``InMemoryRollbackStore`` 实现 ``domain.ports.RollbackPort``
  ——deepcopy 双向隔离（快照与返回值都不是活引用）、per-run LRU 有界
  （容量满丢最旧检查点）、恢复不存在的检查点**抛异常**（协议约定：不静默
  降级）。内存态：进程内检查点，重启即失——与 ``InMemoryArtifactStore``
  同界，不假装持久化；
- **计划态**：``enter_plan_mode`` 幂等快照 ``executor.policy`` → 切
  ``ToolSelectionPolicy(max_risk_level="L0")`` 只读探查；``exit_plan_mode``
  按 checkpoint 恢复原策略（保存/恢复语义——恢复值就是进入前的快照，
  含 ``None``：生产默认无策略约束时退出后回到无约束）。**幂等是关键**：
  已在计划态时直接返回既有 checkpoint，绝不二次快照——否则会把 L0
  快照成「原策略」，退出时形同没退（设计红线）；
- **装配**：``install_plan_mode_tools()`` 仿 ``notify_bridge``——spec 声明在
  ``capabilities/tools/plan_mode.py``（registry_init flag 门控挂载），
  本函数只 ``ensure_registered()`` + ``register_handler``；``main._startup``
  仅在 ``AGENT_ROLLBACK_ENABLED=true`` 时调用（flag-off 零接线）。

``run_id`` 固定 ``"global"``：``ToolExecutor.policy`` 是进程级单例属性
（生产无 per-run 隔离通道），用全局槽语义——不假装支持并发多 run。

治理语义（Gotcha 1：出站即副作用）：``ToolExecutor.execute`` 的风险闸门 /
HITL 审批在 handler 之前——本模块不提供任何绕过路径；两工具声明 L0、
名称不命中 HITL 词表 ⇒ 有效风险 LOW ⇒ 免审批（计划态本身是收敛动作）。
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from typing import Any, Dict, Optional
from uuid import uuid4

from loguru import logger

from agentix.config.settings import settings

__all__ = [
    "PLAN_RUN_ID",
    "InMemoryRollbackStore",
    "get_rollback_store",
    "reset_rollback_store",
    "enter_plan_mode",
    "exit_plan_mode",
    "install_plan_mode_tools",
]

#: 计划态 run 槽（executor.policy 为进程级单例属性 ⇒ 全局槽，见模块 docstring）。
PLAN_RUN_ID = "global"

#: 单 run 保留的检查点上限（LRU：容量满丢最旧——有界内存，不无限累积）。
MAX_CHECKPOINTS_PER_RUN = 8

#: 保留的 run 数上限（防御性有界：生产恒单 "global" 槽）。
MAX_RUNS = 64


class InMemoryRollbackStore:
    """进程内检查点存储，实现 ``domain.ports.RollbackPort``。

    - ``snapshot``：deepcopy 落副本，返回 ``checkpoint_id``（uuid4 hex）；
    - ``restore``：按 ``(run_id, checkpoint_id)`` 取回并**再** deepcopy 一份
      ——调用方改返回值不会污染存储；不存在则抛 ``KeyError``（协议：
      「恢复不存在则抛异常」，不静默降级）；
    - 有界：单 run 检查点 LRU 上限 ``max_checkpoints_per_run``、run 数上限
      ``max_runs``；``restore`` 命中即 move_to_end（按使用时间 LRU）。
    """

    def __init__(
        self,
        *,
        max_checkpoints_per_run: int = MAX_CHECKPOINTS_PER_RUN,
        max_runs: int = MAX_RUNS,
    ) -> None:
        self._max_checkpoints = max(1, int(max_checkpoints_per_run))
        self._max_runs = max(1, int(max_runs))
        self._runs: "OrderedDict[str, OrderedDict[str, Dict[str, Any]]]" = OrderedDict()

    async def snapshot(self, run_id: str, state: Dict[str, Any]) -> str:
        """落一个检查点副本，返回 ``checkpoint_id``。"""
        checkpoint_id = uuid4().hex
        run = self._runs.get(run_id)
        if run is None:
            run = OrderedDict()
            self._runs[run_id] = run
        else:
            self._runs.move_to_end(run_id)
        run[checkpoint_id] = deepcopy(dict(state))
        while len(run) > self._max_checkpoints:
            run.popitem(last=False)  # 丢最旧
        while len(self._runs) > self._max_runs:
            self._runs.popitem(last=False)
        return checkpoint_id

    async def restore(self, run_id: str, checkpoint_id: str) -> Dict[str, Any]:
        """取回检查点副本；不存在抛 ``KeyError``（诚实失败，不静默降级）。"""
        run = self._runs.get(run_id)
        if run is None or checkpoint_id not in run:
            raise KeyError(
                f"checkpoint {checkpoint_id!r} not found for run {run_id!r}"
            )
        self._runs.move_to_end(run_id)
        run.move_to_end(checkpoint_id)  # 按使用时间 LRU：命中的检查点不再是最旧
        return deepcopy(run[checkpoint_id])


_rollback_store: Optional[InMemoryRollbackStore] = None

#: 当前计划态检查点（``None`` = 不在计划态）——与 store 同生命周期。
_active_checkpoint: Optional[str] = None


def get_rollback_store() -> InMemoryRollbackStore:
    """进程级单例（lazy）。"""
    global _rollback_store
    if _rollback_store is None:
        _rollback_store = InMemoryRollbackStore()
    return _rollback_store


def reset_rollback_store() -> None:
    """清空存储与计划态标记（测试隔离；不改 ``executor.policy``——
    残留 L0 是可见状态，由调用方显式清理，不在工具函数里藏副作用）。"""
    global _rollback_store, _active_checkpoint
    _rollback_store = None
    _active_checkpoint = None


async def enter_plan_mode(
    *, run_id: str = PLAN_RUN_ID, reason: str = ""
) -> Dict[str, Any]:
    """进入计划态：快照 ``executor.policy`` → 收紧到 L0（幂等，不二次快照）。"""
    global _active_checkpoint
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.selection import ToolSelectionPolicy

    executor = get_tool_executor()
    if _active_checkpoint is not None:
        # 幂等红线：已在计划态 ⇒ 复用原检查点。若二次快照，新快照的
        # 「原策略」就是 L0，退出时形同没退（原检查点被绕过）。
        return {
            "in_plan_mode": True,
            "changed": False,
            "already_active": True,
            "checkpoint_id": _active_checkpoint,
            "max_risk_level": "L0",
            "reason": reason,
        }
    store = get_rollback_store()
    previous_policy = executor.policy  # 快照在切换之前（切换失败 ⇒ 策略未动）
    checkpoint_id = await store.snapshot(run_id, {"policy": previous_policy})
    executor.policy = ToolSelectionPolicy(max_risk_level="L0")
    _active_checkpoint = checkpoint_id
    logger.info(
        "plan mode entered (checkpoint={}, previous_policy={})",
        checkpoint_id,
        type(previous_policy).__name__ if previous_policy is not None else None,
    )
    return {
        "in_plan_mode": True,
        "changed": True,
        "already_active": False,
        "checkpoint_id": checkpoint_id,
        "max_risk_level": "L0",
        "previous_max_risk_level": getattr(previous_policy, "max_risk_level", None),
        "reason": reason,
    }


async def exit_plan_mode(
    *, checkpoint_id: str = "", run_id: str = PLAN_RUN_ID
) -> Dict[str, Any]:
    """退出计划态：按 checkpoint 恢复原策略；未进入/检查点不存在 ⇒ 抛异常。"""
    global _active_checkpoint
    from agentix.tools.gateway.executor import get_tool_executor

    executor = get_tool_executor()
    target = (checkpoint_id or "").strip() or _active_checkpoint
    if not target:
        raise ValueError("plan mode is not active (call enter_plan_mode first)")
    store = get_rollback_store()
    # 检查点不存在 ⇒ KeyError 冒泡成 ToolResult 失败（诚实失败，不静默降级）
    state = await store.restore(run_id, target)
    restored = state.get("policy")  # None 也是合法恢复值（生产默认无策略）
    executor.policy = restored
    _active_checkpoint = None
    logger.info(
        "plan mode exited (checkpoint={}, restored_policy={})",
        target,
        type(restored).__name__ if restored is not None else None,
    )
    return {
        "in_plan_mode": False,
        "changed": True,
        "restored_checkpoint_id": target,
        "policy_restored": True,
        "max_risk_level": getattr(restored, "max_risk_level", None),
    }


def install_plan_mode_tools() -> None:
    """注册 ``enter_plan_mode`` / ``exit_plan_mode`` 处理器（幂等；
    依赖 flag-on 声明清单已有 spec——与 ``notify_bridge`` 同款）。"""
    if not getattr(settings, "AGENT_ROLLBACK_ENABLED", False):
        return  # belt-and-suspenders：调用方已判 flag，这里再判一次
    from agentix.tools.gateway.executor import get_tool_executor
    from agentix.tools.registry_init import ensure_registered

    ensure_registered()  # spec 先入 registry（executor 执行点要求 spec 存在）

    async def _enter(args: dict) -> dict:
        reason = str(args.get("reason") or "")[:200]
        return await enter_plan_mode(reason=reason)

    async def _exit(args: dict) -> dict:
        cid = str(args.get("checkpoint_id") or "").strip()
        return await exit_plan_mode(checkpoint_id=cid)

    executor = get_tool_executor()
    executor.register_handler("enter_plan_mode", _enter)
    executor.register_handler("exit_plan_mode", _exit)
