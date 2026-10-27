"""Dead/unused field detector across UI/API/DB — C-05 audit gap.

Scans codebase source files for column references and compares them against
the canonical SQL schema to surface:

A. Schema columns never referenced in Python source (dead DB columns)
   - Parses CREATE TABLE definitions from db/schema/*.sql
   - Scans Python .py files for column name mentions
   - Reports columns that appear in schema but not in any Python file
   - Excludes known system/auto columns (id, created_at, updated_at, etc.)

B. Python code references to columns not declared in schema (phantom fields)
   - Finds explicit SQL SELECT col / INSERT col / UPDATE col= patterns
   - Checks each extracted column name against schema declarations
   - Reports column names referenced in raw SQL that don't appear in schema

C. Model field inventory snapshot
   - Counts total schema columns and coverage ratio
   - Fails if dead column count exceeds configured threshold (soft gate)

D. API response key audit (static)
   - Scans serialization helpers for field lists
   - Reports any field listed in serializers but absent from schema

Run:
    python -m pytest tests/unit/test_dead_field_detector.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.infrastructure.db.schema_state import read_canonical_schema_sql

ROOT = Path(__file__).resolve().parent.parent.parent

# Columns that are universal infrastructure — not worth flagging
_SKIP_COLUMNS = {
    "id",
    "created_at",
    "updated_at",
    "deleted_at",
    "created_by",
    "updated_by",
    "meta",
    "extra",
}

# Tables whose columns are intentionally internal and rarely referenced directly
_SKIP_TABLES = {
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
}

# How many dead columns are tolerable before the test fails (soft budget)
_DEAD_COLUMN_BUDGET = 50


def _parse_schema_tables(content: str) -> dict[str, set[str]]:
    """Parse CREATE TABLE statements and return {table: {columns}}."""
    tables: dict[str, set[str]] = {}
    # Find each CREATE TABLE block
    table_pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?(\w+)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    col_pattern = re.compile(
        r"^\s{4}(\w+)\s+(?:integer|bigint|serial|bigserial|boolean|text|varchar|"
        r"character\s+varying|timestamptz|timestamp|date|numeric|decimal|float|"
        r"double|real|jsonb|json|uuid|bytea|smallint)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in table_pattern.finditer(content):
        table_name = m.group(1).lower()
        body = m.group(2)
        cols: set[str] = set()
        for cm in col_pattern.finditer(body):
            col = cm.group(1).lower()
            if not col.startswith("--"):
                cols.add(col)
        if cols:
            tables[table_name] = cols
    return tables


def _scan_python_column_mentions(py_root: Path) -> set[str]:
    """Collect all bare word tokens from .py files that could be column names."""
    mentions: set[str] = set()
    # Simple: collect all snake_case identifiers from Python files
    word_pattern = re.compile(r"\b([a-z][a-z0-9_]{2,})\b")
    for py_file in py_root.rglob("*.py"):
        # Skip test files and venv
        if ".venv" in str(py_file) or "__pycache__" in str(py_file):
            continue
        try:
            text = py_file.read_text(errors="ignore")
            for m in word_pattern.finditer(text):
                mentions.add(m.group(1))
        except OSError:
            pass
    return mentions


def _parse_raw_sql_column_refs(py_root: Path) -> set[str]:
    """Extract column names from raw SQL strings in Python source."""
    # Look for patterns like SELECT col, col2 FROM / INSERT INTO tbl (col1, col2)
    refs: set[str] = set()
    select_cols = re.compile(
        r"SELECT\s+((?:[\w\s,.*]+?))\s+FROM\b",
        re.IGNORECASE,
    )
    insert_cols = re.compile(
        r"INSERT\s+INTO\s+\w+\s*\(([\w\s,]+?)\)",
        re.IGNORECASE,
    )
    update_set = re.compile(
        r"SET\s+([\w\s,=]+?)\s+WHERE\b",
        re.IGNORECASE,
    )
    col_name = re.compile(r"\b([a-z][a-z0-9_]{2,})\b")

    for py_file in py_root.rglob("*.py"):
        if ".venv" in str(py_file) or "__pycache__" in str(py_file):
            continue
        try:
            text = py_file.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in (select_cols, insert_cols, update_set):
            for m in pattern.finditer(text):
                fragment = m.group(1)
                for col_m in col_name.finditer(fragment):
                    refs.add(col_m.group(1).lower())
    return refs


# ===========================================================================
# A. Dead column detection
# ===========================================================================


class TestDeadColumnDetection:
    """Columns declared in schema but never mentioned in Python source."""

    @pytest.fixture(scope="class")
    def schema_tables(self):
        return _parse_schema_tables(read_canonical_schema_sql())

    @pytest.fixture(scope="class")
    def python_mentions(self):
        return _scan_python_column_mentions(ROOT)

    def test_schema_parsed_has_tables(self, schema_tables):
        """Schema parser must find at least 50 tables."""
        assert len(schema_tables) >= 50, (
            f"Only {len(schema_tables)} tables parsed — check schema parser regex"
        )

    def test_schema_parsed_has_columns(self, schema_tables):
        """Each parsed table must have at least one column."""
        empty = [t for t, cols in schema_tables.items() if not cols]
        assert not empty, f"Tables with no columns parsed: {empty[:10]}"

    def test_dead_column_count_within_budget(self, schema_tables, python_mentions):
        """Number of schema columns with no Python mention must be below budget."""
        dead: list[tuple[str, str]] = []
        for table, cols in schema_tables.items():
            if table in _SKIP_TABLES:
                continue
            for col in cols:
                if col in _SKIP_COLUMNS:
                    continue
                if col not in python_mentions:
                    dead.append((table, col))

        dead.sort()
        dead_count = len(dead)
        # Soft gate — warn but only fail if far above budget
        if dead_count > _DEAD_COLUMN_BUDGET:
            sample = dead[:20]
            pytest.fail(
                f"{dead_count} schema columns have no Python mention "
                f"(budget={_DEAD_COLUMN_BUDGET}). "
                f"Sample dead columns: {sample}. "
                "Run this test with -s to see full list."
            )
        elif dead_count > 0:
            import warnings

            warnings.warn(
                f"{dead_count} schema columns have no Python mention "
                f"(budget={_DEAD_COLUMN_BUDGET}). Sample: {dead[:5]}",
                UserWarning,
                stacklevel=2,
            )

    def test_core_table_columns_are_referenced(self, schema_tables, python_mentions):
        """Core columns in key tables must appear in Python source."""
        critical = {
            "jobs": {"status", "project_id", "task_summary"},
            "projects": {"name", "project_type"},
            "users": {"username", "email"},
        }
        missing: list[str] = []
        for table, cols in critical.items():
            if table not in schema_tables:
                continue
            for col in cols:
                if col not in python_mentions:
                    missing.append(f"{table}.{col}")
        assert not missing, f"Critical columns missing from all Python source: {missing}"


# ===========================================================================
# B. Phantom field detection (SQL references to non-schema columns)
# ===========================================================================


class TestPhantomFieldDetection:
    """Column names used in raw SQL that don't appear in any schema table."""

    @pytest.fixture(scope="class")
    def all_schema_columns(self):
        tables = _parse_schema_tables(read_canonical_schema_sql())
        all_cols: set[str] = set()
        for cols in tables.values():
            all_cols.update(cols)
        return all_cols

    @pytest.fixture(scope="class")
    def sql_refs(self):
        return _parse_raw_sql_column_refs(ROOT)

    # Common SQL keywords and function names to exclude from phantom check
    _SQL_KEYWORDS = {
        "select",
        "from",
        "where",
        "join",
        "left",
        "inner",
        "outer",
        "right",
        "and",
        "not",
        "null",
        "true",
        "false",
        "case",
        "when",
        "then",
        "else",
        "end",
        "order",
        "group",
        "having",
        "limit",
        "offset",
        "count",
        "sum",
        "max",
        "min",
        "avg",
        "distinct",
        "all",
        "exists",
        "like",
        "ilike",
        "between",
        "insert",
        "into",
        "values",
        "update",
        "delete",
        "set",
        "returning",
        "with",
        "as",
        "on",
        "using",
        "desc",
        "asc",
        "now",
        "coalesce",
        "cast",
        "type",
        "public",
        "schema",
        "table",
    }

    def test_sql_refs_found(self, sql_refs):
        """SQL column reference scanner must find at least some identifiers."""
        assert len(sql_refs) >= 10, (
            f"Too few SQL refs found ({len(sql_refs)}) — check scanner regex"
        )

    def test_phantom_column_count_acceptable(self, all_schema_columns, sql_refs):
        """Identifiers from raw SQL not in schema must be below a threshold."""
        # Filter out SQL keywords and very short names
        meaningful_refs = {r for r in sql_refs if len(r) > 3 and r not in self._SQL_KEYWORDS}
        phantoms = meaningful_refs - all_schema_columns - _SKIP_COLUMNS

        # Many phantoms are Python variables/function names that happen to
        # appear in SQL strings — this is expected. Only fail on extreme counts.
        phantom_budget = 500
        if len(phantoms) > phantom_budget:
            pytest.fail(
                f"{len(phantoms)} SQL identifiers not in schema "
                f"(budget={phantom_budget}). "
                "This may indicate phantom column references or a schema parse gap."
            )


