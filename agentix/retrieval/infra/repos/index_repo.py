"""
索引相关 Repo（infra 实现）
"""

import logging
import threading
import uuid
from datetime import datetime

from sqlmodel import Session, select  # type: ignore

from agentix.retrieval.domain.datetime_utils import utc_now
from agentix.retrieval.domain.models.document import Chunk, Document, FileObject, KbFile
from agentix.retrieval.domain.models.index import IndexArtifact, IndexArtifactManifest, IndexJob
from agentix.retrieval.domain.services.manifest_diff import compute_diff
from agentix.retrieval.settings import settings

_registry_lock = threading.Lock()
_ensure_locks: dict[str, threading.Lock] = {}

logger = logging.getLogger(__name__)


def resolve_stale_running_seconds(configured: int, *, job_timeout_seconds: int) -> int:
    """running 僵尸判定阈值：强制不低于单次任务超时预算。

    任务在自身超时预算内都可能正在正常执行（如步骤 2~4 的长文档解析期间不写心跳），
    若阈值小于该预算，正常任务会被误判为僵尸 → 标 failed 并另起新任务，导致同一
    KB 的同一版本目录被两个 worker 并发写入。配置过小属于误配置，此处抬升并告警。
    """
    if configured < job_timeout_seconds:
        logger.warning(
            "INDEX_JOB_STALE_RUNNING_SECONDS=%s 小于 INDEX_JOB_TIMEOUT_SECONDS=%s，"
            "已抬升为 %s 以避免误杀正常索引任务",
            configured,
            job_timeout_seconds,
            job_timeout_seconds,
        )
        return job_timeout_seconds
    return configured


# queued 长时间 progress=0：视为未入队/未消费的僵尸任务
STALE_QUEUED_SECONDS = settings.INDEX_JOB_STALE_QUEUED_SECONDS
# running 长时间无心跳（updated_at 不变）：视为 worker 中断
STALE_RUNNING_SECONDS = resolve_stale_running_seconds(
    settings.INDEX_JOB_STALE_RUNNING_SECONDS,
    job_timeout_seconds=settings.INDEX_JOB_TIMEOUT_SECONDS,
)


def _per_kb_ensure_lock(kb_id: str) -> threading.Lock:
    """同一进程内，同一 kb 的 ensure 串行化，避免文件夹多文件并发上传创建多条 queued job。"""
    with _registry_lock:
        if kb_id not in _ensure_locks:
            _ensure_locks[kb_id] = threading.Lock()
        return _ensure_locks[kb_id]


def _job_last_activity(job: IndexJob) -> datetime:
    return job.updated_at or job.created_at


def is_stale_index_job(job: IndexJob, *, now: datetime | None = None) -> bool:
    """判断 KB 索引任务是否已超时未推进（僵尸 queued / 卡死 running）。"""
    now = now or utc_now()
    last = _job_last_activity(job)
    age = (now - last).total_seconds()
    if job.status == "queued":
        return job.progress <= 0 and age >= STALE_QUEUED_SECONDS
    if job.status == "running":
        return age >= STALE_RUNNING_SECONDS
    return False


def mark_index_job_stale_failed(session: Session, job: IndexJob) -> None:
    """将僵尸/超时任务标为 failed，便于 ensure 新建并入队。"""
    prev = job.status
    job.status = "failed"
    job.error_message = f"stale: {prev} exceeded timeout (queued={STALE_QUEUED_SECONDS}s, running={STALE_RUNNING_SECONDS}s)"
    job.finished_at = utc_now()
    job.updated_at = utc_now()
    session.add(job)
    session.flush()


def get_index_job_by_id(session: Session, job_id: str) -> IndexJob | None:
    return session.get(IndexJob, job_id)


def get_index_job_by_idempotency_key(session: Session, key: str) -> IndexJob | None:
    return session.exec(select(IndexJob).where(IndexJob.idempotency_key == key)).first()


def deactivate_active_artifacts(session: Session, kb_id: str) -> None:
    """将该 KB 下所有 active 索引产物置为非 active（如整库无 chunk 时清空检索面）。"""
    for art in session.exec(
        select(IndexArtifact).where(
            IndexArtifact.kb_id == kb_id,
            IndexArtifact.is_active == True,  # noqa: E712
        )
    ).all():
        art.is_active = False
        session.add(art)


