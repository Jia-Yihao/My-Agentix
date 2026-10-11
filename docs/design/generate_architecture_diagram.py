"""生成 My Agentix 平台架构图（docs/assets/*.png）。

输出:
    docs/assets/architecture.png                —— 平台整体架构：前端 → API/中间件 → 三层 Multi-Agent → 数据层/横切能力
    docs/assets/agent-core-architecture.png     —— Agent Core 2.0 与 Agent Runtime 内核架构

用法:
    python docs/design/generate_architecture_diagram.py

依赖:
    matplotlib；中文字体使用 C:/Windows/fonts/msyh.ttc（微软雅黑）。
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import matplotlib.font_manager as fm

FONT_PATH = "C:/Windows/fonts/msyh.ttc"
fm.fontManager.addfont(FONT_PATH)
_prop = fm.FontProperties(fname=FONT_PATH)
plt.rcParams["font.family"] = _prop.get_name()
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = "docs/assets"

# ------------------------------------------------------------------ 配色 ----
C_TITLE = "#1F4E79"
C_FRONT, C_FRONT_E = "#D6E8F5", "#5B9BD5"    # 前端 / API / 路由
C_L2, C_L2_E = "#D5F0E8", "#4DB6A0"          # L2 多工具子图
C_L3, C_L3_E = "#FDEBD0", "#E8A33D"          # L3 原子工具
C_DATA, C_DATA_E = "#FFF6D9", "#C9A227"      # 数据层
C_CORE, C_CORE_E = "#EAF3FB", "#2E75B6"      # Agent Core / Runtime 面板
C_STRIP, C_STRIP_E = "#F2F2F2", "#8C8C8C"    # 横切能力条
C_ARROW = "#8A8A8A"


def _draw_node(ax, x, y, w, h, text, color, edgecolor="#333333",
               fontsize=9, fontweight="normal", lw=1.2, zorder=2):
    box = FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                         boxstyle="round,pad=0.08",
                         facecolor=color, edgecolor=edgecolor,
                         linewidth=lw, zorder=zorder)
    ax.add_patch(box)
    ax.text(x, y, text, fontsize=fontsize, fontweight=fontweight,
            ha="center", va="center", zorder=3, color="#222222")


def _draw_arrow(ax, x1, y1, x2, y2, color=C_ARROW, style="-|>"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle=style, color=color, lw=1.3),
                zorder=1)


def _draw_architecture():
    fig, ax = plt.subplots(1, 1, figsize=(20, 14))
    ax.set_xlim(0, 20)
    ax.set_ylim(2.6, 14)
    ax.axis("off")
    ax.set_aspect("equal")

    # ------------------------------------------------------------ 标题 ----
    ax.text(10, 13.58, "My Agentix · Agent Platform 系统架构",
            fontsize=23, fontweight="bold", color=C_TITLE, ha="center", va="center")
    ax.text(10, 13.05,
            "LangGraph 三层 Multi-Agent 编排 ｜ Agent Core 2.0 · Agent Runtime · Control Plane · MCP · 多路检索融合",
            fontsize=10.5, color="#7A7A7A", ha="center", va="center")

    # -------------------------------------------------------- 分层标签 ----
    for y, label in [(12.6, "前端"), (11.05, "API 与中间件"), (9.62, "L1 主路由"),
                     (7.8, "L2 多工具子图"), (5.7, "L3 原子工具"), (4.45, "数据层"), (3.65, "横切能力")]:
        ax.text(0.28, y, label, fontsize=9.5, fontweight="bold", color=C_TITLE,
                ha="left", va="center")

    # ------------------------------------------------------------ 前端 ----
    _draw_node(ax, 10, 12.60, 7.8, 0.66,
               "Vue3 前端 ｜ ChatWidget 对话 · 知识管理 · 文档管理 · 审批面板",
               C_FRONT, C_FRONT_E, fontsize=9.6)
    _draw_node(ax, 10, 11.90, 6.2, 0.36,
               "SSE 流式对话 · 执行轨迹时间线 · 反馈与评测视图",
               C_FRONT, C_FRONT_E, fontsize=7.8)

    # ------------------------------------------------------------ API ----
    _draw_node(ax, 10, 11.05, 7.8, 0.62,
               "FastAPI ｜ /api/v1/chat (SSE) · /api/v1/agents/runs · /kb · /approvals",
               C_FRONT, C_FRONT_E, fontsize=8.6)
    _draw_node(ax, 10, 10.42, 7.2, 0.34,
               "中间件链: 限流 → trace_id → 请求日志 → APIKey 鉴权 → CORS",
               C_FRONT, C_FRONT_E, fontsize=7.6)

    # ------------------------------------------------------- L1 主路由 ----
    _draw_node(ax, 10, 9.62, 7.8, 0.60,
               "L1 主路由 ｜ analyze_and_route_query（启发式 + LLM 双保险）",
               C_FRONT, C_FRONT_E, fontsize=9.4)
    _draw_node(ax, 10, 9.00, 7.2, 0.32,
               "意图分类: general / additional / graphrag / kb / image / file",
               C_FRONT, C_FRONT_E, fontsize=7.6)

    # ----------------------------------------------------- L2 多工具子图 ----
    _draw_node(ax, 6.3, 8.15, 5.4, 0.50, "graphrag 多工具工作流",
               C_L2, C_L2_E, fontsize=9.4, fontweight="bold")
    _draw_node(ax, 6.3, 7.55, 6.6, 0.34, "guardrails → planner → tool_selection → 并行工具",
               C_L2, C_L2_E, fontsize=7.4)
    _draw_node(ax, 6.3, 7.10, 6.6, 0.34, "[cypher / predefined / graphrag / text2sql] → summarize → final",
               C_L2, C_L2_E, fontsize=7.4)

    _draw_node(ax, 13.7, 8.15, 5.4, 0.50, "kb 多工具工作流",
               C_L2, C_L2_E, fontsize=9.4, fontweight="bold")
    _draw_node(ax, 13.7, 7.55, 6.2, 0.34, "guardrails → kb_router → local_search",
               C_L2, C_L2_E, fontsize=7.4)
    _draw_node(ax, 13.7, 7.10, 6.2, 0.34, "[结构化优先 / Milvus 兜底] → reranker → finalize",
               C_L2, C_L2_E, fontsize=7.4)

    # ---------------------------------------------------- L3 原子工具 ----
    for x, label in [
        (3.3, "Neo4j\n知识图谱"),
        (7.5, "Milvus\n向量语义检索"),
        (11.7, "LightRAG\n社区摘要"),
        (15.8, "MCP Server\n外部工具子进程"),
    ]:
        _draw_node(ax, x, 6.10, 2.5, 0.72, label, C_L3, C_L3_E, fontsize=8.4)
    _draw_node(ax, 10, 5.35, 8.6, 0.40,
               "Reranker 重排 · kb_ingest 入库服务 · 外部搜索兜底 · ToolExecutor 统一治理",
               C_L3, C_L3_E, fontsize=7.6)

    # ---------------------------------------------------------- 数据层 ----
    _draw_node(ax, 10, 4.45, 13.0, 0.70,
               "基础设施: Redis(缓存/Checkpointer) · Neo4j · Milvus · PostgreSQL · MySQL · MinIO · Prometheus/Grafana 观测栈",
               C_DATA, C_DATA_E, fontsize=8.6)

    # ------------------------------------------------------- 横切能力 ----
    _draw_node(ax, 10, 3.65, 13.0, 0.40,
               "可观测 trace_id/轨迹/指标 ｜ 评测回归 + 质量门禁 ｜ 安全 APIKey/JWT/RBAC/输出过滤 ｜ 可靠 限流/熔断/降级",
               C_STRIP, C_STRIP_E, fontsize=7.6)

    # ---------------------------------------------- Agent Core/Runtime ----
    panel = FancyBboxPatch((17.05, 4.60), 2.9, 5.90,
                           boxstyle="round,pad=0.1", facecolor=C_CORE,
                           edgecolor=C_CORE_E, linewidth=1.6, linestyle="--", zorder=2)
    ax.add_patch(panel)
    ax.text(18.5, 10.15, "Agent Core 2.0\nAgent Runtime",
            fontsize=9.5, fontweight="bold", color=C_TITLE, ha="center", va="center", zorder=3)
    core_lines = [
        "主循环 Task→Context→Plan→",
        "Execute→Observe→Verify",
        "Replanner 失败纠偏(Append-only)",
        "Verifier 分层验收 · Context 隔离",
        "Memory · HITL Sidecar · 事件总线",
        "多智能体六失败模式检测",
        "Runtime 状态/步骤/生命周期",
        "四维预算 BudgetManager",
        "Control Plane 登记/取消/归属",
        "MCP Client+Server 统一治理",
    ]
    for i, line in enumerate(core_lines):
        ax.text(17.2, 9.62 - i * 0.5, line, fontsize=7.2,
                color="#2E5F8A", ha="left", va="center", zorder=3)

    # ------------------------------------------------------------ 箭头 ----
    _draw_arrow(ax, 10, 12.27, 10, 11.36)    # 前端 → API
    _draw_arrow(ax, 10, 10.74, 10, 9.92)     # API → L1
    _draw_arrow(ax, 7.0, 9.32, 6.3, 8.40)    # L1 → graphrag
    _draw_arrow(ax, 13.0, 9.32, 13.7, 8.40)  # L1 → kb
    _draw_arrow(ax, 5.0, 6.93, 3.3, 6.46)    # graphrag → Neo4j
    _draw_arrow(ax, 7.6, 6.93, 7.5, 6.46)    # graphrag → Milvus
    _draw_arrow(ax, 12.6, 6.93, 11.7, 6.46)  # kb → LightRAG
    _draw_arrow(ax, 14.6, 6.93, 15.8, 6.46)  # kb → MCP Server

    plt.tight_layout()
    out = f"{OUT_DIR}/architecture.png"
    plt.savefig(out, dpi=180, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close()
    print(f"架构图已生成: {out}")


def _draw_agent_core():
    fig, ax = plt.subplots(1, 1, figsize=(16, 11))
    ax.set_xlim(0, 16)
    ax.set_ylim(1.6, 11)
    ax.axis("off")
    ax.set_aspect("equal")

    # ------------------------------------------------------------ 标题 ----
    ax.text(8, 10.55, "My Agentix · Agent Core 2.0 & Agent Runtime",
            fontsize=20, fontweight="bold", color=C_TITLE, ha="center", va="center")
    ax.text(8, 10.15,
            "可独立复用的 Agent 内核（Harness + Capabilities）＋ 运行治理（State / Steps / Budget / Control Plane）",
            fontsize=10, color="#7A7A7A", ha="center", va="center")

    # ------------------------------------------------ 运行治理（顶层） ----
    _draw_node(ax, 3.0, 9.3, 3.2, 0.5, "任务级 Run API\nspawn · status · cancel",
               C_CORE, C_CORE_E, fontsize=7.8)
    _draw_node(ax, 8.0, 9.3, 4.0, 0.5, "Control Plane\n登记(有界FIFO) · 取消 · 预算归属 · 终态幂等",
               C_CORE, C_CORE_E, fontsize=7.8)
    _draw_node(ax, 13.0, 9.3, 3.4, 0.5, "BudgetManager 四维预算\n步数 / LLM / 工具 / 耗时",
               C_CORE, C_CORE_E, fontsize=7.8)

    _draw_arrow(ax, 4.6, 9.3, 6.0, 9.3)     # Run API → Control Plane
    _draw_arrow(ax, 10.0, 9.3, 11.3, 9.3)   # Control Plane → BudgetManager
    _draw_arrow(ax, 8, 9.05, 8, 8.60)       # Control Plane → 主循环容器

    # ---------------------------------------------------- 左列：能力/治理 ----
    left_items = [
        (8.35, "ToolExecutor 治理链\n权限 → 风险(L0-L3) → 超时 → 重试 → 轨迹"),
        (7.60, "ToolRegistry 声明式注册\n工具限量 · 结果限量"),
        (6.85, "MCP Client → MCP Server\nstdio JSON-RPC · 远端工具接入"),
        (6.10, "HITL Sidecar\nApprove / Reject / Edit / Resume"),
        (5.35, "事件总线\n去重 · 双时刻 · 重试交付"),
    ]
    for y, label in left_items:
        _draw_node(ax, 2.6, y, 4.2, 0.5, label, C_CORE, C_CORE_E, fontsize=7.2)

    # ------------------------------------------------------ 主循环容器 ----
    container = FancyBboxPatch((4.2, 4.60), 7.6, 3.95,
                               boxstyle="round,pad=0.12", facecolor="#F7FBFF",
                               edgecolor=C_CORE_E, linewidth=1.8, zorder=1)
    ax.add_patch(container)
    ax.text(8, 8.35, "Agent Core 主循环 ｜ agentix/orchestration/orchestrator.py",
            fontsize=9, fontweight="bold", color=C_TITLE, ha="center", va="center", zorder=3)

    steps = [
        (7.80, "Task 任务入参"),
        (7.35, "Context Engine 上下文"),
        (6.90, "Planner 单步规划"),
        (6.45, "Executor 工具编排"),
        (6.00, "Observer 观测"),
        (5.55, "Verifier 分层验收 L1+L2"),
    ]
    for i, (y, label) in enumerate(steps):
        _draw_node(ax, 8, y, 3.0, 0.38, label, C_FRONT, C_FRONT_E, fontsize=7.4)
        if i < len(steps) - 1:
            _draw_arrow(ax, 8, y - 0.21, 8, steps[i + 1][0] + 0.21)

    # ------------------------------------------------- Replanner 反馈回路 ----
    _draw_node(ax, 8, 5.05, 3.0, 0.38, "Replanner 失败纠偏\nAppend-only 新 Plan · 同因熔断",
               C_L2, C_L2_E, fontsize=7.2)
    _draw_arrow(ax, 8, 5.35, 8, 5.26)  # Verifier → Replanner
    _draw_arrow(ax, 9.5, 5.05, 9.5, 7.10)  # Replanner → Planner（纠偏回路，沿容器右侧上行）

    # ---------------------------------------------------- 右列：记忆/协同 ----
    right_items = [
        (8.35, "Memory 记忆\n三 scope × 五 kind · 读写冲突裁决(时间窗+标记) · PII 脱敏"),
        (7.60, "多智能体\nPeer + Manager · 六失败模式检测"),
        (6.85, "演化回路\n在线录制提案轨迹 → 离线回放回归"),
        (6.10, "可观测\n步骤级轨迹落盘 · 指标 · Explanation API"),
        (5.35, "端口契约 agentix/domain/ports\nContext / Planner / Verifier / Memory / Event / Rollback"),
    ]
    for y, label in right_items:
        _draw_node(ax, 13.2, y, 3.8, 0.5, label, C_CORE, C_CORE_E, fontsize=7.0)

    # ----------------------------------------------------------- 底部条 ----
    _draw_node(ax, 8, 3.3, 12.6, 0.6,
               "横切: 评测与门禁（黄金集回归 · 质量门禁 · 轨迹前缀回归） ｜ 安全（RBAC · JWT · 输出过滤） ｜ 可靠（限流 · 熔断）",
               C_STRIP, C_STRIP_E, fontsize=8)

    plt.tight_layout()
    out = f"{OUT_DIR}/agent-core-architecture.png"
    plt.savefig(out, dpi=180, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close()
    print(f"内核架构图已生成: {out}")


if __name__ == "__main__":
    _draw_architecture()
    _draw_agent_core()