"""A-P2-1 子代理隔离三件套（Stage F2c-b，flag ``MULTIAGENT_SUBAGENT_ISOLATION_ENABLED`` 默认关）。

multiagent-optimization §6 A-P2-1：``ephemeral_prompt / skip_memory /
独立 session + budget`` + 父记忆快照只读拷贝 + 心跳保活 + live registry
（kill/pause/steer）。本模块是**纯基础设施**（零 I/O、零 LLM），生产接线在
``lg_supervisor``（flag-off 走原路径逐位等价，flag-on 才启用隔离语义）：

* **ephemeral_prompt**：子代理只见任务面（goal + 黑板），不见父对话轨迹；
* **skip_memory**：父记忆的**活**上下文不进子代理（配置面摘除
  ``memory_context``），唯一记忆入口是 :func:`take_memory_snapshot` 产出的
  只读快照（``memory_snapshot_readonly``，子代理无任何写回路径）；
* **独立 session + budget**：每个子代理 actor 一个 :class:`SubAgentSession`
  （session_id + 独立 :class:`PeerBudget` 副本），经 Manager ``budgets``
  单源注入——预算随会话生命周期归属，而非匿名共享；
* **心跳保活 + live registry**：包装层每次被调即 ``heartbeat``；
  :class:`LiveSubagentRegistry` 提供 kill / pause / steer 控制面
  （kill 终态保留审计记录，pause 由 :func:`wait_while_paused` 有界等待、
  超时 fail-open 放行，steer 指令注入下一次任务面 context）。
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from agentix.orchestration.strategies.multi_agent.manager import PeerBudget
from agentix.orchestration.strategies.multi_agent.peer import AgentRole, PeerResult

__all__ = [
    "IsolationPolicy",
    "MemorySnapshot",
    "SubAgentSession",
    "LiveSubagentRegistry",
    "SESSION_ALIVE",
    "SESSION_PAUSED",
    "SESSION_KILLED",
    "take_memory_snapshot",
    "build_task_face_prompt",
    "wait_while_paused",
    "ephemeral_task_face_state",
    "isolated_config",
    "session_gate",
    "wrap_peer",
]

SESSION_ALIVE = "alive"
SESSION_PAUSED = "paused"
SESSION_KILLED = "killed"

#: 心跳轮询间隔（秒）——等待 pause 解除 / kill 的粒度
_PAUSE_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class IsolationPolicy:
    """隔离三件套开关面（frozen：会话生命周期内策略不可变）。"""

    #: 子代理只收任务面 prompt（goal + 黑板），不拼父轨迹
    ephemeral_prompt: bool = True
    #: 父记忆**活**上下文不进子代理（只读快照除外）
    skip_memory: bool = True
    #: 允许注入父记忆**只读快照**（唯一记忆入口；关掉 ⇒ 子代理零记忆）
    memory_snapshot_readonly: bool = True
    #: 心跳保鲜窗口（秒）：``now - last_heartbeat > ttl`` ⇒ 视为失活
    heartbeat_ttl_seconds: float = 30.0
    #: pause 等待上限（秒）：超时 fail-open 放行（绝不悬挂）
    max_pause_wait_seconds: float = 5.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ephemeral_prompt": self.ephemeral_prompt,
            "skip_memory": self.skip_memory,
            "memory_snapshot_readonly": self.memory_snapshot_readonly,
            "heartbeat_ttl_seconds": self.heartbeat_ttl_seconds,
            "max_pause_wait_seconds": self.max_pause_wait_seconds,
        }


@dataclass(frozen=True)
class MemorySnapshot:
    """父记忆只读快照：文本面 + （可选）结构化面深拷贝后冻结。"""

    text: str = ""
    #: Mapping 深拷贝 → MappingProxyType（写入抛 TypeError——真只读，非口头）
    data: Optional[Mapping] = None
    created_at: float = 0.0

    @property
    def chars(self) -> int:
        return len(self.text)

    def section(self) -> str:
        """注入任务面 prompt 的段落（文本优先，结构面退化为键值摘要）。"""
        if self.text:
            return self.text
        if self.data is not None:
            return "\n".join(f"{k}: {v}" for k, v in self.data.items())
        return ""


def take_memory_snapshot(memory_context: Any) -> Optional[MemorySnapshot]:
    """把父记忆活上下文固化为只读快照（str 原样冻结；Mapping 深拷贝冻结）。

    空值（``None`` / 空串 / 空 Mapping）⇒ ``None``（不产无意义快照）。
    """
    if memory_context is None:
        return None
    if isinstance(memory_context, str):
        if not memory_context:
            return None
        return MemorySnapshot(text=memory_context, created_at=time.time())
    if isinstance(memory_context, Mapping):
        if not memory_context:
            return None
        frozen = MappingProxyType(copy.deepcopy(dict(memory_context)))
        return MemorySnapshot(text="", data=frozen, created_at=time.time())
    return None


def build_task_face_prompt(
    goal: str,
    *,
    blackboard: str = "",
    parent_context: str = "",
    snapshot: Optional[MemorySnapshot] = None,
    policy: Optional[IsolationPolicy] = None,
) -> str:
    """组装子代理任务面 prompt（ephemeral 语义的唯一构造点）。

    * ``ephemeral_prompt=True`` ⇒ ``parent_context``（父轨迹）**永不**出现；
    * 只读快照段按 ``memory_snapshot_readonly`` 注入（唯一记忆入口）。
    """
    pol = policy or IsolationPolicy()
    parts: List[str] = [f"【目标】{goal or ''}"]
    if blackboard:
        parts.append(f"【黑板上下文】{blackboard}")
    if snapshot is not None and pol.memory_snapshot_readonly:
        parts.append(f"【父记忆快照（只读）】\n{snapshot.section()}")
    if not pol.ephemeral_prompt and parent_context:
        parts.append(f"【父上下文】{parent_context}")
    return "\n\n".join(parts)


@dataclass
class SubAgentSession:
    """子代理会话：独立 session_id + 独立 budget + 心跳/控制面状态。"""

    actor: str
    budget: PeerBudget
    policy: IsolationPolicy = field(default_factory=IsolationPolicy)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_run_id: str = ""
    state: str = SESSION_ALIVE
    created_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    #: 待消费的 steer 指令（take_steering 弹出，一次性）
    steering: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actor": self.actor,
            "parent_run_id": self.parent_run_id,
            "state": self.state,
            "budget": {
                "max_steps": self.budget.max_steps,
                "max_messages": self.budget.max_messages,
            },
        }


class LiveSubagentRegistry:
    """live registry：注册 / 心跳 / kill·pause·steer 控制面（审计保留）。"""

    def __init__(self, *, policy: Optional[IsolationPolicy] = None) -> None:
        self.policy = policy or IsolationPolicy()
        self._sessions: Dict[str, SubAgentSession] = {}

    # -- 注册 / 查询 ---------------------------------------------------------

    def register(self, session: SubAgentSession) -> SubAgentSession:
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[SubAgentSession]:
        return self._sessions.get(session_id)

    def by_actor(self, actor: str) -> Optional[SubAgentSession]:
        for session in self._sessions.values():
            if session.actor == actor:
                return session
        return None

    def sessions(self) -> List[SubAgentSession]:
        return list(self._sessions.values())

    # -- 心跳保活 ------------------------------------------------------------

    def heartbeat(self, session_id: str, *, at: Optional[float] = None) -> bool:
        """刷新心跳；killed 终态不再保活（返回 False）。"""
        session = self._sessions.get(session_id)
        if session is None or session.state == SESSION_KILLED:
            return False
        session.last_heartbeat = at if at is not None else time.time()
        return True

    def is_alive(
        self,
        session_id: str,
        *,
        ttl: Optional[float] = None,
        at: Optional[float] = None,
    ) -> bool:
        """state==alive 且心跳在 TTL 窗口内（``at`` 可注入做时间旅行测试）。"""
        session = self._sessions.get(session_id)
        if session is None or session.state != SESSION_ALIVE:
            return False
        window = ttl if ttl is not None else self.policy.heartbeat_ttl_seconds
        now = at if at is not None else time.time()
        return (now - session.last_heartbeat) <= window

    # -- kill / pause / steer -------------------------------------------------

    def pause(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.state == SESSION_KILLED:
            return False
        if session.state == SESSION_PAUSED:
            return True
        session.state = SESSION_PAUSED
        return True

    def resume(self, session_id: str, *, at: Optional[float] = None) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.state != SESSION_PAUSED:
            return False
        session.state = SESSION_ALIVE
        self.heartbeat(session_id, at=at)
        return True

    def kill(self, session_id: str) -> bool:
        """kill = 终态（记录保留作审计，heartbeat/steer/resume 对其失效）。"""
        session = self._sessions.get(session_id)
        if session is None or session.state == SESSION_KILLED:
            return False
        session.state = SESSION_KILLED
        return True

    def steer(self, session_id: str, instruction: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.state == SESSION_KILLED:
            return False
        session.steering = str(instruction)
        return True

    def take_steering(self, session_id: str) -> str:
        session = self._sessions.get(session_id)
        if session is None:
            return ""
        taken = session.steering
        session.steering = ""
        return taken

    # -- 观测 -----------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {
            SESSION_ALIVE: 0,
            SESSION_PAUSED: 0,
            SESSION_KILLED: 0,
        }
        now = time.time()
        records: List[Dict[str, Any]] = []
        for session in sorted(self._sessions.values(), key=lambda s: s.session_id):
            counts[session.state] = counts.get(session.state, 0) + 1
            record = session.to_dict()
            record["heartbeat_age"] = round(max(0.0, now - session.last_heartbeat), 3)
            record["steering_pending"] = bool(session.steering)
            records.append(record)
        return {"counts": counts, "sessions": records}


async def wait_while_paused(
    registry: LiveSubagentRegistry,
    session_id: str,
    *,
    policy: Optional[IsolationPolicy] = None,
    poll_seconds: float = _PAUSE_POLL_SECONDS,
) -> str:
    """等待 pause 解除：resume ⇒ alive；kill ⇒ killed；超时 ⇒ fail-open 放行。

    绝不无限悬挂（``max_pause_wait_seconds`` 上限）。
    """
    pol = policy or registry.policy
    deadline = time.monotonic() + max(0.0, pol.max_pause_wait_seconds)
    while True:
        session = registry.get(session_id)
        if session is None:
            return SESSION_KILLED
        if session.state != SESSION_PAUSED:
            return session.state
        if time.monotonic() >= deadline:
            return SESSION_ALIVE  # fail-open：等待超时按放行处理
        await asyncio.sleep(poll_seconds)


def ephemeral_task_face_state(state: Any) -> Any:
    """把父状态收敛为任务面：messages 只留最后一条（父对话轨迹不进子代理）。

    浅拷贝（router/steps/documents 等任务态字段原样共享——它们属于任务面）；
    拷贝失败 ⇒ 原样返回（fail-open，绝不打断执行）。
    """
    try:
        clone = copy.copy(state)
    except Exception:  # noqa: BLE001 —— 隔离是增强，失败不阻断
        return state
    messages = list(getattr(state, "messages", None) or [])
    if messages:
        try:
            clone.messages = [messages[-1]]
        except Exception:  # noqa: BLE001
            return state
    return clone


def isolated_config(
    config: Any, snapshot: Optional[MemorySnapshot] = None
) -> Any:
    """flag-on 的 config 面：摘除活 ``memory_context``（skip_memory），
    只读快照以 ``memory_snapshot`` 键提供。非 dict 原样返回。"""
    if not isinstance(config, dict):
        return config
    cfg = dict(config)
    configurable = dict(cfg.get("configurable") or {})
    configurable.pop("memory_context", None)
    if snapshot is not None:
        configurable["memory_snapshot"] = snapshot
    cfg["configurable"] = configurable
    return cfg


async def session_gate(
    registry: LiveSubagentRegistry,
    session: SubAgentSession,
    *,
    role: AgentRole,
) -> Optional[PeerResult]:
    """子代理执行前置闸：心跳 → pause 有界等待 → kill 短路。

    ``None`` ⇒ 放行；返回 :class:`PeerResult` ⇒ kill 后的短路结果
    （决策 reject + ``session=killed`` 标记，绝不执行内层工作）。
    """
    registry.heartbeat(session.session_id)
    state = registry.get(session.session_id)
    current = state.state if state is not None else SESSION_KILLED
    if current == SESSION_PAUSED:
        current = await wait_while_paused(registry, session.session_id, policy=session.policy)
        if current != SESSION_KILLED:
            registry.heartbeat(session.session_id)
    if current == SESSION_KILLED:
        return PeerResult(
            agent_id=session.actor,
            role=role,
            output="",
            ok=False,
            error="session_killed",
            meta={"decision": "reject", "session": SESSION_KILLED},
        )
    return None


def wrap_peer(
    inner: Any,
    *,
    registry: LiveSubagentRegistry,
    session: SubAgentSession,
    role: AgentRole,
    snapshot: Optional[MemorySnapshot] = None,
) -> Any:
    """把隔离语义包在 Peer 内层函数外（heartbeat/steer/快照段/kill 短路）。"""

    async def wrapped(*, goal: str, context: str, round: int, **kw: Any) -> Any:
        gated = await session_gate(registry, session, role=role)
        if gated is not None:
            return gated
        ctx = str(context or "")
        steering = registry.take_steering(session.session_id)
        if steering:
            ctx = f"{ctx}\n[steering] {steering}"
        if snapshot is not None and session.policy.memory_snapshot_readonly:
            text = snapshot.section()
            if text:
                ctx = f"{ctx}\n\n【父记忆快照（只读）】\n{text}"
        out = await _maybe_await(inner(goal=goal, context=ctx, round=round, **kw))
        if steering and isinstance(out, PeerResult):
            meta = dict(out.meta or {})
            meta["steered"] = True
            out.meta = meta
        return out

    return wrapped


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def session_budgets(sessions: Tuple[SubAgentSession, ...]) -> Dict[str, PeerBudget]:
    """``{actor: session.budget}``——Manager 的 budgets 单源注入面。"""
    return {session.actor: session.budget for session in sessions}
