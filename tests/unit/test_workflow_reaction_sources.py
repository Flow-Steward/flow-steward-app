import pytest

from core.application.settings import ui_i18n
from core.application.workflows.workflow_reaction_sources import (
    build_workflow_reaction_trigger_sources,
)

_KEY = "registry.workflow_detail.automation.event."


@pytest.mark.parametrize(
    ("scope_kind", "expected_events"),
    [
        (
            "workflow",
            [
                ("job.completed", f"{_KEY}job_completed"),
                ("job.failed", f"{_KEY}job_failed"),
                ("job.step_started", f"{_KEY}workflow_step_started"),
                ("job.step_completed", f"{_KEY}workflow_step_completed"),
                ("job.step_failed", f"{_KEY}workflow_step_failed"),
                ("job.step_blocked", f"{_KEY}workflow_step_blocked"),
                ("job.group_started", f"{_KEY}group_started"),
                ("job.group_completed", f"{_KEY}group_completed"),
                ("job.group_failed", f"{_KEY}group_failed"),
                ("job.approval_waiting", f"{_KEY}approval_waiting"),
                ("job.timer_waiting", f"{_KEY}timer_waiting"),
            ],
        ),
        (
            "group",
            [
                ("job.group_started", f"{_KEY}group_started_local"),
                ("job.group_completed", f"{_KEY}group_completed_local"),
                ("job.group_failed", f"{_KEY}group_failed_local"),
                ("job.step_started", f"{_KEY}group_step_started"),
                ("job.step_completed", f"{_KEY}group_step_completed"),
                ("job.step_failed", f"{_KEY}group_step_failed"),
                ("job.step_blocked", f"{_KEY}group_step_blocked"),
                ("job.approval_waiting", f"{_KEY}group_approval_waiting"),
                ("job.timer_waiting", f"{_KEY}group_timer_waiting"),
            ],
        ),
        (
            "step",
            [
                ("job.step_started", f"{_KEY}step_started"),
                ("job.step_completed", f"{_KEY}step_completed"),
                ("job.step_failed", f"{_KEY}step_failed"),
                ("job.step_blocked", f"{_KEY}step_blocked"),
                ("job.approval_waiting", f"{_KEY}step_approval_waiting"),
                ("job.timer_waiting", f"{_KEY}step_timer_waiting"),
            ],
        ),
    ],
)
def test_builtin_events_of_every_scope_expose_explicit_label_keys(scope_kind, expected_events):
    source = build_workflow_reaction_trigger_sources(object(), scope_kind=scope_kind)[0]

    assert source["source_id"] == "__workflow__"
    assert source["display_name"] == "Workflow runtime"
    assert source.get("display_name_key") == "react.workflows.workflow_runtime"
    assert [
        (event["event_type"], event.get("display_name_key")) for event in source["events"]
    ] == expected_events


def test_every_builtin_label_key_is_in_the_shipped_catalogs():
    english = ui_i18n._catalog_messages("en")
    lithuanian = ui_i18n._catalog_messages("lt")
    for scope_kind in ("workflow", "group", "step"):
        source = build_workflow_reaction_trigger_sources(object(), scope_kind=scope_kind)[0]
        for event in source["events"]:
            key = event["display_name_key"]
            assert english.get(key) == event["display_name"], key
            assert lithuanian.get(key), key


def test_retired_phase_scope_is_not_reintroduced_for_localization():
    with pytest.raises(ValueError):
        build_workflow_reaction_trigger_sources(object(), scope_kind="phase")


def test_identical_extension_labels_do_not_inherit_builtin_translation_keys():
    class ExtensionSource:
        def list_project_connections(self, project_id, *, enabled_only):
            assert project_id == "owner-project"
            assert enabled_only
            return [{"extension_id": "owner.extension", "connection_id": "owner-channel"}]

        def get_extension(self, extension_id):
            assert extension_id == "owner.extension"
            return {
                "enabled": True,
                "display_name": "Workflow runtime",
                "event_bindings": [
                    {
                        "event_name": "job.step_started",
                        "display_name": "This step starts",
                        "trigger_id": "owner-trigger",
                    }
                ],
            }

    source = build_workflow_reaction_trigger_sources(
        ExtensionSource(), project_id="owner-project", scope_kind="workflow"
    )[1]
    assert source["source_id"] == "owner.extension"
    assert source["connection_id"] == "owner-channel"
    assert source["display_name"] == "Workflow runtime"
    assert "display_name_key" not in source
    assert source["events"] == [
        {
            "event_type": "job.step_started",
            "display_name": "This step starts",
            "trigger_id": "owner-trigger",
        }
    ]
