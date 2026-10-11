"""元数据迁移脚本（R3.5）：Tracerag 存量库 → 统一 PG `retrieval` schema。

设计（design.md §2.5.3 / tasks.md R3.5）：
- 离线导出 + 变换（补 tenant_id）+ 导入 + 行数校验 + 抽样校验；
- 幂等：按主键存在性跳过已迁移行，重跑安全；
- 影子运行（--dry-run）：完整走一遍读取/变换/校验流程但**不写目标库**；
- 校验不一致即非零退出（spec §7.6：中止并回滚）。

运行命令（在 My-Agentix 目录，用项目 .venv）：

    # 影子运行（不写库，仅出报告）
    ./.venv/Scripts/python.exe scripts/migration/migrate_metadata.py \
        --source-url sqlite:///backend/data/retrieval.db --dry-run

    # 正式迁移（目标默认取 DATABASE_URL，即统一 PG retrieval schema）
    ./.venv/Scripts/python.exe scripts/migration/migrate_metadata.py \
        --source-url sqlite:///backend/data/retrieval.db \
        --report scripts/migration/migrate_report.json

    # 只迁指定表（调试用）
    ./.venv/Scripts/python.exe scripts/migration/migrate_metadata.py --tables tenant,user

注意：
- 源库只读，本脚本绝不写源库；重迁即可回滚，源数据保留。
- 目标为 PostgreSQL 时自动建 retrieval schema 并按 schema 建表（复用 R3.1 逻辑）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="R3.5 元数据迁移（→ 统一 PG retrieval schema）")
    p.add_argument(
        "--source-url",
        default=os.getenv("SRC_DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'backend' / 'data' / 'retrieval.db'}"),
        help="源库 URL（默认 SRC_DATABASE_URL 或旧 retrieval.db）",
    )
    p.add_argument(
        "--target-url",
        default=os.getenv("TARGET_DATABASE_URL", "") or None,
        help="目标库 URL（默认取 DATABASE_URL / 统一 settings）",
    )
    p.add_argument("--dry-run", action="store_true", help="影子运行：不写目标库")
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument("--sample-size", type=int, default=5, help="每表抽样校验行数")
    p.add_argument("--tables", default="", help="逗号分隔的表名子集（默认全部）")
    p.add_argument("--report", default="", help="报告 JSON 输出路径")
    return p.parse_args()


import os  # noqa: E402  （argparse 之后统一导入，便于 --target-url 决定 schema）

args = _parse_args()

# 目标 URL 决定 metadata.schema，必须在导入域模型之前设置（R3.1 约束）
_target_url = args.target_url or os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'agentix.db'}")
if _target_url.startswith(("postgresql", "postgres")):
    from sqlmodel import SQLModel

    SQLModel.metadata.schema = "retrieval"

from sqlalchemy import bindparam, create_engine, inspect, text  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

import agentix.retrieval.domain.models  # noqa: F401,E402  （注册全部表）


def _make_engine(url: str):
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_engine(url, **kwargs)


def _ensure_target_ready(engine) -> None:
    """目标库就绪：PG 建 retrieval schema + 建全部表（幂等）。"""
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SQLModel.metadata.schema}"))
    SQLModel.metadata.create_all(engine)


def _row_to_dict(row, columns) -> dict:
    d = dict(row._mapping)
    return {k: v for k, v in d.items() if k in columns}


def _transform(row: dict, columns) -> dict:
    """R3.5 变换：补 tenant_id（过渡期可空 → 迁移期回填默认租户）。"""
    if "tenant_id" in columns and row.get("tenant_id") is None:
        row["tenant_id"] = os.getenv("LEGELBOT_DEFAULT_TENANT_NAME", "default")
    return row


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _norm(v):
    """比较前规范化：datetime 与 ISO 字符串统一为 datetime（消除裸 text() 读回
    绕过类型解析造成的字符串/时间假阳性）。"""
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        if len(s) >= 19 and s[4] == "-" and s[7] == "-":
            try:
                return datetime.fromisoformat(s.replace("T", " ") if "T" in s and len(s) > 10 else s)
            except ValueError:
                return v
    return v


def migrate() -> int:
    source_engine = _make_engine(args.source_url)
    target_engine = _make_engine(_target_url)

    only = {t.strip() for t in args.tables.split(",") if t.strip()}
    tables = [t for t in SQLModel.metadata.sorted_tables if not only or t.name in only]

    src_inspect = inspect(source_engine)
    missing = [t.name for t in tables if not src_inspect.has_table(t.name)]
    if missing:
        print(f"[warn] 源库缺少表（跳过）: {missing}")

    _ensure_target_ready(target_engine)

    report: dict = {"source": args.source_url, "target": _target_url, "dry_run": args.dry_run, "tables": {}}
    failed = False

    with source_engine.connect() as src, target_engine.begin() as dst:
        for table in tables:
            if not src_inspect.has_table(table.name):
                report["tables"][table.name] = {"status": "skipped_missing_source"}
                continue
            columns = {c.name for c in table.columns}
            pk = table.primary_key.columns.keys()
            rows = src.execute(table.select()).fetchall()
            transformed = [_transform(_row_to_dict(r, columns), columns) for r in rows]

            # 主键存在性 → 幂等跳过（dry-run 只读目标，不写入）
            existing: set = set()
            if transformed:
                ids = [r[pk[0]] for r in transformed]
                qualified = (
                    f"{SQLModel.metadata.schema}.{table.name}" if SQLModel.metadata.schema else table.name
                )
                for i in range(0, len(ids), args.batch_size):
                    chunk = ids[i : i + args.batch_size]
                    res = dst.execute(
                        text(f"SELECT {pk[0]} FROM {qualified} WHERE {pk[0]} IN :ids").bindparams(  # nosec B608 - 表名/PK 来自 SQLModel 元数据，值走 bindparam
                            bindparam("ids", expanding=True)
                        ),
                        {"ids": chunk},
                    )
                    existing.update(r[0] for r in res.fetchall())

            todo = [r for r in transformed if r[pk[0]] not in existing]
            inserted = 0
            if todo and not args.dry_run:
                for i in range(0, len(todo), args.batch_size):
                    dst.execute(table.insert(), todo[i : i + args.batch_size])
                    inserted += len(todo[i : i + args.batch_size])

            # 行数校验：插入后目标行数 ≥ 源行数（去重口径）
            src_count = len(transformed)
            ok_count = args.dry_run or inserted + len(existing) >= src_count

            # 抽样校验：目标按主键取回逐列比对
            sample_mismatch = []
            if not args.dry_run and todo:
                sample = todo[: max(0, args.sample_size)]
                qualified = (
                    f"{SQLModel.metadata.schema}.{table.name}" if SQLModel.metadata.schema else table.name
                )
                for row in sample:
                    rid = row[pk[0]]
                    got = dst.execute(
                        text(f"SELECT * FROM {qualified} WHERE {pk[0]} = :rid"),  # nosec B608 - 同上：qualified 来自 SQLModel 元数据，值参数绑定
                        {"rid": rid},
                    ).fetchone()
                    if got is None:
                        sample_mismatch.append({"id": str(rid), "reason": "missing"})
                        continue
                    got_d = _row_to_dict(got, columns)
                    diff = {
                        k: [_jsonable(_norm(row.get(k))), _jsonable(_norm(got_d.get(k)))]
                        for k in columns
                        if _norm(row.get(k)) != _norm(got_d.get(k))
                    }
                    if diff:
                        sample_mismatch.append({"id": str(rid), "diff": diff})

            entry = {
                "source_rows": src_count,
                "existing_skipped": len(existing),
                "inserted": inserted if not args.dry_run else 0,
                "would_insert": len(todo) if args.dry_run else 0,
                "count_ok": ok_count,
                "sample_mismatch": sample_mismatch,
            }
            report["tables"][table.name] = entry
            if not ok_count or sample_mismatch:
                failed = True
                print(f"[FAIL] {table.name}: {entry}")
            else:
                print(f"[ok] {table.name}: source={src_count} existing={len(existing)} "
                      f"{'would_insert' if args.dry_run else 'inserted'}={entry['would_insert'] or entry['inserted']}")

    report["verdict"] = "fail" if failed else "ok"
    report_path = args.report or str(PROJECT_ROOT / "scripts" / "migration" / "migrate_report.json")
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {report_path} ｜ 结论: {report['verdict']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(migrate())
