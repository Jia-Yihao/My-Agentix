"""LangGraph Checkpointer 装配（P0-1）。

采用 LangGraph 官方子包 langgraph-checkpoint-redis 的 AsyncRedisSaver，
替代 lg_builder.py 模块级的 MemorySaver，使会话状态跨进程、跨重启持久化。

关键约束（两条都是 2026-10-23 对真 Redis Stack 实测出来的，不是推测）：
  1. Redis 必须带 RediSearch 模块（否则 `asetup()` 报 unknown command 'FT.*'）——
     docker-compose.yml 已换用 redis/redis-stack-server 镜像；用官方 `redis:7-alpine`
     会走到下面的降级分支。
  2. REDIS_CHECKPOINT_URL 必须指向 **db0**。RediSearch 只能在 db0 建索引，实测 db2 时
     `asetup()` 抛 `RedisSearchError: Failed to create index 'checkpoints' on Redis:
     Cannot create index on db != 0`，checkpointer 静默退化成 MemorySaver —— 会话状态
     从此不持久，而启动仍然成功，只有一条 ERROR 日志。R3.2 原先把 checkpoint 分到 db2
     正是踩了这条，已改回 db0（键前缀 `checkpoint:` 与 cache 的 `semantic:`/`history:`
     不重叠，隔离靠命名空间而非分库）。
     T7（2026-10-27）补了运行时校验：建连后从**生效 client 的池参数**读 db，非 0 即打
     `checkpointer db0 不变式违例` ERROR 告警——只告警不设卡，不改任何降级/fail-closed
     语义（守卫用例 tests/test_p139_redis_db_invariant.py）。
  3. asetup() 是协程，必须在应用启动阶段 await 一次；它是幂等的，可重复调用。
     降级后的单例仍是粘的：一旦 _saver 变成 MemorySaver，`init_checkpointer()` 自己
     不会再试 Redis（它第一行就返回既有单例）。T22 把这个「粘死」交给独立的
     `retry_upgrade_checkpointer()` 处理——**同进程内的升级重试是它的事**，不是
     init 的事；要不要重试只看模块级 `_upgrade_retryable`（见其 docstring）。

降级设计依据方案 [第5章]（恢复优先级：重试 → 回滚 → 替代路径 → 熔断 → 人工升级）：
Redis 不可用属于"替代路径"场景——
  - langgraph-checkpoint-redis 未安装 → 降级 MemorySaver
  - settings.CHECKPOINTER_TYPE == "memory" → 显式使用 MemorySaver
  - Redis 连接/asetup 失败 → 降级 MemorySaver
保证核心问答仍可用，而非整体启动失败。
例外开关：settings.CHECKPOINTER_FAIL_CLOSED（默认 false = 维持上述降级基线）；
为 true 时上面两条 **Redis 路径**失败分支改为抛 RuntimeError 拒绝启动，且单例
不会被粘成 MemorySaver——显式 memory 分支不受影响（显式配置不是故障）。

T22（可重试升级）：三条降级原因里**只有「Redis 连接/asetup 失败」是可重试的**——
显式 `CHECKPOINTER_TYPE=memory` 是人配的不是故障、包未安装运行期不会变，这两条重试
一万次也换不来成功。这个判定只有一个源：模块级 `_upgrade_retryable`，只在 Redis
失败分支置真，调用方（`agentix/main.py` 的重试循环）只读它，不按配置/字符串重判。
"""

from typing import Callable, Optional, Union

from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from agentix.config import settings

_saver: Optional[Union[object, MemorySaver]] = None
_degraded_reason: Optional[str] = None

#: T22：「这次降级能不能靠重试换回 Redis」——**唯一判定源**（见模块 docstring 末段）。
#: 只在「Redis 连接/asetup 失败」那条降级分支置真；显式 memory、包未安装、fail-closed
#: 都不置真。升级成功 / 重试次数用尽后复位为假，避免重试循环拿旧标记空转。
_upgrade_retryable: bool = False

#: T22 指标 `checkpointer_upgrade_total{result}` 的封闭取值——进指标前先按它闭合，
#: 否则一个拼错的结果名会静默开出一个新时间序列（有界性靠这里守）。
_UPGRADE_RESULT_VALUES = frozenset({"ok", "fail"})

