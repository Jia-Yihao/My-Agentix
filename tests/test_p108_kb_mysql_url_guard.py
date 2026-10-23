"""G12 / §24.3 T1 ⑫：kb_ingest 的 MySQL 连接串闸门。

覆盖四层，缺任一层都算没验完：

1. **纯函数**（`core/db_url_guard.py`）：scheme 白名单、主机必填、白名单条目
   （精确 / 子域 / 含 apex 与否 / 端口绑定）、非法端口、日志形态必须不含密码。
2. **开关语义**（`enforce_connection_url`）：flag-off 与加固前**逐位等价**
   （连 `sqlite:` 都原样放行），flag-on 才读白名单；开关开、白名单空的
   「过渡态」也显式钉住，防止读者以为已经收紧。
3. **真实入口**：闸门放在**服务层 + 路由**而不是 pydantic 层——放 pydantic 层
   时 FastAPI 会把被拒的连接串（含密码）塞进 422 响应体的 `input`，实测泄漏。
   这里用真实 TestClient 断言 400 且响应体不含凭据，并留下一枚**反向钉子**：
   schema 上不得再出现 `connection_url` 校验器。
4. **同步面**：两个新键在 `kb_ingest/.env.example`、根 `.env.example`、
   `docker-compose.yml` 三处都在（p35 只守 config.py ↔ kb_ingest/.env.example
   这一对，compose 与根 example 需要另外的钉子）。

刻意**不**测「解析后 IP 属于内网段」这类判定——闸门设计上不做 DNS 解析
（理由见 db_url_guard 模块 docstring），测试也不该暗示它做了。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kb_service.core.db_url_guard import (
    AllowedHost,
    ConnectionUrlError,
    check_connection_url,
    check_query_options,
    describe_for_log,
    enforce_connection_url,
    parse_allowed_hosts,
)
from kb_service.schemas.ingest import MySQLIngestRequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mk(connection_url: str) -> MySQLIngestRequest:
    return MySQLIngestRequest(connection_url=connection_url, table="t")


# ---------------------------------------------------------------------------
# 1. 纯函数：scheme
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@db.internal:3306/kb",
        "mysql+pymysql://u:p@db.internal:3306/kb",
        "mysql+asyncmy://u:p@db.internal:3306/kb",
        "mysql+aiomysql://u:p@db.internal/kb",
    ],
)
def test_mysql_schemes_pass(url: str) -> None:
    assert check_connection_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:////app/secrets.db",
        "sqlite:///relative.db",
        "postgresql://u:p@db.internal:5432/kb",
        "postgres://db.internal/kb",
        "oracle://db.internal:1521/orcl",
        "mssql://u:p@db.internal/kb",
        "clickhouse://db.internal:8123/kb",
        "file:///etc/passwd",
        "http://attacker.example/hook",
        "//db.internal/kb",
        "db.internal:3306/kb",
    ],
)
def test_non_mysql_schemes_rejected(url: str) -> None:
    with pytest.raises(ConnectionUrlError):
        check_connection_url(url)


@pytest.mark.parametrize("url", ["", "   ", "\t"])
def test_blank_url_rejected(url: str) -> None:
    with pytest.raises(ConnectionUrlError):
        check_connection_url(url)


def test_scheme_error_names_the_offending_scheme() -> None:
    """报错要能被操作员读懂——「scheme 不合法」不足以让人意识到自己在读本地文件。"""
    with pytest.raises(ConnectionUrlError, match="sqlite"):
        check_connection_url("sqlite:////app/x.db")


# ---------------------------------------------------------------------------
# 2. 纯函数：主机与白名单
# ---------------------------------------------------------------------------


def test_host_is_required() -> None:
    with pytest.raises(ConnectionUrlError, match="must name a database host"):
        check_connection_url("mysql://")


def test_host_only_url_is_allowed_when_host_required_false() -> None:
    """socket / 只走 path 的连接串（`mysql:///db?unix_socket=…`）走这个逃生口的责任在调用方，
    schema 层不用它——这里存在只是为了让函数可被更窄的场景复用。"""
    assert check_connection_url("mysql://", require_host=False) == "mysql://"


def test_trailing_dot_host_is_normalised() -> None:
    """FQDN 尾点是合法写法（`db.internal.`），不该因为字符串不等而误拒。"""
    assert check_connection_url("mysql://db.internal./kb", (AllowedHost.parse("db.internal"),))


def test_allowlist_exact_host() -> None:
    rules = parse_allowed_hosts("db.internal")
    assert check_connection_url("mysql://u@db.internal:3306/kb", rules)
    with pytest.raises(ConnectionUrlError, match="not in KB_MYSQL_ALLOWED_HOSTS"):
        check_connection_url("mysql://u@evil.example:3306/kb", rules)


def test_allowlist_is_case_insensitive_both_sides() -> None:
    rules = parse_allowed_hosts("DB.Internal")
    assert check_connection_url("mysql://u@db.INTERNAL/kb", rules)


def test_subdomain_entry_covers_children_but_not_apex() -> None:
    """`.corp.example.com` 不含 apex——加了通配顺手把 apex 也放行是静默扩大范围。"""
    rules = parse_allowed_hosts("*.corp.example.com")
    assert check_connection_url("mysql://u@db.corp.example.com/kb", rules)
    with pytest.raises(ConnectionUrlError):
        check_connection_url("mysql://u@corp.example.com/kb", rules)
    # 相似后缀必须不误命中（endswith 的经典坑）：
    with pytest.raises(ConnectionUrlError):
        check_connection_url("mysql://u@evil-corp.example.com/kb", rules)


def test_apex_needs_its_own_entry() -> None:
    rules = parse_allowed_hosts("*.corp.example.com, corp.example.com")
    assert check_connection_url("mysql://u@corp.example.com/kb", rules)


def test_port_bound_entry() -> None:
    rules = parse_allowed_hosts("db.internal:3307")
    assert check_connection_url("mysql://u@db.internal:3307/kb", rules)
    with pytest.raises(ConnectionUrlError):
        check_connection_url("mysql://u@db.internal:3306/kb", rules)


def test_empty_allowlist_means_scheme_only() -> None:
    """过渡态：闸门开、白名单空 ⇒ 主机不限制。文档与注释都这么讲，测试钉住口径。"""
    assert parse_allowed_hosts("") == ()
    assert parse_allowed_hosts("  ,, ;") == ()
    assert check_connection_url("mysql://u@attacker.example/kb", ())


@pytest.mark.parametrize("raw", ["a, b", "a,b\n", "a;b", " a , b "])
def test_allowed_hosts_separators(raw: str) -> None:
    assert {rule.host for rule in parse_allowed_hosts(raw)} == {"a", "b"}


def test_invalid_port_raises_guard_error_not_value_error() -> None:
    """`urlsplit(...).port` 对非法端口抛 ValueError；闸门必须把它转成自己的类型，
    否则「被校验的输入」会以内部错误的形态冒出去。"""
    with pytest.raises(ConnectionUrlError, match="not parseable"):
        check_connection_url("mysql://u@db.internal:notaport/kb")


def test_unparseable_url_raises_guard_error() -> None:
    with pytest.raises(ConnectionUrlError, match="not parseable"):
        check_connection_url("mysql://[::1/kb")


# ---------------------------------------------------------------------------
# 2b. query 选项闸：主机白名单管不到「客户端愿意干什么」
# ---------------------------------------------------------------------------

#: 实测依据：`create_engine("mysql+pymysql://u:p@h/db?local_infile=1")` 的
#: `dialect.create_connect_args()` 产出 `{'local_infile': 1, 'client_flag': 2, ...}`
#: ——对端服务器据此可要求本客户端上传本地文件。
@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@db.internal/kb?local_infile=1",
        "mysql+pymysql://u:p@db.internal/kb?local_infile=true",
        "mysql://u:p@db.internal/kb?local_infile",  # 无值也算开启意图
        "mysql://u:p@db.internal/kb?LOCAL_INFILE=1",  # 键名大小写不敏感
        "mysql://u:p@db.internal/kb?charset=utf8mb4&allow_local_infile=1",
        "mysql://u:p@db.internal/kb?read_default_file=%2Fetc%2Fpasswd",
        "mysql://u:p@db.internal/kb?read_default_group=client",
        "mysql://u:p@db.internal/kb?client_flag=2",  # CLIENT_LOCAL_FILES
        "mysql://u:p@db.internal/kb?client_flag=0x3",
    ],
)
def test_dangerous_query_options_are_rejected(url: str) -> None:
    with pytest.raises(ConnectionUrlError, match="not allowed|CLIENT_LOCAL_FILES|capability"):
        check_connection_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@db.internal/kb?charset=utf8mb4&connect_timeout=5",
        "mysql://u:p@db.internal/kb?local_infile=0",  # 显式钉死为关，是要的行为
        "mysql://u:p@db.internal/kb?local_infile=false",
        "mysql://u:p@db.internal/kb?client_flag=2048",  # 不含 LOCAL_FILES 位
        "mysql://u:p@db.internal/kb?client_flag=0",
    ],
)
def test_benign_query_options_pass(url: str) -> None:
    assert check_connection_url(url) == url


def test_query_gate_is_orthogonal_to_allowlist() -> None:
    """白名单命中不等于放过选项：两条闸各自独立成立。"""
    rules = parse_allowed_hosts("db.internal")
    assert check_connection_url(GOOD_URL, rules) == GOOD_URL
    with pytest.raises(ConnectionUrlError, match="local_infile"):
        check_connection_url("mysql://u:p@db.internal:3306/kb?local_infile=1", rules)


def test_rejection_does_not_echo_option_value() -> None:
    """值里可能是路径（`read_default_file=/app/secrets.db`），detail 只说选项名。"""
    with pytest.raises(ConnectionUrlError) as excinfo:
        check_query_options("read_default_file=%2Fapp%2Fsecrets.db")
    assert "/app/secrets.db" not in str(excinfo.value)
    assert "read_default_file" in str(excinfo.value)


def test_non_numeric_client_flag_is_guard_error() -> None:
    """`client_flag` 的值是被校验的输入，不是我们的故障——不能冒 ValueError。"""
    with pytest.raises(ConnectionUrlError, match="not a numeric capability mask"):
        check_query_options("client_flag=abc")


def test_guard_off_does_not_look_at_options() -> None:
    """flag-off 逐位等价的另一面：`local_infile=1` 照样原样放行。"""
    url = "mysql://u:p@evil.example/kb?local_infile=1"
    assert enforce_connection_url(url, _Cfg(False)) == url


# ---------------------------------------------------------------------------
# 3. 日志形态不得带出密码
# ---------------------------------------------------------------------------


def test_describe_for_log_masks_password() -> None:
    line = describe_for_log("mysql+pymysql://ingest:S3cr3t!@db.internal:3306/kb")
    assert "S3cr3t" not in line
    assert "db.internal" in line
    assert "ingest" in line, "用户名保留：它不是机密，且排查时是定位线索"


def test_error_message_does_not_echo_url() -> None:
    rules = parse_allowed_hosts("db.internal")
    with pytest.raises(ConnectionUrlError) as excinfo:
        check_connection_url("mysql://u:Sup3rS3cret@evil.example/kb", rules)
    assert "Sup3rS3cret" not in str(excinfo.value)


def test_describe_for_log_survives_garbage() -> None:
    """日志格式化函数绝不能自己抛——它活在被记录的路径上。"""
    assert describe_for_log("mysql://[::1/kb") == "<unparseable url>"
    assert isinstance(describe_for_log(""), str)


# ---------------------------------------------------------------------------
# 4. 开关语义 + 真实入口：闸门放在服务层而不是 pydantic 层
# ---------------------------------------------------------------------------

BAD_URL = "mysql://ing3st:Tr3m0d1a@evil.example:3306/kb"
GOOD_URL = "mysql+pymysql://u:p@db.internal:3306/kb?charset=utf8mb4"


class _Cfg:
    """只带闸门两个字段的哑配置——避免为了测闸门去构造整个 Config。"""

    def __init__(self, guard: bool, hosts: str = "") -> None:
        self.mysql_url_guard = guard
        self.mysql_allowed_hosts = hosts


def test_guard_off_returns_input_unchanged() -> None:
    """flag-off 与加固前逐位等价：连 `sqlite:` 都原样放行，且**不改写**字符串。"""
    url = "sqlite:////app/secrets.db"
    assert enforce_connection_url(url, _Cfg(False)) == url
    assert enforce_connection_url(BAD_URL, _Cfg(False)) == BAD_URL


def test_guard_off_never_touches_allowlist() -> None:
    """关闭时白名单再窄也不生效——防止"我明明关了开关还被拦"。"""
    assert enforce_connection_url(BAD_URL, _Cfg(False, "db.internal")) == BAD_URL


@pytest.mark.parametrize("guard", [True])
def test_guard_on_rejects_non_mysql(guard: bool) -> None:
    with pytest.raises(ConnectionUrlError, match="unsupported scheme"):
        enforce_connection_url("sqlite:////app/x.db", _Cfg(guard))


def test_guard_on_without_allowlist_is_scheme_only() -> None:
    """过渡态必须在测试里显式成立：闸门开、白名单空 ⇒ 外部主机照样放行。
    文档说这是"只校验 scheme"，测试就得钉住这个口径，别让读者以为已经收紧。"""
    assert enforce_connection_url(BAD_URL, _Cfg(True, "")) == BAD_URL


def test_guard_on_with_allowlist_rejects_and_keeps_password_out() -> None:
    with pytest.raises(ConnectionUrlError) as excinfo:
        enforce_connection_url(BAD_URL, _Cfg(True, "db.internal"))
    message = str(excinfo.value)
    assert "evil.example" in message, "要能说清是哪个主机被拒"
    assert "Tr3m0d1a" not in message, "拒绝原因里绝不能带回凭据"


def test_guard_on_accepts_allowlisted_host() -> None:
    assert enforce_connection_url(GOOD_URL, _Cfg(True, "db.internal")) == GOOD_URL


def test_service_choke_point_runs_the_guard_before_connecting() -> None:
    """`MySQLIngestor.ingest` 是 HTTP 与 CLI 的唯一汇合点——闸门必须在 `create_engine` 之前。"""
    from kb_service.services import mysql_ingest as mysql_ingest_module
    from kb_service.services.mysql_ingest import MySQLIngestor

    calls: list[str] = []
    monkey_fail = AssertionError("create_engine 被调用了：闸门没有挡在连接之前")

    def _fake_create_engine(url: str):
        calls.append(url)
        raise monkey_fail

    original = mysql_ingest_module.create_engine
    mysql_ingest_module.create_engine = _fake_create_engine
    try:
        ingestor = MySQLIngestor.__new__(MySQLIngestor)  # 绕开 LLM/Milvus 客户端构造
        ingestor.config = _Cfg(True, "db.internal")
        with pytest.raises(ConnectionUrlError):
            ingestor.ingest(MySQLIngestRequest(connection_url=BAD_URL, table="t"))
        assert calls == [], "被拒的连接串不应走到 create_engine"
    finally:
        mysql_ingest_module.create_engine = original


def test_route_rejects_with_400_and_no_credential_echo(monkeypatch) -> None:
    """端到端：422 会被 pydantic 塞进 `input`（连密码一起回显），所以闸门必须走
    服务层 + 400。这里真起一个只挂 kb_ingest 路由的 app 验证响应体。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kb_service.api import routes as routes_module
    from kb_service.api.deps import get_config

    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_config] = lambda: _Cfg(True, "db.internal")

    client = TestClient(app)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": BAD_URL, "table": "t"},
    )
    assert response.status_code == 400, response.text
    assert "Tr3m0d1a" not in response.text, "400 的 detail 不得回显凭据"
    assert "KB_MYSQL_URL_GUARD" in response.text


