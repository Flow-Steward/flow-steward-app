"""DoS hardening tests for the extension archive install path (FS-DEEP-CAND-038).

Negative + positive coverage for:
  - Endpoint chunked bounded reader: oversized multipart upload rejected at read
    time (413) before the whole archive is buffered.
  - Service: zip safety guard runs BEFORE the full archive is written to a temp
    file / extracted, and an oversized archive is rejected before temp write.
  - Positive: a normal, within-limit, safe archive passes the guard ordering and
    reaches extraction.

Run:
    .venv/bin/python -m pytest tests/unit/test_extension_archive_dos.py -q
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _skip_only_missing_optional_dep(exc: Exception, *, context: str) -> None:
    if isinstance(exc, ModuleNotFoundError) and getattr(exc, "name", "") in _OPTIONAL_TEST_DEPS:
        pytest.skip(f"{context}: missing optional dependency '{exc.name}'")
    raise exc


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeUpload:
    """Minimal async UploadFile stub yielding bytes in chunks."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.filename = "ext.zip"
        self.content_type = "application/zip"

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Endpoint-level bounded reader
# ---------------------------------------------------------------------------


class TestExtensionUploadBoundedReader:
    def _reader(self):
        try:
            from core.entrypoints.api.reactions_extensions import (
                _MAX_EXTENSION_ARCHIVE_BYTES,
                _read_extension_upload_with_limit,
            )
        except Exception as e:  # pragma: no cover
            _skip_only_missing_optional_dep(e, context="import reactions_extensions reader")
        return _read_extension_upload_with_limit, _MAX_EXTENSION_ARCHIVE_BYTES

    def test_oversized_upload_rejected_at_read_time(self):
        reader, max_bytes = self._reader()
        up = _FakeUpload(b"x" * (max_bytes + 1))
        _raises_input_96_1 = reader(up, max_bytes=max_bytes)
        with pytest.raises(ValueError):
            asyncio.run(_raises_input_96_1)

    def test_within_limit_upload_returned_whole(self):
        reader, _max_bytes = self._reader()
        payload = _make_zip({"extension.yaml": b"name: demo"})
        up = _FakeUpload(payload)
        out = asyncio.run(reader(up, max_bytes=10 * 1024 * 1024))
        assert out == payload


# ---------------------------------------------------------------------------
# Service-level guard ordering
# ---------------------------------------------------------------------------


def _make_service():
    """Build an archive install component with mock collaborators."""
    try:
        from core.application.extensions.archive_install_service import (
            ExtensionArchiveInstallService,
        )
        from core.application.extensions.archive_new_install_service import (
            ExtensionArchiveNewInstallService,
        )
        from core.application.extensions.archive_update_service import (
            ExtensionArchiveUpdateService,
        )
        from core.application.extensions.binding_cleanup_service import (
            ExtensionCleanupService,
        )
        from core.application.extensions.binding_context import ExtensionLifecycleContext
        from core.application.extensions.binding_projection_service import (
            ExtensionBindingProjectionService,
        )
        from core.application.extensions.extension_state_service import ExtensionStateService
        from core.application.extensions.managed_release_service import ManagedReleaseService
    except Exception as e:  # pragma: no cover
        _skip_only_missing_optional_dep(e, context="import extension archive service")

    logger = MagicMock()
    logger.conn = None

    def get_logger():
        return logger

    context = ExtensionLifecycleContext()
    managed_release = ManagedReleaseService(context=context)
    cleanup = ExtensionCleanupService(context=context, managed_release=managed_release)
    projection = ExtensionBindingProjectionService(
        context=context,
        managed_release=managed_release,
    )
    state = ExtensionStateService(
        context=context,
        projection=projection,
        cleanup=cleanup,
        managed_release=managed_release,
        get_logger=get_logger,
    )
    new_install = ExtensionArchiveNewInstallService(
        context=context,
        projection=projection,
        managed_release=managed_release,
        cleanup=cleanup,
        state=state,
        get_logger=get_logger,
    )
    update = ExtensionArchiveUpdateService(
        context=context,
        projection=projection,
        managed_release=managed_release,
        cleanup=cleanup,
        state=state,
    )
    svc = ExtensionArchiveInstallService(
        context=context,
        projection=projection,
        managed_release=managed_release,
        state=state,
        new_install=new_install,
        update=update,
        get_logger=get_logger,
    )
    return svc, ExtensionArchiveInstallService


