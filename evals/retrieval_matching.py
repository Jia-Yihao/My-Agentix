"""Shared legal-citation matching helpers for end-to-end demo diagnostics."""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence

from evals.run_eval import (
    _ARTICLE_ANY_RE,
    _cn_to_int,
    _extract_article,
    _extract_law,
    _flatten_citation,
)

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")


def _int_to_cn(number: int) -> str:
    if number < 0 or number > 9999:
        return str(number)
    if number < 10:
        return _CN_DIGITS[number]
    parts: List[str] = []
    last_unit = 0
    remainder = number
    for unit_index in (3, 2, 1):
        unit_value = 10**unit_index
        digit = remainder // unit_value
        remainder %= unit_value
        if digit:
            if parts and last_unit > unit_value * 10:
                parts.append("零")
            parts.append(_CN_DIGITS[digit] + _CN_UNITS[unit_index])
            last_unit = unit_value
    if remainder:
        if parts and last_unit >= 100:
            parts.append("零")
        parts.append(_CN_DIGITS[remainder])
    text = "".join(parts)
    return text[1:] if text.startswith("一十") else text


def _doc_haystack(doc: Any) -> str:
    parts: List[str] = [str(getattr(doc, "page_content", "") or "")]
    metadata = getattr(doc, "metadata", None) or {}
    if isinstance(metadata, dict):
        parts.extend(str(value) for value in metadata.values() if value is not None)
    return "\n".join(parts)


def _law_in_haystack(law: str, haystack: str) -> bool:
    if law in haystack:
        return True
    for title in re.findall(r"《([^》]+)》", haystack):
        normalized = _extract_law(f"《{title}》") or title
        if law in normalized or normalized in law:
            return True
    return False


def doc_matches_gold(gold: Any, doc: Any) -> bool:
    """Match one legal citation against a retrieved document."""
    gold_text = _flatten_citation(gold)
    haystack = _doc_haystack(doc)
    if not gold_text or not haystack:
        return False

    article = _extract_article(gold_text)
    law = _extract_law(_ARTICLE_ANY_RE.sub(" ", gold_text))
    if law is None:
        return gold_text in haystack
    if not _law_in_haystack(law, haystack):
        return False
    if article is None:
        return True

    forms = {f"第{article}条"}
    article_number = _cn_to_int(article)
    if article_number is not None:
        forms.add(f"第{article_number}条")
        forms.add(f"第{_int_to_cn(article_number)}条")
    return any(form in haystack for form in forms)


def first_rank(docs: Sequence[Any], golds: Sequence[Any]) -> Optional[int]:
    """Return the rank of the first matching document, or ``None``."""
    for rank, doc in enumerate(docs or [], start=1):
        if any(doc_matches_gold(gold, doc) for gold in golds):
            return rank
    return None
