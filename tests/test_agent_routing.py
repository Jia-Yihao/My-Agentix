"""
Agent 路由测试脚本（**不是 pytest 用例**）

测试 LangGraph 多 Agent 系统的路由和智能决策功能。
执行方式：``python -m tests.test_agent_routing --suite``

⚠️ 本文件是**需要真实 LLM + Neo4j + Milvus 的交互脚本**，不是单元测试：
它的辅助函数 `run_routing_case` 需要位置参数（测试用例对象），pytest 无法
把它当 fixture 解析。历史上这两个文件把辅助函数命名成 `test_routing` /
`test_query`，于是 ``LEGELBOT_RUN_INTEGRATION_TESTS=1`` 这个"逃逸开关"
一旦打开，pytest 就会把它们收集成用例并**报 fixture 错误**
（实测：``ERROR tests/test_agent_routing.py::test_routing``）——
换言之"按文档开启集成测试"只会得到报错。现已把辅助函数改名为 `run_*`，
pytest 不再收集；同时保留模块级 skip 作为双保险。
离线可跑的等价覆盖见 ``tests/test_unit_router.py``（启发式路由关键词）与
``tests/test_p37_http_endpoints.py``（TestClient 端到端）。
"""


import os

if __name__ != "__main__" and not os.getenv("LEGELBOT_RUN_INTEGRATION_TESTS"):
    import pytest

    pytest.skip(
        "Routing script (not a pytest suite) — run it via "
        "`python -m tests.test_agent_routing --suite`; "
        "offline coverage lives in tests/test_unit_router.py.",
        allow_module_level=True,
    )

import asyncio
import sys
from pathlib import Path
from typing import Optional, Dict, Any
from langchain_core.messages import HumanMessage

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agentix.orchestration.langgraph.lg_builder import graph
from agentix.infrastructure.core.logger import get_logger

logger = get_logger(service="test_agent_routing")


class RoutingTestCase:
    """路由测试用例"""

    def __init__(
        self,
        question: str,
        expected_route: str,
        expected_node: str,
        description: str,
        sub_decision: Optional[str] = None,
        image_path: Optional[str] = None,
        file_path: Optional[str] = None,
    ):
        self.question = question
        self.expected_route = expected_route
        self.expected_node = expected_node
        self.description = description
        self.sub_decision = sub_decision
        self.image_path = image_path
        self.file_path = file_path


async def run_routing_case(
    test_case: RoutingTestCase,
    session_id: str = "test_session"
) -> Dict[str, Any]:
    """测试单个路由问题"""

    config = {
        "configurable": {
            "thread_id": session_id,
            "image_path": test_case.image_path,
            "file_path": test_case.file_path,
        }
    }

    input_state = {
        "messages": [HumanMessage(content=test_case.question)]
    }

    try:
        result = await graph.ainvoke(input_state, config=config)

        actual_route = result.get('router', {}).get('type', 'unknown')
        actual_logic = result.get('router', {}).get('logic', '')
        response_text = result['messages'][-1].content if result.get('messages') else ''

        # 验证结果
        route_match = actual_route == test_case.expected_route
        status = "✅ PASS" if route_match else "❌ FAIL"

        logger.info(f"\n{'='*80}")
        logger.info(f"测试: {test_case.description}")
        logger.info(f"问题: {test_case.question}")
        logger.info(f"预期路由: {test_case.expected_route}")
        logger.info(f"实际路由: {actual_route}")
        logger.info(f"路由逻辑: {actual_logic}")
        logger.info(f"预期节点: {test_case.expected_node}")
        if test_case.sub_decision:
            logger.info(f"预期子决策: {test_case.sub_decision}")
        logger.info(f"回复摘要: {response_text[:150]}...")
        logger.info(f"状态: {status}")
        logger.info(f"{'='*80}\n")

        return {
            "test_case": test_case.description,
            "question": test_case.question,
            "expected_route": test_case.expected_route,
            "actual_route": actual_route,
            "route_match": route_match,
            "logic": actual_logic,
            "response": response_text,
            "status": status,
        }

    except Exception as e:
        logger.error(f"测试失败: {test_case.description}")
        logger.error(f"错误: {str(e)}", exc_info=True)
        return {
            "test_case": test_case.description,
            "question": test_case.question,
            "expected_route": test_case.expected_route,
            "actual_route": "ERROR",
            "route_match": False,
            "error": str(e),
            "status": "❌ ERROR",
        }


