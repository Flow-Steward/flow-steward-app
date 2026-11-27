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
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infrastructure.extension_runtime.package_identity import is_canonical_extension_id

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]

MIGRATIONS_DIR_RELATIVE = "db/migrations"
MIGRATIONS_GLOB = "db/migrations/*.sql"
SCHEMA_MIGRATIONS_TABLE = "public.platform_schema_migrations"
MODULE_MIGRATIONS_TABLE = "public.platform_module_migrations"

#: Prefix of every platform-module schema; the rest is the module id with ``.``
#: mapped to ``_`` and ``-`` mapped to ``__`` (see :func:`module_schema_name`).
MODULE_SCHEMA_PREFIX = "mod_"
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

# Lexical classes of PostgreSQL's scanner (scan.l). Bytes >= 0x80 count as
# identifier characters there, so every non-ASCII character does here.
_NON_ASCII = "\u0080-\U0010ffff"
_IDENT_START_RE = re.compile(rf"[A-Za-z_{_NON_ASCII}]")
_IDENT_CONT_RE = re.compile(rf"[A-Za-z0-9_${_NON_ASCII}]*")
# Dollar-quote opener: $$ or $tag$. A tag never contains ``$``.
_DOLLAR_TAG_RE = re.compile(rf"\$(?:[A-Za-z_{_NON_ASCII}][A-Za-z0-9_{_NON_ASCII}]*)?\$")
# PostgreSQL ends a ``--`` comment at either newline character.
_LINE_END = "\r\n"
# A plain identifier, the only quoted identifier the code view unquotes.
_PLAIN_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
# What may follow a closed escape string and still continue it: PostgreSQL glues
# 'a'<newline>'b' into one literal and keeps the first part's escape mode.
_STRING_CONTINUATION_RE = re.compile(r"(?:\s|--[^\r\n]*)*'")

