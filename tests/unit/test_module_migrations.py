"""Platform-module SQL migrations applied by the host runner (ADR 0082)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.application.tooling.db import migration_runner

MODULE_ID = "acme.billing"
SCHEMA = "mod_acme_billing"

_INIT_SQL = "CREATE TABLE invoices (id bigint PRIMARY KEY, total numeric NOT NULL);\n"
_UPGRADE_SQL = (
    "ALTER TABLE invoices ADD COLUMN currency text NOT NULL DEFAULT 'EUR';\n"
    "CREATE INDEX invoices_currency_idx ON mod_acme_billing.invoices (currency);\n"
)
_CATALOG_PATH = "SET LOCAL search_path TO pg_catalog, pg_temp"
_MODULE_PATH = f"SET LOCAL search_path TO {SCHEMA}, pg_catalog, pg_temp"


# ---------------------------------------------------------------------------
# Fake DB connection (same approach as tests/unit/test_db_migrations.py)
# ---------------------------------------------------------------------------


_FINGERPRINT_PARTS = {query: part for part, query in migration_runner._FINGERPRINT_QUERIES}


def _is_catalog_read(sql: str) -> bool:
    return sql in _FINGERPRINT_PARTS or sql == migration_runner._MODULE_SCHEMA_NAMES_SQL


def _catalog_rows(schema: str) -> dict[str, list[tuple]]:
    """A small, plausible catalog for one schema, keyed by fingerprint part."""
    return {
        "relations": [(schema, "users", "r", "p", "fs", "", False, False, "")],
        "columns": [
            (schema, "users", 1, "id", "bigint", True, "", "", ""),
            (schema, "users", 2, "role", "text", False, "'viewer'::text", "", ""),
        ],
        "constraints": [
            (schema, "users", "users_pkey", "p", "PRIMARY KEY (id)", False, False, True)
        ],
        "triggers": [(schema, "users", "users_touch", "CREATE TRIGGER users_touch ...", "O")],
        "functions": [(schema, "touch", "", "16500", "trigger", "abc", False, "", "fs", "")],
        "indexes": [(schema, "users_pkey", "CREATE UNIQUE INDEX users_pkey ON ...")],
    }


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self._result: list | None = None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        conn = self._conn
        statement = sql.strip()
        conn.executed.append((sql, params))
        if "pg_advisory_lock" in sql:
            conn.locks.append(params[0])
            return
        if "pg_advisory_unlock" in sql:
            conn.unlocks.append(params[0])
            return
        if sql in _FINGERPRINT_PARTS:
            conn.fingerprint_queries += 1
            part = _FINGERPRINT_PARTS[sql]
            if part == "schemas":
                self._result = [
                    (name, "fs", "", conn.schema_names.get(name) or "")
                    for name in params["schemas"]
                ]
            else:
                self._result = [
                    row
                    for name in params["schemas"]
                    for row in conn.catalog.get(name, {}).get(part, [])
                ]
            return
        if sql == migration_runner._MODULE_SCHEMA_NAMES_SQL:
            conn.fingerprint_reads += 1
            assert params == {"pattern": "mod\\_%"}
            self._result = [
                (name,) for name in sorted(conn.schema_names) if name.startswith("mod_")
            ]
            return
        if sql == migration_runner._SCHEMAS_WITH_COMMENTS_SQL:
            self._result = list(conn.schema_names.items())
            return
        if "platform_schema_migrations" in sql:
            conn.core_ledger_statements.append(sql)
            return
        if "CREATE TABLE IF NOT EXISTS public.platform_module_migrations" in sql:
            conn.ledger_created = True
            return
        if "to_regclass('public.platform_module_migrations')" in sql:
            exists = conn.ledger_created or bool(conn.committed)
            self._result = [("public.platform_module_migrations" if exists else None,)]
            return
        if statement.upper().startswith("SELECT MODULE_ID"):
            self._result = [
                (module_id, migration_id, checksum)
                for (module_id, migration_id), (checksum, _version) in conn.committed.items()
            ]
            return
        if "INSERT INTO public.platform_module_migrations" in sql:
            module_id, migration_id, checksum, version = params
            target = conn.committed if conn.autocommit else conn.staged
            target[(module_id, migration_id)] = (checksum, version)
            return
        if statement.startswith("CREATE SCHEMA "):
            name = statement.rsplit(" ", 1)[-1]
            conn.schemas.append(name)
            conn.schema_names[name] = None
            return
        if statement.startswith("COMMENT ON SCHEMA "):
            name, _is, comment = statement[len("COMMENT ON SCHEMA ") :].split(" ", 2)
            conn.schema_names[name] = comment.strip("'")
            return
        if "search_path" in sql:
            conn.search_path.append(statement)
            return
        if statement.startswith(("SET LOCAL standard_conforming_strings", "SET CONSTRAINTS")):
            conn.settings.append(statement)
            return
        conn.bodies.append((sql, conn.autocommit))
        if conn.on_body is not None:
            conn.on_body(conn, sql)
        if conn.fail_on is not None and conn.fail_on in sql:
            raise RuntimeError("boom: module migration body failed")

    def fetchall(self):
        return self._result

    def fetchone(self):
        rows = self._result or []
        return rows[0] if rows else None


class _FakeConn:
    def __init__(
        self,
        *,
        applied: dict | None = None,
        fail_on: str | None = None,
        on_body=None,
    ) -> None:
        self.autocommit = False
        self.committed: dict[tuple[str, str], tuple[str, str]] = dict(applied or {})
        self.staged: dict[tuple[str, str], tuple[str, str]] = {}
        self.executed: list = []
        self.bodies: list = []
        self.search_path: list[str] = []
        self.settings: list[str] = []
        self.schemas: list[str] = []
        self.core_ledger_statements: list[str] = []
        self.locks: list = []
        self.unlocks: list = []
        self.ledger_created = False
        self.fail_on = fail_on
        self.on_body = on_body
        #: Schema name -> COMMENT ON SCHEMA text (None without one).
        self.schema_names: dict[str, str | None] = {
            "public": "standard public schema",
            "pg_catalog": None,
            "information_schema": None,
            "ag_catalog": None,
            "mod_acme_other": migration_runner.module_schema_owner_marker("acme.other"),
        }
        self.catalog: dict[str, dict[str, list[tuple]]] = {
            "public": _catalog_rows("public"),
            "mod_acme_other": _catalog_rows("mod_acme_other"),
        }
        self.fingerprint_reads = 0
        self.fingerprint_queries = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.executed.append(("<COMMIT>", None))
        self.committed.update(self.staged)
        self.staged = {}

    def rollback(self) -> None:
        self.rollbacks += 1
        self.executed.append(("<ROLLBACK>", None))
        self.staged = {}


def _write(directory: Path, filename: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def _apply(conn: _FakeConn, directory: Path, *, version: str = "1.0.0", **kwargs):
    return migration_runner.apply_module_migrations(
        conn,
        module_id=MODULE_ID,
        module_version=version,
        migrations_dir=directory,
        **kwargs,
    )


def _mutating_statements(conn: _FakeConn) -> list[str]:
    return [
        sql
        for sql, _params in conn.executed
        if not sql.strip().upper().startswith("SELECT") and not sql.startswith("<")
    ]


# ---------------------------------------------------------------------------
# Module id validation and schema-name derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_id", "schema"),
    [
        ("acme.billing", "mod_acme_billing"),
        ("com.acme.audit-log", "mod_com_acme_audit__log"),
        ("a1.b2-c3.d4", "mod_a1_b2__c3_d4"),
        ("acme.a--b", "mod_acme_a____b"),
    ],
)
def test_module_schema_name_maps_dots_and_hyphens(module_id, schema):
    assert migration_runner.module_schema_name(module_id) == schema


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("a.b.c", "a.b-c"),
        ("acme.audit-log", "acme.audit.log"),
        ("acme.a--b", "acme.a.b.b"),
        ("acme.a-b.c", "acme.a.b-c"),
    ],
)
def test_module_schema_name_is_injective(first, second):
    assert migration_runner.module_schema_name(first) != migration_runner.module_schema_name(second)


@pytest.mark.parametrize(
    "module_id",
    [
        "",
        "billing",  # ADR 0013 ids have at least two dot-separated labels
        "Acme.billing",
        "acme..billing",
        "acme.billing.",
        "acme.-billing",
        "acme_x.billing",
        " acme.billing",
        "acme.billing ",
        "acme.billing;drop",
    ],
)
def test_module_schema_name_rejects_non_canonical_ids(module_id):
    with pytest.raises(migration_runner.ModuleMigrationError):
        migration_runner.module_schema_name(module_id)


def test_module_schema_name_enforces_the_postgres_identifier_limit():
    fits = "a" * 29 + "." + "b" * 29  # mod_ + 59 = 63 bytes
    assert len(migration_runner.module_schema_name(fits)) == 63

    with pytest.raises(migration_runner.ModuleMigrationError, match="63-byte"):
        migration_runner.module_schema_name("a" * 30 + "." + "b" * 30)


# ---------------------------------------------------------------------------
# Static guard (pure)
# ---------------------------------------------------------------------------


def _violations(sql: str, foreign=("ag_catalog",)) -> list[str]:
    return migration_runner.module_migration_violations(
        sql, schema_name=SCHEMA, foreign_schemas=foreign
    )


@pytest.mark.parametrize(
    "sql",
    [
        _INIT_SQL,
        _UPGRADE_SQL,
        "CREATE TABLE mod_acme_billing.invoice_lines (invoice_id bigint, amount numeric);",
        "SELECT i.total, l.amount FROM invoices AS i JOIN invoice_lines AS l ON l.id = i.id;",
        "SELECT pg_catalog.now(), information_schema.tables.table_name "
        "FROM information_schema.tables;",
        "-- public.users belongs to Core and is only mentioned here\nCREATE TABLE notes (id int);",
        "/* SET search_path TO public */ CREATE TABLE tags_local (id int);",
        "/* nested /* comment */ still a comment; COMMIT; */ CREATE TABLE t2 (id int);",
        "INSERT INTO invoices (id, total) VALUES (1, 0) ON CONFLICT DO NOTHING;",
        "INSERT INTO notes (body) VALUES ('grant, execute, copy; commit; set role x');",
        "INSERT INTO notes (body) VALUES (E'it\\'s; COMMIT; fine'), ('a\\');",
        "CREATE TABLE prices (amount numeric DEFAULT 0, label text DEFAULT $$x$$);",
    ],
)
def test_guard_accepts_own_schema_ddl(sql):
    assert _violations(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        # Core table names as module names are fine: they resolve in the module schema.
        "CREATE TABLE users (id bigint PRIMARY KEY, status text, accounts int, jobs int);",
        "CREATE TABLE seats (users int, projects int, roles text[]);",
        "-- transaction: true\nCREATE TABLE mod_users (id int);",
    ],
)
def test_guard_accepts_core_table_names_without_qualification(sql):
    assert _violations(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE public.invoices (id int);",
        'INSERT INTO "public".invoices VALUES (1);',
        "SELECT * FROM PUBLIC . invoices;",
        "SELECT '1'::public.vector;",
        "DO $$ BEGIN EXECUTE 'DELETE FROM public.invoices'; END $$;",
        "SELECT 1 OPERATOR(public.===) 1;",
        'SELECT * FROM "public"."users";',
    ],
)
def test_guard_rejects_explicit_public_references(sql):
    assert any("schema public" in reason for reason in _violations(sql))


@pytest.mark.parametrize(
    ("sql", "schema"),
    [
        ("CREATE TABLE mod_other_module.secrets (id int);", "mod_other_module"),
        ('SELECT * FROM "mod_acme_other".users;', "mod_acme_other"),
        ("SELECT * FROM ag_catalog.ag_graph;", "ag_catalog"),
        ('SELECT * FROM "ag_catalog".ag_graph;', "ag_catalog"),
    ],
)
def test_guard_rejects_cross_schema_references(sql, schema):
    assert any(f"schema {schema}" in reason for reason in _violations(sql))


def test_guard_uses_the_schemas_the_runner_found():
    sql = "SELECT * FROM analytics.events;"
    assert _violations(sql, foreign=()) == []
    assert _violations(sql, foreign=("analytics",))


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE SCHEMA analytics;",
        "ALTER SCHEMA public OWNER TO module_role;",
        "COMMENT ON SCHEMA public IS 'x';",
        "ALTER TABLE invoices SET SCHEMA public;",
    ],
)
def test_guard_rejects_schema_clauses_for_other_schemas(sql):
    assert any("schema other than" in reason for reason in _violations(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "SET search_path TO public;",
        "SET LOCAL search_path = mod_acme_billing, public;",
        "SELECT set_config('search_path', 'public', true);",
        "ALTER FUNCTION f() SET search_path = public;",
    ],
)
def test_guard_rejects_search_path_changes(sql):
    assert "changes search_path" in _violations(sql)


@pytest.mark.parametrize("sql", ["RESET ALL;", "DISCARD ALL;"])
def test_guard_rejects_resetting_session_settings(sql):
    assert _violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
        "create extension hstore;",
        "ALTER EXTENSION vector UPDATE;",
        "DROP EXTENSION vector;",
    ],
)
def test_guard_rejects_extension_changes(sql):
    assert any("extension" in reason for reason in _violations(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER ROLE flow_steward SUPERUSER;",
        "ALTER USER flow_steward WITH PASSWORD 'x';",
        "ALTER SYSTEM SET max_connections = 1000;",
        "ALTER DATABASE flow SET timezone = 'UTC';",
        "CREATE ROLE module_admin;",
        "SET ROLE postgres;",
        "SET SESSION AUTHORIZATION postgres;",
        "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO module_role;",
        "CREATE EVENT TRIGGER watch ON ddl_command_start EXECUTE FUNCTION f();",
    ],
)
def test_guard_rejects_role_database_and_system_changes(sql):
    assert _violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "GRANT SELECT ON invoices TO fs_reporting;",
        "GRANT SELECT ON mod_acme_billing.invoices TO fs_reporting;",
        "REVOKE ALL ON invoices FROM fs_reporting;",
        "GRANT fs_admin TO module_role;",
        "GRANT ALL ON DATABASE flow TO module_role;",
    ],
)
def test_guard_rejects_grant_and_revoke(sql):
    assert "grants or revokes privileges" in _violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP SCHEMA mod_acme_billing CASCADE;",
        "DROP SCHEMA IF EXISTS mod_other CASCADE;",
    ],
)
def test_guard_rejects_drop_schema(sql):
    assert "drops a schema" in _violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "BEGIN;\nCREATE TABLE invoices (id int);\nCOMMIT;",
        "CREATE TABLE invoices (id int);\nCOMMIT;",
        "START TRANSACTION;\nSELECT 1;",
        "SAVEPOINT sp1;",
    ],
)
def test_guard_rejects_explicit_transaction_control(sql):
    assert any("transaction control" in reason for reason in _violations(sql))


def test_guard_rejects_non_transactional_module_migrations():
    sql = "-- transaction: false\nCREATE INDEX CONCURRENTLY idx ON invoices (total);"
    assert any("transaction: false" in reason for reason in _violations(sql))


# Bypasses an independent review proved against the first guard. Each must now be
# refused; the strings are the reviewer's.
_REVIEW_BYPASSES = {
    "escape-string": ("SELECT E'\\' -- '; COMMIT; ALTER TABLE public.users ADD COLUMN x int; -- '"),
    "unicode-identifier": r'UPDATE U&"\0070ublic".users SET ...',
    "unicode-search-path": r'SET LOCAL U&"search\005Fpath" TO public',
    "set-config-concat": ("SELECT set_config('search'||'_path','public',true); UPDATE users ..."),
    "copy-program": "COPY ... FROM PROGRAM",
    "copy-program-concrete": "COPY invoices FROM PROGRAM 'cat /etc/passwd';",
}


@pytest.mark.parametrize("sql", list(_REVIEW_BYPASSES.values()), ids=list(_REVIEW_BYPASSES))
def test_guard_refuses_the_review_bypasses(sql):
    assert _violations(sql)


def test_escape_string_bypass_exposes_the_hidden_statements():
    violations = _violations(_REVIEW_BYPASSES["escape-string"])

    assert "references the schema public explicitly (public.)" in violations
    assert any("transaction control" in reason for reason in violations)
    assert migration_runner.find_explicit_transaction_control(_REVIEW_BYPASSES["escape-string"])


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (_REVIEW_BYPASSES["unicode-identifier"], "Unicode-escape"),
        (_REVIEW_BYPASSES["unicode-search-path"], "Unicode-escape"),
        ("SELECT U&'d\\0061t\\+000061';", "Unicode-escape"),
        (_REVIEW_BYPASSES["set-config-concat"], "set_config"),
        ("SELECT pg_catalog.set_config('a.b', 'c', false);", "set_config"),
        ("SELECT \"set_config\"('a.b', 'c', false);", "set_config"),
        (_REVIEW_BYPASSES["copy-program"], "COPY"),
        ("COPY invoices TO '/tmp/invoices.csv';", "COPY"),
        ("copy invoices from stdin;", "COPY"),
    ],
)
def test_review_bypasses_are_refused_for_the_right_reason(sql, reason):
    assert any(reason in violation for violation in _violations(sql))


@pytest.mark.parametrize(
    "sql",
    [
        # A $ inside an identifier does not open a dollar quote.
        "SELECT a$$ ; COMMIT; ALTER TABLE public.users ADD x int; $$",
        # Block comments nest: the first */ does not end the comment.
        "/* /* */ '  */ COMMIT; ALTER TABLE public.users ADD x int; -- '",
        # A line comment ends at a carriage return too.
        "-- note\rCOMMIT; ALTER TABLE public.users ADD x int;\n",
        # 'a'<newline>'b' continues an escape string in escape mode.
        "SELECT E'a'\n'\\' ' ; COMMIT; -- '",
        "SELECT 'unterminated",
    ],
)
def test_lexer_follows_postgres_where_a_mismatch_would_hide_statements(sql):
    assert _violations(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE t SET role = 'admin' WHERE id = 1;",
        "UPDATE seats SET role = 'viewer', session = 2;",
        "ALTER TABLE seats ALTER COLUMN role SET DEFAULT 'viewer';",
        "CREATE TABLE grants_log (role text, execute_at timestamptz);",
    ],
)
def test_guard_accepts_role_as_a_column_name(sql):
    assert _violations(sql) == []


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SET ROLE postgres;", "switches the session role"),
        ("SET SESSION ROLE postgres;", "switches the session role"),
        ("SET LOCAL ROLE postgres;", "switches the session role"),
        ("RESET ROLE;", "switches the session role"),
        ("SET SESSION AUTHORIZATION postgres;", "switches the session role"),
        ("SET LOCAL standard_conforming_strings = off;", "standard_conforming_strings"),
        ("SELECT set_config('standard_conforming_strings', 'off', true);", "set_config"),
        ("SET statement_timeout = 0;", "SET/RESET"),
        ("SET CONSTRAINTS ALL DEFERRED;", "SET/RESET"),
        ("SET SCHEMA 'public';", "SET SCHEMA"),
        ("DO $$ BEGIN PERFORM 1; END $$;", "DO block"),
        (
            "CREATE FUNCTION touch() RETURNS trigger AS $$\nBEGIN\n  RETURN NEW;\nEND;\n"
            "$$ LANGUAGE plpgsql;",
            "function or procedure",
        ),
        ("CREATE OR REPLACE PROCEDURE p() LANGUAGE sql AS 'SELECT 1';", "function or procedure"),
        ("ALTER FUNCTION now() RENAME TO later;", "function or procedure"),
        (
            "CREATE TRIGGER t BEFORE INSERT ON invoices FOR EACH ROW EXECUTE FUNCTION f();",
            "creates a trigger",
        ),
        (
            "CREATE CONSTRAINT TRIGGER t AFTER INSERT ON invoices DEFERRABLE INITIALLY "
            "DEFERRED FOR EACH ROW EXECUTE FUNCTION f();",
            "creates a trigger",
        ),
        ("CREATE EVENT TRIGGER e ON ddl_command_end EXECUTE FUNCTION f();", "event trigger"),
        ("CREATE RULE r AS ON INSERT TO invoices DO INSTEAD NOTHING;", "creates a rule"),
        ("EXECUTE stmt;", "EXECUTE"),
        ("PREPARE stmt AS SELECT 1;", "PREPARE"),
        ("CALL do_something();", "CALL"),
        ("LISTEN jobs;", "LISTEN/NOTIFY"),
        ("NOTIFY jobs, 'x';", "LISTEN/NOTIFY"),
        ("UNLISTEN *;", "LISTEN/NOTIFY"),
        ("SELECT pg_notify('jobs', 'x');", "pg_notify"),
        ("LOCK TABLE invoices IN ACCESS EXCLUSIVE MODE;", "LOCK"),
        ("VACUUM invoices;", "VACUUM"),
        ("CLUSTER invoices USING invoices_pkey;", "VACUUM"),
        ("REINDEX TABLE invoices;", "VACUUM"),
        ("LOAD 'plugin';", "LOAD"),
        ("ALTER TABLE invoices SET SCHEMA mod_acme_billing;", "SET SCHEMA"),
        ("ALTER TABLE invoices OWNER TO postgres;", "OWNER TO"),
        ("COMMENT ON SCHEMA mod_acme_billing IS 'flow-steward-module:acme.other';", "schema"),
        ("ALTER SCHEMA mod_acme_billing RENAME TO mod_x;", "schema"),
        ("CREATE SCHEMA mod_acme_billing;", "schema"),
        (
            "ALTER TABLE invoices ADD COLUMN f int; CREATE FUNCTION f() RETURNS int "
            "SECURITY DEFINER LANGUAGE sql AS 'SELECT 1';",
            "SECURITY DEFINER",
        ),
        ("SELECT lo_import('/etc/passwd');", "large-object"),
        ("SELECT lo_unlink(42);", "large-object"),
        ("SELECT loread(0, 10);", "large-object"),
        ("SELECT pg_read_file('/etc/passwd');", "server file"),
        ("SELECT pg_read_binary_file('/etc/passwd');", "server file"),
        ("SELECT pg_ls_dir('.');", "server file"),
        ("SELECT pg_write_file('x', 'y');", "server file"),
        ("SELECT * FROM dblink('host=x', 'SELECT 1') AS t(a int);", "dblink"),
        ("SELECT dblink_exec('UPDATE users SET x = 1');", "dblink"),
        ("CREATE CAST (text AS int) WITH INOUT;", "database-wide"),
        ("CREATE PUBLICATION p FOR ALL TABLES;", "database-wide"),
        ("CREATE SERVER s FOREIGN DATA WRAPPER w;", "database-wide"),
    ],
)
def test_guard_refuses_procedural_dynamic_and_server_constructs(sql, reason):
    violations = _violations(sql)
    assert any(reason in violation for violation in violations), violations


def test_refusal_message_says_what_module_migrations_may_contain(tmp_path):
    _write(tmp_path, "20261201000000_do.sql", "DO $$ BEGIN PERFORM 1; END $$;\n")

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="plain DDL and DML"):
        _apply(_FakeConn(), tmp_path)


# Second review: temporary objects and names resolved from text at run time.
_SECOND_REVIEW_REFUSALS = {
    "query-to-xml": (
        "SELECT pg_catalog.query_to_xml('select ...',true,false,'')",
        "*_to_xml",
    ),
    "schema-to-xml": ("schema_to_xml('public',...)", "*_to_xml"),
    "table-to-xml": ("table_to_xml(format('%I.users','public')::regclass,...)", "*_to_xml"),
    "setval-computed": ("SELECT setval(('pub'||'lic.users_id_seq')::regclass,1)", "setval"),
}


@pytest.mark.parametrize(
    ("sql", "reason"), list(_SECOND_REVIEW_REFUSALS.values()), ids=list(_SECOND_REVIEW_REFUSALS)
)
def test_guard_refuses_the_second_review_bypasses(sql, reason):
    violations = _violations(sql)
    assert any(reason in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("CREATE TEMP TABLE notes (id int);", "temporary object"),
        ("CREATE TEMPORARY TABLE notes (id int);", "temporary object"),
        ("CREATE GLOBAL TEMPORARY TABLE notes (id int);", "temporary object"),
        ("CREATE TEMP VIEW v AS SELECT 1;", "temporary object"),
        ("CREATE TEMP SEQUENCE s;", "temporary object"),
        ("SELECT * INTO TEMP copy_of_notes FROM notes;", "temporary object"),
        ("CREATE TABLE t (id int) ON COMMIT DELETE ROWS;", "ON COMMIT"),
        ("DISCARD TEMP;", "DISCARD"),
        ("DISCARD PLANS;", "DISCARD"),
        ("INSERT INTO pg_temp.notes VALUES (1);", "pg_temp"),
        ('INSERT INTO "pg_temp".notes VALUES (1);', "pg_temp"),
        ("SELECT * FROM PG_TEMP_3.notes;", "pg_temp"),
        ("SELECT Query_To_Xml('select 1', true, false, '');", "*_to_xml"),
        ("SELECT cursor_to_xml(c, 1, false, false, '');", "*_to_xml"),
        ("SELECT DATABASE_TO_XML(true, false, '');", "*_to_xml"),
        ("SELECT table_to_xmlschema('users', true, false, '');", "*_to_xml"),
        ("SELECT setval('notes_id_seq', 1);", "setval"),
        ("SELECT pg_catalog.\"setval\"('notes_id_seq', 1);", "setval"),
        ("SELECT 'users'::regclass;", "reg*"),
        ("SELECT 'users'::REGCLASS;", "reg*"),
        ("SELECT CAST('users' AS regclass);", "reg*"),
        ("SELECT 'now'::regproc, 'now()'::regprocedure, 'int4'::regtype;", "reg*"),
        ("SELECT 'public'::regnamespace, 'postgres'::regrole;", "reg*"),
        ("SELECT pg_catalog.to_regclass('users');", "to_reg*"),
        ("SELECT to_regproc('now');", "to_reg*"),
        ("SELECT nextval('public.users_id_seq');", "nextval/currval"),
        ("SELECT nextval('pub' || 'lic.users_id_seq');", "nextval/currval"),
        ("SELECT currval(format('%s', 'users_id_seq'));", "nextval/currval"),
        ("SELECT NEXTVAL(E'users_id_seq');", "nextval/currval"),
        ("ALTER EVENT TRIGGER e DISABLE;", "event trigger"),
        ("DROP EVENT TRIGGER e;", "event trigger"),
        ("CREATE ACCESS METHOD am TYPE INDEX HANDLER h;", "access method"),
        ("DROP OWNED BY someone;", "OWNED"),
        ("REASSIGN OWNED BY a TO b;", "OWNED"),
        ("CREATE INDEX CONCURRENTLY i ON notes (id);", "cannot run inside the migration"),
        ("REINDEX INDEX CONCURRENTLY i;", "cannot run inside the migration"),
        ("DROP INDEX CONCURRENTLY i;", "cannot run inside the migration"),
    ],
)
def test_guard_refuses_temporary_objects_and_names_resolved_at_run_time(sql, reason):
    violations = _violations(sql)
    assert any(reason in violation for violation in violations), violations


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE notes (id bigint DEFAULT nextval('notes_id_seq'), body text);",
        "SELECT currval( 'notes_id_seq' );",
        "CREATE TABLE readings (temp numeric, temporary_note text, committed_on date);",
    ],
)
def test_guard_accepts_plain_sequence_calls_and_temp_as_a_column_name(sql):
    assert _violations(sql) == []


def test_fingerprint_sql_does_not_use_md5():
    constants = [query for _part, query in migration_runner._FINGERPRINT_QUERIES]
    constants += [
        migration_runner._MODULE_SCHEMA_NAMES_SQL,
        migration_runner._SCHEMAS_WITH_COMMENTS_SQL,
    ]
    assert all("md5(" not in query.lower() for query in constants)
    hashed = [query for query in constants if "sha256(" in query]
    assert len(hashed) == 2
    assert all("pg_catalog.convert_to(" in query for query in hashed)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def test_first_run_creates_ledger_and_schema_and_applies_in_the_module_schema(tmp_path):
    path = _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()

    result = _apply(conn, tmp_path)

    checksum = migration_runner.migration_checksum(path.read_text(encoding="utf-8"))
    assert result == {
        "module_id": MODULE_ID,
        "module_version": "1.0.0",
        "schema": SCHEMA,
        "total": 1,
        "applied": ["20261201000000_init.sql"],
        "skipped": [],
    }
    assert conn.ledger_created is True
    assert conn.schemas == [SCHEMA]
    # Catalog reads run on a fixed path; the body runs in the module schema.
    assert conn.search_path == [_CATALOG_PATH, _MODULE_PATH, _CATALOG_PATH]
    assert conn.fingerprint_reads == 2
    assert [(body, autocommit) for body, autocommit in conn.bodies] == [(_INIT_SQL, False)]
    assert conn.committed == {(MODULE_ID, "20261201000000_init.sql"): (checksum, "1.0.0")}
    assert conn.commits == 2  # the schema creation and the file
    assert conn.locks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_module_migrations_never_touch_the_core_ledger(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()

    _apply(conn, tmp_path)
    migration_runner.apply_platform_module_migrations(
        conn, [(MODULE_ID, "1.0.0", tmp_path)], acquire_lock=False
    )
    migration_runner.inspect_module_migrations(conn, [(MODULE_ID, "1.0.0", tmp_path)])

    assert conn.core_ledger_statements == []


def test_second_run_is_a_no_op(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    _apply(conn, tmp_path)
    bodies_after_first = list(conn.bodies)

    second = _apply(conn, tmp_path)

    assert second["applied"] == []
    assert second["skipped"] == ["20261201000000_init.sql"]
    assert conn.bodies == bodies_after_first
    assert conn.commits == 2  # the schema creation and the file


def test_added_file_applies_as_an_upgrade_from_the_previous_version(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    _apply(conn, tmp_path, version="1.0.0")

    _write(tmp_path, "20261215000000_currency.sql", _UPGRADE_SQL)
    upgrade = _apply(conn, tmp_path, version="1.1.0")

    assert upgrade["applied"] == ["20261215000000_currency.sql"]
    assert upgrade["skipped"] == ["20261201000000_init.sql"]
    assert [body for body, _ in conn.bodies] == [_INIT_SQL, _UPGRADE_SQL]
    versions = {
        migration_id: version for (_m, migration_id), (_c, version) in conn.committed.items()
    }
    assert versions == {
        "20261201000000_init.sql": "1.0.0",
        "20261215000000_currency.sql": "1.1.0",
    }


def test_module_column_named_like_a_core_table_applies(tmp_path):
    body = "CREATE TABLE seats (id bigint, users int, status text);\n"
    _write(tmp_path, "20261201000000_seats.sql", body)
    conn = _FakeConn()

    result = _apply(conn, tmp_path)

    assert result["applied"] == ["20261201000000_seats.sql"]


def test_checksum_drift_of_an_applied_module_migration_raises(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(applied={(MODULE_ID, "20261201000000_init.sql"): ("stale", "1.0.0")})
    conn.schema_names[SCHEMA] = migration_runner.module_schema_owner_marker(MODULE_ID)

    with pytest.raises(migration_runner.MigrationChecksumError):
        _apply(conn, tmp_path)

    assert conn.bodies == []


def test_guard_refuses_the_whole_upgrade_before_any_file_runs(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    _write(tmp_path, "20261202000000_reach.sql", "DELETE FROM public.users;\n")
    conn = _FakeConn()

    with pytest.raises(migration_runner.ModuleMigrationGuardError) as error:
        _apply(conn, tmp_path)

    assert "20261202000000_reach.sql" in str(error.value)
    assert conn.bodies == []
    assert conn.committed == {}
    assert conn.schemas == []
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_cross_schema_reference_to_an_existing_schema_is_refused(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", "SELECT * FROM ag_catalog.ag_graph;\n")
    conn = _FakeConn()

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="ag_catalog"):
        _apply(conn, tmp_path)

    assert conn.bodies == []


def test_explicit_transaction_control_is_refused_before_execution(tmp_path):
    body = "BEGIN;\nCREATE TABLE invoices (id int);\nCOMMIT;\n"
    _write(tmp_path, "20261201000000_init.sql", body)
    conn = _FakeConn()

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="transaction control"):
        _apply(conn, tmp_path)

    assert conn.bodies == []


def _add_public_relation(conn: _FakeConn, _sql: str) -> None:
    # What a statement slipping past the static guard would do.
    conn.catalog["public"]["relations"].append(
        ("public", "module_leak", "r", "p", "fs", "", False, False, "")
    )


def test_public_catalog_change_rolls_the_migration_back(tmp_path):
    body = "CREATE TABLE module_leak (id int);\n"
    _write(tmp_path, "20261201000000_leak.sql", body)
    conn = _FakeConn(on_body=_add_public_relation)

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="public schema"):
        _apply(conn, tmp_path)

    assert len(conn.bodies) == 1  # the static guard passed it; the body ran
    assert conn.rollbacks == 1
    assert conn.commits == 1  # only the schema creation
    assert conn.committed == {}


def _index(executed: list, predicate, *, start: int = 0) -> int:
    for index in range(start, len(executed)):
        if predicate(executed[index][0]):
            return index
    raise AssertionError("statement not executed")


def test_after_fingerprint_is_the_last_statement_before_commit(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()

    _apply(conn, tmp_path)

    executed = conn.executed
    body = _index(executed, lambda sql: sql == _INIT_SQL)
    scs = _index(executed, lambda sql: sql.startswith("SET LOCAL standard_conforming_strings"))
    insert = _index(executed, lambda sql: "INSERT INTO public.platform_module_migrations" in sql)
    immediate = _index(executed, lambda sql: sql == "SET CONSTRAINTS ALL IMMEDIATE")
    after = _index(executed, _is_catalog_read, start=body)
    commit = _index(executed, lambda sql: sql == "<COMMIT>", start=body)
    before = _index(executed, _is_catalog_read)

    assert before < scs < body
    assert executed[scs][0] == "SET LOCAL standard_conforming_strings TO on"
    # Deferred work is forced to run, the ledger row is written, and only then is
    # the catalog read; nothing but catalog reads sits between it and COMMIT.
    assert body < insert < immediate < after < commit
    assert all(_is_catalog_read(sql) for sql, _params in executed[after:commit])
    assert executed[commit - 1][0] in _FINGERPRINT_PARTS


def _change_public_constraint(conn: _FakeConn, _sql: str) -> None:
    # Drop one constraint and add another: the count stays the same.
    conn.catalog["public"]["constraints"] = [
        ("public", "users", "users_pkey", "p", "PRIMARY KEY (id, role)", False, False, True)
    ]


def _disable_public_trigger(conn: _FakeConn, _sql: str) -> None:
    name, table, trigger, definition, _enabled = conn.catalog["public"]["triggers"][0]
    conn.catalog["public"]["triggers"] = [(name, table, trigger, definition, "D")]


def _change_public_default(conn: _FakeConn, _sql: str) -> None:
    conn.catalog["public"]["columns"][1] = (
        "public",
        "users",
        2,
        "role",
        "text",
        False,
        "'admin'::text",
        "",
        "",
    )


def _force_public_rls_off(conn: _FakeConn, _sql: str) -> None:
    row = list(conn.catalog["public"]["relations"][0])
    row[7] = True
    conn.catalog["public"]["relations"][0] = tuple(row)


@pytest.mark.parametrize(
    "change",
    [
        _change_public_constraint,
        _disable_public_trigger,
        _change_public_default,
        _force_public_rls_off,
    ],
)
def test_public_change_with_the_same_object_counts_is_detected(tmp_path, change):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(on_body=change)

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="public schema"):
        _apply(conn, tmp_path)

    assert conn.rollbacks == 1
    assert conn.committed == {}


def _change_other_module(conn: _FakeConn, _sql: str) -> None:
    conn.catalog["mod_acme_other"]["columns"].append(
        ("mod_acme_other", "users", 3, "leak", "text", False, "", "", "")
    )


def _plant_module_schema(conn: _FakeConn, _sql: str) -> None:
    conn.schema_names["mod_acme_planted"] = None


def _remark_own_schema(conn: _FakeConn, _sql: str) -> None:
    conn.schema_names[SCHEMA] = migration_runner.module_schema_owner_marker("acme.other")


@pytest.mark.parametrize("change", [_change_other_module, _plant_module_schema, _remark_own_schema])
def test_change_to_another_module_schema_is_detected(tmp_path, change):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(on_body=change)

    with pytest.raises(migration_runner.ModuleMigrationGuardError, match="another module"):
        _apply(conn, tmp_path)

    assert conn.commits == 1  # only the schema creation
    assert conn.committed == {}


def test_own_schema_changes_are_not_flagged(tmp_path):
    def change_own(conn: _FakeConn, _sql: str) -> None:
        conn.catalog.setdefault(SCHEMA, {})["relations"] = [
            (SCHEMA, "invoices", "r", "p", "fs", "", False, False, "")
        ]

    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(on_body=change_own)

    assert _apply(conn, tmp_path)["applied"] == ["20261201000000_init.sql"]


def test_module_body_runs_with_pg_temp_named_last(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()

    _apply(conn, tmp_path)

    body = _index(conn.executed, lambda sql: sql == _INIT_SQL)
    path = _index(conn.executed, lambda sql: sql == _MODULE_PATH)
    assert path < body
    assert all("search_path" not in sql for sql, _params in conn.executed[path + 1 : body])
    assert _MODULE_PATH.endswith(", pg_catalog, pg_temp")
    # Catalog reads before and after keep their own path.
    assert conn.search_path == [_CATALOG_PATH, _MODULE_PATH, _CATALOG_PATH]


# ---------------------------------------------------------------------------
# Schema ownership
# ---------------------------------------------------------------------------


def test_runner_marks_the_schema_it_creates(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()

    _apply(conn, tmp_path)

    assert conn.schemas == [SCHEMA]
    assert conn.schema_names[SCHEMA] == f"flow-steward-module:{MODULE_ID}"
    create = _index(conn.executed, lambda sql: sql == f"CREATE SCHEMA {SCHEMA}")
    assert conn.executed[create + 1][0] == (
        f"COMMENT ON SCHEMA {SCHEMA} IS 'flow-steward-module:{MODULE_ID}'"
    )
    assert conn.executed[create + 2][0] == "<COMMIT>"


def test_marked_schema_is_reused_without_creating_it_again(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    conn.schema_names[SCHEMA] = migration_runner.module_schema_owner_marker(MODULE_ID)

    result = _apply(conn, tmp_path)

    assert result["applied"] == ["20261201000000_init.sql"]
    assert conn.schemas == []


@pytest.mark.parametrize(
    "comment",
    [None, "created by an operator", "flow-steward-module:acme.other"],
)
def test_existing_schema_without_this_modules_mark_is_refused(tmp_path, comment):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    conn.schema_names[SCHEMA] = comment

    with pytest.raises(migration_runner.ModuleSchemaOwnershipError, match=SCHEMA):
        _apply(conn, tmp_path)

    assert conn.bodies == []
    assert conn.schemas == []
    assert conn.committed == {}


def test_module_cannot_adopt_an_orphaned_schema_of_another_module(tmp_path):
    # acme.audit-log ran under the old mapping (mod_acme_audit_log), was removed,
    # and left its schema and ledger rows. acme.audit.log maps to that name now.
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(applied={("acme.audit-log", "20261101000000_init.sql"): ("abc", "0.9.0")})
    conn.schema_names["mod_acme_audit_log"] = None

    with pytest.raises(migration_runner.ModuleSchemaOwnershipError, match="mod_acme_audit_log"):
        migration_runner.apply_platform_module_migrations(
            conn, [("acme.audit.log", "1.0.0", tmp_path)]
        )

    assert conn.bodies == []
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_ownership_is_checked_for_every_module_before_any_runs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "20261201000000_init.sql", _INIT_SQL)
    _write(second, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    conn.schema_names["mod_acme_second"] = None

    with pytest.raises(migration_runner.ModuleSchemaOwnershipError):
        migration_runner.apply_platform_module_migrations(
            conn, [("acme.first", "1.0.0", first), ("acme.second", "1.0.0", second)]
        )

    assert conn.bodies == []
    assert conn.schemas == []


def test_failed_module_migration_rolls_back_and_records_nothing(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", "CREATE TABLE will_fail (id int);\n")
    conn = _FakeConn(fail_on="will_fail")

    with pytest.raises(RuntimeError, match="module migration body failed"):
        _apply(conn, tmp_path)

    assert conn.rollbacks == 1
    assert conn.committed == {}


def test_invalid_module_migration_filename_is_rejected(tmp_path):
    _write(tmp_path, "init.sql", _INIT_SQL)

    with pytest.raises(migration_runner.InvalidMigrationFilenameError):
        _apply(_FakeConn(), tmp_path)


def test_module_without_migrations_gets_no_schema(tmp_path):
    conn = _FakeConn()

    result = _apply(conn, tmp_path / "missing")

    assert result["total"] == 0
    assert conn.schemas == []


def test_downgrade_of_a_prepared_module_is_refused(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    checksum = migration_runner.migration_checksum(_INIT_SQL)
    conn = _FakeConn(
        applied={
            (MODULE_ID, "20261201000000_init.sql"): (checksum, "1.0.0"),
            (MODULE_ID, "20261220000000_newer.sql"): ("abc", "1.2.0"),
        }
    )

    with pytest.raises(migration_runner.ModuleMigrationDowngradeError, match=r"newer\.sql"):
        _apply(conn, tmp_path)

    assert conn.bodies == []


def test_downgrade_stops_before_any_other_module_runs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "20261201000000_init.sql", _INIT_SQL)
    _write(second, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(applied={("acme.second", "20261220000000_newer.sql"): ("abc", "2.0.0")})

    with pytest.raises(migration_runner.ModuleMigrationDowngradeError):
        migration_runner.apply_platform_module_migrations(
            conn, [("acme.first", "1.0.0", first), ("acme.second", "1.0.0", second)]
        )

    assert conn.bodies == []


def test_orphaned_module_rows_are_reported_and_never_block(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(
        applied={
            ("acme.legacy", "20261101000000_init.sql"): ("abc", "0.9.0"),
            ("acme.legacy", "20261102000000_more.sql"): ("def", "0.9.1"),
        }
    )

    result = migration_runner.apply_platform_module_migrations(
        conn,
        [migration_runner.ModuleMigrationSource(MODULE_ID, "1.0.0", tmp_path)],
    )

    assert [module["applied"] for module in result["modules"]] == [["20261201000000_init.sql"]]
    assert result["orphaned_modules"] == [
        {
            "module_id": "acme.legacy",
            "migrations": ["20261101000000_init.sql", "20261102000000_more.sql"],
        }
    ]


def test_no_prepared_module_reads_but_never_creates_the_ledger():
    empty = _FakeConn()
    assert migration_runner.apply_platform_module_migrations(empty, [], acquire_lock=False) == {
        "modules": [],
        "orphaned_modules": [],
    }
    assert empty.ledger_created is False

    removed = _FakeConn(applied={("acme.legacy", "20261101000000_init.sql"): ("abc", "0.9.0")})
    result = migration_runner.apply_platform_module_migrations(removed, [], acquire_lock=False)
    assert result["orphaned_modules"] == [
        {"module_id": "acme.legacy", "migrations": ["20261101000000_init.sql"]}
    ]
    assert removed.locks == []


def test_modules_sharing_a_schema_name_are_refused(tmp_path):
    # The mapping is injective, so only the same id prepared twice collides.
    with pytest.raises(migration_runner.ModuleMigrationError, match="same schema"):
        migration_runner.apply_platform_module_migrations(
            _FakeConn(),
            [("acme.audit-log", "1.0.0", tmp_path), ("acme.audit-log", "1.1.0", tmp_path)],
        )


def test_module_without_version_is_refused(tmp_path):
    with pytest.raises(migration_runner.ModuleMigrationError, match="no version"):
        migration_runner.apply_platform_module_migrations(_FakeConn(), [(MODULE_ID, "", tmp_path)])


def test_module_migrations_reuse_a_caller_owned_lock(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    conn.autocommit = True

    migration_runner.apply_platform_module_migrations(
        conn, [(MODULE_ID, "1.0.0", tmp_path)], acquire_lock=False
    )

    assert conn.locks == []
    assert conn.unlocks == []
    assert conn.autocommit is True


def test_applied_rows_without_a_schema_are_refused_before_any_module_runs(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "20261201000000_init.sql", _INIT_SQL)
    _write(second, "20261201000000_init.sql", _INIT_SQL)
    checksum = migration_runner.migration_checksum(_INIT_SQL)
    conn = _FakeConn(applied={("acme.audit-log", "20261201000000_init.sql"): (checksum, "1.0")})

    with pytest.raises(migration_runner.ModuleSchemaMissingError) as error:
        migration_runner.apply_platform_module_migrations(
            conn, [("acme.first", "1.0.0", first), ("acme.audit-log", "1.0.0", second)]
        )

    message = str(error.value)
    assert "acme.audit-log" in message
    assert "mod_acme_audit__log" in message
    assert "dropped" in message
    assert "mod_acme_audit_log" in message  # the earlier name mapping
    assert conn.bodies == []
    assert conn.schemas == []  # acme.first did not run either
    assert conn.unlocks == [migration_runner.MIGRATION_ADVISORY_LOCK_KEY]


def test_first_install_without_rows_or_schema_creates_the_schema(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn()
    assert SCHEMA not in conn.schema_names

    result = _apply(conn, tmp_path)

    assert result["applied"] == ["20261201000000_init.sql"]
    assert conn.schemas == [SCHEMA]


# ---------------------------------------------------------------------------
# Read-only inspection
# ---------------------------------------------------------------------------


def test_inspection_without_a_ledger_reports_every_file_pending_and_creates_nothing(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    _write(tmp_path, "20261215000000_currency.sql", _UPGRADE_SQL)
    conn = _FakeConn()

    report = migration_runner.inspect_module_migrations(conn, [(MODULE_ID, "1.1.0", tmp_path)])

    assert report == {
        MODULE_ID: {
            "pending": ["20261201000000_init.sql", "20261215000000_currency.sql"],
            "drifted": [],
            "unknown": [],
        }
    }
    assert migration_runner.module_migrations_need_upgrade(report) is True
    assert _mutating_statements(conn) == []
    assert conn.ledger_created is False
    assert conn.schemas == []


def test_inspection_reports_a_module_only_upgrade(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    _write(tmp_path, "20261215000000_currency.sql", _UPGRADE_SQL)
    conn = _FakeConn(
        applied={
            (MODULE_ID, "20261201000000_init.sql"): (
                migration_runner.migration_checksum(_INIT_SQL),
                "1.0.0",
            )
        }
    )

    report = migration_runner.inspect_module_migrations(conn, [(MODULE_ID, "1.1.0", tmp_path)])

    assert report[MODULE_ID]["pending"] == ["20261215000000_currency.sql"]
    assert _mutating_statements(conn) == []


def test_inspection_reports_drift_and_downgrade(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    conn = _FakeConn(
        applied={
            (MODULE_ID, "20261201000000_init.sql"): ("stale", "1.0.0"),
            (MODULE_ID, "20261220000000_newer.sql"): ("abc", "1.2.0"),
        }
    )

    report = migration_runner.inspect_module_migrations(conn, [(MODULE_ID, "1.0.0", tmp_path)])

    assert report == {
        MODULE_ID: {
            "pending": [],
            "drifted": ["20261201000000_init.sql"],
            "unknown": ["20261220000000_newer.sql"],
        }
    }


def test_up_to_date_module_needs_no_upgrade(tmp_path):
    _write(tmp_path, "20261201000000_init.sql", _INIT_SQL)
    checksum = migration_runner.migration_checksum(_INIT_SQL)
    conn = _FakeConn(applied={(MODULE_ID, "20261201000000_init.sql"): (checksum, "1.0.0")})

    report = migration_runner.inspect_module_migrations(conn, [(MODULE_ID, "1.0.0", tmp_path)])

    assert migration_runner.module_migrations_need_upgrade(report) is False


def test_inspection_without_prepared_modules_runs_no_query():
    conn = _FakeConn()

    assert migration_runner.inspect_module_migrations(conn, []) == {}
    assert conn.executed == []
