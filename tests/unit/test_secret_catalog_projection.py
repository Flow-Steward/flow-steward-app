"""What changes a connection's approved fingerprint, and what does not (ADR 0057)."""

from __future__ import annotations

import copy

import pytest

from core.application.secrets.projection import (
    connection_projection,
    normalized_origin,
    projection_fingerprint,
)

_RELEASE = {"release_id": "rel-1", "descriptor_sha256": "abc"}
_BASE = {
    "connection_id": "c1",
    "project_id": "p1",
    "connection_kind": "extension",
    "extension_id": "demo",
    "extension_install_id": "i1",
    "connection_type": "api",
    "runtime_mode": "remote_https",
    "remote_base_url": "https://api.example.test/v1",
    "connection_config": {
        "site_url": "https://shop.example.test",
        "oauth": {"lifecycle_status": "connected", "expires_at": 1.0, "audience": "x"},
    },
    "connection_routes": [],
    "ingress_config": {},
    "workflow_mappings": {},
    "display_name": "Shop",
}


def _fingerprint(connection, *, bindings=None, release=None) -> str:
    return projection_fingerprint(
        connection_projection(
            connection, bindings=bindings or {"api_token": "s1"}, release=release or _RELEASE
        )
    )


def _changed(path: tuple[str, ...], value) -> dict:
    connection = copy.deepcopy(_BASE)
    target = connection
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return connection


@pytest.mark.parametrize(
    "path",
    [
        ("connection_config", "oauth", "lifecycle_status"),
        ("connection_config", "oauth", "expires_at"),
        ("connection_config", "oauth", "connected_at"),
        ("connection_config", "oauth", "granted_scopes"),
        ("display_name",),
    ],
)
def test_oauth_refresh_and_labels_keep_the_fingerprint(path) -> None:
    assert _fingerprint(_changed(path, "refreshed")) == _fingerprint(_BASE)


@pytest.mark.parametrize(
    "path",
    [
        ("remote_base_url",),
        ("runtime_mode",),
        ("connection_config", "site_url"),
        ("connection_config", "oauth", "audience"),
        ("connection_routes",),
        ("workflow_mappings",),
    ],
)
def test_destinations_and_executable_settings_change_it(path) -> None:
    assert _fingerprint(_changed(path, "https://elsewhere.test")) != _fingerprint(_BASE)


def test_a_new_release_or_binding_changes_it() -> None:
    assert _fingerprint(_BASE, release={"release_id": "rel-2", "descriptor_sha256": "abc"}) != (
        _fingerprint(_BASE)
    )
    assert _fingerprint(_BASE, release={"release_id": "rel-1", "descriptor_sha256": "def"}) != (
        _fingerprint(_BASE)
    )
    assert _fingerprint(_BASE, bindings={"api_token": "s2"}) != _fingerprint(_BASE)


def test_the_projection_never_holds_a_value_and_records_the_destination() -> None:
    projection = connection_projection(_BASE, bindings={"api_token": "s1"}, release=_RELEASE)
    assert projection["destinations"] == [
        {"kind": "exact_host", "scheme": "https", "host": "api.example.test", "port": 443}
    ]
    assert "secret_refs" not in projection


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://API.Example.test:443/x", ("https", "api.example.test", 443)),
        ("http://example.test", ("http", "example.test", 80)),
        ("https://example.test:8443", ("https", "example.test", 8443)),
        ("ftp://example.test", None),
        ("not a url", None),
        ("https://", None),
    ],
)
def test_origins_are_normalized(url, expected) -> None:
    origin = normalized_origin(url)
    assert (None if origin is None else (origin["scheme"], origin["host"], origin["port"])) == (
        expected
    )