# db0 不变式的约定值（T7）：RediSearch 只能在 db0 建索引——这是 2026-10-23 实测出的
# 平台硬约束（见模块 docstring 约束 2），不随 REDIS_CHECKPOINT_URL 的默认值漂移。
# 刻意取字面量而非"字段默认值解析结果"：默认值本身也要受这条不变式审判，
# 拿被审对象当基准等于没有基准（tests/test_r32_redis_sharding.py 的形状守卫同理，
# 它守静态默认，这里守运行时生效值）。
_REQUIRED_CHECKPOINT_DB = 0


def _db_in_effect(redis_client) -> Optional[int]:
    """取本次连接**实际生效**的 db 号——判据直接读建连 client 的连接池参数。

    诊断必须与执行链同源（AGENTS.md 硬性约定 6 ④）：若另解析一遍
    settings.REDIS_CHECKPOINT_URL，就立了第二个判点——from_url 的缺省语义
    （URL 无路径段 ⇒ 生效 db 0）与手写解析可能漂移，改一处漏一处。
    返回 None 表示"无法判定"（测试桩 / 异形 client）：跳过告警，
    诊断绝不能反过来打断执行链（约定 2）。
    """
    try:
        pool_kwargs = redis_client.connection_pool.connection_kwargs
    except AttributeError:
        return None
    db = pool_kwargs.get("db")
    if db is None:
        # redis-py 在 URL 不带路径时不放 db 键，Connection 缺省连 db0——与执行链行为一致
        return 0
    try:
        return int(db)
    except (TypeError, ValueError):
        return None


def _check_checkpoint_db_invariant(redis_client) -> None:
    """T7 运行时校验：生效 db 非约定值 ⇒ ERROR 日志告警；**只告警不设卡**（约定 2 / 验收②）。

    存在的理由：db0 不变式此前只有注释与静态形状守卫——容器 env 注入 db2 时，
    静态守卫照常绿，运行期只剩 asetup 失败后的静默降级，无人可见（规划文档 T7 卡）。
    本函数不抛异常、不改降级/fail-closed 语义；任何取不到判据的情形都静默跳过。
    """
    db = _db_in_effect(redis_client)
    if db is None or db == _REQUIRED_CHECKPOINT_DB:
        return
    logger.error(
        "checkpointer db0 不变式违例：本次连接实际生效 db 号为 {}，约定为 {}"
        "（RediSearch 只能在 db0 建索引）。asetup() 大概率随之失败并降级 MemorySaver，"
        "会话状态不再持久化——请检查容器 env / .env 对 REDIS_CHECKPOINT_URL 的注入。"
        "本告警只留痕、不阻断执行（诊断口径）",
        db,
        _REQUIRED_CHECKPOINT_DB,
    )


#: G2（台账 §8.2 表 D）：关闭被丢弃 client 的唯一动作点——「谁丢弃谁关闭」的关闭
#: 判据只写这一枚，两个丢弃点（`_connect_redis_saver` 内部失败、
#: `retry_upgrade_checkpointer` 的 rewire 失败臂）共用（约定 6 ④：同一个判定
#: 不许两个源，两处各写一份必然漂移）。
async def _close_redis_client_quietly(client) -> None:
    """尽力关闭一只被丢弃的 Redis client（含其新建的连接池）；失败只留痕，不抛。

    fail-open（约定 2）：关闭自身失败只 `logger.warning`，绝不把异常传出——泄漏
    一个池是可观测的资源问题，而把关闭失败升格成异常会篡改调用方的原失败语义
    （降级原因 / 重试判定 / `return False` 都读原异常，关闭动作无论排在哪个位置
    都不许改写它们）。

    鸭子判据 `getattr(client, "aclose", None)` 而非「直接 await 靠宽 except 兜
    AttributeError」：判据含**测试桩形态**——仓里多处 `_FakeRedis` 桩只有
    `from_url`（test_p132 / test_p139 / test_checkpointer_p0），若走 except 兜底，
    桩缺能力会被伪装成「关闭失败」刷 warning，把「桩没有 aclose」误报成
    「池没关掉」，与真正的关闭故障混在同一句话里。真客户端恒有 `aclose`
    （redis-py 5.3.1 `redis/asyncio/client.py:588`），跳过只可能发生在桩上。

    `close_connection_pool=True` 显式传参，不依赖 `auto_close_connection_pool`
    的推导（redis-py 5.3.1 `client.py:603-606`：`close_connection_pool` 为真直接
    短路推导分支并 `connection_pool.disconnect()`）——本卡要关的正是这次尝试
    新建的池，语义必须由调用侧钉死而不是赌构造路径推导出的属性值。
    """
    close = getattr(client, "aclose", None)
    if close is None:
        return
    try:
        await close(close_connection_pool=True)
    except Exception as exc:  # noqa: BLE001 - 关闭失败降级为一条 warning，不改调用方语义（约定 2）
        logger.warning(
            "checkpointer: closing discarded redis client failed (pool may leak): {}",
            exc,
        )


