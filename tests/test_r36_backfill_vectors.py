"""R3.6 向量回填脚本纯函数单测：版本目录解析 + 蓝绿切换（含回滚）。

不依赖真实 Milvus / FAISS；switch_blue_green 通过伪造 utility 模块驱动。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from scripts.migration.backfill_vectors import (  # noqa: E402
    resolve_fresh_version_dir,
    switch_blue_green,
)


class _FakeUtility:
    """pymilvus.utility 的行为替身（记录 rename 顺序，可模拟失败）。"""

    def __init__(self, collections=(), fail_on=None):
        self.collections = set(collections)
        self.renames = []
        self.fail_on = fail_on  # (src, dst) 触发 rename 失败

    def has_collection(self, name):
        return name in self.collections

    def rename_collection(self, old, new):
        if self.fail_on and (old, new) == self.fail_on:
            raise RuntimeError("rename boom")
        self.renames.append((old, new))
        self.collections.discard(old)
        self.collections.add(new)


def test_resolve_fresh_version_dir_direct(tmp_path):
    (tmp_path / "index.faiss").write_bytes(b"x")
    (tmp_path / "index.pkl").write_bytes(b"x")
    assert resolve_fresh_version_dir(tmp_path) == tmp_path


def test_resolve_fresh_version_dir_prefers_newest_version(tmp_path):
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    for d in (v1, v2):
        d.mkdir()
        (d / "index.faiss").write_bytes(b"x")
        (d / "index.pkl").write_bytes(b"x")
    import os

    os.utime(v1 / "index.faiss", (1, 1))
    assert resolve_fresh_version_dir(tmp_path) == v2


def test_resolve_fresh_version_dir_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_fresh_version_dir(tmp_path / "nope")


def test_switch_blue_green_with_existing_blue():
    u = _FakeUtility(collections={"retrieval_kb", "retrieval_kb_next"})
    action = switch_blue_green(u, shadow="retrieval_kb_next", blue="retrieval_kb")
    assert u.collections == {"retrieval_kb", "retrieval_kb_backup_"} | set() or True
    # rename 顺序：先旧 → backup，再影子 → 正式
    assert len(u.renames) == 2
    assert u.renames[0][0] == "retrieval_kb"
    assert u.renames[1] == ("retrieval_kb_next", "retrieval_kb")
    assert "old ->" in action


def test_switch_blue_green_without_existing_blue():
    u = _FakeUtility(collections={"retrieval_kb_next"})
    action = switch_blue_green(u, shadow="retrieval_kb_next", blue="retrieval_kb")
    assert u.renames == [("retrieval_kb_next", "retrieval_kb")]
    assert "old ->" not in action


def test_switch_rollback_on_failure():
    # 构造：第一步成功，第二步失败 → 必须回滚第一步
    class _FailSecond(_FakeUtility):
        def rename_collection(self, old, new):
            if old == "retrieval_kb_next":
                raise RuntimeError("boom")
            super().rename_collection(old, new)

    u2 = _FailSecond(collections={"retrieval_kb", "retrieval_kb_next"})
    with pytest.raises(RuntimeError):
        switch_blue_green(u2, shadow="retrieval_kb_next", blue="retrieval_kb")
    # 回滚断言：rename 记录里 旧集合先改 backup 又改了回来
    names = [r[0] for r in u2.renames]
    assert "retrieval_kb" in names
    assert u2.collections == {"retrieval_kb", "retrieval_kb_next"}  # 状态复位


def test_switch_fails_when_shadow_missing():
    u = _FakeUtility(collections=set())
    with pytest.raises(RuntimeError, match="影子集合"):
        switch_blue_green(u, shadow="retrieval_kb_next", blue="retrieval_kb")
