"""任务级 Agent Run API（Control Plane 的 HTTP 化；A1c 上移至接口层）。

把「一次 Agent 运行」从 Chatbot 会话语义升级为一等公民：
``POST /agents/runs`` 派生运行 → ``GET /agents/runs/{run_id}`` 查状态 →
``POST /agents/runs/{run_id}/cancel`` 取消。Chatbot API 之外，
这是 Agent Runtime API 的入口。

A1c（统一执行链）：本模块自检索域的 ``agent_runs`` 路由模块
**上移**至接口层——Run API 是协议转换（HTTP ↔ ControlPlane），按分层
归属 ``interfaces``；路径从 ``/api/v1/retrieval/agents/runs`` 迁为
``/api/v1/agents/runs``（路由路径 ``/agents/runs`` 不变，仅去掉 retrieval
挂载前缀），文档（README / failure-recovery runbook）同步。

设计约束（遵守既有纪律）：
- **运行态默认只在内存**（``ControlPlane`` 有界 FIFO）。T21 起可选落库
  （``RUN_STATE_PERSIST_ENABLED``，默认关）：开时 spawn/状态迁移经平面的后台写队列
  落 ``agent_run_states``，单查与列表在内存 miss 时**回落到库**（重启后仍可查历史
  终态），合并口径见本模块 ``api_list_agent_runs``；写库失败 fail-open（只 warning +
  ``stats().persist_*`` 计数），**绝不阻断运行**。flag-off 时本模块与基线逐位等价：
  不触持久层、响应形状不变（含 ``meta`` 不出现 ``thread_id`` 键）；
- 执行后端走 :mod:`agentix.application.services.agent_executors` 注册表，
  API 层不关心执行器实现（``probe`` 离线可测 + ``chat`` 真实 Agent 图）；
- 超时语义：``timeout_s`` 由 runner 统一 ``asyncio.wait_for`` 施加，
  超时落 ``FAILED``（error=timeout after Ns），绝不悬挂；
- 取消语义：``ControlPlane.cancel`` 置终态并真正打断底层 Task；
  终态幂等（取消后执行器结果不会覆盖 cancelled）。

鉴权（上移后口径不变：默认拒绝无票请求）：router 级严格 Bearer JWT
（``require_run_bearer``，只验签 + 过期 + type=access，不查库——区别于
retrieval 域 ``get_current_user`` 的 User 行校验；Run API 不消费用户行）。
``AUTH_COOKIE_SESSION_FALLBACK`` 的会话 Cookie 回退**不适用**于本路由
（接口层不依赖 persistence；需要 Cookie 的调用方改带 Bearer 头）。

T20 客户端重试幂等（契约真源 ``tests/test_p143_run_idempotency.py``）：
``POST /agents/runs`` 支持 ``Idempotency-Key`` header，同键重放返 200 复用原
run。**保证边界 = 进程存活期**（键存储只在内存，跨重启不保证——持久层属
T21，不得超卖）。与审批侧「一次批准一次执行」（``agentix/tools/gateway/
human_review.py``）是**两层不同的幂等**，互不援引：本处防的是 HTTP 调用方
超时重试重复创建运行，那一层管的是批准消费，任何一方不构成对另一方的保证。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agentix.application.services.agent_executors import (
    executor_names,
    get_executor,
)
from agentix.runtime.control_plane import (
    AgentRun,
    RunState,
    get_control_plane,
)
from agentix.application.services.token_budget import TokenBudget
from agentix.security.jwt import safe_decode_access_token

logger = logging.getLogger("agentix.interfaces.runs")

#: 默认单次运行超时（秒）——必须有限，防止后台任务悬挂
DEFAULT_TIMEOUT_S = 120.0
#: timeout_s 上限（防呆）
MAX_TIMEOUT_S = 3600.0
#: T21 列表回落的库侧读取上限（有界读取：历史 run 不做无界全表拉取）
PERSIST_LIST_LIMIT = 200

# ---------------------------------------------------------------------------
# T20：Idempotency-Key 进程内键存储（契约：tests/test_p143_run_idempotency.py）
# ---------------------------------------------------------------------------

#: 幂等 header 名（对外契约字面量，不得改名）
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: 键长上限（字符数）——键是调用方提供的任意字符串，必须设界
IDEMPOTENCY_MAX_KEY_CHARS = 256
#: TTL（秒，自登记起算）——过期键视同不存在（同键重试会新建运行）
IDEMPOTENCY_TTL_S = 3600.0
#: 键条目上限——超限按登记顺序 FIFO 逐出；与 TTL 共同构成有界内存的充要两条
IDEMPOTENCY_MAX_ENTRIES = 4096


@dataclass(frozen=True)
class IdempotencyClaim:
    """``claim`` 结果：``replayed=True`` 表示键已在窗内，复用原运行。"""

    replayed: bool
    run_id: str
    agent: str


@dataclass(frozen=True)
class _Entry:
    run_id: str
    agent: str
    expires_at: float


class RunIdempotencyStore:
    """客户端重试幂等键的进程内存存储。**保证边界=进程存活期**。

    为什么只在内存：持久化幂等层归 T21（运行态落 Postgres），本类是其降级
    口径——跨重启、崩溃恢复、多副本下同键可能再次创建运行，契约文档如实
    写明了这条边界，不得超卖。

    并发判据：本端点为 ``async def``，FastAPI 恒在事件循环线程执行 ⇒ 原子性成立
    的形态是**单一事件循环**——``claim`` 的「检查键 + 登记 run_id」临界区内零
    ``await``，在事件循环线程内即构成互斥；只靠"查了再写"两步写法就是竞态（两个
    请求同时查不到键会各建一个运行）。外层 ``threading.Lock`` 只是存储层对任意
    调用线程的**防御性守卫**，**本契约不为"路由被下放线程池"的部署形态作保证**：
    该形态下本实现工厂（spawn+bind）里的 ``asyncio.create_task`` 要求调用线程
    有运行中的事件循环、``ControlPlane`` 亦无线程安全原语，两层守卫都救不了。
    ``make_run`` 的约束是：同步、且在临界区内不得 ``await``（本契约不宣称其
    线程可重入安全；无锁反向依赖：ControlPlane 不取本锁，不存在死锁环）。

    清理口径（与契约文档 §4 逐字对应）：TTL 过期 + 上限 FIFO 逐出 +
    惰性清扫（每次键操作顺带清除队首已过期条目，无后台定时器）。
    """

    def __init__(
        self,
        *,
        ttl_s: float = IDEMPOTENCY_TTL_S,
        max_entries: int = IDEMPOTENCY_MAX_ENTRIES,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = float(ttl_s)
        self._max_entries = max(1, int(max_entries))
        # 时钟注入点：TTL 用例不许真等一小时（测试传假时钟）
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        # 观测计数（无高基数维度，只有聚合）
        self._runs_created = 0
        self._hits = 0
        self._expired_swept = 0
        self._evicted = 0

    def claim(
        self, key: str, make_run: Callable[[], Tuple[str, str]]
    ) -> IdempotencyClaim:
        """原子地「查键 → 未命中则建运行并登记」。"""
        with self._lock:
            now = self._time_fn()
            # 惰性清扫：TTL 恒定 + 单调钟 ⇒ 插入序=过期序，从头扫到未过期即停
            while self._entries:
                oldest_entry = next(iter(self._entries.values()))
                if oldest_entry.expires_at > now:
                    break
                self._entries.popitem(last=False)
                self._expired_swept += 1

            entry = self._entries.get(key)
            if entry is not None:  # 未过期（过期已被上面清扫掉）
                self._hits += 1
                return IdempotencyClaim(True, entry.run_id, entry.agent)

            run_id, agent = make_run()
            self._entries[key] = _Entry(run_id, agent, now + self._ttl_s)
            self._runs_created += 1
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
                self._evicted += 1
            return IdempotencyClaim(False, run_id, agent)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "runs_created": self._runs_created,
                "hits": self._hits,
                "expired_swept": self._expired_swept,
                "evicted": self._evicted,
            }


_store_lock = threading.Lock()
_idempotency_store: Optional[RunIdempotencyStore] = None


def get_run_idempotency_store() -> RunIdempotencyStore:
    """进程级单例（与 ControlPlane 同生命周期语义：T21 持久层落地后替换）。"""
    global _idempotency_store
    if _idempotency_store is None:
        with _store_lock:
            if _idempotency_store is None:
                _idempotency_store = RunIdempotencyStore()
    return _idempotency_store


def reset_run_idempotency_store(
    *,
    ttl_s: Optional[float] = None,
    max_entries: Optional[int] = None,
    time_fn: Optional[Callable[[], float]] = None,
) -> RunIdempotencyStore:
    """重建单例（测试隔离 + 假时钟/口径注入点；生产启动路径不调用）。"""
    global _idempotency_store
    with _store_lock:
        _idempotency_store = RunIdempotencyStore(
            ttl_s=IDEMPOTENCY_TTL_S if ttl_s is None else ttl_s,
            max_entries=IDEMPOTENCY_MAX_ENTRIES if max_entries is None else max_entries,
            time_fn=time_fn if time_fn is not None else time.monotonic,
        )
        return _idempotency_store


def _key_fingerprint(key: str) -> str:
    """键的可关联形式：SHA-256 前 12 位。

    键是调用方提供的任意字符串，可能承载调用方侧敏感语义，且属高基数内容
    （约定 3 禁止进指标 label）——日志里只出现指纹，不出现原文。
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _extract_idempotency_key(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """解析 ``Idempotency-Key`` header，返回 (键, 拒绝理由)。

    header 缺位 ⇒ ``(None, None)``：调用方完全没带键，走基线路径。
    拒绝理由只说为什么被拒，**不回放键内容**（约定 6 ③ 精神：被拒输入
    不进响应体，错误路径本身不得变成回显器）。
    """
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None, None
    key = raw.strip()
    if not key:
        return None, "Idempotency-Key must be a non-empty string"
    if len(key) > IDEMPOTENCY_MAX_KEY_CHARS:
        return None, f"Idempotency-Key exceeds {IDEMPOTENCY_MAX_KEY_CHARS} characters"
    return key, None


def require_run_bearer(request: Request) -> None:
    """严格 Bearer JWT 校验（上移前 retrieval ``get_current_user`` 的默认口径）。

    无 token / token 无效或过期 ⇒ 401。不查库（Run API 不消费用户行，
    v1 轻量域纪律）；``V1_REQUIRE_JWT`` 不 gate 本路由——上移前它就是
    默认拒绝，上移后不得出现「默认开→默认关」的鉴权倒退。
    """
    auth = request.headers.get("Authorization", "")
    token = ""
    parts = auth.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1].strip()
    if not token or safe_decode_access_token(token) is None:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(dependencies=[Depends(require_run_bearer)])


