"""
索引任务执行器

后台任务执行器（可替换 Celery/RQ/Arq）。
负责：PDF 解析 -> 分块 -> 向量构建 -> 产物记录。
仅支持 KB 级任务：始终对该 kb_id 做 manifest diff、增量 chunk + FAISS 全量重建。
"""
import logging
import os
import re
import shutil
import time
import asyncio
from dataclasses import dataclass

from sqlmodel import Session, select

from agentix.retrieval.config import get_default_config
from agentix.retrieval.infra.parsers.pdf_loader import load_pdf_documents
from agentix.retrieval.infra.storage.s3_minio import delete_object, download_to_temp
from agentix.retrieval.infra.vectorstore.manager import VectorStoreManager


# M1.1: 流水线阶段依赖显式声明（§9 第 4 项：串行/并行依赖显式声明）
@dataclass(frozen=True)
class PipelineStage:
    stage_id: str
    dep_type: str  # "serial" | "parallel"
    serial_reason: str
    parallel_candidate: bool
    parallel_feasibility: str


PIPELINE_STAGES: list[PipelineStage] = [
    PipelineStage("1_diff", "serial", "入口阶段，无前置依赖", False, "无并行意义"),
    PipelineStage("2_delete_chunks", "serial", "依赖步骤1的diff['deleted']结果", False, "数据依赖步骤1"),
    PipelineStage("3_chunk_documents", "serial", "依赖步骤1的diff['modified']/diff['added']；modified与added内部无共享状态", True,
                  "modified与added可并行切分（各自独立文档集），当前串行因单worker CPU/内存约束"),
    PipelineStage("4_has_any_check", "serial", "依赖步骤2/3的chunk写入完成，需判断KB是否还有chunk", False, "数据依赖步骤2+3"),
    PipelineStage("5_build_vector", "serial", "依赖步骤4确认有chunk，需全量读取所有chunk构建FAISS", False, "数据依赖步骤4"),
    PipelineStage("6_artifact_verify", "serial", "依赖步骤5的索引文件路径，需写入artifact+manifest+验证", False, "数据依赖步骤5"),
]
from agentix.retrieval.infra.db.session import engine
from agentix.retrieval.domain.models.document import Document, Chunk
from agentix.retrieval.infra.repos.document_repo import (
    delete_chunks_by_document_id,
    delete_chunks_for_document_ids,
    fail_documents_in_indexing,
    has_any_chunk,
    resolve_document_file,
    set_document_status,
)
from agentix.retrieval.infra.repos.index_repo import (
    create_kb_index_artifact,
    deactivate_active_artifacts,
    get_next_artifact_version,
    mark_index_job_running,
    merge_job_parse_failures,
    record_kb_diff_on_job,
    update_index_job,
)
from agentix.retrieval.infra.tasks.reliable import RetryPolicy, run_with_retry
from agentix.retrieval.settings import settings
from agentix.observability import record_index_job
from agentix.observability.context import bind_request_id
from agentix.observability.spans import sync_span

logger = logging.getLogger(__name__)


def run_index_job(job_id: str) -> None:
    """执行 KB 级索引任务（Celery / BackgroundTasks 入口）。

    标 running 后委托 ``_execute_kb_index_pipeline``：manifest diff → 按变更增删/重切 chunk
    → 全库 FAISS 重建 → 写 artifact/manifest；失败时将该 KB 下 indexing 文档标 failed。
    """
    bind_request_id(f"job_{job_id}")
    with Session(engine) as session:
        kb_id = mark_index_job_running(session, job_id)
        if not kb_id:
            return
        session.commit()

    started = time.monotonic()

    def execute_with_timeout() -> None:
        if time.monotonic() - started >= settings.INDEX_JOB_TIMEOUT_SECONDS:
            raise TimeoutError("index job timeout")
        _execute_kb_index_pipeline(job_id, kb_id)
        if time.monotonic() - started >= settings.INDEX_JOB_TIMEOUT_SECONDS:
            raise TimeoutError("index job timeout")

    result = run_with_retry(
        execute_with_timeout,
        RetryPolicy(
            max_attempts=settings.INDEX_MAX_ATTEMPTS,
            base_delay_seconds=settings.INDEX_RETRY_BASE_DELAY_SECONDS,
            max_delay_seconds=settings.INDEX_RETRY_MAX_DELAY_SECONDS,
        ),
    )
    if not result.ok:
        from agentix.retrieval.domain.policies.errors import classify
        record_index_job(
            time.monotonic() - started,
            "failed",
            classify(result.error).value if result.error else "unknown",
        )
        # 服务端日志保留全量细节；对外 error_message 只写脱敏后的用户可行动信息
        logger.error("index job %s failed after %s attempts: %s", job_id, result.attempts, result.error, exc_info=result.error)
        from agentix.retrieval.domain.policies.errors import sanitize_error
        from agentix.retrieval.domain.policies.errors import classify
        with Session(engine) as session:
            fail_documents_in_indexing(session, kb_id)
            update_index_job(session, job_id, status="failed",
                             error_message=sanitize_error(result.error)[:500],
                             error_code=classify(result.error).value if result.error else None,
                             attempts=result.attempts)
            session.commit()
    else:
        record_index_job(time.monotonic() - started, "succeeded", "")


