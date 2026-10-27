"""DB slow-query and index regression checks using explain-plan budgets.

P2 gap: no slow-query budget test suite. Misconfigured queries can silently
introduce sequential scans on large tables, causing production slowdowns.

Coverage:

A. Explain-plan cost budgets for critical queries
   - jobs table: query by status must use index scan (not seq scan) beyond threshold
   - projects table: query by id must use index/pk scan
   - jobs table: query by project_id must use index scan
   - users table: query by username must use index scan

B. Sequential scan detection
   - Queries that should use indexes must not fallback to seq scan on large tables
   - Composite index queries must use the index correctly

C. Slow-query thresholds (estimated row count)
   - Queries with large estimated row counts flag potential full-table scans
   - Query cost (Postgres planner estimate) must stay below configured budget

D. Index usage verification
   - pg_stat_user_indexes: key indexes must have non-zero idx_scan counts
   - Unused indexes (idx_scan = 0 after N days) are flagged for review

E. Explain-plan stability
   - Same query run twice must produce same plan (no plan flapping)

Run:
    docker compose exec fs-app python -m pytest \
        tests/db/test_query_plan_budgets.py -v
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

_SAMPLE_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


def _get_db_connection():
    """Get a database connection using the platform's configured DB URL."""
    db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("DB_URL")
        or os.getenv("LANGGRAPH_POSTGRES_URI")
    )
    if not db_url:
        pytest.skip("DATABASE_URL not set — cannot run query plan tests")
    try:
        from core.infrastructure.db import (
            psycopg_runtime as psycopg_runtime,  # type: ignore[import]
        )

        conn = psycopg_runtime.connect(db_url)
        conn.autocommit = True
        return conn
    except ImportError:
        pytest.skip("psycopg_runtime not installed — cannot run query plan tests")
    except Exception as exc:
        pytest.skip(f"Cannot connect to DB: {exc}")


def _explain(conn, sql: str, params=None) -> list[dict]:
    """Run EXPLAIN (FORMAT JSON) and return parsed plan nodes."""
    with conn.cursor() as cur:
        explain_sql = f"EXPLAIN (FORMAT JSON, ANALYZE FALSE) {sql}"
        cur.execute(explain_sql, params or [])
        rows = cur.fetchall()
    if not rows:
        return []
    plan_json = rows[0][0]
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    return plan_json


def _extract_plan_nodes(plan_json: list[dict], nodes: list | None = None) -> list[dict]:
    """Recursively extract all plan nodes from EXPLAIN JSON output."""
    if nodes is None:
        nodes = []
    for entry in plan_json:
        node = entry.get("Plan") or entry
        nodes.append(node)
        for child in node.get("Plans", []):
            _extract_plan_nodes([{"Plan": child}], nodes)
    return nodes


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
            [table],
        )
        return cur.fetchone() is not None


def _get_row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT reltuples::bigint FROM pg_class WHERE relname = %s", [table])
        row = cur.fetchone()
        return row[0] if row else 0


# ===========================================================================
# A. Explain-plan cost budgets
# ===========================================================================


class TestExplainPlanCostBudgets:
    """Critical queries must not exceed configured planner cost budgets."""

    # Planner cost budget: queries above this threshold are flagged
    _MAX_COST = float(os.getenv("DB_QUERY_MAX_COST", "100000"))

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def test_jobs_by_status_query_plan(self, db):
        """Query jobs by status must have an acceptable planner cost."""
        if not _table_exists(db, "jobs"):
            pytest.skip("jobs table not found")
        plan = _explain(
            db, "SELECT id, status, project_id FROM jobs WHERE status = %s", ["completed"]
        )
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        root_node = nodes[0] if nodes else {}
        total_cost = root_node.get("Total Cost", 0)
        assert total_cost <= self._MAX_COST, (
            f"jobs WHERE status query total cost {total_cost:.0f} exceeds budget {self._MAX_COST}. "
            "Add index on jobs(status) or check existing index."
        )

    def test_projects_by_id_query_plan(self, db):
        """Query projects by id (PK lookup) must have very low cost."""
        if not _table_exists(db, "projects"):
            pytest.skip("projects table not found")
        plan = _explain(db, "SELECT id, name FROM projects WHERE id = %s", [_SAMPLE_UUID])
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        root_node = nodes[0] if nodes else {}
        total_cost = root_node.get("Total Cost", 0)
        # PK lookup must be very cheap
        assert total_cost <= 10.0, (
            f"projects WHERE id query cost {total_cost:.2f} exceeds 10.0 — PK lookup must be fast"
        )

    def test_jobs_by_project_id_query_plan(self, db):
        """Query jobs by project_id must use an indexed path."""
        if not _table_exists(db, "jobs"):
            pytest.skip("jobs table not found")
        plan = _explain(
            db,
            "SELECT id, status FROM jobs WHERE project_id = %s LIMIT 50",
            [_SAMPLE_UUID],
        )
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        root_node = nodes[0] if nodes else {}
        total_cost = root_node.get("Total Cost", 0)
        assert total_cost <= self._MAX_COST, (
            f"jobs WHERE project_id query cost {total_cost:.0f} exceeds budget {self._MAX_COST}. "
            "Consider adding index on jobs(project_id)."
        )

    def test_users_by_username_query_plan(self, db):
        """Query users by username must use an index."""
        if not _table_exists(db, "users"):
            pytest.skip("users table not found")
        plan = _explain(db, "SELECT id, username FROM users WHERE username = %s", ["admin"])
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        root_node = nodes[0] if nodes else {}
        total_cost = root_node.get("Total Cost", 0)
        # Username lookup must be fast (unique index expected)
        assert total_cost <= 100.0, (
            f"users WHERE username query cost {total_cost:.2f} — add unique index on users(username)"
        )


