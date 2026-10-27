"""One final execution scope from the run cursor to the extension (ADR 0038, spec §14).

Every scope carries ``group_id``, ``for_each_id``, ``step_id`` and
``step_execution_id``. Owners are the step's immediate container: a For each
step is not a root step, and a grant issued to one owner or one item execution
never serves another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from core.application.extensions import host_contract
from core.application.extensions.host_contract import (
    EXECUTION_SCOPE_FIELDS,
    ExecutionScopeError,
    canonical_envelope_scope,
    normalize_execution_scope,
)
from core.application.extensions.remote_runtime_protocol_v2 import (
    build_artifact_access_descriptor,
    build_artifact_audit_record,
    validate_artifact_descriptor_access,
)
from core.application.orchestration.runtime_identity import runtime_identity_from_state
from core.application.orchestration.step_hub import orchestrator_hub_node
from core.infrastructure.extension_sdk.public.flowsteward_extension_sdk import runtime as sdk
from tests._workflow_definitions import edge, node
from tests.unit.test_step_hub import _isolated as _isolated
from tests.unit.test_step_hub import _run, _step

FINAL_FIELDS = ["group_id", "for_each_id", "step_id", "step_execution_id"]


ASSIGN = {"set_variable": {"assignments": {"state.seen": 1}}}


def definition() -> dict:
    """prepare → Group g: [For each fe over state.orders: check → fe exit] → done."""
    return {
        "steps": [
            {"step_id": "prepare", "step_kind": "set_variable", "override": ASSIGN},
            {
                "step_id": "check",
                "step_kind": "set_variable",
                "for_each_id": "fe",
                "override": ASSIGN,
            },
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [
            {"group_id": "g", "body": {"kind": "sequence", "entry": node("fe", "for_each")}}
        ],
        "for_each": [
            {
                "for_each_id": "fe",
                "group_id": "g",
                "over": "state.orders",
                "item_name": "order",
                "body": {"kind": "sequence", "entry": node("check")},
            }
        ],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("prepare"),
            "edges": [
                edge("prepare", node("g", "group")),
                edge("check", {"kind": "for_each_exit", "for_each_id": "fe", "lane_id": None}),
                edge(
                    "fe",
                    {"kind": "group_exit", "group_id": "g", "lane_id": None},
                    source_kind="for_each",
                ),
                edge("g", node("done"), source_kind="group"),
            ],
        },
        "settings": {},
    }


def dispatched_scopes(orders: list) -> list[tuple[str, dict]]:
    """(step id, runtime context) of every dispatch, steps passing at once."""
    state = _run(definition(), orders=orders)
    seen: list[tuple[str, dict]] = []
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        command = orchestrator_hub_node(state)
        for _ in range(40):
            state = _step(state, command)
            if command.goto == "orchestrator_validation":
                return seen
            seen.append((state["current_step_id"], runtime_identity_from_state(state).as_context()))
            from core.application.orchestration.control_flow_step_node import (
                control_flow_step_node,
            )

            state = {**state, **control_flow_step_node(state)}
            command = orchestrator_hub_node(state)
    raise AssertionError("no end")


def test_for_each_is_not_root_scope() -> None:
    scopes = dispatched_scopes([{"n": 1}, {"n": 2}])
    by_step = dict(scopes)
    root = normalize_execution_scope(by_step["prepare"])
    assert (root["group_id"], root["for_each_id"]) == (None, None)
    body = normalize_execution_scope(by_step["check"])
    # The immediate owner is the For each; its parent Group is not repeated.
    assert (body["group_id"], body["for_each_id"]) == (None, "fe")
    assert body["step_id"] == "check"
    assert list(body) == FINAL_FIELDS


def test_each_item_has_its_own_step_execution() -> None:
    executions = [
        context["step_execution_id"]
        for step_id, context in dispatched_scopes([{"n": 1}, {"n": 2}])
        if step_id == "check"
    ]
    assert len(executions) == 2
    assert len(set(executions)) == 2


def test_final_scope_has_four_fields_in_every_envelope() -> None:
    assert list(EXECUTION_SCOPE_FIELDS) == FINAL_FIELDS
    workflow_level = canonical_envelope_scope({"account_id": "acc", "job_id": "9"})
    assert {field: workflow_level[field] for field in FINAL_FIELDS} == dict.fromkeys(FINAL_FIELDS)


@pytest.mark.parametrize(
    "scope",
    [
        # Both owners named.
        {"group_id": "g", "for_each_id": "fe", "step_id": "s", "step_execution_id": "e"},
        # A step scope without its owner keys is not a root step.
        {"step_id": "s", "step_execution_id": "e"},
        {"group_id": None, "step_id": "s", "step_execution_id": "e"},
        # A step without its logical execution, or the other way round.
        {"group_id": None, "for_each_id": None, "step_id": "s", "step_execution_id": None},
        {"group_id": None, "for_each_id": None, "step_id": None, "step_execution_id": "e"},
    ],
)
def test_incomplete_or_conflicting_scope_is_refused(scope) -> None:
    with pytest.raises(ExecutionScopeError):
        normalize_execution_scope(scope)


def _grant(scope: dict) -> dict:
    return build_artifact_access_descriptor(
        binding_key="input_file",
        role="input",
        artifact_id="art",
        transport="presigned_url",
        mode="read",
        access_expires_at="2026-04-07T12:10:00Z",
        download_url="https://signed.local/art/download",
        scope=scope,
    )


def _access(grant: dict, scope: dict) -> bool:
    return validate_artifact_descriptor_access(
        grant, scope=scope, expected_mode="read", now=datetime(2026, 4, 7, 12, 0, tzinfo=UTC)
    )["ok"]


E1 = "11111111-1111-4111-8111-111111111111"
E2 = "22222222-2222-4222-8222-222222222222"
ROOT = {
    "job_id": "9",
    "group_id": None,
    "for_each_id": None,
    "step_id": "s",
    "step_execution_id": E1,
}
IN_FE = {**ROOT, "for_each_id": "fe"}


def test_root_grant_denies_for_each_request() -> None:
    root_grant = _grant(dict(ROOT))
    assert root_grant["scope"]["for_each_id"] is None
    assert _access(root_grant, dict(ROOT)) is True
    assert _access(root_grant, dict(IN_FE)) is False
    fe_grant = _grant(dict(IN_FE))
    assert _access(fe_grant, dict(IN_FE)) is True
    assert _access(fe_grant, dict(ROOT)) is False
    assert _access(fe_grant, {**IN_FE, "for_each_id": "other"}) is False
    assert _access(fe_grant, {k: v for k, v in IN_FE.items() if k != "for_each_id"}) is False


def test_other_item_execution_cannot_reuse_step_grant() -> None:
    first_item = _grant(dict(IN_FE))
    assert _access(first_item, {**IN_FE, "step_execution_id": E2}) is False


def test_artifact_audit_record_names_the_for_each() -> None:
    record = build_artifact_audit_record(
        account_id="acc",
        project_id="p",
        workflow_id="wf",
        run_id="r",
        group_id=None,
        for_each_id="fe",
        step_id="s",
        step_execution_id=E1,
        extension_id="ext",
        install_id="i",
        operation_id="op",
        artifact_id="art",
        grant_mode="read",
        grant_transport="presigned_url",
        issued_at="2026-04-07T12:00:00Z",
        expires_at="2026-04-07T12:10:00Z",
    )
    assert record["for_each_id"] == "fe"
    assert record["group_id"] is None


def test_handshake_requires_final_scope_fields() -> None:
    confirmed = {"contract_version": "extension_host_v2", "execution_scope_fields": FINAL_FIELDS}
    assert host_contract.handshake_contract_error({"result": confirmed}) == ""
    token_only = host_contract.handshake_contract_error(
        {"result": {"contract_version": "extension_host_v2"}}
    )
    assert "execution_scope_fields" in token_only
    for fields in (
        ["group_id", "step_id", "step_execution_id"],
        ["for_each_id", "group_id", "step_id", "step_execution_id"],
        [*FINAL_FIELDS, "lane_id"],
        "group_id,for_each_id,step_id,step_execution_id",
    ):
        assert host_contract.handshake_contract_error(
            {"result": {**confirmed, "execution_scope_fields": fields}}
        )
    assert host_contract.build_handshake_request()["execution_scope_fields"] == FINAL_FIELDS


def test_runtime_side_envelope_check_requires_the_final_scope() -> None:
    scope = {"account_id": "acc", **dict.fromkeys(FINAL_FIELDS)}
    envelope = {"contract_version": "extension_host_v2", "scope": scope}
    assert host_contract.envelope_contract_error(envelope) == ""
    missing = {"contract_version": "extension_host_v2", "scope": {"group_id": None}}
    assert "for_each_id" in host_contract.envelope_contract_error(missing)
    both = {**envelope, "scope": {**scope, "group_id": "g", "for_each_id": "fe"}}
    assert host_contract.envelope_contract_error(both)


def test_sdk_speaks_the_same_final_scope() -> None:
    assert list(sdk.EXECUTION_SCOPE_FIELDS) == FINAL_FIELDS
    assert sdk.handshake_result() == {
        "contract_version": "extension_host_v2",
        "execution_scope_fields": FINAL_FIELDS,
    }
    context = sdk.runtime_context_from_payload(
        {
            "contract_version": "extension_host_v2",
            "runtime_context": {
                "group_id": None,
                "for_each_id": "fe",
                "step_id": "s",
                "step_execution_id": E1,
            },
        }
    )
    assert (context.group_id, context.for_each_id) == (None, "fe")
    for broken in (
        {"group_id": None, "step_id": "s", "step_execution_id": E1},
        {"group_id": "g", "for_each_id": "fe", "step_id": "s", "step_execution_id": E1},
    ):
        with pytest.raises(ValueError):
            sdk.runtime_context_from_payload(
                {"contract_version": "extension_host_v2", "runtime_context": broken}
            )


# --- read structure / change / isolated execute (spec §8.2) ---------------------


def protected_author_workflow() -> dict:
    return {
        "workflow_id": "wf_ext",
        "source_install_id": "inst-ext",
        "projection_mode": "derived",
        "steps": [
            {
                "step_id": "author_step",
                "step_kind": "http_request",
                "override": {
                    "http_request": {
                        "url": "https://x.test",
                        "api_key": "s3cret",  # pragma: allowlist secret
                    }
                },
            }
        ],
        "groups": [],
        "for_each": [],
        "settings": {"execution_graph": {"schema_version": 1, "edges": []}},
    }


def test_read_only_author_graph_does_not_authorize_step_test() -> None:
    from core.application.workflows.workflow_portability_common import WorkflowPortabilityError
    from core.application.workflows.workflow_portability_policy import (
        WorkflowPortabilityPolicyMixin,
    )

    policy_owner = WorkflowPortabilityPolicyMixin(None)
    workflow = protected_author_workflow()
    policy = policy_owner.export_policy_for_workflow(dict(workflow))
    assert "internal_structure_allowed" not in policy
    assert policy["structure_read_allowed"] is True
    assert policy["structure_change_allowed"] is False
    assert policy["step_test_run_allowed"] is False

    readable = policy_owner.redact_protected_workflow(dict(workflow))
    assert readable["protected_workflow"] is True
    assert [step["step_id"] for step in readable["steps"]] == ["author_step"]
    assert "s3cret" not in str(readable)
    assert readable["settings"]["execution_graph"] == workflow["settings"]["execution_graph"]

    with pytest.raises(WorkflowPortabilityError) as refused:
        policy_owner.assert_step_test_run_allowed_for(dict(workflow), action="step_test_run")
    assert refused.value.code == "workflow_step_test_run_protected"
    with pytest.raises(WorkflowPortabilityError):
        policy_owner.assert_structure_change_allowed_for(dict(workflow), action="apply_changes")

    own = {**workflow, "source_install_id": "", "projection_mode": "manual"}
    policy_owner.assert_step_test_run_allowed_for(own, action="step_test_run")
    policy_owner.assert_structure_change_allowed_for(own, action="apply_changes")


def test_a_run_pins_its_for_each_containers_and_protection() -> None:
    from core.application.workflows.workflow_definition_snapshot import frozen_definition

    workflow = {
        **definition(),
        "workflow_id": "wf",
        "settings": {"execution_graph": definition()["execution_graph"]},
    }
    frozen = frozen_definition(workflow)
    assert [item["for_each_id"] for item in frozen["for_each"]] == ["fe"]
    assert frozen["protected"] is False
    assert "internal_structure_allowed" not in frozen
