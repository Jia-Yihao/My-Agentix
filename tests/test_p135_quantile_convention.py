"""分位数口径统一守卫。

本文件钉的是**形态**，不是数值——数值口径是 ``evals/perf_stats.nearest_rank_quantile``
那唯一一把尺子（最近邻秩 ``sorted[round(q*(n-1))]``）。四条例径：

1. **不许有副本**：全仓（``agentix`` / ``evals`` / ``scripts`` / ``kb_ingest``）任何模块
   都不许在自己函数体里"就地算一个秩下标再去取已排序序列的第 i 个"——AST 扫描，
   唯一实现所在文件除外。新增一份内联实现 ⇒ 本用例红。
2. **名字像分位数的函数必须委托**：``_pct`` / ``percentile`` / ``_percentiles`` /
   ``_percentile`` 这类名字的实现，要么就是唯一实现本身，要么函数体里必须
   **调用** ``nearest_rank_quantile``（留一份"看起来在算其实没委托"的壳也判红）。
   确有别的语义的（bootstrap CI）必须进 ``NON_QUANTILE_RANK_HOLDERS`` 处置表署名登记，
   新增登记 = 本文件改动 = 强制 reviewer 看一眼。
3. **默认重复数 ≥5**：``evals/performance/runner.py`` 的 ``--iterations`` 与
   ``run_benchmark`` 的缺省都不得回落到 5 以下（重复 <5 的样本没有可发布的尾部）。
4. **口径切换是写在外面的**：唯一实现的文件里必须同时出现切换日期与
   "历史读数不可直接比"的警示——不得静默换尺。

另附变异可红：手工造一份内联副本喂给同一个检测函数，它必须报警。

验收口径按分位数与积分口径两条钉住。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 扫描范围：评测/脚本/生产包都要过这把尺子（只读源码，不改它们）
SCAN_ROOTS = ("agentix", "evals", "scripts", "kb_ingest")

#: 唯一实现所在文件（相对仓库根）
SHARED_IMPL = "evals/perf_stats.py"

#: 秩函数（把连续分位点折成整数下标用的那几个）
RANK_FUNCS = {"round", "int", "ceil", "floor", "quantiles"}

#: 名字长得像分位数实现的函数（必须委托唯一实现，或进处置表）
QUANTILE_NAME_RE = re.compile(r"(^_?pct$|percentile|quantile)", re.IGNORECASE)

#: 口径切换日期（与唯一实现、规划文档一致）
SWITCH_DATE = "2026-10-27"

#: **处置表**：名字命中 ``QUANTILE_NAME_RE`` 但语义不是"延迟/样本分位数"的在册点。
#: 每一项都必须写理由；新增在册点等于放宽本守卫，reviewer 必须看到。
NON_QUANTILE_RANK_HOLDERS: dict = {
    "evals/run_eval.py::_bootstrap_percentile_ci":
        "percentile bootstrap 的 95% CI：下标取自重采样次数 n_resamples 与 alpha，"
        "不是样本长度定标的秩；与延迟分位数不是同一个判定，不参与单源口径",
}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_rank_arith(
    node: ast.AST, sorted_names: Set[str], len_names: Set[str]
) -> bool:
    """表达式是不是"按长度把分位点折成下标"的秩算式。

    三个条件同时成立才算：① 里面有一个秩函数调用；② 有一次乘/除做标度；
    ③ 长度来源明确——直接 ``len(...)``，或引用了本函数里由 ``sorted()`` / ``len()``
    绑定的名字。少了 ③ 就会把 ``round(a / b, 2)`` 这类普通比例误判成分位数。
    """
    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    if not any(_call_name(c) in RANK_FUNCS for c in calls):
        return False
    bins = [n for n in ast.walk(node) if isinstance(n, ast.BinOp)]
    if not any(isinstance(b.op, (ast.Mult, ast.Div)) for b in bins):
        return False
    if any(_call_name(c) == "len" for c in calls):
        return True
    refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    return bool(refs & (sorted_names | len_names))


def _delegates(fn: ast.AST) -> bool:
    """函数体里是否显式调用了唯一实现（含 ``perf_stats.nearest_rank_quantile``）。"""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) == "nearest_rank_quantile":
            return True
    return False


def find_inline_quantiles(source: str) -> List[str]:
    """检测源码里的**内联分位数实现**（副本），返回 ``函数名@行号`` 列表。

    判据：同一函数内既有 ``x = sorted(...)`` 的绑定，又有对 ``x`` 的下标访问，
    且下标是秩算式（直接写，或先赋给一个局部变量再用作下标）。这正是本仓历史上
    四份副本共同的形态。判据刻意要"两个证据都在同一个函数里"，否则就会误伤
    bootstrap CI 那类"下标不是样本长度定标"的写法。
    """
    tree = ast.parse(source)
    offenders: List[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        sorted_names: Set[str] = set()
        len_names: Set[str] = set()
        rank_names: Set[str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or node.value is None:
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Call) and _call_name(node.value) == "sorted":
                    sorted_names.add(target.id)
                elif isinstance(node.value, ast.Call) and _call_name(node.value) == "len":
                    len_names.add(target.id)
                elif _is_rank_arith(node.value, sorted_names, len_names):
                    rank_names.add(target.id)
        if not sorted_names:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Subscript):
                continue
            base = node.value
            if not (isinstance(base, ast.Name) and base.id in sorted_names):
                continue
            index = node.slice
            if isinstance(index, ast.Name) and index.id in rank_names:
                offenders.append(f"{fn.name}@{node.lineno}")
            elif _is_rank_arith(index, sorted_names, len_names):
                offenders.append(f"{fn.name}@{node.lineno}")
    return sorted(set(offenders))


def _iter_sources() -> List[tuple]:
    """(相对路径, 源码)。路径一律 posix 分隔——处置表的键是按它逐字对齐的。

    跳过 __pycache__ / node_modules / 隐藏目录。
    """
    found: List[tuple] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(REPO_ROOT).parts
            if "__pycache__" in parts or "node_modules" in parts:
                continue
            if any(part.startswith(".") for part in parts):
                continue
            try:
                found.append(
                    (
                        path.relative_to(REPO_ROOT).as_posix(),
                        path.read_text(encoding="utf-8-sig"),
                    )
                )
            except (OSError, UnicodeDecodeError):  # noqa: PERF203 - 读不到就跳过，不误报
                continue
    return found


# ---------------------------------------------------------------------------
# ① 副本检测
# ---------------------------------------------------------------------------


def test_no_inline_quantile_copy_anywhere():
    """★ 全仓只有一份分位数实现：别处再写一个秩下标就是副本，直接红。

    这是 T2 验收标准 ① 的守卫。历史四份副本（``report_eval._pct``、
    ``local_kb_demo._pct``、``runner._percentile``、``load_test.percentile``）
    加上 ``scripts/eval/mcq_report._percentiles`` 与 ``scripts/test/load_test.py``
    的内联算式都已改为委托；扫描范围含 ``agentix``，防止以后在生产包里长第六份。
    """
    offenders = []
    for rel, source in _iter_sources():
        if rel == SHARED_IMPL:
            continue
        try:
            hits = find_inline_quantiles(source)
        except SyntaxError:  # pragma: no cover - 语法都不过的文件不归本守卫管
            continue
        offenders += [f"{rel}:{h}" for h in hits]
    assert not offenders, "出现分位数副本实现（应改为 import evals.perf_stats）：" + ", ".join(
        offenders
    )


def test_mutation_inline_copy_is_caught(tmp_path):
    """★ 变异可红：手工塞一份内联分位数进脚本，守卫必须抓到（T2 验收标准 ③）。

    塞的就是本仓切换之前 ``scripts/demo/report_eval.py`` 的原始实现——
    如果这条都能漏，第 ① 条守卫等于没写。
    """
    copied = '''
def _pct(values, q):
    ordered = sorted(values)
    return ordered[round(q * (len(ordered) - 1))]
'''
    assert find_inline_quantiles(copied) == ["_pct@4"]

    # 另一形态：先把秩算式赋给局部变量，再取下标（原 local_kb_demo 的写法）
    staged = '''
def _pct(values, q):
    ordered = sorted(values)
    pos = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[pos]
'''
    assert find_inline_quantiles(staged) == ["_pct@5"]

    victim = tmp_path / "sneaky.py"
    victim.write_text(copied, encoding="utf-8")
    assert find_inline_quantiles(victim.read_text(encoding="utf-8"))


def test_delegating_wrapper_is_not_flagged():
    """委托形态不得被误伤——守卫只在"没有单一真源"时报警。"""
    wrapper = '''
def _pct(values, q):
    value = nearest_rank_quantile(values, q)
    return 0.0 if value is None else value
'''
    assert find_inline_quantiles(wrapper) == []


# ---------------------------------------------------------------------------
# ② 名字像分位数的函数必须委托（或署进处置表）
# ---------------------------------------------------------------------------


def test_quantile_named_functions_delegate_or_are_registered():
    """补第 ① 条的死角：下标在**调用方**算的那类副本（runner 的历史形态）。

    ``runner._percentile`` 收到的是已排序样本，副本判据（"同函数内有 sorted"）
    看不见它。于是再加一条按名字的判据：函数名长得像分位数实现，就必须显式调用
    唯一实现；确有其它语义的进 ``NON_QUANTILE_RANK_HOLDERS`` 写明理由。
    """
    offenders = []
    for rel, source in _iter_sources():
        if rel == SHARED_IMPL:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 语法都不过的文件不归本守卫管
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not QUANTILE_NAME_RE.search(fn.name):
                continue
            key = f"{rel}::{fn.name}"
            if key in NON_QUANTILE_RANK_HOLDERS:
                continue
            if not _delegates(fn):
                offenders.append(key)
    assert not offenders, (
        "这些名字像分位数的函数没有委托唯一实现（也没有署进处置表）："
        f"{offenders}"
    )


def test_mutation_shell_wrapper_is_caught():
    """★ 变异可红：只挂着 ``_pct`` 的名字、自己重算一遍的壳，也必须红。"""
    shell = '''
def _pct(values, q):
    ordered = sorted(values)
    n = len(ordered)
    return ordered[max(0, min(n - 1, int(round(q * n)) - 1))]
'''
    tree = ast.parse(shell)
    fn = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)][0]
    assert QUANTILE_NAME_RE.search(fn.name)
    assert not _delegates(fn)


# ---------------------------------------------------------------------------
# ③ 默认重复数 ≥5
# ---------------------------------------------------------------------------


def _find_add_argument_default(source: str, flag: str):
    """从源码里取 ``add_argument("--flag", ..., default=X)`` 的默认值字面量。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) != "add_argument":
            continue
        names = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if flag not in names:
            continue
        for kw in node.keywords:
            if kw.arg == "default":
                value = kw.value
                if isinstance(value, ast.Constant):
                    return value.value
                return value  # 非字面量：交给调用方判"不是 5 以上整数"
    return None


