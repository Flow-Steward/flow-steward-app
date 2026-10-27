from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.external_effect_replay import (
    ExternalEffectReplayPersistenceError,
    hydrate_external_effect_claim,
    hydrate_external_effect_replay,
)
from core.application.extensions.external_effects import (
    ExternalEffectConfirmationLeaseLostError,
    claim_external_effect,
    confirm_external_effect_from_invocation,
    mark_external_effect_running,
    mark_external_effect_suppressed,
)
from core.application.extensions.uninstall_cleanup_queries import (
    ExtensionUninstallCleanupQueries,
)
from core.infrastructure.db.db_pool import (
    register_transaction_after_commit,
    transaction_connection,
)
from core.infrastructure.db.external_effect_store import (
    cleanup_stale_external_effects,
    get_exact_committed_external_effect_replay,
    list_external_effect_output_reconciliations_for_cleanup,
    register_external_effect_output_reconciliation,
    resolve_external_effect_output_reconciliation,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _external_effect_identity_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")


def _extension() -> dict:
    operation = {
        "operation_id": "send",
        "outputs": [
            {"name": "message_id", "value_type": "text"},
            {"name": "receipt", "value_type": "object"},
        ],
    }
    return {
        "extension_key": "fixture.encrypted-replay",
        "operation_manifest": [operation],
        "compiled_manifest": {"operation_manifest": [dict(operation)]},
    }


def _seed_project(conn, *, account_id: str = str(DEFAULT_ACCOUNT_ID)) -> str:
    project_id = str(uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"encrypted-replay-{project_id[:8]}", account_id),
        )
    return project_id


def _claim(
    conn,
    *,
    project_id: str,
    marker: str,
    account_id: str = str(DEFAULT_ACCOUNT_ID),
) -> dict:
    return claim_external_effect(
        conn,
        declaration={"effect_kind": "message_delivery", "channel": "api"},
        account_id=account_id,
        project_id=project_id,
        extension_id="fixture.encrypted-replay",
        operation_id="send",
        source_type="workflow_step",
        source_ref={"job_id": f"job-{marker}", "step_id": "send"},
        request_payload={"marker": marker},
    )


def _confirm(
    conn,
    claim: dict,
    *,
    message_id: str,
    account_id: str = str(DEFAULT_ACCOUNT_ID),
) -> dict:
    effect = claim["effect"]
    # Production uses the autocommit pooled proxy here, allowing the replay
    # store to own and observe its root transaction. Keep this raw integration
    # connection production-like without weakening the ambient-txn guard.
    conn.commit()
    conn.autocommit = True
    try:
        running = mark_external_effect_running(
            conn,
            effect_id=str(effect["effect_id"]),
            claim_token=str(effect["claim_token"]),
        )
        assert running
        assert running["status"] == "running"
        invocation = {
            "status": "succeeded",
            "detail": "provider free text is not replayed",
            "response": {
                "ok": True,
                "result": {
                    "message_id": message_id,
                    "receipt": {"accepted": True},
                    "undeclared": "drop",
                },
            },
        }
        confirmed = confirm_external_effect_from_invocation(
            conn,
            effect_id=str(effect["effect_id"]),
            claim_token=str(effect["claim_token"]),
            account_id=account_id,
            extension=_extension(),
            operation_id="send",
            invocation=invocation,
            protected_values=frozenset({"provider-secret-never-returned"}),
            expected_project_id=str(effect["project_id"]),
            expected_connection_id=None,
        )
        assert confirmed
        assert confirmed["status"] == "succeeded"
        return invocation
    finally:
        conn.autocommit = False