class AgentRunCreate(BaseModel):
    """``POST /agents/runs`` 请求体。"""

    agent: str = Field(..., min_length=1, description="执行器名（见 executor_names()）")
    task: str = Field(..., min_length=1, description="任务描述/输入")
    options: Dict[str, Any] = Field(default_factory=dict, description="透传给执行器的选项")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, description="单次运行超时（秒）")
    max_steps: int = Field(default=0, ge=0, description="步数预算（0=不限额）")


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _success(request: Request, data: Any) -> Dict[str, Any]:
    """成功响应（与上移前 retrieval ``success()`` 信封逐字段一致）。"""
    return {"request_id": _rid(request), "data": data}


def _error(
    request: Request,
    *,
    status_code: int,
    error_code: str,
    message: str,
    user_hint: str = "",
) -> JSONResponse:
    """错误响应（顶层 ``error_code``，与上移前 AppError 信封一致）。"""
    body: Dict[str, Any] = {
        "request_id": _rid(request),
        "error_code": error_code,
        "message": message,
    }
    if user_hint:
        body["user_hint"] = user_hint
    return JSONResponse(status_code=status_code, content=body)


async def _execute_run(
    run_id: str,
    agent_name: str,
    task: str,
    options: Dict[str, Any],
    timeout_s: float,
) -> None:
    """后台执行协程：running → 终态。终态迁移幂等由 ControlPlane 保证。"""
    plane = get_control_plane()
    plane.mark_running(run_id)
    try:
        executor = get_executor(agent_name)
        coro = executor(task, dict(options))
        if timeout_s and timeout_s > 0:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        else:
            result = await coro
        plane.finish(run_id, state=RunState.SUCCEEDED)
        run = plane.status(run_id)
        if run is not None:
            run.meta["result"] = result
            # T21：终态在 finish 时已入队，但 result 是在其后写进 meta 的——
            # 补投一次，库行与响应面看到的 meta 才一致。flag-off 是零副作用 no-op。
            plane.persist_run(run_id)
    except asyncio.TimeoutError:
        plane.finish(run_id, state=RunState.FAILED, error=f"timeout after {timeout_s}s")
    except asyncio.CancelledError:
        # cancel() 已置 CANCELLED 终态；finish 幂等也不会覆盖。
        # 这里**重新抛出**以正确结束被取消的任务（asyncio 契约）。
        raise
    except Exception as exc:  # noqa: BLE001 - 执行器异常统一落 FAILED
        plane.finish(run_id, state=RunState.FAILED, error=str(exc))
        logger.warning("agent run failed: run=%s agent=%s error=%s", run_id, agent_name, exc)