def _try_import_async_redis_saver():
    """兼容不同版本的导入路径（aio 子模块 / 包根）。"""
    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver  # noqa: PLC0415

        return AsyncRedisSaver
    except ImportError:
        pass
    try:
        from langgraph.checkpoint.redis import AsyncRedisSaver  # noqa: PLC0415

        return AsyncRedisSaver
    except ImportError:
        return None


async def _connect_redis_saver(AsyncRedisSaver):
    """建连 + db0 不变式校验 + asetup，返回 `(saver, redis_client)`；失败**原样抛出**。

    抽成函数而不是在「启动初始化」与「运行期升级重试」各写一遍：建连参数、不变式
    校验点、asetup 的调用顺序是同一段口径，写两份必然漂移（约定 6 ④）。

    异常在这里刻意不翻译——「降级 / 拒绝启动 / 本轮重试失败」是三种不同处置，
    判定留在两个调用点（它们各自知道自己在哪条语义里）。

    G2（台账 §8.2 表 D）：`from_url` 建池之后的每一步（不变式校验、构造 saver、
    asetup）都纳入同一个 try——任一步抛即本次 client 被本函数丢弃，先尽力关闭
    再原样抛出（「失败原样抛出」的既有承诺不变，只补资源收口）。返回带上
    `redis_client` 而不从 saver 反查内部属性：属性名未经证实，不凭记忆发明；
    调用点由此**显式**知道成功路径移交给自己的是什么（rewire 失败臂才有关闭对象）。
    """
    from redis.asyncio import Redis  # noqa: PLC0415

    # R3.2：checkpoint 落独立分片 db2（RediSearch 索引与 cache db0 隔离）
    redis_client = Redis.from_url(settings.REDIS_CHECKPOINT_URL, decode_responses=False)
    try:
        # T7：db0 不变式运行时校验。判据取自上面这个 client 的池参数（与执行链同源，
        # 约定 6 ④），位置必须在建连之后、AsyncRedisSaver 构造之前——失败发生在
        # asetup（更晚）才暴露，等读到降级日志再追注入源就晚了。只告警不阻断（约定 2）。
        _check_checkpoint_db_invariant(redis_client)
        saver = AsyncRedisSaver(
            redis_client=redis_client,
            ttl={"default_ttl": 60 * 24 * 7, "refresh_on_read": True},  # 分钟 → 7 天
        )
        await saver.asetup()  # 创建 RediSearch 索引，幂等
    except Exception:  # noqa: BLE001 - 收口本次丢弃的池后原样抛出，不翻译异常类型（G2 / 约定 2）
        # 建池即负责：from_url 本身抛时还没有 client，不进这里；此处关闭的
        # 只会是本函数自己刚建的那一只。只关这一次，调用点不再重复关闭。
        await _close_redis_client_quietly(redis_client)
        raise
    return saver, redis_client


