"""Post-1.0 SQL migration runner.

Flow Steward keeps ``db/schema/*.sql`` as the 1.0 clean-bootstrap baseline. After
1.0, all database changes are expressed as ordered, checksum-tracked SQL files
under ``db/migrations/``. This module owns:

- generating timestamped migration files (``YYYYMMDDHHMMSS_slug.sql``)
- validating migration filenames and rejecting empty/template-only files
- discovering and lexicographically ordering pending migrations
- applying only unapplied migrations under a PostgreSQL advisory lock
- tracking applied migrations in ``public.platform_schema_migrations`` and
  failing on checksum drift for an already-applied migration
- executing every migration body under one fixed ``search_path`` so a migration
  reads the same catalog whatever the caller's session happens to be set to

The runner is wired into the operator-facing ``flow-steward database upgrade`` command so a
fresh install (initialized from the ``db/schema/`` baseline) and a long-lived
database both converge by applying any unapplied ``db/migrations/`` files.

There is no per-migration Python file and no ``applied_by`` column: the upgrade
command has no authenticated user context.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infrastructure.db.schema_state import split_sql_statements

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

MIGRATIONS_DIR_RELATIVE = "db/migrations"
MIGRATIONS_GLOB = "db/migrations/*.sql"
SCHEMA_MIGRATIONS_TABLE = "public.platform_schema_migrations"

# Session-level advisory lock key for the migration run ("FMIG"). Distinct from
# the workflow-knowledge schema lock (0x5746574B).
MIGRATION_ADVISORY_LOCK_KEY = 0x464D4947
NON_TRANSACTIONAL_IN_PROGRESS_PREFIX = "in_progress:"
MIGRATION_RECONCILIATION_COMMAND = "./flow-steward database status --json"

# YYYYMMDDHHMMSS_slug.sql where slug is lowercase alphanumeric words joined by
# single underscores.
_FILENAME_RE = re.compile(r"^(?P<timestamp>\d{14})_(?P<slug>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")

# A standalone transaction-control marker line: "-- transaction: true|false".
# Documentation lines that embed the marker inside another comment (for example
# "--   -- transaction: false  use for ...") deliberately do not match because of
# the extra prefix/trailing text.
_TXN_MARKER_RE = re.compile(
    r"^[ \t]*--[ \t]*transaction:[ \t]*(true|false)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Dollar-quote opener: $$ or $tag$.
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_](?a:\w)*)?\$")

# A top-level statement that opens/closes/marks a transaction. Migration files
# must not contain explicit transaction control — the runner owns transactions
# so the body and the platform_schema_migrations insert stay atomic. This is
# only matched against top-level statement starts (dollar-quoted PL/pgSQL bodies
# and CASE...END remain one statement and are not flagged).
_TXN_CONTROL_START_RE = re.compile(
    r"^(begin|start[ \t]+transaction|commit|end([ \t]+transaction)?|rollback|abort"
    r"|savepoint|release([ \t]+savepoint)?)\b",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """Base class for migration runner failures."""


class InvalidMigrationFilenameError(MigrationError, ValueError):
    """A migration filename does not match ``YYYYMMDDHHMMSS_slug.sql``."""


class EmptyMigrationError(MigrationError):
    """A migration file has no executable SQL (empty or template-only)."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration's on-disk checksum no longer matches."""


class MigrationTransactionControlError(MigrationError):
    """A migration file contains explicit transaction-control statements."""


class MigrationInProgressError(MigrationError):
    """A non-transactional migration has an unknown durable outcome."""


def _raise_if_in_progress(migration_id: str, recorded_checksum: str) -> None:
    if recorded_checksum.startswith(NON_TRANSACTIONAL_IN_PROGRESS_PREFIX):
        raise MigrationInProgressError(
            f"Non-transactional migration {migration_id} has an unknown in-progress "
            "outcome and will not be run automatically. Inspect and reconcile it, then run "
            f"{MIGRATION_RECONCILIATION_COMMAND}."
        )


def _resolve_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else REPO_ROOT


def migrations_dir(root: Path | None = None) -> Path:
    return _resolve_root(root) / "db" / "migrations"


# ---------------------------------------------------------------------------
# Filename generation and validation
# ---------------------------------------------------------------------------