# ============================================================================
# 测试用例定义
# ============================================================================

TEST_CASES = [
    # 1. General-Query 测试
    RoutingTestCase(
        question="你好",
        expected_route="general-query",
        expected_node="respond_to_general_query",
        description="基本问候",
    ),
    RoutingTestCase(
        question="早上好",
        expected_route="general-query",
        expected_node="respond_to_general_query",
        description="礼貌寒暄",
    ),
    RoutingTestCase(
        question="谢谢你的帮助",
        expected_route="general-query",
        expected_node="respond_to_general_query",
        description="感谢反馈",
    ),
    RoutingTestCase(
        question="今天心情不错",
        expected_route="general-query",
        expected_node="respond_to_general_query",
        description="情绪表达",
    ),

    # 2. Additional-Query 测试
    RoutingTestCase(
        question="我想咨询法律问题",
        expected_route="additional-query",
        expected_node="get_additional_info",
        description="模糊提问（缺具体法条）",
    ),
    RoutingTestCase(
        question="这条法律怎么规定的",
        expected_route="additional-query",
        expected_node="get_additional_info",
        description="缺少关键信息（哪部法律）",
    ),
    RoutingTestCase(
        question="这个罪名会判几年",
        expected_route="additional-query",
        expected_node="get_additional_info",
        description="缺少具体案情情节",
    ),
    RoutingTestCase(
        question="我的合同好像有问题",
        expected_route="additional-query",
        expected_node="get_additional_info",
        description="纠纷描述不清",
    ),

    # 3. KB-Query 测试（向量知识库）
    RoutingTestCase(
        question="民法典的立法背景是什么",
        expected_route="kb-query",
        expected_node="create_kb_query",
        description="法律立法背景",
        sub_decision="KB Multi-tool: Milvus 或 kb_ingest 结构化库",
    ),
    RoutingTestCase(
        question="劳动合同法的立法目的",
        expected_route="kb-query",
        expected_node="create_kb_query",
        description="法规背景文化",
        sub_decision="KB Multi-tool: 智能选择向量检索源",
    ),
    RoutingTestCase(
        question="民法典合同编的特点是什么",
        expected_route="kb-query",
        expected_node="create_kb_query",
        description="法律编章介绍",
        sub_decision="KB Multi-tool: 可能合并多个来源",
    ),
    RoutingTestCase(
        question="民法典有哪些配套司法解释",
        expected_route="kb-query",
        expected_node="create_kb_query",
        description="司法解释清单介绍",
    ),
    RoutingTestCase(
        question="个人信息保护法的主要内容",
        expected_route="kb-query",
        expected_node="create_kb_query",
        description="法律内容科普",
        sub_decision="KB Multi-tool: Reranker 重排序",
    ),

    # 4. GraphRAG-Query 测试（图谱推理）
    RoutingTestCase(
        question="民法典第1条引用了哪些法条",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="法条引用关系 → Cypher",
        sub_decision="cypher_query 或 predefined_cypher → Neo4j",
    ),
    RoutingTestCase(
        question="民法典第1条包含哪些构成要件",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="法条要件查询 → Cypher",
        sub_decision="predefined_cypher: BELONGS_TO_CHAPTER 关系",
    ),
    RoutingTestCase(
        question="合同解除的法定条件是什么",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="法律条件细节 → Cypher + GraphRAG",
        sub_decision="可能同时调用 Cypher 和 microsoft_graphrag_query",
    ),
    RoutingTestCase(
        question="怎么判断合同是否成立",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="成立要件判断 → GraphRAG",
        sub_decision="microsoft_graphrag_query: LightRAG 图推理",
    ),
    RoutingTestCase(
        question="为什么我的合同被认定无效",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="效力排查 → GraphRAG + Cypher",
        sub_decision="对比有效要件推理原因",
    ),
    RoutingTestCase(
        question="什么情况下可以主张违约责任",
        expected_route="graphrag-query",
        expected_node="create_research_plan",
        description="综合图谱推理 → Multiple Tools",
        sub_decision="Cypher 查询 ARTICLE_CAUSE + GraphRAG 推理",
    ),

    # 5. Text2SQL-Query 测试（启发式路由）
    RoutingTestCase(
        question="数据库里有多少条法条",
        expected_route="text2sql-query",  # 启发式关键词 "多少"
        expected_node="create_research_plan",
        description="数据统计 → Text2SQL",
        sub_decision="text2sql_query: SELECT COUNT(*)",
    ),
    RoutingTestCase(
        question="哪部法律的条文最多",
        expected_route="text2sql-query",  # 启发式关键词 "最多"
        expected_node="create_research_plan",
        description="数据排名 → Text2SQL",
        sub_decision="text2sql_query: GROUP BY + ORDER BY",
    ),
    RoutingTestCase(
        question="统计每部法律的法条数量",
        expected_route="text2sql-query",  # 启发式关键词 "统计"
        expected_node="create_research_plan",
        description="数据趋势分析 → Text2SQL",
        sub_decision="text2sql_query: GROUP BY 查询",
    ),

    # 6. Image-Query 测试
    RoutingTestCase(
        question="生成一张法律文书的示意图",
        expected_route="image-query",
        expected_node="create_image_query",
        description="图片生成",
        sub_decision="LLM 优化提示词 → CogView-4 API",
    ),

    # 7. Guardrails 边界测试
    RoutingTestCase(
        question="今天天气怎么样",
        expected_route="additional-query",  # 可能先进入 additional-query
        expected_node="get_additional_info",
        description="无关问题（应被 Guardrails 拦截）",
        sub_decision="Guardrails 返回 end → 礼貌拒绝",
    ),
    RoutingTestCase(
        question="我肚子疼应该吃什么药",
        expected_route="additional-query",
        expected_node="get_additional_info",
        description="医疗诊断（应被 Guardrails 拦截）",
        sub_decision="Guardrails 拦截 → 建议咨询医生",
    ),
]


