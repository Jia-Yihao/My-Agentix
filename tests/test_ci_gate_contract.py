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

import ast
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












def test_applicable_metrics_are_stricter_or_equal_than_diluted():
    """空真条目存在时，适用口径必须 ≤ 全量口径（否则说明口径写反了）。

    2026-10-26：金标集下线，原断言读的是真实 golden。口径本身与数据集无关，改用
    3 行内联数据（含 1 条无引用要求的空真条目）即可覆盖。
    """
    golden = [
        {"id": "G1", "question": "q1", "route_type": "kb-query", "expected_sources": ["《示例集》第1条"],
         "must_contain": [], "expect_reject": False},
        {"id": "G2", "question": "q2", "route_type": "kb-query", "expected_sources": ["《示例集》第2条"],
         "must_contain": [], "expect_reject": False},
        {"id": "G3", "question": "q3", "route_type": "general-query", "expected_sources": [],
         "must_contain": [], "expect_reject": False},
    ]
    s = run_eval.evaluate(golden, [])["summary"]

    assert s["citation_free_hit_count"] > 0, "存在无引用要求的空真条目"
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


def test_missing_baseline_file_is_loud_and_blocks_by_default(tmp_path, monkeypatch, capsys):
    """基线文件不存在：默认**不放行**——"没把关"不许被读成"把关通过"。

    旧口径是 rc==0 + 建档（首跑友好），代价是 CI 里"删掉基线文件"这条路只要不被
    workflow 的 skip 判定拦住，就一路绿灯。2026-10-26 起改为默认硬失败，
    首跑建档退成一个必须显式声明的动作（`--allow-bootstrap`，见下一条）。
    """
    rc = _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(tmp_path / "nope.json"),
            "--metric", "route_accuracy", "--current", "0.9",
        ],
    )
    out = capsys.readouterr().out
    assert rc != 0, "缺基线 = 门禁未武装，必须阻断"
    assert "::error" in out
    assert "NOT-ARMED" in out


def test_missing_baseline_file_can_still_bootstrap_behind_the_flag(
    tmp_path, monkeypatch, capsys
):
    """`--allow-bootstrap` 保留首跑建档语义：不阻断、但照样声明未生效。"""
    rc = _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(tmp_path / "nope.json"),
            "--metric", "route_accuracy", "--current", "0.9",
            "--allow-bootstrap",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT-ENFORCED" in out
    assert "::warning" in out


def test_missing_key_blocks_and_leaves_the_committed_baseline_untouched(
    tmp_path, monkeypatch, capsys
):
    """缺键 ⇒ 硬失败，且**不写文件**：CI 悄悄改写 committed 基线是最坏的一种假绿。"""
    baseline = tmp_path / "quality.json"
    original = json.dumps({"other_metric": 0.5})
    baseline.write_text(original, encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.9"],
    )
    out = capsys.readouterr().out
    assert rc != 0
    assert "NOT-ARMED" in out
    assert baseline.read_text(encoding="utf-8") == original, "拒绝的一刻起不许改动基线文件"


def test_empty_baseline_dict_is_also_loud(tmp_path, monkeypatch, capsys):
    """空基线（``{}``）里每个指标都算"键不存在" ⇒ 与缺键同一形态：响亮且阻断。

    仓内那份 committed 质量基线曾由 ``evals/write_quality_baseline.py`` 从真实录制的
    summary 生成、不再为空（该文件已随评测集于 2026-10-26 删除）；
    本用例守的是**生成物退化成空档**——旧口径下它会"每跑必绿"且日志无迹可查。
    """
    baseline = tmp_path / "quality.json"
    baseline.write_text("{}", encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        ["--baseline-file", str(baseline), "--metric", "route_accuracy", "--current", "0.9"],
    )
    out = capsys.readouterr().out
    assert rc != 0, "空基线不是「通过」，是「什么都没比对」"
    assert "NOT-ARMED" in out
    assert json.loads(baseline.read_text(encoding="utf-8")) == {}, "默认不许改写基线"


def test_empty_baseline_bootstrap_is_still_available_behind_the_flag(tmp_path, monkeypatch):
    """显式选择首跑建档时，写入行为照旧可用（脚本与本地录制仍要这条路）。"""
    baseline = tmp_path / "quality.json"
    baseline.write_text("{}", encoding="utf-8")

    rc = _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(baseline),
            "--metric", "route_accuracy", "--current", "0.9",
            "--allow-bootstrap",
        ],
    )
    assert rc == 0
    assert json.loads(baseline.read_text(encoding="utf-8"))["route_accuracy"] == 0.9


def test_step_summary_written_on_not_armed(tmp_path, monkeypatch):
    """默认（硬失败）路径也要往 step summary 写一行：CI 页面上看得见"没比对"。"""
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
    assert "NOT-ARMED" in text


