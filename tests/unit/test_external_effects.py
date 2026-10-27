from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _external_effect_identity_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-external-effect-identity-root")


def test_external_effect_scope_is_account_and_test_mode_scoped():
    from core.application.extensions.external_effects import external_effect_scope_hash

    base = {
        "project_id": "project_1",
        "extension_id": "example.host-fixture",
        "operation_id": "publish_record",
        "connection_id": "conn_external",
    }

    assert external_effect_scope_hash(account_id="acc_a", test_mode=False, **base) != (
        external_effect_scope_hash(account_id="acc_b", test_mode=False, **base)
    )
    assert external_effect_scope_hash(account_id="acc_a", test_mode=True, **base) != (
        external_effect_scope_hash(account_id="acc_a", test_mode=False, **base)
    )


def test_external_effect_fingerprint_keeps_provider_visible_retry_named_fields():
    from core.application.extensions.external_effects import build_external_effect_identity

    common = {
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "example.host-fixture",
        "operation_id": "publish_record",
        "connection_id": "conn_external",
        "source_ref": {"workflow_id": "wf_1", "job_id": "job_1", "step_id": "step_1"},
        "test_mode": False,
    }
    first = build_external_effect_identity(
        **common,
        request_payload={
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi",
            "retry_attempt": 1,
            "invocation_attempt_id": "inv_1",
        },
    )
    second = build_external_effect_identity(
        **common,
        request_payload={
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi",
            "retry_attempt": 2,
            "invocation_attempt_id": "inv_2",
        },
    )

    assert first["request_fingerprint"] != second["request_fingerprint"]
    assert first["idempotency_key"] != second["idempotency_key"]


def test_external_effect_identity_hashes_provider_visible_idempotency_key():
    from core.application.extensions.external_effects import build_external_effect_identity

    common = {
        "account_id": "acc_a",
        "project_id": "project_1",
        "extension_id": "example.host-fixture",
        "operation_id": "publish_record",
        "connection_id": "conn_external",
        "source_ref": {"workflow_id": "wf_1", "job_id": "job_1", "step_id": "step_1"},
        "test_mode": False,
    }
    first = build_external_effect_identity(
        **common,
        request_payload={
            "idempotency_key": "caller-key",
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi Alice",
        },
    )
    second = build_external_effect_identity(
        **common,
        request_payload={
            "idempotency_key": "caller-key",
            "record_id": ["record-b"],
            "title": "Hello",
            "content": "Hi Bob",
        },
    )
    same_send_different_caller_key = build_external_effect_identity(
        **common,
        request_payload={
            "idempotency_key": "different-caller-key",
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi Alice",
        },
    )

    assert first["idempotency_key"] != "caller-key"
    assert first["idempotency_key"] != second["idempotency_key"]
    assert first["request_fingerprint"] != same_send_different_caller_key["request_fingerprint"]
    assert first["idempotency_key"] != same_send_different_caller_key["idempotency_key"]


def test_connector_external_effect_identity_source_ref_is_stable():
    from core.application.extensions.external_effects import build_external_effect_identity
    from core.application.orchestration.connector_effects import _source_ref_from_runtime_context

    source_ref = _source_ref_from_runtime_context(
        {
            "workflow_id": "wf_1",
            "job_id": "job_1",
            "group_id": "customers",
            "step_id": "publish_record",
            # Neither the execution nor the attempt is part of the effect's scope.
            "step_execution_id": "0b7a2f7e-2a49-4e0c-8f6d-7f0f6a1d2c3b",
            "attempt": 2,
            "loop": {"index": 2, "iteration_key": "customer:2"},
        }
    )

    assert source_ref == {
        "workflow_id": "wf_1",
        "job_id": "job_1",
        "group_id": "customers",
        "step_id": "publish_record",
        "loop": {"index": 2, "iteration_key": "customer:2"},
    }
    identity = build_external_effect_identity(
        account_id="acc_alpha",
        project_id="project_alpha",
        extension_id="example.host-fixture",
        operation_id="publish_record",
        connection_id="conn_external",
        source_ref=source_ref,
        request_payload={
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi",
        },
        test_mode=False,
    )

    assert identity["idempotency_key"].startswith("hmac-sha256:")
    assert identity == build_external_effect_identity(
        account_id="acc_alpha",
        project_id="project_alpha",
        extension_id="example.host-fixture",
        operation_id="publish_record",
        connection_id="conn_external",
        source_ref=source_ref,
        request_payload={
            "record_id": ["record-a"],
            "title": "Hello",
            "content": "Hi",
        },
        test_mode=False,
    )


def test_notification_external_effect_identity_source_ref_is_stable():
    from core.application.extensions.external_effects import build_external_effect_identity
    from core.application.notifications.delivery import _notification_external_effect_source_ref

    source_ref = _notification_external_effect_source_ref(
        {
            "event_id": "evt_1",
            "correlation_id": "corr_1",
            "workflow_id": "wf_1",
            "job_id": "job_1",
        },
        {"route_id": "route_1", "route_key": "critical-notifications"},
    )

    assert source_ref == {
        "event_id": "evt_1",
        "correlation_id": "corr_1",
        "workflow_id": "wf_1",
        "job_id": "job_1",
        "route_id": "route_1",
        "route_key": "critical-notifications",
    }
    identity = build_external_effect_identity(
        account_id="acc_alpha",
        project_id="project_alpha",
        extension_id="example.host-fixture",
        operation_id="deliver_notification",
        connection_id="conn_external",
        source_ref=source_ref,
        request_payload={
            "event": {"event_type": "job.failed"},
            "action_payload": {"target": {"connection": {"connection_id": "conn_external"}}},
        },
        test_mode=False,
    )

    assert identity["idempotency_key"].startswith("hmac-sha256:")


