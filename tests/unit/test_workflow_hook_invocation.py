"""A hook runs as an ordinary Invoke of the child its pinned binding names (spec §8-11)."""

from __future__ import annotations

import pytest

from core.application.orchestration import invoke_workflow_hook_runtime as hook_runtime
from core.application.workflows.workflow_definition_validation import (
    ChildDefinition,
    validate_workflow_definition,
)
from core.application.workflows.workflow_invocation_target import (
    HookBindingRevision,
    HookOwner,
    MappingBindings,
)
from tests._step_run_state import step_run_state
from tests._workflow_definitions import edge, node
from tests.unit.test_invoke_workflow_step_node import (
    INVOKE_EXECUTION_ID,
    _base_state,
    _capture_append_messages,
    _FakeLogger,
    _install_runtime_stubs,
)
from tests.unit.test_invoke_workflow_step_node import (
    _application_submission_repositories as _application_submission_repositories,
)
from tests.unit.test_workflow_hook_contract import declaration

HOOK_STEP = {
    "step_id": "before",
    "agent": "invoke_workflow",
    "skill": "run_child",
    "override": {"invoke_workflow": {"hook": declaration()}},
}
CHILD = {
    "hook_child": {
        "workflow_id": "hook_child",
        "enabled": True,
        "settings": {
            "callable": {
                "enabled": True,
                "expected_inputs": [
                    {"name": "invoice_id", "type": "number", "required": True},
                    {"name": "company", "type": "string"},
                ],
                "expected_outputs": [
                    {"name": "company_name", "type": "string"},
                    {"name": "crm_id", "type": "string"},
                ],
            }
        },
    }
}
REVISION = HookBindingRevision(
    id="rev-1",
    binding_id="b-1",
    revision=1,
    account_id="acc",
    project_id="7",
    target_workflow_id="hook_child",
    input_mapping={"invoice_id": "invoice_id", "company": "company"},
    output_mapping={"company_name": "company"},
)


def hook_state(**extra) -> dict:
    base = {key: value for key, value in _base_state().items() if key != "agent_messages"}
    base.update(
        {
            "job_id": 500,
            "workflow_id": "invoice_flow",
            "invoice": {"id": 123, "company": "Example UAB"},
            "workflow_data": {"invoice": {"id": 123, "company": "Example UAB"}},
            **extra,
        }
    )
    return step_run_state(HOOK_STEP, step_execution_id=INVOKE_EXECUTION_ID, **base)


