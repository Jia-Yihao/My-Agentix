"""对 ``evals/run_eval.py`` 录制的预测档做**多 agent 性能报告**。

为什么不用现成的 summary 就够了：``run_eval`` 的 summary 是**通用打分口径**
（路由 / 引用 / 拒答 / must_contain），它答不了"多 agent 系统这次跑得怎么样"——
退出原因分布、失败模式、协调轮数、每次任务多花了几次模型调用、单题墙钟。
本脚本把这些从预测档里挖出来，和 MCQ 正确率放在一起，才是一张**性能**报告。

判分口径（诚实标注，别把宽松当准确）：

* **MCQ 正确率**用**选项字母抽取**，不是子串命中。抽取优先级：
  ① "答案/选 X" 类显式标注 → ② 行首 "X."/"（X）" → ③ 四个选项原文里
  出现了哪一个（只在抽不到字母时用）→ ④ 抽不出记 ``none``。
  ④ 的占比会单独报出来，不混进正确率的分母以外的解释里。
* **多 agent 指标**只统计带 ``multi_agent`` 元数据的行；扁平拓扑恒无 ⇒
  该组显示「不适用」而不是 0。
* **性能读数**取自预测行的 ``perf`` 段（墙钟 / LLM 步数 / token），
  缺失就显示「未采集」，不补 0。

用法::

    .\\.venv\\Scripts\\python.exe scripts/eval/mcq_report.py \\
        --golden evals/ceval_golden.jsonl \\
        --predictions out/preds_multi_agent.jsonl \\
        --out docs/evals/multi-agent-perf.md
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK，报告是中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals.perf_stats import nearest_rank_quantile  # noqa: E402  —— 分位数单源
from evals.run_eval import evaluate  # noqa: E402  —— 打分口径单源

OPTION_KEYS = ("A", "B", "C", "D")

#: 显式标注优先：绝大多数模型会写"答案是 D"/"选B"/"正确选项为C"。
#: 备选分支按「长在前」排：先吃"正确选项是"与"选择"，再吃"应选"，最后才是裸"选"——
#: 裸"选"放最后是必要的，否则"选项 D"会先被裸"选"截断成"选"+"项 D"（项不是字母，
#: 回溯后仍能落到正确的 D，但顺序乱了就不可预期）。
_ANSWER_PATTERNS = (
    re.compile(r"答\s*案\s*(?:是|为|应该是)?\s*[:：]?\s*\(?([A-D])\)?(?![A-Za-z])"),
    re.compile(
        r"(?:正\s*确\s*选\s*项\s*(?:是|为)?|选\s*择|应\s*选|应\s*为|选)"
        r"\s*[:：]?\s*\(?([A-D])\)?(?![A-Za-z])"
    ),
    re.compile(r"^\s*\(?([A-D])\)?\s*[\.、:：\)]\s", re.MULTILINE),
    re.compile(r"^\s*\(?([A-D])\)?\s*$", re.MULTILINE),
)


def _normalize(text: str) -> str:
    """比选项原文时用的归一：去空白、统一全角标点、去 LaTeX 间距命令。"""
    value = str(text or "")
    value = value.replace("，", ",").replace("：", ":").replace("（", "(").replace("）", ")")
    value = re.sub(r"\\(?:quad|,|;|!| )", " ", value)
    value = re.sub(r"\s+", "", value)
    return value.strip()


def extract_letter(message: str, options: Dict[str, str]) -> Tuple[Optional[str], str]:
    """从答案正文抽选项字母。返回 (字母, 抽取方式)。

    抽取方式用于诚实标注：``letter``（显式字母）/ ``text``（靠选项原文反推）/
    ``none``（抽不出）。``text`` 兜底只在**完全抽不到字母**时才启用。
    """
    text = str(message or "")
    if not text.strip():
        return None, "none"
    for pattern in _ANSWER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).upper(), "letter"

    haystack = _normalize(text)
    hits = [
        key
        for key in OPTION_KEYS
        if _normalize(options.get(key, "")) and _normalize(options[key]) in haystack
    ]
    if len(hits) == 1:
        return hits[0], "text"
    return None, "none"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _percentiles(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """延迟摘要：p50/p90 走全仓唯一的最近邻秩实现（``evals/perf_stats``）。

    此前这里 p50 用 ``statistics.median``、p90 用秩——同一份样本两个分位两种尺，
    是本仓分位数口径漂移的一个实例。2026-10-27（T2）统一到最近邻秩 ⇒ p50 在偶数
    样本上可能比历史上一个位次；跨该日期的读数不可直接比。
    空样本沿用本函数的既有契约返回 ``None``（不是 0.0）。
    """
    clean = [float(v) for v in values if isinstance(v, (int, float))]
    if not clean:
        return {"n": 0, "p50": None, "p90": None, "mean": None, "max": None}
    p50 = nearest_rank_quantile(clean, 0.5)
    p90 = nearest_rank_quantile(clean, 0.9)
    return {
        "n": len(clean),
        "p50": round(p50, 1) if p50 is not None else None,
        "p90": round(p90, 1) if p90 is not None else None,
        "mean": round(statistics.fmean(clean), 1),
        "max": round(max(clean), 1),
    }


def _dist(values: Sequence[Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_report(
    golden_rows: List[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    label: str,
) -> Dict[str, Any]:
    golden_by_id = {str(row.get("id")): row for row in golden_rows}

    per_id_prediction: Dict[str, Dict[str, Any]] = {}
    for row in prediction_rows:
        per_id_prediction.setdefault(str(row.get("id")), row)

    correct = 0
    parsed = 0
    unparsed: List[str] = []
    by_method = _dist([])
    method_counts: Dict[str, int] = {}
    per_subject_total: Dict[str, int] = {}
    per_subject_correct: Dict[str, int] = {}
    errors: List[str] = []

    for gid, prediction in per_id_prediction.items():
        golden = golden_by_id.get(gid)
        if golden is None:
            continue
        notes = json.loads(golden.get("notes") or "{}")
        options = notes.get("options") or {}
        gold_letter = str(notes.get("gold_letter") or "")
        subject = str(golden.get("source") or "").split(":", 1)[-1]
        message = str(prediction.get("message") or "")
        if prediction.get("status") == "error":
            errors.append(gid)
        letter, method = extract_letter(message, options)
        method_counts[method] = method_counts.get(method, 0) + 1
        if letter is None:
            unparsed.append(gid)
            per_subject_total[subject] = per_subject_total.get(subject, 0) + 1
            continue
        parsed += 1
        per_subject_total[subject] = per_subject_total.get(subject, 0) + 1
        if letter == gold_letter:
            correct += 1
            per_subject_correct[subject] = per_subject_correct.get(subject, 0) + 1

    total = len(per_id_prediction)
    accuracy = correct / parsed if parsed else None

    metas = [
        row.get("multi_agent")
        for row in prediction_rows
        if isinstance(row.get("multi_agent"), dict)
    ]
    multi_agent: Dict[str, Any] = {"applicable": bool(metas), "rows": len(metas)}
    if metas:
        rounds = [m.get("rounds") for m in metas if isinstance(m.get("rounds"), int)]
        multi_agent.update(
            {
                "exit_reason_dist": _dist([m.get("exit_reason") for m in metas]),
                "status_dist": _dist([m.get("status") for m in metas]),
                "failure_mode_dist": _dist([m.get("failure_mode") for m in metas]),
                "handler_dist": _dist([m.get("handler") for m in metas]),
                "ok_rate": round(sum(1 for m in metas if m.get("ok")) / len(metas), 4),
                "truncated_count": sum(1 for m in metas if m.get("truncated")),
                "rounds_mean": round(statistics.fmean(rounds), 3) if rounds else None,
                "rounds_dist": _dist(rounds),
                "incremental_ratio_mean": round(
                    statistics.fmean(
                        [
                            float(m["incremental_ratio"])
                            for m in metas
                            if isinstance(m.get("incremental_ratio"), (int, float))
                        ]
                    ),
                    4,
                )
                if any(isinstance(m.get("incremental_ratio"), (int, float)) for m in metas)
                else None,
            }
        )

    perf_rows = [row.get("perf") for row in prediction_rows if isinstance(row.get("perf"), dict)]
    latency = _percentiles(
        [p.get("latency_ms") for p in perf_rows if p.get("latency_ms") is not None]
    )
    llm_steps = [p.get("llm_steps") for p in perf_rows if isinstance(p.get("llm_steps"), int)]
    tokens = {
        key: _percentiles([p.get(key) for p in perf_rows if p.get(key) is not None])
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }

    summary = evaluate(golden_rows, prediction_rows)["summary"]

    return {
        "label": label,
        "rows": total,
        "mcq": {
            "parsed": parsed,
            "unparsed": len(unparsed),
            "unparsed_ids": unparsed[:20],
            "correct": correct,
            "accuracy": round(accuracy, 4) if accuracy is not None else None,
            "extraction_method_dist": method_counts,
        },
        "errors": {"count": len(errors), "ids": errors[:20]},
        "route": {
            "accuracy": summary.get("route_accuracy"),
            "dist": summary.get("route_type_distribution"),
        },
        "multi_agent": multi_agent,
        "perf": {
            "latency_ms": latency,
            "llm_steps": {
                "n": len(llm_steps),
                "mean": round(statistics.fmean(llm_steps), 3) if llm_steps else None,
                "dist": _dist(llm_steps),
            },
            "tokens": tokens,
        },
        "harness_summary": summary,
        "per_subject": {
            subject: {
                "total": per_subject_total.get(subject, 0),
                "correct": per_subject_correct.get(subject, 0),
                "accuracy": round(
                    per_subject_correct.get(subject, 0) / per_subject_total[subject], 4
                )
                if per_subject_total.get(subject)
                else None,
            }
            for subject in sorted(per_subject_total)
        },
    }


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4g}{suffix}"
    return f"{value}{suffix}"


def _fmt_dist(dist: Dict[str, Any]) -> str:
    if not dist:
        return "—"
    return " · ".join(f"`{k}` {v}" for k, v in dist.items())


def render_markdown(report: Dict[str, Any]) -> str:
    mcq = report["mcq"]
    ma = report["multi_agent"]
    perf = report["perf"]
    lines: List[str] = []
    lines.append(f"# 多 agent 系统性能报告 · {report['label']}")
    lines.append("")
    lines.append(f"- 题目数：**{report['rows']}**")
    lines.append(
        f"- MCQ 正确率：**{_fmt(mcq['accuracy'])}**（{mcq['correct']}/{mcq['parsed']} 可判分；抽不出字母 {mcq['unparsed']} 题）"
    )
    lines.append(f"- 抽取方式分布：{_fmt_dist(mcq['extraction_method_dist'])}")
    lines.append(f"- 路由准确率（期望 general-query）：{_fmt(report['route']['accuracy'])}")
    lines.append("")

    lines.append("## 延迟与模型调用")
    lines.append("")
    lat = perf["latency_ms"]
    lines.append(
        f"- 单题墙钟：p50 **{_fmt(lat['p50'], ' ms')}** · p90 **{_fmt(lat['p90'], ' ms')}** "
        f"· 均值 {_fmt(lat['mean'], ' ms')} · 最大 {_fmt(lat['max'], ' ms')}（n={lat['n']}）"
    )
    steps = perf["llm_steps"]
    lines.append(
        f"- 每题 LLM 步数：均值 **{_fmt(steps['mean'])}** · 分布 {_fmt_dist(steps['dist'])}"
    )
    for key, label in (
        ("prompt_tokens", "输入 token"),
        ("completion_tokens", "输出 token"),
        ("total_tokens", "总 token"),
    ):
        tok = perf["tokens"][key]
        if tok.get("n"):
            lines.append(
                f"- {label}：p50 {_fmt(tok['p50'])} · 均值 {_fmt(tok['mean'])}（n={tok['n']}）"
            )
    lines.append("")

    lines.append("## 多 agent 出口契约")
    lines.append("")
    if not ma.get("applicable"):
        lines.append("- 不适用（本次录制没有 supervisor 元数据 ⇒ 跑的是扁平拓扑）")
    else:
        lines.append(f"- 覆盖行数：{ma['rows']}")
        lines.append(f"- `exit_reason` 分布：{_fmt_dist(ma.get('exit_reason_dist', {}))}")
        lines.append(f"- `status` 分布：{_fmt_dist(ma.get('status_dist', {}))}")
        lines.append(f"- `failure_mode` 分布：{_fmt_dist(ma.get('failure_mode_dist', {}))}")
        lines.append(f"- `handler` 分布：{_fmt_dist(ma.get('handler_dist', {}))}")
        lines.append(
            f"- ok 率：{_fmt(ma.get('ok_rate'))} · truncated 行数：{ma.get('truncated_count')}"
        )
        lines.append(
            f"- 协调轮数：均值 {_fmt(ma.get('rounds_mean'))} · 分布 {_fmt_dist(ma.get('rounds_dist', {}))}"
        )
        lines.append(
            f"- 信息增量比（incremental_ratio）均值：{_fmt(ma.get('incremental_ratio_mean'))}"
        )
    lines.append("")

    lines.append("## 逐学科正确率")
    lines.append("")
    lines.append("| 学科 | 题数 | 正确 | 正确率 |")
    lines.append("|---|---|---|---|")
    for subject, stats in report["per_subject"].items():
        lines.append(
            f"| {subject} | {stats['total']} | {stats['correct']} | {_fmt(stats['accuracy'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", default="", help="markdown 报告落盘路径")
    parser.add_argument("--json-out", default="", help="结构化报告落盘路径")
    args = parser.parse_args(argv)

    golden_rows = _load_jsonl(Path(args.golden))
    prediction_rows = _load_jsonl(Path(args.predictions))
    report = build_report(golden_rows, prediction_rows, args.label)

    markdown = render_markdown(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown + "\n", encoding="utf-8")
    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