def test_claim_external_effect_suppresses_test_mode(monkeypatch):
    from core.application.extensions import external_effects

    claimed = {
        "effect_id": "eff_1",
        "status": "claimed",
        "claim_token": "lease-1",
        "idempotency_key": "idem_1",
        "idempotency_scope_hash": "scope_1",
    }
    suppressed = {**claimed, "status": "suppressed"}
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        lambda *args, **kwargs: (claimed, True),
    )
    update = MagicMock(return_value=suppressed)
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    result = external_effects.claim_external_effect(
        object(),
        declaration={
            "operation_id": "publish_record",
            "effect_kind": "message_delivery",
            "channel": "api",
            "test_mode_behavior": "suppress",
        },
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        connection_id="conn_external",
        operation_id="publish_record",
        source_type="workflow_step",
        source_ref={"job_id": "job_1"},
        request_payload={"record_id": "record-a", "title": "Hi"},
        test_mode=True,
    )

    assert result["should_spawn"] is False
    assert result["suppressed"] is True
    assert result["effect"]["status"] == "suppressed"
    assert update.call_args.kwargs["status"] == "suppressed"
    assert update.call_args.kwargs["claim_token"] == "lease-1"


def test_claim_external_effect_suppression_fails_closed_when_claim_fence_is_lost(
    monkeypatch,
):
    from core.application.extensions import external_effects

    claimed = {
        "effect_id": "eff_1",
        "status": "claimed",
        "claim_token": "lease-1",
    }
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        lambda *args, **kwargs: (claimed, True),
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="external_effect_claim_transition_lost"):
        external_effects.claim_external_effect(
            object(),
            declaration={
                "operation_id": "publish_record",
                "effect_kind": "message_delivery",
                "channel": "api",
                "test_mode_behavior": "suppress",
            },
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            connection_id="conn_external",
            operation_id="publish_record",
            source_type="workflow_step",
            source_ref={"job_id": "job_1"},
            request_payload={"record_id": "record-a"},
            test_mode=True,
        )


def test_direct_intent_key_conflicts_when_reused_with_changed_payload(monkeypatch):
    from core.application.extensions import external_effects

    stored = {
        "effect_id": "eff_1",
        "status": "succeeded",
        "request_fingerprint": "fingerprint-from-first-request",
    }
    claim = MagicMock(return_value=(stored, False))
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        claim,
    )

    result = external_effects.claim_external_effect(
        object(),
        declaration={"effect_kind": "message_delivery", "channel": "email"},
        account_id="acc_a",
        project_id="project_1",
        extension_id="fixture.mail",
        extension_install_id="install-1",
        connection_id="connection-1",
        operation_id="send",
        source_type="api_action",
        source_ref={"direct_action_intent_hash": "intent-digest"},
        request_payload={"subject": "changed payload"},
        idempotency_key_override="intent-digest",
    )

    assert claim.call_args.kwargs["idempotency_key"] == "intent-digest"
    assert claim.call_args.kwargs["request_fingerprint"] != stored["request_fingerprint"]
    assert result == {
        "effect": stored,
        "should_spawn": False,
        "suppressed": False,
        "deduped": True,
        "error": "external_effect_idempotency_conflict",
    }


def test_direct_intent_scope_does_not_change_with_project_or_connection() -> None:
    from core.application.extensions.external_effects import (
        direct_action_external_effect_scope_hash,
    )

    common = {
        "account_id": "account-1",
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "operation_id": "send",
        "test_mode": False,
    }
    first = direct_action_external_effect_scope_hash(**common)
    second = direct_action_external_effect_scope_hash(**common)

    assert first == second


def test_retryable_effect_is_reclaimed_only_for_the_same_request_fingerprint() -> None:
    from core.infrastructure.db.external_effect_store import claim_external_effect

    class Cursor:
        def __init__(self):
            self.description = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.sql = str(sql)
            self.params = params

        def fetchone(self):
            return None

    class Db:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

    db = Db()
    claim_external_effect(
        db,
        account_id="acc_a",
        project_id=None,
        extension_id="fixture.mail",
        extension_install_id="install-1",
        connection_id=None,
        operation_id="send",
        effect_kind="message_delivery",
        channel="email",
        source_type="api_action",
        source_ref_jsonb={},
        idempotency_key="intent-digest",
        idempotency_scope_hash="scope-digest",
        request_fingerprint="request-digest",
    )

    assert "external_effects.request_fingerprint = EXCLUDED.request_fingerprint" in db.cur.sql


