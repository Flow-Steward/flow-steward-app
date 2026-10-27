from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.application.registry.contract_validation import validate_workflow_readiness
from core.application.registry.workflow_contract import interface_from_definition
from core.application.workflows.workflow_definition_validation import DEFINITION_STEP_KINDS
from core.application.workflows.workflow_step_config_contracts import (
    STEP_CONFIG_ALLOWED_KEYS,
    STEP_OVERRIDE_ALLOWED_KEYS,
    validate_step_config_keys,
    validate_workflow_step_config_keys,
)
from core.application.workflows.workflow_step_kinds import SUPPORTED_STEP_KINDS
from tests._workflow_definitions import edge, linear_workflow, node, stop_step


class _WorkflowDB:
    def __init__(self, workflow: dict):
        self.workflow = workflow

    def get_workflow(self, workflow_id: str):
        return self.workflow if workflow_id == self.workflow["workflow_id"] else None


def test_every_canonical_step_kind_has_an_unknown_key_contract() -> None:
    assert set(STEP_CONFIG_ALLOWED_KEYS) == DEFINITION_STEP_KINDS

    for step_kind in sorted(DEFINITION_STEP_KINDS):
        issues = validate_step_config_keys(
            step_kind=step_kind,
            override={step_kind: {"definitely_unknown": True}},
            step_path="workflow.steps.s1",
        )
        assert issues == [
            {
                "code": "workflow.step_config_key_unknown",
                "message": (f"Unknown {step_kind} config key 'definitely_unknown'"),
                "path": (f"workflow.steps.s1.override.{step_kind}.definitely_unknown"),
                "severity": "error",
            }
        ]


@pytest.mark.parametrize("step_kind", sorted(SUPPORTED_STEP_KINDS))
def test_every_canonical_step_kind_rejects_a_foreign_wrapper(step_kind: str) -> None:
    foreign_kind = next(
        candidate for candidate in sorted(SUPPORTED_STEP_KINDS) if candidate != step_kind
    )

    issues = validate_step_config_keys(
        step_kind=step_kind,
        override={step_kind: {}, foreign_kind: {}},
        step_path="workflow.steps.s1",
    )

    assert issues == [
        {
            "code": "workflow.step_override_key_unknown",
            "message": f"Unknown step override key '{foreign_kind}'",
            "path": f"workflow.steps.s1.override.{foreign_kind}",
            "severity": "error",
        }
    ]


@pytest.mark.parametrize("loop_kind", ["batch"])
def test_nested_loop_step_rejects_foreign_wrapper_at_exact_child_path(
    loop_kind: str,
) -> None:
    workflow = {
        "steps": [
            {
                "step_id": "loop",
                "step_kind": loop_kind,
                "override": {
                    loop_kind: {
                        "steps": [
                            {
                                "step_kind": "set_variable",
                                "override": {
                                    "set_variable": {
                                        "target": "result",
                                        "value": "ok",
                                    },
                                    "http_request": {
                                        "method": "POST",
                                        "url": "https://example.invalid",
                                    },
                                },
                            }
                        ]
                    }
                },
            }
        ]
    }

    issues = validate_workflow_step_config_keys(workflow)

    assert issues == [
        {
            "code": "workflow.step_override_key_unknown",
            "message": "Unknown step override key 'http_request'",
            "path": (f"workflow.steps.loop.override.{loop_kind}.steps[0].override.http_request"),
            "severity": "error",
        }
    ]


@pytest.mark.parametrize("step_kind", ["agent", ""])
def test_agent_kind_rejects_invoke_workflow_wrapper_at_exact_path(step_kind: str) -> None:
    step = {"step_id": "s1", "override": {"invoke_workflow": {"workflow_id": "child-workflow"}}}
    if step_kind:
        step["step_kind"] = step_kind

    assert validate_workflow_step_config_keys({"steps": [step]}) == [
        {
            "code": "workflow.step_override_key_unknown",
            "message": "Unknown step override key 'invoke_workflow'",
            "path": "workflow.steps.s1.override.invoke_workflow",
            "severity": "error",
        }
    ]