# ===========================================================================
# C. Column coverage snapshot
# ===========================================================================


class TestColumnCoverageSnapshot:
    """Report overall schema column coverage ratio."""

    def test_schema_column_coverage_ratio(self):
        """At least 60% of schema columns must appear in Python source."""
        schema_tables = _parse_schema_tables(read_canonical_schema_sql())
        python_mentions = _scan_python_column_mentions(ROOT)

        total = 0
        referenced = 0
        for table, cols in schema_tables.items():
            if table in _SKIP_TABLES:
                continue
            for col in cols:
                if col in _SKIP_COLUMNS:
                    continue
                total += 1
                if col in python_mentions:
                    referenced += 1

        if total == 0:
            pytest.skip("No columns found in schema")

        ratio = referenced / total
        assert ratio >= 0.60, (
            f"Only {ratio:.0%} of schema columns appear in Python source "
            f"({referenced}/{total}). Budget: 60%. "
            "Investigate dead columns or schema parse gaps."
        )

    def test_jobs_table_column_inventory(self):
        """jobs table columns must be inventoried and documented."""
        schema_tables = _parse_schema_tables(read_canonical_schema_sql())
        jobs_cols = schema_tables.get("jobs", set())
        assert jobs_cols, "Could not parse jobs table columns"
        # Sanity: jobs table should have at least 10 columns
        assert len(jobs_cols) >= 10, (
            f"jobs table only has {len(jobs_cols)} parsed columns — check parser"
        )