async def run_test_suite():
    """运行完整测试套件"""

    logger.info("=" * 80)
    logger.info("开始 My Agentix Agent 路由测试")
    logger.info("=" * 80)

    results = []
    for i, test_case in enumerate(TEST_CASES, 1):
        logger.info(f"\n[{i}/{len(TEST_CASES)}] 测试: {test_case.description}")
        result = await run_routing_case(test_case, session_id=f"test_{i}")
        results.append(result)

        # 避免请求过快
        await asyncio.sleep(1)

    # 汇总统计
    total = len(results)
    passed = sum(1 for r in results if r.get("route_match", False))
    failed = total - passed

    logger.info("\n" + "=" * 80)
    logger.info("测试汇总")
    logger.info("=" * 80)
    logger.info(f"总计: {total}")
    logger.info(f"通过: {passed} ✅")
    logger.info(f"失败: {failed} ❌")
    logger.info(f"成功率: {passed/total*100:.1f}%")
    logger.info("=" * 80)

    # 输出失败详情
    if failed > 0:
        logger.info("\n失败的测试用例:")
        for r in results:
            if not r.get("route_match", False):
                logger.info(f"  ❌ {r['test_case']}")
                logger.info(f"     问题: {r['question']}")
                logger.info(f"     预期: {r['expected_route']}")
                logger.info(f"     实际: {r['actual_route']}")
                logger.info("")

    return results


async def run_single_test(question: str):
    """运行单个测试（快速调试用）"""
    test_case = RoutingTestCase(
        question=question,
        expected_route="unknown",
        expected_node="unknown",
        description="手动测试",
    )
    result = await run_routing_case(test_case)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="My Agentix Agent 路由测试")
    parser.add_argument(
        "--single",
        type=str,
        help="运行单个测试问题（快速调试）",
    )
    parser.add_argument(
        "--suite",
        action="store_true",
        help="运行完整测试套件",
    )

    args = parser.parse_args()

    if args.single:
        # 单个测试模式
        asyncio.run(run_single_test(args.single))
    elif args.suite or not (args.single or args.suite):
        # 默认运行完整测试套件
        asyncio.run(run_test_suite())
