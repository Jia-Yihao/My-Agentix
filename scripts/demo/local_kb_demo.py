#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小可演示切片（local kb demo）：SQLite + FAISS + LM Studio，一条带引用的 RAG 问答。

为什么存在
----------
README §10 的全栈路径要 8 个容器 + 远程 LLM，演示成本压不住。本脚本把
「L1 路由 → L2 kb 检索 → 带引用回答」压进同一台机器上的三条命令：

* 语料 = ``data/kb/legal_corpus.txt``（4,865 条数据条目，与 §15.1 同一份）；
* 元数据 = SQLite 文件（``data/demo/``，gitignored）；
* 向量索引 = FAISS 本地目录（``data/indexes/``，gitignored）；
* 嵌入与生成 = LM Studio 的 OpenAI 兼容端口（默认 ``127.0.0.1:1234``）。

不需要 Neo4j / Milvus / DeepSeek，也不需要起后端进程。

纪律（对齐 AGENTS.md）
----------------------
1. **纯新增**：不改既有脚本、测试与评测基线；只写 ``data/demo/``、
   ``data/indexes/`` 与 ``--json-out`` 指定的产物。
2. **不新增 Settings 字段**（避开配置键五处同步）：只经 ``os.environ`` 提供
   默认值——pydantic-settings 里进程环境优先级高于 ``.env``，因此本机演示
   不需要改 ``.env``。
3. **检索方式统一**：使用项目默认的 Dense + BM25/z-score；sparse 故障时沿用
   主检索链的 Dense 降级行为。
4. **指标边界清晰**：此端到端 smoke eval 的 query hit rate 仅作演示诊断；
   检索质量以统一 STARD 相关文档级 Recall@K 报告为准。

用法
----
    python scripts/demo/local_kb_demo.py seed                       # 建库 + 建索引
    python scripts/demo/local_kb_demo.py ask "以欺诈手段订立的合同效力如何"
    python scripts/demo/local_kb_demo.py eval --limit 23 --json-out data/demo/eval.json

``ask`` / ``eval`` 需要 LM Studio 服务端在跑且模型可加载
（``lms status`` 应为 ON；嵌入默认 ``text-embedding-bge-m3``，
生成默认 ``qwen/qwen3-8b``，均可用环境变量覆盖）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
# 直跑脚本时 sys.path[0] 是 scripts/demo/，仓库根不在搜索路径里；不依赖
# `pip install -e .` 也能跑，是「3 条命令」演示的前提之一。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 分位数的唯一实现与"默认题集缺失就响亮退出"的判定都住在 `evals/` 包里：
# 本脚本是评测入口之一，不许另立第二套尺子或第二份"默认题集"判据（约定 6 ④）。
from evals.golden_source import ensure_default_golden  # noqa: E402 - 依赖上面的 ROOT 注入
from evals.perf_stats import nearest_rank_quantile  # noqa: E402 - 同上

DEMO_DIR = ROOT / "data" / "demo"
CORPUS_PATH = ROOT / "data" / "kb" / "legal_corpus.txt"
GOLDEN_PATH = ROOT / "evals" / "legal_golden.jsonl"

DEFAULT_KB_ID = "legal-demo"
DEFAULT_TENANT = "demo-tenant"
DEFAULT_EMBED_MODEL = "text-embedding-bge-m3"
DEFAULT_CHAT_MODEL = "qwen/qwen3-8b"
LMSTUDIO_BASE_URL = "http://127.0.0.1:1234/v1"
# 与 evals/README 登记的同源口径：该语料按「# 《X》第N条」切出 4,865 条数据条目。
# 解析结果不等于它，就说明语料或解析任一侧漂了，指标不能与 §15.1 并排读。
EXPECTED_CORPUS_ENTRIES = 4865


def _bootstrap_env(args: argparse.Namespace) -> None:
    """在导入 agentix 之前把本地默认值塞进进程环境。

    ``agentix.config.settings`` 与 ``agentix.retrieval.settings`` 都在**导入时**
    读 env（且 ``database.engine`` 是导入时建好的单例），所以任何
    ``from agentix...`` 都必须发生在本函数之后——这就是本文件所有 agentix
    导入都写在函数体里的原因，不是风格问题。
    """
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    defaults = {
        "DATABASE_URL": f"sqlite:///{(DEMO_DIR / 'agentix_demo.db').as_posix()}",
        "VECTOR_STORE_BACKEND": "faiss",
        "EMBEDDING_BASE_URL": args.embedding_base_url,
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_MODEL": args.embedding_model,
        "EMBEDDING_API_KEY": "lm-studio",
        "LLM_BASE_URL": args.llm_base_url,
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": args.chat_model,
        "LLM_API_KEY": "lm-studio",
        "OPENAI_API_KEY": "lm-studio",
        # 演示链路不碰外部检索/编排设施：Celery 留空 ⇒ 索引构建走同进程直调
        "CELERY_BROKER_URL": "",
        "CELERY_RESULT_BACKEND": "",
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "WARNING"),
        # 宿主 .env 里 DEBUG 常为真 ⇒ SQLAlchemy 会把每条 SQL 打到 stdout，
        # 演示输出会被淹掉。这里只在本进程静音，不改 .env。
        "DB_ECHO": "false",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)


