"""Runtime grants come from an extension's own manifest, limited to its own namespace.

There is no system / official / third-party tier and no host policy file naming
extensions. The single host rule, ``own_runtime_grants``, accepts a declaration
only over the declaring extension's own data: scope ``owning_extension`` and a
provider key equal to the extension's own id. Both directions are pinned here:
what an extension may declare for itself, and what it may never reach.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.extensions.runtime_host_grants import (
    RUNTIME_GRANT_FORBIDDEN,
    own_runtime_grants,
    trusted_mutation_grant,
    trusted_resource_grant,
)

_OWN_ID = "acme.notifier"
_FOREIGN_ID = "acme.other-notifier"


def _full_own_declaration() -> dict[str, Any]:
    return {
        "resources": {
            "project_connection": {},
            "provider_secrets": {"secret_keys": ["bot_token"]},
            "notification_deliveries": {},
            "notification_routes": {"provider_key": _OWN_ID},
            "callback_registrations": {"scope": "owning_extension"},
        },
        "mutations": {
            "project_connection_upsert": {},
            "project_connection_secrets_save": {
                "secret_keys": ["bot_token", "bot_token"],
                "connection_type_ids": ["bot"],
            },
            "notification_delivery_log": {},
            "notification_routes_save": {"provider_key": "ACME.Notifier"},
        },
    }


# ---------------------------------------------------------------------------
# own_runtime_grants: the accepting direction
# ---------------------------------------------------------------------------


def test_an_own_namespace_declaration_is_accepted_with_defaults_filled() -> None:
    accepted, problems = own_runtime_grants(_full_own_declaration(), _OWN_ID)

    assert problems == []
    assert accepted == {
        "resources": {
            "project_connection": {"scope": "owning_extension"},
            "provider_secrets": {"provider_key": _OWN_ID, "secret_keys": ["bot_token"]},
            "notification_deliveries": {"provider_key": _OWN_ID},
            "notification_routes": {"provider_key": _OWN_ID},
            "callback_registrations": {"scope": "owning_extension"},
        },
        "mutations": {
            "project_connection_upsert": {"scope": "owning_extension"},
            "project_connection_secrets_save": {
                "scope": "owning_extension",
                "provider_key": _OWN_ID,
                # Deduplicated, order kept.
                "secret_keys": ["bot_token"],
                "connection_type_ids": ["bot"],
            },
            "notification_delivery_log": {"provider_key": _OWN_ID},
            # Compared case-insensitively, stored as the canonical id.
            "notification_routes_save": {"provider_key": _OWN_ID},
        },
    }


@pytest.mark.parametrize("declared", [None, {}])
def test_declaring_nothing_grants_nothing_and_is_not_a_problem(declared: Any) -> None:
    assert own_runtime_grants(declared, _OWN_ID) == ({}, [])


# ---------------------------------------------------------------------------
# own_runtime_grants: the refusing direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "kind"),
    [
        ("resources", "provider_secrets"),
        ("resources", "notification_deliveries"),
        ("resources", "notification_routes"),
        ("mutations", "project_connection_secrets_save"),
        ("mutations", "notification_delivery_log"),
        ("mutations", "notification_routes_save"),
    ],
)
def test_a_foreign_provider_key_is_refused_and_dropped(section: str, kind: str) -> None:
    """The security boundary: no declaration may reach another extension's data."""
    accepted, problems = own_runtime_grants(
        {section: {kind: {"provider_key": _FOREIGN_ID}}}, _OWN_ID
    )

    assert accepted == {}
    assert [(p["code"], p["field_path"]) for p in problems] == [
        (RUNTIME_GRANT_FORBIDDEN, f"runtime_grants.{section}.{kind}")
    ]


@pytest.mark.parametrize("provider_key", ["", " ", 7, None, ["acme.notifier"]])
def test_a_malformed_provider_key_is_refused(provider_key: Any) -> None:
    accepted, problems = own_runtime_grants(
        {"resources": {"provider_secrets": {"provider_key": provider_key}}}, _OWN_ID
    )

    assert accepted == {}
    assert len(problems) == 1


@pytest.mark.parametrize("scope", ["global", "account", "any_extension", "", None, 1])
def test_a_non_owning_scope_is_refused_and_dropped(scope: Any) -> None:
    accepted, problems = own_runtime_grants(
        {"mutations": {"project_connection_upsert": {"scope": scope}}}, _OWN_ID
    )

    assert accepted == {}
    assert [(p["code"], p["field_path"]) for p in problems] == [
        (RUNTIME_GRANT_FORBIDDEN, "runtime_grants.mutations.project_connection_upsert")
    ]


