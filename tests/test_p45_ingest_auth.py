"""P45：kb_ingest 鉴权（服务端 + 客户端两侧）的一致性守卫。

**背景（为什么这里需要一整组测试）**

`kb_ingest` 此前 5 个端点**全部零鉴权**：`api/deps.py` 只注入配置，没有
API Key、没有中间件。而 compose 把它以 ``ports: 8100:8000`` **发布到宿主机**、
启动命令又是 ``uvicorn --host 0.0.0.0``。端点是写入语义（Excel/MySQL 入库，
直接改 Milvus 集合），`vector_search` / `hybrid_search` 还能把知识库读出去。
四者叠加 = 同一网段内任何人可无凭证改写知识库并读取全部法条语料。

本轮补上鉴权，沿用 agentix 已有的**协议**约定（同头名 ``X-API-Key``、同 401 形状、
同宽松真值解析），但**刻意不复用它的环境变量名**：这里读的是
``KB_REQUIRE_API_KEY`` / ``KB_API_KEY``。

**为什么环境变量名必须与服务端作用域一致、而不能与主应用共用**

首版实现复用了主应用的 ``API_KEY``，这是一个安全设计错误（权限抬升）：
compose 里 `kb_ingest` 容器用 `env_file: .env` 加载仓库根 `.env`，于是主应用的
`API_KEY` 会被**原样注入这个容器**；而 ``API_KEY`` 是对外发布的凭证（任何拿到
它的人都能以智能体身份调用网关）。复用的后果是：**一支对外凭证同时成了对内写权限**
（`vector_search` 读全库 + Excel/MySQL 入库写 Milvus）。改成服务作用域的
``KB_*`` 后，两支密钥可以独立轮换、独立吊销，信任边界不再重合。
`kb_ingest/.env.example` 开头本来就写着"主应用的配置见仓库根 `.env.example`，
两者**不共享**"，本测试把这句话变成可执行的断言。

**为什么"客户端侧"也必须有测试**

只修服务端会**制造一个新故障**：一旦有人把 ``KB_REQUIRE_API_KEY`` 打开，
agentix 侧 4 个调用点（file_handler / task_handlers / external_search_node /
kb_retrieval）全都不带 Key ⇒ 集体 401。且它们的报错形态很误导——
`task_handlers` 会把它包成 ``Excel import failed (401)``，看起来像服务挂了，
实际是配置没同步。所以两侧一起锁。
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 客户端：密钥 → 请求头（纯函数，零依赖）
# ---------------------------------------------------------------------------


class TestIngestAuthHeaders:
    def test_normal_key_becomes_x_api_key_header(self):
        from agentix.domain.policies.ingest_auth import API_KEY_HEADER, ingest_auth_headers

        assert ingest_auth_headers("s3cret") == {API_KEY_HEADER: "s3cret"}
        assert API_KEY_HEADER == "X-API-Key", (
            "头名必须与 kb_ingest.api.deps 及 agentix.infrastructure.core.security 一致"
        )

    @pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
    def test_absent_or_blank_key_sends_no_header(self, bad):
        """发一个空的 ``X-API-Key:`` 等于制造一个必然 401 的请求，不如不带。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers

        assert ingest_auth_headers(bad) == {}

    def test_key_value_is_not_mutated(self):
        """★ 值必须原样发出。

        首版实现写的是 ``(api_key or "").strip()`` 再发送：若 ``.env`` 里是
        ``KB_API_KEY=abc ``（尾随空格这种最常见的复制粘贴残留），客户端发 ``"abc"``、
        服务端比对原值 ``"abc "`` ⇒ 401，而两边"看起来配置一致"。
        收敛成单一真源的目的正是消除这类静默不一致，所以不做规整。
        """
        from agentix.domain.policies.ingest_auth import API_KEY_HEADER, ingest_auth_headers

        assert ingest_auth_headers("abc ") == {API_KEY_HEADER: "abc "}
        assert ingest_auth_headers(" abc") == {API_KEY_HEADER: " abc"}

    def test_module_is_stdlib_only(self):
        """本模块落在 domain/policies，必须零第三方依赖。"""
        src = (REPO_ROOT / "agentix" / "domain" / "policies" / "ingest_auth.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {"__future__", "urllib"}, f"领域策略引入了额外依赖：{sorted(roots)}"


class TestIngestAuthHeadersFor:
    """只在目标确实是本仓 kb_ingest 时才附带密钥。"""

    INGEST = "http://kb_ingest:8000"

    def test_same_origin_different_path_gets_header(self):
        """同源不同路径（/api/search vs /api/ingest/excel）应同样附带。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        for target in (f"{self.INGEST}/api/search", f"{self.INGEST}/api/ingest/excel"):
            assert ingest_auth_headers_for(
                target, api_key="k", ingest_service_url=self.INGEST
            ) == {"X-API-Key": "k"}, target

    def test_different_host_gets_no_header(self):
        """★ 第三方检索服务**绝不能**收到本仓密钥——那是拿一个安全问题换另一个。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        assert ingest_auth_headers_for(
            "https://search.thirdparty.example/api/search",
            api_key="k",
            ingest_service_url=self.INGEST,
        ) == {}

    def test_prefix_lookalike_host_gets_no_header(self):
        """★ 字符串前缀比较会放过这个地址；origin 比较不会。

        ``"http://kb_ingest:8000.evil.example".startswith("http://kb_ingest:8000")``
        为真 —— 与 ``upload_paths`` 里修掉的兄弟目录问题是同一类错误。
        """
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        evil = "http://kb_ingest:8000.evil.example/api/search"
        assert evil.startswith(self.INGEST), "探针失效：这个地址本来就是前缀式攻击样本"
        assert ingest_auth_headers_for(
            evil, api_key="k", ingest_service_url=self.INGEST
        ) == {}

    def test_scheme_difference_gets_no_header(self):
        """https→http 或反之属不同 origin（密钥不该跨协议降级发送）。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        assert ingest_auth_headers_for(
            "https://kb_ingest:8000/api/search",
            api_key="k",
            ingest_service_url="http://kb_ingest:8000",
        ) == {}

    @pytest.mark.parametrize("bad", [None, "", "/api/search", "not a url", "kb_ingest:8000"])
    def test_unparseable_target_sends_nothing(self, bad):
        """解析不出 origin ⇒ 一律不带（宁可 401 可见，也不要静默外发密钥）。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        assert ingest_auth_headers_for(
            bad, api_key="k", ingest_service_url=self.INGEST
        ) == {}

    def test_missing_ingest_service_url_sends_nothing(self):
        """未配置 INGEST_SERVICE_URL 时没有"自家服务"可比，因此不带。"""
        from agentix.domain.policies.ingest_auth import ingest_auth_headers_for

        assert ingest_auth_headers_for(
            self.INGEST, api_key="k", ingest_service_url=None
        ) == {}


# ---------------------------------------------------------------------------
# 2. 服务端：require_api_key 依赖
# ---------------------------------------------------------------------------


@pytest.fixture
def kb_config(monkeypatch):
    """把 kb_ingest 的配置缓存清空，供每个用例独立设置环境变量。"""
    from kb_service.api import deps

    def _set(*, require: bool, key: str):
        monkeypatch.setenv("KB_REQUIRE_API_KEY", "true" if require else "false")
        monkeypatch.setenv("KB_API_KEY", key)
        deps.get_config.cache_clear()
        return deps.get_config()

    yield _set
    deps.get_config.cache_clear()


class TestRequireApiKeyDependency:
    """直接驱动依赖函数（最精确，不经过 HTTP 栈）。"""

    @pytest.mark.asyncio
    async def test_disabled_mode_allows_without_header(self, kb_config):
        from kb_service.api.deps import require_api_key

        kb_config(require=False, key="")
        assert await require_api_key(None) == ""

    @pytest.mark.asyncio
    async def test_disabled_mode_allows_even_with_configured_key(self, kb_config):
        """开关关闭就是关闭——配了密钥也不拦（保持既有部署行为）。"""
        from kb_service.api.deps import require_api_key

        kb_config(require=False, key="s3cret")
        assert await require_api_key(None) == ""

    @pytest.mark.asyncio
    async def test_enabled_and_correct_key_passes(self, kb_config):
        from kb_service.api.deps import require_api_key

        kb_config(require=True, key="s3cret")
        assert await require_api_key("s3cret") == "s3cret"

    @pytest.mark.asyncio
    async def test_enabled_missing_header_rejected(self, kb_config):
        from fastapi import HTTPException

        from kb_service.api.deps import require_api_key

        kb_config(require=True, key="s3cret")
        with pytest.raises(HTTPException) as ei:
            await require_api_key(None)
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_enabled_wrong_key_rejected(self, kb_config):
        from fastapi import HTTPException

        from kb_service.api.deps import require_api_key

        kb_config(require=True, key="s3cret")
        with pytest.raises(HTTPException) as ei:
            await require_api_key("s3cret-wrong")
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_enabled_empty_configured_key_is_fail_closed(self, kb_config):
        """★ 开启鉴权但没配密钥 ⇒ 必须全部 401。

        若实现写成 ``if not x_api_key or not compare_digest(x_api_key, expected)``
        而**不**判 ``expected`` 为空，那么 ``compare_digest("", "")`` 为真、
        发一个空头就能进——这是最容易被忽略的一个洞。
        """
        from fastapi import HTTPException

        from kb_service.api.deps import require_api_key

        kb_config(require=True, key="")
        with pytest.raises(HTTPException) as ei:
            await require_api_key("")
        assert ei.value.status_code == 401


class TestRequireApiKeyOverHttp:
    """端到端：证明依赖真的**挂在路由上**，而不是只写了个函数没人调用。

    判别力来自状态码对照：认证通过 ⇒ 端点执行 ⇒ 返回 404（路径不存在）；
    认证未通过 ⇒ 401。用不存在的路径是为了**不触碰 Milvus**——
    端点在 ``excel_path.exists()`` 处即早退。
    """

    MISSING = "/definitely/not/here.xlsx"

    def _client(self):
        from fastapi.testclient import TestClient

        from kb_service.main import create_app

        return TestClient(create_app())

    def test_no_header_when_disabled_reaches_endpoint(self, kb_config, monkeypatch):
        kb_config(require=False, key="")
        client = self._client()
        r = client.post("/api/ingest/excel", json={"excel_path": self.MISSING})
        assert r.status_code == 404, f"认证应放行、由端点自己报文件不存在：{r.status_code}"

    def test_no_header_when_enabled_is_401(self, kb_config):
        kb_config(require=True, key="s3cret")
        client = self._client()
        r = client.post("/api/ingest/excel", json={"excel_path": self.MISSING})
        assert r.status_code == 401
        assert r.json().get("detail") == "Invalid or missing API key"

    def test_wrong_header_when_enabled_is_401(self, kb_config):
        kb_config(require=True, key="s3cret")
        client = self._client()
        r = client.post(
            "/api/ingest/excel",
            json={"excel_path": self.MISSING},
            headers={"X-API-Key": "nope"},
        )
        assert r.status_code == 401

    def test_correct_header_when_enabled_reaches_endpoint(self, kb_config):
        """★ 反向断言：没有这一条，上面三个 401 可能只是"路由永远 401"。"""
        kb_config(require=True, key="s3cret")
        client = self._client()
        r = client.post(
            "/api/ingest/excel",
            json={"excel_path": self.MISSING},
            headers={"X-API-Key": "s3cret"},
        )
        assert r.status_code == 404, f"带正确 Key 应放行：{r.status_code} {r.text}"

    def test_all_endpoints_are_behind_auth(self, kb_config):
        """逐个端点验证——不能只测一个就假定其余都挂了依赖。

        路径清单从 ``app.routes`` 实测得来（首版我按名字猜了
        ``/api/vector_search``，结果 404 ⇒ 路由没匹配 ⇒ 依赖没跑 ⇒
        用例报的是"未受保护"，其实是**探针指错了**。教训与 §7.11 一致：
        "红"要先怀疑验证方法）。
        """
        kb_config(require=True, key="s3cret")
        client = self._client()
        endpoints = [
            ("/api/ingest/excel", {"excel_path": self.MISSING}),
            ("/api/ingest/mysql", {}),
            ("/api/search", {"query": "q"}),
            ("/api/search/hybrid", {"query": "q"}),
        ]
        for path, body in endpoints:
            r = client.post(path, json=body)
            assert r.status_code == 401, f"{path} 未受鉴权保护：{r.status_code}"

    def test_upload_endpoint_checks_auth_before_touching_the_file(self, kb_config):
        """``/api/ingest/excel/upload`` 是**写文件**的端点，鉴权必须先于落盘。

        若依赖没挂在 router 上而是写在函数体尾部，就会"先写盘再校验"——
        未鉴权调用仍然能在磁盘上留下文件（配合路径穿越历史上就是任意写）。
        这里断言未带 Key 时拿不到 401 以外的任何东西。
        """
        kb_config(require=True, key="s3cret")
        client = self._client()
        r = client.post(
            "/api/ingest/excel/upload",
            files={"file": ("x.xlsx", b"PK\x03\x04stub", "application/vnd.ms-excel")},
        )
        assert r.status_code == 401, f"上传端点在鉴权前就做了别的事：{r.status_code}"

    def test_health_endpoint_stays_open(self, kb_config):
        """``/health`` 必须免鉴权——compose 的 healthcheck 依赖它。

        如果它也被 401 了，容器会被判为不健康并反复重启，属"修好了安全、
        弄坏了可用性"。
        """
        kb_config(require=True, key="s3cret")
        client = self._client()
        assert client.get("/health").status_code == 200

    def test_v1_knowledge_prefix_also_protected(self, kb_config):
        """同一 router 挂了两个 prefix（/api 与 /api/v1/knowledge），都要覆盖。"""
        kb_config(require=True, key="s3cret")
        client = self._client()
        r = client.post("/api/v1/knowledge/ingest/excel", json={"excel_path": self.MISSING})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3. 配置口径：必须与 agentix 读同一组环境变量
# ---------------------------------------------------------------------------


class TestConfigNamesAreServiceScoped:
    """kb_ingest 读 ``KB_*``，**不读**主应用的 ``API_KEY``。

    compose 的 `env_file: .env` 会把主应用整份配置注入这个容器，所以"变量名"
    就是信任边界：名字重合 ⇒ 主应用密钥自动成为本服务的写权限。
    """

    def test_reads_service_scoped_kb_names(self, monkeypatch):
        from kb_service.core.config import load_config

        monkeypatch.setenv("KB_REQUIRE_API_KEY", "true")
        monkeypatch.setenv("KB_API_KEY", "kb-scoped-secret")
        cfg = load_config()
        assert cfg.require_api_key is True
        assert cfg.api_key == "kb-scoped-secret"

    def test_does_not_read_the_main_app_api_key(self, monkeypatch):
        """★ 反向断言：只给主应用的 ``API_KEY`` / ``REQUIRE_API_KEY``，本服务必须无感。

        这条是权限抬升的回归守卫。若将来有人"顺手"把主应用变量名加回
        ``os.getenv`` 里，此测试立刻变红。
        """
        from kb_service.core.config import load_config

        monkeypatch.delenv("KB_REQUIRE_API_KEY", raising=False)
        monkeypatch.delenv("KB_API_KEY", raising=False)
        monkeypatch.setenv("REQUIRE_API_KEY", "true")
        monkeypatch.setenv("API_KEY", "main-app-public-key")
        cfg = load_config()
        assert cfg.require_api_key is False, (
            "kb_ingest 不得从主应用的 REQUIRE_API_KEY 取得开关——那会让 compose 的 "
            "env_file 注入把主应用配置变成本服务的鉴权配置"
        )
        assert cfg.api_key == "", (
            "kb_ingest 不得从主应用的 API_KEY 取密钥（权限抬升）"
        )

    @pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("no", False),
                                              ("true", True), ("1", True), ("yes", True),
                                              ("TRUE", True)])
    def test_truthy_parsing(self, monkeypatch, raw, expected):
        """与 agentix/security 侧语义一致的宽松真值解析。"""
        from kb_service.core.config import load_config

        monkeypatch.setenv("KB_REQUIRE_API_KEY", raw)
        assert load_config().require_api_key is expected

    def test_default_is_disabled_to_preserve_existing_deploys(self, monkeypatch):
        from kb_service.core.config import load_config

        monkeypatch.delenv("KB_REQUIRE_API_KEY", raising=False)
        monkeypatch.delenv("KB_API_KEY", raising=False)
        cfg = load_config()
        assert cfg.require_api_key is False
        assert cfg.api_key == ""

    def test_clone_config_preserves_auth_fields(self, monkeypatch):
        """``clone_config`` 是逐字段复制的——新增字段必须跟着走。

        否则运行期 clone 出来的 config 会把鉴权开关丢掉，出现"同一进程内
        两个 config 鉴权状态不同"的诡异现象。
        """
        from kb_service.core.config import clone_config, load_config

        monkeypatch.setenv("KB_REQUIRE_API_KEY", "true")
        monkeypatch.setenv("KB_API_KEY", "abc")
        cloned = clone_config(load_config())
        assert cloned.require_api_key is True
        assert cloned.api_key == "abc"

    def test_wire_name_is_identical_on_both_sides(self):
        """两侧必须读**同一个字面量** ``KB_API_KEY``。

        这是本次改名后新增的守卫：改名解决了权限抬升，却引入一个新风险——
        如果只有一侧改了名（例如 kb_ingest 读 ``KB_API_KEY``、agentix 发
        ``settings.KB_API_KEY`` 却仍从 ``API_KEY`` 取值），就会在开启鉴权时
        集体 401，而两边的配置"看起来都配好了"。
        """
        kb_src = (REPO_ROOT / "kb_ingest" / "kb_service" / "core" / "config.py").read_text(
            encoding="utf-8"
        )
        ax_src = (REPO_ROOT / "agentix" / "config" / "settings.py").read_text(encoding="utf-8")

        # 服务端：取 kb_api_key 字段时读的环境变量名
        server_keys = set(re.findall(r'os\.getenv\(\s*"(KB_[A-Z_]*API_KEY)"', kb_src))
        assert server_keys == {"KB_API_KEY", "KB_REQUIRE_API_KEY"}, (
            f"kb_ingest 读取的 KB_* 环境变量集合变了：{server_keys}"
        )
        # 客户端：settings.py 里必须声明同名字段
        assert re.search(r"^\s*KB_API_KEY\s*:\s*str", ax_src, re.M), (
            "agentix/config/settings.py 缺少 KB_API_KEY 字段——"
            "客户端将无从取得与 kb_ingest 约定的密钥"
        )
        # 开关属于服务端自述项，agentix 侧**不得**声明为配置字段。
        # 只看字段声明，不看注释（settings.py 的注释里会提到这个变量名做解释）。
        assert not re.search(r"^\s*KB_REQUIRE_API_KEY\s*:", ax_src, re.M), (
            "agentix 侧声明了 KB_REQUIRE_API_KEY 字段；客户端只需知道密钥本身，"
            "多读一个开关只会多一处可能不一致的配置面"
        )


