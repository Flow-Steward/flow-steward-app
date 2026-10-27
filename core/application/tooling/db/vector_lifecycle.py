from __future__ import annotations

from typing import Any

EXPECTED_PGVECTOR_VERSION = "0.8.5"

PROTECTED_DATA_TABLES = (
    (
        "memory_records",
        "t.id::text",
        "to_jsonb(t)::text",
        "embedding",
        False,
    ),
    (
        "platform_workflow_knowledge_documents",
        "concat_ws(E'\\x1f', t.account_id, t.project_scope, t.doc_id)",
        "(to_jsonb(t) - ARRAY['tenant_id', 'embedding'])::text",
        "embedding",
        True,
    ),
    (
        "platform_workflow_knowledge_nodes",
        "concat_ws(E'\\x1f', t.account_id, t.project_scope, t.entity_id)",
        "(to_jsonb(t) - 'tenant_id')::text",
        None,
        False,
    ),
    (
        "platform_workflow_knowledge_edges",
        "concat_ws(E'\\x1f', t.account_id, t.project_scope, t.edge_id)",
        "(to_jsonb(t) - 'tenant_id')::text",
        None,
        False,
    ),
)

REQUIRED_VECTOR_COLUMNS = (
    ("memory_records", "embedding"),
    ("platform_workflow_knowledge_documents", "embedding"),
)

REQUIRED_HNSW_INDEXES = (
    "idx_memory_records_embedding_cosine",
    "idx_platform_workflow_knowledge_docs_embedding_cosine",
)


