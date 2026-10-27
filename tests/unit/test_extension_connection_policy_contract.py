"""One canonical operation connection policy, carried through production.

The policy travels as the plural `connection_type_ids` list. These tests use a
synthetic install contract: an operation that declares two connection types must
accept a binding of either, and no layer may reduce the set to a single name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.application.extensions.operation_support import extension_operation_contract
from core.application.extensions.workflow_install_contract import extension_action_operations

ROOT = Path(
    os.environ.get("PYTEST_SOURCE_REPO_ROOT", "").strip() or Path(__file__).resolve().parents[2]
)

_FILE_OPERATIONS = ("list_files", "fetch_file", "upload_file")


def _install_contract() -> dict:
    """A synthetic storage extension whose file operations work over FTP or SFTP."""
    return {
        "extension_id": "acme.storage",
        "operation_manifest": [
            {"operation_id": operation_id, "connection_type_ids": ["ftp", "sftp"]}
            for operation_id in _FILE_OPERATIONS
        ],
    }


def _operation(contract: dict, operation_id: str) -> dict:
    rows = [
        row
        for row in contract["operation_manifest"]
        if str(row.get("operation_id") or "") == operation_id
    ]
    assert len(rows) == 1, f"{operation_id} appears {len(rows)} times"
    return rows[0]


def test_action_only_release_does_not_synthesize_a_workflow_operation() -> None:
    operations = extension_action_operations(
        "acme.action-only",
        [{"action_id": "run", "workflow_visible": True, "parameters": []}],
        [],
    )

    assert operations == []


@pytest.mark.parametrize("operation_id", _FILE_OPERATIONS)
def test_multi_type_operations_accept_both_protocols(operation_id: str) -> None:
    contract = _install_contract()

    row = _operation(contract, operation_id)
    operation = extension_operation_contract(contract, operation_id=operation_id)

    assert sorted(row["connection_type_ids"]) == ["ftp", "sftp"]
    assert operation.accepts_connection_type("ftp")
    assert operation.accepts_connection_type("sftp")


@pytest.mark.parametrize("operation_id", _FILE_OPERATIONS)
def test_no_layer_reduces_the_policy_to_one_protocol(operation_id: str) -> None:
    """Collapsing the set to a single name is how a valid SFTP binding was
    rejected at runtime with `connection_runtime_requirement_failed`."""
    contract = _install_contract()

    operation = extension_operation_contract(contract, operation_id=operation_id)

    assert len(operation.connection_type_ids) == 2, operation.connection_type_ids


# ---------------------------------------------------------------------------
# Runtime membership
#
# The runtime reduced the policy to one name with `next(iter(sorted(...)))` and
# then compared for equality, so a valid SFTP binding was refused with
# `connection_runtime_requirement_failed` while the identical FTP one ran.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bound_type", ["ftp", "sftp"])
def test_runtime_validation_accepts_every_declared_type(bound_type: str) -> None:
    from core.application.extensions.operation_support import (
        connection_type_satisfies_operation,
    )

    contract = _install_contract()

    assert connection_type_satisfies_operation(
        contract, operation_id="list_files", connection_type=bound_type
    )


def test_runtime_validation_rejects_an_undeclared_type() -> None:
    from core.application.extensions.operation_support import (
        connection_type_satisfies_operation,
    )

    contract = _install_contract()

    assert not connection_type_satisfies_operation(
        contract, operation_id="list_files", connection_type="acme_cms_site"
    )


def test_an_operation_needing_no_connection_accepts_any_binding() -> None:
    """Membership only constrains an operation that declares a policy."""
    from core.application.extensions.operation_support import (
        connection_type_satisfies_operation,
    )

    contract = {"operation_manifest": [{"operation_id": "ping"}]}

    assert connection_type_satisfies_operation(
        contract, operation_id="ping", connection_type="anything"
    )


def test_no_production_adapter_collapses_the_policy_to_one_name() -> None:
    """The reduction itself is the defect, so its shape is banned by name."""
    import re

    for relative in (
        "core/application/extensions/operation_service.py",
        "core/application/extensions/operation_targets.py",
        "core/infrastructure/extension_runtime/extension_runtime_helpers.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not re.search(r"next\(iter\(sorted\(.*connection_type", source), relative