def parse_corpus(path: Path, limit: Optional[int] = None) -> List[Tuple[str, str]]:
    """语料 → ``[(title, content), ...]``，一条数据条目一项。

    标题行的判据是**行首恰好 ``# ``**（一个 # + 一个空格）。这个细节不是洁癖：
    语料里混着 3 行光秃秃的 ``#`` 分隔符，早期用 ``^#\\s*(.+)$`` 一类宽正则会把
    它们当成条目，于是条目数从 4,865 变成 4,868——多出的 3 条"文档"没有标题，
    内容还和邻条粘连，检索指标会跟着一起失真。逐行状态机 + 严格前缀，才能
    复现 evals/README 登记的 4,865 条语料口径。
    """
    entries: List[Tuple[str, str]] = []
    title: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        if title is None:
            return
        content = "\n".join(buffer).strip()
        if content:
            entries.append((title, content))

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("# "):
            flush()
            title = raw[2:].strip()
            buffer = []
            continue
        if raw.strip() == "#":
            continue  # 语料里的裸分隔行：不属于任何条目
        if title is not None:
            buffer.append(raw)
        if limit is not None and len(entries) >= limit:
            flush()
            break
    flush()
    if limit is not None:
        entries = entries[:limit]
    return entries


@dataclass
class SeedResult:
    kb_id: str
    documents: int
    chunks: int
    index_path: str
    artifact_id: str
    corpus_sha256: str
    embedding_seconds: float