def test_step_summary_written_on_not_enforced(tmp_path, monkeypatch):
    """显式首跑建档（``--allow-bootstrap``）时，step summary 仍写 NOT-ENFORCED。"""
    summary_file = tmp_path / "summary.md"
    monkeypatch.setenv(quality_gate.STEP_SUMMARY_ENV, str(summary_file))

    _run_gate(
        monkeypatch,
        [
            "--baseline-file", str(tmp_path / "nope.json"),
            "--metric", "route_accuracy", "--current", "0.9",
            "--allow-bootstrap",
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


def test_perf_gate_report_contract_branch_has_no_zero_fallback():
    """★ 表 B G9：「报告契约破了 ⇒ 判红」分支不许被写回 `or 0` 式兜底。

    **为什么只能靠结构断言**：这段 Python 活在 workflow 的 ``run:`` 字符串里，
    本仓不把它 import 进来跑，判据只能是"形状"层面的。本条钉的是**回归面**——
    一旦 ``report.get("errors") or 0`` 这种兜底被写回，缺失会静默退化成 0/None，
    ``_is_count`` 判据根本没生效，而既有断言（比对语义、阻断形态）照样全绿；
    它**不是**在验证当前行为正确性（那是 workflow 内 ``_is_count`` 自己的事）。

    四条合取：① ``_is_count`` 的定义与消费点（逐字 ``if not _is_count(value)``）
    同时在场——只断言"名字出现过"会被注释里的引用糊过去；② ``errors`` / ``success``
    / ``requests`` 三枚键全在 ``invalid_counts`` 列表推导的取值面里，缺一即红；
    ②′（第 2 轮返工）``errors`` / ``success`` / ``requests_total`` 三行赋值逐字钉
    **无默认参**裸形态——``report.get("errors", 0)`` 这类写回不含 ``or 0``，子串判据
    够不着，必须靠逐字在场断言封死；③ 执行面（非注释行）不出现 ``or 0``；④ 契约破的
    红必须有自己的文案
    （「报告契约破了」）与自己的失败行标记（``_failed_row(slug, "报告字段缺失：``），
    与「真有失败样本」那种红分得开。

    实测偏离（落笔时点）：``perf-baseline.yml:281`` 的**注释**逐字保留着历史形态
    ``report.get("errors") or 0`` 作警示，故第 ③ 条按"非注释行"取判据——注释是叙述、
    不在判据作用面；该收窄不损判别力（证伪自检：代码行写回兜底必红）。
    """
    text = _workflow_text(PERF_PATH)
    compare_block = text.split("Compare with previous baseline")[-1]

    # ① 判据单源在用：定义与消费点并存。
    assert "def _is_count" in compare_block, (
        "`_is_count` 的定义不在了——判据没有单源，消费点形同引用空气"
    )
    assert "if not _is_count(value)" in compare_block, (
        "`_is_count` 的消费点（逐字 `if not _is_count(value)`）不在——判据没被任何执行语句用上"
    )

    # ② 三枚键全过闸：invalid_counts 列表推导的取值面逐枚在场。
    for entry in ('("errors", errors)', '("success", success)', '("requests", requests_total)'):
        assert entry in compare_block, f"报告键 {entry} 不在 _is_count 闸的取值面里"

    # ②'（WD-2 第 2 轮返工，G9 严重项）三行赋值必须逐字钉现树**无默认参**形态。
    # 只排子串 `or 0` 封不住 `report.get("errors", 0)` 这种**默认参数**写回——它不含
    # `or 0`，四条合取全部原样通过，而缺失已被默认参静默成 0，判据照样失效。
    for assign in (
        'errors = report.get("errors")',
        'success = report.get("success")',
        'requests_total = report.get("requests")',
    ):
        assert assign in compare_block, (
            f"赋值行 {assign!r} 不在场——取值行被改写（含加默认参兜底）即红，G9 回归面"
        )

    # ③ `or 0` 兜底不回写执行面（:281 的警示注释不计，见 docstring）。
    fallback_lines = [
        line
        for line in compare_block.splitlines()
        if "or 0" in line and not line.lstrip().startswith("#")
    ]
    assert not fallback_lines, (
        f"`or 0` 兜底被写回执行面（G9 点名的回归形态），判据将静默失效：{fallback_lines}"
    )

    # ④ 两种红分得开：契约破有自己的文案与失败行标记，不与样本失败共文案。
    assert "报告契约破了" in compare_block, (
        "契约破分支必须显式写明「报告契约破了」，否则与「真有失败样本」那种红不可分辨"
    )
    assert '_failed_row(slug, "报告字段缺失：' in compare_block, (
        "契约破分支必须产出专属失败行（_failed_row(slug, \"报告字段缺失：…））"
    )


# ---------------------------------------------------------------------------
# 6. 门禁的两个输入文件必须是"真武装"状态（2026-10-26 基线录制后收紧）
# ---------------------------------------------------------------------------






def test_workflow_bootstrap_posture_is_explicit_where_it_is_needed() -> None:
    """perf 门禁的"首跑无基线"是常态（缓存可能被清），必须**显式**声明建档而不是靠默认放行。

    默认值翻成硬失败之后，这条守的是：谁在 quality_gate 里改默认，不会连带把
    perf-baseline 的 nightly 首跑打成一堆假红。
    """
    text = _workflow_text(PERF_PATH)
    assert "--allow-bootstrap" in text, (
        "perf-baseline 的首跑建档必须显式声明；缺了这一行，缓存未命中时该 job 会硬失败"
    )




# ---------------------------------------------------------------------------
# 5. 「谁来阻断性能冒烟」必须与 ci.yml 的实际形状同源
# ---------------------------------------------------------------------------

PERF_SMOKE_TEST = PROJECT_ROOT / "tests" / "test_perf_smoke.py"


def _marker_expressions(path: Path) -> list[str]:
    """AST 取模块级 ``pytestmark`` 赋值与所有装饰器的源码形状。

    不用"文件文本里没出现过 ``integration``"当判据：注释里写过一次就永远读成"标了"，
    而真标了 marker 也可能因为写在别处（如 ``conftest`` 的 ``addini``）读成"没标"。
    判据必须与 pytest 实际选择用例的那个结构同源（AGENTS.md 纪律 6 ④）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            found.append(ast.unparse(node.value))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.extend(ast.unparse(dec) for dec in node.decorator_list)
    return found


def test_perf_smoke_blocks_through_the_test_job_it_actually_runs_in() -> None:
    """性能冒烟的阻断力来自 ``test`` job，而不是一个"独立性能冒烟 job"——文档只能这么写。

    **为什么需要这条**：``docs/migration/PhaseH_高性能运行时优化规划.md`` 与 CHANGELOG 的 W7 条
    都记着"perf-smoke 摘 ``continue-on-error`` 转阻塞（job 改名「性能冒烟」）"。后来 ``.github/``
    整目录在快照里缺失、按契约重建成 8 个 job（CHANGELOG 的 M·CI 重建条自己记着这个数），那个 job
    **没有被恢复**。于是没有任何一步变红：测试照跑（被 ``test`` job 收走）、超预算照样阻断汇总，
    但三份文档指向的 job 名在 CI 里根本找不到——按图找的那一格是空的。这正是纪律 6 ④ 说的形态：
    **诊断（文档）口径与执行链判定不同源，两边都"看起来正常"**。

    四条**合取**，任一断掉就说明 §2 E5 那句是假的：① ``test`` job 真跑 ``pytest -m "not integration"``
    （选择器决定谁被收进来）；② ``tests/test_perf_smoke.py`` 没有 integration 标记（AST 扫）；
    ③ ``ci-gate`` 的 ``needs`` 含 ``test``（红能传到汇总 job 才叫阻断）；④ ``ci.yml`` 里确实**没有**
    独立 perf job——第 ④ 条刻意"过严"：有人恢复那个 job 时它必须红一次，逼着把措辞一起改回来。
    """
    text = _workflow_text(CI_PATH)

    # ① test job：锚点带行首缩进（"  test:" 在 needs 列表里不会出现，但 strategy/matrix 里
    # 有 python-version 之类的行，切片必须落在 job 定义上而不是别处）。
    assert "\n  test:\n" in text, "ci.yml 里没有 `test` job——本守卫的作用面消失了，请同步 §2 E5"
    test_job = text.split("\n  test:\n", 1)[1].split("\n  architecture-guards:\n", 1)[0]
    assert test_job.startswith("    name: Unit tests"), "切片没落在 test job 上（防空真）"
    assert 'pytest -m "not integration"' in test_job, (
        "test job 不再用 `not integration` 选择器 ⇒ 『性能冒烟被 test job 收走』这句措辞失效，"
        "要么它跑了全量（选择器变了），要么它根本不跑 perf smoke（命令变了）"
    )

    # ② perf smoke 文件确实被那个选择器收进来：没有任何 integration marker。
    markers = _marker_expressions(PERF_SMOKE_TEST)
    blocked = [m for m in markers if "integration" in m]
    assert not blocked, (
        f"tests/test_perf_smoke.py 带上了 integration 标记（{blocked}）⇒ CI 的 test job 不再跑它，"
        "性能冒烟只剩夜间 workflow，§2 E5 的『阻塞』措辞必须改写"
    )

    # ③ ci-gate 的 needs 含 test。
    assert "\n  ci-gate:\n" in text, "ci.yml 里没有 ci-gate 汇总 job"
    gate_block = text.split("\n  ci-gate:\n", 1)[1]
    needs = re.findall(r"^\s+- ([\w-]+)\s*$", gate_block.split("needs:")[1].split("if:")[0], re.M)
    assert (
        "test" in needs
    ), f"ci-gate 的 needs 不含 test（当前 {needs}）⇒ 性能冒烟红传不到汇总，阻断是假的"

    # ④ 没有那个"文档里存在、CI 里不存在"的独立 job。
    for ghost in ("perf-smoke", "perf_smoke", "性能冒烟"):
        assert (
            ghost not in text
        ), f"ci.yml 出现了 {ghost}：独立性能 job 被恢复了，请把 §2 E5 与本条的措辞改成按 job 阻断"
