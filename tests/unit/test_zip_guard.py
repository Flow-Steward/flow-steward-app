"""Unit tests for security/zip_guard.py.

Covers all safety checks:
- Normal safe zip → ZipGuardResult(safe=True)
- Empty zip → safe
- File count exceeded → unsafe
- Total size exceeded → unsafe
- Decompression ratio exceeded (zip bomb) → unsafe
- Path traversal: absolute path in entry name → unsafe
- Path traversal: '..' component → unsafe
- Corrupt / invalid zip data → unsafe
- Read error on buf → unsafe
- Nested zip within depth limit → safe
- Nested zip exceeding depth limit → unsafe (at-depth flagging)
- Nested corrupt inner zip → unsafe
"""

from __future__ import annotations

import io
import zipfile

from core.infrastructure.security.zip_guard import ZipGuardResult, _check_zip, validate_zip_safe

# ---------------------------------------------------------------------------
# Helpers to build in-memory zip archives
# ---------------------------------------------------------------------------


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Create an in-memory zip containing given filename → content mappings."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_nested_zip(inner_files: dict[str, bytes]) -> bytes:
    """Create a zip with one nested .zip entry."""
    inner_data = _make_zip(inner_files)
    return _make_zip({"inner.zip": inner_data})


def _buf(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


# ---------------------------------------------------------------------------
# validate_zip_safe
# ---------------------------------------------------------------------------


class TestValidateZipSafe:
    def test_normal_zip_is_safe(self):
        data = _make_zip({"hello.txt": b"hello world"})
        result = validate_zip_safe(_buf(data))
        assert result.safe is True
        assert result.reason is None

    def test_empty_zip_is_safe(self):
        data = _make_zip({})
        result = validate_zip_safe(_buf(data))
        assert result.safe is True

    def test_read_error_returns_unsafe(self):
        class BadBuf:
            def read(self):
                raise OSError("disk error")

        result = validate_zip_safe(BadBuf())  # type: ignore[arg-type]
        assert result.safe is False
        assert "Could not read archive" in result.reason

    def test_corrupt_zip_returns_unsafe(self):
        result = validate_zip_safe(_buf(b"this is not a zip"))
        assert result.safe is False
        assert "corrupt" in result.reason.lower() or "invalid" in result.reason.lower()

    def test_seekable_archive_is_not_read_as_one_buffer(self):
        class NoFullRead(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                # Python 3.11's zipfile reads the small trailing EOCD record with
                # read(-1); only an unbounded read from offset zero materializes
                # the entire archive and violates the streaming contract.
                if size < 0 and self.tell() == 0:
                    raise AssertionError("ZIP guard must not materialize the archive")
                return super().read(size)

        result = validate_zip_safe(NoFullRead(_make_zip({"invoice.txt": b"ok"})))
        assert result.safe is True


# ---------------------------------------------------------------------------
# _check_zip — file count
# ---------------------------------------------------------------------------


class TestFileCountLimit:
    def test_exceeding_file_count_returns_unsafe(self):
        files = {f"file{i}.txt": b"x" for i in range(5)}
        data = _make_zip(files)
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=3,
            current_depth=0,
        )
        assert result.safe is False
        assert "File count" in result.reason

    def test_at_limit_file_count_is_safe(self):
        files = {f"file{i}.txt": b"x" for i in range(3)}
        data = _make_zip(files)
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=3,
            current_depth=0,
        )
        assert result.safe is True


# ---------------------------------------------------------------------------
# _check_zip — total uncompressed size
# ---------------------------------------------------------------------------