def test_postgres_replays_exact_projected_success_without_spawning(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)
    first = _claim(disposable_db_conn, project_id=project_id, marker="exact")
    assert first["should_spawn"] is True
    _confirm(disposable_db_conn, first, message_id="message-42")

    duplicate = _claim(disposable_db_conn, project_id=project_id, marker="exact")
    assert duplicate["should_spawn"] is False
    hydrated = hydrate_external_effect_claim(disposable_db_conn, duplicate)

    assert hydrated.get("error", "") == ""
    assert hydrated["replay_invocation"] == {
        "status": "succeeded",
        "response": {
            "ok": True,
            "result": {
                "message_id": "message-42",
                "receipt": {"accepted": True},
            },
        },
    }


def test_postgres_output_reconciliation_is_attempt_fenced_and_tenant_scoped(
    disposable_db_conn,
) -> None:
    account_id = str(DEFAULT_ACCOUNT_ID)
    project_id = _seed_project(disposable_db_conn)
    first = _claim(disposable_db_conn, project_id=project_id, marker="reconcile")
    effect_id = str(first["effect"]["effect_id"])
    first_token = str(first["effect"]["claim_token"])
    first_version = int(first["effect"]["claim_version"])
    register_external_effect_output_reconciliation(
        disposable_db_conn,
        effect_id=effect_id,
        account_id=account_id,
        claim_token=first_token,
        minted_artifact_ids=["artifact-v1"],
        minted_dataset_ids=[],
    )
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "UPDATE external_effects SET status = 'failed' WHERE effect_id = %s::uuid",
            (effect_id,),
        )

    second = _claim(disposable_db_conn, project_id=project_id, marker="reconcile")
    second_token = str(second["effect"]["claim_token"])
    second_version = int(second["effect"]["claim_version"])
    assert second_version == first_version + 1
    register_external_effect_output_reconciliation(
        disposable_db_conn,
        effect_id=effect_id,
        account_id=account_id,
        claim_token=second_token,
        minted_artifact_ids=["artifact-v2"],
        minted_dataset_ids=[],
    )
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT claim_version, minted_artifact_ids_jsonb
              FROM external_effect_output_reconciliations
             WHERE effect_id = %s::uuid
             ORDER BY claim_version
            """,
            (effect_id,),
        )
        assert cur.fetchall() == [
            (first_version, ["artifact-v1"]),
            (second_version, ["artifact-v2"]),
        ]

    assert not resolve_external_effect_output_reconciliation(
        disposable_db_conn,
        effect_id=effect_id,
        account_id=account_id,
        claim_token=first_token,
        claim_version=second_version,
    )
    _confirm(disposable_db_conn, second, message_id="message-v2")
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT claim_version, minted_artifact_ids_jsonb
              FROM external_effect_output_reconciliations
             WHERE effect_id = %s::uuid
            """,
            (effect_id,),
        )
        assert cur.fetchall() == [(first_version, ["artifact-v1"])]
        cur.execute(
            """
            SELECT encrypted_payload, replay_binding_hash, plaintext_bytes
              FROM external_effect_replays
             WHERE effect_id = %s::uuid AND account_id = %s::uuid
            """,
            (effect_id, account_id),
        )
        ciphertext, binding_hash, plaintext_bytes = cur.fetchone()

    cleanup_rows = list_external_effect_output_reconciliations_for_cleanup(
        disposable_db_conn,
        account_id=account_id,
    )
    assert [row["claim_version"] for row in cleanup_rows] == [first_version]
    assert (
        list_external_effect_output_reconciliations_for_cleanup(
            disposable_db_conn,
            account_id=str(uuid4()),
        )
        == []
    )

    exact = get_exact_committed_external_effect_replay(
        disposable_db_conn,
        effect=second["effect"],
        replay_ciphertext=ciphertext,
        replay_binding_hash=binding_hash,
        replay_plaintext_bytes=plaintext_bytes,
    )
    assert exact
    assert exact["status"] == "succeeded"
    mismatches = (
        {"account_id": str(uuid4())},
        {"claim_token": str(uuid4())},
        {"claim_version": first_version},
        {"request_fingerprint": "different-request-fingerprint"},
    )
    for mismatch in mismatches:
        assert (
            get_exact_committed_external_effect_replay(
                disposable_db_conn,
                effect={**second["effect"], **mismatch},
                replay_ciphertext=ciphertext,
                replay_binding_hash=binding_hash,
                replay_plaintext_bytes=plaintext_bytes,
            )
            is None
        )
    assert (
        get_exact_committed_external_effect_replay(
            disposable_db_conn,
            effect=second["effect"],
            replay_ciphertext=f"{ciphertext}-mismatch",
            replay_binding_hash=binding_hash,
            replay_plaintext_bytes=plaintext_bytes,
        )
        is None
    )

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'external_effect_output_reconciliations'::regclass
               AND contype IN ('p', 'f')
             ORDER BY contype
            """
        )
        definitions = "\n".join(row[0] for row in cur.fetchall())
        assert "PRIMARY KEY (effect_id, account_id, claim_version)" in definitions
        assert "FOREIGN KEY (effect_id, account_id)" in definitions
        assert "ON DELETE CASCADE" in definitions
        cur.execute("DELETE FROM external_effects WHERE effect_id = %s::uuid", (effect_id,))
        cur.execute(
            "SELECT count(*) FROM external_effect_output_reconciliations WHERE effect_id = %s::uuid",
            (effect_id,),
        )
        assert cur.fetchone() == (0,)


def test_postgres_replay_confirmation_rejects_ambient_transaction_without_replay(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="rollback")
    effect = claim["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
    )
    disposable_db_conn.commit()

    with transaction_connection(disposable_db_conn):
        _raises_input_335_1 = str(effect["effect_id"])
        _raises_input_335_2 = str(effect["claim_token"])
        _raises_input_335_3 = str(DEFAULT_ACCOUNT_ID)
        _raises_input_335_4 = _extension()
        with pytest.raises(ExternalEffectReplayPersistenceError) as exc_info:
            confirm_external_effect_from_invocation(
                disposable_db_conn,
                effect_id=_raises_input_335_1,
                claim_token=_raises_input_335_2,
                account_id=_raises_input_335_3,
                extension=_raises_input_335_4,
                operation_id="send",
                expected_project_id=project_id,
                invocation={
                    "status": "succeeded",
                    "response": {"result": {"message_id": "not-persisted"}},
                },
            )
        assert exc_info.value.commit_outcome == "unknown"

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM external_effects WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == ("timeout_unknown",)
        cur.execute(
            "SELECT count(*) FROM external_effect_replays WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == (0,)


def test_postgres_replay_write_failure_rolls_back_atomically_and_is_classified(
    disposable_db_conn,
) -> None:
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="trigger-rollback")
    effect = claim["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
    )
    disposable_db_conn.commit()
    disposable_db_conn.autocommit = True
    try:
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                """
                CREATE FUNCTION reject_test_external_effect_success() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.status = 'succeeded' THEN
                        RAISE EXCEPTION 'injected success write failure';
                    END IF;
                    RETURN NEW;
                END
                $$
                """
            )
            cur.execute(
                """
                CREATE TRIGGER reject_test_external_effect_success
                BEFORE UPDATE ON external_effects
                FOR EACH ROW EXECUTE FUNCTION reject_test_external_effect_success()
                """
            )

        _raises_input_400_1 = str(effect["effect_id"])
        _raises_input_400_2 = str(effect["claim_token"])
        _raises_input_400_3 = str(DEFAULT_ACCOUNT_ID)
        _raises_input_400_4 = _extension()
        with pytest.raises(ExternalEffectReplayPersistenceError) as exc_info:
            confirm_external_effect_from_invocation(
                disposable_db_conn,
                effect_id=_raises_input_400_1,
                claim_token=_raises_input_400_2,
                account_id=_raises_input_400_3,
                extension=_raises_input_400_4,
                operation_id="send",
                expected_project_id=project_id,
                invocation={
                    "status": "succeeded",
                    "response": {
                        "result": {"message_id": "rolled-back", "receipt": {"accepted": True}}
                    },
                },
            )
        assert exc_info.value.commit_outcome == "rolled_back"
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM external_effects WHERE effect_id = %s::uuid",
                (str(effect["effect_id"]),),
            )
            assert cur.fetchone() == ("timeout_unknown",)
            cur.execute(
                "SELECT count(*) FROM external_effect_replays WHERE effect_id = %s::uuid",
                (str(effect["effect_id"]),),
            )
            assert cur.fetchone() == (0,)
    finally:
        with disposable_db_conn.cursor() as cur:
            cur.execute(
                "DROP TRIGGER IF EXISTS reject_test_external_effect_success ON external_effects"
            )
            cur.execute("DROP FUNCTION IF EXISTS reject_test_external_effect_success()")
        disposable_db_conn.autocommit = False


def test_postgres_after_commit_callback_runs_only_after_real_commit(disposable_db_conn) -> None:
    observed: list[str] = []
    disposable_db_conn.commit()
    disposable_db_conn.autocommit = True
    try:
        with transaction_connection(disposable_db_conn) as conn:
            assert register_transaction_after_commit(conn, lambda: observed.append("committed"))
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        assert observed == ["committed"]

        def _raises_operation_430():
            with transaction_connection(disposable_db_conn) as conn:
                assert register_transaction_after_commit(
                    conn, lambda: observed.append("must-not-run")
                )
                raise RuntimeError("force rollback")

        with pytest.raises(RuntimeError, match="force rollback"):
            _raises_operation_430()
        assert observed == ["committed"]
    finally:
        disposable_db_conn.autocommit = False


def test_postgres_replay_rejects_tamper_scope_swap_and_expires_ciphertext(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)

    tampered = _claim(disposable_db_conn, project_id=project_id, marker="tampered")
    _confirm(disposable_db_conn, tampered, message_id="message-tampered")
    tampered_effect = tampered["effect"]
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE external_effect_replays
               SET encrypted_payload = overlay(encrypted_payload placing 'x' from 4 for 1)
             WHERE effect_id = %s
            """,
            (str(tampered_effect["effect_id"]),),
        )
    corrupt = hydrate_external_effect_replay(disposable_db_conn, effect=tampered_effect)
    assert corrupt["error"] == "external_effect_replay_corrupt"

    swapped = _claim(disposable_db_conn, project_id=project_id, marker="swapped")
    _confirm(disposable_db_conn, swapped, message_id="message-swapped")
    swapped_effect = dict(swapped["effect"])
    swapped_effect["project_id"] = str(uuid4())
    mismatch = hydrate_external_effect_replay(disposable_db_conn, effect=swapped_effect)
    assert mismatch["error"] == "external_effect_replay_binding_mismatch"

    for field, replacement in (
        ("extension_install_id", "different-install"),
        ("connection_id", "different-connection"),
    ):
        rebound_effect = dict(swapped["effect"])
        rebound_effect[field] = replacement
        rebound = hydrate_external_effect_replay(disposable_db_conn, effect=rebound_effect)
        assert rebound["error"] == "external_effect_replay_binding_mismatch"

    other_account_effect = dict(swapped["effect"])
    other_account_effect["account_id"] = str(uuid4())
    other_account = hydrate_external_effect_replay(
        disposable_db_conn,
        effect=other_account_effect,
    )
    assert other_account["error"] == "external_effect_replay_missing"

    expiring = _claim(disposable_db_conn, project_id=project_id, marker="expiring")
    _confirm(disposable_db_conn, expiring, message_id="message-expiring")
    expiring_effect = expiring["effect"]
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb)
            VALUES (%s, 'retention_policy', '{"action_logs_days": 1}'::jsonb)
            ON CONFLICT (account_id, setting_key)
            DO UPDATE SET setting_value_jsonb = EXCLUDED.setting_value_jsonb
            """,
            (str(DEFAULT_ACCOUNT_ID),),
        )
        cur.execute(
            """
            UPDATE external_effect_replays
               SET updated_at = now() - interval '31 days'
             WHERE effect_id = %s
            """,
            (str(expiring_effect["effect_id"]),),
        )
    cleanup = cleanup_stale_external_effects(
        disposable_db_conn,
        account_id=str(DEFAULT_ACCOUNT_ID),
    )
    assert cleanup["replay_payloads_expired"] >= 1
    expired = hydrate_external_effect_replay(disposable_db_conn, effect=expiring_effect)
    assert expired["error"] == "external_effect_replay_expired"
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM external_effects WHERE effect_id = %s",
            (str(expiring_effect["effect_id"]),),
        )
        assert cur.fetchone() == (1,)


def test_postgres_claim_cleanup_does_not_expire_another_accounts_replay(
    disposable_db_conn,
) -> None:
    account_b = str(uuid4())
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO accounts (id, account_key, display_name)
            VALUES (%s, %s, 'Replay isolation account')
            """,
            (account_b, f"replay-isolation-{account_b[:8]}"),
        )
    project_a = _seed_project(disposable_db_conn)
    project_b = _seed_project(disposable_db_conn, account_id=account_b)
    replay_a = _claim(disposable_db_conn, project_id=project_a, marker="retention-a")
    replay_b = _claim(
        disposable_db_conn,
        project_id=project_b,
        marker="retention-b",
        account_id=account_b,
    )
    _confirm(disposable_db_conn, replay_a, message_id="message-a")
    _confirm(disposable_db_conn, replay_b, message_id="message-b", account_id=account_b)
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_settings (account_id, setting_key, setting_value_jsonb)
            VALUES
                (%s, 'retention_policy', '{"action_logs_days": 1}'::jsonb),
                (%s, 'retention_policy', '{"action_logs_days": 1}'::jsonb)
            ON CONFLICT (account_id, setting_key)
            DO UPDATE SET setting_value_jsonb = EXCLUDED.setting_value_jsonb
            """,
            (str(DEFAULT_ACCOUNT_ID), account_b),
        )
        cur.execute(
            """
            UPDATE external_effect_replays
               SET updated_at = now() - interval '31 days'
             WHERE effect_id IN (%s, %s)
            """,
            (replay_a["effect"]["effect_id"], replay_b["effect"]["effect_id"]),
        )

    cleanup_stale_external_effects(
        disposable_db_conn,
        account_id=str(DEFAULT_ACCOUNT_ID),
    )

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT account_id::text, replay_state
              FROM external_effect_replays
             WHERE effect_id IN (%s, %s)
             ORDER BY account_id::text
            """,
            (replay_a["effect"]["effect_id"], replay_b["effect"]["effect_id"]),
        )
        states = dict(cur.fetchall())
    assert states[str(DEFAULT_ACCOUNT_ID)] == "nonreplayable"
    assert states[account_b] == "available"


