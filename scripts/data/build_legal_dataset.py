#!/usr/bin/env python3
"""build_legal_dataset.py — 法律领域数据集转换脚本（可复用）。

读取免费的法律法规结构化数据集 ``legal-cn-core-codices``，解析、清洗后，
**覆盖写入** My Agentix 项目已有的三个数据文件（写入前先备份为 ``.bak``）：

1. ``data/legal/legal_seed.json``          —— Neo4j 图谱本体（被 ``legal_kg_to_csv.py`` 读取）
2. ``data/legal/legal_lightrag_corpus.json`` —— LightRAG 语料（被 ``init_lightrag.py`` 读取）
3. ``data/kb/legal_corpus.txt``             —— kb 向量库语料

设计要点（详见任务说明）：
* 源法规条文是**扁平**数组，章节标题作为「预告标记」嵌在某条条文**末尾**；
  用 ``TRAILER_RE`` 从行尾提取，并把该标记从条文正文中剔除。
* 章节 ``层级`` 推导优先级：含「编」→ 1；含「章」→ 2；含「节」→ 3；否则 2。
* 司法解释存在两种字段风格（A：name/authority/promulgated；B：title/issued_by/
  publish_date/document_number），且 article 字段可能是 ``id/text`` 或
  ``article_id/content``，全部防御式解析。
* 合并规则：保留现有 seed 的 ``causes/interpretations/authorities``，仅追加新解析节点，按 name 去重。

仅依赖 Python 标准库，可在无项目环境的受管 Python 中直接运行。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------

# 本次纳入的 4 部法规（文件名，相对 laws/ 目录）
DEFAULT_LAW_FILES: List[str] = [
    "civil_code.json",          # 民法典
    "criminal_law.json",        # 刑法
    "labor_contract_law.json",  # 劳动合同法
    "constitution.json",        # 宪法
]

# 发文字号已知映射（其余法规留空，不编造）
DOC_NUMBER_MAP: Dict[str, str] = {
    "民法典": "主席令第四十五号",
    "劳动合同法": "主席令第六十五号",
}

# 机关层级映射（仅用于「新增」机关节点；现有节点原样保留，未知留空）
AUTHORITY_LEVEL_MAP: Dict[str, str] = {
    "全国人民代表大会": "最高国家权力机关",
    "全国人民代表大会常务委员会": "国家立法机关",
    "全国人大常委会": "国家立法机关",  # 与上者为同一机关，避免重复节点
    "最高人民法院": "最高审判机关",
    "最高人民检察院": "最高检察机关",
    "国务院": "最高行政机关",
}

# 初始（未标注章节）章标题
INITIAL_CHAPTER_TITLE = "（开篇·未标注章节）"

# 预告标记正则：仅匹配「行尾 $」，避免误伤正文中出现的「第X条」引用
TRAILER_RE = re.compile(
    r'(第[0-9一二三四五六七八九十百零]+(?:编|分编|章|节)[^。，；：]*)$'
)

# 章节层级推导
CHAPTER_LEVELS = ("编", "章", "节")

# ---------------------------------------------------------------------------
# 司法解释名覆盖表（脏名/损坏名 → 正确中文名）
# ---------------------------------------------------------------------------
# key 为源文件名主干（stem），value 为还原后的正确中文司法解释名。
# 覆盖两类已知脏名：
#   A 组：源文件 name 字段损坏为全 '?'，无法反推，直接用给出的正确中文名；
#   B 组：源文件 name 字段为中文脏串（括号失衡/序言片段/机关名等），在其源文件中
#        依据干净 short_name 字段或标准司法解释命名还原正确中文名。
# 在 _resolve_interp_name 中优先于任何字段被使用（见该函数实现）。
INTERP_NAME_OVERRIDES: Dict[str, str] = {
    # ---- A 组：name 字段损坏为全 '?'，已反推正确中文名 ----
    "environmental_tort_liability_interpretation":
        "最高人民法院关于审理生态环境侵权责任纠纷案件适用法律若干问题的解释",
    "environmental_punitive_damages_interpretation":
        "最高人民法院关于审理生态环境侵权惩罚性赔偿纠纷案件适用法律若干问题的解释",
    "bride_price_dispute_rules":
        "最高人民法院关于审理涉彩礼纠纷案件适用法律若干问题的规定",
    # ---- B 组：中文脏串，已从源库定位并还原正确中文名 ----
    # 源文件 negotiable_instruments_dispute_rules.json：name="中华人民共和国票据法》"
    "negotiable_instruments_dispute_rules":
        "最高人民法院关于审理票据纠纷案件若干问题的规定",
    # 源文件 bankruptcy_law_interpretation_1.json：name="中华人民共和国最高人民法院"
    "bankruptcy_law_interpretation_1":
        "最高人民法院关于适用〈中华人民共和国企业破产法〉若干问题的规定（一）",
    # 源文件 arbitration_interpretation.json：name="中华人民共和国最高人民法院"
    "arbitration_interpretation":
        "最高人民法院关于适用〈中华人民共和国仲裁法〉若干问题的解释",
    # 源文件 copyright_civil_dispute_interpretation.json：name="民法典》《著作权法》《民事诉讼法》…解释如下："
    "copyright_civil_dispute_interpretation":
        "最高人民法院关于审理著作权民事纠纷案件适用法律若干问题的解释",
    # 源文件 trademark_civil_dispute_interpretation.json：name="民法典》《商标法》《民事诉讼法》…解释如下："
    "trademark_civil_dispute_interpretation":
        "最高人民法院关于审理商标民事纠纷案件适用法律若干问题的解释",
    # 源文件 trademark_jurisdiction_interpretation.json：name="中华人民共和国商标法〉的决定》"
    "trademark_jurisdiction_interpretation":
        "最高人民法院关于审理商标案件有关管辖和法律适用范围问题的解释",
    # 源文件 domain_name_dispute_interpretation.json：name="民法典》《反不正当竞争法》和《民事诉讼法》"
    "domain_name_dispute_interpretation":
        "最高人民法院关于审理涉及计算机网络域名民事纠纷案件适用法律若干问题的解释",
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    """读取 JSON 文件，失败抛出清晰异常。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise SystemExit(f"无法读取/解析 JSON 文件 {path}: {exc}")


