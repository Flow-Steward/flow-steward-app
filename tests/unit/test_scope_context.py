from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.scope import normalize_scope, scope_from_principal, scope_from_state


def test_scope_from_principal_preserves_single_account_default():
    scope = scope_from_principal(None)

    assert scope.account_id == DEFAULT_ACCOUNT_ID
    assert scope.actor_id == "system"
    assert scope.principal_type == "system_actor"


def test_scope_from_state_normalizes_project_and_workflow_identifiers():
    scope = scope_from_state(
        {
            "account_id": "acc_42",
            "project_id": "7",
            "selected_workflow": "wf-review",
            "actor_username": "alice",
            "principal_type": "user",
        }
    )

    assert scope.account_id == "acc_42"
    assert scope.project_id == "7"
    assert scope.workflow_id == "wf-review"
    assert scope.actor_id == "alice"
    assert scope.principal_type == "user"


def test_normalize_scope_overrides_project_without_losing_account():
    scope = normalize_scope({"account_id": "acc_99", "project_id": 3}, project_id="11")

    assert scope.account_id == "acc_99"
    assert scope.project_id == "11"


def test_scope_from_principal_maps_interactive_roles_to_role_aware_principal_type():
    cases = [
        ("admin", ["admin"]),
        ("operator", ["operator"]),
        ("viewer", ["viewer"]),
    ]

    for expected, roles in cases:
        scope = scope_from_principal(
            {
                "username": f"{expected}@example.com",
                "roles": roles,
                "auth_source": "local",
                "account_id": "acc_roles",
            }
        )

        assert scope.account_id == "acc_roles"
        assert scope.actor_id == f"{expected}@example.com"
        assert scope.principal_type == expected
