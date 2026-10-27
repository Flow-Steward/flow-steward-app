"""Canonical DB schema state tracking for install/upgrade workflows."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.infrastructure.config.version import get_app_version

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

# SQLSTATEs that mean "this canonical object already exists" — expected and
# safely skipped when idempotently re-applying the canonical schema to an
# existing database (see reconcile_canonical_schema).
_ALREADY_EXISTS_SQLSTATES: frozenset[str] = frozenset(
    {
        "42P07",  # duplicate_table (also duplicate index/view/sequence name)
        "42710",  # duplicate_object (constraint, trigger, type, etc.)
        "42P06",  # duplicate_schema
        "42701",  # duplicate_column
        "42723",  # duplicate_function
        "42712",  # duplicate_alias
        "42P05",  # duplicate_prepared_statement
        "42P04",  # duplicate_database
        "42P03",  # duplicate_cursor
        "42P16",  # invalid_table_definition — re-adding a PRIMARY KEY that exists
    }
)

# Dollar-quote opener: $$ or $tag$.
_DOLLAR_TAG_RE = re.compile(r"\$(?:[A-Za-z_](?a:\w)*)?\$")

CANONICAL_SCHEMA_STATE_KEY = "canonical"
CANONICAL_SCHEMA_GLOB = "db/schema/*.sql"
CANONICAL_SEED_FILES: tuple[str, ...] = (
    "db/seeds/lookup_seed.json",
    "db/seeds/registry_seed.json",
)


def _resolve_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else REPO_ROOT


def canonical_schema_files(root: Path | None = None) -> list[Path]:
    repo_root = _resolve_root(root)
    return sorted((repo_root / "db" / "schema").glob("*.sql"))


def canonical_state_files(root: Path | None = None) -> list[Path]:
    repo_root = _resolve_root(root)
    schema_files = canonical_schema_files(repo_root)
    seed_files = [repo_root / rel_path for rel_path in CANONICAL_SEED_FILES]
    return [*schema_files, *seed_files]


def read_canonical_schema_sql(root: Path | None = None) -> str:
    schema_files = canonical_schema_files(root)
    if not schema_files:
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing schema files: {CANONICAL_SCHEMA_GLOB}"
        )
    return "".join(path.read_text(encoding="utf-8") for path in schema_files)


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into top-level statements.

    Dollar-quote aware ($$...$$ and $tag$...$tag$) and aware of single/double
    quotes plus ``--`` line and ``/* */`` block comments, so function bodies and
    string literals that contain ``;`` are not mis-split. Used to apply the
    canonical pg_dump-style schema statement-by-statement (it is not wrapped in
    ``IF NOT EXISTS``), so each statement can be tolerated independently when an
    object already exists. Empty/whitespace-only statements are dropped.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    in_single = False
    in_double = False
    in_line_comment = False
    in_block_comment = False
    dollar_tag: str | None = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if dollar_tag is not None:
            if ch == "$" and sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue
        if in_single:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":
                    buf.append(nxt)
                    i += 2
                    continue
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == '"':
                if nxt == '"':
                    buf.append(nxt)
                    i += 2
                    continue
                in_double = False
            i += 1
            continue
        # Not inside any quote/comment context.
        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            match = _DOLLAR_TAG_RE.match(sql, i)
            if match:
                dollar_tag = match.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _sqlstate_of(exc: BaseException) -> str | None:
    state = getattr(exc, "sqlstate", None)
    if state:
        return str(state)
    diag = getattr(exc, "diag", None)
    diag_state = getattr(diag, "sqlstate", None) if diag is not None else None
    return str(diag_state) if diag_state else None


class SchemaReconcileError(RuntimeError):
    """Canonical baseline replay failed on an unexpected SQL error."""


def reconcile_canonical_schema(
    *,
    connect: Callable[[], Any],
    root: Path | None = None,
) -> dict[str, Any]:
    """Replay each canonical schema file in an independent DB session."""
    files = canonical_schema_files(root)
    if not files:
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing schema files: {CANONICAL_SCHEMA_GLOB}"
        )
    applied = 0
    skipped = 0
    statement_count = 0
    for path in files:
        conn = connect()
        previous_autocommit = getattr(conn, "autocommit", None)
        try:
            with contextlib.suppress(Exception):
                conn.autocommit = True
            statements = split_sql_statements(path.read_text(encoding="utf-8"))
            statement_count += len(statements)
            for statement in statements:
                try:
                    with conn.cursor() as cur:
                        cur.execute(statement)
                    applied += 1
                except Exception as exc:
                    sqlstate = _sqlstate_of(exc)
                    if sqlstate in _ALREADY_EXISTS_SQLSTATES:
                        skipped += 1
                        continue
                    raise SchemaReconcileError(
                        "Canonical schema reconcile failed in "
                        f"{path.name}: sqlstate={sqlstate or ''} "
                        f"error={str(exc).strip()} "
                        f"statement={statement[:160]}"
                    ) from exc
        finally:
            if previous_autocommit is not None:
                with contextlib.suppress(Exception):
                    conn.autocommit = previous_autocommit
            try:
                conn.close()
            except Exception:
                logger.debug(
                    "Failed to close schema reconcile connection",
                    exc_info=True,
                )
    return {
        "files": len(files),
        "statements": statement_count,
        "applied": applied,
        "skipped": skipped,
        "failed": [],
    }


def _fingerprint_files(paths: list[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def canonical_schema_fingerprint(root: Path | None = None) -> str:
    repo_root = _resolve_root(root)
    files = canonical_schema_files(repo_root)
    if not files:
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing schema files: {CANONICAL_SCHEMA_GLOB}"
        )
    return _fingerprint_files(files, root=repo_root)


def canonical_seed_fingerprint(root: Path | None = None) -> str:
    repo_root = _resolve_root(root)
    files = [repo_root / relative for relative in CANONICAL_SEED_FILES]
    missing = [path for path in files if not path.is_file()]
    if missing:
        rendered = ", ".join(str(path.relative_to(repo_root)) for path in missing)
        raise FileNotFoundError(
            f"Canonical DB layout is incomplete; missing seed files: {rendered}"
        )
    return _fingerprint_files(files, root=repo_root)


def ensure_schema_state_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.platform_schema_state (
                schema_key text NOT NULL,
                schema_fingerprint text DEFAULT ''::text NOT NULL,
                app_version text DEFAULT ''::text NOT NULL,
                applied_by text DEFAULT 'system'::text NOT NULL,
                metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
                created_at timestamp with time zone DEFAULT now() NOT NULL,
                updated_at timestamp with time zone DEFAULT now() NOT NULL,
                CONSTRAINT platform_schema_state_pkey PRIMARY KEY (schema_key)
            )
            """
        )