# A top-level statement that opens/closes/marks a transaction. Migration files
# must not contain explicit transaction control — the runner owns transactions
# so the body and the platform_schema_migrations insert stay atomic. This is
# only matched against top-level statement starts (dollar-quoted PL/pgSQL bodies
# and CASE...END remain one statement and are not flagged).
_TXN_CONTROL_START_RE = re.compile(
    r"^(begin|start\s+transaction|commit|end(\s+transaction)?|rollback|abort"
    r"|savepoint|release(\s+savepoint)?|prepare\s+transaction)\b",
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


@dataclass(frozen=True)
class _ScannedSql:
    """One pass of a PostgreSQL-compatible scanner over a migration file.

    ``text``  comments replaced by a space; literals and identifiers as written.
    ``code``  the same, with every string or dollar-quoted literal replaced by
              ``''`` and every quoted identifier replaced by its lowercased name
              (or ``_quoted_identifier_`` when the name is not a plain one), so
              ``;`` only ever separates statements and keywords are never hidden
              in a literal or behind quotes.
    ``unsupported`` constructs the scanner refuses rather than interprets.

    It follows PostgreSQL's lexer with ``standard_conforming_strings = on``: a
    plain ``'...'`` treats backslash literally, ``E'...'`` honours backslash
    escapes, block comments nest, a ``--`` comment ends at ``\\n`` or ``\\r``,
    identifiers may contain ``$`` (so ``a$$`` is a name, not a dollar quote), and
    a dollar quote opens only where a token starts.
    """

    text: str
    code: str
    unsupported: tuple[str, ...]


def _scan_quoted(sql: str, start: int, quote: str, *, backslash_escapes: bool) -> int:
    """Index just past the literal or identifier opened at ``start - 1``; -1 if open."""
    i = start
    n = len(sql)
    while i < n:
        ch = sql[i]
        if backslash_escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


class _SqlScanner:
    """The state of one :func:`_scan_sql` pass; each ``_take_*`` returns the next index."""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.n = len(sql)
        self.text: list[str] = []
        self.code: list[str] = []
        self.unsupported: list[str] = []

    def refuse(self, reason: str) -> None:
        if reason not in self.unsupported:
            self.unsupported.append(reason)

    def emit(self, text: str, code: str) -> None:
        self.text.append(text)
        self.code.append(code)

    def closing(self, start: int, quote: str, *, backslash_escapes: bool, kind: str) -> int:
        end = _scan_quoted(self.sql, start, quote, backslash_escapes=backslash_escapes)
        if end < 0:
            self.refuse(f"an unterminated {kind}")
            return self.n
        return end

    def take_line_comment(self, i: int) -> int:
        j = i + 2
        while j < self.n and self.sql[j] not in _LINE_END:
            j += 1
        self.emit(" ", " ")
        return j

    def take_block_comment(self, i: int) -> int:
        depth = 1
        j = i + 2
        while j < self.n and depth:
            if self.sql.startswith("/*", j):
                depth += 1
                j += 2
            elif self.sql.startswith("*/", j):
                depth -= 1
                j += 2
            else:
                j += 1
        if depth:
            self.refuse("an unterminated block comment")
        self.emit(" ", " ")
        return j

    def take_string(self, i: int, body_start: int, *, backslash_escapes: bool) -> int:
        end = self.closing(
            body_start, "'", backslash_escapes=backslash_escapes, kind="string literal"
        )
        if backslash_escapes and end < self.n and _STRING_CONTINUATION_RE.match(self.sql, end):
            self.refuse("an escape string continued in a second quoted part")
        self.emit(self.sql[i:end], " '' ")
        return end

    def take_identifier(self, i: int, body_start: int) -> int:
        end = self.closing(body_start, '"', backslash_escapes=False, kind="quoted identifier")
        name = self.sql[body_start : end - 1].replace('""', '"')
        if _PLAIN_IDENT_RE.fullmatch(name):
            self.emit(self.sql[i:end], f" {name.lower()} ")
        else:
            self.emit(self.sql[i:end], " _quoted_identifier_ ")
        return end

    def take_dollar(self, i: int) -> int:
        match = _DOLLAR_TAG_RE.match(self.sql, i)
        if not match:  # $1 and friends
            self.emit("$", "$")
            return i + 1
        tag = match.group(0)
        close = self.sql.find(tag, match.end())
        if close < 0:
            self.refuse("an unterminated dollar-quoted string")
            end = self.n
        else:
            end = close + len(tag)
        self.emit(self.sql[i:end], " '' ")
        return end

    def take_word(self, i: int) -> int:
        """An identifier or keyword, or the ``E'``/``U&`` prefix of a literal."""
        sql = self.sql
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < self.n else ""
        if ch in "eE" and nxt == "'":
            return self.take_string(i, i + 2, backslash_escapes=True)
        if ch in "uU" and nxt == "&" and i + 2 < self.n and sql[i + 2] in "'\"":
            self.refuse("a Unicode-escape string or identifier (U&'...' or U&\"...\")")
            if sql[i + 2] == "'":
                return self.take_string(i, i + 3, backslash_escapes=False)
            end = self.closing(i + 3, '"', backslash_escapes=False, kind="quoted identifier")
            self.emit(sql[i:end], " _quoted_identifier_ ")
            return end
        match = _IDENT_CONT_RE.match(sql, i + 1)
        end = match.end() if match else i + 1
        self.emit(sql[i:end], sql[i:end])
        return end

    def step(self, i: int) -> int:
        ch = self.sql[i]
        pair = self.sql[i : i + 2]
        if pair == "--":
            return self.take_line_comment(i)
        if pair == "/*":
            return self.take_block_comment(i)
        if ch == "'":
            return self.take_string(i, i + 1, backslash_escapes=False)
        if ch == '"':
            return self.take_identifier(i, i + 1)
        if ch == "$":
            return self.take_dollar(i)
        if _IDENT_START_RE.match(ch):
            return self.take_word(i)
        self.emit(ch, ch)
        return i + 1


def _scan_sql(sql: str) -> _ScannedSql:
    scanner = _SqlScanner(sql)
    i = 0
    while i < scanner.n:
        i = scanner.step(i)
    return _ScannedSql("".join(scanner.text), "".join(scanner.code), tuple(scanner.unsupported))


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line/block comments, preserving string and dollar-quoted text."""
    return _scan_sql(sql).text


def _code_statements(code: str) -> list[str]:
    """Top-level statements of a scanned ``code`` view, stripped, empty ones dropped."""
    return [part.strip() for part in code.split(";") if part.strip()]


def migration_has_executable_sql(sql: str) -> bool:
    """Return True if the migration contains SQL beyond comments/whitespace."""
    stripped = _strip_sql_comments(str(sql or ""))
    return bool(stripped.replace(";", "").strip())


def find_explicit_transaction_control(sql: str) -> list[str]:
    """Return offending top-level transaction-control statements, if any.

    The runner owns transactions; a migration that opens/commits its own
    transaction would break the atomicity of the body plus the tracking insert.
    Statements are split on the scanner's ``code`` view (:func:`_scan_sql`), where
    every literal is already blanked, so ``BEGIN``/``COMMIT`` inside a PL/pgSQL
    function body, a ``DO`` block or an ``E'...'`` string is not flagged, and one
    hidden behind a mis-read literal is.
    """
    offenders: list[str] = []
    for statement in _code_statements(_scan_sql(str(sql or "")).code):
        if _TXN_CONTROL_START_RE.match(statement):
            offenders.append(statement[:40])
    return offenders


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


#: Schemas a module migration may qualify a name with besides its own.
#: ``pg_temp`` is not one: module migrations create no temporary objects.
_SHARED_SCHEMAS = frozenset({"pg_catalog", "information_schema"})

#: The runner's own catalog reads inside a module transaction. ``pg_catalog``
#: first and ``pg_temp`` named explicitly, so neither a module object nor a
#: temporary table can shadow a catalog relation, function or operator.
_CATALOG_SEARCH_PATH = "pg_catalog, pg_temp"

#: Template of the path a module file runs under. PostgreSQL searches ``pg_temp``
#: FIRST for relations and types whenever the path does not name it, and one
#: connection serves every module file of a run and Core's later steps, so a
#: temporary ``notes`` left behind by one module would capture another module's
#: unqualified ``INSERT INTO notes``. Naming ``pg_temp`` last makes the module
#: schema and ``pg_catalog`` win every lookup. (Functions and operators are never
#: looked up in ``pg_temp``.)
_MODULE_SEARCH_PATH_TEMPLATE = "{schema}, pg_catalog, pg_temp"

#: ``LIKE`` pattern of every module schema (``\_`` is a literal underscore).
_MODULE_SCHEMA_LIKE = "mod\\_%"

_QUALIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9_$])\"?([A-Za-z_][A-Za-z0-9_$]*)\"?\s*\.\s*(?=[^\s0-9.])"
)
_SCHEMA_CLAUSE_RE = re.compile(
    r"\b(?:create|alter|in|on|set)\s+schema\s+(?:if\s+not\s+exists\s+)?\"?([A-Za-z_][A-Za-z0-9_$]*)\"?",
    re.IGNORECASE,
)

#: Refused when they appear anywhere in the ``code`` view (literals blanked,
#: quoted identifiers unquoted). Module migrations are plain DDL/DML on the
#: module's own schema; anything procedural, dynamic, session-changing or acting
#: on the server is refused rather than analysed.
_FORBIDDEN_SETTING_NAMES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bsearch_path\b", "changes search_path"),
        (r"\bset_config\b", "calls set_config()"),
        (r"\bstandard_conforming_strings\b", "changes standard_conforming_strings"),
    )
)
_FORBIDDEN_MODULE_SQL: tuple[tuple[re.Pattern[str], str], ...] = _FORBIDDEN_SETTING_NAMES + tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\b(?:reset|discard)\s+all\b", "resets session settings (search_path included)"),
        (r"\b(?:create|alter|drop)\s+extension\b", "creates, alters or drops an extension"),
        (
            r"\balter\s+(?:system|role|user|group|database)\b",
            "alters a role, the database or the server configuration",
        ),
        (r"\b(?:create|drop)\s+(?:role|user|group)\b", "creates or drops a role"),
        (
            r"\b(?:create|alter)\s+schema\b|\bcomment\s+on\s+schema\b",
            "creates, alters or comments on a schema (the runner owns the schema)",
        ),
        (r"\bdrop\s+schema\b", "drops a schema"),
        (r"\b(?:grant|revoke)\b", "grants or revokes privileges"),
        (r"\balter\s+default\s+privileges\b", "alters default privileges"),
        (
            r"\b(?:create|alter|drop)\s+(?:or\s+replace\s+)?(?:function|procedure|routine)\b",
            "creates or changes a function or procedure",
        ),
        (r"\bcreate\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\b", "creates a trigger"),
        (r"\bcreate\s+(?:or\s+replace\s+)?event\s+trigger\b", "creates an event trigger"),
        (r"\bcreate\s+(?:or\s+replace\s+)?rule\b", "creates a rule"),
        (r"\bexecute\b", "uses EXECUTE (dynamic SQL or a prepared statement)"),
        (r"\bprepare\b", "uses PREPARE"),
        (r"\bsecurity\s+definer\b", "declares SECURITY DEFINER"),
        (r"\bset\s+schema\b", "moves an object to another schema (SET SCHEMA)"),
        (r"\bowner\s+to\b", "changes an owner (OWNER TO)"),
        (
            r"\b(?:lo_[a-z0-9_]+|loread|lowrite)\s*\(|\bpg_largeobject",
            "uses large-object functions",
        ),
        (
            r"\bpg_(?:read|write|ls|execute)_[a-z0-9_]*|\bpg_stat_file\b|\bpg_file_[a-z0-9_]*",
            "uses server file or program functions",
        ),
        (r"\bdblink[a-z0-9_]*", "uses dblink"),
        (r"\bpg_notify\b", "sends a notification (pg_notify)"),
        (
            r"\b(?:temp|temporary)\s+(?:table|view|sequence|recursive)\b"
            r"|\binto\s+(?:unlogged\s+)?(?:temp|temporary)\b"
            r"|\b(?:global|local)\s+(?:temp|temporary)\b",
            "creates a temporary object",
        ),
        (r"\bon\s+commit\b", "uses an ON COMMIT clause"),
        (
            r"\bpg_(?:toast_)?temp(?:_[0-9]+)?\s*\.",
            "qualifies a name with pg_temp",
        ),
        (
            r"\b(?:query|cursor|table|schema|database)_to_xml[a-z_]*",
            "runs or reads SQL by text or computed name (*_to_xml)",
        ),
        (r"\bsetval\b", "sets a sequence value (setval)"),
        (
            r"\breg(?:class|proc|procedure|oper|operator|type|namespace|role|config"
            r"|dictionary|collation)\b",
            "uses a reg* object-identifier type (a name resolved at run time)",
        ),
        (r"\bto_reg[a-z]*\s*\(", "resolves a name at run time (to_reg*)"),
        (r"\b(?:alter|drop)\s+event\s+trigger\b", "alters or drops an event trigger"),
        (r"\b(?:create|drop)\s+access\s+method\b", "creates or drops an access method"),
        (r"\b(?:drop|reassign)\s+owned\b", "uses DROP OWNED or REASSIGN OWNED"),
        (
            r"\bconcurrently\b",
            "uses CONCURRENTLY (CREATE INDEX, DROP INDEX and REINDEX CONCURRENTLY cannot "
            "run inside the migration's transaction)",
        ),
        (
            r"\b(?:create|alter|drop)\s+(?:or\s+replace\s+)?(?:cast|(?:trusted\s+)?"
            r"(?:procedural\s+)?language|foreign\s+data\s+wrapper|server|user\s+mapping"
            r"|publication|subscription|tablespace)\b",
            "creates, alters or drops a database-wide object",
        ),
    )
)

#: ``nextval(``/``currval(`` (any case, quoted or not); checked on the ``text``
#: view, where the argument literal is still visible.
_SEQUENCE_CALL_RE = re.compile(r'(?<![A-Za-z0-9_$])"?(?:nextval|currval)"?\s*\(', re.IGNORECASE)
#: The only argument accepted: ``'name'`` with no schema, quote or escape.
_PLAIN_SEQUENCE_ARGUMENT_RE = re.compile(r"\s*'[A-Za-z_][A-Za-z0-9_$]*'\s*\)")

#: Refused when a top-level statement starts with them (checked per statement of
#: the ``code`` view, so ``UPDATE t SET role = ...`` or ``ON CONFLICT DO NOTHING``
#: are not mistaken for ``SET ROLE`` or ``DO``).
_FORBIDDEN_MODULE_STATEMENTS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (
            r"^(?:set\s+(?:(?:session|local)\s+)?(?:role|session\s+authorization)"
            r"|reset\s+(?:role|session\s+authorization))\b",
            "switches the session role",
        ),
        (r"^(?:set|reset)\b", "changes a session setting (SET/RESET)"),
        (r"^do\b", "runs a DO block"),
        (r"^call\b", "calls a procedure (CALL)"),
        (r"^copy\b", "uses COPY"),
        (r"^(?:listen|notify|unlisten)\b", "uses LISTEN/NOTIFY/UNLISTEN"),
        (r"^lock\b", "takes an explicit LOCK"),
        (r"^(?:vacuum|cluster|reindex)\b", "runs VACUUM, CLUSTER or REINDEX"),
        (r"^load\b", "loads a shared library (LOAD)"),
        (r"^import\b", "imports a foreign schema"),
        (r"^discard\b", "discards session state (DISCARD)"),
    )
)


# Catalog fingerprint. Each query takes ``%(schemas)s`` (a list of schema names)
# and returns rows in a deterministic order; the runner hashes the rows. Run
# only under ``SET LOCAL search_path TO pg_catalog, pg_temp`` so the deparsing
# functions qualify names the same way every time. No query reads table rows:
# row content of any table, the two ledgers included, is never part of it.

#: Schema name, owner, privileges and comment (the ownership marker).
_FINGERPRINT_SCHEMAS_SQL = """
    SELECT n.nspname::text,
           pg_catalog.pg_get_userbyid(n.nspowner)::text,
           coalesce(n.nspacl::text, ''),
           coalesce(pg_catalog.obj_description(n.oid, 'pg_namespace'), '')
      FROM pg_catalog.pg_namespace AS n
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1
"""

#: Every relation (tables, views, indexes, sequences, ...) with its kind,
#: persistence, owner, privileges, row-level-security flags and view text.
_FINGERPRINT_RELATIONS_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           c.relkind::text,
           c.relpersistence::text,
           pg_catalog.pg_get_userbyid(c.relowner)::text,
           coalesce(c.relacl::text, ''),
           c.relrowsecurity,
           c.relforcerowsecurity,
           CASE WHEN c.relkind IN ('v', 'm')
                THEN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    pg_catalog.pg_get_viewdef(c.oid), 'UTF8')), 'hex')
                ELSE '' END
      FROM pg_catalog.pg_class AS c
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2
"""

#: Columns: name, type with modifier, NOT NULL, default expression, identity
#: and generated kind, in column order.
_FINGERPRINT_COLUMNS_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           a.attnum,
           a.attname::text,
           pg_catalog.format_type(a.atttypid, a.atttypmod),
           a.attnotnull,
           coalesce(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''),
           a.attidentity::text,
           a.attgenerated::text
      FROM pg_catalog.pg_attribute AS a
      JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
      LEFT JOIN pg_catalog.pg_attrdef AS d
        ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
       AND a.attnum > 0
       AND NOT a.attisdropped
     ORDER BY 1, 2, 3
"""

#: Constraints of tables and domains, as ``pg_get_constraintdef`` prints them.
_FINGERPRINT_CONSTRAINTS_SQL = """
    SELECT n.nspname::text,
           coalesce(c.relname::text, ''),
           k.conname::text,
           k.contype::text,
           pg_catalog.pg_get_constraintdef(k.oid),
           k.condeferrable,
           k.condeferred,
           k.convalidated
      FROM pg_catalog.pg_constraint AS k
      JOIN pg_catalog.pg_namespace AS n ON n.oid = k.connamespace
      LEFT JOIN pg_catalog.pg_class AS c ON c.oid = k.conrelid
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2, 3, 5
"""

#: Triggers, internal ones included, with their definition and enabled state.
_FINGERPRINT_TRIGGERS_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           t.tgname::text,
           pg_catalog.pg_get_triggerdef(t.oid),
           t.tgenabled::text
      FROM pg_catalog.pg_trigger AS t
      JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2, 3
"""

#: Functions and procedures: oid, name, argument and result types, a hash of
#: the source text, SECURITY DEFINER, settings, owner and privileges.
_FINGERPRINT_FUNCTIONS_SQL = """
    SELECT n.nspname::text,
           p.proname::text,
           pg_catalog.pg_get_function_identity_arguments(p.oid),
           p.oid::text,
           coalesce(pg_catalog.pg_get_function_result(p.oid), ''),
           pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p.prosrc, 'UTF8')), 'hex'),
           p.prosecdef,
           coalesce(p.proconfig::text, ''),
           pg_catalog.pg_get_userbyid(p.proowner)::text,
           coalesce(p.proacl::text, '')
      FROM pg_catalog.pg_proc AS p
      JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2, 3, 4
"""

#: Row-level-security policies with command, roles and both expressions.
_FINGERPRINT_POLICIES_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           y.polname::text,
           y.polcmd::text,
           y.polpermissive,
           y.polroles::text,
           coalesce(pg_catalog.pg_get_expr(y.polqual, y.polrelid), ''),
           coalesce(pg_catalog.pg_get_expr(y.polwithcheck, y.polrelid), '')
      FROM pg_catalog.pg_policy AS y
      JOIN pg_catalog.pg_class AS c ON c.oid = y.polrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2, 3
"""

#: Rewrite rules (views' ``_RETURN`` rules included) and their enabled state.
_FINGERPRINT_RULES_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           r.rulename::text,
           pg_catalog.pg_get_ruledef(r.oid),
           r.ev_enabled::text
      FROM pg_catalog.pg_rewrite AS r
      JOIN pg_catalog.pg_class AS c ON c.oid = r.ev_class
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2, 3
"""

#: Index definitions as ``pg_get_indexdef`` prints them.
_FINGERPRINT_INDEXES_SQL = """
    SELECT n.nspname::text,
           i.relname::text,
           pg_catalog.pg_get_indexdef(x.indexrelid)
      FROM pg_catalog.pg_index AS x
      JOIN pg_catalog.pg_class AS i ON i.oid = x.indexrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = i.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2
"""

#: Sequence parameters (never their current value).
_FINGERPRINT_SEQUENCES_SQL = """
    SELECT n.nspname::text,
           c.relname::text,
           pg_catalog.format_type(s.seqtypid, NULL),
           s.seqstart,
           s.seqincrement,
           s.seqmax,
           s.seqmin,
           s.seqcache,
           s.seqcycle
      FROM pg_catalog.pg_sequence AS s
      JOIN pg_catalog.pg_class AS c ON c.oid = s.seqrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname::text = ANY (%(schemas)s::text[])
     ORDER BY 1, 2
"""

#: Every part of a schema's fingerprint, in hashing order.
_FINGERPRINT_QUERIES: tuple[tuple[str, str], ...] = (
    ("schemas", _FINGERPRINT_SCHEMAS_SQL),
    ("relations", _FINGERPRINT_RELATIONS_SQL),
    ("columns", _FINGERPRINT_COLUMNS_SQL),
    ("constraints", _FINGERPRINT_CONSTRAINTS_SQL),
    ("triggers", _FINGERPRINT_TRIGGERS_SQL),
    ("functions", _FINGERPRINT_FUNCTIONS_SQL),
    ("policies", _FINGERPRINT_POLICIES_SQL),
    ("rules", _FINGERPRINT_RULES_SQL),
    ("indexes", _FINGERPRINT_INDEXES_SQL),
    ("sequences", _FINGERPRINT_SEQUENCES_SQL),
)

#: Names of every module schema, the running module's own included.
_MODULE_SCHEMA_NAMES_SQL = """
    SELECT n.nspname::text
      FROM pg_catalog.pg_namespace AS n
     WHERE n.nspname LIKE %(pattern)s
     ORDER BY 1
"""

#: Every schema with its comment, read once per run for the ownership check.
_SCHEMAS_WITH_COMMENTS_SQL = """
    SELECT n.nspname::text,
           pg_catalog.obj_description(n.oid, 'pg_namespace')
      FROM pg_catalog.pg_namespace AS n
"""


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


def module_migration_violations(
    sql: str,
    *,
    schema_name: str,
    foreign_schemas: Iterable[str] = (),
) -> list[str]:
    """Return why a platform-module migration may not run; empty when it may.

    What this is: a static lint, run before anything executes, that catches honest
    mistakes and refuses a fixed list of constructs. It is not a sandbox and not a
    security boundary.

    Module migrations are plain DDL and DML on the module's own schema. The file is
    scanned with a PostgreSQL-compatible lexer (:func:`_scan_sql`; the runner sets
    ``standard_conforming_strings = on`` so plain literals lex the way the scanner
    assumes) and refused when it contains:

    - a name qualified with a schema other than the module's own, ``pg_catalog``
      and ``information_schema`` (``pg_temp`` included): always ``public``, any other
      ``mod_*`` schema, plus every name in ``foreign_schemas`` (the runner passes
      the schemas that exist in the database). Literal text is searched too;
    - ``U&'...'``/``U&"..."``, an escape string continued in a second quoted part,
      or an unterminated literal or comment;
    - anything procedural or dynamic: ``DO``, ``CREATE/ALTER/DROP FUNCTION`` or
      ``PROCEDURE``, triggers (``CONSTRAINT TRIGGER`` and event triggers included),
      rules, ``EXECUTE``, ``PREPARE``, ``CALL``, ``SECURITY DEFINER``;
    - session changes: every ``SET``/``RESET`` statement (``SET ROLE``, ``SET
      SESSION AUTHORIZATION`` and ``RESET ROLE`` reported as role switches),
      ``search_path``, ``standard_conforming_strings``, ``set_config``,
      ``RESET ALL`` and every ``DISCARD``;
    - temporary objects (``TEMP``/``TEMPORARY``, ``SELECT ... INTO TEMP``) and
      ``ON COMMIT`` clauses;
    - names resolved or SQL run from text at run time: ``*_to_xml`` functions
      (``query_to_xml``, ``table_to_xml``, ``schema_to_xml`` ...), every ``reg*``
      type (``::regclass`` and the rest, casts included) and ``to_reg*``;
      ``setval``; ``nextval``/``currval`` unless the argument is one plain
      single-quoted sequence name without a schema;
    - ``CONCURRENTLY`` (it cannot run inside the migration's transaction),
      ``ALTER``/``DROP EVENT TRIGGER``, ``CREATE``/``DROP ACCESS METHOD``,
      ``DROP OWNED``, ``REASSIGN OWNED``;
    - ``COPY``, ``LISTEN``/``NOTIFY``/``UNLISTEN``/``pg_notify``, ``LOCK``,
      ``VACUUM``/``CLUSTER``/``REINDEX``, ``LOAD``, ``IMPORT FOREIGN SCHEMA``;
    - large-object functions, ``pg_read_*``/``pg_write_*``/``pg_ls_*``/
      ``pg_execute_*`` and other server file functions, ``dblink``;
    - schema changes (``CREATE``/``ALTER``/``DROP SCHEMA``, ``COMMENT ON SCHEMA``,
      ``... SET SCHEMA``), ``OWNER TO``, extensions, roles, the database or server
      settings, ``GRANT``/``REVOKE``, ``ALTER DEFAULT PRIVILEGES`` and other
      database-wide objects;
    - explicit transaction control and ``-- transaction: false``.

    Keywords are matched on the scanner's ``code`` view, where literals are blank
    and quoted identifiers are unquoted, so a keyword inside a literal is not
    refused and one spelled ``"set_config"`` is.

    Unqualified names are not judged: the migration runs with ``search_path`` set
    to ``<module schema>, pg_catalog, pg_temp``, so an unqualified relation
    resolves in the module schema, then ``pg_catalog``, and a leftover temporary
    object is found last.

    What neither this check nor the runner's catalog fingerprint does: the
    fingerprint detects structural changes to ``public`` and other module schemas
    only. Neither stops a module from reading or writing Core rows through
    statements this lint allows if some name resolves to a Core object. Not
    covered either: ``pg_catalog`` functions outside the listed refusals that read
    or act by text or computed name, on the server or on other sessions;
    unqualified ``pg_catalog`` objects named by allowed statements (``COMMENT ON``,
    ``DROP`` of a catalog object, subject to privileges); database-wide objects
    outside the listed refusals; types, enum values and other objects the
    fingerprint does not read; and any lexer case this scanner gets wrong. The
    enforcing boundary for row access is a database role limited to the module
    schema; this prototype does not create one.
    """
    own_schema = str(schema_name or "").lower()
    foreign = ({"public"} | {name.lower() for name in foreign_schemas}) - {own_schema}
    scanned = _scan_sql(str(sql or ""))
    violations: list[str] = []

    def add(reason: str) -> None:
        if reason not in violations:
            violations.append(reason)

    for construct in scanned.unsupported:
        add(f"uses {construct}")
    for view in (scanned.code, scanned.text):
        for match in _QUALIFIER_RE.finditer(view):
            qualifier = match.group(1).lower()
            if qualifier == own_schema or qualifier in _SHARED_SCHEMAS:
                continue
            if qualifier in foreign or qualifier.startswith(MODULE_SCHEMA_PREFIX):
                add(f"references the schema {qualifier} explicitly ({qualifier}.)")
    for match in _SCHEMA_CLAUSE_RE.finditer(scanned.code):
        if match.group(1).lower() != own_schema:
            add(f"names a schema other than {own_schema} ({match.group(1)})")
    for pattern, reason in _FORBIDDEN_MODULE_SQL:
        if pattern.search(scanned.code):
            add(reason)
    # Literal text too, for names that matter as text: set_config('search_path', ...).
    for pattern, reason in _FORBIDDEN_SETTING_NAMES:
        if pattern.search(scanned.text):
            add(reason)
    for match in _SEQUENCE_CALL_RE.finditer(scanned.text):
        if not _PLAIN_SEQUENCE_ARGUMENT_RE.match(scanned.text, match.end()):
            add(
                "calls nextval/currval with something other than one plain single-quoted "
                "sequence name without a schema"
            )
    statements = _code_statements(scanned.code)
    offenders = [s[:40] for s in statements if _TXN_CONTROL_START_RE.match(s)]
    if offenders:
        add(f"contains explicit transaction control {offenders}")
    for statement in statements:
        for pattern, reason in _FORBIDDEN_MODULE_STATEMENTS:
            if pattern.match(statement):
                add(reason)
                break
    if not parse_transactional(sql):
        add("declares '-- transaction: false'; module migrations always run in a transaction")
    return violations


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


def _row_values(row: Any) -> tuple[Any, ...]:
    return tuple(row.values()) if isinstance(row, Mapping) else tuple(row)


def _existing_schemas(conn) -> dict[str, str | None]:
    """Every schema of the database with its comment (``None`` without one)."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMAS_WITH_COMMENTS_SQL)
        rows = cur.fetchall() or []
    schemas: dict[str, str | None] = {}
    for row in rows:
        values = _row_values(row)
        schemas[str(values[0]).lower()] = None if values[1] is None else str(values[1])
    return schemas


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


def _catalog_fingerprint(
    cur, schemas: Sequence[str], parts: Sequence[tuple[str, str]] = _FINGERPRINT_QUERIES
) -> str:
    """SHA-256 over ``parts`` of the catalog for ``schemas``, in query order."""
    digest = hashlib.sha256()
    names = sorted(schemas)
    digest.update(repr(("schema-set", names)).encode("utf-8"))
    for part, query in parts:
        cur.execute(query, {"schemas": names})
        for row in cur.fetchall() or []:
            digest.update(repr((part, _row_values(row))).encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class _CatalogFingerprint:
    public: str
    other_modules: str


def _module_catalog_fingerprint(cur, own_schema: str) -> _CatalogFingerprint:
    """Fingerprints of ``public`` and of every module schema but ``own_schema``.

    Must run under :data:`_CATALOG_SEARCH_PATH`. Content, not counts: relations
    with kind, owner, privileges and row-level-security flags, columns with type,
    NOT NULL and default, constraints, triggers with enabled state, functions
    with a hash of their source, policies, rules, indexes and sequence settings.
    Both ledgers' structure is included; no table's rows are.

    The module part covers every ``mod_*`` schema other than the module's own,
    plus the name, owner, privileges and owner mark of all of them (the module's
    own included), so a module can neither change another module's schema nor
    create, drop or re-mark a module schema.
    """
    cur.execute(_MODULE_SCHEMA_NAMES_SQL, {"pattern": _MODULE_SCHEMA_LIKE})
    module_schemas = [str(_row_values(row)[0]) for row in cur.fetchall() or []]
    others = [name for name in module_schemas if name != own_schema]
    public = _catalog_fingerprint(cur, ["public"])
    marks = _catalog_fingerprint(cur, module_schemas, _FINGERPRINT_QUERIES[:1])
    content = _catalog_fingerprint(cur, others)
    return _CatalogFingerprint(public=public, other_modules=f"{marks}:{content}")


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
