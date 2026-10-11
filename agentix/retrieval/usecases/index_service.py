"""
索引任务编排

负责创建任务、触发后台执行、查询状态。
"""
from sqlmodel import Session

from agentix.retrieval.domain.models.index import IndexJob
from agentix.retrieval.infra.repos.index_repo import (
    get_index_job_by_id,
    get_latest_index_job_by_document,
    is_stale_index_job,
)
from agentix.retrieval.api.schemas.index_jobs import IndexJobData


def _to_job_data(job: IndexJob) -> dict:
    return IndexJobData(
        id=job.id,
        kb_id=job.kb_id,
        status=job.status,
        progress=job.progress,
        error_message=job.error_message,
        error_code=job.error_code,
        attempts=job.attempts,
        diff_summary=job.diff_summary,
        created_at=job.created_at,
        updated_at=job.updated_at,
    ).model_dump()


def _get_job_impl(session: Session, job_id: str) -> dict | None:
    job = get_index_job_by_id(session, job_id)
    if not job:
        return None
    return _to_job_data(job)


def _get_document_latest_job_impl(session: Session, document_id: str) -> dict | None:
    job = get_latest_index_job_by_document(session, document_id)
    if not job:
        return None
    return _to_job_data(job)


class IndexService:
    def __init__(self, session: Session):
        self.session = session

    def get_job(self, *, job_id: str) -> dict | None:
        return _get_job_impl(self.session, job_id)

    def get_document_latest_job(self, *, document_id: str) -> dict | None:
        return _get_document_latest_job_impl(self.session, document_id)

    def enqueue_index_job(self, *, job_id: str, background_tasks: object | None = None) -> None:
        """
        入队索引任务。

        - 若配置了 CELERY_BROKER_URL，则优先投递到 Celery（Web 进程只投递）。
        - 若传入 background_tasks（FastAPI BackgroundTasks），则使用 add_task 异步执行。
        - 否则直接同步执行（适用于脚本/测试/worker 调用）。
        """
        from agentix.retrieval.infra.tasks.index_worker import run_index_job
        from agentix.retrieval.settings import settings

        if (settings.CELERY_BROKER_URL or "").strip():
            from agentix.retrieval.infra.tasks.celery_tasks import index_run_index_job

            index_run_index_job.apply_async(args=[job_id], task_id=job_id)
            return

        if background_tasks is not None and hasattr(background_tasks, "add_task"):
            # duck-typing: avoid binding usecases to FastAPI types
            background_tasks.add_task(run_index_job, job_id)  # type: ignore[attr-defined]
            return
        run_index_job(job_id)

    def retry_job(self, *, job_id: str, background_tasks: object | None = None) -> dict | None:
        """重试索引任务（复用原 job，不新建）。

        允许重试的两种状态：
        - ``failed``：常规失败重试；
        - **已判定为僵尸的** ``running`` / ``queued``（长时间无心跳）：worker 中断时任务会永远
          停在 running。僵尸回收原先只在 ``ensure_kb_index_job``（上传/删除/审批）路径触发，
          用户侧没有任何入口能自救，因此这里放开。仍保持「正在正常执行」的任务不可重试。

        注意：重试不清理已写入的 chunk，幂等性由流水线自身保证（added/modified 都会先清旧 chunk）。
        """
        job = get_index_job_by_id(self.session, job_id)
        if not job:
            return None
        if job.status != "failed" and not is_stale_index_job(job):
            raise ValueError("only failed or stale index jobs can be retried")
        job.status = "queued"
        job.progress = 0
        job.error_message = None
        job.error_code = None
        job.finished_at = None
        self.session.add(job)
        self.session.commit()
        self.enqueue_index_job(job_id=job.id, background_tasks=background_tasks)
        return _to_job_data(job)
