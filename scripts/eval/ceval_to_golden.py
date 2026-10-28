"""把公开数据集（C-Eval）转成 ``evals/run_eval.py`` 认的 golden 行。

为什么要有这个脚本：仓内原法律金标集已随知识图谱 / RAG 评测下线（2026-10-26），
评测框架本身留着。要评多 agent 系统的性能，需要一份**有出处、可回源校验**的
公开评测集——不能自己编题（编出来的题只能证伪不能证真），也不能手抄答案。

本脚本的纪律：

1. **只用公开数据集**，不生成、不改写题目本身；选项与答案原样取自源数据集。
2. **分层抽样**：按学科（config）分层，每科取 N 题 ⇒ 覆盖面不因抽样而塌成
   「全是某几科」。
3. **带回源校验**：写盘之后**重新打开产物**、**重新加载源数据集**，逐行比对
   题干、四个选项、正确答案——产物与源不一致 ⇒ 退出码 2，不留半成品。
4. **出处随行**：每行带 ``source``（数据集:学科）与 ``notes``（源行 id、标准
   答案字母、四个选项），事后能逐题追回公开数据集的原始行。

C-Eval 数据集卡声明的许可为 **CC BY-NC-SA 4.0**（非商业 + 相同方式共享），
出处与许可随 manifest 一起落盘，换机器复现时不必再查。

用法::

    HF_ENDPOINT=https://hf-mirror.com \\
    .\\.venv\\Scripts\\python.exe scripts/eval/ceval_to_golden.py --per-subject 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台默认 GBK，本脚本输出含中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals.run_eval import ALLOWED_ROUTE_TYPES  # noqa: E402  —— 真源在 evals 包内

#: 公开数据集标识与许可（取自其数据集卡 license 字段）。
DEFAULT_DATASET = "ceval/ceval-exam"
DATASET_LICENSE = "cc-by-nc-sa-4.0"
DATASET_LICENSE_URL = "https://huggingface.co/datasets/ceval/ceval-exam"

#: 默认端点：本机直连 huggingface.co 不通时走国内镜像；可用 --endpoint 覆盖。
DEFAULT_ENDPOINT = "https://hf-mirror.com"

#: 期望路由。C-Eval 是**纯文本知识问答**：没有附件、没有图、没有表、没有 DB
#: 也没有知识图谱在评测范围内 ⇒ 唯一正确的出口是 ``general-query``。
#:
#: 诚实的边界：这不是"用户问知识题就该走 general"的普适结论——真实部署里用户
#: 问"退休年龄是多少"可能确实想查语料库。本评测把语料检索排除在范围外，所以
#: 期望路由取 general-query；路由是否符合预期由 route_accuracy 单独度量。
ROUTE_FOR_PLAIN_QA = "general-query"

#: 选项字母。源数据集固定四选项 A-D。
OPTION_KEYS = ("A", "B", "C", "D")

#: 提示语。必须与题目一起发出去，否则 agent 可能自由作答、答案无从判分。
MCQ_INSTRUCTION = "请只回答正确选项的字母和对应内容，例如：答案 D. <选项内容>"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to_repo(path: Path) -> str:
    """仓内路径用相对写法；仓外路径原样返回（不假装它在仓里）。"""
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path.resolve())


def _build_question(question: str, options: Dict[str, str]) -> str:
    """题干 + 四个选项 + 作答指令（选项原样，不改一个字）。"""
    lines = [question.strip()]
    lines.extend(f"{key}. {str(options[key]).strip()}" for key in OPTION_KEYS)
    lines.append(MCQ_INSTRUCTION)
    return "\n".join(lines)


def _row_to_golden(subject: str, row: Dict[str, Any], source_split: str) -> Dict[str, Any]:
    """源行 → golden 行。字段名与 ``evals/README.md`` §2 的 schema 对齐。"""
    options = {key: str(row.get(key) or "") for key in OPTION_KEYS}
    gold = str(row.get("answer") or "").strip().upper()
    if gold not in OPTION_KEYS:
        raise ValueError(f"源行答案不是合法选项字母：{gold!r}")
    if not all(options[key].strip() for key in OPTION_KEYS):
        raise ValueError("源行存在空选项")

    return {
        "id": f"ceval-{subject}-{row.get('id')}",
        "question": _build_question(str(row.get("question") or ""), options),
        "route_type": ROUTE_FOR_PLAIN_QA,
        "expected_sources": [],
        # must_contain 用**正确选项的原文**：run_eval 的 must_contain 是子串命中，
        # 选项原文比单个字母抗噪（字母 D 会出现在正文各处）。
        "must_contain": [options[gold]],
        "expect_reject": False,
        "source": f"{DEFAULT_DATASET}:{subject}",
        # notes：源行 id / 标准答案 / 四选项 —— 事后可逐题追回源数据集原始行。
        "notes": json.dumps(
            {
                "src_id": row.get("id"),
                "src_split": source_split,
                "gold_letter": gold,
                "options": options,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def _sample_rows(
    rows: List[Dict[str, Any]], subject: str, per_subject: int, seed: int
) -> List[Dict[str, Any]]:
    """按学科分层抽样：稳定种子 ⇒ 同一学科每次抽样结果一致。"""
    ordered = list(rows)
    random.Random(f"{seed}:{subject}").shuffle(ordered)
    return ordered[:per_subject]


def _resolve_revision(dataset: str, endpoint: str) -> str:
    """取数据集当前 revision（commit sha）——出处的一部分，锁死"用的是哪一版"。"""
    try:
        from huggingface_hub import HfApi

        return str(HfApi(endpoint=endpoint).dataset_info(dataset).sha)
    except Exception as exc:  # noqa: BLE001 —— 取不到不该阻断抽样
        print(f"[警告] 取不到 dataset revision（{exc}），manifest 里记为 unknown")
        return "unknown"


def _load_subject_rows(dataset: str, subject: str, split: str) -> List[Dict[str, Any]]:
    from datasets import load_dataset

    loaded = load_dataset(dataset, subject, split=split)
    return [dict(item) for item in loaded]


def build_golden(
    dataset: str,
    split: str,
    per_subject: int,
    seed: int,
    subjects: Iterable[str] | None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """抽样 + 转换。返回 (golden 行, 每科题数)。"""
    from datasets import get_dataset_config_names

    all_subjects = list(get_dataset_config_names(dataset))
    wanted = [s for s in subjects] if subjects else all_subjects
    unknown = [s for s in wanted if s not in all_subjects]
    if unknown:
        raise SystemExit(f"[错误] 数据集里没有这些 config：{unknown}")

    golden_rows: List[Dict[str, Any]] = []
    per_subject_counts: Dict[str, int] = {}
    for subject in wanted:
        rows = _load_subject_rows(dataset, subject, split)
        picked = _sample_rows(rows, subject, per_subject, seed)
        for row in picked:
            golden_rows.append(_row_to_golden(subject, row, split))
        per_subject_counts[subject] = len(picked)
    return golden_rows, per_subject_counts


def verify_against_source(
    golden_path: Path,
    dataset: str,
    split: str,
) -> Dict[str, Any]:
    """回源校验：重开产物 + 重载源数据，逐行比对。

    比对四件事：题干（去掉作答指令后应等于源题干 + 源选项）、四个选项、
    标准答案字母、source 学科标记。任一不符 ⇒ 抛错退出（调用方转成退出码 2）。
    """
    source_cache: Dict[str, List[Dict[str, Any]]] = {}
    checked = 0
    for line_no, line in enumerate(golden_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        notes = json.loads(row["notes"])
        subject = row["source"].split(":", 1)[1]
        if subject not in source_cache:
            source_cache[subject] = _load_subject_rows(dataset, subject, split)
        src = next((r for r in source_cache[subject] if r.get("id") == notes["src_id"]), None)
        if src is None:
            raise ValueError(f"第 {line_no} 行：源数据集里找不到 id={notes['src_id']}")

        options = {key: str(src.get(key) or "") for key in OPTION_KEYS}
        if notes["options"] != options:
            raise ValueError(f"第 {line_no} 行：选项与源不一致")
        if str(src.get("answer") or "").strip().upper() != notes["gold_letter"]:
            raise ValueError(f"第 {line_no} 行：标准答案与源不一致")
        expected_question = _build_question(str(src.get("question") or ""), options)
        if row["question"] != expected_question:
            raise ValueError(f"第 {line_no} 行：题干与源不一致")
        if row["route_type"] not in ALLOWED_ROUTE_TYPES:
            raise ValueError(f"第 {line_no} 行：route_type 不在 7 类白名单内")
        if row["must_contain"] != [options[notes["gold_letter"]]]:
            raise ValueError(f"第 {line_no} 行：must_contain 与标准答案选项不一致")
        checked += 1

    return {"rows_checked": checked, "subjects_loaded": len(source_cache)}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--split", default="test")
    parser.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT") or DEFAULT_ENDPOINT)
    parser.add_argument("--per-subject", type=int, default=3, help="每科抽多少题")
    parser.add_argument("--seed", type=int, default=20261026)
    parser.add_argument("--subjects", default="", help="逗号分隔；留空 = 全部 config")
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "ceval_golden.jsonl"))
    parser.add_argument(
        "--manifest", default=str(REPO_ROOT / "evals" / "ceval_golden.manifest.json")
    )
    args = parser.parse_args(argv)

    # datasets / huggingface_hub 在 import 时读 HF_ENDPOINT，必须先设好再导。
    os.environ["HF_ENDPOINT"] = args.endpoint
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    subjects = [s.strip() for s in args.subjects.split(",") if s.strip()]
    golden_rows, per_subject_counts = build_golden(
        dataset=args.dataset,
        split=args.split,
        per_subject=args.per_subject,
        seed=args.seed,
        subjects=subjects or None,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in golden_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    try:
        verification = verify_against_source(out_path, args.dataset, args.split)
    except Exception as exc:  # noqa: BLE001 —— 回源不过 = 产物不可信
        print(f"[错误] 回源校验失败：{exc}")
        print(f"[错误] 已写入的产物不可信，请删除后重试：{out_path}")
        return 2

    revision = _resolve_revision(args.dataset, args.endpoint)
    manifest = {
        "dataset": args.dataset,
        "split": args.split,
        "revision": revision,
        "endpoint": args.endpoint,
        "license": DATASET_LICENSE,
        "license_url": DATASET_LICENSE_URL,
        "sampling": {
            "method": "per-subject shuffled head",
            "per_subject": args.per_subject,
            "seed": args.seed,
            "subjects": len(per_subject_counts),
        },
        "rows": len(golden_rows),
        "rows_per_subject": per_subject_counts,
        "route_rule": (
            "全部行 → general-query（C-Eval 是纯文本知识问答："
            "无附件/无图/无表/无 DB/无知识图谱在评测范围内）"
        ),
        "golden_path": _relative_to_repo(out_path),
        "golden_sha256": _sha256_file(out_path),
        "verification": verification,
        "generator": "scripts/eval/ceval_to_golden.py",
    }
    manifest_path = Path(args.manifest)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"[完成] {len(golden_rows)} 题 × {len(per_subject_counts)} 学科 → {out_path}")
    print(f"[回源校验] 逐行比对通过：{verification['rows_checked']} 行")
    print(f"[出处] {args.dataset} @ {revision}（{DATASET_LICENSE}）")
    print(f"[manifest] {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
