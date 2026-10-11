from __future__ import annotations

from agentix.retrieval.infra.tasks.celery_app import celery_app
from agentix.retrieval.infra.tasks.index_worker import run_index_job


@celery_app.task(
    name="agentix_retrieval.index.run_index_job",
)
def index_run_index_job(job_id: str) -> None:
    run_index_job(job_id)