def test_postgres_replay_hot_paths_use_uuid_indexes(disposable_db_conn) -> None:
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="planner")
    _confirm(disposable_db_conn, claim, message_id="message-planner")
    effect_id = str(claim["effect"]["effect_id"])

    with disposable_db_conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        try:
            cur.execute(
                """
                EXPLAIN (FORMAT TEXT)
                SELECT effect_id
                  FROM external_effect_replays
                 WHERE effect_id = %s::uuid
                   AND account_id = %s::uuid
                """,
                (effect_id, str(DEFAULT_ACCOUNT_ID)),
            )
            replay_plan = "\n".join(row[0] for row in cur.fetchall())
            cur.execute(
                """
                EXPLAIN (FORMAT TEXT)
                UPDATE external_effects
                   SET updated_at = updated_at
                 WHERE effect_id = %s::uuid
                """,
                (effect_id,),
            )
            effect_plan = "\n".join(row[0] for row in cur.fetchall())
        finally:
            cur.execute("RESET enable_seqscan")

    assert "Index Scan" in replay_plan
    assert "external_effect_replays_pkey" in replay_plan
    assert "Index Scan" in effect_plan
    assert "Index Cond: (effect_id =" in effect_plan
    assert "::uuid" in effect_plan


def test_postgres_only_one_concurrent_confirm_can_win(
    disposable_db,
    monkeypatch,
) -> None:
    import psycopg

    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    with psycopg.connect(disposable_db.dsn) as setup:
        project_id = _seed_project(setup)
        claim = _claim(setup, project_id=project_id, marker="concurrent")
        effect = claim["effect"]
        assert mark_external_effect_running(
            setup,
            effect_id=str(effect["effect_id"]),
            claim_token=str(effect["claim_token"]),
        )
        setup.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def confirm_once() -> None:
        with psycopg.connect(disposable_db.dsn) as conn:
            conn.autocommit = True
            barrier.wait()
            try:
                confirm_external_effect_from_invocation(
                    conn,
                    effect_id=str(effect["effect_id"]),
                    claim_token=str(effect["claim_token"]),
                    account_id=str(DEFAULT_ACCOUNT_ID),
                    extension=_extension(),
                    operation_id="send",
                    expected_project_id=project_id,
                    invocation={
                        "status": "succeeded",
                        "response": {"result": {"message_id": "concurrent-message"}},
                    },
                )
            except ExternalEffectConfirmationLeaseLostError:
                outcomes.append("lease_lost")
            else:
                outcomes.append("succeeded")

    threads = [threading.Thread(target=confirm_once, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcomes) == ["lease_lost", "succeeded"]
    with psycopg.connect(disposable_db.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM external_effects WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == ("succeeded",)
        cur.execute(
            "SELECT count(*) FROM external_effect_replays WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == (1,)


def test_postgres_uninstall_purges_ciphertext_and_leaves_typed_tombstone(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="uninstall")
    _confirm(disposable_db_conn, claim, message_id="message-uninstall")
    effect = claim["effect"]
    removed_data: list[str] = []
    context = SimpleNamespace(
        account_id=str(DEFAULT_ACCOUNT_ID),
        extension_id="fixture.encrypted-replay",
        bl=SimpleNamespace(
            _table_exists=lambda table: table in {"external_effects", "external_effect_replays"}
        ),
    )
    target = SimpleNamespace(include_consumer_accounts=False)
    state = SimpleNamespace(install_id="uninstalled-install")

    with disposable_db_conn.cursor() as cur:
        ExtensionUninstallCleanupQueries()._detach_external_effects(
            context,
            target,
            state,
            cur,
            removed_data,
        )

    decision = hydrate_external_effect_replay(disposable_db_conn, effect=effect)
    assert decision["error"] == "external_effect_replay_extension_uninstalled"
    assert "external_effect_replay_payloads" in removed_data
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT encrypted_payload, replay_state, replay_reason FROM external_effect_replays WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == (None, "nonreplayable", "extension_uninstalled")


def test_postgres_direct_intent_key_reuse_with_changed_payload_is_a_conflict(
    disposable_db_conn,
) -> None:
    project_id = _seed_project(disposable_db_conn)
    common = {
        "declaration": {"effect_kind": "message_delivery", "channel": "email"},
        "account_id": str(DEFAULT_ACCOUNT_ID),
        "project_id": project_id,
        "extension_id": "fixture.encrypted-replay",
        "extension_install_id": None,
        "connection_id": None,
        "operation_id": "send",
        "source_type": "api_action",
        "source_ref": {"direct_action_intent_hash": "intent-digest"},
        "test_mode": False,
        "idempotency_key_override": "intent-digest",
    }

    first = claim_external_effect(
        disposable_db_conn,
        **common,
        request_payload={"subject": "first payload"},
    )
    other_project_id = _seed_project(disposable_db_conn)
    conflict = claim_external_effect(
        disposable_db_conn,
        **{**common, "project_id": other_project_id},
        request_payload={"subject": "changed payload"},
    )

    assert first["should_spawn"] is True
    assert conflict["should_spawn"] is False
    assert conflict["error"] == "external_effect_idempotency_conflict"
    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM external_effects WHERE account_id = %s AND idempotency_key = %s",
            (str(DEFAULT_ACCOUNT_ID), "intent-digest"),
        )
        assert cur.fetchone() == (1,)


def test_postgres_non_success_confirmation_rejects_stale_claim_token(
    disposable_db_conn,
) -> None:
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="stale-terminal")
    effect = claim["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
    )

    _raises_input_805_1 = str(effect["effect_id"])
    with pytest.raises(ExternalEffectConfirmationLeaseLostError):
        confirm_external_effect_from_invocation(
            disposable_db_conn,
            effect_id=_raises_input_805_1,
            claim_token="stale-claim-token",
            invocation={
                "status": "succeeded",
                "response": {"external_effect_status": "timeout_unknown"},
            },
        )

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM external_effects WHERE effect_id = %s",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone() == ("running",)


def test_postgres_provider_reported_suppression_fails_closed_without_respawn(
    disposable_db_conn,
) -> None:
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="provider-suppressed")
    effect = claim["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
    )

    confirmed = confirm_external_effect_from_invocation(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
        invocation={
            "status": "succeeded",
            "response": {"ok": True, "external_effect_status": "suppressed"},
        },
    )
    duplicate = _claim(
        disposable_db_conn,
        project_id=project_id,
        marker="provider-suppressed",
    )

    assert confirmed
    assert confirmed["status"] == "timeout_unknown"
    assert confirmed["error_code"] == "external_effect_metadata_conflict"
    assert duplicate["should_spawn"] is False
    assert duplicate["effect"]["status"] == "timeout_unknown"