def test_explicit_invoke_workflow_kind_accepts_canonical_wrapper() -> None:
    assert (
        validate_step_config_keys(
            step_kind="invoke_workflow",
            override={
                "invoke_workflow": {
                    "workflow_id": "child-workflow",
                    "input_mapping": {},
                    "output_mapping": {},
                    "artifact_mapping": {},
                }
            },
            step_path="workflow.steps.s1",
        )
        == []
    )


def test_switch_misspelling_is_rejected_at_exact_readiness_path() -> None:
    workflow = linear_workflow(
        {
            "step_id": "route-value",
            "step_kind": "switch",
            "override": {
                "switch": {
                    "selector": "state.value",
                    "cases": [{"case_id": "known", "when": "known"}],
                    "default_target_phase": "failed",
                }
            },
        },
        stop_step("done"),
        stop_step("failed", "stop_fail"),
        workflow_id="strict-switch-config",
        settings={
            "execution_graph": {
                "schema_version": 1,
                "entry": node("route-value"),
                "edges": [
                    edge("route-value", node("done"), "case", case_id="known"),
                    edge("route-value", node("failed"), "default"),
                ],
            }
        },
    )

    validation = validate_workflow_readiness(_WorkflowDB(workflow), workflow["workflow_id"])

    matching = [
        error
        for error in validation["errors"]
        if error.get("code") == "workflow.step_config_key_unknown"
    ]
    assert matching == [
        {
            "code": "workflow.step_config_key_unknown",
            "message": "Unknown switch config key 'default_target_phase'",
            "path": "workflow.steps.route-value.override.switch.default_target_phase",
            "severity": "error",
        }
    ]


def test_unknown_step_override_envelope_keys_are_rejected_at_exact_paths() -> None:
    issues = validate_step_config_keys(
        step_kind="set_variable",
        override={
            "set_variable": {"target": "result", "value": "ok"},
            "require_runtime_tool": True,
            "totally_bogus_key": "persisted",
        },
        step_path="workflow.steps.s1",
    )

    assert issues == [
        {
            "code": "workflow.step_override_key_unknown",
            "message": "Unknown step override key 'require_runtime_tool'",
            "path": "workflow.steps.s1.override.require_runtime_tool",
            "severity": "error",
        },
        {
            "code": "workflow.step_override_key_unknown",
            "message": "Unknown step override key 'totally_bogus_key'",
            "path": "workflow.steps.s1.override.totally_bogus_key",
            "severity": "error",
        },
    ]


