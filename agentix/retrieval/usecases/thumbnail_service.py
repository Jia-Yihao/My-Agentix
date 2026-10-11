"""
文档缩略图：渲染首页 WebP → 上传 S3 → 写入 documents.thumbnail_key
"""
from __future__ import annotations

import logging
import os

from sqlmodel import Session

from agentix.retrieval.infra.parsers.pdf_thumbnail import render_first_page_webp
from agentix.retrieval.infra.repos.document_repo import get_document_by_id, resolve_document_file
from agentix.retrieval.infra.storage.s3_minio import delete_object, download_to_temp, upload_bytes
from agentix.retrieval.settings import settings

logger = logging.getLogger(__name__)

# 缩略图响应的缓存策略。
#
# 为什么**不能**是 ``public``：该路由是带鉴权的（``get_current_user`` +
# ``require_document_readable``），鉴权粒度是"每文档 × 每用户"。``public`` 允许
# **共享缓存**（CDN / 反向代理 / 中间代理）留存并复用该响应，而共享缓存无法复制
# 这份逐用户判定，等于把 A 用户可读的缩略图交给 B 用户。
#
# 为什么**不能**是 ``immutable``：``immutable`` 向浏览器承诺"在 max-age 内本 URL 的
# 字节永不变化"。但缩略图**没有**按内容做版本化——``generate_document_thumbnail``
# 每次重新生成都写回同一个 key ``thumbs/{document_id}.webp`` 并原地覆盖
# （``old_key == key``，因此不会走 ``delete_object`` 分支），而响应取用的 object key
# 又来自可变的 ``documents.thumbnail_key`` 列。于是"同一 URL 的字节会变"，承诺不成立；
# 浏览器在整个 max-age 内不再校验，这正是陈旧窗口的来源。
#
# 保留的折中：max-age 由 24h 收紧到 1h，既保住文档宫格反复渲染的浏览器缓存收益，
# 又把最坏陈旧时间从 86400s 压到 3600s。配合路由侧的 ETag/If-None-Match → 304 分支，
# 过期后的校验是"零 S3 GET"的廉价请求，因此收紧 max-age 不会带来带宽回退。
THUMBNAIL_CACHE_CONTROL = "private, max-age=3600"


def thumbnail_object_key(document_id: str) -> str:
    return f"thumbs/{document_id}.webp"


def generate_document_thumbnail(session: Session, document_id: str, *, pdf_path: str | None = None) -> str | None:
    """
    为文档生成缩略图并更新 thumbnail_key。

    pdf_path 可选：索引任务已下载 PDF 时可复用本地路径，避免重复下载。
    失败时记录日志并返回 None，不抛出异常（索引主流程不应被缩略图阻断）。
    """
    doc = get_document_by_id(session, document_id)
    if not doc:
        return None

    temp_path = pdf_path
    owned_temp = False
    try:
        if not temp_path:
            object_key, _ = resolve_document_file(session, document_id)
            temp_path = download_to_temp(object_key)
            owned_temp = True

        webp_bytes = render_first_page_webp(temp_path, max_width=settings.THUMBNAIL_MAX_WIDTH)
        key = thumbnail_object_key(document_id)
        upload_bytes(webp_bytes, key, content_type="image/webp")

        old_key = (doc.thumbnail_key or "").strip()
        doc.thumbnail_key = key
        session.add(doc)
        session.commit()

        if old_key and old_key != key:
            try:
                delete_object(old_key)
            except Exception:
                logger.warning("failed to delete old thumbnail %s", old_key, exc_info=True)

        return key
    except Exception:
        logger.warning("thumbnail generation failed for document %s", document_id, exc_info=True)
        session.rollback()
        return None
    finally:
        if owned_temp and temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
