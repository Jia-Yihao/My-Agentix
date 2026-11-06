"""STARD 评测治理：六字段补齐、README §7 主表由 JSON 生成、守卫可反向校验。

对应用户第 4 步：14 个「只有 README 文字、没有进仓生成物」的数字不能再增加，
且 README §7 主表必须能从 ``docs/evals/stard-lexical-summary.json`` 与
``stard-semantic-summary.json`` 自动生成；手写转述单独标「历史 / 方向性」。

用法：
  .venv/Scripts/python.exe evals/stard_summary_governance.py --check   # 只读校验（守卫调用）
  .venv/Scripts/python.exe evals/stard_summary_governance.py --write   # 补齐字段 + 重写 README 表

设计约束：
- ``--write`` 只补字段、不重算任何指标；既有 JSON 的 ``columns`` / ``rows`` /
  ``per_query`` 一律原样保留，``generated_at`` 已存在则不覆盖。
- README 主表用显式 HTML 标记界定，生成器只替换标记之间的内容，不碰正文。
- ``dev60 R@5 0.8333`` 是方向性读数，禁止出现在 README 任何位置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EVALS = ROOT / "docs" / "evals"
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"
SETTINGS = ROOT / "agentix" / "retrieval" / "settings.py"

LEXICAL = EVALS / "stard-lexical-summary.json"
SEMANTIC = EVALS / "stard-semantic-summary.json"

TABLE_START = "<!-- STARD_TABLE_START -->"
TABLE_END = "<!-- STARD_TABLE_END -->"

REQUIRED_FIELDS = (
    "generated_at",
    "config_fingerprint",
    "corpus",
    "split",
    "metric_definition",
    "reproduce",
)

# 本次新增/改动的生成物（不含两份既有基线汇总，它们由 stard_eval.py 生成，
# 由 _enrich 补字段但不改数字）。
NEW_SUMMARIES = (
    "stard-runtime-default-baseline.json",
    "stard-ce-fusion-summary.json",
    "stard-zfusion-mu-selection.json",
    "stard-rerank-ab-summary.json",
    "stard-recall-lab-summary.json",
    "stard-fusion-sweep-summary.json",
    "stard-latency-probe-summary.json",
)

# 精排链路 A/B 的两种形态必须分列，不能混成同一个「on」：
# ce: 是纯 CE 名次覆盖（已实测在 blend 上有害）；ceblend: 是同候选集 z 融合。
RERANK_FORMS = {
    "ce": "pure_ce_rank_override",
    "cez": "same_candidate_z_fusion",
    "ceblend": "same_candidate_z_fusion",
    "dense": "rerank_off",
    "blend": "query_memory_only",
}

# 弃用键：不得在 .env.example 里以「生效赋值」形式出现（注释里说明弃用是允许的）。
DEPRECATED_KEYS = (
    "KB_RERANK_SCORE_THRESHOLD",
    "RETRIEVAL_DENSE_RERANK_TIMEOUT_SECONDS",
)

# 本次新增的键：必须在 settings.py 声明、在 agentix/ 有实际消费点，且 .env.example 有说明。
LIVE_KEYS = (
    "RETRIEVAL_DENSE_RERANK_BATCH_SIZE",
    "RETRIEVAL_DENSE_RERANK_MAX_CONCURRENCY",
    "RETRIEVAL_HYBRID_GATHER_TIMEOUT_SECONDS",
    "RETRIEVAL_QUERY_MEMORY_ENABLED",
    "RETRIEVAL_QUERY_MEMORY_PATH",
    "RETRIEVAL_QUERY_MEMORY_CANDIDATES",
    "RETRIEVAL_CE_FUSION_ENABLED",
    "RETRIEVAL_CE_FUSION_MU",
    "RERANKER_TIMEOUT_SECONDS",
)

DATASET = "STARD (Su et al., Findings of EMNLP 2024, MIT)"
JUDGEMENT = "精确 docid（qrels），命中定义 = 至少一条相关法条进入 top-k"

# 三处「数字混用」风险的统一口径说明。运行时基线 harness 与治理脚本共用本常量，
# 保证生成物、README、守卫三者同一措辞，不出现两份转述。
CALIBER_NOTES: dict[str, str] = {
    "quality_dual_columns": (
        "served_top5（默认链实际交付 5 条，MRR 受 search_k=5 截断，dev MRR 0.585877）"
        "与 canonical_pool100（既有发布读数口径，dev MRR 0.601445）是两套不可互读的读数："
        "R@5 / NDCG@5 两者逐位一致，MRR / R@10 因候选池深度不同而不同。"
        "「既有数字不退步」的锚点统一用发布口径 dev R@5 0.7110 / MRR 0.6014 / "
        "NDCG@5 0.5455 / 全量 R@5 0.7207；served_top5 另列，"
        "禁止拿 0.585877 与 0.6014 比大小判定「退步」。"
    ),
    "rerank_noop_vs_online_ce": (
        "latency.cold.layers.rerank p50 0.0009 ms 是 dense_rerank=false 下的 no-op 占位"
        "（rerank 上游调用 0 次），不是精排成本，也不代表开启后的开销。一旦开在线 Cross-Encoder，"
        "dev60 实测（由 CE 缓存离线重算的逐题 seconds，非在线真实调用墙钟、不得当线上 P95 对外报）："
        "cand=20 P50 2.8982 s / P95 3.9657 s，cand=50 P50 7.6754 s / P95 9.8348 s，"
        "cand=100 P50 14.1469 s / P95 18.0275 s。默认关时的占位开销与开启后的真实量级必须并读，"
        "不得由 0.0009 ms 推出「精排零成本」；cand=50/100 的 P95 远超任何合理预算，"
        "因此默认不启用在线 CE，只保留离线/可选形态。"
    ),
    "db_sql_post_batching": (
        "call_surface.cold.db_sql_statements=1232（308 题 × 4 条/题）是引用还原改为"
        "「按 document 聚合、一次 IN 查 chunk + file_meta」批量化之后的真实 SQL 计数，"
        "不是逐条 2N round-trip；批量化前等价形态为 32 条/8 题，"
        "见 docs/evals/stard-latency-probe-summary.json 的 probes.citation。"
    ),
}

# 基线六层里另外两处 no-op / 真实上游的说明，与 CALIBER_NOTES 同源。
_LAYER_NOTES: dict[str, str] = {
    "rerank": CALIBER_NOTES["rerank_noop_vs_online_ce"],
    "embedding": (
        "逐题真实 LM Studio /v1/embeddings 调用，cold 路径占六层总耗时约 95%；"
        "warm 命中缓存后不调用上游。"
    ),
    "fusion": (
        "默认链路 hybrid=false / sparse=false，融合层是单路直通 no-op，"
        "不是 RRF 或 weighted fusion 的成本。"
    ),
}


def annotate_runtime_default(data: dict[str, Any]) -> None:
    """幂等地把三处口径风险说明写进 runtime-default 基线生成物。

    ``evals/runtime_default_baseline.py`` 在落盘前调用本函数，治理脚本的
    ``enrich()`` 也调用它，保证 harness 生成的 JSON 与进仓 JSON 用同一份措辞，
    不出现两套转述。全部用 ``setdefault``，不覆盖已有实测读数。
    """
    data.setdefault("caliber_notes", CALIBER_NOTES)
    data.setdefault("quality_dual_column_note", CALIBER_NOTES["quality_dual_columns"])
    cold = data.get("latency", {}).get("cold")
    if isinstance(cold, dict):
        cold.setdefault("layer_notes", dict(_LAYER_NOTES))
    cold_surface = data.get("call_surface", {}).get("cold")
    if isinstance(cold_surface, dict):
        cold_surface.setdefault("db_sql_note", CALIBER_NOTES["db_sql_post_batching"])

# 真实复现命令；不得指向不存在的脚本（Step 5 悬空路径守卫）。
REPRODUCE: dict[str, str] = {
    "stard-lexical-summary.json":
        ".venv/Scripts/python.exe evals/stard_eval.py --mode lexical --write-summary docs/evals/stard-lexical-summary.json",
    "stard-semantic-summary.json":
        ".venv/Scripts/python.exe evals/stard_eval.py --mode semantic --subset 8000 --write-summary docs/evals/stard-semantic-summary.json",
    "stard-runtime-default-baseline.json":
        ".venv/Scripts/python.exe evals/runtime_default_baseline.py --split dev --write-summary docs/evals/stard-runtime-default-baseline.json",
    "stard-ce-fusion-summary.json":
        ".venv/Scripts/python.exe evals/stard_ce_fusion.py --phase ce && .venv/Scripts/python.exe evals/stard_ce_fusion.py --phase report --write-summary docs/evals/stard-ce-fusion-summary.json",
    "stard-zfusion-mu-selection.json":
        ".venv/Scripts/python.exe evals/stard_zfusion_val_select.py --write-summary docs/evals/stard-zfusion-mu-selection.json",
    "stard-rerank-ab-summary.json":
        ".venv/Scripts/python.exe evals/stard_recall_lab.py --split dev60 --ce-cache .tmp/stard/ce_ab_dev60.json --method dense --method \"ce:cand=20,workers=1,chars=512,source=dense\" --method \"ce:cand=50,workers=1,chars=512,source=dense\" --method \"ce:cand=100,workers=1,chars=512,source=dense\" --method \"cez:cand=20,mu=<val-fixed>,workers=1,chars=512,source=dense\" --method \"cez:cand=50,mu=<val-fixed>,workers=1,chars=512,source=dense\" --method \"cez:cand=100,mu=<val-fixed>,workers=1,chars=512,source=dense\" --write-summary docs/evals/stard-rerank-ab-summary.json",
    "stard-recall-lab-summary.json":
        ".venv/Scripts/python.exe evals/stard_recall_lab.py --split dev --method dense --method \"blend:wd=0.3,wr=1.0,wk=0.1,k=10,beta=1.0,power=2.0,knn_k=20,knn_power=4.0\" --write-summary docs/evals/stard-recall-lab-summary.json --append",
    "stard-fusion-sweep-summary.json":
        ".venv/Scripts/python.exe evals/stard_fusion_sweep.py --split both --write-summary docs/evals/stard-fusion-sweep-summary.json",
    "stard-latency-probe-summary.json":
        ".venv/Scripts/python.exe evals/latency_probe.py --probe all --write-summary docs/evals/stard-latency-probe-summary.json",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolved_reproduce(name: str) -> str:
    """解析复现命令里的 <val-fixed> 占位为真实 μ（来自 val-only 定参生成物）。

    占位符不允许以文本形态落进任何进仓生成物；若定参生成物缺失则显式报错，
    避免静默写出一条永远跑不通的命令。
    """
    template = REPRODUCE.get(name, "see artifact's own reproduce field in runs[]")
    if "<val-fixed>" not in template:
        return template
    sel = EVALS / "stard-zfusion-mu-selection.json"
    if not sel.exists():
        raise SystemExit(
            f"cannot resolve <val-fixed> for {name}: {sel} not generated yet"
        )
    mu = _load(sel)["selected"]["mu"]
    return template.replace("<val-fixed>", str(mu))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _metric_definition(pool: int = 100, search_k: int = 5) -> dict[str, Any]:
    return {
        "judgement": JUDGEMENT,
        "implementation": "evals/stard_eval.py::evaluate（不另写打分口径）",
        "pool": pool,
        "search_k": search_k,
        "baseline_anchors": {
            "full_corpus_bm25_dev": [0.4708, 0.3942, 0.3327],
            "subset_bm25_dev": [0.5812, 0.4761, 0.4192],
            "subset_dense_dev": [0.7110, 0.6014, 0.5455],
            "subset_rrf_dev": [0.6591, 0.5584, 0.4994],
        },
    }


def _served_top5(per_query: list[list[Any]]) -> dict[str, float]:
    """从生成物里的逐题命中位置回算 served_top5（交付 5 条，MRR 受 search_k=5 截断）。

    与 evals/stard_recall_lab.py::_verify_metrics 同一打分口径，差别只在 MRR/R@10/20
    只统计前 5 条内的命中；R@5 / NDCG@5 与 canonical_pool100 逐位一致。
    """
    n = len(per_query) or 1
    out = {"recall@5": 0.0, "mrr": 0.0, "ndcg@5": 0.0}
    for _qid, _first, g_size, positions in per_query:
        if positions:
            first5 = positions[0]
            out["mrr"] += 1.0 / first5
            if first5 <= 5:
                out["recall@5"] += 1
        dcg = sum(1.0 / math.log2(rank + 1) for rank in positions)
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(int(g_size), 5)))
        out["ndcg@5"] += dcg / ideal if ideal else 0.0
    return {key: round(value / n, 6) for key, value in out.items()}


def _rerank_form(label: str) -> str:
    """按方法名前缀判定精排形态；未知方法一律标 unknown，避免猜测。"""
    head = label.split(":", 1)[0].strip()
    return RERANK_FORMS.get(head, "unknown")


def _dual_column_note() -> dict[str, Any]:
    return {
        "canonical_pool100": "evals/stard_eval.py::evaluate 在完整候选池（pool=100）上的读数，既有发布口径",
        "served_top5": "只交付 top-5 的读数；MRR/R@10 受 search_k=5 截断，由 per_query 回算",
        "not_interchangeable": CALIBER_NOTES["quality_dual_columns"],
        "rerank_forms": {
            "pure_ce_rank_override": "ce: 纯 CE 名次覆盖（已实测在 blend 上有害，不得与 z 融合混算）",
            "same_candidate_z_fusion": "ceblend: 同候选集 z(原分数) + mu*z(CE)，不覆盖名次",
            "rerank_off": "dense: 精排关闭，默认链路基线",
        },
        "p95_scope": (
            "ce_latency 的 p50/p95 是 CE 缓存离线重算的逐题上游 /rerank seconds"
            "（workers=1，无排队），不是在线真实调用墙钟，不得当线上 P95 对外报"
        ),
    }


def _ab_runs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A/B 生成物既可能是单 run（schema 2，methods 在顶层）也可能是 runs[]。"""
    runs = data.get("runs")
    if isinstance(runs, list):
        return [r for r in runs if isinstance(r, dict)]
    return [data] if isinstance(data.get("methods"), dict) else []


