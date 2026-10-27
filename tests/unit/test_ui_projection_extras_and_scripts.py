from __future__ import annotations

import json

import pytest

from core.application.reports import ui_projection_extras as extras
from core.application.reports.ui_projection_common import (
    _bounded_diagnostic_value,
    _map_step_status,
)
from core.infrastructure.security.secret_detection import contains_detected_secret


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + ("s" * 36),  # pragma: allowlist secret
        "AKIAABCDEFGHIJKLMNOP",  # pragma: allowlist secret
        "SK" + ("0" * 32),  # pragma: allowlist secret
    ],
)
@pytest.mark.parametrize("crosses_boundary", [False, True])
def test_bounded_diagnostic_value_redacts_secret_at_string_boundary(
    token: str,
    crosses_boundary: bool,
) -> None:
    prefix_length = 1_000 - len(token) + (8 if crosses_boundary else 0)
    value = ("x" * (prefix_length - 1)) + " " + token
    assert contains_detected_secret(value)

    preview = _bounded_diagnostic_value(value)

    assert token not in preview
    assert not contains_detected_secret(preview)
    assert "[REDACTED" in preview


def test_bounded_diagnostic_value_redacts_nested_secret_before_string_truncation() -> None:
    token = "ghp_" + ("n" * 36)  # pragma: allowlist secret
    source = {"outer": {"items": [{"detail": "x" * 980 + token}]}}

    preview = _bounded_diagnostic_value(source)

    encoded = repr(preview)
    assert token not in encoded
    assert "ghp_" not in encoded
    assert "[REDACTED" in encoded
    assert source["outer"]["items"][0]["detail"].endswith(token)


def test_bounded_diagnostic_value_preserves_canonical_structured_workflow_id() -> None:
    canonical = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.demo_workflow"
    dotted_token = "stripe.abcdefghijklmnopqrstuvwxyz123456"

    preview = _bounded_diagnostic_value(
        {
            "selected_workflow": canonical,
            "provider_debug": dotted_token,
        }
    )

    assert preview["selected_workflow"] == canonical
    assert dotted_token not in preview["provider_debug"]


@pytest.mark.parametrize(
    "comment",
    [
        "credential " + "ghp_" + ("q" * 36),  # pragma: allowlist secret
        ("x" * 980) + " " + "ghp_" + ("b" * 36),  # pragma: allowlist secret
    ],
)
def test_previous_decision_semantic_text_is_bounded_and_secret_safe(comment: str) -> None:
    decisions = extras._build_previous_decisions(
        approval_history=[
            {
                "stage": "Review",
                "approved": True,
                "comment": comment,
                "content_preview": comment,
                "timestamp": comment,
            }
        ],
        decision_overrides=[
            {
                "domain": "workflow",
                "created_at": comment,
                "created_by": "operator",
            }
        ],
        project_id="proj-42",
        job_id="42",
    )

    encoded = json.dumps(decisions)
    assert {decision["kind"] for decision in decisions} == {"approval", "decision_override"}
    approval = next(decision for decision in decisions if decision["kind"] == "approval")
    assert len(approval["summary"]) <= 700
    assert len(approval["detail"]) <= 700
    assert all(len(decision["timestamp"]) <= 80 for decision in decisions)
    assert not contains_detected_secret(encoded)


def test_previous_decision_actor_preserves_identity_but_redacts_credentials() -> None:
    decisions = extras._build_previous_decisions(
        approval_history=[
            {
                "stage": "Review",
                "approved": True,
                "actor": "alice@example.com",
            },
            {
                "stage": "Security review",
                "approved": False,
                "actor": "Bearer abcdefghijklmnopqrstuvwxyz012345",
            },
        ],
        decision_overrides=[],
    )

    assert decisions[0]["actor"] == "alice@example.com"
    assert decisions[0]["facts"][2] == {"label": "Actor", "value": "alice@example.com"}
    assert "abcdefghijklmnopqrstuvwxyz012345" not in decisions[1]["actor"]


def test_ui_projection_extras_builders_cover_branches() -> None:
    decisions = extras._build_previous_decisions(
        approval_history=[
            {
                "stage": "Architecture",
                "approved": False,
                "action": "reject",
                "question": "Need changes?",
                "comment": "Please revise",
                "timestamp": "2026-03-29T10:00:00Z",
                "actor_username": "alice",
                "selected_stories": ["S-1", "S-2"],
            }
        ],
        decision_overrides=[
            {
                "domain": "workflow",
                "original_value": {"id": "wf-old"},
                "override_value": {"id": "wf-new"},
                "active": True,
                "created_at": "2026-03-29T10:05:00Z",
                "created_by": "operator",
                "reason": "incident mitigation",
            }
        ],
    )
    assert len(decisions) == 2
    assert decisions[0]["kind"] in {"approval", "decision_override"}
    assert any(item["kind"] == "approval" for item in decisions)
    assert any(item["kind"] == "decision_override" for item in decisions)

    job = {"id": 91, "project_id": 12, "thread_id": "th-1"}
    runtime = {
        "selected_workflow": "wf-main",
        "current_step_id": "dev",
        "current_group_id": "release",
        "active_agent": "coder",
        "wait_reason": "approval_required",
        "retry_count": 2,
        "last_routing_decision": {
            "title": "route to review",
            "timestamp": "2026-03-29T10:10:00Z",
            "event_type": "route",
        },
        "last_structured_error": {
            "source": "security",
            "category": "policy_resolution",
            "error_message": "policy resolution failed",
            "context_jsonb": {"mode": "strict"},
        },
    }
    approval_gate = {"stage": "Final Delivery Review", "message": "Approve release?"}
    causal_chain = {"nodes": [{"title": "Plan", "summary": "Planning complete"}]}
    job_errors = [
        {
            "source": "security",
            "category": "policy_resolution",
            "error_message": "blocked by policy",
            "context_jsonb": {"mode": "strict"},
        }
    ]
    messages = [{"step_id": "dev", "from_agent": "designer", "content": "handoff"}]
    events = [{"id": 55, "rule_id": "policy:resolution_error", "redacted_preview": "policy failed"}]

    diagnostics = extras._build_diagnostics(
        job=job,
        runtime=runtime,
        approval_gate=approval_gate,
        causal_chain=causal_chain,
        job_errors=job_errors,
        messages=messages,
        events=events,
    )
    facts = {fact["key"]: fact["value"] for fact in diagnostics["facts"]}
    assert facts["Current step"] == "Dev"
    assert facts["Current group"] == "Release"
    assert diagnostics["causal_sequence"]
    assert diagnostics["evidence_cards"]
    assert diagnostics["raw_refs"]
    assert any(card.get("title") == "Security cause" for card in diagnostics["evidence_cards"])


@pytest.mark.parametrize("status", ["awaiting_timer", "awaiting_child"])
def test_a_step_paused_on_a_timer_or_child_is_in_progress_not_done(status: str) -> None:
    assert _map_step_status(status) == "in_progress"
    assert _map_step_status("awaiting_approval") == "awaiting_review"
