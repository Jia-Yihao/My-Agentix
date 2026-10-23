r"""结构化过滤条件 → 参数化 WHERE 子句（§24.3 T1 ⑫，`KB_MYSQL_WHERE_PARAMETRIZED` 默认关）。

为什么需要这一格
----------------
`MySQLIngestRequest.where` 是一段**原始 SQL 片段**，被 `f" WHERE {request.where}"`
直接拼进 `SELECT * FROM \`table\` WHERE …`。现有的闸（`_validate_where_fragment`）只
拦分号与注释，它挡得住「堆叠语句」，挡不住「布尔恒真 + 子查询」这类合法但越界的片段
（`1=1 OR …`、`id IN (SELECT … FROM mysql.user)`），因为对拼出来的 SQL 而言，
调用给的**就是**语法。更麻烦的是这条路径没有边界：一旦有人把 `where` 接到任何
非操作员输入的界面（未来的 Web 表单、任何 API 转发），它就是注入点。

参数化把判据从「语法对不对」换成「值走不走绑定」：
- **列名**不能进 bindparam，所以只接受纯标识符（字母/数字/下划线，≤64 位），
  与 `table` 用同一份口径；反引号、点号、空格、注释一律拒绝。
- **运算符**来自封闭枚举，展开成写死的 SQL 词，不参与字符串拼接。
- **值**全部走 `bindparams`，永远不成为 SQL 文本的一部分。

所以本模块产出的字符串里，唯一来自调用方的字节是「标识符」，其余全是模板。
错误消息**不回显值**（与 `db_url_guard.py` 同一条口径：错误路径才是泄露的高发地）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

#: 列名标识符：与 `schemas/ingest.py::_TABLE_IDENTIFIER` 同口径（字母/数字/下划线，1-64 位）。
#: 这里不复用那个常量，避免 core 反向依赖 schemas。
_COLUMN_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")

#: 单次导入允许的过滤条件上限：一条 `SELECT` 不该挂几百个谓词，
#: 超出即拒——也让「谁在用 filters 打探针」在请求期就现形，而不是变成一条巨型 SQL。
MAX_FILTERS = 32

#: 运算符 → SQL 模板。`{col}` 是已校验的标识符，`{p}` 是绑定名占位。
#: 值一律不进模板，模板本身不含任何调用方可控字符。
SINGLE_VALUE_OPS = {
    "eq": "{col} = {p}",
    "ne": "{col} <> {p}",
    "gt": "{col} > {p}",
    "ge": "{col} >= {p}",
    "lt": "{col} < {p}",
    "le": "{col} <= {p}",
    "like": "{col} LIKE {p}",
}
MULTI_VALUE_OPS = {
    "in": "{col} IN ({p})",
    "not_in": "{col} NOT IN ({p})",
}
NO_VALUE_OPS = {
    "is_null": "{col} IS NULL",
    "is_not_null": "{col} IS NOT NULL",
}

#: 供 schema / 文档 / 报错文案使用的完整枚举面。
SUPPORTED_OPS = (
    *SINGLE_VALUE_OPS,
    *MULTI_VALUE_OPS,
    *NO_VALUE_OPS,
)


class FilterError(ValueError):
    """过滤条件不合法。**消息只说为什么被拒，不回显列值**（同 G12 的凭据回显口径）。"""


def _require_identifier(column: Any) -> str:
    if not isinstance(column, str) or not _COLUMN_IDENTIFIER.match(column):
        raise FilterError(
            "filter column must be a plain SQL identifier (letters, digits, underscore; 1-64 chars)"
        )
    return column


def _check_op(op: Any) -> str:
    if not isinstance(op, str) or op not in SUPPORTED_OPS:
        raise FilterError(
            f"filter op must be one of {', '.join(SUPPORTED_OPS)}"
        )
    return op


def _bind_name(index: int, suffix: int = 0) -> str:
    return f"kbfilter_{index}_{suffix}"


def build_where(filters: Iterable[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """把过滤条件列表编译成 ``("… :kbfilter_0_0 …", {"kbfilter_0_0": value})``。

    返回的 SQL 片段**不含** `WHERE` 关键字，多个条件以 `AND` 连接（与原来那段
    自由文本相比这是刻意的收窄：不接受 `OR`/括号/子查询。需要布尔或关系时，
    应当由调用方分多次导入，而不是让本服务拼一段它看不懂的 SQL）。
    """
    items = list(filters or [])
    if len(items) > MAX_FILTERS:
        raise FilterError(f"too many filters: {len(items)} > {MAX_FILTERS}")

    fragments: list[str] = []
    params: dict[str, Any] = {}

    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise FilterError(f"filter #{index} must be an object with column/op/value")
        column = _require_identifier(raw.get("column"))
        op = _check_op(raw.get("op"))
        value = raw.get("value")

        if op in NO_VALUE_OPS:
            if value is not None:
                raise FilterError(f"filter #{index}: op '{op}' takes no value")
            fragments.append(NO_VALUE_OPS[op].format(col=f"`{column}`"))
            continue

        if value is None:
            # `col = NULL` 恒不成立、`col IN (NULL)` 同理——把这类写法当错误而不是
            # 静默产出一个永远为假的谓词，否则调用方会以为「过滤生效了但没数据」。
            raise FilterError(
                f"filter #{index}: op '{op}' requires a non-null value (use is_null / is_not_null)"
            )

        if op in SINGLE_VALUE_OPS:
            name = _bind_name(index)
            fragments.append(SINGLE_VALUE_OPS[op].format(col=f"`{column}`", p=f":{name}"))
            params[name] = value
            continue

        if not isinstance(value, (list, tuple)) or not value:
            # 空集合要么写成 `1=0` 要么语法报错；这里直接拒，让它是一条明确的 400。
            raise FilterError(f"filter #{index}: op '{op}' requires a non-empty list value")
        placeholders = []
        for sub, item in enumerate(value):
            name = _bind_name(index, sub)
            params[name] = item
            placeholders.append(f":{name}")
        fragments.append(
            MULTI_VALUE_OPS[op].format(col=f"`{column}`", p=", ".join(placeholders))
        )

    return " AND ".join(fragments), params


def assert_raw_where_allowed(enabled: bool, where: str | None) -> None:
    """flag 打开后原始 `where` 不再被接受——否则「参数化」只是多一个可选字段，
    注入面一条都没关。"""
    if enabled and where:
        raise FilterError(
            "raw 'where' is not accepted while KB_MYSQL_WHERE_PARAMETRIZED=true; use 'filters'"
        )


def assert_filters_need_the_flag(enabled: bool, filters: list[Any] | None) -> None:
    """flag 关着时 `filters` 是**未被启用**的输入，静默忽略比报错更坏
    （调用方以为过滤生效、实际把全表灌进了向量库），所以显式拒绝。"""
    if not enabled and filters:
        raise FilterError(
            "'filters' requires KB_MYSQL_WHERE_PARAMETRIZED=true"
        )


def enforce_where_policy(parametrized: bool, where: str | None, filters: list[Any] | None) -> None:
    """两条口径的单一入口。

    路由（入队前，回 400）与服务层（HTTP/CLI 的汇合点）都调它——和 G12 的连接串闸门
    同一个理由：只放在路由，CLI 就绕过；只放在服务层，客户端拿到的就是 202 + 一条
    服务端异常日志，不知道自己传的过滤条件被拒了。
    """
    assert_raw_where_allowed(parametrized, where)
    assert_filters_need_the_flag(parametrized, filters)