def get_next_artifact_version(session: Session, kb_id: str) -> int:
    latest = session.exec(
        select(IndexArtifact)
        .where(IndexArtifact.kb_id == kb_id)
        .order_by(IndexArtifact.version.desc())
        .limit(1)
    ).first()
    return (latest.version + 1) if latest else 1


def record_kb_diff_on_job(
    session: Session,
    job_id: str,
    kb_id: str,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """计算 manifest diff，写入 Job.diff_summary，返回 (diff, new_content_hash_map)。"""
    new_map = get_current_kb_content_hash_map(session, kb_id)
    active = get_active_index_artifact(session, kb_id)
    old_map: dict[str, str] = {}
    if active:
        old_map = load_manifest_content_hashes(session, active.id)
    diff = compute_diff(old_map, new_map)
    summary = {
        "added": len(diff["added"]),
        "deleted": len(diff["deleted"]),
        "modified": len(diff["modified"]),
        "unchanged": len(diff["unchanged"]),
        "document_ids": diff,
        "failed_parse_document_ids": [],
        "has_parse_failures": False,
    }
    job = session.get(IndexJob, job_id)
    if job:
        job.diff_summary = summary
        session.add(job)
    return diff, new_map


def merge_job_parse_failures(session: Session, job_id: str, failed_parse: list[str]) -> None:
    uniq = sorted(set(failed_parse))
    job = session.get(IndexJob, job_id)
    if not job:
        return
    summary = dict(job.diff_summary or {})
    summary["failed_parse_document_ids"] = uniq
    summary["has_parse_failures"] = len(uniq) > 0
    job.diff_summary = summary
    session.add(job)


def mark_index_job_running(session: Session, job_id: str) -> str | None:
    """将任务标为 running，返回 kb_id；无此任务则 None。"""
    job = session.get(IndexJob, job_id)
    if not job:
        return None
    job.status = "running"
    job.started_at = utc_now()
    job.updated_at = utc_now()
    job.progress = 5
    job.attempts = (job.attempts or 0) + 1
    session.add(job)
    session.flush()
    return job.kb_id


def update_index_job(
    session: Session,
    job_id: str,
    *,
    progress: int | None = None,
    status: str | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    attempts: int | None = None,
) -> None:
    job = session.get(IndexJob, job_id)
    if not job:
        return
    if progress is not None:
        job.progress = progress
    if status is not None:
        job.status = status
        job.error_message = error_message
        if error_code is not None:
            job.error_code = error_code
        job.finished_at = utc_now()
        if status == "succeeded":
            job.progress = 100
    job.updated_at = utc_now()
    if attempts is not None:
        job.attempts = attempts
    session.add(job)


def create_kb_index_artifact(
    session: Session,
    kb_id: str,
    version: int,
    index_path: str,
    job_id: str,
    *,
    new_content_hashes: dict[str, str] | None,
) -> None:
    """创建并激活索引产物，写入 manifest，更新 Document.indexed_*。"""
    deactivate_active_artifacts(session, kb_id)
    artifact = IndexArtifact(
        kb_id=kb_id,
        version=version,
        index_path=index_path,
        index_job_id=job_id,
        is_active=True,
    )
    session.add(artifact)
    session.flush()

    if not new_content_hashes:
        return

    now = utc_now()
    for doc_id, chash in new_content_hashes.items():
        n_chunks = len(session.exec(select(Chunk.id).where(Chunk.document_id == doc_id)).all())
        if n_chunks <= 0:
            continue
        session.add(
            IndexArtifactManifest(
                artifact_id=artifact.id,
                document_id=doc_id,
                content_hash=chash,
                chunk_count=n_chunks,
            )
        )
        doc = session.get(Document, doc_id)
        if doc:
            doc.indexed_content_hash = chash
            doc.indexed_artifact_id = artifact.id
            doc.indexed_at = now
            doc.status = "ready"
            session.add(doc)


def get_active_index_artifact(session: Session, kb_id: str) -> IndexArtifact | None:
    return (
        session.exec(
            select(IndexArtifact)
            .where(IndexArtifact.kb_id == kb_id, IndexArtifact.is_active == True)  # noqa: E712
            .order_by(IndexArtifact.version.desc())
            .limit(1)
        )
        .first()
    )


def load_manifest_content_hashes(session: Session, artifact_id: str) -> dict[str, str]:
    """active artifact 对应的 document_id -> content_hash（用于 diff）"""
    rows = session.exec(
        select(IndexArtifactManifest).where(IndexArtifactManifest.artifact_id == artifact_id)
    ).all()
    return {r.document_id: r.content_hash for r in rows}


def get_current_kb_content_hash_map(session: Session, kb_id: str) -> dict[str, str]:
    """当前知识库内、关联有效 KbFile 的文档 -> FileObject.sha256"""
    rows = session.exec(
        select(Document.id, FileObject.sha256)
        .join(KbFile, Document.kb_file_id == KbFile.id)
        .join(FileObject, KbFile.file_object_id == FileObject.id)
        .where(
            Document.kb_id == kb_id,
            Document.status != "deleted_pending_purge",
            KbFile.status == "active",
        )
    ).all()
    return {str(doc_id): str(sha) for doc_id, sha in rows}


def get_latest_index_job_for_kb(session: Session, kb_id: str) -> IndexJob | None:
    """该知识库下最近一条索引任务（任意状态）。"""
    return (
        session.exec(
            select(IndexJob)
            .where(IndexJob.kb_id == kb_id)
            .order_by(IndexJob.created_at.desc())
            .limit(1)
        )
        .first()
    )


def get_latest_index_job_by_document(session: Session, document_id: str) -> IndexJob | None:
    """按文档所属 KB 取最近一条索引任务（KB 级流水线）。"""
    doc = session.get(Document, document_id)
    if not doc:
        return None
    return get_latest_index_job_for_kb(session, doc.kb_id)


def _resolve_existing_kb_job(
    session: Session,
    job: IndexJob,
) -> tuple[str, bool] | None:
    """
    处理同 KB 已存在的 running/queued（或幂等命中）任务。

    Returns:
        (job_id, should_enqueue) 若应直接返回；None 表示已标 failed 或不应复用，继续新建。
    """
    if is_stale_index_job(job):
        logger.warning("stale index job marked failed job_id=%s", job.id)
        mark_index_job_stale_failed(session, job)
        return None
    if job.status == "queued":
        # 非僵尸 queued：复用 job_id，但仍投递 Celery（避免「有号未喊后厨」）
        return job.id, True

    # running：正在执行，不重复入队
    return job.id, False

def ensure_kb_index_job(
    session: Session,
    kb_id: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    """
    保证存在可复用的 KB 级索引任务。

    Returns:
        (job_id, should_enqueue_background)
        - 已存在相同 idempotency_key 的任务：非僵尸则按状态复用（queued 会再入队）。
        - 同 kb 已有 running（非僵尸）：复用，不再入队。
        - 同 kb 已有 queued（非僵尸）：复用，应再次入队（必达 worker）。
        - 僵尸 queued/running：标 failed 后新建 queued 并入队。
        - 否则新建 KB 级 queued 任务，应入队。
    """
    with _per_kb_ensure_lock(kb_id):
        if idempotency_key:
            existing = get_index_job_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                resolved = _resolve_existing_kb_job(session, existing)
                if resolved is not None:
                    return resolved

        running = session.exec(
            select(IndexJob)
            .where(IndexJob.kb_id == kb_id, IndexJob.status == "running")
            .order_by(IndexJob.created_at.desc())
            .limit(1)
        ).first()
        if running is not None:
            resolved = _resolve_existing_kb_job(session, running)
            if resolved is not None:
                return resolved

        queued = session.exec(
            select(IndexJob)
            .where(IndexJob.kb_id == kb_id, IndexJob.status == "queued")
            .order_by(IndexJob.created_at.asc())
            .limit(1)
        ).first()
        if queued is not None:
            resolved = _resolve_existing_kb_job(session, queued)
            if resolved is not None:
                return resolved

        key = idempotency_key or f"kb:{kb_id}:build:{uuid.uuid4().hex}"
        job = IndexJob(
            kb_id=kb_id,
            status="queued",
            progress=0,
            idempotency_key=key,
        )
        session.add(job)
        session.flush()
        return job.id, True


# -- M2.2: 等价数据访问方法（供 usecases 层替代 inline ORM） ------------------


def list_manifests_by_artifact(session: Session, artifact_id: str) -> list[IndexArtifactManifest]:
    """返回指定 artifact 的全部 manifest 记录（供索引完成验证器校验）。"""
    return list(
        session.exec(
            select(IndexArtifactManifest)
            .where(IndexArtifactManifest.artifact_id == artifact_id)
        ).all()
    )
