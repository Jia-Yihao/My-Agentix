"""声称一致性检查（Ch7 §7.9「说对做错」：答案声称 ↔ 工具轨迹来源）。

用途：
    从模型答案文本中提取**法条引用声称**（《法名》第X条，中/阿数字条号），
    与该次运行的实际 ``sources``（trajectory 工具来源）以及 golden 的
    ``expected_sources`` / ``expected_citations``（人工核定真值）比对，
    报告「声称了但证据池里没有」的样本。

纪律：
    * **只标记不判分**：``claim_consistency`` 仅进评估摘要，不参与任何门禁指标；
    * 纯 stdlib 离线正则，零网络、零 LLM；
    * 条号正则可能误伤（合同文书的「第 11 条」、非法律语境的书名号等），
      因此报告口径固定带人工复核说明——**不符样本须人工复核后方可作结论**；
    * 证据池 = 实际 ``sources`` ∪ golden 期望引用：golden 期望是人工核定真值，
      答案照它引用不算虚假声称；「检索没召回该条」归 ``citation_hit_rate`` 管，
      两件事不混。

本模块不得 import ``run_eval``（``run_eval`` 会以双路径方式 import 本模块，
互相 import 会成环）；所需正则/归一化在此自持。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "Claim",
    "NOTE",
    "build_report",
    "check_row",
    "claim_supported",
    "cn_to_int",
    "extract_claims",
    "flatten_source",
]

#: 报告固定口径（human review 兜底，Gotcha：正则误伤靠人工复核）。
NOTE = (
    "只标记不判分：条号正则可能误伤（如合同「第 11 条」），"
    "不符样本须人工复核后方可作为结论"
)

#: 答案/来源中的《法名》第X条 引用族（与 run_eval._ANSWER_CITATION_RE 同族，
#: 刻意各自持有一份：两模块互不 import，防环）。
_CLAIM_RE = re.compile(
    r"《(?P<law>[^《》\n]{2,40})》\s*第\s*(?P<article>[0-9零一二三四五六七八九十百千]+)\s*条"
)

#: 任意位置的条号（供来源侧兜底解析，如 dict 展平后的「第577条」）
_ARTICLE_ANY_RE = re.compile(r"第?\s*([0-9零一二三四五六七八九十百千]+)\s*条")

#: 连续中文串（来源侧抽法规名兜底）
_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")

#: 法规名清洗：书名号/空白/分隔符
_LAW_STRIP_RE = re.compile(r"[《》〈〉\s·・\-_/]+")

#: 法令常见前缀（比对前剥离，「中华人民共和国民法典」≡「民法典」）
_LAW_PREFIXES: Tuple[str, ...] = ("中华人民共和国",)

_CN_DIGIT_VALUES = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT_VALUES = {"十": 10, "百": 100, "千": 1000}


class Claim(NamedTuple):
    """一条法条引用声称（法规名已归一，条号已转整数）。"""

    law: str
    article: int
    raw: str


def cn_to_int(text: str) -> Optional[int]:
    """中文/阿拉伯数字转整数（支持 十/百/千 组合，五百七十七→577）。

    解析失败返回 ``None``（fail-open：该条声称不成立，不抛错）。
    """
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, section, digit = 0, 0, 0
    for ch in text:
        if ch in _CN_DIGIT_VALUES:
            digit = _CN_DIGIT_VALUES[ch]
        elif ch in _CN_UNIT_VALUES:
            unit = _CN_UNIT_VALUES[ch]
            if digit == 0:
                digit = 1
            if unit == 10:
                section += digit * 10
            elif unit == 100:
                total += section + digit * 100
                section = 0
            else:
                total += section + digit * 1000
                section = 0
            digit = 0
        else:
            return None
    result = total + section + digit
    return result if result > 0 else None


def normalize_law(name: str) -> str:
    """法规名归一：剥书名号/空白/分隔符与「中华人民共和国」前缀。"""
    cleaned = _LAW_STRIP_RE.sub("", str(name or ""))
    for prefix in _LAW_PREFIXES:
        if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
            cleaned = cleaned[len(prefix) :]
    return cleaned


def extract_claims(text: Any) -> List[Claim]:
    """抽取答案中的法条声称（去重保序）；条号/法规名解析失败的跳过。"""
    seen = set()
    out: List[Claim] = []
    for match in _CLAIM_RE.finditer(str(text or "")):
        law = normalize_law(match.group("law"))
        article = cn_to_int(match.group("article"))
        if not law or article is None:
            continue
        key = (law, article)
        if key in seen:
            continue
        seen.add(key)
        out.append(Claim(law=law, article=article, raw=match.group(0)))
    return out


def flatten_source(source: Any) -> str:
    """把一条 source（字符串或 dict）展平为可解析文本。"""
    if isinstance(source, dict):
        parts: List[str] = []
        for key in ("law", "article", "source", "document_id", "content_snippet", "name"):
            raw = source.get(key)
            if raw:
                parts.append(str(raw))
        if parts:
            return " ".join(parts)
        return json.dumps(source, ensure_ascii=False)
    return str(source)


def _source_law_article(source: Any) -> Tuple[Optional[str], Optional[int]]:
    """解析来源侧的 (法规名归一条号, 条号整数)；解析不到的部分为 None。"""
    text = flatten_source(source)
    law: Optional[str] = None
    article: Optional[int] = None

    named = _CLAIM_RE.search(text)
    if named:
        law = normalize_law(named.group("law"))
        article = cn_to_int(named.group("article"))
    elif isinstance(source, dict) and source.get("law"):
        law = normalize_law(str(source.get("law")))

    if article is None:
        article_match = _ARTICLE_ANY_RE.search(text)
        if article_match:
            article = cn_to_int(article_match.group(1))

    if not law:
        cjk = _CJK_RUN_RE.search(_LAW_STRIP_RE.sub("", text))
        if cjk:
            law = normalize_law(cjk.group(0))
    return (law or None), article


def claim_supported(claim: Claim, sources: Iterable[Any]) -> bool:
    """声称是否被证据池中任一来源支持。

    规则：
    * 法规名互相包含（「民法典」≡「中华人民共和国民法典」）；
    * 来源可解析出条号 → 必须相等；来源无法解析条号（如整本文档）→ 法规名命中即支持。
    """
    for source in sources:
        src_law, src_article = _source_law_article(source)
        if not src_law:
            continue
        if not (claim.law in src_law or src_law in claim.law):
            continue
        if src_article is None or src_article == claim.article:
            return True
    return False


def check_row(
    message: Any,
    sources: Any,
    expected: Any,
) -> str:
    """单样本声称一致性判定。

    返回：
        * ``"no_claims"`` —— 答案未提取出任何法条声称（无可校验对象）；
        * ``"ok"`` —— 全部声称有证据支持；
        * ``"mismatch"`` —— 存在证据池不支持的声称（「说对做错」候选）。

    ``expected`` 为 golden 的期望引用（``expected_sources`` ∪
    ``expected_citations``），作为人工核定真值并入证据池。
    """
    claims = extract_claims(message)
    if not claims:
        return "no_claims"
    backing: List[Any] = list(sources or []) + list(expected or [])
    if all(claim_supported(claim, backing) for claim in claims):
        return "ok"
    return "mismatch"


def build_report(rows: Sequence[Dict[str, Any]], *, has_predictions: bool) -> Dict[str, Any]:
    """把逐条 ``claim_status`` 聚合成 summary 报告键 ``claim_consistency``。

    * 无任何预测行（无 trajectory）→ 诚实 ``applicable=False``；
    * ``mismatch_ids`` 只给 id，不回显答案原文（报告口径只标记）。
    """
    if not has_predictions:
        return {
            "applicable": False,
            "reason": "no_predictions_for_any_golden_row",
            "checked": 0,
            "ok_count": 0,
            "mismatch_count": 0,
            "mismatch_ids": [],
            "no_claims_count": 0,
            "missing_prediction_count": len(rows),
            "note": NOTE,
        }

    mismatch_ids = [
        str(row.get("id"))
        for row in rows
        if row.get("claim_status") == "mismatch"
    ]
    ok_count = sum(1 for row in rows if row.get("claim_status") == "ok")
    no_claims_count = sum(1 for row in rows if row.get("claim_status") == "no_claims")
    missing_count = sum(
        1 for row in rows if row.get("claim_status") == "missing_prediction"
    )
    return {
        "applicable": True,
        "reason": None,
        "checked": ok_count + len(mismatch_ids),
        "ok_count": ok_count,
        "mismatch_count": len(mismatch_ids),
        "mismatch_ids": mismatch_ids,
        "no_claims_count": no_claims_count,
        "missing_prediction_count": missing_count,
        "note": NOTE,
    }
