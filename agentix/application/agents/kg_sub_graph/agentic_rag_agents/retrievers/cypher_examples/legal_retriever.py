"""法律领域 Cypher 示例检索器（自包含）。

本模块提供 ``LegalCypherRetriever``，为 Neo4j 法律法规知识图谱的 text2cypher
生成提供高质量 few-shot 示例。它不调用任何规则式分类模块
（``QuestionClassifier`` / ``QuestionParser`` 已移除）。

R-P0-4（图谱硬化最小集）：原「按关键词静默分类」的动态检索部分**显式标记为
未实现**——``_dynamic_classify`` 抛 ``NotImplementedError``，``get_examples``
命中即走显式降级（warning 留痕 + 通用示例兜底），不再假装动态分类。

R-P2-3（Graph 动态化，Stage F2b-d）最终裁定：**动态 schema 由发现态渲染承担**
（``components/utils/utils.py::resolve_generation_schema``，flag
``KG_SCHEMA_AWARE_GENERATION_ENABLED``，见 rag-optimization §6）；**示例分类
不在冻结设计范围内**——few-shot 维持策展通用示例 fast-path + 词法相关性重排，
``_dynamic_classify`` 保持显式未实现（契约见 ``tests/test_p68_rag_p0_convergence.py``）。

常量 ``LEGAL_GRAPH_SCHEMA`` 同时作为 text2cypher 的静态 schema 说明，供
``kg_prompts.TEXT2CYPHER_GENERATION_PROMPT`` 与
``text2cypher/generation/prompts.py`` 引用，避免描述漂移。
"""

import logging
import re

