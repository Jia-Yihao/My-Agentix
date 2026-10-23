"""§24.3 T1 ⑫：`kb_ingest` 的 `where` 从「拼接片段」改「结构化 + bindparams」。

G12 收的是「连到哪儿」（`connection_url`），⑫ 收的是「连过去之后跑什么 SQL」。
`MySQLIngestRequest.where` 是一段原始 SQL 文本，原实现写的是
`base += f" WHERE {request.where}"`——原有的校验只去掉 `;` 与注释，
它拦得住堆叠语句，拦不住 `1=1 OR 1=1`、`id IN (SELECT … FROM mysql.user)`
这类**合法但越界**的片段，因为对拼出来的 SQL 而言，调用方给的就是语法。

覆盖七层，缺任一层都算没验完：

1. **默认姿态**：`KB_MYSQL_WHERE_PARAMETRIZED` 出厂 false，且它不是主应用 Settings 字段
   （所以必须进 selfcheck 的合法省略名单，否则 p35 的同步面会漏掉这个键）。
2. **flag-off 逐位等价**：产出的 SQL 字符串与加固前**逐字节相同**、`_bindparams` 为空；
   新字段 `filters` 在关着的 flag 下被**显式拒绝**而不是静默忽略。
3. **flag-on 的收紧面**：原始 `where` 一律拒；`filters` 编出来的 SQL 里**只有模板**，
   值全在 bindparams 里（用带引号/布尔恒真的哨兵值当探针）；列名走标识符白名单、
   运算符走封闭枚举；`eq NULL`、空 `IN ()`、`is_null` 带值这类「静默恒假」写法报错。
4. **两个入口 + CLI**：闸门放在服务层（HTTP/CLI 的唯一汇合点，且排在 `create_engine`
   之前）与路由入队前（回 400，detail 不回显值）。
5. **口径同源**：路由与服务必须共用 `core/sql_filters`，不许出现第二份实现。
6. **部署面**：两个 `.env.example`、`docker-compose.yml`、`clone_config` 的字段保全。
7. **真执行**：在内存 sqlite 上跑 `_build_query` 产出的语句，并且走生产那条**一模一样**的
   调用形状 `pd.read_sql_query(stmt, conn, chunksize=…)`——字符串与方言编译都证不了
   「语句跑得通、值真的被绑住」，布尔恒真探针在这里必须匹配 0 行。

刻意**不**验「这条 SQL 语义对不对」——本模块的职责是「值不进 SQL 文本」，
过滤条件本身选错了列是调用方的事。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kb_service.core.sql_filters import (
    MAX_FILTERS,
    SUPPORTED_OPS,
    FilterError,
    build_where,
    enforce_where_policy,
)
from kb_service.schemas.ingest import MySQLIngestRequest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KB_ENV_FILE = PROJECT_ROOT / "kb_ingest" / ".env.example"
ROOT_ENV_FILE = PROJECT_ROOT / ".env.example"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

#: 只在本测试里出现的哨兵值（与仓库既用的探针串同族），用来验「值不回显」。
SENTINEL = "MARKER_LEAK_TEST"


class _Cfg:
    """哑配置：闸门用到的三个字段（不构造整个 Config，避免拖进 Milvus/LLM 默认值）。"""

    def __init__(self, parametrized: bool = False, guard: bool = False, hosts: str = "") -> None:
        self.mysql_where_parametrized = parametrized
        self.mysql_url_guard = guard
        self.mysql_allowed_hosts = hosts


def _request(**kwargs) -> MySQLIngestRequest:
    base = {"connection_url": "mysql://u:p@db.internal:3306/kb", "table": "t"}
    base.update(kwargs)
    return MySQLIngestRequest(**base)


def _query(request: MySQLIngestRequest, parametrized: bool = False):
    """借 MySQLIngestor._build_query，但不构造它的 LLM/Milvus 客户端。"""
    from kb_service.services.mysql_ingest import MySQLIngestor

    ingestor = MySQLIngestor.__new__(MySQLIngestor)
    ingestor.config = _Cfg(parametrized)
    return ingestor._build_query(request)  # noqa: SLF001 - 被测的就是这个私有组装点


def _sql_of(statement) -> str:
    return str(statement)


# ---------------------------------------------------------------------------
# 1. 默认姿态
# ---------------------------------------------------------------------------


def test_config_default_is_off(monkeypatch) -> None:
    from kb_service.core.config import Config

    monkeypatch.delenv("KB_MYSQL_WHERE_PARAMETRIZED", raising=False)
    assert Config().mysql_where_parametrized is False


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True), ("false", False), ("", False)])
def test_config_parses_the_env_value(monkeypatch, raw: str, expected: bool) -> None:
    from kb_service.core.config import Config

    monkeypatch.setenv("KB_MYSQL_WHERE_PARAMETRIZED", raw)
    assert Config().mysql_where_parametrized is expected


def test_key_is_in_the_selfcheck_ignore_list() -> None:
    """它属于 kb_ingest 自己的配置，主应用不读 ⇒ 必须列进合法省略名单，
    否则 `make selfcheck` 会把它当成「示例里有、本地没有」的漂移。"""
    from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

    assert "KB_MYSQL_WHERE_PARAMETRIZED" in DEFAULT_IGNORED_MISSING


# ---------------------------------------------------------------------------
# 2. flag-off 与加固前逐位等价
# ---------------------------------------------------------------------------


def test_flagon_off_reproduces_the_legacy_sql_exactly() -> None:
    statement = _query(_request(where="year = 2024", limit=10))
    assert _sql_of(statement) == "SELECT * FROM `t` WHERE year = 2024 LIMIT 10"


def test_flagon_off_binds_nothing() -> None:
    """逐位等价的另一半：不偷偷往语句里塞绑定，否则 pandas 那侧的取数路径会变。"""
    statement = _query(_request(where="year = 2024"))
    assert statement._bindparams == {}  # noqa: SLF001


def test_flagon_off_without_filters_matches_the_bare_select() -> None:
    assert _sql_of(_query(_request())) == "SELECT * FROM `t`"


def test_filters_are_rejected_while_the_flag_is_off() -> None:
    """不是「忽略」而是「报错」：静默丢掉一个调用方以为生效的过滤条件，
    等于把整张表灌进向量库。"""
    with pytest.raises(FilterError, match="requires KB_MYSQL_WHERE_PARAMETRIZED=true"):
        _query(_request(filters=[{"column": "year", "op": "eq", "value": 2024}]))


def test_legacy_where_shape_checks_are_untouched() -> None:
    """原 schema 的 `;` / 注释拦截必须还在——flag 关着时它仍是唯一的那道形状闸。"""
    with pytest.raises(ValidationError):
        _request(where="1=1; DROP TABLE t")
    with pytest.raises(ValidationError):
        _request(where="1=1 -- tail")


# ---------------------------------------------------------------------------
# 3. flag-on 的收紧面
# ---------------------------------------------------------------------------


def test_raw_where_is_rejected_once_the_flag_is_on() -> None:
    """开flag 却仍然接受 `where`，等于「参数化」只是多了一个可选字段，注入面一条没关。"""
    with pytest.raises(FilterError, match="raw 'where' is not accepted"):
        _query(_request(where="year = 2024"), parametrized=True)


def test_single_filter_produces_template_plus_binding() -> None:
    statement = _query(_request(filters=[{"column": "year", "op": "eq", "value": 2024}]), parametrized=True)
    sql = _sql_of(statement)
    assert "`year` = :kbfilter_0_0" in sql
    assert "2024" not in sql, "值只能出现在 bindparams 里"
    assert statement._bindparams["kbfilter_0_0"].value == 2024  # noqa: SLF001


@pytest.mark.parametrize(
    "value",
    [
        "1' OR '1'='1",
        "x\" OR \"1\"=\"1",
        "1=1 UNION SELECT password FROM mysql.user --",
        f"{SENTINEL}' ; DROP TABLE t --",
    ],
)
def test_adversarial_values_stay_out_of_the_sql_text(value: str) -> None:
    """这些串在旧实现里会**原样进 SQL**；现在它们是绑定的参数值。"""
    statement = _query(_request(filters=[{"column": "note", "op": "eq", "value": value}]), parametrized=True)
    sql = _sql_of(statement)
    assert "note" in sql
    assert value not in sql
    assert "OR" not in sql and "UNION" not in sql and ";" not in sql and "--" not in sql
    assert statement._bindparams["kbfilter_0_0"].value == value  # noqa: SLF001


def test_values_survive_a_real_dialect_compile_as_placeholders() -> None:
    """`str(text())` 还没到驱动那一层；真编译一次，确认渲染出来的是占位符而不是字面量。

    MySQL 侧三个方言（pymysql / mysqldb / mysqlconnector）在本项目锁定的 SQLAlchemy 上
    都是 `format` 参数风格，渲染成**位置** `%s`；sqlite 是 `qmark` 风格渲染 `?`。
    四种渲染都不嵌值，但"位置占位符"证不了「按名字传值、不靠顺序拼串」——那由
    `compiled.params` 的键名证明（值挂在 `kbfilter_*_*` 上，而不是排在文本里）。
    """
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.dialects.mysql import mysqlconnector, mysqldb, pymysql as pymysql_dialect

    statement = _query(
        _request(filters=[{"column": "year", "op": "eq", "value": SENTINEL},
                          {"column": "id", "op": "in", "value": [1, 2]}]),
        parametrized=True,
    )
    dialects = [(pymysql_dialect.dialect(), "%s"), (mysqldb.dialect(), "%s"),
                (mysqlconnector.dialect(), "%s"), (sqlite.dialect(), "?")]
    for dialect, token in dialects:
        compiled = statement.compile(dialect=dialect)
        assert SENTINEL not in str(compiled), f"{dialect} 把值嵌进 SQL 文本了"
        assert "2024" not in str(compiled)
        assert str(compiled).count(token) == 3, str(compiled)
        assert set(compiled.params) == {"kbfilter_0_0", "kbfilter_1_0", "kbfilter_1_1"}
        assert compiled.params["kbfilter_0_0"] == SENTINEL


@pytest.mark.parametrize(
    "op,column",
    [
        ("eq", "a; DROP TABLE t"),
        ("eq", "a OR 1=1"),
        ("eq", "`a`"),
        ("eq", "a.b"),
        ("eq", "a b"),
        ("eq", ""),
        ("eq", "a" * 65),
        ("eq", "a-b"),
    ],
)
def test_column_names_that_are_not_plain_identifiers_are_rejected(op: str, column: str) -> None:
    with pytest.raises(FilterError, match="plain SQL identifier"):
        build_where([{"column": column, "op": op, "value": 1}])


def test_digits_first_column_names_are_accepted_on_purpose() -> None:
    """`1a` 这种"数字开头"是**故意放行**的：列名一律反引号quoted，MySQL 的 quoted
    标识符允许数字开头；这与 `table` 字段的白名单口径一致（见 schemas/ingest.py）。
    收紧到"字母开头"会拒掉真实存在的名，那是兼容性回归，不是安全增益。"""
    sql, _params = build_where([{"column": "1a", "op": "eq", "value": 1}])
    assert sql == "`1a` = :kbfilter_0_0"


@pytest.mark.parametrize("op", ["or", "AND", "", "drop", "eq ;", "regexp", "1"])
def test_operators_come_from_a_closed_enum(op: str) -> None:
    with pytest.raises(FilterError, match="filter op must be one of"):
        build_where([{"column": "a", "op": op, "value": 1}])


@pytest.mark.parametrize("op", sorted(SUPPORTED_OPS))
def test_every_advertised_operator_builds(op: str) -> None:
    """枚举里列出的每个运算符都得真能用——文档与实现不许两套口径。"""
    value = [1, 2] if op in ("in", "not_in") else None if op in ("is_null", "is_not_null") else 1
    sql, params = build_where([{"column": "a", "op": op, "value": value}])
    assert sql.startswith("`a` ")
    if op in ("is_null", "is_not_null"):
        assert params == {}
    else:
        assert params, f"{op} 应产生绑定"


def test_null_valued_predicates_are_rejected_not_silently_false() -> None:
    """`col = NULL` 恒不成立：让它报错，否则调用方读到「0 行」会以为过滤生效了。"""
    with pytest.raises(FilterError, match="requires a non-null value"):
        build_where([{"column": "a", "op": "eq", "value": None}])


def test_no_value_operators_reject_a_value() -> None:
    with pytest.raises(FilterError, match="takes no value"):
        build_where([{"column": "a", "op": "is_null", "value": 1}])


def test_in_requires_a_non_empty_list() -> None:
    with pytest.raises(FilterError, match="non-empty list value"):
        build_where([{"column": "a", "op": "in", "value": []}])
    with pytest.raises(FilterError, match="non-empty list value"):
        build_where([{"column": "a", "op": "in", "value": 3}])


def test_in_expands_to_one_placeholder_per_item() -> None:
    sql, params = build_where([{"column": "id", "op": "in", "value": [1, 2, 3]}])
    assert sql.count(":kbfilter_0_") == 3
    assert "IN (:kbfilter_0_0, :kbfilter_0_1, :kbfilter_0_2)" in sql
    assert list(params) == ["kbfilter_0_0", "kbfilter_0_1", "kbfilter_0_2"]


def test_tuple_values_are_accepted_for_in() -> None:
    sql, params = build_where([{"column": "id", "op": "not_in", "value": (1, 2)}])
    assert "NOT IN" in sql and len(params) == 2


def test_filters_compose_with_and_only() -> None:
    sql, params = build_where(
        [
            {"column": "year", "op": "eq", "value": 2024},
            {"column": "status", "op": "ne", "value": "draft"},
        ]
    )
    assert sql == "`year` = :kbfilter_0_0 AND `status` <> :kbfilter_1_0"
    assert " OR " not in sql
    assert list(params) == ["kbfilter_0_0", "kbfilter_1_0"]


def test_empty_filter_list_produces_no_where_clause() -> None:
    """`filters=[]` 与「不过滤」同义，不应产出一个空 WHERE（那是语法错误）。"""
    assert _sql_of(_query(_request(filters=[]), parametrized=True)) == "SELECT * FROM `t`"


def test_too_many_filters_is_rejected() -> None:
    many = [{"column": f"c{index}", "op": "eq", "value": index} for index in range(MAX_FILTERS + 1)]
    with pytest.raises(FilterError, match="too many filters"):
        build_where(many)
    assert build_where(many[:MAX_FILTERS])


def test_filter_entry_must_be_an_object() -> None:
    with pytest.raises(FilterError, match="must be an object"):
        build_where(["year=2024"])  # type: ignore[list-item]


def test_error_messages_do_not_echo_the_value() -> None:
    """错误路径是泄露的高发地（G12 已实测过一次 422 把密码原文回显）。

    本模块的口径与 `db_url_guard.py` 一致：消息只说**违反了哪条规则**，不回显
    调用方给的值；列名也只报规则，不原样打印那串被拒的东西（它本身就是注入探针）。
    注意 `MARKER_LEAK_TEST` 单独当列名是**合法**标识符，所以列名用例要拼上非法字符。
    """
    too_many = [{"column": f"c{index}", "op": "eq", "value": SENTINEL} for index in range(MAX_FILTERS + 1)]
    cases = [
        [{"column": f"{SENTINEL}; DROP TABLE t", "op": "eq", "value": 1}],
        [{"column": "a", "op": f"eq OR {SENTINEL}", "value": 1}],
        [{"column": "a", "op": "in", "value": SENTINEL}],
        [{"column": "a", "op": "is_null", "value": SENTINEL}],
        [{"column": "a", "op": "in", "value": []}],
        # 第一条合法地把哨兵绑上了，第二条才报错——消息不许顺手把整份 filters 打出来
        [{"column": "a", "op": "eq", "value": SENTINEL}, {"column": "b", "op": "eq", "value": None}],
        too_many,
    ]
    for filters in cases:
        with pytest.raises(FilterError) as caught:
            build_where(filters)
        assert SENTINEL not in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# 4. 两个入口 + CLI
# ---------------------------------------------------------------------------


def _install_fake_engine(monkeypatch) -> list[str]:
    from kb_service.services import mysql_ingest as module

    calls: list[str] = []

    def _fake(url: str):
        calls.append(url)
        raise AssertionError("create_engine 被调用了：闸门没挡在连接之前")

    monkeypatch.setattr(module, "create_engine", _fake)
    return calls


@pytest.mark.parametrize("parametrized", [True, False])
def test_service_choke_point_decides_before_connecting(monkeypatch, parametrized: bool) -> None:
    """HTTP 与 CLI 的唯一汇合点是 `MySQLIngestor.ingest`：两个方向的拒绝都要排在
    `create_engine` 之前（flag-on + 原始 where；flag-off + filters）。"""
    calls = _install_fake_engine(monkeypatch)
    from kb_service.services.mysql_ingest import MySQLIngestor

    ingestor = MySQLIngestor.__new__(MySQLIngestor)
    ingestor.config = _Cfg(parametrized)
    request = (
        _request(where="year = 2024")
        if parametrized
        else _request(filters=[{"column": "year", "op": "eq", "value": 2024}])
    )
    with pytest.raises(FilterError):
        ingestor.ingest(request)
    assert calls == []


def _app_config(parametrized: bool):
    """路由的成功路径会 `clone_config(config)`（它遍历 `fields(Config)`），所以 HTTP 面
    必须用真 `Config`：这里换哑配置不是"更宽松"，是"跑不到那条代码"。
    顺带也真的验了一次「新字段活过 clone_config」。"""
    from kb_service.core.config import Config

    config = Config()
    config.mysql_url_guard = False
    config.mysql_allowed_hosts = ""
    config.mysql_where_parametrized = parametrized
    return config


def _app_client(parametrized: bool, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from kb_service.api import routes as routes_module
    from kb_service.api.deps import get_config

    started: list = []

    class _Recorder:
        def __init__(self, *_a, **_k) -> None:
            pass

        def ingest(self, request) -> None:
            started.append(request)

    monkeypatch.setattr(routes_module, "MySQLIngestor", _Recorder)
    app = FastAPI()
    app.include_router(routes_module.router)
    app.dependency_overrides[get_config] = lambda: _app_config(parametrized)
    return TestClient(app), started


def test_route_returns_400_for_raw_where_when_the_flag_is_on(monkeypatch) -> None:
    client, started = _app_client(True, monkeypatch)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": "mysql://u:p@db.internal:3306/kb", "table": "t",
              "where": f"note = '{SENTINEL}'"},
    )
    assert response.status_code == 400, response.text
    assert "KB_MYSQL_WHERE_PARAMETRIZED" in response.text
    assert SENTINEL not in response.text, "400 的 detail 不得回显调用方给的值"
    assert started == [], "被拒的任务不应排进后台"


def test_route_returns_400_for_filters_when_the_flag_is_off(monkeypatch) -> None:
    client, started = _app_client(False, monkeypatch)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": "mysql://u:p@db.internal:3306/kb", "table": "t",
              "filters": [{"column": "year", "op": "eq", "value": 2024}]},
    )
    assert response.status_code == 400, response.text
    assert "requires KB_MYSQL_WHERE_PARAMETRIZED=true" in response.text
    assert started == []


def test_route_accepts_structured_filters_and_forwards_them(monkeypatch) -> None:
    client, started = _app_client(True, monkeypatch)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": "mysql://u:p@db.internal:3306/kb", "table": "t",
              "filters": [{"column": "year", "op": "eq", "value": 2024}], "limit": 5},
    )
    assert response.status_code == 202, response.text
    assert len(started) == 1
    passed = started[0]
    assert passed.filters[0].column == "year"
    assert passed.filters[0].value == 2024
    assert passed.where is None


def test_route_rejects_unknown_filter_key_before_queuing(monkeypatch) -> None:
    """`extra="forbid"`：有人试着把裸 SQL 塞成 `{"column":"a","op":"eq","sql":"1=1"}`
    时必须报错，而不是静默丢掉那个键。"""
    client, started = _app_client(True, monkeypatch)
    response = client.post(
        "/ingest/mysql",
        json={"connection_url": "mysql://u:p@db.internal:3306/kb", "table": "t",
              "filters": [{"column": "a", "op": "eq", "value": 1, "sql": "1=1 OR 1=1"}]},
    )
    assert response.status_code == 422, response.text
    assert started == []


def test_cli_filters_parser_accepts_json_and_rejects_other_shapes() -> None:
    from kb_service.cli import _parse_filters

    assert _parse_filters(None) is None
    assert _parse_filters('[{"column":"year","op":"eq","value":2024}]') == [
        {"column": "year", "op": "eq", "value": 2024}
    ]
    with pytest.raises(SystemExit):
        _parse_filters("year=2024")
    with pytest.raises(SystemExit):
        _parse_filters(json.dumps({"column": "year"}))


def test_cli_wires_filters_into_the_request() -> None:
    """CLI 也得能用 `filters`：否则开了 flag 的部署里，命令行导入直接不可用。"""
    import inspect

    from kb_service.cli import ingest_mysql as cli_ingest_mysql

    assert "filters" in inspect.signature(cli_ingest_mysql).parameters


# ---------------------------------------------------------------------------
# 5. 口径同源（只允许一处实现）
# ---------------------------------------------------------------------------


def test_service_and_route_reuse_the_shared_helper() -> None:
    from kb_service.api import routes as routes_module
    from kb_service.services import mysql_ingest as service_module

    for module in (routes_module, service_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from kb_service.core.sql_filters import" in source, module.__name__
        assert "enforce_where_policy" in source, module.__name__

    service_source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "build_where" in service_source


def test_the_flag_on_branch_never_concatenates_raw_where() -> None:
    """反向钉子：原始 `where` 只允许出现在 flag-off 那一支（`elif request.where:`）。
    谁把两条分支合并成「先拼 where 再拼 filters」，参数化就变成一句空话。

    判据用 `{request.where}` 这个**插值形状**而不是 `request.where` 这个名字——
    flag-on 分支照样要把它交给 `enforce_where_policy` 去拒，那是参数不是拼接。
    """
    import inspect

    from kb_service.services.mysql_ingest import MySQLIngestor

    source = inspect.getsource(MySQLIngestor._build_query)
    assert source.count('f" WHERE {request.where}"') == 1, "legacy 分支必须还在且只有一处"
    assert "elif request.where:" in source, "拼接点必须挂在 flag-off 分支上"
    flag_on_side = source.split("elif request.where:")[0]
    assert "{request.where}" not in flag_on_side
    assert "{clause}" in flag_on_side, "flag-on 走的是 build_where 产出的片段"


# ---------------------------------------------------------------------------
# 6. 部署面同步
# ---------------------------------------------------------------------------


def test_kb_ingest_env_example_pins_the_default_off() -> None:
    text = KB_ENV_FILE.read_text(encoding="utf-8")
    assert "KB_MYSQL_WHERE_PARAMETRIZED=false" in text


def test_root_env_example_forwards_the_key() -> None:
    text = ROOT_ENV_FILE.read_text(encoding="utf-8")
    assert "KB_MYSQL_WHERE_PARAMETRIZED=false" in text


def test_compose_forwards_it_with_a_false_default() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "KB_MYSQL_WHERE_PARAMETRIZED: ${KB_MYSQL_WHERE_PARAMETRIZED:-false}" in text


def test_clone_config_preserves_the_knob() -> None:
    """路由用的是 `clone_config(config)` 起的后台任务——漏抄字段会让闸门
    在 HTTP 面生效、在真正执行导入的那份配置里消失。"""
    from dataclasses import fields

    from kb_service.core.config import Config, clone_config

    assert "mysql_where_parametrized" in {f.name for f in fields(Config)}
    original = Config()
    original.mysql_where_parametrized = True
    assert clone_config(original).mysql_where_parametrized is True


def test_enforce_policy_is_symmetric() -> None:
    """helper 自身的口径：两档各关一条口子，合起来恰好覆盖 {where, filters} 的四种组合。"""
    ok_filters = [{"column": "a", "op": "eq", "value": 1}]
    enforce_where_policy(False, "a = 1", None)          # legacy：放行
    with pytest.raises(FilterError):
        enforce_where_policy(False, None, ok_filters)    # flag 关 + filters → 拒
    with pytest.raises(FilterError):
        enforce_where_policy(True, "a = 1", None)        # flag 开 + where → 拒
    enforce_where_policy(True, None, ok_filters)         # 目标态：放行
    enforce_where_policy(True, None, None)               # 无过滤：两档都放行


# ---------------------------------------------------------------------------
# 7. 真执行（字符串与方言编译都证不了「语句跑得通、值真的被绑住」）
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_conn():
    """内存 sqlite 上建一张真表。sqlite 接受反引号 quoted 标识符，
    所以 `_build_query` 产出的 MySQL 形状 SQL 可以原样执行。"""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.exec_driver_sql("CREATE TABLE t (year INTEGER, status TEXT)")
        conn.exec_driver_sql("INSERT INTO t VALUES (2024, 'ok'), (2024, 'draft'), (2023, 'ok')")
        conn.commit()
        yield conn


def _rows(statement, conn) -> list[dict]:
    """走生产那条**一模一样**的调用形状：`pd.read_sql_query(query, conn, chunksize=…)`。
    pandas 拿到 `TextClause` 与拿到裸字符串的处置不同（后者才会自己拼 params），
    所以这条同时是「绑定没被 pandas 拆掉」的证据。"""
    import pandas as pd

    return [
        row
        for chunk in pd.read_sql_query(statement, conn, chunksize=2)
        for row in chunk.to_dict("records")
    ]


def test_the_built_statement_executes_and_filters_for_real(sqlite_conn) -> None:
    statement = _query(
        _request(filters=[{"column": "year", "op": "eq", "value": 2024},
                          {"column": "status", "op": "not_in", "value": ["draft"]}]),
        parametrized=True,
    )
    assert _rows(statement, sqlite_conn) == [{"year": 2024, "status": "ok"}]


def test_an_injection_shaped_value_is_inert_when_executed(sqlite_conn) -> None:
    """布尔恒真串在 flag-off 那一支是**语法**，在 flag-on 这一支只是一个比字符串更长的**值**：
    真跑一次，它必须匹配 0 行，而不是把整张表放出去。"""
    from sqlalchemy import text

    statement = _query(
        _request(filters=[{"column": "status", "op": "eq", "value": f"{SENTINEL}' OR '1'='1"}]),
        parametrized=True,
    )
    assert _rows(statement, sqlite_conn) == []
    # 对照组：同一张表不过滤确实有 3 行——"0 行"是绑定生效的结果，不是没连上库
    assert len(_rows(text("SELECT * FROM `t`"), sqlite_conn)) == 3


def test_like_binds_the_pattern_instead_of_splicing_it(sqlite_conn) -> None:
    statement = _query(
        _request(filters=[{"column": "status", "op": "like", "value": "dra%"}]), parametrized=True
    )
    assert _rows(statement, sqlite_conn) == [{"year": 2024, "status": "draft"}]


def test_flag_off_path_still_executes_the_legacy_fragment(sqlite_conn) -> None:
    """flag 关的那支也要真跑得通：「逐位等价」不止是字符串等价。"""
    statement = _query(_request(where="year = 2024", limit=10))
    assert len(_rows(statement, sqlite_conn)) == 2
