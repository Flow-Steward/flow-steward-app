"""An extension's locales/*.yaml dictionaries: loading, keys and where they reach the user."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2
from core.application.extensions.extension_locales import (
    load_locale_bundles,
    resolve_locale_text,
    ui_manifest_locale_keys,
)
from core.application.extensions.workflow_install_contract import extension_action_operations

OAUTH_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/extensions/fixture_extension_oauth"


def _bundle(tmp_path: Path, files: dict[str, str]) -> Path:
    locales = tmp_path / "locales"
    locales.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (locales / name).write_text(text, encoding="utf-8")
    return tmp_path


def _codes(issues: list[dict[str, str]]) -> list[str]:
    return [issue["code"] for issue in issues]


# --- loading -----------------------------------------------------------------------


def test_flat_yaml_dictionaries_load_per_language(tmp_path: Path) -> None:
    root = _bundle(
        tmp_path,
        {"en.yaml": "page.setup.title: Setup\n", "lt.yaml": "page.setup.title: Nustatymai\n"},
    )

    bundles, errors, warnings = load_locale_bundles(root)

    assert bundles == {
        "en": {"page.setup.title": "Setup"},
        "lt": {"page.setup.title": "Nustatymai"},
    }
    assert errors == warnings == []


def test_a_json_dictionary_is_never_read(tmp_path: Path) -> None:
    root = _bundle(tmp_path, {"en.json": '{"page.setup.title": "Setup"}'})

    assert load_locale_bundles(root) == ({}, [], [])


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("page.setup.title: [unclosed\n", "invalid_locale_file"),
        ("- just\n- a list\n", "invalid_locale_file"),
        ("page:\n  setup: Nested\n", "invalid_locale_value"),
        ("page.count: 3\n", "invalid_locale_value"),
        ("flag: true\n", "invalid_locale_value"),
    ],
)
def test_a_broken_dictionary_is_an_error_with_its_file(
    tmp_path: Path, text: str, code: str
) -> None:
    root = _bundle(tmp_path, {"en.yaml": "ok: fine\n", "lt.yaml": text})

    bundles, errors, _ = load_locale_bundles(root)

    assert code in _codes(errors)
    assert errors[0]["path"].startswith("locales/lt.yaml")
    assert "lt" not in bundles  # a broken file is reported, not loaded as "no translations"


def test_an_empty_value_is_an_empty_translation(tmp_path: Path) -> None:
    root = _bundle(tmp_path, {"en.yaml": "a: A\nb: ''\nc:\n"})

    bundles, errors, _ = load_locale_bundles(root)

    assert errors == []
    assert bundles["en"] == {"a": "A", "b": "", "c": ""}


def test_language_files_need_a_language_code_and_one_file_each(tmp_path: Path) -> None:
    root = _bundle(tmp_path, {"en.yaml": "a: A\n", "en.yml": "a: B\n", "Not A Code.yaml": "a: C\n"})

    _, errors, _ = load_locale_bundles(root)

    assert sorted(_codes(errors)) == ["duplicate_locale_file", "invalid_locale_code"]


def test_dictionaries_without_english_keep_the_existing_warning(tmp_path: Path) -> None:
    root = _bundle(tmp_path, {"lt.yaml": "a: A\n"})

    _, errors, warnings = load_locale_bundles(root)

    assert errors == []
    assert _codes(warnings) == ["locale_missing_default"]


def test_no_locales_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_locale_bundles(tmp_path) == ({}, [], [])
    assert load_locale_bundles(None) == ({}, [], [])


# --- resolution --------------------------------------------------------------------


def test_language_then_english_then_declaration() -> None:
    bundles = {
        "lt": {"a": "lt-a", "b": "", "w": "  "},
        "en": {"a": "en-a", "b": "en-b", "w": "en-w"},
    }

    assert resolve_locale_text(bundles, "lt", "a", "src") == "lt-a"
    assert resolve_locale_text(bundles, "lt", "b", "src") == "en-b"  # empty falls back
    assert resolve_locale_text(bundles, "lt", "w", "src") == "en-w"  # blank falls back
    assert resolve_locale_text(bundles, "lt", "missing", "src") == "src"
    assert resolve_locale_text(bundles, "de", "a", "src") == "en-a"  # language the extension lacks
    assert resolve_locale_text(None, "lt", "a", "src") == "src"


# --- keys the pages use ------------------------------------------------------------


def test_page_component_and_field_keys_follow_declared_ids() -> None:
    ui_manifest = {
        "pages": [
            {
                "page_id": "setup",
                "title": "Setup",
                "description": "Start here",
                "components": [
                    {
                        "component_id": "form",
                        "type": "form",
                        "title": "Client",
                        "ui_schema": {
                            "sections": [{"section_id": "main", "title": "Main"}],
                            "fields": [
                                {
                                    "field_id": "client",
                                    "label": "Client",
                                    "help": "Who",
                                    "placeholder": "ACME",
                                },
                                {"label": "Legacy"},
                                {
                                    "field_id": "kind",
                                    "label": "Kind",
                                    "options": [{"value": "a", "label": "A"}],
                                },
                                {
                                    "field_id": "rows",
                                    "widget": "repeater",
                                    "label": "Rows",
                                    "props": {"fields": [{"name": "name", "label": "Name"}]},
                                },
                                {
                                    "field_id": "other_rows",
                                    "widget": "repeater",
                                    "label": "Other",
                                    "props": {"fields": [{"name": "name", "label": "Name"}]},
                                },
                            ],
                            "actions": {"save_label": "Save", "sync": {"label": "Sync"}},
                        },
                    },
                    {
                        "component_id": "list",
                        "type": "grid",
                        "title": "Items",
                        "columns": [{"key": "total", "label": "Total"}],
                        "row_actions": [{"action_id": "retry", "label": "Retry"}],
                        "empty_state": {"title": "Nothing", "description": "Yet"},
                    },
                ],
            }
        ]
    }

    keys = ui_manifest_locale_keys(ui_manifest)

    assert {
        "page.setup.title",
        "page.setup.description",
        "component.form.title",
        "component.form.section.main.title",
        "component.form.field.client.label",
        "component.form.field.client.help",
        "component.form.field.client.placeholder",
        "component.form.field.Legacy.label",
        "component.form.field.kind.option.a.label",
        "component.form.field.rows.field.name.label",
        "component.form.field.other_rows.field.name.label",
        "component.form.save_label",
        "component.form.action.sync.label",
        "component.list.title",
        "component.list.column.total.label",
        "component.list.row_action.retry.label",
        "component.list.empty_state",
        "component.list.empty_state_description",
    } <= keys


# --- validation --------------------------------------------------------------------


def test_an_extension_with_pages_and_no_contract_still_loads_its_dictionaries(
    tmp_path: Path,
) -> None:
    root = _bundle(
        tmp_path,
        {"en.yaml": "page.setup.title: Setup\n", "lt.yaml": "page.setup.title: Nustatymai\n"},
    )
    ui_manifest = {"pages": [{"page_id": "setup", "title": "Setup"}]}

    result = validate_extension_contract_v2(
        {"extension_id": "acme.pages"}, bundle_root=root, ui_manifest=ui_manifest
    )

    assert result["present"] is False
    assert result["valid"] is True
    assert result["locale_codes"] == ["en", "lt"]
    assert result["normalized_contract"] == {}
    assert "locale_orphan_key" not in _codes(result["warnings"])


def test_a_broken_dictionary_makes_the_extension_invalid(tmp_path: Path) -> None:
    root = _bundle(tmp_path, {"en.yaml": "page:\n  nested: no\n"})

    result = validate_extension_contract_v2({"extension_id": "acme.pages"}, bundle_root=root)

    assert result["valid"] is False
    assert "invalid_locale_value" in _codes(result["errors"])


def test_compile_rejects_a_json_dictionary_and_keeps_yaml_ones(tmp_path: Path) -> None:
    from core.infrastructure.extension_runtime.extension_runtime_compile import (
        compile_extension_bundle,
    )

    bundle = tmp_path / "fixture_extension_oauth"
    shutil.copytree(OAUTH_FIXTURE, bundle)
    clean = compile_extension_bundle(str(bundle))
    assert set(clean["compiled_manifest"]["locale_codes"]) >= {"en"}
    assert "extension.yaml_only_violation" not in [
        issue.get("code")
        for issue in clean.get("compile_errors", []) + clean.get("validation_errors", [])
    ]

    (bundle / "locales" / "lt.json").write_text('{"ext.display_name": "x"}', encoding="utf-8")
    rejected = compile_extension_bundle(str(bundle))
    codes = [
        issue.get("code")
        for issue in rejected.get("compile_errors", []) + rejected.get("validation_errors", [])
    ]
    assert "extension.yaml_only_violation" in codes
    assert "lt" not in rejected["compiled_manifest"].get("locale_codes", [])


# --- workflow ----------------------------------------------------------------------


def _operation_contract() -> tuple[list[dict], list[dict]]:
    actions = [{"action_id": "deliver", "workflow_visible": True, "title": "Deliver"}]
    manifest = [
        {
            "operation_id": "deliver",
            "display_name": "Deliver notification",
            "description": "Send it",
            "inputs": [{"name": "event", "display_name": "Event", "value_type": "object"}],
            "outputs": [
                {"name": "delivered", "display_name": "Delivered", "value_type": "boolean"}
            ],
        }
    ]
    return actions, manifest


def test_workflow_operations_read_their_release_dictionaries() -> None:
    actions, manifest = _operation_contract()
    bundles = {
        "en": {"op.deliver.display_name": "Deliver notification"},
        "lt": {
            "op.deliver.display_name": "Pristatyti pranešimą",
            "op.deliver.input.event.display_name": "Įvykis",
            "op.deliver.output.delivered.display_name": "Pristatyta",
        },
    }

    [english] = extension_action_operations(
        "acme.notify", actions, manifest, locale="en", locale_bundles=bundles
    )
    [lithuanian] = extension_action_operations(
        "acme.notify", actions, manifest, locale="lt", locale_bundles=bundles
    )

    assert english["display_name"] == "Deliver notification"
    assert lithuanian["display_name"] == "Pristatyti pranešimą"
    assert lithuanian["description"] == "Send it"
    assert lithuanian["parameters"][0]["display_name"] == "Įvykis"
    assert lithuanian["returns"][0]["display_name"] == "Pristatyta"
    # Names, ids and types are the contract's own in every language.
    for key in ("operation_id", "action_id", "extension_id", "connection_type_ids"):
        assert lithuanian[key] == english[key]
    assert [row["name"] for row in lithuanian["parameters"]] == ["event"]
    assert [row["name"] for row in lithuanian["returns"]] == ["delivered"]


def test_install_contract_route_answers_in_the_requested_language() -> None:
    from core.entrypoints.api import workflow_extension_contract as contract_api

    actions, manifest = _operation_contract()
    contract = {
        "account_id": contract_api.DEFAULT_ACCOUNT_ID,
        "install_id": "install-1",
        "extension_id": "acme.notify",
        "install_state": "active",
        "release_id": "release-1",
        "descriptor_sha256": "b" * 64,
        "action_definitions": actions,
        "operation_manifest": manifest,
        "locale_bundles": {"lt": {"op.deliver.display_name": "Pristatyti pranešimą"}},
    }
    app = FastAPI()
    app.include_router(contract_api.router)
    client = TestClient(app, raise_server_exceptions=False)

    with (
        patch.object(contract_api, "_resolve_extension_contract", return_value=contract),
        patch(
            "core.application.settings.ui_i18n._supported_locale_codes", return_value={"en", "lt"}
        ),
    ):
        lithuanian = client.get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": "install-1", "locale": "lt"},
        )
        english = client.get(
            "/api/workflows/meta/extension-install-contract",
            params={"install_id": "install-1", "locale": "en"},
        )

    assert lithuanian.status_code == 200
    assert lithuanian.json()["operations"][0]["display_name"] == "Pristatyti pranešimą"
    assert english.json()["operations"][0]["display_name"] == "Deliver notification"
    # Translation does not change which release the contract is bound to.
    for response in (lithuanian, english):
        assert response.json()["release_id"] == "release-1"
        assert response.json()["descriptor_sha256"] == "b" * 64


def test_install_contract_carries_the_releases_own_dictionaries() -> None:
    from core.application.extensions import workflow_install_contract as module

    install = SimpleNamespace(
        install_id="install-1", release_id="release-2", extension_id="acme.notify", state="active"
    )
    release = SimpleNamespace(
        extension_id="acme.notify",
        descriptor_sha256="C" * 64,
        descriptor={"locale_bundles": {"lt": {"ext.display_name": "v2"}}, "action_definitions": []},
    )
    with (
        patch(
            "core.application.extensions.install.DBExtensionInstallStore",
            return_value=SimpleNamespace(list_for_account=lambda *_a, **_k: [install]),
        ),
        patch(
            "core.application.extensions.runtime_store_db.DBExtensionRuntimeStore",
            return_value=SimpleNamespace(get_release=lambda release_id: release),
        ),
    ):
        contract = module.resolve_workflow_extension_install_contract(
            None, install_id="install-1", account_id="acc", default_account_id="acc"
        )

    assert contract is not None
    assert contract["release_id"] == "release-2"
    assert contract["locale_bundles"] == {"lt": {"ext.display_name": "v2"}}


def test_installed_list_names_an_extension_from_its_release() -> None:
    from core.application.extensions.query_list import _localized_summary
    from core.application.management.query_models_registry import ExtensionSummaryView

    row = ExtensionSummaryView.model_construct(
        extension_id="acme.notify", display_name="Notify", description="Sends"
    )
    release = SimpleNamespace(
        descriptor={"locale_bundles": {"lt": {"ext.display_name": "Pranešimai"}}}
    )

    localized = _localized_summary(row, release, locale="lt")

    assert localized.display_name == "Pranešimai"
    assert localized.description == "Sends"
    assert _localized_summary(row, None, locale="lt") is row


@pytest.mark.parametrize(
    ("locale", "bundles", "expected"),
    [
        ("en", {}, "Invoice Showcase"),
        (
            "lt",
            {
                "lt": {"ext.display_name": "Sąskaitų pavyzdžiai"},
                "en": {"ext.display_name": "Invoice Showcase EN"},
            },
            "Sąskaitų pavyzdžiai",
        ),
        (
            "lt",
            {"lt": {"ext.display_name": "  "}, "en": {"ext.display_name": "Invoice Showcase EN"}},
            "Invoice Showcase EN",
        ),
    ],
)
def test_action_source_matches_installed_list_for_nested_release_identity(
    locale: str, bundles: dict, expected: str
) -> None:
    from core.application.extensions.query_catalog import _descriptor_catalog_fields
    from core.application.extensions.query_list import _localized_summary
    from core.application.management.query_models_registry import ExtensionSummaryView
    from core.entrypoints.admin import workflows_connector_catalog as catalog

    extension_id = "acme.invoice_showcase"
    install = SimpleNamespace(
        install_id="11111111-1111-4111-8111-111111111111",
        release_id="release-2",
        extension_id=extension_id,
        account_id="account-1",
        state="active",
    )
    actions, operations = _operation_contract()
    descriptor = {
        "identity": {"display_name": "Invoice Showcase"},
        "locale_bundles": bundles,
        "action_definitions": actions,
        "operation_manifest": operations,
    }
    release = SimpleNamespace(descriptor=descriptor, extension_id=extension_id)
    listed = _localized_summary(
        ExtensionSummaryView.model_construct(
            extension_id=extension_id, **_descriptor_catalog_fields(release)
        ),
        release,
        locale=locale,
    )
    scope = SimpleNamespace(account_id="account-1", project_id="project-1")
    runtime = SimpleNamespace(
        get_release=lambda release_id: release if release_id == "release-2" else None
    )
    with (
        patch.object(
            catalog,
            "_platform_logger_for_admin_catalog",
            return_value=SimpleNamespace(conn=object()),
        ),
        patch.object(
            catalog,
            "_active_extension_install_index_for_catalog",
            return_value={extension_id: install},
        ),
        patch.object(catalog, "DBExtensionRuntimeStore", return_value=runtime),
        patch.object(catalog, "_list_project_connections_for_catalog", return_value=[]),
    ):
        [source] = catalog._extension_action_connector_rows(
            object(), project_id="project-1", scope=scope, locale=locale
        )

    assert listed.display_name == expected
    assert source["display_name"] == expected
    assert source["connector_id"] == "extension.acme.invoice_showcase"
    assert source["extension_id"] == extension_id
    assert source["connector_install_id"] == install.install_id
    assert source["operations"][0]["operation_id"] == "deliver"
    assert source["operations"][0]["display_name"] == "Deliver notification"
