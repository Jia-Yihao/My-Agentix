from langchain_core.prompts import ChatPromptTemplate
from agentix.application.agents.kg_sub_graph.prompts.kg_prompts import PLANNER_SYSTEM_PROMPT


# planner 节点的人类消息模板：静态规则在前，动态问题以 {question} 占位于后。
PLANNER_HUMAN_PROMPT = """规则:
    * 确保任务不会返回重复或相似的信息。
    * 确保任务不依赖于从其他任务收集的信息！
    * 相互依赖的任务应该合并为单个问题。
    * 返回相同信息的任务应该合并为单个问题。

    问题: {question}
"""


def create_planner_prompt_template() -> ChatPromptTemplate:
    """
    Create a planner prompt template.

    Returns
    -------
    ChatPromptTemplate
        The prompt template.
    """
    return ChatPromptTemplate.from_messages(
        [
            (
                "system",
                PLANNER_SYSTEM_PROMPT,
            ),
            (
                "human",
                PLANNER_HUMAN_PROMPT,
            ),
        ]
    )
