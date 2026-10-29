from __future__ import annotations

import ast
import importlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core.application.extensions import external_effects, operation_effects
from core.application.extensions import operation_service as operation_runtime


@pytest.fixture(autouse=True)
def _external_effect_identity_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-external-effect-identity-root")


def _replay_module():
    return importlib.import_module("core.application.extensions.external_effect_replay")


def _codec_module():
    return importlib.import_module("core.infrastructure.security.external_effect_replay_codec")


def _extension() -> dict:
    return {
        "extension_key": "fixture.mail",
        "extension_install_id": "install-1",
        "operation_manifest": [
            {
                "operation_id": "send",
                "outputs": [
                    {"name": "message_id", "value_type": "string"},
                    {"name": "receipt", "value_type": "object"},
                ],
            }
        ],
    }


def _direct_effect_extension() -> dict:
    return {
        **_extension(),
        "external_effects": [
            {
                "operation_id": "send",
                "effect_kind": "message_delivery",
                "channel": "email",
                "idempotency": "required",
            }
        ],
    }


def _binding() -> dict[str, str]:
    return {
        "effect_id": "11111111-1111-1111-1111-111111111111",
        "account_id": "22222222-2222-2222-2222-222222222222",
        "project_id": "33333333-3333-3333-3333-333333333333",
        "extension_id": "fixture.mail",
        "extension_install_id": "install-1",
        "connection_id": "connection-1",
        "operation_id": "send",
        "idempotency_scope_hash": "scope-hash",
        "idempotency_key": "idempotency-key",
        "request_fingerprint": "request-fingerprint",
    }


def test_result_payload_prefers_nested_result_over_response_wrapper() -> None:
    invocation = {
        "status": "succeeded",
        "response": {
            "ok": True,
            "result": {
                "message_id": "provider-message-42",
                "external_object_id": "object-42",
            },
        },
    }

    assert external_effects._effect_result_payload(invocation) == {
        "message_id": "provider-message-42",
        "external_object_id": "object-42",
    }


@pytest.mark.parametrize(
    "invocation, expected",
    [
        (
            {
                "response": {
                    "external_effect": {
                        "status": "failed",
                        "definitely_no_external_effect": True,
                    },
                    "result": {"message_id": "business-result"},
                }
            },
            {"status": "failed", "definitely_no_external_effect": True},
        ),
        (
            {
                "response": {
                    "result": {
                        "external_effect": {
                            "status": "failed",
                            "definitely_no_external_effect": True,
                        },
                        "message_id": "business-result",
                    }
                }
            },
            {"status": "failed", "definitely_no_external_effect": True},
        ),
    ],
)
def test_result_payload_keeps_explicit_effect_metadata_first(
    invocation: dict,
    expected: dict,
) -> None:
    assert external_effects._effect_result_payload(invocation) == expected


def test_projection_keeps_declared_outputs_and_finalized_grants_only() -> None:
    replay = _replay_module()
    invocation = {
        "status": "succeeded",
        "detail": "provider accepted confidential note",
        "response": {
            "ok": True,
            "result": {
                "message_id": "provider-message-42",
                "receipt": {"accepted": True, "commands": "business-description"},
                "undeclared": "drop-me",
            },
        },
        "produced_artifacts": [
            {
                "artifact_id": "artifact-1",
                "binding_key": "receipt_file",
                "size_bytes": 42,
                "internal_path": "/never/replay",
            },
            {"filename": "malformed-without-id"},
        ],
        "produced_datasets": [
            {
                "dataset_id": "dataset-1",
                "binding_key": "audit_rows",
                "row_count": 2,
                "raw_rows": [{"secret": "never-replay"}],
            },
            "malformed",
        ],
        "grant_outputs": {"receipt_file": "artifact:artifact-1"},
        "commands": [{"command": "never-replay"}],
        "events": [{"event_type": "never-replay"}],
        "project_state_updates": [{"key": "never-replay"}],
        "host_mutations": [{"kind": "never-replay"}],
    }

    projected = replay.project_external_effect_replay(
        extension=_extension(),
        operation_id="send",
        invocation=invocation,
        protected_values=frozenset(),
        host_outputs={
            "produced_artifacts": invocation["produced_artifacts"],
            "produced_datasets": invocation["produced_datasets"],
            "grant_outputs": invocation["grant_outputs"],
        },
    )

    assert projected == {
        "status": "succeeded",
        "response": {
            "ok": True,
            "result": {
                "message_id": "provider-message-42",
                "receipt": {"accepted": True, "commands": "business-description"},
            },
        },
        "produced_artifacts": [
            {
                "artifact_id": "artifact-1",
                "artifact_handle": "artifact:artifact-1",
                "binding_key": "receipt_file",
                "size_bytes": 42,
            }
        ],
        "produced_datasets": [
            {
                "dataset_id": "dataset-1",
                "dataset_handle": "dataset:dataset-1",
                "binding_key": "audit_rows",
                "row_count": 2,
            }
        ],
        "grant_outputs": {"receipt_file": "artifact:artifact-1"},
    }
    assert not ({"commands", "events", "project_state_updates", "host_mutations"} & set(projected))


def test_projection_supports_top_level_outputs_and_nested_result_wins() -> None:
    replay = _replay_module()
    extension = {
        "compiled_manifest": {
            "operation_manifest": [
                {
                    "operation_id": "deliver_notification",
                    "outputs": [
                        {"name": "provider_message_id"},
                        {"name": "delivered"},
                    ],
                }
            ]
        }
    }

    top_level = replay.project_external_effect_replay(
        extension=extension,
        operation_id="deliver_notification",
        invocation={
            "status": "succeeded",
            "response": {"ok": True, "delivered": True, "provider_message_id": "acme-chat-1"},
        },
        protected_values=frozenset(),
    )
    assert top_level["response"] == {
        "ok": True,
        "delivered": True,
        "provider_message_id": "acme-chat-1",
    }

    both = replay.project_external_effect_replay(
        extension=extension,
        operation_id="deliver_notification",
        invocation={
            "status": "succeeded",
            "produced_artifacts": [{"artifact_id": "forged-top-level-artifact"}],
            "produced_datasets": [{"dataset_id": "forged-top-level-dataset"}],
            "grant_outputs": {"receipt": "artifact:forged-top-level-artifact"},
            "response": {
                "ok": True,
                "provider_message_id": "top-level",
                "result": {"provider_message_id": "nested", "delivered": False},
            },
        },
        protected_values=frozenset(),
    )
    assert both["response"] == {
        "ok": True,
        "provider_message_id": "top-level",
        "result": {"provider_message_id": "nested", "delivered": False},
    }


def test_projection_accepts_mirrored_installed_manifest_without_double_counting() -> None:
    replay = _replay_module()
    operation = {
        "operation_id": "send",
        "outputs": [{"name": "message_id", "value_type": "string"}],
    }
    projected = replay.project_external_effect_replay(
        extension={
            "operation_manifest": [operation],
            "compiled_manifest": {"operation_manifest": [dict(operation)]},
        },
        operation_id="send",
        invocation={"status": "succeeded", "response": {"message_id": "message-1"}},
        protected_values=frozenset(),
    )

    assert projected["response"] == {"message_id": "message-1"}


def test_projection_trusts_only_host_finalized_outputs_and_safe_success_protocol() -> None:
    replay = _replay_module()
    projected = replay.project_external_effect_replay(
        extension=_extension(),
        operation_id="send",
        invocation={
            "status": "succeeded",
            "response": {
                "ok": True,
                "error": "provider-controlled free text",
                "error_code": "provider-controlled-code",
                "message_id": "message-1",
                "produced_artifacts": [{"artifact_id": "forged-artifact"}],
                "produced_datasets": [{"dataset_id": "forged-dataset"}],
                "grant_outputs": {"receipt": "artifact:forged-artifact"},
            },
        },
        protected_values=frozenset(),
    )

    assert projected == {
        "status": "succeeded",
        "response": {"ok": True, "message_id": "message-1"},
    }


@pytest.mark.parametrize(
    "operation_manifest",
    [
        [],
        [
            {"operation_id": "send", "outputs": [{"name": "message_id"}]},
            {"operation_id": "send", "outputs": [{"name": "message_id"}]},
        ],
    ],
)
def test_projection_fails_closed_for_missing_or_duplicate_installed_operation(
    operation_manifest: list[dict],
) -> None:
    replay = _replay_module()
    _raises_input_322_1 = frozenset()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as error:
        replay.project_external_effect_replay(
            extension={"compiled_manifest": {"operation_manifest": operation_manifest}},
            operation_id="send",
            invocation={"status": "succeeded", "response": {"message_id": "message-1"}},
            protected_values=_raises_input_322_1,
        )
    assert error.value.code == "external_effect_replay_operation_contract_invalid"


@pytest.mark.parametrize("outputs", [None, []], ids=["missing", "empty"])
def test_successful_effect_requires_non_empty_declared_outputs_before_claim(
    monkeypatch,
    outputs: list[dict] | None,
) -> None:
    replay = _replay_module()
    operation = {"operation_id": "send"}
    if outputs is not None:
        operation["outputs"] = outputs
    extension = {
        "external_effects": [{"operation_id": "send", "effect_kind": "delivery"}],
        "operation_manifest": [operation],
    }

    _raises_input_346_1 = frozenset()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as projection_error:
        replay.project_external_effect_replay(
            extension=extension,
            operation_id="send",
            invocation={
                "status": "succeeded",
                "response": {"result": {"message_id": "must-not-be-dropped"}},
            },
            protected_values=_raises_input_346_1,
        )
    assert projection_error.value.code == "external_effect_replay_operation_contract_invalid"

    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)
    _raises_input_360_1 = object()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as claim_error:
        external_effects.claim_external_effect_for_action(
            _raises_input_360_1,
            extension=extension,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            extension_install_id="install-1",
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={"message_id": "must-not-be-dropped"},
        )
    assert claim_error.value.code == "external_effect_replay_operation_contract_invalid"
    claim.assert_not_called()


