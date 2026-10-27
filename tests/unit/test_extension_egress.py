"""The optional ``egress`` declaration: where an extension says it connects.

Declared and shown to the administrator; nothing enforces it in this release.
A missing declaration installs with a visible warning, never an error.
"""

from __future__ import annotations

import pytest

from core.infrastructure.extension_runtime.extension_egress import validate_egress

CONNECTION_TYPES = [
    {
        "connection_type_id": "custom_smtp",
        "config_schema": {"properties": {"host": {}, "port": {}}},
    },
    {"connection_type_id": "api_account", "config_schema": {"properties": {"base_url": {}}}},
]


def _codes(issues: list[dict]) -> list[str]:
    return [issue["code"] for issue in issues]


def test_a_missing_declaration_is_a_warning_not_an_error() -> None:
    declared, errors, warnings = validate_egress({}, CONNECTION_TYPES)

    assert declared is None
    assert errors == []
    assert _codes(warnings) == ["egress_undeclared"]
    assert "does not declare where it connects" in warnings[0]["message"]


def test_an_empty_declaration_says_the_extension_connects_nowhere() -> None:
    assert validate_egress({"egress": {}}, CONNECTION_TYPES) == (
        {"hosts": [], "connection_fields": []},
        [],
        [],
    )


def test_hosts_and_connection_fields_are_declared() -> None:
    declared, errors, warnings = validate_egress(
        {
            "egress": {
                "hosts": ["API.Telegram.org", "*.googleapis.com", "203.0.113.7"],
                "connection_fields": ["custom_smtp.host", "base_url"],
            }
        },
        CONNECTION_TYPES,
    )

    assert errors == warnings == []
    assert declared == {
        "hosts": ["api.telegram.org", "*.googleapis.com", "203.0.113.7"],
        "connection_fields": ["custom_smtp.host", "base_url"],
    }


@pytest.mark.parametrize(
    "host",
    [
        "https://api.example.com",
        "api.example.com:443",
        "api.example.com/v1",
        "*",
        "-bad.example",
        "",
    ],
)
def test_a_host_must_be_a_bare_host_name(host: str) -> None:
    _declared, errors, _warnings = validate_egress({"egress": {"hosts": [host]}}, CONNECTION_TYPES)

    assert _codes(errors) == ["egress_host_invalid"]
    assert errors[0]["field_path"] == "egress.hosts[0]"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("custom_smtp.nope", "Connection type 'custom_smtp' has no config field 'nope'"),
        ("other_type.host", "declares no connection type 'other_type'"),
        ("password_hint", "No connection type of the extension has a config field 'password_hint'"),
    ],
)
def test_a_connection_field_must_exist_in_the_extension_connections(
    field: str, message: str
) -> None:
    _declared, errors, _warnings = validate_egress(
        {"egress": {"connection_fields": [field]}}, CONNECTION_TYPES
    )

    assert _codes(errors) == ["egress_connection_field_unknown"]
    assert message in errors[0]["message"]


def test_unknown_keys_and_wrong_shapes_are_refused() -> None:
    _declared, errors, _warnings = validate_egress(
        {"egress": {"hosts": "api.example.com", "ports": [443]}}, CONNECTION_TYPES
    )

    assert sorted(_codes(errors)) == ["egress_invalid", "egress_invalid"]
    assert _codes(validate_egress({"egress": ["x"]}, CONNECTION_TYPES)[1]) == ["egress_invalid"]


def test_the_compiled_manifest_carries_the_declaration_and_the_warning(tmp_path) -> None:
    import yaml

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle
    from tests.unit.test_extension_runtime import _write_minimal_extension

    silent = tmp_path / "extensions" / "acme_silent"
    _write_minimal_extension(silent, extension_id="acme.silent")
    declared = tmp_path / "extensions" / "acme_declared"
    _write_minimal_extension(declared, extension_id="acme.declared")
    manifest = yaml.safe_load((declared / "extension.yaml").read_text())
    manifest["egress"] = {"hosts": ["api.example.com"]}
    (declared / "extension.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    silent_compiled = compile_extension_bundle(silent)
    declared_compiled = compile_extension_bundle(declared)

    assert silent_compiled["status"] == "valid"
    assert "egress_undeclared" in _codes(silent_compiled["validation_warnings"])
    assert silent_compiled["compiled_manifest"]["egress"] is None
    assert "egress_undeclared" not in _codes(declared_compiled["validation_warnings"])
    assert declared_compiled["compiled_manifest"]["egress"] == {
        "hosts": ["api.example.com"],
        "connection_fields": [],
    }
