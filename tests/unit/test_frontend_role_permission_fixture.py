"""ADR 0076: the interface's permission keys are the catalogue's.

The frontend decides menus and buttons from ``auth.user.permissions``. Its tests use
each system role's list from ``src/test/fixtures/role-permissions.json``, and its
code names keys through the ``PermissionKey`` type and ``CAPABILITY_PERMISSIONS``.
A key renamed or a role's list changed in ``permissions.py`` without the frontend
following would let those tests pass against a matrix the server no longer sends.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.application.auth.permissions import (
    ALL_PERMISSION_KEYS,
    OPERATOR_PERMISSIONS,
    VIEWER_PERMISSIONS,
)

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "assets/frontend/src"
FIXTURE = FRONTEND / "test/fixtures/role-permissions.json"


def test_the_role_fixture_is_the_catalogue_matrix() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["admin"] == sorted(ALL_PERMISSION_KEYS)
    assert fixture["operator"] == sorted(OPERATOR_PERMISSIONS)
    assert fixture["viewer"] == sorted(VIEWER_PERMISSIONS)


def test_the_frontend_permission_key_type_lists_the_catalogue() -> None:
    source = (FRONTEND / "lib/auth/permissions.ts").read_text(encoding="utf-8")
    union = source.split("export type PermissionKey =", 1)[1].split(";", 1)[0]

    assert set(re.findall(r'"([a-z_.]+)"', union)) == set(ALL_PERMISSION_KEYS)


def test_every_capability_names_a_catalogue_permission() -> None:
    source = (FRONTEND / "lib/auth/capabilities.ts").read_text(encoding="utf-8")
    table = source.split("CAPABILITY_PERMISSIONS", 1)[1].split("};", 1)[0]
    mapping = dict(re.findall(r'^\s*([a-z_]+): "([a-z_.]+)",', table, flags=re.MULTILINE))

    assert set(mapping) == {
        "administer_platform",
        "manage_users",
        "create_projects",
        "mutate_projects",
        "read_registry",
        "run_workflows",
        "manage_secret_values",
    }
    assert set(mapping.values()) <= ALL_PERMISSION_KEYS