def test_iterations_default_at_least_five():
    """``--iterations`` 与 ``run_benchmark`` 的缺省都必须 ≥5（T2 验收标准 ②）。"""
    source = (REPO_ROOT / "evals" / "performance" / "runner.py").read_text(
        encoding="utf-8-sig"
    )
    cli_default = _find_add_argument_default(source, "--iterations")
    assert isinstance(cli_default, int) and cli_default >= 5, (
        f"--iterations 默认值必须 ≥5，实际：{cli_default!r}"
    )

    tree = ast.parse(source)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_benchmark"
    )
    benchmark_default = fn.args.defaults[0]
    assert isinstance(benchmark_default, ast.Constant) and benchmark_default.value >= 5, (
        f"run_benchmark(iterations=…) 缺省必须 ≥5，实际：{ast.dump(benchmark_default)}"
    )


# ---------------------------------------------------------------------------
# ④ 唯一实现自身：尺子与"不静默换尺"
# ---------------------------------------------------------------------------


def test_shared_impl_is_the_only_source_and_is_documented():
    """唯一实现必须存在、可导入，且把切换日期与"历史不可比"警示写在自己脸上。"""
    source = (REPO_ROOT / SHARED_IMPL).read_text(encoding="utf-8-sig")
    assert SWITCH_DATE in source, "口径切换日期没写在唯一实现里（不得静默换尺）"
    assert "不可直接比" in source, "历史读数不可直接比的警示缺失"
    assert "nearest-rank" in source or "最近邻秩" in source

    from evals.perf_stats import nearest_rank_quantile

    assert callable(nearest_rank_quantile)


@pytest.mark.parametrize(
    "values,q,expected",
    [
        # n=1：任何分位点都是它自己
        ([7.0], 0.95, 7.0),
        # README §15.2 口径：n=23 时 P95 = 第 22 小
        ([float(i) for i in range(1, 24)], 0.95, 22.0),
        # 最近邻秩不插值：偶数样本上 p50 取中间偏右那个，不是两端平均
        ([1.0, 2.0, 3.0, 4.0], 0.5, 3.0),
        # 空样本如实返回 None（"没跑成" ≠ "全都很快"）
        ([], 0.95, None),
    ],
)
def test_shared_impl_pinned_values(values, q, expected):
    from evals.perf_stats import nearest_rank_quantile

    assert nearest_rank_quantile(values, q) == expected


def test_shared_impl_rejects_percent_scale():
    """把 95 当 0.95 传进来必须炸——0–100 与 0–1 混用是本类工具最典型的静默错。"""
    from evals.perf_stats import nearest_rank_quantile

    with pytest.raises(ValueError, match="0–1"):
        nearest_rank_quantile([1.0, 2.0, 3.0], 95)
