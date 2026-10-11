"""p70 Stage M·M1：capabilities/tools → 顶层 tools/（gateway / registry / adapters）。

蓝图 §4 L118 / §5 L141（M0 清单 `MIGRATION_MANIFEST` 的第 1 条）。三件事：

1. **目标位落成**——``agentix/tools/``（gateway / adapters 两子包 + 顶层注册面）
   + 16 个迁移模块 + 包门面。**注**：注册三件套（definitions / registry_init /
   selection）落**顶层**而非 ``registry/`` 子包——公开单例 ``registry`` 与
   子包名冲突会让 ``import agentix.tools.registry.x`` 的属性链解析到单例上
   （影子化地雷）；§4 路线表本就只写「顶层 tools/」，registry 概念由顶层
   ``agentix.tools.registry`` 单例本身承载。
2. **旧路径清尾（M6，2026-10-03）**——迁移期 16 个 ``_TARGET`` 转发 shim +
   PEP 562 惰性包门面已**全部删除**，断言方向翻转为：旧路径文件不存在、
   ``import`` 抛 ``ImportError``、规范位单一入口。
3. **两处刻意的不变量（仍有效）**：
    - 新家自立 import（``import agentix.tools``）不得反向拖起
      ``agentix.capabilities``（搬家切断旧路径依赖的证明）；
    - executor 对 guardrails 的引用：M6 前经 capabilities shim 转发（满足
      p66 守卫 #3），M6 删 shim 后直连 ``agentix.policies.guardrails``——
      p66 以 ``TOOLS_GUARDRAILS_EDGE_ALLOWED`` 窄豁免登记该边（蓝图 §5
      Tool Gateway 链含 Guardrails 步）。

守卫随实体迁移：code_hygiene 三枚 fix-marker、p66 守卫 #3 扫描面、p40
import 目标检查均已指向新家（各自文件内改动，此处只钉住契约本身）。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

import pytest

REPO = Path(__file__).resolve().parents[1]

#: 迁移期旧路径 → 规范位模块（16 个；guardrails 不在表内——M3 已先迁 policies）。
#: **M6 后旧路径文件全部删除**：断言 = 文件不存在 + import 抛错。
M1_DELETED_SHIMS: Dict[str, str] = {
    "agentix/capabilities/tools/definitions.py": "agentix.tools.definitions",
    "agentix/capabilities/tools/registry_init.py": "agentix.tools.registry_init",
    "agentix/capabilities/tools/selection.py": "agentix.tools.selection",
    "agentix/capabilities/tools/executor.py": "agentix.tools.gateway.executor",
    "agentix/capabilities/tools/result_limiter.py": "agentix.tools.gateway.result_limiter",
    "agentix/capabilities/tools/human_review.py": "agentix.tools.gateway.human_review",
    "agentix/capabilities/tools/kg_gateway.py": "agentix.tools.gateway.kg_gateway",
    "agentix/capabilities/tools/search.py": "agentix.tools.adapters.search",
    "agentix/capabilities/tools/notify_user.py": "agentix.tools.adapters.notify_user",
    "agentix/capabilities/tools/memory_tools.py": "agentix.tools.adapters.memory_tools",
    "agentix/capabilities/tools/cron_tools.py": "agentix.tools.adapters.cron_tools",
    "agentix/capabilities/tools/artifact_read.py": "agentix.tools.adapters.artifact_read",
    "agentix/capabilities/tools/task_writes.py": "agentix.tools.adapters.task_writes",
    "agentix/capabilities/tools/plan_mode.py": "agentix.tools.adapters.plan_mode",
    "agentix/capabilities/tools/integrity.py": "agentix.tools.adapters.integrity",
    "agentix/capabilities/tools/tool_search.py": "agentix.tools.adapters.tool_search",
}

#: 目标位全量文件清单（19 个：16 模块 + 3 包门面）
TARGET_FILES: List[str] = [
    "agentix/tools/__init__.py",
    "agentix/tools/definitions.py",
    "agentix/tools/registry_init.py",
    "agentix/tools/selection.py",
    "agentix/tools/gateway/__init__.py",
    "agentix/tools/gateway/executor.py",
    "agentix/tools/gateway/result_limiter.py",
    "agentix/tools/gateway/human_review.py",
    "agentix/tools/gateway/kg_gateway.py",
    "agentix/tools/adapters/__init__.py",
    "agentix/tools/adapters/search.py",
    "agentix/tools/adapters/notify_user.py",
    "agentix/tools/adapters/memory_tools.py",
    "agentix/tools/adapters/cron_tools.py",
    "agentix/tools/adapters/artifact_read.py",
    "agentix/tools/adapters/task_writes.py",
    "agentix/tools/adapters/plan_mode.py",
    "agentix/tools/adapters/integrity.py",
    "agentix/tools/adapters/tool_search.py",
]

#: 代表性符号（按模块可断言的公开面抽样）
SYMBOLS: Dict[str, List[str]] = {
    "agentix.tools.definitions": ["ToolSpec", "registry", "RISK_LEVELS", "FORBIDDEN_RISK_LEVEL"],
    "agentix.tools.registry_init": ["ensure_registered", "get_active_tools"],
    "agentix.tools.selection": ["score_tool_match"],
    "agentix.tools.gateway.executor": ["ToolExecutor", "get_tool_executor"],
    "agentix.tools.gateway.result_limiter": ["limit_value"],
    "agentix.tools.gateway.human_review": ["HumanReviewGate", "classify_risk", "preflight_review", "RiskLevel"],
    "agentix.tools.adapters.search": ["SearchTool"],
}


class TestTargetLayout:
    @pytest.mark.parametrize("rel", TARGET_FILES)
    def test_target_file_exists(self, rel: str):
        assert (REPO / rel).is_file(), f"M1 目标位缺失: {rel}"

    def test_target_does_not_contain_guardrails(self):
        # M1 目标位不含 guardrails（M3 已迁 agentix/policies/guardrails/；
        # 迁移期的 capabilities/tools/guardrails.py shim 已随 M6 删除）
        assert not (REPO / "agentix" / "tools" / "guardrails.py").exists()
        assert not (REPO / "agentix" / "capabilities" / "tools" / "guardrails.py").exists()
        assert (REPO / "agentix" / "policies" / "guardrails" / "guardrails.py").is_file()


class TestOldPathShimsDeleted:
    """M6 清尾：16 个 ``_TARGET`` 转发 shim 全数删除。"""

    @pytest.mark.parametrize("rel,new_dotted", sorted(M1_DELETED_SHIMS.items()))
    def test_old_path_file_deleted(self, rel: str, new_dotted: str):
        assert not (REPO / rel).exists(), f"M6 已完成但旧路径 shim 仍存在: {rel}"

    @pytest.mark.parametrize("rel,new_dotted", sorted(M1_DELETED_SHIMS.items()))
    def test_old_path_import_raises(self, rel: str, new_dotted: str):
        old_dotted = rel[:-3].replace("/", ".")
        with pytest.raises(ImportError):
            importlib.import_module(old_dotted)

    @pytest.mark.parametrize("new_dotted,names", sorted(SYMBOLS.items()))
    def test_canonical_symbol_present(self, new_dotted: str, names: List[str]):
        new_mod = importlib.import_module(new_dotted)
        for name in names:
            assert hasattr(new_mod, name), f"规范位 {new_dotted}.{name} 缺失"

    def test_lazy_package_shim_is_gone(self):
        """PEP 562 惰性包门面（迁移期形态）已随 M6 目录删除。"""
        assert not (REPO / "agentix" / "capabilities" / "tools").exists()
        with pytest.raises(ImportError):
            importlib.import_module("agentix.capabilities.tools")


class TestPackageBehavior:
    def test_new_home_exports_the_public_face(self):
        import agentix.tools as new_pkg

        for name in ("get_tool", "registry", "list_tool_specs", "ToolSpec"):
            assert hasattr(new_pkg, name), f"agentix.tools 缺公开面 {name}"
        assert "ToolSpec" in new_pkg.__all__

    def test_registration_side_effect_on_canonical_path(self):
        import agentix.tools.definitions as defs

        assert defs.registry.has_tool("search"), "import 规范位必须完成内置工具注册"

    def test_new_home_init_does_not_drag_old_path(self):
        """fresh 进程 ``import agentix.tools`` → 不得反向拖起 agentix.capabilities（切断旧路径依赖的证明）。"""
        code = (
            "import sys\n"
            "import agentix.tools\n"
            "assert 'agentix.capabilities' not in sys.modules, '新家不得反向依赖旧路径'\n"
            "assert 'agentix.capabilities.tools' not in sys.modules\n"
            "assert agentix.tools.registry.has_tool('search'), '注册副作用随规范位落地'\n"
            "print('SELF_CONTAINED_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        assert "SELF_CONTAINED_OK" in proc.stdout

    def test_old_path_first_import_fails_then_canonical_works(self):
        """旧路径首行 import（迁移期现实主路径）现已 ``ImportError``——
        规范位是唯一入口，注册副作用只随 ``agentix.tools`` 落地。"""
        code = (
            "import importlib\n"
            "try:\n"
            "    importlib.import_module('agentix.capabilities.tools.executor')\n"
            "except ImportError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('旧路径仍可导入')\n"
            "import agentix.tools\n"
            "assert agentix.tools.registry.has_tool('search')\n"
            "print('OLDFIRST_GONE_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert proc.returncode == 0, proc.stderr
        assert "OLDFIRST_GONE_OK" in proc.stdout


class TestGuardrailsMigrated:
    """M3（2026-10-02）：guardrails 已迁 ``agentix/policies/guardrails/``；
    迁移期 ``capabilities/tools/guardrails.py`` 的 ``sys.modules`` 别名式 shim
    已随 M6 删除，executor 直连规范位。"""

    def test_capabilities_guardrails_shim_is_gone(self):
        with pytest.raises(ImportError):
            importlib.import_module("agentix.capabilities.tools.guardrails")

    def test_new_home_is_real_implementation(self):
        text = (REPO / "agentix" / "policies" / "guardrails" / "guardrails.py").read_text(
            encoding="utf-8"
        )
        assert "_TARGET =" not in text
        assert "def get_tool_guardrails" in text

    def test_executor_imports_guardrails_canonical_path(self):
        """executor（tools 位）M6 起**直连规范位** import：迁移期经 capabilities
        shim 转发以满足 p66 守卫 #3；M6 删 shim 后 p66 以
        ``TOOLS_GUARDRAILS_EDGE_ALLOWED`` 窄豁免登记该边（蓝图 §5 Tool
        Gateway 链含 Guardrails 步）。"""
        text = (REPO / "agentix" / "tools" / "gateway" / "executor.py").read_text(
            encoding="utf-8"
        )
        assert "agentix.policies.guardrails" in text
        assert "agentix.capabilities.tools" not in text