def test_external_effect_uuid_hot_paths_use_typed_indexable_predicates() -> None:
    from core.infrastructure.db import external_effect_store

    class Cursor:
        def __init__(self) -> None:
            self.description = []
            self.calls: list[tuple[str, tuple]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.calls.append((str(sql), tuple(params)))

        def fetchone(self):
            return None

    cursor = Cursor()
    db = MagicMock()
    db.cursor.return_value = cursor
    effect_id = str(uuid4())
    account_id = str(uuid4())

    external_effect_store.get_external_effect(db, effect_id=effect_id)
    external_effect_store.get_external_effect_replay(
        db,
        effect_id=effect_id,
        account_id=account_id,
    )
    external_effect_store.update_external_effect_status(
        db,
        effect_id=effect_id,
        status="running",
    )

    assert "effect_id = %s::uuid" in cursor.calls[0][0]
    assert "effect_id = %s::uuid" in cursor.calls[1][0]
    assert "account_id = %s::uuid" in cursor.calls[1][0]
    assert "effect_id = %s::uuid" in cursor.calls[2][0]
    assert all("effect_id::text" not in sql for sql, _params in cursor.calls)
    assert all("account_id::text" not in sql for sql, _params in cursor.calls)

    call_count = len(cursor.calls)
    assert external_effect_store.get_external_effect(db, effect_id="not-a-uuid") is None
    assert (
        external_effect_store.get_external_effect_replay(
            db,
            effect_id=effect_id,
            account_id="not-a-uuid",
        )
        is None
    )
    assert (
        external_effect_store.update_external_effect_status(
            db,
            effect_id="not-a-uuid",
            status="failed",
        )
        is None
    )
    assert len(cursor.calls) == call_count


def test_external_effect_running_and_confirmation_pin_claim_token(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "running"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "get_external_effect",
        lambda *_args, **_kwargs: {
            "effect_id": "eff_1",
            "account_id": "acc_1",
            "extension_id": "fixture.publisher",
            "operation_id": "publish_record",
        },
    )
    confirm = MagicMock(return_value={"effect_id": "eff_1", "status": "succeeded"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        confirm,
    )
    from core.application.extensions import external_effect_replay

    monkeypatch.setattr(
        external_effect_replay,
        "encrypt_projected_replay",
        lambda *_args, **_kwargs: {
            "ciphertext": "v1:ciphertext",
            "binding_hash": "binding-hash",
            "plaintext_bytes": 42,
        },
    )

    external_effects.mark_external_effect_running(
        object(), effect_id="eff_1", claim_token="lease-1"
    )
    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        claim_token="lease-1",
        account_id="acc_1",
        operation_id="publish_record",
        extension={
            "extension_key": "fixture.publisher",
            "operation_manifest": [
                {"operation_id": "publish_record", "outputs": [{"name": "receipt"}]},
            ],
        },
        invocation={"status": "succeeded", "response": {"ok": True}},
    )

    assert update.call_args.kwargs["claim_token"] == "lease-1"
    assert confirm.call_args.kwargs["claim_token"] == "lease-1"
    assert confirm.call_args.kwargs["account_id"] == "acc_1"


@pytest.mark.parametrize(
    "extension",
    [
        {
            "external_effects": [
                {"operation_id": "send", "effect_kind": "message_delivery"},
                {"operation_id": "send", "effect_kind": "message_delivery"},
            ]
        },
        {
            "external_effects": [{"operation_id": "send", "effect_kind": "message_delivery"}],
            "compiled_manifest": {
                "external_effects": [{"operation_id": "send", "effect_kind": "record_mutation"}]
            },
        },
    ],
)
def test_external_effect_declaration_conflicts_fail_before_claim(
    monkeypatch,
    extension: dict,
) -> None:
    from core.application.extensions import external_effects

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)

    with pytest.raises(external_effects.ExternalEffectDeclarationInvalidError) as error:
        external_effects.claim_external_effect_for_action(
            object(),
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={},
        )

    assert error.value.code == "external_effect_declaration_invalid"
    claim.assert_not_called()


@pytest.mark.parametrize(
    "operation_manifest",
    [
        [],
        [
            {"operation_id": "send", "outputs": [{"name": "receipt"}]},
            {"operation_id": "send", "outputs": [{"name": "receipt"}]},
        ],
    ],
)
def test_invalid_replay_operation_contract_fails_before_claim(
    monkeypatch,
    operation_manifest: list[dict],
) -> None:
    from core.application.extensions import external_effects
    from core.application.extensions.external_effect_replay import (
        ExternalEffectReplayProjectionError,
    )

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)
    extension = {
        "external_effects": [{"operation_id": "send", "effect_kind": "message_delivery"}],
        "operation_manifest": operation_manifest,
    }

    with pytest.raises(ExternalEffectReplayProjectionError) as error:
        external_effects.claim_external_effect_for_action(
            object(),
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={},
        )

    assert error.value.code == "external_effect_replay_operation_contract_invalid"
    claim.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        {"operation_id": "send", "outputs": ["message_id"]},
        {"operation_id": "send", "outputs": [{"name": ""}]},
        {
            "operation_id": "send",
            "outputs": [{"name": "message_id"}, {"name": "message_id"}],
        },
        {
            "operation_id": "send",
            "outputs": [{"name": "message_id"}],
            "public_outputs": [{"name": "receipt"}],
        },
    ],
)
def test_malformed_replay_outputs_fail_before_claim(
    monkeypatch,
    operation: dict,
) -> None:
    from core.application.extensions import external_effects
    from core.application.extensions.external_effect_replay import (
        ExternalEffectReplayProjectionError,
    )

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)
    extension = {
        "external_effects": [{"operation_id": "send", "effect_kind": "message_delivery"}],
        "operation_manifest": [operation],
    }

    with pytest.raises(ExternalEffectReplayProjectionError):
        external_effects.claim_external_effect_for_action(
            object(),
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={},
        )

    claim.assert_not_called()


