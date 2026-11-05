"""README §15.2 发布数字的生成物守卫（工程约定 7 的同一口径）。

约定 7 说的是"被运维当核对基准的表必须是生成物，不是抄本"。§15.2 这张表原先
是手抄的：九个数字靠事后写脚本回查。本测试把它接到
``docs/evals/minimal-slice-summary.json``（由 ``scripts/demo/report_eval.py``
从逐题明细生成、**进仓**）上，于是：

1. 手改 README 里任何一个数字/措辞 ⇒ CI 红（``test_published_strings_appear_in_readme``）；
2. 手改汇总 JSON 的 ``published`` 列表使其与 ``metrics`` 不一致 ⇒ CI 红
   （``test_published_list_matches_metrics``，用生成器自己的函数回算）；
3. 生成器被删/改名 ⇒ CI 红（``test_generator_exists_and_schema_is_declared``）——
   防空真：守卫依赖的源码结构若消失，必须显式报警而不是静默失效。

口径边界：本测试**不**重算指标（那需要被 gitignore 的 data/demo/ 逐题明细与
2MB 语料）；它守的是"进仓汇总 ↔ README"这条边。让汇总过期（改了明细没重跑
生成器）不在此覆盖范围内——那条边由生成时的自检（eval 自带 summary 对拍）
与 `--write-summary` 使用纪律承担。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = REPO_ROOT / "docs" / "evals" / "minimal-slice-summary.json"
GENERATOR_PATH = REPO_ROOT / "scripts" / "demo" / "report_eval.py"
README_PATH = REPO_ROOT / "README.md"

# 语料指纹：§15.1 与 §15.2 必须报同一份语料，否则两组数不可比。
CORPUS_SHA = "1ca58564b1f3a58c212c0fc23e9e5da317576ce524fc58cd4f6d0ec6f3aea2e5"


@pytest.fixture(scope="module")
def summary() -> dict:
    return json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def generator():
    """按文件路径加载生成器（scripts/ 不是包，不能 import）。"""
    spec = importlib.util.spec_from_file_location("report_eval_under_test", GENERATOR_PATH)
    assert spec and spec.loader, "生成器无法加载——路径/文件被移动了"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_exists_and_schema_is_declared(summary):
    assert GENERATOR_PATH.exists(), "生成器必须存在（README §15.2 的数字由它产出）"
    assert summary["schema"] == 1
    assert summary["generator"] == "scripts/demo/report_eval.py"
    assert summary["source_eval"].startswith("data/demo/"), (
        "逐题明细属本地产物（data/demo/ 已 gitignore），汇总 JSON 才是进仓基准"
    )


def test_corpus_fingerprint_matches_readme(summary):
    assert summary["corpus_sha256"] == CORPUS_SHA, "汇总与 §15.1 必须是同一份语料"
    assert CORPUS_SHA[:16] in README_PATH.read_text(encoding="utf-8")


def test_published_list_matches_metrics(summary, generator):
    """生成的 published 列表必须能由 metrics 回算出来（挡住手改列表）。"""
    recomputed = generator.published_strings(summary["metrics"])
    assert [(label, text) for label, text in recomputed] == [
        (item["label"], item["string"]) for item in summary["published"]
    ], "published 与 metrics 不一致——请重跑 scripts/demo/report_eval.py"


SECTION_HEADING = "### 15.2"


def _section(text: str, heading: str) -> str:
    """取 ``heading`` 到下一个 ``### `` 之间的正文（含 ``#### `` 子节）。"""
    start = text.index(heading)
    rest = text[start:]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def test_published_strings_must_appear_in_the_15_2_section(summary):
    """**锚到 §15.2 小节**，不是"README 里某处出现过"。

    这不是洁癖：整文件 ``in`` 判定会被别处的同名数字满足——实测过，把表里的
    ``0.8696`` 改成 ``0.8697`` 而 §15/§16 里还有一处 ``0.8696`` 时，整文件判定
    依然通过，守卫就是假的。锚定小节之后，改表里的数必红。
    """
    readme = README_PATH.read_text(encoding="utf-8")
    section = _section(readme, SECTION_HEADING)

    # 防空真：小节必须真的是小节（取值失败/命名漂移要让本测试报警，不是静默换范围）
    assert "Recall@5" in section, "§15.2 小节里找不到指标表——小节名或结构漂了"
    assert 500 < len(section) < len(readme), "小节范围异常（要么空、要么吃掉了整份 README）"

    missing = [
        f"{item['label']}: {item['string']!r}"
        for item in summary["published"]
        if item["string"] not in section
    ]
    assert not missing, (
        "§15.2 与进仓汇总不一致（改数字必须重跑 scripts/demo/report_eval.py）：\n"
        + "\n".join(missing)
    )


def test_cross_stack_identity_is_recorded(summary):
    """跨栈结论（逐题位次/集合 0 差异）必须有进仓证据，不能只写在散文里。"""
    cross = summary.get("cross_stack")
    assert cross, "缺少跨栈对照块"
    assert cross["rank_diffs"] == 0 and cross["set_diffs"] == 0
    assert cross["shared_rows"] == summary["metrics"]["n"], "对照必须覆盖全部样本"
    assert cross["retrieval_p50_ratio_text"] in README_PATH.read_text(encoding="utf-8")


def test_coverage_gap_ceiling_is_recorded(summary):
    """覆盖缺口必须显式记录（golds + rows），否则天花板 18/23 无从复核。"""
    metrics = summary["metrics"]
    assert len(metrics["coverage_gap_golds"]) == len(metrics["coverage_gap_rows"])
    assert metrics["strict_ceiling_hits"] == metrics["n"] - len(metrics["coverage_gap_rows"])
    assert metrics["citation_only_hits"] == metrics["silver_hits"] - metrics["strict_hits"]