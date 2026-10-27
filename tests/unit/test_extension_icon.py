"""The extension logo: icon.png in the bundle, the catalog icon, and their routes.

One rule set (``core/infrastructure/static_catalog/icons.py``) governs every
catalog icon and every bundle's ``icon.png``: PNG, square, 32-512 px, at most
100 KB. These tests hold the bundle gate, the catalog index contract, the
verified catalog fetch, the installed logo route and the projections to it.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.application.extensions import extension_catalog as catalog
from core.application.extensions.install import ExtensionInstallStore
from core.application.extensions.query_service import ExtensionQueryService
from core.application.extensions.runtime_store import ExtensionRuntimeStore
from core.application.management.errors import ServiceNotFoundError, ServiceValidationError
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime.bundle_icon import extension_icon_issues
from core.infrastructure.static_catalog import settings as catalog_settings
from core.infrastructure.static_catalog.http_fetch import CatalogUrlGuard, FetchedCatalogIndex
from core.infrastructure.static_catalog.icons import (
    icon_problem,
    parse_catalog_icon,
    png_size,
)
from tests.fixtures.catalogs.synthetic_catalogs import build_extension_index
from tests.support.extension_icon import EXTENSION_ICON_PNG, png_bytes, write_extension_icon

EXTENSION_ID = "acme.logo-demo"
ACCOUNT = "account-current"


# ---------------------------------------------------------------------------
# The shared rule set
# ---------------------------------------------------------------------------


def test_the_first_party_and_default_icons_follow_the_rules() -> None:
    root = Path(__file__).resolve().parents[2] / "core" / "application" / "extensions" / "icons"
    for name in ("default-icon.png", "flow-steward-icon.png"):
        data = (root / name).read_bytes()
        assert icon_problem(data) == "", name
        assert png_size(data) == (128, 128)
    assert (root / "flow-steward-icon.png").read_bytes() == EXTENSION_ICON_PNG


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"", "is empty"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "is not a PNG file"),
        (png_bytes(64, 32), "must be square, 32-512 px on a side (is 64x32)"),
        (png_bytes(31, 31), "(is 31x31)"),
        (png_bytes(513, 513), "(is 513x513)"),
        (png_bytes(64, 64, padding=100 * 1024), "the limit is 102400 bytes"),
    ],
)
def test_icon_problem_names_what_is_wrong(data: bytes, problem: str) -> None:
    assert problem in icon_problem(data)


@pytest.mark.parametrize("side", [32, 128, 512])
def test_icon_problem_accepts_square_pngs_in_range(side: int) -> None:
    assert icon_problem(png_bytes(side, side)) == ""


# ---------------------------------------------------------------------------
# The bundle gate
# ---------------------------------------------------------------------------


def test_bundle_icon_issues_are_field_level(tmp_path: Path) -> None:
    [missing] = extension_icon_issues(tmp_path)
    assert missing["code"] == "extension_icon_missing"
    assert missing["field_path"] == "icon.png"
    assert missing["file_path"] == str(tmp_path / "icon.png")
    assert "replace that file" in missing["hint"]

    (tmp_path / "icon.png").write_bytes(png_bytes(128, 64))
    [invalid] = extension_icon_issues(tmp_path)
    assert invalid["code"] == "extension_icon_invalid"
    assert invalid["message"] == "icon.png must be square, 32-512 px on a side (is 128x64)"

    write_extension_icon(tmp_path)
    assert extension_icon_issues(tmp_path) == []


def test_a_linked_icon_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.png"
    target.write_bytes(EXTENSION_ICON_PNG)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "icon.png").symlink_to(target)

    [issue] = extension_icon_issues(bundle)

    assert issue["code"] == "extension_icon_invalid"
    assert "not a link" in issue["message"]


def test_the_archive_installer_refuses_a_bundle_without_its_icon(tmp_path: Path) -> None:
    from core.application.extensions.archive_validation_service import (
        ExtensionArchiveValidationService,
    )

    with pytest.raises(ServiceValidationError) as refused:
        ExtensionArchiveValidationService._validate_icon(tmp_path)

    assert "icon.png is missing" in str(refused.value)
    payload = getattr(refused.value, "payload", {}) or {}
    assert [issue["field_path"] for issue in payload["validation_errors"]] == ["icon.png"]

    write_extension_icon(tmp_path)
    ExtensionArchiveValidationService._validate_icon(tmp_path)


# ---------------------------------------------------------------------------
# The catalog index contract and the verified fetch
# ---------------------------------------------------------------------------


def test_the_catalog_icon_object_has_the_mcp_shape() -> None:
    raw = {
        "url": "https://raw.githubusercontent.com/Flow-Steward/extensions/abc/acme/icon.png",
        "sha256": "A" * 64,
        "bytes": 2048,
        "mime_type": "image/png",
        "size": "128x128",
    }

    icon = parse_catalog_icon(raw, "items[0].icon")

    assert (icon.url, icon.sha256, icon.bytes, icon.size) == (raw["url"], "a" * 64, 2048, "128x128")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda item: item.pop("icon"), "items[0].icon is required"),
        (lambda item: item["icon"].update(url="http://catalog.example.test/i.png"), "icon.url"),
        (lambda item: item["icon"].update(sha256="nope"), "icon.sha256"),
        (lambda item: item["icon"].update(bytes=0), "icon.bytes must be 1..102400"),
        (lambda item: item["icon"].update(bytes=100 * 1024 + 1), "icon.bytes must be"),
        (lambda item: item["icon"].update(mime_type="image/svg+xml"), "icon.mime_type"),
        (lambda item: item["icon"].update(size="128x64"), "icon.size"),
        (lambda item: item["icon"].update(size="16x16"), "icon.size"),
    ],
)
def test_the_catalog_index_requires_a_valid_icon(change: Any, message: str) -> None:
    payload = build_extension_index(1)
    change(payload["items"][0])

    with pytest.raises(ValueError, match=re.escape(message)):
        catalog.parse_extension_catalog_index(payload, runtime_target="linux-amd64")


def test_a_catalog_item_points_the_browser_at_core_not_the_catalog_host() -> None:
    payload = build_extension_index(1)
    item = catalog.parse_extension_catalog_index(payload, runtime_target="linux-amd64").items[0]

    projected = item.as_dict()

    assert projected["has_icon"] is True
    assert projected["icon_url"] == (
        f"/api/extensions/catalog/{item.extension_id}/icon?v={item.icon.sha256[:16]}"
    )
    assert "icon" not in projected


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def served_catalog(monkeypatch: pytest.MonkeyPatch):
    """The catalog with one entry whose icon host serves whatever the test sets."""
    monkeypatch.setitem(
        catalog_settings.CONFIG, "extension_catalog_url", "https://catalog.example.test/index.json"
    )
    catalog.reset_extension_catalog_cache()
    payload = build_extension_index(1)
    body = {"value": EXTENSION_ICON_PNG}
    entry = payload["items"][0]
    entry["icon"].update(
        sha256=hashlib.sha256(EXTENSION_ICON_PNG).hexdigest(), bytes=len(EXTENSION_ICON_PNG)
    )
    monkeypatch.setattr(
        catalog,
        "fetch_catalog_index_json",
        lambda url, **kwargs: FetchedCatalogIndex(payload=payload, etag=""),
    )
    requests: list[str] = []

    def open_pinned_url(request: Any, **_kwargs: Any) -> _Response:
        requests.append(request.full_url)
        return _Response(body["value"])

    monkeypatch.setattr(
        catalog,
        "_URL_GUARD",
        CatalogUrlGuard(
            assert_safe_remote_https_url=lambda url, **_kwargs: url,
            open_pinned_url=open_pinned_url,
        ),
    )
    yield SimpleNamespace(
        extension_id=entry["extension_id"], entry=entry, body=body, requests=requests
    )
    catalog.reset_extension_catalog_cache()


def test_the_catalog_icon_is_fetched_once_and_verified(served_catalog: Any) -> None:
    assert catalog.extension_catalog_icon(served_catalog.extension_id) == EXTENSION_ICON_PNG
    assert catalog.extension_catalog_icon(served_catalog.extension_id) == EXTENSION_ICON_PNG
    assert len(served_catalog.requests) == 1


def test_a_catalog_icon_whose_digest_differs_is_refused(served_catalog: Any) -> None:
    tampered = bytearray(EXTENSION_ICON_PNG)
    tampered[-20] ^= 0xFF
    served_catalog.body["value"] = bytes(tampered)

    with pytest.raises(catalog.ExtensionCatalogError) as refused:
        catalog.extension_catalog_icon(served_catalog.extension_id)

    assert refused.value.code == "extension_catalog_icon_unavailable"
    assert refused.value.status_code == 503


def test_a_catalog_icon_that_is_not_a_png_is_refused(served_catalog: Any) -> None:
    svg = b"<svg onload=alert(1)>"
    served_catalog.body["value"] = svg
    served_catalog.entry["icon"].update(sha256=hashlib.sha256(svg).hexdigest(), bytes=len(svg))

    with pytest.raises(catalog.ExtensionCatalogError, match="not a PNG"):
        catalog.extension_catalog_icon(served_catalog.extension_id)


def test_an_unlisted_extension_has_no_catalog_icon(served_catalog: Any) -> None:
    with pytest.raises(catalog.ExtensionCatalogError) as missing:
        catalog.extension_catalog_icon("acme.not-listed")
    assert missing.value.status_code == 404


def test_the_catalog_icon_route_serves_a_locked_down_png(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.entrypoints.api import extension_catalog as routes

    monkeypatch.setattr(routes, "extension_catalog_icon", lambda extension_id: EXTENSION_ICON_PNG)
    app = FastAPI()
    app.include_router(
        routes.create_extension_catalog_router(
            get_extension_install_store=lambda: None,
            extension_binding_service=lambda: None,
            request_scope=lambda request: None,
            service_error_response=lambda exc, *_args: JSONResponse({}, status_code=400),
            api_error_response=lambda exc, **_kwargs: JSONResponse({}, status_code=500),
        )
    )

    response = TestClient(app).get(f"/api/extensions/catalog/{EXTENSION_ID}/icon")

    assert response.status_code == 200
    assert response.content == EXTENSION_ICON_PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["cache-control"] == "private, max-age=86400"


# ---------------------------------------------------------------------------
# The installed extension's logo
# ---------------------------------------------------------------------------


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One extension installed for ACCOUNT, its bundle in a temporary extensions root."""
    from core.application.extensions.identity import extension_folder_name

    monkeypatch.setattr(
        "core.infrastructure.extension_runtime.extension_runtime.default_extensions_root",
        lambda: str(tmp_path),
    )
    bundle = tmp_path / extension_folder_name(EXTENSION_ID)
    bundle.mkdir()
    write_extension_icon(bundle)
    installs = ExtensionInstallStore()
    runtime = ExtensionRuntimeStore()
    installs.install(
        extension_id=EXTENSION_ID,
        version="1.2.0",
        account_id=ACCOUNT,
        install_id="install-current",
        release_id="release-current",
    )
    service = ExtensionQueryService(
        get_logger=lambda: None,
        get_install_store=lambda: installs,
        get_runtime_store=lambda: runtime,
        list_extension_projects=lambda *, account_id: [],
    )
    return SimpleNamespace(service=service, bundle=bundle)