def test_conflicting_mirrored_replay_contract_fails_before_claim(monkeypatch) -> None:
    from core.application.extensions import external_effects
    from core.application.extensions.external_effect_replay import (
        ExternalEffectReplayProjectionError,
    )

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)
    extension = {
        "external_effects": [{"operation_id": "send", "effect_kind": "message_delivery"}],
        "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        "compiled_manifest": {
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}]
        },
    }

    with pytest.raises(ExternalEffectReplayProjectionError):
        external_effects.claim_external_effect_for_action(
            object(),
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={},
        )

    claim.assert_not_called()


@pytest.mark.parametrize(
    "reserved_output",
    ["commands", "events", "project_state_updates", "host_mutations", "session_update"],
)
def test_reserved_host_control_replay_output_fails_before_claim(
    monkeypatch,
    reserved_output: str,
) -> None:
    from core.application.extensions import external_effects
    from core.application.extensions.external_effect_replay import (
        ExternalEffectReplayProjectionError,
    )

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)
    extension = {
        "external_effects": [{"operation_id": "send", "effect_kind": "message_delivery"}],
        "operation_manifest": [{"operation_id": "send", "outputs": [{"name": reserved_output}]}],
    }

    with pytest.raises(ExternalEffectReplayProjectionError):
        external_effects.claim_external_effect_for_action(
            object(),
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={},
        )

    claim.assert_not_called()


def test_claim_action_preserves_typed_claim_transition_loss(monkeypatch) -> None:
    from core.application.extensions import external_effects

    monkeypatch.setattr(
        external_effects,
        "claim_external_effect",
        MagicMock(side_effect=external_effects.ExternalEffectClaimTransitionLostError()),
    )

    result = external_effects.claim_external_effect_for_action(
        MagicMock(),
        extension={
            "extension_install_id": "install-1",
            "external_effects": [{"operation_id": "send", "effect_kind": "delivery"}],
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        },
        account_id="account-1",
        project_id=None,
        extension_id="fixture.mail",
        extension_install_id="install-1",
        operation_id="send",
        source_type="direct_action",
        source_ref={},
        request_payload={},
    )

    assert result["error"] == "external_effect_claim_transition_lost"


def test_claim_action_uses_stable_generic_error_without_exception_detail(monkeypatch) -> None:
    from core.application.extensions import external_effects

    monkeypatch.setattr(
        external_effects,
        "claim_external_effect",
        MagicMock(side_effect=RuntimeError("private database endpoint and query detail")),
    )

    result = external_effects.claim_external_effect_for_action(
        MagicMock(),
        extension={
            "external_effects": [{"operation_id": "send", "effect_kind": "delivery"}],
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        },
        account_id="account-1",
        project_id=None,
        extension_id="fixture.mail",
        extension_install_id="install-1",
        operation_id="send",
        source_type="direct_action",
        source_ref={},
        request_payload={},
    )

    assert result["error"] == "external_effect_claim_failed"
    assert "private database" not in repr(result)


def test_external_effect_identity_keeps_provider_attempt_fields_but_ignores_host_retry_counter():
    from core.application.extensions.external_effects import build_external_effect_identity

    common = {
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.mail",
        "operation_id": "send",
        "connection_id": "connection-1",
        "test_mode": False,
    }
    first = build_external_effect_identity(
        **common,
        source_ref={"job_id": "job-1", "step_id": "send", "retry_attempt": 1},
        request_payload={"attempt": 1, "message": "hello"},
    )
    host_retry = build_external_effect_identity(
        **common,
        source_ref={"job_id": "job-1", "step_id": "send", "retry_attempt": 2},
        request_payload={"attempt": 1, "message": "hello"},
    )
    changed_provider_input = build_external_effect_identity(
        **common,
        source_ref={"job_id": "job-1", "step_id": "send", "retry_attempt": 2},
        request_payload={"attempt": 2, "message": "hello"},
    )

    assert first == host_retry
    assert first["request_fingerprint"] != changed_provider_input["request_fingerprint"]
    assert first["idempotency_key"] != changed_provider_input["idempotency_key"]


