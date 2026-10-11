"""向量回填脚本（R3.6）：FAISS 存量索引 → 统一 Milvus `retrieval_kb`。

设计（design.md §2.5.3 / §2.10.2-O9 / tasks.md R3.6）：
- 重读 chunk（LangChain FAISS docstore）→ 重嵌入（EMBEDDING_CACHE_ENABLED 时
  自动复用检索域 embedding 缓存，已缓存文本不再计费/计时）→ 批量写入影子集合；
- 蓝绿原子切换：影子集合 `retrieval_kb_next` 校验通过后，rename 旧集合为
  backup、rename 影子为正式名；切换失败自动回滚 rename（旧集合原样保留）；
- 重试复用 retrieval/infra/tasks/reliable.py 的 RetryPolicy（指数退避）；
- `--no-switch`：只回填影子集合，不切换（人工复核后手动二次运行切换）。

运行命令（My-Agentix 目录，项目 .venv）：

    ./.venv/Scripts/python.exe scripts/migration/backfill_vectors.py \
        --source-dir ./data/indexes \
        --host 127.0.0.1 --port 19530

    # 仅回填不切换
    ./.venv/Scripts/python.exe scripts/migration/backfill_vectors.py --no-switch
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from agentix.retrieval.infra.tasks.reliable import RetryPolicy, run_with_retry  # noqa: E402

DEFAULT_COLLECTION = "retrieval_kb"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="R3.6 向量回填（FAISS → Milvus retrieval_kb，蓝绿切换）")
    p.add_argument("--source-dir", default=str(PROJECT_ROOT / "data" / "indexes"), help="FAISS 索引根目录")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=19530)
    p.add_argument("--collection", default=DEFAULT_COLLECTION, help="正式集合名（蓝）")
    p.add_argument("--shadow-collection", default=None, help="影子集合名（绿，默认 <collection>_next）")
    p.add_argument("--tenant", default="default", help="存量数据归属租户")
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--no-switch", action="store_true", help="只回填影子集合，不执行切换")
    p.add_argument("--report", default="")
    return p.parse_args()


def _index_mtime(version_dir: Path) -> float:
    """版本新鲜度 = 该目录内 index.faiss 的 mtime（目录 mtime 在不同
    文件系统上精度不足且不随内容更新，用目录排序会导致同刻创建的
    版本目录排序不稳定，回填可能选到旧版本）。"""
    try:
        return (version_dir / "index.faiss").stat().st_mtime
    except OSError:
        return 0.0


def resolve_fresh_version_dir(source_dir: str | Path) -> Path:
    """定位最新可用版本目录：优先含 index.faiss+index.pkl 的子目录（按 index.faiss mtime 最新）。"""
    root = Path(source_dir)
    if not root.exists():
        raise FileNotFoundError(f"索引目录不存在: {root}")
    candidates = []
    if (root / "index.faiss").exists():
        candidates.append(root)
    for child in sorted(root.iterdir(), key=_index_mtime, reverse=True):
        if child.is_dir() and (child / "index.faiss").exists():
            candidates.append(child)
    if not candidates:
        raise FileNotFoundError(f"{root} 下未找到任何含 index.faiss 的版本目录")
    return candidates[0]


def load_chunks(source_path: str | Path) -> list[dict]:
    """从 LangChain FAISS docstore 读出全部 chunk（text + metadata）。"""
    from langchain_community.vectorstores import FAISS  # 延迟导入：脚本运行期才需要

    from agentix.retrieval.infra.vectorstore.faiss_manager import FAISSVectorStoreManager

    manager = FAISSVectorStoreManager()
    vs = FAISS.load_local(
        str(source_path),
        manager.embeddings,
        allow_dangerous_deserialization=True,  # 本仓自产索引；迁移属离线受控操作
    )
    chunks = []
    for doc in vs.docstore._dict.values():  # noqa: SLF001 - LangChain 内部结构的受控访问
        chunks.append({"text": doc.page_content, "metadata": dict(doc.metadata or {})})
    return chunks


def build_green_store(host: str, port: int, shadow_collection: str):
    """连接 Milvus 并拿到影子集合的 VectorStore（不存在则自动建）。"""
    from agentix.retrieval.infra.vectorstore.milvus_impl import MilvusVectorStore

    store = MilvusVectorStore(
        collection_name=shadow_collection,
        host=host,
        port=port,
    )
    if store._collection is None:
        raise ConnectionError(f"Milvus 影子集合 {shadow_collection} 初始化失败（服务不可达或建集合失败）")
    return store


def upsert_chunks(store, chunks: list[dict], *, tenant: str, batch_size: int, max_attempts: int) -> int:
    """批量写入影子集合（每个批次一次 RetryPolicy 重试），返回写入条数。"""
    from agentix.retrieval.infra.vectorstore.base import StoredVectorDocument

    policy = RetryPolicy(max_attempts=max_attempts)
    written = 0
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        docs = [
            StoredVectorDocument(
                id=f"bf_{i + j}",
                text=c["text"],
                metadata={**c["metadata"], "tenant_id": c["metadata"].get("tenant_id") or tenant},
            )
            for j, c in enumerate(batch)
        ]

        def _op(docs=docs):
            store.add_documents(docs)

        result = run_with_retry(_op, policy)
        if not result.ok:
            raise RuntimeError(f"批次 {i} 回填失败（attempts={result.attempts}）: {result.error}")
        written += len(docs)
    return written


def switch_blue_green(utility_mod, *, shadow: str, blue: str) -> str:
    """原子切换：shadow→blue，blue→backup。失败自动回滚。返回动作描述。

    utility_mod 需提供 has_collection / rename_collection（pymilvus.utility）。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not utility_mod.has_collection(shadow):
        raise RuntimeError(f"影子集合 {shadow} 不存在，无法切换")
    backup = f"{blue}_backup_{ts}"
    had_blue = utility_mod.has_collection(blue)
    if had_blue:
        utility_mod.rename_collection(blue, backup)
    try:
        utility_mod.rename_collection(shadow, blue)
    except Exception:
        if had_blue:
            utility_mod.rename_collection(backup, blue)  # 回滚：旧集合复位
        raise
    return f"switched: {shadow} -> {blue}" + (f", old -> {backup}" if had_blue else "")


