from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.application.extensions.archive_lifecycle_capability import (
    ExtensionArchiveLifecycleCapability,
)
from core.application.extensions.binding_service import ExtensionBindingService
from core.application.management.errors import (
    ServicePreconditionError,
    ServiceUnavailableError,
)


def test_read_only_managed_root_is_reported_without_exposing_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=os.ST_RDONLY))
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    view = capability.availability()

    assert view.available is False
    assert view.reason_code == "managed_root_read_only"
    assert str(tmp_path) not in view.message
    assert view.remediation["install_update"] == [
        "./flow-steward extensions install --archive <path>"
    ]
    assert view.remediation["uninstall"] == ["./flow-steward extensions uninstall <extension-id>"]


def test_effective_write_probe_detects_read_only_bind_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        tempfile,
        "mkstemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError(errno.EROFS, "read-only", str(tmp_path))),
    )
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    view = capability.availability()

    assert view.available is False
    assert view.reason_code == "managed_root_read_only"
    assert str(tmp_path) not in view.message


def test_writable_managed_root_keeps_archive_lifecycle_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    assert capability.availability().available is True
    capability.require_available(operation="install_update")


def test_unavailable_precondition_uses_stable_structured_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=os.ST_RDONLY))
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    with pytest.raises(ServicePreconditionError) as captured:
        capability.require_available(operation="install_update")

    assert captured.value.status_code == 412
    assert captured.value.payload == {
        "code": "extension_archive_lifecycle_unavailable",
        "message": (
            "Extension archive installation and updates are unavailable in this deployment."
        ),
        "details": {"remediation": ["./flow-steward extensions install --archive <path>"]},
    }


def test_only_managed_root_permission_errors_are_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "statvfs", lambda _path: SimpleNamespace(f_flag=0))
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    managed_error = OSError(errno.EROFS, "read-only", str(tmp_path / ".staged"))
    with pytest.raises(ServicePreconditionError):
        capability.translate_managed_root_error(managed_error, operation="install_update")

    unrelated = OSError(errno.EACCES, "denied", str(tmp_path.parent / "elsewhere"))
    with pytest.raises(OSError) as captured:
        capability.translate_managed_root_error(unrelated, operation="install_update")
    assert captured.value is unrelated

    arbitrary = OSError(errno.EIO, "io", str(tmp_path / ".staged"))
    with pytest.raises(OSError) as captured:
        capability.translate_managed_root_error(arbitrary, operation="install_update")
    assert captured.value is arbitrary


def test_a_denied_write_under_delegation_names_which_half_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R101: the guard opened, the write was denied, and the answer blamed the deployment.

    On Compact the availability guard passes because the installer owns the write, so a
    permission error reaching *this* process means the request was never proxied. That is
    an internal routing fault. The answer used to be the same 412 "unavailable in this
    deployment" pointing at a CLI a Compact launcher refuses -- a closed loop that costs an
    investigator an hour precisely because it names nothing.
    """
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    monkeypatch.delenv("FS_EXTENSION_INSTALLER_PROXY_ENABLED", raising=False)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)
    denied = OSError(errno.EACCES, "denied", str(tmp_path / ".staged"))

    with pytest.raises(ServiceUnavailableError) as captured:
        capability.translate_managed_root_error(denied, operation="install_update")

    message = str(captured.value)
    assert "FS_EXTENSION_INSTALLER_PROXY_ENABLED" in message
    assert "never forwarded" in message
    # it must not send an operator to a command this profile refuses, and must not
    # leak the host path the old precondition was careful to hide either
    assert "flow-steward extensions install" not in message
    assert str(tmp_path) not in message

    # the other half of the delegation: forwarded, but nobody answered
    monkeypatch.setenv("FS_EXTENSION_INSTALLER_PROXY_ENABLED", "1")
    monkeypatch.setenv("FS_EXTENSION_INSTALLER_ORIGIN", "http://127.0.0.1:8101")
    with pytest.raises(ServiceUnavailableError) as captured:
        capability.translate_managed_root_error(denied, operation="install_update")
    assert "http://127.0.0.1:8101" in str(captured.value)


def test_scalable_still_reports_a_denied_write_as_a_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the app really is the writer, the old answer is the right one."""
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)
    denied = OSError(errno.EACCES, "denied", str(tmp_path / ".staged"))

    with pytest.raises(ServicePreconditionError) as captured:
        capability.translate_managed_root_error(denied, operation="install_update")
    assert captured.value.payload["code"] == "extension_archive_lifecycle_unavailable"


def test_binding_facade_repeats_preflight_before_archive_install_entrypoints() -> None:
    capability = cast(
        ExtensionArchiveLifecycleCapability,
        SimpleNamespace(
            availability=lambda: SimpleNamespace(available=False),
            require_available=lambda **_kwargs: (_ for _ in ()).throw(
                ServicePreconditionError("blocked")
            ),
        ),
    )

    def handler_mode(
        extension: dict[str, Any] | None,
        *,
        operation_type: str,
        operation_id: str,
    ) -> str:
        del extension, operation_type, operation_id
        return "extension"

    service = ExtensionBindingService(
        get_logger=lambda: None,
        get_operation_store=lambda: None,
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_identifier=lambda _extension: "",
        extension_operation_handler_mode=handler_mode,
        extension_operation_failure_status=lambda _extension: 500,
        archive_lifecycle_capability=capability,
    )
    install = SimpleNamespace(
        install_extension_archive=lambda **_kwargs: pytest.fail("mutated"),
        install_extension_archive_path=lambda **_kwargs: pytest.fail("mutated"),
    )
    service._archive_install_service = install

    with pytest.raises(ServicePreconditionError):
        service.install_extension_archive_path(archive_path=Path("extension.zip"))
    with pytest.raises(ServicePreconditionError):
        service.install_extension_archive(filename="extension.zip", content=b"PK")


def test_compact_delegates_the_question_to_the_installer_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC17-R59: the owner could not install an extension by any route.

    On Compact the API runs as uid 10001 and `/data/extensions` is owned by the extension
    installer, uid 10005, mode 0750 — and the API proxies archive lifecycle to that installer
    rather than writing itself. Probing "can *this* process write?" therefore answered no by
    design, the UI offered `./flow-steward extensions install --archive`, which a Compact
    launcher refuses, and `require_available` closed the API path too.
    """
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "compact")
    # an unwritable root, exactly the situation on that profile
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    view = capability.availability()

    assert view.available is True
    assert view.reason_code == "delegated_to_installer_process"
    # and it must stop advertising a command that profile rejects
    assert view.remediation["install_update"] == []
    assert view.remediation["uninstall"] == []
    # the API gate opens with it, which is what closed the non-UI route
    capability.require_available(operation="install_update")
    capability.require_available(operation="uninstall")


def test_scalable_still_refuses_when_this_process_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary stays fail-closed where the app really is the writer."""
    monkeypatch.setenv("FS_DEPLOYMENT_PROFILE", "scalable")
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    view = capability.availability()

    assert view.available is False
    assert view.reason_code == "managed_root_not_writable"
    with pytest.raises(ServicePreconditionError):
        capability.require_available(operation="install_update")


def test_an_unset_profile_is_treated_as_scalable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absence must not be read as delegation, or every deployment would report available."""
    monkeypatch.delenv("FS_DEPLOYMENT_PROFILE", raising=False)
    monkeypatch.setattr(os, "access", lambda *_args, **_kwargs: False)
    capability = ExtensionArchiveLifecycleCapability(root_provider=lambda: tmp_path)

    assert capability.availability().available is False
