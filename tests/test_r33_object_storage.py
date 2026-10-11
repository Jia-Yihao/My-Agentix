"""R3.3 统一 MinIO：bucket 隔离（documents / milvus）验证。

覆盖：
- 业务 bucket 默认命名 documents（kb-files 废弃）；
- 默认凭据与 docker-compose MinIO 服务对齐；
- ensure_bucket 幂等语义（存在→True 不重建；404→建桶；其他失败→False 不抛出）。
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from agentix.retrieval.settings import settings as retrieval_settings  # noqa: E402


def test_bucket_default_documents():
    assert retrieval_settings.S3_BUCKET == "documents"
    assert "kb-files" not in retrieval_settings.S3_BUCKET


def test_default_credentials_match_compose():
    """与 docker-compose MINIO_ACCESS/SECRET_KEY=minioadmin 对齐，避免凭据漂移。"""
    assert retrieval_settings.S3_ACCESS_KEY == "minioadmin"
    assert retrieval_settings.S3_SECRET_KEY == "minioadmin"


def _reset_ensure_state():
    import agentix.retrieval.infra.storage.s3_minio as m

    m._bucket_ensured = False
    return m


def test_ensure_bucket_idempotent(monkeypatch):
    m = _reset_ensure_state()
    calls = {"head": 0, "create": 0}

    class FakeClient:
        def head_bucket(self, Bucket):
            calls["head"] += 1
            return {}

    monkeypatch.setattr(m, "_client", lambda: FakeClient())
    assert m.ensure_bucket() is True
    assert m.ensure_bucket() is True  # 第二次走进程内标记，不再探测
    assert calls["head"] == 1
    assert calls["create"] == 0


def test_ensure_bucket_creates_when_missing(monkeypatch):
    m = _reset_ensure_state()
    calls = {"head": 0, "create": 0}

    class FakeClient:
        def head_bucket(self, Bucket):
            calls["head"] += 1
            raise m.ClientError({"Error": {"Code": "404"}}, "HeadBucket")

        def create_bucket(self, **kwargs):
            calls["create"] += 1
            return {}

    monkeypatch.setattr(m, "_client", lambda: FakeClient())
    assert m.ensure_bucket() is True
    assert calls["create"] == 1


def test_ensure_bucket_failure_returns_false(monkeypatch):
    m = _reset_ensure_state()

    class FakeClient:
        def head_bucket(self, Bucket):
            raise ConnectionError("minio down")

    monkeypatch.setattr(m, "_client", lambda: FakeClient())
    assert m.ensure_bucket() is False  # 不抛出，交由调用方降级


def test_ensure_bucket_region_constraint(monkeypatch):
    """非 us-east-1 region 的 create_bucket 必须携带 LocationConstraint。"""
    m = _reset_ensure_state()
    captured = {}

    class FakeClient:
        def head_bucket(self, Bucket):
            raise m.ClientError({"Error": {"Code": "404"}}, "HeadBucket")

        def create_bucket(self, **kwargs):
            captured.update(kwargs)
            return {}

    monkeypatch.setattr(m, "_client", lambda: FakeClient())
    monkeypatch.setattr(retrieval_settings, "S3_REGION", "ap-beijing")
    m.ensure_bucket()
    assert captured.get("CreateBucketConfiguration") == {"LocationConstraint": "ap-beijing"}