def dump_json(path: Path, data: Any) -> None:
    """以 UTF-8、非 ASCII 转义、缩进 2 写入 JSON。"""
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def backup_if_exists(path: Path) -> Optional[Path]:
    """若存在则备份为 .bak，返回备份路径（不存在返回 None）。"""
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        return bak
    return None


def clean_trailer(text: str) -> Tuple[str, Optional[str]]:
    """剔除条文末尾的章节预告标记。

    Returns
    -------
    (cleaned_text, trailer)
        cleaned_text 为去掉预告标记后的正文；trailer 为提取到的标记文字，
        若没有匹配则为 None。
    """
    if not text:
        return text, None
    stripped = text.rstrip()
    match = TRAILER_RE.search(stripped)
    if match:
        trailer = match.group(1)
        cleaned = stripped[: match.start()].rstrip()
        return cleaned, trailer
    return text, None


def chapter_level(title: str) -> int:
    """根据标题推导章节层级：编→1，章→2，节→3，否则 2。"""
    for idx, marker in enumerate(CHAPTER_LEVELS, start=1):
        if marker in title:
            return idx
    return 2


# ---------------------------------------------------------------------------
# 法条引用（条号）规则化抽取（不依赖 LLM，离线可跑）
# ---------------------------------------------------------------------------

# 中文数字 → 阿拉伯数字（支持 0~9999；法条条号在该范围内）
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# 条号数字：阿拉伯数字 或 中文数字（如 577 / 五百七十七）
_NUM_CLASS = r"(?:\d+|[零一二两三四五六七八九十百千]+)"

# 覆盖以下「同法规内引用其他条」的写法（被引用对象均为条号）：
#   1) 第X条 / 第X款 / 第X项                  —— 最常见
#   2) 本法第X条                              —— 显式「本法」
#   3) 第X编第X条 / 第X章第X条                —— 带编/章前缀
# 注：款/项引用多为条内细分，按任务要求一并抽取数字；最终仅保留确实存在于
# 本法规条号集合中的数字，过滤掉跨法/不存在/自引用，保证图谱精度。
_CITATION_PATTERNS: List[re.Pattern] = [
    re.compile(r"本法第(" + _NUM_CLASS + r")条"),
    re.compile(r"第[一二三四五六七八九十百零千两]+\s*(?:编|章)第(" + _NUM_CLASS + r")条"),
    re.compile(r"第(" + _NUM_CLASS + r")(?:条|款|项)"),
]


