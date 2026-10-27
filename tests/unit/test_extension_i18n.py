"""Unit tests for extension i18n — locale bundles, resolution, fallback, validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.application.extensions.extension_contract_v2 import (
    derive_expected_locale_keys,
    validate_extension_contract_v2,
)
from core.application.extensions.extension_projection_v2 import (
    build_builder_catalog,
    project_contract_to_registry,
    render_step_form,
    resolve_locale_string,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _locale_bundles() -> dict[str, dict[str, str]]:
    return {
        "en": {
            "ext.display_name": "Sample Tasks Integration",
            "ext.description": "Sync tasks via Sample Tasks API",
            "category.task_ops.display_name": "Task operations",
            "op.get_tasks.display_name": "Get Tasks",
            "op.get_tasks.description": "Retrieve tasks from Sample Tasks",
            "trigger.on_task_created.display_name": "When task is created",
            "step.get_tasks.section": "General",
            "step.get_tasks.field.list_id.label": "List ID",
            "step.get_tasks.field.list_id.help_text": "The Sample Tasks list ID",
            "query.list_picker.display_name": "Available Lists",
        },
        "ru": {
            "ext.display_name": "Интеграция Sample Tasks",
            "category.task_ops.display_name": "Операции задач",
            "op.get_tasks.display_name": "Получить задачи",
            "trigger.on_task_created.display_name": "При создании задачи",
            "step.get_tasks.section": "Основное",
            "step.get_tasks.field.list_id.label": "ID списка",
        },
    }


def _contract() -> dict[str, list[dict[str, Any]]]:
    return {
        "connection_types": [
            {"connection_type_id": "sample_tasks_api", "display_name": "Sample Tasks API"}
        ],
        "operation_manifest": [
            {
                "operation_id": "get_tasks",
                "operation_kind": "action",
                "category": "task_ops",
                "display_name": "Get Tasks",
                "inputs": [{"name": "list_id", "value_type": "text"}],
                "outputs": [{"name": "tasks", "value_type": "json"}],
            },
        ],
        "step_ui_manifest": [
            {
                "operation_id": "get_tasks",
                "section": "General",
                "fields": [
                    {
                        "name": "list_id",
                        "widget_type": "text",
                        "label": "List ID",
                        "help_text": "The Sample Tasks list ID",
                    },
                ],
            },
        ],
        "resource_query_manifest": [{"query_id": "list_picker", "cursor_mode": "none"}],
        "event_bindings": [
            {
                "trigger_id": "on_task_created",
                "event_name": "notification.requested",
                "display_name": "When task is created",
            },
        ],
        "artifact_policies": [],
    }


# ---------------------------------------------------------------------------
# 1. resolve_locale_string() fallback chain
# ---------------------------------------------------------------------------


class TestResolveLocaleString:
    def test_returns_translated_value_for_requested_locale(self) -> None:
        bundles = _locale_bundles()
        assert (
            resolve_locale_string(
                bundles, "ru", "category.task_ops.display_name", "Task operations"
            )
            == "Операции задач"
        )

    def test_falls_back_to_english_when_key_missing_in_locale(self) -> None:
        bundles = _locale_bundles()
        assert (
            resolve_locale_string(bundles, "ru", "op.get_tasks.description", "raw")
            == "Retrieve tasks from Sample Tasks"
        )

    def test_falls_back_to_raw_when_missing_in_both(self) -> None:
        bundles = _locale_bundles()
        assert (
            resolve_locale_string(bundles, "ru", "category.unknown.display_name", "raw fallback")
            == "raw fallback"
        )

    def test_returns_fallback_when_bundles_none(self) -> None:
        assert resolve_locale_string(None, "ru", "category.task_ops.display_name", "raw") == "raw"

    def test_returns_fallback_when_key_empty(self) -> None:
        bundles = _locale_bundles()
        assert resolve_locale_string(bundles, "ru", "", "raw") == "raw"

    def test_returns_fallback_when_bundles_empty(self) -> None:
        assert resolve_locale_string({}, "ru", "category.task_ops.display_name", "raw") == "raw"

    def test_returns_english_when_locale_empty(self) -> None:
        bundles = _locale_bundles()
        assert (
            resolve_locale_string(bundles, "", "category.task_ops.display_name", "raw")
            == "Task operations"
        )

    def test_returns_english_when_locale_not_in_bundles(self) -> None:
        bundles = _locale_bundles()
        assert (
            resolve_locale_string(bundles, "de", "category.task_ops.display_name", "raw")
            == "Task operations"
        )


# ---------------------------------------------------------------------------
# 2. project_contract_to_registry() with locale
# ---------------------------------------------------------------------------


class TestProjectContractWithLocale:
    def test_operation_display_name_resolved_from_ru(self) -> None:
        projected = project_contract_to_registry(
            _contract(),
            account_id="acc",
            install_id="pi_1",
            release_id="pr_1",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        op_block = next(b for b in projected["blocks"] if b["block_type"] == "operation")
        assert op_block["display_name"] == "Получить задачи"

    def test_trigger_display_name_resolved_from_ru(self) -> None:
        projected = project_contract_to_registry(
            _contract(),
            account_id="acc",
            install_id="pi_1",
            release_id="pr_1",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        trigger_block = next(b for b in projected["blocks"] if b["block_type"] == "trigger")
        assert trigger_block["display_name"] == "При создании задачи"

    def test_removed_family_skill_rows_are_not_projected(self) -> None:
        projected = project_contract_to_registry(
            _contract(),
            account_id="acc",
            install_id="pi_1",
            release_id="pr_1",
        )
        assert projected["families"] == []
        assert projected["skills"] == []


# ---------------------------------------------------------------------------
# 3. render_step_form() with locale
# ---------------------------------------------------------------------------


class TestRenderStepFormWithLocale:
    def test_field_label_resolved(self) -> None:
        rendered = render_step_form(
            contract=_contract(),
            operation_id="get_tasks",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        field = rendered["sections"][0]["fields"][0]
        assert field["label"] == "ID списка"

    def test_section_title_resolved(self) -> None:
        rendered = render_step_form(
            contract=_contract(),
            operation_id="get_tasks",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        assert rendered["sections"][0]["section"] == "Основное"

    def test_help_text_falls_back_to_en(self) -> None:
        rendered = render_step_form(
            contract=_contract(),
            operation_id="get_tasks",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        field = rendered["sections"][0]["fields"][0]
        assert field["help_text"] == "The Sample Tasks list ID"

    def test_no_locale_uses_raw(self) -> None:
        rendered = render_step_form(
            contract=_contract(),
            operation_id="get_tasks",
        )
        field = rendered["sections"][0]["fields"][0]
        assert field["label"] == "List ID"

    def test_placeholder_resolved(self) -> None:
        bundles = _locale_bundles()
        bundles["en"]["step.get_tasks.field.list_id.placeholder"] = "901234567890"
        bundles["ru"]["step.get_tasks.field.list_id.placeholder"] = "Введите ID"
        contract = _contract()
        contract["step_ui_manifest"][0]["fields"][0]["placeholder"] = "901234567890"
        rendered = render_step_form(
            contract=contract,
            operation_id="get_tasks",
            locale="ru",
            locale_bundles=bundles,
        )
        field = rendered["sections"][0]["fields"][0]
        assert field["placeholder"] == "Введите ID"

    def test_placeholder_included_without_locale(self) -> None:
        contract = _contract()
        contract["step_ui_manifest"][0]["fields"][0]["placeholder"] = "type here"
        rendered = render_step_form(contract=contract, operation_id="get_tasks")
        field = rendered["sections"][0]["fields"][0]
        assert field["placeholder"] == "type here"


# ---------------------------------------------------------------------------
# 4. build_builder_catalog() with locale
# ---------------------------------------------------------------------------


class TestBuilderCatalogWithLocale:
    def test_catalog_display_name_resolved(self) -> None:
        contract = _contract()
        projected = project_contract_to_registry(
            contract,
            account_id="acc",
            install_id="pi_1",
            release_id="pr_1",
            locale="ru",
            locale_bundles=_locale_bundles(),
        )
        catalog = build_builder_catalog(
            contract=contract,
            projected_blocks=projected["blocks"],
        )
        op_entry = next(e for e in catalog if e["block_type"] == "operation")
        # display_name comes from the already-resolved projected block
        assert op_entry["display_name"] == "Получить задачи"

    def test_catalog_io_display_name_resolved(self) -> None:
        bundles = _locale_bundles()
        bundles["ru"]["op.get_tasks.input.list_id.display_name"] = "ID списка"
        bundles["en"]["op.get_tasks.input.list_id.display_name"] = "List ID"
        contract = _contract()
        contract["operation_manifest"][0]["inputs"][0]["display_name"] = "List ID"
        projected = project_contract_to_registry(
            contract,
            account_id="acc",
            install_id="pi_1",
            release_id="pr_1",
            locale="ru",
            locale_bundles=bundles,
        )
        catalog = build_builder_catalog(
            contract=contract,
            projected_blocks=projected["blocks"],
            locale="ru",
            locale_bundles=bundles,
        )
        op_entry = next(e for e in catalog if e["block_type"] == "operation")
        inp = op_entry["public_inputs"][0]
        assert inp["display_name"] == "ID списка"


# ---------------------------------------------------------------------------
# 5. Locale bundle loading via validate_extension_contract_v2
# ---------------------------------------------------------------------------


class TestLocaleBundleLoading:
    def test_loads_locales_from_bundle_root(self, tmp_path: Path) -> None:
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "en.yaml").write_text("ext.display_name: Hello\next.description: World\n")
        (locales_dir / "ru.yaml").write_text("ext.display_name: Привет\n")

        descriptor = {
            "extension_contract_v2": {
                "operation_manifest": [],
                "connection_types": [],
                "step_ui_manifest": [],
                "resource_query_manifest": [],
                "event_bindings": [],
                "artifact_policies": [],
            }
        }
        result = validate_extension_contract_v2(descriptor, bundle_root=str(tmp_path))
        assert result["locale_bundles"]["en"]["ext.display_name"] == "Hello"
        assert result["locale_bundles"]["ru"]["ext.display_name"] == "Привет"
        assert sorted(result["locale_codes"]) == ["en", "ru"]

    def test_no_locales_dir_returns_empty(self, tmp_path: Path) -> None:
        descriptor = {
            "extension_contract_v2": {
                "operation_manifest": [],
                "connection_types": [],
                "step_ui_manifest": [],
                "resource_query_manifest": [],
                "event_bindings": [],
                "artifact_policies": [],
            }
        }
        result = validate_extension_contract_v2(descriptor, bundle_root=str(tmp_path))
        assert result["locale_bundles"] == {}
        assert result["locale_codes"] == []

    def test_missing_en_yaml_emits_warning(self, tmp_path: Path) -> None:
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "ru.yaml").write_text("ext.display_name: Привет\n")

        descriptor = {
            "extension_contract_v2": {
                "operation_manifest": [],
                "connection_types": [],
                "step_ui_manifest": [],
                "resource_query_manifest": [],
                "event_bindings": [],
                "artifact_policies": [],
            }
        }
        result = validate_extension_contract_v2(descriptor, bundle_root=str(tmp_path))
        warning_codes = [w["code"] for w in result["warnings"]]
        assert "locale_missing_default" in warning_codes


# ---------------------------------------------------------------------------
# 6. Validation warnings
# ---------------------------------------------------------------------------


class TestLocaleValidationWarnings:
    def test_dead_keys_in_non_default_locale(self, tmp_path: Path) -> None:
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "en.yaml").write_text("ext.display_name: Hello\next.description: World\n")
        (locales_dir / "ru.yaml").write_text("ext.display_name: Привет\nbogus.key: Extra\n")

        descriptor = {
            "extension_contract_v2": {
                "operation_manifest": [],
                "connection_types": [],
                "step_ui_manifest": [],
                "resource_query_manifest": [],
                "event_bindings": [],
                "artifact_policies": [],
            }
        }
        result = validate_extension_contract_v2(descriptor, bundle_root=str(tmp_path))
        dead_warnings = [w for w in result["warnings"] if w["code"] == "locale_dead_key"]
        assert len(dead_warnings) == 1
        assert "bogus.key" in dead_warnings[0]["message"]

    def test_orphan_keys_in_en_not_matching_manifest(self, tmp_path: Path) -> None:
        locales_dir = tmp_path / "locales"
        locales_dir.mkdir()
        (locales_dir / "en.yaml").write_text(
            "ext.display_name: Hello\next.description: World\ncategory.nonexistent.display_name: Ghost\n"
        )

        descriptor = {
            "extension_contract_v2": {
                "operation_manifest": [],
                "connection_types": [],
                "step_ui_manifest": [],
                "resource_query_manifest": [],
                "event_bindings": [],
                "artifact_policies": [],
            }
        }
        result = validate_extension_contract_v2(descriptor, bundle_root=str(tmp_path))
        orphan_warnings = [w for w in result["warnings"] if w["code"] == "locale_orphan_key"]
        assert len(orphan_warnings) == 1
        assert "category.nonexistent.display_name" in orphan_warnings[0]["message"]


# ---------------------------------------------------------------------------
# 7. derive_expected_locale_keys()
# ---------------------------------------------------------------------------


class TestDeriveExpectedLocaleKeys:
    def test_includes_extension_owned_oauth_setup_copy(self) -> None:
        keys = derive_expected_locale_keys(
            {
                "oauth_providers": [
                    {
                        "provider_id": "example",
                        "credential_fields": [
                            {"field_id": "client_id", "placeholder": "example-client-id"},
                            {"field_id": "client_secret"},
                        ],
                    }
                ]
            }
        )

        assert keys >= {
            "oauth.example.display_name",
            "oauth.example.description",
            "oauth.example.credential.client_id.label",
            "oauth.example.credential.client_id.help_text",
            "oauth.example.credential.client_id.placeholder",
        }
        assert "oauth.example.credential.client_secret.placeholder" not in keys

    def test_returns_correct_keys_for_full_contract(self) -> None:
        keys = derive_expected_locale_keys(_contract())
        assert "ext.display_name" in keys
        assert "ext.description" in keys
        assert "op.get_tasks.display_name" in keys
        assert "op.get_tasks.description" in keys
        assert "op.get_tasks.input.list_id.display_name" in keys
        assert "op.get_tasks.output.tasks.display_name" in keys
        assert "category.task_ops.display_name" in keys
        assert "step.get_tasks.section" in keys
        assert "step.get_tasks.field.list_id.label" in keys
        assert "step.get_tasks.field.list_id.help_text" in keys
        assert "step.get_tasks.field.list_id.placeholder" in keys
        assert "query.list_picker.display_name" in keys
        assert "trigger.on_task_created.display_name" in keys
        assert "connection.sample_tasks_api.display_name" in keys

    def test_empty_contract_returns_minimal_keys(self) -> None:
        contract: dict[str, list] = {
            "connection_types": [],
            "operation_manifest": [],
            "step_ui_manifest": [],
            "resource_query_manifest": [],
            "event_bindings": [],
            "artifact_policies": [],
        }
        keys = derive_expected_locale_keys(contract)
        assert keys == {"ext.display_name", "ext.description"}

    def test_account_scope_description_included_when_present(self) -> None:
        contract: dict[str, list] = {
            "connection_types": [],
            "operation_manifest": [],
            "step_ui_manifest": [],
            "resource_query_manifest": [],
            "event_bindings": [],
            "artifact_policies": [],
        }
        descriptor = {"account_scope": {"description": "Account access"}}
        keys = derive_expected_locale_keys(contract, descriptor=descriptor)
        assert "ext.account_scope_description" in keys
