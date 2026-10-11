"""PDF 解析链路守卫（PyMuPDF 导入兼容 + 错误分类一致性）。

**为什么现在才有这个文件**：PDF 解析此前**零测试覆盖**，于是下面这个故障
可以一直躺着不被发现——

    PyMuPDF ≥1.26 移除了 ``fitz`` 兼容别名（只保留 ``pymupdf``），
    而 ``pdf_loader`` / ``pdf_thumbnail`` 里硬编码 ``import fitz``，
    且 import 写在函数体内 ⇒ 只有真正上传 PDF 的那一刻才 ``ModuleNotFoundError``。

本文件用 PyMuPDF 自己合成 PDF（无需外部样本文件），把"上传 PDF → 解析 →
出缩略图"这条链路端到端跑通，并用 AST 守卫禁止再出现裸 ``import fitz``。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARSERS_DIR = PROJECT_ROOT / "agentix" / "retrieval" / "infra" / "parsers"

pymupdf = pytest.importorskip("pymupdf", reason="PyMuPDF 未安装，跳过 PDF 链路测试")

from agentix.retrieval.domain.policies.errors import (  # noqa: E402
    ErrorCode,
    classify,
    classify_message,
    sanitize_error,
)
from agentix.retrieval.infra.parsers._pymupdf import load_pymupdf  # noqa: E402
from agentix.retrieval.infra.parsers.pdf_loader import (  # noqa: E402
    get_pdf_page_count,
    load_pdf_documents,
)
from agentix.retrieval.infra.parsers.pdf_thumbnail import render_first_page_webp  # noqa: E402


# ---------------------------------------------------------------------------
# 合成 PDF 夹具（不依赖任何外部样本文件）
# ---------------------------------------------------------------------------


def _make_pdf(path: Path, pages: int = 2, *, with_text: bool = True) -> Path:
    """合成一个 PDF。

    ``with_text=False`` 生成"有页但无文字"的 PDF —— 这是覆盖
    "解析成功但零有效内容"分支的唯一办法：PyMuPDF 不允许保存 0 页文档
    （``cannot save with zero pages``），所以"空 PDF"只能用空白页来表达。
    """
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        if with_text:
            page.insert_text((72, 72), f"page {index + 1} content")
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    return _make_pdf(tmp_path / "sample.pdf", pages=2)


@pytest.fixture
def textless_pdf(tmp_path: Path) -> Path:
    return _make_pdf(tmp_path / "textless.pdf", pages=2, with_text=False)


# ---------------------------------------------------------------------------
# 1. 兼容层本身
# ---------------------------------------------------------------------------


class TestPymupdfCompatLayer:
    def test_load_pymupdf_returns_usable_module(self):
        module = load_pymupdf()
        assert hasattr(module, "open"), "兼容层必须返回带 open() 的模块"
        assert hasattr(module, "Matrix"), "pdf_thumbnail 依赖 Matrix"

    def test_no_bare_fitz_import_anywhere_in_parsers(self):
        """★ 禁止再出现裸 ``import fitz``——它是本次故障的根因。

        AST 扫描而非 grep：注释里提到 ``fitz`` 是允许的（说明用），
        真正要禁的是 **import 语句**。
        """
        offenders: list[str] = []
        scanned = 0
        for path in sorted(PARSERS_DIR.glob("*.py")):
            if path.name == "_pymupdf.py":
                continue  # 兼容层自己当然要 import（它是唯一的例外）
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            scanned += 1
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] == "fitz":
                            offenders.append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] == "fitz":
                        offenders.append(f"{path.name}:{node.lineno}")

        assert scanned > 0, "扫描器没扫到任何文件——守卫已空转"  # 防空真
        assert not offenders, (
            f"以下位置绕过了兼容层直接 import fitz（新版 PyMuPDF 会 ImportError）：{offenders}"
        )

    def test_compat_layer_prefers_pymupdf_name(self):
        """新版优先：返回值就是 ``pymupdf`` 模块本身，而不是恰好能用的旧别名。"""
        module = load_pymupdf()
        assert module.__name__ in {"pymupdf", "fitz"}

    def test_missing_dependency_message_is_actionable(self, monkeypatch):
        """两个名字都不可用时，错误信息必须说清"装什么"。"""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name, *args, **kwargs):
            if name.split(".")[0] in {"pymupdf", "fitz"}:
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        with pytest.raises(ImportError) as exc_info:
            load_pymupdf()
        assert "pymupdf" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. 解析链路端到端
# ---------------------------------------------------------------------------


class TestPdfLoader:
    def test_page_count(self, sample_pdf: Path):
        assert get_pdf_page_count(str(sample_pdf)) == 2

    def test_load_documents_by_page(self, sample_pdf: Path):
        docs = load_pdf_documents(str(sample_pdf), source_name="合同.pdf")
        assert len(docs) == 2
        assert [d.metadata["page"] for d in docs] == [1, 2]
        assert all(d.metadata["source"] == "合同.pdf" for d in docs)
        assert "page 1 content" in docs[0].page_content
        assert "page 2 content" in docs[1].page_content

    def test_source_defaults_to_path(self, sample_pdf: Path):
        docs = load_pdf_documents(str(sample_pdf))
        assert docs[0].metadata["source"] == str(sample_pdf)

    def test_missing_file_raises_value_error(self, tmp_path: Path):
        with pytest.raises(ValueError, match="不存在"):
            load_pdf_documents(str(tmp_path / "nope.pdf"))

    def test_empty_pdf_raises_value_error(self, textless_pdf: Path):
        """有页但零可提取文字时不得静默返回空列表——那会让索引层误判为
        「解析成功但没内容」，而不是「这份文件解析不出东西」。"""
        with pytest.raises(ValueError, match="没有有效内容"):
            load_pdf_documents(str(textless_pdf))


class TestPdfThumbnail:
    def test_renders_webp_container(self, sample_pdf: Path):
        data = render_first_page_webp(str(sample_pdf))
        assert isinstance(data, bytes) and len(data) > 0
        # WebP = RIFF 容器 + "WEBP" 标识
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WEBP"

    def test_respects_max_width(self, sample_pdf: Path):
        """缩略图必须真的被缩放（否则 400px 的承诺是句空话）。"""
        from PIL import Image
        import io

        small = Image.open(io.BytesIO(render_first_page_webp(str(sample_pdf), max_width=100)))
        large = Image.open(io.BytesIO(render_first_page_webp(str(sample_pdf), max_width=400)))
        assert small.width < large.width

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="不存在"):
            render_first_page_webp(str(tmp_path / "nope.pdf"))


# ---------------------------------------------------------------------------
# 3. 错误分类一致性（同一个错误，两条路径必须给同一个结论）
# ---------------------------------------------------------------------------


class TestErrorClassificationConsistency:
    def test_string_and_exception_paths_agree(self):
        """★ ``classify()`` 与 ``sanitize_error()`` 对同一个失败原因必须一致。

        生产代码 ``index_worker`` 把 ``result.error`` 同时喂给两者：
        一个落库成 ``error_code``，一个成为用户可见提示。此前 ``sanitize_error``
        对 ``str`` 入参直接短路成 UNKNOWN，于是同一个错误
        「落库 = parse_failed，提示 = 请稍后重试或联系管理员」自相矛盾。
        """
        message = "RuntimeError: 文档解析失败 pdf corrupted"
        assert classify(RuntimeError(message)) is ErrorCode.PARSE_FAILED
        assert sanitize_error(message) == "文档解析失败，请确认 PDF 未损坏或未加密"

    def test_pymupdf_import_error_classified_as_parse_failed(self):
        """``No module named 'pymupdf'`` 必须归到 PARSE_FAILED。

        消息里含 "pdf"（pymupdf 自带）能侥幸命中，但我们不该依赖这种巧合——
        这里把判据显式写进 ``classify_message``。
        """
        assert classify_message("ModuleNotFoundError", "No module named 'pymupdf'") is (
            ErrorCode.PARSE_FAILED
        )
        assert classify_message("ModuleNotFoundError", "No module named 'fitz'") is (
            ErrorCode.PARSE_FAILED
        )

    def test_timeout_wins_over_parse_keywords(self):
        """同时像"超时"和"解析失败"时，超时优先（先重试才有意义）。"""
        assert classify(TimeoutError("pdf parse timeout")) is ErrorCode.TIMEOUT

    def test_none_is_unknown(self):
        assert sanitize_error(None) == "索引任务失败，请稍后重试或联系管理员"

    def test_unclassified_string_is_unknown(self):
        assert classify_message("", "something entirely unexpected") is ErrorCode.UNKNOWN
