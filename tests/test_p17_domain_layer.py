"""P1-7 验收测试：``domain/`` 层骨架（ports / policies）+ 依赖规则守卫。

方案要求（P1-7 / 2.2 节）：
1. 补齐 ``domain/`` 三子包：``models/`` ``ports/`` ``policies/``
2. 规则 2：``application`` 依赖 ``domain.ports`` 抽象，不得直接引用基础设施符号
3. 规则 3：``domain`` **不得**依赖任何 ``infrastructure`` / ``interfaces`` 符号
4. 规则 4：``infrastructure`` 实现 ``domain.ports`` 中定义的 Protocol

所有断言均为**静态源码扫描** + **子进程导入观测**（不启服务、不连外部依赖）。
"""

import ast
import subprocess
import sys
from pathlib import Path

from agentix.application.services.chat_service import ChatService
from agentix.domain.policies import is_retryable as policy_is_retryable
from agentix.domain.ports import (
    GraphProviderPort,
    MessageRepositoryPort,
    SessionRepositoryPort,
)
# P2-6 (T6)：Repository 默认实现已从 chat_service 下沉至 infrastructure
from agentix.infrastructure.persistence.repositories import (
    MessageRepository,
    SessionRepository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAIN_DIR = PROJECT_ROOT / "agentix" / "domain"

# P2-6 T7：**零豁免**。此前唯一的例外是 `domain/models/__init__.py`
# （legacy re-export 代理，反向依赖 interfaces + infrastructure）。
# 该文件已按 P2-5 流程删除，故 domain 下不再允许任何例外——
# 下面的断言从「只允许一条」收紧为「一条都不允许」。
UPSTREAM_PREFIXES = ("agentix.infrastructure", "agentix.interfaces")

# 领域层禁止出现的第三方根包（domain 必须零第三方依赖）
FORBIDDEN_THIRD_PARTY = {
    "sqlalchemy",
    "langchain",
    "langchain_core",
    "langchain_neo4j",
    "langgraph",
    "fastapi",
    "pydantic",
    "loguru",
    "aiohttp",
    "redis",
    "pymilvus",
    "openai",
}


def _domain_files():
    """domain 下所有 .py（**零豁免**：不再跳过任何文件）。"""
    yield from sorted(DOMAIN_DIR.rglob("*.py"))


def _imported_modules(path: Path):
    """AST 提取绝对导入的模块名集合（相对导入只取 module 段）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
    return modules


def _run_in_subprocess(code: str) -> str:
    """在干净子进程里执行代码并回传 stdout（导入链观测必须隔离）。"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"子进程失败：\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# 1. 骨架完整性
# ---------------------------------------------------------------------------


class TestSkeleton:
    def test_domain_has_expected_structure(self):
        """P2-6 T7 后：domain 含 ports / policies 两个既有子包 + 顶层模块。

        `models` 已删除（legacy 代理），故不再出现在断言里。
        M0（统一执行链 Stage M）：新增 agent / runtime / tool 三个纯契约子包。
        """
        for name in ("ports", "policies"):
            assert (DOMAIN_DIR / name / "__init__.py").is_file(), f"缺少 domain/{name}/"
        for name in ("errors.py", "status.py", "tracing.py", "__init__.py"):
            assert (DOMAIN_DIR / name).is_file(), f"缺少 domain/{name}"
        assert not (DOMAIN_DIR / "models").exists(), "legacy 代理 domain/models/ 应已删除"

    def test_domain_m0_contract_subpackages_exist(self):
        """M0：agent / runtime / tool 三个契约子包（拆分自散落的纯类型）。"""
        for name in ("agent", "runtime", "tool"):
            assert (DOMAIN_DIR / name / "__init__.py").is_file(), f"缺少 domain/{name}/"
            assert (DOMAIN_DIR / name).is_dir(), f"缺少 domain/{name}/"

    def test_errors_stays_in_domain(self):
        """方案的「已有内容迁移」示例：错误分类词汇表在 domain 层。"""
        assert (DOMAIN_DIR / "errors.py").is_file()

    def test_ports_exports_protocols(self):
        import agentix.domain.ports as ports

        # P2-6 (T2)：CRUD 载体 TypedDict（payloads）+ RetrievalPort（R1.x）。
        # Agent Core 2.0 Phase 1：agent_core 七端口（Planner/Verifier/…/Rollback）。
        assert set(ports.__all__) == {
            "GraphProviderPort",
            "MessageRepositoryPort",
            "SessionRepositoryPort",
            "SessionCreatePayload",
            "MessageCreatePayload",
            "SessionSnapshotPayload",
            "ConversationSnapshotPayload",
            "RetrievalPort",
            "RetrievalChunk",
            "RetrievalResult",
            "RetrievalCitation",
            "IndexStatus",
            "PlannerPort",
            "VerifierPort",
            "ToolSelectorPort",
            "MemoryPort",
            "ContextPort",
            # Phase 22：L4 语义压缩端口（Context 家族可选扩展，零第三方）
            "CompactorPort",
            "EventPort",
            "RollbackPort",
        }

    def test_payloads_are_pure_stdlib_typed_dicts(self):
        """P2-6：载体必须是 stdlib TypedDict，导入零第三方（AST 判定非子串）。"""
        modules = _imported_modules(DOMAIN_DIR / "ports" / "payloads.py")
        roots = {m.split(".")[0] for m in modules}
        assert roots <= {"typing"}, f"payloads.py 出现非 stdlib 导入: {roots}"
        src = (DOMAIN_DIR / "ports" / "payloads.py").read_text(encoding="utf-8")
        assert "TypedDict" in src


# ---------------------------------------------------------------------------
# 2. 依赖规则 3：domain 不得依赖 infrastructure / interfaces
# ---------------------------------------------------------------------------


class TestDependencyRule3:
    def test_no_domain_file_imports_upstream_statically(self):
        offenders = {}
        for path in _domain_files():
            hits = [
                m
                for m in _imported_modules(path)
                if m.startswith(UPSTREAM_PREFIXES)
            ]
            if hits:
                offenders[path.relative_to(PROJECT_ROOT).as_posix()] = hits
        assert offenders == {}, f"domain 出现上层依赖：{offenders}"

    def test_domain_files_have_no_third_party_imports(self):
        offenders = {}
        for path in _domain_files():
            roots = {m.split(".")[0] for m in _imported_modules(path)}
            hits = sorted(roots & FORBIDDEN_THIRD_PARTY)
            if hits:
                offenders[path.relative_to(PROJECT_ROOT).as_posix()] = hits
        assert offenders == {}, f"domain 出现第三方依赖：{offenders}"

    def test_importing_domain_does_not_drag_upstream_at_runtime(self):
        """运行时锚点：``import agentix.domain.errors`` 后上层模块**零**进 sys.modules。

        这是 import 期修复（``domain/__init__.py`` 改 PEP 562 惰性导出）的直接证据。
        """
        code = (
            "import sys\n"
            "import agentix.domain.errors\n"
            "import agentix.domain.ports\n"
            "import agentix.domain.policies\n"
            "leaked = sorted(m for m in sys.modules "
            "if m.startswith(('agentix.infrastructure', 'agentix.interfaces')))\n"
            "print(len(leaked))\n"
        )
        assert _run_in_subprocess(code) == "0"

    def test_legacy_proxy_is_gone(self):
        """P2-6 T7：legacy 代理必须已删除，且 `domain.models` 不再可导入。"""
        assert not (DOMAIN_DIR / "models").exists(), (
            "domain/models/ 仍存在——P2-6 T7 要求删除该 legacy 代理"
        )
        code = (
            "import sys, warnings\n"
            "with warnings.catch_warnings():\n"
            "    warnings.simplefilter('ignore')\n"
            "    try:\n"
            "        import agentix.domain.models  # noqa: F401\n"
            "    except ModuleNotFoundError:\n"
            "        print('gone')\n"
            "    else:\n"
            "        print('still-importable')\n"
        )
        assert _run_in_subprocess(code) == "gone"

    def test_no_exemption_remains_in_guard(self):
        """守卫本身不得再留豁免分支（防「删了文件但忘了收紧守卫」）。

        用 **AST** 判定而非字符串匹配：字符串匹配会命中本断言自身的字面量
        （自我指涉），也容易被注释绕过。
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        # ① 全文件不得再出现 LEGACY_PROXY 标识符
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "LEGACY_PROXY" not in names, "守卫里仍残留 LEGACY_PROXY 豁免"

        # ② `_domain_files` 必须是「无条件产出全部文件」——不得有跳过分支
        target = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_domain_files"
            ),
            None,
        )
        assert target is not None, "守卫缺少 _domain_files 函数"
        assert not any(
            isinstance(node, (ast.If, ast.Continue)) for node in ast.walk(target)
        ), "_domain_files 仍存在按条件跳过文件的逻辑（豁免未清除）"


# ---------------------------------------------------------------------------
# 3. 惰性导出（PEP 562）
# ---------------------------------------------------------------------------


class TestDomainPackageSurface:
    """P2-6 T7 后的 domain 包表面：**不再暴露 `models`**（惰性导出也随之移除）。

    历史：`domain/__init__.py` 曾用 PEP 562 惰性导出 `models` 兼容代理。
    代理删除后，惰性导出已无存在意义，一并移除——`domain` 现在只导出
    `errors` / `ports` / `policies` 三个子模块。
    """

    def test_domain_init_has_no_eager_upstream_import(self):
        """`domain/__init__.py` 不得有模块级（或任何）上层导入。"""
        src = (DOMAIN_DIR / "__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level_imports.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_level_imports.add(node.module)
                else:
                    top_level_imports.update(a.name for a in node.names)
        assert not any(m.startswith("agentix") for m in top_level_imports)

    def test_domain_init_no_longer_exports_models(self):
        """`models` 已从 `__all__` 与惰性导出中移除。"""
        src = (DOMAIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert '"models"' not in src and "'models'" not in src
        assert "import_module" not in src, "惰性导出残留（代理已删除，不应再有）"

    def test_models_attribute_is_gone(self):
        code = (
            "import agentix.domain as d\n"
            "print(hasattr(d, 'models'))\n"
        )
        assert _run_in_subprocess(code) == "False"

    def test_domain_exports_the_real_subpackages(self):
        code = (
            "import agentix.domain.errors\n"
            "import agentix.domain.ports\n"
            "import agentix.domain.policies\n"
            # M0：三个契约子包（agent / runtime / tool）
            "import agentix.domain.agent\n"
            "import agentix.domain.runtime\n"
            "import agentix.domain.tool\n"
            "print('ok')\n"
        )
        assert _run_in_subprocess(code) == "ok"

    def test_plain_domain_import_has_no_deprecation_warning(self):
        code = (
            "import warnings\n"
            "with warnings.catch_warnings(record=True) as w:\n"
            "    warnings.simplefilter('always')\n"
            "    import agentix.domain\n"
            "    print(any(issubclass(x.category, DeprecationWarning) for x in w))\n"
        )
        assert _run_in_subprocess(code) == "False"


# ---------------------------------------------------------------------------
# 4. 依赖规则 4：infrastructure 实现 domain.ports 的 Protocol
# ---------------------------------------------------------------------------


class _FakeSessionRepo:
    def get(self, db, *, id):
        return None

    def create(self, db, *, obj_in):
        return None

    def update_activity(self, db, *, session_id):
        return None


class _FakeMessageRepo:
    def get_latest_by_session(self, db, *, session_id):
        return None

    def create(self, db, *, obj_in):
        return None

    def get_by_session(self, db, *, session_id, skip=0, limit=100):
        return []

    def delete_by_session(self, db, *, session_id):
        return 0


class TestPortsAreImplemented:
    def test_protocols_are_runtime_checkable(self):
        assert isinstance(SessionRepository(), SessionRepositoryPort)
        assert isinstance(MessageRepository(), MessageRepositoryPort)

    def test_duck_typed_fakes_satisfy_ports(self):
        """注入点只依赖契约：任意满足方法签名的假实现都可用（P1-6 验收 2 的底座）。"""
        assert isinstance(_FakeSessionRepo(), SessionRepositoryPort)
        assert isinstance(_FakeMessageRepo(), MessageRepositoryPort)

    def test_object_missing_methods_fails_port_check(self):
        class _Incomplete:
            def get(self, db, *, id):
                return None

        assert not isinstance(_Incomplete(), SessionRepositoryPort)

    def test_graph_getter_satisfies_graph_provider_port(self):
        service = ChatService(graph_getter=lambda: object())
        assert isinstance(service._graph_getter, GraphProviderPort)
        assert isinstance(lambda: object(), GraphProviderPort)

    def test_chat_service_annotations_reference_ports(self):
        """注入点类型必须写成 domain.ports 的 Protocol（而非具体实现）。"""
        import inspect

        src = inspect.getsource(ChatService.__init__)
        assert "SessionRepositoryPort" in src
        assert "MessageRepositoryPort" in src


# ---------------------------------------------------------------------------
# 5. 依赖规则 2：application 不直接引用基础设施错误分类
# ---------------------------------------------------------------------------


class TestDependencyRule2:
    def test_policy_is_reexported_with_identical_identity(self):
        from agentix.infrastructure.llm.retry import is_retryable as legacy

        assert legacy is policy_is_retryable

    def test_policy_behaviour_preserved(self):
        assert policy_is_retryable(ConnectionError("connection refused")) is True
        assert policy_is_retryable(TimeoutError("timed out")) is True
        assert policy_is_retryable(ValueError("bad input")) is False
        assert policy_is_retryable(Exception("invalid api key")) is False
        # 先否后肯：文本同时含两类特征时以「不可重试」优先
        assert policy_is_retryable(Exception("400 invalid_request after 500")) is False

    def test_chat_service_no_longer_imports_infrastructure_llm(self):
        src = (
            PROJECT_ROOT / "agentix" / "application" / "services" / "chat_service.py"
        ).read_text(encoding="utf-8")
        assert "from agentix.infrastructure.llm" not in src
        assert "from agentix.domain.policies" in src

    def test_retry_module_keeps_backward_compatible_path(self):
        """infra 依赖 domain（方向正确），且旧导入路径保持可用。"""
        src = (
            PROJECT_ROOT / "agentix" / "infrastructure" / "llm" / "retry.py"
        ).read_text(encoding="utf-8")
        assert "from agentix.domain.policies.error_classification import" in src
        # 不得再有本地重复定义
        assert "def is_retryable" not in src


# ---------------------------------------------------------------------------
# 6. P2-6 (T5)：infrastructure → interfaces 零容忍守卫
#
# 背景：P2-6 T1-T3 前，infrastructure/persistence/crud 的三个文件反向 import
# interfaces 的 HTTP 契约 schema（ChatMessageCreate / ChatSessionCreate /
# ChatSessionSnapshotCreate / ConversationHistorySnapshotCreate）。T3 将 CRUD
# 载体改为 Mapping 后已全部消除。本守卫把该成果钉死：infrastructure 下任何
# 对 ``agentix.interfaces.*`` 的导入（含间接子模块）都判违规。
# ---------------------------------------------------------------------------

INFRA_DIR = PROJECT_ROOT / "agentix" / "infrastructure"
INTERFACES_MODULE_PREFIX = "agentix.interfaces"


def _infrastructure_interfaces_offenders(root=None):
    """扫描 infrastructure 下所有指向 agentix.interfaces 的导入，返回违规表。

    :param root: 扫描根目录；默认真实 ``agentix/infrastructure``。
        可传入临时目录用于**对抗性验证**——避免把探针写进源码树
        （进程中断会残留文件，进而毒化 ``test_no_infrastructure_file_imports_interfaces``）。
    """
    root = Path(root) if root is not None else INFRA_DIR
    # 报告基准按"是不是真实源码树"决定，而不是按 relative_to 会不会抛：
    # 用 try/except 时，只要 pytest 的 basetemp 落在仓库内（例如 --basetemp=.tmp/...），
    # 临时探针目录就能相对仓库根解析成功，键名变成一长串 basetemp 路径，
    # test_probe_import_would_trip_the_guard 随即 KeyError——换台机器就红的那种红。
    base = PROJECT_ROOT if root == INFRA_DIR else root
    offenders = {}
    for path in sorted(root.rglob("*.py")):
        hits = [
            m
            for m in _imported_modules(path)
            if m == INTERFACES_MODULE_PREFIX
            or m.startswith(INTERFACES_MODULE_PREFIX + ".")
        ]
        if hits:
            offenders[path.relative_to(base).as_posix()] = hits
    return offenders


class TestInfrastructureInterfacesBan:
    def test_no_infrastructure_file_imports_interfaces(self):
        offenders = _infrastructure_interfaces_offenders()
        assert offenders == {}, f"infrastructure 出现 interfaces 依赖：{offenders}"

    def test_probe_import_would_trip_the_guard(self, tmp_path):
        """对抗性验证：造一个违规探针证明守卫真的会绊倒。

        教训（批次 7 事故 2）：探针绝不改现有文件。
        本用例进一步把探针放进 **临时目录**（而非源码树）——原因：
        若进程在写入与删除之间中断，残留文件会毒化
        ``test_no_infrastructure_file_imports_interfaces``；
        且源码树写入在并行测试 / 只读检出下都不可靠。
        扫描函数与真实守卫完全相同，只是换了 ``root``。
        """
        root = tmp_path / "infrastructure"
        (root / "sub").mkdir(parents=True)
        (root / "clean_module.py").write_text("import os\n", encoding="utf-8")
        (root / "sub" / "_probe_interfaces_tmp.py").write_text(
            "from agentix.interfaces.http.models.chat_session import (  # noqa: F401\n"
            "    ChatSessionCreate,\n"
            ")\n",
            encoding="utf-8",
        )

        offenders = _infrastructure_interfaces_offenders(root=root)
        assert "_probe_interfaces_tmp.py" in {
            Path(k).name for k in offenders
        }, "守卫未能绊倒违规探针——守卫失效！"
        assert offenders["sub/_probe_interfaces_tmp.py"] == [
            "agentix.interfaces.http.models.chat_session"
        ]
        # 干净文件不得被误报
        assert "clean_module.py" not in {Path(k).name for k in offenders}

    def test_real_infrastructure_tree_is_clean(self):
        """真实源码树必须零违规（探针已移至临时目录，不再污染源码树）。"""
        assert _infrastructure_interfaces_offenders() == {}
