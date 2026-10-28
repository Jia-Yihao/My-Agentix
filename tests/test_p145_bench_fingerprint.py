"""T1 bench 骨架守卫：每份结果必含指纹块（规划文档 T1 卡 · 2026-10-27）。

钉的是**形态与必需字段**，不钉任何性能读数——本卡不下任何性能结论。
验收口径出处：``docs/design/审批与中断恢复优化规划.md`` T1 卡（验收①②③）
+ 维护者基准方法学第 4/5 条（重复 ≥5、跨轮池化分位数、无环境不得伪造读数）。

覆盖面：
1. 入口产出的每份结果文件顶层含 ``fingerprint`` 块，且必需字段齐全
   （Python 版本、SDK 版本、温度、重复次数为验收②逐字要求）；
2. 分位数一律委托全仓唯一实现——bench 目录里不许长出第二份秩算式副本
   （复用 p135 的 AST 判据，把 ``bench`` 也管起来：p135 的 SCAN_ROOTS 不含
   bench，这条由本文件补位，纪律 6 ④"同一个判定不许两个源"对 bench 同样有效）；
3. 池化口径：对原始样本池做最近邻秩，**不是**各轮分位数再平均；
4. 缺环境的被测对象 ⇒ 显式 ``skipped_no_environment``，读数字段为空，不伪造；
5. 落盘器拒绝无指纹的结果（"每次测量强制落环境指纹"的执行点）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = REPO_ROOT / "bench"

#: 验收②逐字要求 + 方法学第 5 条（测量日期/环境/被测对象/尺子来源）
REQUIRED_FINGERPRINT_KEYS = {
    "measured_at",
    "target",
    "entry",
    "python_version",
    "sdk_versions",
    "temperature",
    "repeats",
    "os_platform",
    "cpu",
    "quantile_source",
    "quantile_convention",
}

#: 尺子的唯一源（与 evals/perf_stats.py 逐字对齐）
QUANTILE_SOURCE = "evals.perf_stats.nearest_rank_quantile"

#: 名字长得像分位数实现的函数必须委托唯一实现——判据直接取 p135 的
#: ``QUANTILE_NAME_RE``（见 test_bench_quantile_named_functions_delegate），
#: 本文件不留副本（纪律 6 ④：同一个判定不许两个源）。


def _load_p135():
    """按文件路径加载 p135，复用它的 AST 副本判据——不另立第二套判据。"""
    path = Path(__file__).with_name("test_p135_quantile_convention.py")
    spec = importlib.util.spec_from_file_location("t135_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(tmp_path: Path, argv: List[str]) -> int:
    from bench import run as bench_run

    return bench_run.main(["--out-dir", str(tmp_path)] + argv)


def _only_result(tmp_path: Path) -> dict:
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) == 1, f"期望恰好一份结果文件，实际：{[f.name for f in files]}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# ① 每份结果文件必含指纹块（验收①②）
# ---------------------------------------------------------------------------


def test_offline_selfcheck_result_carries_required_fingerprint(tmp_path):
    """离线自证路径：入口产出的结果 JSON 顶层 fingerprint 字段齐全。"""
    code = _run_main(tmp_path, ["--target", "selfcheck"])
    assert code == 0
    payload = _only_result(tmp_path)

    fp = payload.get("fingerprint")
    assert isinstance(fp, dict), "结果文件顶层缺 fingerprint 块（验收①）"
    missing = REQUIRED_FINGERPRINT_KEYS - set(fp)
    assert not missing, f"fingerprint 缺必需字段：{sorted(missing)}"

    assert fp["python_version"]  # 验收②：Python 版本
    assert isinstance(fp["sdk_versions"], dict) and fp["sdk_versions"], (
        "验收②：SDK 版本清单不能为空"
    )
    assert fp["sdk_versions"].get("agentix"), "agentix 自身版本必须可解析"
    assert fp["repeats"] >= 5, "方法学第 4 条：重复次数 ≥5"
    assert fp["quantile_source"] == QUANTILE_SOURCE

    # 验收②：温度。selfcheck 与被测模型无关 ⇒ 必须显式 not_applicable + 判据，
    # 不许留空、不许省略字段（省略 = 下游无法区分"没记"与"不适用"）。
    assert fp["temperature"] == "not_applicable"
    assert fp.get("temperature_basis"), "not_applicable 必须附判据（验收②）"

    # 方法学第 5 条：任何读数必须自带测量日期与环境
    assert "T" in fp["measured_at"], "measured_at 必须是 ISO8601 带时刻"
    assert fp["target"] == "selfcheck"
    assert fp["entry"]

    # selfcheck 的读数必须被明确标记为非测量，防止被当性能声明引用
    assert payload.get("data_class") == "skeleton_selfcheck_not_a_measurement"


def test_skipped_target_result_still_carries_fingerprint(tmp_path):
    """无环境的对象（load_test 未给 --url）⇒ 跳过并如实标注，但指纹块照落。

    "每份结果文件含指纹块"不允许有例外分支——跳过态也是一份被产出的结果。
    """
    code = _run_main(tmp_path, ["--target", "load_test"])
    assert code == 0
    payload = _only_result(tmp_path)

    assert isinstance(payload.get("fingerprint"), dict)
    assert REQUIRED_FINGERPRINT_KEYS <= set(payload["fingerprint"])
    assert payload["measurement_status"] == "skipped_no_environment"
    assert payload["results"]["pooled_quantiles_ms"] is None, "跳过态不得有读数"
    assert payload["results"]["per_repeat"] == [], "跳过态不得伪造样本轮次"


def test_writer_refuses_result_without_fingerprint(tmp_path):
    """执行点在落盘器上：无指纹块的结果拒绝写出——不是约定，是闸。"""
    from bench import results as bench_results

    with pytest.raises(ValueError, match="指纹"):
        bench_results.write_result({"target": "x"}, out_dir=tmp_path)

    forged = {"fingerprint": {"python_version": "3.12"}, "target": "x"}
    with pytest.raises(ValueError, match="指纹"):
        bench_results.write_result(forged, out_dir=tmp_path)


# ---------------------------------------------------------------------------
# ② 分位数单源：bench 目录不许长副本（复用 p135 判据）
# ---------------------------------------------------------------------------


def _bench_sources():
    assert BENCH_ROOT.is_dir(), "bench/ 目录不存在"
    return [
        (p.relative_to(REPO_ROOT).as_posix(), p.read_text(encoding="utf-8-sig"))
        for p in sorted(BENCH_ROOT.rglob("*.py"))
        if "__pycache__" not in p.parts
    ]


def test_bench_has_no_inline_quantile_copy():
    """p135 的 SCAN_ROOTS 不含 bench ⇒ 本条把它补上，判据仍取 p135 原函数。"""
    guard = _load_p135()
    offenders = []
    for rel, source in _bench_sources():
        try:
            hits = guard.find_inline_quantiles(source)
        except SyntaxError:  # pragma: no cover - 语法不过的文件不归本守卫管
            continue
        offenders += [f"{rel}:{h}" for h in hits]
    assert not offenders, "bench 出现分位数副本实现：" + ", ".join(offenders)


def test_bench_quantile_named_functions_delegate():
    """名字像分位数实现的 bench 函数必须显式调用唯一实现。"""
    guard = _load_p135()
    offenders = []
    for rel, source in _bench_sources():
        tree = ast.parse(source)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not guard.QUANTILE_NAME_RE.search(fn.name):
                continue
            if not guard._delegates(fn):
                offenders.append(f"{rel}::{fn.name}")
    assert not offenders, f"这些 bench 函数没有委托唯一实现：{offenders}"


# ---------------------------------------------------------------------------
# ③ 池化口径：对原始样本池做最近邻秩，不是各轮分位数再平均
# ---------------------------------------------------------------------------


def test_pooled_summary_is_nearest_rank_on_pooled_samples():
    """n=10、q=0.5：最近邻秩给 5.0（第 round(0.5*9)=4 小），插值会给 5.5。

    这个差值就是"用了哪把尺子"的判别位；读数必须钉在唯一实现的口径上。
    """
    from bench.aggregate import pooled_summary

    summary = pooled_summary([float(i) for i in range(1, 11)])
    assert summary["p50"] == 5.0, "不是最近邻秩口径（5.5 = 线性插值，全仓禁止）"
    assert summary["count"] == 10

    empty = pooled_summary([])
    assert empty["p50"] is None and empty["count"] == 0, (
        "空样本必须 None，不得伪装成 0.0（'没跑成'≠'全都很快'）"
    )


def test_low_repeats_is_a_loud_error(tmp_path):
    """重复 <5 没有可发布的尾部：入口必须响亮拒绝，而不是悄悄降样本。"""
    from bench import run as bench_run

    with pytest.raises(SystemExit) as excinfo:
        bench_run.main(["--out-dir", str(tmp_path), "--target", "selfcheck",
                        "--repeats", "2"])
    assert excinfo.value.code != 0
    assert list(tmp_path.glob("*.json")) == [], "拒绝时不得产出结果文件"
