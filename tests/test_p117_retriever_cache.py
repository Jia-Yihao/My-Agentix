"""R-P3 检索器进程内缓存守卫（README §15.2 口径③ 的代码侧）。

覆盖面与验收口径：

1. **flag 关 = 逐位等价**：每次调用都直调 ``build_retriever``（同一对象图、同一
   异常），缓存区始终为空——这是 Strangler 惯例的硬要求，不是"看起来一样"；
2. **flag 开 = 同签名只构造一次**：第二次起复用同一 ``(retriever, vectorstore)``
   实例（``is`` 判等，不是值相等）；并发首建 single-flight（8 线程只构造 1 次）；
3. **失效只由磁盘/配置事实决定**：重写 ``index.faiss``（mtime/size 变）即重建，
   换 embedding 模型或 base_url 即重建；没有 TTL、没有显式失效钩子；
4. **异常语义不变**：索引文件缺失、构造抛错 ⇒ 照旧抛出且不入缓存
   （上层靠 ``FileNotFoundError`` 判 degraded，缓存不得把它变静默）；
5. **有界**：LRU 上限生效，最旧条目被逐出；
6. **接线守卫**：请求路径两个调用点必须走 helper，而
   ``index_completion_verifier`` 必须**不**走（自证校验无效）。

默认值本身走源码级断言（第 5 条测试）：类体里的 ``os.getenv`` 只在导入时求值，
用断言默认字符串比断言运行时属性更能抓住"有人把默认翻成 true"。
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def _write_index(dir_path: Path, payload: bytes = b"fake-index") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    index_file = dir_path / "index.faiss"
    index_file.write_bytes(payload)
    return index_file


class _FakeBuild:
    """计数 + 可控失败/延时的 ``build_retriever`` 替身。

    每次调用返回**新**元组 ⇒ 缓存命中与否可用 ``is`` 严格区分（值相等会骗过断言）。
    """

    def __init__(self, *, fail_with: BaseException | None = None, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self._fail_with = fail_with
        self._delay = delay
        self._lock = threading.Lock()

    def __call__(self, index_path: str):
        with self._lock:
            self.calls.append(str(index_path))
            n = len(self.calls)
        if self._delay:
            time.sleep(self._delay)
        if self._fail_with is not None:
            raise self._fail_with
        return (f"retriever-{n}", f"vectorstore-{n}")


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例前后清空进程内缓存——模块级字典是跨用例共享状态。"""
    from agentix.retrieval.infra.vectorstore import retriever_cache

    retriever_cache.reset_retriever_cache()
    yield
    retriever_cache.reset_retriever_cache()


@pytest.fixture
def fake_build(monkeypatch):
    """把 chat_service 上的 ``build_retriever`` 换成替身（helper 是调用时导入的）。"""

    def _install(fake: _FakeBuild) -> _FakeBuild:
        monkeypatch.setattr(
            "agentix.retrieval.usecases.chat_service.build_retriever", fake
        )
        return fake

    return _install


@pytest.fixture
def flag_on(monkeypatch):
    from agentix.retrieval.settings import settings

    monkeypatch.setattr(settings, "RETRIEVAL_RETRIEVER_CACHE_ENABLED", True)
    return settings


# ---------------------------------------------------------------------------
# 裸行为：flag 关 = 逐位等价
# ---------------------------------------------------------------------------


def test_default_flag_is_off_and_bounded_in_source():
    """默认必须关 + 上限 8：类体 os.getenv 只在导入时求值，故断源码字符串。"""
    src = _read("agentix/retrieval/settings.py")
    assert (
        'RETRIEVAL_RETRIEVER_CACHE_ENABLED = _bool_env("RETRIEVAL_RETRIEVER_CACHE_ENABLED", "false")'
        in src
    ), "新开关默认必须为 false（工程约定 1）"
    assert 'os.getenv("RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES", "8")' in src


