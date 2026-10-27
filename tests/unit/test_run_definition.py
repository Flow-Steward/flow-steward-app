from __future__ import annotations

import pytest

from core.application.orchestration.active_step import active_step, message_matches_execution
from core.application.orchestration.run_cursor import RUNTIME_SCHEMA_VERSION, RunCursor, begin_step
from core.application.orchestration.run_definition import (
    IncompatibleRuntimeStateError,
    RunDefinitionIntegrityError,
    clear_run_definition_cache,
    ensure_run_definition,
    load_run_definition,
    prime_run_definition,
)
from core.application.workflows.workflow_definition_snapshot import definition_digest
from tests._workflow_definitions import linear_graph


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_run_definition_cache()
    yield
    clear_run_definition_cache()


DEFINITION = {
    "steps": [
        {"step_id": "fetch", "step_kind": "http_request", "config": {"url": "https://x"}},
        {"step_id": "done", "step_kind": "stop_success"},
    ],
    "groups": [],
    "execution_graph": linear_graph(["fetch", "done"]),
    "settings": {},
}


def _row(definition=DEFINITION, *, snapshot_id="snap-1", revision=4, digest=None):
    return {
        "id": snapshot_id,
        "revision": revision,
        "digest": digest or definition_digest(definition),
        "definition_jsonb": definition,
    }


def _state(**ref_overrides):
    ref = {"snapshot_id": "snap-1", "revision": 4, "digest": definition_digest(DEFINITION)}
    ref.update(ref_overrides)
    return {
        "job_id": "job-1",
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "workflow_definition_ref": ref,
    }


def test_the_archived_definition_is_loaded_once_through_the_job() -> None:
    calls = []

    def loader(job_id):
        calls.append(job_id)
        return _row()

    first = load_run_definition(_state(), loader=loader)
    second = load_run_definition(_state(), loader=loader)
    assert first is second
    assert calls == ["job-1"]
    assert first.step("fetch")["config"] == {"url": "https://x"}


@pytest.mark.parametrize(
    "row",
    [
        None,
        _row(snapshot_id="snap-other"),
        _row(revision=5),
        _row(digest="0" * 64),
        # Content edited after archiving: the stored digest no longer matches it.
        {**_row(), "definition_jsonb": {**DEFINITION, "settings": {"tampered": True}}},
    ],
)
def test_a_missing_or_mismatched_archive_row_stops_the_run(row) -> None:
    state = _state()
    with pytest.raises(RunDefinitionIntegrityError):
        load_run_definition(state, loader=lambda _job: row)


def test_a_cached_definition_still_has_to_match_the_ref() -> None:
    load_run_definition(_state(), loader=lambda _job: _row())
    stale_state = _state(revision=9)
    with pytest.raises(RunDefinitionIntegrityError):
        load_run_definition(stale_state, loader=lambda _job: _row())


def test_a_phase_era_checkpoint_is_refused_explicitly() -> None:
    legacy = {"job_id": "job-1", "phase_stack": [{"phase_id": "build"}]}
    with pytest.raises(IncompatibleRuntimeStateError):
        load_run_definition(legacy, loader=lambda _job: _row())


def test_the_active_step_comes_from_the_cursor_and_the_pinned_definition() -> None:
    prime_run_definition(_state()["workflow_definition_ref"], DEFINITION)
    state = _state()
    assert active_step(state) is None
    cursor = begin_step(RunCursor(), "fetch", id_factory=lambda: "exec-1")
    state["run_cursor"] = cursor.dump()

    current = active_step(state)
    assert current is not None
    assert (current.step_id, current.step_kind, current.attempt) == ("fetch", "http_request", 1)
    identity = current.message_identity()
    assert message_matches_execution(identity, current)
    assert not message_matches_execution({**identity, "attempt": 2}, current)
    assert not message_matches_execution({**identity, "step_execution_id": "exec-0"}, current)


@pytest.mark.parametrize(
    ("version", "partial_ref"),
    [(None, False), (None, True), (1, False), (1, True), (RUNTIME_SCHEMA_VERSION, True)],
)
def test_ensure_refuses_incompatible_or_partial_checkpoints_without_repinning(version, partial_ref):
    state = _state()
    state["runtime_schema_version"] = version
    if partial_ref:
        state["workflow_definition_ref"].pop("snapshot_id")

    def never_pin(*_args):
        pytest.fail("A continuation must not pin a replacement archive")

    def never_load(*_args):
        pytest.fail("Malformed checkpoint must fail before loading any archive")

    error = (
        RunDefinitionIntegrityError
        if version == RUNTIME_SCHEMA_VERSION
        else IncompatibleRuntimeStateError
    )
    with pytest.raises(error):
        ensure_run_definition(state, pin=never_pin, loader=never_load)