def _execute_kb_index_pipeline(job_id: str, kb_id: str) -> None:
    """index.pipeline 主 span + 委托实现（R3.6 接入点 5）。"""
    with sync_span("index.pipeline", kb_id=kb_id, job_id=job_id):
        _execute_kb_index_pipeline_impl(job_id, kb_id)


def _execute_kb_index_pipeline_impl(job_id: str, kb_id: str) -> None:
    """KB 级增量索引主流程：diff → 删/重切 chunk → 全量 FAISS 重建 → 落 artifact。"""
    # 1. 对比当前 manifest 与库内文档，得到 added/modified/deleted，并写入 job 的 diff_summary字段
    with Session(engine) as session:
        diff, new_map = record_kb_diff_on_job(session, job_id, kb_id)
        session.commit()

    _touch_job_progress(job_id, 20)

    # 2. 已删除文档：直接清掉其 chunk（向量索引稍后全量重建，此处只维护 DB）
    with Session(engine) as session:
        delete_chunks_for_document_ids(session, diff["deleted"])
        session.commit()

    # 3. 变更/新增文档：先删旧 chunk，再解析 PDF 并写入新 chunk；解析失败记入 failed_parse
    #    「新增」也必须先清：doc 相对 active manifest 是 added，但库内可能已有上次尝试
    #    写入的 chunk（如本次 run_with_retry 重跑、或上轮在步骤 6 前中断）。chunk 落库是
    #    纯追加（_parse_and_chunk_document），不清会导致 chunk 与向量重复、同内容重复计费。
    #    对真正的新文档该删除是 no-op。
    failed_parse: list[str] = []
    for doc_id in [*diff["modified"], *diff["added"]]:
        with Session(engine) as session:
            delete_chunks_by_document_id(session, doc_id)
            session.commit()
    with sync_span("index.parse_chunk", kb_id=kb_id):
        _chunk_documents_for_index(diff["modified"], failed_parse)
        _chunk_documents_for_index(diff["added"], failed_parse)

    with Session(engine) as session:
        merge_job_parse_failures(session, job_id, failed_parse)
        session.commit()

    _touch_job_progress(job_id, 50)

    # 4. 若 KB 下已无任何 chunk（例如全部删除或解析均失败），停用旧 artifact 后直接成功结束
    with Session(engine) as session:
        if not has_any_chunk(session, kb_id):
            deactivate_active_artifacts(session, kb_id)
            update_index_job(session, job_id, status="succeeded")
            purged_objects = _purge_deleted_documents(session, diff["deleted"])
            session.commit()
            _delete_purged_objects(purged_objects, kb_id)
            _verify_purged_documents(kb_id, diff["deleted"])
            return
        next_version = get_next_artifact_version(session, kb_id)
        session.commit()

    # 5. 基于当前 KB 全部 chunk 全量重建 FAISS，写入新版本目录
    index_path = _index_path_for_kb(kb_id, next_version)
    _prepare_index_dir(index_path)

    with sync_span("index.build", kb_id=kb_id):
        _build_vector_index(kb_id, index_path)
    _touch_job_progress(job_id, 80)

    # 6. 登记新 artifact（含 manifest content_hash），验证通过后才留着它 active 并标成功
    with Session(engine) as session:
        from agentix.retrieval.infra.repos.index_repo import get_active_index_artifact
        from agentix.retrieval.usecases.index_completion_verifier import verify_index_completion

        # create_kb_index_artifact 会「下线旧产物 + 激活新产物」，故先记下切换前的 active：
        # 验证失败时要能回滚到上一个可用索引，否则 KB 会拿验证不通过的索引对外检索。
        previous_active = get_active_index_artifact(session, kb_id)

        create_kb_index_artifact(
            session,
            kb_id,
            next_version,
            index_path,
            job_id,
            new_content_hashes=new_map,
        )
        # M4.1/M4.2: 完成验证器（fail-closed）——验证通过才标 succeeded
        artifact = get_active_index_artifact(session, kb_id)
        if artifact is not None:
            verification = verify_index_completion(
                session,
                kb_id=kb_id,
                artifact_id=artifact.id,
                index_path=index_path,
            )
            if not verification.passed:
                logger.error(
                    "index completion verification failed: %s | %s",
                    verification.failure_stage,
                    "; ".join(verification.details),
                )
                _rollback_active_artifact(session, artifact, previous_active)
                update_index_job(
                    session,
                    job_id,
                    status="failed",
                    error_code="INDEX_FAILED",
                    error_message=f"验证失败: {verification.failure_stage}",
                )
                session.commit()
                return
            logger.info("index completion verification passed: %s", verification.details)
        update_index_job(session, job_id, status="succeeded")
        session.commit()
        purged_objects = _purge_deleted_documents(session, diff["deleted"])
        session.commit()
    _delete_purged_objects(purged_objects, kb_id)
    _verify_purged_documents(kb_id, diff["deleted"])


