#!/usr/bin/env python3
"""Convert legal ontology seed JSON into Neo4j-admin compatible CSV files.

It reads a self-described legal seed file (``data/legal/legal_seed.json``) that
models the 测试数据集本体 (数据集 / 数据条目 / 章节 / 数据分类 / 示例说明 / 机构) and emits one CSV
per node label and one CSV per relationship type, ready for
``neo4j-admin database import full``.

The converter is fully self-contained: it does NOT import anything from the
former rule-based KG package (already removed during the domain migration).
To run on the template seed (which contains placeholder text) it still
produces valid, importable CSV files.
"""

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Small helper to keep node registries deduplicated by a stable id
# ---------------------------------------------------------------------------


def _ensure_node(
    registry: Dict[str, Dict[str, Any]],
    name: str,
    prefix: str,
    collection: List[Dict[str, Any]],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the registry node for ``name``; create it if missing."""
    node = registry.get(name)
    if node is None:
        node = {"id": f"{prefix}_{len(registry) + 1}"}
        if extra:
            node.update(extra)
        registry[name] = node
        collection.append(node)
    elif extra:
        # Backfill any missing extra attributes onto the existing node.
        for key, value in extra.items():
            node.setdefault(key, value)
    return node


def _chapter_id(reg_name: str, title: str) -> str:
    return f"chap::{reg_name}::{title}"


def _article_id(reg_name: str, article_no: str) -> str:
    return f"art::{reg_name}::{article_no}"


# 2026-10-25（基线录制）：此处原有 `_regulation_id` / `_cause_id` /
# `_interpretation_id` / `_authority_id`，返回 `reg::名称` 这类**注册表键**。
# 它们只是 registry 的去重键，不是写进 CSV 的节点 id（节点 id 由 `_ensure_node`
# 生成 `reg_N`）。关系行早期误用它们当端点，导入时 Neo4j 报
# "referring to missing node auth::全国人民代表大会" —— 即文档里
# `legal_kg_to_csv.py` → `neo4j_seeded` 这条灌库链路从未真正跑通过。
# 删掉这套第二 id 方案，避免再次被当成端点使用。


def main() -> None:
    args = _parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    seed = _load_seed(args.seed_json)
    assets = _build_graph_assets(seed)
    _write_csv(output_dir, assets)
    print(f"Generated Neo4j CSV files in {output_dir}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Neo4j CSV import files from legal ontology seed JSON."
    )
    parser.add_argument(
        "--seed-json",
        type=Path,
        default=Path("data/legal/legal_seed.json"),
        help="Path to the legal ontology seed JSON file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where generated CSV files will be written.",
    )
    return parser.parse_args()


def _load_seed(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Seed file not found: {path}")
    try:
        data = __import__("json").loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        raise SystemExit(f"Failed to parse seed JSON {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit("Seed JSON top-level must be an object.")
    return data


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _build_graph_assets(seed: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    # ----- registries -----------------------------------------------------
    regulation_registry: Dict[str, Dict[str, Any]] = {}
    chapter_registry: Dict[str, Dict[str, Any]] = {}
    article_registry: Dict[str, Dict[str, Any]] = {}
    cause_registry: Dict[str, Dict[str, Any]] = {}
    interpretation_registry: Dict[str, Dict[str, Any]] = {}
    authority_registry: Dict[str, Dict[str, Any]] = {}

    regulations: List[Dict[str, Any]] = []
    chapters: List[Dict[str, Any]] = []
    articles: List[Dict[str, Any]] = []
    causes: List[Dict[str, Any]] = []
    interpretations: List[Dict[str, Any]] = []
    authorities: List[Dict[str, Any]] = []

    rel_article_belongs_chapter: List[Dict[str, Any]] = []
    rel_chapter_belongs_regulation: List[Dict[str, Any]] = []
    rel_article_cites_article: List[Dict[str, Any]] = []
    rel_article_cause: List[Dict[str, Any]] = []
    rel_article_interpretation: List[Dict[str, Any]] = []
    rel_regulation_authority: List[Dict[str, Any]] = []
    rel_interpretation_authority: List[Dict[str, Any]] = []
    rel_upper_law: List[Dict[str, Any]] = []
    # 引用是**顺序敏感的前向引用**：种子里 286 个不同引用对里有 34 个，被引条号在
    # 遍历到引用方时还没登记（同数据集内、出现在更后的章节/条目）。单遍顺解会让这
    # 34 条边静默消失（实测图谱只有 252 条引用边，而 SQL 侧 286 行齐全），所以先把
    # 声明挂起来，等节点全部登记完再统一解析。
    pending_citations: List[Tuple[str, str, str]] = []

    # ----- standalone cause / interpretation / authority lists -----------
    for cause in seed.get("causes", []) or []:
        name = cause.get("名称") or cause.get("name")
        if not name:
            continue
        _ensure_node(
            cause_registry,
            name,
            "cause",
            causes,
            {"名称": name, "类别": cause.get("类别", "")},
        )

    for interp in seed.get("interpretations", []) or []:
        name = interp.get("name") or interp.get("名称")
        if not name:
            continue
        node = _ensure_node(
            interpretation_registry,
            name,
            "interp",
            interpretations,
            {
                "name": name,
                "发布机关": interp.get("发布机关", ""),
                "日期": interp.get("日期", ""),
                "文号": interp.get("文号", ""),
            },
        )
        issuing = interp.get("制定机关") or interp.get("发布机关")
        if issuing:
            # 关系端点必须用 _ensure_node 返回的**节点 id**（`auth_N`），不能再用
            # 注册表键（`auth::名称`）：两者方案不同，neo4j-admin 批量导入会报
            # "referring to missing node"，整库导不进去。
            auth_node = _ensure_node(
                authority_registry, issuing, "auth", authorities, {"name": issuing, "层级": ""}
            )
            rel_interpretation_authority.append(
                {"start": node["id"], "end": auth_node["id"]}
            )

    for auth in seed.get("authorities", []) or []:
        name = auth.get("name") or auth.get("名称")
        if not name:
            continue
        _ensure_node(
            authority_registry, name, "auth", authorities, {"name": name, "层级": auth.get("层级", "")}
        )

    # ----- regulations (with nested chapters / articles) ------------------
    for reg in seed.get("regulations", []) or []:
        reg_name = reg.get("name")
        if not reg_name:
            continue
        reg_node = _ensure_node(
            regulation_registry,
            reg_name,
            "reg",
            regulations,
            {
                "name": reg_name,
                "生效日期": reg.get("生效日期", ""),
                "发文字号": reg.get("发文字号", ""),
                "效力层级": reg.get("效力层级", ""),
            },
        )

        # 数据来源 (数据集 -> 机构)
        issuing = reg.get("制定机关")
        if issuing:
            auth_node = _ensure_node(
                authority_registry, issuing, "auth", authorities, {"name": issuing, "层级": ""}
            )
            rel_regulation_authority.append(
                {"start": reg_node["id"], "end": auth_node["id"]}
            )

        # 上级数据集 (下级数据集 -> 上级数据集)
        upper = reg.get("上位法")
        if upper and upper != reg_name:
            upper_node = _ensure_node(
                regulation_registry, upper, "reg", regulations,
                {"name": upper, "生效日期": "", "发文字号": "", "效力层级": ""},
            )
            rel_upper_law.append({"start": reg_node["id"], "end": upper_node["id"]})

        for chapter in reg.get("chapters", []) or []:
            chap_title = chapter.get("标题")
            if not chap_title:
                continue
            chap_node = _ensure_node(
                chapter_registry,
                _chapter_id(reg_name, chap_title),
                "chap",
                chapters,
                {
                    "标题": chap_title,
                    "层级": chapter.get("层级", 1),
                    "所属法": reg_name,
                },
            )
            # 章节 -> 数据集
            rel_chapter_belongs_regulation.append(
                {"start": chap_node["id"], "end": reg_node["id"]}
            )

            for article in chapter.get("articles", []) or []:
                article_no = str(article.get("条号", "")).strip()
                if not article_no:
                    continue
                art_node = _ensure_node(
                    article_registry,
                    _article_id(reg_name, article_no),
                    "art",
                    articles,
                    {
                        "条号": article_no,
                        "法规": reg_name,
                        "内容": article.get("内容", ""),
                        "关键词": "；".join(_as_list(article.get("关键词"))),
                    },
                )
                # 数据条目 -> 章节
                rel_article_belongs_chapter.append(
                    {"start": art_node["id"], "end": chap_node["id"]}
                )

                # 引用 (数据条目 -> 数据条目)，条号限定在同一数据集内
                for cited in _as_list(article.get("引用")):
                    pending_citations.append((art_node["id"], reg_name, str(cited)))

                # 对应数据分类 (数据条目 -> 数据分类)
                for cause_name in _as_list(article.get("案由")):
                    cause_node = _ensure_node(
                        cause_registry, cause_name, "cause", causes,
                        {"名称": cause_name, "类别": ""},
                    )
                    rel_article_cause.append(
                        {"start": art_node["id"], "end": cause_node["id"]}
                    )

                # 有示例说明 (数据条目 -> 示例说明)
                for interp_name in _as_list(article.get("司法解释")):
                    interp_node = _ensure_node(
                        interpretation_registry, interp_name, "interp", interpretations,
                        {"name": interp_name, "发布机关": "", "日期": "", "文号": ""},
                    )
                    rel_article_interpretation.append(
                        {"start": art_node["id"], "end": interp_node["id"]}
                    )

    # ----- 引用统一解析（节点全部登记之后，顺序不再影响边的存在性）-----------
    seen_citation_edges: set = set()
    unresolved_citations: List[Tuple[str, str]] = []
    for start_id, reg_name, cited_no in pending_citations:
        cited_node = article_registry.get(_article_id(reg_name, cited_no))
        if cited_node is None:
            unresolved_citations.append((reg_name, cited_no))
            continue
        pair = (start_id, cited_node["id"])
        if pair in seen_citation_edges:
            continue
        seen_citation_edges.add(pair)
        rel_article_cites_article.append({"start": pair[0], "end": pair[1]})
    if unresolved_citations:
        # 引用指向本数据集中不存在的条号：边只能不建（导入器会以 missing node
        # 直接拒绝整库），但必须留痕——静默丢边正是这次 252/286 差异的成因形态。
        print(
            f"WARN: {len(unresolved_citations)} 条引用指向不存在的条目，未建边："
            f"{unresolved_citations[:5]}"
        )

    return {
        "nodes_regulation": regulations,
        "nodes_chapter": chapters,
        "nodes_article": articles,
        "nodes_cause": causes,
        "nodes_interpretation": interpretations,
        "nodes_authority": authorities,
        "rel_article_belongs_chapter": rel_article_belongs_chapter,
        "rel_chapter_belongs_regulation": rel_chapter_belongs_regulation,
        "rel_article_cites_article": rel_article_cites_article,
        "rel_article_cause": rel_article_cause,
        "rel_article_interpretation": rel_article_interpretation,
        "rel_regulation_authority": rel_regulation_authority,
        "rel_interpretation_authority": rel_interpretation_authority,
        "rel_upper_law": rel_upper_law,
    }


def _write_csv(output_dir: Path, assets: Dict[str, List[Dict[str, Any]]]) -> None:
    # Label / 关系类型必须用**中文功能性标识符**（`法条`/`章节`/`案由`…），与
    # `docs/design/测试数据域-本体Schema.md` §1.1–1.2 及运行时的 Cypher 模板一一对应。
    # 早期这里写的是英文（`Article`/`Chapter`/…），导入命令也用英文，于是
    # "导入成功、图也在、但 `MATCH (f:法条)` 永远返回空" ——graphrag 静默退化。
    # 英文括注（Regulation/Article/…）只是文档里的别名，不是 Neo4j label。
    writers = {
        "nodes_regulation": (
            "regulation_nodes.csv",
            [":ID(法规)", "name", "生效日期", "发文字号", "效力层级"],
        ),
        "nodes_chapter": (
            "chapter_nodes.csv",
            [":ID(章节)", "标题", "层级:int", "所属法"],
        ),
        "nodes_article": (
            "article_nodes.csv",
            [":ID(法条)", "条号", "法规", "内容", "关键词"],
        ),
        "nodes_cause": (
            "cause_nodes.csv",
            [":ID(案由)", "名称", "类别"],
        ),
        "nodes_interpretation": (
            "interpretation_nodes.csv",
            [":ID(司法解释)", "name", "发布机关", "日期", "文号"],
        ),
        "nodes_authority": (
            "authority_nodes.csv",
            [":ID(机关)", "name", "层级"],
        ),
        "rel_article_belongs_chapter": (
            "rel_article_belongs_chapter.csv",
            [":START_ID(法条)", ":END_ID(章节)"],
        ),
        "rel_chapter_belongs_regulation": (
            "rel_chapter_belongs_regulation.csv",
            [":START_ID(章节)", ":END_ID(法规)"],
        ),
        "rel_article_cites_article": (
            "rel_article_cites_article.csv",
            [":START_ID(法条)", ":END_ID(法条)"],
        ),
        "rel_article_cause": (
            "rel_article_cause.csv",
            [":START_ID(法条)", ":END_ID(案由)"],
        ),
        "rel_article_interpretation": (
            "rel_article_interpretation.csv",
            [":START_ID(法条)", ":END_ID(司法解释)"],
        ),
        "rel_regulation_authority": (
            "rel_regulation_authority.csv",
            [":START_ID(法规)", ":END_ID(机关)"],
        ),
        "rel_interpretation_authority": (
            "rel_interpretation_authority.csv",
            [":START_ID(司法解释)", ":END_ID(机关)"],
        ),
        "rel_upper_law": (
            "rel_upper_law.csv",
            [":START_ID(法规)", ":END_ID(法规)"],
        ),
    }

    for key, (filename, headers) in writers.items():
        rows = assets.get(key, [])
        path = output_dir / filename
        with path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(
                fp,
                fieldnames=headers,
                extrasaction="ignore",
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(_encode_row(headers, row))


def _encode_row(headers: List[str], row: Dict[str, Any]) -> Dict[str, Any]:
    encoded: Dict[str, Any] = {}
    for header in headers:
        key = header
        if header.startswith(":START_ID"):
            key = "start"
        elif header.startswith(":END_ID"):
            key = "end"
        elif header.startswith(":ID("):
            # 裸 `:ID(法条)`（不带列名）：写成 `id:ID(法条)` 时 neo4j-admin 除了
            # 元素 id，还会把这一列**再存成一个名为 id 的属性**——2201 个节点全都
            # 白背一个 `id="art_31"`。它进 `db.propertyKeys()`、进 apoc.meta.data、
            # 进而进 text2cypher 的 schema 提示，等于教模型去 `WHERE n.id = 'art_31'`
            # 这种业务上不存在的东西。去掉列名即可只留元素 id。
            key = "id"
        elif header.endswith(":int"):
            key = header.split(":")[0]
        elif ":ID" in header:
            key = header.split(":")[0]
        value = row.get(key, "")
        encoded[header] = value if value is not None else ""
    return encoded


if __name__ == "__main__":
    main()
