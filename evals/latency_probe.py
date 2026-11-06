"""延迟侧真值探针：SingleFlight 合并、引用还原批量化、Redis 连接池读取点。

不依赖外部服务：SingleFlight 用纯 asyncio 计数；引用还原用内存 SQLite +
SQLAlchemy ``before_cursor_execute`` 事件数真实 SQL 条数；Redis 连接池只做
静态定位（真实读取点 + 并发上限推算），不伪造命中率。

用法：
  .venv/Scripts/python.exe evals/latency_probe.py --probe all \
      --write-summary docs/evals/stard-latency-probe-summary.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def probe_singleflight(n: int = 16, sleep_s: float = 0.05) -> dict[str, Any]:
    """相同 key 并发 miss：flag-off 各跑一次，flag-on 只跑一次。"""
    from agentix.infrastructure.cache.singleflight import SingleFlight

    async def run(use_singleflight: bool) -> dict[str, Any]:
        calls = 0
        sf = SingleFlight()

        async def factory() -> str:
            nonlocal calls
            calls += 1
            await asyncio.sleep(sleep_s)
            return "ok"

        started = time.perf_counter()
        if use_singleflight:
            results = await asyncio.gather(*[sf.do("same-key", factory) for _ in range(n)])
        else:
            results = await asyncio.gather(*[factory() for _ in range(n)])
        wall_ms = (time.perf_counter() - started) * 1000.0
        assert len(results) == n
        return {"downstream_calls": calls, "wall_ms": round(wall_ms, 2)}

    off = asyncio.run(run(False))
    on = asyncio.run(run(True))
    return {
        "concurrent_queries": n,
        "factory_sleep_ms": sleep_s * 1000,
        "flag_off": off,
        "flag_on": on,
        "reduction": f"{off['downstream_calls']} -> {on['downstream_calls']}",
        "note": "下游指 retrieve_scoped_docs_cached miss 主体；只计合并次数，不含缓存命中率",
    }


def probe_citation(n: int = 8) -> dict[str, Any]:
    """用内存 SQLite 数真实 SQL：旧逐条路径 vs 新按 document 聚合路径。"""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlmodel import Session, SQLModel, create_engine

    from agentix.retrieval.domain.models.document import (
        Chunk,
        Document,
        FileObject,
        KbFile,
    )
    from agentix.retrieval.infra.repos import document_repo

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[Document.__table__, FileObject.__table__, KbFile.__table__, Chunk.__table__],
    )
    with Session(engine) as session:
        for i in range(n):
            session.add(
                FileObject(object_key=f"k{i}", sha256=f"h{i}", size_bytes=1024, file_type="pdf")
            )
        session.flush()
        objects = session.query(FileObject).all()
        for i in range(n):
            kbf = KbFile(
                kb_id="kb0",
                file_object_id=objects[i].id,
                original_filename=f"doc{i}.pdf",
                uploaded_by="probe",
            )
            session.add(kbf)
        session.flush()
        kb_files = session.query(KbFile).all()
        for i in range(n):
            session.add(
                Document(
                    kb_id="kb0",
                    kb_file_id=kb_files[i].id,
                    title=f"文档{i}",
                    status="ready",
                    folder_path="/probe",
                )
            )
        session.flush()
        documents = session.query(Document).all()
        for i in range(n):
            session.add(
                Chunk(
                    id=f"c{i}",
                    document_id=documents[i].id,
                    content=f"内容{i}" * 40,
                    page_no=i + 1,
                )
            )
        session.commit()
        chunk_ids = [f"c{i}" for i in range(n)]
        doc_ids = [d.id for d in documents]

    counters: dict[str, int] = {"sql": 0}

    @event.listens_for(Engine, "before_cursor_execute")
    def _count(_conn, _cursor, _statement, _parameters, _context, _executemany):
        counters["sql"] += 1

    with Session(engine) as session:
        # 旧路径：每条引用 get_chunk_by_id + get_document_file_meta
        counters["sql"] = 0
        for cid in chunk_ids:
            chunk = document_repo.get_chunk_by_id(session, cid)
            document_repo.get_document_file_meta(session, chunk.document_id if chunk else None)
        old_sql = counters["sql"]

        # 新路径：一次 IN 查 chunk + 三次 IN 查文件级元信息
        counters["sql"] = 0
        chunks = document_repo.get_chunks_by_ids(session, chunk_ids)
        metas = document_repo.get_document_file_metas(
            session, [str(c.document_id) for c in chunks.values() if c.document_id]
        )
        new_sql = counters["sql"]
        assert len(chunks) == n and len(metas) >= n - 1

    event.remove(Engine, "before_cursor_execute", _count)
    return {
        "citations": n,
        "old_sql_statements": old_sql,
        "new_sql_statements": new_sql,
        "reduction": f"{old_sql} -> {new_sql}",
        "old_formula": "每条引用 1 次 session.get(Chunk) + 至多 3 次 session.get(Document/KbFile/FileObject)",
        "new_formula": "1 次 IN(Chunk) + 1 次 IN(Document) + 1 次 IN(KbFile) + 1 次 IN(FileObject)",
        "note": "SQLAlchemy 事件计数；session.get 会命中 identity map，数字是保守下界",
    }


def probe_redis() -> dict[str, Any]:
    """定位 CACHE_REDIS_MAX_CONNECTIONS 的真实读取点与并发上限关系。"""
    import concurrent.futures
    import os

    from agentix.retrieval.settings import settings

    default_workers = min(32, (os.cpu_count() or 1) + 4)
    pool_size = int(settings.CACHE_REDIS_MAX_CONNECTIONS)
    return {
        "max_connections": pool_size,
        "config_source": "agentix/retrieval/settings.py::CACHE_REDIS_MAX_CONNECTIONS",
        "pool_creation": "agentix/retrieval/infra/cache/redis_client.py::get_cache_redis -> redis.Redis.from_url(max_connections=...)",
        "read_site": "agentix/retrieval/usecases/chat_service.py::_compose_key 之后 await asyncio.to_thread(cache_redis.get, cache_key)",
        "write_site": "同文件 miss 分支 await asyncio.to_thread(cache_redis.set, ...)",
        "default_threadpool_max_workers": default_workers,
        "conclusion": (
            "连接池不是瓶颈"
            if pool_size >= default_workers
            else "连接池小于线程池上限，需上调 CACHE_REDIS_MAX_CONNECTIONS"
        ),
        "note": "静态定位 + 本机 CPU 推算；未伪造命中率/QPS",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", choices=("singleflight", "citation", "redis", "all"), default="all")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--write-summary", type=Path)
    args = ap.parse_args()

    report: dict[str, Any] = {
        "schema": 1,
        "reproduce": f"{sys.executable} evals/latency_probe.py " + " ".join(sys.argv[1:]),
        "probes": {},
    }
    if args.probe in ("singleflight", "all"):
        report["probes"]["singleflight"] = probe_singleflight(n=args.concurrency)
    if args.probe in ("citation", "all"):
        report["probes"]["citation"] = probe_citation(n=args.n)
    if args.probe in ("redis", "all"):
        report["probes"]["redis"] = probe_redis()
    print(json.dumps(report["probes"], ensure_ascii=False, indent=2), flush=True)
    if args.write_summary:
        args.write_summary.parent.mkdir(parents=True, exist_ok=True)
        args.write_summary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[saved] {args.write_summary}", flush=True)


if __name__ == "__main__":
    main()
