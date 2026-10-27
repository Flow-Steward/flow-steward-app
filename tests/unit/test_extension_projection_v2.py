from __future__ import annotations

from core.application.extensions.extension_projection_v2 import (
    build_builder_catalog,
    build_extension_grounding_tool,
    execute_resource_query,
    human_channel_event_flow,
    map_provider_event_to_canonical,
    project_contract_to_registry,
    reconcile_derived_rows,
    render_step_form,
    validate_canonical_event_bindings,
)
from core.application.workflows.workflow_catalog_identity import projected_block_key


def _contract() -> dict:
    return {
        "connection_types": [{"connection_type_id": "jira_cloud"}],
        "operation_manifest": [
            {
                "operation_id": "create_issue",
                "operation_kind": "action",
                "connection_type_ids": ["jira_cloud"],
                "category": "jira_ops",
                "inputs": [{"name": "summary", "value_type": "text"}],
                "outputs": [{"name": "issue_key", "value_type": "text"}],
            }
        ],
        "resource_query_manifest": [{"query_id": "list_projects", "cursor_mode": "none"}],
        "step_ui_manifest": [
            {
                "operation_id": "create_issue",
                "fields": [{"name": "summary", "widget_type": "text"}],
            }
        ],
        "event_bindings": [
            {
                "trigger_id": "jira_issue_created",
                "event_name": "approval.received",
                "operation_id": "create_issue",
            }
        ],
        "artifact_policies": [],
    }


def test_project_contract_to_registry_rows_have_ownership() -> None:
    projected = project_contract_to_registry(
        _contract(),
        account_id="acc_default",
        install_id="pi_1",
        release_id="pr_1",
        enabled=True,
    )
    assert projected["families"] == []
    assert projected["skills"] == []
    keys = {row["block_key"] for row in projected["blocks"]}
    assert projected_block_key("pi_1", "operation.create_issue") in keys
    assert projected_block_key("pi_1", "trigger.jira_issue_created") in keys
    assert all(row["install_id"] == "pi_1" for row in projected["blocks"])
    assert all(row.get("source_install_id") == "pi_1" for row in projected["blocks"])
    assert all(row.get("source_release_id") == "pr_1" for row in projected["blocks"])
    assert all(str(row.get("local_block_key") or "").strip() for row in projected["blocks"])


def test_reconcile_marks_missing_rows_orphaned() -> None:
    previous = [
        {"block_key": "operation.old", "enabled": True, "sync_status": "active"},
        {"block_key": "operation.keep", "enabled": True, "sync_status": "active"},
    ]
    projected = [{"block_key": "operation.keep", "enabled": True, "sync_status": "active"}]
    reconciled = reconcile_derived_rows(
        previous_rows=previous, projected_rows=projected, key_field="block_key"
    )
    by_key = {row["block_key"]: row for row in reconciled}
    assert by_key["operation.keep"]["sync_status"] == "active"
    assert by_key["operation.old"]["sync_status"] == "orphaned"
    assert by_key["operation.old"]["enabled"] is False


def test_builder_catalog_uses_active_projected_blocks_only() -> None:
    contract = _contract()
    projected = project_contract_to_registry(
        contract,
        account_id="acc_default",
        install_id="pi_1",
        release_id="pr_1",
        enabled=True,
    )["blocks"]
    projected.append(
        {
            "block_key": projected_block_key("pi_1", "operation.disabled"),
            "block_type": "operation",
            "enabled": False,
            "sync_status": "active",
            "binding": {"operation_id": "create_issue"},
        }
    )
    catalog = build_builder_catalog(contract=contract, projected_blocks=projected)
    keys = {row["block_key"] for row in catalog}
    assert projected_block_key("pi_1", "operation.create_issue") in keys
    assert projected_block_key("pi_1", "operation.disabled") not in keys