def test_postgres_external_effect_terminal_states_are_irreversible(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)

    successful = _claim(disposable_db_conn, project_id=project_id, marker="terminal-success")
    success_effect = successful["effect"]
    _confirm(disposable_db_conn, successful, message_id="terminal-success")
    _raises_input_868_1 = str(success_effect["effect_id"])
    _raises_input_868_2 = str(success_effect["claim_token"])
    with pytest.raises(ExternalEffectConfirmationLeaseLostError):
        confirm_external_effect_from_invocation(
            disposable_db_conn,
            effect_id=_raises_input_868_1,
            claim_token=_raises_input_868_2,
            invocation={
                "status": "succeeded",
                "response": {"external_effect_status": "timeout_unknown"},
            },
        )
    assert (
        mark_external_effect_running(
            disposable_db_conn,
            effect_id=str(success_effect["effect_id"]),
            claim_token=str(success_effect["claim_token"]),
        )
        is None
    )
    assert (
        mark_external_effect_suppressed(
            disposable_db_conn,
            effect_id=str(success_effect["effect_id"]),
            claim_token=str(success_effect["claim_token"]),
        )
        is None
    )

    uncertain = _claim(disposable_db_conn, project_id=project_id, marker="terminal-uncertain")
    uncertain_effect = uncertain["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(uncertain_effect["effect_id"]),
        claim_token=str(uncertain_effect["claim_token"]),
    )
    confirmed_uncertain = confirm_external_effect_from_invocation(
        disposable_db_conn,
        effect_id=str(uncertain_effect["effect_id"]),
        claim_token=str(uncertain_effect["claim_token"]),
        invocation={
            "status": "succeeded",
            "response": {"external_effect_status": "timeout_unknown"},
        },
    )
    assert confirmed_uncertain
    assert confirmed_uncertain["status"] == "timeout_unknown"
    _raises_input_913_1 = str(uncertain_effect["effect_id"])
    _raises_input_913_2 = str(uncertain_effect["claim_token"])
    _raises_input_913_3 = str(DEFAULT_ACCOUNT_ID)
    _raises_input_913_4 = _extension()
    with pytest.raises(ExternalEffectReplayPersistenceError):
        confirm_external_effect_from_invocation(
            disposable_db_conn,
            effect_id=_raises_input_913_1,
            claim_token=_raises_input_913_2,
            account_id=_raises_input_913_3,
            extension=_raises_input_913_4,
            operation_id="send",
            invocation={
                "status": "succeeded",
                "response": {"result": {"message_id": "late-success"}},
            },
        )

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT effect_id, status FROM external_effects WHERE effect_id = ANY(%s::uuid[]) ORDER BY effect_id",
            ([str(success_effect["effect_id"]), str(uncertain_effect["effect_id"])],),
        )
        statuses = {str(effect_id): status for effect_id, status in cur.fetchall()}
    assert statuses[str(success_effect["effect_id"])] == "succeeded"
    assert statuses[str(uncertain_effect["effect_id"])] == "timeout_unknown"


