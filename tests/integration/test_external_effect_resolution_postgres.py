"""Operator decisions fence one ledger intent and authorize one exact retry."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest

from core.application.extensions import external_effects
from core.infrastructure.db import external_effect_store

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _identity_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "unit-effect-resolution-root")


@pytest.fixture
def effect_case(disposable_db):
    """Use only the disposable audit database; leave no durable fixture rows."""
    account_id = str(uuid4())
    with psycopg.connect(disposable_db.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (id, account_key) VALUES (%s, %s)",
                (account_id, f"effect-resolution-{uuid4().hex}"),
            )
        request = {
            "declaration": {"effect_kind": "record_create", "channel": "api"},
            "account_id": account_id,
            "project_id": None,
            "extension_id": "fixture.resolution",
            "operation_id": "create",
            "source_type": "workflow_step",
            "source_ref": {"job_id": "job", "workflow_id": "wf", "step_id": "create"},
            "request_payload": {"title": "original"},
            "idempotency_key_override": "original-intent",
        }
        claim = external_effects.claim_external_effect(conn, **request)
        effect = external_effect_store.update_external_effect_status(
            conn,
            effect_id=claim["effect"]["effect_id"],
            claim_token=claim["effect"]["claim_token"],
            status="timeout_unknown",
            status_reason="provider_timeout",
            error_code="provider_timeout",
            error_text="Original provider response was lost",
            response_summary_jsonb={"real_partial_data": "preserve"},
        )
        yield conn, request, effect, disposable_db.dsn
        with conn.cursor() as cur:
            cur.execute("DELETE FROM command_audit_log WHERE account_id = %s", (account_id,))
            cur.execute("DELETE FROM external_effects WHERE account_id = %s", (account_id,))
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))


def _resolve(conn, request, effect, **changes):
    from core.application.extensions.external_effect_resolution import resolve_external_effect

    kwargs = {
        "account_id": request["account_id"],
        "project_id": request["project_id"],
        "effect_id": str(effect["effect_id"]),
        "observed_version": effect["claim_version"],
        "action": "reject",
        "comment": "Checked with the operator: no record exists.",
        "actor_id": "operator-1",
        "actor_type": "user",
        "approval_id": "approval-original",
        "expected_source_ref": request["source_ref"],
        "account_wide_authorized": True,
    }
    return resolve_external_effect(conn, **{**kwargs, **changes})


def _read(conn, effect):
    return external_effect_store.get_external_effect(conn, effect_id=str(effect["effect_id"]))


def test_approve_fences_old_worker_and_does_not_fabricate_provider_output(effect_case):
    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect, action="approve")
    row = _read(conn, effect)
    assert row["status"] == "succeeded"
    assert row["status_reason"] == "operator_confirmed_success"
    assert row["claim_version"] == effect["claim_version"] + 1
    assert row["claim_token"] != effect["claim_token"]
    assert row["error_code"] == "provider_timeout"
    assert row["response_summary_jsonb"] == {"real_partial_data": "preserve"}
    assert receipt["observed_version"] == effect["claim_version"]
    assert receipt["fenced_version"] == row["claim_version"]
    replay = external_effect_store.get_external_effect_replay(
        conn, account_id=request["account_id"], effect_id=str(effect["effect_id"])
    )
    assert replay["replay_state"] == "nonreplayable"
    assert replay["encrypted_payload"] is None
    assert "replay_invocation" not in receipt
    assert not external_effects.claim_external_effect(conn, **request)["should_spawn"]
    assert (
        external_effect_store.update_external_effect_status(
            conn,
            effect_id=str(effect["effect_id"]),
            claim_token=effect["claim_token"],
            status="failed",
            status_reason="late_worker",
        )
        is None
    )
    assert _read(conn, effect)["status"] == "succeeded"


def test_reject_blocks_ordinary_claim_and_allows_one_exact_retry(effect_case):
    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    assert _read(conn, effect)["status"] == "timeout_unknown"
    assert not external_effects.claim_external_effect(conn, **request)["should_spawn"]
    retried = external_effects.claim_external_effect(
        conn, **request, resolution_ref=receipt["resolution_ref"]
    )
    assert retried["should_spawn"]
    assert retried["effect"]["effect_id"] == effect["effect_id"]
    assert retried["effect"]["claim_version"] == receipt["fenced_version"] + 1
    assert retried["effect"]["claim_token"] != effect["claim_token"]
    assert not external_effects.claim_external_effect(
        conn, **request, resolution_ref=receipt["resolution_ref"]
    )["should_spawn"]


def test_resolution_is_first_decision_wins_and_exact_request_replays(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    assert _resolve(conn, request, effect) == receipt
    with pytest.raises(ExternalEffectResolutionError, match="already_resolved"):
        _resolve(conn, request, effect, action="approve")
    resolved = _read(conn, effect)
    with pytest.raises(ExternalEffectResolutionError, match="already_resolved"):
        _resolve(conn, request, resolved)


@pytest.mark.parametrize(
    "change",
    [
        {"request_payload": {"title": "changed"}},
        {"operation_id": "other"},
        {"source_ref": {"job_id": "other"}},
        {"idempotency_key_override": "new-intent"},
    ],
)
def test_resolution_cannot_authorize_a_different_intent(effect_case, change):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    with pytest.raises(ExternalEffectResolutionError, match="intent_mismatch"):
        external_effects.claim_external_effect(
            conn, **{**request, **change}, resolution_ref=receipt["resolution_ref"]
        )
    assert _read(conn, effect)["status_reason"] == "operator_confirmed_no_effect"


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": str(uuid4())},
        {"project_id": str(uuid4())},
        {"expected_source_ref": {"job_id": "other"}},
    ],
)
def test_resolution_scope_and_source_are_not_client_assertions(effect_case, change):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    with pytest.raises(ExternalEffectResolutionError, match=r"not_found|source_mismatch"):
        _resolve(conn, request, effect, **change)
    assert _read(conn, effect)["claim_token"] == effect["claim_token"]


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_both_resolution_actions_require_an_operator_reason(effect_case, action):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    with pytest.raises(ExternalEffectResolutionError, match="comment_required"):
        _resolve(conn, request, effect, action=action, comment=" \t ")
    assert _read(conn, effect)["claim_version"] == effect["claim_version"]


def test_audit_failure_rolls_back_fence_and_decision(effect_case, monkeypatch):
    from core.application.extensions.external_effect_resolution_repository import (
        ExternalEffectResolutionRepository,
    )

    conn, request, effect, _dsn = effect_case
    monkeypatch.setattr(
        ExternalEffectResolutionRepository,
        "insert_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        _resolve(conn, request, effect)
    row = _read(conn, effect)
    assert row["claim_version"] == effect["claim_version"]
    assert row["claim_token"] == effect["claim_token"]


def test_parallel_consumers_obtain_only_one_provider_claim(effect_case):
    conn, request, effect, dsn = effect_case
    receipt = _resolve(conn, request, effect)

    def consume(_ordinal):
        with psycopg.connect(dsn, autocommit=True) as worker:
            return external_effects.claim_external_effect(
                worker, **request, resolution_ref=receipt["resolution_ref"]
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(consume, range(2)))
    assert sum(bool(result["should_spawn"]) for result in results) == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM command_audit_log WHERE account_id=%s AND command_type='external_effect.resolution.consume'",
            (request["account_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_parallel_conflicting_decisions_have_one_winner(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, dsn = effect_case

    def decide(action):
        with psycopg.connect(dsn, autocommit=True) as worker:
            try:
                return _resolve(worker, request, effect, action=action)
            except ExternalEffectResolutionError:
                return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(decide, ["approve", "reject"]))
    assert len([value for value in results if value is not None]) == 1
    assert _read(conn, effect)["claim_version"] == effect["claim_version"] + 1


def test_new_ambiguity_requires_a_new_resolution_and_old_ref_cannot_be_reused(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    first = _resolve(conn, request, effect)
    claim = external_effects.claim_external_effect(
        conn, **request, resolution_ref=first["resolution_ref"]
    )
    fresh = external_effect_store.update_external_effect_status(
        conn,
        effect_id=str(effect["effect_id"]),
        claim_token=claim["effect"]["claim_token"],
        status="timeout_unknown",
        status_reason="new_timeout",
    )
    assert not external_effects.claim_external_effect(
        conn, **request, resolution_ref=first["resolution_ref"]
    )["should_spawn"]
    second = _resolve(conn, request, fresh, approval_id="new-approval")
    assert second["resolution_ref"] != first["resolution_ref"]
    assert external_effects.claim_external_effect(
        conn, **request, resolution_ref=second["resolution_ref"]
    )["should_spawn"]
    forged = copy.deepcopy(second["resolution_ref"])
    forged["effect_id"] = str(uuid4())
    with pytest.raises(ExternalEffectResolutionError):
        external_effects.claim_external_effect(conn, **request, resolution_ref=forged)


def test_unknown_consumption_commit_never_returns_permission_to_spawn(effect_case, monkeypatch):
    from core.application.extensions import external_effect_resolution

    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    transaction = external_effect_resolution.transaction_connection

    @contextmanager
    def unknown_commit(db):
        with transaction(db) as tx:
            yield tx
        raise RuntimeError("commit outcome unknown")

    monkeypatch.setattr(external_effect_resolution, "transaction_connection", unknown_commit)
    with pytest.raises(RuntimeError, match="commit outcome unknown"):
        external_effects.claim_external_effect(
            conn, **request, resolution_ref=receipt["resolution_ref"]
        )
    monkeypatch.setattr(external_effect_resolution, "transaction_connection", transaction)
    assert not external_effects.claim_external_effect(
        conn, **request, resolution_ref=receipt["resolution_ref"]
    )["should_spawn"]


def test_projectless_resolution_requires_account_wide_authority(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    with pytest.raises(ExternalEffectResolutionError, match="account_scope_required"):
        _resolve(conn, request, effect, account_wide_authorized=False)
    assert _read(conn, effect)["claim_version"] == effect["claim_version"]


def test_workflow_resolution_requires_exact_host_source(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError

    conn, request, effect, _dsn = effect_case
    with pytest.raises(ExternalEffectResolutionError, match="source_mismatch"):
        _resolve(conn, request, effect, expected_source_ref=None)
    assert _read(conn, effect)["claim_version"] == effect["claim_version"]


def test_approve_preserves_an_authenticated_actual_replay(effect_case):
    from core.application.extensions.external_effect_replay import (
        encrypt_projected_replay,
        hydrate_external_effect_replay,
    )

    conn, request, effect, _dsn = effect_case
    actual = {"status": "failed", "response": {"record_id": "actual-provider-id"}}
    encrypted = encrypt_projected_replay(actual, effect=effect)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO external_effect_replays
                   (effect_id,account_id,replay_state,encrypted_payload,replay_binding_hash,plaintext_bytes)
               VALUES (%s,%s,'available',%s,%s,%s)""",
            (
                effect["effect_id"],
                request["account_id"],
                encrypted["ciphertext"],
                encrypted["binding_hash"],
                encrypted["plaintext_bytes"],
            ),
        )
    _resolve(conn, request, effect, action="approve")
    row = _read(conn, effect)
    assert row["claim_token"] != effect["claim_token"]
    replay = hydrate_external_effect_replay(conn, effect=row)
    assert replay["invocation"] == actual
    assert replay["invocation"]["status"] == "failed"


