"""Retain approved catalog-secret use across a trusted extension release transition."""

from __future__ import annotations

import json
from typing import Any

from core.application.secrets import repository as secret_repository
from core.application.secrets.approval_gate import catalog_marker
from core.application.secrets.projection import connection_projection, projection_fingerprint
from core.application.secrets.workflow_gate import approved_receivers


def _carry_connections(
    cur: Any,
    *,
    account_id: str,
    install_id: str,
    previous: dict[str, Any],
    candidate: dict[str, Any],
) -> int:
    connection_ids = secret_repository.lock_connections_with_approvals(
        cur, account_id=account_id, install_id=install_id
    )
    carried = 0
    for connection_id in connection_ids:
        connection = secret_repository.lock_connection(
            cur, account_id=account_id, connection_id=connection_id, for_update=False
        )
        if connection is None:
            continue
        bindings = secret_repository.connection_bindings(
            cur, account_id=account_id, connection_id=connection_id
        )
        if not bindings or bindings != catalog_marker(connection["secret_refs"]):
            continue
        old_projection = connection_projection(connection, bindings=bindings, release=previous)
        projection = {**old_projection, "release": candidate}
        cur.execute(
            """
            UPDATE catalog_secret_approvals
               SET fingerprint = %s, projection_jsonb = %s::jsonb
             WHERE project_connection_id = %s AND account_id::text = %s AND fingerprint = %s
            """,
            (
                projection_fingerprint(projection),
                json.dumps(projection),
                connection_id,
                account_id,
                projection_fingerprint(old_projection),
            ),
        )
        carried += cur.rowcount
    return carried


def _carry_workflows(
    cur: Any,
    *,
    account_id: str,
    install_id: str,
    previous: dict[str, Any],
    candidate: dict[str, Any],
) -> int:
    old_identity = {"install_id": install_id, **previous}
    approvals = secret_repository.lock_workflow_approvals_for_receiver(
        cur, account_id=account_id, receiver=old_identity
    )
    carried = 0
    for approval in approvals:
        receivers = approved_receivers(approval)
        additions = []
        for receiver in receivers:
            if receiver.get("kind") not in {"extension", "tool_extension"}:
                continue
            if not all(receiver.get(key) == value for key, value in old_identity.items()):
                continue
            updated = {**receiver, **candidate}
            if updated not in receivers and updated not in additions:
                additions.append(updated)
        if not additions:
            continue
        # A run may already hold a pinned invocation of the outgoing release.
        projection = {**approval["projection_jsonb"], "receivers": [*receivers, *additions]}
        cur.execute(
            "UPDATE catalog_secret_approvals SET projection_jsonb = %s::jsonb WHERE id = %s",
            (json.dumps(projection), approval["id"]),
        )
        carried += cur.rowcount
    return carried


def carry_release_approvals(
    cur: Any,
    *,
    account_id: str,
    install_id: str,
    previous: dict[str, Any],
    candidate: dict[str, Any],
    repository: str,
    actor: str,
    actor_kind: str,
) -> None:
    """Carry only approvals for the exact outgoing release of this install."""
    if not all(previous.values()) or not all(candidate.values()) or previous == candidate:
        return
    connection_count = _carry_connections(
        cur, account_id=account_id, install_id=install_id, previous=previous, candidate=candidate
    )
    workflow_count = _carry_workflows(
        cur, account_id=account_id, install_id=install_id, previous=previous, candidate=candidate
    )
    secret_repository.record_audit(
        cur,
        account_id=account_id,
        command_type="secret.carry_release_approvals",
        actor_id=actor,
        actor_kind=actor_kind,
        target={"install_id": install_id},
        payload={
            "repository": repository,
            "previous": previous,
            "candidate": candidate,
            "connections": connection_count,
            "workflows": workflow_count,
        },
    )