from agentix.application.agents.kg_sub_graph.agentic_rag_agents.retrievers.cypher_examples.base import (
    BaseCypherExampleRetriever,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 法律法规图谱静态 Schema 说明（喂给 text2cypher 的强化文本）
# ---------------------------------------------------------------------------
LEGAL_GRAPH_SCHEMA = """你正在查询一个中国法律法规知识图谱（Neo4j）。图谱结构如下：

【节点 Label 与属性】
- 法规(Regulation): name(法规名), 生效日期, 发文字号, 效力层级
- 法条(Article): 条号, 法规(所属法规名), 内容, 关键词
- 章节(Chapter): 标题, 层级, 所属法
- 案由(Cause): 名称, 类别(民事/刑事/行政)
- 司法解释(JudicialInterpretation): name, 发布机关, 日期, 文号
- 机关(Authority): name, 层级

【关系类型与方向】
- (法条)-[:属于]->(章节)
- (章节)-[:属于]->(法规)
- (法条)-[:引用]->(法条)            // 法条互引
- (法条)-[:对应案由]->(案由)        // 法条适用的纠纷类型
- (法条)-[:有司法解释]->(司法解释)  // 本条文对应的司法解释
- (法规)-[:制定机关]->(机关)
- (司法解释)-[:制定机关]->(机关)
- (法规)-[:上位法]->(法规)          // 下位法指向上位法

【生成规则】
1. 仅输出 Cypher，禁止反引号/解释。
2. 用 MATCH/WITH 开头；标签、关系、属性名必须与上面完全一致。
3. 过滤用参数化（$法规名/$条号/$案由名），不要硬编码。
4. 排序法条按条号数值；多结果用 LIMIT。
5. 返回时尽量带上 法规名 与 条号 以便溯源。
"""


class LegalCypherRetriever(BaseCypherExampleRetriever):
    """法律法规场景的 Cypher 示例检索器（自包含；动态分类未实现——R-P0-4 显式降级）。"""

    def __init__(self):
        """初始化检索器，无需任何外部分类器。"""
        super().__init__()

    def get_examples(self, query: str, k: int = 5) -> str:
        """根据用户查询返回相关的 Cypher 查询示例。

        R-P0-4：dynamic 分类显式未实现——``_dynamic_classify`` 抛
        ``NotImplementedError`` 时 warning 留痕并降级为通用示例，
        不再按关键词静默分类（R-P2-3 裁定：分类不在范围，见模块 docstring）。

        Parameters
        ----------
        query : str
            用户的自然语言查询
        k : int, optional
            返回的示例数量, by default 5

        Returns
        -------
        str
            格式化的示例字符串，每个示例包含问题和对应的 Cypher 查询
        """
        try:
            self._dynamic_classify(query)
        except NotImplementedError:
            logger.warning(
                "dynamic cypher-example classification not implemented (R-P0-4); "
                "falling back to general examples"
            )

        # 通用示例（显式降级路径的现役内容；R-P2-3 裁定分类不在范围，
        # 类型桶不再引入——策展 fast-path + 词法重排即终态）
        general_examples = [
            {
                "question": "列出民法典的全部章节",
                "cypher": (
                    "MATCH (c:章节)-[:属于]->(:法规 {name:'民法典'}) "
                    "RETURN c.标题 AS 章节, c.层级 AS 层级 ORDER BY c.层级"
                ),
            },
            {
                "question": "某法条同时属于哪些案由",
                "cypher": (
                    "MATCH (f:法条 {条号:'577', 法规:'民法典'})-[:对应案由]->(ca:案由) "
                    "RETURN ca.名称 AS 案由, ca.类别 AS 类别"
                ),
            },
            {
                "question": "统计刑事类案由有多少种",
                "cypher": (
                    "MATCH (c:案由 {类别:'刑事'}) "
                    "WITH count(c) AS 数量 RETURN 数量"
                ),
            },
            {
                "question": "查找包含关键词'赔偿'的法条",
                "cypher": (
                    "MATCH (f:法条) WHERE '赔偿' IN f.关键词 "
                    "RETURN f.法规 AS 法规, f.条号 AS 条号 LIMIT 15"
                ),
            },
        ]

        # 1. 显式降级：dynamic 分类未实现，全量走通用示例
        #    （R-P2-3 裁定：分类不在冻结设计范围，此处即终态）
        selected_examples: list = list(general_examples)

        # 2. 关键词相关性排序与截取
        def compute_relevance(example: dict, query_text: str) -> int:
            score = 0
            query_words = set(re.findall(r"[\w一-鿿]+", query_text.lower()))
            example_words = set(re.findall(r"[\w一-鿿]+", example["question"].lower()))
            overlap = len(query_words.intersection(example_words))
            score += overlap * 2

            important_keywords = {
                "法条": 3, "条号": 3, "条文": 3, "司法解释": 3,
                "案由": 2, "法规": 2, "宪法": 1, "民法典": 2, "刑法": 2,
                "劳动合同": 2, "违约": 2, "赔偿": 2, "盗窃": 2,
                "正当防卫": 2, "机关": 2, "上位法": 2, "引用": 2,
                "章节": 1, "制定": 1,
            }
            for keyword, weight in important_keywords.items():
                if keyword in query_text and keyword in example["question"]:
                    score += weight
            return score

        scored = [(ex, compute_relevance(ex, query)) for ex in selected_examples]
        scored.sort(key=lambda x: x[1], reverse=True)
        final_examples = [ex for ex, _ in scored[:k]]

        # 3. 格式化输出
        formatted = "\n\n".join(
            f"Question: {ex['question']}\nCypher: {ex['cypher']}" for ex in final_examples
        )
        return formatted

    # ------------------------------------------------------------------
    # 内部：dynamic 分类标记（R-P0-4：显式未实现，不再静默关键词分类）
    # ------------------------------------------------------------------
    @staticmethod
    def _dynamic_classify(query: str) -> str:
        """dynamic 示例分类入口——显式未实现（R-P0-4；R-P2-3 裁定保持）。

        历史实现按关键词把问题静默分桶（``_classify``），伪装成动态检索；
        本该由 dynamic retriever 完成的分类不具备实现，故显式抛
        ``NotImplementedError``——调用方 ``get_examples`` 命中即 warning
        留痕并降级到通用示例。R-P2-3 落地时经评估裁定：示例分类不在
        冻结设计范围（schema 动态化由发现态渲染承担），本方法保持显式
        未实现，契约由 p68 钉住。
        """
        raise NotImplementedError(
            "dynamic cypher-example classification is not implemented (R-P0-4)"
        )