def test_bad_replay_is_tombstoned_without_using_unverified_provider_data(effect_case):
    conn, request, effect, _dsn = effect_case
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO external_effect_replays
                   (effect_id,account_id,replay_state,encrypted_payload,replay_binding_hash,plaintext_bytes)
               VALUES (%s,%s,'available','corrupt-ciphertext','unverified-binding',12)""",
            (effect["effect_id"], request["account_id"]),
        )
    _resolve(conn, request, effect, action="approve")
    replay = external_effect_store.get_external_effect_replay(
        conn, account_id=request["account_id"], effect_id=str(effect["effect_id"])
    )
    assert replay["replay_state"] == "nonreplayable"
    assert replay["encrypted_payload"] is None
    assert _read(conn, effect)["response_summary_jsonb"] == {"real_partial_data": "preserve"}


def test_consumption_audit_failure_rolls_back_new_provider_claim(effect_case, monkeypatch):
    from core.application.extensions.external_effect_resolution_repository import (
        ExternalEffectResolutionRepository,
    )

    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    monkeypatch.setattr(
        ExternalEffectResolutionRepository,
        "insert_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("consumption audit unavailable")
        ),
    )
    with pytest.raises(RuntimeError, match="consumption audit unavailable"):
        external_effects.claim_external_effect(
            conn, **request, resolution_ref=receipt["resolution_ref"]
        )
    row = _read(conn, effect)
    assert row["claim_version"] == receipt["fenced_version"]
    assert row["status_reason"] == "operator_confirmed_no_effect"


def test_enclosing_job_transaction_owns_resolution_rollback(effect_case):
    from core.infrastructure.db.db_pool import transaction_connection

    conn, request, effect, _dsn = effect_case

    def resolve_then_fail_job_history():
        _resolve(conn, request, effect)
        raise RuntimeError("job history failed")

    with pytest.raises(RuntimeError, match="job history failed"), transaction_connection(conn):
        resolve_then_fail_job_history()
    row = _read(conn, effect)
    assert row["claim_version"] == effect["claim_version"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM command_audit_log WHERE account_id=%s", (request["account_id"],)
        )
        assert cur.fetchone()[0] == 0


def test_consumer_cannot_authorize_spawn_inside_an_uncommitted_job_transaction(effect_case):
    from core.application.extensions.external_effect_resolution import ExternalEffectResolutionError
    from core.infrastructure.db.db_pool import transaction_connection

    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    with (
        transaction_connection(conn),
        pytest.raises(ExternalEffectResolutionError, match="commit_required"),
    ):
        external_effects.claim_external_effect(
            conn, **request, resolution_ref=receipt["resolution_ref"]
        )
    assert _read(conn, effect)["claim_version"] == receipt["fenced_version"]


def test_retry_receipt_retains_original_failure_after_claim_clears_the_row(effect_case):
    conn, request, effect, _dsn = effect_case
    receipt = _resolve(conn, request, effect)
    external_effects.claim_external_effect(
        conn, **request, resolution_ref=receipt["resolution_ref"]
    )
    assert _read(conn, effect)["error_text"] is None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_jsonb FROM command_audit_log WHERE command_id=%s",
            (receipt["resolution_ref"]["command_id"],),
        )
        original = cur.fetchone()[0]["original_failure"]
    assert original["status"] == "timeout_unknown"
    assert original["status_reason"] == "provider_timeout"
    assert original["error_code"] == "provider_timeout"
    assert original["error_text"] == "Original provider response was lost"
    assert '"real_partial_data": "preserve"' in original["response_summary_redacted"]
    assert len(original["response_summary_redacted"]) <= 4096


def test_approve_tombstones_replay_with_unresolved_output_grants_and_keeps_cleanup_record(
    effect_case,
):
    from core.application.extensions.external_effect_replay import encrypt_projected_replay

    conn, request, effect, _dsn = effect_case
    actual = {
        "status": "succeeded",
        "response": {"record_id": "provider-id"},
        "produced_artifacts": [
            {"artifact_id": "old-output", "artifact_handle": "artifact:old-output"}
        ],
    }
    encrypted = encrypt_projected_replay(actual, effect=effect)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO external_effect_replays
                   (effect_id,account_id,replay_state,encrypted_payload,replay_binding_hash,plaintext_bytes)
               VALUES (%s,%s,'available',%s,%s,%s)""",
            (
                effect["effect_id"],
                request["account_id"],
                encrypted["ciphertext"],
                encrypted["binding_hash"],
                encrypted["plaintext_bytes"],
            ),
        )
        cur.execute(
            """INSERT INTO external_effect_output_reconciliations
                   (effect_id,account_id,claim_token,claim_version,minted_artifact_ids_jsonb)
               VALUES (%s,%s,%s,%s,'["old-output"]'::jsonb)""",
            (
                effect["effect_id"],
                request["account_id"],
                effect["claim_token"],
                effect["claim_version"],
            ),
        )
    _resolve(conn, request, effect, action="approve")
    replay = external_effect_store.get_external_effect_replay(
        conn, account_id=request["account_id"], effect_id=str(effect["effect_id"])
    )
    assert replay["replay_state"] == "nonreplayable"
    assert replay["encrypted_payload"] is None
    cleanup = external_effect_store.list_external_effect_output_reconciliations_for_cleanup(
        conn, account_id=request["account_id"]
    )
    assert [row["claim_version"] for row in cleanup] == [effect["claim_version"]]
    assert cleanup[0]["minted_artifact_ids_jsonb"] == ["old-output"]


