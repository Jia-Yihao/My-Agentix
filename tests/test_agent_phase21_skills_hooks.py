"""Phase 21 — Skills 渐进披露 + Hooks 生命周期验收（规划 DoD 全项）。

DoD 映射：
1. flag-off：指令区/工具表与基线字节等价（回归断言）
2. flag-on：元数据注入 → read_skill 命中读正文 → 正文只进前缀外（目录无正文）
3. frontmatter 缺 anti_when → 条目跳过并告警
4. read_skill 路径穿越（../、绝对路径）被拒
5. hook：注册→触发顺序正确；hook 抛异常主路径不受影响；未注册/关 flag 零调用
6. describe_groups() 分组完整、同配置两次输出序稳定

全局单例纪律：本文件会动 settings 字段、_DECLARATIONS、skills 单例、
hooks 表、全局 ToolExecutor —— 均经 fixture 在 teardown 复位。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from loguru import logger

from agentix.agent import AgentLoop
from agentix.agent.context import SimpleContextBuilder
from agentix.agent.context.instructions import build_instructions
from agentix.orchestration.executor import MappingStepExecutor
from agentix.agent.hooks import (
    ON_ERROR,
    ON_RUN_END,
    ON_STEP_END,
    ON_TOOL_AFTER,
    ON_TOOL_BEFORE,
    clear_hooks,
    emit,
    hook_events,
    register,
)
from agentix.agent.hooks.install import install_tool_hooks
from agentix.policies.planner import SingleStepPlanner
from agentix.agent.skills.install import install_read_skill_tool
from agentix.agent.skills.loader import SkillMeta, scan_skills
from agentix.agent.skills.registry import catalog_block, get_registry, read_skill, reset_registry
from agentix.policies.verifier import StructuralVerifier
from agentix.tools import registry_init
from agentix.tools.definitions import ToolRegistry, ToolSpec, registry as tool_registry
from agentix.tools.gateway.executor import get_tool_executor, reset_tool_executor
from agentix.config.settings import settings
from agentix.domain.agent.types import TaskSpec

_KG_TOOLS = {
    "cypher_query",
    "predefined_cypher",
    "microsoft_graphrag_query",
    "text2sql_query",
}


# ---------- 夹具与工具 ----------------------------------------------------

def _reset_declarations() -> None:
    """清声明缓存 + 摘掉 flag-on 时挂进全局 registry 的 read_skill。"""
    registry_init._DECLARATIONS.clear()
    tool_registry.delete_tool("read_skill")


def _write_skill(
    root: Path,
    rel: str,
    name: str,
    description: str = "何时用：演示",
    anti_when: str | None = "何时不能用：演示之外",
    body: str = "# Demo\n\nBODY_MARKER",
) -> Path:
    path = root / rel / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}", f"description: {description}"]
    if anti_when is not None:
        lines.append(f"anti_when: {anti_when}")
    lines += ["---", "", body]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def skills_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """flag-on：tmp skills 目录（1 有效 + 1 缺 anti_when）+ 单例复位。"""
    _write_skill(tmp_path, "good", "demo-skill")
    _write_skill(tmp_path, "bad", "bad-skill", anti_when=None)
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "HOOKS_ENABLED", False)
    reset_registry()
    _reset_declarations()
    clear_hooks()
    reset_tool_executor()
    yield tmp_path
    reset_registry()
    _reset_declarations()
    clear_hooks()
    reset_tool_executor()


@pytest.fixture
def hooks_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "HOOKS_ENABLED", True)
    clear_hooks()
    yield
    clear_hooks()


def _collect(hook_event: str, sink: list) -> None:
    register(hook_event, lambda payload: sink.append(dict(payload)))


# ---------- DoD 1：flag-off 字节等价 -------------------------------------

def test_flag_off_instructions_byte_identical(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
    rules = ("RULE-A", "RULE-B")
    default = build_instructions(settings_rules=rules, load_project=False)
    explicit = build_instructions(
        settings_rules=rules, load_project=False, skills_enabled=False
    )
    assert default == explicit, "flag-off 输出必须与显式关完全一致"
    assert not any("Skills" in block for block in default)


def test_flag_off_tool_declarations_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
    _reset_declarations()
    declared = {spec.name for spec, _cls in registry_init._get_declarations()}
    assert declared == _KG_TOOLS, f"flag-off 声明清单被污染: {declared}"
    active = {cls.__name__ for cls in registry_init.get_active_tools()}
    assert "_ReadSkillSchema" not in active
    registry_init.ensure_registered()
    assert not tool_registry.has_tool("read_skill")


# ---------- DoD 2：flag-on 注入 + read_skill 惰性正文 ---------------------

def test_flag_on_catalog_injected_without_body(skills_env: Path):
    off = build_instructions(
        settings_rules=("RULE-A",), load_project=False, skills_enabled=False
    )
    on = build_instructions(settings_rules=("RULE-A",), load_project=False)
    assert on[0] == "RULE-A"
    assert len(on) == 2, f"flag-on 应恰好多出 skills 目录块: {on!r}"
    catalog = on[1]
    assert catalog.startswith("Skills（目录")
    assert "demo-skill" in catalog
    assert "不适用" in catalog, "目录行必须带 anti_when 负面条件"
    assert "BODY_MARKER" not in catalog, "正文绝不进静态前缀"
    assert off == build_instructions(
        settings_rules=("RULE-A",), load_project=False, skills_enabled=False
    )


def test_flag_on_empty_dir_adds_no_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    monkeypatch.setattr(settings, "SKILLS_DIR", str(tmp_path / "missing"))
    reset_registry()
    try:
        assert catalog_block() == ""
        on = build_instructions(settings_rules=("RULE-A",), load_project=False)
        assert on == ("RULE-A",), "空目录不得产生空白块"
    finally:
        reset_registry()


@pytest.mark.asyncio
async def test_read_skill_via_executor_returns_body_only(skills_env: Path):
    install_read_skill_tool()
    result = await get_tool_executor().execute("read_skill", {"name": "demo-skill"})
    assert result.success, result.error
    data = result.data
    assert data["found"] is True
    assert data["name"] == "demo-skill"
    assert "BODY_MARKER" in data["body"]
    assert not data["body"].lstrip().startswith("---"), "正文不得携带 frontmatter"
    # 未知名字：友好回执 + 可用清单（工具层不抛）
    miss = read_skill("nope")
    assert miss["found"] is False
    assert "demo-skill" in miss["available_skills"]


def test_read_skill_declaration_flag_gated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "SKILLS_ENABLED", False)
    _reset_declarations()
    assert "_ReadSkillSchema" not in {c.__name__ for c in registry_init.get_active_tools()}
    monkeypatch.setattr(settings, "SKILLS_ENABLED", True)
    _reset_declarations()
    try:
        assert "_ReadSkillSchema" in {c.__name__ for c in registry_init.get_active_tools()}
        registry_init.ensure_registered()
        assert tool_registry.has_tool("read_skill"), "flag-on 声明须入 registry"
    finally:
        _reset_declarations()


# ---------- DoD 3：结构校验（缺 anti_when 跳过并告警） ---------------------

def test_missing_anti_when_skipped_with_warning(skills_env: Path):
    buf = io.StringIO()
    sink_id = logger.add(buf, level="WARNING")
    try:
        metas = scan_skills(skills_env)
    finally:
        logger.remove(sink_id)
    names = [m.name for m in metas]
    assert names == ["demo-skill"], f"缺 anti_when 的条目必须被跳过: {names}"
    warnings = buf.getvalue()
    assert "anti_when" in warnings and "SKILL.md" in warnings


def test_invalid_and_duplicate_first_wins(skills_env: Path):
    # 无 frontmatter → 跳过
    (skills_env / "nofm").mkdir()
    (skills_env / "nofm" / "SKILL.md").write_text("just text", encoding="utf-8")
    # 重名 → 先到（排序）者胜
    _write_skill(skills_env, "aaa", "dup-skill", description="first")
    _write_skill(skills_env, "zzz", "dup-skill", description="second")
    buf = io.StringIO()
    sink_id = logger.add(buf, level="WARNING")
    try:
        metas = scan_skills(skills_env)
    finally:
        logger.remove(sink_id)
    by_name = {m.name: m for m in metas}
    assert "dup-skill" in by_name
    assert by_name["dup-skill"].description == "first"
    assert "duplicate" in buf.getvalue().lower()


# ---------- DoD 4：路径穿越被拒 -------------------------------------------

def test_read_skill_path_traversal_refused(skills_env: Path):
    # (a) 名字层：../ 与绝对路径不在注册表 → found=False，不抛不读盘
    for evil in ("../outside", "..\\outside", "/etc/passwd", "C:\\Windows\\win.ini"):
        out = read_skill(evil)
        assert out["found"] is False, f"穿越名 {evil!r} 必须拒绝"
        assert "available_skills" in out
    # (b) 纵深防御：伪造越过 root 的注册项 → read_body 拒绝（KeyError）
    registry = get_registry()
    outside = skills_env.parent / "secret.md"
    outside.write_text("---\nname: evil\n---\nSECRET", encoding="utf-8")
    registry._skills["evil"] = SkillMeta(
        name="evil", description="d", anti_when="a",
        path=outside.resolve(), root=registry.root,
    )
    try:
        with pytest.raises(KeyError):
            registry.read_body("evil")
    finally:
        registry._skills.pop("evil", None)


# ---------- DoD 5：hook 顺序 / 隔离 / 零调用 -------------------------------

def test_hook_order_and_exception_isolation(hooks_on: None):
    calls: list = []
    register(ON_STEP_END, lambda p: calls.append("first"))
    register(ON_STEP_END, lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    register(ON_STEP_END, lambda p: calls.append("third"))
    emit(ON_STEP_END, {"x": 1})  # 坏 hook 不得抛出
    assert calls == ["first", "third"], "注册顺序 == 触发顺序，坏 hook 不拖垮好 hook"
    assert hook_events() == (ON_STEP_END,)


def test_emit_flag_off_zero_calls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "HOOKS_ENABLED", False)
    clear_hooks()
    hits: list = []
    register(ON_TOOL_BEFORE, lambda p: hits.append(p))
    emit(ON_TOOL_BEFORE, {"tool_name": "t"})
    assert hits == [], "flag-off 时 hook 一次都不得被调用"
    emit(ON_TOOL_BEFORE, {})  # 未注册事件/空表同样零异常
    assert hits == []


def test_unknown_event_rejected_on_register_and_safe_on_emit(hooks_on: None):
    with pytest.raises(ValueError):
        register("on_nope", lambda p: None)
    with pytest.raises(TypeError):
        register(ON_ERROR, "not-callable")  # type: ignore[arg-type]
    emit("on_nope", {})  # 不抛


@pytest.mark.asyncio
async def test_executor_hooks_pair_after_gates(skills_env: Path, hooks_on: None):
    install_read_skill_tool()
    install_tool_hooks()
    events: list = []
    get_tool_executor().set_observation_hooks(
        before=lambda name, args: events.append(("before", name, dict(args))),
        after=lambda name, res: events.append(("after", name, getattr(res, "success", None))),
    )
    try:
        ok = await get_tool_executor().execute("read_skill", {"name": "demo-skill"})
        assert ok.success, ok.error
        assert [e[0] for e in events] == ["before", "after"]
        assert events[0][1] == "read_skill" and events[0][2] == {"name": "demo-skill"}
        assert events[1][2] is True
        # unknown tool 不过闸 → 不观察
        events.clear()
        bad = await get_tool_executor().execute("no_such_tool", {})
        assert not bad.success
        assert events == [], "未过闸的尝试不得触发观测钩子"
        # hook 抛异常 → 主路径结果不变
        get_tool_executor().set_observation_hooks(
            before=lambda n, a: 1 / 0, after=lambda n, r: 1 / 0
        )
        again = await get_tool_executor().execute("read_skill", {"name": "demo-skill"})
        assert again.success, "hook 故障不得改变执行结果"
    finally:
        get_tool_executor().set_observation_hooks(before=None, after=None)


@pytest.mark.asyncio
async def test_executor_hooks_flag_off_reaches_no_user_hook(skills_env: Path, monkeypatch: pytest.MonkeyPatch):
    """SKILLS 开、HOOKS 关：组合根装了 emit 包装，用户 hook 仍一次不触发。"""
    monkeypatch.setattr(settings, "HOOKS_ENABLED", False)
    clear_hooks()
    install_read_skill_tool()
    install_tool_hooks()
    hits: list = []
    register(ON_TOOL_BEFORE, lambda p: hits.append(p))
    register(ON_TOOL_AFTER, lambda p: hits.append(p))
    try:
        ok = await get_tool_executor().execute("read_skill", {"name": "demo-skill"})
        assert ok.success, ok.error
        assert hits == [], "HOOKS_ENABLED=false ⇒ 用户 hook 零调用"
    finally:
        get_tool_executor().set_observation_hooks(before=None, after=None)


# ---------- 21.8：loop 侧 step / run / error -------------------------------

def _ok_tools():
    return {"act": lambda **kw: "done-echo"}


@pytest.mark.asyncio
async def test_loop_hooks_step_and_run(hooks_on: None):
    events: list = []
    register(ON_STEP_END, lambda p: events.append(("step", p["index"], p["ok"])))
    register(ON_RUN_END, lambda p: events.append(("run", p["result"].terminal)))
    register(ON_ERROR, lambda p: events.append(("error", p["phase"])))
    loop = AgentLoop(
        planner=SingleStepPlanner(),
        context=SimpleContextBuilder(),
        verifier=StructuralVerifier(),
        executor=MappingStepExecutor(_ok_tools()),
        max_steps=5,
    )
    result = await loop.run(TaskSpec(goal="say done-echo", task_id="t21"))
    assert result.terminal == "completed"
    assert events == [("step", 1, True), ("run", "completed")]
    assert ON_STEP_END in hook_events()


@pytest.mark.asyncio
async def test_loop_hooks_error_phases(hooks_on: None):
    from agentix.domain.agent.types import ContextSnapshot

    class _BoomContext:
        async def build(self, task, *, trajectory=(), budget_tokens=0):
            raise RuntimeError("ctx down")

    class _BoomPlanner:
        async def plan(self, task, *, context=None):
            raise RuntimeError("plan down")

    class _BoomExecutor:
        async def execute(self, task, plan, step, context):
            raise RuntimeError("exec down")

    async def _ctx_ok(task, *, trajectory=(), budget_tokens=0):
        return ContextSnapshot(prompt="p")

    class _OkContext:
        build = staticmethod(_ctx_ok)

    # (a) context 相位
    errors: list = []
    runs: list = []
    register(ON_ERROR, lambda p: errors.append(p["phase"]))
    register(ON_RUN_END, lambda p: runs.append(p["result"]))
    loop = AgentLoop(
        planner=SingleStepPlanner(), context=_BoomContext(),
        verifier=StructuralVerifier(), executor=MappingStepExecutor(_ok_tools()),
    )
    r1 = await loop.run(TaskSpec(goal="g", task_id="e1"))
    assert r1.terminal == "failed"
    assert errors == ["context"] and len(runs) == 1

    # (b) plan 相位
    errors.clear(); runs.clear()
    loop = AgentLoop(
        planner=_BoomPlanner(), context=_OkContext(),
        verifier=StructuralVerifier(), executor=MappingStepExecutor(_ok_tools()),
    )
    r2 = await loop.run(TaskSpec(goal="g", task_id="e2"))
    assert r2.terminal == "failed"
    assert errors == ["plan"] and len(runs) == 1

    # (c) step 相位（executor 抛异常 → Observation + on_error）
    errors.clear(); runs.clear()
    steps: list = []
    register(ON_STEP_END, lambda p: steps.append(p["ok"]))
    loop = AgentLoop(
        planner=SingleStepPlanner(), context=_OkContext(),
        verifier=StructuralVerifier(), executor=_BoomExecutor(),
    )
    r3 = await loop.run(TaskSpec(goal="g", task_id="e3"))
    assert r3.terminal == "completed" or r3.terminal == "verify_failed"
    assert errors == ["step"] and len(runs) == 1
    assert steps == [False], "step_end 必须在异常观测入列后触发"

    # (d) verify 相位（step 正常成功 ⇒ 只有 verify 报错）
    errors.clear()

    class _BoomVerifier:
        async def verify(self, task, plan, observations):
            raise RuntimeError("verify down")

    loop = AgentLoop(
        planner=SingleStepPlanner(), context=_OkContext(),
        verifier=_BoomVerifier(), executor=MappingStepExecutor(_ok_tools()),
    )
    r4 = await loop.run(TaskSpec(goal="say done-echo", task_id="e4"))
    assert r4.terminal == "failed"
    assert errors == ["verify"], f"verify 异常相位各一次: {errors}"


# ---------- DoD 6：describe_groups ----------------------------------------

def test_describe_groups_stable_and_complete():
    reg = ToolRegistry()
    reg.register(ToolSpec(name="t1", description="desc one\n更多", capabilities=("alpha",)), lambda: None)
    reg.register(ToolSpec(name="t2", description="desc two", capabilities=("alpha", "beta")), lambda: None)
    reg.register(ToolSpec(name="t3", description="desc three", capabilities=("beta",)), lambda: None)
    reg.register(ToolSpec(name="t4", description="desc four"), lambda: None)
    g1 = reg.describe_groups()
    g2 = reg.describe_groups()
    assert g1 == g2, "同配置两次输出必须稳定"
    assert list(g1) == ["alpha", "beta", "general"], "组序 = 首次出现的注册序"
    assert g1["alpha"] == ("t1：desc one", "t2：desc two")
    assert g1["beta"] == ("t3：desc three",)
    assert g1["general"] == ("t4：desc four",)
    covered = [entry.split("：")[0] for entries in g1.values() for entry in entries]
    assert sorted(covered) == ["t1", "t2", "t3", "t4"], "分组必须覆盖全部工具且不重复"