@pytest.mark.parametrize(
    ("section", "kind"),
    [
        ("resources", "platform_secrets"),
        ("mutations", "provider_secrets"),
        ("resources", "project_connection_upsert"),
        ("mutations", "account_settings_save"),
    ],
)
def test_an_unknown_kind_is_refused_and_dropped(section: str, kind: str) -> None:
    accepted, problems = own_runtime_grants({section: {kind: {}}}, _OWN_ID)

    assert accepted == {}
    assert [(p["code"], p["field_path"]) for p in problems] == [
        (RUNTIME_GRANT_FORBIDDEN, f"runtime_grants.{section}.{kind}")
    ]


@pytest.mark.parametrize("field", ["secret_keys", "connection_type_ids"])
@pytest.mark.parametrize("value", ["bot_token", ["bot_token", ""], ["bot_token", 3], {}, None])
def test_a_malformed_list_is_refused(field: str, value: Any) -> None:
    accepted, problems = own_runtime_grants(
        {"mutations": {"project_connection_secrets_save": {field: value}}}, _OWN_ID
    )

    assert accepted == {}
    assert [(p["code"], p["field_path"]) for p in problems] == [
        (RUNTIME_GRANT_FORBIDDEN, "runtime_grants.mutations.project_connection_secrets_save")
    ]


def test_one_refused_grant_does_not_drop_the_accepted_ones() -> None:
    accepted, problems = own_runtime_grants(
        {
            "resources": {
                "provider_secrets": {"provider_key": _FOREIGN_ID},
                "project_connection": {},
            }
        },
        _OWN_ID,
    )

    assert accepted == {"resources": {"project_connection": {"scope": "owning_extension"}}}
    assert [p["field_path"] for p in problems] == ["runtime_grants.resources.provider_secrets"]


@pytest.mark.parametrize(
    "declared",
    [["resources"], "all", {"resources": ["provider_secrets"]}, {"privileges": {}}],
)
def test_a_malformed_declaration_is_refused(declared: Any) -> None:
    accepted, problems = own_runtime_grants(declared, _OWN_ID)

    assert accepted == {}
    assert problems
    assert all(p["code"] == RUNTIME_GRANT_FORBIDDEN for p in problems)


def test_a_grant_is_refused_when_the_extension_id_is_unknown() -> None:
    accepted, problems = own_runtime_grants({"resources": {"project_connection": {}}}, "")

    assert accepted == {}
    assert len(problems) == 1


# ---------------------------------------------------------------------------
# Runtime readers re-limit whatever a record carries
# ---------------------------------------------------------------------------


def test_a_forged_install_metadata_grant_over_another_namespace_is_never_returned() -> None:
    extension = {
        "extension_key": _OWN_ID,
        "install_metadata": {
            "runtime_grants": {
                "resources": {
                    "provider_secrets": {"provider_key": _FOREIGN_ID, "secret_keys": ["token"]}
                },
                "mutations": {"notification_routes_save": {"provider_key": _FOREIGN_ID}},
            }
        },
    }

    assert trusted_resource_grant(extension, "provider_secrets") is None
    assert trusted_mutation_grant(extension, "notification_routes_save") is None


def test_the_record_id_and_not_the_declared_key_decides_the_namespace() -> None:
    """A manifest naming another extension's id as its own provider key gets nothing."""
    extension = {
        "extension_key": _OWN_ID,
        "compiled_manifest": {
            "extension_id": _OWN_ID,
            "runtime_grants": {"resources": {"notification_routes": {"provider_key": "openai"}}},
        },
    }

    assert trusted_resource_grant(extension, "notification_routes") is None


def test_an_extension_without_declared_grants_has_none() -> None:
    extension = {"extension_key": _OWN_ID, "compiled_manifest": {"extension_id": _OWN_ID}}

    assert trusted_resource_grant(extension, "project_connection") is None
    assert trusted_mutation_grant(extension, "project_connection_upsert") is None


# ---------------------------------------------------------------------------
# Compile: the manifest field is validated where the bundle is validated
# ---------------------------------------------------------------------------