def test_postgres_pre_spawn_failure_transitions_claimed_and_retries_immediately(
    disposable_db_conn,
) -> None:
    project_id = _seed_project(disposable_db_conn)
    first = _claim(disposable_db_conn, project_id=project_id, marker="pre-spawn-failure")
    effect = first["effect"]

    confirmed = confirm_external_effect_from_invocation(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
        invocation={
            "status": "failed",
            "detail": "pre_dispatch_persistence_failed",
            "response": {
                "ok": False,
                "external_effect_status": "failed",
                "definitely_no_external_effect": True,
            },
        },
        expected_current_status="claimed",
    )

    assert confirmed
    assert confirmed["status"] == "failed"
    _raises_input_962_1 = str(effect["effect_id"])
    with pytest.raises(ExternalEffectConfirmationLeaseLostError):
        confirm_external_effect_from_invocation(
            disposable_db_conn,
            effect_id=_raises_input_962_1,
            claim_token="stale-claim-token",
            invocation={
                "status": "failed",
                "response": {
                    "external_effect_status": "failed",
                    "definitely_no_external_effect": True,
                },
            },
            expected_current_status="claimed",
        )

    retry = _claim(disposable_db_conn, project_id=project_id, marker="pre-spawn-failure")
    assert retry["should_spawn"] is True
    assert retry["effect"]["status"] == "claimed"
    assert retry["effect"]["claim_token"] != effect["claim_token"]