def _rollback_active_artifact(session: Session, failed_artifact, previous_active) -> None:
    """把 active 指针从「验证不通过的产物」回滚到「上一个可用产物」。

    没有上一个可用产物（首次索引）时保持无 active —— 宁可不检索，也不用坏索引。
    注意：新产物的 manifest 与 Document.indexed_* 不回滚，因此下次索引会把这些文档
    重新判为 added（它们确实不在 active 索引里）；chunk 写入侧已做幂等处理，不会重复。
    """
    failed_artifact.is_active = False
    session.add(failed_artifact)
    if previous_active is not None and previous_active.id != failed_artifact.id:
        previous_active.is_active = True
        session.add(previous_active)
        logger.warning(
            "rolled back active index artifact to version=%s after verification failure",
            previous_active.version,
        )
    else:
        logger.warning(
            "no previous active index artifact to roll back to; KB has no active index"
        )


def _verify_purged_documents(kb_id: str, document_ids: list[str]) -> None:
    """删除后置校验：确认已删文档无 chunk / manifest / 文档记录残留（R3.3）。"""
    if not document_ids:
        return
    from agentix.retrieval.usecases.document_purge_verifier import verify_document_purged

    with Session(engine) as session:
        for document_id in document_ids:
            verify_document_purged(session, kb_id=kb_id, document_id=document_id)


def _purge_deleted_documents(session: Session, document_ids: list[str]) -> list[str]:
    from agentix.retrieval.infra.repos.document_repo import purge_deleted_document

    objects: list[str] = []
    for deleted_id in document_ids:
        pdf_key, thumbnail_key = purge_deleted_document(session, deleted_id)
        objects.extend(key for key in (pdf_key, thumbnail_key) if key)
    return objects


def _delete_purged_objects(object_keys: list[str], kb_id: str) -> None:
    for object_key in object_keys:
        try:
            delete_object(object_key)
        except Exception:
            logger.warning("failed to purge object key=%s", object_key, exc_info=True)
    _cleanup_old_index_versions(kb_id)


def _touch_job_progress(job_id: str, progress: int) -> None:
    with Session(engine) as session:
        update_index_job(session, job_id, progress=progress)
        session.commit()


