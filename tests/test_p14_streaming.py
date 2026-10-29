"""P1-4 验收测试：伪流式 → LangGraph 原生真流式 + 节点过滤（mock graph，无需 LLM/Docker）。

方案验收标准：
1. ``stream_events`` 改用 ``astream_events(version="v2")``，按事件类型映射业务事件
2. ``on_chat_model_stream`` 逐 token 下发 ``message``（首字延迟 = 首个 token）
3. **节点级过滤**：只转发「回答类」节点的 token，内部编排节点（路由/
   guardrails/planner/tool_selection/summarize/router）的碎片不得外泄
4. 整段兜底：一个 token 都没转发时，从最外层图输出 ``messages[-1]`` 整段补发
5. 空 content 不发 ``message``；``done`` 的 sources 从图输出提取
6. 异常不抛给调用方（产出 ``error``）；无结束事件时兜底补发 ``done``
7. 首帧 ``metadata{"status":"processing"}`` 保持（前端兼容）

本文件全部使用 mock graph（提供 ``astream_events`` 异步生成器）或离线
FakeChatModel，不依赖本地 LLM 服务 / Docker。
"""

import asyncio
import sys
from typing import Any, Dict, List, Optional, TypedDict

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agentix.application.services.chat_service import ChatService


# ---------------------------------------------------------------------------
# Mock 图：提供 astream_events 异步生成器（可注入事件序列 / 中途异常）
# ---------------------------------------------------------------------------


class _FakeChunk:
    """最小消息块替身（仅需 ``content``）。"""

    def __init__(self, content: Any) -> None:
        self.content = content


class FakeStreamGraph:
    """按顺序回放预设事件；可选在消费 N 个事件后抛出异常。"""

    def __init__(
        self,
        events: List[Dict[str, Any]],
        exc: Optional[BaseException] = None,
        raise_after: Optional[int] = None,
    ) -> None:
        self._events = events
        self._exc = exc
        # raise_after=None 表示事件回放完后抛出（若 exc 非空）
        self._raise_after = raise_after
        self.captured: Dict[str, Any] = {}

    async def astream_events(self, input_state, config=None, version=None):
        self.captured = {
            "input_state": input_state,
            "config": config,
            "version": version,
        }
        for idx, event in enumerate(self._events):
            yield event
            if self._exc is not None and self._raise_after == idx:
                raise self._exc
        if self._exc is not None and self._raise_after is None:
            raise self._exc


def _ev(
    kind: str,
    name: Optional[str] = None,
    data: Optional[Dict] = None,
    metadata: Optional[Dict] = None,
) -> Dict:
    return {"event": kind, "name": name, "data": data or {}, "metadata": metadata or {}}


# 回答类节点（默认 chunk 归属）——对应 ChatService._STREAM_FORWARD_NODES
ANSWER_NODE = "create_kb_query"

# 内部编排类节点：实测会被 astream_events 以 on_chat_model_stream 抛出，
# 但绝不该转发给前端
INTERNAL_NODES = [
    "analyze_and_route_query",
    "guardrails",
    "router",
    "planner",
    "tool_selection",
    "error_tool_selection",
    "summarize",
    "cypher_query",
    "predefined_cypher",
    "customer_tools",
    "text2sql_query",
    "local_search",
    "external_search",
]


def _chunk_event(content: Any, node: Optional[str] = ANSWER_NODE) -> Dict:
    """构造 on_chat_model_stream 事件；node=None 表示 metadata 缺失 langgraph_node。"""
    metadata = {} if node is None else {"langgraph_node": node}
    return _ev("on_chat_model_stream", name="ChatModel", data={"chunk": _FakeChunk(content)}, metadata=metadata)


def _langgraph_end(
    sources: Any = None,
    messages: Optional[List[Any]] = None,
    node: Optional[str] = None,
) -> Dict:
    """构造 on_chain_end(name=="LangGraph")；node=None 表示最外层图结束。"""
    output: Dict[str, Any] = {}
    if sources is not None:
        output["sources"] = sources
    if messages is not None:
        output["messages"] = messages
    metadata = {} if node is None else {"langgraph_node": node}
    return _ev("on_chain_end", name="LangGraph", data={"output": output}, metadata=metadata)


