"""被收纳的测量对象：全部**走原接口**，本文件零测量逻辑复制。

"收纳"的含义钉死在这里（T1 卡边界③）：bench 只做 import + 调用 + 结果整形，
计时、压测、分位数语义全在被测对象自己家里；本文件不许出现
``perf_counter`` 环绕被测调用的替代实现（两处例外，理由随代码写死：
``selfcheck`` 不测任何生产组件；``retrieval_*``（T12）的生产缝上**没有**计时
API，只能做"一次原接口调用首尾"的薄边界计时，分位数仍由
``bench.aggregate`` 跨轮池化，不在本文件计算）。

三种接口形态对应三种诚实的池化口径（``pooled_status``）：

- ``raw_samples``：原接口交回原始样本 → bench 跨轮池化（``pooled_from_raw_samples``）；
- ``internal_pooled``：原接口内部已对 ≥5 轮样本做了池化分位（runner 的
  ``summarize``）→ 直接采用其池化读数（``internal_pooled_by_target``）；
- ``summary_only``：原接口只交回聚合摘要（load_test 的 ``run_load``）→
  跨轮合并的分位数置 ``None`` 并写明原因——**各轮分位数再平均是禁止动作**，
  宁缺毋造。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]

SELF_CHECK_DATA_CLASS = "skeleton_selfcheck_not_a_measurement"


class SkipNoEnvironment(Exception):
    """外部依赖缺失的显式跳过信号——调用方落 ``skipped_no_environment`` 状态，
    这不是失败伪装成成功，而是"未实测"这一等公民状态。"""


def _import_by_path(rel_path: str, module_name: str):
    """按文件路径导入（``scripts/`` 无 ``__init__.py``，不是包）。"""
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法按路径导入 {rel_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# selfcheck：骨架离线自证路径（不测任何生产组件）
# ---------------------------------------------------------------------------


def _selfcheck_once() -> List[float]:
    """对一段纯内存操作计时 20 个样本——唯一目的是让入口能**离线**产出
    一份带原始样本的结果文件，验证"每份结果必含指纹块"这条闸。

    读数没有性能含义：``data_class`` 会逐字写进结果文件，防止被引用成
    任何组件的延迟声明。
    """
    samples: List[float] = []
    payload_rows = list(range(200))
    for _ in range(20):
        t0 = time.perf_counter()
        # 一段确定性的轻活：字典计数 + 排序，不触碰任何被测组件
        counts: Dict[int, int] = {}
        for x in payload_rows:
            counts[x % 37] = counts.get(x % 37, 0) + 1
        sorted(counts.items())
        samples.append((time.perf_counter() - t0) * 1000.0)
    return samples


def _selfcheck_measure(repeats: int, options: Dict[str, Any]) -> Dict[str, Any]:
    """跑 ``repeats`` 轮自证计时，原始样本全量交回 bench 池化。

    轮与轮之间不取每轮分位再合并——原接口形态是 ``raw_samples``，池化发生在
    ``bench.aggregate.pooled_summary`` 一处。
    """
    samples: List[float] = []
    for _ in range(repeats):
        samples.extend(_selfcheck_once())
    return {"form": "raw_samples", "samples_ms": samples, "per_repeat": None}


# ---------------------------------------------------------------------------
# perf_runner：收纳 evals/performance/runner.py（原接口 run_benchmark）
# ---------------------------------------------------------------------------


def _perf_runner_measure(repeats: int, options: Dict[str, Any]) -> Dict[str, Any]:
    """调 ``runner.run_benchmark(iterations=repeats)``——轮数与池化都发生
    在原接口内部：runner 对每个负载类重复 ``iterations`` 次，``summarize``
    即这些原始样本的池化最近邻秩（T2 口径）。bench 不再包一层计时，
    否则就是双重测量。
    """
    if REPO_ROOT.as_posix() not in sys.path:
        sys.path.insert(0, REPO_ROOT.as_posix())
    from evals.performance import runner as perf_runner

    report = perf_runner.run_benchmark(
        repeats,
        http=bool(options.get("families", {}).get("http", True)),
        stream=bool(options.get("families", {}).get("stream", True)),
        cpu=bool(options.get("families", {}).get("cpu", True)),
    )
    return {
        "form": "internal_pooled",
        "pooled_quantiles_ms": report["overall"],
        "per_repeat": [report],
        "per_repeat_note": (
            "单项元素是 runner 一次完整测量程序的报告（每类已含 iterations"
            f"={repeats} 的池化摘要）；本 target 的『重复』由原接口的 "
            "iterations 承担，bench 不叠第二层重复。"
        ),
    }


# ---------------------------------------------------------------------------
# load_test：收纳 scripts/load_test.py（原接口 run_load）
# ---------------------------------------------------------------------------


def _load_test_measure(repeats: int, options: Dict[str, Any]) -> Dict[str, Any]:
    """重复调用 ``load_test.run_load`` 并保留每轮报告；跨轮合并的分位数是
    ``None``——原接口不回吐逐请求原始样本，而"每轮 p95 再平均"是禁止动作。

    单轮报告内部的 p50/p95 已是该轮全部成功样本（默认 200 请求）的池化
    最近邻秩，读单个 ``reports[r]["latency_seconds"]`` 即可，无需跨轮再算。
    """
    url = options.get("url")
    if not url:
        raise SkipNoEnvironment(
            "load_test 需要一个可达的 HTTP 端点（--url），本机无可假定存在的"
            "服务 ⇒ 未实测（方法学第 5 条：无环境不得推断读数）"
        )
    lt = _import_by_path("scripts/load_test.py", "bench_wrap_load_test")
    requests_total = int(options.get("requests", 200))
    workers = int(options.get("workers", 10))

    reports: List[Dict[str, Any]] = []
    pooled_single_run_samples: List[float] = []
    for _ in range(repeats):
        report, exit_code = lt.run_load(
            url, requests_total=requests_total, workers=workers, timeout=10.0
        )
        entry = {"report": report, "exit_code": exit_code}
        reports.append(entry)
        if report["success"] == 0:
            raise SkipNoEnvironment(
                "load_test 零成功样本（服务不可达/未启动）——报告分位数为 null，"
                "不得当成『很快』继续池化"
            )
    # 原接口不回吐原始样本；pooled_single_run_samples 恒空是**事实**而非疏漏。
    if pooled_single_run_samples:  # pragma: no cover - 防御分支，当前接口形态下不可达
        return {"form": "raw_samples", "samples_ms": pooled_single_run_samples,
                "per_repeat": reports}
    return {
        "form": "summary_only",
        "pooled_quantiles_ms": None,
        "per_repeat": reports,
        "per_repeat_note": (
            "跨轮池化不可得：run_load 只回聚合摘要。单轮 latency_seconds 即该轮"
            "全部成功样本的池化最近邻秩（evals.perf_stats 同尺）。"
        ),
    }


# ---------------------------------------------------------------------------
# retrieval：每请求重建 vectorstore / embedding 客户端（T12）
# ---------------------------------------------------------------------------
#
# 为什么在这里计时：``agentix/retrieval`` 的构造缝上**没有**任何计时 API，
# 而 T12 要的是"每次请求构造一次"的可复现读数。计时范围刻意只有一次原接口
# 调用的首尾；压测语义、分位数、池化仍归 ``bench.run`` / ``bench.aggregate``。
# 本段不读生产默认值的分支，也不写任何默认值。

#: fixture 的维数与文档数：够 FAISS 落一份可反序列化的索引，又小到不让
#: 反序列化成本盖过"新建客户端"这块本卡要测的对象。
_RETRIEVAL_FIXTURE_DIM = 8
_RETRIEVAL_FIXTURE_DOCS = 20


class _FixtureEmbeddings:
    """**只用于铸造 fixture 索引**的确定性假 embedding（不在被测路径上）。

    为什么允许存在：``FAISS.from_documents`` 落索引需要一份 embedding，而本卡
    要的是构造期新建客户端的代价、不是嵌入质量。SHA-256 前 8 字节折成 8 维
    向量 ⇒ 同一文本恒得同一向量、零网络、零 numpy。被测调用
    （``build_retriever → create_embeddings``）走的仍是生产实现。
    """

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [byte / 255.0 for byte in digest[:_RETRIEVAL_FIXTURE_DIM]]


def _write_retrieval_fixture(index_dir: Path) -> None:
    """在 ``index_dir`` 落一份小型 FAISS 索引（离线、确定性、可重复生成）。"""
    from langchain_community.vectorstores import FAISS
    from langchain_core.documents import Document

    docs = [
        Document(page_content=f"bench fixture doc {i}", metadata={"id": str(i)})
        for i in range(_RETRIEVAL_FIXTURE_DOCS)
    ]
    FAISS.from_documents(docs, _FixtureEmbeddings()).save_local(str(index_dir))


@contextmanager
def _retrieval_index(index_path: Optional[str]) -> Iterator[Tuple[Path, str]]:
    """本次测量用的索引目录：``--index-path`` 优先，否则运行时铸 fixture。

    显式给了路径但目录里没有 ``index.faiss`` ⇒ ``FileNotFoundError``（是调用方
    写错了，响亮失败）；默认 fixture 铸不出来（缺 langchain/faiss）⇒
    ``SkipNoEnvironment``（环境缺失，如实跳过，不伪造读数）。
    """
    if index_path:
        resolved = Path(index_path).expanduser().resolve()
        if not (resolved / "index.faiss").is_file():
            raise FileNotFoundError(
                f"--index-path={resolved} 下没有 index.faiss——不是可加载的 FAISS 索引"
            )
        yield resolved, "external_index_path"
        return

    import tempfile

    with tempfile.TemporaryDirectory(prefix="bench-retrieval-") as tmp:
        try:
            _write_retrieval_fixture(Path(tmp))
        except ImportError as exc:
            raise SkipNoEnvironment(
                f"FAISS fixture 无法生成（{exc}）⇒ 本机缺 langchain/faiss，未实测"
            ) from exc
        yield Path(tmp), "generated_fixture"


def _retrieval_backend() -> str:
    from agentix.retrieval.settings import settings as retrieval_settings

    return str(getattr(retrieval_settings, "VECTOR_STORE_BACKEND", "faiss")).lower()


def _assert_default_retriever_cache_off() -> None:
    """本卡测**默认路径**（flag 关 ⇒ 每请求直构）；flag 开时拒绝取读数。

    刻意不改默认值（T12 边界）：环境把开关打开后，读数会被缓存命中污染，
    那不是"每请求重建"的代价。
    """
    from agentix.retrieval.settings import settings as retrieval_settings

    if bool(getattr(retrieval_settings, "RETRIEVAL_RETRIEVER_CACHE_ENABLED", False)):
        raise RuntimeError(
            "RETRIEVAL_RETRIEVER_CACHE_ENABLED=true：请求路径已在复用进程内实例，"
            "测不到『每请求重建』。本 bench 不改默认值，请在默认关的环境重跑"
        )


def _retrieval_samples_per_repeat(options: Dict[str, Any]) -> int:
    samples = int(options.get("samples") or 10)
    if samples < 1:
        raise ValueError(f"samples={samples} 非法：每轮至少 1 个样本")
    return samples


def _retrieval_vectorstore_build_measure(
    repeats: int, options: Dict[str, Any]
) -> Dict[str, Any]:
    """请求路径构造缝：``get_or_build_retriever(index_path)`` 每次完整重建。

    flag 关（默认）⇒ 内部直调 ``build_retriever → create_vector_store →
    FAISSVectorStore.load → create_embeddings``。计时只包这一次原接口调用；
    分位数由 ``bench.aggregate`` 跨轮池化，本函数不写任何秩算式。

    caveat（与结果文件 ``notes`` 同文）：
    - 只测 **faiss** 档：milvus/qdrant 构造期只连服务，``_get_embeddings`` 是
      惰性调用点，本机无服务 ⇒ 未实测，**不得**据此推断跨栈差异；
    - 默认 fixture 是小索引（20 文档 / 8 维）：读数主要反映客户端新建 +
      索引反序列化的固定开销，真实索引越大反序列化占比越高；
    - 不含检索/嵌入请求（零网络），不是端到端检索延迟，也不是线上结论。
    """
    _assert_default_retriever_cache_off()
    backend = _retrieval_backend()
    if backend != "faiss":
        raise SkipNoEnvironment(
            f"VECTOR_STORE_BACKEND={backend!r}：本卡只测 faiss 档；milvus/qdrant"
            "需要外部服务，本机未实测（跨栈差异只写 caveat，不作结论）"
        )
    samples_per_repeat = _retrieval_samples_per_repeat(options)
    from agentix.retrieval.infra.embedding import client as embedding_client
    from agentix.retrieval.infra.vectorstore import retriever_cache

    samples_ms: List[float] = []
    per_repeat: List[Dict[str, Any]] = []
    with _retrieval_index(options.get("index_path")) as (index_path, fixture_kind):
        index_ref = str(index_path)
        # 预热一次：首次 import / 首次反序列化挪出样本（生产里这段发生在启动期）。
        retriever_cache.get_or_build_retriever(index_ref)
        keepalive: List[Any] = []  # 防 id 复用：对象必须活着才能按 is 判重
        distinct = set()
        for repeat in range(repeats):
            repeat_samples: List[float] = []
            builds_per_sample: List[int] = []
            for _ in range(samples_per_repeat):
                counter = {"n": 0}
                real_create = embedding_client.create_embeddings

                # 默认参数把循环内对象绑进闭包（B023）：计数壳只加一次函数调用，
                # 不复制被测逻辑；它数的是"生产实现被调用了几次"。
                def _counting(
                    *args: Any,
                    _counter: Dict[str, int] = counter,
                    _real: Callable[..., Any] = real_create,
                    **kwargs: Any,
                ) -> Any:
                    _counter["n"] += 1
                    return _real(*args, **kwargs)

                with patch.object(embedding_client, "create_embeddings", _counting):
                    t0 = time.perf_counter()
                    pair = retriever_cache.get_or_build_retriever(index_ref)
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0

                if counter["n"] != 1:
                    raise RuntimeError(
                        "一次 vectorstore 构造应恰好新建 1 个 embedding 客户端，"
                        f"实际 {counter['n']} 次——前提不成立，读数作废"
                    )
                if not isinstance(pair, tuple) or len(pair) != 2 or pair[0] is None:
                    raise RuntimeError(f"get_or_build_retriever 返回形态异常：{pair!r}")
                keepalive.append(pair)
                distinct.add(id(pair[1]))
                repeat_samples.append(elapsed_ms)
                builds_per_sample.append(counter["n"])
            samples_ms.extend(repeat_samples)
            per_repeat.append(
                {
                    "repeat": repeat,
                    "sample_count": len(repeat_samples),
                    "embedding_client_builds_per_sample": builds_per_sample,
                    "distinct_vectorstore_instances_cumulative": len(distinct),
                }
            )
        if len(distinct) != len(keepalive):
            raise RuntimeError(
                "flag 关时不得复用 vectorstore 实例："
                f"{len(keepalive)} 次构造只出现 {len(distinct)} 个实例"
            )

        return {
            "form": "raw_samples",
            "samples_ms": samples_ms,
            "per_repeat": per_repeat,
            "notes": {
                "what_is_measured": (
                    "默认检索路径的每请求构造：get_or_build_retriever(index_path) 一次调用"
                    "（flag 关 ⇒ 直调 build_retriever，含新建 embedding 客户端 + FAISS 反序列化）"
                ),
                "path_state": "RETRIEVAL_RETRIEVER_CACHE_ENABLED=false（默认值，本 bench 未改动）",
                "backend": backend,
                "fixture_kind": fixture_kind,
                "index_path": index_ref,
                "index_faiss_bytes": (index_path / "index.faiss").stat().st_size,
                "warmup": "每个 target 采样前预热 1 次；样本不含首次 import/反序列化",
                "cross_stack_caveat": (
                    "只测 faiss。milvus/qdrant 构造期只建连接，_get_embeddings 是惰性"
                    "调用点，需外部服务 ⇒ 未实测；跨栈差异不得由本读数推断成结论"
                ),
                "scope_caveat": (
                    "不含检索/嵌入请求（零网络），不是端到端检索延迟；"
                    "读数只在本指纹 + 同一索引规模下可比"
                ),
            },
        }


def _retrieval_embedding_client_measure(
    repeats: int, options: Dict[str, Any]
) -> Dict[str, Any]:
    """构造缝：``create_embeddings(get_default_config())`` 的单次构造代价。

    这是 faiss 档 ``FAISSVectorStore.load → _get_embeddings`` 每请求都会走的
    同一个生产函数。计时只包这一次调用；分位数由 ``bench.aggregate`` 池化。

    caveat（与结果文件 ``notes`` 同文）：构造期开销随 SDK/httpx/证书栈变化大
    （README §15.2 记过两套栈的构造量级差），读数只对本指纹环境成立；不含任何
    嵌入请求；milvus/qdrant 的 ``_get_embeddings`` 调用时机不同（惰性），
    **不得**把本读数直接读成它们的每请求代价。
    """
    samples_per_repeat = _retrieval_samples_per_repeat(options)
    from agentix.retrieval.config import get_default_config
    from agentix.retrieval.infra.embedding.client import create_embeddings

    cfg = get_default_config()
    create_embeddings(cfg)  # 预热：把首次 import / 证书栈初始化挪出样本

    samples_ms: List[float] = []
    per_repeat: List[Dict[str, Any]] = []
    keepalive: List[Any] = []
    distinct = set()
    for repeat in range(repeats):
        repeat_samples: List[float] = []
        for _ in range(samples_per_repeat):
            t0 = time.perf_counter()
            instance = create_embeddings(cfg)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if instance is None or not hasattr(instance, "embed_query"):
                raise RuntimeError(f"create_embeddings 返回形态异常：{instance!r}")
            keepalive.append(instance)
            distinct.add(id(instance))
            repeat_samples.append(elapsed_ms)
        samples_ms.extend(repeat_samples)
        per_repeat.append(
            {
                "repeat": repeat,
                "sample_count": len(repeat_samples),
                "distinct_embedding_instances_cumulative": len(distinct),
            }
        )
    if len(distinct) != len(keepalive):
        raise RuntimeError(
            "create_embeddings 应每次返回新实例："
            f"{len(keepalive)} 次构造只出现 {len(distinct)} 个实例"
        )

    from agentix.retrieval.settings import settings as retrieval_settings

    return {
        "form": "raw_samples",
        "samples_ms": samples_ms,
        "per_repeat": per_repeat,
        "notes": {
            "what_is_measured": (
                "create_embeddings(get_default_config()) 的单次构造"
                "（OpenAIEmbeddings + openai/httpx 客户端 + with_embedding_cache 包装）"
            ),
            "embedding_model": cfg.get("embedding_model"),
            "embedding_base_url": cfg.get("embedding_base_url"),
            "embedding_cache_enabled": bool(
                getattr(retrieval_settings, "EMBEDDING_CACHE_ENABLED", False)
            ),
            "warmup": "采样前预热 1 次；样本不含首次 import/证书栈初始化",
            "scope_caveat": (
                "只测构造、零网络：不发起任何嵌入请求，不是检索延迟；"
                "构造期开销随 SDK/httpx/证书栈变化大，读数只对本指纹环境成立"
            ),
            "cross_stack_caveat": (
                "faiss 在 load 时调用本缝，milvus/qdrant 的 _get_embeddings 是惰性"
                "调用点（构造期不新建客户端）⇒ 不得把本读数读成它们的每请求代价"
            ),
        },
    }


# ---------------------------------------------------------------------------
# 目标登记表：元数据是唯一判据源（touches_model 等不在 CLI 层再判一次）
# ---------------------------------------------------------------------------

TargetSpec = Dict[str, Any]

TARGETS: Dict[str, TargetSpec] = {
    "selfcheck": {
        "entry": "bench.targets:_selfcheck_once（骨架自证路径，不测生产组件）",
        "touches_model": False,
        "measure": _selfcheck_measure,
        "options": {},
    },
    "perf_runner": {
        "entry": "evals.performance.runner:run_benchmark(iterations=repeats)",
        "touches_model": False,
        "measure": _perf_runner_measure,
        "options": {"families": {"http": True, "stream": True, "cpu": True}},
    },
    "load_test": {
        "entry": "scripts/load_test.py:run_load（HTTP 压测原接口）",
        "touches_model": False,
        "measure": _load_test_measure,
        "options": {"url": None, "requests": 200, "workers": 10},
    },
    "retrieval_vectorstore_build": {
        "entry": (
            "agentix.retrieval.infra.vectorstore.retriever_cache:"
            "get_or_build_retriever(index_path)（flag 关 ⇒ 每次直调 build_retriever）"
        ),
        "touches_model": False,
        "measure": _retrieval_vectorstore_build_measure,
        "options": {"samples": 10, "index_path": None},
    },
    "retrieval_embedding_client": {
        "entry": (
            "agentix.retrieval.infra.embedding.client:"
            "create_embeddings(get_default_config())"
        ),
        "touches_model": False,
        "measure": _retrieval_embedding_client_measure,
        "options": {"samples": 10},
    },
}


def resolve(name: str) -> TargetSpec:
    if name not in TARGETS:
        raise KeyError(
            f"未知 bench target {name!r}，可用：{sorted(TARGETS)}"
        )
    return TARGETS[name]


def jsonable(per_repeat: Optional[List[Any]]) -> List[Any]:
    """per_repeat 进结果文件前的兜底：非 JSON 原生类型一律拒绝并指路。

    刻意不做默认 str() 转换——把不可序列化对象悄悄变成字符串，等于给读数
    穿隐身衣，下游 json.load 后再也认不出它曾经不是数。
    """
    if per_repeat is None:
        return []
    try:
        round_trip = json.loads(json.dumps(per_repeat, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"per_repeat 含不可 JSON 序列化内容，读数将不可复核：{exc}"
        ) from exc
    return round_trip