def test_projections_carry_the_installed_logo_url(installed: Any) -> None:
    scope = ScopeContext(account_id=ACCOUNT)

    [row] = installed.service.list_extensions(scope=scope).extensions
    detail = installed.service.get_extension(EXTENSION_ID, scope=scope)
    safe = installed.service.get_extension_safe(EXTENSION_ID, scope=scope)

    assert row.has_icon is True
    assert row.icon_url.startswith(f"/api/extensions/{EXTENSION_ID}/icon?v=")
    assert (detail.has_icon, detail.icon_url) == (True, row.icon_url)
    assert (safe.has_icon, safe.icon_url) == (True, row.icon_url)
    assert installed.service.get_extension_icon(EXTENSION_ID, scope=scope) == EXTENSION_ICON_PNG


def test_a_bundle_without_a_valid_logo_projects_none(installed: Any) -> None:
    scope = ScopeContext(account_id=ACCOUNT)
    (installed.bundle / "icon.png").write_bytes(b"<svg/>")

    with pytest.raises(ServiceNotFoundError, match="Extension icon not found"):
        installed.service.get_extension_icon(EXTENSION_ID, scope=scope)

    (installed.bundle / "icon.png").unlink()
    [row] = installed.service.list_extensions(scope=scope).extensions
    assert (row.has_icon, row.icon_url) == (False, "")


