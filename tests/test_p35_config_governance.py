"""P35：配置治理（P2-4）——字段集合自检 + kb_ingest 环境变量契约一致性。

覆盖 P2-4 的两项收尾：

1. `.env` 与 `.env.example` 不一致 → 启动自检比对字段集合并告警
   （缺项会让 Pydantic Settings **静默**回落到默认值，语义合法但行为危险）。
2. `kb_ingest` 配置收敛：`kb_service/core/config.py` 读取的**每一个**环境变量
   都必须在 `kb_ingest/.env.example` 中声明（契约唯一，防漂移）。

全部离线，零外部依赖。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentix.config.selfcheck import (
    DEFAULT_IGNORED_MISSING,
    EnvDiff,
    compare_env_files,
    parse_env_keys,
    run_startup_selfcheck,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_CONFIG = PROJECT_ROOT / "kb_ingest" / "kb_service" / "core" / "config.py"
KB_ENV_EXAMPLE = PROJECT_ROOT / "kb_ingest" / ".env.example"

#: 从 `os.getenv("NAME"` / `os.getenv('NAME'` 中提取变量名
_GETENV_RE = re.compile(r"os\.getenv\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)")


def _config_env_keys(path: Path = KB_CONFIG) -> set:
    """提取 config.py 通过 os.getenv 读取的全部环境变量名。"""
    text = Path(path).read_text(encoding="utf-8")
    return set(_GETENV_RE.findall(text))


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


class TestParseEnvKeys:
    def test_parses_simple_keys(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("A=1\nB=2\n", encoding="utf-8")
        assert parse_env_keys(f) == {"A", "B"}

    def test_skips_comments_and_blanks(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("# comment\n\nA=1\n  # indented comment\n", encoding="utf-8")
        assert parse_env_keys(f) == {"A"}

    def test_handles_export_prefix(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("export A=1\nB=2\n", encoding="utf-8")
        assert parse_env_keys(f) == {"A", "B"}

    def test_never_returns_values(self, tmp_path: Path):
        """安全性：解析结果**只含键**，绝不含值（值里有密钥）。"""
        f = tmp_path / ".env"
        f.write_text("SECRET_KEY=sk-topsecret123\nAPI_KEY=secretvalue\n", encoding="utf-8")
        keys = parse_env_keys(f)
        assert keys == {"SECRET_KEY", "API_KEY"}
        # 任何返回的项都不得带出**值**片段
        assert all("sk-topsecret" not in k and "secretvalue" not in k for k in keys)
        assert all("=" not in k for k in keys)

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert parse_env_keys(tmp_path / "nope") == set()

    def test_empty_value_counts_as_present(self, tmp_path: Path):
        """`KEY=` 也算声明了（只是值为空）。"""
        f = tmp_path / ".env"
        f.write_text("KEY=\n", encoding="utf-8")
        assert parse_env_keys(f) == {"KEY"}


# ---------------------------------------------------------------------------
# 集合比对
# ---------------------------------------------------------------------------


class TestCompareEnvFiles:
    def test_detects_missing_keys(self, tmp_path: Path):
        env = tmp_path / ".env"
        example = tmp_path / ".env.example"
        env.write_text("A=1\n", encoding="utf-8")
        example.write_text("A=1\nB=2\nC=3\n", encoding="utf-8")
        diff = compare_env_files(env, example, ignored_missing=set())
        assert diff.missing == {"B", "C"}
        assert diff.ok is False

    def test_detects_extra_keys(self, tmp_path: Path):
        env = tmp_path / ".env"
        example = tmp_path / ".env.example"
        env.write_text("A=1\nZ=9\n", encoding="utf-8")
        example.write_text("A=1\n", encoding="utf-8")
        diff = compare_env_files(env, example)
        assert diff.extra == {"Z"}
        assert diff.ok is True  # 多配不算错

    def test_ignored_missing_is_surfaced_but_not_blocking(self, tmp_path: Path):
        env = tmp_path / ".env"
        example = tmp_path / ".env.example"
        env.write_text("", encoding="utf-8")
        example.write_text("SECRET_KEY=xxx\nOTHER=1\n", encoding="utf-8")
        diff = compare_env_files(env, example, ignored_missing={"SECRET_KEY"})
        assert diff.missing == {"OTHER"}
        assert diff.ignored == {"SECRET_KEY"}

    def test_default_ignore_list_covers_secrets_and_flags(self):
        assert "SECRET_KEY" in DEFAULT_IGNORED_MISSING
        assert "API_KEY" in DEFAULT_IGNORED_MISSING
        assert "ENABLE_EXTERNAL_SEARCH" in DEFAULT_IGNORED_MISSING

    def test_missing_env_file_is_not_an_error(self, tmp_path: Path):
        example = tmp_path / ".env.example"
        example.write_text("A=1\n", encoding="utf-8")
        diff = compare_env_files(tmp_path / "nope", example)
        assert diff.present is False
        assert diff.ok is True  # 未找到 .env 不算配置错误（可用环境变量）

    def test_missing_example_is_not_an_error(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text("A=1\n", encoding="utf-8")
        diff = compare_env_files(env, tmp_path / "nope")
        assert diff.ok is True

    def test_summary_text(self, tmp_path: Path):
        env = tmp_path / ".env"
        example = tmp_path / ".env.example"
        env.write_text("", encoding="utf-8")
        example.write_text("X=1\n", encoding="utf-8")
        assert "X" in compare_env_files(env, example).summary()


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


class TestStartupSelfcheck:
    def test_never_raises_on_garbage_paths(self, tmp_path: Path):
        result = run_startup_selfcheck(
            tmp_path / "no" / "env", tmp_path / "no" / "example", root=tmp_path
        )
        assert result is None or isinstance(result, EnvDiff)

    def test_returns_diff_for_realistic_files(self, tmp_path: Path):
        env = tmp_path / ".env"
        example = tmp_path / ".env.example"
        env.write_text("A=1\n", encoding="utf-8")
        example.write_text("A=1\nB=2\n", encoding="utf-8")
        result = run_startup_selfcheck(env, example, root=tmp_path)
        assert result is not None
        assert result.missing == {"B"}

    def test_selfcheck_on_repo_files_is_consistent(self):
        """仓库自带的 `.env` 不得缺少 `.env.example` 声明的**非可选**字段。

        这是 P2-4 的核心验收：本地配置漂移必须被自检发现。
        """
        env = PROJECT_ROOT / ".env"
        if not env.exists():  # 未落 .env 的环境（CI）跳过
            pytest.skip("仓库中不存在 .env，跳过本地漂移检查")
        diff = compare_env_files(env, PROJECT_ROOT / ".env.example")
        assert diff.ok, f".env 缺少非可选字段：{sorted(diff.missing)}"


# ---------------------------------------------------------------------------
# kb_ingest 环境变量契约
# ---------------------------------------------------------------------------


class TestVersionSingleSourceOfTruth:
    """版本号必须只有一个权威来源（`agentix.__version__`）。

    历史事故（批次 13 修复）：`settings.py` 写 0.1.2，而 `.env`/`.env.example`
    写 `APP_VERSION=0.1.1`；运行期被 `.env` **覆盖**后健康检查对外报 0.1.1，
    而旧版 `bump_version --check` 只比对三处代码文件，完全看不见该漂移。
    """

    def test_settings_derives_from_package(self):
        from agentix.config import settings

        import agentix

        assert settings.APP_VERSION == agentix.__version__

    def test_settings_does_not_hardcode_version(self):
        """settings.py 不得再保留硬编码版本字面量（否则又成一处事实来源）。"""
        src = (PROJECT_ROOT / "agentix" / "config" / "settings.py").read_text(
            encoding="utf-8"
        )
        assert "APP_VERSION: str = \"0." not in src
        assert "_default_app_version" in src

    def test_bump_script_checks_env_example(self):
        """`bump_version.py` 必须把 `.env.example` 纳入校验（防漂移复发）。"""
        src = (PROJECT_ROOT / "scripts" / "bump_version.py").read_text(encoding="utf-8")
        assert "ENV_EXAMPLE" in src
        assert "_read_env_example_version" in src

    def test_bump_check_passes_now(self):
        """当前仓库状态必须通过版本一致性检查（CI 门禁同口径）。"""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/bump_version.py", "--check"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            # 子进程现已输出 UTF-8（bump_version.py reconfigure），
            # 读取端必须同编码，否则中文 Windows 默认 GBK 解码会炸在 reader 线程。
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, f"版本一致性检查失败：\n{result.stdout}\n{result.stderr}"
    def test_config_file_exists(self):
        assert KB_CONFIG.is_file()
        assert KB_ENV_EXAMPLE.is_file()

    def test_config_reads_env_vars(self):
        keys = _config_env_keys()
        assert len(keys) >= 30, f"提取到的环境变量过少（{len(keys)}），正则可能失效"

    def test_every_read_key_is_documented(self):
        """契约核心：config.py 读取的键 ⊆ .env.example 声明的键。"""
        read_keys = _config_env_keys()
        documented = parse_env_keys(KB_ENV_EXAMPLE)
        undocumented = sorted(read_keys - documented)
        assert not undocumented, (
            "kb_service/core/config.py 读取了未在 kb_ingest/.env.example 中声明的"
            f"环境变量：{undocumented}\n"
            "请同步更新 kb_ingest/.env.example（P2-4：配置事实来源唯一）。"
        )

    def test_documented_keys_are_actually_read(self):
        """反向检查：声明了却没人读 = 死配置项，同样要暴露。"""
        read_keys = _config_env_keys()
        documented = parse_env_keys(KB_ENV_EXAMPLE)
        unused = sorted(documented - read_keys)
        assert not unused, (
            f"kb_ingest/.env.example 声明了但 config.py 从不读取：{unused}\n"
            "要么接入它，要么删除它（避免误导部署方）。"
        )

    def test_example_does_not_leak_real_secrets(self):
        """示例文件绝不能含真密钥（只能有占位符或空值）。"""
        text = KB_ENV_EXAMPLE.read_text(encoding="utf-8")
        leaked = re.findall(r"(?:sk-[A-Za-z0-9]{16,})", text)
        assert not leaked, f"示例文件疑似包含真实密钥：{leaked}"

    def test_no_duplication_of_main_app_config(self):
        """反重复：kb_ingest 示例不得再混入主应用专有项。

        历史问题：该文件曾是根目录 `.env.example` 的整份拷贝，
        含 Milvus / Neo4j / LightRAG / CORS 等主应用专有项，对 kb_ingest 无意义。
        """
        documented = parse_env_keys(KB_ENV_EXAMPLE)
        # R3.7 起 kb_ingest 向量后端为 Milvus legal 集合，MILVUS_HOST/PORT/COLLECTION
        # 是 kb_ingest 的真实依赖（不再属于主应用专有），故从 main_only 移除。
        main_only = {"NEO4J_URI", "LIGHTRAG_WORKING_DIR",
                     "CORS_ORIGINS", "APP_NAME", "REDIS_URL", "DATABASE_URL"}
        overlap = documented & main_only
        assert not overlap, (
            f"kb_ingest/.env.example 混入了主应用专有配置项：{sorted(overlap)}\n"
            "主应用配置请见仓库根目录 .env.example。"
        )
