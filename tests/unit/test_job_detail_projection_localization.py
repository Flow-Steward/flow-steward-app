from unittest.mock import patch

import pytest

from core.application.reports.job_detail_contract import _build_lifecycle_contract
from core.application.reports.job_trigger import job_trigger
from core.application.reports.ui_projection import project_job_detail
from core.application.reports.ui_projection_helpers import _build_message_steps
from tests._step_run_state import step_run_state


@pytest.mark.parametrize(
    ("kind", "config", "key", "params"),
    [
        (
            "set_variable",
            {"target": "state.output", "value": 42},
            "react.jobs.step_variables_written",
            {"count": 2},
        ),
        ("stop_success", {}, "react.jobs.step_success_finished", {}),
        (
            "if",
            {"condition": {"operator": "always_true"}},
            "react.jobs.step_if_condition_evaluated",
            {"result": "true"},
        ),
        (
            "if",
            {"condition": {"operator": "always_false"}},
            "react.jobs.step_if_condition_evaluated",
            {"result": "false"},
        ),
    ],
)
def test_builtin_runtime_summary_has_explicit_projection_metadata(kind, config, key, params):
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {"step_id": "owned", "step_kind": kind, "override": {kind: config}},
    )
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        command = control_flow_step_node(state)
    message = command["agent_messages"][-1]
    projected = _build_message_steps([message])[0]
    assert projected["summary_key"] == key
    assert projected["summary_params"] == params
    assert projected["summary"] == message["summary"]
    if kind == "if":
        assert message["artifacts"]["result"] is (params["result"] == "true")
        assert projected["summary"] == f"if condition evaluated to {params['result']}"


@pytest.mark.parametrize("locale", ["en", "lt"])
@pytest.mark.parametrize("matched", [False, True], ids=["default", "matched"])
@pytest.mark.parametrize(
    ("inputs", "value", "when", "display_value", "reason_en", "reason_lt"),
    [
        ({}, None, "", None, "has no value", "neturi reikšmės"),
        ({"channel": None}, None, "", None, "has no value", "neturi reikšmės"),
        ({"channel": False}, False, "False", "false", "resolved to false", "reikšmė: false"),
        ({"channel": 0}, 0, "0", "0", "resolved to 0", "reikšmė: 0"),
        ({"channel": ""}, "", None, '""', 'resolved to ""', 'reikšmė: ""'),
        (
            {"channel": "email"},
            "email",
            "email",
            '"email"',
            'resolved to "email"',
            'reikšmė: "email"',
        ),
        (
            {"channel": "unknown"},
            "unknown",
            "unknown",
            '"unknown"',
            'resolved to "unknown"',
            'reikšmė: "unknown"',
        ),
    ],
    ids=["missing", "null", "false", "zero", "empty", "email", "unmatched"],
)
def test_switch_reason_reaches_localized_job_projection_without_changing_raw_values(
    locale, matched, inputs, value, when, display_value, reason_en, reason_lt
):
    import gettext
    from pathlib import Path

    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    cases = [{"case_id": "owner", "when": when, "label": "Owner Ąžuolas"}] if matched else []
    state = step_run_state(
        {
            "step_id": "route",
            "step_kind": "switch",
            "override": {"switch": {"selector": "state.inputs.channel", "cases": cases}},
        },
        inputs=inputs,
    )
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        message = control_flow_step_node(state)["agent_messages"][-1]
    projected = _build_message_steps([message])[0]
    kind = "no_value" if display_value is None else "value"
    outcome = "case" if matched else "default"
    expected_params = {} if display_value is None else {"value": display_value}
    if matched:
        expected_params["case_id"] = "owner"
    expected_outcome = {"name": "case", "case_id": "owner"} if matched else {"name": "default"}
    expected_output = (
        {**expected_outcome, "label": "Owner Ąžuolas"} if matched else expected_outcome
    )
    suffix_en = "case 'owner'" if matched else "the default case"
    suffix_lt = "atvejis „owner“" if matched else "numatytasis atvejis"
    assert projected["summary"] == message["summary"] == f"switch selector {reason_en}: {suffix_en}"
    assert projected["summary_key"] == f"react.jobs.step_switch_{kind}_{outcome}"
    assert projected["summary_params"] == expected_params
    catalog = gettext.translation(
        "ui", localedir=Path(__file__).resolve().parents[2] / "locales", languages=[locale]
    )
    rendered = catalog.gettext(projected["summary_key"]).format(**projected["summary_params"])
    expected_lt = (
        f"„switch“ parinkiklis {reason_lt}: {suffix_lt}"
        if value is None
        else f"„switch“ parinkiklio {reason_lt}; {suffix_lt}"
    )
    assert rendered == (projected["summary"] if locale == "en" else expected_lt)
    assert message["artifacts"]["selector"] == value
    assert type(message["artifacts"]["selector"]) is type(value)
    assert message["artifacts"]["_outcome"] == expected_outcome
    assert projected["inputs"] == {"selector": "state.inputs.channel", "value": value}
    assert type(projected["inputs"]["value"]) is type(value)
    assert projected["outputs"] == expected_output