def cmd_seed(args: argparse.Namespace) -> int:
    """建 SQLite 元数据 + FAISS 索引 + 激活索引产物（全程真实生产组件）。"""
    _bootstrap_env(args)

    from sqlmodel import Session, SQLModel, select

    import agentix.retrieval.domain.models  # noqa: F401 - 注册表到 metadata
    from agentix.infrastructure.core.database import engine
    from agentix.retrieval.domain.models import (
        Chunk,
        Document,
        IndexArtifact,
        IndexArtifactManifest,
    )
    from agentix.retrieval.infra.tasks import index_worker

    SQLModel.metadata.create_all(engine)

    entries = parse_corpus(Path(args.corpus), args.limit)
    if not entries:
        print(f"[seed] 语料为空：{args.corpus}", file=sys.stderr)
        return 2
    corpus_sha = sha256(Path(args.corpus).read_bytes()).hexdigest()
    print(f"[seed] 语料条目={len(entries)} corpus_sha256={corpus_sha[:16]}…")
    if args.limit is None and len(entries) != EXPECTED_CORPUS_ENTRIES:
        print(
            f"[seed][警告] 解析出 {len(entries)} 条，与 §15.1 记录的 "
            f"{EXPECTED_CORPUS_ENTRIES} 条不一致——语料或解析口径漂了，"
            "跑出来的指标不能与 §15.1 并排读。",
            file=sys.stderr,
        )

    # 幂等重跑：同 kb_id 的旧行先清掉，否则 unique(kb_id,title,folder_path) 会撞
    with Session(engine) as session:
        stale_docs = session.exec(
            select(Document).where(Document.kb_id == args.kb_id)
        ).all()
        for doc in stale_docs:
            for chunk in session.exec(
                select(Chunk).where(Chunk.document_id == doc.id)
            ).all():
                session.delete(chunk)
            session.delete(doc)
        for artifact in session.exec(
            select(IndexArtifact).where(IndexArtifact.kb_id == args.kb_id)
        ).all():
            for manifest in session.exec(
                select(IndexArtifactManifest).where(
                    IndexArtifactManifest.artifact_id == artifact.id
                )
            ).all():
                session.delete(manifest)
            session.delete(artifact)
        session.commit()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session(engine) as session:
        for title, content in entries:
            # 切片文本**保留标题行**（``# 《民法典》第148条`` 与正文同条）。
            # 不是排版偏好，是口径保真：BM25 的语料只取 chunk.content
            # （``sparse._load_index`` 把 metadata 压成 id/page），标题里的法规名
            # 一旦剥掉，"民法典"这个高频查询词在词法通道里彻底消失。
            chunk_text = f"# {title}\n{content}"
            doc = Document(
                kb_id=args.kb_id,
                title=title,
                content="",
                status="draft",
                created_at=now,
            )
            session.add(doc)
            session.flush()
            session.add(
                Chunk(
                    document_id=doc.id,
                    content=chunk_text,
                    page_no=1,
                    start_offset=0,
                    end_offset=len(chunk_text),
                )
            )
        session.commit()
        chunk_count = len(session.exec(select(Chunk)).all())
    print(f"[seed] 元数据落库 documents={len(entries)} chunks={chunk_count}")

    index_path = str(DEMO_DIR / "indexes" / args.kb_id)
    t0 = time.perf_counter()
    # 复用生产索引构建函数：chunk → langchain Document → 向量库 add_documents → save
    #
    # 必须先 _prepare_index_dir 再 build：FAISS 后端在 __init__ 里发现 index.faiss
    # 存在就会**加载旧索引再追加**，重复 seed 于是把同一份语料灌两遍（实测
    # doc_count 40+4868+4865=9773，检索结果里《民法典》第148条连排两个名次，
    # query hit rate / MRR 被重复槽位稀释）。生产链路由 index_worker 的重建流程负责清
    # 目录，这里直接调同一个函数，而不是自己在脚本里另写一套删除逻辑。
    index_worker._prepare_index_dir(index_path)
    index_worker._build_vector_index(args.kb_id, index_path)
    embed_seconds = time.perf_counter() - t0
    print(f"[seed] 索引构建完成 path={index_path} 用时={embed_seconds:.1f}s")

    # 守卫：索引里的向量数必须等于库内切片数，否则说明有残留或漏灌——
    # 检索指标的分母会错，宁可停下来也不能带着脏索引去跑分。
    meta_file = Path(index_path) / "index_metadata.json"
    if meta_file.exists():
        doc_count = json.loads(meta_file.read_text(encoding="utf-8")).get("doc_count")
        if doc_count != chunk_count:
            print(
                f"[seed][失败] 索引向量数 {doc_count} ≠ 切片数 {chunk_count}，"
                "索引不干净，已中止（请检查 _prepare_index_dir 是否生效）",
                file=sys.stderr,
            )
            return 3
        print(f"[seed] 守卫通过 index.doc_count={doc_count} == chunks")

    artifact_id = f"{args.kb_id}-v1"
    with Session(engine) as session:
        session.add(
            IndexArtifact(
                id=artifact_id,
                kb_id=args.kb_id,
                version=1,
                index_path=index_path,
                index_job_id="local-demo-seed",
                is_active=True,
            )
        )
        docs = session.exec(
            select(Document).where(Document.kb_id == args.kb_id)
        ).all()
        for doc in docs:
            chunks = session.exec(select(Chunk).where(Chunk.document_id == doc.id)).all()
            digest = sha256(
                "\n".join((c.content or "") for c in chunks).encode("utf-8")
            ).hexdigest()
            session.add(
                IndexArtifactManifest(
                    artifact_id=artifact_id,
                    document_id=doc.id,
                    content_hash=digest,
                    chunk_count=len(chunks),
                )
            )
            # sparse 通道只吃「ready + 属于当前激活产物」的切片，必须一并置好
            doc.status = "ready"
            doc.indexed_artifact_id = artifact_id
            doc.indexed_content_hash = digest
            doc.indexed_at = now
        session.commit()

    result = SeedResult(
        kb_id=args.kb_id,
        documents=len(entries),
        chunks=chunk_count,
        index_path=index_path,
        artifact_id=artifact_id,
        corpus_sha256=corpus_sha,
        embedding_seconds=round(embed_seconds, 1),
    )
    manifest = DEMO_DIR / "seed_manifest.json"
    manifest.write_text(
        json.dumps(result.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[seed] 完成 artifact={artifact_id} 清单={manifest}")
    return 0


@dataclass
class AskResult:
    question: str
    route: str
    route_logic: str
    answer: str
    sources: List[str]
    chunks: int
    retrieval_ms: float
    generation_ms: float
    total_ms: float
    prompt_tokens: int
    completion_tokens: int
    status: str


def _prewarm_chat_model() -> float:
    """冷加载预热：先打一个 1-token 请求，把本地模型装进运行时。

    为什么需要：LM Studio 的空闲卸载 + 冷启动加载（8B 约 5GB）实测超过角色
    profile 的超时（router=10s、general=45s）——客户端先到点断开，服务端就会
    报 ``Engine protocol startup was aborted``，表现成"模型不可用"而不是"加载
    太慢"。预热把加载成本和业务请求的超时预算分开，演示与采数才谈得上口径。
    """
    import openai

    started = time.perf_counter()
    client = openai.OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", LMSTUDIO_BASE_URL),
        api_key=os.environ.get("LLM_API_KEY", "lm-studio"),
        timeout=300.0,
    )
    client.chat.completions.create(
        model=os.environ.get("LLM_MODEL", DEFAULT_CHAT_MODEL),
        messages=[{"role": "user", "content": "ping"}],
        max_tokens=1,
        temperature=0.0,
    )
    return (time.perf_counter() - started) * 1000.0


def _sdk_supports_raw_parse() -> bool:
    """本机 openai SDK 的 ``chat.completions.with_raw_response`` 有没有 ``parse``。

    这是 ``method="json_schema"`` 在本地能不能用的真正分水岭，而且**与版本号无关**：
    langchain-openai 1.1.6 的 json_schema 分支走
    ``self.root_client.chat.completions.with_raw_response.parse(...)``，而 openai
    1.47.0 的那个包装类没有 ``parse`` ⇒ 调用时 ``AttributeError``，被 ``router.py``
    的 except 吞成兜底路由（服务端本身支持 strict json_schema，卡的是客户端）；
    openai 1.109.1 有 ``parse``，langchain-openai 0.2.14 的 json_schema 分支更是
    只做 ``bind(response_format=...)``，两条路径都通。

    只是**构造**客户端（假 key + 指向不可能有服务的 base_url），不发包。
    """
    import openai

    for cls_name in ("SyncOpenAI", "OpenAI"):
        client_cls = getattr(openai, cls_name, None)
        if client_cls is None:
            continue
        try:
            client = client_cls(api_key="probe", base_url="http://127.0.0.1:1/v1")
        except Exception:  # noqa: BLE001 - 探测建不出客户端 ⇒ 按"不支持"处理，换下一组合
            return False
        return hasattr(client.chat.completions.with_raw_response, "parse")
    return False


def _structured_output_builds(chat_openai: Any, method: str, extra: Dict[str, Any]) -> bool:
    """同步探测：这套已装库能不能用 ``method`` + ``extra`` **构造出**结构化输出链。

    只构造不 invoke（不发请求、不等模型加载）。能挡住版本间真实的签名差：
    langchain-openai 0.2.14 的 ``with_structured_output`` 开头就是
    ``if kwargs: raise ValueError(f"Received unsupported arguments {kwargs}")``，
    所以 ``tool_choice="auto"`` 在这个版本连构造都过不去；1.1.6 则把 ``**kwargs``
    透传进 ``bind_tools``，可以覆盖它自己算出来的那个 ``tool_choice``。
    """
    from pydantic import BaseModel

    class _ProbeSchema(BaseModel):
        route_type: str = "kb-query"

    try:
        llm = chat_openai(
            model="local-probe",
            api_key="probe",
            base_url="http://127.0.0.1:1/v1",
            timeout=5,
        )
        llm.with_structured_output(_ProbeSchema, method=method, **extra)
        return True
    except Exception:  # noqa: BLE001 - 探测：这个组合不通，试下一个
        return False


def _pick_local_structured_output() -> Tuple[str, Dict[str, Any]]:
    """按**当前解释器里已装库的实际能力**选出可用的结构化输出组合。

    优先级：``json_schema``（LM Studio 原生支持 strict schema，语义最稳）⇒
    ``function_calling + tool_choice="auto"``（LM Studio 只收字符串 tool_choice，
    而 function_calling 分支默认绑成 ``{"type":"function","function":{...}}`` 的对象
    形式，实测 400 ``Invalid tool_choice type: 'object'. Supported string values:
    none, auto, required``，所以必须显式覆盖）⇒ 什么都不钉（保持出厂调用形态）。
    """
    from langchain_openai import ChatOpenAI

    candidates: List[Tuple[str, Dict[str, Any]]] = []
    if _sdk_supports_raw_parse():
        candidates.append(("json_schema", {}))
    candidates.append(("function_calling", {"tool_choice": "auto"}))
    for method, extra in candidates:
        if _structured_output_builds(ChatOpenAI, method, extra):
            return method, extra
    return "", {}


def _package_versions() -> str:
    """本地环境指纹（打印给读者，也是 docs/已知限制.md 登记读数的来源）。"""
    from importlib.metadata import PackageNotFoundError, version

    parts = [f"python {sys.version.split()[0]}"]
    for pkg in ("openai", "langchain-openai", "langchain-core"):
        try:
            parts.append(f"{pkg} {version(pkg)}")
        except PackageNotFoundError:  # noqa: BLE001 - 缺包不是错误，只是不写进指纹
            parts.append(f"{pkg} ?")
    return " / ".join(parts)


def _enable_local_llm_compatibility() -> List[str]:
    """本地模型的两处演示层适配，返回"改了什么"的清单（打印给用户，不留暗改）。

    ① **结构化输出方法**：``router.py`` 走 ``with_structured_output(cls)``，不传
       ``method``，于是"本地跑不跑得通"完全由已装的 langchain-openai / openai 版本
       组合决定——下面两套在本机都真实出现过，所以这里按**能力探测**选
       （``_pick_local_structured_output``），不写死版本号：

       * openai 1.109.1 + langchain-openai 0.2.14（仓库 ``.venv``，py3.12）：
         出厂默认 ``function_calling`` 把 ``tool_choice`` 绑成指定工具的对象形式
         ⇒ LM Studio 400；而 ``tool_choice`` 这个 kwarg 又过不了构造
         （``Received unsupported arguments``）⇒ 可用的是 **``json_schema``**。
       * openai 1.47.0 + langchain-openai 1.1.6（Anaconda py3.13）：出厂默认
         ``json_schema`` 分支要调 ``with_raw_response.parse()``，SDK 里没这个方法
         ⇒ ``AttributeError``；``json_mode`` 被服务端拒（``'response_format.type'
         must be 'json_schema' or 'text'``）⇒ 可用的是
         **``function_calling + tool_choice="auto"``**。

       两种都只补 kwarg，**不改生产代码、也不改生产默认**（生产走 DeepSeek /
       OpenAI 兼容 tool-calling，出厂形态本就通）。选中的组合与环境指纹一并打印。
    ② **路由超时余量**：router profile 出厂 ``timeout=10s``，本地 8B 单条分类实测
       Anaconda 栈 3.4–6.8s、``.venv`` + ``json_schema`` 10.6–19.2s（端到端减检索减生成的
       残差口径，本机实测），余量太薄会随机退化成兜底路由。演示进程抬到 60s。
    """
    from dataclasses import replace

    from langchain_openai import ChatOpenAI

    notes: List[str] = [f"环境指纹: {_package_versions()}"]

    if not getattr(ChatOpenAI, "_local_demo_patched_structured", False):
        method, extra = _pick_local_structured_output()
        if method:
            original = ChatOpenAI.with_structured_output

            def patched(self, schema, **kwargs):  # type: ignore[no-untyped-def]
                kwargs.setdefault("method", method)
                for key, value in extra.items():
                    kwargs.setdefault(key, value)
                return original(self, schema, **kwargs)

            ChatOpenAI.with_structured_output = patched  # type: ignore[method-assign]
            ChatOpenAI._local_demo_patched_structured = True  # type: ignore[attr-defined]
            tail = "".join(f" + {key}={value!r}" for key, value in extra.items())
            notes.append(f"with_structured_output ⇒ method={method}{tail}")
        else:
            notes.append("未找到可用结构化输出组合，保持出厂调用形态")

    from agentix.application.agents import llm_factory

    router_profile = llm_factory.DEFAULT_PROFILES["router"]
    if router_profile.timeout < 60.0:
        llm_factory.DEFAULT_PROFILES["router"] = replace(router_profile, timeout=60.0)
        notes.append(f"router 角色超时 {router_profile.timeout:.0f}s ⇒ 60s")

    return notes


_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _run_async(coro: Any) -> Any:
    """整个演示进程复用一个事件循环。

    ``asyncio.run`` 每次都新建再关闭循环，而 langchain/openai 的异步 httpx
    客户端在循环关闭之后才被 GC ⇒ Windows 上抛出 ``RuntimeError: Event loop is
    closed`` 的收尾噪声（结果是对的，输出却脏）。单循环 + 退出前显式排水，
    是消掉这层假故障的最小改动。
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _close_async() -> None:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        return
    import gc

    gc.collect()  # 先回收待销毁的异步传输，再关循环
    try:
        _LOOP.run_until_complete(_LOOP.shutdown_asyncgens())
    except Exception:  # noqa: BLE001 - 收尾失败不影响已产出的结果，直接关循环
        pass
    finally:
        _LOOP.close()
        _LOOP = None


def _route(question: str) -> Dict[str, Any]:
    """L1 意图路由：调用生产 ``analyze_and_route_query``，原样带回判定依据。

    演示时最值钱的就是 ``logic``/``decision``/``reasoning`` 这几个字段——它们
    证明路由是模型判的，不是脚本写死的。路由本身有生产兜底（LLM 失败/非法类型
    ⇒ 退化到启发式或 ``kb-query``），兜底路径同样会体现在 ``logic`` 里。
    """
    from langchain_core.messages import HumanMessage

    from agentix.application.agents.router import analyze_and_route_query
    from agentix.orchestration.langgraph.lg_states import AgentState

    # 路由节点按 ``state.messages[-1].content`` 取问题文本，必须是
    # langchain Message 对象而非 dict（dict 没有 .content 属性）
    state = AgentState(messages=[HumanMessage(content=question)])
    out = _run_async(analyze_and_route_query(state, config={"configurable": {}}))
    router = out.get("router")
    return {
        "type": getattr(router, "type", "kb-query"),
        "logic": getattr(router, "logic", "") or "",
        "decision": getattr(router, "decision", "") or "",
        "confidence": getattr(router, "confidence", None),
        "reasoning": getattr(router, "reasoning", "") or "",
    }


def _retrieve_docs(
    question: str, kb_id: str, tenant_id: str, top_k: int
) -> Tuple[List[Any], str, List[str]]:
    """L2 kb 检索：走检索能力域对编排层的唯一出口 ``RetrievalPort``。

    返回 ``(docs, status, sources)``，其中 docs 是 langchain Document——
    评测匹配器要吃 ``page_content``/``metadata``，这里刻意不提前压扁成字典。
    """
    from agentix.retrieval.usecases.retrieval_port_adapter import create_retrieval_port

    port = create_retrieval_port()
    result = _run_async(
        port.search(query=question, kb_id=kb_id, tenant_id=tenant_id, top_k=top_k)
    )
    docs = [_DocView(c) for c in result.get("chunks", [])]
    sources = [
        (c.get("citation") or {}).get("doc_title") or c.get("chunk_id", "")
        for c in result.get("chunks", [])
    ]
    return docs, result.get("status", "degraded"), sources


class _DocView:
    """把端口的 chunk 还原成匹配器认识的 ``page_content``/``metadata`` 形状。

    存在的理由只有一个：共享 citation matcher 的入参是检索
    文档对象，而端口对外暴露的是 TypedDict。形状对齐 ≠ 新造口径——法规名/
    条号解析、三形 ``第X条`` 归一全部由原函数完成。
    """

    __slots__ = ("page_content", "metadata")

    def __init__(self, chunk: Dict[str, Any]) -> None:
        citation = chunk.get("citation") or {}
        self.page_content = chunk.get("content", "")
        self.metadata = {
            "chunk_id": chunk.get("chunk_id", ""),
            "source": citation.get("doc_title", ""),
            "document_id": citation.get("doc_id", ""),
            "page": citation.get("page"),
            "_retrieval_score": chunk.get("score", 0.0),
        }


def _generate(question: str, docs: Sequence[Any], *, no_think: bool) -> Tuple[str, int, int, float]:
    """带引用生成：复用 L2 知识节点的系统提示词 + 生产 LLM 工厂。

    ``no_think`` 是给 Qwen3 这类"先推理后作答"模型用的：LM Studio 的
    ``chat_template_kwargs`` 在本实例上不生效（实测 reasoning_tokens 照算），
    而把 ``/no_think`` 追加进提问可以真正关掉思考链——实测 200 token 生成
    reasoning_tokens 从 194 降到 0。演示与采数都关掉，口径里必须写明。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from agentix.application.agents.kb_tools.prompts import build_knowledge_system_prompt
    from agentix.application.agents.llm_factory import get_llm_client

    context = "\n\n".join(
        f"文档 {i + 1}（{d.metadata.get('source') or '未知出处'}）:\n{d.page_content}"
        for i, d in enumerate(docs[:5])
    )
    llm = get_llm_client(
        temperature=0.0,
        tags=["local-demo"],
        role="general",
        # 本地 8B 生成 400+ token 会超 profile 默认的 45s；显式放宽到 180s，
        # 不改代码默认值（生产链路仍按角色分级超时）
        timeout=180.0,
        max_retries=1,
    )
    payload_question = question + ("/no_think" if no_think else "")
    messages = [
        SystemMessage(content=build_knowledge_system_prompt(context)),
        HumanMessage(content=payload_question),
    ]
    started = time.perf_counter()
    message = llm.invoke(messages)
    generation_ms = (time.perf_counter() - started) * 1000.0
    usage = getattr(message, "usage_metadata", None) or {}
    answer = message.content if isinstance(message.content, str) else str(message.content)
    return answer, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), generation_ms  # type: ignore[return-value]