def _chunk_documents_for_index(document_ids: list[str], failed_parse: list[str]) -> None:
    for doc_id in document_ids:
        with Session(engine) as session:
            set_document_status(session, doc_id, "indexing")
            session.commit()
        try:
            with Session(engine) as session:
                object_key, filename = resolve_document_file(session, doc_id)
            temp_pdf_path = download_to_temp(object_key)
            try:
                if os.path.getsize(temp_pdf_path) <= 0:
                    raise ValueError(f"empty pdf after download: {object_key}")
                with Session(engine) as session:
                    from agentix.retrieval.usecases.thumbnail_service import generate_document_thumbnail

                    generate_document_thumbnail(session, doc_id, pdf_path=temp_pdf_path)
                n = _parse_and_chunk_document_off_thread(doc_id, temp_pdf_path, filename)
            finally:
                try:
                    if os.path.exists(temp_pdf_path):
                        os.remove(temp_pdf_path)
                except Exception:
                    pass
        except Exception as exc:
            # 这里此前是裸的 `except Exception: n = 0`：**异常对象被直接丢弃**。
            # 下游只会看到文档被标成 "failed"，看不到任何原因——而
            # ``_parse_and_chunk_document`` 与 ``download_to_temp`` 抛出的
            # 恰恰都是精心写过的高价值信息（``empty pdf after download: <key>``、
            # ``document not found before chunk insert``、S3 下载失败的具体错误）。
            # 「我的 PDF 传上去了为什么检索不到」是入库侧的头号问题，
            # 把原因丢掉等于让每个此类问题都必须靠加日志复现。
            # 处置仍是 n=0 → 标 failed（控制流不变），只是把原因留下来。
            logger.warning(
                "chunking failed for document %s, marking as failed: %s", doc_id, exc,
                exc_info=True,
            )
            n = 0
        if n == 0:
            with Session(engine) as session:
                set_document_status(session, doc_id, "failed")
                session.commit()
            failed_parse.append(doc_id)


def _index_path_for_kb(kb_id: str, version: int) -> str:
    return os.path.join(settings.INDEX_DIR, kb_id, f"v{version}")


def _prepare_index_dir(index_path: str) -> None:
    """让版本目录满足「要么不存在、要么完整」：清掉上次中断留下的半成品目录。

    ``next_version`` 由 artifact 表最大 version + 1 推导，因此落在该路径上的存量目录
    只可能来自「上次运行走到步骤 5（FAISS 构建）却未登记 artifact 就中断」的残留。
    若不复用，faiss 后端构造时会自动加载已存在的 ``index.faiss``
    （``faiss_impl.py:110``），随后 ``add_documents`` 退化为追加 → 向量重复，
    而完成验证器原先只检查 ``ntotal != 0``，无法发现。
    """
    if os.path.isdir(index_path):
        logger.warning("removing half-built index dir before rebuild: %s", index_path)
        shutil.rmtree(index_path, ignore_errors=True)
    os.makedirs(index_path, exist_ok=True)


_UNSAFE_SLUG_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _document_slug(source: str) -> str:
    """chunk_key 的文档前缀：只保留 [A-Za-z0-9_-]，其余字符折叠为下划线。"""
    return _UNSAFE_SLUG_CHARS.sub("_", (source or "doc").strip()) or "doc"


def _resolve_document_slugs(rows: list) -> dict[str, str]:
    """document_id -> slug。同 KB 内标题相同时追加 _2/_3，避免 chunk_key 撞车。"""
    slugs: dict[str, str] = {}
    taken: set[str] = set()
    for _chunk, doc in rows:
        if doc.id in slugs:
            continue
        base = _document_slug(doc.title)
        slug, suffix = base, 2
        while slug in taken:
            slug = f"{base}_{suffix}"
            suffix += 1
        slugs[doc.id] = slug
        taken.add(slug)
    return slugs


def _chunk_key(slug: str, ordinal: int) -> str:
    """稳定且可读的切片标识：{文档slug}#{文档内序号}。chunk_id(uuid) 保持不变。"""
    return f"{slug}#{ordinal}"


def _cleanup_old_index_versions(kb_id: str) -> None:
    """保留最近几个版本，避免每次全量重建无限占用磁盘。"""
    keep = max(1, settings.INDEX_VERSIONS_TO_KEEP)
    root = os.path.join(settings.INDEX_DIR, kb_id)
    if not os.path.isdir(root):
        return
    versions = []
    for name in os.listdir(root):
        if name.startswith("v") and name[1:].isdigit():
            versions.append((int(name[1:]), os.path.join(root, name)))
    for _, path in sorted(versions, reverse=True)[keep:]:
        try:
            shutil.rmtree(path)
            logger.info("removed old index version: %s", path)
        except OSError:
            logger.warning("could not remove old index version: %s", path, exc_info=True)