def install(
    monkeypatch, *, pins, child=None, child_outputs=None, existing_child=None, workflows=None
):
    from core.application.orchestration import invoke_workflow_step_node as invoke_node

    runtime = _install_runtime_stubs(monkeypatch, workflows=workflows or CHILD)
    meta: dict = {"hook_binding_pins": pins}
    if existing_child is not None:
        meta["invoke_children"] = {
            str(existing_child): {
                "child_job_id": str(existing_child),
                "child_workflow_id": "hook_child",
                "step_id": "before",
                "step_execution_id": INVOKE_EXECUTION_ID,
                "invocation_key": f"500:{INVOKE_EXECUTION_ID}",
                "created_at": "2026-10-15T10:00:00+00:00",
            }
        }
    logger = _FakeLogger(
        child_job_id=existing_child or 931,
        child_messages=[{"envelope": {"artifacts": dict(child_outputs or {})}}],
        child_status_sequence=["completed"],
        parent_job={"account_id": "acc", "meta": meta},
    )
    monkeypatch.setattr(invoke_node, "_get_logger", lambda: logger)
    monkeypatch.setattr(invoke_node, "_should_execute_child_inline", lambda _parent_job: False)
    monkeypatch.setattr(
        invoke_node,
        "_wait_for_terminal_child",
        lambda _bl, child_job_id, **_kwargs: (
            {"id": int(child_job_id), "status": "completed", "error": ""},
            "completed",
        ),
    )
    monkeypatch.setattr(
        hook_runtime, "load_binding_revision", lambda rid, _acc: {"rev-1": REVISION}.get(rid)
    )
    monkeypatch.setenv("FS_INVOKE_CHILD_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("FS_INVOKE_CHILD_POLL_SECONDS", "1")
    captured = _capture_append_messages(monkeypatch, invoke_node)
    return invoke_node, runtime, logger, captured


def published(result: dict) -> dict:
    return result["workflow_data"]["hooks"]["before_invoice"]


def test_empty_hook_has_no_child_and_publishes_defaults(monkeypatch) -> None:
    invoke_node, runtime, logger, captured = install(monkeypatch, pins={"before_invoice": None})
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert runtime["enqueue_calls"] == []
    assert logger.create_calls == []
    assert captured[-1]["status"] == "pass"
    # The run history says it in the viewer's language, with the author's business name.
    assert captured[-1]["artifacts"]["summary_key"] == "react.jobs.step_hook_empty"
    assert captured[-1]["artifacts"]["summary_params"] == {"hook": "Before creating an invoice"}
    assert published(result) == {
        "output": {"invoice_id": 123, "company": "Example UAB", "skip_request": False},
        "step_execution_id": INVOKE_EXECUTION_ID,
    }
    # The original author variables are untouched.
    assert result["workflow_data"]["invoice"] == {"id": 123, "company": "Example UAB"}


def test_hook_output_is_published_atomically(monkeypatch) -> None:
    invoke_node, runtime, _logger, captured = install(
        monkeypatch, pins={"before_invoice": "rev-1"}, child_outputs={"company_name": ""}
    )
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert runtime["enqueue_calls"], "the bound child is submitted as an ordinary Invoke"
    assert captured[-1]["status"] == "pass", captured[-1]
    # The mapped empty string is a value; the default and passthrough fill the rest.
    # Consumers read the complete public Hook result at state.hooks.before_invoice.
    assert published(result) == {
        "output": {"invoice_id": 123, "company": "", "skip_request": False},
        "step_execution_id": INVOKE_EXECUTION_ID,
    }
    assert result["workflow_data"]["invoice"]["company"] == "Example UAB"


def test_an_invalid_child_value_publishes_nothing(monkeypatch) -> None:
    invoke_node, _runtime, _logger, captured = install(
        monkeypatch, pins={"before_invoice": "rev-1"}, child_outputs={"company_name": 5}
    )
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert captured[-1]["status"] == "fail"
    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "workflow.hook.output_invalid"
    # An ordinary step failure the hook's On error or its container can handle.
    assert "_contract_errors" not in captured[-1]["artifacts"]
    assert captured[-1]["artifacts"]["hook_error"]["code"] == "workflow.hook.output_invalid"
    assert "workflow_data" not in result


def test_a_hook_whose_storage_holds_no_steps_runs_nothing(monkeypatch) -> None:
    """R2-11: a new or cleared hook keeps only its hidden end: it passes its inputs through."""
    empty = {
        "hook_child": {
            **CHILD["hook_child"],
            "settings": {
                **CHILD["hook_child"]["settings"],
                "hook_storage": {
                    "author_workflow_id": "invoice_flow",
                    "hook_id": "before_invoice",
                    "project_id": "7",
                },
            },
            "steps": [{"step_id": "hook_end", "step_kind": "stop_success"}],
        }
    }
    invoke_node, runtime, logger, captured = install(
        monkeypatch, pins={"before_invoice": "rev-1"}, workflows=empty
    )
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert runtime["enqueue_calls"] == []
    assert logger.create_calls == []
    assert captured[-1]["status"] == "pass", captured[-1]
    assert published(result)["output"] == {
        "invoice_id": 123,
        "company": "Example UAB",
        "skip_request": False,
    }


def test_a_result_the_hook_steps_did_not_set_keeps_the_hook_input(monkeypatch) -> None:
    invoke_node, _runtime, _logger, captured = install(
        monkeypatch, pins={"before_invoice": "rev-1"}, child_outputs={"crm_id": "c-1"}
    )
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert captured[-1]["status"] == "pass", captured[-1]
    assert published(result)["output"] == {
        "invoice_id": 123,
        "company": "Example UAB",
        "skip_request": False,
    }


def test_hook_resume_adopts_same_child(monkeypatch) -> None:
    invoke_node, runtime, logger, captured = install(
        monkeypatch,
        pins={"before_invoice": "rev-1"},
        child_outputs={"company_name": "Resumed UAB"},
        existing_child=2301,
    )
    result = invoke_node.invoke_workflow_step_node(hook_state())
    assert logger.create_calls == []
    assert runtime["enqueue_calls"] == []
    assert captured[-1]["status"] == "pass", captured[-1]
    assert captured[-1]["artifacts"]["_invoke_workflow"]["child_job_id"] == "2301"
    assert published(result)["output"]["company"] == "Resumed UAB"


def test_changed_child_is_revalidated_before_its_effects(monkeypatch) -> None:
    changed = {
        "hook_child": {
            **CHILD["hook_child"],
            "settings": {
                "callable": {
                    **CHILD["hook_child"]["settings"]["callable"],
                    "expected_inputs": [
                        *CHILD["hook_child"]["settings"]["callable"]["expected_inputs"],
                        {"name": "vat_code", "type": "string", "required": True},
                    ],
                }
            },
        }
    }
    invoke_node, runtime, logger, captured = install(
        monkeypatch, pins={"before_invoice": "rev-1"}, workflows=changed
    )
    invoke_node.invoke_workflow_step_node(hook_state())
    assert captured[-1]["status"] == "fail"
    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "hook_binding_incompatible"
    assert logger.create_calls == []
    assert runtime["enqueue_calls"] == []


def test_a_run_without_a_pin_for_the_hook_never_reads_the_live_binding(monkeypatch) -> None:
    invoke_node, runtime, _logger, captured = install(monkeypatch, pins={})
    invoke_node.invoke_workflow_step_node(hook_state())
    assert captured[-1]["status"] == "fail"
    assert captured[-1]["artifacts"]["_invoke_workflow_error"] == "workflow.hook.binding_not_pinned"
    assert runtime["enqueue_calls"] == []


def test_parallel_ancestry_denies_nested_wait() -> None:
    owner = HookOwner("acc", "p-1", "", "invoice_flow")
    hook_step = {**HOOK_STEP, "group_id": "lanes", "lane_id": "a"}
    definition = {
        "steps": [
            {"step_id": "start", "step_kind": "set_variable"},
            {**hook_step, "step_kind": "invoke_workflow"},
            {"step_id": "other", "step_kind": "set_variable", "group_id": "lanes", "lane_id": "b"},
            {"step_id": "done", "step_kind": "stop_success"},
        ],
        "groups": [
            {
                "group_id": "lanes",
                "body": {
                    "kind": "lanes",
                    "lanes": [
                        {"lane_id": "a", "entry": node("before")},
                        {"lane_id": "b", "entry": node("other")},
                    ],
                },
            }
        ],
        "for_each": [],
        "execution_graph": {
            "schema_version": 1,
            "entry": node("start"),
            "edges": [
                edge("start", node("lanes", "group")),
                edge("before", {"kind": "group_exit", "group_id": "lanes", "lane_id": "a"}),
                edge("other", {"kind": "group_exit", "group_id": "lanes", "lane_id": "b"}),
                edge("lanes", node("done"), source_kind="group"),
            ],
        },
        "settings": {},
    }
    waiting_child = {"steps": [{"step_id": "ask", "step_kind": "approval"}]}

    def load_child(workflow_id: str) -> ChildDefinition | None:
        return (
            ChildDefinition(workflow_id, 1, waiting_child) if workflow_id == "hook_child" else None
        )

    bound = MappingBindings({(owner, "before_invoice"): REVISION})
    codes = [
        issue["code"]
        for issue in validate_workflow_definition(
            definition, load_child=load_child, owner=owner, bindings=bound
        )
    ]
    assert "workflow.placement.lane_child_waits" in codes
    # The same lane with the hook empty calls nothing, so nothing waits.
    empty = MappingBindings({})
    assert "workflow.placement.lane_child_waits" not in [
        issue["code"]
        for issue in validate_workflow_definition(
            definition, load_child=load_child, owner=owner, bindings=empty
        )
    ]


@pytest.mark.parametrize("target", ["state.hooks.before_invoice.output", "hooks.x"])
def test_no_author_step_writes_the_hook_outputs(target) -> None:
    from core.application.orchestration.state_path_guards import is_reserved_state_target

    assert is_reserved_state_target(target)


def test_the_next_item_never_reads_the_previous_item_hook_output() -> None:
    state = {
        "workflow_data": {
            "hooks": {
                "before_invoice": {"output": {"company": "A"}, "step_execution_id": "e-1"},
                "elsewhere": {"output": {"x": 1}, "step_execution_id": "e-0"},
            },
            "invoice": {"id": 1},
        }
    }
    steps = {
        "before": HOOK_STEP,
        "outside": {
            "step_id": "outside",
            "override": {"invoke_workflow": {"hook": declaration(hook_id="elsewhere")}},
        },
    }
    body_hooks = hook_runtime.hook_ids_of_steps(steps, {"before"})
    assert body_hooks == {"before_invoice"}
    cleared = hook_runtime.clear_hook_outputs(state, body_hooks)
    assert cleared == {
        "workflow_data": {
            "hooks": {"elsewhere": {"output": {"x": 1}, "step_execution_id": "e-0"}},
            "invoice": {"id": 1},
        }
    }
    assert hook_runtime.clear_hook_outputs(state, {"absent"}) is None