def cn_numeral_to_int(text: str) -> Optional[int]:
    """把中文数字（如 五百七十七 / 一千二百六十 / 十 / 二十）转为 int。

    支持范围 0~9999；纯阿拉伯数字直接转；非法字符返回 ``None``。
    """
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (current if current > 0 else 1) * unit
            current = 0
        else:
            return None
    total += current
    return total


def extract_citations(content: str, valid_nos: set, self_no: str) -> List[str]:
    """从法条正文抽取同法规内被引用的条号（规则化，不依赖 LLM）。

    正文中的条号既可能是阿拉伯数字（第577条），也常见中文数字
    （第五百七十七条）。本函数统一把两种写法转为阿拉伯数字串，
    仅保留确实存在于 ``valid_nos`` 中、且非自引用的条号。

    Parameters
    ----------
    content : str
        法条正文（已剔除章节预告标记）。
    valid_nos : set
        本法规全部条号集合（字符串），用于过滤不存在的引用。
    self_no : str
        当前法条自身条号，避免自引用（如「本条」「第一款」）。

    Returns
    -------
    List[str]
        排序后的被引用条号列表（如 ``["577", "584"]``）；无则空列表。
    """
    if not content:
        return []
    try:
        self_int = int(self_no)
    except (ValueError, TypeError):
        self_int = None
    found: set = set()
    for pat in _CITATION_PATTERNS:
        for m in pat.finditer(content):
            num = cn_numeral_to_int(m.group(1))
            if num is None:
                continue
            if self_int is not None and num == self_int:
                continue
            key = str(num)
            if key in valid_nos:
                found.add(key)
    # 按数值升序，保证结果稳定、可复现
    return sorted(found, key=lambda x: int(x))


# ---------------------------------------------------------------------------
# 源数据解析
# ---------------------------------------------------------------------------

def parse_law(path: Path) -> Dict[str, Any]:
    """解析单部法规源文件，返回标准化结构。"""
    data = load_json(path)
    if not isinstance(data, dict):
        raise SystemExit(f"法规文件结构异常（非对象）: {path}")

    short_name = data.get("short_name") or data.get("name") or ""
    authority = data.get("authority") or ""
    effective = data.get("effective") or ""

    articles: List[Dict[str, Any]] = []
    for raw in data.get("articles", []) or []:
        if not isinstance(raw, dict):
            continue
        aid = raw.get("id")
        if aid is None:
            continue
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        articles.append({"id": str(aid), "text": text})

    return {
        "short_name": short_name,
        "authority": authority,
        "effective": effective,
        "articles": articles,
    }


def _is_preamble_fragment(text: str) -> bool:
    """判断文本是否像「制定本解释/制定本规定」这类被错误放入 name 的序言片段。"""
    return (
        ("制定本解释" in text)
        or ("制定本规定" in text)
        or ("本解释" in text)
        or ("本规定" in text)
        or text.rstrip().endswith("。")
    )


def _looks_garbled(text: str) -> bool:
    """判断名称是否不可用：含乱码（U+FFFD）或由清一色 '?' 组成（源数据损坏）。"""
    t = text.strip()
    if not t:
        return True
    if "�" in t:
        return True
    # 去掉空格后若只剩 '?'，视为损坏
    if t.replace("?", "").replace(" ", "") == "":
        return True
    return False


