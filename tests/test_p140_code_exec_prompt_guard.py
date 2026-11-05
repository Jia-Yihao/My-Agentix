"""P2（code exec）：flag-off 提示词逐字节守卫（T4）。

钉住 ``docs/architecture/adr/0007-执行沙箱端口.md`` 中「flag-off 等价性承诺」的这一条：
``CODE_EXEC_ENABLED=false`` 时**提示词与基线逐字节相同**。此前仓里没有
用例钉住这条承诺（同款守卫仅 task 段 ``tests/test_p49_task_execution.py:145``）。

被测装配点（实测消费清单）：``CODE_EXEC_ENABLED`` 在 ``agentix/**`` 的消费点中，
唯一产出提示词的是 ``agentix/application/agents/router.py`` 的
``analyze_and_route_query``（行 268–274：flag-on 才把 ``CODE_EXEC_ROUTE_BLOCK``
拼进 ``ROUTER_SYSTEM_PROMPT``）。其余消费点（main.py 注册、lg_builder 节点、
lg_states schema、router.route_query 派发、code_exec_bridge/adapters、
code_handler）都不生产提示词——code_handler 自带的提示词只在 flag-on 可达的
``code_agent`` 节点内使用，不属于 flag-off 装配面。

基线钉法与既有范式的关系（防"同一判定两个源"）：
- task 段守卫（test_p49:138–149）钉的是"装配结果 == 引用的基线常量"，
  判据是「flag-off 不追加任何段」——它对基线常量**自身被改动**是盲的
  （两边同时变）。
- 本卡验收②要求"往提示词模板注入一个字符能让守卫变红"，因此本文件把
  flag-off 基线的**字节快照**以内联常量钉死（仓内提示词字节守卫的既有
  形态即内联字面量，见 ``tests/test_p23_prompt_externalization.py:306``
  "常量拼接必须与原内联 f-string 逐字节一致"）。
- 两个判据互补不重复：本文件同时保留 test_p49 同款的
  ``装配 == ROUTER_SYSTEM_PROMPT`` 断言（装配面纯净性），外加
  ``ROUTER_SYSTEM_PROMPT == 字节快照``（基线冻结）。链条传递 ⇒
  装配 == 快照。全仓对 ``ROUTER_SYSTEM_PROMPT`` 的字节快照**只在本文件
  钉这一份**。
- 模板被合法编辑时本守卫变红是**按设计**：它迫使改模板的人显式更新快照
  并署名，而不是让"逐字节相同"的承诺悄悄失效。更新快照属基线变更
  （约定 5），需要维护者确认。

不依赖网络/模型：沿用 test_p49 的替身 LLM 桩范式。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

import agentix.application.agents.router as router_mod
from agentix.application.agents.lg_prompts import (
    CODE_EXEC_ROUTE_BLOCK,
    ROUTER_SYSTEM_PROMPT,
)
from agentix.config.settings import settings
from agentix.orchestration.langgraph.lg_states import AgentState, Router

# ---------------------------------------------------------------------------
# flag-off 基线字节快照（== 现网 lg_prompts.ROUTER_SYSTEM_PROMPT，UTF-8 3285 字节）
# ---------------------------------------------------------------------------

_PINNED_ROUTER_BASELINE = """你是一名测试数据集领域的智能客服路由器，专注于将用户问题精准分类到合适的处理模块。

你的任务是根据问题内容进行分类，严格从以下类型中选择一个：

## `general-query` - 轻量对话
适用于无需查询任何内部系统即可回答的日常对话。
常见情况：
- 问候、寒暄、情绪反馈（如"你好"、"早上好"、"谢谢"）
- 与测试数据无关的简短对话（如"今天天气真好"）

## `additional-query` - 需要补充信息
当用户提问模糊或缺少关键信息时使用此类。例如：
- 未提供具体数据集、数据分类或条目（如"我想咨询一下测试数据"）
- 问题描述不够明确（如"这个条目对不上怎么办"但缺少数据集/编号等）

## `kb-query` - 知识库查询（内容说明/背景/规则）
当问题涉及数据概念说明、数据来源背景、条目沿革、规则观点、典型案例解读时使用此类。
这类问题需要从知识库检索长文本内容。
典型场景：
- 某个数据概念的说明（如"什么是示例分类 A"）
- 数据来源背景与沿革（如"示例数据集 A 的构建历程"）
- 数据规则与观点（如"条目匹配规则的含义"）

## `graphrag-query` - 图谱查询（数据条目/分类/示例说明）
当问题涉及"具体数据条目内容"、"条目适用关系"、"分类对应条目"、"示例说明"或"条目互引"时必须使用此类。
请在以下情况毫不犹豫地选择 `graphrag-query`：
- 询问某个数据集某条的具体内容（如"示例数据集 A 第 577 条讲了什么"）
- 询问某类问题适用哪些条目（如"示例文档异常适用哪些数据条目"）
- 询问条目之间的关系（如"示例数据集 B 第 232 条引用了哪些条目"）
- 询问示例说明（如"关于异常行为的示例说明有哪些"）
- 需要从数据图谱综合汇总信息

