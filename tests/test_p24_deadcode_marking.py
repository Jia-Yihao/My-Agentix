"""批次 6 工作流 C：未接线能力守卫 + 已删除遗留物防复活 + search 工具描述回归。

⚠️ 本文件是**绊线（guard test / tripwire）**，不是功能覆盖（feature coverage）。

2026-09 状态变更（已获用户授权）
--------------------------------
本文件此前断言"三个死代码符号**都在**、且都带 DEAD-CODE 标注"。经审计后按
「不可达 + 非公开契约 + 非已文档化能力」三条同时成立才删除的原则处置：

| 符号 | 处置 | 依据 |
|---|---|---|
| `create_validate_final_answer_node` | **删除** | 唯一消费者是自身无法导入的遗留工作流 ⇒ 传递性不可达 |
| `text2cypher_with_viz_and_follow_ups.py` | **删除** | 0 处 import + 断链 import（永久无法导入） |
| `lg_builder.check_hallucinations` | **保留** | 是文档化的未接线能力（`docs/design/orchestration-rationale.md` A6），接线会引入每次执行的额外 LLM 调用，属需独立评估的运行期变更 |
| `ErrorCode.MAX_STEPS_EXCEEDED` | **保留** | 出现在文档化的对外契约（错误码→HTTP 状态映射表），删除属破坏性接口变更 |

因此本文件现在的职责是：**防接线（运行时行为变更）+ 防复活（回滚已删的东西）+
防新增（未登记的死代码）**。第三条由 ``DISPOSITION_TABLE`` 冻结快照承担。

- 一旦有人把 ``check_hallucinations`` 注册进 ``build_graph``，本文件失败，
  强制同步更新方案文档与标注；
- 一旦有人把已删除的遗留工作流/节点函数恢复回来，本文件失败；
- 一旦有人新增一处 ``DEAD-CODE(P2-5)`` 标注而未在处置表登记，本文件失败。

请勿把本文件的用例计入覆盖率成绩，也不要读作「这些功能被验证可用」。
"""

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "agentix"
LG_BUILDER_PATH = SRC_ROOT / "orchestration" / "langgraph" / "lg_builder.py"
VALIDATE_FA_PATH = (
    SRC_ROOT
    / "application"
    / "agents"
    / "kg_sub_graph"
    / "agentic_rag_agents"
    / "components"
    / "validate_final_answer"
    / "node.py"
)
ERRORS_PATH = SRC_ROOT / "domain" / "errors.py"

DEAD_CODE_TAG = "DEAD-CODE(P2-5)"

# build_graph 当前应注册的 9 个业务节点（不含 __start__）。
EXPECTED_NODES = {
    # 批次 7（§10.9.4）：新增入口节点 message_window（历史窗口化，
    # settings.MAX_HISTORY_MESSAGES<=0 时 no-op）——这是**有意接线**，
    # 同步更新本绊线；删除 message_window 或再增其他节点仍会触发本测试。
    "message_window",
    "analyze_and_route_query",
    "respond_to_general_query",
    "get_additional_info",
    "create_research_plan",
    "create_image_query",
    "create_file_query",
    "create_kb_query",
    # P1（任务执行）：execute_task 无条件注册（route_query 的 Literal 与
    # ROUTE_HANDLERS 需与节点集一致，phase19 绊线断言三者相等）；
    # 可达性由 TASK_EXEC_ENABLED 在路由侧门控。删节点或再增节点同样触发本绊线。
    "execute_task",
}


