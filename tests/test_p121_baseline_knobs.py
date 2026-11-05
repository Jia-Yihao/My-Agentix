"""录制 evals 基线时补的两处接线旋钮（§24.3 T2 附注，2026-10-25）。

覆盖面与验收口径：

1. ``TEXT2SQL_DATABASE_URL`` / ``text2sql_database_url()``——Text2SQL 目标库的
   **单源**：留空逐位回退 ``DATABASE_URL``（约定 1），显式配置时 schema 拉取、
   SQL 执行、``EXPLAIN`` 试跑、schema 缓存键**四处同源**；并守住"四处不再各自
   读 ``settings.DATABASE_URL``"这条静态边（防止日后又冒出一个第五处直读）。
2. ``KB_SCOPE_DESCRIPTION``——KB 子图 guardrails 的领域范围说明：空 →
   ``scope_description=None``（子图内置测试数据集文案，逐位等价）；非空 → 原样透传。
   以及子图侧 ``scope_text`` 的选择（None ⇒ 内置文案；给了 ⇒ 用给的）。

不测的东西：LLM 实际拒答/放行的语义（那是 guardrails 提示词的运行时行为，
需要真实端点，属离线录制口径——见 README §15.2 与 docs/项目实现手册.md §24）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agentix.config import settings

# 导入顺序垫片（**不是**装饰用）：``components.text2sql/__init__`` 与
# ``application.agents.text2sql/__init__`` 互为上下游，直接从深层子模块
# （``...text2sql.target_db``）进入会撞进半初始化的包并抛
# ``cannot import name 'create_answer_formatter_node'``；先从任一个"外层"包
# 进入即可正常收敛。生产链路（ChatService/graph）天然走外层。
import agentix.application.agents.text2sql as _outer_text2sql  # noqa: E402,F401

TARGET_DB = (
    "agentix.application.agents.kg_sub_graph.agentic_rag_agents."
    "components.text2sql.target_db"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT2SQL_DIR = (
    REPO_ROOT
    / "agentix"
    / "application"
    / "agents"
    / "kg_sub_graph"
    / "agentic_rag_agents"
    / "components"
    / "text2sql"
)

# ---------------------------------------------------------------------------
# 1) Text2SQL 目标库单源
# ---------------------------------------------------------------------------


def _load_target_db():
    """按"外层包先进来"的顺序取到 target_db 模块。"""
    import importlib

    return importlib.import_module(TARGET_DB)


class TestText2SQLTargetDb:
    def test_empty_falls_back_to_database_url(self, monkeypatch):
        """默认（空串）= 回退应用自身 DATABASE_URL = 接线前逐位等价。"""
        text2sql_database_url = _load_target_db().text2sql_database_url

        monkeypatch.setattr(settings, "TEXT2SQL_DATABASE_URL", "")
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///data/demo/app.db")
        assert text2sql_database_url() == "sqlite+pysqlite:///data/demo/app.db"

    def test_whitespace_only_also_falls_back(self, monkeypatch):
        """只填了空白 = 没填（不能被当成一个连不上的库地址）。"""
        text2sql_database_url = _load_target_db().text2sql_database_url

        monkeypatch.setattr(settings, "TEXT2SQL_DATABASE_URL", "   ")
        monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@h:5432/app")
        assert text2sql_database_url() == "postgresql+psycopg://u:p@h:5432/app"

    def test_explicit_target_wins(self, monkeypatch):
        text2sql_database_url = _load_target_db().text2sql_database_url

        monkeypatch.setattr(
            settings,
            "TEXT2SQL_DATABASE_URL",
            "mysql+pymysql://legal_user:legalpass@127.0.0.1:13306/legal_db",
        )
        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+pysqlite:///data/demo/app.db")
        assert text2sql_database_url().startswith("mysql+pymysql://")

    def test_default_setting_is_empty(self):
        """新键默认必须是空（约定 1：不配即与基线等价）。"""
        from agentix.config.settings import Settings

        fields = Settings.model_fields
        assert "TEXT2SQL_DATABASE_URL" in fields
        assert fields["TEXT2SQL_DATABASE_URL"].default == ""

    def test_no_site_reads_settings_database_url_directly(self):
        """四个消费点必须走单源，不许再出现第五处直读 ``settings.DATABASE_URL``。

        分叉的后果不是难看，是**schema 从 A 库拉、SQL 在 B 库跑**：报错会指向
        一张在目标库里根本不存在的表，排查方向被彻底带偏。
        """
        offenders: list[str] = []
        for path in TEXT2SQL_DIR.rglob("*.py"):
            if path.name == "target_db.py":
                continue  # 单源自己读 DATABASE_URL 是它的职责
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "DATABASE_URL" not in line:
                    continue
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                if "settings.DATABASE_URL" in stripped or 'getattr(settings, "DATABASE_URL"' in stripped:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, "Text2SQL 目标库应统一经 target_db 单源：\n  " + "\n  ".join(offenders)

    def test_all_four_sites_import_the_single_source(self):
        """反向防空真：上面那条如果因为文件改名而扫不到东西，这里会红。"""
        expected = {
            "schema_retrieval/node.py",
            "sql_execution/node.py",
            "sql_validation/dry_run.py",
        }
        for rel in expected:
            path = TEXT2SQL_DIR / rel
            assert path.exists(), f"Text2SQL 接线点改名/丢失：{rel}"
            assert "text2sql_database_url" in path.read_text(encoding="utf-8"), rel


# ---------------------------------------------------------------------------
# 2) KB 子图领域范围说明
# ---------------------------------------------------------------------------


class _FakeWorkflow:
    """最小 KB 子图替身：只回答 finalize 需要的两个键。"""

    async def ainvoke(self, payload, config=None):  # noqa: ANN001 - 替身签名随子图
        return {"answer": "依据《民法典》第一百四十八条，受欺诈方有权请求撤销。", "sources": []}


@pytest.fixture()
def capture_kb_workflow(monkeypatch):
    """把 ``create_kb_query`` 的协作者全部换成替身，只留「传了什么 scope」。"""
    from agentix.application.agents import general_handler as gh
    from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
        multi_tool,
    )

    captured: dict[str, object] = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeWorkflow()

    monkeypatch.setattr(multi_tool, "create_kb_multi_tool_workflow", fake_create)
    monkeypatch.setattr(gh, "get_llm_client", lambda **kwargs: object())
    monkeypatch.setattr(gh, "KnowledgeGateway", lambda *args, **kwargs: object())
    monkeypatch.setattr(gh.ablation, "is_enabled", lambda key: True)
    monkeypatch.setattr(gh, "_maybe_clarify", lambda state, question: (None, False))
    return captured


async def _run_kb_query(monkeypatch, scope_value: str, captured: dict) -> None:
    from agentix.application.agents import general_handler as gh

    monkeypatch.setattr(settings, "KB_SCOPE_DESCRIPTION", scope_value)
    state = gh.AgentState(messages=[HumanMessage(content="民法典一共有多少条规定？")])
    await gh.create_kb_query(state, config={"configurable": {}})
    assert "scope_description" in captured, "create_kb_query 未把 scope 传给子图"


class TestKbScopeDescription:
    @pytest.mark.asyncio
    async def test_empty_passes_none(self, monkeypatch, capture_kb_workflow):
        """空 = 传 ``None`` = 子图沿用内置测试数据集文案（逐位等价，约定 1）。"""
        await _run_kb_query(monkeypatch, "", capture_kb_workflow)
        assert capture_kb_workflow["scope_description"] is None

    @pytest.mark.asyncio
    async def test_whitespace_passes_none(self, monkeypatch, capture_kb_workflow):
        await _run_kb_query(monkeypatch, "  \n ", capture_kb_workflow)
        assert capture_kb_workflow["scope_description"] is None

    @pytest.mark.asyncio
    async def test_configured_scope_is_passed_through(self, monkeypatch, capture_kb_workflow):
        text = "本知识库收录中华人民共和国现行法律法规与司法解释，处理法条释义与适用条件查询。"
        await _run_kb_query(monkeypatch, text, capture_kb_workflow)
        assert capture_kb_workflow["scope_description"] == text

    def test_default_setting_is_empty(self):
        from agentix.config.settings import Settings

        assert Settings.model_fields["KB_SCOPE_DESCRIPTION"].default == ""

    def test_workflow_keeps_builtin_text_when_none(self, monkeypatch):
        """子图侧：``scope_description=None`` ⇒ guardrails 收到内置测试数据集文案。"""
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_workflow,
        )

        seen: dict[str, str] = {}

        def fake_guardrails(llm, *, scope_text):  # noqa: ANN001 - 替身
            seen["scope_text"] = scope_text
            return lambda state: {}

        monkeypatch.setattr(kb_workflow, "create_guardrails_node", fake_guardrails)
        for name in ("create_router_node", "create_local_search_node", "create_external_search_node", "create_finalize_node"):
            monkeypatch.setattr(kb_workflow, name, lambda *a, **k: (lambda state: {}), raising=True)

        kb_workflow.create_kb_multi_tool_workflow(llm=object(), scope_description=None)
        assert "测试数据集知识库" in seen["scope_text"]

    def test_workflow_uses_configured_text(self, monkeypatch):
        from agentix.application.agents.kg_sub_graph.agentic_rag_agents.workflows.multi_agent import (
            kb_workflow,
        )

        seen: dict[str, str] = {}

        def fake_guardrails(llm, *, scope_text):  # noqa: ANN001 - 替身
            seen["scope_text"] = scope_text
            return lambda state: {}

        monkeypatch.setattr(kb_workflow, "create_guardrails_node", fake_guardrails)
        for name in ("create_router_node", "create_local_search_node", "create_external_search_node", "create_finalize_node"):
            monkeypatch.setattr(kb_workflow, name, lambda *a, **k: (lambda state: {}), raising=True)

        kb_workflow.create_kb_multi_tool_workflow(llm=object(), scope_description="法律条文库范围")
        assert seen["scope_text"] == "法律条文库范围"
