"""Connection slots of extension workflows (ADR 0068)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, ClassVar

import pytest

from core.application.extensions.workflow_connection_slot_bundle import (
    connection_slot_descriptor_issues,
)
from core.application.orchestration.composite_action_runtime import (
    resolve_connector_action_binding,
)
from core.application.workflows import (
    workflow_connection_slot_release as release_module,
)
from core.application.workflows import (
    workflow_connection_slot_service as service_module,
)
from core.application.workflows.workflow_connection_slot_launch import (
    pin_workflow_connection_slots,
)
from core.application.workflows.workflow_connection_slot_release import (
    ConnectionSlotReleaseBlockedError,
    changed_slots,
    check_and_apply_release_slots,
    release_slot_declarations,
)
from core.application.workflows.workflow_connection_slot_repository import SlotOwner
from core.application.workflows.workflow_connection_slot_service import (
    CHOICE_AUTOMATIC,
    CHOICE_CHOSEN,
    CHOICE_NONE,
    WorkflowConnectionSlotService,
    WorkflowConnectionSlotsNotReadyError,
    pinned_context,
    slot_state,
)
from core.application.workflows.workflow_connection_slots import (
    CONNECTION_SLOTS_META_KEY,
    ConnectionSlotContractError,
    WorkflowConnectionSlot,
    declared_slot_names,
    installed_connection_slots,
    job_connection_context,
    parse_connection_slots,
    undeclared_slot_issues,
    workflow_slot_owner,
)

EXTENSION = "acme.store"
SLOT = {
    "slot": "store",
    "extension_id": EXTENSION,
    "connection_type_ids": ["api_token"],
    "label": "Store account",
}


def _workflow(**settings: Any) -> dict[str, Any]:
    return {
        "workflow_id": "extension.inst-1.sync_orders",
        "source_install_id": "inst-1",
        "settings": {"connection_slots": [SLOT], **settings},
        "steps": [
            {
                "step_id": "fetch",
                "step_kind": "connector",
                "connector_id": f"extension.{EXTENSION}",
                "connection_binding_jsonb": {"slot": "store"},
            }
        ],
    }


def _connection(connection_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "connection_id": connection_id,
        "display_name": connection_id.title(),
        "extension_id": EXTENSION,
        "extension_install_id": "inst-1",
        "connection_type": "api_token",
        "enabled": True,
        "health_status": "",
        "runtime_status": "",
        **fields,
    }


# --- declaration ----------------------------------------------------------------


def test_a_declaration_parses_with_its_label() -> None:
    (slot,) = parse_connection_slots([SLOT])

    assert slot == WorkflowConnectionSlot("store", EXTENSION, ("api_token",), "Store account")
    assert slot.display_label == "Store account"
    assert slot.accepts_type("api_token") and not slot.accepts_type("oauth")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"slot": "store"}, "workflow.connection_slots.invalid"),
        ([{**SLOT, "connection_id": "c-1"}], "workflow.connection_slots.field_unknown"),
        ([{**SLOT, "slot": "Store"}], "workflow.connection_slots.name_invalid"),
        ([{**SLOT, "extension_id": "Not An Id"}], "workflow.connection_slots.extension_invalid"),
        ([{**SLOT, "connection_type_ids": [""]}], "workflow.connection_slots.types_invalid"),
        ([SLOT, SLOT], "workflow.connection_slots.duplicate"),
    ],
)
def test_a_malformed_declaration_names_its_field(raw: Any, code: str) -> None:
    with pytest.raises(ConnectionSlotContractError) as raised:
        parse_connection_slots(raw)

    assert raised.value.code == code


def test_only_an_installed_extension_workflow_is_filled_by_the_project() -> None:
    own = {**_workflow(), "source_install_id": ""}

    assert [slot.slot for slot in installed_connection_slots(_workflow())] == ["store"]
    assert installed_connection_slots(own) == ()
    assert declared_slot_names(own) == frozenset({"store"})


def test_the_choice_is_keyed_by_install_and_local_id_not_by_release() -> None:
    assert workflow_slot_owner(_workflow()) == ("inst-1", "sync_orders")
    assert workflow_slot_owner({**_workflow(), "local_workflow_id": "sync"}) == ("inst-1", "sync")


def test_a_job_reads_the_action_context_first_then_the_pinned_slots() -> None:
    pinned = {CONNECTION_SLOTS_META_KEY: {"connections": {"store": "c-1"}, "installs": {}}}
    action = {"composite_action": {"connections": {"store": "c-9"}}, **pinned}

    assert job_connection_context(pinned)["connections"] == {"store": "c-1"}
    assert job_connection_context(action)["connections"] == {"store": "c-9"}
    assert job_connection_context({}) == {}


def test_readiness_flags_a_step_reading_an_undeclared_slot() -> None:
    workflow = {**_workflow(), "settings": {}}
    step = workflow["steps"][0]

    issues = undeclared_slot_issues(workflow, step=step, step_path="workflow.steps.fetch")
    assert [issue["code"] for issue in issues] == ["workflow.connection_slots.undeclared"]
    assert undeclared_slot_issues(_workflow(), step=step, step_path="workflow.steps.fetch") == []
    testing = {**step, "connection_binding_jsonb": {"slot": "store", "connection_id": "c-1"}}
    assert undeclared_slot_issues(workflow, step=testing, step_path="workflow.steps.fetch") == []


# --- bundle ---------------------------------------------------------------------


def _descriptor(workflow: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in workflow.items() if key != "source_install_id"}
    return {
        "extension_id": EXTENSION,
        "runtime": {"compatibility": {"platform_min": "1.8.0"}},
        "artifacts": {"workflows": [{"workflow_id": "sync_orders", "snapshot": body}]},
    }


def test_a_bundle_with_declared_slots_is_accepted() -> None:
    assert connection_slot_descriptor_issues(_descriptor(_workflow())) == []


def test_a_bundle_with_slots_requires_host_contract_1_8_0() -> None:
    descriptor = {
        **_descriptor(_workflow()),
        "runtime": {"compatibility": {"platform_min": "1.7.0"}},
    }

    issues = connection_slot_descriptor_issues(descriptor)
    assert [issue["code"] for issue in issues] == ["workflow.connection_slots.host_minimum"]


def test_a_bundle_step_reading_an_undeclared_slot_is_refused() -> None:
    workflow = {**_workflow(), "settings": {}}

    issues = connection_slot_descriptor_issues(_descriptor(workflow))
    assert [issue["code"] for issue in issues] == ["workflow.connection_slots.undeclared"]
    assert issues[0]["path"].startswith("artifacts.workflows.sync_orders.steps.fetch")


def test_a_bundle_slot_of_an_undeclared_extension_is_refused() -> None:
    workflow = _workflow()
    workflow["settings"]["connection_slots"] = [{**SLOT, "extension_id": "other.ext"}]
    workflow["steps"] = []

    issues = connection_slot_descriptor_issues(_descriptor(workflow))
    assert [issue["code"] for issue in issues] == [
        "workflow.connection_slots.extension_not_declared"
    ]


def test_a_bundle_step_never_carries_a_connection_next_to_its_slot() -> None:
    workflow = _workflow()
    workflow["steps"][0]["connection_binding_jsonb"] = {"slot": "store", "connection_id": "c-1"}

    issues = connection_slot_descriptor_issues(_descriptor(workflow))
    assert [issue["code"] for issue in issues] == ["workflow.connection_slots.fixed_connection"]


def test_an_action_implementation_keeps_its_own_slots() -> None:
    workflow = _workflow(action_implementation={"action_id": "sync", "connections": []})

    issues = connection_slot_descriptor_issues(_descriptor(workflow))
    assert [issue["code"] for issue in issues] == [
        "workflow.connection_slots.implementation_forbidden"
    ]


# --- the one rule ---------------------------------------------------------------


SLOT_ROW = WorkflowConnectionSlot("store", EXTENSION, ("api_token",), "Store account")


def test_the_only_usable_connection_is_chosen_automatically() -> None:
    state = slot_state(
        SLOT_ROW,
        stored="",
        candidates=[
            _connection("c-1"),
            _connection("c-2", enabled=False),
            _connection("c-3", connection_type="oauth"),
            _connection("c-4", extension_id="other.ext"),
        ],
    )

    assert (state.connection_id, state.choice, state.ready) == ("c-1", CHOICE_AUTOMATIC, True)
    assert state.install_id == "inst-1"


def test_several_connections_wait_for_a_choice_with_a_plain_reason() -> None:
    state = slot_state(SLOT_ROW, stored="", candidates=[_connection("c-1"), _connection("c-2")])

    assert (state.ready, state.choice) == (False, CHOICE_NONE)
    assert "the project has 2" in state.reason


def test_no_connection_says_what_to_create() -> None:
    state = slot_state(SLOT_ROW, stored="", candidates=[])

    assert not state.ready
    assert EXTENSION in state.reason and "api_token" in state.reason


def test_a_stored_choice_wins_and_a_stale_one_is_never_replaced_silently() -> None:
    rows = [_connection("c-1"), _connection("c-2")]
    chosen = slot_state(SLOT_ROW, stored="c-2", candidates=rows)
    stale = slot_state(
        SLOT_ROW, stored="c-2", candidates=[_connection("c-1"), _connection("c-2", enabled=False)]
    )

    assert (chosen.connection_id, chosen.choice) == ("c-2", CHOICE_CHOSEN)
    assert not stale.ready and "Choose another connection" in stale.reason


def test_a_parent_run_connection_is_inherited_when_it_fits() -> None:
    rows = [_connection("c-1"), _connection("c-2")]

    assert slot_state(SLOT_ROW, stored="c-1", candidates=rows, preferred="c-2").connection_id == (
        "c-2"
    )
    assert slot_state(SLOT_ROW, stored="c-1", candidates=rows, preferred="c-x").connection_id == (
        "c-1"
    )


def test_the_pinned_context_has_the_action_context_shape() -> None:
    state = slot_state(SLOT_ROW, stored="", candidates=[_connection("c-1")])

    assert pinned_context([state]) == {
        "connections": {"store": "c-1"},
        "installs": {EXTENSION: "inst-1"},
    }


# --- service, pin and release --------------------------------------------------


class _FakeRepository:
    choices_by_owner: ClassVar[dict[tuple[str, str, str, str], dict[str, str]]] = {}
    rows: ClassVar[list[dict[str, Any]]] = []
    links: ClassVar[list[dict[str, str]]] = []
    deleted: ClassVar[list[Any]] = []

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def choices(self, owner: SlotOwner) -> dict[str, str]:
        return dict(self.choices_by_owner.get(owner.key(), {}))

    def candidates(self, **_: Any) -> list[dict[str, Any]]:
        return list(self.rows)

    def linked_projects(self, **_: Any) -> list[dict[str, str]]:
        return list(self.links)

    def delete_slots(self, *, keep: Any, **_: Any) -> int:
        self.deleted.append(sorted(keep))
        return 0


class _FakeDb:
    @contextmanager
    def _cursor(self):
        yield object()


@pytest.fixture
def fake_repository(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRepository]:
    _FakeRepository.choices_by_owner = {}
    _FakeRepository.rows = []
    _FakeRepository.links = []
    _FakeRepository.deleted = []
    monkeypatch.setattr(service_module, "WorkflowConnectionSlotRepository", _FakeRepository)
    monkeypatch.setattr(release_module, "WorkflowConnectionSlotRepository", _FakeRepository)
    return _FakeRepository


def _pin(meta: dict[str, Any], *, parent: dict[str, Any] | None = None) -> dict[str, Any]:
    return pin_workflow_connection_slots(
        workflow_id="extension.inst-1.sync_orders",
        meta=meta,
        project_id="p-1",
        account_id="a-1",
        load_workflow=lambda *_: _workflow(),
        load_job=lambda _job_id: parent,
        service_factory=lambda account: WorkflowConnectionSlotService(
            _FakeDb(), account_id=account
        ),
    )


def test_submission_pins_the_project_connection(fake_repository: type[_FakeRepository]) -> None:
    fake_repository.rows = [_connection("c-1")]

    meta = _pin({CONNECTION_SLOTS_META_KEY: {"connections": {"store": "forged"}}})

    assert meta[CONNECTION_SLOTS_META_KEY] == {
        "connections": {"store": "c-1"},
        "installs": {EXTENSION: "inst-1"},
        "workflow_id": "extension.inst-1.sync_orders",
    }


def test_a_workflow_that_is_not_ready_is_refused_before_it_starts(
    fake_repository: type[_FakeRepository],
) -> None:
    fake_repository.rows = [_connection("c-1"), _connection("c-2")]

    with pytest.raises(WorkflowConnectionSlotsNotReadyError) as raised:
        _pin({})

    assert raised.value.status_code == 409
    assert raised.value.payload["code"] == "workflow.connection_slots.not_ready"
    assert raised.value.payload["slots"][0]["label"] == "Store account"


def test_an_invoke_child_inherits_the_parent_run_connection(
    fake_repository: type[_FakeRepository],
) -> None:
    fake_repository.rows = [_connection("c-1"), _connection("c-2")]
    parent = {
        "project_id": "p-1",
        "meta": {CONNECTION_SLOTS_META_KEY: {"connections": {"store": "c-2"}}},
    }

    meta = _pin({"invoke_parent_job_id": "j-1"}, parent=parent)

    assert meta[CONNECTION_SLOTS_META_KEY]["connections"] == {"store": "c-2"}
    with pytest.raises(WorkflowConnectionSlotsNotReadyError):
        _pin({"invoke_parent_job_id": "j-1"}, parent={**parent, "project_id": "p-2"})


def test_an_action_child_and_an_ordinary_workflow_are_left_alone() -> None:
    action = {"composite_action": {"connections": {"store": "c-1"}}}

    assert _pin(dict(action)) == action
    assert (
        pin_workflow_connection_slots(
            workflow_id="orders", meta={}, project_id="p-1", account_id="a-1"
        )
        == {}
    )


def test_a_release_adding_a_slot_the_project_cannot_fill_is_blocked(
    fake_repository: type[_FakeRepository],
) -> None:
    fake_repository.links = [{"project_id": "p-1", "workflow_id": "extension.inst-1.sync_orders"}]
    fake_repository.rows = [_connection("c-1"), _connection("c-2")]
    new = {"sync_orders": _workflow()}

    with pytest.raises(ConnectionSlotReleaseBlockedError) as raised:
        check_and_apply_release_slots(
            object(),
            account_id="a-1",
            install_id="inst-1",
            release_id="rel-2",
            workflows=new,
            previous_workflows={"sync_orders": {"settings": {}}},
        )

    (issue,) = raised.value.issues
    assert issue["code"] == "workflow.connection_slots.release_needs_choice"
    assert (issue["project_id"], issue["slot"], issue["release_id"]) == ("p-1", "store", "rel-2")


def test_an_unchanged_slot_never_blocks_and_removed_slots_are_cleaned(
    fake_repository: type[_FakeRepository],
) -> None:
    fake_repository.links = [{"project_id": "p-1", "workflow_id": "extension.inst-1.sync_orders"}]
    relabelled = _workflow()
    relabelled["settings"]["connection_slots"] = [{**SLOT, "label": "Shop"}]

    check_and_apply_release_slots(
        object(),
        account_id="a-1",
        install_id="inst-1",
        release_id="rel-2",
        workflows={"sync_orders": relabelled, "other": {"settings": {}}},
        previous_workflows={"sync_orders": _workflow()},
    )

    assert fake_repository.deleted == [[("sync_orders", "store")]]


def test_changed_types_count_as_a_new_slot() -> None:
    old = release_slot_declarations({"w": _workflow()})
    widened = _workflow()
    widened["settings"]["connection_slots"] = [{**SLOT, "connection_type_ids": ["oauth"]}]

    assert changed_slots(release_slot_declarations({"w": widened}), old)["w"][0].slot == "store"
    assert changed_slots(old, old) == {}


# --- runtime ----------------------------------------------------------------------


def test_the_runtime_fills_the_slot_from_the_pinned_context() -> None:
    step = {"step_id": "fetch", "connector_id": f"extension.{EXTENSION}"}

    resolved = resolve_connector_action_binding(
        step=step,
        connection_binding={"slot": "store"},
        connector_install_id="inst-1",
        load_context=lambda: job_connection_context(
            {CONNECTION_SLOTS_META_KEY: {"connections": {"store": "c-1"}, "installs": {}}}
        ),
    )

    assert resolved.error is None
    assert resolved.connection_binding == {"connection_id": "c-1"}