def test_provider_summary_never_gains_builtin_translation_metadata():
    projected = _build_message_steps(
        [
            {
                "id": "owner",
                "status": "pass",
                "summary": "Owner output",
                "from_agent": "connector_step",
                "artifacts": {"step_kind": "connector"},
            }
        ]
    )[0]
    assert not projected.get("summary_key")
    assert projected["summary"] == "Owner output"


def test_completed_schedule_job_exposes_localization_keys_for_owned_projection_copy():
    lifecycle = _build_lifecycle_contract(
        normalized_job_id="job-lt",
        status_payload={"id": "job-lt", "status": "completed", "current_step_id": "end"},
        runtime={},
        incident={},
        approval_gate={},
        approval_history=[],
        sections={},
    )
    projection = project_job_detail(
        job={"id": "job-lt", "status": "completed"},
        lifecycle=lifecycle,
        include_sections=False,
    )

    assert lifecycle["status"]["label_key"] == "react.jobs.completed_successfully"
    hero = projection["hero_summary"]
    assert hero["headline_key"] == "react.jobs.completed_requested_work"
    assert hero["summary_key"] == "react.jobs.completed_requested_work"
    assert [(fact["id"], fact["label_key"]) for fact in hero["facts"]] == [
        ("current_step", "react.jobs.current_step"),
        ("started", "project_detail.jobs.grid.started"),
        ("duration", "jobs.columns.duration"),
        ("last_update", "react.jobs.last_update"),
        ("current_cost", "react.jobs.current_cost"),
    ]
    assert projection["next_action"]["title_key"] == "react.jobs.no_action_required"
    assert projection["next_action"]["body_key"] == "react.jobs.completed_restart_help"
    trigger = job_trigger({"task_source": "schedule", "meta": {"scheduled_cron_expr": "* * * * *"}})
    assert trigger["label_key"] == "project_detail.scheduled.fields.schedule"
    assert trigger["detail"] == "cron * * * * *"


def test_dynamic_runtime_summary_has_no_static_translation_key():
    projection = project_job_detail(
        job={"id": "job-lt", "status": "completed"},
        runtime={"wait_reason": "Provider-specific original diagnostic."},
        include_sections=False,
    )

    assert projection["hero_summary"]["summary"] == "Provider-specific original diagnostic."
    assert not projection["hero_summary"].get("summary_key")


def test_builtin_transform_labels_have_keys_without_relabeling_owner_payload_fields():
    steps = _build_message_steps(
        [
            {
                "id": "transform-msg",
                "from_agent": "transform_step",
                "step_id": "process",
                "status": "completed",
                "artifacts": {
                    "step_kind": "transform",
                    "input_payload": {
                        "rules": [],
                        "source": "state.inputs.rows",
                        "step_id": "pick_fields",
                        "step_kind": "transform",
                        "Batchlabel": "Owner batch",
                        "Parameters": {"team": "Team A", "region": "EU"},
                    },
                    "output_payload": {
                        "result_target": "state.output",
                        "value": {"team": "Team A", "region": "EU"},
                    },
                },
            }
        ]
    )

    assert steps[0]["step_type_label_key"] == "react.workflows.transform"
    assert steps[0]["input_field_label_keys"] == {
        "rules": "react.jobs.field_rules",
        "source": "react.jobs.field_source",
        "step_id": "react.jobs.field_step_id",
        "step_kind": "react.jobs.field_step_kind",
    }
    assert steps[0]["output_field_label_keys"] == {
        "result_target": "react.jobs.field_result_target",
        "value": "react.jobs.field_value",
    }
    assert steps[0]["inputs"]["Parameters"] == {"team": "Team A", "region": "EU"}


def test_provider_fields_named_like_builtin_schema_fields_are_not_relabelled():
    steps = _build_message_steps(
        [
            {
                "id": "provider-msg",
                "step_id": "sync",
                "from_agent": "connector_step",
                "status": "completed",
                "artifacts": {
                    "step_kind": "connector",
                    "input_payload": {
                        "rules": "Provider input",
                        "value": "Owner value",
                        "source": "Provider source",
                    },
                    "output_payload": {
                        "result_target": "Provider result",
                        "value": "Provider value",
                    },
                },
            }
        ]
    )

    assert not steps[0].get("input_field_label_keys")
    assert not steps[0].get("output_field_label_keys")