async def init_checkpointer():
    """初始化并返回 checkpointer 单例（AsyncRedisSaver 或降级的 MemorySaver）。

    必须在 FastAPI 的 startup/lifespan 中调用一次（因为 asetup() 是协程）。
    默认任何失败都降级为 MemorySaver 并记录原因；`CHECKPOINTER_FAIL_CLOSED=true`
    时 Redis 路径失败改为拒绝启动（抛 RuntimeError，且单例不会被粘成 MemorySaver）。
    """
    global _saver, _degraded_reason, _upgrade_retryable

    if _saver is not None:
        return _saver

    # 进判定前先复位：下面的分支只有「Redis 失败」会把它置真。放在这里而不是每条
    # 分支各写一次，是为了让 fail-closed 抛出的那两条路径也**确定**不留上一次的真值。
    _upgrade_retryable = False

    if settings.CHECKPOINTER_TYPE == "memory":
        _saver = MemorySaver()
        _degraded_reason = "CHECKPOINTER_TYPE=memory (explicit)"
        logger.warning("checkpointer: using MemorySaver (explicit config)")
        _record_degraded("checkpointer")
        return _saver

    AsyncRedisSaver = _try_import_async_redis_saver()
    if AsyncRedisSaver is None:
        reason = "langgraph-checkpoint-redis not installed"
        if settings.CHECKPOINTER_FAIL_CLOSED:
            # fail-closed：拒绝启动，且不给 _saver 赋值——否则单例粘成 MemorySaver（见模块 docstring 第 3 条）
            raise RuntimeError(f"checkpointer fail-closed: {reason}")
        _saver = MemorySaver()
        _degraded_reason = reason
        logger.warning(
            "checkpointer: langgraph-checkpoint-redis not installed, "
            "falling back to MemorySaver (session state will not persist)"
        )
        _record_degraded("checkpointer")
        return _saver

    try:
        # G2：成功路径所有权移交 saver（构造时已把这只 client 交出去），本侧不关；
        # 失败路径中 `_connect_redis_saver` 在抛出点之前已把 client 关闭恰好一次，
        # 解包赋值不会执行，降级臂（下面 except）没有可再关的对象。
        saver, _redis_client = await _connect_redis_saver(AsyncRedisSaver)
        _saver = saver
        _degraded_reason = None
        logger.info("Redis checkpointer initialized (langgraph-checkpoint-redis)")
        return _saver
    except Exception as exc:  # noqa: BLE001 - 降级或 fail-closed 抛出，见模块 docstring
        reason = f"{type(exc).__name__}: {exc}"
        if settings.CHECKPOINTER_FAIL_CLOSED:
            # fail-closed：拒绝启动，不给 _saver 赋值（粘死见模块 docstring 第 3 条）
            raise RuntimeError(f"checkpointer fail-closed: {reason}") from exc
        _saver = MemorySaver()
        _degraded_reason = reason
        # T22：三条降级原因里**只有这一条**可重试（Redis 可能只是还没起来 / 索引还没建完）。
        _upgrade_retryable = True
        logger.error(
            "checkpointer init failed, fallback to MemorySaver: {} — "
            "core Q&A remains available; session state will not persist",
            _degraded_reason,
        )
        _record_degraded("checkpointer")
        return _saver


def _record_degraded(component: str) -> None:
    """企业化 P1-2.1：降级事件上报 Prometheus（失败不影响主链路）。"""
    try:
        from agentix.observability.metrics import record_degraded

        record_degraded(component)
    except Exception:  # noqa: BLE001
        pass


def _record_upgrade(result: str) -> None:
    """T22：升级重试结果上报 `checkpointer_upgrade_total{result}`（失败不影响升级本体）。

    `result` 进指标前按 `_UPGRADE_RESULT_VALUES` 闭合：本模块的写点都传字面量（建连失败 /
    装配失败 → `"fail"`，提交成功 → `"ok"`），因此闭合分支不可达，是为了将来新增写点时不
    静默开出第三条时间序列（有界性由指标层守，不靠调用点自觉）。
    """
    try:
        from agentix.observability.metrics import record_checkpointer_upgrade

        record_checkpointer_upgrade(result if result in _UPGRADE_RESULT_VALUES else "fail")
    except Exception:  # noqa: BLE001 - 指标故障不影响升级与主链路（约定 2）
        pass


def is_upgrade_retryable() -> bool:
    """T22：当前降级态**值不值得**重试（唯一判定源，见模块 docstring 末段）。

    重试循环只读这个函数，不在 `agentix/main.py` 里按配置键或降级原因字符串再判一遍
    （约定 6 ④：同一个判定不许两个源）。
    """
    return _upgrade_retryable


def disarm_upgrade_retry() -> None:
    """T22：复位「可重试」标记（重试次数用尽时由重试循环调用）。

    用尽后必须复位：标记留真会让任何后续调用再判一次「还能升级」并重复打一条
    「次数用尽」warning（日志刷屏），而事实上这一进程已经不会再有下一次尝试。
    """
    global _upgrade_retryable
    _upgrade_retryable = False


