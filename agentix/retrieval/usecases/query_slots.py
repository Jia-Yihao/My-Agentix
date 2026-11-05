"""检索侧确定性槽位提取：先抽 5W1H 关键信息，再加法式扩展检索文本。

改写只作用于送去检索的那段文字。用户原话、回复与判断使用的文本不受影响，
因此一次扩展不会悄悄改掉业务结论，评测也能离线复现。

三件事都是加法，不做替换——原句自带的词（尤其是口语说法和法规名）是 BM25
已经能用的信号，替换只会把它们弄丢：

1. 旧法名补一份现行法名（《婚姻法》→《民法典》），只补法名、不补条号；
2. 口语命中时追加规范法律术语（``怎么赔`` → ``赔偿损失``）；
3. 中文数字条号补一份阿拉伯写法（``第一百四十八条`` → ``第148条``）。

5W1H 面由 cue 词典判定，作为结构化槽位返回，供路由与后续排序消费。
本模块不认识数据库、不认识权限、不调用模型：只吃字符串吐字符串。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FACET_ORDER: tuple[str, ...] = ("WHO", "WHAT", "WHEN", "WHERE", "WHY", "HOW")

#: 单条查询最多追加的词数。追加过多会稀释 BM25 中稀有词的权重。
MAX_TERMS = 8

_FACET_CUES: dict[str, tuple[str, ...]] = {
    "WHO": ("谁", "哪位", "哪个部门", "由谁", "哪个机关", "谁负责", "承担主体"),
    "WHAT": (
        "什么",
        "哪些",
        "哪种",
        "条件",
        "标准",
        "情形",
        "范围",
        "定义",
        "种类",
        "比例",
        "顺序",
    ),
    "WHEN": ("多久", "什么时候", "何时", "期限", "时效", "周岁", "之前", "之后", "以内", "超过"),
    "WHERE": ("哪里", "哪个法院", "向谁申请", "在哪", "管辖"),
    "WHY": ("为什么", "为何", "依据", "根据", "凭什么", "原因", "怎么认定"),
    "HOW": ("怎么", "如何", "怎样", "怎么办", "流程", "处理", "申请", "主张", "计算", "能同时"),
}

# 民法典施行后被替代的旧法。只映射法名，绝不映射条号——把答案写进特征是
# 评测自欺，且法条编号会随修订变化，这张表不会。
_SUPERSEDED_LAWS: dict[str, str] = {
    "民法通则": "民法典",
    "侵权责任法": "民法典",
    "婚姻法": "民法典",
    "合同法": "民法典",
    "物权法": "民法典",
    "继承法": "民法典",
    "担保法": "民法典",
    "收养法": "民法典",
}

# 口语说法 -> 语料正文里实际使用的规范术语。左列是追加词，右列是触发词。
_LEGAL_SYNONYMS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("赔偿损失", "损害赔偿"), ("赔钱", "怎么赔", "赔多少", "赔付")),
    (("撤销权", "可撤销"), ("能不能撤", "可以撤", "请求撤销", "能撤")),
    (("责任承担", "侵权责任"), ("谁来担责", "谁担责", "谁承担", "谁负责", "由谁承担")),
    (("工伤认定",), ("算工伤", "是不是工伤")),
    (("劳动报酬",), ("加班费", "二倍工资")),
    (("赔偿金",), ("几倍赔偿", "赔几倍")),
    (("诉讼时效",), ("时效期间", "过了时效")),
    (("意思表示",), ("违背真实意思", "真实意思")),
    (("善意取得",), ("能不能取得所有权",)),
)

_CN_DIGITS: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_ARTICLE_CN_RE = re.compile(r"第([零〇一二两三四五六七八九十百千万]+)条")


@dataclass(frozen=True, slots=True)
class SlotExpansion:
    """一次扩展的结果；``search_text`` 是唯一该送去检索的文本。"""

    original: str
    search_text: str
    facets: tuple[str, ...]
    added_terms: tuple[str, ...]
    laws: tuple[str, ...]
    numbers: tuple[str, ...]
    synonyms: tuple[str, ...]


def detect_facets(query: str) -> tuple[str, ...]:
    """按固定顺序返回命中的 5W1H 面；无 cue 命中时返回空元组。"""
    return tuple(facet for facet in FACET_ORDER if any(cue in query for cue in _FACET_CUES[facet]))


def _cn_to_int(text: str) -> int | None:
    total = section = number = 0
    seen = False
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            seen = True
            continue
        unit = _CN_UNITS.get(char)
        if unit is None:
            return None
        seen = True
        if unit < 10000:
            section += (number or 1) * unit
            number = 0
        else:
            section = (section + number) * unit
            total += section
            section = number = 0
    return total + section + number if seen else None


def _number_terms(query: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in _ARTICLE_CN_RE.finditer(query):
        value = _cn_to_int(match.group(1))
        if value is None:
            continue
        term = f"第{value}条"
        if term not in query and term not in found:
            found.append(term)
    return tuple(found)


def _law_terms(query: str) -> tuple[str, ...]:
    found: list[str] = []
    # 长名优先，避免「民法通则」被「民法」截短后错配。
    for old in sorted(_SUPERSEDED_LAWS, key=len, reverse=True):
        if old not in query:
            continue
        current = _SUPERSEDED_LAWS[old]
        if current not in query and current not in found:
            found.append(current)
    return tuple(found)


def _synonym_terms(query: str, limit: int) -> tuple[str, ...]:
    if limit <= 0:
        return ()
    found: list[str] = []
    for policy_terms, user_terms in _LEGAL_SYNONYMS:
        if not any(term in query for term in user_terms):
            continue
        if any(term in query for term in policy_terms):
            continue
        for term in policy_terms:
            if term not in query and term not in found:
                found.append(term)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found)


def expand_query(query: str, *, max_terms: int = MAX_TERMS) -> SlotExpansion:
    """抽槽位并生成加法式检索文本；原句始终完整保留在最前面。"""
    text = query or ""
    limit = min(MAX_TERMS, max(0, max_terms))
    laws = _law_terms(text)
    numbers = _number_terms(text)

    added: list[str] = []
    for term in (*laws, *numbers):
        if term not in text and term not in added:
            added.append(term)
    synonyms = _synonym_terms(text, max(0, limit - len(added)))
    for term in synonyms:
        if term not in text and term not in added:
            added.append(term)
    added = added[:limit]

    return SlotExpansion(
        original=text,
        search_text=text if not added else f"{text} {' '.join(added)}",
        facets=detect_facets(text),
        added_terms=tuple(added),
        laws=laws,
        numbers=numbers,
        synonyms=synonyms,
    )
