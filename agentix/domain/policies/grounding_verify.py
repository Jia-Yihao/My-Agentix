"""R3-6 GroundingValidator v1（架构文档 §23-25 / §104：Evidence Layer）。

职责定位（与既有验证器互补，构成 §104 要求的四件套）：

    Evidence / Citation      → get_citations_from_docs（已有）
    CitationValidator        → citation_verify.py（R1-2，引用可归因）
    GroundingValidator       → **本模块**（句子级证据锚定）
    effective_status         → legal_effectiveness.py（R2-2）

GroundingValidator 回答的问题不是"引用对不对"（那是 CitationValidator），
而是"**答案的每句话有没有证据支撑**"——对答案做句子级切分后，逐句与
本次检索证据池做字符 bigram 包含度比对，产出 grounding score 与
未锚定句列表。

v1 设计纪律：
- **纯函数、零 LLM 调用**：确定性、可解释、毫秒级——语义级 grounding
  （NLI / entailment）属后续演进，不在 v1；
- **观测模式**：调用方（`_post_process_and_persist`）只记录报告与低分
  WARN，不改答案文本、不拦截交付——与 R3-3 观测模式同纪律；
- **fail-open**：无证据（无检索命中）时跳过判定——没有证据就谈不上
  "与证据不一致"，避免把合理拒答误判为无锚定（与 citation_verify 同哲学）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 句子切分（中文标点为主，兼顾换行）
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；!?\n]+")

#: 纯数字/标点/空白句（无实质内容，跳过判定）
_NON_SUBSTANTIVE_RE = re.compile(r"^[\d\s\.\-—:：,，、%°'\"（）()\[\]【】]+$")

_MIN_CLAIM_CHARS = 8

#: 报告里未锚定句预览的最大长度（防止报告本身膨胀）
_PREVIEW_CHARS = 60

#: 归一化：仅保留字母/数字/CJK（\W 含全角标点与空白；额外剔除下划线）
_NORMALIZE_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)


def split_claims(answer_text: str, *, min_chars: int = _MIN_CLAIM_CHARS) -> list[str]:
    """把答案切成待校验的实质句列表（短句/纯数字句过滤）。"""
    if not answer_text:
        return []
    claims: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(answer_text):
        text = raw.strip().lstrip("-•*·1234567890.、） ")
        if len(text) < min_chars:
            continue
        if _NON_SUBSTANTIVE_RE.match(text):
            continue
        claims.append(text)
    return claims


def _normalize(text: str) -> str:
    """去空白与标点（bigram 比对用，标点差异不参与锚定判断）。"""
    return _NORMALIZE_STRIP_RE.sub("", text or "").lower()


def _bigrams(text: str) -> set[str]:
    """字符 bigram 集合（长度不足 2 返回空集）。"""
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _containment(claim_grams: set[str], evidence_grams: set[str]) -> float:
    """包含度：claim 的 bigram 有多少比例出现在证据里（0-1）。"""
    if not claim_grams:
        return 0.0
    return len(claim_grams & evidence_grams) / len(claim_grams)


@dataclass
class GroundingReport:
    """Grounding 校验报告（观测模式产物，进日志/tool_ctx/轨迹）。"""

    #: "observe"（v1 固定——只报告不拦截）
    mode: str = "observe"
    #: 待校验的实质句数
    total_claims: int = 0
    #: 锚定成功的句数
    supported_claims: int = 0
    #: grounding score = supported / total（无实质句或跳过时为 None）
    score: float | None = None
    #: 未锚定句预览（最多 max_report_claims 条）
    unsupported: list[str] = field(default_factory=list)
    #: 跳过原因（no_evidence / empty_answer / disabled）；正常判定时为空
    skipped_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "total_claims": self.total_claims,
            "supported_claims": self.supported_claims,
            "score": self.score,
            "unsupported": self.unsupported,
            "skipped_reason": self.skipped_reason,
        }


def _build_evidence_grams(hit_chunks: list[dict]) -> set[str]:
    """从命中集构建证据池 bigram 集合（字段约定与 citation_verify 一致）。"""
    pool: list[str] = []
    for chunk in hit_chunks or []:
        content = (
            chunk.get("content")
            or chunk.get("page_content")
            or chunk.get("snippet")
            or ""
        )
        source = str(chunk.get("source") or chunk.get("document_title") or "")
        pool.append(f"{source}\n{content}")
    grams: set[str] = set()
    for text in pool:
        grams |= _bigrams(_normalize(text))
    return grams


def validate_answer_grounding(
    answer_text: str,
    hit_chunks: list[dict],
    *,
    min_chars: int = _MIN_CLAIM_CHARS,
    support_threshold: float = 0.35,
    max_report_claims: int = 8,
) -> GroundingReport:
    """答案句子级证据锚定校验（GroundingValidator v1，纯函数）。

    Args:
        answer_text: 生成的最终答案。
        hit_chunks: 本次检索命中集（字段约定同 citation_verify）。
        min_chars: 实质句最小长度（低于则跳过判定）。
        support_threshold: 句子锚定阈值（bigram 包含度 ≥ 视为有证据支撑）。
        max_report_claims: 报告中未锚定句预览上限。

    Returns:
        GroundingReport。注意：**本函数只判定不处置**——是否拦截/标注
        由调用方按模式决定（v1 固定观测模式）。
    """
    claims = split_claims(answer_text or "", min_chars=min_chars)
    if not claims:
        return GroundingReport(skipped_reason="empty_answer")

    if not hit_chunks:
        # fail-open：无证据不判定（与 citation_verify 同哲学）
        return GroundingReport(total_claims=len(claims), skipped_reason="no_evidence")

    evidence_grams = _build_evidence_grams(hit_chunks)
    if not evidence_grams:
        return GroundingReport(total_claims=len(claims), skipped_reason="no_evidence")

    supported = 0
    unsupported: list[str] = []
    for claim in claims:
        grams = _bigrams(_normalize(claim))
        if _containment(grams, evidence_grams) >= support_threshold:
            supported += 1
        elif len(unsupported) < max_report_claims:
            unsupported.append(claim[:_PREVIEW_CHARS])

    score = round(supported / len(claims), 2)
    return GroundingReport(
        total_claims=len(claims),
        supported_claims=supported,
        score=score,
        unsupported=unsupported,
    )


class GroundingValidator:
    """GroundingValidator（架构文档 §104 命名）——可注入阈值的校验器实例。

    与 :func:`validate_answer_grounding` 等价的面向对象封装；为后续
    语义级 grounding（NLI 模型）替换内核留出接缝（接口不变换实现）。
    """

    def __init__(
        self,
        *,
        min_chars: int = _MIN_CLAIM_CHARS,
        support_threshold: float = 0.35,
        max_report_claims: int = 8,
    ) -> None:
        self._min_chars = min_chars
        self._support_threshold = support_threshold
        self._max_report_claims = max_report_claims

    def validate(self, answer_text: str, hit_chunks: list[dict]) -> GroundingReport:
        return validate_answer_grounding(
            answer_text,
            hit_chunks,
            min_chars=self._min_chars,
            support_threshold=self._support_threshold,
            max_report_claims=self._max_report_claims,
        )