def _is_bad_name(text: str) -> bool:
    """判断候选司法解释名是否为「脏名」而应被跳过（继续取下一个字段）。

    命中以下任一模式即视为脏名（这些模式都是源数据集损坏或被错误放入 name 的
    序言片段，并非合法司法解释名）：

    * 书名号（全角《》）失衡：含 ``》`` 但 ``《`` 计数 != ``》`` 计数，或名称以
      ``》`` / ``〉`` 结尾；
    * 半角/全角书名号混用：``〉`` 与 ``》`` 同时存在；
    * 含 ``》《``：多部法律引用拼接，不是单一名称；
    * 含序言尾巴：``解释如下`` / ``若干问题的规定``（合法，已排除）/ ``的规定，就`` /
      ``根据……的规定`` 这类；
    * 名称等于已知机关名（全名），机关名被误当成司法解释名。

    注意：像 ``关于适用《中华人民共和国民法典》合同编通则若干问题的解释`` 这种
    《》成对且不在结尾、不含上述脏模式的名称必须保留为合法（不要把合法名称误杀）。
    """
    t = text.strip()
    if not t:
        return True
    # 书名号（全角《》）失衡
    if "》" in t:
        if t.count("《") != t.count("》"):
            return True
        if t.endswith("》") or t.endswith("〉"):
            return True
    # 半角/全角书名号混用（〉 与 》 同时存在）
    if "〉" in t and "》" in t:
        return True
    # 多部法律引用拼接（不是单一名称）
    if "》《" in t:
        return True
    # 序言尾巴
    if "解释如下" in t:
        return True
    if "的规定，就" in t:
        return True
    if "根据" in t and "的规定" in t:
        return True
    # 机关名（全名）被误当名称
    if t in _AUTHORITY_NAMES:
        return True
    return False


# 已知机关名（全名）。司法解释名不应等于这些；命中则视为脏名。
_AUTHORITY_NAMES: frozenset = frozenset(
    {
        "中华人民共和国最高人民法院",
        "最高人民法院",
        "最高人民检察院",
        "全国人民代表大会",
        "全国人民代表大会常务委员会",
        "全国人大常委会",
        "国务院",
    }
)


def _resolve_interp_name(data: Dict[str, Any], fallback_stem: str) -> str:
    """解析司法解释名称：优先使用已知覆盖（override），再依次选取干净字段
    title → name → short_name。

    跳过含乱码（U+FFFD 或清一色 '?'）、像序言片段、或属于「脏名」的字段；
    全部不可用时退化为文件名主干（保证唯一且不污染图谱）。
    """
    # 已知脏名/损坏名 → 直接返回正确中文名（覆盖 A、B 两组所有已知脏名，
    # 优先于任何字段，确保这些源文件稳定产出正确名称）。
    if fallback_stem in INTERP_NAME_OVERRIDES:
        return INTERP_NAME_OVERRIDES[fallback_stem]
    for key in ("title", "name", "short_name"):
        val = data.get(key)
        if (
            isinstance(val, str)
            and val.strip()
            and not _looks_garbled(val)
            and not _is_preamble_fragment(val)
            and not _is_bad_name(val)
        ):
            return val.strip()
    return fallback_stem


def parse_interpretation(path: Path) -> Optional[Dict[str, Any]]:
    """防御式解析单条司法解释（兼容 A/B 两种字段风格）。"""
    data = load_json(path)
    if not isinstance(data, dict):
        return None

    name = _resolve_interp_name(data, path.stem)
    if not name:
        return None

    issuing = data.get("authority") or data.get("issued_by") or ""
    date = data.get("promulgated") or data.get("publish_date") or ""
    doc_number = data.get("document_number") or ""

    articles: List[Dict[str, Any]] = []
    for raw in data.get("articles", []) or []:
        if not isinstance(raw, dict):
            continue
        aid = raw.get("id")
        if aid is None:
            aid = raw.get("article_id")
        if aid is None:
            continue
        text = raw.get("text")
        if text is None:
            text = raw.get("content")
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        cleaned, _ = clean_trailer(text)
        articles.append({"id": str(aid), "text": cleaned})

    return {
        "name": name,
        "issuing": issuing,
        "date": date,
        "doc_number": doc_number,
        "articles": articles,
    }


# ---------------------------------------------------------------------------
# 章节分组（核心算法）
# ---------------------------------------------------------------------------

