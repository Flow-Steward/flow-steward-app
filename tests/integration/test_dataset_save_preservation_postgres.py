"""Dataset persistence regressions on the guarded disposable audit PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import CheckViolation

from core.application.auth.config import DEFAULT_WORKSPACE_ID
from core.application.registry.db_store import RegistryDBStore
from tests._db_env import resolve_test_db_dsn
from tests.integration.test_dataset_plan_assignment_history_postgres import (
    owned_dataset_assignment as owned_dataset_assignment,
)
from tests.integration.test_dataset_plan_assignment_history_postgres import (
    require_audit_database as require_audit_database,
)


@pytest.fixture
def persisted_dataset(owned_dataset_assignment):
    # Imported autouse require_audit_database runs before the owning fixture's
    # writes. Reuse its unique dataset/project IDs and exact scoped cleanup.
    owner = owned_dataset_assignment
    assert owner.conn.info.host == "pg-audit"
    assert owner.conn.info.dbname == "flow_steward_audit"
    # Do not re-resolve an environment DSN that might point at another plane.
    dsn = resolve_test_db_dsn()
    params = conninfo_to_dict(dsn)
    assert params.get("host") == "pg-audit"
    assert params.get("dbname") == "flow_steward_audit"
    store = RegistryDBStore(dsn)
    metadata = {
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "owner": "ops",
        "custom_flags": {"keep": True},
    }
    payload = {
        "dataset_id": owner.dataset_id,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "display_name": "Dataset preservation fixture",
        "availability_mode": "specific_projects_only",
    }
    try:
        store.save_dataset(
            {**payload, "tags": ["orders"], "metadata_jsonb": metadata}, author=owner.actor
        )
        yield SimpleNamespace(owner=owner, store=store, payload=payload, metadata=metadata)
    finally:
        # Close our own repository connection before the owning fixture deletes
        # its changelog, project association, project and exact dataset key.
        if store._pool is not None:
            store._pool.closeall()
        elif store._conn is not None:
            store._conn.close()


def _persisted_row(fixture):
    with fixture.owner.conn.cursor() as cur:
        cur.execute(
            """
            SELECT dataset_key, dataset_id, tags, metadata_jsonb, display_name, runtime_kind
            FROM registry_datasets
            WHERE dataset_id = %s
            """,
            (fixture.owner.dataset_id,),
        )
        row = cur.fetchone()
    fixture.owner.conn.commit()
    assert row is not None
    # Canonical baseline's dataset_id is generated from dataset_key; the real
    # repository must neither insert it explicitly nor mismatch the public key.
    assert row[0] == row[1] == fixture.owner.dataset_id
    return row


@pytest.mark.parametrize(
    ("updates", "expected"),
    [({}, ["orders"]), ({"tags": []}, []), ({"tags": ["new"]}, ["new"])],
    ids=["omitted-preserves", "empty-clears", "replacement"],
)
def test_dataset_tags_postgres_save_and_readback(persisted_dataset, updates, expected):
    fixture = persisted_dataset
    assert _persisted_row(fixture)[2] == ["orders"]
    fixture.store.save_dataset(
        {**fixture.payload, "display_name": "Edited", **updates}, author=fixture.owner.actor
    )
    readback = fixture.store.get_dataset(fixture.owner.dataset_id)
    raw = _persisted_row(fixture)
    assert raw[2] == readback["tags"] == expected
    assert raw[4] == readback["display_name"] == "Edited"
    assert raw[3] == readback["metadata_jsonb"] == fixture.metadata


@pytest.mark.parametrize("operation", ["omitted", "clear-custom", "replace-custom"])
def test_dataset_metadata_postgres_save_and_readback(persisted_dataset, operation):
    fixture = persisted_dataset
    assert _persisted_row(fixture)[3] == fixture.metadata
    # The API validator always emits authoritative binding metadata. An empty
    # user object therefore reaches the repository as a nonempty binding map,
    # not {}. This test deliberately does not invent a direct-store {} contract.
    binding_metadata = {"workspace_id": DEFAULT_WORKSPACE_ID}
    if operation == "omitted":
        updates = {}
        expected = fixture.metadata
    elif operation == "clear-custom":
        updates = {"metadata_jsonb": binding_metadata}
        expected = binding_metadata
    else:
        expected = {**binding_metadata, "owner": "new-owner"}
        updates = {"metadata_jsonb": expected}
    fixture.store.save_dataset(
        {**fixture.payload, "display_name": "Edited", **updates}, author=fixture.owner.actor
    )
    readback = fixture.store.get_dataset(fixture.owner.dataset_id)
    raw = _persisted_row(fixture)
    assert raw[3] == readback["metadata_jsonb"] == expected
    assert raw[2] == readback["tags"] == ["orders"]
    assert raw[4] == readback["display_name"] == "Edited"


def test_dataset_postgres_constraint_failure_preserves_saved_state(persisted_dataset):
    fixture = persisted_dataset
    before = _persisted_row(fixture)
    with pytest.raises(CheckViolation):
        fixture.store.save_dataset(
            {
                **fixture.payload,
                "runtime_kind": "not-a-canonical-runtime",
                "tags": ["must-not-persist"],
                "metadata_jsonb": {"must_not_persist": True},
            },
            author=fixture.owner.actor,
        )
    assert _persisted_row(fixture) == before
    readback = fixture.store.get_dataset(fixture.owner.dataset_id)
    assert readback["tags"] == before[2]
    assert readback["metadata_jsonb"] == before[3]
    assert readback["runtime_kind"] == before[5]