class TestSizeLimit:
    def test_exceeding_size_returns_unsafe(self):
        # 1000 bytes uncompressed, limit 500
        files = {"big.bin": b"A" * 1000}
        data = _make_zip(files)
        result = _check_zip(
            data,
            max_ratio=1000.0,
            max_size_bytes=500,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False
        assert "uncompressed size" in result.reason

    def test_at_size_limit_is_safe(self):
        files = {"file.bin": b"A" * 100}
        data = _make_zip(files)
        result = _check_zip(
            data,
            max_ratio=1000.0,
            max_size_bytes=100,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is True


# ---------------------------------------------------------------------------
# _check_zip — decompression ratio (zip bomb)
# ---------------------------------------------------------------------------


class TestRatioLimit:
    def test_zip_bomb_detected(self):
        # Highly compressible data: 100KB of zeros
        payload = b"\x00" * 100_000
        files = {"bomb.bin": payload}
        data = _make_zip(files)
        # Compression ratio of ~100KB/~100B ≈ 1000x — set limit to 10x
        result = _check_zip(
            data,
            max_ratio=10.0,
            max_size_bytes=200_000,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False
        assert "ratio" in result.reason.lower() or "bomb" in result.reason.lower()

    def test_zero_compressed_size_skips_ratio_check(self):
        # An empty file has 0 compressed size — ratio check should be skipped
        files = {"empty.txt": b""}
        data = _make_zip(files)
        result = _check_zip(
            data,
            max_ratio=0.001,
            max_size_bytes=10_000,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        # Should not fail on ratio for a zero-size entry
        assert result.safe is True


# ---------------------------------------------------------------------------
# _check_zip — path traversal
# ---------------------------------------------------------------------------


class TestPathTraversal:
    def test_dotdot_path_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("../escape.txt")
            zf.writestr(info, "evil")
        result = _check_zip(
            buf.getvalue(),
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False
        assert "traversal" in result.reason.lower() or ".." in result.reason

    def test_absolute_path_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("/etc/passwd")
            zf.writestr(info, "root:x:0:0")
        result = _check_zip(
            buf.getvalue(),
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False
        assert "traversal" in result.reason.lower()

    def test_normal_nested_path_is_safe(self):
        data = _make_zip({"subdir/file.txt": b"content"})
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is True


# ---------------------------------------------------------------------------
# _check_zip — nesting
# ---------------------------------------------------------------------------


class TestNesting:
    def test_depth_exceeded_before_open_returns_unsafe(self):
        data = _make_zip({"file.txt": b"content"})
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=1,
            max_file_count=1000,
            current_depth=2,
        )
        assert result.safe is False
        assert "depth" in result.reason.lower()

    def test_nested_zip_within_depth_limit_safe(self):
        data = _make_nested_zip({"inner.txt": b"safe"})
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is True

    def test_nested_zip_at_depth_limit_flagged(self):
        # At max_nesting_depth, finding a .zip entry should fail without descending
        inner = _make_zip({"deep.txt": b"content"})
        data = _make_zip({"inner.zip": inner})
        # current_depth=max_nesting_depth → at-depth branch
        result = _check_zip(
            data,
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=1,
            max_file_count=1000,
            current_depth=1,
        )
        assert result.safe is False
        assert "depth" in result.reason.lower()

    def test_nested_zip_with_bomb_inner_detected(self):
        # Inner zip contains a bomb — detection must propagate up
        inner_payload = b"\x00" * 100_000
        inner_data = _make_zip({"bomb.bin": inner_payload})
        outer_data = _make_zip({"inner.zip": inner_data})
        result = _check_zip(
            outer_data,
            max_ratio=10.0,
            max_size_bytes=200_000,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False

    def test_nested_corrupt_inner_zip_returns_unsafe(self):
        # Extension a corrupt bytes sequence as a .zip entry
        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as zf:
            zf.writestr("corrupt.zip", b"definitely not a zip")
        result = _check_zip(
            outer_buf.getvalue(),
            max_ratio=100.0,
            max_size_bytes=10**9,
            max_nesting_depth=3,
            max_file_count=1000,
            current_depth=0,
        )
        assert result.safe is False


# ---------------------------------------------------------------------------
# ZipGuardResult dataclass
# ---------------------------------------------------------------------------


class TestZipGuardResult:
    def test_safe_result_has_no_reason(self):
        r = ZipGuardResult(safe=True)
        assert r.reason is None

    def test_unsafe_result_has_reason(self):
        r = ZipGuardResult(safe=False, reason="too big")
        assert r.safe is False
        assert r.reason == "too big"


# ---------------------------------------------------------------------------
# DoS hardening: Office ZIP-container documents reuse this guard (FS-DEEP-CAND-033)
# and the extension archive path runs it before temp write/extract (FS-DEEP-CAND-038).
# ---------------------------------------------------------------------------


class TestZipGuardReusedForContainersAndArchives:
    def test_office_zip_bomb_payload_is_unsafe(self):
        """A highly compressible Office-style container trips the decompression-ratio guard."""
        data = _make_zip({"word/document.xml": b"\x00" * (5 * 1024 * 1024)})
        result = validate_zip_safe(_buf(data))
        assert result.safe is False
        assert result.reason is not None

    def test_small_safe_office_container_is_safe(self):
        data = _make_zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>"})
        result = validate_zip_safe(_buf(data))
        assert result.safe is True
