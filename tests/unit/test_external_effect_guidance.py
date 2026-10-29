"""Ambiguous external effects retain bounded operator guidance, never authority."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from core.application.extensions import external_effects
from core.infrastructure.security import prompt_safety

GUIDANCE = "The destination may need to be selected. Verify the outcome before another submission."


@pytest.fixture
def redact(monkeypatch):
    scanner = MagicMock(side_effect=lambda text, **kwargs: text)
    monkeypatch.setattr(prompt_safety, "redact_text_for_persistence", scanner)
    return scanner


def _confirm(monkeypatch, guidance=None, *, protected_values=()):
    result = {"external_effect_status": "timeout_unknown", "definitely_no_external_effect": False}
    if guidance is not None:
        result["guidance"] = guidance
    invocation = {"status": "succeeded", "response": {"ok": True, "result": result}}
    invocation["response"]["result"]["private_payload"] = "must-not-be-published"
    original = deepcopy(invocation)
    update = MagicMock(
        side_effect=lambda _db, **values: {
            "effect_id": values["effect_id"],
            "status": values["status"],
            "response_summary_jsonb": values["response_summary_jsonb"],
            "project_id": "project-a",
        }
    )
    monkeypatch.setattr(
        external_effects.external_effect_store, "update_external_effect_status", update
    )
    confirmed = external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="effect-a",
        invocation=invocation,
        claim_token="claim-a",
        protected_values=protected_values,
        expected_project_id="project-a",
    )
    checked = external_effects.apply_external_effect_confirmation(invocation, confirmed)
    assert invocation == original
    assert confirmed["status"] == "timeout_unknown"
    assert checked["status"] == "failed"
    assert checked["detail"] == "external_effect_timeout_unknown"
    assert checked["response"]["error_code"] == "external_effect_timeout_unknown"
    assert "result" not in checked["response"]
    assert "private_payload" not in repr(confirmed) + repr(checked)
    assert "retryable" not in checked["response"]
    return checked, confirmed, update.call_args.kwargs


def test_guidance_survives_confirmation_and_is_redacted_in_the_owning_project(monkeypatch, redact):
    checked, confirmed, _ = _confirm(monkeypatch, GUIDANCE)
    assert confirmed["response_summary_jsonb"]["guidance"] == GUIDANCE
    assert checked["response"]["guidance"] == GUIDANCE
    assert checked["errors"] == [{"code": "external_effect_timeout_unknown", "message": GUIDANCE}]
    assert redact.call_args.kwargs["project_id"] == "project-a"
    assert redact.call_args.kwargs["context_type"] == "display"


@pytest.mark.parametrize(
    "guidance",
    [
        None,
        {},
        [],
        7,
        True,
        "x" * 513,
        "line\nbreak",
        "line\rbreak",
        "tab\tvalue",
        "bad\x00value",
        "bad\x1bvalue",
        "bad\u202evalue",
        "password=private",
        "https://private.invalid",
    ],
)
def test_invalid_or_credential_like_guidance_does_not_cross_confirmation(
    monkeypatch, redact, guidance
):
    checked, confirmed, _ = _confirm(monkeypatch, guidance)
    assert "guidance" not in confirmed["response_summary_jsonb"]
    assert checked["response"] == {"ok": False, "error_code": "external_effect_timeout_unknown"}
    assert checked["errors"] == [{"code": "external_effect_timeout_unknown"}]
    redact.assert_not_called()


def test_known_credential_reflection_is_blocked_before_guidance_redaction(monkeypatch, redact):
    checked, confirmed, persisted = _confirm(
        monkeypatch, "Verify opaque-value-123 first.", protected_values={"opaque-value-123"}
    )
    assert persisted["error_code"] == "extension_secret_exposure_blocked"
    assert confirmed["response_summary_jsonb"] == {}
    assert "opaque-value-123" not in repr(checked) + repr(persisted)
    redact.assert_not_called()


def test_canonical_redaction_runs_before_ledger_storage(monkeypatch, redact):
    redact.side_effect = lambda text, **kwargs: "[REDACTED] Verify before another submission."
    checked, confirmed, _ = _confirm(monkeypatch, GUIDANCE)
    assert confirmed["response_summary_jsonb"]["guidance"].startswith("[REDACTED]")
    assert GUIDANCE not in repr(confirmed) + repr(checked)


def test_confirmation_ignores_provider_guidance_without_confirmed_ledger_guidance(redact):
    checked = external_effects.apply_external_effect_confirmation(
        {"status": "succeeded", "response": {"result": {"guidance": GUIDANCE}}},
        {"status": "timeout_unknown", "response_summary_jsonb": {}},
    )
    assert checked["response"] == {"ok": False, "error_code": "external_effect_timeout_unknown"}
    redact.assert_not_called()


@pytest.mark.parametrize(
    "guidance", [{"message": GUIDANCE}, "x" * 513, "bad\x00value", "token=private"]
)
def test_legacy_ledger_guidance_is_revalidated(redact, guidance):
    checked = external_effects.apply_external_effect_confirmation(
        {"status": "succeeded", "response": {}},
        {"status": "timeout_unknown", "response_summary_jsonb": {"guidance": guidance}},
    )
    assert "guidance" not in checked["response"]
    assert checked["errors"] == [{"code": "external_effect_timeout_unknown"}]


def test_visible_job_message_preserves_safe_guidance_and_unknown_outcome(monkeypatch, redact):
    from core.application.orchestration import connector_step_runtime
    from core.application.orchestration.graph.nodes import execute_step_node
    from tests._step_run_state import step_run_state

    checked, confirmed, _ = _confirm(monkeypatch, GUIDANCE)
    monkeypatch.setattr(
        connector_step_runtime,
        "execute_connector_step",
        lambda **kwargs: {
            "ok": False,
            "runtime": "extension_action",
            "detail": "external_effect_timeout_unknown",
            "invocation": checked,
            "external_effect": confirmed,
        },
    )
    state = step_run_state(
        {
            "step_id": "transfer",
            "display_name": "Transfer selected items",
            "step_kind": "connector",
            "connector_id": "fixture.transfer",
        }
    )
    message = execute_step_node(state)["agent_messages"][-1]
    assert message["status"] == "fail"
    assert GUIDANCE in message["summary"]
    assert any(GUIDANCE in text for text in message["errors"])
    assert (
        message["artifacts"]["connector_invocation"]["detail"] == "external_effect_timeout_unknown"
    )


def test_unknown_effect_job_summary_does_not_trust_unconfirmed_error_text(monkeypatch, redact):
    from core.application.orchestration import connector_step_runtime
    from core.application.orchestration.graph.nodes import execute_step_node
    from tests._step_run_state import step_run_state

    monkeypatch.setattr(
        connector_step_runtime,
        "execute_connector_step",
        lambda **kwargs: {
            "ok": False,
            "runtime": "extension_action",
            "detail": "external_effect_timeout_unknown",
            "errors": [
                {"code": "external_effect_timeout_unknown", "message": "Unconfirmed advice"}
            ],
            "external_effect": {"status": "timeout_unknown", "response_summary_jsonb": {}},
        },
    )
    state = step_run_state(
        {"step_id": "transfer", "step_kind": "connector", "connector_id": "fixture.transfer"}
    )
    message = execute_step_node(state)["agent_messages"][-1]
    assert "Unconfirmed advice" not in message["summary"]
    assert message["summary"].endswith("external_effect_timeout_unknown")


@pytest.mark.parametrize("status", ["failed", "suppressed", "unexpected"])
def test_guidance_never_changes_other_confirmation_classifications(redact, status):
    checked = external_effects.apply_external_effect_confirmation(
        {"status": "succeeded", "response": {}},
        {"status": status, "response_summary_jsonb": {"guidance": GUIDANCE}},
    )
    assert "guidance" not in checked["response"]
    assert "message" not in checked["errors"][0]
    redact.assert_not_called()
