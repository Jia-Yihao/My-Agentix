"""P6-6.2 后台记忆评审（background review；§3.5 异步评审 + §5 表 6.2）。

借鉴 source-code-study §3.5「turn 结束后异步线程后台评审」与 §3.6 显式经验
沉淀——规则路由为兜底基线，F1b·M-P1-2 起增加 **LLM 路由分支**（prompt 决定
写 USER 画像还是留在 MEMORY 工作记忆，沿 Hermes background_review 语义）：

- :func:`route_memory_item`：确定性规则路由（无 IO、保持纯函数契约），
  把条目分到 ``user_profile``（晋升为稳定画像条目，经 MemoryPort 持久、由
  ``MEMORY_FANOUT_ENABLED`` 读路径召回）或 ``episodic``（摘要/卡片层已
  持久，不晋升——reason 诚实记录，不做假写入）；
- :func:`route_memory_item_llm`：异步 LLM 路由（fail-open——LLM 不可用/
  解析失败 ⇒ 退回规则路由，reason 标 ``fallback-rule:`` 前缀，绝不炸评审）；
- :func:`review_items`：逐项 fail-open——单项晋升失败只计数，不中断后续项；
  ``router`` 参数缺省走规则路由（既有契约零变化）；
- :func:`schedule_background_review`：fire-and-forget 挂到当前事件循环，
  异常只进 done-callback 日志（honest failure，不静默吞，不回传调用方）。

接线点：``task_handlers.handle_conversation_summary``（flag
``MEMORY_REVIEW_ENABLED`` 默认关 ⇒ handler 报告与基线完全一致）与
F1b 新增的 chat turn-end 生产者 ``ChatService._maybe_schedule_turn_review``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope

__all__ = [
    "TARGET_PROFILE",
    "TARGET_EPISODIC",
    "LOW_CONFIDENCE",
    "PROFILE_MIN_CONFIDENCE",
    "STABLE_CARD_TAGS",
    "REVIEW_ROUTE_PROMPT",
    "RouteDecision",
    "ReviewReport",
    "route_memory_item",
    "route_memory_item_llm",
    "review_items",
    "items_from_summary",
    "schedule_background_review",
    "schedule_review_for_summary",
]

#: 路由目标：晋升为稳定画像条目 / 留在情节层（不晋升）
TARGET_PROFILE = "user_profile"
TARGET_EPISODIC = "episodic"

#: 置信门槛：低于 LOW_CONFIDENCE 一律不进画像（防污染稳定画像）；
#: SEMANTIC 需 ≥ PROFILE_MIN_CONFIDENCE 且带稳定类标签才晋升。
LOW_CONFIDENCE = 0.5
PROFILE_MIN_CONFIDENCE = 0.6

#: 稳定类标签（与 semantic_memory 的 card_type 值一致）
STABLE_CARD_TAGS = frozenset({"preference", "instruction"})

#: F1b·M-P1-2：LLM 路由 prompt（Hermes background_review 语义——由 prompt
#: 决定写 USER 画像还是留在工作记忆；输出只认两个目标词，其余一律 fail-open）。
REVIEW_ROUTE_PROMPT = """你是记忆路由评审器。判断下面这条记忆应当作为【长期用户画像】\
(user_profile) 保留，还是只留在【工作记忆/情节层】(episodic)。

判断标准：
- user_profile：跨会话稳定的长期事实与偏好（用户身份、硬性偏好、长期约束、\
反复出现的稳定习惯），置信可靠、不会随单次会话改变；
- episodic：一次性上下文、任务细节、临时状态、低置信抽取、会话片段。

只输出一个词：user_profile 或 episodic。