def _parse_and_chunk_document(document_id: str, temp_pdf_path: str, original_filename: str) -> int:
    """解析 PDF 并分块，返回写入的 chunk 数量。调用方负责提供/清理 temp_pdf_path。"""
    cfg = get_default_config()
    vsm = VectorStoreManager(cfg)

    if os.path.getsize(temp_pdf_path) <= 0:
        raise ValueError(f"empty pdf: {temp_pdf_path}")
    docs = load_pdf_documents(temp_pdf_path, source_name=original_filename)
    chunks = vsm.split_documents(docs)

    # 企业化 P1-4.2 ②：入库侧注入模式扫描（默认 observe-only 计数+告警；
    # PROMPT_INJECTION_INGEST_BLOCK=true 时阻断入库，文档标记 failed）。
    from agentix.config.settings import settings as main_settings
    from agentix.security.prompt_injection import scan_chunks_for_injection

    if main_settings.PROMPT_INJECTION_DETECTION_ENABLED:
        hit_chunks = scan_chunks_for_injection(
            [c.page_content for c in chunks], document_id=document_id
        )
        if hit_chunks and main_settings.PROMPT_INJECTION_INGEST_BLOCK:
            logger.warning(
                "blocking document %s: injection patterns in %d chunks "
                "(PROMPT_INJECTION_INGEST_BLOCK=true)",
                document_id,
                hit_chunks,
            )
            return 0  # 调用方按 n==0 → status=failed 处理

    chunks_to_create = []
    for doc in chunks:
        chunks_to_create.append(
            Chunk(
                document_id=document_id,
                content=doc.page_content,
                page_no=doc.metadata.get("page"),
                # 持久化分块起始字符偏移，使 chunk_key 的 ordinal 由内容位置决定，
                # 跨「删除重建索引」保持稳定（否则 fallback 到 uuid 顺序，每次重建都会打乱）。
                start_offset=doc.metadata.get("start_index"),
            )
        )

    if not chunks_to_create:
        return 0

    with Session(engine) as session:
        if not session.get(Document, document_id):
            raise RuntimeError(f"document not found before chunk insert: {document_id}")
        session.add_all(chunks_to_create)
        session.commit()
    return len(chunks_to_create)


def _parse_and_chunk_document_off_thread(
    document_id: str,
    temp_pdf_path: str,
    original_filename: str,
) -> int:
    """在线程中执行 PyMuPDF/分块，避免异步任务上下文被长 PDF 阻塞。"""
    return asyncio.run(asyncio.to_thread(
        _parse_and_chunk_document,
        document_id,
        temp_pdf_path,
        original_filename,
    ))


def _build_vector_index(kb_id: str, index_path: str) -> None:
    """全量重建知识库向量索引。"""
    from langchain_core.documents import Document as LCDocument

    all_docs_for_index = []

    with Session(engine) as session:
        rows = session.exec(
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(Document.kb_id == kb_id)
            .order_by(
                Document.created_at.asc(),
                Document.id.asc(),
                Chunk.page_no.asc(),
                Chunk.start_offset.asc(),
                Chunk.id.asc(),
            )
        ).all()

        logger.info("index collect kb_id=%s chunks=%s", kb_id, len(rows))

        slugs = _resolve_document_slugs(rows)
        ordinals: dict[str, int] = {}
        for chunk, doc in rows:
            doc_title = doc.title or "未命名文档"
            ordinal = ordinals.get(doc.id, 0)
            ordinals[doc.id] = ordinal + 1
            all_docs_for_index.append(
                LCDocument(
                    page_content=chunk.content,
                    metadata={
                        "chunk_id": chunk.id,
                        "chunk_key": _chunk_key(slugs[doc.id], ordinal),
                        "document_id": doc.id,
                        "page": chunk.page_no,
                        "source": doc_title,
                    },
                )
            )

    logger.info("index collected chunks=%s", len(all_docs_for_index))

    if not all_docs_for_index:
        raise ValueError("没有可索引的文档内容")

    from agentix.retrieval.infra.vectorstore.base import StoredVectorDocument
    from agentix.retrieval.infra.vectorstore.manager import create_vector_store

    backend = settings.VECTOR_STORE_BACKEND
    options: dict = {"index_path": index_path}
    if backend == "milvus":
        options.update(host=settings.MILVUS_HOST, port=settings.MILVUS_PORT, collection_name=settings.MILVUS_COLLECTION)
    elif backend == "qdrant":
        options.update(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, collection_name=settings.QDRANT_COLLECTION)
    vsm = create_vector_store(backend=backend, **options)
    vec_docs = [
        StoredVectorDocument(
            text=doc.page_content,
            metadata=doc.metadata,
            id=doc.metadata.get("chunk_id"),
        )
        for doc in all_docs_for_index
    ]
    vsm.add_documents(vec_docs)
    vsm.save(index_path)
    logger.info("index built backend=%s documents=%s", backend, len(vec_docs))
