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

Platform modules (ADR 0082) ship their own SQL migrations in
``<module dir>/db/migrations/*.sql``. The same runner applies them, but each module
runs in its own schema (``mod_<id>``), is recorded in the separate ledger
``public.platform_module_migrations`` and never in the Core ledger, and every file
first passes :func:`module_migration_violations`. That check and the catalog
fingerprint taken around each file are guard rails, not an isolation boundary; see
:func:`module_migration_violations` for what they do and do not cover.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.application.tooling.db.migration_text import (
    find_explicit_transaction_control,
    generate_migration_filename,
    is_valid_migration_filename,
    migration_has_executable_sql,
    parse_transactional,
    render_migration_template,
)
from core.application.tooling.db.migration_text import (
    migration_timestamp as migration_timestamp,
)
from core.application.tooling.db.migration_text import (
    slugify_migration_name as slugify_migration_name,
)
from core.application.tooling.db.module_migration_guard import (
    _CATALOG_SEARCH_PATH,
    _MODULE_SEARCH_PATH_TEMPLATE,
    MODULE_SCHEMA_PREFIX,
    _existing_schemas,
    _module_catalog_fingerprint,
    module_migration_violations,
)
from core.infrastructure.extension_runtime.package_identity import is_canonical_extension_id

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

MIGRATIONS_DIR_RELATIVE = "db/migrations"
MIGRATIONS_GLOB = "db/migrations/*.sql"
SCHEMA_MIGRATIONS_TABLE = "public.platform_schema_migrations"
MODULE_MIGRATIONS_TABLE = "public.platform_module_migrations"

#: ``COMMENT ON SCHEMA`` text the runner writes when it creates a module schema;
#: the module id follows the prefix. A schema without this exact comment is not
#: the module's, whatever its name.
MODULE_SCHEMA_OWNER_PREFIX = "flow-steward-module:"
#: PostgreSQL truncates identifiers longer than NAMEDATALEN - 1 bytes.
POSTGRES_IDENTIFIER_MAX_BYTES = 63

# Session-level advisory lock key for the migration run ("FMIG"). Distinct from
# the workflow-knowledge schema lock (0x5746574B).
MIGRATION_ADVISORY_LOCK_KEY = 0x464D4947
NON_TRANSACTIONAL_IN_PROGRESS_PREFIX = "in_progress:"
MIGRATION_RECONCILIATION_COMMAND = "./flow-steward database status --json"


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


class ModuleMigrationError(MigrationError, ValueError):
    """A platform module's migration source is invalid (id, schema name, duplicates)."""


class ModuleMigrationGuardError(MigrationError):
    """A platform module's migration reaches outside the module's own schema."""


class ModuleMigrationDowngradeError(MigrationError):
    """A prepared module's ledger records migrations its files no longer carry."""


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


def migration_checksum(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_migration_files(root: Path | None = None) -> list[Path]:
    """Return validated migration files sorted lexicographically by filename."""
    return _discover_sql_files(migrations_dir(root), location="db/migrations/")


def _discover_sql_files(directory: Path, *, location: str) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in directory.glob("*.sql"):
        if not is_valid_migration_filename(path.name):
            raise InvalidMigrationFilenameError(
                f"Invalid migration filename: {path.name}. "
                f"Expected YYYYMMDDHHMMSS_slug.sql under {location}."
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


def _apply_core_file(conn, migration_id: str, content: str, checksum: str) -> None:
    if parse_transactional(content):
        _apply_transactional(conn, migration_id, content, checksum)
    else:
        _apply_non_transactional(conn, migration_id, content, checksum)


def _apply_discovered_migrations(
    conn,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    return _apply_migration_files(
        conn,
        discover_migration_files(root),
        fetch_applied_migrations(conn),
        apply_file=_apply_core_file,
    )


def _apply_migration_files(
    conn,
    files: list[Path],
    already_applied: dict[str, str],
    *,
    apply_file: Callable[[Any, str, str, str], None],
) -> dict[str, Any]:
    """Apply ``files`` not yet in ``already_applied``; the ledger is ``apply_file``'s.

    Shared by Core and platform-module migrations: the same checksum-drift,
    empty-file and transaction-control rules hold for both.
    """
    applied_ids: list[str] = []
    skipped_ids: list[str] = []
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
        apply_file(conn, migration_id, content, checksum)
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

    def work() -> dict[str, Any]:
        ensure_migrations_table(conn)
        return _apply_discovered_migrations(conn, root=root)

    return _run_under_migration_lock(conn, acquire_lock=acquire_lock, work=work)


def _run_under_migration_lock(
    conn,
    *,
    acquire_lock: bool,
    work: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    caller_autocommit = bool(getattr(conn, "autocommit", False))
    lock_acquired = False
    succeeded = False
    try:
        if acquire_lock:
            _acquire_advisory_lock(conn)
            lock_acquired = True
        result = work()
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


# ---------------------------------------------------------------------------
# Platform-module migrations (ADR 0082)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleMigrationSource:
    """One prepared platform module whose ``db/migrations`` the host applies."""

    module_id: str
    module_version: str
    migrations_dir: Path


class ModuleSchemaOwnershipError(ModuleMigrationError):
    """A module's schema exists but the runner did not create it for that module."""


class ModuleSchemaMissingError(ModuleMigrationError):
    """The ledger records a module's migrations as applied but its schema is gone."""


def module_schema_name(module_id: str) -> str:
    """Return the deterministic schema of a platform module: ``mod_`` + mapped id.

    The id must be a canonical package id (ADR 0013): lowercase labels of
    ``[a-z0-9-]`` separated by ``.``, no label starting or ending with ``-``.
    ``.`` maps to ``_`` and ``-`` to ``__``. The mapping is injective: ids carry
    no ``_``, and ``-`` is never next to ``.``, so a run of underscores of odd
    length is one ``.`` and a run of length 2k is k hyphens. ``a.b-c`` and
    ``a.b.c`` therefore never share a schema. PostgreSQL would silently truncate
    a name longer than 63 bytes, so one is refused.
    """
    raw = str(module_id or "")
    if raw != raw.strip() or not is_canonical_extension_id(raw):
        raise ModuleMigrationError(
            f"Platform module id {module_id!r} is not a canonical lowercase dot-separated "
            "package id (ADR 0013), for example 'acme.example' or 'com.acme.example-module'."
        )
    schema = MODULE_SCHEMA_PREFIX + raw.replace("-", "__").replace(".", "_")
    if len(schema.encode("utf-8")) > POSTGRES_IDENTIFIER_MAX_BYTES:
        raise ModuleMigrationError(
            f"Platform module id {module_id!r} gives the schema name {schema!r}, longer than "
            f"PostgreSQL's {POSTGRES_IDENTIFIER_MAX_BYTES}-byte identifier limit."
        )
    return schema


def module_schema_owner_marker(module_id: str) -> str:
    """The ``COMMENT ON SCHEMA`` text that marks a schema as ``module_id``'s."""
    return f"{MODULE_SCHEMA_OWNER_PREFIX}{module_id}"


def ensure_module_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_module_migrations (
                module_id text NOT NULL,
                migration_id text NOT NULL,
                checksum text NOT NULL,
                module_version text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (module_id, migration_id)
            )
            """
        )


def fetch_applied_module_migrations(conn) -> dict[str, dict[str, str]]:
    """Return ``{module_id: {migration_id: checksum}}`` without creating the ledger."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.platform_module_migrations')")
        relation = cur.fetchone()
        if not relation or relation[0] is None:
            return {}
        cur.execute(
            "SELECT module_id, migration_id, checksum FROM public.platform_module_migrations"
        )
        rows = cur.fetchall() or []
    applied: dict[str, dict[str, str]] = {}
    for row in rows:
        if isinstance(row, dict):
            module_id, migration_id, checksum = (
                row["module_id"],
                row["migration_id"],
                row["checksum"],
            )
        else:
            module_id, migration_id, checksum = row[0], row[1], row[2]
        applied.setdefault(str(module_id), {})[str(migration_id)] = str(checksum)
    return applied


def _record_module_migration(
    conn,
    *,
    module_id: str,
    module_version: str,
    migration_id: str,
    checksum: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.platform_module_migrations
                (module_id, migration_id, checksum, module_version)
            VALUES (%s, %s, %s, %s)
            """,
            (module_id, migration_id, checksum, module_version),
        )


def _check_module_schema_owner(
    source: ModuleMigrationSource, schema: str, existing: Mapping[str, str | None]
) -> None:
    """Refuse a module whose schema exists without the runner's mark for that module.

    The runner writes ``COMMENT ON SCHEMA <schema> IS 'flow-steward-module:<id>'``
    in the transaction that creates the schema, and module migrations may not
    touch schema comments. A schema of that name without the exact mark was
    created by someone else: an operator, a module under an earlier name mapping,
    or a module whose ledger rows were removed. Taking it over could hand one
    module another module's tables, so the run stops and an operator decides.
    """
    if schema not in existing:
        return
    marker = existing[schema]
    if marker == module_schema_owner_marker(source.module_id):
        return
    owner = "no owner mark" if marker is None else f"the comment {marker!r}"
    raise ModuleSchemaOwnershipError(
        f"Platform module {source.module_id} maps to the schema {schema}, which already "
        f"exists with {owner} instead of {module_schema_owner_marker(source.module_id)!r}. "
        "The runner did not create it for this module, so it will not adopt it. Rename or "
        "drop that schema after checking whose data it holds, or set the comment by hand "
        "if it really is this module's."
    )


def _create_module_schema(conn, schema: str, module_id: str) -> None:
    # ``schema`` comes from module_schema_name() ([a-z0-9_] only) and
    # ``module_id`` is a canonical id ([a-z0-9.-] only), so both are inlined
    # safely; COMMENT takes no bind parameters. One transaction, so a schema
    # never exists without its owner mark.
    previous_autocommit = bool(getattr(conn, "autocommit", False))
    _set_autocommit(conn, False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"COMMENT ON SCHEMA {schema} IS '{module_schema_owner_marker(module_id)}'")
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise
    finally:
        _set_autocommit(conn, previous_autocommit)


def _coerce_module_source(value: Any) -> ModuleMigrationSource:
    if isinstance(value, ModuleMigrationSource):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        module_id, module_version, directory = value
        return ModuleMigrationSource(str(module_id), str(module_version), Path(directory))
    raise ModuleMigrationError(
        "A platform module migration source is a ModuleMigrationSource or a "
        f"(module_id, module_version, migrations_dir) tuple, got {type(value).__name__}."
    )


def _validated_module_sources(
    modules: Iterable[Any],
) -> list[tuple[ModuleMigrationSource, str]]:
    validated: list[tuple[ModuleMigrationSource, str]] = []
    seen_schemas: dict[str, str] = {}
    for value in modules:
        source = _coerce_module_source(value)
        schema = module_schema_name(source.module_id)
        if not str(source.module_version or "").strip():
            raise ModuleMigrationError(f"Platform module {source.module_id} has no version.")
        previous = seen_schemas.get(schema)
        if previous is not None:
            # The mapping is injective, so only the same id prepared twice gets here.
            raise ModuleMigrationError(
                f"Platform modules {previous} and {source.module_id} map to the same "
                f"schema {schema}; only one of them can be prepared."
            )
        seen_schemas[schema] = source.module_id
        validated.append((source, schema))
    return validated


def _module_files(source: ModuleMigrationSource) -> list[Path]:
    directory = Path(source.migrations_dir)
    return _discover_sql_files(directory, location=str(directory))


def inspect_module_migrations(conn, modules: Iterable[Any]) -> dict[str, dict[str, list[str]]]:
    """Classify each prepared module's migrations without changing anything.

    Returns ``{module_id: {"pending": [...], "drifted": [...], "unknown": [...]}}``.
    ``unknown`` rows mean the module was rolled back over a newer schema; the
    upgrade refuses that module. Without the ledger every shipped file is pending.
    With no prepared module the database is not queried at all.
    """
    sources = _validated_module_sources(modules)
    if not sources:
        return {}
    applied = fetch_applied_module_migrations(conn)
    report: dict[str, dict[str, list[str]]] = {}
    for source, _schema in sources:
        files = _module_files(source)
        recorded = applied.get(source.module_id, {})
        pending: list[str] = []
        drifted: list[str] = []
        for path in files:
            checksum = recorded.get(path.name)
            if checksum is None:
                pending.append(path.name)
            elif checksum != migration_checksum(path.read_text(encoding="utf-8")):
                drifted.append(path.name)
        report[source.module_id] = {
            "pending": pending,
            "drifted": drifted,
            "unknown": sorted(set(recorded) - {path.name for path in files}),
        }
    return report


def module_migrations_need_upgrade(report: dict[str, dict[str, list[str]]]) -> bool:
    """Whether an :func:`inspect_module_migrations` report needs the database step."""
    return any(entry.get(key) for entry in report.values() for key in entry)


@dataclass(frozen=True)
class _ModulePlan:
    source: ModuleMigrationSource
    schema: str
    files: tuple[Path, ...]
    applied: dict[str, str]
    schema_exists: bool


def _guard_module_file(plan: _ModulePlan, migration_id: str, content: str, foreign) -> None:
    violations = module_migration_violations(
        content, schema_name=plan.schema, foreign_schemas=foreign
    )
    if violations:
        raise ModuleMigrationGuardError(
            f"Platform module {plan.source.module_id} migration {migration_id} was refused "
            f"before execution: {'; '.join(violations)}. Module migrations are plain DDL "
            "and DML on the module's own schema; procedural, dynamic and session-changing "
            "SQL is not accepted."
        )


def _plan_module(
    source: ModuleMigrationSource,
    schema: str,
    applied: dict[str, str],
    existing: Mapping[str, str | None],
) -> _ModulePlan:
    _check_module_schema_owner(source, schema, existing)
    files = _module_files(source)
    unknown = sorted(set(applied) - {path.name for path in files})
    if unknown:
        raise ModuleMigrationDowngradeError(
            f"Platform module {source.module_id} {source.module_version} does not carry "
            f"migrations its ledger records as applied: {unknown}. A newer version of the "
            "module upgraded this database; prepare that version again or restore the "
            "backup taken before it."
        )
    if applied and schema not in existing:
        legacy = MODULE_SCHEMA_PREFIX + source.module_id.replace(".", "_").replace("-", "_")
        raise ModuleSchemaMissingError(
            f"Platform module {source.module_id}: the ledger records {len(applied)} applied "
            f"migration(s), but the schema {schema} does not exist. Either the schema was "
            "dropped (restore it from a backup, or delete this module's rows from "
            "public.platform_module_migrations to start the module empty), or it was "
            f"created under the earlier name mapping (look for {legacy}) and has to be "
            f"renamed to {schema} and marked with COMMENT ON SCHEMA {schema} IS "
            f"'{module_schema_owner_marker(source.module_id)}'. No module was migrated."
        )
    plan = _ModulePlan(source, schema, tuple(files), dict(applied), schema in existing)
    foreign = frozenset(existing)
    for path in files:
        if path.name not in applied:
            _guard_module_file(plan, path.name, path.read_text(encoding="utf-8"), foreign)
    return plan


def _apply_module_file(
    conn,
    plan: _ModulePlan,
    migration_id: str,
    content: str,
    checksum: str,
) -> None:
    previous_autocommit = bool(getattr(conn, "autocommit", False))
    _set_autocommit(conn, False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL search_path TO {_CATALOG_SEARCH_PATH}")
            before = _module_catalog_fingerprint(cur, plan.schema)
            # The static guard lexes plain '...' literals with backslash taken
            # literally; make that true for this transaction. The guard refuses
            # any file that changes the setting back.
            cur.execute("SET LOCAL standard_conforming_strings TO on")
            module_path = _MODULE_SEARCH_PATH_TEMPLATE.format(schema=plan.schema)
            cur.execute(f"SET LOCAL search_path TO {module_path}")
            cur.execute(content)
            cur.execute(f"SET LOCAL search_path TO {_CATALOG_SEARCH_PATH}")
        _record_module_migration(
            conn,
            module_id=plan.source.module_id,
            module_version=plan.source.module_version,
            migration_id=migration_id,
            checksum=checksum,
        )
        # Order matters. Deferred constraint triggers fire at COMMIT, after any
        # check taken before it, so they are forced to fire now; and the ledger
        # insert itself can fire triggers. The "after" fingerprint is therefore
        # the last statement before COMMIT, once nothing else is left to run.
        with conn.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            after = _module_catalog_fingerprint(cur, plan.schema)
        if after.public != before.public:
            raise ModuleMigrationGuardError(
                f"Platform module {plan.source.module_id} migration {migration_id} changed "
                "the public schema's catalog and was rolled back."
            )
        if after.other_modules != before.other_modules:
            raise ModuleMigrationGuardError(
                f"Platform module {plan.source.module_id} migration {migration_id} changed "
                "another module's schema (or a module schema's name or owner mark) and was "
                "rolled back."
            )
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        raise
    finally:
        _set_autocommit(conn, previous_autocommit)


def _apply_module_plan(conn, plan: _ModulePlan, foreign: frozenset[str]) -> dict[str, Any]:
    if plan.files and not plan.schema_exists:
        _create_module_schema(conn, plan.schema, plan.source.module_id)

    def apply_file(file_conn, migration_id: str, content: str, checksum: str) -> None:
        # Checked again on the exact text that runs.
        _guard_module_file(plan, migration_id, content, foreign)
        _apply_module_file(file_conn, plan, migration_id, content, checksum)

    result = _apply_migration_files(conn, list(plan.files), plan.applied, apply_file=apply_file)
    return {
        "module_id": plan.source.module_id,
        "module_version": plan.source.module_version,
        "schema": plan.schema,
        **result,
    }


def apply_platform_module_migrations(
    conn,
    modules: Iterable[Any],
    *,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Apply the migrations of every prepared platform module, in the given order.

    Each file runs in one transaction under ``SET LOCAL search_path TO <schema>,
    pg_catalog, pg_temp`` and ``standard_conforming_strings = on`` and is recorded in
    ``public.platform_module_migrations``, never in the Core ledger. An upgrade
    from the previous module version is the files that version did not have; an
    applied file whose content changed is an error.

    Before any file runs, every module is checked: a module whose schema exists
    without the runner's owner mark for it, a module whose ledger records files it
    no longer ships (a downgrade), a module whose ledger records applied files but
    whose schema is missing, and any pending file the static guard refuses
    stop the whole run. Around each file the runner compares content fingerprints
    of the ``public`` catalog and of every other module schema; a difference rolls
    the file back. These detect structural changes only; see
    :func:`module_migration_violations` for what is not covered.

    Ledger rows of a module that is no longer prepared are reported under
    ``orphaned_modules`` and never block: removing a module keeps its schema,
    its owner mark and its data. With no module prepared, the ledger is read but
    never created.
    """
    sources = _validated_module_sources(modules)

    def work() -> dict[str, Any]:
        if sources:
            ensure_module_migrations_table(conn)
        applied = fetch_applied_module_migrations(conn)
        existing = _existing_schemas(conn) if sources else {}
        foreign = frozenset(existing)
        plans = [
            _plan_module(source, schema, applied.get(source.module_id, {}), existing)
            for source, schema in sources
        ]
        results = [_apply_module_plan(conn, plan, foreign) for plan in plans]
        prepared = {source.module_id for source, _schema in sources}
        orphaned = [
            {"module_id": module_id, "migrations": sorted(rows)}
            for module_id, rows in sorted(applied.items())
            if module_id not in prepared
        ]
        if orphaned:
            logger.warning(
                "Module migration ledger has rows for modules that are not prepared: %s",
                [entry["module_id"] for entry in orphaned],
            )
        return {"modules": results, "orphaned_modules": orphaned}

    return _run_under_migration_lock(conn, acquire_lock=acquire_lock, work=work)


def apply_module_migrations(
    conn,
    *,
    module_id: str,
    module_version: str,
    migrations_dir: Path,
    acquire_lock: bool = True,
) -> dict[str, Any]:
    """Apply one platform module's unapplied migrations; see the function above."""
    source = ModuleMigrationSource(module_id, module_version, Path(migrations_dir))
    result = apply_platform_module_migrations(conn, [source], acquire_lock=acquire_lock)
    return result["modules"][0]


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
