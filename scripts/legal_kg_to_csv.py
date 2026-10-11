#!/usr/bin/env python3
"""Convert legal ontology seed JSON into Neo4j-admin compatible CSV files.

It reads a self-described legal seed file (``data/legal/legal_seed.json``) that
models the 法律法规本体 (法规 / 法条 / 章节 / 案由 / 司法解释 / 机关) and emits one CSV
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
from typing import Any, Dict, List, Optional

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


def _regulation_id(name: str) -> str:
    return f"reg::{name}"


def _chapter_id(reg_name: str, title: str) -> str:
    return f"chap::{reg_name}::{title}"


def _article_id(reg_name: str, article_no: str) -> str:
    return f"art::{reg_name}::{article_no}"


def _cause_id(name: str) -> str:
    return f"cause::{name}"


def _interpretation_id(name: str) -> str:
    return f"interp::{name}"


def _authority_id(name: str) -> str:
    return f"auth::{name}"


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
            _ensure_node(
                authority_registry, issuing, "auth", authorities, {"name": issuing, "层级": ""}
            )
            rel_interpretation_authority.append(
                {"start": node["id"], "end": _authority_id(issuing)}
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

        # 制定机关 (法规 -> 机关)
        issuing = reg.get("制定机关")
        if issuing:
            _ensure_node(
                authority_registry, issuing, "auth", authorities, {"name": issuing, "层级": ""}
            )
            rel_regulation_authority.append(
                {"start": reg_node["id"], "end": _authority_id(issuing)}
            )

        # 上位法 (下位法 -> 上位法)
        upper = reg.get("上位法")
        if upper and upper != reg_name:
            _ensure_node(
                authority_registry, upper, "auth", authorities, {"name": upper, "层级": ""}
            ) if False else None
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
            # 章节 -> 法规
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
                # 法条 -> 章节
                rel_article_belongs_chapter.append(
                    {"start": art_node["id"], "end": chap_node["id"]}
                )

                # 引用 (法条 -> 法条)，条号限定在同一法规内
                for cited in _as_list(article.get("引用")):
                    cited_id = _article_id(reg_name, cited) if cited in article_registry or True else cited
                    if cited_id in article_registry:
                        rel_article_cites_article.append(
                            {"start": art_node["id"], "end": cited_id}
                        )

                # 对应案由 (法条 -> 案由)
                for cause_name in _as_list(article.get("案由")):
                    cause_node = _ensure_node(
                        cause_registry, cause_name, "cause", causes,
                        {"名称": cause_name, "类别": ""},
                    )
                    rel_article_cause.append(
                        {"start": art_node["id"], "end": cause_node["id"]}
                    )

                # 有司法解释 (法条 -> 司法解释)
                for interp_name in _as_list(article.get("司法解释")):
                    interp_node = _ensure_node(
                        interpretation_registry, interp_name, "interp", interpretations,
                        {"name": interp_name, "发布机关": "", "日期": "", "文号": ""},
                    )
                    rel_article_interpretation.append(
                        {"start": art_node["id"], "end": interp_node["id"]}
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
    writers = {
        "nodes_regulation": (
            "regulation_nodes.csv",
            ["id:ID(Regulation)", "name", "生效日期", "发文字号", "效力层级"],
        ),
        "nodes_chapter": (
            "chapter_nodes.csv",
            ["id:ID(Chapter)", "标题", "层级:int", "所属法"],
        ),
        "nodes_article": (
            "article_nodes.csv",
            ["id:ID(Article)", "条号", "法规", "内容", "关键词"],
        ),
        "nodes_cause": (
            "cause_nodes.csv",
            ["id:ID(Cause)", "名称", "类别"],
        ),
        "nodes_interpretation": (
            "interpretation_nodes.csv",
            ["id:ID(JudicialInterpretation)", "name", "发布机关", "日期", "文号"],
        ),
        "nodes_authority": (
            "authority_nodes.csv",
            ["id:ID(Authority)", "name", "层级"],
        ),
        "rel_article_belongs_chapter": (
            "rel_article_belongs_chapter.csv",
            [":START_ID(Article)", ":END_ID(Chapter)"],
        ),
        "rel_chapter_belongs_regulation": (
            "rel_chapter_belongs_regulation.csv",
            [":START_ID(Chapter)", ":END_ID(Regulation)"],
        ),
        "rel_article_cites_article": (
            "rel_article_cites_article.csv",
            [":START_ID(Article)", ":END_ID(Article)"],
        ),
        "rel_article_cause": (
            "rel_article_cause.csv",
            [":START_ID(Article)", ":END_ID(Cause)"],
        ),
        "rel_article_interpretation": (
            "rel_article_interpretation.csv",
            [":START_ID(Article)", ":END_ID(JudicialInterpretation)"],
        ),
        "rel_regulation_authority": (
            "rel_regulation_authority.csv",
            [":START_ID(Regulation)", ":END_ID(Authority)"],
        ),
        "rel_interpretation_authority": (
            "rel_interpretation_authority.csv",
            [":START_ID(JudicialInterpretation)", ":END_ID(Authority)"],
        ),
        "rel_upper_law": (
            "rel_upper_law.csv",
            [":START_ID(Regulation)", ":END_ID(Regulation)"],
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
        elif header.endswith(":int"):
            key = header.split(":")[0]
        elif ":ID" in header:
            key = header.split(":")[0]
        value = row.get(key, "")
        encoded[header] = value if value is not None else ""
    return encoded


if __name__ == "__main__":
    main()