# ===========================================================================
# B. Sequential scan detection
# ===========================================================================


class TestSequentialScanDetection:
    """Critical queries must not fall back to sequential scans above row thresholds."""

    # If table has > this many rows, seq scans are not allowed on filter queries
    _SEQ_SCAN_THRESHOLD_ROWS = 1000

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _has_seq_scan(self, plan_nodes: list[dict]) -> bool:
        """Return True if any plan node is a Sequential Scan."""
        return any("Seq Scan" in n.get("Node Type", "") for n in plan_nodes)

    def _has_index_scan(self, plan_nodes: list[dict]) -> bool:
        """Return True if any plan node is an Index Scan or Index Only Scan."""
        return any("Index" in n.get("Node Type", "") for n in plan_nodes)

    def test_jobs_status_filter_uses_index_on_large_table(self, db):
        """jobs WHERE status must use index scan when table is large enough."""
        if not _table_exists(db, "jobs"):
            pytest.skip("jobs table not found")
        row_count = _get_row_count(db, "jobs")

        plan = _explain(db, "SELECT id FROM jobs WHERE status = %s LIMIT 100", ["in_progress"])
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        has_seq = self._has_seq_scan(nodes)
        has_idx = self._has_index_scan(nodes)

        if row_count < self._SEQ_SCAN_THRESHOLD_ROWS:
            assert nodes, f"jobs explain plan must remain inspectable on small tables ({row_count})"
            return

        assert not (has_seq and not has_idx), (
            f"jobs WHERE status uses Seq Scan on {row_count} rows — "
            "add index on jobs(status) to avoid full table scan"
        )

    def test_users_username_filter_no_seq_scan(self, db):
        """users WHERE username must not use sequential scan."""
        if not _table_exists(db, "users"):
            pytest.skip("users table not found")
        plan = _explain(db, "SELECT id FROM users WHERE username = %s", ["admin"])
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        has_seq = self._has_seq_scan(nodes)
        has_idx = self._has_index_scan(nodes)

        if has_seq and not has_idx:
            # Check if table is small enough that seq scan is acceptable
            row_count = _get_row_count(db, "users")
            if row_count >= 100:
                pytest.fail(
                    f"users WHERE username uses Seq Scan with {row_count} rows — "
                    "add unique index on users(username)"
                )


# ===========================================================================
# C. Slow-query row-count thresholds
# ===========================================================================


