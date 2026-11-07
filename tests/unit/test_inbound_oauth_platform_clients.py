"""Platform OAuth client presets and identifiers (ADR 0063 decision 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.auth import inbound_oauth_platform_clients as platform_clients
from core.application.auth.enforce import is_admin_only_route
from core.application.auth.inbound_oauth_clients import validate_client_id

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_new_client_id_is_opaque_and_recognised() -> None:
    preset = platform_clients.PLATFORM_PRESETS["microsoft_365"]
    first = platform_clients.new_platform_client_id(preset)
    second = platform_clients.new_platform_client_id(preset)
    assert first != second
    assert first.startswith("fs-platform-m365-")
    assert platform_clients.is_platform_client_id(first)


@pytest.mark.parametrize(
    "client_id",
    [
        "",
        "fs-platform-m365-",
        "fs-platform-m365-" + "0" * 31,
        "fs-platform-m365-" + "G" * 32,
        "fs-platform-M365-" + "0" * 32,
        " fs-platform-m365-" + "0" * 32,
        "fs-platform-m365-" + "0" * 32 + "\n",
        "https://claude.ai/oauth/claude-code-client-metadata",
    ],
)
def test_anything_else_is_not_a_platform_client_id(client_id: str) -> None:
    assert not platform_clients.is_platform_client_id(client_id)


def test_a_platform_client_id_is_never_a_cimd_client_id() -> None:
    # The two paths cannot overlap: a CIMD client id is an https URL.
    preset = platform_clients.PLATFORM_PRESETS["microsoft_365"]
    with pytest.raises(ValueError):
        validate_client_id(
            platform_clients.new_platform_client_id(preset),
            allowed_hosts=frozenset({"claude.ai", "chatgpt.com"}),
        )


def test_the_microsoft_preset_fixes_the_documented_redirect() -> None:
    preset = platform_clients.PLATFORM_PRESETS["microsoft_365"]
    assert preset.redirect_uris == ("https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",)
    assert preset.implies_resource is True
    for item in platform_clients.PLATFORM_PRESETS.values():
        assert all(uri.startswith("https://") for uri in item.redirect_uris)


def test_an_unknown_preset_is_refused_before_storage() -> None:
    with pytest.raises(platform_clients.PlatformClientError) as caught:
        platform_clients.register(
            None, "account", "anything", created_by="admin", now=datetime.now(UTC)
        )
    assert caught.value.code == "unknown_preset"
    assert caught.value.status == 400


def test_the_management_routes_are_admin_only() -> None:
    assert is_admin_only_route("/api/settings/mcp-oauth/platform-clients")
    assert is_admin_only_route("/api/settings/mcp-oauth/platform-clients/x/revoke")
    assert not is_admin_only_route("/api/settings/mcp-oauth/platform-clientsx")


@pytest.mark.parametrize("role", ["app", "compact"])
def test_the_runtimes_that_serve_oauth_ship_the_platform_modules(role: str) -> None:
    # The app (and Compact, which runs it) serves /oauth/* and the admin routes.
    manifest = json.loads(
        (REPO_ROOT / "infra/docker/source-slices" / f"{role}.json").read_text(encoding="utf-8")
    )
    paths = set(manifest["static_paths"])
    assert "core/application/auth/inbound_oauth_platform_clients.py" in paths
    assert "core/application/auth/inbound_oauth_platform_admin.py" in paths
