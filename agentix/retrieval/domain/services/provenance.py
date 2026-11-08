"""召回溯源（provenance）：把「这段话引自哪里」随片段一起交给模型。

链路位置::

    检索命中 → assemble_context 给每条片段打 [S1] 标号 → build_prompt 注入标号指令
    → 模型在句末标 [S1] → 出答后 align_citations 把引用卡按标号排前

设计口径：

- **标号只在一次问答内有效**：``S1`` 是本次召回的序号，不是持久 ID。落库时
  只把标号随引用卡一起存（``ref`` 字段），回放不再重算，也不跨请求复用。
- **纯函数**：本模块不碰 DB / 网络。来源名只取片段 metadata（``source`` /
  ``document_title`` / ``page``）；文件级元信息由 usecases 侧回表补齐，键名同源。
- **fail-open**：答案里没有标号、或标号对不上任何引用时，引用列表原序返回、
  一条不删——溯源是增强，不是新的拦截点（拦截另有 citation_verify 两道闸）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

#: 标号前缀。改这里等于改 Prompt 里的引用语法，须同步 PROVENANCE_INSTRUCTION。
REF_PREFIX = "S"

#: 来源名超长时截断，避免一条标号把 Prompt 预算吃掉一截
MAX_SOURCE_CHARS = 60

#: 模型侧溯源指令（build_prompt 在开启溯源时注入）。不含花括号：会被 .format() 吃。
PROVENANCE_INSTRUCTION = """下方每条片段以 [S1]、[S2] … 开头，标号后是来源文档与页码。
回答时若用到某条片段的内容，请在对应句子末尾标上该片段标号（如 [S1]）；
未使用的片段不要标注。
"""

#: metadata 里可能承载「文档名」的键，按优先级取第一个非空值
_SOURCE_KEYS = ("document_title", "source", "file_name")

#: metadata 里可能承载「页码」的键
_PAGE_KEYS = ("page_no", "page")

#: 答案里的标号：容忍模型写成 [s1] / [ S1 ] / 全角括号外的空白
_REF_RE = re.compile(r"\[\s*[Ss]\s*(\d{1,3})\s*\]")


# ---------------------------------------------------------------------------
# 1. 标号与标签
# ---------------------------------------------------------------------------


def provenance_ref(index: int) -> str:
    """第 ``index`` 条片段（从 1 开始）的标号：``S1``。"""
    return f"{REF_PREFIX}{int(index)}"


def _source_of(meta: Mapping[str, Any]) -> str:
    for key in _SOURCE_KEYS:
        value = meta.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return ""


def _page_of(meta: Mapping[str, Any]) -> Optional[int]:
    for key in _PAGE_KEYS:
        value = meta.get(key)
        if value in (None, ""):
            continue
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0:
            return page
    return None


def provenance_label(meta: Optional[Mapping[str, Any]], *, index: int) -> str:
    """生成片段标号行：``[S1] 劳动合同法 · 第3页``。

    来源与页码都缺失时退化为 ``[S1] 未命名片段``——标号本身不能省，
    否则模型无法引用这一条。
    """
    meta = meta or {}
    source = _source_of(meta)
    if len(source) > MAX_SOURCE_CHARS:
        source = source[:MAX_SOURCE_CHARS] + "…"
    ref = f"[{provenance_ref(index)}]"
    details = [source] if source else []
    page = _page_of(meta)
    if page is not None:
        details.append(f"第{page}页")
    if not details:
        return f"{ref} 未命名片段"
    return f"{ref} {' · '.join(details)}"


def provenance_entry(doc: Any, *, index: int) -> dict:
    """单条片段的溯源记录（落 meta / 对齐引用都靠它）。"""
    meta = dict(getattr(doc, "metadata", None) or {})
    chunk_id = meta.get("chunk_id")
    return {
        "ref": provenance_ref(index),
        "chunk_id": str(chunk_id) if chunk_id not in (None, "") else None,
        "document_id": meta.get("document_id"),
        "source": _source_of(meta) or None,
        "page_no": _page_of(meta),
        "label": provenance_label(meta, index=index),
    }


def provenance_entries(docs: Sequence[Any], *, start: int = 1) -> list[dict]:
    """按装配顺序给一批片段打号（``start`` 允许续号）。"""
    return [
        provenance_entry(doc, index=start + offset)
        for offset, doc in enumerate(docs or [])
    ]


def format_block(entry: Mapping[str, Any], text: str) -> str:
    """标号行 + 片段正文（装配层拼接用）。"""
    return f"{entry['label']}\n{text}"


# ---------------------------------------------------------------------------
# 2. 从答案回读标号
# ---------------------------------------------------------------------------


def parse_refs(answer_text: Optional[str]) -> list[str]:
    """抽取答案里出现的标号，按**首次出现**顺序去重（``["S2", "S1"]`` 保序）。"""
    if not answer_text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _REF_RE.finditer(answer_text):
        ref = provenance_ref(match.group(1))
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


# ---------------------------------------------------------------------------
# 3. 引用卡与标号对齐
# ---------------------------------------------------------------------------


def align_citations(
    citations: Sequence[dict],
    entries: Sequence[dict],
    answer_text: Optional[str],
) -> tuple[list[dict], dict]:
    """按答案标号重排引用卡：被引用的在前（按首次出现序），其余原序垫后。

    Args:
        citations: ``get_citations_from_docs`` 产出的引用卡（可已带 ``ref``）。
        entries: 本次召回的溯源记录（``provenance_entries`` 产出）。
        answer_text: 模型最终答案。

    Returns:
        ``(ordered_citations, report)``。``report`` 含 ``refs``（答案里读到的标号）、
        ``cited``（对上的条数）、``unresolved``（读了标号但没有对应引用）、
        ``aligned``（是否真的重排过）。

    对齐失败一律返回原序（``aligned=False``）：没有标号不是错误，
    只说明这一轮模型没引用，引用卡照旧按检索序展示。
    """
    cites = list(citations or [])
    report: dict[str, Any] = {
        "refs": [],
        "cited": 0,
        "unresolved": [],
        "aligned": False,
    }
    if not cites:
        return cites, report

    refs = parse_refs(answer_text)
    report["refs"] = refs
    if not refs:
        return cites, report

    # 标号 → 引用卡下标：先认引用卡自带的 ref，再用溯源记录的 chunk_id 兜底
    # （引用卡可能来自未带 provenance 的老路径）。
    index_by_ref: dict[str, int] = {}
    chunk_by_ref = {
        str(e.get("ref")): str(e.get("chunk_id"))
        for e in (entries or [])
        if e.get("ref") and e.get("chunk_id")
    }
    for position, cite in enumerate(cites):
        ref = cite.get("ref")
        if ref and str(ref) not in index_by_ref:
            index_by_ref[str(ref)] = position
    for ref, chunk_id in chunk_by_ref.items():
        if ref in index_by_ref:
            continue
        for position, cite in enumerate(cites):
            if str(cite.get("chunk_id")) == chunk_id:
                index_by_ref[ref] = position
                break

    head: list[int] = []
    for ref in refs:
        position = index_by_ref.get(ref)
        if position is None:
            report["unresolved"].append(ref)
            continue
        if position not in head:
            head.append(position)

    if not head:
        return cites, report

    tail = [i for i in range(len(cites)) if i not in head]
    ordered = [{**cites[i], "cited": True} for i in head]
    ordered += [{**cites[i], "cited": False} for i in tail]
    report["cited"] = len(head)
    report["aligned"] = True
    return ordered, report
