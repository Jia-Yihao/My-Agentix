"""生成 RAG 混合检索链路图（docs/assets/rag-hybrid-pipeline.png）。

用法：
    python docs/design/generate_rag_pipeline_diagram.py

输出：
    docs/assets/rag-hybrid-pipeline.png —— Dense 与 BM25 并发召回 → RRF 融合
    → Cross-encoder 精排 / Context Budget → Grounding 与 evidence gate。

图中事实源：`agentix/retrieval/usecases/retrieval_hybrid.py`、
`agentix/retrieval/infra/rag/sparse.py`、`agentix/retrieval/infra/rag/rerank.py`、
`docs/design/检索增强深度优化.md` §5/§8。中文字体使用 C:/Windows/fonts/msyh.ttc。
"""

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

FONT_PATH = "C:/Windows/fonts/msyh.ttc"
fm.fontManager.addfont(FONT_PATH)
_prop = fm.FontProperties(fname=FONT_PATH)
plt.rcParams["font.family"] = _prop.get_name()
plt.rcParams["axes.unicode_minus"] = False

OUT = "docs/assets/rag-hybrid-pipeline.png"

C_TITLE = "#1f3a68"
C_TEXT = "#22303f"
C_NOTE = "#5a6b7d"
C_BLUE, C_BLUE_F = "#2f5aa8", "#eaf1fb"
C_GREEN, C_GREEN_F = "#2e7d5b", "#e8f6ee"
C_ORANGE, C_ORANGE_F = "#c8862b", "#fdf1dc"
C_PURPLE, C_PURPLE_F = "#7b4fb0", "#f1eafb"
C_GREY, C_GREY_F = "#3f5c85", "#eef2f7"
C_RED, C_RED_F = "#c0392b", "#fdecec"

ROW_Y, ROW_H = 0.40, 0.24
UP_Y, UP_H = 0.68, 0.24
LOW_Y, LOW_H = 0.10, 0.24


def box(ax, x, y, w, h, edge, face, title, lines, title_size=11.5, body_size=9.5):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.006,rounding_size=0.018",
            linewidth=1.7,
            edgecolor=edge,
            facecolor=face,
            zorder=2,
        )
    )
    cx = x + w / 2
    ax.text(cx, y + h * 0.80, title, ha="center", va="center",
            fontsize=title_size, fontweight="bold", color=C_TEXT, zorder=3)
    n = len(lines)
    if n:
        top, bottom = 0.58, 0.16
        for i, line in enumerate(lines):
            frac = top if n == 1 else top - i * (top - bottom) / (n - 1)
            ax.text(cx, y + h * frac, line, ha="center", va="center",
                    fontsize=body_size, color=C_TEXT, zorder=3)


def arrow(ax, x1, y1, x2, y2):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=15,
            linewidth=2.0,
            color=C_BLUE,
            zorder=1,
        )
    )


def main():
    fig, ax = plt.subplots(figsize=(18, 6.4), dpi=100)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    fig.patch.set_facecolor("#f7f9fc")

    ax.text(0.5, 0.965, "RAG 混合检索与融合精排链路", ha="center", va="center",
            fontsize=20, fontweight="bold", color=C_TITLE)

    box(ax, 0.010, ROW_Y, 0.140, ROW_H, C_BLUE, C_BLUE_F,
        "检索请求", ["归一化 query", "kb_id + artifact_id"])
    box(ax, 0.190, UP_Y, 0.170, UP_H, C_GREEN, C_GREEN_F,
        "Dense 向量召回", ["与 BM25 并发", "阈值 + 有效性过滤"])
    box(ax, 0.190, LOW_Y, 0.170, LOW_H, C_ORANGE, C_ORANGE_F,
        "BM25 稀疏召回", ["倒排 posting", "只扫命中查询词 chunk"])
    box(ax, 0.400, ROW_Y, 0.160, ROW_H, C_PURPLE, C_PURPLE_F,
        "RRF / z-score 融合", ["按 chunk_id 去重", "FUSION_TOP_N 截断"])
    box(ax, 0.600, ROW_Y, 0.170, ROW_H, C_GREY, C_GREY_F,
        "精排与预算", ["Cross-encoder /rerank", "Context Budget"])
    box(ax, 0.810, ROW_Y, 0.180, ROW_H, C_RED, C_RED_F,
        "答案与引用", ["Grounding 校验", "evidence gate"])

    mid_row = ROW_Y + ROW_H / 2
    arrow(ax, 0.150, mid_row + 0.05, 0.190, UP_Y + UP_H / 2)
    arrow(ax, 0.150, mid_row - 0.05, 0.190, LOW_Y + LOW_H / 2)
    arrow(ax, 0.360, UP_Y + UP_H / 2, 0.400, mid_row + 0.05)
    arrow(ax, 0.360, LOW_Y + LOW_H / 2, 0.400, mid_row - 0.05)
    arrow(ax, 0.560, mid_row, 0.600, mid_row)
    arrow(ax, 0.770, mid_row, 0.810, mid_row)

    ax.text(
        0.5,
        0.015,
        "BM25 缓存键 (kb_id, artifact_id)｜语料只取活动 manifest 内 status=ready 且 "
        "indexed_artifact_id 相符的 chunk｜总闸 RETRIEVAL_HYBRID_ENABLED 默认关，"
        "精排失败 fail-open 保序放行并标记 applied=false",
        ha="center",
        va="bottom",
        fontsize=10,
        color=C_NOTE,
    )

    fig.savefig(OUT, dpi=100, facecolor=fig.get_facecolor())
    plt.close(fig)
    print("saved: " + OUT)


if __name__ == "__main__":
    main()