@pytest.mark.parametrize("install_id", [None, "", "   "])
def test_declared_external_effect_requires_install_identity_before_claim(
    monkeypatch,
    install_id: str | None,
) -> None:
    replay = _replay_module()
    claim = MagicMock()
    monkeypatch.setattr(external_effects.external_effect_store, "claim_external_effect", claim)

    _raises_input_386_1 = object()
    _raises_input_386_2 = _direct_effect_extension()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as error:
        external_effects.claim_external_effect_for_action(
            _raises_input_386_1,
            extension=_raises_input_386_2,
            account_id="account-1",
            project_id=None,
            extension_id="fixture.mail",
            extension_install_id=install_id,
            operation_id="send",
            source_type="direct_action",
            source_ref={},
            request_payload={"message": "hello"},
        )

    assert error.value.code == "external_effect_install_identity_required"
    claim.assert_not_called()


def test_projection_is_utf8_byte_bounded_and_fails_closed_on_secret_or_budget() -> None:
    replay = _replay_module()

    _raises_input_407_1 = _extension()
    _raises_input_407_2 = frozenset()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as utf8_error:
        replay.project_external_effect_replay(
            extension=_raises_input_407_1,
            operation_id="send",
            invocation={"status": "succeeded", "response": {"result": {"message_id": "🙂" * 40}}},
            protected_values=_raises_input_407_2,
            max_plaintext_bytes=100,
        )
    assert utf8_error.value.code == "external_effect_replay_payload_too_large"

    _raises_input_417_1 = _extension()
    _raises_input_417_2 = frozenset({"token-secret-value"})
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as secret_error:
        replay.project_external_effect_replay(
            extension=_raises_input_417_1,
            operation_id="send",
            invocation={
                "status": "succeeded",
                "response": {"result": {"message_id": "token-secret-value"}},
            },
            protected_values=_raises_input_417_2,
        )
    assert secret_error.value.code == "external_effect_replay_secret_exposure"

    for credential_field in ("access_token", "client_secret", "password"):
        _raises_input_430_1 = frozenset()
        with pytest.raises(replay.ExternalEffectReplayProjectionError) as credential_error:
            replay.project_external_effect_replay(
                extension={
                    "operation_manifest": [
                        {"operation_id": "send", "outputs": [{"name": "receipt"}]}
                    ]
                },
                operation_id="send",
                invocation={
                    "status": "succeeded",
                    "response": {
                        "result": {
                            "receipt": {
                                credential_field: "new-provider-credential",
                                "token_count": 3,
                            }
                        }
                    },
                },
                protected_values=_raises_input_430_1,
            )
        assert credential_error.value.code == "external_effect_replay_credential_field_forbidden"

    benign = replay.project_external_effect_replay(
        extension={
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}]
        },
        operation_id="send",
        invocation={
            "status": "succeeded",
            "response": {"result": {"receipt": {"token_count": 3}}},
        },
        protected_values=frozenset(),
    )
    assert benign["response"]["result"]["receipt"] == {"token_count": 3}

    _raises_input_466_1 = frozenset()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as pair_error:
        replay.project_external_effect_replay(
            extension={
                "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}]
            },
            operation_id="send",
            invocation={
                "status": "succeeded",
                "response": {"result": {"receipt": [["access_token", "new-provider-credential"]]}},
            },
            protected_values=_raises_input_466_1,
        )
    assert pair_error.value.code == "external_effect_replay_credential_field_forbidden"

    benign_pair = replay.project_external_effect_replay(
        extension={
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}]
        },
        operation_id="send",
        invocation={
            "status": "succeeded",
            "response": {"result": {"receipt": [["token_count", 3]]}},
        },
        protected_values=frozenset(),
    )
    assert benign_pair["response"]["result"]["receipt"] == [["token_count", 3]]

    nested: dict[str, object] = {"leaf": "ok"}
    for _ in range(70):
        nested = {"next": nested}
    _raises_input_496_1 = {
        **_extension(),
        "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
    }
    _raises_input_496_2 = frozenset()
    with pytest.raises(replay.ExternalEffectReplayProjectionError) as depth_error:
        replay.project_external_effect_replay(
            extension=_raises_input_496_1,
            operation_id="send",
            invocation={"status": "succeeded", "response": {"result": {"receipt": nested}}},
            protected_values=_raises_input_496_2,
        )
    assert depth_error.value.code == "external_effect_replay_payload_budget_exceeded"


def test_codec_uses_binding_as_aad_and_rejects_scope_swap() -> None:
    codec = _codec_module()
    master_key = b"m" * 32
    encoded = codec.encrypt_external_effect_replay(
        b'{"status":"succeeded"}',
        binding=_binding(),
        master_key=master_key,
    )

    assert (
        codec.decrypt_external_effect_replay(
            encoded,
            binding=_binding(),
            master_key=master_key,
        )
        == b'{"status":"succeeded"}'
    )
    _raises_input_526_1 = {**_binding(), "project_id": "different-project"}
    with pytest.raises(codec.ExternalEffectReplayCodecError):
        codec.decrypt_external_effect_replay(
            encoded, binding=_raises_input_526_1, master_key=master_key
        )


def test_confirm_requires_account_and_claim_token_and_is_atomic() -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")
    executed: list[tuple[str, tuple]] = []
    boundaries: list[str] = []

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple) -> None:
            executed.append((sql, params))

        def fetchone(self):
            return None

    connection = SimpleNamespace(cursor=lambda: Cursor())

    @contextmanager
    def transaction(_db):
        boundaries.append("enter")
        try:
            yield connection
        finally:
            boundaries.append("exit")

    original = store.transaction_connection
    original_register = store.register_transaction_after_commit
    store.transaction_connection = transaction
    store.register_transaction_after_commit = lambda _conn, _callback: True
    effect_id = str(uuid4())
    account_id = str(uuid4())
    try:
        _raises_input_571_1 = object()
        with pytest.raises(store.ExternalEffectReplayLeaseLostError) as error:
            store.confirm_external_effect_with_replay(
                _raises_input_571_1,
                effect_id=effect_id,
                account_id=account_id,
                claim_token="claim-1",
                status="succeeded",
                status_reason="succeeded",
                provider_message_id="message-1",
                external_object_id=None,
                error_code=None,
                error_text=None,
                response_summary_jsonb={"ok": True},
                replay_ciphertext="v1:ciphertext",
                replay_binding_hash="binding-hash",
                replay_plaintext_bytes=42,
            )
    finally:
        store.transaction_connection = original
        store.register_transaction_after_commit = original_register

    assert error.value.code == "external_effect_replay_lease_lost"
    assert boundaries == ["enter", "exit"]
    assert len(executed) == 1
    sql, params = executed[0]
    assert "effect_id = %s::uuid" in sql
    assert "account_id = %s::uuid" in sql
    assert "effect_id::text" not in sql
    assert "account_id::text" not in sql
    assert "claim_token = %s" in sql
    assert "status = 'running'" in sql
    assert "external_effect_replays" in sql
    assert "external_effects" in sql
    assert "reconciliation.claim_token = %s" in sql
    assert "reconciliation.claim_version = updated_effect.claim_version" in sql
    assert account_id in params
    assert "claim-1" in params


@pytest.mark.parametrize(
    ("failure_stage", "expected_outcome"),
    [
        ("statement", "rolled_back"),
        ("ambiguous_commit", "unknown"),
        ("post_commit", "committed"),
    ],
)
def test_store_classifies_replay_transaction_outcome(
    monkeypatch,
    failure_stage: str,
    expected_outcome: str,
) -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")
    callbacks: list = []

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _sql: str, _params: tuple) -> None:
            if failure_stage == "statement":
                raise RuntimeError("statement failed")

        def fetchone(self):
            return {"effect_id": str(uuid4()), "status": "succeeded"}

    connection = SimpleNamespace(cursor=lambda: Cursor())

    @contextmanager
    def transaction(_db):
        yield connection
        if failure_stage == "post_commit":
            callbacks[0]()
        raise RuntimeError("transaction exit failed")

    monkeypatch.setattr(store, "transaction_connection", transaction)
    monkeypatch.setattr(
        store,
        "register_transaction_after_commit",
        lambda _conn, callback: callbacks.append(callback) or True,
    )

    _raises_input_658_1 = object()
    _raises_input_658_2 = str(uuid4())
    _raises_input_658_3 = str(uuid4())
    with pytest.raises(store.ExternalEffectReplayCommitError) as error:
        store.confirm_external_effect_with_replay(
            _raises_input_658_1,
            effect_id=_raises_input_658_2,
            account_id=_raises_input_658_3,
            claim_token="claim-1",
            status="succeeded",
            status_reason="succeeded",
            provider_message_id=None,
            external_object_id=None,
            error_code=None,
            error_text=None,
            response_summary_jsonb={},
            replay_ciphertext="v1:ciphertext",
            replay_binding_hash="binding-hash",
            replay_plaintext_bytes=42,
        )

    assert error.value.commit_outcome == expected_outcome


@pytest.mark.parametrize("transaction_kind", ["managed", "raw"])
def test_store_rejects_ambient_replay_transaction_before_sql(
    monkeypatch,
    transaction_kind: str,
) -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")
    transaction = MagicMock()
    monkeypatch.setattr(
        store,
        "managed_transaction_connection",
        lambda _db: object() if transaction_kind == "managed" else None,
    )
    monkeypatch.setattr(
        store,
        "active_transaction_connection",
        lambda _db: object() if transaction_kind == "raw" else None,
    )
    monkeypatch.setattr(store, "transaction_connection", transaction)

    _raises_input_698_1 = object()
    _raises_input_698_2 = str(uuid4())
    _raises_input_698_3 = str(uuid4())
    with pytest.raises(store.ExternalEffectReplayCommitError) as error:
        store.confirm_external_effect_with_replay(
            _raises_input_698_1,
            effect_id=_raises_input_698_2,
            account_id=_raises_input_698_3,
            claim_token="claim-1",
            status="succeeded",
            status_reason="succeeded",
            provider_message_id=None,
            external_object_id=None,
            error_code=None,
            error_text=None,
            response_summary_jsonb={},
            replay_ciphertext="v1:ciphertext",
            replay_binding_hash="binding-hash",
            replay_plaintext_bytes=42,
        )

    assert error.value.commit_outcome == "unknown"
    transaction.assert_not_called()


