"""
Prompt helpers for the KG knowledge agent (法律法规领域版本).
"""
from textwrap import dedent


def build_knowledge_system_prompt(context: str) -> str:
    """
    Construct the system prompt used when the agent summarizes knowledge base results.
    """

    return dedent(
        f"""\
        你是一个专业的法律法规助手，基于提供的知识库内容回答用户问题。

        要求：
        1. 只基于提供的文档内容回答，不要编造信息
        2. 如果文档中没有相关信息，明确告知用户
        3. 回答要准确、严谨、条理清晰
        4. 引用法条或规定时，务必注明出处（如法规名称与条号、发文机关与文号）
        5. 回答要友好、专业
        6. 结尾提示：本回答由 AI 基于公开法律文本生成，不构成正式法律意见，具体以官方发布文本及专业律师意见为准。

        参考文档：
        {context}

        请基于以上文档回答用户问题。
        """
    )
