"""PhaseK 任务 2：审批指标 ``decision`` label 的**静态值域守卫**。

验收口径（PhaseK 计划 §任务 2「值域归一」+ §事实表第 21 行那条本批新发现的腐烂）：

- ``retrieval_approval_decisions_total{target_type,decision}`` 的 ``decision``
  只允许六值 ``{approved, rejected, edited, resumed, auto, bypass}``
  （维护者 2026-10-27 裁决；第 1 轮审查发现初稿的五值是枚举缺漏——
  ``PermissionMode.BYPASS`` 下 ``hitl_bridge.py:211``→``:223`` 也写同一 label）。
  本文件里的 ``ALLOWED_DECISIONS`` 是这张表在**测试侧**的唯一真源，并与
  ``agentix/observability/metrics.py`` 的 docstring/help 文案逐字对照
  （约定 6 ④：同一个判定不许两个源）。
- 守卫是**静态**的（读源码 AST，不跑生产代码）：计划 148 行明写"不靠跑一遍才有值"，
  而 CI 上恒真的守卫等于没写。行为用例只会钉住它跑到的那几条分支，静态守卫一次
  钉住全部四个写入点：
  ① ``task_execution.py`` 进环私有包装 ``_record_in_loop_decision`` 的全部调用点；
  ② ``hitl_bridge.py`` 人工裁决的 ``bucket[result.action]`` 打点；
  ③ ``hitl_bridge.py`` 自动放行的 ``auto_label`` 打点（``auto``/``bypass``）；
  ④ ``retrieval/usecases/approval_service.py`` 的直接调用（approved/rejected）。
- 禁止开放取值重新出现：腐烂的第一现场是 ``decision=result.action.value``
  （枚举原值 ⇒ 第四套词汇）。这里判得住的不只是那一个写法——任何 ``Attribute``
  链、函数调用、f-string 作为 decision 实参都算越界，只放行「字面量 / 闭合映射取值 /
  同作用域内取值集合闭合的本地名字」。

本文件不改生产代码、不写指标：只读源码文本与 AST。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

import pytest

REPO = Path(__file__).resolve().parents[1]

#: decision label 的唯一允许值域（维护者 2026-10-27 裁决，六值）
ALLOWED_DECISIONS = frozenset(
    {"approved", "rejected", "edited", "resumed", "auto", "bypass"}
)

#: 写入点所在的生产文件（四个写入点分布在这三个文件里，漏一个文件等于另开一个洞）
GUARDED_FILES = (
    "agentix/application/services/task_execution.py",
    "agentix/policies/hitl/hitl_bridge.py",
    "agentix/retrieval/usecases/approval_service.py",
)

#: 指标定义文件：测试侧值域表必须与它的 docstring / help 文案逐字一致
METRICS_FILE = "agentix/observability/metrics.py"

#: 真正落指标的那一个函数（其余同名参数只是转发）
_METRIC_SINK = "record_approval_decision"

#: 已知包装函数（函数体内把形参转交给 sink）——其**签名形参**不参与判定，
#: 判定发生在它的调用点上，这样"加一层包装"不能用来绕过值域检查。
_KNOWN_WRAPPERS = frozenset({"_record_in_loop_decision", "_record_decision"})

_FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def _src(rel_path: str) -> str:
    return (REPO / rel_path).read_text(encoding="utf-8")


def _tree(rel_path: str) -> ast.Module:
    return ast.parse(_src(rel_path))


def _callee_name(node: ast.Call) -> Optional[str]:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _decision_expr(node: ast.Call) -> Optional[ast.expr]:
    """取 decision 实参节点（按被调者的签名定位，不看位置就认错）。

    三种形状各有其位：
    - sink ``record_approval_decision(target_type, decision)`` ⇒ keyword ``decision``
      或第 2 枚位置参数（仓内两处直接调用都走 keyword；位置形态一并认，免得换写法就漏）；
    - 进环包装 ``_record_in_loop_decision(decision)`` ⇒ 第 1 枚位置参数；
    - 桥侧包装 ``_record_decision(label)`` ⇒ 第 1 枚位置参数（形参名是 ``label``，
      语义就是 decision——名字不同不影响判定）。
    """
    callee = _callee_name(node)
    for kw in node.keywords:
        if kw.arg in ("decision", "label"):
            return kw.value
    if node.args:
        if callee == _METRIC_SINK:
            return node.args[1] if len(node.args) >= 2 else None
        return node.args[0]
    return None


def _param_names(func: ast.AST) -> Set[str]:
    if not isinstance(func, _FUNC_TYPES):
        return set()
    a = func.args
    names = {arg.arg for arg in a.posonlyargs}
    names |= {arg.arg for arg in a.args}
    names |= {arg.arg for arg in a.kwonlyargs}
    if a.vararg is not None:
        names.add(a.vararg.arg)
    if a.kwarg is not None:
        names.add(a.kwarg.arg)
    return names


def _local_literal_assigns(scope: ast.AST, name: str) -> List[str]:
    """收集作用域内 ``name = "字面量"`` 的赋值（含 AnnAssign / augmented）。

    为什么要这一层：``auto_label`` 这类名字在调用点看是"开放取值"，但它的可取值
    集合在同一函数体内**完全闭合**（``= "bypass"`` / ``= "auto"``）。只看调用点会
    误判成越界，只看定义点会漏掉真正的调用形态——两处接起来才判得住。
    """
    found: List[str] = []
    for sub in ast.walk(scope):
        value: Optional[ast.expr] = None
        targets: Sequence[ast.expr] = ()
        if isinstance(sub, ast.Assign):
            targets, value = sub.targets, sub.value
        elif isinstance(sub, ast.AnnAssign):
            targets, value = [sub.target], sub.value
        elif isinstance(sub, ast.AugAssign):
            targets, value = [sub.target], sub.value
        if value is None or not isinstance(value, ast.Constant):
            continue
        if not isinstance(value.value, str):
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            found.append(value.value)
    return found


def _dict_literal_in(scope: ast.AST, name: str) -> Optional[ast.Dict]:
    """找 ``name = {...}`` 的字面量映射（桥侧 ``bucket`` 就是这一形态）。"""
    for sub in ast.walk(scope):
        if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Dict):
            if any(isinstance(t, ast.Name) and t.id == name for t in sub.targets):
                return sub.value
    return None


def _resolve(expr: ast.expr, scope: ast.AST) -> Tuple[Set[str], List[str]]:
    """把 decision 实参解析成"它可能取到的字面量集合"，返回 ``(values, violations)``。

    只放行三种形态，其余一律记违规：字符串字面量、同作用域内取值闭合的本地名字
    （``auto_label`` 这类）、同作用域内是字面量映射的下标取值（``bucket[...]``）。
    """
    values: Set[str] = set()
    violations: List[str] = []

    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, str):
            values.add(expr.value)
        else:
            violations.append(
                f"decision 实参是非字符串常量 {expr.value!r}"
            )
        return values, violations

    if isinstance(expr, ast.Name):
        if expr.id in _param_names(scope) and getattr(scope, "name", "") in _KNOWN_WRAPPERS:
            # 包装函数的形参：判定发生在调用点，这里跳过以免重复计数
            return values, violations
        lits = _local_literal_assigns(scope, expr.id)
        if lits:
            values.update(lits)
            return values, violations
        violations.append(
            f"decision 实参是名字 {expr.id}，但在本作用域内找不到闭合的字面量赋值"
            "（取值集合不可判定）"
        )
        return values, violations

    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
        mapping = _dict_literal_in(scope, expr.value.id)
        if mapping is None:
            violations.append(
                f"decision 实参是 {expr.value.id}[...]，但 {expr.value.id} "
                f"在本作用域不是字面量映射（取值集合不可判定）"
            )
            return values, violations
        for elt in mapping.values:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                values.add(elt.value)
            else:
                violations.append(
                    f"{expr.value.id} 映射的值不是字符串字面量：{ast.dump(elt)[:60]}"
                )
        return values, violations

    violations.append(
        "decision 实参形态越界（只允许字面量 / 闭合映射取值 / 闭合的本地名字）："
        f"{type(expr).__name__} {ast.dump(expr)[:100]}"
    )
    return values, violations


def _forbidden_forms(expr: ast.expr) -> List[str]:
    """找出 decision 实参里的**开放取值**形态（属性链 / 调用 / f-string）。

    刻意不无脑 ``ast.walk`` 整棵树：被允许的写法 ``bucket[result.action]``
    的**映射键**天然是一个属性链，按字面 walk 会把收敛后的正确形态误判成越界。
    所以这里按「值本身可能来自哪里」逐形态判：
    - 顶层就是属性链 / 调用 / f-string ⇒ 越界（``result.action.value`` 的第一现场）；
    - 下标取值 ⇒ 本用例只判**键**里有没有函数调用与 f-string（键可以是枚举成员，
      因为映射的值域闭合性由 ``_resolve`` 与 ``bucket`` 专门用例两处判）；
    - 名字 / 字符串常量 ⇒ 这里放行，闭合性交给 ``_resolve`` 判（它会在名字既不是
      包装形参、又在本作用域找不到字面量赋值时报红）。
    """
    if isinstance(expr, ast.Constant):
        return []
    if isinstance(expr, ast.Name):
        return []
    if isinstance(expr, ast.Attribute):
        return [f"属性链 {ast.unparse(expr)!r}（取值集合不可判定）"]
    if isinstance(expr, ast.Call):
        return [f"函数调用 {ast.unparse(expr)!r}（取值集合不可判定）"]
    if isinstance(expr, ast.JoinedStr):
        return [f"f-string {ast.unparse(expr)[:60]!r}（取值集合不可判定）"]
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
        bad: List[str] = []
        for sub in ast.walk(expr.slice):
            if isinstance(sub, (ast.Call, ast.JoinedStr)):
                bad.append(
                    f"{expr.value.id}[...] 的键含 {type(sub).__name__}"
                    "（键不可判定 ⇒ 映射值域也不再闭合）"
                )
        return bad
    return [f"形态 {type(expr).__name__}（只允许字面量/闭合映射取值/闭合本地名字）"]


def _parent_map(tree: ast.AST) -> dict:
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _innermost_scope(node: ast.AST, parents: dict, tree: ast.AST) -> ast.AST:
    """调用点的**最内层**函数作用域（找不到函数就回落模块）。

    为什么按最内层归因而非"遍历各作用域各自 walk"：BFS 顺序会让同一个调用点先被
    **外层**函数走一遍，于是包装体内那行转发调用被外层判成"越界取值"——同一个判定
    两个结论，正是约定 6 ④ 反对的形态。最内层归因是唯一确定的那个作用域。
    """
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, _FUNC_TYPES):
            return cur
        cur = parents.get(id(cur))
    return tree


def _sites_from_src(rel: str, src: str) -> List[Tuple[str, ast.AST, ast.Call]]:
    """从**给定源码**里取写入点（判定逻辑与真文件共用同一个函数）。

    为什么单独拆出来：守卫自己也要被验一次"能变红"。一个只会对现树发绿的 AST
    检查，与被它漏掉的越界写法长得一模一样——把判定接成「输入源码 ⇒ 输出违规」，
    才能拿一段构造的越界源码当场证明它会响。
    """
    out: List[Tuple[str, ast.AST, ast.Call]] = []
    tree = ast.parse(src)
    parents = _parent_map(tree)
    for sub in ast.walk(tree):
        if not isinstance(sub, ast.Call):
            continue
        callee = _callee_name(sub)
        if callee != _METRIC_SINK and callee not in _KNOWN_WRAPPERS:
            continue
        scope = _innermost_scope(sub, parents, tree)
        expr = _decision_expr(sub)
        # 转发（包装体内把形参原样交给 sink）不参与值域判定：判定发生在调用点
        if (
            callee == _METRIC_SINK
            and isinstance(scope, _FUNC_TYPES)
            and scope.name in _KNOWN_WRAPPERS
            and isinstance(expr, ast.Name)
            and expr.id in _param_names(scope)
        ):
            continue
        out.append((rel, scope, sub))
    return out


def _violations_for(rel: str, src: str) -> Tuple[Set[str], List[str]]:
    """给定源码返回 ``(看过的 decision 取值集合, 违规清单)``。"""
    values: Set[str] = set()
    bad: List[str] = []
    for _rel, scope, node in _sites_from_src(rel, src):
        expr = _decision_expr(node)
        if expr is None:
            bad.append(f"{rel}:{node.lineno} 调用缺 decision 实参")
            continue
        for msg in _forbidden_forms(expr):
            bad.append(f"{rel}:{node.lineno} decision 实参 {msg}")
        got, viol = _resolve(expr, scope)
        bad.extend(f"{rel}:{node.lineno} {v}" for v in viol)
        for val in sorted(got):
            if val not in ALLOWED_DECISIONS:
                bad.append(f"{rel}:{node.lineno} decision={val!r} 不在允许值域内")
        if not got and not viol:
            bad.append(f"{rel}:{node.lineno} decision 取值集合为空（解析失败）")
        values |= got
    return values, bad


def _record_call_sites() -> List[Tuple[str, ast.AST, ast.Call]]:
    """四个写入点：``(文件, 最内层作用域, 调用节点)``。

    收集两类调用：① 对 sink 的直接调用；② 对已知包装（``_record_in_loop_decision`` /
    ``_record_decision``）的调用——包装内部那一层 sink 调用只是转发形参，真正决定
    取值的是这些调用点。**新出现的包装名不会被本表收进来**，所以新增打点路径必须
    同步登记，否则它的 sink 调用会因为「实参是不可解析的名字」被判红。
    """
    return [site for rel in GUARDED_FILES for site in _sites_from_src(rel, _src(rel))]


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


def test_every_decision_write_site_is_within_allowed_domain():
    """四个写入点的 decision 实参只能落进六值表（静态，不靠跑出来的值）。"""
    bad: List[str] = []
    seen_files: Set[str] = set()
    all_values: Set[str] = set()
    for rel in GUARDED_FILES:
        seen_files.add(rel)
        values, violations = _violations_for(rel, _src(rel))
        bad.extend(violations)
        all_values |= values

    sites = _record_call_sites()
    assert sites, "一个打点调用点都没找到 ⇒ 守卫恒真，等于没写"
    assert seen_files == set(GUARDED_FILES), "守卫覆盖不到全部写入点"
    assert not bad, "decision 值域被突破：\n" + "\n".join(bad)
    # 现树实际写入的取值集合：必须是六值表的**全部**（少一个值＝那条路径没人打点）
    assert all_values == set(ALLOWED_DECISIONS), (
        f"四个写入点实际够到的 decision 取值 {sorted(all_values)} 与值域表 "
        f"{sorted(ALLOWED_DECISIONS)} 不一致"
    )


@pytest.mark.parametrize(
    ("snippet", "expect_hit"),
    [
        # ① 腐烂的第一现场：枚举原值直接进 label
        (
            "def f():\n"
            "    record_approval_decision(target_type=\"tool\", "
            "decision=result.action.value)\n",
            True,
        ),
        # ② 越界字面量（第四套词汇的另一种形态：自己另起一个词）
        (
            "def f():\n"
            "    record_approval_decision(target_type=\"tool\", decision=\"ok\")\n",
            True,
        ),
        # ③ 动态取值（拼出来的 label：值域不可判定）
        (
            "def f(action):\n"
            "    record_approval_decision(target_type=\"tool\", "
            "decision=f\"a{action}\")\n",
            True,
        ),
        # ④ 新加一层未登记的包装 ⇒ 必须判红（它体内的 sink 调用实参是裸形参，
        #    取值集合不可判定）。这条钉的是"扩展点默认 fail-closed"：想加包装就得
        #    显式进 _KNOWN_WRAPPERS 并被调用点判定覆盖，不可能悄悄漏过去。
        (
            "def _my_wrapper(d):\n"
            "    record_approval_decision(target_type=\"tool\", decision=d)\n",
            True,
        ),
        # ⑤ 合规写法：字面量（守卫必须放过，否则它只是在喊狼）
        (
            "def f():\n"
            "    record_approval_decision(target_type=\"tool\", decision=\"approved\")\n",
            False,
        ),
        # ⑥ 合规写法：闭合映射取值（桥侧现形态，键是属性链但值域闭合）
        (
            "def f(result):\n"
            "    bucket = {A: \"approved\", B: \"rejected\"}\n"
            "    record_approval_decision(target_type=\"tool\", "
            "decision=bucket[result.action])\n",
            False,
        ),
    ],
    ids=["enum-value", "unknown-literal", "fstring", "unregistered-wrapper",
         "literal-ok", "closed-mapping-ok"],)
def test_guard_actually_bites_on_planted_violations(snippet: str, expect_hit: bool):
    """守卫的守卫： planted 越界写法必须让它变红，合规写法必须放过。

    没有这一条，"绿"就分不清是"没问题"还是"检测不出问题"——计划 149 行那句
    "CI 上恒真的守卫等于没写"针对的正是这个。判定函数与真文件用的是同一个
    （``_violations_for``），不另立第二套判据（约定 6 ④）。
    """
    _values, bad = _violations_for("synthetic.py", snippet)
    if expect_hit:
        assert bad, f" planted 越界写法没被抓住：{snippet}"
    else:
        assert not bad, f"合规写法被误判：{bad}"


def test_no_open_enum_value_expression_reappears():
    """腐烂的第一现场：``record_approval_decision(decision=result.action.value)``。

    枚举原值（``approve|reject|edit|resume``）一旦重新流进 label 就是第四套词汇。
    这里不止钉那一个写法：任何属性链 / 调用 / f-string 作为 decision 实参都判失败。
    """
    offenders: List[str] = []
    for rel, _scope, node in _record_call_sites():
        expr = _decision_expr(node)
        if expr is None:
            continue
        for msg in _forbidden_forms(expr):
            offenders.append(f"{rel}:{node.lineno} decision 实参 {msg}")
    assert not offenders, (
        "decision 实参重新出现开放取值（请改写字面量或闭合映射）：\n"
        + "\n".join(offenders)
    )


def test_bridge_decision_bucket_matches_enum_exactly():
    """桥侧 bucket 映射必须**恰好**覆盖 ``ReviewDecisionAction`` 四个成员。

    少一个 ⇒ ``bucket[result.action]`` 运行期抛 ``KeyError``（裁决落空）；
    多一个 ⇒ 值域被悄悄放宽。两侧都判。
    """
    from agentix.policies.hitl.sidecar import ReviewDecisionAction

    tree = _tree("agentix/policies/hitl/hitl_bridge.py")
    buckets = [
        sub.value
        for sub in ast.walk(tree)
        if isinstance(sub, ast.Assign)
        and isinstance(sub.value, ast.Dict)
        and any(isinstance(t, ast.Name) and t.id == "bucket" for t in sub.targets)
    ]
    assert len(buckets) == 1, f"桥侧 bucket 映射应为 1 处，实测 {len(buckets)} 处"

    keys = {
        f"ReviewDecisionAction.{k.attr}"
        for k in buckets[0].keys
        if isinstance(k, ast.Attribute)
        and isinstance(k.value, ast.Name)
        and k.value.id == "ReviewDecisionAction"
    }
    expected = {f"ReviewDecisionAction.{m.name}" for m in ReviewDecisionAction}
    assert keys == expected, (
        f"bucket 覆盖的枚举成员与 ReviewDecisionAction 不一致："
        f"缺 {sorted(expected - keys)}／多 {sorted(keys - expected)}"
    )

    vals = {v.value for v in buckets[0].values if isinstance(v, ast.Constant)}
    assert vals == {"approved", "rejected", "edited", "resumed"}, (
        f"bucket 取值必须是收敛后的四值，实测 {sorted(vals)}"
    )


def test_in_loop_records_exactly_once_per_decision_path():
    """进环每条裁决路径恰好一次打点（5 处 = 1 放行 approved + 4 失败 rejected）。

    双计会让"谁批的"这条序列失真（一次裁决记两笔），漏计就是本任务要补的断点。
    出口数是刻意手写：改分支结构时必须同步这里并说明理由，不许默默漂移。
    """
    sites = [
        (rel, scope, node)
        for (rel, scope, node) in _record_call_sites()
        if rel.endswith("task_execution.py")
    ]
    assert sites, "进环路径没有任何打点调用点 ⇒ 断点未收口"

    values: List[str] = []
    for rel, scope, node in sites:
        expr = _decision_expr(node)
        assert expr is not None, f"{rel}:{node.lineno} 缺 decision 实参"
        vals, viol = _resolve(expr, scope)
        assert not viol, f"{rel}:{node.lineno} 进环打点取值不可解析：{viol}"
        assert vals and vals <= set(ALLOWED_DECISIONS), f"{rel}:{node.lineno} {vals}"
        values.extend(sorted(vals))

    assert len(sites) == 5, f"进环打点调用应为 5 处（每条裁决路径恰好一次），实测 {len(sites)}"
    assert values.count("approved") == 1, f"放行出口应恰好 1 处，实测 {values}"
    assert values.count("rejected") == 4, f"失败出口应恰好 4 处，实测 {values}"


def _fold(src: str) -> str:
    """折叠空白、拼接相邻字符串字面量、去掉 RST 反引号：让跨行断值的清单能被逐字比对。

    为什么要这三步：
    - help 文案是隐式拼接的（``"...auto/"`` 紧跟 ``""bypass; ..."``），不接就会把
      ``bypass`` 判成"文档里没有"——而它明明在；
    - docstring 里的 ``{approved, ..., auto,\\n bypass}`` 会因换行断字而"肉眼一致、
      字面不等"——这正是约定 6 ④ 要防的漂移形态，判据本身不能靠运气对齐排版。
    """
    joined = re.sub(r'"\s*"', "", src)
    return " ".join(joined.split()).replace("`", "")


def test_metrics_vocabulary_matches_guard_single_source():
    """值域表与指标定义逐字一致（约定 6 ④：同一个判定不许两个源）。

    ``record_approval_decision`` 的 docstring 与 ``APPROVAL_DECISIONS`` 的 help 文案
    是这张表对外唯一可查之处。历史上正是它写着 ``approved / rejected`` 两值、而桥侧
    实际写枚举原值，才让"三套词汇"长出来且无人发现。
    """
    folded = _fold(_src(METRICS_FILE))

    # ① docstring 里的花括号清单必须**恰好**是这六值（多值少值都算两个源）
    documented = set()
    for m in re.finditer(r"\{([^{}]*)\}", folded):
        toks = frozenset(t.strip() for t in m.group(1).split(",") if t.strip())
        if {"approved", "rejected"} <= toks:
            documented.add(toks)
    assert ALLOWED_DECISIONS in documented, (
        "metrics.py 没有逐字列出六值表 "
        f"{sorted(ALLOWED_DECISIONS)}（实测花括号清单：{[sorted(d) for d in documented]}）"
        "⇒ 值域有两个源（守卫一张表、指标注释另一张表），迟早各说一套"
    )

    # ② Prometheus 导出面上人看得见的那行 help（decision: a/b/c…）也必须含全部六值
    help_match = re.search(r"decision:\s*([a-z/]+)", folded)
    assert help_match is not None, "APPROVAL_DECISIONS 的 help 文案没列出 decision 取值"
    help_vals = frozenset(v for v in help_match.group(1).split("/") if v)
    assert help_vals == ALLOWED_DECISIONS, (
        f"help 文案的 decision 取值 {sorted(help_vals)} 与守卫值域 "
        f"{sorted(ALLOWED_DECISIONS)} 不一致"
    )


def test_allowed_domain_is_reachable_in_source():
    """六值不是纸面清单：每个值都必须在源码里真被写到（否则该值已无人写入）。

    只判"越界"不判"够不够"的守卫会随时间退化——某分支被删掉后值域表还写着它，
    读指标的人以为有数据，实际恒 0。
    """
    corpus = "\n".join(_src(rel) for rel in (*GUARDED_FILES, METRICS_FILE))
    missing = [v for v in sorted(ALLOWED_DECISIONS) if f'"{v}"' not in corpus]
    assert not missing, (
        f"decision 值 {missing} 在四个写入点的源码里已无人写入 ⇒ 值域表腐烂，"
        "要么删除该值、要么补回写入点（并说明理由）"
    )


def test_in_loop_metric_never_silent_on_failure() -> None:
    """进环打点必须 fail-open 且留痕（约定 2），但**绝不吞掉裁决语义**。

    钉三点：① ``_record_in_loop_decision`` 体内 catch 宽异常并 ``logger.debug``；
    ② 带 ``# noqa: BLE001 - <理由>``（理由说明降级去向）；③ 函数只打点、不返回值
    ——调用点不参与任何放行判定（"指标记 rejected 但实际放行执行"是审查重点第 46 行
    明令禁止的形态，靠"返回值无人消费"这一结构事实钉住）。
    """
    tree = _tree("agentix/application/services/task_execution.py")
    fn = next(
        (
            sub
            for sub in ast.walk(tree)
            if isinstance(sub, _FUNC_TYPES) and sub.name == "_record_in_loop_decision"
        ),
        None,
    )
    assert fn is not None, "_record_in_loop_decision 不存在 ⇒ 进环断点未收口"

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert not returns, "进环打点函数不得返回任何值：一旦有返回值就可能被拿去做放行判定"

    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "打点必须有 except（约定 2：观测故障不得影响主链）"
    for h in handlers:
        assert (
            h.type is None or ast.unparse(h.type) == "Exception"
        ), f"必须捕获 Exception（宽 catch），实测 {ast.unparse(h.type) if h.type else 'bare'}"

    body_src = ast.unparse(fn)
    assert "logger.debug" in body_src, "打点失败必须留痕（logger.debug）"
    assert "record_approval_decision" in body_src, "打点函数体内必须真的调 sink"