def inspect_pgvector(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT available.default_version, installed.extversion
            FROM pg_available_extensions AS available
            LEFT JOIN pg_extension AS installed
              ON installed.extname = available.name
            WHERE available.name = 'vector'
            """
        )
        row = cur.fetchone()
    if row is None:
        return {"default_version": None, "installed_version": None}
    return {
        "default_version": str(row[0]) if row[0] is not None else None,
        "installed_version": str(row[1]) if row[1] is not None else None,
    }


def require_pgvector_image(conn) -> dict[str, str | None]:
    versions = inspect_pgvector(conn)
    default_version = versions["default_version"]
    if default_version != EXPECTED_PGVECTOR_VERSION:
        raise RuntimeError(
            "Flow Steward requires PostgreSQL image files for pgvector "
            f"{EXPECTED_PGVECTOR_VERSION}, but the image exposes "
            f"{default_version or 'no vector extension'}; rebuild or "
            "pull/recreate pg-fs, then rerun flow-steward database upgrade."
        )
    return versions


def capture_protected_data_snapshot(conn) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    with conn.cursor() as cur:
        for (
            table_name,
            key_sql,
            payload_sql,
            vector_column,
            allow_vector_backfill,
        ) in PROTECTED_DATA_TABLES:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
            table_row = cur.fetchone()
            if table_row is None or table_row[0] is None:
                snapshot[table_name] = {"present": False}
                continue

            vector_column_present = False
            if vector_column:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = %s
                          AND column_name = %s
                    )
                    """,
                    (table_name, vector_column),
                )
                column_row = cur.fetchone()
                vector_column_present = bool(column_row and column_row[0])

            vector_rows_sql = (
                f"count(t.{vector_column})"
                if vector_column and vector_column_present
                else "0::bigint"
            )
            cur.execute(
                f"SELECT count(*), {vector_rows_sql}, "  # nosec B608
                f"COALESCE(bit_xor(hashtextextended(({key_sql})::text, 0)), 0), "
                "COALESCE(bit_xor(hashtextextended("
                f"coalesce({payload_sql}, ''), 1)), 0) "
                f"FROM public.{table_name} AS t"
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Could not snapshot public.{table_name}")
            rows, vector_rows, stable_key_checksum, payload_checksum = row
            snapshot[table_name] = {
                "present": True,
                "vector_column_expected": bool(vector_column),
                "vector_column_present": vector_column_present,
                "rows": int(rows or 0),
                "vector_rows": int(vector_rows or 0),
                "stable_key_checksum": int(stable_key_checksum or 0),
                "authoritative_payload_checksum": int(payload_checksum or 0),
                "allow_vector_backfill": allow_vector_backfill,
            }
    return snapshot


def assert_protected_data_unchanged(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> None:
    mismatches: list[str] = []
    exact_fields = (
        "present",
        "rows",
        "stable_key_checksum",
        "authoritative_payload_checksum",
    )
    for table_name, expected in before.items():
        actual = after.get(table_name, {"present": False})
        for field in exact_fields:
            if actual.get(field) != expected.get(field):
                mismatches.append(
                    f"{table_name}.{field}: {expected.get(field)!r} -> {actual.get(field)!r}"
                )

        if not expected.get("vector_column_expected"):
            continue
        if not actual.get("vector_column_present"):
            mismatches.append(f"{table_name}.vector_column_present: false")
            continue
        before_vectors = int(expected.get("vector_rows") or 0)
        after_vectors = int(actual.get("vector_rows") or 0)
        if expected.get("allow_vector_backfill"):
            invalid = after_vectors < before_vectors
        else:
            invalid = after_vectors != before_vectors
        if invalid:
            mismatches.append(f"{table_name}.vector_rows: {before_vectors} -> {after_vectors}")

    if mismatches:
        raise RuntimeError(
            "protected data changed during database upgrade: "
            + "; ".join(mismatches)
            + f". before={before!r} after={after!r}"
        )


def _vector_coverage(conn) -> dict[str, dict[str, int]]:
    coverage: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), count(embedding)
            FROM public.memory_records
            """
        )
        rows, vector_rows = cur.fetchone()
        coverage["memory_records"] = {
            "rows": int(rows or 0),
            "authoritative_payload_rows": int(vector_rows or 0),
            "backfillable_payload_rows": 0,
            "vector_rows": int(vector_rows or 0),
            "backfillable_missing": 0,
            "non_backfillable_missing": max(
                0,
                int(rows or 0) - int(vector_rows or 0),
            ),
        }

        for table_name in ("platform_workflow_knowledge_documents",):
            cur.execute(
                f"""
                WITH payloads AS (
                    SELECT embedding, embedding_jsonb,
                           CASE
                               WHEN jsonb_typeof(embedding_jsonb) = 'array'
                               THEN jsonb_array_length(embedding_jsonb) = 1536
                                    AND NOT EXISTS (
                                        SELECT 1
                                        FROM jsonb_array_elements(
                                            embedding_jsonb
                                        ) AS element(value)
                                        WHERE jsonb_typeof(element.value)
                                              <> 'number'
                                    )
                               ELSE false
                           END AS backfillable
                    FROM public.{table_name}
                )
                SELECT count(*),
                       count(embedding_jsonb),
                       count(*) FILTER (WHERE backfillable),
                       count(embedding),
                       count(*) FILTER (
                           WHERE embedding IS NULL AND backfillable
                       ),
                       count(*) FILTER (
                           WHERE embedding IS NULL AND NOT backfillable
                       )
                FROM payloads
                """  # nosec B608
            )
            row = cur.fetchone()
            coverage[table_name] = {
                "rows": int(row[0] or 0),
                "authoritative_payload_rows": int(row[1] or 0),
                "backfillable_payload_rows": int(row[2] or 0),
                "vector_rows": int(row[3] or 0),
                "backfillable_missing": int(row[4] or 0),
                "non_backfillable_missing": int(row[5] or 0),
            }
    return coverage


def inspect_vector_health(conn, *, deep: bool = False) -> dict[str, Any]:
    health: dict[str, Any] = {
        "status": "unavailable",
        "required_version": EXPECTED_PGVECTOR_VERSION,
        "default_version": None,
        "installed_version": None,
        "version_match": False,
        "columns": {},
        "indexes": {},
        "vector_probe_ok": False,
        "coverage_checked": False,
        "coverage": {},
        "remediation": "",
        "error": "",
    }
    try:
        versions = inspect_pgvector(conn)
        health.update(versions)
        health["version_match"] = bool(
            versions["default_version"] == EXPECTED_PGVECTOR_VERSION
            and versions["installed_version"] == EXPECTED_PGVECTOR_VERSION
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND (table_name, column_name) IN (
                      ('memory_records', 'embedding'),
                      ('platform_workflow_knowledge_documents', 'embedding')
                  )
                """
            )
            present_columns = {
                (str(table_name), str(column_name)) for table_name, column_name in cur.fetchall()
            }
            health["columns"] = {
                f"{table_name}.{column_name}": (
                    table_name,
                    column_name,
                )
                in present_columns
                for table_name, column_name in REQUIRED_VECTOR_COLUMNS
            }

            cur.execute(
                """
                SELECT c.relname, i.indisvalid, i.indisready
                FROM pg_class AS c
                JOIN pg_index AS i ON i.indexrelid = c.oid
                WHERE c.relname = ANY(%s)
                """,
                (list(REQUIRED_HNSW_INDEXES),),
            )
            index_rows = {
                str(name): {"valid": bool(valid), "ready": bool(ready)}
                for name, valid, ready in cur.fetchall()
            }
            health["indexes"] = {
                name: index_rows.get(name, {"valid": False, "ready": False})
                for name in REQUIRED_HNSW_INDEXES
            }

            cur.execute(
                """
                SELECT '[1,0]'::public.vector
                       <=> '[0,1]'::public.vector
                """
            )
            health["vector_probe_ok"] = cur.fetchone() is not None

        if deep:
            health["coverage"] = _vector_coverage(conn)
            health["coverage_checked"] = True

        columns_ok = all(health["columns"].values())
        indexes_ok = all(item["valid"] and item["ready"] for item in health["indexes"].values())
        coverage_ok = not deep or all(
            item["backfillable_missing"] == 0 for item in health["coverage"].values()
        )
        installed = bool(health["installed_version"])
        if not installed or not health["vector_probe_ok"]:
            health["status"] = "unavailable"
        elif health["version_match"] and columns_ok and indexes_ok and coverage_ok:
            health["status"] = "healthy"
        else:
            health["status"] = "degraded"
    except Exception as exc:
        health["error"] = str(exc)
        health["status"] = "unavailable"

    if health["status"] != "healthy":
        health["remediation"] = (
            "Run release upgrade; it must recreate pg-fs before flow-steward database upgrade."
        )
    return health