def test_flag_off_builds_every_call_and_keeps_cache_empty(tmp_path, fake_build):
    from agentix.retrieval.infra.vectorstore.retriever_cache import (
        cache_stats,
        get_or_build_retriever,
    )

    fake = fake_build(_FakeBuild())
    index_dir = tmp_path / "kb-1"
    _write_index(index_dir)

    pairs = [get_or_build_retriever(str(index_dir)) for _ in range(3)]

    assert len(fake.calls) == 3, "flag 关时必须每次直调 build_retriever"
    assert len({id(p) for p in pairs}) == 3, "flag 关时不得复用任何实例"
    assert cache_stats()["entries"] == 0, "flag 关时缓存区必须始终为空"


def test_flag_off_does_not_swallow_exceptions(tmp_path, fake_build):
    """基线异常语义：flag 关时构造抛什么就抛什么，不许被缓存层改写。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import get_or_build_retriever

    fake_build(_FakeBuild(fail_with=FileNotFoundError("no index")))
    with pytest.raises(FileNotFoundError):
        get_or_build_retriever(str(tmp_path / "missing"))


# ---------------------------------------------------------------------------
# flag 开：复用、失效、异常、有界、并发
# ---------------------------------------------------------------------------


def test_flag_on_reuses_same_pair_object(tmp_path, fake_build, flag_on):
    from agentix.retrieval.infra.vectorstore.retriever_cache import (
        cache_stats,
        get_or_build_retriever,
    )

    fake = fake_build(_FakeBuild())
    index_dir = tmp_path / "kb-2"
    _write_index(index_dir)

    first = get_or_build_retriever(str(index_dir))
    second = get_or_build_retriever(str(index_dir))
    third = get_or_build_retriever(str(index_dir))

    assert len(fake.calls) == 1, "同签名只应构造一次"
    assert second is first and third is first, "命中必须是同一对象（is 判等）"
    assert cache_stats()["entries"] == 1


def test_index_rewrite_invalidates(tmp_path, fake_build, flag_on):
    """索引换代 = 文件被重写 ⇒ 签名变 ⇒ 自动重建（无 TTL、无失效钩子）。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import get_or_build_retriever

    fake = fake_build(_FakeBuild())
    index_dir = tmp_path / "kb-3"
    index_file = _write_index(index_dir)

    get_or_build_retriever(str(index_dir))
    index_file.write_bytes(b"fake-index-v2-longer")  # size 变
    # mtime_ns 在粗粒度文件系统上可能不变，显式推进，使本用例只测签名而非时钟
    future = time.time_ns() + 10**9
    os.utime(index_file, ns=(future, future))

    rebuilt = get_or_build_retriever(str(index_dir))

    assert len(fake.calls) == 2, "文件被重写后必须重建"
    assert rebuilt == ("retriever-2", "vectorstore-2")


def test_embedding_model_change_invalidates(tmp_path, fake_build, flag_on, monkeypatch):
    """签名含 embedding 模型/base_url：换模型必须重建（跨模型复用会得到错向量）。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import get_or_build_retriever

    fake = fake_build(_FakeBuild())
    index_dir = tmp_path / "kb-4"
    _write_index(index_dir)

    holder = {"model": "text-embedding-bge-m3", "base": "http://127.0.0.1:1234/v1"}
    monkeypatch.setattr(
        "agentix.retrieval.config.get_default_config",
        lambda: {
            "embedding_model": holder["model"],
            "embedding_base_url": holder["base"],
            "search_k": 5,
        },
    )

    get_or_build_retriever(str(index_dir))
    get_or_build_retriever(str(index_dir))  # 命中
    holder["model"] = "another-model"
    get_or_build_retriever(str(index_dir))  # 重建

    assert len(fake.calls) == 2


def test_missing_index_file_falls_back_to_direct_build(tmp_path, fake_build, flag_on):
    """stat 失败 ⇒ 不走缓存（保证 FileNotFoundError 抛出时机与基线一致）。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import (
        cache_stats,
        get_or_build_retriever,
    )

    fake = fake_build(_FakeBuild(fail_with=FileNotFoundError("index not found")))
    with pytest.raises(FileNotFoundError):
        get_or_build_retriever(str(tmp_path / "no-such-kb"))
    assert len(fake.calls) == 1
    assert cache_stats()["entries"] == 0


