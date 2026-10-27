"""Per-worker isolation for the parallel backend suite (pytest-xdist).

The gate runs ``tests/unit`` on several xdist workers that share one disposable
``pg-audit`` cluster and one copied workspace. A single-process run used one
database, ``flow_steward_audit``, and every test saw the state the tests before
it left behind. Sharing that database between concurrent workers would let one
worker's rows, deletes and schema-level writes reach another worker's tests, so
each worker gets its own copy instead:

* ``isolate_worker()`` runs in every worker before any product module is
  imported. It clones ``flow_steward_audit`` (initialized by lint.sh) into
  ``flow_steward_audit_<worker>`` with ``CREATE DATABASE ... TEMPLATE`` and points
  ``FS_DB_DSN``, ``DATABASE_URL``, ``LANGGRAPH_POSTGRES_URI`` and ``POSTGRES_DB`` at
  it. Each worker therefore starts from the same canonical state a serial run
  starts from, and runs its share of the tests in order against it.
* The gate's local artifact roots are tmpfs directories shared by the whole
  container; each worker gets a subdirectory of its own.
* ``release_worker()`` drops the copy when the worker finishes.

Outside an xdist worker, or outside lint.sh's isolated audit environment, all
of this is a no-op, so a serial run (Tier 1 ``targeted-tests``) behaves exactly
as before. A test that cannot be isolated this way is pinned to one worker with
``@pytest.mark.xdist_group("serial")`` (see docs/standards/testing-standard.md).

Nested runs (a test that starts pytest in a subprocess) inherit the redirected
environment, see that it already names this worker's database, and leave it
alone — they must never drop or recreate the database their parent is using.
"""

from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit, urlunsplit

AUDIT_DATABASE = "flow_steward_audit"
AUDIT_HOST = "pg-audit"
_WORKER_ID = re.compile(r"gw[0-9]+")
_URI_KEYS = ("DATABASE_URL", "LANGGRAPH_POSTGRES_URI")
_SCRATCH_ROOT_KEYS = ("ARTIFACT_LOCAL_ROOT_DIR", "FS_ARTIFACT_ROOT")
# Serializes template copies across workers. CREATE DATABASE refuses to copy a
# template that another session is connected to, and copies run in parallel
# would otherwise race on that check.
_PROVISION_LOCK_KEY = 0x46535844
_TEMPLATE_BUSY_TIMEOUT_SECONDS = 60

_provisioned: tuple[str, str] | None = None


def xdist_worker() -> str:
    """Return this process's xdist worker id (``gw0`` ...), or ``""`` when serial."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "").strip()
    if not worker:
        return ""
    if not _WORKER_ID.fullmatch(worker):
        raise RuntimeError(f"Unexpected pytest-xdist worker id {worker!r}")
    return worker


def _isolated_gate() -> bool:
    return os.environ.get("FS_AUDIT_ISOLATED") == "1"


def audit_database_name() -> str:
    """The audit database this process must use: its own copy on an xdist worker."""
    worker = xdist_worker()
    if worker and _isolated_gate():
        return f"{AUDIT_DATABASE}_{worker}"
    return AUDIT_DATABASE


def _with_uri_database(uri: str, database: str) -> str:
    parts = urlsplit(uri)
    if parts.path != f"/{AUDIT_DATABASE}":
        raise RuntimeError("Refusing to parallelise: a database URI is not the audit database")
    return urlunsplit(parts._replace(path=f"/{database}"))


def _create_from_template(admin_dsn: str, database: str) -> None:
    import psycopg
    from psycopg import errors, sql

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_PROVISION_LOCK_KEY,))
        try:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
            deadline = time.monotonic() + _TEMPLATE_BUSY_TIMEOUT_SECONDS
            while True:
                try:
                    conn.execute(
                        sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                            sql.Identifier(database), sql.Identifier(AUDIT_DATABASE)
                        )
                    )
                    return
                except errors.ObjectInUse:
                    # A session that just left the template (init_db, a sibling
                    # worker's probe) can linger for a moment after disconnect.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.5)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_PROVISION_LOCK_KEY,))


def isolate_worker() -> None:
    """Give this xdist worker its own audit database and scratch roots. Idempotent."""
    global _provisioned
    worker = xdist_worker()
    if not worker or not _isolated_gate():
        return

    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dsn = os.environ.get("FS_DB_DSN", "")
    info = conninfo_to_dict(dsn)
    database = audit_database_name()
    if info.get("dbname") == database:
        return
    if (info.get("host"), info.get("dbname")) != (AUDIT_HOST, AUDIT_DATABASE):
        raise RuntimeError("Refusing to parallelise: FS_DB_DSN is not the pg-audit database")

    uris = {
        key: _with_uri_database(os.environ[key], database)
        for key in _URI_KEYS
        if os.environ.get(key)
    }
    admin_dsn = make_conninfo(dsn, dbname="postgres")
    _create_from_template(admin_dsn, database)
    _provisioned = (admin_dsn, database)

    os.environ["FS_DB_DSN"] = make_conninfo(dsn, dbname=database)
    os.environ.update(uris)
    if os.environ.get("POSTGRES_DB") == AUDIT_DATABASE:
        os.environ["POSTGRES_DB"] = database
    for key in _SCRATCH_ROOT_KEYS:
        root = os.environ.get(key, "").strip()
        if root:
            scoped = os.path.join(root, worker)
            os.makedirs(scoped, exist_ok=True)
            os.environ[key] = scoped


def release_worker() -> None:
    """Drop the database ``isolate_worker`` created in this process, if any."""
    global _provisioned
    if _provisioned is None:
        return
    admin_dsn, database = _provisioned
    _provisioned = None
    import psycopg
    from psycopg import sql

    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
    except psycopg.Error:
        # Best effort: pg-audit is a tmpfs service lint.sh removes after the run.
        pass


__all__ = [
    "AUDIT_DATABASE",
    "audit_database_name",
    "isolate_worker",
    "release_worker",
    "xdist_worker",
]
