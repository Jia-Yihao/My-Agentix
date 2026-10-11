r"""上传接口安全修复测试（P0 接口 bug 排查项）。

覆盖：
- 写路径穿越防护（file.filename 含 ..\ 不再写出上传目录）
- 分块限长写入（超限中断 + 半成品清理）
- 删除端点 file_id 的 UUID 格式校验（glob 注入防护）
"""


import pytest
from fastapi import HTTPException

from agentix.interfaces.http.v1.upload import (
    _safe_resolve,
    UPLOAD_DIR,
)


class TestWritePathTraversal:
    """上传写路径必须走 _safe_resolve。"""

    def test_safe_resolve_blocks_parent_traversal(self):
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(UPLOAD_DIR, "..\\..\\evil.txt")
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../../evil.txt",
            "..\\evil.txt",
            "a/b.txt",
            "a\\b.txt",
            "x\x00y.txt",
            "",
        ],
    )
    def test_safe_resolve_rejects_patterns(self, bad_name):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, bad_name)

    def test_safe_resolve_allows_normal_name(self):
        resolved = _safe_resolve(UPLOAD_DIR, "uuid-123_report.txt")
        assert resolved.parent == UPLOAD_DIR.resolve()
        assert resolved.name == "uuid-123_report.txt"

    def test_upload_save_goes_through_safe_resolve(self):
        """模拟上传端点逻辑：含 ..\\ 的 filename 拼接 uuid 前缀后仍须被拦。"""
        import uuid

        malicious = "..\\evil.txt"
        filename = f"{uuid.uuid4()}_{malicious}"
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, filename)


class TestLimitedSave:
    @pytest.fixture
    def tmp_upload_dir(self, tmp_path, monkeypatch):
        import agentix.interfaces.http.v1.upload as up

        monkeypatch.setattr(up, "UPLOAD_DIR", tmp_path)
        return tmp_path

    class _FakeUpload:
        """模拟 UploadFile.read(size) 行为。"""

        def __init__(self, payload: bytes):
            self._payload = payload
            self._pos = 0

        async def read(self, size: int) -> bytes:
            chunk = self._payload[self._pos : self._pos + size]
            self._pos += len(chunk)
            return chunk

    @pytest.mark.asyncio
    async def test_small_file_written_fully(self, tmp_upload_dir):
        from agentix.interfaces.http.v1.upload import _save_upload_limited

        dest = tmp_upload_dir / "ok.txt"
        written = await _save_upload_limited(self._FakeUpload(b"hello world"), dest)
        assert written == 11
        assert dest.read_bytes() == b"hello world"

    @pytest.mark.asyncio
    async def test_oversize_aborts_and_cleans(self, tmp_upload_dir, monkeypatch):
        import agentix.interfaces.http.v1.upload as up
        from agentix.interfaces.http.v1.upload import _save_upload_limited

        monkeypatch.setattr(up, "MAX_FILE_SIZE", 1024)
        dest = tmp_upload_dir / "big.bin"
        payload = b"x" * (2048 + 10)

        with pytest.raises(HTTPException) as exc_info:
            await _save_upload_limited(self._FakeUpload(payload), dest)
        assert exc_info.value.status_code == 413
        assert not dest.exists()  # 半成品已清理
        # Windows 文件句柄已释放（unlink 成功即证明）


class TestDeleteFileIdValidation:
    @pytest.mark.asyncio
    async def test_non_uuid_file_id_rejected(self, monkeypatch, tmp_path):
        import agentix.interfaces.http.v1.upload as up
        from agentix.interfaces.http.v1.upload import delete_uploaded_file

        monkeypatch.setattr(up, "UPLOAD_DIR", tmp_path)

        with pytest.raises(HTTPException) as exc_info:
            await delete_uploaded_file("*")  # glob 注入字符
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_wildcard_file_id_rejected(self, monkeypatch, tmp_path):
        from agentix.interfaces.http.v1.upload import delete_uploaded_file

        monkeypatch.setattr(
            __import__("agentix.interfaces.http.v1.upload", fromlist=["UPLOAD_DIR"]),
            "UPLOAD_DIR",
            tmp_path,
        )
        with pytest.raises(HTTPException):
            await delete_uploaded_file("abc_*")

    @pytest.mark.asyncio
    async def test_valid_uuid_but_missing_returns_404(self, monkeypatch, tmp_path):
        from agentix.interfaces.http.v1.upload import delete_uploaded_file

        monkeypatch.setattr(
            __import__("agentix.interfaces.http.v1.upload", fromlist=["UPLOAD_DIR"]),
            "UPLOAD_DIR",
            tmp_path,
        )
        with pytest.raises(HTTPException) as exc_info:
            await delete_uploaded_file("d3b07384-d9a0-4d9f-9a71-1a2b3c4d5e6f")
        assert exc_info.value.status_code == 404