async def retry_upgrade_checkpointer(rewire: Optional[Callable[[object], None]] = None) -> bool:
    """T22：尝试把降级中的 MemorySaver **升级**成 AsyncRedisSaver（一次尝试）。

    返回是否真的升级成功。三件前置事实缺一即返回 ``False``：
    ① `_upgrade_retryable` 为假（显式 memory / 包未安装 / 已升级 / fail-closed）；
    ② 单例为空（fail-closed 抛过，没有降级态可升级，循环对它空转毫无意义）；
    ③ 运行期包仍取不到（运行期不会凭空装上，顺势把标记复位）。

    ``rewire``：把新 saver 换进**正在服务的图**的装配回调（生产唯一调用点
    `agentix/main.py` 传 `_wire_graph_checkpointer`）。默认 ``None`` = 不装配，
    只给不关心图的用例用。**它是「升级成功」的判定条件之一**：装配在提交之前
    调用，抛异常即本轮失败——反过来（先换 `_saver` 再装配）在装配失败时会留下
    「`get_degraded_reason()` 返回 None 自称已恢复、服务图仍挂旧 saver」的中间态，
    `/ops/active-threads` 的线程 id 取自新 saver 而「未完成」判据取自旧图，
    静默返回空列表，指标却已记 ok。顺序因此固定为：建连 + `asetup()` ⇒ 装配
    ⇒ 提交（saver / 原因 / 标记 / 指标 / INFO）。

    节奏由调用方给（定间隔，不做指数退避——重试目标是同进程的一个 Redis 连接，
    退避不换成功率，只多一套要记的口径）。

    fail-open：任何异常就地收口成 ``False`` 并刷新 `_degraded_reason`，让下一轮与
    「次数用尽」告警都能读到**最后一次**失败原因；绝不把升级失败抛进主链路（约定 2）。
    失败时**不提交新 saver**（`_saver` 保持原值），`_upgrade_retryable` 保持真，
    下一轮还会再试。
    """
    global _saver, _degraded_reason, _upgrade_retryable

    if not _upgrade_retryable:
        return False
    if _saver is None:
        _upgrade_retryable = False
        return False
    AsyncRedisSaver = _try_import_async_redis_saver()
    if AsyncRedisSaver is None:
        _upgrade_retryable = False
        logger.warning(
            "checkpointer upgrade retry dropped: langgraph-checkpoint-redis is not "
            "importable (that cannot change within one process)"
        )
        return False
    try:
        saver, redis_client = await _connect_redis_saver(AsyncRedisSaver)
    except Exception as exc:  # noqa: BLE001 - 本轮失败要能被下一轮再试，绝不打断主链路
        # G2：该臂的 client 已在 `_connect_redis_saver` 内部关闭（只关一次），
        # 解包未执行，这里没有可再关的对象。
        _degraded_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "checkpointer upgrade attempt failed, still on MemorySaver: {}",
            _degraded_reason,
        )
        _record_upgrade("fail")
        return False

    # T22（第 2 轮返工）：装配成功才提交。装配失败按「本轮尝试失败」处置——两条事实
    # 都写进 `_degraded_reason`（Redis 侧其实已就绪 + 装配失败的原因），`result="fail"`，
    # 标记保持真让下一轮再来；不在这里另写 `_saver`（提交点只有下面这一处，
    # 判定与提交同源，约定 6 ④）。
    if rewire is not None:
        try:
            rewire(saver)
        except Exception as exc:  # noqa: BLE001 - 本轮失败要能被下一轮再试，绝不打断主链路
            # G2（台账 §8.2 表 D）：此臂里 saver 已建连并 asetup 成功、整只因装配
            # 失败被丢弃 ⇒ 用解包出来的 redis_client 尽力关闭（saver 未被提交、
            # 没有任何人继续持有这只 client）。关闭排最前且自身不抛（helper 契约），
            # 不改变下面 `_degraded_reason` / 日志 / 指标 / `return False` 的取值与时机。
            await _close_redis_client_quietly(redis_client)
            _degraded_reason = (
                "redis saver ready (connect + asetup ok) but graph rewire failed: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning(
                "checkpointer upgrade attempt failed at graph rewire, still on MemorySaver: {}",
                _degraded_reason,
            )
            _record_upgrade("fail")
            return False

    _saver = saver
    _degraded_reason = None
    # 升级成功即终止重试：循环条件读的就是这个标记，不需要另立一个「已成功」布尔。
    _upgrade_retryable = False
    _record_upgrade("ok")
    logger.info("checkpointer upgraded to Redis (session state is persistent again)")
    return True


def get_checkpointer():
    """取已初始化的 checkpointer；未初始化返回 None（表示不持久化）。"""
    return _saver


def get_degraded_reason() -> Optional[str]:
    """返回降级原因；None 表示 Redis checkpointer 正常生效。"""
    return _degraded_reason


def reset_checkpointer() -> None:
    """重置单例（仅供测试使用）。"""
    global _saver, _degraded_reason, _upgrade_retryable
    _saver = None
    _degraded_reason = None
    _upgrade_retryable = False