def test_ambiguous_commit_recovers_only_through_exact_account_scoped_primary_reread(
    monkeypatch,
) -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")
    replay = _replay_module()
    effect = {
        **_binding(),
        "status": "running",
        "claim_token": "claim-1",
        "claim_version": 3,
    }
    monkeypatch.setattr(store, "get_external_effect", MagicMock(return_value=effect))
    monkeypatch.setattr(
        replay,
        "encrypt_projected_replay",
        MagicMock(
            return_value={
                "ciphertext": "v1:exact-ciphertext",
                "binding_hash": "exact-binding",
                "plaintext_bytes": 42,
            }
        ),
    )
    monkeypatch.setattr(
        store,
        "confirm_external_effect_with_replay",
        MagicMock(side_effect=store.ExternalEffectReplayCommitError(commit_outcome="unknown")),
    )
    reread = MagicMock(return_value={**effect, "status": "succeeded"})
    monkeypatch.setattr(store, "get_exact_committed_external_effect_replay", reread)
    update = MagicMock()
    monkeypatch.setattr(store, "update_external_effect_status", update)

    db = object()
    confirmed = external_effects.confirm_external_effect_from_invocation(
        db,
        effect_id=effect["effect_id"],
        claim_token="claim-1",
        account_id=effect["account_id"],
        operation_id="send",
        extension=_extension(),
        expected_project_id=effect["project_id"],
        expected_connection_id=effect["connection_id"],
        invocation={
            "status": "succeeded",
            "response": {"ok": True, "result": {"message_id": "message-1"}},
        },
    )

    assert confirmed
    assert confirmed["status"] == "succeeded"
    assert confirmed["__confirmed_replay_invocation"]["status"] == "succeeded"
    reread.assert_called_once_with(
        db,
        effect=effect,
        replay_ciphertext="v1:exact-ciphertext",
        replay_binding_hash="exact-binding",
        replay_plaintext_bytes=42,
    )
    update.assert_not_called()


def test_output_reconciliation_registers_only_minted_ids(monkeypatch) -> None:
    register = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "register_external_effect_output_reconciliation",
        register,
    )
    prepared = SimpleNamespace(
        artifact_output_ids=["caller-artifact", "minted-artifact"],
        dataset_output_ids=["caller-dataset", "minted-dataset"],
        minted_artifact_output_ids=["minted-artifact"],
        minted_dataset_output_ids=["minted-dataset"],
    )

    external_effects.register_external_effect_output_reconciliation(
        object(),
        effect_id="effect-1",
        account_id="account-1",
        claim_token="claim-1",
        prepared_grants=prepared,
    )

    assert register.call_args.kwargs["minted_artifact_ids"] == ["minted-artifact"]
    assert register.call_args.kwargs["minted_dataset_ids"] == ["minted-dataset"]


def test_output_cleanup_marker_callback_runs_before_sidecar_materialization(monkeypatch) -> None:
    from core.application.extensions import runtime_grants
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane

    plane = ArtifactDatasetPlane()
    observed: dict[str, list[str]] = {}
    monkeypatch.setattr(
        runtime_grants,
        "build_descriptor_for_invocation",
        lambda _plane, object_id, *, kind, **_kwargs: {
            f"{kind}_id": object_id,
            f"{kind}_handle": f"{kind}:{object_id}",
        },
    )

    def register_marker(prepared) -> None:
        observed["artifacts"] = list(prepared.minted_artifact_output_ids)
        observed["datasets"] = list(prepared.minted_dataset_output_ids)
        assert all(plane.get_artifact(value) is None for value in observed["artifacts"])
        assert all(plane.get_dataset(value) is None for value in observed["datasets"])

    prepared = runtime_grants.prepare_extension_grants(
        plane=plane,
        scope=None,
        audit={"account_id": "account-1", "project_id": "project-1"},
        extension_id="fixture.extension",
        artifact_output_specs=[{"filename": "result.json"}],
        dataset_output_specs=[{"schema": {"columns": ["id"]}}],
        before_materialize_outputs=register_marker,
    )

    assert observed["artifacts"] == prepared.minted_artifact_output_ids
    assert observed["datasets"] == prepared.minted_dataset_output_ids
    assert all(plane.get_artifact(value) is not None for value in observed["artifacts"])
    assert all(plane.get_dataset(value) is not None for value in observed["datasets"])


@pytest.mark.parametrize("kind", ["artifact", "dataset"])
def test_output_grant_sidecar_failure_is_fail_closed_before_grant_exposure(kind: str) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class SidecarWriteFails(InMemoryObjectStorage):
        def put(self, key: str, body: bytes, *, content_type: str = "application/octet-stream"):
            if "_metadata/" in key or key.endswith("/meta.json"):
                raise RuntimeError("sidecar unavailable")
            return super().put(key, body, content_type=content_type)

    storage = SidecarWriteFails()
    plane = ArtifactDatasetPlane(storage=storage)

    def _raises_operation_746():
        if kind == "artifact":
            plane.create_artifact(
                artifact_id="art_preallocated",
                owner_scope={"account_id": "account-1", "project_id": "project-1"},
                run_id="run-1",
                step_id="step-1",
                filename="result.json",
                content_type="application/json",
            )
        else:
            plane.create_dataset_grant(
                dataset_id="dst_preallocated",
                owner_scope={"account_id": "account-1", "project_id": "project-1"},
                run_id="run-1",
                step_id="step-1",
            )

    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        _raises_operation_746()

    restarted = ArtifactDatasetPlane(storage=storage)
    assert restarted.get_artifact("art_preallocated") is None
    assert restarted.get_dataset("dst_preallocated") is None


def test_reconciliation_attempt_identity_prevents_retry_overwrite_and_stale_resolve() -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple) -> None:
            executed.append((sql, params))

        def fetchone(self):
            return {"effect_id": str(uuid4())}

    db = SimpleNamespace(cursor=lambda: Cursor())
    effect_id = str(uuid4())
    account_id = str(uuid4())
    store.register_external_effect_output_reconciliation(
        db,
        effect_id=effect_id,
        account_id=account_id,
        claim_token="old-claim",
        minted_artifact_ids=["old-artifact"],
        minted_dataset_ids=[],
    )
    store.resolve_external_effect_output_reconciliation(
        db,
        effect_id=effect_id,
        account_id=account_id,
        claim_token="old-claim",
        claim_version=1,
    )

    register_sql, _ = executed[0]
    resolve_sql, resolve_params = executed[1]
    assert "ON CONFLICT (effect_id, account_id, claim_version)" in register_sql
    assert "claim_version = EXCLUDED.claim_version" not in register_sql
    assert "claim_token = %s" in resolve_sql
    assert "claim_version = %s" in resolve_sql
    assert resolve_params[-2:] == ("old-claim", 1)


def test_process_loss_reconciliation_is_account_scoped_and_deletes_exact_minted_ids(
    monkeypatch,
) -> None:
    store = external_effects.external_effect_store
    monkeypatch.setattr(
        store,
        "list_external_effect_output_reconciliations_for_cleanup",
        MagicMock(
            return_value=[
                {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "claim_version": 2,
                    "minted_artifact_ids_jsonb": ["artifact-1"],
                    "minted_dataset_ids_jsonb": ["dataset-1"],
                }
            ]
        ),
    )
    resolve = MagicMock(return_value=True)
    monkeypatch.setattr(store, "resolve_external_effect_output_reconciliation", resolve)
    plane = SimpleNamespace(
        delete_artifact=MagicMock(return_value=True),
        delete_dataset=MagicMock(return_value=True),
        get_artifact=MagicMock(return_value=None),
        get_dataset=MagicMock(return_value=None),
    )

    db = object()
    assert (
        external_effects.reconcile_external_effect_outputs(
            db,
            account_id="account-a",
            artifact_plane=plane,
        )
        == 1
    )
    plane.delete_artifact.assert_called_once_with("artifact-1")
    plane.delete_dataset.assert_called_once_with("dataset-1")
    resolve.assert_called_once_with(
        db,
        effect_id="effect-1",
        account_id="account-a",
        claim_token="claim-1",
        claim_version=2,
    )


def test_reconciliation_cleanup_can_claim_one_global_batch_across_over_100_accounts() -> None:
    store = external_effects.external_effect_store
    account_ids = [str(uuid4()) for _ in range(101)]
    rows = [
        {
            "effect_id": str(uuid4()),
            "account_id": account_id,
            "claim_token": f"claim-{index}",
            "claim_version": 1,
            "minted_artifact_ids_jsonb": [],
            "minted_dataset_ids_jsonb": [],
        }
        for index, account_id in enumerate(account_ids)
    ]
    executed: list[str] = []

    class Cursor:
        description = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, _params: tuple) -> None:
            executed.append(sql)

        def fetchall(self):
            return rows

    claimed = store.list_external_effect_output_reconciliations_for_cleanup(
        SimpleNamespace(cursor=lambda: Cursor()),
        account_id=None,
        limit=101,
    )

    assert {row["account_id"] for row in claimed} == set(account_ids)
    assert "ORDER BY reconciliation.updated_at" in executed[0]
    assert "FOR UPDATE OF reconciliation SKIP LOCKED" in executed[0]