def cmd_ask(args: argparse.Namespace) -> int:
    _bootstrap_env(args)
    question = args.question.strip()
    if not question:
        print("[ask] 问题为空", file=sys.stderr)
        return 2

    notes = _enable_local_llm_compatibility()
    for note in notes:
        print(f"本地适配   : {note}")
    try:
        return _ask_body(args, question)
    finally:
        _close_async()


def _ask_body(args: argparse.Namespace, question: str) -> int:
    t_all = time.perf_counter()
    if not args.no_prewarm:
        warm_ms = _prewarm_chat_model()
        print(f"预热     : 模型驻留 {warm_ms:.0f}ms（不计入端到端延迟）")
    route = _route(question)
    confidence = route["confidence"]
    suffix = f"  confidence={confidence}" if confidence is not None else ""
    print(f"路由     : {route['type']}{suffix}")
    basis = route["decision"] or route["reasoning"] or route["logic"]
    if basis:
        print(f"判定依据 : {basis[:120]}")
    if route["type"] != "kb-query":
        print(
            "\n本演示切片只执行 kb-query 子链路（检索→带引用回答）。上面展示的是 L1 的"
            "真实判定与依据；其余分支（general/image/file/text2sql/graphrag）见 README §10 "
            "全栈路径，不在三条命令范围内。"
        )
        return 3
    t_retrieve = time.perf_counter()
    docs, status, sources = _retrieve_docs(
        question, args.kb_id, args.tenant, args.top_k
    )
    retrieval_ms = (time.perf_counter() - t_retrieve) * 1000.0

    print(f"检索状态 : {status}  命中={len(docs)}  search_k={args.top_k}  {retrieval_ms:.0f}ms")
    for idx, src in enumerate(sources, start=1):
        print(f"  [{idx}] {src}")

    if not docs:
        print("\n回答     : （检索为空，未调用生成——不编造答案）")
        return 1

    answer, pin, pout, generation_ms = _generate(  # type: ignore[misc]
        question, docs, no_think=not args.keep_thinking
    )
    total_ms = (time.perf_counter() - t_all) * 1000.0
    print(f"\n回答     :\n{answer.strip()}")
    print(
        f"\n生成={generation_ms:.0f}ms 端到端={total_ms:.0f}ms "
        f"token prompt={pin}/completion={pout}"
    )
    return 0