@router.post("/agents/runs")
async def api_create_agent_run(request: Request, body: AgentRunCreate):
    """派生一次 Agent 运行（任务级入口，Chatbot 会话语义之外）。

    可选 ``Idempotency-Key`` header（T20，契约见
    ``tests/test_p143_run_idempotency.py``）：**无 header 时行为与现状
    逐位等价**——不带键的执行序列与本改动之前逐语句一致，不加隐式键、
    不改响应体形状。同键重放（先后或并发）返 200 复用原 run_id，
    不二次执行副作用；键只在运行真正登记成功后占用（登记前失败不毒化键）。
    """
    timeout_s = min(float(body.timeout_s), MAX_TIMEOUT_S)
    try:
        get_executor(body.agent)
    except KeyError:
        return _error(
            request,
            status_code=400,
            error_code="UNKNOWN_AGENT",
            message=f"unknown agent executor: {body.agent}",
            user_hint=f"可用执行器：{', '.join(executor_names())}",
        )

    # 键校验放在登记之前：被拒请求不占键、不创建运行（UNKNOWN_AGENT 同理，
    # 两者都发生在 spawn 前 ⇒ 同键的后续合法重试不会被一次失败请求毒化）。
    idem_key, key_error = _extract_idempotency_key(request)
    if key_error is not None:
        return _error(
            request,
            status_code=400,
            error_code="INVALID_IDEMPOTENCY_KEY",
            message=key_error,
            user_hint=f"键长限 1–{IDEMPOTENCY_MAX_KEY_CHARS} 字符",
        )

    # 准入三态（flag 关直通）；拒绝抛 AdmissionDenied →
    # 全局异常处理器映射 429（fail-closed，不登记不执行）。
    from agentix.performance.admission import admission_wait

    await admission_wait(kind="agent_run")

    def _spawn_and_bind():
        """基线创建序列原样封装（同步、零 await）：键路径与无键路径共用，
        保证无 header 行为逐位等价；键路径下它运行在 claim 的原子临界区内，
        「检查键 + 登记 run_id」因此是一步而不是两步（并发判据）。"""
        plane = get_control_plane()
        budget = (
            TokenBudget(limit=int(body.max_steps), name="agent_run_steps")
            if body.max_steps > 0
            else None
        )
        run_meta: Dict[str, Any] = {
            "task_preview": body.task[:200],
            "timeout_s": timeout_s,
        }
        # T21：``meta.thread_id`` 是对外**响应体变更**，只在落库面装配时出现——flag-off 的
        # 响应体与基线逐字节相同（约定 1）。取值 = ``options.session_id``（``chat``
        # 执行器的会话语义，也是图形的 checkpoint thread）；没有就**不写该键**，
        # 不拿空串占位（历史 run 回落时「没有线程」与「线程是空串」不该混为一谈）。
        # 「落库面是否装配」就是 flag 在本模块内的唯一判据（单一来源：
        # ``control_plane.resolve_run_state_store``），这里不再读一次 settings。
        if plane.run_store is not None:
            thread_id = str(body.options.get("session_id") or "").strip()
            if thread_id:
                run_meta["thread_id"] = thread_id
        run = plane.spawn(
            body.agent,
            budget=budget,
            trace_id=getattr(request.state, "request_id", "-"),
            meta=run_meta,
        )
        worker = asyncio.create_task(
            _execute_run(run.run_id, body.agent, body.task, body.options, timeout_s)
        )
        plane.bind(run.run_id, worker)
        return run

    def _make_run_pair() -> Tuple[str, str]:
        run = _spawn_and_bind()
        return run.run_id, body.agent

    if idem_key is None:
        # 无 header：与基线逐语句同路径（不落任何隐式键）
        run = _spawn_and_bind()
        return _success(
            request,
            {"run_id": run.run_id, "agent": body.agent, "status": run.state.value},
        )

    store = get_run_idempotency_store()
    claim = store.claim(idem_key, _make_run_pair)
    # 命中不静默（契约 §5）：只记指纹，键原文不进日志/指标
    if claim.replayed:
        logger.info(
            "run idempotency replay hit: key_fp=%s run=%s agent=%s",
            _key_fingerprint(idem_key),
            claim.run_id,
            claim.agent,
        )
    else:
        logger.info(
            "run created with idempotency key: key_fp=%s run=%s",
            _key_fingerprint(idem_key),
            claim.run_id,
        )
    # status 取运行当前状态；已被有界 FIFO 淘汰时回落登记时快照（pending）——
    # 与契约 §3.4 一致：复用响应不新增字段，形状与首建相同。
    current = get_control_plane().status(claim.run_id)
    status = current.state.value if current is not None else RunState.PENDING.value
    return _success(
        request,
        {"run_id": claim.run_id, "agent": claim.agent, "status": status},
    )


