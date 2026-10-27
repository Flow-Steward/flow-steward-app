from __future__ import annotations

import os
from uuid import uuid4

import pytest

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.extensions.external_effects import claim_external_effect
from core.infrastructure.db.external_effect_store import update_external_effect_status


@pytest.mark.integration
def test_step_rework_dedupes_committed_effect_and_changed_input_gets_new_identity(
    pg_conn,
    default_account,
) -> None:
    """A reworked step must not replay its already committed provider effect."""
    if os.getenv("FS_AUDIT_ISOLATED") != "1":
        pytest.skip("RS5-003 persistence mutation requires the isolated audit data plane")
    account_id = str(DEFAULT_ACCOUNT_ID)
    project_id = str(uuid4())
    job_id = f"rs5_003_job_{uuid4().hex}"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (id, name, account_id) VALUES (%s, %s, %s)",
            (project_id, f"RS5-003 {project_id}", account_id),
        )
    source_ref = {
        "workflow_id": "rs5_003_outgoing_email",
        "job_id": job_id,
        "step_id": "send_email",
    }
    common = {
        "declaration": {
            "effect_kind": "message_delivery",
            "channel": "email",
        },
        "account_id": account_id,
        "project_id": project_id,
        "extension_id": "flow-steward.outgoing-email",
        "operation_id": "send_email",
        "source_type": "workflow_step",
        "source_ref": source_ref,
    }
    original_payload = {
        "to": ["reviewer@example.invalid"],
        "subject": "Release candidate",
        "body": "Candidate is ready.",
    }

    first = claim_external_effect(
        pg_conn,
        request_payload=original_payload,
        **common,
    )
    assert first["should_spawn"] is True
    first_effect = first["effect"]
    committed = update_external_effect_status(
        pg_conn,
        effect_id=str(first_effect["effect_id"]),
        claim_token=str(first_effect["claim_token"]),
        status="succeeded",
        status_reason="provider_confirmed",
        provider_message_id="mailpit-message-1",
    )
    assert committed
    assert committed["status"] == "succeeded"
    pg_conn.commit()

    # This is the same logical step after a rejection/rework edge and a
    # process boundary. The durable claim must suppress a second provider call.
    replay = claim_external_effect(
        pg_conn,
        request_payload=original_payload,
        **common,
    )
    assert replay["should_spawn"] is False
    assert replay["deduped"] is True
    assert replay["effect"]["effect_id"] == first_effect["effect_id"]
    assert replay["effect"]["status"] == "succeeded"

    changed = claim_external_effect(
        pg_conn,
        request_payload={**original_payload, "body": "Candidate needs one explicit revision."},
        **common,
    )
    assert changed["should_spawn"] is True
    assert changed["effect"]["effect_id"] != first_effect["effect_id"]
    assert changed["effect"]["request_fingerprint"] != first_effect["request_fingerprint"]

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, provider_message_id
              FROM external_effects
             WHERE account_id = %s
               AND project_id = %s
               AND source_ref_jsonb->>'job_id' = %s
             ORDER BY created_at
            """,
            (account_id, project_id, job_id),
        )
        rows = cur.fetchall()
    assert rows == [("succeeded", "mailpit-message-1"), ("claimed", None)]
    pg_conn.rollback()
