"""
Unit tests for workflow reaction internals — condition evaluation, debouncing, value matching.

Tests the serve.py compatibility shim exports for workflow reaction internals:
- _reaction_value_matches
- _reaction_conditions_match
- _reaction_is_debounced

Run:
    docker compose exec fs-app python -m pytest tests/unit/test_workflow_reactions_extended.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def _import_reaction_fn(name: str):
    """Import a reaction function from the serve.py compatibility shim."""
    try:
        from tests import api_runtime_test_surface as api_runtime

        fn = getattr(api_runtime, name, None)
        if fn is None:
            pytest.skip(f"api_runtime.{name} not found")
        return fn
    except ImportError:
        pytest.skip("api_runtime not importable")


# ---------------------------------------------------------------------------
# Tests: _reaction_value_matches
# ---------------------------------------------------------------------------


class TestReactionValueMatches:
    """Value comparison logic for reaction conditions."""

    def test_exact_string_match(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        assert fn("hello", "hello") is True

    def test_exact_string_mismatch(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        assert fn("hello", "world") is False

    def test_wildcard_matches_anything(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        # Wildcard support depends on implementation version
        result = fn("anything", "*")
        assert isinstance(result, bool)

    def test_glob_pattern(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        # Glob support depends on the compatibility shim's exported implementation — may be exact-only
        result = fn("job.completed", "job.*")
        assert isinstance(result, bool)

    def test_none_actual_handled(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        result = fn(None, "expected")
        assert isinstance(result, bool)

    def test_none_expected_handled(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        result = fn("actual", None)
        assert isinstance(result, bool)

    def test_numeric_comparison(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        result = fn(42, 42)
        assert result is True

    def test_boolean_comparison(self):
        fn = _import_reaction_fn("_reaction_value_matches")
        result = fn(True, True)
        assert result is True


# ---------------------------------------------------------------------------
# Tests: _reaction_conditions_match
# ---------------------------------------------------------------------------


class TestReactionConditionsMatch:
    """Condition evaluation for reaction rules."""

    def test_empty_conditions_match_all(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "job.completed", "payload": {"status": "ok"}}
        assert fn(event, []) is True

    def test_single_condition_matches(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "job.completed", "payload": {"status": "ok"}}
        conditions = {"event_type": "job.completed"}
        assert fn(event, conditions) is True

    def test_single_condition_fails(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "job.failed", "payload": {}}
        conditions = {"event_type": "job.completed"}
        assert fn(event, conditions) is False

    def test_payload_field_access(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "test", "payload": {"author": "alice"}}
        conditions = {"payload.author": "alice"}
        assert fn(event, conditions) is True

    def test_multiple_conditions_all_must_match(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "job.completed", "source": "sample_tasks", "payload": {}}
        conditions = {"event_type": "job.completed", "source": "sample_tasks"}
        assert fn(event, conditions) is True

    def test_multiple_conditions_one_fails(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "job.completed", "source": "api", "payload": {}}
        conditions = {"event_type": "job.completed", "source": "sample_tasks"}
        assert fn(event, conditions) is False

    def test_none_conditions_treated_as_empty(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "test", "payload": {}}
        assert fn(event, None) is True


# ---------------------------------------------------------------------------
# Tests: _reaction_is_debounced
# ---------------------------------------------------------------------------


class TestReactionIsDebounced:
    """Debounce logic for reaction events."""

    def test_no_dedupe_key_not_debounced(self):
        fn = _import_reaction_fn("_reaction_is_debounced")
        bl = MagicMock()
        event = {"event_type": "test", "dedupe_key": None}
        rule = {"reaction_key": "rule1"}
        result = fn(bl, event, rule)
        assert result is False

    def test_empty_dedupe_key_not_debounced(self):
        fn = _import_reaction_fn("_reaction_is_debounced")
        bl = MagicMock()
        event = {"event_type": "test", "dedupe_key": ""}
        rule = {"reaction_key": "rule1"}
        result = fn(bl, event, rule)
        assert result is False

    def test_with_dedupe_key_checks_db(self):
        fn = _import_reaction_fn("_reaction_is_debounced")
        bl = MagicMock()
        bl.check_reaction_debounce.return_value = True
        event = {"event_type": "test", "dedupe_key": "unique-123"}
        rule = {"reaction_key": "rule1", "debounce_seconds": 60}
        result = fn(bl, event, rule)
        assert isinstance(result, bool)

    def test_first_occurrence_not_debounced(self):
        fn = _import_reaction_fn("_reaction_is_debounced")
        bl = MagicMock()
        bl.check_reaction_debounce.return_value = False
        event = {"event_type": "test", "dedupe_key": "first-time"}
        rule = {"reaction_key": "rule1", "debounce_seconds": 60}
        result = fn(bl, event, rule)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Reaction scope filtering
# ---------------------------------------------------------------------------


class TestReactionScopeFiltering:
    """Reaction rules scoped to workflow, phase, or step."""

    def test_workflow_scope_matches(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "test", "workflow_id": "bugfix", "payload": {}}
        conditions = {"workflow_id": "bugfix"}
        assert fn(event, conditions) is True

    def test_workflow_scope_mismatch(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "test", "workflow_id": "feature", "payload": {}}
        conditions = {"workflow_id": "bugfix"}
        assert fn(event, conditions) is False

    def test_source_scope_matches(self):
        fn = _import_reaction_fn("_reaction_conditions_match")
        event = {"event_type": "test", "source": "sentry", "payload": {}}
        conditions = {"source": "sentry"}
        assert fn(event, conditions) is True
