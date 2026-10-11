from __future__ import annotations

import logging

from celery import Celery

from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    return (url or "").strip()


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


celery_app = Celery(
    "agentix_retrieval",
    broker=_normalize_url(settings.CELERY_BROKER_URL),
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

# 注册 @celery_app.task（import 副作用）
from agentix.retrieval.infra.tasks import celery_tasks  # noqa: E402,F401
