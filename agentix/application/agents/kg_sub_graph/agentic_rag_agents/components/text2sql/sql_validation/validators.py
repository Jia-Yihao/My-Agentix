"""
SQL validation helpers.

G5：语法闸与安全检查都改用 ``sql_lexicon`` 的同一份词法（先剥字符串字面量与注释、
再按词边界匹配），否则 ``WHERE note LIKE '%update %'`` 会被误杀，
而 ``LOAD_FILE('/etc/passwd')`` 一条都不命中。
"""

from typing import List, Tuple

from agentix.domain.policies.sql_read_only import (
    SYNTAX_PREFIXES,
    check_read_only,
    find_dangerous,
    read_only_prefix,
    strip_literals,
)


def validate_sql_syntax(sql: str, db_type: str = "MySQL") -> Tuple[bool, List[str]]:
    """
    Perform lightweight syntax checks suitable for LLM-generated SQL.
    """
    errors: List[str] = []

    if not sql or not sql.strip():
        errors.append("SQL 语句为空")
        return False, errors

    # 语法面维持原有的窄口径（只认 SELECT/WITH）：放宽等于扩大能进执行器的语句种类。
    if not read_only_prefix(sql, SYNTAX_PREFIXES):
        errors.append("仅支持以 SELECT 或 WITH 开头的只读查询")

    # 括号/引号计数在剥掉字面量之后做：完整字符串里的引号与括号不是语法的一部分
    # （旧版在原文上数，``SELECT 'it''s'`` 会因引号数为奇数被误判成语法错）。
    # 真正未闭合的引号剥不掉，会原样留在 body 里，仍然报得出来。
    body = strip_literals(sql)
    if body.count("(") != body.count(")"):
        errors.append("括号不匹配")
    if body.count("'") % 2 != 0:
        errors.append("单引号不匹配")
    if body.count('"') % 2 != 0:
        errors.append("双引号不匹配")

    return len(errors) == 0, errors


def validate_sql_security(sql: str) -> Tuple[bool, List[str]]:
    """
    Detect obviously destructive SQL patterns.
    """
    hits = find_dangerous(sql)
    warnings = [f"检测到危险操作: {hit}" for hit in hits]
    if not warnings and not check_read_only(sql)[0]:
        warnings.append("语句未通过只读判定")
    return len(warnings) == 0, warnings