def _write_bundle(bundle_dir: Path, *, extension_id: str, runtime_grants: Any) -> Path:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 2,
        "runtime": {"contract_version": "extension_host_v2"},
        "lifecycle": {"data_schema_version": 1, "data_migrations": []},
        "extension_id": extension_id,
        "kind": "tool_provider",
        "version": "1.0.0",
        "display_name": "Acme Grants Demo",
        "description": "Test-only bundle declaring runtime grants.",
        "vendor": {"name": "Acme Test"},
        "features": ["tool"],
        "required_scopes": ["extension:invoke"],
        "runtime_grants": runtime_grants,
        "entrypoint": {"mode": "subprocess", "command": [sys.executable, "main.py"]},
        "health": {"command": [sys.executable, "health.py"], "timeout_seconds": 5},
        "ui_manifest": "ui/ui_manifest.yaml",
    }
    (bundle_dir / "extension.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (bundle_dir / "ui").mkdir(exist_ok=True)
    (bundle_dir / "ui" / "ui_manifest.yaml").write_text(
        "version: '1'\npages: []\n", encoding="utf-8"
    )
    (bundle_dir / "main.py").write_text(
        "import json\nprint(json.dumps({'ok': True}))\n", encoding="utf-8"
    )
    (bundle_dir / "health.py").write_text("print('ok')\n", encoding="utf-8")
    return bundle_dir


def _runtime_grant_errors(compiled: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        issue
        for issue in list(compiled.get("compile_errors") or [])
        + list(compiled.get("validation_errors") or [])
        if issue.get("code") == RUNTIME_GRANT_FORBIDDEN
    ]


def test_a_bundle_declaring_its_own_grants_compiles_and_exposes_them(tmp_path: Path) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import (
        compile_extension_bundle,
        extension_host_metadata,
    )

    bundle = _write_bundle(
        tmp_path / "acme_notifier",
        extension_id=_OWN_ID,
        runtime_grants={
            "resources": {"provider_secrets": {"secret_keys": ["bot_token"]}},
            "mutations": {
                "project_connection_upsert": {},
                "project_connection_secrets_save": {"secret_keys": ["bot_token"]},
            },
        },
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "valid", compiled.get("scan_error")
    assert _runtime_grant_errors(compiled) == []
    compiled.update(extension_host_metadata(compiled))
    assert trusted_resource_grant(compiled, "provider_secrets") == {
        "provider_key": _OWN_ID,
        "secret_keys": ["bot_token"],
    }
    assert trusted_mutation_grant(compiled, "project_connection_upsert") == {
        "scope": "owning_extension"
    }
    assert trusted_mutation_grant(compiled, "project_connection_secrets_save") == {
        "scope": "owning_extension",
        "provider_key": _OWN_ID,
        "secret_keys": ["bot_token"],
    }
    # Nothing it did not declare.
    assert trusted_resource_grant(compiled, "notification_routes") is None


@pytest.mark.parametrize(
    "runtime_grants",
    [
        {"resources": {"provider_secrets": {"provider_key": _FOREIGN_ID}}},
        {"mutations": {"notification_routes_save": {"provider_key": _FOREIGN_ID}}},
        {"mutations": {"project_connection_upsert": {"scope": "global"}}},
        {"resources": {"platform_secrets": {}}},
    ],
)
def test_a_bundle_asking_for_someone_elses_data_fails_validation(
    tmp_path: Path, runtime_grants: dict[str, Any]
) -> None:
    from core.infrastructure.extension_runtime.extension_runtime import (
        compile_extension_bundle,
        extension_host_metadata,
    )

    bundle = _write_bundle(
        tmp_path / "acme_notifier", extension_id=_OWN_ID, runtime_grants=runtime_grants
    )

    compiled = compile_extension_bundle(bundle)

    assert compiled["status"] == "invalid"
    errors = _runtime_grant_errors(compiled)
    assert errors
    assert all(issue["severity"] == "error" for issue in errors)
    assert all(issue["field_path"].startswith("runtime_grants.") for issue in errors)
    # And even if such a record were used, no reader returns the refused grant.
    compiled.update(extension_host_metadata(compiled))
    for section, rows in runtime_grants.items():
        reader = trusted_resource_grant if section == "resources" else trusted_mutation_grant
        for kind in rows:
            assert reader(compiled, kind) is None, (section, kind)


def test_runtime_grants_is_a_documented_authoring_field() -> None:
    from core.infrastructure.extension_runtime.extension_runtime_common import (
        AUTHORING_MANIFEST_ALLOWED_FIELDS,
        COMPILED_MANIFEST_ALLOWED_FIELDS,
    )

    standard = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "docs/standards/extension-manifest-standard.yaml"
        ).read_text(encoding="utf-8")
    )

    assert "runtime_grants" in AUTHORING_MANIFEST_ALLOWED_FIELDS
    assert "runtime_grants" in COMPILED_MANIFEST_ALLOWED_FIELDS
    assert "runtime_grants" in standard
    contract = standard["x-validator-contract"]["runtime_grants"]
    assert set(contract["resource_kinds"]) == {
        "project_connection",
        "provider_secrets",
        "notification_deliveries",
        "notification_routes",
        "callback_registrations",
    }
    assert set(contract["mutation_kinds"]) == {
        "project_connection_upsert",
        "project_connection_secrets_save",
        "notification_delivery_log",
        "notification_routes_save",
    }
    accepted, problems = own_runtime_grants(standard["runtime_grants"], standard["extension_id"])
    assert problems == []
    assert accepted
