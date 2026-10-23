"""
M4.3: 问答引用可溯源校验。
- 回答完成事件发出前，校验 citations 均来自本次真实检索命中集；
- 无法溯源的引用被剥离并记录 WARN。

R1-2（引用验证器 v1，依据《技术变更与架构演进指南 v2.0》§2.1.5⑥）：
- 从**答案文本**中抽取数据条目引用（`《测试数据名》第X条`），与本次检索命中集
  交叉核验——防止"引用格式正确但内容不在证据里"的伪造/幻觉引用；
- 无法核验的引用**显式标注**在答案尾部（v1 不做自动修正；LLM 复核 Agent
  属 L5 演进项）。验证器与被审 Agent 隔离：纯函数、无 Agent 上下文依赖。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 数据条目引用模式：《测试数据名》第X条（中文数字/阿拉伯数字均支持）
_STATUTE_CITATION_RE = re.compile(
    r"《(?P<law>[^《》\n]{2,40})》\s*第\s*(?P<article>[0-9零一二三四五六七八九十百千]+)\s*条"
)

_ANNOTATION_HEADER = "\n\n---\n> ⚠️ **引用核验提示**：以下引用未能与本次检索结果核验一致，请以权威文本为准："


def _normalize(text: str) -> str:
    """去空白后比对（中文文本常含排版空格）。"""
    return re.sub(r"\s+", "", text or "")


def extract_statute_citations(answer_text: str) -> list[tuple[str, str]]:
    """从答案文本抽取 (测试数据名, 条号) 引用列表（保持出现顺序、去重）。"""
    if not answer_text:
        return []
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _STATUTE_CITATION_RE.finditer(answer_text):
        ref = (match.group("law").strip(), match.group("article").strip())
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def verify_answer_citations(
    answer_text: str,
    hit_chunks: list[dict],
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """答案文本数据条目引用 ↔ 检索证据交叉核验（引用验证器 v1）。

    Args:
        answer_text: 生成的最终答案。
        hit_chunks: 本次检索命中集（元素含 ``content``/``page_content`` 预览
            与可选 ``source``；无内容字段时退化为 source 匹配）。

    Returns:
        (annotated_text, verified_refs, unverified_refs)
        - annotated_text: 附核验标注后的答案（全部核验通过或无引用时原样返回）；
        - 命中集为空（无证据路径）时不判定不标注——没有证据
          就谈不上"与证据不一致"，避免把合理拒答误伤为伪造引用。
    """
    refs = extract_statute_citations(answer_text)
    if not refs or not hit_chunks:
        # 无引用或无证据路径：不判定、不标注（没有证据就谈不上"与证据不一致"）
        return answer_text, [], []

    # 拼接证据池：命中 chunk 内容 + 来源名（去空白归一化后做子串匹配）
    evidence_pool: list[str] = []
    for chunk in hit_chunks:
        content = (
            chunk.get("content")
            or chunk.get("page_content")
            or chunk.get("snippet")
            or ""
        )
        source = str(chunk.get("source") or chunk.get("document_title") or "")
        evidence_pool.append(_normalize(f"{source}\n{content}"))

    verified: list[tuple[str, str]] = []
    unverified: list[tuple[str, str]] = []
    for law, article in refs:
        needle = _normalize(f"《{law}》第{article}条")
        # 宽松匹配：证据里出现测试数据名 + 同条号（允许条编号写法差异）
        loose_law = _normalize(f"《{law}》")
        loose_article = re.compile(rf"第{_normalize(article)}条")
        matched = any(
            (needle in evidence) or (loose_law in evidence and loose_article.search(evidence))
            for evidence in evidence_pool
        )
        if matched:
            verified.append((law, article))
        else:
            unverified.append((law, article))

    if unverified:
        logger.warning(
            "citation verifier: %d/%d answer-text citations unverified: %s",
            len(unverified),
            len(refs),
            [f"《{law}》第{article}条" for law, article in unverified],
            extra={"stage": "citation_verifier", "status": "unverified"},
        )
        items = "、".join(f"《{law}》第{article}条" for law, article in unverified)
        answer_text = f"{answer_text}{_ANNOTATION_HEADER}{items}"

    return answer_text, verified, unverified


def verify_citations_traceable(
    citations: list[dict],
    hit_chunks: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    校验引用可溯源：每个 citation 的 chunk_id 应在 hit_chunks 中。

    Returns:
        (verified_citations, stripped_citations)
        - verified_citations: 通过校验的引用（保留）
        - stripped_citations: 无法溯源的引用（剥离）
    """
    if not citations:
        return [], []

    hit_chunk_ids = {
        str(h.get("chunk_id", "")) for h in hit_chunks if h.get("chunk_id")
    }

    verified: list[dict] = []
    stripped: list[dict] = []
    for cite in citations:
        chunk_id = str(cite.get("chunk_id", ""))
        if chunk_id and chunk_id in hit_chunk_ids:
            verified.append(cite)
        else:
            stripped.append(cite)
            logger.warning(
                "citation stripped: chunk_id=%s not in retrieval hits",
                chunk_id,
                extra={"stage": "verification"},
            )

    return verified, stripped