def main() -> int:
    args = _parse_args()
    shadow = args.shadow_collection or f"{args.collection}_next"

    report: dict = {"source_dir": args.source_dir, "collection": args.collection, "shadow": shadow}

    version_dir = resolve_fresh_version_dir(args.source_dir)
    report["version_dir"] = str(version_dir)
    print(f"[1/4] 使用版本目录: {version_dir}")

    chunks = load_chunks(version_dir)
    report["chunks"] = len(chunks)
    print(f"[2/4] 读取 chunk: {len(chunks)} 条")

    store = build_green_store(args.host, args.port, shadow)
    written = upsert_chunks(store, chunks, tenant=args.tenant, batch_size=args.batch_size, max_attempts=args.max_attempts)
    report["written"] = written
    print(f"[3/4] 影子集合 {shadow} 写入: {written} 条")

    # 校验：影子集合实体数应 ≥ 写入数（flush 后精确）
    entity_count = store._collection.num_entities
    report["shadow_entities"] = entity_count
    if entity_count < written:
        report["verdict"] = "fail"
        print(f"[FAIL] 影子集合实体数 {entity_count} < 写入数 {written}")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    if args.no_switch:
        report["verdict"] = "ok(no-switch)"
        print(f"[4/4] 跳过切换（--no-switch）。影子集合 {shadow} 已就绪，复核后手动切换。")
    else:
        from pymilvus import utility

        action = switch_blue_green(utility, shadow=shadow, blue=args.collection)
        report["switch"] = action
        print(f"[4/4] {action}")

    report["verdict"] = "ok"
    report_path = args.report or str(PROJECT_ROOT / "scripts" / "migration" / "backfill_report.json")
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {report_path} ｜ 结论: {report['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
