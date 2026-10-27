from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.orchestration.journal import (
    coerce_journal_entries,
    coerce_journal_entry,
    journal_message,
    make_journal_entry,
)


def test_make_journal_entry_defaults_message_from_event_action_reason():
    entry = make_journal_entry(
        iteration=3,
        event="phase_decision",
        action="retry_phase",
        reason="missing required outputs",
        message="",
    )

    assert entry["iteration"] == 3
    assert entry["event"] == "phase_decision"
    assert entry["action"] == "retry_phase"
    assert entry["reason"] == "missing required outputs"
    assert entry["message"] == "phase_decision: retry_phase — missing required outputs"
    assert isinstance(entry["detail"], dict)


def test_coerce_journal_entry_dict_preserves_structured_fields():
    raw = {
        "iteration": 7,
        "event": "step_input_blocked",
        "step_id": "development",
        "action": "blocked",
        "reason": "Missing inputs: branch_name",
        "message": "Phase blocked",
        "severity": "error",
        "detail": {"missing": ["branch_name"]},
    }
    entry = coerce_journal_entry(raw)

    assert entry["iteration"] == 7
    assert entry["event"] == "step_input_blocked"
    assert entry["step_id"] == "development"
    assert entry["action"] == "blocked"
    assert entry["severity"] == "error"
    assert entry["detail"] == {"missing": ["branch_name"]}


def test_coerce_journal_entry_dict_invalid_iteration_does_not_raise():
    raw = {
        "iteration": "not-a-number",
        "event": "note",
        "message": "hello",
    }
    entry = coerce_journal_entry(raw)
    assert entry["iteration"] == 0
    assert entry["message"] == "hello"


def test_coerce_journal_entry_rejects_legacy_text_payloads():
    with pytest.raises(TypeError, match="structured dict objects"):
        coerce_journal_entry(
            "[iter=5] Phase 'analysis': retry_phase — Missing required outputs: spec_doc"
        )


def test_coerce_journal_entries_handles_none_and_rejects_non_dict_payloads():
    assert coerce_journal_entries(None) == []

    with pytest.raises(TypeError, match="structured dict objects"):
        coerce_journal_entries(
            [
                {"event": "note", "message": "structured"},
                "[iter=1] Phase 'analysis': next_phase - done",
            ]
        )


def test_journal_message_always_returns_string():
    assert journal_message({"event": "note", "message": "abc"}) == "abc"
    with pytest.raises(TypeError, match="structured dict objects"):
        journal_message("[iter=1] Phase 'analysis': next_phase - ok")