## `text2sql-query` - 数据库统计查询
**重要**: 当问题需要进行数据统计、计数、排名、聚合分析时，必须使用此类！
这类问题需要访问结构化测试数据统计库进行 SQL 查询。
典型场景（请特别注意识别这些关键词）：
- **统计查询**：包含"统计"、"有多少"、"数量"、"总数"、"计算"等关键词
  - 如："测试数据里有多少个数据集"、"统计有多少条示例条目"
- **排名查询**：包含"最多"、"最少"、"排名"、"TOP"、"前几"等关键词
- **数据分析**：包含"平均"、"趋势"、"占比"、"对比"等关键词

> 注意：测试数据统计库（text2sql）本期暂缓启用，此类请求将由系统兜底处理。

## `image-query` - 图片处理
用户提供测试文档、示例文档、数据条目截图等图片或请求图片生成时使用。

## `file-query` - 文件上传
用户上传测试文档、PDF、Excel 等文件资料时使用。

---

**重要提示**：
1. 对于"有多少"、"统计"、"数量"这类问题，务必选择 `text2sql-query`
2. 对于"某条怎么规定"、"适用什么条目"、"示例说明"这类问题，选择 `graphrag-query`
3. 对于"概念说明"、"数据来源背景"、"规则"这类问题，选择 `kb-query`
4. 请准确输出类型字符串，不要输出其他内容或添加额外字段

"""


class _FakeRouterLLM:
    """替身 router LLM：捕获 schema/提示词（与 test_p49 同款桩范式）。"""

    def __init__(self, response) -> None:
        self._response = response
        self.structured_cls = None
        self.seen_messages = None

    def with_structured_output(self, cls):
        self.structured_cls = cls
        return self

    async def ainvoke(self, messages):
        self.seen_messages = messages
        return self._response


def _state(content: str = "你好") -> AgentState:
    return AgentState(messages=[HumanMessage(content=content)])


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


class TestCodeExecFlagOffPromptGuard:
    def test_default_flag_off(self):
        # 业务总闸默认关（code-exec.md §3）；本守卫的"flag-off"前提本身也要被钉住
        assert settings.CODE_EXEC_ENABLED is False

    def test_pinned_baseline_is_frozen_snapshot_of_template(self):
        # 基线冻结：生产模板必须与钉死的字节快照逐字节相同（UTF-8 3285 字节）。
        # 改模板 ⇒ 本用例红 ⇒ 逼改动作显式更新快照（约定 5 的基线变更）。
        assert ROUTER_SYSTEM_PROMPT == _PINNED_ROUTER_BASELINE
        assert (
            ROUTER_SYSTEM_PROMPT.encode("utf-8")
            == _PINNED_ROUTER_BASELINE.encode("utf-8")
        )
        assert len(_PINNED_ROUTER_BASELINE.encode("utf-8")) == 3285

    @pytest.mark.asyncio
    async def test_flag_off_assembled_prompt_is_byte_equal_to_baseline(
        self, monkeypatch
    ):
        # 双 flag 显式关死（不依赖宿主机 .env 的缺省值）
        monkeypatch.setattr(settings, "CODE_EXEC_ENABLED", False)
        monkeypatch.setattr(settings, "TASK_EXEC_ENABLED", False)
        fake = _FakeRouterLLM(response=Router(type="kb-query", logic="fake"))
        monkeypatch.setattr(router_mod, "get_llm_client", lambda **kw: fake)

        await router_mod.analyze_and_route_query(_state("你好"), config={})

        assembled = fake.seen_messages[0]["content"]
        # 逐字节相等：装配结果 ≡ 钉死快照（验收①）
        assert assembled == _PINNED_ROUTER_BASELINE
        assert assembled.encode("utf-8") == _PINNED_ROUTER_BASELINE.encode("utf-8")
        # 与 task 段守卫同款的装配面纯净性断言（同一判定同形态，不另立判据）
        assert assembled == ROUTER_SYSTEM_PROMPT
        # schema 也未换：flag-off 仍是基类 Router
        assert fake.structured_cls is Router

    def test_code_exec_block_never_enters_baseline_prompt(self):
        # 反向断言（test_p49:147-148 同款）：代码段确实存在且只活在段里，
        # 基线快照与生产模板都不含该段点名的意图字面量。
        assert "code-query" in CODE_EXEC_ROUTE_BLOCK
        assert "code-query" not in _PINNED_ROUTER_BASELINE
        assert "code-query" not in ROUTER_SYSTEM_PROMPT