记忆内容：
---
{content}
---"""


@dataclass(frozen=True)
class RouteDecision:
    """一条评审裁决：目标 + 机器可读 reason（进报告，不进 prompt）。"""

    target: str
    reason: str


@dataclass
class ReviewReport:
    """一次评审的结果计数与逐项裁决（供 handler 报告透传）。"""

    checked: int = 0
    promoted: int = 0
    skipped: int = 0
    failed: int = 0
    decisions: List[Tuple[str, str, str]] = field(default_factory=list)  # (item_id, target, reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "promoted": self.promoted,
            "skipped": self.skipped,
            "failed": self.failed,
            "decisions": [
                {"item_id": iid, "target": tgt, "reason": rsn}
                for iid, tgt, rsn in self.decisions
            ],
        }


def route_memory_item(item: MemoryItem) -> RouteDecision:
    """纯规则路由（确定性、无 LLM、无 IO）。规则顺序即优先级。"""
    if item.scope != MemoryScope.USER:
        return RouteDecision(TARGET_EPISODIC, "scope-not-user")
    if item.confidence < LOW_CONFIDENCE:
        return RouteDecision(TARGET_EPISODIC, "low-confidence")
    if item.kind == MemoryKind.USER:
        return RouteDecision(TARGET_PROFILE, "kind-user")
    if item.kind == MemoryKind.SEMANTIC:
        if item.confidence >= PROFILE_MIN_CONFIDENCE and (
            STABLE_CARD_TAGS & set(item.tags)
        ):
            return RouteDecision(TARGET_PROFILE, "stable-semantic")
        return RouteDecision(TARGET_EPISODIC, "semantic-not-stable")
    if item.kind == MemoryKind.PROCEDURAL:
        # Procedural 属 Skill 域（6.3 由 create_skill_draft 显式沉淀），
        # 评审不代劳——记 episodic 保持诚实。
        return RouteDecision(TARGET_EPISODIC, "procedural-skill-domain")
    return RouteDecision(TARGET_EPISODIC, f"kind-{item.kind.value}")


async def route_memory_item_llm(
    item: MemoryItem, *, client: Any = None
) -> RouteDecision:
    """F1b·M-P1-2：LLM 路由（prompt 决定目标）；任何失败 fail-open 回规则。

    - ``client`` 缺省时经 ``get_llm_client(role="summarizer")`` 懒获取
      （与 compactor 同工厂，不新增客户端面）；
    - 输出解析只认 ``user_profile`` / ``episodic`` 两个词（大小写不敏感，
      含子串即可）；其余输出/异常 ⇒ 规则路由兜底，reason 加
      ``fallback-rule:`` 前缀（报告里诚实可审计）。
    """
    try:
        if client is None:
            from agentix.application.agents.llm_factory import get_llm_client

            client = get_llm_client(
                temperature=0.0, tags=["memory_review"], role="summarizer"
            )
        prompt = REVIEW_ROUTE_PROMPT.format(content=item.content[:2000])
        response = await client.ainvoke([{"role": "user", "content": prompt}])
        text = str(getattr(response, "content", "") or "").strip().lower()
        if "user_profile" in text:
            return RouteDecision(TARGET_PROFILE, "llm-route")
        if "episodic" in text:
            return RouteDecision(TARGET_EPISODIC, "llm-route")
        raise ValueError(f"unrecognized llm route output: {text[:64]!r}")
    except Exception as exc:  # noqa: BLE001 - LLM 不可用必须能评审
        logger.warning("llm route degraded, rule fallback (non-fatal): {}", exc)
        fallback = route_memory_item(item)
        return RouteDecision(fallback.target, f"fallback-rule:{fallback.reason}")


async def review_items(
    manager: Any,
    items: List[MemoryItem],
    *,
    router: Optional[Callable[[MemoryItem], Awaitable[RouteDecision]]] = None,
) -> ReviewReport:
    """逐项路由并执行晋升；单项失败只计数（fail-open），不中断后续项。

    :param manager: 提供 ``remember(item)`` 的扇出（``MemoryManager`` 或测试替身）。
    :param router: 可选异步路由器（F1b·M-P1-2 传 LLM 路由）；缺省或路由器
        抛异常 ⇒ 规则路由 ``route_memory_item``（既有契约零变化）。
    """
    report = ReviewReport()
    for item in items:
        if router is not None:
            try:
                decision = await router(item)
            except Exception as exc:  # noqa: BLE001 - 路由器坏了也不能中断
                logger.warning(
                    "review router failed, rule fallback (non-fatal): {}", exc
                )
                decision = route_memory_item(item)
        else:
            decision = route_memory_item(item)
        report.checked += 1
        report.decisions.append(
            (item.item_id or item.content[:24], decision.target, decision.reason)
        )
        if decision.target == TARGET_EPISODIC:
            report.skipped += 1
            continue
        try:
            await manager.remember(item)
        except Exception as exc:  # noqa: BLE001 - 评审不能炸调用方
            report.failed += 1
            logger.warning(
                "memory review promote failed ({}): {}", item.item_id or "-", exc
            )
        else:
            report.promoted += 1
    return report


def schedule_background_review(
    factory: Callable[[], Awaitable[ReviewReport]],
) -> Optional["asyncio.Task[ReviewReport]"]:
    """fire-and-forget：把评审任务挂到当前事件循环，异常只记日志。

    无运行循环（同步上下文调用）⇒ 返回 ``None`` 并告警——诚实不伪装成功。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("no running event loop; background memory review skipped")
        return None
    task = loop.create_task(factory())

    def _on_done(done: "asyncio.Task[ReviewReport]") -> None:
        if done.cancelled():
            return
        exc = done.exception()
        if exc is not None:
            logger.warning("background memory review failed (non-fatal): {}", exc)

    task.add_done_callback(_on_done)
    return task


def items_from_summary(
    summary_text: str, *, user_id: str = "default_user"
) -> List[MemoryItem]:
    """会话摘要 → 语义卡片 → ``MemoryItem``（kind=SEMANTIC, scope=USER）。"""
    from agentix.application.services.semantic_memory import extract_cards

    items: List[MemoryItem] = []
    for card in extract_cards(summary_text or ""):
        items.append(
            MemoryItem(
                content=card.content,
                kind=MemoryKind.SEMANTIC,
                scope=MemoryScope.USER,
                item_id=f"card:{card.card_id}",
                subject=user_id,
                confidence=card.confidence,
                tags=(card.card_type,),
                meta={"card_id": card.card_id, "importance": card.importance},
            )
        )
    return items


def schedule_review_for_summary(
    summary_text: str,
    *,
    user_id: str = "default_user",
    manager_factory: Optional[Callable[[], Any]] = None,
    use_llm: bool = False,
) -> Optional["asyncio.Task[ReviewReport]"]:
    """``handle_conversation_summary`` / turn-end 生产者接线入口。

    无卡片 ⇒ 零调度（``None``）；``use_llm=True``（F1b·M-P1-2）⇒ 走
    :func:`route_memory_item_llm`（内部 fail-open 回规则）。
    """
    items = items_from_summary(summary_text, user_id=user_id)
    if not items:
        return None

    async def _run() -> ReviewReport:
        if manager_factory is not None:
            manager = manager_factory()
        else:
            from agentix.application.services.memory_manager import get_memory_manager

            manager = get_memory_manager()
        router = route_memory_item_llm if use_llm else None
        return await review_items(manager, items, router=router)

    return schedule_background_review(_run)