def slugify_migration_name(name: str) -> str:
    """Normalize a free-text migration name into a filename slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    if not slug:
        raise ValueError("Migration name must contain at least one alphanumeric character")
    return slug


def migration_timestamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y%m%d%H%M%S")


def generate_migration_filename(name: str, *, now: datetime | None = None) -> str:
    """Build a ``YYYYMMDDHHMMSS_slug.sql`` filename for ``name``."""
    return f"{migration_timestamp(now)}_{slugify_migration_name(name)}.sql"


def is_valid_migration_filename(filename: str) -> bool:
    return bool(_FILENAME_RE.match(str(filename or "")))


def render_migration_template(name: str) -> str:
    """Render the SQL-only template body for a new migration file.

    The template is SQL-only, carries a clear header, defaults to
    ``-- transaction: true`` and documents ``-- transaction: false``. It never
    includes ``BEGIN``/``COMMIT`` — the runner owns transaction control.
    """
    slug = slugify_migration_name(name)
    return (
        f"-- Migration: {slug}\n"
        "-- Flow Steward post-1.0 SQL migration.\n"
        "--\n"
        "-- Write SQL statements only. Do NOT add explicit transaction-control\n"
        "-- statements; the runner manages transactions and records this migration\n"
        "-- in public.platform_schema_migrations after it succeeds.\n"
        "--\n"
        "-- Transaction control:\n"
        "--   -- transaction: true   (default) the runner wraps this file in a\n"
        "--                          single transaction together with the tracking\n"
        "--                          insert; any failure rolls the whole migration back.\n"
        "--   -- transaction: false  use for statements that cannot run inside a\n"
        "--                          transaction block, e.g. CREATE INDEX CONCURRENTLY.\n"
        "--                          The runner records durable in-progress state first;\n"
        "--                          interrupted outcomes require manual reconciliation.\n"
        "-- transaction: true\n"
        "\n"
        "-- Replace this comment with your migration SQL.\n"
    )


def create_migration_file(
    name: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Create ``db/migrations/`` (if needed) and write a new migration file."""
    directory = migrations_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    filename = generate_migration_filename(name, now=now)
    path = directory / filename
    if path.exists():
        raise FileExistsError(f"Migration file already exists: {path}")
    path.write_text(render_migration_template(name), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Content inspection: transaction marker, executable-SQL detection, checksum
# ---------------------------------------------------------------------------


def parse_transactional(sql: str) -> bool:
    """Return whether a migration should run inside a transaction.

    Defaults to ``True`` when no marker is present. ``-- transaction: false``
    opts out (for operations such as ``CREATE INDEX CONCURRENTLY``). If multiple
    markers appear, the last one wins.
    """
    matches = _TXN_MARKER_RE.findall(str(sql or ""))
    if not matches:
        return True
    return matches[-1].strip().lower() != "false"


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line/block comments, preserving string and dollar-quoted text."""
    out: list[str] = []
    i = 0
    n = len(sql)
    in_single = in_double = in_line = in_block = False
    dollar_tag: str | None = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
                out.append(ch)
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                i += 2
                in_block = False
                continue
            i += 1
            continue
        if dollar_tag is not None:
            if ch == "$" and sql.startswith(dollar_tag, i):
                out.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            out.append(ch)
            i += 1
            continue
        if in_single:
            out.append(ch)
            if ch == "'":
                if nxt == "'":
                    out.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            out.append(ch)
            if ch == '"':
                if nxt == '"':
                    out.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        if ch == "$":
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                out.append(dollar_tag)
                i += len(dollar_tag)
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def migration_has_executable_sql(sql: str) -> bool:
    """Return True if the migration contains SQL beyond comments/whitespace."""
    stripped = _strip_sql_comments(str(sql or ""))
    return bool(stripped.replace(";", "").strip())


def find_explicit_transaction_control(sql: str) -> list[str]:
    """Return offending top-level transaction-control statements, if any.

    The runner owns transactions; a migration that opens/commits its own
    transaction would break the atomicity of the body plus the tracking insert.
    Statement splitting is dollar-quote aware, so ``BEGIN``/``COMMIT`` inside a
    PL/pgSQL function body or ``DO`` block is not flagged.
    """
    offenders: list[str] = []
    for statement in split_sql_statements(str(sql or "")):
        normalized = _strip_sql_comments(statement).strip()
        if normalized and _TXN_CONTROL_START_RE.match(normalized):
            offenders.append(normalized[:40])
    return offenders


def migration_checksum(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_migration_files(root: Path | None = None) -> list[Path]:
    """Return validated migration files sorted lexicographically by filename."""
    directory = migrations_dir(root)
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in directory.glob("*.sql"):
        if not is_valid_migration_filename(path.name):
            raise InvalidMigrationFilenameError(
                f"Invalid migration filename: {path.name}. "
                "Expected YYYYMMDDHHMMSS_slug.sql under db/migrations/."
            )
        files.append(path)
    return sorted(files, key=lambda candidate: candidate.name)


# ---------------------------------------------------------------------------
# Tracking table + apply
# ---------------------------------------------------------------------------


def ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_schema_migrations (
                migration_id text PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def migrations_table_exists(conn) -> bool:
    """Return whether the migration ledger exists without creating it."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.platform_schema_migrations')")
        row = cur.fetchone()
    return bool(row and row[0] is not None)