# ---------------------------------------------------------------------------
# 4. 启动期自述：鉴权状态必须"说出来"
# ---------------------------------------------------------------------------


class TestStartupAuthSelfReport:
    """没有鉴权这件事必须有日志。

    此前完全没有：运维看不到、审计查不到，只有读代码才知道 5 个端点是裸的。
    agentix 早就有同样的 self-report，本服务补齐以对齐口径。
    """

    def test_warns_when_disabled(self, kb_config, caplog):
        from kb_service.main import _report_auth_mode

        kb_config(require=False, key="")
        with caplog.at_level(logging.WARNING):
            _report_auth_mode()
        text = caplog.text
        assert "DISABLED" in text
        # 告警必须点明"暴露面"，否则读者不知道需要去改什么
        assert "8100" in text or "0.0.0.0" in text

    def test_errors_when_enabled_without_key(self, kb_config, caplog):
        """开启鉴权但没配密钥 = 锁死而非安全，必须用 error 级别说清。"""
        from kb_service.main import _report_auth_mode

        kb_config(require=True, key="")
        with caplog.at_level(logging.ERROR):
            _report_auth_mode()
        assert "KB_API_KEY is empty" in caplog.text
        assert "401" in caplog.text

    def test_reports_enabled_on_happy_path(self, kb_config, caplog):
        from kb_service.main import _report_auth_mode

        kb_config(require=True, key="s3cret")
        with caplog.at_level(logging.INFO):
            _report_auth_mode()
        assert "auth enabled" in caplog.text