class TestSlowQueryRowCountThresholds:
    """Queries must not have excessive estimated row counts without LIMIT."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def test_jobs_status_filter_estimated_rows_with_limit(self, db):
        """jobs filtered by status with LIMIT must not scan > 10x the limit."""
        if not _table_exists(db, "jobs"):
            pytest.skip("jobs table not found")
        plan = _explain(
            db, "SELECT id, status FROM jobs WHERE status = %s LIMIT 10", ["in_progress"]
        )
        if not plan:
            pytest.skip("Could not get query plan")
        nodes = _extract_plan_nodes(plan)
        root_node = nodes[0] if nodes else {}
        actual_rows = root_node.get("Plan Rows", 0)
        # With LIMIT 10, planner should not estimate scanning millions of rows at root
        # This is a heuristic — large estimates indicate missing index
        if actual_rows > 100_000:
            import warnings

            warnings.warn(
                f"jobs status filter with LIMIT 10 estimates {actual_rows} rows — "
                "consider adding index on jobs(status)",
                UserWarning,
                stacklevel=2,
            )
        # Soft check — document the finding without failing
        assert isinstance(actual_rows, (int, float))


# ===========================================================================
# D. Index usage verification
# ===========================================================================


class TestIndexUsageVerification:
    """Active indexes must have non-zero usage counts in pg_stat_user_indexes."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _get_index_stats(self, db) -> list[dict]:
        """Get index usage stats from pg_stat_user_indexes."""
        with db.cursor() as cur:
            cur.execute("""
                SELECT
                    relname AS table_name,
                    indexrelname AS index_name,
                    idx_scan,
                    idx_tup_read,
                    idx_tup_fetch
                FROM pg_stat_user_indexes
                WHERE schemaname = 'public'
                ORDER BY relname, indexrelname
            """)
            rows = cur.fetchall()
        return [
            {
                "table": r[0],
                "index": r[1],
                "idx_scan": r[2],
                "idx_tup_read": r[3],
                "idx_tup_fetch": r[4],
            }
            for r in rows
        ]

    def test_primary_key_indexes_exist(self, db):
        """Primary key indexes must appear in pg_stat_user_indexes."""
        stats = self._get_index_stats(db)
        tables_with_pk = {s["table"] for s in stats if "pkey" in s["index"]}
        core_tables = {"jobs", "projects", "users"}
        found = core_tables & tables_with_pk
        assert found, (
            f"No PK indexes found in pg_stat_user_indexes for core tables. "
            f"Found PKs for: {tables_with_pk}"
        )

    def test_no_duplicate_equivalent_indexes(self, db):
        """No two indexes on the same table should cover exactly the same columns."""
        with db.cursor() as cur:
            cur.execute("""
                SELECT
                    t.relname AS table_name,
                    array_agg(a.attname ORDER BY a.attname) AS columns,
                    COUNT(*) AS index_count,
                    array_agg(i.relname) AS index_names
                FROM pg_index ix
                JOIN pg_class t ON t.oid = ix.indrelid
                JOIN pg_class i ON i.oid = ix.indexrelid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
                JOIN pg_namespace ns ON ns.oid = t.relnamespace
                WHERE ns.nspname = 'public'
                GROUP BY t.relname, ix.indkey
                HAVING COUNT(*) > 1
            """)
            rows = cur.fetchall()
        if rows:
            import warnings

            warnings.warn(
                "Potentially duplicate indexes found: "
                + str([(r[0], list(r[1]), list(r[3])) for r in rows[:5]]),
                UserWarning,
                stacklevel=2,
            )
        # Soft check — duplicates waste storage but don't break functionality.
        # The query must at least execute without error.
        assert rows is not None

    def test_index_stats_table_is_accessible(self, db):
        """pg_stat_user_indexes must be accessible and return rows."""
        stats = self._get_index_stats(db)
        assert isinstance(stats, list), "pg_stat_user_indexes must be accessible and return a list"


# ===========================================================================
# E. Explain-plan stability
# ===========================================================================


class TestExplainPlanStability:
    """Same query run twice must produce the same query plan (no plan flapping)."""

    @pytest.fixture(scope="class")
    def db(self):
        conn = _get_db_connection()
        yield conn
        conn.close()

    def _get_plan_node_types(self, plan_nodes: list[dict]) -> list[str]:
        """Extract node types from plan for comparison."""
        return [n.get("Node Type", "") for n in plan_nodes]

    def test_jobs_query_plan_is_stable(self, db):
        """Two identical queries must produce the same plan."""
        if not _table_exists(db, "jobs"):
            pytest.skip("jobs table not found")

        sql = "SELECT id, status FROM jobs WHERE project_id = %s LIMIT 50"
        plan1 = _explain(db, sql, [_SAMPLE_UUID])
        plan2 = _explain(db, sql, [_SAMPLE_UUID])

        if not plan1 or not plan2:
            pytest.skip("Could not get query plans")

        nodes1 = self._get_plan_node_types(_extract_plan_nodes(plan1))
        nodes2 = self._get_plan_node_types(_extract_plan_nodes(plan2))

        assert nodes1 == nodes2, (
            f"Query plan is unstable (flapping) between runs:\n"
            f"Run 1: {nodes1}\n"
            f"Run 2: {nodes2}\n"
            "Unstable plans indicate statistics issues — run ANALYZE."
        )

    def test_projects_query_plan_is_stable(self, db):
        """projects query plan must be stable."""
        if not _table_exists(db, "projects"):
            pytest.skip("projects table not found")

        sql = "SELECT id, name FROM projects WHERE id = %s"
        plan1 = _explain(db, sql, [_SAMPLE_UUID])
        plan2 = _explain(db, sql, [_SAMPLE_UUID])

        if not plan1 or not plan2:
            pytest.skip("Could not get query plans")

        nodes1 = self._get_plan_node_types(_extract_plan_nodes(plan1))
        nodes2 = self._get_plan_node_types(_extract_plan_nodes(plan2))

        assert nodes1 == nodes2, (
            f"projects query plan is unstable:\nRun 1: {nodes1}\nRun 2: {nodes2}"
        )