def test_route_rejects_option_gate_even_for_allowlisted_host(monkeypatch) -> None:
    """端到端验证两条闸正交：主机在白名单内，`?local_infile=1` 照样在 HTTP 面被拒。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kb_service.api import routes as routes_module
    from kb_service.api.deps import get_config

    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_config] = lambda: _Cfg(True, "db.internal")
    client = TestClient(app)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": "mysql://u:p@db.internal:3306/kb?local_infile=1", "table": "t"},
    )
    assert response.status_code == 400, response.text
    assert "local_infile" in response.text, "要能说清是哪个选项被拒"


def test_route_rejects_before_queuing(monkeypatch) -> None:
    """被拒时不能先回 202 再在后台炸——调用方看不见失败。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kb_service.api import routes as routes_module
    from kb_service.api.deps import get_config

    started: list[str] = []

    class _Boom:
        def __init__(self, *_a, **_k) -> None:
            pass

        def ingest(self, request) -> None:
            started.append(request.connection_url)

    monkeypatch.setattr(routes_module, "MySQLIngestor", _Boom)
    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_config] = lambda: _Cfg(True, "db.internal")
    client = TestClient(app)
    response = client.post("/ingest/mysql", json={"connection_url": BAD_URL, "table": "t"})
    assert response.status_code == 400
    assert started == [], "闸门拒绝后不应把任务排进后台"


