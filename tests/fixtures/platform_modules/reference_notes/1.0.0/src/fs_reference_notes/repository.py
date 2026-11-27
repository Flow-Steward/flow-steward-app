"""Account-scoped persistence of notes in the module's own schema.

The connection comes from Core, the way Core's own stores get one: the ``db``
service factory of the runtime container returns the shared platform store
(``None`` when no database is configured) and its ``cursor()`` runs on the
pooled, autocommit connection. The module never opens a connection itself.

Every statement filters on ``account_id``, so one account never reads, changes
or deletes another account's note.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

_SCHEMA_PATTERN = re.compile(r"^mod_[a-z0-9_]{1,59}$")
_NOTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class NotesUnavailableError(RuntimeError):
    """The database is not configured or cannot be reached."""


def module_schema(module_id: str) -> str:
    """The schema Core's migrator gives the module.

    The same rule as Core's ``module_schema_name``: ``mod_`` + the id with ``-``
    as ``__`` and ``.`` as ``_``. The module does not import Core's runner, so the
    rule is repeated here; the reference-module test checks both agree.
    """
    schema = "mod_" + str(module_id).replace("-", "__").replace(".", "_")
    if not _SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError(f"module id {module_id!r} does not give a usable schema name")
    return schema


def valid_note_id(value: str) -> bool:
    return bool(_NOTE_ID_PATTERN.fullmatch(str(value or "")))


def _plain(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


class NoteRepository:
    """Notes of one module schema; every method takes the caller's account."""

    def __init__(
        self,
        connection_source: Callable[[], Any],
        *,
        schema: str,
        pinned_column: bool,
    ) -> None:
        if not _SCHEMA_PATTERN.fullmatch(schema):
            raise ValueError(f"invalid module schema {schema!r}")
        self._connection_source = connection_source
        # Both identifiers are validated above; only values are ever bound.
        self._table = f"{schema}.notes"
        self._pinned_column = pinned_column
        self._columns: tuple[str, ...] = (
            "id",
            "title",
            "body",
            *(("pinned",) if pinned_column else ()),
            "created_at",
            "updated_at",
        )

    # -- reads and writes ----------------------------------------------------

    def list_notes(self, account_id: str) -> list[dict[str, Any]]:
        sql = (
            f"SELECT {self._column_list()} FROM {self._table} "  # nosec B608 - validated identifiers
            "WHERE account_id = %s ORDER BY created_at, id"
        )
        return self._fetch(sql, (account_id,))

    def create_note(self, account_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        columns = ["id", "account_id", "title", "body"]
        params: list[Any] = [
            str(uuid.uuid4()),
            account_id,
            values["title"],
            values.get("body") or "",
        ]
        if self._pinned_column:
            columns.append("pinned")
            params.append(values.get("pinned"))
        placeholders = ", ".join(["%s"] * len(columns))
        sql = (
            f"INSERT INTO {self._table} ({', '.join(columns)}) "  # nosec B608 - validated identifiers
            f"VALUES ({placeholders}) RETURNING {self._column_list()}"
        )
        return self._fetch(sql, tuple(params))[0]

    def update_note(
        self, account_id: str, note_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if not valid_note_id(note_id):
            return None
        assignments = ["title = %s", "body = %s"]
        params: list[Any] = [values["title"], values.get("body") or ""]
        if self._pinned_column:
            assignments.append("pinned = %s")
            params.append(values.get("pinned"))
        assignments.append("updated_at = now()")
        params.extend([account_id, note_id])
        sql = (
            f"UPDATE {self._table} SET {', '.join(assignments)} "  # nosec B608 - validated identifiers
            f"WHERE account_id = %s AND id = %s RETURNING {self._column_list()}"
        )
        rows = self._fetch(sql, tuple(params))
        return rows[0] if rows else None

    def delete_note(self, account_id: str, note_id: str) -> bool:
        if not valid_note_id(note_id):
            return False
        sql = (
            f"DELETE FROM {self._table} "  # nosec B608 - validated identifiers
            "WHERE account_id = %s AND id = %s RETURNING id"
        )
        with self._store().cursor() as cursor:
            cursor.execute(sql, (account_id, note_id))
            return bool(cursor.fetchall())

    # -- helpers -------------------------------------------------------------

    def _column_list(self) -> str:
        return ", ".join(self._columns)

    def _store(self) -> Any:
        try:
            store = self._connection_source()
        except Exception as exc:
            raise NotesUnavailableError("the database is unavailable") from exc
        if store is None:
            raise NotesUnavailableError("the database is not configured")
        return store

    def _fetch(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self._store().cursor() as cursor:
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
        return [self._note(row) for row in rows]

    def _note(self, row: Any) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return {column: _plain(row.get(column)) for column in self._columns}
        return {column: _plain(value) for column, value in zip(self._columns, row, strict=True)}