def _admin_scope():
    from core.application.auth.config import DEFAULT_ACCOUNT_ID
    from core.application.management.scope import ScopeContext

    return ScopeContext(
        account_id=DEFAULT_ACCOUNT_ID,
        actor_id="archive-test-admin",
        principal_type="admin",
    )


class TestExtensionServiceArchiveGuards:
    def test_unauthorized_archive_path_rejected_before_filesystem_io(self, tmp_path: Path):
        from core.application.auth.config import DEFAULT_ACCOUNT_ID
        from core.application.management.errors import ServiceForbiddenError
        from core.application.management.scope import ScopeContext

        svc, _cls = _make_service()
        archive = tmp_path / "private.zip"
        viewer_scope = ScopeContext(
            account_id=DEFAULT_ACCOUNT_ID,
            actor_id="archive-test-viewer",
            principal_type="viewer",
        )

        with (
            patch.object(Path, "is_file", side_effect=AssertionError("must not inspect path")),
            patch.object(Path, "stat", side_effect=AssertionError("must not stat path")),
            patch.object(Path, "open", side_effect=AssertionError("must not open path")),
            patch.object(os, "open", side_effect=AssertionError("must not open path")),
            patch.object(os, "fstat", side_effect=AssertionError("must not fstat")),
            patch.object(os, "fdopen", side_effect=AssertionError("must not wrap fd")),
            pytest.raises(ServiceForbiddenError, match="Admin role required"),
        ):
            svc.install_extension_archive_path(
                archive_path=archive,
                scope=viewer_scope,
            )

    def test_oversized_archive_path_rejected_from_open_descriptor_before_read(self, tmp_path: Path):
        from core.application.extensions import archive_install_service as mod
        from core.application.management.errors import ServiceValidationError

        svc, _cls = _make_service()
        archive = tmp_path / "oversized.zip"
        with archive.open("wb") as source:
            source.seek(mod._MAX_EXTENSION_ARCHIVE_BYTES)
            source.write(b"x")

        real_open = os.open
        _raises_input_233_1 = _admin_scope()
        with (
            patch.object(os, "open", wraps=real_open) as open_mock,
            patch.object(os, "fdopen", side_effect=AssertionError("archive must not be read")),
            pytest.raises(
                ServiceValidationError,
                match=(
                    "Extension archive is too large "
                    f"— maximum {mod._MAX_EXTENSION_ARCHIVE_BYTES} bytes"
                ),
            ),
        ):
            svc.install_extension_archive_path(archive_path=archive, scope=_raises_input_233_1)

        open_mock.assert_called_once()

    def test_archive_path_reads_at_most_limit_plus_one_before_delegating(self, tmp_path: Path):
        svc, _cls = _make_service()
        archive = tmp_path / "accepted.zip"
        archive.write_bytes(b"archive-content")

        expected = MagicMock()
        scope = _admin_scope()
        real_open = os.open
        real_fstat = os.fstat
        real_fdopen = os.fdopen
        with (
            patch.object(os, "open", wraps=real_open) as open_mock,
            patch.object(os, "fstat", wraps=real_fstat) as fstat_mock,
            patch.object(os, "fdopen", wraps=real_fdopen) as fdopen_mock,
            patch.object(svc, "install_extension_archive", return_value=expected) as install,
        ):
            result = svc.install_extension_archive_path(archive_path=archive, scope=scope)

        assert result is expected
        open_mock.assert_called_once()
        opened_path, flags = open_mock.call_args.args
        assert Path(opened_path) == archive
        expected_flags = os.O_RDONLY
        for flag_name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
            expected_flags |= getattr(os, flag_name, 0)
        assert flags == expected_flags
        opened_fd = fstat_mock.call_args.args[0]
        assert isinstance(opened_fd, int)
        fstat_mock.assert_called_once_with(opened_fd)
        fdopen_mock.assert_called_once_with(opened_fd, "rb", closefd=True)
        install.assert_called_once_with(
            filename="accepted.zip",
            content=b"archive-content",
            scope=scope,
            approved_browser_authority_impact_hash="",
        )

    def test_archive_path_rejects_post_stat_growth_without_delegating(self, tmp_path: Path):
        from core.application.extensions import archive_install_service as mod
        from core.application.management.errors import ServiceValidationError

        svc, _cls = _make_service()
        archive = tmp_path / "grown.zip"
        limit = 8
        archive.write_bytes(b"x" * limit)

        class _GrowingSource:
            def __init__(self, source) -> None:
                self.source = source
                self.read_sizes: list[int] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.source.close()
                return False

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return b"x" * (limit + 1)

        real_fdopen = os.fdopen
        growing_sources: list[_GrowingSource] = []

        def _growing_fdopen(fd: int, *args, **kwargs):
            source = _GrowingSource(real_fdopen(fd, *args, **kwargs))
            growing_sources.append(source)
            return source

        _raises_input_318_1 = _admin_scope()
        with (
            patch.object(mod, "_MAX_EXTENSION_ARCHIVE_BYTES", limit),
            patch.object(os, "fdopen", side_effect=_growing_fdopen),
            patch.object(svc, "install_extension_archive") as install,
            pytest.raises(
                ServiceValidationError,
                match=f"Extension archive is too large — maximum {limit} bytes",
            ),
        ):
            svc.install_extension_archive_path(archive_path=archive, scope=_raises_input_318_1)

        assert len(growing_sources) == 1
        assert growing_sources[0].read_sizes == [limit + 1]
        install.assert_not_called()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
    def test_archive_path_rejects_symlink_without_wrapping_or_reading_target(self, tmp_path: Path):
        from core.application.management.errors import ServiceValidationError

        svc, _cls = _make_service()
        target = tmp_path / "target.zip"
        target.write_bytes(b"target-content")
        archive = tmp_path / "link.zip"
        archive.symlink_to(target)

        _raises_input_343_1 = _admin_scope()
        with (
            patch.object(os, "fdopen", side_effect=AssertionError("target must not be read")),
            patch.object(svc, "install_extension_archive") as install,
            pytest.raises(ServiceValidationError, match="readable regular file"),
        ):
            svc.install_extension_archive_path(archive_path=archive, scope=_raises_input_343_1)

        install.assert_not_called()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
    def test_archive_path_rejects_fifo_without_wrapping_or_reading(self, tmp_path: Path):
        from core.application.management.errors import ServiceValidationError

        svc, _cls = _make_service()
        archive = tmp_path / "stream.zip"
        os.mkfifo(archive)

        _raises_input_360_1 = _admin_scope()
        with (
            patch.object(os, "fdopen", side_effect=AssertionError("FIFO must not be read")),
            patch.object(svc, "install_extension_archive") as install,
            pytest.raises(ServiceValidationError, match="regular file"),
        ):
            svc.install_extension_archive_path(archive_path=archive, scope=_raises_input_360_1)

        install.assert_not_called()

    def test_archive_path_reads_original_descriptor_when_path_is_replaced(self, tmp_path: Path):
        svc, _cls = _make_service()
        archive = tmp_path / "raced.zip"
        replacement = tmp_path / "replacement.zip"
        archive.write_bytes(b"original-content")
        replacement.write_bytes(b"replacement-content")
        real_open = os.open
        opened_fds: list[int] = []

        def _open_then_replace(path: os.PathLike[str] | str, flags: int) -> int:
            fd = real_open(path, flags)
            opened_fds.append(fd)
            os.replace(replacement, archive)
            return fd

        expected = MagicMock()
        scope = _admin_scope()
        with (
            patch.object(os, "open", side_effect=_open_then_replace) as open_mock,
            patch.object(svc, "install_extension_archive", return_value=expected) as install,
        ):
            result = svc.install_extension_archive_path(archive_path=archive, scope=scope)

        assert result is expected
        open_mock.assert_called_once()
        assert len(opened_fds) == 1
        assert archive.read_bytes() == b"replacement-content"
        install.assert_called_once_with(
            filename="raced.zip",
            content=b"original-content",
            scope=scope,
            approved_browser_authority_impact_hash="",
        )

    def test_oversized_archive_rejected_before_temp_write(self):
        from core.application.extensions import archive_install_service as mod

        svc, _cls = _make_service()
        oversized = b"x" * (mod._MAX_EXTENSION_ARCHIVE_BYTES + 1)

        from core.application.management.errors import ServiceValidationError

        with (
            patch.object(mod, "service_scope", return_value=MagicMock()),
            patch.object(mod, "require_global_extension_lifecycle_scope"),
            patch.object(Path, "write_bytes") as write_mock,
            pytest.raises(ServiceValidationError),
        ):
            svc.install_extension_archive(filename="big.zip", content=oversized)

        write_mock.assert_not_called()

    def test_zip_bomb_rejected_before_temp_write_and_extract(self):
        from core.application.extensions import archive_install_service as mod
        from core.application.management.errors import ServiceValidationError

        svc, _cls = _make_service()
        # Highly compressible -> decompression ratio guard trips.
        bomb = _make_zip({"payload.bin": b"\x00" * (5 * 1024 * 1024)})

        with (
            patch.object(mod, "service_scope", return_value=MagicMock()),
            patch.object(mod, "require_global_extension_lifecycle_scope"),
            patch.object(Path, "write_bytes") as write_mock,
            pytest.raises(ServiceValidationError) as exc,
        ):
            svc.install_extension_archive(filename="bomb.zip", content=bomb)

        # Guard must run before the archive is written to disk.
        write_mock.assert_not_called()
        assert "Rejected archive" in str(exc.value)

    def test_safe_archive_passes_guard_and_reaches_write(self):
        """A small safe zip is not blocked by the guard (guard ordering preserved)."""
        from core.application.extensions import archive_install_service as mod

        svc, _cls = _make_service()
        safe = _make_zip({"extension.yaml": b"name: demo", "handler.py": b"print('hi')"})

        # Patch validate_zip_safe to confirm it is invoked BEFORE write_bytes, then
        # short-circuit the rest of install with a sentinel raised from write_bytes.
        call_order: list[str] = []

        from core.infrastructure.security import zip_guard as zg_mod

        real_validate = zg_mod.validate_zip_safe

        def _tracking_validate(buf, **kwargs):
            call_order.append("guard")
            return real_validate(buf, **kwargs)

        def _tracking_write(self, data):
            call_order.append("write")
            raise RuntimeError("stop-after-write")

        with (
            patch.object(mod, "service_scope", return_value=MagicMock()),
            patch.object(mod, "require_global_extension_lifecycle_scope"),
            patch.object(zg_mod, "validate_zip_safe", side_effect=_tracking_validate),
            patch.object(Path, "write_bytes", _tracking_write),
            pytest.raises(RuntimeError, match="stop-after-write"),
        ):
            svc.install_extension_archive(filename="ok.zip", content=safe)

        assert call_order[:2] == ["guard", "write"], (
            f"zip guard must run before temp write; got {call_order}"
        )
