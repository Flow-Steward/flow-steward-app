"""Shared helpers for encrypted job attachment storage."""

from __future__ import annotations

import hashlib
from typing import Any

from core.infrastructure.db import psycopg_runtime as psycopg_runtime

OCTET_STREAM_VALUE = "application/octet-stream"


def validate_attachment_encryption_schema(*, table_exists, column_exists) -> None:
    """Fail fast when attachment table exists without required crypto columns."""
    if not table_exists("job_attachments"):
        return
    required_columns = ("content_ciphertext", "content_nonce", "key_version")
    missing = [
        column for column in required_columns if not column_exists("job_attachments", column)
    ]
    if missing:
        raise RuntimeError(
            "Attachment encryption schema is incomplete; missing columns: "
            + ", ".join(sorted(missing))
        )


def backfill_plaintext_attachment_storage(
    *,
    conn,
    attachment_encryption_enabled: bool,
    table_exists,
    column_exists,
    encrypt_attachment_value,
) -> None:
    if not (
        attachment_encryption_enabled
        and table_exists("job_attachments")
        and column_exists("job_attachments", "content_ciphertext")
        and column_exists("job_attachments", "content_nonce")
        and column_exists("job_attachments", "key_version")
    ):
        return

    while True:
        with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, content
                FROM job_attachments
                WHERE content IS NOT NULL
                  AND (content_ciphertext IS NULL OR octet_length(content_ciphertext) = 0)
                ORDER BY uploaded_at ASC, id ASC
                LIMIT 100
                """
            )
            rows = cur.fetchall() or []
        if not rows:
            break
        for row in rows:
            ciphertext, nonce_value, key_version = encrypt_attachment_value(
                bytes((row or {}).get("content") or b"")
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_attachments
                       SET content_ciphertext = %s,
                           content_nonce = %s,
                           key_version = %s,
                           content = NULL
                     WHERE id = %s
                    """,
                    (
                        psycopg_runtime.Binary(ciphertext),
                        psycopg_runtime.Binary(nonce_value),
                        key_version,
                        str((row or {}).get("id") or "").strip(),
                    ),
                )


def _build_file_ref(
    *,
    attachment_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256_hex: str = "",
) -> dict[str, Any]:
    return {
        "artifact_id": "",
        "filename": str(filename or "").strip() or "upload.bin",
        "content_type": str(mime_type or OCTET_STREAM_VALUE).strip() or OCTET_STREAM_VALUE,
        "size_bytes": int(size_bytes or 0),
        "sha256": str(sha256_hex or "").strip(),
        "object_key": "",
        "source_ref": f"job_attachment:{str(attachment_id or '').strip()}",
        "retention_class": "run_window",
    }


def save_files(
    *,
    conn,
    job_id: str,
    files: list[dict[str, Any]],
    column_exists,
    encrypt_attachment_value,
) -> list[dict[str, Any]]:
    """Persist uploaded files to job_attachments table and return canonical file_ref rows."""
    if not (
        column_exists("job_attachments", "content_ciphertext")
        and column_exists("job_attachments", "content_nonce")
        and column_exists("job_attachments", "key_version")
    ):
        raise RuntimeError("Attachment encryption schema is not available")

    rows: list[dict[str, Any]] = []
    if not files:
        return rows

    with conn.cursor() as cur:
        # The Flow Steward logger may use autocommit connections, so use a session-level
        # advisory lock to make per-job upload_order reservation atomic.
        cur.execute(
            "SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))",
            ("job_attachments_upload_order", str(job_id or "").strip()),
        )
        try:
            cur.execute(
                """
                SELECT COALESCE(MAX(upload_order) + 1, 0)
                FROM job_attachments
                WHERE job_id = %s
                """,
                (job_id,),
            )
            next_upload_order_row = cur.fetchone()
            if isinstance(next_upload_order_row, dict):
                next_upload_order = int(
                    next_upload_order_row.get("coalesce")
                    or next_upload_order_row.get("next_upload_order")
                    or 0
                )
            elif isinstance(next_upload_order_row, (tuple, list)):
                next_upload_order = int(
                    (next_upload_order_row[0] if next_upload_order_row else 0) or 0
                )
            else:
                next_upload_order = int(next_upload_order_row or 0)
            for upload_order, file_row in enumerate(files):
                raw_content = bytes(file_row.get("content") or b"")
                ciphertext, nonce_value, key_version = encrypt_attachment_value(raw_content)
                cur.execute(
                    """INSERT INTO job_attachments (
                           job_id,
                           upload_order,
                           filename,
                           mime_type,
                           size_bytes,
                           content,
                           content_ciphertext,
                           content_nonce,
                           key_version
                       )
                       VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s) RETURNING id""",
                    (
                        job_id,
                        next_upload_order + upload_order,
                        file_row["filename"],
                        file_row["mime_type"],
                        file_row["size_bytes"],
                        psycopg_runtime.Binary(ciphertext),
                        psycopg_runtime.Binary(nonce_value),
                        key_version,
                    ),
                )
                attachment_id = cur.fetchone()[0]
                rows.append(
                    _build_file_ref(
                        attachment_id=str(attachment_id).strip(),
                        filename=str(file_row.get("filename") or ""),
                        mime_type=str(file_row.get("mime_type") or OCTET_STREAM_VALUE),
                        size_bytes=int(file_row.get("size_bytes") or 0),
                        sha256_hex=hashlib.sha256(raw_content).hexdigest() if raw_content else "",
                    )
                )
        finally:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                ("job_attachments_upload_order", str(job_id or "").strip()),
            )
    return rows


def get_attachments(
    *,
    conn,
    job_id: str,
    column_exists,
    decrypt_attachment_value,
    coerce_bytes,
) -> list[dict[str, Any]]:
    """Load all attachments for a job, including binary content."""
    has_encrypted_blob_columns = (
        column_exists("job_attachments", "content_ciphertext")
        and column_exists("job_attachments", "content_nonce")
        and column_exists("job_attachments", "key_version")
    )
    with conn.cursor(cursor_factory=psycopg_runtime.extras.RealDictCursor) as cur:
        if has_encrypted_blob_columns:
            cur.execute(
                """
                SELECT
                    id,
                    filename,
                    mime_type,
                    size_bytes,
                    content,
                    content_ciphertext,
                    content_nonce,
                    key_version
                FROM job_attachments
                WHERE job_id = %s
                ORDER BY upload_order ASC, uploaded_at ASC, id ASC
                """,
                (job_id,),
            )
        else:
            cur.execute(
                "SELECT id, filename, mime_type, size_bytes, content "
                "FROM job_attachments WHERE job_id = %s "
                "ORDER BY upload_order ASC, uploaded_at ASC, id ASC",
                (job_id,),
            )
        rows: list[dict[str, Any]] = []
        for row in cur.fetchall():
            payload = dict(row)
            if has_encrypted_blob_columns and payload.get("content_ciphertext") is not None:
                payload["content"] = decrypt_attachment_value(
                    coerce_bytes(payload.get("content_ciphertext")),
                    coerce_bytes(payload.get("content_nonce")),
                    str(payload.get("key_version") or "") or None,
                )
            else:
                payload["content"] = coerce_bytes(payload.get("content"))
            payload.pop("content_ciphertext", None)
            payload.pop("content_nonce", None)
            payload.pop("key_version", None)
            rows.append(payload)
        return rows
