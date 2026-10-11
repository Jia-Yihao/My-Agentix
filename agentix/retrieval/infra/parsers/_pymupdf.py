"""PyMuPDF 导入兼容层（唯一导入入口）。

**为什么需要这个文件**：PyMuPDF 的**导入名在版本间变过**，而项目里有三处
``import fitz`` 硬编码：

- ``pdf_loader.get_pdf_page_count`` / ``load_pdf_documents``
- ``pdf_thumbnail.render_first_page_webp``

旧版（≤1.25.x）同时提供 ``fitz`` 与 ``pymupdf`` 两个顶层模块，所以硬编码
``fitz`` 一直能跑；新版（≥1.26）**移除了 ``fitz`` 兼容别名**，只保留
``pymupdf``。于是只要环境里的 PyMuPDF 比声明的新，PDF 解析与缩略图就会在
运行时 ``ModuleNotFoundError`` —— 而且**是静默的**：调用发生在函数体内，
只有真正上传 PDF 的那一刻才炸，单测完全覆盖不到（此前仓库里没有一个
PDF 解析测试）。

两件事一起改才是真的修好：

1. **代码侧**（本文件）：按 ``pymupdf`` → ``fitz`` 顺序尝试，两个名字都能跑，
   不再把整个 PDF 链路绑死在某个版本的别名上；
2. **环境侧**：``requirements.txt`` 的 pin 与实装版本必须一致
   （见 ``tests/test_dependency_consistency.py``）——版本漂移本身就是本次
   故障的成因。

调用方式::

    from ._pymupdf import load_pymupdf
    pymupdf = load_pymupdf()
    with pymupdf.open(path) as doc:
        ...
"""

from __future__ import annotations

from types import ModuleType

__all__ = ["load_pymupdf", "PYMUPDF_IMPORT_HINT"]

PYMUPDF_IMPORT_HINT = (
    "未找到 PyMuPDF：请安装 `pymupdf`（requirements.txt 已声明）。"
    "注意新版 PyMuPDF 只提供 `pymupdf` 模块、不再提供 `fitz` 别名。"
)


def load_pymupdf() -> ModuleType:
    """返回可用的 PyMuPDF 模块对象。

    优先 ``pymupdf``（新版的正式名），回退 ``fitz``（旧版遗留别名）。

    :raises ImportError: 两者都不可用时抛出，错误信息指明该装什么。
    """
    try:
        import pymupdf  # type: ignore[import-not-found]

        return pymupdf
    except ImportError:  # pragma: no cover - 旧版环境
        pass
    try:
        import fitz  # type: ignore[import-not-found]

        return fitz
    except ImportError as exc:  # pragma: no cover - 未安装依赖时的兜底
        raise ImportError(PYMUPDF_IMPORT_HINT) from exc
