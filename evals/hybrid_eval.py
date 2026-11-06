"""R-P2-5 混合检索归因报告（只增报告键，不动质量门禁）。

设计依据：§6 R-P2-5——「``evals`` 新增混合
检索报告（``Recall@5/MRR × {BM25, Dense, RRF, +Rerank}`` 四列归因表，10–50 问
小集即可回归）；只增报告键，不动质量门禁」。

四列口径（同一 ``--search-k``、同一问题集、同一匹配函数，唯一变量是通道）：

* ``bm25``——直接 ``sparse_search``（词法通道，独立于 hybrid 总闸）；
* ``dense``——直接 ``retrieve_docs_with_retry``（向量通道）；
* ``rrf``——``retrieve_hybrid_docs`` 融合链（强制 ``RETRIEVAL_SPARSE_ENABLED``
  参与融合，``RETRIEVAL_HYBRID_RERANK=False`` ⇒ 纯 RRF 融合序）；
* ``rerank``——同融合链且 ``RETRIEVAL_HYBRID_RERANK=True``（+Rerank 终序）。

匹配口径（silver gold）：golden 的 ``expected_sources``（如「《民法典》第148条」）
与**检索到的文档**（正文 + metadata 拼接干草）比对——法规名经
``run_eval._extract_law`` 归一后互相包含；条号统一转整数，按
``第X条`` 三形（原文/阿拉伯/中文数字，如 ``第148条`` ≡ ``第一百四十八条``）
在干草中查找。与 ``citation_hit_rate`` 同源解析器，绝不新造口径。

指标（仅对 ``expected_sources`` 非空的问题计算，空金标问题不进分母）：

* ``recall_at_5``——top-5 内任一文档命中任一金标的题占比；
* ``mrr``——首个命中位次（1 起）倒数的题均值（在返回列表内，不截 5）。

门禁边界（总纲 §6 纪律）：本脚本**独立成篇**（先例 = ``evals/memory_eval.py``），
不改 ``run_eval.evaluate()`` 既有 summary 键 / ``CSV_FIELDS`` / CI ``METRICS``
五键门 / ``quality_gate``——报告键只增、不进门禁。

用法::

    python evals/hybrid_eval.py --kb-id <KB> --golden <题集>   # 缺省题集已下线，须显式指定
    python evals/hybrid_eval.py --kb-id <KB> --limit 40 --json-out out.json
    python evals/hybrid_eval.py --help

离线（无 Milvus/PG 索引）跑不了 live 通道——``--kb-id`` 缺失/索引未就绪时
诚实返回 exit 2，不产出假表。缺省题集 ``legal_golden.jsonl`` 已于 2026-10-26
删除（评测集下线）：不给 ``--golden`` 且默认文件不存在时同样 exit 2，并列出
当前可用题集与改道示例（不静默跑空、不新建题集）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 双路径导入（沿 run_eval 先例）：
# * ``python evals/hybrid_eval.py`` 直跑：sys.path[0] = evals/；
# * pytest 以 ``evals.hybrid_eval`` 包名导入：走 ``evals.run_eval``。
try:
    from run_eval import (
        _ARTICLE_ANY_RE,
        _cn_to_int,
        _extract_article,
        _extract_law,
        _flatten_citation,
    )
except ModuleNotFoundError as _rm_exc:  # pragma: no cover - 包名路径
    if _rm_exc.name != "run_eval":
        raise
    from evals.run_eval import (  # type: ignore[no-redef]
        _ARTICLE_ANY_RE,
        _cn_to_int,
        _extract_article,
        _extract_law,
        _flatten_citation,
    )

EVALS_DIR: Path = Path(__file__).resolve().parent
DEFAULT_GOLDEN: Path = EVALS_DIR / "legal_golden.jsonl"

# T3：默认题集缺失 ⇒ 响亮退出。判据/文案与其余入口同源（evals/golden_source.py）。
try:
    from golden_source import ensure_default_golden
except ModuleNotFoundError as _gs_exc:  # pragma: no cover - 包名路径
    if _gs_exc.name != "golden_source":
        raise
    from evals.golden_source import ensure_default_golden

COLUMNS = ("bm25", "dense", "rrf", "rerank")
COLUMN_LABELS = {"bm25": "BM25", "dense": "Dense", "rrf": "RRF", "rerank": "+Rerank"}
RECALL_K = 5
DEFAULT_LIMIT = 40
DEFAULT_SEARCH_K = 20

_CN_DIGITS = "零一二三四五六七八九"
_CN_UNITS = ("", "十", "百", "千")


# ---------------------------------------------------------------------------
# 中文数字（条号三形匹配用；run_eval 只有 _cn_to_int 反方向）
# ---------------------------------------------------------------------------


def _int_to_cn(n: int) -> str:
    """整数 → 中文数字（0–9999，条号量级足够；超界退回 str(n)）。

    空位补「零」：1005→一千零五、1050→一千零五十、1105→一千一百零五；
    「一十」→「十」（条号惯例：第十五条，非 第一十五条）。
    """
    if n < 0 or n > 9999:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    parts: List[str] = []
    last_unit = 0
    remainder = n
    for unit_idx in (3, 2, 1):  # 千、百、十
        unit_value = 10**unit_idx
        digit = remainder // unit_value
        remainder %= unit_value
        if digit:
            if parts and last_unit > unit_value * 10:  # 空位（如 千→十 跳百）
                parts.append("零")
            parts.append(_CN_DIGITS[digit] + _CN_UNITS[unit_idx])
            last_unit = unit_value
    if remainder:
        if parts and last_unit >= 100:  # 个位前跳了十位
            parts.append("零")
        parts.append(_CN_DIGITS[remainder])
    text = "".join(parts)
    return text[1:] if text.startswith("一十") else text


# ---------------------------------------------------------------------------
# 匹配与排名（silver gold）
# ---------------------------------------------------------------------------


def _doc_haystack(doc: Any) -> str:
    """检索文档 → 干草堆（正文 + metadata 全部值）。"""
    parts: List[str] = [str(getattr(doc, "page_content", "") or "")]
    meta = getattr(doc, "metadata", None) or {}
    if isinstance(meta, dict):
        parts.extend(str(v) for v in meta.values() if v is not None)
    return "\n".join(parts)


def _law_in_haystack(law: str, hay: str) -> bool:
    """法规名在干草中（字符串包含或 《》 标题归一后互相包含）。"""
    if law in hay:
        return True
    for title in re.findall(r"《([^》]+)》", hay):
        norm_title = _extract_law(f"《{title}》") or title
        if law in norm_title or norm_title in law:
            return True
    return False


def doc_matches_gold(gold: Any, doc: Any) -> bool:
    """单条金标是否被单个检索文档命中（silver 匹配，口径见模块 docstring）。"""
    gold_text = _flatten_citation(gold)
    hay = _doc_haystack(doc)
    if not gold_text or not hay:
        return False

    article = _extract_article(gold_text)
    # _extract_law 为「引用 vs 引用」设计，会把条号段吞进法规名（CJK 连续串），
    # 故金标侧先剥掉 第X条 再抽法规名（干草侧走 _law_in_haystack 归一）。
    law = _extract_law(_ARTICLE_ANY_RE.sub(" ", gold_text))
    if law is None:
        # 金标无法解析法规名：退化为子串包含（与 _citation_matches 同策略）
        return gold_text in hay
    if not _law_in_haystack(law, hay):
        return False

    if article is None:
        return True  # 只约束法规名：法规名命中即算命中

    forms = {f"第{article}条"}
    art_int = _cn_to_int(article)
    if art_int is not None:
        forms.add(f"第{art_int}条")
        forms.add(f"第{_int_to_cn(art_int)}条")
    return any(form in hay for form in forms)


def first_rank(docs: Sequence[Any], golds: Sequence[Any]) -> Optional[int]:
    """首个命中位次（1 起）；未命中/空列表 → None。"""
    for idx, doc in enumerate(docs or [], start=1):
        if any(doc_matches_gold(gold, doc) for gold in golds):
            return idx
    return None


# ---------------------------------------------------------------------------
# 指标与报告（纯函数）
# ---------------------------------------------------------------------------


def build_hybrid_report(
    ranks_by_column: Dict[str, List[Optional[int]]],
    *,
    n_questions: int,
    recall_k: int = RECALL_K,
) -> Dict[str, Any]:
    """四列归因报告（纯函数；infra 失败列 = 空 rank 列表 ⇒ ``unavailable``）。

    ``ranks_by_column[col]``：该通道**实际跑出结果**的问题的首中位次列表
    （``None`` = 跑了但未命中；缺项/空列表 = 该通道整列未跑通）。
    """
    columns: Dict[str, Any] = {}
    unavailable: List[str] = []
    for col in COLUMNS:
        ranks = ranks_by_column.get(col) or []
        if not ranks:
            unavailable.append(col)
            columns[col] = {"recall_at_5": None, "mrr": None, "applicable": 0}
            continue
        ran = len(ranks)
        hits = sum(1 for r in ranks if r is not None and r <= recall_k)
        mrr = sum((1.0 / r) for r in ranks if r) / ran
        columns[col] = {
            "recall_at_5": round(hits / ran, 4),
            "mrr": round(mrr, 4),
            "applicable": ran,
        }
    return {
        "columns": columns,
        "questions": n_questions,
        "table": _render_table(columns, unavailable, recall_k),
        "unavailable": unavailable,
    }


def _render_table(
    columns: Dict[str, Any], unavailable: List[str], recall_k: int
) -> str:
    """markdown 四列归因表。"""
    lines = [
        f"| 通道 | Recall@{recall_k} | MRR | 有效问题数 |",
        "| --- | --- | --- | --- |",
    ]
    for col in COLUMNS:
        label = COLUMN_LABELS[col]
        cell = columns[col]
        if col in unavailable:
            lines.append(f"| {label} | n/a | n/a | 0 |")
        else:
            lines.append(
                f"| {label} | {cell['recall_at_5']} | {cell['mrr']} "
                f"| {cell['applicable']} |"
            )
    return "\n".join(lines)


async def run_report(
    *,
    rows: Sequence[Dict[str, Any]],
    runner: Any,
    recall_k: int = RECALL_K,
) -> Dict[str, Any]:
    """跑四通道 + 出报告。

    ``runner(question) -> dict[str, list | None]``：通道 → 排序文档列表；
    ``None`` = 该通道本题未跑通（fail-open，剔出该列分母，整列全失 =
    ``unavailable``）。仅 ``expected_sources`` 非空的问题进分母（与
    ``*_applicable`` 口径同源）。
    """
    applicable = [
        row for row in rows if (row.get("expected_sources") or [])
    ]
    ranks_by_column: Dict[str, List[Optional[int]]] = {c: [] for c in COLUMNS}
    for row in applicable:
        golds = list(row.get("expected_sources") or [])
        per_col = await runner(str(row.get("question") or ""))
        if not isinstance(per_col, dict):
            per_col = {}
        for col in COLUMNS:
            docs = per_col.get(col)
            if docs is None:
                continue  # 通道级失败：不进分母
            ranks_by_column[col].append(first_rank(docs, golds))
    return build_hybrid_report(
        ranks_by_column, n_questions=len(applicable), recall_k=recall_k
    )


# ---------------------------------------------------------------------------
# golden 装载
# ---------------------------------------------------------------------------


def load_golden_rows(
    path: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    route: str = "kb-query",
) -> List[Dict[str, Any]]:
    """装载小集：默认取 ``kb-query`` 且金标非空的前 ``limit`` 问（10–50 带）。"""
    rows: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not (row.get("expected_sources") or []):
                continue
            if route and route != "any" and row.get("route_type") != route:
                continue
            rows.append(row)
            if 0 < limit <= len(rows):
                break
    return rows


# ---------------------------------------------------------------------------
# live 通道 runner（离线环境不可用——缺 --kb-id / 索引未就绪时诚实退出）
# ---------------------------------------------------------------------------


def make_live_runner(
    *,
    kb_id: str,
    session: Any,
    vectorstore: Any,
    artifact_id: str,
    search_k: int,
) -> Any:
    """构造 live runner（懒 import，测试注入 runner 时不触达任何基础设施）。"""

    async def runner(question: str) -> Dict[str, Optional[List[Any]]]:
        from agentix.retrieval.infra.rag.sparse import sparse_search
        from agentix.retrieval.settings import settings as rs
        from agentix.retrieval.usecases.chat_service import (
            _normalize_question,
            retrieve_docs_with_retry,
        )
        from agentix.retrieval.usecases.retrieval_hybrid import (
            retrieve_hybrid_docs,
        )

        norm = _normalize_question(question)
        out: Dict[str, Optional[List[Any]]] = {}

        try:
            out["dense"] = await asyncio.to_thread(
                retrieve_docs_with_retry,
                vectorstore,
                norm,
                search_k=search_k,
                metadata_filter=None,
            )
        except Exception as exc:  # noqa: BLE001 - 单通道失败不拖垮报告
            print(f"[dense] unavailable: {exc}", file=sys.stderr)
            out["dense"] = None

        try:
            pairs = await asyncio.to_thread(
                sparse_search,
                session=session,
                kb_id=kb_id,
                query=norm,
                top_n=search_k,
                metadata_filter=None,
                min_score=float(getattr(rs, "RETRIEVAL_SPARSE_MIN_SCORE", 0.0)),
                ttl_seconds=int(getattr(rs, "RETRIEVAL_SPARSE_CACHE_TTL", 600)),
            )
            out["bm25"] = [doc for doc, _score in pairs]
        except Exception as exc:  # noqa: BLE001
            print(f"[bm25] unavailable: {exc}", file=sys.stderr)
            out["bm25"] = None

        # 融合链两变体：唯一差异 = 是否 rerank；flag 调用后立即还原
        prev_sparse = rs.RETRIEVAL_SPARSE_ENABLED
        prev_rerank = rs.RETRIEVAL_HYBRID_RERANK
        try:
            rs.RETRIEVAL_SPARSE_ENABLED = True
            for col, want_rerank in (("rrf", False), ("rerank", True)):
                rs.RETRIEVAL_HYBRID_RERANK = want_rerank
                try:
                    docs, _stage_report = await retrieve_hybrid_docs(
                        kb_id=kb_id,
                        question=question,
                        question_norm=norm,
                        search_k=search_k,
                        metadata_filter=None,
                        vectorstore=vectorstore,
                        artifact_id=artifact_id,
                        rewrite_ms=0.0,
                        session=session,
                    )
                    out[col] = docs
                except Exception as exc:  # noqa: BLE001
                    print(f"[{col}] unavailable: {exc}", file=sys.stderr)
                    out[col] = None
        finally:
            rs.RETRIEVAL_SPARSE_ENABLED = prev_sparse
            rs.RETRIEVAL_HYBRID_RERANK = prev_rerank
        return out

    return runner


async def _run_live(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    from agentix.retrieval.infra.db.session import session_scope
    from agentix.retrieval.infra.repos.index_repo import (
        get_active_index_artifact,
    )
    from agentix.retrieval.usecases.chat_service import build_retriever

    rows = load_golden_rows(
        Path(args.golden), limit=args.limit, route=args.route
    )
    if not rows:
        print("[hybrid_eval] 金标小集为空——检查 --golden/--route/--limit", file=sys.stderr)
        return None

    with session_scope() as session:
        artifact = get_active_index_artifact(session, args.kb_id)
        if not artifact:
            print(
                f"[hybrid_eval] kb_id={args.kb_id} 无 active 索引 artifact（索引未就绪）",
                file=sys.stderr,
            )
            return None
        artifact_id = getattr(artifact, "id", "") or str(
            getattr(artifact, "version", "")
        )
        try:
            _, vectorstore = build_retriever(artifact.index_path)
        except FileNotFoundError:
            print(
                f"[hybrid_eval] 索引文件不存在：{getattr(artifact, 'index_path', '?')}",
                file=sys.stderr,
            )
            return None
        runner = make_live_runner(
            kb_id=args.kb_id,
            session=session,
            vectorstore=vectorstore,
            artifact_id=artifact_id,
            search_k=args.search_k,
        )
        return await run_report(rows=rows, runner=runner)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hybrid_eval.py",
        description=(
            "R-P2-5 混合检索归因报告：Recall@5/MRR × {BM25, Dense, RRF, +Rerank} "
            "四列（只增报告键，不进门禁）"
        ),
    )
    parser.add_argument("--kb-id", default="", help="知识库 ID（live 必填）")
    parser.add_argument(
        "--golden",
        # 缺省留空 = 用 DEFAULT_GOLDEN；该文件不存在时 main() 响亮退出（T3）。
        default=None,
        help=f"金标 JSONL 路径（默认 {DEFAULT_GOLDEN.name}；该集已下线时必须显式指定）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"小集上限（默认 {DEFAULT_LIMIT}，10–50 带）",
    )
    parser.add_argument(
        "--route",
        default="kb-query",
        help="route_type 过滤（默认 kb-query；any = 不过滤）",
    )
    parser.add_argument(
        "--search-k",
        type=int,
        default=DEFAULT_SEARCH_K,
        help=f"每通道召回深度（默认 {DEFAULT_SEARCH_K}，需 ≥ {RECALL_K}）",
    )
    parser.add_argument(
        "--json-out", default="", help="报告 JSON 落盘路径（可选）"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # T3：默认题集不存在 ⇒ 响亮退出（列可用集 + 改道示例）。这条**先**跑但**不吞**
    # 既有的 --kb-id 校验：两者都是本入口的必填输入，都缺就把两条都讲清楚——
    # 只报其中一条就是在让看的人猜（tests/test_p87 钉的 "--kb-id 必须出现在 stderr"
    # 也因此照旧成立）。
    golden, golden_rc = ensure_default_golden(
        args.golden,
        entry="evals/hybrid_eval.py",
        default=DEFAULT_GOLDEN,
        example_flags="--kb-id <KB>",
    )
    if not (args.kb_id or "").strip():
        print(
            "[hybrid_eval] live 归因需要 --kb-id（无索引环境跑不了四通道；"
            "本报告不进门禁，不做降级假表）",
            file=sys.stderr,
        )
        return 2
    if golden_rc:
        return golden_rc
    args.golden = str(golden)

    if args.search_k < RECALL_K:
        print(
            f"[hybrid_eval] --search-k 需 ≥ {RECALL_K}（Recall@5 窗口）",
            file=sys.stderr,
        )
        return 2

    try:
        report = asyncio.run(_run_live(args))
    except Exception as exc:  # noqa: BLE001 - 工具脚本：诚实报错退出
        print(f"[hybrid_eval] 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if report is None:
        return 2

    print(report["table"])
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"[hybrid_eval] 报告已写入 {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
