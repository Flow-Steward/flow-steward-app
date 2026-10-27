from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.extensions.extension_runtime import ExtensionReleaseRecord
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.query_service import ExtensionQueryService
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.management.errors import ServiceNotFoundError
from core.application.management.scope import ScopeContext
from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router


def _query_service() -> tuple[ExtensionQueryService, ExtensionInstallStore, ExtensionRuntimeStore]:
    installs = ExtensionInstallStore()
    runtime = ExtensionRuntimeStore()
    return (
        ExtensionQueryService(
            get_logger=lambda: None,
            get_install_store=lambda: installs,
            get_runtime_store=lambda: runtime,
            list_extension_projects=lambda *, account_id: [
                {"id": "project-current", "account_id": account_id}
            ],
        ),
        installs,
        runtime,
    )


def _install_fixture(
    installs: ExtensionInstallStore,
    runtime: ExtensionRuntimeStore,
) -> tuple[str, str]:
    install = installs.install(
        extension_id="neutral-picker",
        version="1.0.0",
        account_id="account-current",
        install_id="install-current",
        release_id="release-current",
    )
    release = runtime.create_release(
        ExtensionReleaseRecord(
            release_id="release-current",
            extension_id=install.extension_id,
            descriptor={
                "extension_id": install.extension_id,
                "ui_manifest": {"pages": []},
            },
        )
    )
    return install.install_id, release.descriptor_sha256


def test_browser_asset_manifest_is_bound_to_exact_active_project_install_release() -> None:
    service, installs, runtime = _query_service()
    install_id, descriptor_sha256 = _install_fixture(installs, runtime)
    scope = ScopeContext(
        account_id="account-current",
        project_id="project-current",
        allowed_project_ids=frozenset({"project-current"}),
    )

    manifest = service.get_browser_asset_manifest(
        "neutral-picker",
        install_id=install_id,
        release_id="release-current",
        descriptor_sha256=descriptor_sha256,
        project_id="project-current",
        scope=scope,
    )

    assert manifest["browser_asset_authority"] == {
        "extension_id": "neutral-picker",
        "install_id": "install-current",
        "release_id": "release-current",
        "descriptor_sha256": descriptor_sha256,
        "project_id": "project-current",
        "account_id": "account-current",
        "install_state": "active",
    }


@pytest.mark.parametrize(
    ("release_id", "descriptor_sha256", "project_id", "scope"),
    [
        (
            "release-stale",
            "a" * 64,
            "project-current",
            ScopeContext(account_id="account-current", project_id="project-current"),
        ),
        (
            "release-current",
            "b" * 64,
            "project-current",
            ScopeContext(account_id="account-current", project_id="project-current"),
        ),
        (
            "release-current",
            "a" * 64,
            "project-foreign",
            ScopeContext(
                account_id="account-current",
                project_id="project-foreign",
                allowed_project_ids=frozenset({"project-current"}),
            ),
        ),
    ],
)
def test_browser_asset_manifest_rejects_stale_or_foreign_authority(
    release_id: str,
    descriptor_sha256: str,
    project_id: str,
    scope: ScopeContext,
) -> None:
    service, installs, runtime = _query_service()
    install_id, actual_hash = _install_fixture(installs, runtime)
    if descriptor_sha256 == "a" * 64:
        descriptor_sha256 = actual_hash

    with pytest.raises(ServiceNotFoundError):
        service.get_browser_asset_manifest(
            "neutral-picker",
            install_id=install_id,
            release_id=release_id,
            descriptor_sha256=descriptor_sha256,
            project_id=project_id,
            scope=scope,
        )


@pytest.mark.parametrize("boundary", ["foreign_account", "disabled", "uninstalled"])
def test_browser_asset_manifest_rejects_non_active_or_non_owned_install(boundary: str) -> None:
    service, installs, runtime = _query_service()
    install_id, descriptor_sha256 = _install_fixture(installs, runtime)
    scope = ScopeContext(
        account_id="account-current",
        project_id="project-current",
        allowed_project_ids=frozenset({"project-current"}),
    )
    if boundary == "foreign_account":
        scope = scope.model_copy(update={"account_id": "account-foreign"})
    elif boundary == "disabled":
        installs.disable(extension_id="neutral-picker", account_id="account-current")
    else:
        installs.uninstall(extension_id="neutral-picker", account_id="account-current")

    with pytest.raises(ServiceNotFoundError):
        service.get_browser_asset_manifest(
            "neutral-picker",
            install_id=install_id,
            release_id="release-current",
            descriptor_sha256=descriptor_sha256,
            project_id="project-current",
            scope=scope,
        )


