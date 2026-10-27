"""Real PostgreSQL invariants for scoped snapshot heads and durable receipts."""

from __future__ import annotations

import importlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import psycopg
import pytest

pytestmark = pytest.mark.integration


def _snapshots():
    """Keep missing feature failures explicit rather than breaking collection."""
    module = "core.application.tabular_feed.snapshot_repository"
    assert importlib.util.find_spec(module), "snapshot repository is not implemented"
    return importlib.import_module(module)


@pytest.fixture
def snapshot_tenants(two_tenants, disposable_db_conn):
    # The shared helper creates projects without account_id, leaving the canonical
    # default account. These tests need actual matching project/account ownership.
    with disposable_db_conn.cursor() as cur:
        for tenant in (two_tenants.a, two_tenants.b):
            project_ids = [project.id for project in tenant.projects.values()]
            cur.execute(
                "UPDATE projects SET account_id = %s WHERE id = ANY(%s)",
                (tenant.account_id, project_ids),
            )
            cur.execute("SELECT account_id FROM projects WHERE id = ANY(%s)", (project_ids,))
            assert {str(row[0]) for row in cur.fetchall()} == {tenant.account_id}
    disposable_db_conn.commit()
    return two_tenants


@pytest.fixture
def scope(snapshot_tenants):
    return {
        "account_id": snapshot_tenants.a.account_id,
        "project_id": snapshot_tenants.a.shared.id,
        "workflow_id": "workflow",
        "profile_key": "supplier feed",
    }


def _commit(repo, scope, candidate, expected, **metadata):
    return repo.commit(
        scope,
        candidate_artifact_id=candidate,
        expected_ref=expected,
        metadata=metadata,
    )