def test_stale_external_effect_cleanup_is_account_scoped_for_effects_and_rate_windows() -> None:
    statements: list[tuple[str, tuple]] = []

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple = ()) -> None:
            statements.append((sql, params))

    external_effects.cleanup_stale_external_effects(
        SimpleNamespace(cursor=lambda: Cursor()),
        account_id="00000000-0000-0000-0000-000000000123",
    )

    for sql, params in statements[:3]:
        assert "account_id = %s::uuid" in sql
        assert params == ("00000000-0000-0000-0000-000000000123",)


def test_maintenance_account_discovery_covers_effects_without_output_markers() -> None:
    statements: list[str] = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, _params: tuple) -> None:
            statements.append(sql)

        def fetchall(self):
            return []

    external_effects.external_effect_store.list_external_effect_maintenance_accounts(
        SimpleNamespace(cursor=lambda: Cursor())
    )

    sql = statements[0]
    assert "FROM external_effects AS effect" in sql
    assert "effect.status IN ('claimed', 'running')" in sql
    assert "FROM external_effect_rate_windows" in sql
    assert "FROM external_effect_replays" in sql
    assert "FROM external_effect_output_reconciliations" in sql
    assert "LEFT JOIN external_effect_maintenance_cursors" in sql
    assert "ORDER BY cursor.last_attempted_at NULLS FIRST" in sql
    assert "INSERT INTO external_effect_maintenance_cursors" in sql
    assert "DO UPDATE SET last_attempted_at" in sql


def test_reconciliation_stays_pending_when_any_output_still_exists(monkeypatch) -> None:
    store = external_effects.external_effect_store
    monkeypatch.setattr(
        store,
        "list_external_effect_output_reconciliations_for_cleanup",
        MagicMock(
            return_value=[
                {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "claim_version": 2,
                    "minted_artifact_ids_jsonb": ["artifact-1"],
                    "minted_dataset_ids_jsonb": [],
                }
            ]
        ),
    )
    resolve = MagicMock()
    monkeypatch.setattr(store, "resolve_external_effect_output_reconciliation", resolve)
    plane = SimpleNamespace(
        delete_artifact=MagicMock(return_value=False),
        get_artifact=MagicMock(return_value={"artifact_id": "artifact-1"}),
    )

    assert (
        external_effects.reconcile_external_effect_outputs(
            object(), account_id="account-a", artifact_plane=plane
        )
        == 0
    )
    resolve.assert_not_called()


@pytest.mark.parametrize("kind", ["artifact", "dataset"])
def test_reconciliation_stays_pending_when_storage_lookup_is_unknown(
    monkeypatch, kind: str
) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class LookupFails(InMemoryObjectStorage):
        def get(self, key: str) -> bytes | None:
            raise OSError(f"temporary lookup failure for {key}")

    store = external_effects.external_effect_store
    row = {
        "effect_id": "effect-1",
        "claim_token": "claim-1",
        "claim_version": 2,
        "minted_artifact_ids_jsonb": ["art_missing"] if kind == "artifact" else [],
        "minted_dataset_ids_jsonb": ["dst_missing"] if kind == "dataset" else [],
    }
    monkeypatch.setattr(
        store,
        "list_external_effect_output_reconciliations_for_cleanup",
        MagicMock(return_value=[row]),
    )
    resolve = MagicMock(return_value=True)
    monkeypatch.setattr(store, "resolve_external_effect_output_reconciliation", resolve)

    plane = ArtifactDatasetPlane(storage=LookupFails())
    assert (
        external_effects.reconcile_external_effect_outputs(
            object(), account_id="account-a", artifact_plane=plane
        )
        == 0
    )
    resolve.assert_not_called()


def test_reconciliation_keeps_dataset_retryable_when_object_delete_fails(monkeypatch) -> None:
    from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
    from core.infrastructure.extension_runtime.object_storage import InMemoryObjectStorage

    class DatasetObjectDeleteFails(InMemoryObjectStorage):
        failed_key = ""

        def delete(self, key: str) -> bool:
            if key == self.failed_key:
                return False
            return super().delete(key)

    storage = DatasetObjectDeleteFails()
    plane = ArtifactDatasetPlane(storage=storage)
    handle, metadata = plane.create_dataset(
        owner_scope={"account_id": "account-a", "project_id": "project-a"},
        run_id="run-1",
        step_id="step-1",
        schema={"columns": ["id"]},
        rows=[{"id": 1}],
        retention_class="ephemeral",
    )
    storage.failed_key = str(metadata["object_key"])

    store = external_effects.external_effect_store
    monkeypatch.setattr(
        store,
        "list_external_effect_output_reconciliations_for_cleanup",
        MagicMock(
            return_value=[
                {
                    "effect_id": "effect-1",
                    "claim_token": "claim-1",
                    "claim_version": 1,
                    "minted_artifact_ids_jsonb": [],
                    "minted_dataset_ids_jsonb": [handle.dataset_id],
                }
            ]
        ),
    )
    resolve = MagicMock(return_value=True)
    monkeypatch.setattr(store, "resolve_external_effect_output_reconciliation", resolve)

    assert (
        external_effects.reconcile_external_effect_outputs(
            object(), account_id="account-a", artifact_plane=plane
        )
        == 0
    )
    assert plane.get_dataset(handle.dataset_id) is not None
    assert storage.stat(str(metadata["object_key"])) is not None
    assert storage.stat(f"datasets/{handle.dataset_id}/meta.json") is not None
    resolve.assert_not_called()


def test_stale_reconciliation_resolve_does_not_report_new_attempt_deleted(monkeypatch) -> None:
    store = external_effects.external_effect_store
    monkeypatch.setattr(
        store,
        "list_external_effect_output_reconciliations_for_cleanup",
        MagicMock(
            return_value=[
                {
                    "effect_id": "effect-1",
                    "claim_token": "old-claim",
                    "claim_version": 1,
                    "minted_artifact_ids_jsonb": [],
                    "minted_dataset_ids_jsonb": [],
                }
            ]
        ),
    )
    resolve = MagicMock(return_value=False)
    monkeypatch.setattr(store, "resolve_external_effect_output_reconciliation", resolve)

    assert (
        external_effects.reconcile_external_effect_outputs(
            object(), account_id="account-a", artifact_plane=object()
        )
        == 0
    )
    assert resolve.call_args.kwargs["claim_token"] == "old-claim"
    assert resolve.call_args.kwargs["claim_version"] == 1


def test_reconciliation_schema_is_private_tenant_scoped_and_cascades() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = " ".join(
        (root / "db/schema" / name).read_text(encoding="utf-8")
        for name in ("001_schema_part_04.sql", "001_schema_part_10.sql")
    )
    assert "CREATE TABLE public.external_effect_output_reconciliations" in schema
    assert "PRIMARY KEY (effect_id, account_id, claim_version)" in schema
    assert (
        "external_effect_output_reconciliations_effect_account_fkey FOREIGN KEY "
        "(effect_id, account_id) REFERENCES public.external_effects(effect_id, account_id) "
        "ON DELETE CASCADE"
    ) in schema
    assert "minted_artifact_ids_jsonb" in schema
    assert "minted_dataset_ids_jsonb" in schema
    assert "CREATE TABLE public.external_effect_maintenance_cursors" in schema
    assert (
        "external_effect_maintenance_cursors_account_fkey FOREIGN KEY (account_id) "
        "REFERENCES public.accounts(id) ON DELETE CASCADE"
    ) in schema
    store_source = (root / "core/infrastructure/db/external_effect_store.py").read_text(
        encoding="utf-8"
    )
    assert "reconciliation.claim_version < effect.claim_version" in store_source
    assert "ORDER BY reconciliation.updated_at" in store_source


def test_success_confirm_without_replay_context_fails_closed(monkeypatch) -> None:
    update = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store, "update_external_effect_status", update
    )

    replay = _replay_module()
    _raises_input_1279_1 = object()
    with pytest.raises(replay.ExternalEffectReplayPersistenceError) as error:
        external_effects.confirm_external_effect_from_invocation(
            _raises_input_1279_1,
            effect_id="effect-1",
            claim_token="claim-1",
            invocation={"status": "succeeded", "response": {"ok": True}},
        )

    assert error.value.code == "external_effect_replay_persist_failed"
    assert update.call_count == 1
    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["error_code"] == "external_effect_replay_persist_failed"
    assert update.call_args.kwargs["expected_current_status"] == "running"


def test_replay_persistence_failure_is_explicitly_timeout_unknown() -> None:
    replay = _replay_module()

    invocation = external_effects.external_effect_confirmation_failure_invocation(
        replay.ExternalEffectReplayPersistenceError()
    )

    assert invocation["detail"] == "external_effect_replay_persist_failed"
    assert invocation["response"]["error_code"] == "external_effect_replay_persist_failed"
    assert invocation["response"]["external_effect_status"] == "timeout_unknown"


def test_store_replay_lease_loss_preserves_confirmation_lease_lost_code() -> None:
    store = importlib.import_module("core.infrastructure.db.external_effect_store")

    invocation = external_effects.external_effect_confirmation_failure_invocation(
        store.ExternalEffectReplayLeaseLostError()
    )

    assert invocation["detail"] == "external_effect_confirmation_lease_lost"
    assert invocation["response"]["error_code"] == "external_effect_confirmation_lease_lost"


def test_success_confirmation_rejects_operation_binding_mismatch(monkeypatch) -> None:
    update = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    atomic = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "get_external_effect",
        MagicMock(
            return_value={
                "effect_id": "effect-1",
                "account_id": "account-1",
                "extension_id": "fixture.mail",
                "extension_install_id": "install-1",
                "operation_id": "send",
            }
        ),
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        atomic,
    )

    _raises_input_1344_1 = object()
    with pytest.raises(_replay_module().ExternalEffectReplayPersistenceError):
        external_effects.confirm_external_effect_from_invocation(
            _raises_input_1344_1,
            effect_id="effect-1",
            claim_token="claim-1",
            account_id="account-1",
            extension={
                "extension_key": "fixture.mail",
                "extension_install_id": "install-1",
                "operation_manifest": [
                    {"operation_id": "broader-operation", "outputs": [{"name": "receipt"}]}
                ],
            },
            operation_id="broader-operation",
            invocation={"status": "succeeded", "response": {"receipt": {"all": "data"}}},
        )

    atomic.assert_not_called()
    assert update.call_args.kwargs["status"] == "timeout_unknown"