def test_failed_build_is_not_cached(tmp_path, fake_build, flag_on):
    from agentix.retrieval.infra.vectorstore.retriever_cache import (
        cache_stats,
        get_or_build_retriever,
    )

    fake = fake_build(_FakeBuild(fail_with=RuntimeError("boom")))
    index_dir = tmp_path / "kb-5"
    _write_index(index_dir)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            get_or_build_retriever(str(index_dir))

    assert len(fake.calls) == 2, "失败的构造不得被缓存（否则错误会被永久固化）"
    assert cache_stats()["entries"] == 0


def test_lru_evicts_oldest_and_is_bounded(tmp_path, fake_build, flag_on, monkeypatch):
    from agentix.retrieval.infra.vectorstore import retriever_cache
    from agentix.retrieval.settings import settings

    monkeypatch.setattr(settings, "RETRIEVAL_RETRIEVER_CACHE_MAX_ENTRIES", 2)
    fake = fake_build(_FakeBuild())

    dirs = []
    for i in range(3):
        d = tmp_path / f"kb-lru-{i}"
        _write_index(d)
        dirs.append(d)
        retriever_cache.get_or_build_retriever(str(d))

    assert len(fake.calls) == 3
    assert retriever_cache.cache_stats()["entries"] == 2, "上限 2 必须生效"

    retriever_cache.get_or_build_retriever(str(dirs[0]))
    assert len(fake.calls) == 4, "最旧条目（dirs[0]）已被逐出 ⇒ 必须重建"


def test_concurrent_first_build_is_single_flight(tmp_path, fake_build, flag_on):
    """并发首建只构造一次：构造持锁（口径 4），不是"可能只构造一次"。"""
    from agentix.retrieval.infra.vectorstore.retriever_cache import get_or_build_retriever

    fake = fake_build(_FakeBuild(delay=0.05))
    index_dir = tmp_path / "kb-6"
    _write_index(index_dir)

    results: list[object] = []
    start = threading.Barrier(8)

    def _worker() -> None:
        start.wait()
        results.append(get_or_build_retriever(str(index_dir)))

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake.calls) == 1, f"并发首建构造了 {len(fake.calls)} 次（应 1 次）"
    assert len({id(r) for r in results}) == 1, "8 个线程必须拿到同一实例"


def test_reset_clears_cache(tmp_path, fake_build, flag_on):
    from agentix.retrieval.infra.vectorstore.retriever_cache import (
        cache_stats,
        get_or_build_retriever,
        reset_retriever_cache,
    )

    fake = fake_build(_FakeBuild())
    index_dir = tmp_path / "kb-7"
    _write_index(index_dir)

    get_or_build_retriever(str(index_dir))
    assert cache_stats()["entries"] == 1

    reset_retriever_cache()
    assert cache_stats()["entries"] == 0
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 接线守卫：谁必须走 helper、谁必须不走
# ---------------------------------------------------------------------------


def test_request_paths_use_helper_and_verifier_does_not():
    # 注意：``get_or_build_retriever(...)`` 里含子串 ``build_retriever(...)``，
    # 所以"没直调"必须用负向后顾的 re，不能用 ``in``（会假红）。
    raw_call = re.compile(r"(?<!get_or_)build_retriever\(artifact\.index_path\)")

    adapter = _read("agentix/retrieval/usecases/retrieval_port_adapter.py")
    assert "get_or_build_retriever(artifact.index_path)" in adapter
    assert not raw_call.search(adapter), "请求路径必须走 helper，不得直调"

    chat_service = _read("agentix/retrieval/usecases/chat_service.py")
    assert "get_or_build_retriever(artifact.index_path)" in chat_service
    assert not raw_call.search(chat_service)

    verifier = _read("agentix/retrieval/usecases/index_completion_verifier.py")
    assert "build_retriever(index_path)" in verifier
    assert "get_or_build_retriever" not in verifier, (
        "校验器必须独立从磁盘重新加载产物；走缓存等于自证，校验失效"
    )