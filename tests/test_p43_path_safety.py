# -*- coding: utf-8 -*-
"""P43：路径安全守卫。

背景（2026-09 深挖审计）——此前只审过"上传大小上限 / 后缀白名单"这类**表面**防护，
路径拼接本身从未被检查。实测发现两件事：

1. **真漏洞**：``kb_ingest`` 的 ``/ingest/excel/upload`` 直接把客户端给的
   ``file.filename`` 拼进临时目录（``Path(temp_dir) / file.filename``）。
   Windows 实测两种逃逸：``../../evil.xlsx`` 穿透出目录；
   ``C:/Windows/evil.xlsx`` 带盘符的**绝对路径会整体替换** base。
   而该服务**无鉴权**（``api/deps.py`` 只注入配置）且端口发布到宿主
   （compose ``8100:8000``，``--host 0.0.0.0``）⇒ 等于任意路径写入。

2. **地雷（非活跃漏洞）**：``agentix`` 侧 ``upload._safe_resolve`` 的包含性判定
   用的是 ``str(resolved).startswith(str(base))``。这是**字符串前缀**比较，
   会放过 ``/app/uploads_evil`` 这类"前缀相同的兄弟目录"。当前因为前面有
   分隔符黑名单（``..`` ``/`` ``\\``）而侥幸安全，但谁放宽那份黑名单就会立刻变成真漏洞。

本文件把两条都钉住：一条行为测试 + 一条 AST 守卫（防同类新代码再写出来）。

**本守卫不覆盖什么（诚实说明）**：只抓"拼接点直接出现 ``.filename``"这一形态。
间接传播（如 ``n = f"{fid}_{f.filename}"`` 之后再把 ``n`` 拼进路径）抓不到——
那条线靠 ``_safe_resolve`` / ``_safe_temp_path`` 的**行为测试**兜，
而不是靠这层模式匹配。所以下面行为测试是主力，AST 守卫是补充。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. kb_ingest：_safe_temp_path 的行为（真漏洞的回归）
# ---------------------------------------------------------------------------

#: 穿越/逃逸载荷 → 期望文件名（落在 temp_dir 内）
ESCAPE_PAYLOADS = [
    ("../../evil.xlsx", "evil.xlsx"),
    ("..\\..\\evil.xlsx", "evil.xlsx"),
    ("/etc/passwd.xlsx", "passwd.xlsx"),
    ("C:/Windows/evil.xlsx", "evil.xlsx"),
    ("C:\\Windows\\evil.xlsx", "evil.xlsx"),
    # 盘符相对路径：Windows 上 ``Path("C:evil.xlsx").name`` 剥掉盘符得 evil.xlsx；
    # POSIX 无盘符概念，name 保留 ``C:evil.xlsx``，冒号随后被
    # ``_UNSAFE_FILENAME_CHARS`` 换成下划线 → ``C_evil.xlsx``。
    # 两种结果都**落在 temp_dir 内**（本测试的核心判定），差别只是净化后文件名。
    ("C:evil.xlsx", "evil.xlsx" if sys.platform == "win32" else "C_evil.xlsx"),
    ("sub/dir/evil.xlsx", "evil.xlsx"),
    ("....//....//evil.xlsx", "evil.xlsx"),
    ("ok.xlsx", "ok.xlsx"),
]

#: 必须 400 的载荷
REJECT_PAYLOADS = ["", "   ", ".", "..", "NUL.xlsx", "con.xls", "COM1.xlsx"]

#: 供 ``test_old_implementation_would_have_escaped`` 复现"原实现真的会逃逸"的载荷。
#:
#: 必须按平台选**在本平台真能逃逸**的载荷，否则那条防空真断言会反噬（2026-10-06
#: 实测：CI 的 Linux 上 ``Path(tmp) / "C:/Windows/evil.xlsx"`` 里 ``C:`` 只是普通
#: 目录名、根本不逃逸，断言"本应复现逃逸"直接失败）：
#:
#: - 绝对路径 ``/etc/passwd.xlsx``：POSIX 整体替换 base；Windows 得同盘根路径，
#:   两者都落在 temp_dir 之外——**两平台通用**；
#: - ``../../evil.xlsx``：词法穿越，两平台通用；
#: - ``C:/Windows/evil.xlsx``（仅 Windows）：盘符整体替换 base——原漏洞（2026-09
#:   深挖审计）的最初形态，必须留在本平台钉死。
NAIVE_ESCAPE_PAYLOADS = ["/etc/passwd.xlsx", "../../evil.xlsx"]
if sys.platform == "win32":
    NAIVE_ESCAPE_PAYLOADS.insert(0, "C:/Windows/evil.xlsx")


class TestKbIngestSafeTempPath:
    def test_escape_payloads_land_inside_temp_dir(self, tmp_path):
        """★ 核心回归：任何穿越载荷都必须落在 temp_dir 内。

        这是原漏洞的判定式——原实现 ``Path(tmp) / filename`` 对前 8 个载荷
        全部会逃出 temp_dir（实测），修复后必须全部失效。
        """
        from kb_service.api.routes import _safe_temp_path

        temp_dir = str(tmp_path / "uploads")
        (tmp_path / "uploads").mkdir()
        outside = []

        for payload, expect_name in ESCAPE_PAYLOADS:
            target = _safe_temp_path(temp_dir, payload)
            root = Path(temp_dir).resolve()
            if not target.is_relative_to(root):
                outside.append(f"{payload!r} -> {target}（逃出 {root}）")
            if target.name != expect_name:
                outside.append(f"{payload!r} -> name={target.name!r}，期望 {expect_name!r}")

        assert not outside, "以下载荷未落在 temp_dir 内或文件名不符：\n  " + "\n  ".join(outside)

    def test_dangerous_payloads_are_rejected(self, tmp_path):
        """空名 / 点名 / Windows 保留设备名必须直接 400，而不是"尽力猜一个名字"。"""
        from fastapi import HTTPException

        from kb_service.api.routes import _safe_temp_path

        temp_dir = str(tmp_path)
        for payload in REJECT_PAYLOADS:
            with pytest.raises(HTTPException) as exc:
                _safe_temp_path(temp_dir, payload)
            assert exc.value.status_code == 400, f"{payload!r} 应 400，实得 {exc.value.status_code}"

    def test_control_and_reserved_chars_are_scrubbed(self, tmp_path):
        """控制字符与 Windows 保留字符要被替换掉，而不是留在文件名里。"""
        from kb_service.api.routes import _safe_temp_path

        target = _safe_temp_path(str(tmp_path), "a\x00b|c<d>e.xlsx")
        assert "\x00" not in target.name
        for ch in '|<>':
            assert ch not in target.name

    def test_old_implementation_would_have_escaped(self, tmp_path):
        """★ 防空真：在同一台机器上复现**原实现**的逃逸，证明修复真的在起作用。

        原实现写作 ``Path(temp_dir) / file.filename``（不带 basename）。
        如果这条断言不成立，说明本文件的载荷选错了，上面的"全都没逃逸"
        就是空转通过。

        载荷按平台取"在本平台真能逃逸"者（见 ``NAIVE_ESCAPE_PAYLOADS`` 注释），
        保证 Windows 与 CI 的 Linux 两边都能复现。
        """
        from kb_service.api.routes import _safe_temp_path

        temp_dir = str(tmp_path)
        root = Path(temp_dir).resolve()

        for payload in NAIVE_ESCAPE_PAYLOADS:
            naive = Path(temp_dir) / payload  # ← 原实现的写法（未净化）
            assert not naive.resolve().is_relative_to(root), (
                f"载荷 {payload!r} 本应复现原漏洞逃逸——用例构造有误，"
                "请换一个能真正逃逸的载荷，否则本文件失去意义"
            )
            assert _safe_temp_path(temp_dir, payload).is_relative_to(root)

    def test_unresolved_is_relative_to_is_not_enough(self, tmp_path):
        """``..`` 逃逸必须靠 ``resolve()`` 抓——光有 ``is_relative_to`` 抓不到。

        这是本文件里最容易写错的一条：``is_relative_to`` 是**纯词法**比较，
        未 ``resolve()`` 时 ``<root>/../../evil`` 依然"看起来"在 root 里。
        所以"用了 ``is_relative_to`` 就安全"是错的结论——
        ``resolve()`` 才是真正干活的那一步（两个 helper 都先 resolve）。
        """
        root = tmp_path / "t"
        root.mkdir()

        lexical = root / "../../evil.xlsx"
        assert lexical.is_relative_to(root), "未 resolve 时应是词法相通（正是危险所在）"
        assert not lexical.resolve().is_relative_to(root), "resolve 后必须暴露逃逸"

    def test_scanner_targets_exist(self):
        """防空真：本文件依赖的两个 helper 若被重构掉，守卫要显式报警。"""
        from agentix.interfaces.http.v1.upload import _safe_resolve
        from kb_service.api.routes import _safe_temp_path

        assert callable(_safe_temp_path)
        assert callable(_safe_resolve)


# ---------------------------------------------------------------------------
# 2. agentix：_safe_resolve 的前缀语义
# ---------------------------------------------------------------------------


class TestAgentixSafeResolve:
    def test_traversal_payloads_rejected(self, tmp_path):
        from fastapi import HTTPException

        from agentix.interfaces.http.v1.upload import _safe_resolve

        for payload in ["../evil.txt", "..\\evil.txt", "a/b.txt", "a\\b.txt", "", "\x00.txt"]:
            with pytest.raises(HTTPException) as exc:
                _safe_resolve(tmp_path, payload)
            assert exc.value.status_code == 400, f"{payload!r} 应 400"

    def test_normal_name_resolves_under_base(self, tmp_path):
        from agentix.interfaces.http.v1.upload import _safe_resolve

        got = _safe_resolve(tmp_path, "report.pdf")
        assert got.parent == tmp_path.resolve()
        assert got.name == "report.pdf"

    def test_uses_path_semantics_not_string_prefix(self):
        """★ 地雷守卫：``_safe_resolve`` 的包含性判定必须是**路径语义**。

        字符串前缀比较会放过前缀相同的兄弟目录：
        若 base 为 ``/app/uploads``，则 ``/app/uploads_evil/x`` 也满足
        ``startswith('/app/uploads')``。谁改回 ``startswith``，这里就红。

        ⚠️ 用 **AST** 而不是 ``in src``：本文件初版写成
        ``assert "startswith(str(base" not in src``，结果被 ``upload.py`` 里
        **解释这个 bug 的 docstring** 自己触发失败——字符串匹配分不清
        "代码"和"注释里提到这段代码"。走 AST 只查 ``ast.Call`` 节点，
        注释与 docstring 天然不参与。
        """
        path = REPO_ROOT / "agentix/interfaces/http/v1/upload.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        func = next(
            (n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name == "_safe_resolve"),
            None,
        )
        assert func is not None, "_safe_resolve 不见了——本守卫的前提失效"

        startswith_calls = [
            ast.unparse(n)
            for n in ast.walk(func)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "startswith"
        ]
        assert not startswith_calls, (
            "路径包含性判定用了 startswith（字符串前缀比较），"
            "它会放过 /app/uploads_evil 这类兄弟目录，请改回 Path.is_relative_to：\n  "
            + "\n  ".join(startswith_calls)
        )
        assert "is_relative_to" in ast.unparse(func), "路径包含性判定必须用 Path.is_relative_to"

    def test_sibling_prefix_dir_would_have_passed_old_check(self, tmp_path):
        """把"为什么必须改"做成可执行的证据，而不是注释里的说法。"""
        base = tmp_path / "uploads"
        base.mkdir()
        sibling = tmp_path / "uploads_evil"
        sibling.mkdir()
        candidate = sibling / "x.txt"

        # 旧写法的判定：字符串前缀 —— 会**通过**（这就是地雷）
        assert str(candidate).startswith(str(base)), "构造失败：该用例本应复现旧写法的漏洞"
        # 新写法的判定：路径语义 —— 正确拒绝
        assert not candidate.is_relative_to(base)


# ---------------------------------------------------------------------------
# 2.5 任意文件读取：客户端可控的 file_path / image_path 必须被约束到上传根
# ---------------------------------------------------------------------------


class TestClientControlledReadPathsAreConfined:
    """★ 这是本轮最严重的一条。

    ``agentix/interfaces/http/v1/chat.py`` 把 ``file_path`` / ``image_path`` 作为
    **请求体字段**（还有一个 ``Query`` 版本）暴露出去，值经
    ``chat_service`` 塞进 ``config["configurable"]``，最终被
    ``file_handler.create_file_query`` 与 ``image_handler`` 直接读盘。
    主服务 ``REQUIRE_API_KEY`` 默认 **False**（只打 warning），
    于是默认部署下无需任何凭据即可读取进程可读的任意文件
    （受 ``FILE_UPLOAD_MAX_MB`` 默认 2MB 限制），内容随 LLM 回复外泄。
    """

    def test_policy_rejects_out_of_root(self, tmp_path):
        from agentix.domain.policies.upload_paths import resolve_within_root

        root = tmp_path / "uploads"
        root.mkdir()
        outside = tmp_path / "secret.env"
        outside.write_text("API_KEY=leak", encoding="utf-8")

        assert resolve_within_root(root, outside) is None, "上传根之外的文件必须被拒"
        assert resolve_within_root(root, tmp_path / ".." / "secret.env") is None

    def test_policy_allows_in_root(self, tmp_path):
        from agentix.domain.policies.upload_paths import resolve_within_root

        root = tmp_path / "uploads"
        root.mkdir()
        inside = root / "fid_a.txt"
        inside.write_text("ok", encoding="utf-8")

        got = resolve_within_root(root, inside)
        assert got is not None
        assert got == inside.resolve()

    def test_policy_rejects_sibling_prefix_dir(self, tmp_path):
        """前缀相同的兄弟目录不能算"在里面"——这正是 startswith 会犯的错。"""
        from agentix.domain.policies.upload_paths import resolve_within_root

        root = tmp_path / "uploads"
        root.mkdir()
        evil = tmp_path / "uploads_evil"
        evil.mkdir()
        (evil / "x.txt").write_text("x", encoding="utf-8")

        assert resolve_within_root(root, evil / "x.txt") is None

    def test_policy_is_pure_and_stdlib_only(self):
        """领域层不得引入第三方依赖（与 test_p17 同口径，这里就地再钉一次）。"""
        import ast as _ast

        path = REPO_ROOT / "agentix/domain/policies/upload_paths.py"
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        roots = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, _ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {"__future__", "pathlib"}, f"领域策略引入了额外依赖：{sorted(roots)}"

    @pytest.mark.asyncio
    async def test_file_handler_rejects_out_of_root_without_reading(self, tmp_path, monkeypatch):
        """★ 端到端：越界的 file_path 必须被**早退**，连入库都不该尝试。

        载荷后缀刻意用 ``.txt``（受支持类型）——用 ``.env`` 之类会走到
        "类型不支持"分支，那样**旧代码也不会读文件**，用例就成空真了。
        """
        from agentix.application.agents import file_handler
        from agentix.application.agents.constants import FILE_NOT_FOUND_MESSAGE
        from agentix.config import settings

        root = tmp_path / "uploads"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("SUPER_SECRET_TOKEN=leak-me", encoding="utf-8")

        calls: list[dict] = []
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(root), raising=False)
        monkeypatch.setattr(file_handler, "KnowledgeGateway", _spy_gateway(calls))

        out = await file_handler.create_file_query(
            state=_empty_state(),
            config={"configurable": {"file_path": str(secret)}},
        )

        assert calls == [], f"越界文件被读取并送去入库——任意文件读取防护失效：{calls}"
        assert FILE_NOT_FOUND_MESSAGE in out["messages"][0].content

    @pytest.mark.asyncio
    async def test_image_handler_rejects_out_of_root(self, tmp_path, monkeypatch):
        """与 file_path 同源的第二个暴露面：``image_path`` 也必须被约束。

        越界时应在**碰 vision 配置之前**就返回 ``IMAGE_NOT_FOUND_MESSAGE``
        （否则会先因为未配置 VISION_* 而返回同一个话术，用例就分不清
        是"拦住了"还是"恰好没配"——所以这里同时断言不是被配置缺失拦的）。
        """
        from agentix.application.agents import image_handler
        from agentix.application.agents.constants import IMAGE_NOT_FOUND_MESSAGE
        from agentix.config import settings

        root = tmp_path / "uploads"
        root.mkdir()
        outside_img = tmp_path / "private.png"
        outside_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        monkeypatch.setattr(settings, "UPLOAD_DIR", str(root), raising=False)
        # 把 vision 配置设成"完整"——如果防护失效，流程会继续往下走而不是
        # 因为配置缺失返回同一句话术，从而让用例失去判别力。
        monkeypatch.setattr(settings, "VISION_API_KEY", "k", raising=False)
        monkeypatch.setattr(settings, "VISION_BASE_URL", "http://127.0.0.1:1", raising=False)
        monkeypatch.setattr(settings, "VISION_MODEL", "m", raising=False)

        out = await image_handler.create_image_query(
            state=_empty_state(),
            config={"configurable": {"image_path": str(outside_img)}},
        )
        assert IMAGE_NOT_FOUND_MESSAGE in out["messages"][0].content

    @pytest.mark.asyncio
    async def test_file_handler_still_accepts_in_root(self, tmp_path, monkeypatch):
        """★ 反向断言（同时证明探针本身有效）：上传根**之内**的文件必须仍被正常处理。

        没有这一条，上面那句 ``calls == []`` 可能只是因为探针坏了/路径不对，
        而不是因为防护生效——两条合起来才构成有意义的判别。
        """
        from agentix.application.agents import file_handler
        from agentix.application.agents.constants import FILE_NOT_FOUND_MESSAGE
        from agentix.config import settings

        root = tmp_path / "uploads"
        root.mkdir()
        good = root / "fid_notes.txt"
        good.write_text("hello-from-an-upload", encoding="utf-8")

        calls: list[dict] = []
        monkeypatch.setattr(settings, "UPLOAD_DIR", str(root), raising=False)
        monkeypatch.setattr(file_handler, "KnowledgeGateway", _spy_gateway(calls))

        out = await file_handler.create_file_query(
            state=_empty_state(),
            config={"configurable": {"file_path": str(good)}},
        )

        assert calls, "上传目录内的合法文件没有走到入库——修复把正常流程也拦住了"
        assert calls[0]["content"] == "hello-from-an-upload"
        assert FILE_NOT_FOUND_MESSAGE not in out["messages"][0].content


def _spy_gateway(calls: list):
    """返回一个假的 KnowledgeGateway 类：记录 ``add_document`` 的入参后返回 False。

    返回 ``False`` 让调用方走"保存到知识库失败"分支并**就此返回**，
    避免测试继续撞上后续的 Milvus/图谱依赖。
    """

    class _SpyGateway:
        def __init__(self, *args, **kwargs) -> None:  # noqa: D107
            pass

        async def add_document(self, *, doc_id, title, content, metadata=None) -> bool:
            calls.append({"doc_id": doc_id, "title": title, "content": content})
            return False

    return _SpyGateway


def _empty_state():
    """构造一个最小可用的 AgentState（全部字段都有默认值）。"""
    from agentix.orchestration.langgraph.lg_states import AgentState

    return AgentState(messages=[])


class TestTextIngestIsCurrentlyNonFunctional:
    """把一条**已实测确认**的定位钉在这里，避免有人误以为文本入库是好的。

    2026-09 实测（``create_file_query`` 传一个上传根内的 ``.txt``）::

        NotImplementedError: Direct document ingestion is not supported by
        KnowledgeGateway. Use the retrieval domain upload API
        (/api/v1/retrieval/documents).

    即 ``.txt/.md/.csv/.log/.json`` 分支**必然**走到 ``add_document`` 抛错，
    文本类文件问答功能不生效（用户会以为文件已进知识库，其实没有）。

    2026-09 第二轮修正（本类下半部分）——**原先把结论止步于"桩存在"是不够的**：
    它返回给用户的是 ``FILE_INGEST_ERROR_MESSAGE``（「文件导入出现异常，
    请稍后再试」），而这是**永久性**不支持。让用户"稍后再试"一个永远试不通的
    路径，比直接说"不支持"更糟。现已分流为
    ``FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE``，并在此加守卫防回退。

    **这条与路径安全的关系**：今天拦住"任意文件读取"的其实是这个桩——
    内容被读进内存后流程就死了，没机会外泄。属于**意外防护**。
    谁去实现文本入库（显然是代码本意），那个洞就会立刻打开。
    所以 ``resolve_within_root`` 那道根目录校验是**承重结构**，不是可选的纵深防御。

    本用例只固定"桩的行为 + 用户可见文案"，**不实现功能**——补齐文本入库
    需要在检索域新增一条非 PDF 的入库链路（含切分与索引管线改造），
    属产品决策，不在本次授权范围。
    """

    @pytest.mark.asyncio
    async def test_gateway_add_document_is_a_raising_stub(self):
        from agentix.application.services.knowledge_gateway import KnowledgeGateway

        with pytest.raises(NotImplementedError):
            await KnowledgeGateway().add_document(
                doc_id="x", title="t", content="c", metadata={}
            )

    @pytest.mark.asyncio
    async def test_gateway_ingest_text_is_a_raising_stub(self):
        """另一个同类桩：``ingest_text`` 同样直接抛，别只盯 add_document。"""
        from agentix.application.services.knowledge_gateway import KnowledgeGateway

        with pytest.raises(NotImplementedError):
            await KnowledgeGateway().ingest_text("some text")

    @pytest.mark.asyncio
    async def test_text_upload_reports_permanent_not_transient(self, tmp_path, monkeypatch):
        """★ 真人可见行为：文本上传必须报「不支持」，**不能**报「稍后再试」。

        用"抛 NotImplementedError 的假网关"而不是真网关：真网关构造函数会
        拉起 ``RetrievalPortAdapter``（重依赖），而本用例要验证的是
        ``create_file_query`` 的**分流逻辑**，不是网关内部。

        判别力来自同时断言两件事：拿到了准确文案 **且** 没拿到瞬时故障文案。
        只断言前者的话，若将来有人把两者合并成一句话术，用例仍会通过。
        """
        from agentix.application.agents import file_handler
        from agentix.application.agents.constants import (
            FILE_INGEST_ERROR_MESSAGE,
            FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE,
        )
        from agentix.config import settings

        root = tmp_path / "uploads"
        root.mkdir()
        note = root / "note.txt"
        note.write_text("some upload text", encoding="utf-8")

        calls: list[dict] = []

        class _StubGateway:
            def __init__(self, *args, **kwargs) -> None:  # noqa: D107
                pass

            async def add_document(self, *, doc_id, title, content, metadata=None) -> bool:
                calls.append({"doc_id": doc_id, "title": title, "content": content})
                raise NotImplementedError(
                    "Direct document ingestion is not supported by KnowledgeGateway."
                )

        monkeypatch.setattr(settings, "UPLOAD_DIR", str(root), raising=False)
        monkeypatch.setattr(file_handler, "KnowledgeGateway", _StubGateway)

        out = await file_handler.create_file_query(
            state=_empty_state(),
            config={"configurable": {"file_path": str(note)}},
        )
        content = out["messages"][0].content

        assert calls, "探针失效：文本上传根本没有走到 add_document"
        assert ".txt" in content, f"文案没指出具体后缀：{content!r}"
        assert FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE.format(suffix=".txt") == content
        assert FILE_INGEST_ERROR_MESSAGE not in content, (
            "把永久性不支持报成了瞬时故障——用户会一直重试一个不可能成功的路径"
        )

    @pytest.mark.asyncio
    async def test_pdf_upload_points_to_document_api(self, tmp_path, monkeypatch):
        """``.pdf`` 走聊天上传是**用错入口**，必须给指引而不是"不支持"。

        此前 ``.pdf`` 落到 else 分支，用户会被告知「暂不支持该文件类型：.pdf」——
        而 PDF 恰是本平台的主业。这条钉住"不再自相矛盾"。
        """
        from agentix.application.agents import file_handler
        from agentix.application.agents.constants import (
            FILE_PDF_USE_DOCUMENT_API_MESSAGE,
            FILE_TYPE_UNSUPPORTED_MESSAGE,
        )
        from agentix.config import settings

        root = tmp_path / "uploads"
        root.mkdir()
        pdf = root / "law.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%stub\n")

        monkeypatch.setattr(settings, "UPLOAD_DIR", str(root), raising=False)

        out = await file_handler.create_file_query(
            state=_empty_state(),
            config={"configurable": {"file_path": str(pdf)}},
        )
        content = out["messages"][0].content

        assert content == FILE_PDF_USE_DOCUMENT_API_MESSAGE
        assert content != FILE_TYPE_UNSUPPORTED_MESSAGE.format(suffix=".pdf")


class TestUserFacingSupportListMatchesImplementation:
    """防"文案宣称支持 X，代码却没有 X 的实现"再次漂移。

    这是本仓真实发生过的缺陷：``FILE_TYPE_UNSUPPORTED_MESSAGE`` 曾宣称
    支持 ``.txt/.md/.json/.csv/.log/.xlsx/.xls``，其中 5 个后缀的实现在
    ``add_document`` 处必然抛异常。**文案是产品承诺**，必须与分支一一对应。
    """

    def test_unsupported_message_does_not_advertise_text_suffixes(self):
        from agentix.application.agents.constants import FILE_TYPE_UNSUPPORTED_MESSAGE

        text_suffixes = [".txt", ".md", ".json", ".csv", ".log"]
        lied = [s for s in text_suffixes if s in FILE_TYPE_UNSUPPORTED_MESSAGE]
        assert not lied, (
            f"「不支持」文案仍在宣称支持这些文本后缀：{lied}——"
            "而它们会走到 add_document 桩并必然失败"
        )

    def test_source_has_branch_for_every_advertised_suffix(self):
        """AST 级对齐：文案里出现的后缀，源码中必须有对应 ``suffix in {...}`` 分支。"""
        import ast
        import re

        src = (REPO_ROOT / "agentix" / "application" / "agents" / "file_handler.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        # 收集源码里所有被比较的后缀字面量
        handled: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(".") and len(node.value) <= 6:
                    handled.add(node.value.lower())

        from agentix.application.agents.constants import FILE_TYPE_UNSUPPORTED_MESSAGE

        # ⚠️ 不能用 str.split()：文案里写的是 ``.xlsx/.xls``，按空白切分会得到一个
        # 整 token ``.xlsx/.xls``，在本用例里会解析不出任何后缀而变成**真空断言**
        # （首版就是这样：assert advertised 直接炸，暴露了探针缺陷）。
        advertised = {
            m.lower()
            for m in re.findall(r"\.[A-Za-z0-9]{1,5}(?![A-Za-z0-9])", FILE_TYPE_UNSUPPORTED_MESSAGE)
        }
        assert advertised, "文案里没解析出任何后缀——本用例已失去判别力，请检查文案格式"
        missing = sorted(advertised - handled)
        assert not missing, (
            f"文案宣称支持 {sorted(advertised)}，但 file_handler 里没有这些分支：{missing}"
        )

    def test_both_new_messages_are_importable_and_distinct(self):
        """两条新文案必须都存在且互不相同（合并即回到"说不清"的状态）。"""
        from agentix.application.agents import constants

        unavailable = constants.FILE_TEXT_INGEST_UNAVAILABLE_MESSAGE
        pdf_hint = constants.FILE_PDF_USE_DOCUMENT_API_MESSAGE
        assert unavailable != pdf_hint
        assert unavailable != constants.FILE_INGEST_ERROR_MESSAGE
        assert "{suffix}" in unavailable


# ---------------------------------------------------------------------------
# 3. AST 守卫：禁止新的"未净化 filename 直接拼路径"
# ---------------------------------------------------------------------------


#: 允许直接拼接 ``.filename`` 的文件（每条必须写原因）。
#: 留空本身是有意义的断言：当前全仓没有一处需要这种豁免。
#: ⚠️ 若将来真需要豁免，必须写清"为什么这里的 filename 不是客户端可控的"。
RAW_FILENAME_JOIN_ALLOWLIST: dict[str, str] = {}

SCAN_ROOTS = ["agentix", "kb_ingest", "scripts", "evals"]


def _py_files():
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "site-packages" in path.parts or "__pycache__" in path.parts:
                continue
            if "test" in path.name:
                continue
            yield path


def _raw_filename_joins(src: str, rel: str) -> list[str]:
    """返回 ``[说明, ...]``：把客户端文件名**未净化**就拼进路径的位置。

    抓两种形态：
    - ``<x> / <expr 含 .filename>``  （Path 除法拼接）
    - ``os.path.join(..., <expr 含 .filename>)``
    """
    hits: list[str] = []
    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError:
        return hits

    def mentions_filename(node: ast.AST) -> bool:
        text = ast.unparse(node)
        return ".filename" in text or ".file_name" in text

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if mentions_filename(node.right):
                hits.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")
        elif isinstance(node, ast.Call):
            dotted = ast.unparse(node.func)
            if dotted in {"os.path.join", "posixpath.join", "ntpath.join"}:
                if any(mentions_filename(a) for a in node.args):
                    hits.append(f"{rel}:{node.lineno}  {ast.unparse(node)}")
    return hits


class TestNoRawFilenamePathJoin:
    def test_scanner_is_not_vacuous(self):
        """防空真：扫描器必须真的读到了文件且能解析。

        否则 rglob 路径写错、或 AST 全解析失败时，下面的断言会**空转通过**。
        """
        files = list(_py_files())
        assert len(files) > 100, f"只扫到 {len(files)} 个文件，扫描根疑似配置错误"

    def test_no_raw_filename_join_outside_allowlist(self):
        offenders: list[str] = []
        for path in _py_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in RAW_FILENAME_JOIN_ALLOWLIST:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            offenders.extend(_raw_filename_joins(src, rel))

        assert not offenders, (
            "发现把客户端文件名**未净化**就拼进路径的位置——"
            "`Path(base) / file.filename` 可被 '../../x' 或 'C:/Windows/x' 逃逸，"
            "请改用 _safe_resolve / _safe_temp_path（或 os.path.basename + 包含性断言）：\n  "
            + "\n  ".join(offenders)
        )

    def test_allowlist_entries_are_documented(self):
        """豁免必须逐条写理由，防止它退化成默认放行。"""
        undocumented = [k for k, v in RAW_FILENAME_JOIN_ALLOWLIST.items() if not v.strip()]
        assert not undocumented, f"以下豁免没写原因：{undocumented}"

    def test_allowlist_entries_still_exist(self):
        """豁免项必须仍能在仓库里找到，防止"豁免留着但文件没了"。"""
        for rel in RAW_FILENAME_JOIN_ALLOWLIST:
            assert (REPO_ROOT / rel).exists(), f"豁免表里的文件已不存在：{rel}"


# ---------------------------------------------------------------------------
# 4. 有意暴露的标注（防"悄悄丢掉安全说明"）
# ---------------------------------------------------------------------------

#: 这些位置**保留**已知暴露面，但必须带着说明——文档化了才叫决定，没写就是疏漏。
INTENTIONAL_EXPOSURES = [
    (
        "kb_ingest/kb_service/api/routes.py",
        "支持主机任意路径",
        "该模块的 ingest_excel 接受任意主机本地路径——这是运维刻意的用法，"
        "但必须在同一文件里写清'服务无鉴权 + 端口发布到宿主'的风险前提",
    ),
]


class TestIntentionalExposuresAreDocumented:
    @pytest.mark.parametrize("relative_path,needle,why", INTENTIONAL_EXPOSURES)
    def test_exposure_note_present(self, relative_path, needle, why):
        path = REPO_ROOT / relative_path
        assert path.exists(), f"文件不存在：{relative_path}"
        assert needle in path.read_text(encoding="utf-8"), f"{relative_path} 缺少安全标注：{why}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