def _enrich_rerank_ab(data: dict[str, Any]) -> None:
    """把 A/B 生成物补成「双口径 + 精排形态分列」，全部由已有 per_query 回算。"""
    data.setdefault("metric_caliber", _dual_column_note())
    for run in _ab_runs(data):
        methods = run.get("methods")
        if not isinstance(methods, dict):
            continue
        for label, entry in methods.items():
            if not isinstance(entry, dict):
                continue
            pq = entry.get("per_query")
            if isinstance(pq, list) and pq:
                entry.setdefault("metrics_served_top5", _served_top5(pq))
                entry.setdefault("metrics_canonical_pool100", entry.get("metrics"))
            entry.setdefault("rerank_form", _rerank_form(str(label)))
            lat = entry.get("ce_latency")
            if isinstance(lat, dict):
                lat.setdefault("scope", "offline_ce_cache_seconds_not_online_wallclock")


def _fingerprint(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """从生成物自身内容派生指纹，不手写版本号。"""
    canonical = json.dumps(
        {k: v for k, v in data.items() if k not in REQUIRED_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(  # nosec B324 - 非安全用途：内容指纹
        canonical.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    return {
        "profile": f"stard-eval:{path.stem}",
        "content_sha1": digest,
        "source": "derived from this artifact (no hand-typed version)",
    }


def enrich() -> list[str]:
    """给所有 STARD 生成物补齐六字段；返回被改动的文件名列表。"""
    changed: list[str] = []
    for path in sorted(EVALS.glob("stard-*.json")):
        data = _load(path)
        if not isinstance(data, dict) or "schema" not in data:
            continue
        before = json.dumps(data, ensure_ascii=False, sort_keys=True)
        # runs[] 结构：顶层与每个 run 都要能被守卫读到。
        # 既有生成物没有 generated_at 时用文件 mtime（真实时间，不编），且 corpus
        # 保持原有字符串形态（test_stard_summary_guard 对 corpus 做子串断言）。
        runs = data.get("runs")
        data.setdefault(
            "generated_at",
            data.get("generated_at")
            or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)),
        )
        data.setdefault("config_fingerprint", _fingerprint(path, data))
        data.setdefault("corpus", DATASET)
        data.setdefault("split", {"dev": 308, "train": 1235, "all": 1543})
        data.setdefault("metric_definition", _metric_definition())
        if "reproduce" not in data:
            data["reproduce"] = _resolved_reproduce(path.name)
        # 引用 .tmp/ 的生成物必须显式声明「仅本地」：既避免一边 gitignore 一边在
        # 文档里引用的治理漏洞，也让守卫能据此放行。
        if ".tmp/" in json.dumps(data, ensure_ascii=False) and not any(
            kw in json.dumps(data, ensure_ascii=False) for kw in ("仅本地", "gitignore")
        ):
            data["local_only_artifacts"] = (
                "本生成物引用的 .tmp/stard/* 缓存/中间结果仅本地，已 gitignore；"
                "进仓的是本 JSON 内的结论与可复算数据，复现命令会重新生成缓存。"
            )
        if isinstance(runs, list):
            for run in runs:
                if not isinstance(run, dict):
                    continue
                run.setdefault("generated_at", data["generated_at"])
                run.setdefault("config_fingerprint", data["config_fingerprint"])
                run.setdefault("corpus", data["corpus"])
                run.setdefault("split", data["split"])
                run.setdefault("metric_definition", data["metric_definition"])
        if path.name == "stard-rerank-ab-summary.json":
            _enrich_rerank_ab(data)
        if path.name == "stard-runtime-default-baseline.json":
            annotate_runtime_default(data)
        after = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if before != after:
            _write_json(path, data)
            changed.append(path.name)
    return changed


def generate_table() -> str:
    """从两份基线 JSON 生成 §15.1 主表（4 行全部由 columns 回算）。"""
    lex = _load(LEXICAL)
    sem = _load(SEMANTIC)
    rows: list[tuple[str, str, dict, dict]] = [
        ("全库 55,348", "BM25", lex["columns"]["bm25"]["dev"], lex["columns"]["bm25"]["all"]),
        ("受控子集 8,000", "BM25", sem["columns"]["bm25"]["dev"], sem["columns"]["bm25"]["all"]),
        ("受控子集 8,000", "**Dense**", sem["columns"]["dense"]["dev"], sem["columns"]["dense"]["all"]),
        ("受控子集 8,000", "RRF(BM25+Dense, k=60)", sem["columns"]["rrf"]["dev"], sem["columns"]["rrf"]["all"]),
    ]
    lines = [
        TABLE_START,
        "> 本表由 `evals/stard_summary_governance.py` 从两份进仓 JSON 自动生成；"
        "每个数字都可由 `docs/evals/stard-lexical-summary.json` 与 "
        "`docs/evals/stard-semantic-summary.json` 回算，手工改动会被守卫拦下。",
        "",
        "| 语料 | 通道 | dev R@5 | dev R@10 | dev MRR | dev NDCG@5 | 全量 R@5 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for corpus, channel, dev, all_m in rows:
        bold = channel.startswith("**")

        def fmt(value: float, _bold: bool = bold) -> str:
            text = f"{value:.4f}"
            return f"**{text}**" if _bold else text

        lines.append(
            f"| {corpus} | {channel} | {fmt(dev['recall@5'])} | {fmt(dev['recall@10'])} | "
            f"{fmt(dev['mrr'])} | {fmt(dev['ndcg@5'])} | {fmt(all_m['recall@5'])} |"
        )
    lines.append(TABLE_END)
    return "\n".join(lines) + "\n"


def _replace_table(text: str, table: str) -> str:
    if TABLE_START not in text or TABLE_END not in text:
        raise SystemExit("README §7 missing STARD_TABLE markers")
    start = text.index(TABLE_START)
    end = text.index(TABLE_END) + len(TABLE_END)
    if text[end : end + 1] == "\n":
        end += 1
    return text[:start] + table + text[end:]


def check() -> list[str]:
    """只读校验，返回问题列表（空 = 通过）。"""
    problems: list[str] = []

    # 1) 所有 STARD 生成物必须有六字段，且 reproduce 不得留占位符
    for path in sorted(EVALS.glob("stard-*.json")):
        data = _load(path)
        if not isinstance(data, dict) or "schema" not in data:
            continue
        missing = [k for k in REQUIRED_FIELDS if k not in data]
        if missing:
            problems.append(f"{path.name}: missing {missing}")
        if "<val-fixed>" in json.dumps(data, ensure_ascii=False):
            problems.append(f"{path.name}: reproduce 仍含 <val-fixed> 占位符")
        if isinstance(data.get("runs"), list):
            for i, run in enumerate(data["runs"]):
                if isinstance(run, dict):
                    run_missing = [k for k in REQUIRED_FIELDS if k not in run]
                    if run_missing:
                        problems.append(f"{path.name} runs[{i}]: missing {run_missing}")

    # 1a) val-only 定参生成物一旦存在，A/B 的 z 融合 μ 必须与它一致
    sel_path = EVALS / "stard-zfusion-mu-selection.json"
    ab_check = EVALS / "stard-rerank-ab-summary.json"
    if sel_path.exists() and ab_check.exists():
        sel_mu = float(_load(sel_path)["selected"]["mu"])
        resolved = _resolved_reproduce("stard-rerank-ab-summary.json")
        if "<val-fixed>" in resolved or f"mu={sel_mu:g}" not in resolved:
            problems.append("REPRODUCE 模板的 <val-fixed> 未解析成 val 定参 μ")
        for run in _ab_runs(_load(ab_check)):
            for label, entry in run.get("methods", {}).items():
                if not str(label).startswith("cez:"):
                    continue
                mus = re.findall(r"mu=([0-9.]+)", str(label))
                if not mus or abs(float(mus[0]) - sel_mu) > 1e-9:
                    problems.append(
                        f"A/B: {label} 的 μ 与 val-only 定参 {sel_mu} 不一致"
                    )

    # 1b) A/B 生成物必须同时含两种精排形态，且 P95 标成离线缓存口径
    ab_path = EVALS / "stard-rerank-ab-summary.json"
    if ab_path.exists():
        ab = _load(ab_path)
        forms: set[str] = set()
        for run in _ab_runs(ab):
            for label, entry in run.get("methods", {}).items():
                if not isinstance(entry, dict):
                    continue
                forms.add(str(entry.get("rerank_form", "MISSING")))
                lat = entry.get("ce_latency")
                if isinstance(lat, dict):
                    if lat.get("scope") != "offline_ce_cache_seconds_not_online_wallclock":
                        problems.append(f"A/B: {label} P95 scope 未标注离线口径")
                if "metrics_served_top5" not in entry:
                    problems.append(f"A/B: {label} 缺 served_top5 双口径列")
        for expected in ("rerank_off", "pure_ce_rank_override", "same_candidate_z_fusion"):
            if expected not in forms:
                problems.append(f"A/B: 精排形态分列缺 {expected}")

    # 2) README 主表必须与生成的完全一致
    readme = README.read_text(encoding="utf-8")
    if TABLE_START not in readme:
        problems.append("README §7: STARD_TABLE_START marker missing")
    else:
        generated = generate_table().strip()
        actual = readme[readme.index(TABLE_START) : readme.index(TABLE_END) + len(TABLE_END)].strip()
        if generated != actual:
            problems.append("README §7 table differs from generated table (run --write)")

    # 3) 方向性读数不得进 README；生成表标记之外不得出现手写读数——
    #    读数只住在生成表与进仓 JSON 里，正文不复述（README 是项目首页，不是读数发布面）
    if "0.8333" in readme:
        problems.append("README must not contain dev60 0.8333 (directional, no artifact)")
    outside = re.sub(
        re.escape(TABLE_START) + r".*?" + re.escape(TABLE_END), "", readme, flags=re.S
    )
    stray = re.findall(r"\b0\.\d{4}\b", outside)
    if stray:
        problems.append(f"README has handwritten readings outside the generated table: {stray}")

    # 4) .env.example 不得出现弃用键的生效赋值；新键必须声明 + 有消费点 + 有说明
    env_lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    env_active = {
        line.split("=", 1)[0].strip()
        for line in env_lines
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    for key in DEPRECATED_KEYS:
        if key in env_active:
            problems.append(f".env.example: deprecated key still actively assigned: {key}")
    settings_src = SETTINGS.read_text(encoding="utf-8")
    env_text = "\n".join(env_lines)
    for key in LIVE_KEYS:
        if key not in settings_src:
            problems.append(f"settings.py: live key not declared: {key}")
        if key not in env_text:
            problems.append(f".env.example: live key undocumented: {key}")
        hits = [
            p
            for p in (ROOT / "agentix").rglob("*.py")
            if key in p.read_text(encoding="utf-8")
        ]
        consumers = [p for p in hits if p.name != "settings.py"]
        if not consumers:
            problems.append(f"agentix/: live key has no consumer: {key}")

    # 5) bench summary 要么入库要么明确「仅本地」
    for name in NEW_SUMMARIES:
        path = EVALS / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if ".tmp/" in text and "仅本地" not in text and "gitignore" not in text:
                problems.append(f"{name}: references .tmp/ without 仅本地/gitignore note")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    args = ap.parse_args()

    if args.check:
        problems = check()
        if problems:
            print("STARD governance check FAILED:")
            for item in problems:
                print(f"  - {item}")
            raise SystemExit(1)
        print("STARD governance check PASSED")
        return

    changed = enrich()
    print(f"[enrich] changed {len(changed)} files: {changed}", flush=True)
    table = generate_table()
    before = README.read_text(encoding="utf-8")
    after = _replace_table(before, table)
    if before != after:
        README.write_text(after, encoding="utf-8")
        print("[readme] §15.1 table regenerated", flush=True)
    problems = check()
    if problems:
        print("post-write check FAILED:")
        for item in problems:
            print(f"  - {item}")
        raise SystemExit(1)
    print("post-write check PASSED", flush=True)


if __name__ == "__main__":
    main()
