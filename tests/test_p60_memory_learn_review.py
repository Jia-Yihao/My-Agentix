"""P6-6.2/6.3 长时记忆（source-code-study §3.5/§3.6/§5 表 6.2–6.3）：
后台评审 + Skill 草稿沉淀。

钉死契约：

1. **路由（6.2）**：纯规则确定性——scope≠user → 不晋升；置信 <0.5 → 不晋升
   （先于 kind 判定，防低置信污染画像）；kind=user → 晋升；SEMANTIC +
   置信 ≥0.6 + 标签 preference/instruction → 晋升；过程性条目归 Skill 域
   （6.3 显式沉淀，评审不代劳）；reason 诚实进报告。
2. **执行（6.2）**：逐项 fail-open（单项晋升失败只计数不中断）；
   fire-and-forget 调度（done-callback 只记日志；同步上下文调用返回 None
   并告警，不伪装成功）。
3. **接线（6.2）**：``MEMORY_REVIEW_ENABLED`` 默认关 ⇒
   ``handle_conversation_summary`` 报告与基线完全一致、零调度。
4. **草稿工具（6.3）**：``SKILLS_ENABLED`` 治理——flag-off 不声明不注册；
   flag-on L1 + ``side_effect=True`` + schema 类名 == 工具名（title 契约）；
   必填三键/长度/单行/name 正则校验、**同名拒绝覆盖**、写后复验、
   草稿隔离于 ``SKILLS_DIR``（loader 扫不到 ⇒ 不会被当活技能加载）。
5. **flag 五处同步**：settings / selfcheck / .env.example / p41 / configuration.md。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentix.application.services.memory_review import (
    TARGET_EPISODIC,
    TARGET_PROFILE,
    items_from_summary,
    review_items,
    route_memory_item,
    schedule_background_review,
    schedule_review_for_summary,
)
from agentix.agent.skills.install import (
    install_read_skill_tool,
    install_skill_draft_tool,
)
from agentix.agent.skills.learn import DRAFT_DIR, create_skill_draft
from agentix.agent.skills.loader import parse_skill_file, scan_skills
from agentix.tools import registry_init
from agentix.tools.definitions import registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.tools.adapters.integrity import audit_tool_registries
from agentix.config import settings
from agentix.domain.agent.types import MemoryItem, MemoryKind, MemoryScope

# ---------------------------------------------------------------------------
# 基线断言：flag 默认关（flag 治理；见 configuration.md §5）
# ---------------------------------------------------------------------------

assert settings.MEMORY_REVIEW_ENABLED is False, "P6 基线：记忆后台评审默认关"
assert settings.SKILLS_ENABLED is False, "P2 基线：Skills 默认关"

_ROOT = Path(__file__).resolve().parents[1]
_TEST_TOOLS = ("create_skill_draft", "read_skill")


def _purge() -> None:
    registry_init._DECLARATIONS.clear()
    reset_tool_executor()
    for name in _TEST_TOOLS:
        if tool_registry.has_tool(name):
            tool_registry.delete_tool(name)


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _item(content, *, conf=1.0, kind=MemoryKind.EPISODIC, scope=MemoryScope.USER,
          subject="u1", tags=(), item_id=""):
    return MemoryItem(
        content=content, kind=kind, scope=scope, item_id=item_id,
        subject=subject, confidence=conf, tags=tags,
    )


# ===========================================================================
# 6.2-a 路由规则
# ===========================================================================


class TestRouteRules:
    @pytest.mark.parametrize(
        "item,target,reason",
        [
            (_item("知识", scope=MemoryScope.KNOWLEDGE), TARGET_EPISODIC, "scope-not-user"),
            (_item("低置信", kind=MemoryKind.USER, conf=0.4), TARGET_EPISODIC, "low-confidence"),
            (_item("画像", kind=MemoryKind.USER, conf=1.0), TARGET_PROFILE, "kind-user"),
            (
                _item("偏好", kind=MemoryKind.SEMANTIC, conf=0.6, tags=("preference",)),
                TARGET_PROFILE,
                "stable-semantic",
            ),
            (
                _item("指示", kind=MemoryKind.SEMANTIC, conf=0.9, tags=("instruction",)),
                TARGET_PROFILE,
                "stable-semantic",
            ),
            (
                _item("事实", kind=MemoryKind.SEMANTIC, conf=0.9, tags=("fact",)),
                TARGET_EPISODIC,
                "semantic-not-stable",
            ),
            (
                _item("偏好但低置信", kind=MemoryKind.SEMANTIC, conf=0.5, tags=("preference",)),
                TARGET_EPISODIC,
                "semantic-not-stable",
            ),
            (_item("规程", kind=MemoryKind.PROCEDURAL, conf=1.0), TARGET_EPISODIC, "procedural-skill-domain"),
            (_item("工作中", kind=MemoryKind.WORKING, conf=1.0), TARGET_EPISODIC, "kind-working"),
            (_item("情节", kind=MemoryKind.EPISODIC, conf=1.0), TARGET_EPISODIC, "kind-episodic"),
        ],
    )
    def test_rules(self, item, target, reason):
        decision = route_memory_item(item)
        assert (decision.target, decision.reason) == (target, reason)

    def test_low_confidence_beats_kind_user(self):
        # 优先级：置信地板先于 kind=user——低置信不污染稳定画像
        decision = route_memory_item(_item("x", kind=MemoryKind.USER, conf=0.3))
        assert decision.reason == "low-confidence"


# ===========================================================================
# 6.2-b 执行：逐项 fail-open + 报告
# ===========================================================================


class _RecordingManager:
    def __init__(self, fail_ids=()):
        self.remembered = []
        self.fail_ids = set(fail_ids)

    async def remember(self, item):
        if item.item_id in self.fail_ids:
            raise RuntimeError("redis down")
        self.remembered.append(item)
        return f"id:{item.item_id}"


class TestReviewItems:
    @pytest.mark.asyncio
    async def test_promote_and_skip_counts(self):
        items = [
            _item("晋升", kind=MemoryKind.SEMANTIC, tags=("preference",), conf=0.9, item_id="a"),
            _item("情节", kind=MemoryKind.EPISODIC, item_id="b"),
            _item("工作", kind=MemoryKind.WORKING, item_id="c"),
        ]
        mgr = _RecordingManager()
        report = await review_items(mgr, items)
        assert report.checked == 3
        assert report.promoted == 1
        assert report.skipped == 2
        assert report.failed == 0
        assert [it.item_id for it in mgr.remembered] == ["a"]

    @pytest.mark.asyncio
    async def test_single_failure_does_not_interrupt_others(self):
        items = [
            _item("坏", kind=MemoryKind.SEMANTIC, tags=("preference",), conf=0.9, item_id="bad"),
            _item("好", kind=MemoryKind.SEMANTIC, tags=("instruction",), conf=0.9, item_id="good"),
        ]
        mgr = _RecordingManager(fail_ids=("bad",))
        report = await review_items(mgr, items)
        assert report.failed == 1
        assert report.promoted == 1  # 后续项照常处理（fail-open）
        assert [it.item_id for it in mgr.remembered] == ["good"]

    @pytest.mark.asyncio
    async def test_report_to_dict_shape(self):
        report = await review_items(
            _RecordingManager(), [_item("x", kind=MemoryKind.USER, item_id="i1")]
        )
        data = report.to_dict()
        assert data["checked"] == 1
        assert data["promoted"] == 1
        assert data["decisions"][0]["item_id"] == "i1"
        assert data["decisions"][0]["target"] == TARGET_PROFILE


# ===========================================================================
# 6.2-c 摘要 → 卡片 → 条目 + 调度
# ===========================================================================


class TestSummaryToReview:
    SUMMARY = "用户要求回答时必须附上法条出处。本次咨询了劳动合同解除问题。"

    def test_items_from_summary_maps_cards(self):
        items = items_from_summary(self.SUMMARY)
        assert len(items) == 1
        item = items[0]
        assert item.kind == MemoryKind.SEMANTIC
        assert item.scope == MemoryScope.USER
        assert item.subject == "default_user"
        assert item.tags == ("preference",)
        assert item.item_id.startswith("card:")

    def test_items_from_summary_empty_for_plain_text(self):
        assert items_from_summary("本次咨询了合同违约金标准。") == []
        assert items_from_summary("") == []

    @pytest.mark.asyncio
    async def test_schedule_review_for_summary_promotes(self):
        mgr = _RecordingManager()
        task = schedule_review_for_summary(
            self.SUMMARY, manager_factory=lambda: mgr
        )
        assert task is not None
        report = await task
        assert report.promoted == 1
        assert len(mgr.remembered) == 1

    def test_schedule_review_for_summary_no_cards_is_none(self):
        assert schedule_review_for_summary("普通文本无卡片") is None

    @pytest.mark.asyncio
    async def test_done_callback_swallows_failure_without_crash(self):
        async def _boom():
            raise RuntimeError("评审炸")

        task = schedule_background_review(_boom)
        assert task is not None
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)  # 让 done-callback 跑完
        assert isinstance(task.exception(), RuntimeError)  # 已被 callback 取走不告警

    def test_no_running_loop_returns_none(self):
        # 同步上下文（本测试本身无事件循环）：诚实返回 None，不伪装成功
        async def _ok():
            return None

        assert schedule_background_review(_ok) is None


# ===========================================================================
# 6.2-d 接线：handle_conversation_summary flag 两态
# ===========================================================================


class _FakeSummarizerModel:
    def __init__(self, content):
        self.content = content

    async def ainvoke(self, messages, **kw):
        return SimpleNamespace(content=self.content)


class _FakeStore:
    def __init__(self):
        self.stored = []

    async def store_summary(self, session_id, summary, messages, **kw):
        self.stored.append((session_id, summary))


class TestSummaryHandlerWiring:
    PAYLOAD = {
        "session_id": "s1",
        "messages": [{"role": "user", "content": "你好"}],
    }

    def _patch_handler(self, monkeypatch, *, summary_text):
        from agentix.application.agents import llm_factory
        from agentix.application.services import long_term_memory as ltm

        store = _FakeStore()
        model = _FakeSummarizerModel(summary_text)
        monkeypatch.setattr(llm_factory, "get_llm_client", lambda **kw: model)
        monkeypatch.setattr(ltm, "get_long_term_memory", lambda: store)
        return store

    @pytest.mark.asyncio
    async def test_flag_off_schedules_nothing(self, monkeypatch):
        from agentix.application.services import memory_review
        from agentix.application.services.task_handlers import handle_conversation_summary

        store = self._patch_handler(
            monkeypatch, summary_text="用户要求回答以表格呈现对比信息。"
        )
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", False)
        calls = []
        monkeypatch.setattr(
            memory_review, "schedule_review_for_summary", lambda *a, **k: calls.append(a)
        )
        out = await handle_conversation_summary(dict(self.PAYLOAD))
        assert out["status"] == "summarized"
        assert out["session_id"] == "s1"
        assert len(store.stored) == 1  # 摘要照常落库
        assert calls == []  # flag-off 零调度

    @pytest.mark.asyncio
    async def test_flag_on_schedules_with_summary(self, monkeypatch):
        from agentix.application.services import memory_review
        from agentix.application.services.task_handlers import handle_conversation_summary

        self._patch_handler(
            monkeypatch, summary_text="用户要求回答以表格呈现对比信息。"
        )
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", True)
        calls = []
        monkeypatch.setattr(
            memory_review,
            "schedule_review_for_summary",
            lambda summary, **k: calls.append(summary),
        )
        out = await handle_conversation_summary(dict(self.PAYLOAD))
        assert out["status"] == "summarized"
        assert len(calls) == 1
        assert "表格呈现" in calls[0]

    @pytest.mark.asyncio
    async def test_flag_on_schedule_failure_does_not_break_handler(self, monkeypatch):
        from agentix.application.services import memory_review
        from agentix.application.services.task_handlers import handle_conversation_summary

        self._patch_handler(
            monkeypatch, summary_text="用户要求回答以表格呈现对比信息。"
        )
        monkeypatch.setattr(settings, "MEMORY_REVIEW_ENABLED", True)

        def _boom(*a, **k):
            raise RuntimeError("调度炸")

        monkeypatch.setattr(memory_review, "schedule_review_for_summary", _boom)
        out = await handle_conversation_summary(dict(self.PAYLOAD))
        assert out["status"] == "summarized"  # 评审旁路失败不影响摘要落库


# ===========================================================================
# 6.3-a flag 治理 + 声明契约
# ===========================================================================


class TestLearnFlagGovernance:
    def test_flag_off_declarations_exclude_draft_tool(self):
        registry_init._DECLARATIONS.clear()
        names = [spec.name for spec, _ in registry_init.get_declarations()]
        assert "create_skill_draft" not in names

    def test_flag_off_install_registers_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
        install_skill_draft_tool()
        assert not get_tool_executor().has_handler("create_skill_draft")
        assert not tool_registry.has_tool("create_skill_draft")

    def test_flag_on_declaration_contract(self, monkeypatch):
        monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        decls = {spec.name: cls for spec, cls in registry_init.get_declarations()}
        assert "create_skill_draft" in decls
        # title 契约：schema 类名 == 工具名（selection._spec_for_schema 回查依赖）
        assert decls["create_skill_draft"].__name__ == "create_skill_draft"
        spec = next(
            s for s, _ in registry_init.get_declarations() if s.name == "create_skill_draft"
        )
        assert spec.risk_level == "L1"
        assert spec.side_effect is True
        assert spec.retryable is False  # side_effect 工具禁止自动重试
        assert spec.timeout_ms == 10_000
        assert spec.capabilities == ("skills",)

    def test_flag_on_install_registers_handler(self, monkeypatch):
        monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        install_skill_draft_tool()
        assert get_tool_executor().has_handler("create_skill_draft")

    def test_flag_on_audit_clean(self, monkeypatch):
        monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
        registry_init._DECLARATIONS.clear()
        install_read_skill_tool()
        install_skill_draft_tool()
        report = audit_tool_registries()
        assert "create_skill_draft" not in report.declared_no_handler
        assert "read_skill" not in report.declared_no_handler


# ===========================================================================
# 6.3-b 草稿写入：校验 / 不覆盖 / 复验 / 隔离
# ===========================================================================


def _valid_args(**over):
    base = {
        "name": "p60-release-flow",
        "description": "跑本地发布验证的顺序",
        "anti_when": "只改了文档时（守卫 + ruff 即可）",
        "body": "1. ruff check\n2. 守卫测试\n3. 全量 pytest",
    }
    base.update(over)
    return base


class TestCreateSkillDraft:
    @pytest.mark.parametrize(
        "bad,msg_part",
        [
            ({"name": ""}, "name is required"),
            ({"name": "a/b"}, "invalid skill name"),
            ({"name": "a\\b"}, "invalid skill name"),
            ({"description": ""}, "description is required"),
            ({"description": "x" * 121}, "description 121 chars"),
            ({"anti_when": ""}, "anti_when is required"),
            ({"anti_when": "x" * 241}, "anti_when 241 chars"),
            ({"body": "x" * 8001}, "body 8001 chars"),
            ({"description": "多行\n描述"}, "single line"),
        ],
    )
    def test_validation_rejects_without_writing(self, tmp_path, bad, msg_part):
        out = create_skill_draft(_valid_args(**bad), root=tmp_path)
        assert out["created"] is False
        joined = " ".join(out.get("errors", [])) + out.get("error", "")
        assert msg_part in joined
        assert list(tmp_path.iterdir()) == []  # 拒绝时不落任何文件

    def test_garbage_args_never_raise(self, tmp_path):
        for args in ({}, {"name": None}, {"name": "", "description": "", "anti_when": "", "body": ""}):
            out = create_skill_draft(args, root=tmp_path)
            assert out["created"] is False

    def test_success_writes_parseable_draft(self, tmp_path):
        out = create_skill_draft(_valid_args(), root=tmp_path)
        assert out["created"] is True
        path = Path(out["path"])
        assert path.exists()
        meta = parse_skill_file(path, tmp_path)
        assert meta.name == "p60-release-flow"
        assert meta.description == "跑本地发布验证的顺序"
        assert "1. ruff check" in path.read_text(encoding="utf-8")

    def test_never_overwrites_existing_draft(self, tmp_path):
        first = create_skill_draft(_valid_args(), root=tmp_path)
        assert first["created"] is True
        out = create_skill_draft(_valid_args(body="完全不同的正文"), root=tmp_path)
        assert out["created"] is False
        assert "already exists" in out["error"]
        content = (tmp_path / "p60-release-flow" / "SKILL.md").read_text(encoding="utf-8")
        assert "完全不同的正文" not in content  # 原内容未被覆盖

    def test_draft_dir_isolated_from_skills_dir(self, tmp_path):
        draft_root = tmp_path / "data" / "skills_drafts"
        out = create_skill_draft(_valid_args(name="p60-isolation"), root=draft_root)
        assert out["created"] is True
        skills_dir = Path(settings.SKILLS_DIR).resolve()
        assert not draft_root.resolve().is_relative_to(skills_dir)
        assert not Path(DRAFT_DIR).resolve().is_relative_to(skills_dir)
        # loader 扫描技能根不包含草稿名 ⇒ 草稿不会被当活技能加载
        names = [m.name for m in scan_skills(skills_dir)]
        assert "p60-isolation" not in names

    @pytest.mark.asyncio
    async def test_executor_e2e_creates_draft(self, monkeypatch, tmp_path):
        import agentix.agent.skills.learn as learn

        monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
        monkeypatch.setattr(learn, "DRAFT_DIR", str(tmp_path / "drafts"))
        registry_init._DECLARATIONS.clear()
        install_skill_draft_tool()
        result = await get_tool_executor().execute(
            "create_skill_draft",
            {
                "name": "p60-e2e",
                "description": "端到端草稿验收",
                "anti_when": "一次性任务",
                "body": "步骤一\n步骤二",
            },
        )
        assert result.success, result.error
        assert result.data["created"] is True
        meta = parse_skill_file(Path(result.data["path"]), tmp_path / "drafts")
        assert meta.name == "p60-e2e"

    @pytest.mark.asyncio
    async def test_executor_e2e_invalid_args_returns_honest_failure(self, monkeypatch, tmp_path):
        import agentix.agent.skills.learn as learn

        monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
        monkeypatch.setattr(learn, "DRAFT_DIR", str(tmp_path / "drafts"))
        registry_init._DECLARATIONS.clear()
        install_skill_draft_tool()
        result = await get_tool_executor().execute(
            "create_skill_draft",
            {"name": "bad/name", "description": "x", "anti_when": "y", "body": "z"},
        )
        assert result.success  # 工具层不抛：诚实回执而非异常
        assert result.data["created"] is False
        assert result.data["errors"]
        assert not (tmp_path / "drafts").exists()  # 校验失败零落盘

    def test_wiring_source_level(self):
        main_src = (_ROOT / "agentix" / "main.py").read_text(encoding="utf-8")
        assert "install_skill_draft_tool" in main_src
        install_src = (
            _ROOT / "agentix" / "agent" / "skills" / "install.py"
        ).read_text(encoding="utf-8")
        assert "def install_skill_draft_tool" in install_src
        assert 'register_handler("create_skill_draft"' in install_src


# ===========================================================================
# 5 处同步：settings / selfcheck / .env.example / p41 / configuration.md
# ===========================================================================


class TestFlagSync:
    def test_settings_defaults_off(self):
        assert settings.MEMORY_FANOUT_ENABLED is False
        assert settings.MEMORY_REVIEW_ENABLED is False

    def test_selfcheck_ignores_missing(self):
        from agentix.config.selfcheck import DEFAULT_IGNORED_MISSING

        assert "MEMORY_FANOUT_ENABLED" in DEFAULT_IGNORED_MISSING
        assert "MEMORY_REVIEW_ENABLED" in DEFAULT_IGNORED_MISSING

    def test_env_example_documents_keys(self):
        text = (_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "MEMORY_FANOUT_ENABLED=false" in text
        assert "MEMORY_REVIEW_ENABLED=false" in text

    def test_p41_lists_keys(self):
        text = (_ROOT / "tests" / "test_p41_doc_consistency.py").read_text(encoding="utf-8")
        assert '"MEMORY_FANOUT_ENABLED"' in text
        assert '"MEMORY_REVIEW_ENABLED"' in text

    def test_configuration_md_documents_keys(self):
        text = (_ROOT / "docs" / "architecture" / "configuration.md").read_text(
            encoding="utf-8"
        )
        assert "MEMORY_FANOUT_ENABLED" in text
        assert "MEMORY_REVIEW_ENABLED" in text