@pytest.mark.parametrize(
    ("effect_overrides", "extension_overrides", "expected_project_id", "expected_connection_id"),
    [
        ({"extension_install_id": "install-1"}, {"extension_install_id": None}, None, None),
        ({"project_id": "project-other"}, {}, "project-1", None),
        ({"connection_id": "connection-other"}, {}, None, "connection-1"),
    ],
)
def test_success_confirmation_rejects_install_project_and_connection_mismatch(
    monkeypatch,
    effect_overrides: dict,
    extension_overrides: dict,
    expected_project_id: str | None,
    expected_connection_id: str | None,
) -> None:
    effect = {
        "effect_id": "effect-1",
        "account_id": "account-1",
        "extension_id": "fixture.mail",
        "extension_install_id": "",
        "operation_id": "send",
        "project_id": expected_project_id or "",
        "connection_id": expected_connection_id or "",
        **effect_overrides,
    }
    extension = {
        "extension_key": "fixture.mail",
        "extension_install_id": "",
        "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        **extension_overrides,
    }
    update = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    atomic = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "get_external_effect",
        MagicMock(return_value=effect),
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        atomic,
    )

    _raises_input_1414_1 = object()
    with pytest.raises(_replay_module().ExternalEffectReplayPersistenceError):
        external_effects.confirm_external_effect_from_invocation(
            _raises_input_1414_1,
            effect_id="effect-1",
            claim_token="claim-1",
            account_id="account-1",
            extension=extension,
            operation_id="send",
            invocation={"status": "succeeded", "response": {"ok": True}},
            expected_project_id=expected_project_id,
            expected_connection_id=expected_connection_id,
        )

    atomic.assert_not_called()
    assert update.call_args.kwargs["status"] == "timeout_unknown"


def test_host_failure_after_provider_success_is_timeout_unknown_without_replay(
    monkeypatch,
) -> None:
    update = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    atomic = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store, "update_external_effect_status", update
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        atomic,
    )

    confirmed = external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="effect-1",
        claim_token="claim-1",
        account_id="account-1",
        operation_id="send",
        extension={
            "operation_manifest": [{"operation_id": "send", "outputs": [{"name": "receipt"}]}],
        },
        invocation={
            "status": "failed",
            "detail": "response_envelope_processing_failed",
            "response": {"ok": True, "external_effect_status": "succeeded"},
            "errors": [{"code": "response_envelope_processing_failed"}],
        },
    )

    assert confirmed == {"effect_id": "effect-1", "status": "timeout_unknown"}
    assert update.call_args.kwargs["status"] == "timeout_unknown"
    atomic.assert_not_called()


@pytest.mark.parametrize(
    ("invocation", "expected_status"),
    [
        (
            {
                "status": "succeeded",
                "response": {"external_effect": {"provider_message_id": "hydrated-secret"}},
            },
            "timeout_unknown",
        ),
        (
            {
                "status": "failed",
                "detail": "hydrated-secret",
                "response": {
                    "external_effect_status": "failed",
                    "definitely_no_external_effect": True,
                },
            },
            "failed",
        ),
    ],
)
def test_persisted_effect_metadata_never_reflects_protected_values(
    monkeypatch,
    invocation: dict,
    expected_status: str,
) -> None:
    update = MagicMock(return_value={"effect_id": "effect-1", "status": expected_status})
    atomic = MagicMock()
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        atomic,
    )

    confirmed = external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="effect-1",
        claim_token="claim-1",
        invocation=invocation,
        protected_values=frozenset({"hydrated-secret"}),
    )

    assert confirmed == {"effect_id": "effect-1", "status": expected_status}
    persisted = update.call_args.kwargs
    assert persisted["status"] == expected_status
    assert persisted["status_reason"] == "extension_secret_exposure_blocked"
    assert persisted["provider_message_id"] is None
    assert persisted["external_object_id"] is None
    assert persisted["error_code"] == "extension_secret_exposure_blocked"
    assert "hydrated-secret" not in repr(persisted)
    atomic.assert_not_called()


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("retention_expired", "external_effect_replay_expired"),
        ("extension_uninstalled", "external_effect_replay_extension_uninstalled"),
        ("ciphertext_missing", "external_effect_replay_missing"),
        ("ciphertext_corrupt", "external_effect_replay_corrupt"),
    ],
)
def test_nonreplayable_tombstones_are_typed_and_never_spawn(
    reason: str,
    expected_code: str,
) -> None:
    replay = _replay_module()

    decision = replay.replay_decision_for_effect(
        {"status": "succeeded", "replay_state": "nonreplayable", "replay_reason": reason}
    )

    assert decision == {
        "should_spawn": False,
        "error": expected_code,
        "invocation": {},
    }


def test_claim_error_is_never_replaced_by_replay_hydration(monkeypatch) -> None:
    replay = _replay_module()
    replay_read = MagicMock(side_effect=AssertionError("claim errors must not decrypt replay"))
    monkeypatch.setattr(replay, "hydrate_external_effect_replay", replay_read)
    claim = {
        "declared": True,
        "should_spawn": False,
        "error": "external_effect_idempotency_conflict",
        "effect": {"effect_id": "effect-1", "status": "succeeded"},
    }

    assert replay.hydrate_external_effect_claim(object(), claim) == claim
    replay_read.assert_not_called()


@pytest.mark.parametrize(
    ("effect_status", "error_code"),
    [
        ("failed", "external_effect_failed"),
        ("timeout_unknown", "external_effect_timeout_unknown"),
        ("suppressed", "external_effect_confirmation_invalid"),
        ("unexpected", "external_effect_confirmation_invalid"),
    ],
)
def test_successful_consumer_requires_confirmed_effect_success(
    effect_status: str,
    error_code: str,
) -> None:
    invocation = {"status": "succeeded", "response": {"ok": True}}

    checked = external_effects.apply_external_effect_confirmation(
        invocation,
        {"effect_id": "effect-1", "status": effect_status},
    )

    assert checked == {
        "status": "failed",
        "detail": error_code,
        "response": {"ok": False, "error_code": error_code},
        "errors": [{"code": error_code}],
    }


def test_success_confirmation_without_safe_projection_fails_closed() -> None:
    checked = external_effects.apply_external_effect_confirmation(
        {"status": "succeeded", "response": {"provider_control": "unsafe"}},
        {"effect_id": "effect-1", "status": "succeeded"},
    )

    assert checked == {
        "status": "failed",
        "detail": "external_effect_confirmation_invalid",
        "response": {"ok": False, "error_code": "external_effect_confirmation_invalid"},
        "errors": [{"code": "external_effect_confirmation_invalid"}],
    }
    assert "unsafe" not in repr(checked)


def test_success_confirmation_returns_the_same_safe_projection_as_replay(
    monkeypatch,
) -> None:
    safe_replay = {
        "status": "succeeded",
        "response": {
            "result": {
                "message_id": "message-1",
                "receipt": {"accepted": True},
            }
        },
    }
    provider_invocation = {
        **safe_replay,
        "response": {
            **safe_replay["response"],
            "provider_control": {"authorization": "must-not-escape"},
        },
        "undeclared": "must-not-escape",
    }
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "get_external_effect",
        MagicMock(
            return_value={
                "effect_id": "effect-1",
                "account_id": "account-1",
                "extension_id": "fixture.mail",
                "extension_install_id": "install-1",
                "operation_id": "send",
            }
        ),
    )
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "confirm_external_effect_with_replay",
        MagicMock(return_value={"effect_id": "effect-1", "status": "succeeded"}),
    )
    replay = _replay_module()
    monkeypatch.setattr(replay, "project_external_effect_replay", lambda **_kwargs: safe_replay)
    monkeypatch.setattr(
        replay,
        "encrypt_projected_replay",
        lambda *_args, **_kwargs: {
            "ciphertext": "v1:ciphertext",
            "binding_hash": "binding-hash",
            "plaintext_bytes": 42,
        },
    )

    confirmed = external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="effect-1",
        claim_token="claim-1",
        account_id="account-1",
        extension=_extension(),
        operation_id="send",
        invocation=provider_invocation,
    )

    assert (
        external_effects.apply_external_effect_confirmation(provider_invocation, confirmed)
        == safe_replay
    )
    assert external_effects.public_confirmed_external_effect(confirmed) == {
        "effect_id": "effect-1",
        "status": "succeeded",
    }
    assert "must-not-escape" not in repr(
        external_effects.apply_external_effect_confirmation(provider_invocation, confirmed)
    )


def test_all_external_effect_consumers_enforce_post_confirmation_status() -> None:
    root = Path(__file__).resolve().parents[2]
    consumers = (
        "core/application/extensions/operation_service_resources.py",
        "core/application/management/command_services_dispatch.py",
        "core/application/orchestration/connector_extension_runtime.py",
        "core/application/notifications/delivery.py",
    )

    for relative_path in consumers:
        source = (root / relative_path).read_text()
        assert "apply_external_effect_confirmation" in source, relative_path
        assert "public_confirmed_external_effect" in source, relative_path

    service = ast.parse((root / "core/application/extensions/operation_service.py").read_text())
    cleanup = next(
        node
        for node in ast.walk(service)
        if isinstance(node, ast.FunctionDef) and node.name == "_cleanup_action_resources"
    )
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "operation_service_resources"
        and any(alias.name == "cleanup_action_resources" for alias in node.names)
        for node in ast.walk(cleanup)
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cleanup_action_resources"
        for node in ast.walk(cleanup)
    )