def test_builder_catalog_surfaces_batch_capabilities_only_when_declared() -> None:
    contract = _contract()
    contract["operation_manifest"][0].update(
        {
            "operation_kind": "bulk_action",
            "supports_batch": True,
            "preferred_batch_size": 50,
            "max_batch_size": 200,
            "batch_input_field": "items",
            "partial_failure_mode": "report_failed_items",
            "supports_async_job": True,
        }
    )
    projected = project_contract_to_registry(
        contract,
        account_id="acc_default",
        install_id="pi_1",
        release_id="pr_1",
        enabled=True,
    )["blocks"]
    catalog = build_builder_catalog(contract=contract, projected_blocks=projected)
    op = next(
        row
        for row in catalog
        if row["block_key"] == projected_block_key("pi_1", "operation.create_issue")
    )
    hints = op["validation_hints"]
    assert hints["operation_kind"] == "bulk_action"
    assert hints["supports_batch"] is True
    assert hints["preferred_batch_size"] == 50
    assert hints["max_batch_size"] == 200
    assert hints["batch_input_field"] == "items"
    assert hints["partial_failure_mode"] == "report_failed_items"
    assert hints["supports_async_job"] is True


def test_extension_grounding_tool_preserves_declared_contract_metadata() -> None:
    contract = _contract()
    filters_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject": {"type": "string"},
            "unseen": {"type": "boolean"},
        },
    }
    contract["operation_manifest"][0]["inputs"].append(
        {
            "name": "filters",
            "value_type": "object",
            "required": False,
            "schema": filters_schema,
        }
    )
    contract["external_effects"] = [
        {"operation_id": "create_issue", "effect_kind": "remote_issue_create"}
    ]
    contract["artifact_policies"] = [
        {
            "operation_id": "create_issue",
            "outputs": [
                {
                    "kind": "artifact",
                    "binding_key": "issue_artifact_handle",
                    "value_type": "artifact_handle",
                }
            ],
        }
    ]
    contract["operation_recipes"] = [
        {
            "recipe_id": "create_then_read",
            "sequence": [{"operation_id": "create_issue"}],
        }
    ]
    descriptor = {
        "extension_id": "vendor.jira",
        "display_name": "Jira",
        "description": "Jira operations",
        "listing": {"category": "work_management", "tags": ["jira"]},
        "extension_contract_v2_normalized": contract,
        "external_effects": contract["external_effects"],
        "operation_recipes": contract["operation_recipes"],
    }

    tool = build_extension_grounding_tool(descriptor, account_id="acc-1", install_id="pi-1")

    assert tool["tool_id"] == "extension.vendor.jira"
    assert tool["auth"] == {
        "auth_source": "project_connection",
        "required_connection_roles": ["jira_cloud"],
    }
    operation = tool["interface"]["operations"][0]
    assert operation["side_effect_type"] == "write"
    assert operation["parameters"] == [
        {"name": "summary", "type": "text", "required": False},
        {
            "name": "filters",
            "type": "object",
            "required": False,
            "schema": filters_schema,
        },
    ]
    assert operation["returns"] == [{"name": "issue_key", "type": "text", "required": False}]
    assert tool["external_effects"] == contract["external_effects"]
    assert tool["artifact_policies"] == contract["artifact_policies"]
    assert tool["operation_recipes"] == contract["operation_recipes"]


def test_plural_connection_roles_survive_projection() -> None:
    contract = _contract()
    contract["connection_types"] = [
        {"connection_type_id": "ftp"},
        {"connection_type_id": "sftp"},
    ]
    contract["operation_manifest"][0]["connection_type_ids"] = ["ftp", "sftp"]
    descriptor = {
        "extension_id": "acme.file-transfer",
        "display_name": "Acme Storage",
        "description": "Fetch a remote file.",
        "extension_contract_v2_normalized": contract,
    }

    tool = build_extension_grounding_tool(descriptor, account_id="acc-1", install_id="pi-1")

    assert tool["auth"] == {
        "auth_source": "project_connection",
        "required_connection_roles": ["ftp", "sftp"],
    }
    assert tool["interface"]["operations"][0]["connection_type_ids"] == ["ftp", "sftp"]