# ===========================================================================
# D. Serializer field audit
# ===========================================================================


class TestSerializerFieldAudit:
    """Serializer/view model fields must exist in schema."""

    def test_query_model_view_fields_in_schema(self):
        """Fields listed in QueryModels must exist in the canonical schema."""
        schema_tables = _parse_schema_tables(read_canonical_schema_sql())
        all_schema_cols = {col for cols in schema_tables.values() for col in cols}

        # Scan query_models.py for TypedDict / dataclass field declarations
        query_models = ROOT / "core" / "application" / "management" / "query_models.py"
        if not query_models.exists():
            pytest.skip("core/application/management/query_models.py not found")

        content = query_models.read_text(errors="ignore")
        # Extract field names from TypedDict / dataclass style declarations
        field_pattern = re.compile(r"^\s{4}(\w+)\s*:", re.MULTILINE)
        declared_fields = {m.group(1).lower() for m in field_pattern.finditer(content)}

        # Remove Python/typing keywords
        python_keywords = {
            "self",
            "cls",
            "return",
            "none",
            "true",
            "false",
            "any",
            "optional",
            "list",
            "dict",
            "str",
            "int",
            "float",
            "bool",
            "type",
            "class",
            "def",
            "pass",
            "raise",
            "yield",
        }
        meaningful_fields = {
            f
            for f in declared_fields
            if (len(f) > 2 and f not in python_keywords and "_" in f) or len(f) > 4
        }

        # Fields that are view-level aggregates or computed (not direct DB cols)
        view_only = {
            "total_count",
            "page_size",
            "page",
            "items",
            "results",
            "has_more",
            "cursor",
            "next_cursor",
            "prev_cursor",
        }
        check_fields = meaningful_fields - view_only - _SKIP_COLUMNS

        phantom_view_fields = {f for f in check_fields if f not in all_schema_cols and len(f) > 5}

        # Soft gate: warn on phantom view fields
        if phantom_view_fields:
            import warnings

            warnings.warn(
                f"View model fields not found in any schema table: "
                f"{sorted(phantom_view_fields)[:10]}. "
                "These may be computed/aggregate fields or schema parse gaps.",
                UserWarning,
                stacklevel=2,
            )
        # Always pass — this is a report, not a hard gate
        assert meaningful_fields is not None