def _add_node_first_args(source_path: pathlib.Path) -> set:
    """AST 解析 ``build_graph`` 内所有 ``add_node(...)`` 调用，返回首参标识集合。

    首参为 ``Name`` 时取标识符（如 ``create_image_query``）；
    首参为字符串常量时取字面量（如 ``"create_research_plan"``）。
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
    build_graph = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_graph"
    )
    names = set()
    for node in ast.walk(build_graph):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_node"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            names.add(first.id)
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


# ---------------------------------------------------------------------------
# 1) 运行时：编译后的主图不含 check_hallucinations
# ---------------------------------------------------------------------------

def test_check_hallucinations_absent_from_registered_nodes():
    """真实编译图 ``lg_builder.graph`` 的节点表中不含 check_hallucinations。"""
    from agentix.orchestration.langgraph import lg_builder

    registered = set(lg_builder.graph.get_graph().nodes)
    assert "check_hallucinations" not in registered


def test_build_graph_still_registers_exactly_the_expected_nodes():
    """编译图节点集合与预期 9 节点一致（既防静默接线，也防误删现有节点）。

    批次 7 前为 7 节点；message_window 为历史窗口化（§10.9.4）的有意加入，
    绊线随拓扑变更同步收紧——这正是本守卫的设计意图。
    """
    from agentix.orchestration.langgraph import lg_builder

    registered = set(lg_builder.graph.get_graph().nodes)
    business_nodes = registered - {"__start__"}
    assert business_nodes == EXPECTED_NODES


# ---------------------------------------------------------------------------
# 2) 静态：build_graph 的 add_node 调用不含 check_hallucinations
# ---------------------------------------------------------------------------

def test_add_node_calls_in_build_graph_exclude_check_hallucinations():
    """AST 级双保险：build_graph 源码里没有以 check_hallucinations 注册节点。"""
    assert "check_hallucinations" not in _add_node_first_args(LG_BUILDER_PATH)


def test_add_node_calls_match_expected_node_set():
    """AST 解析出的 add_node 首参集合即预期 9 节点（含 message_window）。"""
    assert _add_node_first_args(LG_BUILDER_PATH) == EXPECTED_NODES


# ---------------------------------------------------------------------------
# 3) 标注字面量仍在（删除标注 = 触发文档同步）
# ---------------------------------------------------------------------------

def test_lg_builder_source_carries_dead_code_tag():
    assert DEAD_CODE_TAG in LG_BUILDER_PATH.read_text(encoding="utf-8")


def test_validate_final_answer_node_is_deleted_and_stays_deleted():
    """★ validate_final_answer 的**节点函数**已删除，且不得复活。

    2026-09 授权清理：该节点传递性不可达——唯一消费者是
    ``workflows/multi_agent/text2cypher_with_viz_and_follow_ups.py``，
    ​而那个工作流全仓 0 处 import **且本身无法导入**
    （``from .edges import viz_mapper_edge``，``edges.py`` 无此符号）。
    两者已一并删除（回滚点 git ``dbca16d``）。

    注意本包**没有**整体删除：``prompts`` / ``models`` 被主链路的
    ``final_answer_guard`` 复用。所以这里断言的是"节点函数消失、prompt 资产留下"。
    """
    assert not VALIDATE_FA_PATH.exists(), (
        f"已删除的节点文件又出现了：{VALIDATE_FA_PATH}"
    )
    pkg_dir = VALIDATE_FA_PATH.parent
    assert (pkg_dir / "prompts.py").exists(), "prompts 被 final_answer_guard 复用，不得删除"
    assert (pkg_dir / "models.py").exists(), "models 被 final_answer_guard 复用，不得删除"


def test_no_dangling_reference_to_deleted_node():
    """★ 全仓不得残留对已删除符号的引用（防"删了定义、留着调用"）。

    这是删除操作真正的风险点：函数删掉、某处 import 还在，运行到那条路径才炸。
    故用 AST 全仓扫描 ``create_validate_final_answer_node`` 的 import/调用。
    """
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "create_validate_final_answer_node":
                offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "create_validate_final_answer_node":
                offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{node.lineno}")
    assert not offenders, f"仍引用已删除的 create_validate_final_answer_node：{offenders}"


def test_max_steps_exceeded_carries_dead_code_tag():
    """ErrorCode.MAX_STEPS_EXCEEDED 仍带标注。

    它与另两项**不同**：不删除。理由是它出现在文档化的对外契约里
    （``docs/architecture/interfaces.md`` 的控制流层 → HTTP 500 映射表），
    错误码属于 API 面，删除是**破坏性接口变更**；而"预留但当前无产生点"
    本身不是缺陷。标注保留，语义从"待删除"改为"预留"。
    """
    source = ERRORS_PATH.read_text(encoding="utf-8")
    assert DEAD_CODE_TAG in source
    assert "MAX_STEPS_EXCEEDED" in source


#: ``DEAD-CODE(P2-5)`` 标注点的**处置表**（文件 → 处置）。
#:
#: 标签语义在此项目里是「**P2-5 审计在册项**」，不是「待删除」——
#: 每个在册点都必须写明处置（``处置=`` 标记）。本表是**冻结快照**：
#: 新增死代码标注点、或删掉某一处，都会让
#: ``test_dead_code_sites_match_disposition_table`` 失败，强制更新本表。
DISPOSITION_TABLE: dict = {
    "agentix/domain/errors.py": "保留·预留语义（对外契约里的错误码，删除属破坏性变更）",
    "agentix/orchestration/langgraph/lg_builder.py": "保留·有意不接线（文档化未接线能力，见 orchestration-rationale A6）",
    "agentix/application/agents/kg_sub_graph/ps_genai_agents/components/guardrails/prompts.py":
        "保留·平行变体家族（有专属导入守卫与行为断言，4 文件/69 行，删除需改 2 处测试）",
}


def test_dead_code_sites_match_disposition_table():
    """★ 审计在册点必须与处置表**逐一对齐**（防"悄悄新增一处死代码"）。

    单点断言（如上面那条）只能守住一个位置；真正会失控的是**新增**：
    某天有人留了个永久不可达的函数、顺手写了 ``DEAD-CODE(P2-5)``，
    却没人登记处置。本用例把"在册点集合"变成冻结快照，让新增必须显式登记。

    实现上扫 ``agentix/`` 全部 ``.py``，收集含标签的文件，
    并要求：①集合与处置表键完全相等；②每个文件都带 ``处置=`` 标记
    （只有标签没有处置 = 审计结论缺失）。
    """
    found: dict = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        if DEAD_CODE_TAG not in text:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        found[rel] = "处置=" in text

    assert found, f"全仓未找到任何 {DEAD_CODE_TAG} 标注——标签被误删或扫描路径失效"
    assert set(found) == set(DISPOSITION_TABLE), (
        "死代码在册点与处置表不一致。\n"
        f"  仅源码有、表里没有（新增未登记）：{sorted(set(found) - set(DISPOSITION_TABLE))}\n"
        f"  仅表里有、源码没有（已清理但表未更新）：{sorted(set(DISPOSITION_TABLE) - set(found))}"
    )
    missing_disposition = sorted(p for p, ok in found.items() if not ok)
    assert not missing_disposition, (
        f"以下在册点只有标签、没有 `处置=` 结论：{missing_disposition}"
    )


# ---------------------------------------------------------------------------
# 4) search 工具描述已补「何时不能用」反例（与 4 个 KG 工具同风格）
# ---------------------------------------------------------------------------

def test_search_description_has_when_not_to_use_counterexample():
    from agentix.tools.definitions import registry

    # 导入包即触发 registry.register(search spec)（见 infrastructure/tools/__init__.py）
    import agentix.tools  # noqa: F401

    description = registry.get_spec("search").description
    # 与 KG 工具统一的正/反例措辞
    assert "何时用" in description
    assert "何时不能用" in description
    # 反例内容要求：法条/司法解释权威查询应走知识库/知识图谱路径
    assert "知识库" in description and "知识图谱" in description
    # 反例内容要求：需要可引用来源页面时不适用
    assert "来源" in description
    # 正向边界：仅用于时效性公开信息
    assert "时效性" in description
