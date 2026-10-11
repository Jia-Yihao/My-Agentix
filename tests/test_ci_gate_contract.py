"""CI 质量门禁契约测试（离线，零外部服务）。

守两件容易悄悄退化、退化后却"看起来正常"的事：

1. **口径一致性**：``.github/workflows/ci.yml`` 里质量门禁要比对的指标名，
   必须都是 ``evals/run_eval.py::evaluate()`` 真会产出的 summary 键。
   一旦脱钩，CI 的失败形态是一个 KeyError 堆栈——看起来像代码炸了，
   实际是"口径不一致"，排查成本极高。
2. **未生效的可见性**：基线缺失 / 指标首次出现时，门禁返回 0 但不做任何比对。
   这是必须的（首跑建档），但**不能静默**：必须有 ``NOT-ENFORCED`` 字样与
   step summary 输出，否则"没把关"会被读成"把关通过"。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"
CI_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_module("cg_run_eval", EVALS_DIR / "run_eval.py")
quality_gate = _load_module("cg_quality_gate", EVALS_DIR / "quality_gate.py")


def _ci_gate_metrics() -> list[str]:
    """从 ci.yml 的 heredoc 里抽出质量门禁要比对的指标名列表。"""
    text = CI_PATH.read_text(encoding="utf-8")
    match = re.search(r"METRICS\s*=\s*\(([^)]*)\)", text)
    assert match, "ci.yml 里找不到 METRICS 元组——门禁步骤写法变了，请同步本测试"
    return re.findall(r'"([^"]+)"', match.group(1))


def _summary_keys() -> set[str]:
    """对真实 golden + 空预测跑一遍 evaluate，取 summary 的键集合。"""
    golden = run_eval.load_jsonl(run_eval.DEFAULT_GOLDEN)
    return set(run_eval.evaluate(golden, [])["summary"])


# ---------------------------------------------------------------------------
# 1. 口径一致性
# ---------------------------------------------------------------------------


def test_ci_metric_list_is_not_empty():
    assert _ci_gate_metrics(), "门禁指标列表为空等于没门禁"


def test_ci_gate_metrics_all_exist_in_summary():
    """CI 门禁引用的每个指标都必须是 evaluate() 真会产出的键。"""
    unknown = [m for m in _ci_gate_metrics() if m not in _summary_keys()]
    assert not unknown, (
        f"ci.yml 门禁引用了 summary 里不存在的指标 {unknown}——"
        f"CI 会以 KeyError 崩溃，看起来像代码故障，实为口径脱钩"
    )


def test_ci_gate_uses_applicable_denominators():
    """门禁必须盯「真实分母」口径，不能盯被空真稀释的均值。"""
    metrics = _ci_gate_metrics()
    diluted = [
        m
        for m in ("citation_hit_rate", "citation_correctness", "must_contain", "reject_accuracy")
        if m in metrics
    ]
    assert not diluted, (
        f"门禁不得直接使用全量口径指标 {diluted}：它们的均值含空真免费满分"
        f"（citation 地板 ≈0.549、reject 地板 ≈0.8725），拦不住真实退化。"
        f"请改用同名 *_applicable 变体。"
    )
    assert "citation_hit_rate_applicable" in metrics
    assert "reject_accuracy_applicable" in metrics


def test_applicable_metrics_are_stricter_or_equal_than_diluted():
    """空真条目存在时，适用口径必须 ≤ 全量口径（否则说明口径写反了）。"""
    golden = run_eval.load_jsonl(run_eval.DEFAULT_GOLDEN)
    s = run_eval.evaluate(golden, [])["summary"]

    assert s["citation_free_hit_count"] > 0, "真实 golden 存在无引用要求条目"
    assert s["citation_hit_rate_applicable"] <= s["citation_hit_rate"]
    assert s["reject_accuracy_applicable"] <= s["reject_accuracy"]


# ---------------------------------------------------------------------------
# 2. 门禁语义与「未生效」可见性
# ---------------------------------------------------------------------------


def _run_gate(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["quality_gate.py", *argv])
    return quality_gate.main()


def test_pass_when_current_above_threshold(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "quality.json"
    baseline.write_text(json.dumps({"route_accuracy": 0.90}), encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.89"],
    )
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_fail_when_regression_exceeds_budget(tmp_path, monkeypatch, capsys):
    baseline = tmp_path / "quality.json"
    baseline.write_text(json.dumps({"route_accuracy": 0.90}), encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.80"],
    )
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_lower_is_better_direction(tmp_path, monkeypatch):
    baseline = tmp_path / "quality.json"
    baseline.write_text(json.dumps({"p95_seconds": 0.10}), encoding="utf-8")
    common = [
        "--baseline-file", str(baseline), "--metric", "p95_seconds",
        "--direction", "lower_is_better", "--max-regression", "0.20",
    ]

    assert _run_gate(monkeypatch, [*common, "--current", "0.11"]) == 0
    assert _run_gate(monkeypatch, [*common, "--current", "0.20"]) == 1


def test_missing_baseline_file_is_loud_but_not_blocking(tmp_path, monkeypatch, capsys):
    """基线文件不存在：首跑建档语义 ⇒ 不阻断，但必须显式声明「未生效」。"""
    rc = _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(tmp_path / "nope.json"),
            "--metric", "route_accuracy", "--current", "0.9",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT-ENFORCED" in out
    assert "::warning" in out


def test_empty_baseline_dict_is_also_loud(tmp_path, monkeypatch, capsys):
    """仓库里 committed 的 quality.json 就是 `{}`——每个指标都会走建档路径，
    因此必须同样打出 NOT-ENFORCED，否则门禁「每跑必绿」且日志无迹可查。"""
    baseline = tmp_path / "quality.json"
    baseline.write_text("{}", encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.9"],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT-ENFORCED" in out
    assert json.loads(baseline.read_text(encoding="utf-8"))["route_accuracy"] == 0.9


def test_step_summary_written_on_not_enforced(tmp_path, monkeypatch):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv(quality_gate.STEP_SUMMARY_ENV, str(summary_file))

    _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(tmp_path / "nope.json"),
            "--metric", "route_accuracy", "--current", "0.9",
        ],
    )
    text = summary_file.read_text(encoding="utf-8")
    assert "NOT-ENFORCED" in text or "未生效" in text


def test_step_summary_written_on_pass(tmp_path, monkeypatch):
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv(quality_gate.STEP_SUMMARY_ENV, str(summary_file))
    baseline = tmp_path / "quality.json"
    baseline.write_text(json.dumps({"route_accuracy": 0.9}), encoding="utf-8")

    _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.9"],
    )
    assert "PASS" in summary_file.read_text(encoding="utf-8")


def test_step_summary_env_absent_is_safe(tmp_path, monkeypatch):
    """本地运行没有 GITHUB_STEP_SUMMARY，不得因此报错。"""
    monkeypatch.delenv(quality_gate.STEP_SUMMARY_ENV, raising=False)
    baseline = tmp_path / "quality.json"
    baseline.write_text(json.dumps({"m": 1.0}), encoding="utf-8")

    assert _run_gate(monkeypatch, ["--baseline-file", str(baseline), "--metric", "m", "--current", "1.0"]) == 0


# ---------------------------------------------------------------------------
# 3. CI workflow 自身的健壮性
# ---------------------------------------------------------------------------


def test_ci_offline_skip_emits_warning_and_summary():
    """跳过离线评估必须是「响亮」的：warning 注解 + step summary 齐备。"""
    text = CI_PATH.read_text(encoding="utf-8")
    assert "baseline_predictions.jsonl" in text
    assert "::warning" in text, "跳过评估必须产生 warning 注解，不能只 ::notice"
    assert "GITHUB_STEP_SUMMARY" in text, "跳过评估必须写入 job summary"


def test_ci_gate_fails_loudly_on_missing_metric():
    """summary 缺指标时应以明确错误退出，而不是抛出难懂的 KeyError 堆栈。"""
    text = CI_PATH.read_text(encoding="utf-8")
    assert "::error title=quality-gate::" in text
    assert "missing" in text


# ---------------------------------------------------------------------------
# 4. workflow 引用的脚本必须真实存在（"配置齐全但其实是死的"）
# ---------------------------------------------------------------------------

WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
PERF_PATH = WORKFLOWS_DIR / "perf-baseline.yml"

#: ``python path/to/x.py`` / ``bash path/to/x.sh`` 形式的调用
_SCRIPT_REF = re.compile(r"(?:^|[\s|;&(])(?:python3?|bash|sh)\s+([\w./\\-]+\.(?:py|sh))")


def _workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workflow_script_references_all_exist():
    """★ 所有 workflow 里 `python xxx.py` 引用的脚本都必须真实存在。

    这守的是一类"最安静的失效"：workflow 文件看起来配置齐全、语法正确、
    job 名字漂亮，但引用的脚本从来没被提交过——一跑就 *No such file or
    directory*，而如果这个 job 不是默认分支的必跑项，可能几周都没人发现。
    实测命中：``perf-baseline.yml`` 引用的 ``scripts/load_test.py`` 长期缺失。
    """
    missing: list[str] = []
    for workflow in sorted(WORKFLOWS_DIR.glob("*.yml")):
        for ref in _SCRIPT_REF.findall(_workflow_text(workflow)):
            if not (PROJECT_ROOT / ref).exists():
                missing.append(f"{workflow.name} → {ref}")
    assert not missing, f"workflow 引用了不存在的脚本：{missing}"
    # 防空真：扫描器必须真的扫到过引用，否则本守卫等于没跑
    total = sum(len(_SCRIPT_REF.findall(_workflow_text(w))) for w in WORKFLOWS_DIR.glob("*.yml"))
    assert total > 0, "没扫到任何脚本引用——正则或路径变了，守卫已空转"


def test_perf_workflow_uses_pipefail_so_load_test_exit_code_survives():
    """``python ... | tee`` 会吃掉退出码；必须显式 pipefail，否则压测全失败也放行。"""
    text = _workflow_text(PERF_PATH)
    assert "set -o pipefail" in text, "缺少 pipefail：load_test 的非零退出码会被 tee 掩盖"


def test_perf_workflow_persists_its_baseline_across_runs():
    """★ 基线文件必须被持久化，否则每次运行都是"首次建档"，门禁永远不生效。

    实测命中：比对用的 ``perf-baseline.json`` 由 quality_gate 写出，但 artifact
    上传的是 ``baseline.json``，且没有任何 cache/commit 环节 —— runner 一销毁基线
    就没了，于是 nightly 每天走一遍 NOT-ENFORCED 分支，看起来"每天都跑"。
    """
    text = _workflow_text(PERF_PATH)
    assert "perf-baseline.json" in text, "门禁比对的文件名变了，请同步本测试"
    # 要么恢复上一版（cache），要么上传（artifact），二者至少要有一个覆盖它
    persists = ("restore-keys" in text and "perf-baseline" in text) or (
        "upload-artifact" in text and "perf-baseline.json" in text.split("upload-artifact")[-1]
    )
    assert persists, "perf-baseline.json 没有被任何 cache/artifact 环节持久化"


def test_perf_gate_direction_is_lower_is_better_for_latency():
    """★ p95 是"越低越好"。用默认 higher_is_better 会把方向弄反：

    阈值 = baseline × (1 - 0.20)，判据 = current ≥ 阈值 ——
    于是 **p95 劣化 50% 反而 PASS，p95 改善 50% 反而 FAIL**。
    实测命中：旧配置未传 --direction。
    """
    text = _workflow_text(PERF_PATH)
    assert "--metric p95_seconds" in text or "--metric" in text
    if "p95_seconds" in text:
        assert "--direction lower_is_better" in text, (
            "p95/latency 类指标必须以 lower_is_better 比对，否则门禁方向反了"
        )


def test_perf_gate_treats_null_p95_as_failure_not_zero():
    """零成功样本时 p95 是 null；必须显式失败，而不是把它当成 0 秒（"很快"）。"""
    text = _workflow_text(PERF_PATH)
    assert "p95_seconds" in text
    assert "::error" in text, "压测无有效样本时必须报 error 注解"
    assert "is None" in text, "必须显式判空，不能直接拿 None 去比大小"


def test_perf_gate_is_actually_enforcing():
    """头部写"阻断"就必须真阻断——被 `|| echo warning` 吞掉的失败等于不阻断。"""
    text = _workflow_text(PERF_PATH)
    compare_block = text.split("Compare with previous baseline")[-1]
    assert "|| echo" not in compare_block, (
        "比对步骤用 `|| echo warning` 吞掉了 quality_gate 的非零退出码，"
        "与文件头部声明的「阻断」不符"
    )
