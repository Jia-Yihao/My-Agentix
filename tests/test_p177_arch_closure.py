"""p177——M1「把归属表变成可执行断言」：顶层包导入边 / 传递闭包的 equality 棘轮守卫。

方案与口径来源（验收基准，缺一即不算合格）：

- ``docs/architecture/分层架构与依赖规则.md``
  §1「终态顶层包归属」表 = ``ALLOWED`` 的唯一人工真源（逐行照抄）；
  §4「横切关注点的归属」= 两处口径补充（``config`` 全层可读——唯一例外 ``domain``，
  依据 §2.3「零外部依赖」，见 ``_universal_extra``；
  ``application → infrastructure/core`` 的 30 条属允许面——但那是**文件级**口径，
  本守卫按**包级边**粒度处理，见 ``VIOLATION_BASELINE`` 对应条目）；
  §7.3 = 本文件的修法本体（允许集 → 闭包计算器 → 相等棘轮 + 两条守卫口径：
  每条例外带理由、登记不许腐化）；§7.6 = 边界（AST 扫描，与 p66 扫描器同源，
  不新增依赖）。
- ``docs/design/性能与优化路线图.md`` §7.1 M1 行（验收五条）与 §7.2（M2 已落地，
  本批才录基线）。
- ``.qoder/rules/task-order.md`` §5（架构守卫两条口径）。

与 M2 grep 口径的差异（务必注意）：本守卫用 ``ast.walk`` 全树遍历，**函数体内的
延迟 import、``if TYPE_CHECKING:`` 块内的 import 一律计为边**——静态可达即算依赖，
不做"运行时才会执行"的豁免。

``__root__`` 口径：``agentix/*.py`` 直属文件（``main.py``、``code_exec_runtime.py``、
``__init__.py``）归伪节点 ``__root__``；指向直属模块的边（如 ``tools → agentix.code_exec_runtime``）
也归到 ``__root__``。**装配根不是一个层**——``main.py`` 触达全部层恰是装配根的职责，
所以直接边集合、闭包、允许集断言一律排除 ``__root__``；计算器仍单独报告它触达了
哪些包（``ScanResult.root_out`` / ``root_in``），供人工审计。

本守卫**只冻结现状、不修现状**：``VIOLATION_BASELINE`` 里的每一条都是已登记的
过渡债（M3 目标 / §4 横切面 / 归属表缺口 / 主会话已裁决「保留为基线例外、待拆除/待收敛」
的过渡债——2026-10-29 裁决的是处置方式，不是修复），新增一条即红、
登记过但实际消失同样即红（防白名单腐化，§7.3 第 4 步）。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

REPO = Path(__file__).resolve().parents[1]
AGENTIX = REPO / "agentix"

#: 伪节点：``agentix/`` 直属文件与指向直属模块的边都归它；不参与层矩阵。
ROOT_NODE = "__root__"


# ---------------------------------------------------------------------------
# 闭包计算器（§7.3 第 2 步）——纯静态 AST 扫描，无副作用，可单测
# ---------------------------------------------------------------------------


def _top_packages(root: Path) -> tuple[str, ...]:
    """agentix 顶层包名（排除 ``__pycache__`` 等双下划线目录）。"""
    return tuple(
        sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("__"))
    )


def _direct_modules(root: Path) -> tuple[str, ...]:
    """``agentix/*.py`` 直属模块名（不含 .py 后缀）。"""
    return tuple(sorted(p.stem for p in root.glob("*.py")))


def _source_node(relparts: tuple[str, ...]) -> str:
    """边的源点归属：``agentix/<pkg>/**`` → ``<pkg>``；直属文件 → ``__root__``。"""
    return relparts[0] if len(relparts) > 1 else ROOT_NODE


def _abs_target(mod: str, pkgs: frozenset[str], direct: frozenset[str]) -> Optional[str]:
    """把 ``agentix.<seg>...`` 绝对模块名映射到目标节点；非本仓 agentix 返回 None。"""
    if not mod.startswith("agentix."):
        return None
    seg = mod.split(".")[1]
    if seg in pkgs:
        return seg
    if seg in direct:
        # 指向直属模块（如 agentix.code_exec_runtime）的边归 __root__。
        return ROOT_NODE
    return None


def _relative_targets(
    relparts: tuple[str, ...],
    level: int,
    module: Optional[str],
    names: list[str],
    pkgs: frozenset[str],
    direct: frozenset[str],
) -> list[str]:
    """解析相对导入，返回其触达的目标节点列表（可能为空）。

    锚点 = 文件所在目录的完整点分名 ``agentix.<dirparts>``；``level`` 层往上溯
    ``level - 1`` 段。``from ..x import y`` 的 ``x`` 是模块、``y`` 通常是其属性，
    边粒度取 ``x``；``from .. import x`` 的每个 name 都按模块解析。
    """
    cp = ["agentix", *relparts[:-1]]  # 所在包（目录），不含文件名
    up = level - 1
    if up >= len(cp):  # 上溯越过仓库根——非法写法，不计
        return []
    base = cp[: len(cp) - up]
    raw: list[list[str]] = []
    if module:
        raw.append([*base, *module.split(".")])
    else:
        raw.extend([*base, name] for name in names)
    out: list[str] = []
    for cand in raw:
        if len(cand) < 2:
            continue  # 恰好解析到 agentix 自身，无具体目标
        out.append(_abs_target(".".join(cand), pkgs, direct) or "")
        if out[-1] == "":
            out.pop()
    return out


@dataclass(frozen=True)
class ScanResult:
    """一次全仓 AST 扫描的结果（全部按「源包 → 目标包」聚合，自环已剔除）。"""

    #: 包→包绝对导入直接边：(src, dst) → 出现该边的文件集合（posix 相对路径）。
    pkg_edges: dict[tuple[str, str], frozenset[str]]
    #: ``__root__`` 触达的包（装配根的出边，单独报告，不进层矩阵）。
    root_out: dict[str, frozenset[str]]
    #: 反向触达 ``__root__`` 的包（如 tools/application import agentix.code_exec_runtime）。
    root_in: dict[str, frozenset[str]]
    #: 跨包相对导入清单：(src, dst, 文件, 行号)。level>1 且解析后跨出本顶层包才计入。
    cross_pkg_relative_imports: tuple[tuple[str, str, str, int], ...]
    packages: tuple[str, ...]


def _parse(path: Path) -> Optional[ast.AST]:
    """与 p66 ``_parse`` 同一形态：语法坏的文件返回 None，扫描处跳过该文件。

    一个坏文件不该崩掉整个模块级夹具（p66 已如此处理，此处对齐，不自创第三种形态）。
    坏文件的兜底来源不是"导入层"——无人 import 的孤儿坏文件压根走不到导入层；真正的
    兜底是 CI 的 ``ruff check agentix scripts evals kb_ingest tests``（``.github/workflows``
    的 lint 步解析全部文件，``requirements-dev.txt`` 钉 ruff 0.5.7；实跑命令另带
    ``--output-format=github`` 输出格式旗标），外加本文件两条相等
    棘轮（坏文件导致已登记边消失即红）。判定逻辑与 p66 同源是刻意的对齐（见
    ``test_p66_arch_guards.py`` 的 ``_parse``），不改。
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def scan_agentix(root: Path = AGENTIX) -> ScanResult:
    """遍历 ``agentix/**/*.py`` 收集导入边。延迟 import 与 TYPE_CHECKING 块内 import 都算。"""
    pkgs = frozenset(_top_packages(root))
    direct = frozenset(_direct_modules(root))
    edges: dict[tuple[str, str], set[str]] = {}
    relcross: list[tuple[str, str, str, int]] = []

    def add_edge(src: str, dst: str, relf: str) -> None:
        edges.setdefault((src, dst), set()).add(relf)

    for f in sorted(root.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        relparts = f.relative_to(root).parts
        src = _source_node(relparts)
        relf = f.relative_to(REPO).as_posix()
        tree = _parse(f)
        if tree is None:  # 语法坏文件：跳过（与 p66 同形态，见 _parse）
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "agentix":
                    # `from agentix import <pkg>` 还原成 agentix.<pkg> 再判。
                    mods = [f"agentix.{alias.name}" for alias in node.names]
                else:
                    mods = [node.module]
            for m in mods:
                dst = _abs_target(m, pkgs, direct)
                if dst is None or dst == src:
                    continue
                add_edge(src, dst, relf)
            if isinstance(node, ast.ImportFrom) and node.level > 1:
                # §7.1 旧口径把相对导入当「必然同包」的前提；这里实测。
                names = [alias.name for alias in node.names]
                for dst in _relative_targets(
                    relparts, node.level, node.module, names, pkgs, direct
                ):
                    if dst == src:
                        continue
                    add_edge(src, dst, relf)
                    if src != ROOT_NODE and dst != ROOT_NODE:
                        relcross.append((src, dst, relf, node.lineno))

    return ScanResult(
        pkg_edges={k: frozenset(v) for k, v in edges.items() if ROOT_NODE not in k},
        root_out={
            dst: files for (s, dst), files in ((k, v) for k, v in edges.items()) if s == ROOT_NODE
        },
        root_in={
            src: files for (src, d), files in ((k, v) for k, v in edges.items()) if d == ROOT_NODE
        },
        cross_pkg_relative_imports=tuple(sorted(relcross)),
        packages=tuple(sorted(pkgs)),
    )


# ---------------------------------------------------------------------------
# 允许集（§7.3 第 1 步）——tests 侧常量，不进 runtime；逐行照抄 §1 归属表
# ---------------------------------------------------------------------------

#: 逐行照抄 §1「终态顶层包归属」表的「依赖方向」列。
#: 表内缺行的包（``config``、``domain``、``performance``）按实测/归属就近补录，
#: 补录处逐条注明（§1 表缺行，M1 补录）。
ALLOWED: dict[str, frozenset[str]] = {
    # §1：→ application / domain
    "interfaces": frozenset({"application", "domain"}),
    # §1：→ domain / orchestration / policies / tools
    "application": frozenset({"domain", "orchestration", "policies", "tools"}),
    # §1：→ domain（纯编排，不 import LangGraph 之外的上层）
    "orchestration": frozenset({"domain"}),
    # §1：→ domain
    "policies": frozenset({"domain"}),
    # §1：→ domain / policies（经 gateway）
    "tools": frozenset({"domain", "policies"}),
    # §1：→ domain only
    "agent": frozenset({"domain"}),
    # §1：→ runtime / domain（运行期自包含）。自环 "runtime" 不参与层矩阵，已剔除。
    "runtime": frozenset({"domain"}),
    # §1：→ tools / domain
    "capabilities": frozenset({"tools", "domain"}),
    # §1：域内自治 ⇒ 包级出边为空集。
    "retrieval": frozenset(),
    # §1：→ domain
    "infrastructure": frozenset({"domain"}),
    # §1：横切支撑行 → domain / config
    "observability": frozenset({"domain", "config"}),
    "reliability": frozenset({"domain", "config"}),
    "security": frozenset({"domain", "config"}),
    "crawler": frozenset({"domain", "config"}),
    # ---- §1 表缺行，M1 补录 ----
    # config：§1 表缺行，M1 补录（实测零出边；§2.3/§4「配置」是全层的被依赖方）。
    "config": frozenset(),
    # domain：§1 表缺行，M1 补录。允许集 = **空集**，依据 §2.3「零外部依赖（仅 stdlib +
    # typing）」——普适放行一维对 domain **不生效**（见 ``_universal_extra``：config 携带
    # pydantic，放行它等于把第三方依赖经传递方式带进零依赖底层）；实测零出边；R3 另有 p17 守卫。
    "domain": frozenset(),
    # performance：§1 表缺行，M1 补录（归属就近按 §1「横切支撑」行 → domain / config）。
    "performance": frozenset({"domain", "config"}),
}

#: 除 ``domain`` 外对**所有**包放行的一维：§4「配置」行明文「全层可读」；且 §1 横切
#: 支撑行本就列了 config。表内其它行未列 config，与 §4 冲突时 M1 以 §4 为准（已登记的
#: 口径缺口，汇报中已列出）。``domain`` 是唯一例外，判据见 ``_universal_extra``。
UNIVERSAL_ALLOWED: frozenset[str] = frozenset({"config"})


def _universal_extra(src: str) -> frozenset[str]:
    """普适放行一维：除 ``domain`` 外的包 = ``UNIVERSAL_ALLOWED``；``domain`` = 空集。

    特判依据（§2.3「零外部依赖」优先于 §4「配置全层可读」）：``domain`` 被 §2.3 声明为
    「零外部依赖（仅 stdlib + typing）」，而 ``agentix/config/settings.py`` 携带 pydantic
    ——把 ``config`` 放进 domain 的允许集，等于经**传递**方式把第三方依赖带进零依赖底层。

    这不是自创口径：p17 的 ``UPSTREAM_PREFIXES`` 不含 ``config``、``FORBIDDEN_THIRD_PARTY``
    只按第三方根包判 ⇒ ``domain → config`` 这条边在 p17 与 p177（改前）**双双无守卫接住**，
    p177 是本仓第一个接住它的地方——正是 §7.3 选闭包而非逐边检查要抓的那类「口径声明了、
    却没有任何执行点认领」的问题。判定链只有 ``_violation_edges``，普适放行维取自本函数
    （``_allowed_targets`` 死 helper 已删——M1 后续批 G3，零调用点；不许另写第二份判据）。
    """
    if src == "domain":
        return frozenset()
    return UNIVERSAL_ALLOWED


def _violation_edges(
    edges: Iterable[tuple[str, str]], allowed: dict[str, frozenset[str]]
) -> set[tuple[str, str]]:
    """纯判定：从直接边集中挑出「目标不在允许集」的违例边（__root__ 不参与）。"""
    out: set[tuple[str, str]] = set()
    for src, dst in edges:
        if src == ROOT_NODE or dst == ROOT_NODE:
            continue
        base = allowed.get(src, frozenset())
        if dst not in base and dst not in _universal_extra(src):
            out.add((src, dst))
    return out


# ---------------------------------------------------------------------------
# 违例边基线——键是直接违例边（dst ∉ 允许集），值是该边被容忍的实质理由。
# 理由必须点名口径：§4 横切允许面 / §7.5 的 M3 目标 / 既有环成员且 M3 前不动 /
# 归属表缺口 / 待维护者追认：<实质疑问>。相等棘轮：新增即红、消失亦红。
# ---------------------------------------------------------------------------

VIOLATION_BASELINE: dict[tuple[str, str], str] = {
    # ---- agent（§1：→ domain only）----
    ("agent", "application"): (
        "主会话已裁决：保留为基线例外，待拆除/待收敛，方向=终态经 domain 端口取产物；本批不改现状"
        "——agent/context/engine.py 延迟 import application.services.artifact_store（上下文装配取"
        "产物），artifact_store 终态归属 §1 未列（2026-10-29 裁决，属处置方式不是修复）"
    ),
    ("agent", "retrieval"): (
        "agent/memory/service.py 函数体内延迟 import retrieval.infra.rag.dense（长期记忆向量化）；"
        "§1 只约束 retrieval「域内自治」的出边，未定义谁可消费其 infra 面——归属表缺口"
    ),
    ("agent", "runtime"): (
        "agent/events/port.py 延迟 import runtime.lifecycle / runtime.state（事件端口复用运行态状态机词汇）；"
        "§2.3 的 domain/runtime 是 domain 内子包、并非顶层 runtime——归属表缺口"
    ),
    ("agent", "tools"): (
        "agent/context|hooks|skills 的 install.py 在装配期直连 tools.gateway.executor / registry_init "
        "注册工具面；§1 agent 行 → domain only——装配面（install）未从 agent 归属中拆出，归属表缺口"
    ),
    # ---- application（§1：→ domain / orchestration / policies / tools）----
    ("application", "agent"): (
        "services 与 compactor import agent.context / memory / events / skills / evolution；"
        "agent 是 Context Provider 家、除装配根外唯一合法调用方就是 application（§1 agent 行"
        "「→ domain only」已预设被 application 编排）——归属表 application 行未同步该方向，缺口"
    ),
    ("application", "infrastructure"): (
        "包级 37 条 import 中 30 条指 infrastructure/core（日志等横切工具，§4 明文允许面）；"
        "其余 7 条具体实现（provider_registry/model_gateway/checkpointer/repositories/http/"
        "step_ledger_store/wren_adapter）由 R2 守卫 test_p22 的文件级白名单另行判定——"
        "本守卫按包级边粒度登记，§4 口径只覆盖 core 子面，不构成整条包级边的授权"
    ),
    ("application", "observability"): (
        "llm_factory/router/services 共 22 条 import observability.spans / metrics——"
        "§4 把「指标埋点」列为横切关注点，§1 application 行未列 observability，归属表缺口"
    ),
    ("application", "performance"): (
        "chat_service 取 admission、run_event_stream 取 budget、task_queue 取 load_shedder；"
        "performance 在 §1 表缺行（补录仅 domain/config）——归属表缺口"
    ),
    ("application", "reliability"): (
        "llm_factory 用 concurrency_limiter、task_retry 用 retry_budget；reliability 是 §1 横切支撑、"
        "application 行未列——归属表缺口"
    ),
    ("application", "retrieval"): (
        "kg 组件与 knowledge_gateway 消费 retrieval.settings / retrieval_port_adapter；"
        "§1 的「域内自治」约束 retrieval 出边、未禁止被 application 消费——归属表缺口"
    ),
    ("application", "runtime"): (
        "chat_service 经 runtime.control_plane spawn/管理 run（p66 守卫 #1 的运行平面落点）；"
        "§1 application 行未列 runtime——归属表缺口"
    ),
    ("application", "security"): (
        "kb_tools/node.py import security.prompt_injection（图内工具节点做注入扫描）；"
        "security 属 §1 横切支撑、application 行未列——归属表缺口"
    ),
    # ---- capabilities（§1：→ tools / domain）----
    ("capabilities", "reliability"): (
        "capabilities/mcp/client.py 函数体内 import reliability.bulkhead（MCP 连接舱壁隔离）；"
        "§1 capabilities 行未列 reliability——归属表缺口"
    ),
    # ---- infrastructure（§1：→ domain）----
    ("infrastructure", "observability"): (
        "model_gateway / checkpointer import observability.metrics 埋点；§4 将指标埋点列为横切关注点，"
        "§1 infrastructure 行未列 observability——归属表缺口"
    ),
    # ---- interfaces（§1：→ application / domain）----
    ("interfaces", "agent"): (
        "ops_router / v1.events / v1.feedback import agent.events.port 与 agent.memory.service"
        "（事件流与反馈端点直读 provider 面）；§1 interfaces 行只放行 application/domain——"
        "事件端口词汇是否允许接口层直读需追认，暂以归属表缺口登记（既有环成员，M3 前不动）"
    ),
    ("interfaces", "infrastructure"): (
        "多为 infrastructure/core（logger/security/database，§4 横切允许面）；但 sessions.py import "
        "infrastructure.persistence.crud、chat.py import core.database 触到 §2.1 明文禁止面（直接 CRUD/"
        "直接操作 DB 会话）——R1/p22 无 interfaces 侧守卫覆盖，本守卫按包级粒度如实登记，"
        "文件级收紧不属 M1 权限"
    ),
    ("interfaces", "observability"): (
        "rate_limit_middleware / feedback import observability.metrics——§4 指标埋点横切面，归属表缺口"
    ),
    ("interfaces", "orchestration"): (
        "主会话已裁决：保留为基线例外，待拆除/待收敛，方向=应经 application 转发，不给接口层开"
        "「运维面可直连任何层」的口子；本批不改现状——ops_router 的图重建/调试端点 6 处直调 "
        "orchestration.langgraph.lg_builder（2026-10-29 裁决，属处置方式不是修复）"
    ),
    ("interfaces", "performance"): (
        "v1/runs.py 延迟 import performance.admission（读准入控制状态）；performance §1 缺行——归属表缺口"
    ),
    ("interfaces", "policies"): (
        "ops_router HITL 审批/裁决端点直调 policies.hitl.hitl_bridge / sidecar；policies 是 §1 横切"
        "策略执行件，但 interfaces 行未列该方向——归属表缺口"
    ),
    ("interfaces", "reliability"): (
        "rate_limit_middleware import reliability.rate_limiter（限流中间件复用限流实现）；"
        "§1 interfaces 行未列 reliability——归属表缺口"
    ),
    ("interfaces", "runtime"): (
        "v1/runs.py import runtime.control_plane（run 查询/取消端点对应 p166 功能）——归属表缺口"
    ),
    ("interfaces", "security"): (
        "deps / runs import security.jwt 做鉴权（§2.1 职责表明文「协议转换与鉴权」）；"
        "但 §1 依赖方向列未写 security——方向缺口"
    ),
    # ---- orchestration（§1：→ domain）----
    ("orchestration", "agent"): (
        "orchestrator import agent.hooks、lg_builder 取 agent.context.token_count（纯计数函数）；"
        "§1 orchestration 行 → domain——归属表缺口"
    ),
    ("orchestration", "application"): (
        "§7.5 点名的 27 条全仓最大反向边（lg_builder / lg_supervisor 的 10 个具体 handler 与 "
        "llm_factory、contract 的 artifact_store），M3 以节点注册协议拆除；M1 只如实冻结"
    ),
    ("orchestration", "observability"): (
        "lg_builder / manager import observability.spans / metrics——§4 指标埋点横切面，归属表缺口"
    ),
    ("orchestration", "policies"): (
        "lg_builder 延迟 import policies.hitl.hitl_bridge（图接 HITL 节点）；"
        "§1 orchestration 行未列 policies——归属表缺口"
    ),
    ("orchestration", "tools"): (
        "executor / orchestrator 取 tools.gateway 的 result_limiter / round_gate、lg_builder 取 "
        "registry_init（§1 tools 行「经 gateway」描述的是 tools→policies 反向，未定义本方向）"
        "——归属表缺口"
    ),
    # ---- performance（§1 缺行，补录为横切支撑 → domain/config）----
    ("performance", "observability"): (
        "admission / budget / load_shedder 埋点 observability.metrics——§4 指标埋点横切面"
    ),
    ("performance", "reliability"): (
        "load_shedder import reliability.circuit_breaker（按断路器状态决定卸荷）；performance 补录行"
        "（横切支撑）未覆盖该方向——归属表缺口"
    ),
    ("performance", "runtime"): (
        "admission 三处延迟 import runtime.control_plane（准入决策读写控制平面）；"
        "§1 runtime「运行期自包含」未说明可被 performance 反向调用——归属表缺口"
    ),
    # ---- policies（§1：→ domain）----
    ("policies", "application"): (
        "hitl_bridge 延迟 import application.services.breakpoint_resume、planner.llm import "
        "application.agents.llm_factory；M2-2 只下沉了纯作用域类型、服务实现仍留 application——"
        "既有环成员且 M3 前不动"
    ),
    ("policies", "infrastructure"): (
        "主会话已裁决：保留为基线例外，待拆除/待收敛，方向=应补 domain 端口并以实现注入；本批不改"
        "现状——hitl_bridge 函数体内直读 infrastructure.persistence.hitl_pending_store，按 R2/§2.4 "
        "口径持久化应经 domain 端口（2026-10-29 裁决，属处置方式不是修复）"
    ),
    ("policies", "observability"): (
        "hitl_bridge 埋点 observability.metrics——§4 指标埋点横切面，归属表缺口"
    ),
    ("policies", "runtime"): (
        "hitl_bridge import runtime.lifecycle / runtime.state（审批闸复用运行态状态机，"
        "与 agent→runtime 同源词汇）——归属表缺口"
    ),
    ("policies", "tools"): (
        "hitl_bridge 取 tools.definitions / gateway.human_review / executor；§7.1 已标该双向边"
        "「表中未定义该方向」，gateway 链的 human_review 闸即在此（§3.1 HITL 词命中）——归属表缺口"
    ),
    # ---- reliability（§1：→ domain / config）----
    ("reliability", "application"): (
        "retry_budget 函数体内 import application.services.request_scope——M2-2 已把作用域**类型**"
        "下沉 domain.request_scope、写入实现仍留 application；既有环成员且 M3 前不动"
    ),
    ("reliability", "observability"): (
        "circuit_breaker / concurrency_limiter 埋点 observability.metrics——§4 指标埋点横切面"
    ),
    # ---- retrieval（§1：域内自治，允许集为空 ∪ config）----
    ("retrieval", "domain"): (
        "retrieval 引用顶层 domain（错误词汇/端口；§2.3 明示 domain 是零外部依赖的词汇表层，"
        "§1 其余各行都放行 → domain）；唯独 retrieval 行只写「域内自治」未注明是否含 → domain"
        "——归属表缺口，按字面照抄登记"
    ),
    ("retrieval", "application"): (
        "usecases/api 复用 application 的 run_event_stream / stream_gate / artifact_store；"
        "§1 写「域内自治」而实际有出边，属最高优先级的已登记偏离——既有环成员，M3 前不动"
    ),
    ("retrieval", "infrastructure"): (
        "主会话已裁决：保留为基线例外，待拆除/待收敛，方向=7 条 import 中 model_gateway / "
        "cache.singleflight / infrastructure.http / core.database 应收敛进 retrieval 自带 infra/"
        "（core.logger×2 属 §4 允许面）；本批不改现状——「域内自治」字面禁一切出边、§4 只放行 core，"
        "两口径冲突已裁决按 §4 允许面处置、不放宽登记（2026-10-29 裁决，属处置方式不是修复）"
    ),
    ("retrieval", "observability"): (
        "埋点 observability.metrics——§4 指标埋点横切面；「域内自治」与 §4 横切口径的边界同上"
    ),
    ("retrieval", "performance"): (
        "chat_service 用 performance.admission 做准入/卸荷——归属表缺口（performance 缺行）"
    ),
    ("retrieval", "reliability"): (
        "api / usecases / infra 多处用 reliability 的 rate_limiter / circuit_breaker / bulkhead / "
        "retry_budget / concurrency_limiter（垂直切片复用横切支撑面）——归属表缺口"
    ),
    ("retrieval", "runtime"): (
        "usecases import runtime（run 生命周期与控制平面登记，对齐 p66 守卫 #1 单一运行平面）——"
        "归属表缺口"
    ),
    ("retrieval", "security"): (
        "api.deps / auth 用 security.jwt、index_worker / chat_output_filter / chat_service 用 "
        "prompt_injection 与 output_filter（鉴权与输出过滤复用横切支撑实现）——归属表缺口"
    ),
    ("retrieval", "tools"): (
        "chat_agent_runner 取 tools.gateway.round_gate（执行链复用工具网关轮次闸）——归属表缺口"
    ),
    # ---- runtime（§1：→ domain，运行期自包含）----
    ("runtime", "infrastructure"): (
        "control_plane 延迟 import infrastructure.persistence.run_state_store（p167 run 状态"
        "持久化的功能落点）；§1 runtime「运行期自包含」无 infrastructure——具体实现依赖，归属表缺口"
    ),
    ("runtime", "observability"): (
        "runtime.py 函数体内 import observability.trajectory（轨迹记录，M2-1 的配置上提落点）；"
        "§4 观测属横切关注点——归属表缺口"
    ),
    # ---- security（§1：→ domain / config）----
    ("security", "observability"): (
        "prompt_injection 埋点 observability.metrics——§4 指标埋点横切面"
    ),
    # ---- tools（§1：→ domain / policies）----
    ("tools", "infrastructure"): (
        "tools/adapters/search.py 只 import infrastructure.core（§6 决策树「跨层工具（日志/安全/"
        "限流）→ infrastructure/core」，§4 横切允许面按文件粒度覆盖本边全部 1 条）；"
        "包级边无法表达 core-only 约束，如实登记"
    ),
    ("tools", "observability"): (
        "gateway/executor 埋点 metrics 与 trajectory——§4 指标埋点横切面，归属表缺口"
    ),
    ("tools", "reliability"): (
        "gateway/executor 用 reliability.bulkhead / retry_budget（执行闸的舱壁与重试预算，"
        "属 M4-f 工具管道的功能依赖）——归属表缺口"
    ),
}


# ---------------------------------------------------------------------------
# 闭包基线（§7.3 第 3 步）——每包传递可达集快照（不含自身、不含 __root__）。
# equality 棘轮：变宽 ⇒ 红（挡住新债）；变窄 ⇒ 红并提示同步收紧（防腐化）。
# 基线键集 = 全部 17 个顶层包（零出边包的闭包为空集，也必须显式登记，
# 否则包被删/改名时守卫核不出来）。
# 注意：闭包会暴露间接越界（如 tools 闭包含 application 与 retrieval，各经
# tools → policies → application → retrieval；M2 只拆了直接边；本批按台账口径
# **不修**、如实冻结）。
# ---------------------------------------------------------------------------

CLOSURE_BASELINE: dict[str, frozenset[str]] = {
    "agent": frozenset(
        {
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "application": frozenset(
        {
            "agent",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "capabilities": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "config": frozenset(),
    "crawler": frozenset(),
    "domain": frozenset(),
    "infrastructure": frozenset({"config", "domain", "observability"}),
    "interfaces": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "observability": frozenset({"config"}),
    "orchestration": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "performance": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "policies": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "reliability",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "reliability": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "retrieval",
            "runtime",
            "security",
            "tools",
        }
    ),
    "retrieval": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "runtime",
            "security",
            "tools",
        }
    ),
    "runtime": frozenset({"config", "domain", "infrastructure", "observability"}),
    "security": frozenset({"config", "observability"}),
    "tools": frozenset(
        {
            "agent",
            "application",
            "config",
            "domain",
            "infrastructure",
            "observability",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "runtime",
            "security",
        }
    ),
}


# ---------------------------------------------------------------------------
# §7.1 正文细项（守卫 #6 判据的一侧；三行**计数**直接解析文档正文）
# ---------------------------------------------------------------------------

#: 守卫 #6 已升级（M1 后续批）：正文三行的**计数**从 ``docs/architecture/分层架构与依赖
#: 规则.md`` §7.1 实测快照块解析核对（fail-closed——抽不到即红，见 ``_doc_snapshot_counts``），
#: 本常量不再充当「待批改写文档的证据」。它保留的角色是判据的一侧：正文里也写了的**细项清单**
#: ——双向边清单、环成员名单、零出边包名清单。
#: 技术说明：文档旧快照「9 对双向边 / 12 包同环」系 M2 之前（§7.1 注明 2026-10-28）的读数，
#: M2 批次（M2-1/M2-2/M2-3/M2-4）拆除 observability↔retrieval、application↔tools、
#: application↔runtime 三条双向对后实测为 6 对。
DOC_CLAIMS: dict[str, object] = {
    # 实测 6 对双向边——清单与 §7.1 实测快照第一行的括号清单逐字对齐。
    "bidirectional_pairs": frozenset(
        {
            ("agent", "application"),
            ("application", "orchestration"),
            ("application", "policies"),
            ("application", "reliability"),
            ("application", "retrieval"),
            ("policies", "tools"),
        }
    ),
    # 实测：8 个包落在同一个强连通分量（文档 §7.1 旧快照列了 12 个）。infrastructure /
    # observability / runtime / security 不在环内的原因是**其出边的传递闭包触不到任何环成员**，
    # 不是「只有出边、无人回流」（observability 恰是被引用最重的包之一）——实测闭包：
    # infrastructure={config,domain,observability}、runtime={config,domain,infrastructure,
    # observability}、observability={config}、security={config,observability}，
    # 四者的闭包都不含任何环成员，故都不在环内。
    "scc_members": frozenset(
        {
            "agent",
            "application",
            "orchestration",
            "performance",
            "policies",
            "reliability",
            "retrieval",
            "tools",
        }
    ),
    # 零出边的只有 config、crawler、domain——§7.1 新旧声称在此项未变。
    "zero_out_packages": frozenset({"config", "crawler", "domain"}),
}

#: §7.1 所属文档（守卫 #6 的正文一侧；路径与 p41 的存活路径校验同源——本文档只写真存在的文件）。
ARCH_DOC = REPO / "docs" / "architecture" / "分层架构与依赖规则.md"

#: 快照块定位：标题含「实测快照」的四级标题起、到下一个三级标题止。
_SNAPSHOT_BLOCK_RE = re.compile(r"^####[^\n]*实测快照[^\n]*\n(.*?)(?=^### |\Z)", re.M | re.S)

#: 三行的字面形态（前缀 + 加粗数字），与 §7.1 改文**同批逐字对齐**（M1 后续批）。
_SNAPSHOT_LINES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "bidirectional",
        "`- 双向边：**N** 对`",
        re.compile(r"^- 双向边：\*\*(\d+)\*\* 对", re.M),
    ),
    (
        "scc",
        "`- 同一强连通分量：**N** 个包`",
        re.compile(r"^- 同一强连通分量：\*\*(\d+)\*\* 个包", re.M),
    ),
    (
        "zero_out",
        "`- 零出边：**N** 个包`",
        re.compile(r"^- 零出边：\*\*(\d+)\*\* 个包", re.M),
    ),
)


def _doc_snapshot_counts() -> tuple[dict[str, int], list[str]]:
    """解析 §7.1 实测快照块三行的计数，返回（计数, 未定位项清单）。

    fail-closed 是本次升级的全部意义：快照块整体缺失、或三行任一定位不到，都进未定位清单，
    由调用方以「判据与正文已脱钩」断红——**绝不允许**抽不到就跳过。
    """
    text = ARCH_DOC.read_text(encoding="utf-8")
    m = _SNAPSHOT_BLOCK_RE.search(text)
    if m is None:
        return {}, ["快照块（标题含「实测快照」的四级标题）整体未定位"]
    block = m.group(1)
    counts: dict[str, int] = {}
    missing: list[str] = []
    for key, label, pat in _SNAPSHOT_LINES:
        lm = pat.search(block)
        if lm is None:
            missing.append(label)
        else:
            counts[key] = int(lm.group(1))
    return counts, missing


# ---------------------------------------------------------------------------
# 图算法（纯函数，无副作用；SCC 用迭代式 Tarjan，避免深递归）
# ---------------------------------------------------------------------------


def _adjacency(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for src, dst in edges:
        adj.setdefault(src, set()).add(dst)
    return adj


def _transitive_closure(
    adj: dict[str, set[str]], all_nodes: Iterable[str]
) -> dict[str, frozenset[str]]:
    """每个节点的传递可达集（不含自身、不含 ``__root__``）。

    零出边节点也要有键（空集）——否则基线键集会随「无出边」漂移而核不出来。
    """
    out: dict[str, frozenset[str]] = {}
    for start in sorted(all_nodes):
        if start == ROOT_NODE:
            continue
        seen: set[str] = set()
        stack = [start]
        while stack:
            n = stack.pop()
            for m in adj.get(n, set()):
                if m != ROOT_NODE and m not in seen:
                    seen.add(m)
                    stack.append(m)
        seen.discard(start)
        seen.discard(ROOT_NODE)
        out[start] = frozenset(seen)
    return out


def _strong_components(nodes: Iterable[str], adj: dict[str, set[str]]) -> list[list[str]]:
    """迭代式 Tarjan。节点是**包名字符串**；邻接表按包聚合，不存在
    「把字符串当节点集合」的逐字符展开问题（nodes 显式传入全部顶层包）。"""
    index_counter = [0]
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    for root in sorted(nodes):
        if root in idx:
            continue
        work: list[tuple[str, Iterator[str]]] = []
        idx[root] = low[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        work.append((root, iter(sorted(adj.get(root, set())))))
        while work:
            v, it = work[-1]
            pushed = False
            for w in it:
                if w == ROOT_NODE:
                    continue
                if w not in idx:
                    idx[w] = low[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(adj.get(w, set())))))
                    pushed = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], idx[w])
            if pushed:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == idx[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                result.append(comp)
    return result


def _bidirectional_pairs(edges: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    eset = {e for e in edges if ROOT_NODE not in e}
    return {(min(s, d), max(s, d)) for (s, d) in eset if (d, s) in eset}


def _zero_out_packages(
    edges: Iterable[tuple[str, str]], all_packages: Iterable[str]
) -> frozenset[str]:
    srcs = {s for (s, _d) in edges if s != ROOT_NODE}
    return frozenset(p for p in all_packages if p not in srcs)


def _diff_violations(
    actual: set[tuple[str, str]], registered: Iterable[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """纯判定：返回（新增未登记的违例边, 已登记但实际消失的违例边）。

    两个方向都非空即失败——前者挡新债，后者防白名单只增不减（§7.3 第 4 步）。
    """
    reg = set(registered)
    return sorted(actual - reg), sorted(reg - actual)


# ---------------------------------------------------------------------------
# 守卫
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scan() -> ScanResult:
    """全仓 AST 扫描只做一次（模块级夹具；函数体/TYPE_CHECKING 内的 import 都算边）。"""
    return scan_agentix()


def test_direct_edges_covered_or_allowed(scan: ScanResult) -> None:
    """守卫 #1：实际直接违例边集 == VIOLATION_BASELINE 的键集（相等，非 ⊆）。

    新增一条越界边 ⇒ 红；登记过但实际已消失的边 ⇒ 同样红（登记不许腐化）。
    """
    actual = _violation_edges(scan.pkg_edges, ALLOWED)
    added, vanished = _diff_violations(actual, VIOLATION_BASELINE)
    assert not added and not vanished, (
        "违例边基线漂移："
        f"\n  新增未登记={[f'{s} -> {d}' for s, d in added]}"
        f"\n  已登记但消失={[f'{s} -> {d}' for s, d in vanished]}"
        "\n  新增边必须补登记（带实质理由）或拆掉；消失边必须从基线删除。"
    )


#: 占位符禁词：空串与各类「没写字由」的形态。
_PLACEHOLDERS = {"", "TODO", "todo", "待定", "待维护者追认", "待维护者追认：", "N/A"}


def test_every_exception_has_a_reason() -> None:
    """守卫 #2（§7.3 第 4 步）：每条登记的违例边必须写实质理由，不许通用套话。

    允许 ``待维护者追认：<实质疑问>`` 形态——冒号后必须有真内容；裸的
    ``待维护者追认`` 视为占位。带追认条数在此打印，汇报时逐条列出。
    """
    assert VIOLATION_BASELINE, "基线不应为空——为空则守卫 #1 失去意义"
    bad: list[str] = []
    pending: list[str] = []
    for (src, dst), reason in VIOLATION_BASELINE.items():
        text = reason.strip()
        if text in _PLACEHOLDERS:
            bad.append(f"{src} -> {dst}：理由为空或占位")
            continue
        if text.startswith("待维护者追认"):
            rest = text.split("：", 1)
            if len(rest) < 2 or len(rest[1].strip()) < 10:
                bad.append(f"{src} -> {dst}：追认条目冒号后必须有实质疑问")
            else:
                pending.append(f"{src} -> {dst}")
    assert not bad, f"违例边理由不合格：{bad}"
    print(
        f"\n[p177] 违例边基线共 {len(VIOLATION_BASELINE)} 条，"
        f"其中待维护者追认 {len(pending)} 条："
    )
    for name in pending:
        print(f"  - 待维护者追认：{name}")


def test_closure_snapshot_matches_reality(scan: ScanResult) -> None:
    """守卫 #3：实际传递闭包 == CLOSURE_BASELINE（相等棘轮，双向都红）。

    变宽 ⇒ 红（新增越界，含间接越界）；变窄 ⇒ 红并提示同步收紧基线。
    基线**刻意不等于允许集**：过渡期间接越界如实冻结（如 tools 闭包含
    application，经 tools → policies → application；§1 表未定义该方向、M2 只拆了直接边）。
    """
    actual = _transitive_closure(_adjacency(scan.pkg_edges.keys()), scan.packages)
    expected = CLOSURE_BASELINE
    widened = {
        p: sorted(actual[p] - expected[p])
        for p in actual
        if p in expected and actual[p] - expected[p]
    }
    narrowed = {
        p: sorted(expected[p] - actual[p])
        for p in expected
        if p in actual and expected[p] - actual[p]
    }
    missing = sorted(set(actual) ^ set(expected))
    assert not widened and not narrowed and not missing, (
        "闭包基线漂移（闭包暴露**间接**越界，比逐边检查更能挡住白名单悄悄变宽，§7.3）："
        f"\n  变宽={widened}\n  变窄={narrowed}\n  键集差异={missing}"
        "\n  变窄时收紧 CLOSURE_BASELINE 是**唯一**合法方向；变宽必须解释并送评审。"
    )


def test_intruder_edge_and_vanished_edge_are_caught() -> None:
    """守卫 #4：对纯判定函数做合成用例——两个方向的漂移都必须被抓到。

    这是路线图 M1 验收「人为新增一条越界 import 时该守卫立刻红」的**等价自证**，
    用合成数据自证判定逻辑，不改产品代码做实验。
    """
    registered = {("alpha", "beta"), ("gamma", "delta")}
    # 注入一条不存在的边 ⇒ 判为「新增」。
    added, vanished = _diff_violations(registered | {("intruder", "victim")}, registered)
    assert added == [("intruder", "victim")], f"注入边未被判为新增：{added}"
    assert vanished == []
    # 删掉一条基线边（现状修好了但没改登记）⇒ 判为「已消失」。
    added, vanished = _diff_violations(registered - {("gamma", "delta")}, registered)
    assert vanished == [("gamma", "delta")], f"消失边未被判为已消失：{vanished}"
    assert added == []
    # 允许集判定同样双向：合成 allowed 下违例集非空、扩大允许集后为空。
    allowed_small = {"alpha": frozenset(), "gamma": frozenset()}
    allowed_big = {"alpha": {"beta"}, "gamma": {"delta"}}
    assert _violation_edges(registered, allowed_small) == registered
    assert _violation_edges(registered, {k: frozenset(v) for k, v in allowed_big.items()}) == set()
    # domain 回归钉（M1 后续批 G4，第 2 轮 reviewer 判过的真实缺口）：``_universal_extra`` 里
    # ``if src == "domain"`` 的特判**没有这条就会被删整套仍绿**——§2.3「零外部依赖优先于 §4
    # 全层可读」这条裁决就失去用例保护。两条必须一起看：``domain → config`` 判**违例**（特判
    # 生效），``tools → config`` 判**放行**（普适维对普通包照常生效，防有人把特判扩成禁全体）。
    assert ("domain", "config") in _violation_edges([("domain", "config")], ALLOWED), (
        "domain → config 被放行——_universal_extra 的 domain 特判被删或放宽（§2.3 零外部依赖口径失守）"
    )
    assert ("tools", "config") not in _violation_edges([("tools", "config")], ALLOWED), (
        "tools → config 被判违例——普适放行维失效（§4「配置全层可读」口径失守）"
    )


def test_no_cross_package_relative_imports(scan: ScanResult) -> None:
    """守卫 #5：实测跨包相对导入必须为 0。

    §7.1 把「相对导入必然同包」当口径前提；本断言把它**升级为实测**——
    一旦有人写出 ``from ..other_pkg import x``（level>1 且上溯出本包），立即红。
    """
    offenders = [
        f"{src} -> {dst}（{file}:{line}）"
        for src, dst, file, line in scan.cross_pkg_relative_imports
    ]
    assert not offenders, f"出现跨包相对导入，绕过绝对导入边矩阵：{offenders}"


def test_doc_claims_match_matrix(scan: ScanResult) -> None:
    """守卫 #6（M1 后续批升级为解析正文）：§7.1 实测快照块三行计数 == 计算器实测。

    旧形态只核对测试侧常量 ``DOC_CLAIMS`` 与实测，**锁不住文档正文**——正文漂移
    （只改文档不改常量）时本守卫恒绿。本批与 §7.1 改文**同批**升级为直接解析
    ``docs/architecture/分层架构与依赖规则.md`` §7.1 实测快照块：
    **fail-closed**——快照块整体、或三行中任何一行定位不到，都以「判据与正文已脱钩」
    断言失败，绝不抽不到就跳过（静默跳过等于升级白做）。
    ``DOC_CLAIMS`` 保留，角色收窄为判据的一侧：正文里也写了的细项清单（双向边清单、
    环成员名单、零出边包名清单）。

    对照三项（计数与清单两侧都比）：双向边对数、同一强连通分量的成员、零出边的包。
    另断言 ALLOWED 键集 == 实测顶层包集（§1 表若新增/删除包，本文件必须同批改）。
    """
    assert set(ALLOWED) == set(scan.packages), (
        f"ALLOWED 键集与实测顶层包不一致："
        f"缺={sorted(set(scan.packages) - set(ALLOWED))} "
        f"多={sorted(set(ALLOWED) - set(scan.packages))}"
    )
    adj = _adjacency(scan.pkg_edges)
    bidi = _bidirectional_pairs(scan.pkg_edges.keys())
    sccs = _strong_components(scan.packages, adj)
    # 断言形态：非平凡（len>1）强连通分量**恰为一个**且等于声称环——不能只取第一个
    # 非平凡分量比对（若将来拓扑变成两个环、其一恰等于声称集，另一个会漏检）。
    nontrivial = [frozenset(c) for c in sccs if len(c) > 1]
    zero = _zero_out_packages(scan.pkg_edges.keys(), scan.packages)

    counts, missing = _doc_snapshot_counts()
    assert not missing, (
        "文档 §7.1 的实测快照三行未能定位 ⇒ 判据与正文已脱钩"
        f"（未定位项={missing}）。fail-closed，不允许抽取失败后跳过：快照块被删除/改名、"
        "或三行的固定前缀形态被改动，都必须在红里被看到；改正文数字必须同批改本判据。"
    )
    # 计数侧：正文三行的数字 vs 计算器实测。
    assert counts["bidirectional"] == len(bidi), (
        f"§7.1 正文声称双向边 {counts['bidirectional']} 对，实测 {len(bidi)} 对；"
        f"实测清单={sorted(bidi)}"
    )
    assert len(nontrivial) == 1, (
        f"实测非平凡强连通分量应恰为一个，实测 {len(nontrivial)} 个："
        f"分量={[sorted(c) for c in nontrivial]}（正文与环成员名单的核对以下一条为准）"
    )
    assert counts["scc"] == len(nontrivial[0]), (
        f"§7.1 正文声称同一强连通分量 {counts['scc']} 个包，实测 {len(nontrivial[0])} 个；"
        f"实测={sorted(nontrivial[0])}"
    )
    assert counts["zero_out"] == len(zero), (
        f"§7.1 正文声称零出边 {counts['zero_out']} 个包，实测 {len(zero)} 个；实测={sorted(zero)}"
    )
    # 细项清单侧：DOC_CLAIMS（正文同样列出的名单）与实测相等。
    assert DOC_CLAIMS["bidirectional_pairs"] == frozenset(bidi), (
        f"双向边清单与实测不符：声称={sorted(DOC_CLAIMS['bidirectional_pairs'])}，"
        f"实测={sorted(bidi)}"
    )
    assert nontrivial == [DOC_CLAIMS["scc_members"]], (
        f"非平凡强连通分量与声称不符：应恰为一个环 {sorted(DOC_CLAIMS['scc_members'])}，"
        f"实测 {len(nontrivial)} 个非平凡分量={[sorted(c) for c in nontrivial]}"
    )
    assert (
        DOC_CLAIMS["zero_out_packages"] == zero
    ), f"零出边包不符：声称={sorted(DOC_CLAIMS['zero_out_packages'])}，实测={sorted(zero)}"
