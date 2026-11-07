from __future__ import annotations

import logging

from celery import Celery

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    return (url or "").strip()


#: AMQP 的 URL scheme。broker 是不是 RabbitMQ 只看这个前缀，不新增配置键——
#: 部署侧换 broker 属于换 URL，不是换架构（代码里没有第二份「用哪个 MQ」的真相）。
_AMQP_SCHEMES = ("amqp://", "amqps://", "pyamqp://")

#: 与 worker 的 `celery worker` 默认队列一致（`-Q` 未指定时消费的就是它）。
_DEFAULT_QUEUE = "celery"
#: 死信交换与队列名。控制台里能直接看到，排查失败索引任务的第一入口。
_DLX_EXCHANGE = "agentix.dlx"
_DLX_QUEUE = "agentix.dead"


def is_amqp_broker(url: str) -> bool:
    """broker 是否走 AMQP（RabbitMQ）。DLX 只在 AMQP 上成立。"""
    return (url or "").strip().lower().startswith(_AMQP_SCHEMES)


def resolve_visibility_timeout(configured: int, *, job_timeout_seconds: int) -> int:
    """visibility_timeout 必须明显大于单次任务超时预算。

    acks_late 下任务未 ack 超过 visibility_timeout 会被 broker **重新投递**，
    造成两个 worker 并发执行同一 job（并发写同一版本目录）。任务可以跑满自身预算，
    因此配置值小于「预算的 2 倍」时抬升到 2 倍并告警（与 stale 阈值下限同一思路：
    代码侧兜底，不信任部署环境的配置）。
    """
    floor = job_timeout_seconds * 2
    if configured < floor:
        logger.warning(
            "INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS=%s 低于建议下限 %s"
            "（INDEX_JOB_TIMEOUT_SECONDS=%s 的 2 倍）：任务跑满预算仍未 ack 时会被 broker"
            "重新投递，导致两个 worker 并发执行同一索引任务，实际生效值将抬升到 %s",
            configured,
            floor,
            job_timeout_seconds,
            floor,
        )
        return floor
    return configured


_BROKER_URL = _normalize_url(settings.CELERY_BROKER_URL)

celery_app = Celery(
    "agentix_retrieval",
    broker=_BROKER_URL,
    backend=_normalize_url(settings.CELERY_RESULT_BACKEND) or None,
)

# Celery 文档要求 broker_transport_options / result_backend_transport_options /
# 顶层 conf 三处 visibility_timeout 取值一致，这里统一取同一个生效值。
VISIBILITY_TIMEOUT_SECONDS = resolve_visibility_timeout(
    settings.INDEX_JOB_VISIBILITY_TIMEOUT_SECONDS,
    job_timeout_seconds=settings.INDEX_JOB_TIMEOUT_SECONDS,
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    result_backend_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    visibility_timeout=VISIBILITY_TIMEOUT_SECONDS,
)


def _declare_amqp_dlx_topology() -> None:
    """在 AMQP broker 上声明死信拓扑：主队列拒收或过期时消息进 ``agentix.dead``。

    只在 ``amqp://`` 下调用。两套 broker 的失败语义不同，不混用：

    - **AMQP（RabbitMQ）**：失败由 ack/nack 表达，死信靠队列参数
      ``x-dead-letter-exchange``；``x-max-length`` 未设，队列不做长度截断。
    - **Redis**：没有 exchange 概念，失败靠 ``visibility_timeout`` 到期重投
      （见 :func:`resolve_visibility_timeout`），死信拓扑在这里不成立。

    队列名与 worker 的默认消费队列一致（``celery``），因此切换 broker 不需要
    改 worker 启动命令。死信队列带 7 天 ``x-message-ttl``，避免无人消费时无限堆积。
    """
    from kombu import Exchange, Queue

    celery_app.conf.task_queues = (
        Queue(
            _DEFAULT_QUEUE,
            Exchange(_DEFAULT_QUEUE, type="direct", durable=True),
            routing_key=_DEFAULT_QUEUE,
            durable=True,
            queue_arguments={
                "x-dead-letter-exchange": _DLX_EXCHANGE,
                "x-dead-letter-routing-key": _DLX_QUEUE,
            },
        ),
        Queue(
            _DLX_QUEUE,
            Exchange(_DLX_EXCHANGE, type="direct", durable=True),
            routing_key=_DLX_QUEUE,
            durable=True,
            queue_arguments={"x-message-ttl": 7 * 24 * 3600 * 1000},
        ),
    )
    celery_app.conf.task_default_queue = _DEFAULT_QUEUE
    celery_app.conf.task_default_exchange = _DEFAULT_QUEUE
    celery_app.conf.task_default_routing_key = _DEFAULT_QUEUE


if is_amqp_broker(_BROKER_URL):
    _declare_amqp_dlx_topology()
    logger.info("broker=amqp，已声明死信拓扑 %s -> %s", _DEFAULT_QUEUE, _DLX_QUEUE)
else:
    logger.debug("broker=redis（或未配置），走 visibility_timeout 重投语义，不声明 DLX")

# 注册 @celery_app.task（import 副作用）
from agentix.retrieval.infra.tasks import celery_tasks  # noqa: E402,F401
