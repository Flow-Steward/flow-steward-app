"""Real transactions in a test-owned DB, never the shared product database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from core.application.monitoring.contracts import HeartbeatReport, RuntimeInstallationContext
from core.application.tooling.db.init_db import bootstrap_canonical_schema_if_empty
from tests._db_env import resolve_test_db_dsn

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def status_database():
    root = Path(__file__).resolve().parents[2]
    base = resolve_test_db_dsn()
    admin = psycopg.connect(base, autocommit=True)
    with admin.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        assert cursor.fetchone()[0] == "flow_steward_audit", "refuse shared product database"
    name = "fs_runtime_status_" + uuid4().hex
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name)
    try:
        bootstrap_canonical_schema_if_empty(dsn=dsn, root=root)
        yield dsn
    finally:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def context():
    return RuntimeInstallationContext(
        installation_id=str(uuid4()), deployment_profile="scalable", expectations=(("worker", 2),)
    )


@pytest.fixture
def report(context):
    instance = str(uuid4())

    def build(**changes):
        data = {
            "schema_version": 1,
            "installation_id": context.installation_id,
            "deployment_profile": context.deployment_profile,
            "service_type": "worker",
            "instance_id": instance,
            "incarnation_id": instance,
            "sequence": 1,
            "observed_at": datetime.now(UTC),
            "source": "self",
            "payload": {
                "type": "runtime_status",
                "version": 1,
                "lifecycle": "running",
                "readiness": {"state": "not_observed"},
                "health": {"state": "not_observed"},
                "consumer_state": "registered",
            },
        }
        data.update(changes)
        return HeartbeatReport.model_validate(data)

    return build


@pytest.fixture
def repo(status_database):
    from core.application.monitoring.repository import RuntimeStatusRepository

    with psycopg.connect(status_database, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE public.fs_runtime_status_current, public.fs_runtime_status_history"
        )
    return RuntimeStatusRepository(status_database)


def history_count(dsn):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT count(*) FROM public.fs_runtime_status_history"
        ).fetchone()[0]


def test_future_check_cannot_be_freshened_by_database_receipt(repo, report, context):
    data = report().model_dump(mode="json")
    data["payload"]["health"] = {
        "state": "healthy",
        "evidence": "protocol",
        "checked_at": (datetime.now(UTC) + timedelta(seconds=31)).isoformat(),
    }
    result = repo.store(HeartbeatReport.model_validate(data), history_enabled=True)
    assert result.disposition == "rejected"
    assert repo.load(context) == []


def test_repository_uses_canonical_config_budget(status_database, report, context):
    from core.application.monitoring.repository import RuntimeStatusRepository
    from core.infrastructure.config.monitoring_runtime import load_monitoring_runtime_config

    policy = load_monitoring_runtime_config(
        {
            "FS_MONITORING_INTERVAL_SECONDS": "1",
            "FS_MONITORING_STALE_SECONDS": "2",
            "FS_MONITORING_OPERATION_TIMEOUT_SECONDS": "1",
        }
    )
    configured = RuntimeStatusRepository(status_database, config=policy)
    result = configured.store(
        report(observed_at=datetime.now(UTC) - timedelta(seconds=3)), history_enabled=False
    )
    assert result.disposition == "rejected"
    assert configured.load(context) == []


def test_concurrent_sequences_do_not_regress_or_duplicate_history(
    repo, report, context, status_database
):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(repo.store, report(sequence=sequence), history_enabled=True)
            for sequence in [2, 1, 4, 3, 6, 5, 8, 7]
        ]
        for future in futures:
            assert future.result().disposition in {"stored", "duplicate"}
    rows = repo.load(context)
    assert len(rows) == 1
    assert rows[0].sequence == 8
    assert history_count(status_database) == 1


def test_old_history_prunes_after_new_history_is_disabled(repo, report, context, status_database):
    repo.store(report(), history_enabled=True)
    repo.store(report(sequence=2), history_enabled=False)
    cutoff = datetime.now(UTC) - timedelta(days=7)
    with psycopg.connect(status_database, autocommit=True) as connection:
        connection.execute(
            "UPDATE public.fs_runtime_status_history SET received_at=%s",
            (cutoff - timedelta(days=1),),
        )
    assert repo.prune(context=context, current_before=cutoff, history_before=cutoff) == {
        "current": 0,
        "history": 1,
    }
    assert repo.load(context)[0].sequence == 2


def test_check_timestamps_alone_are_not_history_events(repo, report, status_database):
    data = report().model_dump(mode="json")
    data["payload"]["health"] = {
        "state": "healthy",
        "evidence": "protocol",
        "checked_at": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
    }
    repo.store(HeartbeatReport.model_validate(data), history_enabled=True)
    data["sequence"] = 2
    data["payload"]["health"]["checked_at"] = datetime.now(UTC).isoformat()
    repo.store(HeartbeatReport.model_validate(data), history_enabled=True)
    assert history_count(status_database) == 1


def test_duplicate_and_older_sequence_do_not_reset_receipt(repo, report, context, status_database):
    repo.store(report(sequence=2), history_enabled=True)
    before = repo.load(context)
    assert repo.store(report(sequence=1), history_enabled=True).disposition == "duplicate"
    assert repo.store(report(sequence=2), history_enabled=True).disposition == "duplicate"
    assert repo.load(context) == before
    assert history_count(status_database) == 1


def test_unchanged_state_advances_heartbeat_without_history(repo, report, context, status_database):
    repo.store(report(sequence=1), history_enabled=True)
    before = repo.load(context)[0]
    repo.store(report(sequence=2), history_enabled=True)
    after = repo.load(context)[0]
    assert after.sequence == 2
    assert after.received_at >= before.received_at
    assert history_count(status_database) == 1


def test_changed_state_records_history_atomically(repo, report, context, status_database):
    first = report()
    repo.store(first, history_enabled=True)
    data = first.model_dump(mode="json")
    data["sequence"] = 2
    data["payload"]["consumer_state"] = "active"
    repo.store(HeartbeatReport.model_validate(data), history_enabled=True)
    assert repo.load(context)[0].payload.consumer_state == "active"
    assert history_count(status_database) == 2


def test_failed_history_insert_rolls_back_current(repo, report, context, status_database):
    with psycopg.connect(status_database, autocommit=True) as connection:
        connection.execute("""CREATE FUNCTION reject_status_history() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'history fixture failure'; END $$;
            CREATE TRIGGER reject_status_history BEFORE INSERT ON public.fs_runtime_status_history
            FOR EACH ROW EXECUTE FUNCTION reject_status_history();""")
    try:
        status_report = report()
        with pytest.raises(psycopg.Error):
            repo.store(status_report, history_enabled=True)
        assert repo.load(context) == []
        assert history_count(status_database) == 0
    finally:
        with psycopg.connect(status_database, autocommit=True) as connection:
            connection.execute(
                "DROP TRIGGER reject_status_history ON public.fs_runtime_status_history"
            )
            connection.execute("DROP FUNCTION reject_status_history()")


def test_instances_and_sources_are_independent(repo, report, context):
    repo.store(report(), history_enabled=False)
    repo.store(report(source="compact-supervisor"), history_enabled=False)
    other = str(uuid4())
    repo.store(report(instance_id=other, incarnation_id=other), history_enabled=False)
    assert len(repo.load(context)) == 3


def test_clone_and_profile_rows_do_not_become_local_evidence(repo, report, context):
    repo.store(report(installation_id=str(uuid4())), history_enabled=False)
    repo.store(report(deployment_profile="compact"), history_enabled=False)
    assert repo.load(context) == []


def test_expired_and_future_reports_cannot_refresh_rows(repo, report, context):
    repo.store(report(), history_enabled=False)
    before = repo.load(context)
    for offset in (-181, 31):
        result = repo.store(
            report(sequence=2, observed_at=datetime.now(UTC) + timedelta(seconds=offset)),
            history_enabled=False,
        )
        assert result.disposition == "rejected"
    assert repo.load(context) == before


def test_retention_is_bounded_and_independent_of_history(repo, report, context, status_database):
    repo.store(report(), history_enabled=False)
    cutoff = datetime.now(UTC) - timedelta(days=7)
    with psycopg.connect(status_database, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO public.fs_runtime_status_current
            (id, schema_version, installation_id, deployment_profile, service_type,
             instance_id, incarnation_id, sequence, observed_at, received_at, source, payload)
            SELECT gen_random_uuid(), schema_version, installation_id, deployment_profile,
                service_type, gen_random_uuid(), incarnation_id, sequence, %s, %s, source, payload
            FROM public.fs_runtime_status_current CROSS JOIN generate_series(1,501)""",
            (cutoff - timedelta(days=1), cutoff - timedelta(days=1)),
        )
    foreign = RuntimeInstallationContext(
        installation_id=str(uuid4()), deployment_profile="scalable"
    )
    repo.store(report(installation_id=foreign.installation_id), history_enabled=False)
    assert repo.prune(context=context, current_before=cutoff, history_before=cutoff) == {
        "current": 500,
        "history": 0,
    }
    assert len(repo.load(context)) == 2
    assert len(repo.load(foreign)) == 1
    assert context.expectations == (("worker", 2),)
