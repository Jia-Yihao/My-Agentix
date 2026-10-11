# 只读闸门黑名单：子串匹配基于 cypher_statement.upper()，因此必须全大写。
# CALL 拦截含 CALL {} 子查询与存储过程（含 apoc.* 写过程），保守但安全；
# APOC. 拦截 apoc.periodic.iterate / apoc.create.* 等动态写路径。
WRITE_CLAUSES = {
    "CREATE",
    "DELETE",
    "DETACH DELETE",
    "SET",
    "REMOVE",
    "FOREACH",
    "MERGE",
    "LOAD CSV",
    "CALL",
    "DROP",
    "USE",
    "IMPORT",
    "APOC.",
}


# 「无结果」占位载荷（单一事实来源）。
#
# 结构整理（批次 13）：此前 `cypher_tools/utils.py` 在**函数内**另起了一份
# 中文同名局部变量，导致同一个语义有两套文案（英文一套、中文一套）。
# 已统一到此处，并把文案统一为中文
# （本项目面向中文用户，且中文版是实际主路径上的实现）。
# 无测试依赖旧英文文案（已 grep 确认）。
NO_CYPHER_RESULTS = [{"error": "在数据库中找不到任何相关信息。"}]
