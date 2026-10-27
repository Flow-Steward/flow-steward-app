from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.extensions.extension_runtime import ExtensionReleaseRecord
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.management.errors import ServiceNotFoundError


def _manifest(root: Path) -> dict:
    return {
        "bundle_root": str(root),
        "step_ui_manifest": [
            {
                "fields": [
                    {
                        "external_resource_picker": {
                            "scripts": [{"asset_ref": "assets/picker-bridge.js"}]
                        }
                    }
                ]
            }
        ],
    }


def test_resolve_declared_browser_asset_returns_only_declared_bundle_file(tmp_path: Path) -> None:
    from core.application.extensions.browser_assets import resolve_declared_browser_asset

    asset = tmp_path / "assets" / "picker-bridge.js"
    asset.parent.mkdir()
    asset.write_text("globalThis.FixturePicker = {};", encoding="utf-8")

    assert (
        resolve_declared_browser_asset(_manifest(tmp_path), asset_ref="assets/picker-bridge.js")
        == asset.resolve()
    )


@pytest.mark.parametrize(
    "asset_ref",
    [
        "assets/not-declared.js",
        "../picker-bridge.js",
        "assets/picker-bridge.css",
        "/assets/picker-bridge.js",
    ],
)
def test_resolve_declared_browser_asset_rejects_undeclared_or_unsafe_paths(
    tmp_path: Path, asset_ref: str
) -> None:
    from core.application.extensions.browser_assets import resolve_declared_browser_asset

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "picker-bridge.js").write_text("safe", encoding="utf-8")

    _raises_input_61_1 = _manifest(tmp_path)
    with pytest.raises(ServiceNotFoundError):
        resolve_declared_browser_asset(_raises_input_61_1, asset_ref=asset_ref)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extension_id", "foreign.extension"),
        ("install_id", "install-foreign"),
        ("release_id", "release-stale"),
        ("descriptor_sha256", "b" * 64),
        ("project_id", "project-foreign"),
        ("install_state", "disabled"),
    ],
)
def test_resolve_authorized_browser_asset_fails_closed_for_stale_or_foreign_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    from core.application.extensions.browser_assets import resolve_authorized_browser_asset

    asset = tmp_path / "assets" / "picker-bridge.js"
    asset.parent.mkdir()
    asset.write_text("globalThis.FixturePicker = {};", encoding="utf-8")
    authority = {
        "extension_id": "neutral-picker",
        "install_id": "install-current",
        "release_id": "release-current",
        "descriptor_sha256": "a" * 64,
        "project_id": "project-current",
        "account_id": "account-current",
        "install_state": "active",
    }
    authority[field] = value
    manifest = {
        **_manifest(tmp_path),
        "browser_asset_authority": authority,
    }
    scope = SimpleNamespace(
        account_id="account-current",
        project_id="project-current",
        allowed_project_ids=frozenset({"project-current"}),
    )

    with pytest.raises(ServiceNotFoundError):
        resolve_authorized_browser_asset(
            manifest,
            asset_ref="assets/picker-bridge.js",
            extension_id="neutral-picker",
            install_id="install-current",
            release_id="release-current",
            descriptor_sha256="a" * 64,
            project_id="project-current",
            scope=scope,
        )


def test_resolve_authorized_browser_asset_returns_only_exact_active_identity(
    tmp_path: Path,
) -> None:
    from core.application.extensions.browser_assets import resolve_authorized_browser_asset

    asset = tmp_path / "assets" / "picker-bridge.js"
    asset.parent.mkdir()
    asset.write_text("globalThis.FixturePicker = {};", encoding="utf-8")
    manifest = {
        **_manifest(tmp_path),
        "browser_asset_authority": {
            "extension_id": "neutral-picker",
            "install_id": "install-current",
            "release_id": "release-current",
            "descriptor_sha256": "a" * 64,
            "project_id": "project-current",
            "account_id": "account-current",
            "install_state": "active",
        },
    }
    scope = SimpleNamespace(
        account_id="account-current",
        project_id="project-current",
        allowed_project_ids=frozenset({"project-current"}),
    )

    assert (
        resolve_authorized_browser_asset(
            manifest,
            asset_ref="assets/picker-bridge.js",
            extension_id="neutral-picker",
            install_id="install-current",
            release_id="release-current",
            descriptor_sha256="a" * 64,
            project_id="project-current",
            scope=scope,
        )
        == asset.resolve()
    )


