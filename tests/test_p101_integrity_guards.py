"""p101 完整性守卫：入口可解析 + 文档相对链接不悬空 + 凭据回退名有落点。

2026-10-06「确保项目完整性而非 demo」审查发现三类**会静默退化**的缺口，
本文件把它们钉死，防止重构时再次漂移：

1. **``[project.scripts]`` 指向不存在的模块**——Phase 6 把 ``agentix.mcp``
   整簇迁到 ``agentix.capabilities.mcp``（见 p75），但 ``pyproject.toml`` 的
   控制台入口漏改，仍写 ``agentix.mcp.server:main``。后果：``pip install -e .``
   成功、命令也生成，可一执行就 ``ImportError``；而 CI 走
   ``pip install -e . --no-deps`` 且从不调用该命令，**全绿也照样红给用户看**。
   守卫：逐个 import 每个入口目标并断言可调用。

2. **文档相对链接悬空**——``docs/版本管理策略.md`` 曾把 ``CHANGELOG.md`` 等
   根目录文件按同级链接（实际需 ``../``），``docs/guides/快速启动.md`` 曾
   引用 ``INTEGRATION_SUMMARY.md`` / ``CLAUDE.md`` 等**从未存在**的文件。
   守卫：扫 README / CHANGELOG / docs/**/*.md 的相对链接，逐个验存在性。

3. **代码读取的凭据回退名没有落点**——``LLM_PROVIDER=glm`` 时代码会回退读
   ``GLM_API_KEY``，shell 里已 ``export`` 的 ``OPENAI_API_KEY`` / 设了会**覆盖**
   主配置的 ``DEEPSEEK_*`` 也都被读，但根 ``.env.example`` 此前一概未提 ⇒
   部署方无从知道这些名字生效（或生效顺序）。
   守卫：这些键必须在 ``.env.example`` 中**出现**（含注释行）。
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Dict, List, Tuple


REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"

#: 扫描面：仓库根的说明文档 + docs 树（archive 属历史留档，不参与）。
DOC_ROOTS = (REPO, REPO / "docs")

#: 排除目录（第三方/生成物/历史留档）。
SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "archive", "web"}

#: markdown 链接：``[text](target)``，排除裸 URL / 锚点 / mailto。
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: 代码里读取、但容易被 example 漏掉的凭据回退名（键名必须出现在 .env.example）。
CREDENTIAL_FALLBACK_KEYS = (
    "GLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "VITE_PORT",
)


def _python_files(*roots: Path) -> List[Path]:
    out: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(
            p
            for p in root.rglob("*.py")
            if not any(part in SKIP_DIRS for part in p.relative_to(REPO).parts)
        )
    return out


class TestConsoleScriptsResolve:
    """``pyproject.toml`` 的每个控制台入口都必须 import 得动且可调用。"""

    @staticmethod
    def _scripts() -> Dict[str, str]:
        """解析 ``[project.scripts]`` → ``{命令名: "module:attr"}``。

        刻意不用 ``tomllib``（Python 3.11+ 才有，本项目基线 3.10，
        与 ``tests/test_dependency_consistency.py`` 同法）。
        """
        text = PYPROJECT.read_text(encoding="utf-8")
        block = re.search(r"^\[project\.scripts\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
        assert block is not None, "pyproject.toml 中未找到 [project.scripts] 段"
        out: Dict[str, str] = {}
        for line in block.group(1).splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            out[name.strip()] = value.strip().strip('"').strip("'")
        assert out, "[project.scripts] 段解析为空，断言会空跑通过（防空真）"
        return out

    def test_every_console_script_target_imports_and_is_callable(self):
        failures = []
        for name, target in sorted(self._scripts().items()):
            if ":" not in target:
                failures.append(f"{name}: 目标 {target!r} 不是 module:attr 形式")
                continue
            module_name, attr = target.split(":", 1)
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - 任何导入失败都要报给用户
                failures.append(f"{name} -> {target}: 导入失败 {type(exc).__name__}: {exc}")
                continue
            obj = getattr(module, attr, None)
            if obj is None:
                failures.append(f"{name} -> {target}: 模块 {module_name} 无属性 {attr!r}")
            elif not callable(obj):
                failures.append(f"{name} -> {target}: 属性不可调用（{type(obj).__name__}）")
        assert not failures, "控制台入口指向无效目标（装得上、跑不起）：\n" + "\n".join(
            f"  {f}" for f in failures
        )

    def test_mcp_entry_no_longer_points_at_removed_module(self):
        """p75 已删 ``agentix.mcp``，入口必须落在 ``agentix.capabilities.mcp``。"""
        target = self._scripts().get("agentix-mcp-server", "")
        assert target.startswith(
            "agentix.capabilities.mcp."
        ), f"agentix-mcp-server 入口 {target!r} 应指向 agentix.capabilities.mcp.*（agentix.mcp 已在 p75 删除）"


class TestMarkdownRelativeLinksResolve:
    """文档里的相对链接必须指向真实存在的文件/目录。"""

    def _docs(self) -> List[Path]:
        files: List[Path] = []
        for name in ("README.md", "CHANGELOG.md"):
            p = REPO / name
            if p.is_file():
                files.append(p)
        docs = REPO / "docs"
        if docs.is_dir():
            files.extend(
                p
                for p in docs.rglob("*.md")
                if not any(part in SKIP_DIRS for part in p.relative_to(REPO).parts)
            )
        assert files, "未扫描到任何 markdown 文件（扫描面失效）"
        return files

    def test_no_broken_relative_links(self) -> None:
        broken: List[Tuple[str, int, str]] = []
        total = 0
        for doc in self._docs():
            text = doc.read_text(encoding="utf-8", errors="replace")
            for match in _MD_LINK.finditer(text):
                url = match.group(1)
                if url.startswith(("http://", "https://", "mailto:", "#", "tel:")):
                    continue
                path = url.split("#", 1)[0].split("?", 1)[0]
                if not path:
                    continue
                # 只认"像路径"的目标：含 / 或带扩展名。否则会把
                # `MessageWindow[T](20)` 这类行内泛型/数字误判为链接。
                if "/" not in path and "." not in path:
                    continue
                total += 1
                if path.startswith("/"):
                    resolved = REPO / path.lstrip("/")
                else:
                    resolved = (doc.parent / path).resolve()
                if not resolved.exists():
                    line = text.count("\n", 0, match.start()) + 1
                    broken.append((str(doc.relative_to(REPO)).replace("\\", "/"), line, url))
        assert total > 100, f"仅解析到 {total} 条相对链接，扫描面可能失效"
        assert not broken, "文档存在悬空相对链接：\n" + "\n".join(
            f"  {where}:{line} -> {url}" for where, line, url in broken
        )


class TestCredentialFallbackKeysAreDocumented:
    """代码会读的凭据回退名必须在根 ``.env.example`` 出现（注释行也算）。"""

    def test_fallback_keys_present_in_root_env_example(self) -> None:
        text = (REPO / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
        missing = [k for k in CREDENTIAL_FALLBACK_KEYS if k not in text]
        assert not missing, (
            f".env.example 未提及这些代码会读的键：{missing}\n"
            "部署方将无从知道它们生效（或生效优先级），"
            "参见 agentix/retrieval/config.py 与 kb_ingest/kb_service/clients/llm.py。"
        )

    def test_ingest_service_url_not_hardcoded_to_self_port(self) -> None:
        """.env.example 不得把 INGEST_SERVICE_URL 写死到 backend 自己的 8000。

        compose 用 ``${INGEST_SERVICE_URL:-...}`` 做变量替换，若 .env 写
        ``localhost:8000``，容器内的 backend 会把请求发回**自己**，且与
        kb_ingest 实际的 8100 端口不符（2026-10-06 实测）。
        """
        text = (REPO / ".env.example").read_text(encoding="utf-8-sig", errors="replace")
        active = [
            line
            for line in text.splitlines()
            if line.strip().startswith("INGEST_SERVICE_URL=")
        ]
        for line in active:
            assert "localhost:8000" not in line, (
                "INGEST_SERVICE_URL 指向 backend 自身端口 8000，"
                "应注释掉交给 compose 缺省 http://kb_ingest:8000，"
                "或本机直跑时写 http://localhost:8100"
            )