# ---------------------------------------------------------------------------
# 5. 结构性守卫：防止"新端点默认漏掉鉴权"与"客户端不带密钥"
# ---------------------------------------------------------------------------


class TestRouterLevelDependencyIsLoadBearing:
    """鉴权挂在 router 上、而不是逐个端点。

    逐个加的失效方式是**静默**的：将来新增一个端点，忘了加依赖，
    它就悄悄裸奔，没有任何测试或告警会提示。挂在 router 上则新端点自动继承。
    """

    def test_router_is_constructed_with_the_dependency(self):
        src = (REPO_ROOT / "kb_ingest" / "kb_service" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "router" not in targets:
                    continue
                call = node.value
                if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "APIRouter"):
                    continue
                for kw in call.keywords:
                    if kw.arg == "dependencies":
                        text = ast.unparse(kw.value)
                        if "require_api_key" in text:
                            found = True
        assert found, (
            "router 未以 dependencies=[Depends(require_api_key)] 构造——"
            "新增端点会静默漏掉鉴权"
        )

    def test_no_endpoint_declares_its_own_router(self):
        """若出现第二个 APIRouter 且没带依赖，上面的守卫就形同虚设。"""
        src = (REPO_ROOT / "kb_ingest" / "kb_service" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        constructions = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "APIRouter"
        ]
        assert len(constructions) == 1, (
            f"出现了 {len(constructions)} 个 APIRouter；多出来的那个可能没挂鉴权依赖"
        )