# 完整事件序列：路由 → 工具开始 → 2 个 token → 工具结束 → 图结束
FULL_SEQUENCE = [
    _ev("on_chain_start", name="LangGraph"),
    _ev(
        "on_chain_end",
        name="analyze_and_route_query",
        data={"output": {"router": {"type": "legal", "logic": "kb-query"}}},
        metadata={"langgraph_node": "analyze_and_route_query"},
    ),
    _ev("on_tool_start", name="milvus_search"),
    _chunk_event("你"),
    _chunk_event("好"),
    _ev("on_tool_end", name="milvus_search"),
    _langgraph_end(sources=[{"document_id": "d1", "source": "d1"}]),
]


def _collect(graph: FakeStreamGraph, message: str = "你好", session_id: str = "s1") -> List[Dict]:
    """驱动 stream_events 收集全部业务事件（asyncio.run 风格）。"""

    async def _run() -> List[Dict]:
        service = ChatService(graph_getter=lambda: graph)
        out: List[Dict] = []
        async for event in service.stream_events(message, session_id):
            out.append(event)
        return out

    return asyncio.run(_run())


def _types(events: List[Dict]) -> List[str]:
    return [e["type"] for e in events]


def _messages(events: List[Dict]) -> List[str]:
    return [e["content"] for e in events if e["type"] == "message"]


# ---------------------------------------------------------------------------
# 1. 事件序列 + 真 token 流
# ---------------------------------------------------------------------------


class TestStreamSequence:
    def test_full_event_sequence(self):
        events = _collect(FakeStreamGraph(FULL_SEQUENCE))
        assert _types(events) == [
            "metadata",  # 首帧 processing
            "metadata",  # 路由帧
            "tool_start",
            "message",
            "message",
            "tool_end",
            "done",
        ]

    def test_first_frame_is_processing_metadata(self):
        """前端兼容锚点：首帧必须是 metadata{"status":"processing"}。"""
        events = _collect(FakeStreamGraph(FULL_SEQUENCE))
        assert events[0]["type"] == "metadata"
        assert events[0]["metadata"] == {"status": "processing"}
        assert events[0]["session_id"] == "s1"

    def test_token_stream_is_per_token(self):
        """真流式：message 事件数 == 非空 token 数（逐 token，而非整段拆词）。"""
        tokens = ["依", "据", "民", "法", "典"]
        graph = FakeStreamGraph(
            [_chunk_event(t) for t in tokens] + [_langgraph_end(sources=[])]
        )
        events = _collect(graph)
        messages = [e for e in events if e["type"] == "message"]
        assert len(messages) == len(tokens)
        assert [m["content"] for m in messages] == tokens
        # 断言未做 "整段拆词 + sleep" 伪流式（内容即 token，无追加空格）
        assert all(not m["content"].endswith(" ") for m in messages)

    def test_uses_astream_events_v2(self):
        graph = FakeStreamGraph(FULL_SEQUENCE)
        _collect(graph)
        assert graph.captured["version"] == "v2"
        assert graph.captured["config"]["configurable"]["thread_id"] == "s1"


# ---------------------------------------------------------------------------
# 2. 空 content 过滤
# ---------------------------------------------------------------------------