def test_schema_does_not_validate_connection_url_anymore() -> None:
    """反向钉子：`connection_url` 上不允许再出现 field_validator。

    这条不是风格约束——pydantic 的 ValidationError 会把被拒输入放进
    `errors()["input"]`，而 FastAPI 的 422 响应体带 `input`，实测会把连接串里的
    **密码明文回显**。谁将来"顺手加个 validator"，就会把凭据泄露面重新打开。
    """
    from pydantic.fields import FieldInfo

    field = MySQLIngestRequest.model_fields["connection_url"]
    assert isinstance(field, FieldInfo)
    assert field.title is None
    # 关闭态下 sqlite URL 必须能顺利构造出模型（校验不在这一层）。
    assert MySQLIngestRequest(connection_url="sqlite:////app/x.db", table="t").connection_url


# ---------------------------------------------------------------------------
# 4b. 既有 validator 的回显面：table / where 不含凭据，允许保留
# ---------------------------------------------------------------------------


def test_existing_validators_still_apply() -> None:
    """table / where 的既有闸门不能被新逻辑顶掉。"""
    with pytest.raises(ValidationError, match="table must be a plain SQL identifier"):
        MySQLIngestRequest(connection_url=GOOD_URL, table="t; DROP TABLE x")
    with pytest.raises(ValidationError, match="where must not contain"):
        MySQLIngestRequest(
            connection_url=GOOD_URL, table="t", where="1=1; DELETE FROM users"
        )


# ---------------------------------------------------------------------------
# 5. 同步面（五处同步在 kb_ingest 侧的形状）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "kb_ingest/kb_service/core/config.py",
        "kb_ingest/.env.example",
        ".env.example",
        "docker-compose.yml",
    ],
)
def test_guard_keys_declared_everywhere(rel: str) -> None:
    text = (PROJECT_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
    for key in ("KB_MYSQL_URL_GUARD", "KB_MYSQL_ALLOWED_HOSTS"):
        assert key in text, f"{key} 未同步到 {rel}"


def test_guard_defaults_are_off_in_compose() -> None:
    """compose 的缺省值必须是"关"——否则改了所有既有部署的行为。"""
    text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${KB_MYSQL_URL_GUARD:-false}" in text
    assert "${KB_MYSQL_ALLOWED_HOSTS:-}" in text
