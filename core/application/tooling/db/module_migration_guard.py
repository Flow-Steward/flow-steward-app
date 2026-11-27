"""Static guard and catalog fingerprint for platform-module migrations (ADR 0082).

:func:`module_migration_violations` lints a module migration before it runs; the
catalog fingerprint taken around each file detects structural changes to
``public`` and to other module schemas. Both are guard rails, not an isolation
boundary; :func:`module_migration_violations` says what they do and do not cover.

:mod:`core.application.tooling.db.migration_runner` applies module migrations and
re-exports the public names.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from core.application.tooling.db.migration_text import (
    _TXN_CONTROL_START_RE,
    _code_statements,
    _scan_sql,
    parse_transactional,
)

#: Prefix of every platform-module schema; the rest is the module id with ``.``
#: mapped to ``_`` and ``-`` mapped to ``__`` (see :func:`module_schema_name`).
MODULE_SCHEMA_PREFIX = "mod_"

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
