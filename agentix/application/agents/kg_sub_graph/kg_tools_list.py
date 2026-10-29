"""
测试数据集知识图谱工具定义
基于数据本体（数据集/数据条目/章节/数据分类/示例说明/数据来源）与 Neo4j 测试数据图谱模型

**M2-4 拆边（2026-10）**：4 个参数 schema 类的规范位已移到
``agentix.tools.kg_arg_schemas``（拆 ``tools → application`` 边；参数 schema 是工具
契约数据，留在 tools 层——domain 层对第三方依赖零豁免，见
``tests/test_p17_domain_layer.py``）；本文件保留为**再导出 shim（对象同一）**——
既有消费方（``tests/test_p12_reuse.py`` / ``tests/test_p13_registry.py``）
的 import 路径不变（先例：``runtime/control_plane.py`` 的 M0 re-export shim）。
"""
from agentix.tools.kg_arg_schemas import (  # noqa: F401 - re-export shim（规范位在 tools 层）
    cypher_query,
    microsoft_graphrag_query,
    predefined_cypher,
    text2sql_query,
)