def test_another_account_cannot_read_the_logo(installed: Any) -> None:
    foreign_scope = ScopeContext(account_id="account-foreign")
    with pytest.raises(ServiceNotFoundError):
        installed.service.get_extension_icon(EXTENSION_ID, scope=foreign_scope)


def _icon_client(service: Any, account_id: str) -> TestClient:
    from core.entrypoints.api.reactions_extensions import create_reactions_extensions_router

    app = FastAPI()
    app.include_router(
        create_reactions_extensions_router(
            event_query_service=lambda: SimpleNamespace(),
            event_command_service=lambda: SimpleNamespace(),
            job_query_service=lambda: SimpleNamespace(),
            extension_query_service=lambda: service,
            extension_binding_service=lambda: SimpleNamespace(),
            tool_execution_service=lambda: SimpleNamespace(),
            command_dispatch_service=lambda: SimpleNamespace(),
            request_scope=lambda _request, **_kwargs: ScopeContext(account_id=account_id),
            service_error_response=lambda exc, *_args: JSONResponse(
                {"error": str(exc)}, status_code=404
            ),
            api_error_response=lambda exc, *_args, **_kwargs: JSONResponse(
                {"error": str(exc)}, status_code=500
            ),
        )
    )
    return TestClient(app, raise_server_exceptions=False)