def test_postgres_success_confirmation_rejects_operation_binding_mismatch(
    disposable_db_conn,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ENCRYPTION_KEY", "integration-encrypted-replay-root")
    project_id = _seed_project(disposable_db_conn)
    claim = _claim(disposable_db_conn, project_id=project_id, marker="operation-mismatch")
    effect = claim["effect"]
    assert mark_external_effect_running(
        disposable_db_conn,
        effect_id=str(effect["effect_id"]),
        claim_token=str(effect["claim_token"]),
    )

    _raises_input_997_1 = str(effect["effect_id"])
    _raises_input_997_2 = str(effect["claim_token"])
    _raises_input_997_3 = str(DEFAULT_ACCOUNT_ID)
    with pytest.raises(ExternalEffectReplayPersistenceError):
        confirm_external_effect_from_invocation(
            disposable_db_conn,
            effect_id=_raises_input_997_1,
            claim_token=_raises_input_997_2,
            account_id=_raises_input_997_3,
            extension={
                "extension_key": "fixture.encrypted-replay",
                "operation_manifest": [
                    {"operation_id": "broader-operation", "outputs": [{"name": "receipt"}]}
                ],
            },
            operation_id="broader-operation",
            expected_project_id=project_id,
            invocation={"status": "succeeded", "response": {"receipt": {"all": "data"}}},
        )

    with disposable_db_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM external_effects WHERE effect_id = %s::uuid",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone()[0] == "timeout_unknown"
        cur.execute(
            "SELECT count(*) FROM external_effect_replays WHERE effect_id = %s::uuid",
            (str(effect["effect_id"]),),
        )
        assert cur.fetchone()[0] == 0