def fetch_applied_migrations(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.platform_schema_migrations')")
        relation = cur.fetchone()
        if not relation or relation[0] is None:
            return {}
        cur.execute("SELECT migration_id, checksum FROM public.platform_schema_migrations")
        rows = cur.fetchall() or []
    applied: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict):
            applied[str(row["migration_id"])] = str(row["checksum"])
        else:
            applied[str(row[0])] = str(row[1])
    for migration_id, recorded_checksum in applied.items():
        _raise_if_in_progress(migration_id, recorded_checksum)
    return applied


def pending_migration_ids(conn, *, root: Path | None = None) -> list[str]:
    """Return ordered migration ids that exist on disk but are not yet applied.

    This tracks presence only (applied vs not by id). Use :func:`inspect_migrations`
    when you also need to detect checksum drift of already-applied migrations.
    """
    files = discover_migration_files(root)
    applied = fetch_applied_migrations(conn)
    return [path.name for path in files if path.name not in applied]


def inspect_migrations(conn, *, root: Path | None = None) -> dict[str, list[str]]:
    """Classify discovered migrations as pending or drifted.

    ``pending``  — present on disk, no tracking row yet.
    ``drifted``  — already applied, but the on-disk content's checksum no longer
                   matches the recorded checksum (an applied file was edited).

    Both conditions require operator action: pending migrations must be applied,
    and drift must fail the upgrade rather than be silently treated as compatible.
    """
    files = discover_migration_files(root)
    applied = fetch_applied_migrations(conn)
    pending: list[str] = []
    drifted: list[str] = []
    for path in files:
        recorded = applied.get(path.name)
        if recorded is None:
            pending.append(path.name)
            continue
        _raise_if_in_progress(path.name, recorded)
        checksum = migration_checksum(path.read_text(encoding="utf-8"))
        if recorded != checksum:
            drifted.append(path.name)
    return {"pending": pending, "drifted": drifted}


def applied_unknown_migrations(conn, *, root: Path | None = None) -> list[str]:
    """Migrations the ledger records as applied that this release's files do not have.

    AC17-R159: what a newer release applied. The comparison above starts from this
    release's files, so such a migration was neither pending nor drifted, and an older
    release -- a rollback -- was taken as matching a schema it has never seen.
    """
    known = {path.name for path in discover_migration_files(root)}
    return sorted(name for name in fetch_applied_migrations(conn) if name not in known)


def _set_autocommit(conn, value: bool) -> None:
    if getattr(conn, "autocommit", None) == value:
        return
    conn.autocommit = value


def _record_migration(conn, migration_id: str, checksum: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.platform_schema_migrations (migration_id, checksum)
            VALUES (%s, %s)
            """,
            (migration_id, checksum),
        )


def _finalize_migration(conn, migration_id: str, checksum: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.platform_schema_migrations
            SET checksum = %s, applied_at = now()
            WHERE migration_id = %s
            """,
            (checksum, migration_id),
        )


#: Migration bodies run under this ``search_path`` rather than the caller's.
#: ``pg_get_constraintdef()`` and ``pg_get_triggerdef()`` qualify otherwise
#: identical objects according to the session's search path, so a migration that
#: fingerprints the catalog would read a different contract depending on who ran
#: it. Fixing the path here — at the execution boundary, not inside the SQL —
#: keeps that guarantee without editing a released migration's bytes.
MIGRATION_SEARCH_PATH = "public, pg_catalog"


