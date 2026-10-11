"""P46：工具链口径守卫——"本机能跑的那把尺子"必须等于"CI 的那把尺子"。

**背景（为什么需要这组测试）**

2026-09 实测发现 `Makefile` 的 `lint` 目标**三步全部失败**，且每一步都与
`.github/workflows/ci.yml` 的 lint job 口径不同：

===  ==========================  ===========================  ==================
步骤   旧 Makefile                  CI 实际                      实测结果
===  ==========================  ===========================  ==================
1    ``flake8 agentix -100``      ``ruff check agentix ...``   900 条（必然红）
2    ``black --check``（阻断）      ``black --check``（非阻断）    332 文件待格式化
3    ``mypy agentix``（宽）         ``mypy agentix/domain``      语法错误 ⇒ 全废
===  ==========================  ===========================  ==================

这类"本地红、CI 绿"（或反过来）的危害不是麻烦，而是**信号失效**：
开发者据此判断"我的改动有没有问题"，而两边判断的是不同的东西。

**最严重的是第 3 步，值得单独记一笔**

`agentix/observability/metrics.py:106` 有一行散文注释恰好以 ``# type:`` 开头——
那是 mypy 的**魔法前缀**，于是 mypy 把它当类型注释解析并报
``invalid syntax``，随即打印 ``errors prevented further checking`` 并**终止
整次类型检查**。后果是该目标**从未真正检查过任何类型**，而它的退出码非零，
看起来只是"本来就有错"，没有人会去追根因。

两个教训：
1. 注释里出现 ``# type:`` / ``# noqa`` / ``# fmt:`` 这类**保留前缀**时必须警觉；
2. "有个检查在跑" ≠ "那个检查在生效"——门禁必须先证明自己会 FIRE。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 需要做语法（含类型注释）校验的源码根目录
SOURCE_ROOTS = ("agentix", "kb_ingest", "evals", "scripts", "tests")

#: 跳过：虚拟环境、构建产物、缓存
SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules",
             "build", "dist", "web"}


def _iter_source_files():
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            yield path


#: 历史病灶的最小复现：散文注释误用 mypy 魔法前缀。
#: 放在模块级是为了让"探针会 FIRE"这件事本身可被断言（见 TestProbeFiresOnRegressions）。
_REGRESSION_SNIPPET = 'X = Counter(\n    "c", "d",\n    ["model", "type"],  # type: prompt | completion\n)\n'


class TestTypeCommentSyntaxIsClean:
    """★ 核心守卫：全仓源码必须能被 ``ast.parse(..., type_comments=True)`` 解析。

    为什么用 ``type_comments=True`` 而不是正则扫 ``# type:``：

    * 它是 **stdlib**，无需装 mypy（CI 的 mypy 只对 domain 层负责，本地若靠
      mypy 兜底就会漏掉 domain 之外的文件——而病灶恰恰在 domain 之外）；
    * 它复现的是与 mypy **完全相同**的失败——`invalid syntax`——而不是近似的
      文本规则。合法写法（``# type: ignore``、``# type: ignore[code]``、
      ``x = []  # type: List[int]``）一律照旧通过，不会误伤；
    * 它足够快（全仓数千文件在本机约 1 秒），可以进单测而不是只进 CI。
    """

    def test_all_source_files_parse_with_type_comments(self):
        failures = []
        for path in _iter_source_files():
            src = path.read_text(encoding="utf-8", errors="replace")
            try:
                ast.parse(src, filename=str(path), type_comments=True)
            except SyntaxError as exc:
                rel = path.relative_to(REPO_ROOT)
                failures.append(f"{rel}:{exc.lineno}: {exc.msg}")
        assert not failures, (
            "以下文件存在**语法层面**的类型注释错误。若报 `invalid syntax`，"
            "极可能是把 `# type:` 当普通散文写在了注释里——那是 mypy 的魔法前缀，"
            "会让 mypy 报错并中止整次类型检查：\n  " + "\n  ".join(failures)
        )

    def test_metrics_counter_label_comment_is_not_a_type_comment(self):
        """BUG-19 的定点回归：这个注释必须保持"不是类型注释"。

        断言的是语义（文件名 + 那一行不再构成类型注释），而不是精确文本，
        这样改措辞不会假红。
        """
        path = REPO_ROOT / "agentix" / "observability" / "metrics.py"
        src = path.read_text(encoding="utf-8")
        # 该文件整体可解析（上一条通用用例也覆盖，这里给出更明确的失败信息）
        ast.parse(src, filename=str(path), type_comments=True)
        assert "# type: prompt | completion" not in src, (
            "`# type:` 前缀又回到了 metrics.py；mypy 会报 invalid syntax 并"
            "中止整次检查。请改用 `# label values: ...` 之类的非魔法措辞。"
        )
        # 反向确认：注释仍在（说明只是改了措辞，没把说明删掉）
        assert "prompt | completion" in src, (
            "label 取值说明被整行删掉了——修 BUG-19 只需改前缀，不需要删除说明"
        )


class TestProbeFiresOnRegressions:
    """防空真断言：证明上面的探针**真的会 FIRE**。

    若 ``ast.parse(type_comments=True)`` 因任何原因只返回"通过"，
    TestTypeCommentSyntaxIsClean 就会变成一条永远绿的废用例——那正是本项目
    反复出现的失效模式（门禁"在跑"但不"生效"）。
    """

    def test_probe_fires_on_the_historical_regression(self):
        with pytest.raises(SyntaxError) as ei:
            ast.parse(_REGRESSION_SNIPPET, type_comments=True)
        assert "invalid syntax" in ei.value.msg

    def test_probe_does_not_fire_on_legitimate_type_comments(self):
        """反向：合法的 mypy 类型注释不能被误杀。"""
        for src in (
            "import os\n" "x = os.getcwd()  # type: ignore\n",
            "import os\n" "x = os.getcwd()  # type: ignore[attr-defined]\n",
            "from typing import List\n" "x = []  # type: List[int]\n",
            # 纯散文但**不以**魔法前缀开头
            "y = 1  # label values: prompt | completion\n",
        ):
            ast.parse(src, type_comments=True)  # 不应抛


class TestMakefileLintMatchesCi:
    """``make lint`` 与 CI 的 lint job 必须用同一套命令与同一套范围。

    单元测试的覆盖面比 lint 更"信任局部"，所以这里比较的是**本质要素**
    （工具、范围、是否阻断），忽略 ``--output-format=github`` 这类只在 CI
    有意义的呈现参数。
    """

    @staticmethod
    def _makefile_lint_recipe() -> list[str]:
        text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        lines = text.splitlines()
        recipe: list[str] = []
        in_target = False
        for line in lines:
            if re.match(r"^lint\s*:", line):
                in_target = True
                continue
            if in_target:
                if line.startswith("\t"):
                    recipe.append(line.strip())
                elif line.strip() == "" or line.lstrip().startswith("#"):
                    # 目标之间通常以注释+空行分隔；遇注释即认为本 target 结束
                    if line.lstrip().startswith("#"):
                        break
                else:
                    break
        return recipe

    @staticmethod
    def _ci_lint_runs() -> dict[str, str]:
        """从 ci.yml 抽出 lint job 里各步骤的 run 命令：``{step_name: command}``。"""
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        # 截取 lint job 区块（到下一个同级 job 定义为止）
        m = re.search(r"^  lint:\s*$", text, re.M)
        assert m, "ci.yml 里找不到 lint job"
        rest = text[m.end():]
        nxt = re.search(r"^  [a-z][\w-]*:\s*$", rest, re.M)
        block = rest[: nxt.start()] if nxt else rest

        runs: dict[str, str] = {}
        # 形如：  - name: <名>\n    ...\n    run: <cmd>
        for sm in re.finditer(
            r"-\s*name:\s*(?P<name>[^\n]+)\n(?P<body>(?:.*\n)*?)\s*run:\s*(?P<cmd>[^\n]+)",
            block,
        ):
            runs[sm.group("name").strip()] = sm.group("cmd").strip()
        return runs

    def test_recipe_is_non_empty(self):
        """先证明抓取逻辑真的抓到了东西（否则后面的断言全是真空）。"""
        recipe = self._makefile_lint_recipe()
        assert len(recipe) >= 3, f"未能从 Makefile 解析出 lint 配方：{recipe}"
        runs = self._ci_lint_runs()
        assert len(runs) >= 3, f"未能从 ci.yml 解析出 lint 步骤：{list(runs)}"

    def test_same_tools_on_both_sides(self):
        recipe = " || ".join(self._makefile_lint_recipe())
        ci = " || ".join(self._ci_lint_runs().values())
        for tool in ("ruff", "black", "mypy"):
            assert tool in recipe, f"make lint 缺少 {tool}"
            assert tool in ci, f"ci.yml 的 lint job 缺少 {tool}"

    def test_no_flake8_in_makefile(self):
        """flake8 不是本仓工具链的一环；实测它在 agentix/ 上报 900 条。

        留着它 = 本地永远红、CI 永远绿，是"信号失效"的典型来源。
        """
        recipe = " ".join(self._makefile_lint_recipe())
        assert "flake8" not in recipe, (
            "Makefile 又用上 flake8 了；本仓工具链的 lint 器是 ruff（见 ci.yml）"
        )

    def test_ruff_scope_identical(self):
        ruff_line = next(
            (ln for ln in self._makefile_lint_recipe() if ln.startswith("ruff ")), None
        )
        assert ruff_line, "make lint 里找不到 ruff 步骤"
        m = re.search(r"ruff check (.+)$", ruff_line)
        assert m, f"make lint 的 ruff 行解析失败：{ruff_line!r}"
        make_scope = m.group(1).split()

        ci_cmd = next(v for k, v in self._ci_lint_runs().items() if "ruff" in k.lower()
                      or "ruff" in v.lower())
        m2 = re.search(r"ruff check (.+)$", ci_cmd)
        assert m2, f"ci.yml 的 ruff 步骤解析失败：{ci_cmd!r}"
        # CI 额外带 --output-format=github（只在 GitHub UI 呈现用），不参与比较
        ci_scope = [tok for tok in m2.group(1).split() if not tok.startswith("--")]

        assert make_scope == ci_scope, (
            f"ruff 范围不一致：make={make_scope} vs ci={ci_scope}"
        )

    def test_mypy_scope_identical_and_narrow(self):
        """mypy 范围必须与 CI 一致（domain 层），不得更宽。

        更宽不是"更严格"，而是**另一个东西**：它会连带上报
        infrastructure/interfaces 的存量问题（实测 67 条）并把首次运行拉到
        10–25 分钟，于是没人会在本地跑，等于没有本地类型检查。
        """
        recipe = " ".join(self._makefile_lint_recipe())
        m = re.search(r"mypy\s+(\S+)", recipe)
        assert m, "make lint 里找不到 mypy"
        assert m.group(1) == "agentix/domain", (
            f"make lint 的 mypy 范围是 {m.group(1)!r}，CI 口径是 'agentix/domain'"
        )
        assert "--config-file=pyproject.toml" in recipe, (
            "mypy 必须显式指定 --config-file=pyproject.toml，与 CI 一致；"
            "否则本地可能吃到另一套配置（例如别处的 mypy.ini）"
        )

    def test_black_is_non_blocking_on_both_sides(self):
        """black 目前**非阻断**（仓库尚未全量 black 化，实测 332 文件）。

        两边都必须非阻断，否则又回到"本地红 / CI 绿"。
        CI 用 `continue-on-error: true`；Makefile 用行首 `-`。
        """
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "continue-on-error: true" in text, "ci.yml 里 black 步骤不再是非阻断"

        recipe = self._makefile_lint_recipe()
        black_lines = [ln for ln in recipe if "black" in ln]
        assert black_lines, "make lint 里找不到 black"
        assert all(ln.startswith("-") for ln in black_lines), (
            f"Makefile 的 black 步骤是阻断的（缺少行首 '-'）：{black_lines}；"
            "而 CI 里它是 continue-on-error，两边必须一致"
        )
