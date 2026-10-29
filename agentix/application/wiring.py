"""application 装配期节点注册（§7.5 第②步的唯一落点）。

## 这个文件为什么存在

`docs/architecture/分层架构与依赖规则.md` §7.5 的修法是三步：①`orchestration` 侧定义
节点注册协议（M3-1，`agentix/orchestration/node_registry.py`）②`application` 装配期
注册全部 handler（**本文件**）③建图/分派文件只接收注册表、不再顶层 import 具体 handler
（M3-2 起的拆边批次）。第②步的落点必须只有这一个文件：哪些名字、什么形态进入注册表，
是编排层解析节点的唯一事实（`AGENTS.md` 纪律 6 ④：同一个判定不许两个源）。

## 为什么 provider 一律是「模块形态 + 零参 thunk」

注册协议要求 thunk 返回**当前模块属性**而非 import 期快照（`node_registry` 模块
docstring 第 2 条：`resolve` 每次都走 thunk、不缓存）。这决定了 import 形态只能是
`import agentix.application.agents.general_handler as general_handler` 再
`lambda: general_handler.respond_to_general_query`；**不能**写成
`from ... import respond_to_general_query` 后 `lambda: respond_to_general_query`
——后者被 import 求值一次就绑死了函数对象，测试对 application 模块属性的 patch
会打不中，而打不中的表现是「测试通过但没测到东西」。

## 本模块必须由装配根显式 import

编排层（`agentix/orchestration/**`）**不得** import 本模块：反向边的拆除靠的是
「编排层不认识具体 handler」，若编排层反过来 import 装配文件，等于把反向边换个名字
绕回来（由 `tests/test_p179_supervisor_wiring.py` 的静态守卫钉住）。装配根的挂线位：

- `agentix/main.py`（FastAPI 装配根）；
- `agentix/application/agents/main.py`（独立 CLI 入口，不经 FastAPI 启动序列）；
- `tests/conftest.py`（测试套件的等价装配根）；
- `agentix/application/services/chat_service.py`（图执行服务的挂线位，M3-3 起）——
  覆盖**不经 FastAPI/CLI 装配根的直调进程**（`evals/`、`bench/` 经 chat_service 或
  直接 import lg_builder 拉起图；建图在 import 期完成、不触注册表，节点解析发生在
  **调用期**，注册必须在此之前落定，本服务是这些进程的共同咽喉点）。

## 重复装配不做幂等保护

文件体直接调用 `register(...)`，`override` 默认关 ⇒ 若本模块被二次执行（reload、
双装配根），注册表抛 `NodeAlreadyRegistered` 把冲突暴露出来。刻意不加
「已注册即跳过」的 if：静默跳过等于允许两份装配以 import 顺序决定哪份生效——
那是最难定位的一类漂移（`node_registry.register` 的 `override` 注释同口径）。
"""

from __future__ import annotations

import agentix.application.agents.code_handler as code_handler
import agentix.application.agents.file_handler as file_handler
import agentix.application.agents.general_handler as general_handler
import agentix.application.agents.guardrails as guardrails
import agentix.application.agents.image_handler as image_handler
import agentix.application.agents.research_handler as research_handler
import agentix.application.agents.router as router
import agentix.application.agents.task_handler as task_handler
from agentix.orchestration import node_registry

# ``ROUTE_HANDLERS`` 的 8 个键：唯一调用点形态是
# ``await handler(exec_state, config=exec_config)``
# （``lg_supervisor.supervisor_dispatch``），故全部 ``awaits=True, takes_config=True``。
# 名字与键逐字一致；`code_agent` 的 provider 指向 `create_code_query`
# （节点名与 handler 函数名本就不同，沿用改造前的映射）。
node_registry.register(
    "respond_to_general_query",
    lambda: general_handler.respond_to_general_query,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "get_additional_info",
    lambda: guardrails.get_additional_info,
    awaits=True,
    takes_config=True,
)
# `create_research_plan` 的真身自 M3-4 起住 application
# （`agentix/application/agents/research_handler.py`）：KG 面的具体实现不再被编排层 import。
# `lg_builder` 侧只留同名延迟解析壳，键名与壳名逐字相同；`awaits/takes_config` 迁移前后未变。
node_registry.register(
    "create_research_plan",
    lambda: research_handler.create_research_plan,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "create_image_query",
    lambda: image_handler.create_image_query,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "create_file_query",
    lambda: file_handler.create_file_query,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "create_kb_query",
    lambda: general_handler.create_kb_query,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "execute_task",
    lambda: task_handler.execute_task,
    awaits=True,
    takes_config=True,
)
node_registry.register(
    "code_agent",
    lambda: code_handler.create_code_query,
    awaits=True,
    takes_config=True,
)

# 路由判定：调用点为同步 ``route_query(state)``（不 await、不接 config）。
node_registry.register(
    "route_query",
    lambda: router.route_query,
    awaits=False,
    takes_config=False,
)
# 图节点：真实签名为 ``async def analyze_and_route_query(state, *, config)``
# （agentix/application/agents/router.py:434），故 awaits/takes_config 均为 True。
# build_supervisor_graph 建图期经注册表解析后把真身交给 langgraph（节点名与签名
# 仍由 langgraph 按真函数推断，行为逐位不变）。
node_registry.register(
    "analyze_and_route_query",
    lambda: router.analyze_and_route_query,
    awaits=True,
    takes_config=True,
)
