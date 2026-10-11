"""生成 My Agentix 品牌资源（docs/assets/*.png）。

输出:
    docs/assets/agentix-logo.png   —— 横幅 logo（MY AGENTIX 字样 + 多 Agent 节点徽章）
    docs/assets/agentix-icon.png   —— 方形图标（品牌色圆角方块 + 节点徽章）

用法:
    python docs/design/generate_logo.py

依赖:
    matplotlib；中文字体使用 C:/Windows/fonts/msyh.ttc（微软雅黑）。
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch
import matplotlib.font_manager as fm

FONT_PATH = "C:/Windows/fonts/msyh.ttc"
fm.fontManager.addfont(FONT_PATH)
_prop = fm.FontProperties(fname=FONT_PATH)
plt.rcParams["font.family"] = _prop.get_name()

C_BRAND = "#1F4E79"      # 品牌深蓝
C_EDGE = "#5B9BD5"       # 连线蓝
C_TXT = "#7A7A7A"        # 副标题灰
OUT_DIR = "docs/assets"


def _draw_emblem(ax, cx, cy, s, node_color, line_color, lw):
    """多 Agent 节点徽章：三个互连节点（象征多智能体协作）。"""
    a = (cx, cy + 0.55 * s)
    b = (cx - 0.40 * s, cy - 0.18 * s)
    c = (cx + 0.40 * s, cy - 0.18 * s)
    for p, q in [(b, c), (a, b), (a, c)]:
        ax.plot([p[0], q[0]], [p[1], q[1]], color=line_color, lw=lw, zorder=1)
    for x, y in (a, b, c):
        ax.add_patch(Circle((x, y), 0.11 * s, facecolor=node_color,
                            edgecolor="none", zorder=2))
        ax.add_patch(Circle((x, y), 0.045 * s, facecolor="white",
                            edgecolor="none", zorder=3))


def _draw_logo():
    """横幅 logo：徽章 + My Agentix + Agent Platform。"""
    fig, ax = plt.subplots(figsize=(6, 1.4), dpi=200)
    ax.set_xlim(0, 6)
    ax.set_ylim(0, 1.4)
    ax.axis("off")

    _draw_emblem(ax, 0.85, 0.62, 1.25, C_BRAND, C_EDGE, 2.2)

    ax.text(1.85, 0.80, "My Agentix", fontsize=25, fontweight="bold",
            color=C_BRAND, ha="left", va="center")
    ax.text(1.85, 0.26, "Agent Platform", fontsize=11.5, color=C_TXT,
            ha="left", va="center")

    out = f"{OUT_DIR}/agentix-logo.png"
    plt.savefig(out, dpi=200, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close()
    print(f"logo 已生成: {out}")


def _draw_icon():
    """方形图标：品牌色圆角方块 + 白色节点徽章 + Agentix 字样。"""
    fig, ax = plt.subplots(figsize=(2, 2), dpi=256)
    ax.set_xlim(0, 2)
    ax.set_ylim(0, 2)
    ax.axis("off")

    badge = FancyBboxPatch((0.06, 0.06), 1.88, 1.88,
                           boxstyle="round,pad=0.0,rounding_size=0.22",
                           facecolor=C_BRAND, edgecolor="none", zorder=1)
    ax.add_patch(badge)

    _draw_emblem(ax, 1.0, 1.18, 0.85, "white", "#FFFFFF", 1.6)
    ax.text(1.0, 0.42, "Agentix", fontsize=15, fontweight="bold",
            color="white", ha="center", va="center", zorder=4)

    out = f"{OUT_DIR}/agentix-icon.png"
    plt.savefig(out, dpi=256, bbox_inches="tight", facecolor="white", edgecolor="none")
    plt.close()
    print(f"icon 已生成: {out}")


if __name__ == "__main__":
    _draw_logo()
    _draw_icon()