def _record_to_dict(record: Dict[str, Any]) -> Dict[str, Any]:
    """库记录 → 与 ``AgentRun.to_dict()`` **同一响应形状**（T21 回落面）。

    形状必须一致：调用方不该从「内存命中」和「库回落」看出两套字段。代价是
    ``budget`` 恒 ``null``、``duration_ms`` 由库里的起止时刻重算（库不落预算对象，
    见 ``agent_run_state`` 模型注释）。词表外的状态（坏数据）抛 ``ValueError``，
    由调用方降级。
    """
    return AgentRun.from_record(record).to_dict()


@router.get("/agents/runs")
async def api_list_agent_runs(request: Request, state: Optional[str] = None):
    """列出运行实例（可按状态过滤）+ 控制面统计。

    T21：装配落库面时**并入**库中内存没有的运行（按 ``run_id`` 去重、**内存优先**），
    否则列表在重启后只剩本次进程的 run，与单查回落各说一套（验收②：列表同口径）。
    库读失败 fail-open：warning 后只回内存面（查询降级不放大故障）。
    """
    plane = get_control_plane()
    state_filter: Optional[RunState] = None
    if state:
        try:
            state_filter = RunState(state)
        except ValueError:
            return _error(
                request,
                status_code=400,
                error_code="VALIDATION_ERROR",
                message=f"invalid state filter: {state}",
                user_hint="state 取值：pending/running/succeeded/failed/cancelled",
            )
    runs = plane.list_runs(state=state_filter)
    items = [r.to_dict() for r in runs]
    store = plane.run_store
    if store is not None:
        try:
            # 同步 repo 经 to_thread 下放，不卡事件循环（约定 6 ②）
            records = await asyncio.to_thread(
                store.list_runs,
                state=state_filter.value if state_filter is not None else None,
                limit=PERSIST_LIST_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 - 降级去向：只回内存面，warning 留痕
            logger.warning(
                "agent run state store list failed (falling back to in-memory list): %s", exc
            )
            records = []
        known = {item["run_id"] for item in items}
        for record in records:
            rid = str((record or {}).get("run_id") or "")
            if not rid or rid in known:
                continue
            try:
                items.append(_record_to_dict(record))
            except Exception as exc:  # noqa: BLE001 - 降级去向：跳过坏行，其余行照常返回
                logger.warning("skip unreadable persisted agent run %s: %s", rid, exc)
                continue
            known.add(rid)
    return _success(
        request,
        {
            "runs": items,
            "stats": plane.stats(),
        },
    )


@router.get("/agents/runs/{run_id}")
async def api_get_agent_run(request: Request, run_id: str):
    """查询运行状态（含预算归属与结果 meta）。

    T21：内存 miss 且装配落库面时**回落库**（重启后仍可查历史终态）；库读失败
    fail-open —— warning 后按基线返 404（读不到 ≠ 不存在，但也不能凭空造一个）。
    """
    plane = get_control_plane()
    run = plane.status(run_id)
    if run is not None:
        return _success(request, run.to_dict())
    store = plane.run_store
    if store is not None:
        try:
            record = await asyncio.to_thread(store.load_run, run_id)
        except Exception as exc:  # noqa: BLE001 - 降级去向：回内存面（miss ⇒ 404），warning 留痕
            logger.warning(
                "agent run state store read failed for %s (falling back to memory): %s",
                run_id,
                exc,
            )
            record = None
        if record:
            try:
                return _success(request, _record_to_dict(record))
            except Exception as exc:  # noqa: BLE001 - 降级去向：坏行按未找到处理，warning 留痕
                logger.warning("unreadable persisted agent run %s: %s", run_id, exc)
    return _error(
        request,
        status_code=404,
        error_code="AGENT_RUN_NOT_FOUND",
        message=f"agent run {run_id} not found",
        user_hint="运行不存在或已被淘汰（终态记录按 FIFO 有界保留）",
    )


@router.post("/agents/runs/{run_id}/cancel")
async def api_cancel_agent_run(request: Request, run_id: str):
    """取消运行：置终态并真正打断底层任务（幂等）。"""
    plane = get_control_plane()
    run = plane.status(run_id)
    if run is None:
        return _error(
            request,
            status_code=404,
            error_code="AGENT_RUN_NOT_FOUND",
            message=f"agent run {run_id} not found",
            user_hint="运行不存在或已被淘汰（终态记录按 FIFO 有界保留）",
        )
    await plane.cancel(run_id, reason="user_cancelled")
    refreshed = plane.status(run_id)
    return _success(request, (refreshed or run).to_dict())
