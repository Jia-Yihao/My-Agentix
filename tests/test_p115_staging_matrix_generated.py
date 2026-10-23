"""p115 · staging 配置矩阵改为部署物的生成物（PROJECT_SPEC §24.2 G13 的终态形态）。

G13 已经错过两次：runbook 那张「配置矩阵」是运维照着做的表，而它的 dev / staging 两列
原先靠人从 `docker-compose.yml` 和 `docker/compose.override.staging.yml` 里抄。
p109 第 6 节把「加粗声明 ↔ override」做成了断言，但那是**针对已知若干键**的补丁；
本轮把它推到底：**表的内容由部署物算出来**，脚本 `scripts/gen_staging_matrix.py` 就是那张表的
生成器与校验器（`--check` 给 CI 用）。

七层用例：
1. 生成器自身的四格口径（compose 的四种写法各自推出什么）；
2. 覆盖面：表 = 环境差异项 = override 里设了的键 ∪ 显式登记的手填行（双向相等）；
3. 一致性：`check()` 必须干净；
4. 反向验证（牙齿）：**八种**漂移形态逐一都必须被 `check()` 报出来，包括"改了部署物没改表"
   与"改了表没改部署物"两个方向，以及"多一行"时加粗与不加粗两种不同性质的问题；
5. 单元格解析的宽容度：括号里的说明、反引号、加粗都不参与等式（否则第 3 层会因为文风报假警）；
6. 可发现性：runbook 是否真说了这张表是生成物、验收清单第 5 条是否还让人眼 diff；
7. CLI：`--check` 的退出码与 UTF-8 输出（Windows GBK 控制台是这个脚本的历史坑）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts/gen_staging_matrix.py"
RUNBOOK = PROJECT_ROOT / "docs/runbooks/staging-environment.md"
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
OVERRIDE = PROJECT_ROOT / "docker/compose.override.staging.yml"

_spec = importlib.util.spec_from_file_location("gen_staging_matrix", SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _env_of(path: Path, service: str = "backend") -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return gen._env_map(doc, service)


# ---------------------------------------------------------------------------
# 1. 生成器自身的口径：compose 的四种写法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected,why_part",
    [
        # `${K:-False}` ⇒ dev 就是这个字面默认值（矩阵里写 false）
        ("${TOOL_SYNC_OFFLOAD_ENABLED:-False}", "false", "默认"),
        # `${K:-}` ⇒ 转发了但没有默认，空值时由 Settings 兜底 ⇒ dev 记「空」
        ("${DB_ECHO:-}", "空", "默认空"),
        # 嵌套默认：宿主机没设时由 DEBUG 推断，仍然是「空」，但理由必须点名 DEBUG
        ("${UVICORN_RELOAD:-${DEBUG:-True}}", "空", "DEBUG"),
        # 写死的字面量（override 侧常见）
        ("false", "false", "写死"),
        ("10", "10", "写死"),
    ],
)
def test_dev_claim_covers_the_four_compose_forms(raw, expected, why_part) -> None:
    claim, why = gen.dev_claim("SOME_KEY", raw, settings_default=True)
    assert claim == expected
    assert why_part in why, f"理由里必须点名依据，实际：{why}"


def test_dev_claim_falls_back_to_the_settings_default_when_compose_ignores_it() -> None:
    """compose 没转发的键，dev 列的含义就是「Settings 出厂默认」——这是读出来的，不是猜的。"""
    claim, why = gen.dev_claim("REQUIRE_API_KEY", None, settings_default=False)
    assert claim == "false"
    assert "未转发" in why
    claim2, _ = gen.dev_claim("LLM_MAX_CONCURRENCY", None, settings_default=0)
    assert claim2 == "0"


def test_dev_claim_and_settings_agree_on_real_defaults() -> None:
    """`_settings_default` 读的是 `model_fields`，不是实例化后的对象：
    实例化会吃宿主机 `.env`，那正是这张表要避免的污染源。"""
    assert gen._settings_default("EXECUTION_SANDBOX_ENABLED") is False
    assert gen._settings_default("PROMPT_INJECTION_DETECTION_ENABLED") is True
    assert gen._settings_default("NO_SUCH_KEY_AT_ALL") is None


def test_normalized_tokens_are_the_words_the_table_uses() -> None:
    assert gen._norm_token(True) == "true"
    assert gen._norm_token(False) == "false"
    assert gen._norm_token(None) == "空"
    assert gen._norm_token("") == "空"
    assert gen._norm_token("HTTP://Prometheus:9090") == "http://prometheus:9090"


def test_environment_accepts_both_compose_forms(tmp_path: Path) -> None:
    """`environment:` 允许 mapping 与 `- K=V` 两种写法，本仓现在用的是前者，但后者同样合法。
    只吃 mapping 的解析器遇到改写过的 compose 会**静默少一行**——`check()` 于是绿着，
    表却不再覆盖那个键。所以两形都必须读出同一个字典。"""
    mapping = yaml.safe_load(
        "services:\n  backend:\n    environment:\n      A: \"1\"\n      B: ${C:-2}\n"
    )
    seq = yaml.safe_load(
        "services:\n  backend:\n    environment:\n      - A=1\n      - B=${C:-2}\n"
    )
    assert gen._env_map(mapping, "backend") == gen._env_map(seq, "backend") == {
        "A": "1", "B": "${C:-2}"
    }
    assert gen._env_map({"services": {"other": {"environment": {"A": "1"}}}}, "backend") == {}


# ---------------------------------------------------------------------------
# 2. 覆盖面：表 = 环境差异项（双向）
# ---------------------------------------------------------------------------


def test_tracked_keys_are_exactly_override_plus_the_registered_manual_rows() -> None:
    override_keys = set(_env_of(OVERRIDE))
    assert gen.tracked_keys() == override_keys | set(gen.MANUAL_KEYS)


def test_runbook_table_rows_equal_the_tracked_set() -> None:
    """多一行 = 有一格机器校验不了；少一行 = 运维看不见某个真实差异。两个方向都算漂移。"""
    assert set(gen.runbook_matrix()) == gen.tracked_keys()


def test_manual_exception_is_registered_only_when_it_really_is_not_derivable() -> None:
    """`MANUAL_KEYS` 是这张表唯一允许手填的口子，所以它的每一条前提都要成立：
    键不在 override 里、且确实在 Settings 里存在（否则它连"字段默认值"都读不到）。"""
    from agentix.config.settings import Settings

    override_keys = set(_env_of(OVERRIDE))
    for key in gen.MANUAL_KEYS:
        assert key not in override_keys, f"{key} 被 override 设了，就不该登记成手填"
        assert key in Settings.model_fields, f"{key} 不是 Settings 字段，手填的理由要重写"


def test_manual_rows_are_not_silently_dropped_from_the_table() -> None:
    for key in gen.MANUAL_KEYS:
        assert key in gen.runbook_matrix(), "登记了例外却没这一行，例外就成了空转"


def test_compose_does_not_set_the_gate_flags_the_matrix_claims_for_dev() -> None:
    """dev 列里那些 `false` 不是从 override 读来的（override 是 staging 侧），
    而是 compose 的 `:-False` 或 Settings 默认——这条用例钉住"两列来自两份不同文件"这件事。"""
    compose_env = _env_of(COMPOSE)
    assert str(compose_env.get("TOOL_SYNC_OFFLOAD_ENABLED")) == "${TOOL_SYNC_OFFLOAD_ENABLED:-False}"
    assert "REQUIRE_API_KEY" not in compose_env, "它若进了 compose，dev 列的依据就换了"


# ---------------------------------------------------------------------------
# 3. 一致性：当前仓库状态下必须干净
# ---------------------------------------------------------------------------


def test_check_is_clean() -> None:
    assert gen.check(verbose=False) == [], "配置矩阵与部署物漂移，见 scripts/gen_staging_matrix.py --check"


def test_the_committed_table_header_is_the_generated_one() -> None:
    """表头也是生成物的一部分：`production（**手写，无部署物兜底**）` 那一格是「这一列没有
    机制保证」这句话的落点。表头与 `render()` 不一致时，把生成表整段粘回 runbook 会顺手
    把这句话改掉。"""
    header = [ln for ln in RUNBOOK.read_text(encoding="utf-8").splitlines()
              if ln.startswith("| 配置 | dev |")][:1]
    assert header, "runbook 里那张矩阵的表头被改了形状"
    assert header[0] == gen.render().splitlines()[0]


def test_every_pinned_row_is_bolded_and_every_unpinned_is_not() -> None:
    """加粗 = 「预发真的设了」。这条把 p109 第 6 节的"已知若干键"推广成整表的通则。"""
    override_keys = set(_env_of(OVERRIDE))
    committed = gen.runbook_matrix()
    for row in gen.generated_rows():
        if row["manual"] == "yes":
            continue
        key = row["key"]
        _, bold = gen.normalize_cell(committed[key]["staging"])
        assert bold is (key in override_keys), key


def test_generated_table_values_match_the_committed_cells() -> None:
    committed = gen.runbook_matrix()
    for row in gen.generated_rows():
        if row["manual"] == "yes":
            continue
        dev, _ = gen.normalize_cell(committed[row["key"]]["dev"])
        staging, _ = gen.normalize_cell(committed[row["key"]]["staging"])
        assert dev == row["dev"], row["key"]
        assert staging == row["staging"], row["key"]


def test_the_two_rows_g13_caught_are_now_derived_not_copied() -> None:
    """G13 两例的回归：`UVICORN_RELOAD` / `DB_ECHO` 的 staging=false 必须由 override 算出来，
    而不是"文档里写着、部署物里没有"。"""
    rows = {row["key"]: row for row in gen.generated_rows()}
    for key in ("UVICORN_RELOAD", "DB_ECHO"):
        assert rows[key]["dev"] == "空", "compose 侧留空 ⇒ dev 不该写成具体值"
        assert rows[key]["staging"] == "false", "override 钉了 false"
        assert rows[key]["pinned"] is True


def test_otlp_endpoint_is_written_as_the_full_url_the_override_sets() -> None:
    """生成器第一轮就报出的一处真漂移：表里写 `prometheus:9090`，override 写
    `http://prometheus:9090`。少协议头的值抄到别的部署物上就是一个无效端点。"""
    cell = gen.runbook_matrix()["OTEL_EXPORTER_OTLP_ENDPOINT"]["staging"]
    assert "http://prometheus:9090" in cell


# ---------------------------------------------------------------------------
# 4. 牙齿：八种漂移形态都必须被报出来
# ---------------------------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch) -> dict:
    """把三个输入文件复制到 tmp_path 并把模块常量指过去，篡改只发生在副本里。"""
    copies = {}
    for name, path in (("compose", COMPOSE), ("override", OVERRIDE), ("runbook", RUNBOOK)):
        target = tmp_path / name
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        copies[name] = target
    monkeypatch.setattr(gen, "COMPOSE", copies["compose"])
    monkeypatch.setattr(gen, "OVERRIDE", copies["override"])
    monkeypatch.setattr(gen, "RUNBOOK", copies["runbook"])
    return copies


def _patch(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"篡改锚点应唯一命中，实际 {text.count(old)} 次：{old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_sandbox_baseline_is_clean(sandbox) -> None:
    """反向验证的前置：副本本身必须是绿的，否则后面那些篡改用例全是假绿。"""
    assert gen.check(verbose=False) == []


def test_teeth_override_key_removed_but_table_still_claims_it(sandbox) -> None:
    """G13 第一例的形态：表里加粗声明，部署物里没有。"""
    _patch(sandbox["override"], "      MCP_SUBPROCESS_ENV_SCRUB: \"true\"\n", "")
    problems = gen.check(verbose=False)
    assert any("MCP_SUBPROCESS_ENV_SCRUB" in p and "G13" in p for p in problems), problems


def test_teeth_override_value_changed(sandbox) -> None:
    _patch(sandbox["override"], "LLM_MAX_CONCURRENCY: \"10\"", "LLM_MAX_CONCURRENCY: \"50\"")
    problems = gen.check(verbose=False)
    assert any("LLM_MAX_CONCURRENCY" in p and "50" in p for p in problems), problems


def test_teeth_table_staging_claim_changed(sandbox) -> None:
    _patch(sandbox["runbook"], "| `RATE_LIMIT_ENABLED` | false | **true**",
           "| `RATE_LIMIT_ENABLED` | false | **false**")
    problems = gen.check(verbose=False)
    assert any("RATE_LIMIT_ENABLED" in p for p in problems), problems


def test_teeth_table_dev_column_changed(sandbox) -> None:
    """dev 列同样是部署物/Settings 的派生物，不是"看着像就行"的那一列。"""
    _patch(sandbox["runbook"], "| `REQUIRE_API_KEY` | false |", "| `REQUIRE_API_KEY` | true |")
    problems = gen.check(verbose=False)
    assert any("REQUIRE_API_KEY" in p and "dev" in p for p in problems), problems


def test_teeth_table_row_deleted(sandbox) -> None:
    lines = sandbox["runbook"].read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [ln for ln in lines if not ln.startswith("| `EXECUTION_SANDBOX_ENABLED`")]
    assert len(kept) == len(lines) - 1, "删行锚点没命中"
    sandbox["runbook"].write_text("".join(kept), encoding="utf-8")
    problems = gen.check(verbose=False)
    assert any("EXECUTION_SANDBOX_ENABLED" in p for p in problems), problems


def test_teeth_table_row_added_for_an_unmanaged_key(sandbox) -> None:
    """新加一行"看着合理"但部署物里没有的键 ⇒ 必须被要求先进 override 或登记例外。"""
    _patch(sandbox["runbook"], "| `SECRET_KEY` |",
           "| `HUMAN_REVIEW_ENABLED` | false | true | 按需 |\n| `SECRET_KEY` |")
    problems = gen.check(verbose=False)
    assert any("HUMAN_REVIEW_ENABLED" in p and "MANUAL_KEYS" in p for p in problems), problems


def test_teeth_row_added_with_a_bold_claim_is_the_g13_shape_not_the_unregistered_one(sandbox) -> None:
    """同一个"多一行"，加粗与不加粗是两种问题：前者是假话，后者只是没登记。
    两条用例分别抓住它们，免得 check() 里只剩一个笼统的「这行不认识」。"""
    _patch(sandbox["runbook"], "| `SECRET_KEY` |",
           "| `HUMAN_REVIEW_ENABLED` | false | **true** | 按需 |\n| `SECRET_KEY` |")
    problems = gen.check(verbose=False)
    assert any("HUMAN_REVIEW_ENABLED" in p and "G13" in p for p in problems), problems
    assert not any("HUMAN_REVIEW_ENABLED" in p and "MANUAL_KEYS" in p for p in problems), problems


def test_teeth_unbolding_a_pinned_row_is_also_drift(sandbox) -> None:
    """加粗不只是文风：它是「override 真设了」的记号，抹掉它 = 表不再说明这一格有部署物兜底。"""
    _patch(sandbox["runbook"], "| `TOOL_HANDLER_CONTRACT_ENABLED` | false | **true**（",
           "| `TOOL_HANDLER_CONTRACT_ENABLED` | false | true（")
    problems = gen.check(verbose=False)
    assert any("TOOL_HANDLER_CONTRACT_ENABLED" in p and "加粗" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 5. 单元格解析的宽容度
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cell,word,bold",
    [
        ("**false**（override 显式钉住：线程下放改变循环亲和语义）", "false", True),
        ("**true**", "true", True),
        ("空（=沿用 `DEBUG`）", "空", False),
        ("0（不限）", "0", False),
        ("**http://prometheus:9090**（带 scheme 的完整 URL）", "http://prometheus:9090", True),
        ("false（先逐个确认 handler 无 loop 亲和依赖再打开）", "false", False),
        ("**staging**", "staging", True),
    ],
)
def test_cell_parsing_ignores_prose_but_keeps_the_signal(cell, word, bold) -> None:
    got_word, got_bold = gen.normalize_cell(cell)
    assert got_word == word
    assert got_bold is bold


def test_parenthesis_split_does_not_eat_a_value_that_contains_one() -> None:
    """值里带括号时按"第一个括号前"取词——所以带括号的值必须登记在 MANUAL_KEYS 或改写法。
    这条用例锁住解析边界，免得将来有人往 override 里塞 `x（y）` 这种值。"""
    assert gen.normalize_cell("a（b）**")[0] == "a"
    assert gen.normalize_cell("**a**")[0] == "a"


def test_the_parser_is_scoped_to_the_matrix_section(sandbox) -> None:
    """解析器读的是「配置矩阵」小节，不是整篇文档。今天 runbook 里那 16 行
    `` | `KEY` | `` 恰好全在这一节内，所以"整篇扫"和"按节扫"看不出差别；但下面还有几张
    形状相近的表，将来任何人新加一张以 `` | `键名` | `` 开头的对照表，整篇扫就会把它的行
    当成矩阵的行、逐条假报"没登记"。小节边界是这条解析的承重墙，得有用例压着。"""
    text = sandbox["runbook"].read_text(encoding="utf-8")
    tail = text.rsplit("\n## ", 1)
    assert len(tail) == 2, "runbook 结构变了，本用例的插桩点需要重写"
    sandbox["runbook"].write_text(
        tail[0] + "\n## 另一节\n\n| 配置 | dev | staging | production |\n|---|---|---|---|\n"
        "| `SOME_OTHER_TABLE_KEY` | x | y | z |\n\n" + tail[1],
        encoding="utf-8",
    )
    assert "SOME_OTHER_TABLE_KEY" not in gen.runbook_matrix()
    assert gen.check(verbose=False) == [], "小节外的表被当成矩阵的行，就会假报漂移"


# ---------------------------------------------------------------------------
# 6. 可发现性：文档得说这张表是生成物
# ---------------------------------------------------------------------------


def test_runbook_points_at_the_generator() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "scripts/gen_staging_matrix.py" in text, "运维不知道这张表可以重算"
    section = text.split("## 配置矩阵", 1)[1].split("\n## ", 1)[0]
    assert "gen_staging_matrix.py" in section, "指向要写在表旁边，不是写在别处"


def test_acceptance_checklist_no_longer_relies_on_eyeballing() -> None:
    """§验收清单第 5 条原先是「逐项 diff」；终态是跑脚本。"""
    text = RUNBOOK.read_text(encoding="utf-8")
    item = [ln for ln in text.splitlines() if ln.strip().startswith("5. 配置矩阵")]
    assert item, "验收清单第 5 条被改名了，指向也要跟着改"
    assert "gen_staging_matrix.py" in item[0], item[0]


def test_the_script_documents_why_the_production_column_is_not_checked() -> None:
    """三列里只有两列有部署物兜底；脚本必须自己说清这件事，否则"表是生成的"会被读成
    "三列都可信"。"""
    doc = SCRIPT.read_text(encoding="utf-8").split('"""')[1]
    assert "没有第三份文件" in doc or "生产列" in doc
    assert "MANUAL_KEYS" in doc