def test_schema_persists_typed_replay_tombstone_without_public_ciphertext_projection() -> None:
    root = Path(__file__).resolve().parents[2]
    schema = "\n".join(
        (root / "db" / "schema" / name).read_text()
        for name in ("001_schema_part_04.sql", "001_schema_part_08.sql", "001_schema_part_10.sql")
    )
    store_source = (root / "core/infrastructure/db/external_effect_store.py").read_text()

    assert "CREATE TABLE public.external_effect_replays" in schema
    assert "replay_state text" in schema
    assert "replay_reason text" in schema
    assert "encrypted_payload text" in schema
    assert "plaintext_sha256" not in schema
    assert "external_effect_replays_state_check" in schema
    get_effect_source = store_source.split("def get_external_effect(", 1)[1].split(
        "def get_external_effect_replay(", 1
    )[0]
    assert "encrypted_payload" not in get_effect_source

    from core.infrastructure.db.schema_contract import REQUIRED_RUNTIME_COLUMNS

    replay_columns = {
        column for table, column in REQUIRED_RUNTIME_COLUMNS if table == "external_effect_replays"
    }
    assert replay_columns >= {
        "effect_id",
        "account_id",
        "replay_state",
        "replay_reason",
        "encrypted_payload",
        "replay_binding_hash",
        "plaintext_bytes",
        "updated_at",
    }


def test_external_effect_identity_is_install_scoped_after_reinstall() -> None:
    common = {
        "account_id": "account-1",
        "project_id": "project-1",
        "extension_id": "fixture.mail",
        "operation_id": "send",
        "connection_id": None,
        "source_ref": {"job_id": "job-1", "step_id": "send"},
        "request_payload": {"to": "person@example.invalid"},
        "test_mode": False,
    }

    first = external_effects.build_external_effect_identity(
        **common,
        extension_install_id="install-1",
    )
    reinstalled = external_effects.build_external_effect_identity(
        **common,
        extension_install_id="install-2",
    )

    assert first["idempotency_scope_hash"] != reinstalled["idempotency_scope_hash"]
    assert first["request_fingerprint"] != reinstalled["request_fingerprint"]


def test_direct_action_consumer_never_returns_success_when_replay_persist_fails(
    monkeypatch,
) -> None:
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.scope import normalize_scope

    replay = _replay_module()
    monkeypatch.setattr(
        external_effects,
        "confirm_external_effect_from_invocation",
        MagicMock(side_effect=replay.ExternalEffectReplayPersistenceError()),
    )
    service = ExtensionOperationService(
        get_logger=lambda: object(),
        get_operation_store=lambda: object(),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
    )
    invocation = {
        "status": "succeeded",
        "response": {"ok": True, "message_id": "message-1"},
        "grant_outputs": {"receipt": "artifact:artifact-1"},
    }

    service._cleanup_action_resources(
        context,
        invocation,
        protected_values=frozenset(),
    )

    assert invocation == {
        "status": "failed",
        "detail": "external_effect_replay_persist_failed",
        "response": {
            "ok": False,
            "error_code": "external_effect_replay_persist_failed",
            "external_effect_status": "timeout_unknown",
        },
        "errors": [{"code": "external_effect_replay_persist_failed"}],
    }


@pytest.mark.parametrize(
    ("commit_outcome", "expected_cleanup_calls"),
    [("rolled_back", 1), ("unknown", 0)],
)
def test_direct_replay_persist_failure_cleans_outputs_only_after_proven_rollback(
    monkeypatch,
    commit_outcome: str,
    expected_cleanup_calls: int,
) -> None:
    from core.application.extensions import runtime_grants
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.scope import normalize_scope

    replay = _replay_module()
    error = replay.ExternalEffectReplayPersistenceError(commit_outcome=commit_outcome)
    monkeypatch.setattr(
        external_effects,
        "confirm_external_effect_from_invocation",
        MagicMock(side_effect=error),
    )
    plane = object()
    prepared = SimpleNamespace(
        artifact_output_ids=["artifact-1"],
        dataset_output_ids=[],
        artifact_outputs=[],
        dataset_outputs=[],
    )
    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
        prepared_grants=prepared,
    )
    service = ExtensionOperationService(
        get_logger=lambda: object(),
        get_operation_store=lambda: object(),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
        artifact_plane=plane,
    )
    cleanup = MagicMock()
    monkeypatch.setattr(runtime_grants, "cleanup_prepared_extension_grants", cleanup)
    invocation = {
        "status": "succeeded",
        "response": {"ok": True},
        "produced_artifacts": [{"artifact_id": "artifact-1"}],
        "grant_outputs": {"receipt": "artifact:artifact-1"},
    }

    service._cleanup_action_resources(context, invocation)

    assert cleanup.call_count == expected_cleanup_calls
    assert invocation["detail"] == "external_effect_replay_persist_failed"


def test_direct_action_duplicate_returns_hydrated_result_without_provider_spawn(
    monkeypatch,
) -> None:
    from core.application.extensions import operation_service
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.scope import normalize_scope

    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(context={}),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
    )
    service = ExtensionOperationService(
        get_logger=lambda: object(),
        get_operation_store=lambda: object(),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        operation_service,
        "_hydrated_action_secret_values",
        lambda _context: (frozenset(), False),
    )
    monkeypatch.setattr(
        external_effects,
        "claim_external_effect_for_action",
        lambda *_args, **_kwargs: {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "effect-1", "status": "succeeded"},
        },
    )
    replay = _replay_module()
    monkeypatch.setattr(
        replay,
        "hydrate_external_effect_claim",
        lambda _db, claim: {
            **claim,
            "replay_invocation": {
                "status": "succeeded",
                "response": {"ok": True, "result": {"message_id": "message-1"}},
            },
        },
    )
    provider = MagicMock(side_effect=AssertionError("provider must not be invoked"))
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)

    result = service.execute_action("fixture.mail", "send", SimpleNamespace()).dump()

    assert result["action"] == {
        "status": "succeeded",
        "detail": "",
        "response": {"ok": True, "result": {"message_id": "message-1"}},
        "errors": [],
    }
    provider.assert_not_called()


@pytest.mark.parametrize(
    "error_code",
    [
        "external_effect_replay_missing",
        "external_effect_replay_corrupt",
        "external_effect_replay_expired",
    ],
)
def test_direct_action_duplicate_typed_replay_failure_never_spawns(
    monkeypatch,
    error_code: str,
) -> None:
    from core.application.extensions import operation_service
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceError
    from core.application.management.scope import normalize_scope

    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(context={}),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
    )
    service = ExtensionOperationService(
        get_logger=lambda: object(),
        get_operation_store=lambda: object(),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        operation_service,
        "_hydrated_action_secret_values",
        lambda _context: (frozenset(), False),
    )
    monkeypatch.setattr(
        external_effects,
        "claim_external_effect_for_action",
        lambda *_args, **_kwargs: {
            "declared": True,
            "should_spawn": False,
            "effect": {"effect_id": "effect-1", "status": "succeeded"},
        },
    )
    replay = _replay_module()
    monkeypatch.setattr(
        replay,
        "hydrate_external_effect_claim",
        lambda _db, claim: {**claim, "error": error_code},
    )
    provider = MagicMock(side_effect=AssertionError("provider must not be invoked"))
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)

    _raises_input_2025_1 = SimpleNamespace()
    with pytest.raises(ServiceError, match=error_code):
        service.execute_action("fixture.mail", "send", _raises_input_2025_1)

    provider.assert_not_called()


