"""Text2SQL 只读闸的唯一词法口径（domain policy：零第三方依赖的纯判定）。

G5（§24.2）：此前 ``sql_execution/_is_read_only_query`` 与
``sql_validation/validate_sql_security`` 各维护一份黑名单，两份不一致——同一句 SQL
可能过了一闸被另一闸拦下（或反过来）。更糟的是两侧都在**未剥离字符串字面量**的原文上做
子串匹配，于是 ``WHERE note LIKE '%update %'`` 这种完全只读的查询会被误杀，
而 ``LOAD_FILE('/etc/passwd')`` 这种真正的读文件外带却一条都不命中。

本模块把三件事收敛成一个来源：
1. 先剥字面量与注释，再匹配（消误杀）；
2. 按词边界匹配，不用裸子串（``selected`` 不该命中 ``SELECT``）；
3. 歧义动词只拦「写用法」：``REPLACE()`` 是只读字符串函数，``REPLACE INTO`` 才是写。
"""

from __future__ import annotations

import re
from typing import List, Tuple

#: 只读查询允许的起始关键词。
READ_ONLY_PREFIXES: Tuple[str, ...] = ("SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC")

#: 语法闸额外收窄：执行闸接受 SHOW/EXPLAIN 这类元数据语句，但 dry-run 语法面历史上
#: 只认 SELECT/WITH。这条更窄的口径是**测试基线**（test_p85 用 `SHOW TABLES` 代表词法失败），
#: 把它并进 READ_ONLY_PREFIXES 等于悄悄放宽能进执行器的语句种类。
SYNTAX_PREFIXES: Tuple[str, ...] = ("SELECT", "WITH")

#: 单个词就足以判定为写/危险，剥字面量后按词边界匹配即可。
DANGEROUS_WORDS: Tuple[str, ...] = (
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE", "MERGE", "EXEC", "EXECUTE", "CALL", "HANDLER",
    "RENAME", "LOCK", "LOAD", "KILL", "SHUTDOWN",
    "SLEEP", "BENCHMARK", "LOAD_FILE", "OPENROWSET",
)

#: 必须成对出现才算危险的短语。
DANGEROUS_PHRASES: Tuple[str, ...] = (
    "REPLACE INTO",
    "INTO OUTFILE",
    "INTO DUMPFILE",
    "LOAD DATA",
    "FOR UPDATE",
    "LOCK IN SHARE MODE",
)

_LITERAL_RE = re.compile(
    r"""
      '(?:[^'\\]|\\.)*'      # 单引号字符串（含转义）
    | "(?:[^"\\]|\\.)*"      # 双引号字符串
    | `[^`]*`                # MySQL 反引号标识符
    | /\*.*?\*/              # 块注释
    | --[^\n]*               # 行注释（-- ）
    | \#[^\n]*               # 行注释（MySQL #）
    """,
    re.VERBOSE | re.DOTALL,
)

_WORD_RE = re.compile(r"[A-Z_]+")


def strip_literals(sql: str) -> str:
    """把字面量、标识符引号与注释替换成等长空格，保持括号/引号计数的位置感。"""

    def _blank(match: re.Match) -> str:
        return " " * (match.end() - match.start())

    return _LITERAL_RE.sub(_blank, sql or "")


def normalized_body(sql: str) -> str:
    """剥字面量后的大写正文，两闸共用这一份。"""
    return strip_literals(sql).upper()


def find_dangerous(sql: str) -> List[str]:
    """返回命中的危险模式（已剥字面量、按词边界匹配）。"""
    body = normalized_body(sql)
    collapsed = re.sub(r"\s+", " ", body)
    hits: List[str] = []
    words = {w.group() for w in _WORD_RE.finditer(body)}
    for word in DANGEROUS_WORDS:
        if word in words:
            hits.append(word)
    for phrase in DANGEROUS_PHRASES:
        if phrase in collapsed:
            hits.append(phrase)
    return hits


def read_only_prefix(sql: str, prefixes: Tuple[str, ...] = READ_ONLY_PREFIXES) -> bool:
    """首关键词是否属于只读集合（忽略前导空白与左括号）。"""
    head = strip_literals(sql).lstrip().lstrip("( \t\r\n").upper()
    return any(head.startswith(prefix) for prefix in prefixes)


def has_multiple_statements(sql: str) -> bool:
    """分号 stacking 检查在**剥字面量之后**做，否则 ``WHERE a=';'`` 会被误判成两条。"""
    body = strip_literals(sql).rstrip().rstrip(";")
    return ";" in body


def check_read_only(sql: str) -> Tuple[bool, List[str]]:
    """两闸统一入口：返回 (是否只读, 原因列表)。"""
    reasons: List[str] = []
    if not sql or not sql.strip():
        return False, ["empty statement"]
    if has_multiple_statements(sql):
        reasons.append("multiple statements")
    if not read_only_prefix(sql):
        reasons.append("statement does not start with a read-only keyword")
    hits = find_dangerous(sql)
    if hits:
        reasons.append("dangerous pattern: " + ", ".join(hits))
    return not reasons, reasons
