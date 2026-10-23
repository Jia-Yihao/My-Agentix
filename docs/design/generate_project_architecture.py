"""生成 My-Agentix 项目整体架构图。

输出:
    docs/assets/project-architecture.png   —— 完整项目架构（默认主链路 + 可选能力 + 横切）
    docs/assets/project-architecture.svg   —— 同图矢量版

用法:
    python docs/design/generate_project_architecture.py

依赖:
    matplotlib；中文字体使用 C:/Windows/fonts/msyh.ttc（微软雅黑）。
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

FONT_PATH = "C:/Windows/fonts/msyh.ttc"
fm.fontManager.addfont(FONT_PATH)
_prop = fm.FontProperties(fname=FONT_PATH)
plt.rcParams["font.family"] = _prop.get_name()
plt.rcParams["axes.unicode_minus"] = False

OUT_PNG = "docs/assets/project-architecture.png"
OUT_SVG = "docs/assets/project-architecture.svg"

# ------------------------------------------------------------------ 配色 ----
C_TITLE = "#163A5F"
C_BG = "#FBFCFE"
C_CLIENT = "#DCEBF7"
C_CLIENT_E = "#3D7FB5"
C_EDGE = "#D9E4F2"
C_EDGE_E = "#365E8C"
C_ROUTE = "#E4EDF8"
C_ROUTE_E = "#4A6FA5"
C_APP = "#DCEFE8"
C_APP_E = "#2F8A70"
C_ORCH = "#E9E1F5"
C_ORCH_E = "#6B4FA0"
C_RUNTIME = "#E3E8F8"
C_RUNTIME_E = "#4D5FA8"
C_CAPS = "#FBE9D2"
C_CAPS_E = "#B1782B"
C_DATA = "#EFE8D8"
C_DATA_E = "#8A6D3B"
C_X = "#EDEFF2"
C_X_E = "#5B6673"
C_OPT = "#FFF3E2"
C_OPT_E = "#D07A1E"
C_ARROW = "#46566B"
C_SSE = "#2F7ED8"
C_OBS = "#7A8797"
C_TEXT = "#1F2937"
C_MUTED = "#526173"
C_WHITE = "#FFFFFF"

X0, X1 = 8.0, 145.0
RAIL_X, RAIL_W = 147.0, 11.5


def box(
    ax,
    x,
    y,
    w,
    h,
    title,
    body=(),
    fc=C_WHITE,
    ec=C_ARROW,
    ls="-",
    lw=1.1,
    title_size=9.6,
    body_size=7.4,
    title_color=C_TEXT,
    body_color=C_MUTED,
    z=4,
    alpha=1.0,
):
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.12,rounding_size=0.55",
        facecolor=fc,
        edgecolor=ec,
        linewidth=lw,
        linestyle=ls,
        alpha=alpha,
        zorder=z,
    )
    ax.add_patch(patch)
    ax.text(
        x + w / 2,
        y + h - 1.05,
        title,
        ha="center",
        va="center",
        fontsize=title_size,
        fontweight="bold",
        color=title_color,
        zorder=z + 1,
    )
    if body:
        ax.text(
            x + w / 2,
            y + (h - 1.55) / 2,
            "\n".join(body),
            ha="center",
            va="center",
            fontsize=body_size,
            color=body_color,
            linespacing=1.38,
            zorder=z + 1,
        )


def group(ax, x, y, w, h, label, fc, ec, ls="--", lw=1.0, label_size=8.2, z=2):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.15,rounding_size=0.7",
            facecolor=fc,
            edgecolor=ec,
            linewidth=lw,
            linestyle=ls,
            alpha=0.42,
            zorder=z,
        )
    )
    ax.text(
        x + 0.8,
        y + h - 0.75,
        label,
        ha="left",
        va="center",
        fontsize=label_size,
        fontweight="bold",
        color=ec,
        zorder=z + 1,
    )


def band(ax, y, h, color):
    ax.add_patch(
        Rectangle(
            (X0, y),
            RAIL_X - X0 - 1.0,
            h,
            facecolor=color,
            edgecolor="none",
            alpha=0.30,
            zorder=0,
        )
    )


def arrow(ax, start, end, color=C_ARROW, lw=1.15, ls="-", rad=0.0, z=3, head=True):
    style = "-|>" if head else "-"
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle=style,
            mutation_scale=11,
            color=color,
            linewidth=lw,
            linestyle=ls,
            connectionstyle=f"arc3,rad={rad}",
            zorder=z,
        )
    )


def poly_arrow(ax, points, color=C_ARROW, lw=1.15, ls="-", z=3, head=True):
    for i in range(len(points) - 2):
        ax.add_line(
            Line2D(
                [points[i][0], points[i + 1][0]],
                [points[i][1], points[i + 1][1]],
                color=color,
                linewidth=lw,
                linestyle=ls,
                zorder=z,
                solid_capstyle="round",
            )
        )
    arrow(ax, points[-2], points[-1], color=color, lw=lw, ls=ls, z=z, head=head)


def badge(ax, x, y, text, color=C_ARROW, size=8.6):
    ax.add_patch(
        plt.Circle((x, y), 0.85, facecolor=C_WHITE, edgecolor=color, linewidth=1.0, zorder=8)
    )
    ax.text(x, y, text, ha="center", va="center", fontsize=size, color=color,
            fontweight="bold", zorder=9)


def layer_label(ax, y, text):
    ax.text(
        1.2,
        y,
        text,
        ha="left",
        va="center",
        fontsize=9.6,
        fontweight="bold",
        color=C_TITLE,
        rotation=90,
        zorder=6,
    )


def main() -> None:
    fig, ax = plt.subplots(figsize=(32, 20), dpi=160)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    ax.set_xlim(0, 160)
    ax.set_ylim(0, 100)
    ax.set_aspect("equal")
    ax.axis("off")

    # ------------------------------------------------------------ 标题 ----
    ax.text(
        80,
        97.7,
        "My Agentix · 项目整体架构图",
        ha="center",
        va="center",
        fontsize=24,
        fontweight="bold",
        color=C_TITLE,
    )
    ax.text(
        80,
        95.45,
        "测试数据集知识库 + 多智能体问答平台 ｜ 默认主链：Vue3 → FastAPI → ChatService → LangGraph L2 → Tool / Retrieval → 数据层",
        ha="center",
        va="center",
        fontsize=10.2,
        color=C_MUTED,
    )

    # ------------------------------------------------------------ 图例 ----
    ly = 93.35
    ax.add_line(Line2D([9.5, 13.5], [ly, ly], color=C_ARROW, lw=1.6))
    ax.text(14.2, ly, "默认主链路", fontsize=8.3, va="center", color=C_TEXT)
    ax.add_line(Line2D([26.5, 30.5], [ly, ly], color=C_OPT_E, lw=1.5, ls="--"))
    ax.text(31.2, ly, "默认关闭 / 可选 / 未接线", fontsize=8.3, va="center", color=C_TEXT)
    ax.add_line(Line2D([52.5, 56.5], [ly, ly], color=C_SSE, lw=1.5, ls=":"))
    ax.text(57.2, ly, "SSE 流式 / 观测旁路", fontsize=8.3, va="center", color=C_TEXT)
    badge(ax, 84.0, ly, "1", size=7.4)
    ax.text(85.2, ly, "请求顺序", fontsize=8.3, va="center", color=C_TEXT)
    ax.text(
        159.2,
        93.35,
        "v0.2.0 · 2026-10-22",
        fontsize=8.0,
        va="center",
        ha="right",
        color=C_MUTED,
    )

    # ------------------------------------------------------------ 分层带 ----
    bands = {
        "clients": (84.5, 8.0, C_CLIENT),
        "edge": (73.5, 9.5, C_EDGE),
        "routes": (63.0, 9.0, C_ROUTE),
        "app": (51.0, 10.5, C_APP),
        "orch": (36.0, 13.5, C_ORCH),
        "runtime": (23.0, 11.5, C_RUNTIME),
        "caps": (11.0, 10.5, C_CAPS),
        "data": (1.0, 8.5, C_DATA),
    }
    for y, h, color in bands.values():
        band(ax, y, h, color)

    layer_label(ax, 88.5, "① 客户端")
    layer_label(ax, 78.25, "② 接入")
    layer_label(ax, 67.5, "③ 接口")
    layer_label(ax, 56.25, "④ 应用")
    layer_label(ax, 42.75, "⑤ 编排")
    layer_label(ax, 28.75, "⑥ 运行时")
    layer_label(ax, 16.25, "⑦ 能力")
    layer_label(ax, 5.25, "⑧ 数据")

    # 编排域 / 检索域 分隔线
    ax.add_line(
        Line2D(
            [102.2, 102.2],
            [1.0, 92.5],
            color="#B9C2CE",
            linewidth=0.9,
            linestyle=(0, (2, 3)),
            zorder=1,
        )
    )
    ax.text(102.2, 92.35, "编排域", ha="right", va="center", fontsize=7.1, color="#6B7788")
    ax.text(103.2, 92.35, "检索域", ha="left", va="center", fontsize=7.1, color="#6B7788")

    # ============================================================ 客户端 ====
    box(ax, 9.5, 85.6, 31, 5.7, "Web 对话端 · Vue3",
        ["ChatWidget / 会话历史 / 反馈", "SSE 流式渲染"], C_CLIENT, C_CLIENT_E)
    box(ax, 42.0, 85.6, 31, 5.7, "Web 知识端 · Vue3",
        ["知识库 / 文档 / 向量库", "检索问答 · 引用展示"], C_CLIENT, C_CLIENT_E)
    box(ax, 74.5, 85.6, 31, 5.7, "外部 API / 脚本",
        ["JWT / API Key / Run API", "同步调用 / SSE 订阅"], C_CLIENT, C_CLIENT_E)
    box(ax, 107.0, 85.6, 37.0, 5.7, "MCP 客户端 / 独立 CLI",
        ["mcp server · REPL · 开发工具", "非主链路"], C_OPT, C_OPT_E, ls="--")

    # ============================================================ 接入层 ====
    box(ax, 9.5, 75.0, 25, 6.8, "Nginx :8080",
        ["compose profile=full", "同源反代 /api → backend"], C_EDGE, C_EDGE_E)
    box(ax, 36.0, 75.0, 27, 6.8, "FastAPI :8000",
        ["uvicorn · lifespan 装配", "路由挂载 · /uploads 静态"], C_EDGE, C_EDGE_E)
    box(ax, 65.0, 75.0, 49, 6.8, "中间件链（外 → 内）",
        ["GlobalRateLimit（默认关）→ TraceId → RequestLogging + Prometheus",
         "APIKey（默认关）→ CORS → 路由"], C_EDGE, C_EDGE_E, body_size=7.1)
    box(ax, 116.0, 75.0, 28, 6.8, "运维与文档端点",
        ["/health · /liveness · /readiness", "/metrics · /docs · /openapi.json"],
        C_EDGE, C_EDGE_E, body_size=7.0)

    # ============================================================ 接口层 ====
    group(ax, 9.0, 63.0, 92.5, 8.2, "编排域 · /api/v1", C_ROUTE, C_ROUTE_E)
    box(ax, 10.5, 65.2, 26, 4.4, "/chat · /chat/stream",
        ["主问答（LangGraph）", "SSE: message/metadata/tool/done"], C_WHITE, C_ROUTE_E,
        title_size=8.6, body_size=6.8)
    box(ax, 38.0, 65.2, 27, 4.4, "/sessions · /upload",
        ["/events · /feedback", "会话 / 上传 / 事件 / 反馈"], C_WHITE, C_ROUTE_E,
        title_size=8.6, body_size=6.8)
    box(ax, 66.5, 65.2, 33.0, 4.4, "/agents/runs · /ops · /lightrag",
        ["Run API · 取消 · 恢复", "HITL 运维 · LightRAG 路由"], C_WHITE, C_ROUTE_E,
        title_size=8.6, body_size=6.8)
    ax.text(10.8, 63.7, "鉴权：APIKey / JWT · thread_id=session_id", fontsize=6.6,
            va="center", color=C_ROUTE_E)

    group(ax, 103.0, 63.0, 42.0, 8.2, "检索域 · /api/v1/retrieval（JWT 强制）", C_ROUTE, C_ROUTE_E)
    box(ax, 104.5, 66.0, 19.0, 3.4, "/chat · /chat/stream",
        ["检索问答 · SSE"], C_WHITE, C_ROUTE_E, title_size=8.4, body_size=6.8)
    box(ax, 124.5, 66.0, 19.0, 3.4, "/kb · /documents",
        ["/index-jobs · /folders"], C_WHITE, C_ROUTE_E, title_size=8.4, body_size=6.8)
    ax.text(104.8, 64.2,
            "/auth · /users · /tenants · /conversations · /feedback · /approvals · /explanations · /settings · /vector-db",
            fontsize=6.1, va="center", color=C_ROUTE_E)

    # ============================================================ 应用层 ====
    group(ax, 9.0, 51.8, 92.5, 9.2, "应用服务层 · 测试数据编排", C_APP, C_APP_E)
    box(ax, 10.5, 52.5, 36.0, 7.4, "ChatService",
        ["会话 / 记忆 / 指令装配 · 图调用", "SSE 事件门控 · 错误分类 · 落库"],
        C_WHITE, C_APP_E, body_size=7.0)
    box(ax, 47.5, 52.5, 25.0, 7.4, "Session / Memory",
        ["会话历史 · 长时记忆", "Redis 持久化 · 上下文压缩"],
        C_WHITE, C_APP_E, body_size=6.9)
    box(ax, 73.5, 52.5, 26.5, 7.4, "Run / Feedback / Ops",
        ["ControlPlane 登记 / 取消", "反馈 · HITL 恢复 · 轨迹查询"],
        C_WHITE, C_APP_E, body_size=6.9)

    group(ax, 103.0, 51.8, 42.0, 9.2, "应用服务层 · 检索域", C_APP, C_APP_E)
    box(ax, 104.5, 52.5, 22.0, 7.4, "Retrieval ChatService",
        ["RAG 问答 · 引用 · SSE", "缓存 · 输出过滤 · 上下文"],
        C_WHITE, C_APP_E, body_size=6.9)
    box(ax, 127.5, 56.3, 16.0, 3.5, "KB / 文档 / 索引",
        ["上传 · 解析 · 索引任务"], C_WHITE, C_APP_E, title_size=8.0, body_size=6.6)
    box(ax, 127.5, 52.3, 16.0, 3.5, "kb_ingest :8100",
        ["独立入库服务"], C_WHITE, C_APP_E, title_size=8.0, body_size=6.6, lw=1.5)

    # ============================================================ 编排层 ====
    group(ax, 9.0, 36.5, 92.5, 12.5, "编排层 · LangGraph L2 主图（默认）", C_ORCH, C_ORCH_E)
    group(ax, 10.5, 36.8, 73.0, 10.6, "StateGraph · AgentState ｜ thread_id=session_id",
          C_WHITE, C_ORCH_E, ls=":", lw=0.9, label_size=6.9, z=3)

    box(ax, 11.5, 44.0, 16.0, 2.8, "message_window",
        ["历史窗口 / token 预算"], C_WHITE, C_ORCH_E, title_size=7.6, body_size=6.1)
    box(ax, 29.5, 44.0, 21.0, 2.8, "analyze_and_route_query",
        ["L1 主路由：启发式 + LLM"], C_WHITE, C_ORCH_E, title_size=7.6, body_size=6.1)
    box(ax, 52.5, 44.0, 13.0, 2.8, "route_query",
        ["条件边 · L1 分发"], C_WHITE, C_ORCH_E, title_size=7.6, body_size=6.1)
    box(ax, 67.0, 44.0, 15.0, 2.8, "Checkpointer（持久化）",
        ["Redis → MemorySaver 降级 · 非图节点"], C_WHITE, C_ORCH_E,
        title_size=7.1, body_size=5.8)

    box(ax, 11.5, 39.8, 14.5, 3.1, "general / additional",
        ["直接回答 / 补充信息"], C_WHITE, C_ORCH_E, title_size=7.2, body_size=6.0)
    box(ax, 27.5, 39.8, 12.5, 3.1, "kb 子图",
        ["L2 多工具工作流"], C_WHITE, C_ORCH_E, title_size=7.4, body_size=6.0)
    box(ax, 41.5, 39.8, 18.0, 3.1, "research_plan / KG 子图",
        ["L2 图谱 + Text2SQL"], C_WHITE, C_ORCH_E, title_size=7.4, body_size=6.0)
    box(ax, 61.0, 39.8, 10.0, 3.1, "image / file",
        ["多模态 / 文件"], C_WHITE, C_ORCH_E, title_size=7.4, body_size=6.0)
    box(ax, 72.5, 39.8, 9.5, 3.1, "execute_task",
        ["TASK_EXEC 可选"], C_OPT, C_OPT_E, ls="--", title_size=7.2, body_size=5.9)

    ax.text(42.0, 38.15,
            "KB: guardrails → kb_router → local_search / external_search → finalize   ｜   "
            "KG: guardrails → planner → tool_selection → 并行工具 → summarize → final_answer_guard → final_answer",
            ha="center", va="center", fontsize=5.9, color=C_ORCH_E)
    ax.text(42.0, 37.55,
            "9 节点：message_window · analyze_and_route_query · respond_to_general_query · get_additional_info · create_kb_query · "
            "create_research_plan · create_image_query · create_file_query · execute_task",
            ha="center", va="center", fontsize=5.7, color=C_ORCH_E)
    ax.text(42.0, 37.05,
            "route_query 是条件边（非节点）；check_hallucinations 已定义但故意未注册",
            ha="center", va="center", fontsize=5.7, color=C_ORCH_E)

    box(ax, 85.0, 43.8, 15.5, 3.0, "Supervisor 多智能体",
        ["Manager + Peer（可选）"], C_OPT, C_OPT_E, ls="--",
        title_size=7.2, body_size=6.0)
    box(ax, 85.0, 39.8, 15.5, 3.0, "AgentLoop L3",
        ["AGENT_LOOP_ENABLED"], C_OPT, C_OPT_E, ls="--",
        title_size=7.2, body_size=6.0)
    ax.text(85.8, 37.9, "AGENTIX_ABLATION_MULTI_AGENT / AGENT_MAX_REPLANS",
            fontsize=5.6, va="center", color=C_OPT_E)

    group(ax, 103.0, 36.5, 42.0, 12.5, "编排层 · 检索域（独立 ReAct）", C_ORCH, C_ORCH_E)
    box(ax, 104.5, 43.8, 39.0, 3.6, "ReAct Runner（无 LangGraph）",
        ["LLM tool-call 轮次 · 并行工具执行", "流式 answer_chunk / progress / meta"],
        C_WHITE, C_ORCH_E, title_size=8.2, body_size=6.6)
    box(ax, 104.5, 39.5, 39.0, 3.6, "会话上下文 + 检索工具",
        ["query rewrite · search / fetch · cite", "输出过滤 · 引用回填"],
        C_WHITE, C_ORCH_E, title_size=8.2, body_size=6.6)
    ax.text(123.5, 37.6, "JWT 强制 · 租户 / KB scope · Redis 缓存",
            ha="center", fontsize=6.6, color=C_ORCH_E)

    # ============================================================ 运行时 ====
    group(ax, 9.0, 23.5, 92.5, 10.5, "运行时与 Agent Core 2.0", C_RUNTIME, C_RUNTIME_E)
    box(ax, 10.5, 28.3, 20.0, 5.2, "AgentRuntime + ControlPlane",
        ["状态 / 步骤 / 预算 / 生命周期", "Run 登记 · 取消 · 超时"],
        C_WHITE, C_RUNTIME_E, title_size=8.0, body_size=6.5)
    box(ax, 32.0, 28.3, 18.0, 5.2, "Context / Memory",
        ["message_window · instructions", "长时记忆（默认关）· 压缩"],
        C_WHITE, C_RUNTIME_E, title_size=8.2, body_size=6.4)
    box(ax, 51.5, 28.3, 16.5, 5.2, "Policies / HITL",
        ["planner · verifier · guardrails", "人工审批（默认关）"],
        C_WHITE, C_RUNTIME_E, title_size=8.2, body_size=6.3)
    box(ax, 69.5, 28.3, 15.0, 5.2, "ToolRegistry / Executor",
        ["风险 / 权限 / 审批 / 重试", "KG 五工具默认直连"],
        C_WHITE, C_RUNTIME_E, title_size=7.6, body_size=6.2)
    box(ax, 86.0, 28.3, 14.0, 5.2, "Skills / Hooks / Events",
        ["默认关 · Evolution 离线", "记忆 / 技能 / 事件端口"],
        C_OPT, C_OPT_E, ls="--", title_size=7.4, body_size=6.0)
    ax.text(50.5, 24.7,
            "默认接线：ChatService 构造 max_*=0 的 Runtime 只产出轨迹；治理链受 TOOL_GATEWAY_ENFORCED / HUMAN_REVIEW_ENABLED 门控",
            ha="center", fontsize=6.3, color=C_RUNTIME_E)

    group(ax, 103.0, 23.5, 42.0, 10.5, "检索与上下文", C_RUNTIME, C_RUNTIME_E)
    box(ax, 104.5, 28.3, 19.0, 5.2, "Hybrid Retrieval",
        ["dense + sparse", "多路召回 / 缓存"], C_WHITE, C_RUNTIME_E,
        title_size=8.4, body_size=6.7)
    box(ax, 125.0, 28.3, 19.0, 5.2, "Fusion / Rerank",
        ["RRF / z-score", "bge-reranker"], C_WHITE, C_RUNTIME_E,
        title_size=8.4, body_size=6.7)
    ax.text(123.5, 24.7, "Citation Verify · Context Engine · Output Filter",
            ha="center", fontsize=6.7, color=C_RUNTIME_E)

    # ============================================================ 能力层 ====
    group(ax, 9.0, 11.5, 92.5, 10.0, "能力与适配（L3 工具）", C_CAPS, C_CAPS_E)
    box(ax, 10.5, 15.8, 24.0, 4.4, "LLM / Embedding Provider",
        ["OpenAI 兼容 · _TrackedChatOpenAI", "embedding / rerank 客户端"],
        C_WHITE, C_CAPS_E, title_size=8.2, body_size=6.4)
    box(ax, 36.0, 15.8, 24.0, 4.4, "Neo4j + Text2SQL",
        ["数据条目图谱 · 只读 Cypher", "Wren → MySQL legal_db"],
        C_WHITE, C_CAPS_E, title_size=8.2, body_size=6.4)
    box(ax, 61.5, 15.8, 19.0, 4.4, "内置工具 / 适配器",
        ["search · memory · cron", "notify · artifact · PDF"],
        C_WHITE, C_CAPS_E, title_size=8.2, body_size=6.3)
    box(ax, 82.0, 15.8, 18.0, 4.4, "LightRAG / MCP",
        ["LightRAG 社区摘要（可选）", "MCP Server/Client（未接主链）"],
        C_OPT, C_OPT_E, ls="--", title_size=8.0, body_size=6.1)
    ax.text(55.0, 12.4,
            "工具声明：ToolRegistry / ToolSpec（L0–L3）· 11 个 adapters · MCP 工具默认不信任（L2 + side_effect）",
            ha="center", fontsize=6.3, color=C_CAPS_E)

    group(ax, 103.0, 11.5, 42.0, 10.0, "检索与入库能力", C_CAPS, C_CAPS_E)
    box(ax, 104.5, 15.8, 18.5, 4.4, "Embedding",
        ["缓存 · 批量 · 1024 维"], C_WHITE, C_CAPS_E,
        title_size=8.2, body_size=6.4)
    box(ax, 124.5, 15.8, 19.5, 4.4, "Vector Store",
        ["Milvus / FAISS / Qdrant", "collection: legal"], C_WHITE, C_CAPS_E,
        title_size=8.2, body_size=6.3)
    box(ax, 104.5, 12.0, 19.5, 3.0, "PDF 解析 / 存储",
        ["PyMuPDF · MinIO / LocalFS"], C_WHITE, C_CAPS_E,
        title_size=7.8, body_size=6.0)
    box(ax, 125.5, 12.0, 18.5, 3.0, "Celery Worker",
        ["index_worker · 可独立伸缩"], C_WHITE, C_CAPS_E,
        title_size=7.8, body_size=6.0)

    # ============================================================ 数据层 ====
    data_y, data_h = 1.8, 6.6
    box(ax, 9.5, data_y, 20.0, data_h, "PostgreSQL / SQLite",
        ["业务元数据", "retrieval schema", "本地默认 SQLite"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 31.0, data_y, 15.0, data_h, "MySQL",
        ["legal_db", "Text2SQL 数据源"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 47.5, data_y, 19.0, data_h, "Redis",
        ["缓存 · Celery broker", "事件流", "LangGraph checkpointer"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 68.0, data_y, 17.5, data_h, "Neo4j",
        ["数据条目 / 示例说明", "知识图谱"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 87.0, data_y, 24.0, data_h, "Milvus / FAISS / Qdrant",
        ["向量索引", "dense retrieval"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 112.5, data_y, 15.5, data_h, "MinIO / uploads",
        ["文档对象", "本地静态目录"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.4)
    box(ax, 129.5, data_y, 15.0, data_h, "监控栈",
        ["Prometheus · Grafana", "Alertmanager · OTel"], C_DATA, C_DATA_E,
        title_size=8.4, body_size=6.1)

    # ============================================================ 横切栏 ====
    ax.add_patch(
        FancyBboxPatch(
            (RAIL_X, 11.0),
            RAIL_W,
            72.5,
            boxstyle="round,pad=0.18,rounding_size=0.7",
            facecolor=C_X,
            edgecolor=C_X_E,
            linewidth=1.0,
            zorder=2,
        )
    )
    ax.text(RAIL_X + RAIL_W / 2, 82.2, "横切能力", ha="center", va="center",
            fontsize=9.4, fontweight="bold", color=C_X_E, zorder=5)
    ax.text(RAIL_X + RAIL_W / 2, 80.8, "作用于所有层", ha="center", va="center",
            fontsize=6.6, color=C_MUTED, zorder=5)

    rail_items = [
        (69.5, 9.4, "Security", ["APIKey / JWT / RBAC", "Prompt Injection", "Output Filter"]),
        (58.0, 9.4, "Reliability", ["Circuit Breaker", "Retry Budget", "Bulkhead / RateLimit"]),
        (46.5, 9.4, "Performance", ["Admission", "Load Shedding", "Budget / Latency"]),
        (35.0, 9.4, "Observability", ["Loguru / Prometheus", "OTel span", "Trajectory"]),
        (23.5, 9.4, "Config / Settings", ["Pydantic Settings", "Ablation / Selfcheck", "Feature Flags（默认关）"]),
        (11.8, 9.4, "Quality / Delivery", ["pytest / evals / CI", "Docker Compose · Makefile"]),
    ]
    for y, h, title, body in rail_items:
        box(ax, RAIL_X + 0.8, y, RAIL_W - 1.6, h, title, body,
            C_WHITE, C_X_E, title_size=8.2, body_size=6.5)

    # ============================================================ 连线 ====
    # ① 客户端 → 接入
    arrow(ax, (24.0, 85.6), (22.0, 81.8), lw=1.35)
    badge(ax, 21.3, 83.7, "1", size=6.8)
    arrow(ax, (57.5, 85.6), (49.5, 81.8), lw=1.35)
    badge(ax, 52.4, 83.7, "2", size=6.8)
    arrow(ax, (90.0, 85.6), (89.5, 81.8), lw=1.35)
    badge(ax, 91.2, 83.7, "3", size=6.8)

    # 接入横向
    arrow(ax, (34.5, 78.4), (36.0, 78.4))
    arrow(ax, (63.0, 78.4), (65.0, 78.4))

    # ② 中间件 → 路由
    arrow(ax, (78.0, 75.0), (78.0, 71.2), lw=1.35)
    badge(ax, 76.8, 73.0, "4", size=6.8)
    arrow(ax, (110.0, 75.0), (110.0, 71.2), lw=1.35)
    badge(ax, 108.8, 73.0, "5", size=6.8)

    # ③ 路由 → 应用
    arrow(ax, (23.5, 65.2), (28.5, 59.9), lw=1.4)
    badge(ax, 25.1, 62.4, "6", size=6.8)
    arrow(ax, (114.0, 66.0), (115.5, 59.9), lw=1.4)
    badge(ax, 113.0, 62.9, "7", size=6.8)

    # ④ 应用 → 编排
    arrow(ax, (28.5, 52.5), (30.0, 47.4), lw=1.5)
    badge(ax, 28.4, 49.9, "8", size=6.8)
    arrow(ax, (115.5, 52.5), (120.0, 47.4), lw=1.5)
    badge(ax, 116.8, 49.9, "9", size=6.8)

    # 主图内部
    arrow(ax, (27.5, 45.4), (29.5, 45.4))
    arrow(ax, (50.5, 45.4), (52.5, 45.4))
    ax.add_line(Line2D([59.0, 59.0], [44.0, 43.55], color=C_ORCH_E, lw=1.0, zorder=5))
    ax.add_line(Line2D([18.0, 77.5], [43.55, 43.55], color=C_ORCH_E, lw=1.0, zorder=5))
    for x in (18.0, 33.5, 50.5, 66.0, 77.0):
        arrow(ax, (x, 43.55), (x, 42.9), color=C_ORCH_E, lw=1.0, head=True)

    # ⑤ 编排 → 运行时
    arrow(ax, (45.0, 36.5), (45.0, 33.7), lw=1.5)
    badge(ax, 43.9, 35.0, "10", size=6.5)
    arrow(ax, (120.0, 36.5), (120.0, 33.7), lw=1.5)
    badge(ax, 118.9, 35.0, "11", size=6.5)

    # 运行时 → 能力
    arrow(ax, (90.0, 23.5), (90.0, 21.5), lw=1.4)
    arrow(ax, (138.0, 23.5), (138.0, 21.5), lw=1.4)

    # 能力 → 数据
    arrow(ax, (55.0, 11.5), (55.0, 9.4), lw=1.4)
    badge(ax, 53.9, 10.4, "12", size=6.5)
    arrow(ax, (100.0, 11.5), (100.0, 9.4), lw=1.4)
    badge(ax, 98.9, 10.4, "13", size=6.5)

    # SSE 回传（同一个接口反向流式）
    arrow(ax, (19.0, 59.9), (19.0, 65.2), color=C_SSE, lw=1.2, ls=":")
    ax.text(17.7, 62.5, "SSE", rotation=90, fontsize=6.2, color=C_SSE, ha="center", va="center")
    arrow(ax, (110.0, 59.9), (110.0, 66.0), color=C_SSE, lw=1.2, ls=":")
    ax.text(108.7, 62.9, "SSE", rotation=90, fontsize=6.2, color=C_SSE, ha="center", va="center")

    # 检索入库旁路（kb_ingest → Celery）
    poly_arrow(
        ax,
        [(143.5, 54.0), (145.8, 54.0), (145.8, 13.5), (144.0, 13.5)],
        color=C_CAPS_E,
        lw=1.2,
        ls="--",
    )
    ax.text(146.6, 33.0, "入库", rotation=90, fontsize=6.2, color=C_CAPS_E,
            ha="center", va="center")

    # 横切栏 ↔ 主区域
    for y in (67.0, 45.0, 22.4):
        arrow(ax, (145.0, y), (147.0, y), color=C_OBS, lw=1.0, ls=":")
        arrow(ax, (147.0, y), (145.0, y), color=C_OBS, lw=1.0, ls=":")
    ax.text(146.0, 69.0, "横切", fontsize=5.8, color=C_OBS, ha="center", va="center")

    # ------------------------------------------------------------ 脚注 ----
    ax.text(
        9.0,
        0.25,
        "注：实线为默认主链路；橙色虚线为默认关闭 / 可选 / 未接线；蓝色点线为 SSE 流式回传。存储读写未逐一连线，"
        "以代码路径与实际开关为准；图中默认值对应 v0.2.0。",
        fontsize=6.8,
        color=C_MUTED,
        ha="left",
        va="center",
    )

    fig.subplots_adjust(left=0.01, right=0.99, top=0.995, bottom=0.005)
    fig.savefig(OUT_PNG, dpi=180, facecolor=C_BG, bbox_inches="tight", pad_inches=0.15)
    fig.savefig(OUT_SVG, facecolor=C_BG, bbox_inches="tight", pad_inches=0.15)
    print(f"written: {OUT_PNG}")
    print(f"written: {OUT_SVG}")


if __name__ == "__main__":
    main()
