"""
PDF 首页缩略图渲染（PyMuPDF → WebP）

PyMuPDF 经 :func:`._pymupdf.load_pymupdf` 兼容层导入（``pymupdf`` 优先，
``fitz`` 回退）——新版 PyMuPDF 已移除 ``fitz`` 别名。
"""
from __future__ import annotations

import io
import os

from ._pymupdf import load_pymupdf


def render_first_page_webp(pdf_path: str, *, max_width: int = 400) -> bytes:
    """
    渲染 PDF 第一页为 WebP 字节流。

    Raises:
        ValueError: 文件不存在、无页面或渲染失败
    """
    pymupdf = load_pymupdf()

    if not os.path.exists(pdf_path):
        raise ValueError(f"PDF 文件不存在: {pdf_path}")

    with pymupdf.open(pdf_path) as doc:
        if doc.page_count <= 0:
            raise ValueError(f"PDF 无页面: {pdf_path}")

        page = doc.load_page(0)
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            raise ValueError(f"PDF 首页尺寸无效: {pdf_path}")

        scale = min(max_width / rect.width, max_width / rect.height)
        scale = min(max(scale, 0.1), 2.0)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)

        buf = io.BytesIO()
        pix.pil_image().save(buf, format="WEBP", quality=85, method=4)
        data = buf.getvalue()
        if not data:
            raise ValueError(f"PDF 缩略图渲染为空: {pdf_path}")
        return data