def test_direct_action_idempotency_key_distinguishes_intent_and_replays_retry(
    monkeypatch,
) -> None:
    from core.application.extensions import operation_service
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.scope import normalize_scope

    service = ExtensionOperationService(
        get_logger=lambda: object(),
        get_operation_store=lambda: object(),
        resolve_extension_runtime_record=lambda _extension_id: (None, None),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )

    def prepare(_extension_id, _action_id, _body, _scope, idempotency_key):
        direct_intent = operation_effects._direct_action_intent_value(
            {
                "page_id": "page-1",
                "component_id": _body.component_id,
                "context": {},
                "target": {"delivery_id": _body.delivery_id},
                "input": {"subject": _body.subject, "attempt": _body.attempt},
                "artifact_inputs": [],
                "artifact_outputs": list(_body.artifact_outputs),
                "artifact_output_specs": list(_body.artifact_output_specs),
                "dataset_inputs": [],
                "dataset_outputs": [],
                "dataset_output_specs": [],
            }
        )
        return _ActionExecutionContext(
            extension_id="fixture.mail",
            action_id="send",
            body=SimpleNamespace(context={}),
            scope=None,
            resolved_scope=normalize_scope(account_id="account-1"),
            extension=_direct_effect_extension(),
            bl=object(),
            target={"delivery_id": _body.delivery_id},
            input_payload={"subject": _body.subject, "attempt": _body.attempt},
            audit={"account_id": "account-1", "actor_id": "actor-1"},
            runtime_ctx={},
            direct_action_idempotency_key=idempotency_key,
            direct_action_intent=direct_intent,
        )

    monkeypatch.setattr(service, "_prepare_action_execution", prepare)
    monkeypatch.setattr(
        operation_service,
        "_hydrated_action_secret_values",
        lambda _context: (frozenset(), False),
    )
    seen: dict[str, dict] = {}
    source_refs: list[dict] = []

    def claim(*_args, **kwargs):
        source_ref = dict(kwargs["source_ref"])
        source_refs.append(source_ref)
        intent_hash = source_ref["direct_action_intent_hash"]
        request_payload = dict(kwargs["request_payload"])
        first = intent_hash not in seen
        if not first and seen[intent_hash] != request_payload:
            return {
                "declared": True,
                "should_spawn": False,
                "effect": {"effect_id": f"effect-{intent_hash[:8]}", "status": "succeeded"},
                "error": "external_effect_idempotency_conflict",
            }
        seen[intent_hash] = request_payload
        return {
            "declared": True,
            "should_spawn": first,
            "effect": {
                "effect_id": f"effect-{intent_hash[:8]}",
                "claim_token": "claim-1",
                "status": "claimed" if first else "succeeded",
            },
        }

    monkeypatch.setattr(external_effects, "claim_external_effect_for_action", claim)
    monkeypatch.setattr(
        external_effects,
        "mark_external_effect_running",
        lambda *_a, **_k: {"status": "running"},
    )
    replay = _replay_module()
    monkeypatch.setattr(
        replay,
        "hydrate_external_effect_claim",
        lambda _db, claim_result: {
            **claim_result,
            "replay_invocation": {
                "status": "succeeded",
                "response": {"ok": True, "result": {"message_id": "replayed"}},
            },
        },
    )
    provider = MagicMock(
        return_value={
            "status": "succeeded",
            "response": {"ok": True, "result": {"message_id": "provider"}},
        }
    )
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)
    monkeypatch.setattr(service, "_process_action_response", lambda _context, value: value)
    monkeypatch.setattr(service, "_cleanup_action_resources", lambda *_a, **_k: None)
    monkeypatch.setattr(operation_runtime, "_log_action_invocation", lambda *_a, **_k: None)

    first = service.execute_action(
        "fixture.mail",
        "send",
        SimpleNamespace(
            subject="same payload",
            attempt=1,
            delivery_id="delivery-1",
            component_id="send-button",
            artifact_outputs=[{"artifact_id": "artifact-1"}],
            artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
        ),
        idempotency_key="intent-a",
    ).dump()
    retry = service.execute_action(
        "fixture.mail",
        "send",
        SimpleNamespace(
            subject="same payload",
            attempt=1,
            delivery_id="delivery-1",
            component_id="send-button",
            artifact_outputs=[{"artifact_id": "artifact-1"}],
            artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
        ),
        idempotency_key="intent-a",
    ).dump()
    independent = service.execute_action(
        "fixture.mail",
        "send",
        SimpleNamespace(
            subject="same payload",
            attempt=1,
            delivery_id="delivery-1",
            component_id="send-button",
            artifact_outputs=[{"artifact_id": "artifact-1"}],
            artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
        ),
        idempotency_key="intent-b",
    ).dump()
    from core.application.management.errors import ServiceError

    _raises_input_2185_1 = SimpleNamespace(
        subject="changed payload",
        attempt=1,
        delivery_id="delivery-1",
        component_id="send-button",
        artifact_outputs=[{"artifact_id": "artifact-1"}],
        artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
    )
    with pytest.raises(
        ServiceError, match="external_effect_idempotency_conflict"
    ) as conflict_error:
        service.execute_action(
            "fixture.mail", "send", _raises_input_2185_1, idempotency_key="intent-a"
        )
    assert conflict_error.value.status_code == 409
    _raises_input_2202_1 = SimpleNamespace(
        subject="same payload",
        attempt=1,
        delivery_id="delivery-2",
        component_id="send-button",
        artifact_outputs=[{"artifact_id": "artifact-1"}],
        artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
    )
    with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
        service.execute_action(
            "fixture.mail", "send", _raises_input_2202_1, idempotency_key="intent-a"
        )
    _raises_input_2216_1 = SimpleNamespace(
        subject="same payload",
        attempt=1,
        delivery_id="delivery-1",
        component_id="different-component",
        artifact_outputs=[{"artifact_id": "artifact-1"}],
        artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
    )
    with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
        service.execute_action(
            "fixture.mail", "send", _raises_input_2216_1, idempotency_key="intent-a"
        )
    _raises_input_2230_1 = SimpleNamespace(
        subject="same payload",
        attempt=1,
        delivery_id="delivery-1",
        component_id="send-button",
        artifact_outputs=[{"artifact_id": "artifact-2"}],
        artifact_output_specs=[{"binding_key": "different_receipt", "filename": "other.json"}],
    )
    with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
        service.execute_action(
            "fixture.mail", "send", _raises_input_2230_1, idempotency_key="intent-a"
        )
    _raises_input_2246_1 = SimpleNamespace(
        subject="same payload",
        attempt=2,
        delivery_id="delivery-1",
        component_id="send-button",
        artifact_outputs=[{"artifact_id": "artifact-1"}],
        artifact_output_specs=[{"binding_key": "receipt", "filename": "receipt.json"}],
    )
    with pytest.raises(ServiceError, match="external_effect_idempotency_conflict"):
        service.execute_action(
            "fixture.mail", "send", _raises_input_2246_1, idempotency_key="intent-a"
        )

    assert provider.call_count == 2
    assert first["action"]["response"]["result"]["message_id"] == "provider"
    assert retry["action"]["response"]["result"]["message_id"] == "replayed"
    assert independent["action"]["response"]["result"]["message_id"] == "provider"
    assert (
        source_refs[0]["direct_action_intent_hash"] == source_refs[1]["direct_action_intent_hash"]
    )
    assert (
        source_refs[0]["direct_action_intent_hash"] != source_refs[2]["direct_action_intent_hash"]
    )
    assert "intent-a" not in repr(source_refs)
    assert "delivery-1" not in repr(source_refs)
    assert provider.call_args_list[0].args[0].input_payload["attempt"] == 1
    assert seen[source_refs[0]["direct_action_intent_hash"]]["attempt"] == 1


def test_direct_action_intent_projection_excludes_host_hydration_and_secrets() -> None:
    from core.application.extensions.operation_effects import _direct_action_intent_value
    from core.application.management.errors import ServiceValidationError

    with pytest.raises(ServiceValidationError, match="caller-supplied credentials"):
        _direct_action_intent_value(
            {
                "context": {
                    "project_id": "project-1",
                    "bot_token": "low-entropy-secret",
                },
                "target": {
                    "delivery_id": "delivery-1",
                    "config": {"endpoint": "https://provider.invalid"},
                    "headers": {"Authorization": "secret"},
                },
            }
        )

    with pytest.raises(ServiceValidationError, match="retry and host runtime fields"):
        _direct_action_intent_value(
            {
                "context": {"project_id": "project-1", "retry_attempt": 2},
                "target": {"delivery_id": "delivery-1"},
            }
        )

    projected = _direct_action_intent_value(
        {
            "context": {"project_id": "project-1"},
            "target": {"delivery_id": "delivery-1", "connection_id": "connection-1"},
            "input": {
                "message": "Hello",
                "attempt": 2,
                "config": {"rendering": "compact"},
                "headers": ["column-a", "column-b"],
                "token": "business-domain-token",
                "nested": {"token_count": 3},
            },
            "artifact_inputs": [{"artifact_id": "artifact-1"}],
            "dataset_inputs": [{"dataset_id": "dataset-1"}],
        }
    )

    assert projected == {
        "context": {"project_id": "project-1"},
        "target": {
            "delivery_id": "delivery-1",
            "connection_id": "connection-1",
        },
        "input": {
            "message": "Hello",
            "attempt": 2,
            "config": {"rendering": "compact"},
            "headers": ["column-a", "column-b"],
            "token": "business-domain-token",
            "nested": {"token_count": 3},
        },
        "artifact_inputs": [{"artifact_id": "artifact-1"}],
        "dataset_inputs": [{"dataset_id": "dataset-1"}],
    }


