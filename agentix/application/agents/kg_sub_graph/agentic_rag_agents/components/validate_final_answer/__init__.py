"""终态答案校验组件：**prompt 与响应模型**（节点函数已删除）。

本包当前只提供两个东西：

- :mod:`models` —— ``ValidateFinalAnswerResponse``（结构化输出模型）
- :mod:`prompts` —— ``create_validate_final_answer_prompt_template``

它们被主链路的 ``components/final_answer_guard.py`` 复用（summarize →
final_answer_guard → final_answer），因此本包**不能**整体删除。

历史（2026-09 审计，已获授权清理）
----------------------------------
本包此前还导出 ``create_validate_final_answer_node``，但该节点函数
**传递性不可达**：它唯一的消费者是
``workflows/multi_agent/text2cypher_with_viz_and_follow_ups.py``，而那个工作流
全仓 0 处 import（主链路走 ``multi_tool.py`` 的 ``create_multi_tool_workflow``），
且它本身**根本无法导入**——``from .edges import viz_mapper_edge`` 里
``edges.py`` 并不存在该符号。

于是形成一条纯负债链：一个不可导入的文件 → 撑着一个不可达的节点函数 →
让 ``tests/test_p40_import_health.py`` 的「已知坏导入」名单长期非空。
两者已于本次一并删除（回滚点：git 提交 ``dbca16d``），
``tests/test_p24_deadcode_marking.py`` 改为守卫「删除后不得复活」。
"""

#: 本包保留的公开符号（节点函数已删除，见上方历史说明）
__all__: list[str] = []