def test_external_effect_identity_uses_keyed_hmac_for_low_entropy_secret(
    monkeypatch,
) -> None:
    import hashlib
    import json

    from core.application.extensions.external_effects import build_external_effect_identity

    monkeypatch.setenv("ENCRYPTION_KEY", "external-effect-identity-root")
    common = {
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.mail",
        "operation_id": "send",
        "connection_id": "connection-1",
        "source_ref": {"job_id": "job-1", "step_id": "send"},
        "test_mode": False,
    }
    first = build_external_effect_identity(
        **common,
        request_payload={"password": "1234"},
    )
    retry = build_external_effect_identity(
        **common,
        request_payload={"password": "1234"},
    )
    changed = build_external_effect_identity(
        **common,
        request_payload={"password": "5678"},
    )
    unkeyed_guess = hashlib.sha256(
        json.dumps(
            {
                **{key: value for key, value in common.items() if key != "test_mode"},
                "request_payload": {"password": "1234"},
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    assert first == retry
    assert first["request_fingerprint"].startswith("hmac-sha256:")
    assert first["request_fingerprint"] != changed["request_fingerprint"]
    assert first["request_fingerprint"] != unkeyed_guess

    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        build_external_effect_identity(
            **common,
            request_payload={"password": "1234"},
        )


def test_external_effect_scope_identities_are_keyed_and_domain_separated(monkeypatch) -> None:
    from core.application.extensions.external_effects import (
        direct_action_external_effect_scope_hash,
        external_effect_scope_hash,
    )

    common = {
        "account_id": "account-1",
        "extension_id": "fixture.mail",
        "operation_id": "send",
        "extension_install_id": "install-1",
        "test_mode": False,
    }
    monkeypatch.setenv("ENCRYPTION_KEY", "scope-root-a")
    general_a = external_effect_scope_hash(
        **common,
        project_id="project-1",
        connection_id="connection-1",
    )
    direct_a = direct_action_external_effect_scope_hash(**common)

    monkeypatch.setenv("ENCRYPTION_KEY", "scope-root-b")
    general_b = external_effect_scope_hash(
        **common,
        project_id="project-1",
        connection_id="connection-1",
    )
    direct_b = direct_action_external_effect_scope_hash(**common)

    assert general_a.startswith("hmac-sha256:")
    assert direct_a.startswith("hmac-sha256:")
    assert general_a != direct_a
    assert general_a != general_b
    assert direct_a != direct_b


def test_claim_external_effect_reads_manifest_declared_observability_metadata(monkeypatch):
    from core.application.extensions import external_effects

    captured: dict = {}

    def fake_claim(*args, **kwargs):
        captured.update(kwargs)
        return {"effect_id": "eff_1", "status": "claimed"}, True

    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        fake_claim,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "consume_external_effect_rate_limits",
        lambda *args, **kwargs: {"allowed": True},
    )

    result = external_effects.claim_external_effect(
        object(),
        declaration={
            "operation_id": "deliver_notification",
            "effect_kind": "message_delivery",
            "channel": "api",
            "observability": {
                "target_fields": ["targets"],
                "target_kind": "endpoint",
                "target_label": "endpoint",
                "artifact_field": "artifacts",
                "payload_size_field": "payload_bytes",
            },
        },
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        connection_id="conn_external",
        operation_id="deliver_notification",
        source_type="notification_event",
        source_ref={"event_id": "evt_1", "route_id": "route_1"},
        request_payload={
            "action": "deliver_notification",
            "action_payload": {"target": {"connection": {"connection_id": "conn_external"}}},
            "route": {
                "route_id": "route_1",
                "route_config": {
                    "targets": ["target-a", "target-b"],
                    "artifacts": [
                        {
                            "artifact_id": "artifact_pdf",
                            "filename": "report.pdf",
                            "size_bytes": 2048,
                            "sha256": "abc123",
                        }
                    ],
                    "payload_bytes": 4096,
                },
            },
        },
        test_mode=False,
    )

    assert result["should_spawn"] is True
    assert captured["target_count"] == 2
    assert captured["target_summary_redacted"] == "2 endpoints"
    assert len(captured["target_hashes_jsonb"]) == 2
    assert captured["artifact_count"] == 1
    assert captured["artifact_total_bytes"] == 2048
    assert captured["estimated_payload_bytes"] == 4096
    assert captured["artifact_refs_jsonb"] == [
        {
            "artifact_id": "artifact_pdf",
            "filename": "report.pdf",
            "size_bytes": 2048,
            "sha256": "abc123",
        }
    ]


def test_external_effect_target_hashes_are_account_scoped_hmacs(monkeypatch):
    from core.application.extensions import external_effect_contracts, external_effects

    monkeypatch.setattr(external_effect_contracts, "SECURITY_PII_SALT", "test-pii-salt")

    first = external_effects._target_hashes(
        account_id="acc_a",
        payload={"target_ids": "record-a"},
        observability={"target_fields": ["target_ids"], "target_kind": "remote_record"},
    )
    repeated = external_effects._target_hashes(
        account_id="acc_a",
        payload={"target_ids": "record-a"},
        observability={"target_fields": ["target_ids"], "target_kind": "remote_record"},
    )
    other_account = external_effects._target_hashes(
        account_id="acc_b",
        payload={"target_ids": "record-a"},
        observability={"target_fields": ["target_ids"], "target_kind": "remote_record"},
    )

    assert first == repeated
    assert first != other_account
    assert first[0]["kind"] == "remote_record"
    assert "hmac_sha256" in first[0]
    assert "sha256" not in first[0]


def test_external_effect_does_not_infer_undeclared_provider_payload_fields():
    from core.application.extensions import external_effects

    assert (
        external_effects._target_hashes(
            account_id="acc_a",
            payload={"provider_target": "provider-owned-value"},
            observability={},
        )
        == []
    )
    assert (
        external_effects._artifact_refs(
            {"provider_artifacts": [{"artifact_id": "provider-owned-artifact"}]},
            {},
        )
        == []
    )


def test_external_effect_rate_limit_policy_cannot_loosen_host_defaults():
    from core.application.extensions.external_effects import external_effect_rate_limit_policy

    assert external_effect_rate_limit_policy(
        {
            "rate_limit": {
                "window_seconds": 300,
                "account_max": 1000,
                "project_max": 1000,
                "connection_max": 1000,
            }
        }
    ) == {
        "window_seconds": 300,
        "account_max": 120,
        "project_max": 60,
        "connection_max": 30,
    }


def test_external_effect_rate_limit_policy_rejects_shorter_manifest_window():
    from core.application.extensions.external_effects import external_effect_rate_limit_policy

    assert (
        external_effect_rate_limit_policy({"rate_limit": {"window_seconds": 1}})["window_seconds"]
        == 60
    )


def test_claim_external_effect_blocks_before_spawn_when_host_rate_limited(monkeypatch):
    from core.application.extensions import external_effects

    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        lambda *args, **kwargs: ({"effect_id": "eff_limited", "status": "claimed"}, True),
    )
    consume = MagicMock(
        return_value={"allowed": False, "scope_type": "connection", "retry_after_seconds": 24}
    )
    update = MagicMock(return_value={"effect_id": "eff_limited", "status": "failed"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "consume_external_effect_rate_limits",
        consume,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    result = external_effects.claim_external_effect(
        object(),
        declaration={
            "operation_id": "publish_record",
            "effect_kind": "message_delivery",
            "channel": "api",
        },
        account_id="acc_a",
        project_id="project_1",
        extension_id="example.host-fixture",
        connection_id="conn_external",
        operation_id="publish_record",
        source_type="workflow_step",
        source_ref={"job_id": "job_1"},
        request_payload={"record_id": "record-a", "title": "Hi"},
    )

    assert result["should_spawn"] is False
    assert result["error"] == "external_effect_rate_limited"
    assert result["retry_after_seconds"] == 24
    assert [scope["scope_type"] for scope in consume.call_args.kwargs["scopes"]] == [
        "account",
        "project",
        "connection",
    ]
    assert update.call_args.kwargs["status"] == "failed"
    assert update.call_args.kwargs["error_code"] == "external_effect_rate_limited"
    assert (
        update.call_args.kwargs["response_summary_jsonb"]["definitely_no_external_effect"] is True
    )


def test_claim_external_effect_rate_limit_fails_closed_when_claim_fence_is_lost(monkeypatch):
    from core.application.extensions import external_effects

    monkeypatch.setattr(
        external_effects.external_effect_store,
        "claim_external_effect",
        lambda *args, **kwargs: (
            {"effect_id": "eff_limited", "status": "claimed", "claim_token": "lease-1"},
            True,
        ),
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "consume_external_effect_rate_limits",
        lambda *args, **kwargs: {
            "allowed": False,
            "scope_type": "connection",
            "retry_after_seconds": 24,
        },
    )
    update = MagicMock(return_value=None)
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    with pytest.raises(RuntimeError, match="external_effect_claim_transition_lost"):
        external_effects.claim_external_effect(
            object(),
            declaration={
                "operation_id": "publish_record",
                "effect_kind": "message_delivery",
                "channel": "api",
            },
            account_id="acc_a",
            project_id="project_1",
            extension_id="example.host-fixture",
            connection_id="conn_external",
            operation_id="publish_record",
            source_type="workflow_step",
            source_ref={"job_id": "job_1"},
            request_payload={"record_id": "record-a"},
        )

    assert update.call_args.kwargs["expected_current_status"] == "claimed"


def test_rate_limit_store_locks_every_scope_before_consuming_counters():
    from core.infrastructure.db.external_effect_store import consume_external_effect_rate_limits

    class _Cursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.calls.append((str(sql), params))

        def fetchall(self):
            return []

    class _Db:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def cursor(self):
            return self.cursor_instance

    db = _Db()
    result = consume_external_effect_rate_limits(
        db,
        window_seconds=60,
        scopes=[
            {
                "scope_hash": "scope-project",
                "scope_type": "project",
                "account_id": "acc_a",
                "project_id": "project_a",
                "effect_kind": "message_delivery",
                "channel": "api",
                "max_effects": 30,
            },
            {
                "scope_hash": "scope-account",
                "scope_type": "account",
                "account_id": "acc_a",
                "effect_kind": "message_delivery",
                "channel": "api",
                "max_effects": 60,
            },
        ],
    )

    assert result["allowed"] is True
    calls = db.cursor_instance.calls
    assert all("pg_advisory_xact_lock" in calls[idx][0] for idx in range(2))
    assert "SELECT scope_hash, effect_count" in calls[2][0]
    assert sum("INSERT INTO external_effect_rate_windows" in sql for sql, _ in calls) == 2


def test_rate_limit_store_pins_one_transaction_connection_for_the_whole_decision():
    from core.infrastructure.db.external_effect_store import consume_external_effect_rate_limits

    class _Cursor:
        def __init__(self, connection) -> None:
            self.connection = connection
            self.calls: list[tuple[str, object]] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            assert self.connection.autocommit is False
            self.calls.append((str(sql), params))

        def fetchall(self):
            return []

    class _Connection:
        def __init__(self) -> None:
            self.autocommit = True
            self.cursor_instance = _Cursor(self)
            self.commits = 0
            self.rollbacks = 0

        def cursor(self):
            return self.cursor_instance

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    class _Db:
        def __init__(self) -> None:
            self.conn = _Connection()

        def cursor(self):
            raise AssertionError("rate limiter must use the pinned connection")

    db = _Db()
    result = consume_external_effect_rate_limits(
        db,
        window_seconds=60,
        scopes=[
            {
                "scope_hash": "scope-account",
                "scope_type": "account",
                "account_id": "acc_a",
                "effect_kind": "message_delivery",
                "channel": "api",
                "max_effects": 1,
            }
        ],
    )

    assert result["allowed"] is True
    assert db.conn.commits == 1
    assert db.conn.rollbacks == 0
    assert db.conn.autocommit is True


def test_cleanup_stale_external_effects_scopes_replay_retention_to_account():
    from core.infrastructure.db.external_effect_store import cleanup_stale_external_effects

    class _Cursor:
        def __init__(self) -> None:
            self.rowcount = 0
            self.calls: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            self.calls.append(str(sql))
            self.params.append(params)
            self.rowcount = [2, 3, 4, 5][len(self.calls) - 1]

    class _Db:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def cursor(self):
            return self.cursor_instance

    db = _Db()
    db.cursor_instance.params = []
    result = cleanup_stale_external_effects(db, account_id="acc_a")

    assert result == {
        "expired": 2,
        "timeout_unknown": 3,
        "rate_windows_deleted": 4,
        "replay_payloads_expired": 5,
        "detached_effects_deleted": 0,
    }
    assert "DELETE FROM external_effect_rate_windows" in db.cursor_instance.calls[2]
    assert "UPDATE external_effect_replays replay" in db.cursor_instance.calls[3]
    assert "replay_reason = 'retention_expired'" in db.cursor_instance.calls[3]
    assert "action_logs_days" in db.cursor_instance.calls[3]
    assert "replay.account_id = %s" in db.cursor_instance.calls[3]
    assert "replay.updated_at" in db.cursor_instance.calls[3]
    assert "FROM external_effects" not in db.cursor_instance.calls[3]
    assert db.cursor_instance.params[3] == ("acc_a", "acc_a")


def test_claim_action_does_not_run_control_plane_maintenance(monkeypatch) -> None:
    from core.application.extensions import external_effects

    db = MagicMock()
    cleanup = MagicMock(return_value={})
    monkeypatch.setattr(external_effects, "cleanup_stale_external_effects", cleanup)
    monkeypatch.setattr(
        external_effects,
        "claim_external_effect",
        MagicMock(
            return_value={
                "effect": {"effect_id": "effect-1", "status": "claimed"},
                "should_spawn": True,
            }
        ),
    )

    external_effects.claim_external_effect_for_action(
        db,
        extension={
            "external_effects": [{"operation_id": "send", "effect_kind": "delivery"}],
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        },
        account_id="account-a",
        project_id=None,
        extension_id="fixture.mail",
        extension_install_id="install-1",
        operation_id="send",
        source_type="direct_action",
        source_ref={},
        request_payload={},
    )

    cleanup.assert_not_called()


def test_confirm_external_effect_uses_timeout_unknown_for_ambiguous_failure(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={"status": "failed", "detail": "process timeout", "response": {}},
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"


def test_confirm_external_effect_marks_project_state_commit_error_timeout_unknown(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "detail": "project_state_revision_conflict",
            "errors": [
                {
                    "code": "project_state_revision_conflict",
                    "message": "revision changed",
                }
            ],
            "response": {"ok": True},
        },
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["error_code"] == "project_state_revision_conflict"
    assert update.call_args.kwargs["error_text"] == "project_state_revision_conflict"


def test_confirm_external_effect_project_state_error_overrides_explicit_success(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "detail": "project_state_revision_conflict",
            "errors": [{"code": "project_state_revision_conflict"}],
            "response": {
                "external_effect_status": "succeeded",
                "ok": True,
            },
        },
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["status_reason"] == "project_state_revision_conflict"
    assert update.call_args.kwargs["error_code"] == "project_state_revision_conflict"


def test_confirm_external_effect_marks_terminal_project_state_errors_no_retry(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "detail": "project_state_write_undeclared",
            "errors": [
                {
                    "code": "project_state_write_undeclared",
                    "message": "write is not declared",
                }
            ],
            "response": {"ok": True},
        },
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["status_reason"] == "project_state_write_undeclared"
    assert update.call_args.kwargs["error_code"] == "project_state_write_undeclared"


def test_confirm_external_effect_stores_produced_grant_handles(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "succeeded"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "get_external_effect",
        lambda *_args, **_kwargs: {
            "effect_id": "eff_1",
            "account_id": "acc_1",
            "extension_id": "fixture.publisher",
            "operation_id": "publish_record",
        },
    )
    confirm = MagicMock(return_value={"effect_id": "eff_1", "status": "succeeded"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        confirm,
    )
    from core.application.extensions import external_effect_replay

    monkeypatch.setattr(
        external_effect_replay,
        "encrypt_projected_replay",
        lambda *_args, **_kwargs: {
            "ciphertext": "v1:ciphertext",
            "binding_hash": "binding-hash",
            "plaintext_bytes": 42,
        },
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        claim_token="lease-1",
        account_id="acc_1",
        operation_id="publish_record",
        extension={
            "extension_key": "fixture.publisher",
            "operation_manifest": [
                {
                    "operation_id": "publish_record",
                    "outputs": [{"name": "receipt"}],
                }
            ],
        },
        invocation={
            "status": "succeeded",
            "response": {"ok": True},
            "produced_artifacts": [
                {
                    "artifact_id": "art_previous",
                    "binding_key": "receipt",
                    "size_bytes": 42,
                    "sha256": "abc123",
                }
            ],
            "produced_datasets": [
                {
                    "dataset_id": "dst_previous",
                    "dataset_handle": "dataset:dst_previous",
                    "binding_key": "audit_rows",
                    "row_count": 3,
                }
            ],
            "grant_outputs": {
                "receipt": "artifact:art_previous",
                "audit_rows": "dataset:dst_previous",
            },
        },
        host_outputs={
            "produced_artifacts": [
                {
                    "artifact_id": "art_previous",
                    "binding_key": "receipt",
                    "size_bytes": 42,
                    "sha256": "abc123",
                }
            ],
            "produced_datasets": [
                {
                    "dataset_id": "dst_previous",
                    "dataset_handle": "dataset:dst_previous",
                    "binding_key": "audit_rows",
                    "row_count": 3,
                }
            ],
            "grant_outputs": {
                "receipt": "artifact:art_previous",
                "audit_rows": "dataset:dst_previous",
            },
        },
    )

    summary = confirm.call_args.kwargs["response_summary_jsonb"]
    assert summary["produced_artifacts"] == [
        {
            "artifact_id": "art_previous",
            "artifact_handle": "artifact:art_previous",
            "binding_key": "receipt",
            "size_bytes": 42,
            "sha256": "abc123",
        }
    ]
    assert summary["produced_datasets"] == [
        {
            "dataset_id": "dst_previous",
            "dataset_handle": "dataset:dst_previous",
            "binding_key": "audit_rows",
            "row_count": 3,
        }
    ]
    assert summary["grant_outputs"]["receipt"] == "artifact:art_previous"


def test_confirm_external_effect_can_record_definitive_failed_no_effect(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "failed"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "response": {"definitely_no_external_effect": True, "error_code": "invalid_input"},
        },
    )

    assert update.call_args.kwargs["status"] == "failed"
    assert update.call_args.kwargs["error_code"] == "invalid_input"


def test_confirm_external_effect_keeps_top_level_effect_metadata_with_nested_result(
    monkeypatch,
):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "failed"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "response": {
                "external_effect_status": "failed",
                "definitely_no_external_effect": True,
                "status_reason": "provider_rejected_before_send",
                "error_code": "invalid_input",
                "result": {
                    "provider_message_id": "provider-message-1",
                    "external_object_id": "provider-object-1",
                    "receipt": {"accepted": False},
                },
            },
        },
    )

    assert update.call_args.kwargs["status"] == "failed"
    assert update.call_args.kwargs["status_reason"] == "provider_rejected_before_send"
    assert update.call_args.kwargs["error_code"] == "invalid_input"
    assert update.call_args.kwargs["provider_message_id"] == "provider-message-1"
    assert update.call_args.kwargs["external_object_id"] == "provider-object-1"


def test_confirm_external_effect_explicit_failed_requires_definitive_no_effect(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "failed",
            "response": {"external_effect_status": "failed", "error_code": "upstream_timeout"},
        },
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["error_code"] == "upstream_timeout"


def test_confirm_external_effect_conflicting_protocol_statuses_fail_closed(monkeypatch):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    confirm = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        confirm,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={
            "status": "succeeded",
            "response": {
                "external_effect_status": "succeeded",
                "result": {
                    "external_effect": {
                        "status": "failed",
                        "definitely_no_external_effect": True,
                    },
                    "message_id": "must-not-be-persisted",
                },
            },
        },
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["status_reason"] == "external_effect_metadata_conflict"
    assert update.call_args.kwargs["error_code"] == "external_effect_metadata_conflict"
    confirm.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        {"external_effect_status": "not-a-status"},
        {"external_effect_status": "suppressed"},
        {
            "external_effect_status": "timeout_unknown",
            "definitely_no_external_effect": True,
        },
        {"external_effect_status": "failed", "definitely_no_external_effect": "false"},
        {"external_effect_status": "failed", "definitely_no_external_effect": 1},
        {"external_effect_status": "failed", "definitely_no_external_effect": {}},
        {
            "external_effect_status": "failed",
            "external_effect": {"definitely_no_external_effect": True},
            "result": {
                "external_effect": {"definitely_no_external_effect": False},
            },
        },
    ],
)
def test_confirm_external_effect_invalid_protocol_metadata_fails_closed(
    monkeypatch,
    response,
):
    from core.application.extensions import external_effects

    update = MagicMock(return_value={"effect_id": "eff_1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    confirm = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        confirm,
    )

    external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="eff_1",
        invocation={"status": "succeeded", "response": response},
    )

    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["error_code"] == "external_effect_metadata_conflict"
    confirm.assert_not_called()


