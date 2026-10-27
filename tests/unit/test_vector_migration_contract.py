from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_baseline_qualifies_vector_objects_and_owns_all_embeddings() -> None:
    sql = (ROOT / "db/schema/001_schema_part_03.sql").read_text(encoding="utf-8")

    assert "embedding public.vector(1536)" in sql
    assert "embedding public.vector_cosine_ops" in sql
    assert "idx_memory_records_embedding_cosine" in sql
    assert "idx_platform_workflow_knowledge_docs_embedding_cosine" in sql


def test_workflow_baseline_is_tenant_aware() -> None:
    sql = (ROOT / "db/schema/001_schema_part_03.sql").read_text(encoding="utf-8")

    assert sql.count("tenant_id text DEFAULT ''::text NOT NULL") >= 3
    for index_name in (
        "idx_platform_workflow_knowledge_nodes_scope",
        "idx_platform_workflow_knowledge_edges_scope",
        "idx_platform_workflow_knowledge_documents_scope",
    ):
        start = sql.index(f"CREATE INDEX {index_name}")
        statement = sql[start : sql.index(";", start)]
        assert "(account_id, tenant_id, project_scope," in statement