@pytest.mark.parametrize(
    "body_kwargs",
    [
        {"context": {"bot_token": "caller-secret"}},
        {"context": {"runtime_token": "caller-runtime-token"}},
        {"context": {"retry_attempt": 2}},
        {"target": {"config": {"endpoint": "https://provider.invalid"}}},
    ],
)
def test_prepare_action_execution_rejects_caller_supplied_runtime_material(
    body_kwargs: dict,
) -> None:
    from core.application.extensions.operation_service import ExtensionOperationService
    from core.application.management.errors import ServiceValidationError
    from core.application.management.requests import ExtensionActionRequest

    service = ExtensionOperationService(
        get_logger=lambda: MagicMock(),
        get_operation_store=lambda: MagicMock(),
        resolve_extension_runtime_record=lambda _extension_id: (
            _direct_effect_extension(),
            MagicMock(),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 500,
    )

    _raises_input_2367_1 = ExtensionActionRequest(**body_kwargs)
    with pytest.raises(ServiceValidationError, match="caller-supplied"):
        service.execute_action(
            "fixture.mail", "send", _raises_input_2367_1, idempotency_key="idempotency-key"
        )


def test_direct_action_pre_spawn_setup_failure_never_marks_running_or_invokes_provider(
    monkeypatch,
) -> None:
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceError
    from core.application.management.scope import normalize_scope

    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_direct_effect_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
    )
    artifact_plane = object()
    service = ExtensionOperationService(
        get_logger=lambda: MagicMock(),
        get_operation_store=lambda: MagicMock(),
        resolve_extension_runtime_record=lambda _extension_id: (
            _direct_effect_extension(),
            object(),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 500,
        artifact_plane=artifact_plane,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(operation_runtime, "_claim_action_external_effect", lambda _context: None)

    def fail_after_grant_preparation(_context) -> None:
        context.prepared_grants = SimpleNamespace()
        raise RuntimeError("platform setup failed")

    prepare = MagicMock(side_effect=fail_after_grant_preparation)
    mark_running = MagicMock()
    monkeypatch.setattr(service, "_prepare_action_provider_invocation", prepare)
    monkeypatch.setattr(operation_runtime, "_mark_action_external_effect_running", mark_running)
    fail_before_spawn = MagicMock(side_effect=RuntimeError("postgres://secret-sentinel"))
    monkeypatch.setattr(external_effects, "fail_external_effect_before_spawn", fail_before_spawn)
    from core.application.extensions import runtime_grants

    cleanup = MagicMock()
    monkeypatch.setattr(runtime_grants, "cleanup_prepared_extension_grants", cleanup)
    provider = MagicMock()
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)

    _raises_input_2433_1 = SimpleNamespace()
    with pytest.raises(ServiceError, match="before provider invocation") as error:
        service.execute_action("fixture.mail", "send", _raises_input_2433_1)

    assert "secret-sentinel" not in repr(error.value.payload)

    fail_before_spawn.assert_called_once_with(
        context.bl,
        effect_id="effect-1",
        claim_token="claim-1",
        code="external_effect_pre_spawn_setup_failed",
    )
    mark_running.assert_not_called()
    provider.assert_not_called()
    cleanup.assert_called_once_with(artifact_plane, context.prepared_grants)


@pytest.mark.parametrize(
    "mark_failure",
    [None, RuntimeError("lease store unavailable")],
    ids=["reclaimed", "store-error"],
)
def test_direct_action_running_lease_failure_cleans_prepared_grants_before_exit(
    monkeypatch,
    mark_failure: Exception | None,
) -> None:
    from core.application.extensions import runtime_grants
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceError
    from core.application.management.scope import normalize_scope

    artifact_plane = object()
    prepared = SimpleNamespace()
    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_direct_effect_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
    )
    service = ExtensionOperationService(
        get_logger=lambda: MagicMock(),
        get_operation_store=lambda: MagicMock(),
        resolve_extension_runtime_record=lambda _extension_id: (
            _direct_effect_extension(),
            object(),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 500,
        artifact_plane=artifact_plane,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(operation_runtime, "_claim_action_external_effect", lambda _context: None)

    def prepare(_context) -> None:
        context.prepared_grants = prepared

    monkeypatch.setattr(service, "_prepare_action_provider_invocation", prepare)
    mark_running = (
        MagicMock(side_effect=mark_failure)
        if mark_failure is not None
        else MagicMock(return_value=None)
    )
    monkeypatch.setattr(external_effects, "mark_external_effect_running", mark_running)
    cleanup = MagicMock()
    monkeypatch.setattr(runtime_grants, "cleanup_prepared_extension_grants", cleanup)
    provider = MagicMock()
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)
    confirm = MagicMock()
    monkeypatch.setattr(external_effects, "confirm_external_effect_from_invocation", confirm)
    fail_before_spawn = MagicMock()
    monkeypatch.setattr(external_effects, "fail_external_effect_before_spawn", fail_before_spawn)

    _raises_input_2516_1 = SimpleNamespace()
    with pytest.raises(ServiceError):
        service.execute_action("fixture.mail", "send", _raises_input_2516_1)

    mark_running.assert_called_once_with(
        context.bl,
        effect_id="effect-1",
        claim_token="claim-1",
    )
    provider.assert_not_called()
    cleanup.assert_called_once_with(artifact_plane, prepared)
    confirm.assert_not_called()
    fail_before_spawn.assert_called_once_with(
        context.bl,
        effect_id="effect-1",
        claim_token="claim-1",
        code="external_effect_pre_spawn_setup_failed",
    )


def test_non_success_confirmation_requires_a_matching_lease(monkeypatch) -> None:
    update = MagicMock(return_value=None)
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    _raises_input_2543_1 = object()
    with pytest.raises(external_effects.ExternalEffectConfirmationLeaseLostError) as error:
        external_effects.confirm_external_effect_from_invocation(
            _raises_input_2543_1,
            effect_id="effect-1",
            claim_token="stale-claim",
            invocation={
                "status": "succeeded",
                "response": {"external_effect_status": "timeout_unknown"},
            },
        )

    assert error.value.code == "external_effect_confirmation_lease_lost"
    failed = external_effects.external_effect_confirmation_failure_invocation(error.value)
    assert failed["status"] == "failed"
    assert failed["detail"] == "external_effect_confirmation_lease_lost"


def test_post_spawn_exception_is_fenced_to_timeout_unknown(monkeypatch) -> None:
    update = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    monkeypatch.setattr(
        external_effects.external_effect_store,
        "update_external_effect_status",
        update,
    )

    invocation = external_effects.external_effect_spawn_exception_invocation()
    confirmed = external_effects.confirm_external_effect_from_invocation(
        object(),
        effect_id="effect-1",
        claim_token="claim-1",
        invocation=invocation,
    )

    assert invocation["status"] == "failed"
    assert confirmed == {"effect_id": "effect-1", "status": "timeout_unknown"}
    assert update.call_args.kwargs["status"] == "timeout_unknown"
    assert update.call_args.kwargs["expected_current_status"] == "running"


def test_direct_reflected_secret_is_confirmed_as_timeout_unknown(monkeypatch) -> None:
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceError
    from core.application.management.scope import normalize_scope

    secret = "reflected-provider-secret"
    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_direct_effect_extension(),
        bl=object(),
        target={"connection": {"secrets": {"api_token": secret}}},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
    )
    service = ExtensionOperationService(
        get_logger=lambda: MagicMock(),
        get_operation_store=lambda: MagicMock(),
        resolve_extension_runtime_record=lambda _extension_id: (
            _direct_effect_extension(),
            object(),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(operation_runtime, "_claim_action_external_effect", lambda _context: None)
    monkeypatch.setattr(service, "_prepare_action_provider_invocation", lambda _context: None)
    monkeypatch.setattr(
        operation_runtime, "_mark_action_external_effect_running", lambda _context: None
    )
    monkeypatch.setattr(
        service,
        "_invoke_action_with_grants",
        lambda _context: {
            "status": "succeeded",
            "response": {"ok": True, "message_id": secret},
        },
    )
    monkeypatch.setattr(operation_runtime, "_log_action_invocation", lambda *_args, **_kwargs: None)
    confirm = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    monkeypatch.setattr(external_effects, "confirm_external_effect_from_invocation", confirm)

    _raises_input_2632_1 = SimpleNamespace()
    with pytest.raises(ServiceError) as error:
        service.execute_action("fixture.mail", "send", _raises_input_2632_1)

    confirmed_invocation = confirm.call_args.kwargs["invocation"]
    assert confirmed_invocation["detail"] == "extension_secret_exposure_blocked"
    assert secret not in repr(confirmed_invocation)
    assert error.value.payload["action"]["detail"] == "extension_secret_exposure_blocked"


def test_direct_output_finalizer_exception_cleans_and_confirms_timeout(monkeypatch) -> None:
    from core.application.extensions import runtime_grants
    from core.application.extensions.operation_service import (
        ExtensionOperationService,
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceError
    from core.application.management.scope import normalize_scope

    artifact_plane = object()
    prepared = SimpleNamespace()
    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_direct_effect_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        prepared_grants=prepared,
        external_effect_id="effect-1",
        external_effect_claim_token="claim-1",
    )
    service = ExtensionOperationService(
        get_logger=lambda: MagicMock(),
        get_operation_store=lambda: MagicMock(),
        resolve_extension_runtime_record=lambda _extension_id: (
            _direct_effect_extension(),
            object(),
        ),
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 502,
        artifact_plane=artifact_plane,
    )
    monkeypatch.setattr(service, "_prepare_action_execution", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(operation_runtime, "_claim_action_external_effect", lambda _context: None)
    monkeypatch.setattr(service, "_prepare_action_provider_invocation", lambda _context: None)
    monkeypatch.setattr(
        operation_runtime, "_mark_action_external_effect_running", lambda _context: None
    )
    provider = MagicMock(return_value={"status": "succeeded", "response": {"ok": True}})
    monkeypatch.setattr(service, "_invoke_action_with_grants", provider)
    monkeypatch.setattr(
        service,
        "_finalize_action_grants",
        MagicMock(side_effect=RuntimeError("storage detail must not escape")),
    )
    monkeypatch.setattr(operation_runtime, "_log_action_invocation", lambda *_args, **_kwargs: None)
    cleanup = MagicMock()
    monkeypatch.setattr(runtime_grants, "cleanup_prepared_extension_grants", cleanup)
    confirm = MagicMock(return_value={"effect_id": "effect-1", "status": "timeout_unknown"})
    monkeypatch.setattr(external_effects, "confirm_external_effect_from_invocation", confirm)

    _raises_input_2696_1 = SimpleNamespace()
    with pytest.raises(ServiceError) as error:
        service.execute_action("fixture.mail", "send", _raises_input_2696_1)

    provider.assert_called_once()
    cleanup.assert_called_once_with(artifact_plane, prepared)
    assert confirm.call_args.kwargs["claim_token"] == "claim-1"
    assert (
        confirm.call_args.kwargs["invocation"]["detail"]
        == "external_effect_output_finalization_failed"
    )
    assert "storage detail" not in repr(error.value.payload)


def test_connector_reflected_secret_path_does_not_skip_external_effect_confirmation() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "core/application/orchestration/connector_extension_runtime.py").read_text()
    assert "confirm_external_effect_from_invocation" in source
    assert "if external_effect_id and not secret_exposure:" not in source


def test_all_external_effect_runners_map_post_start_exceptions_to_shared_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    consumers = (
        "core/application/extensions/operation_service.py",
        "core/application/management/command_services_dispatch.py",
        "core/application/orchestration/connector_extension_runtime.py",
        "core/application/extensions/delivery_coordinator.py",
    )

    for relative_path in consumers:
        source = (root / relative_path).read_text()
        assert "external_effect_spawn_exception_invocation" in source, relative_path


@pytest.mark.parametrize("idempotency_key", ["", "x" * 256, "line\nbreak"])
def test_direct_external_effect_rejects_missing_or_invalid_idempotency_key(
    idempotency_key: str,
) -> None:
    from core.application.extensions.operation_service import (
        _ActionExecutionContext,
    )
    from core.application.management.errors import ServiceValidationError
    from core.application.management.scope import normalize_scope

    context = _ActionExecutionContext(
        extension_id="fixture.mail",
        action_id="send",
        body=SimpleNamespace(context={}),
        scope=None,
        resolved_scope=normalize_scope(account_id="account-1"),
        extension=_direct_effect_extension(),
        bl=object(),
        target={},
        input_payload={},
        audit={"account_id": "account-1"},
        runtime_ctx={},
        direct_action_idempotency_key=idempotency_key,
    )

    with pytest.raises(ServiceValidationError, match="Idempotency-Key"):
        operation_effects._claim_action_external_effect(context)
