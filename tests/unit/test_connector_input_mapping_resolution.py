"""An input the workflow never had must not be sent as null — STATE-001.

A `state.` path that led nowhere was rendered as null and posted to the
receiving contract, which reads it as a supplied empty value. A stock update
went out carrying two of the three fields it declared, the store accepted it,
and the job finished green having changed nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.application.orchestration.connector_mapping import _render_input_payload

_STATE = {
    "facts": {"status": "on-hold", "total": "25.48"},
    "hit": [{"id": 27}],
    "workflow_data": {"carried": "from an earlier step"},
}


def test_a_resolved_mapping_is_sent() -> None:
    payload, unresolved = _render_input_payload({"status": "state.facts.status"}, _STATE)

    assert payload == {"status": "on-hold"}
    assert unresolved == {}


def test_an_indexed_mapping_resolves() -> None:
    payload, unresolved = _render_input_payload({"id": "state.hit[0].id"}, _STATE)

    assert payload == {"id": 27}
    assert unresolved == {}


def test_a_mapping_that_leads_nowhere_is_omitted_and_reported() -> None:
    payload, unresolved = _render_input_payload(
        {"status": "state.facts.status", "qty": "state.facts.quantity"}, _STATE
    )

    assert payload == {"status": "on-hold"}, "absent, not null: the workflow had no value"
    assert unresolved == {"qty": "state.facts.quantity"}, (
        "the failure must be able to name the path, not leave the operator guessing"
    )


def test_an_explicit_default_still_wins_over_omission() -> None:
    payload, unresolved = _render_input_payload({"qty": "state.nothing | default(7)"}, _STATE)

    assert payload == {"qty": 7}
    assert unresolved == {}


def test_workflow_data_is_still_reached() -> None:
    payload, unresolved = _render_input_payload({"carried": "state.carried"}, _STATE)

    assert payload == {"carried": "from an earlier step"}
    assert unresolved == {}


def test_a_literal_and_a_non_string_mapping_are_untouched() -> None:
    payload, unresolved = _render_input_payload(
        {"lit": "literal:5", "flag": True, "count": 3}, _STATE
    )

    assert payload == {"lit": 5, "flag": True, "count": 3}
    assert unresolved == {}


def test_non_json_literal_preserves_quote_characters_exactly() -> None:
    payload, unresolved = _render_input_payload(
        {
            "range": "literal:'CD Stock'!A1:I6",
            "single_quoted": "literal:'plain text'",
            "leading_quote": "literal:'unmatched",
            "trailing_quote": "literal:unmatched'",
            "json_quoted": "literal:\"'CD Stock'!A1:I6\"",
            "number": "literal:25",
            "flag": "literal:true",
        },
        _STATE,
    )

    assert payload == {
        "range": "'CD Stock'!A1:I6",
        "single_quoted": "'plain text'",
        "leading_quote": "'unmatched",
        "trailing_quote": "unmatched'",
        "json_quoted": "'CD Stock'!A1:I6",
        "number": 25,
        "flag": True,
    }
    assert unresolved == {}


def test_non_json_default_literal_preserves_quote_characters_exactly() -> None:
    payload, unresolved = _render_input_payload(
        {"range": "state.missing | default:'CD Stock'!A1:I6"},
        _STATE,
    )

    assert payload == {"range": "'CD Stock'!A1:I6"}
    assert unresolved == {}


def test_a_value_that_really_is_none_is_still_sent() -> None:
    """Resolving to None is a value; running off the end of the data is not."""
    payload, unresolved = _render_input_payload({"note": "state.note"}, {"note": None})

    assert payload == {"note": None}
    assert unresolved == {}


def test_an_unresolved_mapping_stops_the_step_before_the_connector_is_called(monkeypatch):
    """The audit's fail-open: the field was dropped and the connector ran anyway.

    Omitting a value and dispatching is worse than sending null — it produces a
    real external effect built from data the workflow never had.
    """
    from core.application.orchestration import connector_step_runtime

    dispatched: list[dict] = []
    monkeypatch.setattr(
        connector_step_runtime,
        "_dispatch_tool_runtime",
        lambda **kwargs: dispatched.append(kwargs) or {"handled": True, "ok": True},
    )
    monkeypatch.setattr(
        connector_step_runtime,
        "_dispatch_extension_runtime",
        lambda **kwargs: dispatched.append(kwargs) or {"handled": True, "ok": True},
    )

    result = connector_step_runtime.execute_connector_step(
        step={
            "step_id": "push_stock",
            "connector_id": "extension.acme.commerce",
            "input_mapping_jsonb": {"qty": "state.facts.quantity"},
        },
        state={"facts": {"status": "on-hold"}},
    )

    assert dispatched == [], "no connector or tool may be called with a hole in its input"
    assert result["ok"] is False
    assert result["error"] == "unresolved_required_input"
    assert "push_stock" in result["detail"], "the failure names the step"
    assert "state.facts.quantity" in result["detail"], "and the selector that produced nothing"


def test_test_mode_suppression_precedes_dependent_input_resolution(monkeypatch):
    """A suppressed write cannot produce state for a later suppressed write.

    Test mode must therefore establish that the later operation is suppressible
    before rejecting its missing input. Reads and unsuppressed operations still
    fail closed through the existing unresolved-input path.
    """
    from core.application.orchestration import connector_step_runtime

    dispatched: list[dict] = []

    def _extension(**kwargs):
        dispatched.append(kwargs)
        return {
            "handled": True,
            "ok": True,
            "runtime": "extension_action",
            "detail": "test_mode_suppressed",
        }

    monkeypatch.setattr(connector_step_runtime, "_dispatch_extension_runtime", _extension)

    result = connector_step_runtime.execute_connector_step(
        step={
            "step_id": "update_created_record",
            "connector_id": "extension.acme.records.update_record",
            "connector_install_id": "install-acme",
            "override": {
                "connector": {
                    "extension_id": "extension.acme.records",
                    "operation_id": "update_record",
                }
            },
            "input_mapping_jsonb": {"record_id": "state.created_record_id"},
        },
        state={
            "test_mode": True,
            "trusted_runtime_context": {"test_mode": True},
        },
    )

    assert result["ok"] is True
    assert result["detail"] == "test_mode_suppressed"
    assert result["input_mapping_unresolved"] == {"record_id": "state.created_record_id"}
    assert len(dispatched) == 1
    assert dispatched[0]["suppression_probe_only"] is True
    assert dispatched[0]["input_payload"] == {}


def test_test_mode_mcp_mutation_suppression_precedes_dependent_input_resolution():
    """Projected MCP writes suppress before resolving outputs absent by design."""
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.tool_manifest import ToolManifest

    projected_tool_id = "mcp.example.update_spreadsheet"
    manifest = ToolManifest.model_validate(
        {
            "tool_id": projected_tool_id,
            "display_name": "Example / Update spreadsheet",
            "interface": {
                "functions": [
                    {
                        "name": projected_tool_id,
                        "parameters": [
                            {"name": "spreadsheetId", "type": "string", "required": True}
                        ],
                    }
                ]
            },
            "implementation": {
                "type": "mcp",
                "connector_id": "mcp-example",
                "remote_tool_name": "update_spreadsheet",
            },
            "config": {
                "remote_tool_name": "update_spreadsheet",
                "annotations": {"readOnlyHint": False},
            },
        }
    )
    registry = SimpleNamespace(
        tools=SimpleNamespace(
            get_tool=lambda tool_id: manifest if tool_id == projected_tool_id else None
        )
    )

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool"
        ) as invoke_tool,
    ):
        result = execute_connector_step(
            step={
                "step_id": "add_stock_tabs_via_mcp",
                "connector_id": f"connector.{projected_tool_id}",
                "input_mapping_jsonb": {
                    "spreadsheetId": "state.release_stock_sheet_id",
                },
            },
            state={
                "test_mode": True,
                "trusted_runtime_context": {"test_mode": True},
            },
        )

    invoke_tool.assert_not_called()
    assert result["ok"] is True
    assert result["runtime"] == "tool_manifest"
    assert result["detail"] == "test_mode_suppressed"
    assert result["input_mapping_unresolved"] == {"spreadsheetId": "state.release_stock_sheet_id"}


def test_test_mode_mcp_read_with_unresolved_input_still_fails_closed():
    """A read-only MCP operation is not suppressible, so its inputs stay required."""
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.tool_manifest import ToolManifest

    projected_tool_id = "mcp.example.get_spreadsheet"
    manifest = ToolManifest.model_validate(
        {
            "tool_id": projected_tool_id,
            "display_name": "Example / Get spreadsheet",
            "interface": {
                "functions": [
                    {
                        "name": projected_tool_id,
                        "parameters": [
                            {"name": "spreadsheetId", "type": "string", "required": True}
                        ],
                    }
                ]
            },
            "implementation": {
                "type": "mcp",
                "connector_id": "mcp-example",
                "remote_tool_name": "get_spreadsheet",
            },
            "config": {
                "remote_tool_name": "get_spreadsheet",
                "annotations": {"readOnlyHint": True},
            },
        }
    )
    registry = SimpleNamespace(
        tools=SimpleNamespace(
            get_tool=lambda tool_id: manifest if tool_id == projected_tool_id else None
        )
    )

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool"
        ) as invoke_tool,
    ):
        result = execute_connector_step(
            step={
                "step_id": "read_stock_sheet_via_mcp",
                "connector_id": f"connector.{projected_tool_id}",
                "input_mapping_jsonb": {
                    "spreadsheetId": "state.release_stock_sheet_id",
                },
            },
            state={
                "test_mode": True,
                "trusted_runtime_context": {"test_mode": True},
            },
        )

    invoke_tool.assert_not_called()
    assert result["ok"] is False
    assert result["error"] == "unresolved_required_input"
    assert "state.release_stock_sheet_id" in result["detail"]


def test_suppressed_producer_provenance_skips_dependent_mcp_write_and_read():
    """Only inputs absent because Test Mode suppressed their producer are skippable."""
    from core.application.orchestration.connector_step_runtime import execute_connector_step
    from core.application.registry.tool_manifest import ToolManifest

    def manifest(tool_id: str, *, read_only: bool, parameter: str = ""):
        parameters = [{"name": parameter, "type": "string", "required": True}] if parameter else []
        return ToolManifest.model_validate(
            {
                "tool_id": tool_id,
                "display_name": tool_id,
                "interface": {"functions": [{"name": tool_id, "parameters": parameters}]},
                "implementation": {
                    "type": "mcp",
                    "connector_id": "mcp-example",
                    "remote_tool_name": tool_id.rsplit(".", 1)[-1],
                },
                "config": {
                    "remote_tool_name": tool_id.rsplit(".", 1)[-1],
                    "annotations": {"readOnlyHint": read_only},
                },
            }
        )

    manifests = {
        item.tool_id: item
        for item in (
            manifest("mcp.example.create_spreadsheet", read_only=False),
            manifest(
                "mcp.example.update_spreadsheet",
                read_only=False,
                parameter="spreadsheetId",
            ),
            manifest(
                "mcp.example.get_values",
                read_only=True,
                parameter="spreadsheetId",
            ),
        )
    }
    registry = SimpleNamespace(
        tools=SimpleNamespace(get_tool=lambda tool_id: manifests.get(tool_id))
    )
    state = {
        "test_mode": True,
        "trusted_runtime_context": {"test_mode": True},
    }

    with (
        patch(
            "core.application.registry.registry_manager.RegistryManager.instance",
            return_value=registry,
        ),
        patch(
            "core.application.orchestration.tools.tool_resolver.invoke_manifest_tool"
        ) as invoke_tool,
    ):
        created = execute_connector_step(
            step={
                "step_id": "create_release_stock_sheet",
                "connector_id": "connector.mcp.example.create_spreadsheet",
                "output_mapping_jsonb": {"spreadsheetId": "state.release_stock_sheet_id"},
            },
            state=state,
        )
        state.update(created.get("state_patch") or {})
        updated = execute_connector_step(
            step={
                "step_id": "add_stock_tabs_via_mcp",
                "connector_id": "connector.mcp.example.update_spreadsheet",
                "input_mapping_jsonb": {"spreadsheetId": "state.release_stock_sheet_id"},
            },
            state=state,
        )
        state.update(updated.get("state_patch") or {})
        read = execute_connector_step(
            step={
                "step_id": "verify_cd_stock_via_mcp",
                "connector_id": "connector.mcp.example.get_values",
                "input_mapping_jsonb": {"spreadsheetId": "state.release_stock_sheet_id"},
            },
            state=state,
        )
        mixed_hole = execute_connector_step(
            step={
                "step_id": "read_with_unrelated_missing_input",
                "connector_id": "connector.mcp.example.get_values",
                "input_mapping_jsonb": {
                    "spreadsheetId": "state.release_stock_sheet_id",
                    "range": "state.range_that_never_existed",
                },
            },
            state=state,
        )

    invoke_tool.assert_not_called()
    assert created["detail"] == "test_mode_suppressed"
    assert created["suppressed_output_mappings"] == ["spreadsheetId"]
    assert updated["detail"] == "test_mode_dependency_suppressed"
    assert read["ok"] is True
    assert read["runtime"] == "dependency_suppression"
    assert read["detail"] == "test_mode_dependency_suppressed"
    assert read["suppressed_dependencies"] == {
        "spreadsheetId": {
            "selector": "state.release_stock_sheet_id",
            "producer_step_id": "create_release_stock_sheet",
            "producer_output": "spreadsheetId",
        }
    }
    assert mixed_hole["ok"] is False
    assert mixed_hole["error"] == "unresolved_required_input"
    assert mixed_hole["input_mapping_unresolved"] == {
        "spreadsheetId": "state.release_stock_sheet_id",
        "range": "state.range_that_never_existed",
    }


def test_a_resolved_mapping_still_dispatches(monkeypatch):
    from core.application.orchestration import connector_step_runtime

    dispatched: list[dict] = []

    def _tool(**kwargs):
        dispatched.append(kwargs)
        return {"handled": True, "ok": True}

    monkeypatch.setattr(connector_step_runtime, "_dispatch_tool_runtime", _tool)

    result = connector_step_runtime.execute_connector_step(
        step={
            "step_id": "push_stock",
            "connector_id": "extension.acme.commerce",
            "input_mapping_jsonb": {"status": "state.facts.status"},
        },
        state={"facts": {"status": "on-hold"}},
    )

    assert result.get("ok") is True
    assert dispatched
    assert dispatched[0]["input_payload"] == {"status": "on-hold"}


def test_an_explicit_default_keeps_the_step_running(monkeypatch):
    from core.application.orchestration import connector_step_runtime

    dispatched: list[dict] = []
    monkeypatch.setattr(
        connector_step_runtime,
        "_dispatch_tool_runtime",
        lambda **kwargs: dispatched.append(kwargs) or {"handled": True, "ok": True},
    )

    connector_step_runtime.execute_connector_step(
        step={
            "step_id": "push_stock",
            "connector_id": "extension.acme.commerce",
            "input_mapping_jsonb": {"qty": "state.facts.quantity | default(0)"},
        },
        state={"facts": {}},
    )

    assert dispatched
    assert dispatched[0]["input_payload"] == {"qty": 0}


def test_a_present_null_is_not_an_unresolved_path(monkeypatch):
    """`null` is a value the workflow has; a missing path is not."""
    from core.application.orchestration import connector_step_runtime

    dispatched: list[dict] = []
    monkeypatch.setattr(
        connector_step_runtime,
        "_dispatch_tool_runtime",
        lambda **kwargs: dispatched.append(kwargs) or {"handled": True, "ok": True},
    )

    connector_step_runtime.execute_connector_step(
        step={
            "step_id": "push_stock",
            "connector_id": "extension.acme.commerce",
            "input_mapping_jsonb": {"note": "state.note"},
        },
        state={"note": None},
    )

    assert dispatched
    assert dispatched[0]["input_payload"] == {"note": None}


def test_nullable_blank_and_literal_controls_are_distinct_from_absence() -> None:
    mapping = {
        "query": "state.query",
        "filter": None,
        "note": "",
        "zero": 0,
        "flag": False,
        "encoded_null": "literal:null",
        "literal_empty": "literal:",
    }
    payload, unresolved = _render_input_payload(mapping, {"query": "changed"})
    assert payload == {
        "query": "changed",
        "filter": None,
        "note": "",
        "zero": 0,
        "flag": False,
        "encoded_null": None,
        "literal_empty": "",
    }
    assert unresolved == {}
    absent, absent_unresolved = _render_input_payload(
        {"query": "state.query"}, {"query": "changed"}
    )
    assert absent == {"query": "changed"}
    assert "filter" not in absent
    assert "note" not in absent
    assert absent_unresolved == {}
    encoded, _ = _render_input_payload(
        {**mapping, "zero": "literal:0", "flag": "literal:false"}, {"query": "changed"}
    )
    assert encoded == payload


def test_empty_canonical_connector_clear_cannot_select_a_consumed_input_alias(monkeypatch) -> None:
    from contextlib import nullcontext

    from core.application.orchestration import connector_step_runtime

    observed = []
    monkeypatch.setattr(connector_step_runtime, "_shared_job_store", lambda: None)
    monkeypatch.setattr(
        connector_step_runtime, "step_activity_heartbeat", lambda *_args, **_kwargs: nullcontext()
    )
    monkeypatch.setattr(
        connector_step_runtime,
        "_dispatch_tool_runtime",
        lambda **kwargs: observed.append(kwargs["input_payload"]) or {"handled": True, "ok": True},
    )
    step = {
        "step_kind": "connector",
        "step_id": "mapping",
        "connector_id": "connector.fixture",
        "input_mapping_jsonb": {},
        "override": {"connector": {"input_mapping": {}}},
    }
    connector_step_runtime.execute_connector_step(step=step, state={})
    assert observed == [{}]
    # The unchanged runtime still accepts imported aliases: this control records
    # why the authoring serializer must consume one before canonical Clear.
    observed.clear()
    step["override"]["connector"]["input_mapping_jsonb"] = {"query": "literal:old"}
    connector_step_runtime.execute_connector_step(step=step, state={})
    assert observed == [{"query": "old"}]