def test_browser_permissions_fail_closed_for_malformed_picker_declarations() -> None:
    from core.application.extensions.browser_assets import browser_permissions

    assert browser_permissions(
        iter(
            [
                {
                    "ui_manifest": {
                        "pages": [
                            {
                                "components": [
                                    {
                                        "external_resource_picker": {
                                            "allowed_script_origins": [
                                                "https://picker.fixture.test",
                                                "https://idp.fixture.test",
                                                "http://invalid.fixture.test",
                                                "https://picker.fixture.test/path",
                                            ],
                                            "allowed_connect_origins": ["https://api.fixture.test"],
                                            "allowed_frame_origins": ["https://frame.fixture.test"],
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        )
    ) == {
        "script-src": (),
        "connect-src": (),
        "frame-src": (),
    }


def test_browser_permission_projection_uses_only_active_installed_release_manifests() -> None:
    from core.infrastructure.extension_runtime.browser_permissions import (
        ExtensionBrowserPermissionProjection,
    )

    installs = ExtensionInstallStore()
    runtime = ExtensionRuntimeStore()
    active = installs.install(
        extension_id="neutral-picker",
        version="1.0.0",
        account_id="account-a",
        release_id="release-active",
    )
    disabled = installs.install(
        extension_id="disabled-picker",
        version="1.0.0",
        account_id="account-a",
        release_id="release-disabled",
    )
    installs.disable(extension_id=disabled.extension_id, account_id=disabled.account_id)
    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-active",
            extension_id=active.extension_id,
            descriptor={
                "extension_id": active.extension_id,
                "ui_manifest": {
                    "pages": [
                        {
                            "external_resource_picker": {
                                "allowed_script_origins": ["https://picker.fixture.test"],
                                "allowed_connect_origins": ["https://api.fixture.test"],
                                "allowed_frame_origins": ["https://frame.fixture.test"],
                            }
                        }
                    ]
                },
            },
        )
    )
    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-disabled",
            extension_id=disabled.extension_id,
            descriptor={
                "extension_id": disabled.extension_id,
                "ui_manifest": {
                    "pages": [
                        {
                            "external_resource_picker": {
                                "allowed_script_origins": ["https://disabled.fixture.test"]
                            }
                        }
                    ]
                },
            },
        )
    )
    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-orphaned",
            extension_id="orphaned-picker",
            descriptor={
                "extension_id": "orphaned-picker",
                "ui_manifest": {
                    "external_resource_picker": {
                        "allowed_script_origins": ["https://orphaned.fixture.test"]
                    }
                },
            },
        )
    )

    projection = ExtensionBrowserPermissionProjection()

    assert projection.refresh(installs=installs, runtime=runtime) == {
        "script-src": ("https://picker.fixture.test",),
        "connect-src": ("https://api.fixture.test",),
        "frame-src": ("https://frame.fixture.test",),
    }


def test_browser_permission_projection_invalidates_removed_or_changed_active_release() -> None:
    from core.infrastructure.extension_runtime.browser_permissions import (
        ExtensionBrowserPermissionProjection,
    )

    installs = ExtensionInstallStore()
    runtime = ExtensionRuntimeStore()
    install = installs.install(
        extension_id="neutral-picker",
        version="1.0.0",
        account_id="account-a",
        release_id="release-one",
    )
    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-one",
            extension_id=install.extension_id,
            descriptor={
                "extension_id": install.extension_id,
                "ui_manifest": {
                    "external_resource_picker": {
                        "allowed_connect_origins": ["https://one.fixture.test"]
                    }
                },
            },
        )
    )
    projection = ExtensionBrowserPermissionProjection()
    projection.refresh(installs=installs, runtime=runtime)

    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-two",
            extension_id=install.extension_id,
            descriptor={
                "extension_id": install.extension_id,
                "ui_manifest": {
                    "external_resource_picker": {
                        "allowed_connect_origins": ["https://two.fixture.test"]
                    }
                },
            },
        )
    )
    installs.install(
        extension_id=install.extension_id,
        version="2.0.0",
        account_id=install.account_id,
        install_id=install.install_id,
        release_id="release-two",
    )

    assert projection.refresh(installs=installs, runtime=runtime) == {
        "script-src": (),
        "connect-src": ("https://two.fixture.test",),
        "frame-src": (),
    }

    installs.uninstall(extension_id=install.extension_id, account_id=install.account_id)
    assert projection.refresh(installs=installs, runtime=runtime) == {
        "script-src": (),
        "connect-src": (),
        "frame-src": (),
    }


def test_browser_permission_lifecycle_refresh_replaces_global_cache() -> None:
    from core.infrastructure.extension_runtime.browser_permissions import (
        extension_browser_permission_snapshot,
        refresh_browser_permissions_for_lifecycle,
    )

    installs = ExtensionInstallStore()
    runtime = ExtensionRuntimeStore()
    install = installs.install(
        extension_id="neutral-picker",
        version="1.0.0",
        account_id="account-a",
        release_id="release-one",
    )
    runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-one",
            extension_id=install.extension_id,
            descriptor={
                "extension_id": install.extension_id,
                "ui_manifest": {
                    "external_resource_picker": {
                        "allowed_frame_origins": ["https://frame.fixture.test"]
                    }
                },
            },
        )
    )
    context = SimpleNamespace(
        install_store=lambda _bl: installs,
        runtime_store=lambda _bl: runtime,
    )

    refresh_browser_permissions_for_lifecycle(bl=object(), lifecycle_context=context)
    assert extension_browser_permission_snapshot()["frame-src"] == ("https://frame.fixture.test",)

    installs.uninstall(extension_id=install.extension_id, account_id=install.account_id)
    refresh_browser_permissions_for_lifecycle(bl=object(), lifecycle_context=context)

    assert extension_browser_permission_snapshot() == {
        "script-src": (),
        "connect-src": (),
        "frame-src": (),
    }