def fetch_schema_state(
    conn, *, schema_key: str = CANONICAL_SCHEMA_STATE_KEY
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s)",
            ("public.platform_schema_state",),
        )
        relation = cur.fetchone()
        if not relation or relation[0] is None:
            return None
        cur.execute(
            """
            SELECT schema_key, schema_fingerprint, app_version, applied_by, metadata_jsonb
            FROM public.platform_schema_state
            WHERE schema_key = %s
            """,
            (schema_key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {
        "schema_key": row[0],
        "schema_fingerprint": row[1],
        "app_version": row[2],
        "applied_by": row[3],
        "metadata_jsonb": row[4],
    }


def record_canonical_schema_state(
    conn,
    *,
    root: Path | None = None,
    applied_by: str = "system",
    app_version: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_fingerprint = canonical_schema_fingerprint(root)
    resolved_app_version = str(app_version or get_app_version()).strip()
    resolved_metadata = dict(metadata or {})
    resolved_metadata.setdefault(
        "seed_fingerprint",
        canonical_seed_fingerprint(root),
    )
    payload = json.dumps(resolved_metadata)
    ensure_schema_state_table(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.platform_schema_state (
                schema_key,
                schema_fingerprint,
                app_version,
                applied_by,
                metadata_jsonb
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (schema_key) DO UPDATE
            SET schema_fingerprint = EXCLUDED.schema_fingerprint,
                app_version = EXCLUDED.app_version,
                applied_by = EXCLUDED.applied_by,
                metadata_jsonb = EXCLUDED.metadata_jsonb,
                updated_at = now()
            """,
            (
                CANONICAL_SCHEMA_STATE_KEY,
                expected_fingerprint,
                resolved_app_version,
                str(applied_by or "system").strip() or "system",
                payload,
            ),
        )
    return {
        "schema_key": CANONICAL_SCHEMA_STATE_KEY,
        "schema_fingerprint": expected_fingerprint,
        "app_version": resolved_app_version,
        "applied_by": str(applied_by or "system").strip() or "system",
        "metadata_jsonb": resolved_metadata,
    }


def inspect_canonical_schema_state(
    conn,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    current = fetch_schema_state(conn)
    expected_fingerprint = canonical_schema_fingerprint(root)
    expected_seed_fingerprint = canonical_seed_fingerprint(root)
    current_fingerprint = str((current or {}).get("schema_fingerprint") or "").strip()
    metadata = (current or {}).get("metadata_jsonb") or {}
    if isinstance(metadata, str):
        with contextlib.suppress(json.JSONDecodeError):
            metadata = json.loads(metadata)
    current_seed_fingerprint = str(
        (metadata if isinstance(metadata, dict) else {}).get("seed_fingerprint") or ""
    ).strip()
    schema_needs_reconcile = current_fingerprint != expected_fingerprint
    seed_needs_upgrade = current_seed_fingerprint != expected_seed_fingerprint
    return {
        "current": current,
        "expected_fingerprint": expected_fingerprint,
        "expected_seed_fingerprint": expected_seed_fingerprint,
        "schema_needs_reconcile": schema_needs_reconcile,
        "seed_needs_upgrade": seed_needs_upgrade,
        "needs_upgrade": schema_needs_reconcile or seed_needs_upgrade,
    }