def test_selected_connection_free_operation_does_not_inherit_bundle_roles() -> None:
    contract = _contract()
    contract["operation_manifest"].append(
        {
            "operation_id": "inspect_local_config",
            "operation_kind": "action",
            "connection_type_ids": [],
            "inputs": [],
            "outputs": [],
        }
    )
    descriptor = {
        "extension_id": "acme.mixed",
        "display_name": "Mixed",
        "description": "Mixed connection policies.",
        "extension_contract_v2_normalized": contract,
    }
    tool = build_extension_grounding_tool(descriptor, account_id="acc-1", install_id="pi-1")
    operations = {item["operation_id"]: item for item in tool["interface"]["operations"]}
    assert operations["inspect_local_config"]["connection_type_ids"] == []
    assert operations["create_issue"]["connection_type_ids"] == ["jira_cloud"]


def test_render_step_form_keeps_platform_widgets_only() -> None:
    contract = _contract()
    contract["step_ui_manifest"][0]["fields"].append(
        {"name": "bad", "widget_type": "custom_widget"}
    )
    rendered = render_step_form(contract=contract, operation_id="create_issue")
    assert rendered["sections"]
    assert rendered["errors"]
    assert "custom_widget" in rendered["errors"][0]


def test_render_step_form_preserves_select_options() -> None:
    contract = _contract()
    contract["step_ui_manifest"] = [
        {
            "operation_id": "create_issue",
            "fields": [
                {
                    "name": "priority",
                    "widget_type": "select",
                    "options": [
                        {"label": "Low", "value": "low"},
                        {"label": "High", "value": "high"},
                    ],
                }
            ],
        }
    ]

    rendered = render_step_form(contract=contract, operation_id="create_issue")

    assert rendered["errors"] == []
    field = rendered["sections"][0]["fields"][0]
    assert field["widget_type"] == "select"
    assert field["options"] == [
        {"label": "Low", "value": "low"},
        {"label": "High", "value": "high"},
    ]


def test_render_step_form_preserves_closed_external_picker_declaration() -> None:
    declaration = {
        "allowed_script_origins": ["https://sdk.example.test"],
        "bootstrap_action_id": "picker_bootstrap",
        "bridge_global": "FixturePicker",
        "button_label": "Choose resource",
        "scripts": [
            {"src": "https://sdk.example.test/picker.js"},
            {"asset_ref": "assets/picker-bridge.js"},
        ],
        "target_component_id": "resource_form",
        "selection": {
            "maximum": 1,
            "fields": [
                {
                    "source": "opaque_id",
                    "target": "resource_id",
                    "required": True,
                    "max_length": 128,
                }
            ],
        },
    }
    contract = {
        "step_ui_manifest": [
            {
                "operation_id": "choose_resource",
                "fields": [
                    {
                        "name": "resource_id",
                        "widget_type": "resource_picker",
                        "external_resource_picker": declaration,
                    }
                ],
            }
        ]
    }

    rendered = render_step_form(contract=contract, operation_id="choose_resource")

    assert rendered["sections"][0]["fields"][0]["external_resource_picker"] == declaration


def test_execute_resource_query_passes_scoped_context() -> None:
    observed: dict = {}

    def _provider(payload: dict) -> dict:
        observed.update(payload)
        return {"items": [{"id": "p1", "name": "Project One"}], "next_cursor": "n1", "total": 1}

    response = execute_resource_query(
        contract=_contract(),
        query_id="list_projects",
        provider=_provider,
        account_id="acc_default",
        project_id=7,
        connection_id="conn_9",
        search="core",
        cursor="c0",
        limit=25,
    )
    assert response["ok"] is True
    assert observed["scope"]["project_id"] == "7"
    assert observed["query_id"] == "list_projects"
    assert response["next_cursor"] == "n1"


def test_canonical_events_validation_and_mapping() -> None:
    contract = _contract()
    contract["event_bindings"].append(
        {"trigger_id": "x", "event_name": "fixture_events.callback_query"}
    )
    errors = validate_canonical_event_bindings(contract)
    assert errors
    mapped = map_provider_event_to_canonical(
        provider_event="fixture_events.callback_query",
        mapping_rules={"fixture_events.callback_query": "approval.received"},
    )
    assert mapped == "approval.received"


def test_human_channel_event_flow() -> None:
    flow = human_channel_event_flow(
        channel="fixture_events", action="request_approval", payload={"job_id": 9}
    )
    assert flow["canonical_event"] == "approval.requested"
    assert flow["payload"]["job_id"] == 9