@pytest.mark.parametrize(
    ("returned_files", "expected_output_keys"),
    [([{"result": "file", "key": "invoice_pdf"}], ["invoice_pdf"]), ([], [])],
)
def test_file_step_returned_files_override_matches_readiness_and_outputs(
    returned_files: list[dict[str, str]], expected_output_keys: list[str]
) -> None:
    workflow = linear_workflow(
        {
            "step_id": "render",
            "step_kind": "file_from_base64",
            "override": {
                "file_from_base64": {
                    "source": "state.inputs.body",
                    "result_target": "state.invoice_pdf",
                },
                "returned_files": returned_files,
            },
        },
        workflow_id="returned-file-readiness",
        settings={"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
    )

    validation = validate_workflow_readiness(_WorkflowDB(workflow), workflow["workflow_id"])
    interface = interface_from_definition(workflow)

    assert validation["valid"] is True, validation["errors"]
    assert validation["errors"] == []
    assert [row.name for row in interface.returned_files] == expected_output_keys


@pytest.mark.parametrize(
    ("returned_files", "expected_path"),
    [
        ({"result": "file", "key": "invoice_pdf"}, "workflow.steps.render.override.returned_files"),
        ([None], "workflow.steps.render.override.returned_files[0]"),
        ([{}], "workflow.steps.render.override.returned_files[0].result"),
    ],
)
def test_malformed_returned_files_fail_workflow_readiness_at_exact_path(
    returned_files: object, expected_path: str
) -> None:
    workflow = linear_workflow(
        {
            "step_id": "render",
            "step_kind": "file_from_base64",
            "override": {
                "file_from_base64": {
                    "source": "state.inputs.body",
                    "result_target": "state.invoice_pdf",
                },
                "returned_files": returned_files,
            },
        },
        workflow_id="malformed-returned-file-readiness",
        settings={"callable": {"enabled": True, "trigger_channels": ["parent_workflow"]}},
    )

    validation = validate_workflow_readiness(_WorkflowDB(workflow), workflow["workflow_id"])

    matching = [
        issue
        for issue in validation["errors"]
        if issue.get("code", "").startswith("workflow.interface.returned_file")
    ]
    assert validation["valid"] is False
    assert [issue["path"] for issue in matching] == [expected_path]


def test_file_step_returned_files_typo_is_rejected_at_exact_readiness_path() -> None:
    issues = validate_step_config_keys(
        step_kind="file_from_base64",
        override={"returned_file": [{"result": "file", "key": "invoice_pdf"}]},
        step_path="workflow.steps.render",
    )

    assert issues == [
        {
            "code": "workflow.step_override_key_unknown",
            "message": "Unknown step override key 'returned_file'",
            "path": "workflow.steps.render.override.returned_file",
            "severity": "error",
        }
    ]


def test_set_variable_accepts_declared_output_types_consumed_by_io_resolver() -> None:
    issues = validate_step_config_keys(
        step_kind="set_variable",
        override={
            "set_variable": {
                "assignments": {"state.weather_summary": "Weather ready"},
                "output_types": {"state.weather_summary": "paragraph"},
            }
        },
        step_path="workflow.steps.s1",
    )

    assert issues == []


def test_http_request_accepts_only_canonical_body_template() -> None:
    canonical = validate_step_config_keys(
        step_kind="http_request",
        override={"http_request": {"body_template": {"city": "{{state.city}}"}}},
        step_path="workflow.steps.s1",
    )
    assert canonical == []


@pytest.mark.parametrize("legacy_key", ["body", "json", "step_id"])
def test_http_request_rejects_legacy_config_keys_with_exact_path(legacy_key: str) -> None:
    issues = validate_step_config_keys(
        step_kind="http_request",
        override={"http_request": {legacy_key: "legacy"}},
        step_path="workflow.steps.s1",
    )

    assert [issue["path"] for issue in issues] == [
        f"workflow.steps.s1.override.http_request.{legacy_key}"
    ]
    assert [issue["code"] for issue in issues] == ["workflow.step_config_key_unknown"]


@pytest.mark.parametrize("flat_key", ["request_url", "target", "response_target", "body", "json"])
def test_http_request_rejects_flat_config_keys_with_exact_path(flat_key: str) -> None:
    issues = validate_step_config_keys(
        step_kind="http_request",
        override={"http_request": {}, flat_key: "invalid"},
        step_path="workflow.steps.s1",
    )

    assert [issue["path"] for issue in issues] == [f"workflow.steps.s1.override.{flat_key}"]
    assert [issue["code"] for issue in issues] == ["workflow.step_override_key_unknown"]


def test_http_request_contract_declares_every_ui_and_runtime_field() -> None:
    assert STEP_CONFIG_ALLOWED_KEYS["http_request"] == {
        "artifact_filename",
        "auth_mode",
        "auth_ref",
        "body_template",
        "expected_statuses",
        "headers",
        "max_response_bytes",
        "method",
        "query_params",
        "response_mode",
        "response_target",
        "timeout_seconds",
        "url",
    }


def test_http_request_builtin_profile_declares_canonical_authoring_contract() -> None:
    from core.application.registry.builtin_step_kind_profiles import (
        builtin_step_kind_profiles,
    )

    profile = next(
        row for row in builtin_step_kind_profiles() if row["step_kind_key"] == "http_request"
    )
    schema = profile["config_schema_jsonb"]["properties"]["http_request"]

    assert set(schema["properties"]) == {
        "artifact_filename",
        "auth_mode",
        "auth_ref",
        "body_template",
        "expected_statuses",
        "headers",
        "max_response_bytes",
        "method",
        "query_params",
        "response_mode",
        "response_target",
        "timeout_seconds",
        "url",
    }
    assert set(schema["required"]) == {
        "expected_statuses",
        "method",
        "response_target",
        "timeout_seconds",
        "url",
    }


def test_step_override_envelope_declares_all_persisted_runtime_reader_keys() -> None:
    assert (
        DEFINITION_STEP_KINDS
        | {
            "add_tools",
            "block_key",
            "config",
            "connection_binding",
            "connector_install_id",
            "extra_prompt",
            "published_block_key",
            "remove_tools",
            "require_runtime_tools",
            "returned_files",
        }
        == STEP_OVERRIDE_ALLOWED_KEYS
    )

    issues = validate_step_config_keys(
        step_kind="agent",
        override={
            "agent": {"input_mapping": {"brief": "task"}},
            "add_tools": ["web_search"],
            "remove_tools": ["shell"],
            "require_runtime_tools": True,
            "extra_prompt": "Use the project context.",
            "published_block_key": "block.content",
            "connector_install_id": "install-1",
            "connection_binding": {"connection_id": "conn-1"},
        },
        step_path="workflow.steps.s1",
    )
    assert issues == []


def test_canonical_persisted_override_keys_are_declared_by_contract() -> None:
    seed = json.loads(
        (Path(__file__).parents[2] / "db" / "seeds" / "registry_seed.json").read_text(
            encoding="utf-8"
        )
    )
    persisted_keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            override = value.get("override")
            if isinstance(override, dict):
                persisted_keys.update(str(key) for key in override)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(seed)

    assert persisted_keys <= STEP_OVERRIDE_ALLOWED_KEYS


class _ChangeStore:
    """The slice of the registry store the definition change service writes through."""

    def __init__(self, workflow: dict) -> None:
        self.workflow = copy.deepcopy(workflow)
        self.saves: list[dict] = []

    @contextmanager
    def _transaction(self):
        store = self

        class _Cursor:
            def execute(self, _sql: str, _params: tuple) -> None:
                return None

            def fetchone(self) -> dict:
                return {"revision": store.workflow["revision"]}

            def fetchall(self) -> list[dict]:
                # These definition-change fixtures have no retained reaction rules.
                return []

        yield _Cursor()

    def get_workflow(self, workflow_id: str) -> dict | None:
        if workflow_id != self.workflow["workflow_id"]:
            return None
        return copy.deepcopy(self.workflow)

    def save_workflow(self, data: dict, author: str = "admin", *, expected_revision=None) -> int:
        assert expected_revision == self.workflow["revision"]
        self.saves.append(copy.deepcopy(data))
        self.workflow.update(copy.deepcopy(data))
        self.workflow["revision"] += 1
        return self.workflow["revision"]


def _apply_changes(store: _ChangeStore, *operations: dict) -> None:
    from core.application.workflows.workflow_definition_service import apply_workflow_changes

    apply_workflow_changes(
        store,
        store.workflow["workflow_id"],
        expected_revision=store.workflow["revision"],
        operations=list(operations),
        author="test",
    )


def _saved_step(store: _ChangeStore, step_id: str) -> dict:
    return next(step for step in store.saves[-1]["steps"] if step["step_id"] == step_id)


def test_a_supplied_connector_install_id_survives_the_change_set() -> None:
    """The identity the builder derives from the catalog has to reach the step
    unchanged. The live failure was an empty string arriving here; nothing
    between the change set and persistence may reintroduce that."""
    store = _ChangeStore(
        {**linear_workflow(workflow_id="wf-install-id", enabled=False), "revision": 1}
    )

    _apply_changes(
        store,
        {
            "op": "create_step",
            "step": {
                "step_id": "send_the_email",
                "step_kind": "connector",
                "connector_id": "extension.fixture.mailer",
                "connector_install_id": "install_outgoing_email_9f2c",
            },
        },
    )

    assert (
        _saved_step(store, "send_the_email")["connector_install_id"]
        == "install_outgoing_email_9f2c"
    )


def test_a_config_change_that_omits_the_install_id_does_not_erase_it() -> None:
    """A config patch changes only the fields it names."""
    store = _ChangeStore(
        {
            **linear_workflow(
                {
                    "step_id": "send_the_email",
                    "step_kind": "connector",
                    "connector_id": "extension.fixture.mailer",
                    "connector_install_id": "install_outgoing_email_9f2c",
                },
                workflow_id="wf-install-id",
                enabled=False,
            ),
            "revision": 1,
        }
    )

    _apply_changes(
        store,
        {
            "op": "update_step_config",
            "step_id": "send_the_email",
            "patch": {"display_name": "Send the email"},
        },
    )

    saved = _saved_step(store, "send_the_email")
    assert saved["display_name"] == "Send the email"
    assert saved["connector_install_id"] == "install_outgoing_email_9f2c"


@pytest.mark.parametrize(
    ("present", "timeout"),
    [
        pytest.param(False, None, id="omitted"),
        pytest.param(True, None, id="null"),
        pytest.param(True, 0, id="zero"),
        pytest.param(True, False, id="false"),
        pytest.param(True, True, id="true"),
        pytest.param(True, "", id="empty"),
        pytest.param(True, " ", id="whitespace"),
        pytest.param(True, 45, id="integer"),
        pytest.param(True, -2, id="negative"),
        pytest.param(True, 1.5, id="fraction-number"),
        pytest.param(True, 1e2, id="exponent-number"),
        pytest.param(True, "1.5", id="fraction-string"),
        pytest.param(True, "1e2", id="exponent-string"),
        pytest.param(True, "abc", id="nonnumeric-string"),
    ],
)
def test_dataset_timeout_disabled_authoring_preserves_supplied_value_without_type_policy(
    present,
    timeout,
) -> None:
    """Storage acceptance is not evidence that every value can execute."""
    config = {"query_template_id": "query-1", "params": {}, "row_limit": 100}
    if present:
        config["timeout_seconds"] = timeout
    override = {"dataset_query": config}
    assert (
        validate_step_config_keys(
            step_kind="dataset_query", override=override, step_path="workflow.steps.query"
        )
        == []
    )
    store = _ChangeStore(
        {
            **linear_workflow(
                {
                    "step_id": "query",
                    "step_kind": "dataset_query",
                    "override": {"dataset_query": {"query_template_id": "query-1"}},
                },
                workflow_id="dataset-timeout-characterization",
                enabled=False,
            ),
            "revision": 1,
        }
    )
    _apply_changes(
        store,
        {
            "op": "update_step_config",
            "step_id": "query",
            "patch": {"override": override},
        },
    )
    assert len(store.saves) == 1
    assert store.workflow["revision"] == 2
    saved = _saved_step(store, "query")["override"]["dataset_query"]
    returned = next(
        step
        for step in store.get_workflow(store.workflow["workflow_id"])["steps"]
        if step["step_id"] == "query"
    )["override"]["dataset_query"]
    for actual in (saved, returned):
        assert ("timeout_seconds" in actual) is present
        if present:
            assert actual["timeout_seconds"] == timeout
            assert type(actual["timeout_seconds"]) is type(timeout)
    # Exercise real readiness too, without claiming a configured dataset binding.
    validation = validate_workflow_readiness(
        _WorkflowDB(store.workflow), store.workflow["workflow_id"]
    )
    assert not [
        issue for issue in validation["errors"] if "timeout_seconds" in str(issue.get("path", ""))
    ]