def test_old_callback_and_cleanup_cannot_resolve_a_fresh_retry_output_lease(effect_case):
    conn, request, effect, _dsn = effect_case
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO external_effect_output_reconciliations
                   (effect_id,account_id,claim_token,claim_version,minted_artifact_ids_jsonb)
               VALUES (%s,%s,%s,%s,'["old-output"]'::jsonb)""",
            (
                effect["effect_id"],
                request["account_id"],
                effect["claim_token"],
                effect["claim_version"],
            ),
        )
    receipt = _resolve(conn, request, effect)
    retry = external_effects.claim_external_effect(
        conn, **request, resolution_ref=receipt["resolution_ref"]
    )["effect"]
    external_effect_store.register_external_effect_output_reconciliation(
        conn,
        effect_id=str(effect["effect_id"]),
        account_id=request["account_id"],
        claim_token=retry["claim_token"],
        minted_artifact_ids=["fresh-output"],
        minted_dataset_ids=[],
    )
    assert not external_effect_store.resolve_external_effect_output_reconciliation(
        conn,
        effect_id=str(effect["effect_id"]),
        account_id=request["account_id"],
        claim_token=effect["claim_token"],
        claim_version=retry["claim_version"],
    )

    class Plane:
        def __init__(self):
            self.removed = []

        def cleanup_output_grant_placeholder_status(self, object_id, *, kind):
            self.removed.append((kind, object_id))
            return "removed"

    plane = Plane()
    assert (
        external_effects.reconcile_external_effect_outputs(
            conn, account_id=request["account_id"], artifact_plane=plane
        )
        == 1
    )
    assert plane.removed == [("artifact", "old-output")]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claim_token,claim_version,minted_artifact_ids_jsonb FROM external_effect_output_reconciliations WHERE effect_id=%s",
            (effect["effect_id"],),
        )
        assert cur.fetchall() == [(retry["claim_token"], retry["claim_version"], ["fresh-output"])]