@pytest.mark.parametrize(
    "reference",
    [
        {},
        {
            "command_id": "issued-receipt",
            "effect_id": "effect-original",
            "fenced_version": 2,
            "approval_id": "ordinary-gate",
        },
        {"phase": "association", "effect_id": "effect-original", "claim_version": 1},
        {"phase": "issued"},
    ],
)
def test_bound_retry_never_falls_through_removed_declaration_to_spawn(reference):
    from core.application.extensions.external_effects import claim_external_effect_for_action

    common = {
        "extension": {
            "compiled_manifest": {
                "external_effects": [],
                "operation_manifest": [{"operation_id": "publish_record"}],
            }
        },
        "account_id": "account",
        "project_id": "project",
        "extension_id": "fixture.write",
        "extension_install_id": "install-original",
        "connection_id": "connection-original",
        "operation_id": "publish_record",
        "source_type": "workflow_step",
        "source_ref": {"workflow_id": "workflow", "job_id": "job", "step_id": "publish"},
        "request_payload": {"business_key": "record-a"},
    }
    # No database/mock is needed: this is the real early no-declaration branch
    # that must fail closed before it can confer authority to invoke a provider.
    bound = claim_external_effect_for_action(None, **common, resolution_ref=reference)
    assert bound["should_spawn"] is False
    assert bound.get("error")
    ordinary = claim_external_effect_for_action(None, **common)
    assert ordinary["declared"] is False
    assert ordinary["should_spawn"] is True