def group_chapters(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把扁平法条按行尾预告标记分组为章节。

    规则：当前条文先归入「当前章」（初始标题为 INITIAL_CHAPTER_TITLE）；
    若该条文末尾匹配到预告标记，则该标记成为「下一章」标题，后续条文进入新章
    （当前条文仍属旧章）。返回非空章节列表。
    """
    chapters: List[Dict[str, Any]] = []
    current_title = INITIAL_CHAPTER_TITLE
    current: Dict[str, Any] = {
        "标题": current_title,
        "层级": chapter_level(current_title),
        "articles": [],
    }
    chapters.append(current)
    pending_next_title: Optional[str] = None

    for art in articles:
        if pending_next_title is not None:
            current_title = pending_next_title
            current = {
                "标题": current_title,
                "层级": chapter_level(current_title),
                "articles": [],
            }
            chapters.append(current)
            pending_next_title = None
        cleaned, trailer = clean_trailer(art["text"])
        if trailer is not None:
            pending_next_title = trailer
        current["articles"].append({"条号": art["id"], "内容": cleaned})

    # 丢弃空章节（理论上不会发生，仅作防御）
    return [c for c in chapters if c["articles"]]


# ---------------------------------------------------------------------------
# 三个目标文件构建
# ---------------------------------------------------------------------------

def _add_authority(
    nodes: List[Dict[str, str]],
    seen: set,
    name: str,
) -> None:
    """追加机关节点（去重），层级取自映射，未知留空。"""
    if not name or name in seen:
        return
    seen.add(name)
    nodes.append({"name": name, "层级": AUTHORITY_LEVEL_MAP.get(name, "")})


def build_seed(
    source_dir: Path,
    law_files: List[str],
    existing_seed: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """构建 legal_seed.json 内容并合并现有 causes/interpetations/authorities。"""
    regulations: List[Dict[str, Any]] = []
    interp_nodes: List[Dict[str, str]] = []
    interp_seen: set = set()
    authority_nodes: List[Dict[str, str]] = []
    authority_seen: set = set()

    law_stats: List[Dict[str, Any]] = []

    # ---- 法规 ----
    for fn in law_files:
        law_path = source_dir / "laws" / fn
        law = parse_law(law_path)
        short = law["short_name"]
        chapters = group_chapters(law["articles"])

        # 先收集本法规全部条号，用于引用抽取过滤（仅保留确实存在的条号）
        valid_nos = {str(a["条号"]) for c in chapters for a in c["articles"]}

        reg_chapters: List[Dict[str, Any]] = []
        cited_article_count = 0
        cite_edge_count = 0
        for c in chapters:
            chapter_articles: List[Dict[str, Any]] = []
            for a in c["articles"]:
                cited = extract_citations(a["内容"], valid_nos, str(a["条号"]))
                if cited:
                    cited_article_count += 1
                    cite_edge_count += len(cited)
                chapter_articles.append(
                    {
                        "条号": a["条号"],
                        "内容": a["内容"],
                        "关键词": [],
                        "引用": cited,
                        "案由": [],
                        "司法解释": [],
                    }
                )
            reg_chapters.append(
                {
                    "标题": c["标题"],
                    "层级": c["层级"],
                    "articles": chapter_articles,
                }
            )

        reg = {
            "name": short,
            "生效日期": law["effective"],
            "发文字号": DOC_NUMBER_MAP.get(short, ""),
            "效力层级": "法律",
            "制定机关": law["authority"],
            "上位法": "",  # 全部留空，避免错误的法律位阶断言
            "chapters": reg_chapters,
        }
        regulations.append(reg)
        _add_authority(authority_nodes, authority_seen, law["authority"])
        law_stats.append(
            {
                "name": short,
                "file": fn,
                "article_count": len(law["articles"]),
                "chapter_count": len(chapters),
                "articles_with_citation": cited_article_count,
                "citation_edges": cite_edge_count,
            }
        )

    # ---- 司法解释（全部）----
    interp_dir = source_dir / "interpretations"
    interp_stats = {"files": 0, "nodes": 0, "articles": 0}
    if interp_dir.is_dir():
        for f in sorted(interp_dir.glob("*.json")):
            interp = parse_interpretation(f)
            if interp is None:
                continue
            interp_stats["files"] += 1
            interp_stats["articles"] += len(interp["articles"])
            if interp["name"] not in interp_seen:
                interp_seen.add(interp["name"])
                interp_nodes.append(
                    {
                        "name": interp["name"],
                        "发布机关": interp["issuing"],
                        "日期": interp["date"],
                        "文号": interp["doc_number"],
                    }
                )
                interp_stats["nodes"] += 1
            _add_authority(authority_nodes, authority_seen, interp["issuing"])

    # ---- 合并现有数据 ----
    existing_causes = existing_seed.get("causes", []) or []
    existing_interps = existing_seed.get("interpretations", []) or []
    existing_auths = existing_seed.get("authorities", []) or []

    # causes：保留现有，不新增
    causes = [c for c in existing_causes]

    # interpretations：现有 + 新（按 name 去重）
    merged_interps: List[Dict[str, Any]] = []
    seen_i: set = set()
    for it in existing_interps:
        nm = it.get("name") or it.get("名称")
        if nm and nm not in seen_i:
            seen_i.add(nm)
            merged_interps.append(it)
    for it in interp_nodes:
        if it["name"] not in seen_i:
            seen_i.add(it["name"])
            merged_interps.append(it)

    # authorities：现有 + 新（按 name 去重）
    merged_auths: List[Dict[str, Any]] = []
    seen_a: set = set()
    for au in existing_auths:
        nm = au.get("name") or au.get("名称")
        if nm and nm not in seen_a:
            seen_a.add(nm)
            merged_auths.append(au)
    for au in authority_nodes:
        if au["name"] not in seen_a:
            seen_a.add(au["name"])
            merged_auths.append(au)

    seed = {
        "regulations": regulations,
        "causes": causes,
        "interpretations": merged_interps,
        "authorities": merged_auths,
    }

    # 引用抽取总体统计（跨 4 部法规汇总）
    total_articles = sum(
        len(ch["articles"]) for reg in regulations for ch in reg["chapters"]
    )
    articles_with_citation = sum(
        1
        for reg in regulations
        for ch in reg["chapters"]
        for a in ch["articles"]
        if a.get("引用")
    )
    total_citation_edges = sum(
        len(a.get("引用", []) or [])
        for reg in regulations
        for ch in reg["chapters"]
        for a in ch["articles"]
    )

    stats = {
        "law_stats": law_stats,
        "interp_stats": interp_stats,
        "authority_count": len(merged_auths),
        "cause_count": len(causes),
        "interp_node_count": len(merged_interps),
        "citation_stats": {
            "total_articles": total_articles,
            "articles_with_citation": articles_with_citation,
            "total_citation_edges": total_citation_edges,
        },
    }
    return seed, stats


def build_lightrag_corpus(
    source_dir: Path,
    law_files: List[str],
) -> List[Dict[str, str]]:
    """构建 LightRAG 语料列表（4 部法规全文 + 全部司法解释条文）。"""
    corpus: List[Dict[str, str]] = []

    # 法规条文
    for fn in law_files:
        law = parse_law(source_dir / "laws" / fn)
        short = law["short_name"]
        for art in law["articles"]:
            cleaned, _ = clean_trailer(art["text"])
            corpus.append(
                {
                    "title": f"《{short}》第{art['id']}条",
                    "content": cleaned,
                    "法规": short,
                    "条号": art["id"],
                }
            )

    # 司法解释条文
    interp_dir = source_dir / "interpretations"
    if interp_dir.is_dir():
        for f in sorted(interp_dir.glob("*.json")):
            interp = parse_interpretation(f)
            if interp is None:
                continue
            for art in interp["articles"]:
                corpus.append(
                    {
                        "title": f"《{interp['name']}》第{art['id']}条",
                        "content": art["text"],
                        "法规": interp["name"],
                        "条号": art["id"],
                    }
                )

    return corpus


def build_txt_corpus(
    source_dir: Path,
    law_files: List[str],
) -> str:
    """构建 kb 向量库语料文本（一条一区块，块间空行分隔）。"""
    blocks: List[str] = []

    for fn in law_files:
        law = parse_law(source_dir / "laws" / fn)
        short = law["short_name"]
        for art in law["articles"]:
            cleaned, _ = clean_trailer(art["text"])
            blocks.append(f"# 《{short}》第{art['id']}条\n{cleaned}")

    interp_dir = source_dir / "interpretations"
    if interp_dir.is_dir():
        for f in sorted(interp_dir.glob("*.json")):
            interp = parse_interpretation(f)
            if interp is None:
                continue
            for art in interp["articles"]:
                blocks.append(f"# 《{interp['name']}》第{art['id']}条\n{art['text']}")

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def self_check(
    seed: Dict[str, Any],
    corpus: List[Dict[str, str]],
    txt: str,
    source_dir: Path,
    law_files: List[str],
) -> Tuple[bool, List[str]]:
    """执行 IS_PASS 自检，返回 (通过?, 问题列表)。"""
    problems: List[str] = []

    # 1) UTF-8 / 占位符检查在调用方写入后做；此处检查内容层面
    # 2) 法条计数与源一致
    expected_law_articles = 0
    for fn in law_files:
        law = parse_law(source_dir / "laws" / fn)
        expected_law_articles += len(law["articles"])

    seed_law_articles = sum(
        len(ch.get("articles", []) or [])
        for reg in seed.get("regulations", []) or []
        for ch in reg.get("chapters", []) or []
    )
    if seed_law_articles != expected_law_articles:
        problems.append(
            f"seed 法规条文数 {seed_law_articles} != 源 {expected_law_articles}"
        )

    corpus_law_articles = sum(
        1 for item in corpus if item.get("法规") in _short_names(source_dir, law_files)
    )
    if corpus_law_articles != expected_law_articles:
        problems.append(
            f"corpus 法规条目数 {corpus_law_articles} != 源 {expected_law_articles}"
        )

    # 3) 残余占位符
    if "【占位】" in json.dumps(seed, ensure_ascii=False) or "【占位】" in json.dumps(
        corpus, ensure_ascii=False
    ):
        problems.append("seed/corpus 中仍含【占位】")
    if "【占位】" in txt:
        problems.append("txt 中仍含【占位】")

    return (len(problems) == 0), problems


def _short_names(source_dir: Path, law_files: List[str]) -> set:
    names = set()
    for fn in law_files:
        try:
            names.add(parse_law(source_dir / "laws" / fn)["short_name"])
        except Exception:
            pass
    return names


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 legal-cn-core-codices 数据集转换为 My Agentix 法律域三文件。"
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("D:/lawtmp/legal-cn-core-codices"),
        help="legal-cn-core-codices 数据集根目录",
    )
    parser.add_argument(
        "--out-seed",
        type=Path,
        default=Path("data/legal/legal_seed.json"),
        help="legal_seed.json 输出路径",
    )
    parser.add_argument(
        "--out-corpus",
        type=Path,
        default=Path("data/legal/legal_lightrag_corpus.json"),
        help="legal_lightrag_corpus.json 输出路径",
    )
    parser.add_argument(
        "--out-txt",
        type=Path,
        default=Path("data/kb/legal_corpus.txt"),
        help="legal_corpus.txt 输出路径",
    )
    parser.add_argument(
        "--laws",
        nargs="*",
        default=DEFAULT_LAW_FILES,
        help="纳入的法规文件名列表（相对 laws/）",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="不备份原文件",
    )
    args = parser.parse_args()

    # 解析现有 seed（用于合并）：始终以「原始模板」为合并基准。
    # 优先读取 .bak（首轮运行前生成的备份，含有用户已有数据）；
    # 若 .bak 不存在则读取当前 out-seed（首轮运行场景）。
    # 这样重复运行不会产生「已合并数据再次合并」的累积问题（幂等）。
    bak_of_seed = args.out_seed.with_suffix(args.out_seed.suffix + ".bak")
    existing_seed: Dict[str, Any] = {}
    if bak_of_seed.exists():
        existing_seed = load_json(bak_of_seed) or {}
    elif args.out_seed.exists():
        existing_seed = load_json(args.out_seed) or {}

    # ---- 构建 ----
    seed, stats = build_seed(args.source_dir, args.laws, existing_seed)
    corpus = build_lightrag_corpus(args.source_dir, args.laws)
    txt = build_txt_corpus(args.source_dir, args.laws)

    # ---- 备份 + 写入 ----
    # 仅当 .bak 尚不存在时才备份，确保「原始模板」只被保留一次，
    # 不会被后续生成的产物覆盖（避免合并基准被污染）。
    backups = []
    if not args.no_backup:
        for p in (args.out_seed, args.out_corpus, args.out_txt):
            bak = p.with_suffix(p.suffix + ".bak")
            if not bak.exists() and p.exists():
                shutil.copy2(p, bak)
                backups.append(str(bak))

    args.out_seed.parent.mkdir(parents=True, exist_ok=True)
    args.out_corpus.parent.mkdir(parents=True, exist_ok=True)
    args.out_txt.parent.mkdir(parents=True, exist_ok=True)

    dump_json(args.out_seed, seed)
    dump_json(args.out_corpus, corpus)
    args.out_txt.write_text(txt, encoding="utf-8")

    # ---- 写入后再自检（UTF-8 / 占位符 / 计数）----
    pass_utf8 = True
    for p, is_json in (
        (args.out_seed, True),
        (args.out_corpus, True),
        (args.out_txt, False),
    ):
        try:
            if is_json:
                json.loads(p.read_text(encoding="utf-8"))
            else:
                p.read_text(encoding="utf-8")
        except Exception as exc:
            pass_utf8 = False
            print(f"[自检] UTF-8 读取失败 {p}: {exc}", file=sys.stderr)

    passed, problems = self_check(seed, corpus, txt, args.source_dir, args.laws)
    if not pass_utf8:
        passed = False
        problems.append("存在 UTF-8 非法文件")

    # ---- 报告 ----
    print("=" * 60)
    print("数据集转换完成报告")
    print("=" * 60)
    print("\n【备份文件】")
    for b in backups:
        print(f"  - {b}")

    print("\n【各部法规】")
    total_law_articles = 0
    for ls in stats["law_stats"]:
        print(
            f"  - {ls['name']}（{ls['file']}）："
            f"条文 {ls['article_count']} 条，章节 {ls['chapter_count']} 章"
        )
        total_law_articles += ls["article_count"]

    is_stats = stats["interp_stats"]
    print("\n【司法解释】")
    print(
        f"  - 文件 {is_stats['files']} 个，解析出节点 "
        f"{is_stats['nodes']} 个，条文 {is_stats['articles']} 条"
    )

    cs = stats["citation_stats"]
    print("\n【引用边抽取（规则化，不依赖 LLM）】")
    print(f"  - 法条总数：{cs['total_articles']}")
    print(
        f"  - 获得 ≥1 条引用的法条：{cs['articles_with_citation']} "
        f"（占比 {cs['articles_with_citation'] / max(cs['total_articles'], 1) * 100:.1f}%）"
    )
    print(f"  - 引用边总数（rel_article_cites_article）：{cs['total_citation_edges']}")
    for ls in stats["law_stats"]:
        print(
            f"    · {ls['name']}：{ls['articles_with_citation']} 条有引用，"
            f"{ls['citation_edges']} 条边"
        )

    print("\n【图谱本体节点】")
    print(f"  - 法规节点：{len(seed['regulations'])}")
    print(f"  - 案由节点：{stats['cause_count']}（保留现有，未新增）")
    print(f"  - 司法解释节点：{stats['interp_node_count']}")
    print(f"  - 机关节点：{stats['authority_count']}")

    print("\n【输出文件】")
    for p in (args.out_seed, args.out_corpus, args.out_txt):
        size = p.stat().st_size if p.exists() else 0
        print(f"  - {p}  （{size:,} 字节）")

    print("\n【法条计数校验】")
    print(f"  - 源法规条文总数：{total_law_articles}")
    print(f"  - seed 法规条文数：{sum(len(ch['articles']) for r in seed['regulations'] for ch in r['chapters'])}")
    print(f"  - corpus 总条目数：{len(corpus)}（法规 {total_law_articles} + 司法解释 {is_stats['articles']}）")
    print(f"  - txt 区块数：{txt.count('# 《')}")

    print("\n【自检】")
    if passed:
        print("  IS_PASS: YES")
    else:
        print("  IS_PASS: NO")
        for prob in problems:
            print(f"    - {prob}")

    if not passed:
        raise SystemExit("自检未通过，请检查上述问题。")


if __name__ == "__main__":
    main()