def _load_golden(
    route: str,
    limit: Optional[int],
    paths: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """按顺序拼接一个或多个 golden 文件（默认仍是唯一的 23 题基线）。

    多文件是「只增不改」：基线题集一个字节都不动，
    扩样集另立，由 ``--golden`` 显式拼起来跑。
    返回 ``(截断后的题, 截断前的合格题数)``——第二个值只用来提示 ``--limit`` 是否生效，
    避免"刚好等于 limit"时误报截断。
    """
    files = [Path(p) for p in paths] if paths else [GOLDEN_PATH]
    rows: List[Dict[str, Any]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if route and row.get("route_type") != route:
                continue
            if not row.get("expected_sources"):
                continue  # 空金标不进 demo 诊断的分母
            rows.append(row)
    total = len(rows)
    if limit is not None:
        rows = rows[:limit]
    return rows, total


def cmd_eval(args: argparse.Namespace) -> int:
    """小样本 smoke diagnostic: query hit rate / MRR + latency and token means."""
    # T3：不给 --golden 且默认题集不存在 ⇒ 响亮退出（列可用集 + 改道示例），
    # 不在"题集读成 0 条"的前提下继续装配环境、更不把空样本报成 0 分。
    # 显式传 --golden 时走原逻辑，行为逐位不变。
    golden, rc = ensure_default_golden(
        args.golden,
        entry="scripts/demo/local_kb_demo.py eval",
        default=GOLDEN_PATH,
        example_flags="--limit 23 --retrieve-only",
    )
    if rc:
        return rc
    args.golden = [str(p) for p in golden] if isinstance(golden, list) else [str(golden)]
    _bootstrap_env(args)
    notes = _enable_local_llm_compatibility()
    for note in notes:
        print(f"[eval] 本地适配: {note}（生产口径不变，见 _enable_local_llm_compatibility）")
    try:
        return _eval_body(args)
    finally:
        _close_async()


def _pct(values: Sequence[float], q: float) -> float:
    """最近邻秩分位数——**委托**全仓唯一实现 ``evals/perf_stats.nearest_rank_quantile``。

    空样本沿用本脚本既有立场返回 0.0（调用方都在"有样本"的分支上，历史读数不变）；
    口径 2026-10-27 起全仓一把尺子，此前与线性插值并存的读数不可直接比。
    """
    value = nearest_rank_quantile(values, q)
    return 0.0 if value is None else value


def _run_pass(
    rows: List[Dict[str, Any]],
    args: argparse.Namespace,
    generate: bool,
    first_rank: Any,
    rep: int,
    repeat: int,
) -> Dict[str, Any]:
    """跑一轮：逐题 路由 → 检索 →（可选）生成，返回本轮原始观测。"""
    per_query: List[Dict[str, Any]] = []
    ranks: List[Optional[int]] = []
    lat_full: List[float] = []
    lat_retr: List[float] = []
    lat_gen: List[float] = []
    pin_total = pout_total = 0

    for idx, row in enumerate(rows, start=1):
        question = row["question"]
        golds = row.get("expected_sources") or []
        t_all = time.perf_counter()
        if args.skip_route:
            route = "kb-query"
        else:
            try:
                route = _route(question)["type"]
            except Exception as exc:  # noqa: BLE001 - 路由失败降级为固定路由，不影响检索采数
                route = f"route_error:{type(exc).__name__}"
        t_retrieve = time.perf_counter()
        docs, status, sources = _retrieve_docs(question, args.kb_id, args.tenant, args.top_k)
        lat_retr.append((time.perf_counter() - t_retrieve) * 1000.0)

        rank = first_rank(docs, golds)
        ranks.append(rank)
        hit = rank is not None and rank <= args.hit_k

        answer = ""
        pin = pout = 0
        gen_ms = 0.0
        if generate and docs:
            answer, pin, pout, gen_ms = _generate(  # type: ignore[misc]
                question, docs, no_think=not args.keep_thinking
            )
        lat_gen.append(gen_ms)
        total_ms = (time.perf_counter() - t_all) * 1000.0
        lat_full.append(total_ms)
        pin_total += pin
        pout_total += pout
        per_query.append(
            {
                "id": row.get("id"),
                "question": question,
                "route": route,
                "status": status,
                "rank": rank,
                "query_hit_rate_at_k": hit,
                "expected_sources": golds,
                "retrieved_sources": sources,
                "answer_chars": len(answer),
                "retrieval_ms": round(lat_retr[-1], 1),
                "generation_ms": round(gen_ms, 1),
                "total_ms": round(total_ms, 1),
                "prompt_tokens": pin,
                "completion_tokens": pout,
            }
        )
        # repeat == 1 逐题打印（与历史输出一致）；重复跑时逐题噪音太大，
        # 只由 _eval_body 每轮打一行汇总。
        if repeat == 1:
            print(
                f"  [{idx:>3}/{len(rows)}] {row.get('id')} rank={rank if rank else '-'} "
                f"route={route} total={total_ms:.0f}ms"
            )

    return {
        "per_query": per_query,
        "ranks": ranks,
        "lat_retr": lat_retr,
        "lat_gen": lat_gen,
        "lat_full": lat_full,
        "pin_total": pin_total,
        "pout_total": pout_total,
        "n": len(rows),
    }


def _pass_summary(p: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """把一轮原始观测压成与历史口径一致的 summary（键名不变）。"""
    ranks = p["ranks"]
    n = p["n"]
    lat_retr, lat_gen, lat_full = p["lat_retr"], p["lat_gen"], p["lat_full"]
    query_hit_rate = (
        sum(1 for r in ranks if r is not None and r <= args.hit_k) / n if n else 0.0
    )
    mrr = statistics.mean(
        [(1.0 / r) if (r is not None and r <= args.hit_k) else 0.0 for r in ranks]
    ) if n else 0.0
    # MRR 首个命中位次取倒数，完整检索列表不截断到 k
    mrr_full = statistics.mean(
        [(1.0 / r) if r is not None else 0.0 for r in ranks]
    ) if n else 0.0
    return {
        "sample_size": n,
        "route_filter": args.route,
        "search_k": args.top_k,
        "hit_k": args.hit_k,
        "query_hit_rate_at_k": round(query_hit_rate, 4),
        "mrr_within_hit_k": round(mrr, 4),
        "mrr_full": round(mrr_full, 4),
        "latency_ms": {
            "retrieval_p50": round(_pct(lat_retr, 0.5), 1),
            "retrieval_p95": round(_pct(lat_retr, 0.95), 1),
            "generation_p50": round(_pct([v for v in lat_gen if v > 0], 0.5), 1),
            "generation_p95": round(_pct([v for v in lat_gen if v > 0], 0.95), 1),
            "end_to_end_p50": round(_pct(lat_full, 0.5), 1),
            "end_to_end_p95": round(_pct(lat_full, 0.95), 1),
        },
        "tokens": {
            "prompt_mean": round(p["pin_total"] / n, 1) if n else 0.0,
            "completion_mean": round(p["pout_total"] / n, 1) if n else 0.0,
            "total_mean": round((p["pin_total"] + p["pout_total"]) / n, 1) if n else 0.0,
        },
        "env": {
            "chat_model": os.environ.get("LLM_MODEL"),
            "embedding_model": os.environ.get("EMBEDDING_MODEL"),
            "vector_backend": os.environ.get("VECTOR_STORE_BACKEND"),
            "database": os.environ.get("DATABASE_URL"),
            "retrieval_strategy": "Dense + BM25/z-score",
            "thinking_disabled": not args.keep_thinking,
            "kb_id": args.kb_id,
        },
    }


def _eval_body(args: argparse.Namespace) -> int:
    from evals.retrieval_matching import first_rank

    golden_paths = getattr(args, "golden", None)
    rows, candidate_total = _load_golden(args.route, args.limit, golden_paths)
    if not rows:
        print(f"[eval] golden 里 route={args.route} 且金标非空的题为 0 条", file=sys.stderr)
        return 2
    if len(rows) < candidate_total:
        # --limit 默认 23 是给基线集的；扩样集忘了调大就会被静默截断，这里明确提示。
        print(
            f"[eval] 提示: --limit={args.limit} 截断了 golden（合格题 {candidate_total}，"
            f"本轮只跑 {len(rows)}）——如需全量请显式给 --limit {candidate_total}",
            file=sys.stderr,
        )
    repeat = max(1, int(getattr(args, "repeat", 1)))
    print(
        f"[eval] 样本={len(rows)} route={args.route} search_k={args.top_k} "
        f"repeat={repeat} 模型={os.environ.get('LLM_MODEL')} 嵌入={os.environ.get('EMBEDDING_MODEL')}"
    )
    if golden_paths:
        print(f"[eval] golden={', '.join(golden_paths)}")

    if not args.no_prewarm:
        warm_ms = _prewarm_chat_model()
        print(f"[eval] 模型预热 {warm_ms:.0f}ms（不计入端到端延迟；重复跑只在第 1 轮前预热一次）")

    generate = not args.retrieve_only
    passes: List[Dict[str, Any]] = []
    for rep in range(1, repeat + 1):
        p = _run_pass(rows, args, generate, first_rank, rep, repeat)
        passes.append(p)
        if repeat > 1:
            s = _pass_summary(p, args)
            lm = s["latency_ms"]
            print(
                f"[eval] 第 {rep}/{repeat} 轮 done：query-hit-rate@k={s['query_hit_rate_at_k']} "
                f"mrr@k={s['mrr_within_hit_k']} "
                f"retrieval p50/p95={lm['retrieval_p50']}/{lm['retrieval_p95']}ms "
                f"e2e p50/p95={lm['end_to_end_p50']}/{lm['end_to_end_p95']}ms"
            )

    per_rep = [_pass_summary(p, args) for p in passes]
    summary = per_rep[0]

    if golden_paths:
        # 只在显式给 --golden 时写来源：默认 23 题基线的 summary 键集保持逐键不变。
        summary["golden"] = list(golden_paths)

    if repeat > 1:
        # 只增不改：R=1 时输出与历史逐字节一致；R>1 时加三块观测。
        summary["repeat_count"] = repeat
        # ① 每轮的分位数（看同一份问题集在两轮之间差多少）
        summary["repeats"] = [
            {
                "query_hit_rate_at_k": s["query_hit_rate_at_k"],
                "mrr_within_hit_k": s["mrr_within_hit_k"],
                "latency_ms": s["latency_ms"],
            }
            for s in per_rep
        ]
        # ② 跨轮极差：小样本分位数到底稳不稳（min/max 是这轮实验要回答的问题）
        spread: Dict[str, Any] = {}
        for key in summary["latency_ms"]:
            vals = [s["latency_ms"][key] for s in per_rep]
            spread[key] = {"min": min(vals), "max": max(vals)}
        summary["repeat_spread_ms"] = spread
        # ③ 池化：R 轮 = R×n 个样本并成一池重算分位数（比单轮更稳，但仍是同一份问题）
        pooled = {
            "retrieval": [v for p in passes for v in p["lat_retr"]],
            "generation": [v for p in passes for v in p["lat_gen"] if v > 0],
            "end_to_end": [v for p in passes for v in p["lat_full"]],
        }
        summary["pooled_latency_ms"] = {
            f"{name}_p50": round(_pct(vals, 0.5), 1) for name, vals in pooled.items()
        }
        for name, vals in pooled.items():
            summary["pooled_latency_ms"][f"{name}_p95"] = round(_pct(vals, 0.95), 1)

    print("\n=== 小样本端到端指标 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.json_out:
        out = ROOT / args.json_out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"summary": summary, "per_query": passes[0]["per_query"]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[eval] 明细写入 {out}")
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kb-id", default=DEFAULT_KB_ID)
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--llm-base-url", default=os.getenv("LLM_DEMO_BASE_URL", LMSTUDIO_BASE_URL))
    parser.add_argument("--embedding-base-url", default=os.getenv("EMBEDDING_DEMO_BASE_URL", LMSTUDIO_BASE_URL))
    parser.add_argument("--chat-model", default=os.getenv("LLM_DEMO_MODEL", DEFAULT_CHAT_MODEL))
    parser.add_argument("--embedding-model", default=os.getenv("EMBEDDING_DEMO_MODEL", DEFAULT_EMBED_MODEL))
    parser.add_argument(
        "--keep-thinking",
        action="store_true",
        help="不追加 /no_think（默认追加，关掉 Qwen3 思考链以测生成延迟）",
    )
    parser.add_argument(
        "--no-prewarm",
        action="store_true",
        help="跳过本地模型冷加载预热（默认预热，避免角色超时打断模型加载）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SQLite + FAISS + LM Studio 的最小可演示 RAG 切片"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="解析语料 → SQLite 元数据 → FAISS 索引 → 激活产物")
    _add_common(p_seed)
    p_seed.add_argument("--corpus", default=str(CORPUS_PATH))
    p_seed.add_argument("--limit", type=int, default=None, help="只灌前 N 条（调试用）")
    p_seed.set_defaults(func=cmd_seed)

    p_ask = sub.add_parser("ask", help="一条命令跑完 路由 → 检索 → 带引用回答")
    _add_common(p_ask)
    p_ask.add_argument("question")
    p_ask.set_defaults(func=cmd_ask)

    p_eval = sub.add_parser("eval", help="golden 小集 smoke diagnostic（query hit rate/MRR/latency）")
    _add_common(p_eval)
    p_eval.add_argument("--route", default="kb-query")
    p_eval.add_argument("--limit", type=int, default=23)
    p_eval.add_argument(
        "--golden",
        nargs="+",
        default=None,
        metavar="PATH",
        help=(
            "golden 文件（可多个，按序拼接）；不给则用默认题集路径——"
            "该集已于 2026-10-26 随评测集下线删除，缺省文件不存在时本命令直接非零退出"
            "并列出当前可用题集（不新建题集、不改指默认）。"
            "多集拼接示例：--golden <基线集> <扩样集> --limit 50"
        ),
    )
    p_eval.add_argument("--hit-k", "--recall-k", dest="hit_k", type=int, default=5)
    p_eval.add_argument("--skip-route", action="store_true", help="只测检索+生成，跳过 LLM 路由分类")
    p_eval.add_argument("--retrieve-only", action="store_true", help="只测检索层，不调生成")
    p_eval.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="同一问题集重复 R 轮（默认 1）；R>1 时额外输出每轮分位数、跨轮极差与池化分位数",
    )
    p_eval.add_argument("--json-out", default=None)
    p_eval.set_defaults(func=cmd_eval)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Windows 控制台默认 GBK，中文输出会炸码——演示脚本统一走 UTF-8
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 老/非常规终端不支持，保持默认编码即可
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
