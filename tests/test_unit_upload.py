"""Unit tests for upload security - path traversal prevention."""

import pytest
from fastapi import HTTPException

from agentix.interfaces.http.v1.upload import _safe_resolve, UPLOAD_DIR


class TestSafeResolve:
    """Tests for _safe_resolve path traversal prevention."""

    def test_normal_filename(self):
        result = _safe_resolve(UPLOAD_DIR, "test_file.txt")
        assert result.name == "test_file.txt"
        assert str(UPLOAD_DIR.resolve()) in str(result)

    def test_path_traversal_blocked(self):
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(UPLOAD_DIR, "../../../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_nested_path_blocked(self):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, "subdir/../../secret.txt")

    def test_filename_with_slash_blocked(self):
        """Filenames with path separators should be blocked."""
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(UPLOAD_DIR, "some/path/myfile.pdf")
        assert exc_info.value.status_code == 400

    def test_dot_dot_prefix_blocked(self):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, "../sensitive_data.txt")

    def test_absolute_path_blocked(self):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, "/etc/shadow")

    def test_windows_style_traversal_blocked(self):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, "..\\..\\windows\\system32\\config\\sam")

    def test_null_byte_blocked(self):
        with pytest.raises(HTTPException):
            _safe_resolve(UPLOAD_DIR, "file.txt\x00.exe")

    def test_empty_filename(self):
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(UPLOAD_DIR, "")
        assert exc_info.value.status_code == 400


class TestUploadConstants:
    """Tests for upload configuration consistency."""

    def test_file_types_are_sets(self):
        from agentix.interfaces.http.v1.upload import ALLOWED_FILE_TYPES, ALLOWED_IMAGE_TYPES
        assert isinstance(ALLOWED_FILE_TYPES, set)
        assert isinstance(ALLOWED_IMAGE_TYPES, set)

    def test_image_types_are_subset_of_common_formats(self):
        from agentix.interfaces.http.v1.upload import ALLOWED_IMAGE_TYPES
        assert ".jpg" in ALLOWED_IMAGE_TYPES
        assert ".png" in ALLOWED_IMAGE_TYPES
        assert ".gif" in ALLOWED_IMAGE_TYPES
