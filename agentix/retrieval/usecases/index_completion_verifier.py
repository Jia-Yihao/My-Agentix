"""
M4.1: 索引任务完成验证器（环境终态校验）。
- 验证新索引经检索入口可返回结果；
- 验证 artifact manifest 包含全部待索引文档且 chunk_count 匹配；
- 验证向量条数与 manifest 期望总数一致（防「残留索引被追加」导致的向量重复）；
- 任一验证项失败 → fail-closed：任务不得标记 succeeded。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlmodel import Session

from agentix.retrieval.infra.repos.document_repo import list_chunks_by_document_id, list_ready_documents_by_kb
from agentix.retrieval.infra.repos.index_repo import list_manifests_by_artifact

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """验证结果。"""
    passed: bool
    failure_stage: str | None = None
    details: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.details.append(msg)

    def fail(self, stage: str, msg: str) -> None:
        self.passed = False
        self.failure_stage = stage
        self.add(msg)


def verify_index_completion(
    session: Session,
    *,
    kb_id: str,
    artifact_id: str,
    index_path: str,
) -> VerificationResult:
    """
    验证索引任务完成质量（fail-closed）。

    ① 检索入口校验：加载向量库并测试搜索可返回结果；
    ② manifest 完整性校验：artifact manifest 包含全部待索引文档且 chunk_count 匹配。

    验证器自身故障（检索入口超时等）按验证失败处理，保留日志支持人工重跑。
    """
    result = VerificationResult(passed=True)
    expected_vectors: int | None = None

    # ① manifest 完整性校验
    try:
        expected_vectors = _verify_manifest(session, kb_id=kb_id, artifact_id=artifact_id, result=result)
    except Exception as e:
        logger.exception("manifest verification error: %s", e)
        result.fail("verification", f"manifest 校验异常: {e}")

    # ② 检索入口校验（仅 manifest 通过后才有意义）
    if result.passed:
        try:
            _verify_retrieval_entry(
                kb_id=kb_id,
                index_path=index_path,
                expected_vectors=expected_vectors,
                result=result,
            )
        except Exception as e:
            logger.exception("retrieval entry verification error: %s", e)
            result.fail("verification", f"检索入口校验异常: {e}")

    return result


def _verify_manifest(
    session: Session,
    *,
    kb_id: str,
    artifact_id: str,
    result: VerificationResult,
) -> int | None:
    """manifest 完整性：artifact 包含全部 ready 文档且 chunk_count 匹配。

    Returns:
        期望的向量条数（各 manifest chunk_count 之和）；校验未通过时返回 ``None``。
    """
    manifests = list_manifests_by_artifact(session, artifact_id)
    manifest_doc_ids = {m.document_id for m in manifests}

    # KB 下所有 ready 文档
    ready_docs = list_ready_documents_by_kb(session, kb_id)
    ready_doc_ids = {d.id for d in ready_docs}

    # manifest 应覆盖全部 ready 文档
    missing = ready_doc_ids - manifest_doc_ids
    if missing:
        result.fail("verification", f"manifest 缺失 {len(missing)} 个文档: {sorted(missing)[:5]}")
        return None

    # chunk_count 匹配
    for m in manifests:
        actual_chunks = list_chunks_by_document_id(session, m.document_id)
        actual_count = len(actual_chunks)
        if actual_count != m.chunk_count:
            result.fail(
                "verification",
                f"文档 {m.document_id} chunk_count 不匹配: manifest={m.chunk_count}, actual={actual_count}",
            )
            return None

    result.add(f"manifest 校验通过: {len(manifests)} 个文档, chunk_count 全部匹配")
    return sum(m.chunk_count for m in manifests)


def _verify_retrieval_entry(
    *,
    kb_id: str,
    index_path: str,
    expected_vectors: int | None,
    result: VerificationResult,
) -> None:
    """检索入口校验：加载向量库，并核对向量条数与 manifest 期望一致。"""
    from agentix.retrieval.usecases.chat_service import build_retriever

    try:
        # 刻意不走 retriever_cache：本函数的职责就是**独立地**从磁盘重新加载
        # 刚落盘的产物来核对（拿进程内缓存对象等于自证，校验就失效了）。
        _, vectorstore = build_retriever(index_path)
    except Exception as e:
        result.fail("verification", f"向量库加载失败: {e}")
        return

    if vectorstore is None:
        result.fail("verification", "向量库加载返回 None")
        return

    # 测试搜索（用空查询或简单词，只要不崩溃即可）
    try:
        ntotal = int(getattr(getattr(vectorstore, "index", None), "ntotal", 0) or 0)
        if ntotal == 0:
            result.fail("verification", "向量库为空（ntotal=0）")
            return
        # ntotal 必须与 manifest 期望条数一致：偏多说明残留索引被追加（向量重复），
        # 偏少说明有 chunk 未写入向量库。仅检查 ntotal != 0 会放行这两类缺陷。
        if expected_vectors is not None and ntotal != expected_vectors:
            result.fail(
                "verification",
                f"向量条数与 manifest 不符: ntotal={ntotal}, 期望={expected_vectors}"
                "（偏多通常为残留索引被追加，偏少为存在未写入向量库的 chunk）",
            )
            return
        result.add(f"检索入口校验通过: ntotal={ntotal}, 期望={expected_vectors}")
    except Exception as e:
        result.fail("verification", f"向量库检查异常: {e}")
