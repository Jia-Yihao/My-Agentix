"""LG-1-4 部署面守卫：重试预算桶「代码默认关、部署面推荐开」。

## 验收口径（指导文档 §4.2 LG-1-4）

1. `Settings` 字段默认**仍是** `False`（约定 1：新开关默认关，flag-off 与基线逐位等价）；
2. `.env.example` 与 `docker/compose.override.staging.yml` 写推荐值 `true` 并注明理由；
3. 配置矩阵那一行由部署物算出来（走 `scripts/gen_staging_matrix.py` 的同一份口径），
   不靠人抄——表里加粗 = 预发真的设了。

**flag-on 下的行为**（三层共享 token、耗尽即停、分类审计）不在这里重复断言：它由
`tests/test_p97_runtime_control.py::TestRetryBudget` 覆盖（task/tool/retrieval 三层各一条
flag-on 停重试 + 各一条 flag-off 基线等价）。本文件只守「部署物说的」与「代码默认的」
不再各说一套——这正是 G13（表里写了值、部署物里没有）留下的形态。
"""

from __future__ import annotations

import importlib.util
import pathlib

import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
OVERRIDE = PROJECT_ROOT / "docker/compose.override.staging.yml"
SCRIPT = PROJECT_ROOT / "scripts/gen_staging_matrix.py"

_spec = importlib.util.spec_from_file_location("gen_staging_matrix_lg14", SCRIPT)
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def _backend_env(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return gen._env_map(doc, "backend")


def _comment_block_above(lines: list[str], needle: str) -> list[str]:
    """取 `needle` 行上方连续注释行（`#` 开头），逐行去掉前导空格与 `#`。"""
    index = next(i for i, line in enumerate(lines) if line.strip() == needle)
    block: list[str] = []
    for line in reversed(lines[:index]):
        if not line.strip().startswith("#"):
            break
        block.append(line.strip().lstrip("#").strip())
    return block


def test_settings_default_stays_off() -> None:
    """约定 1：读**字段默认值**，不实例化 Settings（实例化会吃宿主机 `.env`）。"""
    from agentix.config.settings import Settings

    field = Settings.model_fields["RETRY_BUDGET_ENABLED"]
    assert field.default is False, "重试预算桶的代码默认必须仍是 false（flag-off 与基线逐位等价）"
    assert Settings.model_fields["RETRY_BUDGET_TOKENS"].default == 6, (
        "部署面注释承诺「额度走 RETRY_BUDGET_TOKENS（默认 6）」，默认值变了注释就失真"
    )


def test_env_example_recommends_it_on_with_reason() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    assert "RETRY_BUDGET_ENABLED=true" in lines, (
        "`.env.example` 应写部署推荐值 true（LG-1-4）；代码默认关由 settings.py 保证"
    )
    block = " ".join(_comment_block_above(lines, "RETRY_BUDGET_ENABLED=true"))
    assert "推荐" in block, f"推荐值必须写明理由（当前注释：{block!r}）"
    assert "不新增" in block, "理由要写清「只收紧放大、不新增调用」，否则读者以为它会加请求"


def test_staging_override_pins_it_on_with_reason() -> None:
    env = _backend_env(OVERRIDE)
    assert env.get("RETRY_BUDGET_ENABLED") == "true", (
        "preview/staging 的推荐值是 true —— override 必须真设，不能只在文档里写"
    )
    text = OVERRIDE.read_text(encoding="utf-8")
    block = " ".join(_comment_block_above(text.splitlines(), 'RETRY_BUDGET_ENABLED: "true"'))
    assert "LG-1-4" in block, f"override 的钉值要署名理由（当前注释：{block!r}）"


def test_runbook_matrix_row_is_derived_from_deployments() -> None:
    rows = {row["key"]: row for row in gen.generated_rows()}
    assert "RETRY_BUDGET_ENABLED" in rows, "override 设了它 ⇒ 矩阵必须有这一行（环境差异项）"
    row = rows["RETRY_BUDGET_ENABLED"]
    assert row["dev"] == "false", "dev 侧 compose 没转发 ⇒ 回落 Settings 默认 false"
    assert row["staging"] == "true" and row["pinned"] is True

    committed = gen.runbook_matrix()["RETRY_BUDGET_ENABLED"]
    _, bold = gen.normalize_cell(committed["staging"])
    assert bold, "staging 列不加粗 = 表里不敢说预发真设了"
    assert gen.check(verbose=False) == [], "矩阵与部署物漂移，见 scripts/gen_staging_matrix.py --check"