def _browser_asset_client(
    *,
    manifest: dict[str, object],
    expected: dict[str, str],
) -> tuple[TestClient, SimpleNamespace]:
    observed = SimpleNamespace(calls=[])

    def get_browser_asset_manifest(extension_id: str, **kwargs: object) -> dict[str, object]:
        observed.calls.append((extension_id, kwargs))
        if extension_id != expected["extension_id"] or any(
            kwargs.get(key) != value for key, value in expected.items() if key != "extension_id"
        ):
            raise ServiceNotFoundError("Extension browser asset not found")
        return manifest

    app = FastAPI()
    app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: SimpleNamespace(),
            event_command_service=lambda: SimpleNamespace(),
            job_query_service=lambda: SimpleNamespace(),
            extension_query_service=lambda: SimpleNamespace(
                get_browser_asset_manifest=get_browser_asset_manifest
            ),
            extension_binding_service=lambda: SimpleNamespace(),
            tool_execution_service=lambda: SimpleNamespace(),
            command_dispatch_service=lambda: SimpleNamespace(),
            request_scope=lambda _request, **kwargs: ScopeContext(
                account_id="account-current",
                project_id=kwargs.get("project_id"),
                allowed_project_ids=frozenset({"project-current"}),
            ),
            service_error_response=lambda exc, *_args: JSONResponse(
                {"error": str(exc)}, status_code=404
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
        )
    )
    return TestClient(app, raise_server_exceptions=False), observed


def test_browser_asset_endpoint_requires_immutable_authority(tmp_path: Path) -> None:
    asset = tmp_path / "assets" / "picker" / "script.js"
    asset.parent.mkdir(parents=True)
    asset.write_text("export {};", encoding="utf-8")
    expected = {
        "extension_id": "neutral-picker",
        "install_id": "install-current",
        "release_id": "release-current",
        "descriptor_sha256": "a" * 64,
        "project_id": "project-current",
    }
    manifest = {
        "bundle_root": str(tmp_path),
        "ui_manifest": {
            "pages": [
                {
                    "components": [
                        {
                            "type": "external_resource_picker",
                            "data": {"scripts": [{"asset_ref": "assets/picker/script.js"}]},
                        }
                    ]
                }
            ]
        },
        "browser_asset_authority": {
            **expected,
            "account_id": "account-current",
            "install_state": "active",
        },
    }
    client, observed = _browser_asset_client(manifest=manifest, expected=expected)

    response = client.get(
        "/api/extensions/neutral-picker/assets/assets/picker/script.js",
        params={
            "extension_install_id": "install-current",
            "release_id": "release-current",
            "descriptor_sha256": "a" * 64,
            "project_id": "project-current",
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "private, max-age=300"
    assert observed.calls == [
        (
            "neutral-picker",
            {
                "install_id": "install-current",
                "release_id": "release-current",
                "descriptor_sha256": "a" * 64,
                "project_id": "project-current",
                "scope": ScopeContext(
                    account_id="account-current",
                    project_id="project-current",
                    allowed_project_ids=frozenset({"project-current"}),
                ),
            },
        )
    ]


def test_browser_asset_endpoint_rejects_missing_or_stale_authority(tmp_path: Path) -> None:
    expected = {
        "extension_id": "neutral-picker",
        "install_id": "install-current",
        "release_id": "release-current",
        "descriptor_sha256": "a" * 64,
        "project_id": "project-current",
    }
    client, _observed = _browser_asset_client(manifest={}, expected=expected)
    path = "/api/extensions/neutral-picker/assets/assets/picker/script.js"

    missing = client.get(path, params={"extension_install_id": "install-current"})
    stale = client.get(
        path,
        params={
            "extension_install_id": "install-current",
            "release_id": "release-stale",
            "descriptor_sha256": "a" * 64,
            "project_id": "project-current",
        },
    )

    assert missing.status_code == 422
    assert stale.status_code == 404