# ---------------------------------------------------------------------------
# 7. CLI：退出码与编码
# ---------------------------------------------------------------------------


def test_cli_check_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "一致" in proc.stdout


def test_cli_render_is_a_markdown_table() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("| `")]
    assert len(lines) == len(gen.tracked_keys())
    assert all(ln.count("|") == 5 for ln in lines)


def test_cli_check_returns_one_when_the_table_drifts(sandbox, monkeypatch, capsys) -> None:
    """`--check` 是给 CI 的那道闸：漂移时退出码必须是 1，而不是打印一句警告继续绿。

    这里跑的是进程内的 `main()`，不是子进程——脚本用 `__file__` 的父目录定位 `agentix`
    包，把它复制到 tmp_path 就跑不到 Settings 了。子进程那条由上面两个 CLI 用例覆盖，
    这一条只负责「有问题 ⇒ 非零」这个映射。
    """
    _patch(sandbox["override"], '      MCP_SUBPROCESS_ENV_SCRUB: "true"\n', "")
    monkeypatch.setattr(sys, "argv", ["gen_staging_matrix.py", "--check"])
    assert gen.main() == 1
    out = capsys.readouterr().out
    assert "MCP_SUBPROCESS_ENV_SCRUB" in out, "报了什么漂移必须能看见，不能只有退出码"