def test_the_installed_logo_route_serves_the_bundle_icon(installed: Any) -> None:
    response = _icon_client(installed.service, ACCOUNT).get(f"/api/extensions/{EXTENSION_ID}/icon")

    assert response.status_code == 200
    assert response.content == EXTENSION_ICON_PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["cache-control"] == "private, max-age=86400"


def test_the_installed_logo_route_hides_another_accounts_extension(installed: Any) -> None:
    response = _icon_client(installed.service, "account-foreign").get(
        f"/api/extensions/{EXTENSION_ID}/icon"
    )

    assert response.status_code == 404
    assert response.content != EXTENSION_ICON_PNG


# ---------------------------------------------------------------------------
# Which extension a workflow step comes from (the canvas shows its logo)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (
            {"step_kind": "connector", "override": {"connector": {"extension_id": "acme.crm"}}},
            "acme.crm",
        ),
        ({"step_kind": "connector", "connector_id": "extension.acme.crm"}, "acme.crm"),
        ({"step_kind": "connector", "connector_id": "extension.pi_abc.send"}, ""),
        (
            {
                "step_kind": "connector",
                "connector_id": "extension.0f8c1e2a-1111-4222-8333-944455556666.send",
            },
            "",
        ),
        ({"step_kind": "connector", "connector_id": "connector.registry_tool"}, ""),
        ({"step_kind": "connector", "override": {"connector": {"extension_id": "Not An Id"}}}, ""),
        ({"step_kind": "agent", "override": {"connector": {"extension_id": "acme.crm"}}}, ""),
    ],
)
def test_step_extension_id_names_only_a_canonical_extension(
    step: dict[str, Any], expected: str
) -> None:
    from core.application.workflows.extension_connection_validation import step_extension_id

    assert step_extension_id(step) == expected