class TestEmptyContentFiltered:
    def test_empty_string_not_emitted(self):
        graph = FakeStreamGraph(
            [
                _chunk_event(""),
                _chunk_event("有效"),
                _chunk_event(""),
                _langgraph_end(sources=[]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["有效"]

    def test_none_chunk_not_emitted(self):
        graph = FakeStreamGraph(
            [
                _ev("on_chat_model_stream", name="ChatModel", data={"chunk": None}, metadata={"langgraph_node": ANSWER_NODE}),
                _chunk_event("A"),
                _langgraph_end(sources=[]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["A"]

    def test_list_content_parts_concatenated(self):
        """多模态 content part 列表：仅拼接文本部分。"""
        graph = FakeStreamGraph(
            [
                _chunk_event([{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]),
                _langgraph_end(sources=[]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["甲乙"]


# ---------------------------------------------------------------------------
# 3. 节点级过滤（P1-4b：防内部编排 token 外泄）
# ---------------------------------------------------------------------------


class TestNodeFiltering:
    @pytest.mark.parametrize(
        "node",
        [
            "respond_to_general_query",
            "get_additional_info",
            "create_kb_query",
            "create_research_plan",
            "create_image_query",
            "create_file_query",
            "final_answer",
            "finalize",
        ],
    )
    def test_answer_nodes_are_forwarded(self, node):
        graph = FakeStreamGraph([_chunk_event("t", node=node), _langgraph_end(sources=[])])
        events = _collect(graph)
        assert _messages(events) == ["t"]

    @pytest.mark.parametrize("node", INTERNAL_NODES)
    def test_internal_nodes_are_not_forwarded(self, node):
        """内部编排节点 token 不得外泄（交由整段兜底保证不空）。"""
        graph = FakeStreamGraph(
            [
                _chunk_event("内部碎片", node=node),
                _langgraph_end(sources=[], messages=[AIMessage(content="最终整段回答")]),
            ]
        )
        events = _collect(graph)
        # 内部 token 被丢弃，只有整段兜底的那条
        assert _messages(events) == ["最终整段回答"]

    def test_missing_langgraph_node_not_forwarded(self):
        """metadata 缺失 langgraph_node → 不转发（策略），空内容由兜底兜住。"""
        graph = FakeStreamGraph(
            [
                _chunk_event("无归属token", node=None),
                _langgraph_end(sources=[], messages=[AIMessage(content="整段")]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["整段"]

    def test_missing_langgraph_node_yields_nothing_without_fallback(self):
        """缺失节点名且无兜底素材 → 不产出任何 message（证明确实未转发）。"""
        graph = FakeStreamGraph([_chunk_event("x", node=None), _langgraph_end(sources=[])])
        events = _collect(graph)
        assert _messages(events) == []

    def test_namespaced_answer_node_forwarded_by_suffix(self):
        """防御性后缀匹配：带命名空间前缀的回答类节点仍转发。"""
        graph = FakeStreamGraph(
            [_chunk_event("t", node="kb_workflow:finalize"), _langgraph_end(sources=[])]
        )
        events = _collect(graph)
        assert _messages(events) == ["t"]

    def test_namespaced_internal_node_not_forwarded(self):
        graph = FakeStreamGraph(
            [
                _chunk_event("内部", node="kb_workflow:router"),
                _langgraph_end(sources=[], messages=[AIMessage(content="整段")]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["整段"]

    def test_mixed_stream_keeps_only_answer_tokens(self):
        """一条流里混有内部节点与回答节点 → 只保留回答节点的 token，且顺序不变。"""
        graph = FakeStreamGraph(
            [
                _chunk_event("路由JSON", node="analyze_and_route_query"),
                _chunk_event("选工具", node="tool_selection"),
                _chunk_event("答", node="create_kb_query"),
                _chunk_event("案", node="create_kb_query"),
                _chunk_event("总结", node="summarize"),
                _langgraph_end(sources=[]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["答", "案"]


# ---------------------------------------------------------------------------
# 4. 整段兜底
# ---------------------------------------------------------------------------


class TestWholeAnswerFallback:
    def test_fallback_emits_whole_message_before_done(self):
        """全部 token 被过滤 + 最外层输出带 messages → 一条整段 message + done。"""
        graph = FakeStreamGraph(
            [
                _chunk_event("内部1", node="router"),
                _chunk_event("内部2", node="planner"),
                _langgraph_end(
                    sources=[{"document_id": "d1"}],
                    messages=[AIMessage(content="整段最终回答")],
                ),
            ]
        )
        events = _collect(graph)
        assert _types(events)[-2:] == ["message", "done"]
        assert _messages(events) == ["整段最终回答"]
        assert events[-1]["metadata"]["sources"] == [{"document_id": "d1"}]

    def test_fallback_not_duplicated_when_tokens_emitted(self):
        """有正常 token 时不得再补整段（否则答案会重复两遍）。"""
        graph = FakeStreamGraph(
            [
                _chunk_event("真", node="create_kb_query"),
                _chunk_event("流", node="create_kb_query"),
                _langgraph_end(sources=[], messages=[AIMessage(content="整段最终回答")]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["真", "流"]

    def test_fallback_uses_dict_message_shape(self):
        """图输出的 messages 元素是 dict 形态时也能取到整段文本。"""
        graph = FakeStreamGraph(
            [
                _langgraph_end(sources=[], messages=[{"content": "dict 整段"}]),
            ]
        )
        events = _collect(graph)
        assert _messages(events) == ["dict 整段"]

    def test_no_fallback_when_output_has_no_messages(self):
        graph = FakeStreamGraph([_langgraph_end(sources=[])])
        events = _collect(graph)
        assert _messages(events) == []
        assert events[-1]["type"] == "done"


# ---------------------------------------------------------------------------
# 5. sources 提取 + 路由帧
# ---------------------------------------------------------------------------


class TestDoneAndSources:
    def test_done_extracts_sources_from_langgraph_output(self):
        sources = [{"document_id": "d1", "source": "民法典"}]
        events = _collect(FakeStreamGraph([_chunk_event("x"), _langgraph_end(sources=sources)]))
        done = events[-1]
        assert done["type"] == "done"
        assert done["metadata"]["sources"] == sources

    def test_done_sources_empty_when_absent(self):
        events = _collect(FakeStreamGraph([_chunk_event("x"), _langgraph_end(sources=None)]))
        assert events[-1]["type"] == "done"
        assert events[-1]["metadata"]["sources"] == []

    def test_string_sources_passed_through(self):
        events = _collect(
            FakeStreamGraph([_chunk_event("x"), _langgraph_end(sources=["doc-a", "doc-b"])])
        )
        assert events[-1]["metadata"]["sources"] == ["doc-a", "doc-b"]

    def test_route_metadata_emitted_from_router_node(self):
        events = _collect(FakeStreamGraph(FULL_SEQUENCE))
        route_frames = [
            e
            for e in events
            if e["type"] == "metadata" and "route" in (e.get("metadata") or {})
        ]
        assert len(route_frames) == 1
        assert route_frames[0]["metadata"] == {"route": "legal", "logic": "kb-query"}
        assert route_frames[0]["route"] == "legal"

    def test_no_route_frame_when_router_unavailable(self):
        """拿不到路由信息 → 不发路由帧（不硬造），但 processing 首帧仍在。"""
        events = _collect(FakeStreamGraph([_chunk_event("x"), _langgraph_end(sources=[])]))
        route_frames = [
            e
            for e in events
            if e["type"] == "metadata" and "route" in (e.get("metadata") or {})
        ]
        assert route_frames == []
        assert events[0]["metadata"] == {"status": "processing"}


# ---------------------------------------------------------------------------
# 6. 嵌套子图的 done 不应提前触发
# ---------------------------------------------------------------------------


class TestNestedSubgraph:
    def test_inner_subgraph_langgraph_end_does_not_emit_done(self):
        """子图自身的 LangGraph on_chain_end（langgraph_node=宿主节点）不得发 done。"""
        events = _collect(
            FakeStreamGraph(
                [
                    _chunk_event("一"),
                    # 子图结束：name=="LangGraph" 但 langgraph_node 非 None
                    _ev("on_chain_end", name="LangGraph", data={"output": {}}, metadata={"langgraph_node": "kb_workflow"}),
                    _chunk_event("二"),
                    _langgraph_end(sources=[]),
                ]
            )
        )
        assert _types(events).count("done") == 1
        assert events[-1]["type"] == "done"
        assert _messages(events) == ["一", "二"]


# ---------------------------------------------------------------------------
# 7. 异常路径 + 兜底 done
# ---------------------------------------------------------------------------


class TestErrorAndFallback:
    def test_exception_yields_error_without_raising(self):
        """异常不抛给调用方，产出 error 事件。"""
        graph = FakeStreamGraph(
            [_chunk_event("部分")],
            exc=RuntimeError("graph boom"),
            raise_after=0,
        )
        events = _collect(graph)  # 不应抛出
        assert "error" in _types(events)
        err = next(e for e in events if e["type"] == "error")
        assert err["content"] == "处理请求时出错，请稍后重试。"
        assert err["session_id"] == "s1"

    def test_exception_still_emits_trailing_done(self):
        """异常路径也不允许调用方挂住：error 之后补发 done。"""
        graph = FakeStreamGraph([], exc=RuntimeError("boom"), raise_after=None)
        events = _collect(graph)
        assert _types(events) == ["metadata", "error", "done"]
        assert events[-1]["metadata"]["sources"] == []

    def test_fallback_done_when_no_graph_end_event(self):
        """事件流里没有最外层 LangGraph 结束事件 → 生成器结束前补发一次 done。"""
        graph = FakeStreamGraph([_chunk_event("a"), _chunk_event("b")])
        events = _collect(graph)
        assert events[-1]["type"] == "done"
        assert _types(events).count("done") == 1

    def test_done_emitted_exactly_once(self):
        events = _collect(FakeStreamGraph(FULL_SEQUENCE))
        assert _types(events).count("done") == 1

    def test_unexpected_chain_end_name_does_not_break_stream(self):
        """name 与预期不符（无匹配结束事件）→ 兜底 done，不抛异常。"""
        graph = FakeStreamGraph(
            [
                _ev("on_chain_end", name="some_other_chain", data={"output": {}}),
                _chunk_event("x"),
            ]
        )
        events = _collect(graph)
        assert _types(events)[-1] == "done"
        assert "error" not in _types(events)


# ---------------------------------------------------------------------------
# 8. 实测锁定：langgraph_node 的真实取值形态（第一手依据）
# ---------------------------------------------------------------------------


class _ProbeState(TypedDict, total=False):
    """最简图状态（探针用；必须是有效 schema，普通 dict 子类会被 langgraph 拒绝）。"""

    x: int


class TestLanggraphNodeMetadataShape:
    """用真 StateGraph 锁定实测结论（不联网、不起服务）：

    - 子图节点的事件里 ``langgraph_node`` 是**裸节点名**（``router``/``finalize``），
      不带 ``<subgraph_node>:`` 命名空间前缀；
    - 最外层 ``on_chain_end(name=="LangGraph")`` 的 ``langgraph_node`` 为 ``None``，
      子图自身的同名事件其 ``langgraph_node`` 为宿主节点名（``kb_workflow``）。

    这一形态决定了过滤规则可用「取最后一段冒号分隔名」做集合匹配，且 ``done``
    必须以 ``langgraph_node is None`` 识别最外层。
    """

    @staticmethod
    def _llm_node(text: str):
        llm = GenericFakeChatModel(messages=iter([AIMessage(content=text)]))

        async def node(state, *, config: RunnableConfig):
            # 实测：必须显式转发 config，chat model 事件才会冒泡到 astream_events
            async for _ in llm.astream([("human", "hi")], config=config):
                pass
            return {}

        return node

    @classmethod
    def _build(cls):
        sub = StateGraph(_ProbeState)
        sub.add_node("router", cls._llm_node("内部路由"))
        sub.add_node("finalize", cls._llm_node("子图回答"))
        sub.add_edge(START, "router")
        sub.add_edge("router", "finalize")
        sub.add_edge("finalize", END)

        main = StateGraph(_ProbeState)
        main.add_node("create_research_plan", cls._llm_node("主图回答"))
        main.add_node("kb_workflow", sub.compile())
        main.add_edge(START, "create_research_plan")
        main.add_edge("create_research_plan", "kb_workflow")
        main.add_edge("kb_workflow", END)
        return main.compile()

    @staticmethod
    def _run():
        async def _collect():
            rows = []
            async for ev in TestLanggraphNodeMetadataShape._build().astream_events(
                {"x": 1}, version="v2"
            ):
                md = ev.get("metadata") or {}
                rows.append(
                    {
                        "event": ev["event"],
                        "name": ev.get("name"),
                        "node": md.get("langgraph_node"),
                        "ns": md.get("langgraph_checkpoint_ns"),
                    }
                )
            return rows

        return asyncio.run(_collect())

    def test_subgraph_llm_stream_node_names_are_bare(self):
        rows = self._run()
        stream_nodes = [
            r["node"] for r in rows if r["event"] == "on_chat_model_stream"
        ]
        assert stream_nodes == ["create_research_plan", "router", "finalize"]
        # 裸名：不含命名空间分隔符
        assert all(":" not in str(n) and "|" not in str(n) for n in stream_nodes)

    def test_inner_subgraph_langgraph_end_node_is_host_node(self):
        rows = self._run()
        langgraph_ends = [
            r for r in rows if r["event"] == "on_chain_end" and r["name"] == "LangGraph"
        ]
        # 内层（子图）+ 外层（主图）各一次
        assert len(langgraph_ends) == 2
        nodes = {str(r["node"]) for r in langgraph_ends}
        assert nodes == {"kb_workflow", "None"}  # 内层=宿主节点名；外层=None

    def test_whitelist_agrees_with_measured_node_names(self):
        """实测节点名落在过滤策略的预期分支上。"""
        assert ChatService._is_answer_node("finalize") is True
        assert ChatService._is_answer_node("create_research_plan") is True
        assert ChatService._is_answer_node("router") is False
        assert ChatService._is_answer_node("kb_workflow:router") is False
        assert ChatService._is_answer_node(None) is False


# ---------------------------------------------------------------------------
# 9. 端到端：config 转发是真流式的必要条件（真两层图 + 离线流式 LLM）
# ---------------------------------------------------------------------------


class CharStreamFakeChatModel(BaseChatModel):
    """离线「逐字符」流式 LLM：每个字符一个 chunk，便于精确计数 token 事件。

    用 ``GenericFakeChatModel`` 做不到干净的逐字断言（它按空白切分，会混入
    空格 chunk），故自定义最小实现，不依赖任何本地模型服务。
    """

    text: str = "民法典"

    @property
    def _llm_type(self) -> str:
        return "char-stream-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.text))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for ch in self.text:
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=ch))
            if run_manager:
                run_manager.on_llm_new_token(ch, chunk=chunk)
            yield chunk


class _E2EState(TypedDict, total=False):
    """端到端测试图状态（主图与子图共用同一 schema）。"""

    messages: List[Any]
    router: Dict[str, Any]
    sources: List[Any]
    question: str
    answer: str
    guardrails_decision: str


def _build_e2e_graph(
    *,
    forward_to_subgraph: bool = True,
    forward_in_finalize: bool = True,
):
    """构造与生产同形的两层图：

    ``main(analyze_and_route_query → create_kb_query)``
    ``└── sub(guardrails → finalize)``

    三个开关分别模拟生产链路中的三处 config 转发点：
    - ``forward_to_subgraph``：主图节点把 config 传给子图 ``ainvoke``
      ⇒ 对应 ``general_handler.create_kb_query`` 的 ``workflow.ainvoke(..., config=config)``
    - ``forward_in_finalize``：子图回答节点把 config 传给 ``llm.ainvoke``
      ⇒ 对应 ``kb_nodes/finalize_node.py`` 的 ``llm.ainvoke(messages, config=config)``
    - 子图 ``guardrails`` 节点**始终不转发**（内部编排，模拟真实行为）
    """
    answer_llm = CharStreamFakeChatModel(text="民法典")
    internal_llm = CharStreamFakeChatModel(text="内部判定")

    async def finalize(state, *, config: RunnableConfig):
        if forward_in_finalize:
            async for _ in answer_llm.astream([("human", "q")], config=config):
                pass
        else:
            async for _ in answer_llm.astream([("human", "q")]):
                pass
        return {"answer": answer_llm.text}

    async def guardrails(state):
        # 内部编排：故意不接收/不转发 config（真实 KB 子图 guardrails 节点即如此）
        async for _ in internal_llm.astream([("human", "q")]):
            pass
        return {"guardrails_decision": "proceed"}

    sub = StateGraph(_E2EState)
    sub.add_node("guardrails", guardrails)
    sub.add_node("finalize", finalize)
    sub.add_edge(START, "guardrails")
    sub.add_edge("guardrails", "finalize")
    sub.add_edge("finalize", END)
    kb_workflow = sub.compile()

    async def analyze_and_route_query(state):
        return {"router": {"type": "legal", "logic": "kb-query"}}

    async def create_kb_query(state, *, config: RunnableConfig):
        last = state["messages"][-1]
        question = last["content"] if isinstance(last, dict) else getattr(last, "content", "")
        if forward_to_subgraph:
            out = await kb_workflow.ainvoke({"question": question}, config=config)
        else:
            out = await kb_workflow.ainvoke({"question": question})
        return {"messages": [AIMessage(content=out.get("answer", ""))]}

    main = StateGraph(_E2EState)
    main.add_node("analyze_and_route_query", analyze_and_route_query)
    main.add_node("create_kb_query", create_kb_query)
    main.add_edge(START, "analyze_and_route_query")
    main.add_edge("analyze_and_route_query", "create_kb_query")
    main.add_edge("create_kb_query", END)
    return main.compile()


class TestEndToEndConfigForwarding:
    """P1-4 核心结论的端到端证明：**不转发 config ⇒ 真流式失效**。

    实测依据（本文件 8 节）：节点声明了 config 但不转发给 ``ainvoke``，
    ``on_chat_model_stream`` 事件数为 0——即便 LangChain 有 ContextVar 兜底
    也不生效。因此 config 转发是「降低首字延迟」这一验收目标的前提。
    """

    def test_real_graph_streams_answer_tokens_only(self):
        graph = _build_e2e_graph()
        events = _collect(graph)
        # 只含子图 finalize（白名单内）的逐字 token；guardrails 内部 token 不出现
        assert _messages(events) == ["民", "法", "典"]
        assert "内" not in _messages(events)
        assert _types(events).count("done") == 1
        assert events[-1]["type"] == "done"

    def test_real_graph_emits_route_frame(self):
        graph = _build_e2e_graph()
        events = _collect(graph)
        route_frames = [
            e for e in events if e["type"] == "metadata" and "route" in (e.get("metadata") or {})
        ]
        assert len(route_frames) == 1
        assert route_frames[0]["metadata"]["route"] == "legal"

    def test_no_config_forward_in_finalize_falls_back_to_whole_text(self):
        """反向对照：回答节点不转发 config。

        py3.10（现役基线）：0 个 token 事件 → 整段兜底；
        py3.12+：langchain-core 经 contextvar 隐式传播 config → 逐 token 流式
        （库行为随解释器变化，特征化按版本跟随；生产节点仍须显式转发 config，
        见 test_production_nodes_forward_config 源码守卫）。
        """
        graph = _build_e2e_graph(forward_in_finalize=False)
        events = _collect(graph)
        if sys.version_info >= (3, 12):
            assert _messages(events) == ["民", "法", "典"]
        else:
            # 不是逐字流，而是最外层输出 messages[-1] 的整段
            assert _messages(events) == ["民法典"]
        assert _types(events).count("done") == 1

    def test_no_config_forward_to_subgraph_falls_back_to_whole_text(self):
        """反向对照：主图节点不把 config 透传进子图。

        版本差异同上（py3.12+ contextvar 隐式传播 → 逐 token）。
        """
        graph = _build_e2e_graph(forward_to_subgraph=False)
        events = _collect(graph)
        if sys.version_info >= (3, 12):
            assert _messages(events) == ["民", "法", "典"]
        else:
            assert _messages(events) == ["民法典"]
        assert _types(events).count("done") == 1

    def test_production_nodes_forward_config(self):
        """源码级守卫：关键转发点必须存在（防止回归时被误删）。"""
        import inspect

        from agentix.application.agents import (
            general_handler,
            guardrails,
            image_handler,
            research_handler,
        )
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            finalize_node,
        )

        # 面向用户的回答类调用点
        assert "config=config" in inspect.getsource(general_handler.respond_to_general_query)
        assert "config=config" in inspect.getsource(general_handler.create_kb_query)
        assert "config=config" in inspect.getsource(guardrails.get_additional_info)
        assert "config=config" in inspect.getsource(image_handler.create_image_query)
        assert "config=config" in inspect.getsource(research_handler.create_research_plan)
        assert "config=config" in inspect.getsource(finalize_node.create_finalize_node)

    def test_internal_calls_do_not_forward_config(self):
        """源码级守卫：同节点内的**内部编排**调用点不得转发 config。

        白名单按「节点」过滤，无法区分节点内的不同调用点——若给内部判定链
        传入 config，其 token 会以该节点名冒泡，被前端当作答案流。
        """
        import inspect

        from agentix.application.agents import guardrails, image_handler
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent.kb_nodes import (
            finalize_node,
        )

        # guardrails 判定链（结构化输出）不得转发
        src = inspect.getsource(guardrails.get_additional_info)
        chain_line = next(
            line for line in src.splitlines() if "guardrails_chain.ainvoke(" in line
        )
        assert "config=config" not in chain_line

        # 图片生成前的「提示词增强」不得转发（同节点 create_image_query 内）
        gen_src = inspect.getsource(image_handler._generate_image)
        assert "config" not in gen_src.split("enhanced_response = ")[1].split("\n")[0]

        # finalize 内部：structured/内部 chain 之外仍只有一处转发（本节点即回答节点）
        assert inspect.getsource(finalize_node.create_finalize_node).count("config=config") == 1


# ---------------------------------------------------------------------------
# 批次 B1（audit-2026-09-28 §2 B1）：流式 idle 看门狗 STREAM_IDLE_TIMEOUT
# ---------------------------------------------------------------------------


class HangingStreamGraph:
    """回放 prefix 事件后**静默挂起**（模拟非 LLM 节点卡死 / 网络黑洞）。"""

    def __init__(self, prefix=None) -> None:
        self._prefix = list(prefix or [])

    async def astream_events(self, input_state, config=None, version=None):
        for event in self._prefix:
            yield event
        await asyncio.sleep(3600)
        yield {}  # 不可达：仅满足生成器形状


class TestStreamIdleWatchdog:
    """STREAM_IDLE_TIMEOUT：__anext__ 静默挂起 ⇒ error 帧 + done 恰好一次 + 槽位归还。"""

    def test_silent_hang_before_first_event_yields_error_and_done(self, monkeypatch):
        from agentix.config import settings as app_settings

        monkeypatch.setattr(app_settings, "STREAM_IDLE_TIMEOUT", 0.05)
        events = _collect(HangingStreamGraph())
        kinds = _types(events)

        assert kinds == ["metadata", "error", "done"], "静默挂起必须经 error 收口"
        assert events[1]["content"] == "处理请求超时，请稍后重试。"
        assert kinds.count("done") == 1, "恰好一次 done（兜底契约不破）"

    def test_mid_stream_hang_keeps_tokens_then_error(self, monkeypatch):
        from agentix.config import settings as app_settings

        monkeypatch.setattr(app_settings, "STREAM_IDLE_TIMEOUT", 0.05)
        graph = HangingStreamGraph(prefix=[_chunk_event("你")])
        events = _collect(graph)
        kinds = _types(events)

        assert kinds == ["metadata", "message", "error", "done"], (
            "挂起前已下发的 token 保留；其后 error + 恰好一次 done"
        )

    def test_idle_timeout_releases_concurrency_slot(self, monkeypatch):
        """槽位归还：看门狗触发后 _AGENT_SEM 恢复挂起前的可用容量。"""
        from agentix.application.services import chat_service as chat_service_module
        from agentix.config import settings as app_settings

        monkeypatch.setattr(app_settings, "STREAM_IDLE_TIMEOUT", 0.05)
        before = chat_service_module._AGENT_SEM._value

        events = _collect(HangingStreamGraph())

        assert any(e["type"] == "error" for e in events)
        assert chat_service_module._AGENT_SEM._value == before, (
            "静默挂起不得永久占用并发槽位"
        )

    def test_fast_stream_not_interrupted_by_watchdog(self, monkeypatch):
        """事件按时到达时看门狗零干扰（既有全序列照常）。"""
        from agentix.config import settings as app_settings

        monkeypatch.setattr(app_settings, "STREAM_IDLE_TIMEOUT", 5.0)
        events = _collect(FakeStreamGraph(FULL_SEQUENCE))
        kinds = _types(events)

        assert "error" not in kinds
        assert kinds.count("done") == 1



def test_frames_single_vocabulary_type_only():
    """A5/R5b（SSE 词汇归一）：chat_service 全部事件帧以 type 为唯一词汇，不带 event。"""
    events = _collect(FakeStreamGraph(FULL_SEQUENCE))
    for e in events:
        assert "type" in e, f"帧缺 type: {e}"
        assert "event" not in e, f"A5 归一后 event 词必须移除: {e}"

    # 异常路径的 error/done 兜底帧同样只带 type
    graph = FakeStreamGraph([_chunk_event("你")], exc=RuntimeError("boom"))
    error_events = _collect(graph)
    for e in error_events:
        assert "event" not in e, f"异常路径仍带 event 词: {e}"
    assert any(e["type"] == "error" for e in error_events)