def _apply_transactional(conn, migration_id: str, content: str, checksum: str) -> None:
    previous_autocommit = bool(getattr(conn, "autocommit", False))
    _set_autocommit(conn, False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL search_path TO {MIGRATION_SEARCH_PATH}")
            cur.execute(content)
        _record_migration(conn, migration_id, checksum)
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise
    finally:
        _set_autocommit(conn, previous_autocommit)


def _apply_non_transactional(conn, migration_id: str, content: str, checksum: str) -> None:
    # CREATE INDEX CONCURRENTLY and friends cannot run inside a transaction
    # block. Persist an in-progress identity before SQL so a process loss in the
    # unavoidably non-atomic window can never cause an automatic re-run.
    _set_autocommit(conn, True)
    _record_migration(
        conn,
        migration_id,
        f"{NON_TRANSACTIONAL_IN_PROGRESS_PREFIX}{checksum}",
    )
    # Autocommit leaves no transaction for SET LOCAL to scope, so the override
    # is set on the session and released again once the body has run.
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {MIGRATION_SEARCH_PATH}")
        try:
            cur.execute(content)
        finally:
            cur.execute("RESET search_path")
    _finalize_migration(conn, migration_id, checksum)


def _acquire_advisory_lock(conn) -> None:
    _set_autocommit(conn, True)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def _release_advisory_lock(conn) -> None:
    with contextlib.suppress(Exception):
        _set_autocommit(conn, True)
    with contextlib.suppress(Exception), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def _apply_discovered_migrations(
    conn,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    files = discover_migration_files(root)
    applied_ids: list[str] = []
    skipped_ids: list[str] = []
    already_applied = fetch_applied_migrations(conn)
    for path in files:
        migration_id = path.name
        content = path.read_text(encoding="utf-8")
        checksum = migration_checksum(content)
        existing = already_applied.get(migration_id)
        if existing is not None:
            _raise_if_in_progress(migration_id, existing)
            if existing != checksum:
                raise MigrationChecksumError(
                    "Checksum drift for already-applied migration "
                    f"{migration_id}: recorded {existing[:12]}…, "
                    f"on-disk {checksum[:12]}…. Edited applied migrations "
                    "are not re-run; create a new migration."
                )
            skipped_ids.append(migration_id)
            continue
        if not migration_has_executable_sql(content):
            raise EmptyMigrationError(
                f"Migration {migration_id} has no executable SQL (empty or template-only)."
            )
        offenders = find_explicit_transaction_control(content)
        if offenders:
            raise MigrationTransactionControlError(
                f"Migration {migration_id} contains explicit "
                "transaction-control statements; the runner owns "
                f"transactions: {offenders}. Remove BEGIN/COMMIT/ROLLBACK/"
                "SAVEPOINT and use the -- transaction: true|false marker "
                "instead."
            )
        if parse_transactional(content):
            _apply_transactional(conn, migration_id, content, checksum)
        else:
            _apply_non_transactional(conn, migration_id, content, checksum)
        applied_ids.append(migration_id)
        logger.info("Applied SQL migration %s", migration_id)

    return {
        "total": len(files),
        "applied": applied_ids,
        "skipped": skipped_ids,
    }


def apply_pending_migrations(
    conn,
    *,
    root: Path | None = None,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Apply unapplied migrations, optionally reusing a caller-owned lock."""
    caller_autocommit = bool(getattr(conn, "autocommit", False))
    lock_acquired = False
    succeeded = False
    try:
        if acquire_lock:
            _acquire_advisory_lock(conn)
            lock_acquired = True
        ensure_migrations_table(conn)
        result = _apply_discovered_migrations(conn, root=root)
        succeeded = True
        return result
    finally:
        if lock_acquired:
            _release_advisory_lock(conn)
        if succeeded:
            _set_autocommit(conn, caller_autocommit)
        else:
            # Preserve the migration failure if a broken connection also
            # rejects cleanup. Healthy connections still recover the caller's
            # transaction mode after the runner has rolled back.
            with contextlib.suppress(Exception):
                _set_autocommit(conn, caller_autocommit)


def run_pending_migrations(*, dsn: str | None = None, root: Path | None = None) -> dict[str, Any]:
    """Connect using the resolved DSN and apply pending ``db/migrations/`` files."""
    from core.application.tooling.db import init_db
    from core.infrastructure.db import psycopg_runtime

    resolved_dsn = str(dsn or init_db._resolve_sql_dsn() or "").strip()
    if not resolved_dsn:
        raise RuntimeError("No Postgres DSN configured for SQL migrations")
    conn = psycopg_runtime.connect(resolved_dsn)
    try:
        return apply_pending_migrations(conn, root=root)
    finally:
        with contextlib.suppress(Exception):
            conn.close()
