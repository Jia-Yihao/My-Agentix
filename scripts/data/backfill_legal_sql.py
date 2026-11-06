#!/usr/bin/env python3
"""把 `data/legal/legal_seed.json`（1953 条数据条目）补灌进 Text2SQL 目标库。

为什么要有这个脚本（缺口是实测出来的，不是猜想）：
compose 给 MySQL 挂的初始化脚本只有 `data/init_mysql.sql`（建表）+
`data/insert_sample_data.sql`（**4 部法律 / 10 个条目**的抽样）。而同一份
`legal_seed.json` 既是图谱（Neo4j，1953 条）又是向量库（Milvus，4865 块）的真源，
唯独 SQL 这条路只有 10 行样本。后果直接体现在金标上：
旧题集里 L075「《民法典》一共多少条」的 `must_contain` 是
`1260` —— 那是完整种子的条数，抽样库里**物理上不可能答对**。于是 text2sql 的
`must_contain_applicable` 被环境（不是被模型）判负。

设计口径：
* **只补、不覆盖**：法律行按 `name` 命中即复用（抽样 SQL 里手工校对过的
  `effective_date` / `upper_law_id` / `doc_number` 一律保留），只把缺的章节 /
  条目 / 关系插进去。因此本脚本对"已经灌过完整数据"的库是幂等的空操作。
* 目标库走 `text2sql_database_url()` 单源（`TEXT2SQL_DATABASE_URL`，未配则回落
  `DATABASE_URL`）——和 schema 拉取、SQL 执行、dry-run 同一个库，不会出现
  "补到 A 库、查 B 库"。
* 默认 **dry-run**（只打印将要插入的行数），必须显式 `--apply` 才写库
  （约定 5：对数据的写操作要人点头）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SEED = REPO_ROOT / "data" / "legal" / "legal_seed.json"

# 关系表用 (左键, 右键) 表达，键是"业务自然键"而不是自增 id——
# id 由 executor 在库里解析，纯函数部分因此可以脱离数据库测试。
ArticleKey = Tuple[str, str]  # (法律名, 条号)


def build_records(seed: Dict[str, Any]) -> Dict[str, List[Any]]:
    """把种子 JSON 展平成"按表分组的自然键行"，去重且不依赖任何数据库状态。

    返回的每个列表都是**待插入候选**（executor 再按库里已有行过滤）。
    """
    laws: List[Dict[str, Any]] = []
    chapters: List[Dict[str, Any]] = []
    articles: List[Dict[str, Any]] = []
    citations: List[Tuple[ArticleKey, ArticleKey]] = []
    article_causes: List[Tuple[ArticleKey, str]] = []
    article_interps: List[Tuple[ArticleKey, str]] = []
    law_authorities: List[Tuple[str, str]] = []
    authorities: List[Dict[str, str]] = []
    causes: List[Dict[str, str]] = []
    interpretations: List[Dict[str, Any]] = []

    seen_law_names: set[str] = set()
    seen_chapter_keys: set[Tuple[str, str]] = set()
    seen_article_keys: set[ArticleKey] = set()

    for reg in seed.get("regulations", []) or []:
        law_name = (reg.get("name") or "").strip()
        if not law_name:
            continue
        if law_name not in seen_law_names:
            seen_law_names.add(law_name)
            laws.append(
                {
                    "name": law_name,
                    "effective_date": (reg.get("生效日期") or "").strip() or None,
                    "doc_number": (reg.get("发文字号") or "").strip() or None,
                    "level": (reg.get("效力层级") or "").strip() or None,
                    "authority": (reg.get("制定机关") or "").strip() or None,
                }
            )
        issuing = (reg.get("制定机关") or "").strip()
        if issuing:
            law_authorities.append((law_name, issuing))

        for chapter in reg.get("chapters", []) or []:
            title = (chapter.get("标题") or "").strip()
            if not title:
                continue
            chapter_key = (law_name, title)
            if chapter_key not in seen_chapter_keys:
                seen_chapter_keys.add(chapter_key)
                chapters.append(
                    {
                        "law": law_name,
                        "title": title,
                        "level": str(chapter.get("层级") or 1),
                    }
                )

            for article in chapter.get("articles", []) or []:
                article_no = str(article.get("条号", "")).strip()
                if not article_no:
                    continue
                key: ArticleKey = (law_name, article_no)
                if key in seen_article_keys:
                    continue
                seen_article_keys.add(key)
                keywords = article.get("关键词")
                if isinstance(keywords, list):
                    keywords = "；".join(str(k) for k in keywords if str(k).strip())
                articles.append(
                    {
                        "law": law_name,
                        "chapter": title,
                        "article_no": article_no,
                        "content": (article.get("内容") or "").strip(),
                        "keywords": (keywords or "").strip(),
                    }
                )
                for cited in _as_str_list(article.get("引用")):
                    citations.append((key, (law_name, cited)))
                for cause_name in _as_str_list(article.get("案由")):
                    article_causes.append((key, cause_name))
                for interp_name in _as_str_list(article.get("司法解释")):
                    article_interps.append((key, interp_name))

    for cause in seed.get("causes", []) or []:
        name = (cause.get("名称") or cause.get("name") or "").strip()
        if name:
            causes.append({"name": name, "category": (cause.get("类别") or "").strip() or None})

    for interp in seed.get("interpretations", []) or []:
        name = (interp.get("name") or interp.get("名称") or "").strip()
        if not name:
            continue
        interpretations.append(
            {
                "name": name,
                "authority": (interp.get("制定机关") or interp.get("发布机关") or "").strip() or None,
                "publish_date": (interp.get("日期") or "").strip() or None,
                "doc_number": (interp.get("文号") or "").strip() or None,
            }
        )

    seen_auth: set[str] = set()
    for auth in seed.get("authorities", []) or []:
        name = (auth.get("name") or auth.get("名称") or "").strip()
        if name and name not in seen_auth:
            seen_auth.add(name)
            authorities.append({"name": name, "level": (auth.get("层级") or "").strip() or None})
    # 法律 / 示例说明引用的机关名可能不在 authorities 清单里，也要建出来
    for law in laws:
        if law["authority"] and law["authority"] not in seen_auth:
            seen_auth.add(law["authority"])
            authorities.append({"name": law["authority"], "level": None})
    for interp in interpretations:
        if interp["authority"] and interp["authority"] not in seen_auth:
            seen_auth.add(interp["authority"])
            authorities.append({"name": interp["authority"], "level": None})

    return {
        "authorities": authorities,
        "causes": causes,
        "interpretations": interpretations,
        "laws": laws,
        "chapters": chapters,
        "articles": articles,
        "law_authorities": _dedup(law_authorities),
        "citations": _dedup(citations),
        "article_causes": _dedup(article_causes),
        "article_interps": _dedup(article_interps),
    }


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _dedup(rows: List[Any]) -> List[Any]:
    seen: set[Any] = set()
    out: List[Any] = []
    for row in rows:
        if row not in seen:
            seen.add(row)
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# executor（需要数据库；纯 planning 部分以上可离线测）
# ---------------------------------------------------------------------------


def _target_db_url() -> str:
    """Text2SQL 目标库连接串（与 schema 拉取 / SQL 执行同一个单源）。

    导入顺序垫片，理由与 tests/test_p121_baseline_knobs.py 同：两个 text2sql 包
    ``__init__`` 互为上下游，直接从深层子模块（``…target_db``）进入会撞进
    半初始化的包并抛 ``cannot import name 'create_answer_formatter_node'``。
    """
    import importlib

    import agentix.application.agents.text2sql  # noqa: F401

    target_db = importlib.import_module(
        "agentix.application.agents.kg_sub_graph.agentic_rag_agents."
        "components.text2sql.target_db"
    )
    return target_db.text2sql_database_url()


def _apply(records: Dict[str, List[Any]], *, echo: bool = False) -> Dict[str, int]:
    """按自然键解析 id 后插入缺失行；已有行一律不动（只补不覆盖）。"""
    from sqlalchemy import create_engine, text

    url = _target_db_url()
    engine = create_engine(url, future=True, echo=echo)
    inserted: Dict[str, int] = {}

    with engine.begin() as conn:
        # --- authorities -------------------------------------------------
        # 自然键 → id 的映射（写反过一次：`{id: name}` 让"已存在"永远判不出来，
        # 于是 INSERT 撞 UNIQUE 把整批事务打回——本函数内四张维度表同一方向，
        # 键是 name、值是 id）。
        auth_ids = {row[1]: row[0] for row in conn.execute(text("SELECT id, name FROM authorities"))}
        for auth in records["authorities"]:
            if auth["name"] in auth_ids:
                continue
            conn.execute(
                text("INSERT INTO authorities (name, level) VALUES (:name, :level)"),
                {"name": auth["name"], "level": auth["level"]},
            )
            auth_ids[auth["name"]] = int(
                conn.execute(
                    text("SELECT id FROM authorities WHERE name = :name"), {"name": auth["name"]}
                ).scalar()
            )
            inserted["authorities"] = inserted.get("authorities", 0) + 1

        # --- causes ------------------------------------------------------
        cause_ids = {row[1]: row[0] for row in conn.execute(text("SELECT id, name FROM causes"))}
        for cause in records["causes"]:
            if cause["name"] in cause_ids:
                continue
            conn.execute(
                text("INSERT INTO causes (name, category) VALUES (:name, :category)"),
                {"name": cause["name"], "category": cause["category"]},
            )
            cause_ids[cause["name"]] = int(
                conn.execute(
                    text("SELECT id FROM causes WHERE name = :name"), {"name": cause["name"]}
                ).scalar()
            )
            inserted["causes"] = inserted.get("causes", 0) + 1

        # --- interpretations --------------------------------------------
        interp_ids = {
            row[1]: row[0]
            for row in conn.execute(text("SELECT id, name FROM interpretations"))
        }
        for interp in records["interpretations"]:
            if interp["name"] in interp_ids:
                continue
            conn.execute(
                text(
                    "INSERT INTO interpretations (name, authority_id, publish_date, doc_number)"
                    " VALUES (:name, :authority_id, :publish_date, :doc_number)"
                ),
                {
                    "name": interp["name"],
                    "authority_id": interp["authority"] and auth_ids.get(interp["authority"]),
                    "publish_date": interp["publish_date"],
                    "doc_number": interp["doc_number"],
                },
            )
            interp_ids[interp["name"]] = int(
                conn.execute(
                    text("SELECT id FROM interpretations WHERE name = :name"),
                    {"name": interp["name"]},
                ).scalar()
            )
            inserted["interpretations"] = inserted.get("interpretations", 0) + 1

        # --- laws --------------------------------------------------------
        law_ids = {row[1]: row[0] for row in conn.execute(text("SELECT id, name FROM laws"))}
        for law in records["laws"]:
            if law["name"] in law_ids:
                continue  # 抽样 SQL 里手工校对过的元数据保留，不覆盖
            conn.execute(
                text(
                    "INSERT INTO laws (name, effective_date, doc_number, level)"
                    " VALUES (:name, :effective_date, :doc_number, :level)"
                ),
                {
                    "name": law["name"],
                    "effective_date": law["effective_date"],
                    "doc_number": law["doc_number"],
                    "level": law["level"],
                },
            )
            law_ids[law["name"]] = int(
                conn.execute(
                    text("SELECT id FROM laws WHERE name = :name"), {"name": law["name"]}
                ).scalar()
            )
            inserted["laws"] = inserted.get("laws", 0) + 1

        # --- law_authorities --------------------------------------------
        existing_la = {
            (row[0], row[1])
            for row in conn.execute(text("SELECT law_id, authority_id FROM law_authorities"))
        }
        for law_name, auth_name in records["law_authorities"]:
            pair = (law_ids.get(law_name), auth_ids.get(auth_name))
            if None in pair or pair in existing_la:
                continue
            conn.execute(
                text("INSERT INTO law_authorities (law_id, authority_id) VALUES (:a, :b)"),
                {"a": pair[0], "b": pair[1]},
            )
            existing_la.add(pair)
            inserted["law_authorities"] = inserted.get("law_authorities", 0) + 1

        # --- chapters ----------------------------------------------------
        existing_ch = {
            (row[0], row[1])
            for row in conn.execute(text("SELECT law_id, title FROM chapters"))
        }
        new_chapters = [
            {
                "law_id": law_ids[chapter["law"]],
                "title": chapter["title"],
                "level": chapter["level"],
            }
            for chapter in records["chapters"]
            if chapter["law"] in law_ids
            and (law_ids[chapter["law"]], chapter["title"]) not in existing_ch
        ]
        if new_chapters:
            conn.execute(
                text("INSERT INTO chapters (law_id, title, level) VALUES (:law_id, :title, :level)"),
                new_chapters,  # SQLAlchemy executemany：1953 行一次过去，别逐条往返
            )
            inserted["chapters"] = len(new_chapters)
        chapter_ids: Dict[Tuple[int, str], int] = {
            (row[0], row[1]): row[2]
            for row in conn.execute(text("SELECT law_id, title, id FROM chapters"))
        }

        # --- articles ----------------------------------------------------
        existing_art = {
            (row[0], row[1])
            for row in conn.execute(text("SELECT law_id, article_no FROM articles"))
        }
        new_articles = []
        for article in records["articles"]:
            law_id = law_ids.get(article["law"])
            if law_id is None or (law_id, article["article_no"]) in existing_art:
                continue
            new_articles.append(
                {
                    "law_id": law_id,
                    "chapter_id": chapter_ids.get((law_id, article["chapter"])),
                    "article_no": article["article_no"],
                    "content": article["content"],
                    "keywords": article["keywords"],
                }
            )
        if new_articles:
            conn.execute(
                text(
                    "INSERT INTO articles (law_id, chapter_id, article_no, content, keywords)"
                    " VALUES (:law_id, :chapter_id, :article_no, :content, :keywords)"
                ),
                new_articles,
            )
            inserted["articles"] = len(new_articles)
        law_name_by_id = {v: k for k, v in law_ids.items()}
        article_ids: Dict[ArticleKey, int] = {
            (law_name_by_id[row[0]], row[1]): row[2]
            for row in conn.execute(text("SELECT law_id, article_no, id FROM articles"))
            if row[0] in law_name_by_id
        }

        # --- article 关系三张桥表 ---------------------------------------
        for table, column_a, column_b, rows, ids_a, ids_b in (
            ("article_citations", "article_id", "cited_article_id", records["citations"], article_ids, article_ids),
            ("article_causes", "article_id", "cause_id", records["article_causes"], article_ids, cause_ids),
            (
                "article_interpretations",
                "article_id",
                "interpretation_id",
                records["article_interps"],
                article_ids,
                interp_ids,
            ),
        ):
            existing = {
                (row[0], row[1])
                for row in conn.execute(text(f"SELECT {column_a}, {column_b} FROM {table}"))
            }
            for left_key, right_key in rows:
                pair = (ids_a.get(left_key), ids_b.get(right_key))
                if None in pair or pair in existing:
                    continue
                conn.execute(
                    text(f"INSERT INTO {table} ({column_a}, {column_b}) VALUES (:a, :b)"),
                    {"a": pair[0], "b": pair[1]},
                )
                existing.add(pair)
                inserted[table] = inserted.get(table, 0) + 1

    return inserted


def _mask_url(url: str) -> str:
    """连接串脱敏打印：密码一律藏掉，只留驱动/主机/库名。

    为什么要打印：本脚本写的是"目标库"，而目标库在没配
    ``TEXT2SQL_DATABASE_URL`` 时会回退应用自身 ``DATABASE_URL``——
    那时它根本不是 legal_db，插入会以"表不存在"失败。让人一眼看到
    这次到底连的是哪个库，比事后猜强。
    """
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url or "(未配置)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed-json", type=Path, default=DEFAULT_SEED)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库（默认只 dry-run 打印将要插入的行数）",
    )
    parser.add_argument("--echo", action="store_true", help="打印 SQL（排障用）")
    args = parser.parse_args()

    seed = json.loads(args.seed_json.read_text(encoding="utf-8"))
    records = build_records(seed)

    print(f"seed: {args.seed_json}")
    print(f"目标库: {_mask_url(_target_db_url())}")
    for table in (
        "authorities",
        "causes",
        "interpretations",
        "laws",
        "chapters",
        "articles",
        "law_authorities",
        "citations",
        "article_causes",
        "article_interps",
    ):
        print(f"  {table:<18s} 候选 {len(records[table]):>5d} 行")

    if not args.apply:
        print("\nDRY-RUN（未写库）。确认无误后加 --apply。")
        return 0

    inserted = _apply(records, echo=args.echo)
    print("\nAPPLIED 实际插入：")
    for table, count in sorted(inserted.items()):
        print(f"  {table:<24s} {count:>5d} 行")
    if not inserted:
        print("  （库里已经是完整数据——本次为空操作）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
