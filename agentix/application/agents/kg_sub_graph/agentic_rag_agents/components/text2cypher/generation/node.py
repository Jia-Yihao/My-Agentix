from typing import Any, Callable, Coroutine, Dict

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_neo4j import Neo4jGraph

from ....components.text2cypher.generation.prompts import (
    create_text2cypher_generation_prompt_template,
    legacy_schema_hint,
)
from ....components.utils.utils import resolve_generation_schema
from ....retrievers.cypher_examples.base import BaseCypherExampleRetriever
from ..state import CypherInputState

# 定义text2cypher generation prompt
generation_prompt = create_text2cypher_generation_prompt_template()


def create_text2cypher_generation_node(
    llm: BaseChatModel,
    graph: Neo4jGraph,
    cypher_example_retriever: BaseCypherExampleRetriever,
) -> Callable[[CypherInputState], Coroutine[Any, Any, dict[str, Any]]]:
    text2cypher_chain = generation_prompt | llm | StrOutputParser()

    async def generate_cypher(state: CypherInputState) -> Dict[str, Any]:
        """
        Generates a cypher statement based on the provided schema and user input
        """
        print("我进入到生成cypher的节点了")
        print("state", state)

        task = state.get("task", "")
        # 获取针对当前任务的cypher示例, 选择 k 个
        examples: str = cypher_example_retriever.get_examples(
            **{"query": task[0] if isinstance(task, list) else task, "k": 3}
        )
        generated_cypher = await text2cypher_chain.ainvoke(
            {
                "question": state.get("task", ""),
                "fewshot_examples": examples,
                # Stage F2b-d（R-P2-3）：flag-off=基线裸 graph.schema +
                # 静态冗余块（逐字节等价）；flag-on=发现态渲染 + 静态块置空
                "schema": resolve_generation_schema(graph),
                "legacy_schema_hint": legacy_schema_hint(),
            }
        )



        return {"statement": generated_cypher, "steps": ["generate_cypher"]}

    return generate_cypher
