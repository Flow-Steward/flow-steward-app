from __future__ import annotations

from types import SimpleNamespace

import pytest

import core.application.orchestration.guardrails.checker as checker
import core.application.registry.registry_manager as registry_manager
from core.application.orchestration.active_step import ActiveStep
from core.application.orchestration.step_guardrails import (
    GUARDRAIL_ESCALATE,
    GUARDRAIL_PASS,
    GUARDRAIL_RETRY,
    check_step_guardrails,
)


def _violation(rule_type: str = "output_addresses_input"):
    return SimpleNamespace(
        display_name="Answers the task", message="Off topic", rule_type=rule_type
    )


@pytest.fixture
def violations(monkeypatch):
    found: list = []
    monkeypatch.setattr(
        registry_manager.RegistryManager,
        "instance",
        classmethod(lambda cls: SimpleNamespace(_db_store=object())),
    )
    monkeypatch.setattr(
        checker,
        "check_guardrails",
        lambda agent_id, state, db: SimpleNamespace(violations=list(found), warnings=[]),
    )
    return found


def _current(step_kind: str = "agent", attempt: int = 1, error_policy=None) -> ActiveStep:
    step = {"step_id": "draft", "step_kind": step_kind}
    if error_policy is not None:
        step["error_policy"] = error_policy
    return ActiveStep(step=step, step_id="draft", step_execution_id="e-1", attempt=attempt)


MESSAGE = {"from_agent": "writer", "status": "pass"}


def _check(state, current):
    return check_step_guardrails(state, current, MESSAGE, emit_event=lambda *a, **k: None)


def test_deterministic_steps_are_never_checked(violations) -> None:
    violations.append(_violation())
    assert _check({}, _current("transform")) is None


def test_a_clean_result_is_recorded_once_per_execution_attempt(violations) -> None:
    verdict = _check({}, _current())
    assert verdict.action == GUARDRAIL_PASS
    assert verdict.update["guardrail_check_results"]["e-1:1"]["status"] == "pass"
    assert _check(verdict.update, _current()) is None


def test_violations_regenerate_then_escalate_when_the_budget_is_spent(violations) -> None:
    violations.append(_violation())
    first = _check({}, _current())
    assert first.action == GUARDRAIL_RETRY
    assert "Answers the task" in first.feedback
    second = _check(first.update, _current(attempt=2))
    assert second.action == GUARDRAIL_ESCALATE
    assert second.detail["retry_limit"] == 1


def test_the_steps_retry_allowance_raises_the_guardrail_budget(violations) -> None:
    violations.append(_violation())
    current = _current(error_policy={"on_error": "retry", "max_retries": 2})
    state: dict = {}
    for _ in range(3):
        verdict = _check(state, current)
        assert verdict.action == GUARDRAIL_RETRY
        state = verdict.update
    assert _check(state, current).action == GUARDRAIL_ESCALATE