def test_builtin_artifact_evidence_has_explicit_label_keys_but_provider_evidence_does_not():
    def evidence(step_kind):
        return _build_message_steps(
            [
                {
                    "id": "step-msg",
                    "from_agent": "transform_step",
                    "status": "completed",
                    "artifacts": {"step_id": "transform_parameters", "step_kind": step_kind},
                }
            ]
        )[0]["evidence"]

    builtin = evidence("transform")
    assert [(item["title_key"], item["summary"]) for item in builtin] == [
        ("react.jobs.field_step_id", "transform_parameters"),
        ("react.jobs.field_step_kind", "transform"),
    ]
    assert all(not item.get("title_key") for item in evidence("connector"))


@pytest.mark.parametrize("locale", ["en", "lt"])
def test_generated_copy_keys_are_available_in_compiled_catalogs(locale):
    import gettext
    from pathlib import Path

    translations = gettext.translation(
        "ui", localedir=Path(__file__).resolve().parents[2] / "locales", languages=[locale]
    )
    expected = {
        "react.jobs.step_if_condition_evaluated": (
            "if condition evaluated to {result}",
            "„if“ sąlygos rezultatas: {result}",
        ),
        "react.jobs.workflow_builder_test_run": (
            "Test run from the workflow builder",
            "Bandomasis paleidimas iš darbo eigos kūrimo priemonės",
        ),
        "react.monitoring.delivery_idle_message": (
            "No queued delivery work is waiting for this scope right now.",
            "Šiuo metu šioje srityje nėra laukiančių eilėje pristatymo užduočių.",
        ),
        "react.workflows.import_preflight": ("Preflight", "Išankstinė patikra"),
        "react.workflows.import_resolve": ("Resolve", "Priklausomybių susiejimas"),
        "react.workflows.import_breadcrumb": ("Import", "Importas"),
    }
    for key, text in expected.items():
        assert translations.gettext(key) == text[locale == "lt"]
    from core.application.jobs.queue_delivery_queries import _QUEUE_HEALTH_META

    for meta in _QUEUE_HEALTH_META.values():
        for field in ("label", "message"):
            translated = translations.gettext(meta[f"{field}_key"])
            assert translated != meta[f"{field}_key"]
            assert (translated == meta[field]) is (locale == "en")
    for key in (
        "react.monitoring.delivery_idle_dead_lettered_message",
        "react.monitoring.delivery_idle_execution_failed_message",
        "react.monitoring.delivery_operator_action",
    ):
        assert translations.gettext(key) != key
    assert (
        translations.gettext("react.jobs.step_if_condition_evaluated")
        .format(result="true")
        .endswith("true")
    )


def test_if_invalid_selector_summary_cannot_be_replaced_by_success_translation():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node

    state = step_run_state(
        {
            "step_id": "choose",
            "step_kind": "if",
            "override": {
                "if": {"condition": "state.rows[abc].sku"},
            },
        },
        rows=[[{"sku": "Owner Ąžuolas"}]],
    )
    with patch(
        "core.application.orchestration.agents.common.publish_agent_message", return_value=True
    ):
        update = control_flow_step_node(state)
    message = update["agent_messages"][-1]
    assert message["status"] == "fail"
    assert message["summary"] == "Control-flow step has an invalid input selector"
    projected = _build_message_steps([message])[0]
    assert projected["summary"] == message["summary"]
    assert not projected.get("summary_key")
    assert not projected.get("summary_params")


def test_missing_if_outcome_edge_fails_in_hub_without_success_projection():
    from core.application.orchestration.control_flow_step_node import control_flow_step_node
    from core.application.orchestration.step_hub import orchestrator_hub_node

    state = step_run_state(
        {
            "step_id": "choose",
            "step_kind": "if",
            "override": {
                "if": {"condition": {"operator": "always_true"}},
            },
        },
    )
    with (
        patch(
            "core.application.orchestration.agents.common.publish_agent_message", return_value=True
        ),
        patch("core.application.orchestration.agents.common.emit_event", return_value=None),
    ):
        state.update(control_flow_step_node(state))
        command = orchestrator_hub_node(state)
    assert command.update["status"] == "failed"
    error = command.update["error"]
    assert "has no edge for outcome 'true'" in error
    projection = project_job_detail(
        job={"id": "missing-edge", **command.update},
        failure_reason=error,
        include_sections=False,
    )
    assert projection["hero_summary"]["summary"] == error
    assert not projection["hero_summary"].get("summary_key")
