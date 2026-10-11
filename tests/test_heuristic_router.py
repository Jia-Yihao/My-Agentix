#!/usr/bin/env python3
"""Heuristic Router 关键词兜底机制**脚本**（不是 pytest 用例）

执行方式：``python -m tests.test_heuristic_router``

⚠️ 需要真实 LLM 才能跑通（走 graph.astream）。辅助函数原名 `test_query`，
带三个位置参数——pytest 会把它当用例收集并报 fixture 错误
（``ERROR tests/test_heuristic_router.py::test_query``），所以"按文档打开
LEGELBOT_RUN_INTEGRATION_TESTS"只会得到报错。已改名为 `run_query_case`，
pytest 不再收集。
**离线**的关键词兜底覆盖在 ``tests/test_unit_router.py``（无需 LLM）。
"""

import os

if __name__ != "__main__" and not os.getenv("LEGELBOT_RUN_INTEGRATION_TESTS"):
    import pytest

    pytest.skip(
        "Heuristic-router script (not a pytest suite) — run it via "
        "`python -m tests.test_heuristic_router`; "
        "offline coverage lives in tests/test_unit_router.py.",
        allow_module_level=True,
    )
import asyncio
import sys
from pathlib import Path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from agentix.orchestration.langgraph.lg_states import InputState
from agentix.orchestration.langgraph.lg_builder import graph
from agentix.application.agents.utils import new_uuid
from langchain_core.messages import HumanMessage

async def run_query_case(query: str, expected_route: str, description: str):
    print(f'\n{"="*80}')
    print(f'🧪 {description}')
    print(f'{"="*80}')
    print(f'📝 查询: {query}')
    print(f'🎯 期望路由: {expected_route}')
    print(f'{"-"*80}')

    try:
        thread = {'configurable': {'thread_id': new_uuid()}}
        input_state = InputState(messages=[HumanMessage(content=query)])

        response_content = []
        router_type = None
        router_logic = None

        async for chunk, metadata in graph.astream(input_state, stream_mode='messages', config=thread):
            if chunk.content and 'research_plan' not in metadata.get('tags', []):
                response_content.append(chunk.content)
                # 只显示前 200 个字符
                if len(''.join(response_content)) < 200:
                    print(chunk.content, end='', flush=True)

        # 获取路由信息
        state = graph.get_state(thread)
        if state and len(state) > 0:
            final_state = state[0]
            if hasattr(final_state, 'values') and 'router' in final_state.values:
                router_info = final_state.values['router']
                router_type = router_info.get('type') if isinstance(router_info, dict) else None
                router_logic = router_info.get('logic') if isinstance(router_info, dict) else None

        print(f'\n{"-"*80}')
        print(f'🔀 实际路由: {router_type}')
        if router_logic:
            print(f'💭 路由逻辑: {router_logic}')

        # 检查是否有 heuristic override
        if router_logic and 'heuristic' in router_logic.lower():
            print('✨ 检测到关键词兜底机制！')

        success = router_type == expected_route
        if success:
            print('✅ 路由正确匹配')
        else:
            print(f'⚠️  路由不匹配 (期望: {expected_route}, 实际: {router_type})')

        return {'success': success, 'actual_route': router_type, 'logic': router_logic}

    except Exception as e:
        print(f'\n❌ 错误: {str(e)[:300]}')
        import traceback
        traceback.print_exc()
        return {'success': False, 'actual_route': 'ERROR', 'error': str(e)}

async def main():
    print(f'\n{"="*80}')
    print('🚀 Heuristic Router 关键词兜底机制测试')
    print(f'{"="*80}')
    print('关键词: 法条、怎么规定、司法解释、赔偿、违约、责任')
    print(f'{"="*80}')

    # 专门测试应该触发关键词兜底的查询
    test_cases = [
        {
            'query': '这份合同违约要承担什么责任？',
            'expected': 'graphrag-query',
            'description': 'GraphRAG - 违约责任查询 (关键词: 违约、责任) ⭐',
            'keywords': ['违约', '责任']
        },
        {
            'query': '民法典第1条怎么规定的？',
            'expected': 'graphrag-query',
            'description': 'GraphRAG - 法条内容查询 (关键词: 怎么规定) ⭐',
            'keywords': ['怎么规定']
        },
        {
            'query': '工伤赔偿的司法解释有哪些？',
            'expected': 'graphrag-query',
            'description': 'GraphRAG - 司法解释查询 (关键词: 司法解释、赔偿) ⭐',
            'keywords': ['司法解释', '赔偿']
        },
        {
            'query': '合同违约了要承担哪些责任？',
            'expected': 'graphrag-query',
            'description': 'GraphRAG - 法律责任查询 (关键词: 违约、责任) ⭐',
            'keywords': ['违约', '责任']
        },
        {
            'query': '你好',
            'expected': 'general-query',
            'description': 'General - 问候 (无关键词)',
            'keywords': []
        },
    ]

    results = []
    for i, test_case in enumerate(test_cases, 1):
        print(f'\n[测试 {i}/{len(test_cases)}]')
        print(f'期望触发关键词: {test_case["keywords"]}')

        result = await run_query_case(
            query=test_case['query'],
            expected_route=test_case['expected'],
            description=test_case['description']
        )

        result.update({
            'query': test_case['query'],
            'expected': test_case['expected'],
            'keywords': test_case['keywords']
        })
        results.append(result)

        await asyncio.sleep(1)

    # 打印总结
    print(f'\n{"="*80}')
    print('📊 测试总结')
    print(f'{"="*80}')

    total = len(results)
    passed = sum(1 for r in results if r['success'])

    print(f'总测试数: {total}')
    print(f'✅ 通过: {passed} ({passed/total*100:.1f}%)')
    print(f'❌ 失败: {total - passed}')

    # 关键词兜底统计
    print(f'\n{"-"*80}')
    print('关键词兜底机制触发情况:')
    print(f'{"-"*80}')

    heuristic_count = 0
    for result in results:
        if result.get('keywords') and result.get('logic'):
            logic = result.get('logic', '').lower()
            has_heuristic = 'heuristic' in logic or 'override' in logic or '关键词' in logic
            if has_heuristic:
                heuristic_count += 1
                print(f"✨ {result['query'][:30]}... → 兜底生效")
            elif result['keywords']:
                print(f"⚠️  {result['query'][:30]}... → 兜底未触发 (包含关键词: {result['keywords']})")

    print(f'\n关键词兜底触发次数: {heuristic_count}')

    # 路由分组统计
    print(f'\n{"-"*80}')
    print('路由分组统计:')
    print(f'{"-"*80}')

    route_stats = {}
    for result in results:
        route = result.get('actual_route', 'UNKNOWN')
        if route not in route_stats:
            route_stats[route] = 0
        route_stats[route] += 1

    for route, count in sorted(route_stats.items()):
        print(f'  {route:20s}: {count} 次')

    print(f'{"="*80}\n')

if __name__ == '__main__':
    asyncio.run(main())
