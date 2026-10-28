"""顶层工具层（M1 自 ``agentix/capabilities/tools`` 迁入）：gateway / registry / adapters 汇聚 + 内置工具注册。"""
from functools import cache
from typing import Any, Dict, List

from agentix.tools.definitions import (
    CONTROL_REQUIRED_FROM,
    FORBIDDEN_RISK_LEVEL,
    RISK_LEVELS,
    ToolRegistry,
    ToolSpec,
    is_llm_visible_tool,
    llm_visible_specs,
    registry,
)
from agentix.tools.gateway.human_review import RiskLevel
from agentix.tools.registry_init import ensure_registered, get_active_tools
from agentix.tools.adapters.search import SearchTool

# Register built-in tools.（原 infrastructure/tools/__init__.py 注册逻辑）
registry.register(
    ToolSpec(
        name="search",
        risk_level="L0",  # 只读外部检索（结果视为不可信输入，走注入隔离——指南 §2.1.4 上下文层）
        description=(
            "使用 SerpAPI 执行 Google 搜索以获取最新的公开信息。\n"
            "何时用：需要时效性强的公开信息时（最新新闻、政策变动、实时数据、"
            "近期事件等静态知识库无法覆盖的内容）。\n"
            "何时不能用：查询权威测试数据集数据条目/示例说明的准确定义时——应走知识库或知识图谱"
            "检索路径（cypher_query / predefined_cypher / microsoft_graphrag_query）；"
            "需要可引用的来源页面、要求答案附出处时——本工具不适用；"
            "问题属于领域内静态测试数据知识、知识库已收录时也无需联网。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用于检索的关键词或问题",
                },
                "num_results": {
                    "type": "integer",
                    "description": "需要返回的结果数量",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["query"],
        },
    ),
    SearchTool,
)


def list_tool_specs() -> List[Dict[str, Any]]:
    """Return tool function definitions formatted for OpenAI function calling.

    按注册表全集导出 ⇒ 必须过一遍 LLM 可见面的唯一判定
    （``agentix/tools/definitions.py::llm_visible_specs``）：不进 LLM 可见
    工具面的工具（``code_execute``）不能出现在这里——它由 ``code_agent``
    节点直接调用，不交给模型自由挑选（口径见 ``.qoder/rules/code-exec.md`` §4）。
    """
    return [spec.as_openai_function() for spec in llm_visible_specs(registry.get_specs())]


def get_tool_spec(name: str) -> Dict[str, Any]:
    """Retrieve a single tool spec formatted for OpenAI function calling.

    单点查询同样受 LLM 可见面约束：请求一条**不进 LLM 可见工具面**的工具
    的 OpenAI schema 是拼装工具面的漏口 ⇒ 直接拒绝（fail-closed），
    不返回 schema 让调用方自己拼进工具清单。
    """
    spec = registry.get_spec(name)
    if not is_llm_visible_tool(spec.name):
        raise KeyError(
            f"tool {name!r} is excluded from the LLM-visible tool surface"
        )
    return spec.as_openai_function()


def list_openai_tools() -> List[Dict[str, Any]]:
    """Return tool entries suitable for the OpenAI `tools` argument.

    同 ``list_tool_specs()``：按注册表全集导出，必须过 LLM 可见面的唯一判定。
    """
    return [spec.as_openai_tool() for spec in llm_visible_specs(registry.get_specs())]


@cache
def get_tool(name: str) -> Any:
    """Instantiate and cache a tool by name（preflight_review 非消费式粗闸门）。

    **本函数的闸门是「声明级 + 非消费式」的粗闸门，不是最终判定。**

    > 一句话记住：**闸门粒度是「每次实例化」，不是「每次调用」**——
    > 本函数只拿得到 ``name``，拿不到 ``arguments``。

    两件事必须分清：

    1. **粒度**：这里只有 ``name``，拿不到调用参数，所以 ``classify_risk`` 的
       **参数提权**（``force`` / ``all=true``）在这条路径上永远不会触发。
       能拦住的是「工具名/声明级就是高风险」的工具；拦不住的是
       「低风险工具 + 高风险参数」（如 ``read_table(all=True)``）。
    2. **是否消费批准**：本函数传 ``consume=False``，只**看**有没有可用批准，
       **不**把它标记为已用。因为实例化只是"拿到工具对象"，不是"执行了一次
       动作"——若在此消费，一次批准会被"取对象"吃掉，真正执行时反而没有
       批准可用，或让一个**没带参数**的检查消费掉**带具体参数**的批准
       （``args=None`` 无从校验 ``args_digest``，等于把批准范围放宽）。

    **真正的调用点级闸门在** :meth:`ToolExecutor.execute` —— 那里同时持有
    ``tool_name`` 与 ``arguments``，并且是唯一消费批准的地方（一次批准 =
    一次执行）。本函数只保证"危险工具不会被无声地取出来"。
    """
    from agentix.tools.gateway.human_review import preflight_review

    spec = registry.get_spec(name) if registry.has_tool(name) else None
    preflight_review(
        name,
        consume=False,
        declared_risk_level=spec.risk_level if spec is not None else None,
    )
    return registry.create(name)


__all__ = [
    "ToolSpec", "ToolRegistry", "registry", "RISK_LEVELS",
    "FORBIDDEN_RISK_LEVEL", "CONTROL_REQUIRED_FROM",
    "SearchTool", "RiskLevel", "ensure_registered", "get_active_tools",
    "list_tool_specs", "get_tool_spec", "list_openai_tools", "get_tool",
    "is_llm_visible_tool", "llm_visible_specs",
]