def _wait_for_lock(observer, pid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if row and row[0] == "Lock":
            return
        time.sleep(0.01)
    pytest.fail("second PostgreSQL writer never blocked on the first transaction")


@pytest.mark.parametrize("initial_revision", [0, 7])
@pytest.mark.parametrize("same_candidate", [False, True])
def test_concurrent_commits_have_one_head_and_replay_or_conflict(
    disposable_db, scope, initial_revision, same_candidate
):
    """B01/B03: the second writer observes the committed winner after waiting."""
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with (
        psycopg.connect(disposable_db.dsn) as first,
        psycopg.connect(disposable_db.dsn, autocommit=True) as observer,
        psycopg.connect(disposable_db.dsn) as second,
    ):
        repo = snapshots.SnapshotRepository(first)
        expected = snapshots.SnapshotRef(0, "")
        for revision in range(initial_revision):
            expected = _commit(repo, scope, f"seed-{revision}", expected).ref
        first.commit()
        winner = _commit(repo, scope, "candidate-A", expected, raw_sha256="original")
        second_pid = second.info.backend_pid

        def competing_commit():
            try:
                receipt = _commit(
                    snapshots.SnapshotRepository(second),
                    scope,
                    "candidate-A" if same_candidate else "candidate-B",
                    expected,
                    raw_sha256="retry",
                )
                second.commit()
                return receipt
            except snapshots.SnapshotConflictError as exc:
                second.rollback()
                return exc

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(competing_commit)
            try:
                _wait_for_lock(observer, second_pid)
            finally:
                # Always release the row/unique lock, including assertion failures.
                first.commit()
            outcome = future.result(timeout=10)
        if same_candidate:
            assert outcome == winner
            assert outcome.metadata == {"raw_sha256": "original"}
        else:
            assert isinstance(outcome, snapshots.SnapshotConflictError)
            assert outcome.code == "snapshot_conflict"
            assert outcome.actual == winner.ref
        assert snapshots.SnapshotRepository(observer).read_head(scope) == winner.ref
        assert winner.ref == snapshots.SnapshotRef(initial_revision + 1, "candidate-A")
        assert (
            observer.execute(
                "SELECT count(*) FROM fs_tabular_snapshot_heads WHERE account_id = %s",
                (scope.account_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            observer.execute(
                "SELECT count(*) FROM fs_tabular_snapshot_commits WHERE account_id = %s",
                (scope.account_id,),
            ).fetchone()[0]
            == initial_revision + 1
        )


def test_late_duplicate_returns_original_receipt_without_rewinding_head(disposable_db, scope):
    """B04: A -> B -> late A survives a new connection/request lifetime."""
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        empty = repo.read_head(scope)
        assert empty == snapshots.SnapshotRef(0, "")
        baseline = empty
        for revision in range(7):
            baseline = _commit(repo, scope, f"seed-{revision}", baseline).ref
        assert baseline.revision == 7
        first = _commit(repo, scope, "A", baseline, raw_sha256="a")
        conn.commit()
        second = _commit(repo, scope, "B", first.ref, raw_sha256="b")
        assert first.ref == snapshots.SnapshotRef(8, "A")
        assert second.ref == snapshots.SnapshotRef(9, "B")
    with psycopg.connect(disposable_db.dsn) as retry:
        repo = snapshots.SnapshotRepository(retry)
        assert _commit(repo, scope, "A", baseline, raw_sha256="changed") == first
        assert repo.read_head(scope) == second.ref
        assert repo.read_metadata(scope) == {"raw_sha256": "b"}
        current = repo.read_current(scope)
        assert current.ref == second.ref
        assert current.metadata == {"raw_sha256": "b"}
        assert second.previous_snapshot_artifact_id == "A"


def test_both_revision_and_artifact_are_compared(disposable_db, scope):
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        head = _commit(repo, scope, "A", snapshots.SnapshotRef(0, "")).ref
        conn.commit()
        stale_ref = snapshots.SnapshotRef(head.revision, "different")
        with pytest.raises(snapshots.SnapshotConflictError):
            _commit(repo, scope, "B", stale_ref)
        conn.rollback()
        assert repo.read_head(scope) == head


@pytest.mark.parametrize("existing_head", [False, True])
def test_receipt_insert_failure_rolls_back_head_advance(disposable_db, scope, existing_head):
    """B05: a real DB failure occurs after UPDATE, before receipt persistence."""
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        initial_ref = snapshots.SnapshotRef(0, "")
        if existing_head:
            initial_ref = _commit(repo, scope, "A", initial_ref).ref
        conn.commit()
        # A session-local rejecting trigger exercises the actual receipt INSERT.
        conn.execute("""CREATE FUNCTION pg_temp.reject_snapshot_receipt() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected receipt failure'; END $$""")
        trigger = "snapshot_failure_" + uuid.uuid4().hex
        conn.execute(
            psycopg.sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON fs_tabular_snapshot_commits "
                "FOR EACH ROW EXECUTE FUNCTION pg_temp.reject_snapshot_receipt()"
            ).format(psycopg.sql.Identifier(trigger))
        )
        # The trigger creation and failed write share the transaction: rollback removes both.
        with pytest.raises(psycopg.errors.RaiseException, match="injected receipt failure"):
            _commit(repo, scope, "B", initial_ref)
        conn.rollback()
        assert repo.read_head(scope) == initial_ref
        assert (
            conn.execute(
                "SELECT count(*) FROM fs_tabular_snapshot_commits "
                "WHERE account_id = %s AND candidate_artifact_id = %s",
                (scope.account_id, "B"),
            ).fetchone()[0]
            == 0
        )
        assert _commit(repo, scope, "B", initial_ref).ref.revision == initial_ref.revision + 1


def test_scopes_and_nullable_normalization_share_one_namespace(
    disposable_db, scope, snapshot_tenants
):
    """B06: every scope component separates heads and candidate receipts."""
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    scopes = [
        scope,
        replace(
            scope, account_id=snapshot_tenants.b.account_id, project_id=snapshot_tenants.b.shared.id
        ),
        replace(scope, project_id=snapshot_tenants.a.private.id),
        replace(scope, workflow_id="other"),
        replace(scope, profile_key="other"),
    ]
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        for index, owner in enumerate(scopes):
            assert repo.read_head(owner) == snapshots.SnapshotRef(0, "")
            receipt = _commit(
                repo, owner, "shared-candidate", snapshots.SnapshotRef(0, ""), owner=index
            )
            assert receipt.ref.revision == 1
            assert receipt.metadata == {"owner": index}
        assert repo.read_metadata(scope) == {"owner": 0}
        nullable = replace(scope, workflow_id=None, profile_key=" supplier feed ")
        default = replace(scope, workflow_id="workflow", profile_key="supplier_feed")
        assert (
            repo.read_head(nullable)
            == repo.read_head(default)
            == snapshots.SnapshotRef(1, "shared-candidate")
        )
        assert _commit(
            repo, nullable, "shared-candidate", snapshots.SnapshotRef(0, "")
        ).metadata == {"owner": 0}


def test_database_constraints_enforce_unique_scope_and_candidate(disposable_db, scope):
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        _commit(repo, scope, "A", snapshots.SnapshotRef(0, ""))
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "INSERT INTO fs_tabular_snapshot_heads SELECT * FROM "
                "fs_tabular_snapshot_heads WHERE account_id = %s",
                (scope.account_id,),
            )
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(
                "INSERT INTO fs_tabular_snapshot_commits SELECT * FROM "
                "fs_tabular_snapshot_commits WHERE account_id = %s",
                (scope.account_id,),
            )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(
                "UPDATE fs_tabular_snapshot_heads SET revision = 0 WHERE account_id = %s",
                (scope.account_id,),
            )


@pytest.mark.parametrize(
    "revision,artifact", [(-1, ""), (0, "A"), (1, ""), (True, "A"), (1.0, "A")]
)
def test_snapshot_reference_rejects_invalid_states(revision, artifact):
    snapshots = _snapshots()
    with pytest.raises(ValueError):
        snapshots.SnapshotRef(revision, artifact)


def test_project_ownership_and_deletion_are_enforced_by_database(
    disposable_db, scope, snapshot_tenants
):
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        foreign = replace(scope, account_id=snapshot_tenants.b.account_id)
        empty_ref = snapshots.SnapshotRef(0, "")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _commit(repo, foreign, "foreign", empty_ref)
        conn.rollback()
        _commit(repo, scope, "owned", snapshots.SnapshotRef(0, ""))
        conn.commit()
        conn.execute("DELETE FROM projects WHERE id = %s", (scope.project_id,))
        conn.commit()
        assert repo.read_head(scope) == snapshots.SnapshotRef(0, "")
        assert (
            conn.execute(
                "SELECT count(*) FROM fs_tabular_snapshot_commits WHERE account_id = %s",
                (scope.account_id,),
            ).fetchone()[0]
            == 0
        )


def test_exact_candidate_receipt_read_preserves_historical_ref_and_scope(disposable_db, scope):
    snapshots = _snapshots()
    scope = snapshots.SnapshotScope(**scope)
    with psycopg.connect(disposable_db.dsn) as conn:
        repo = snapshots.SnapshotRepository(conn)
        assert repo.read_commit(scope, "uncommitted") is None
        receipt_a = _commit(repo, scope, "A", snapshots.SnapshotRef(0, ""), raw_sha256="a")
        receipt_b = _commit(repo, scope, "B", receipt_a.ref, raw_sha256="b")
        assert repo.read_commit(scope, "A") == receipt_a
        assert repo.read_commit(scope, "B") == receipt_b
        assert repo.read_commit(replace(scope, profile_key="other"), "A") is None
        assert repo.read_head(scope) == receipt_b.ref