class TestAgentixCallSitesSendTheKey:
    """agentix → kb_ingest 的每个出站调用都必须能带上 ``X-API-Key``。

    否则一旦开启 ``KB_REQUIRE_API_KEY``，这条链路会集体 401，且报错形态
    会被包成 "Excel import failed (401)" / "外部 Excel 导入请求失败（401）"，
    看起来像服务故障而不是配置没同步。
    """

    #: 直接 POST 到 kb_ingest 的文件（每条都是经过确认的真实调用点）
    CALL_SITES = [
        "agentix/application/agents/file_handler.py",
        "agentix/application/services/task_handlers.py",
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/"
        "multi_agent/kb_nodes/external_search_node.py",
        "agentix/application/agents/kg_sub_graph/agentic_rag_agents/workflows/"
        "multi_agent/kb_retrieval.py",
    ]

    @pytest.mark.parametrize("rel", CALL_SITES)
    def test_post_call_passes_headers(self, rel):
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            furi = node.func
            if not (isinstance(furi, ast.Attribute) and furi.attr == "post"):
                continue
            # 只关心指向入库/检索服务的那些调用（目标参数里含 url 变量）
            target = ast.unparse(node.args[0]) if node.args else ""
            if "url" not in target.lower():
                continue
            checked += 1
            kwargs = {kw.arg for kw in node.keywords}
            assert "headers" in kwargs, (
                f"{rel}:{node.lineno} 的 POST 未传 headers —— "
                f"kb_ingest 开启鉴权后这里会 401（target={target}）"
            )
        assert checked, f"{rel} 里没找到任何 POST(url) 调用——用例已失去判别力"

    def test_callsites_use_the_shared_policy_helper(self):
        """调用点必须走共享策略函数，而不是各自手写请求头字典。

        手写的问题不是"写错"，而是"将来会漂移"：头名改了、判空规则改了，
        4 个调用点就会各写各的。收敛成单一真源是让漂移**不可能**的前提。
        """
        for rel in self.CALL_SITES:
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "ingest_auth_headers_for" in src, f"{rel} 未使用共享鉴权策略"


class TestKbIngestSecurityDocsAreCurrent:
    """代码里的审计注记不能停留在"本服务没有鉴权"——那已经过时了。

    过期文档比没有文档更危险：审计者会照着它做安全判断并得出错误结论。
    """

    def test_routes_do_not_claim_no_auth(self):
        src = (REPO_ROOT / "kb_ingest" / "kb_service" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        assert "本服务**没有任何鉴权**" not in src, "旧结论仍在，会误导审计者"
        assert "鉴权：**已补**" in src, "缺少「已补鉴权」的更新说明"

    def test_temp_path_docstring_records_the_new_state(self):
        src = (REPO_ROOT / "kb_ingest" / "kb_service" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        assert "鉴权已补上" in src, (
            "_safe_temp_path 的 docstring 仍说没有鉴权，与现状矛盾"
        )
