"""Polars tabular feed plane ownership."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import tempfile
import time
import uuid
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.infrastructure.extension_runtime.object_storage import APPLICATION_JSON_VALUE
from core.infrastructure.tabular_feed.common import (
    _as_dict,
    _as_list,
    _input,
    _limit_env,
    _pl,
    _positive_int,
    _text,
)
from core.infrastructure.tabular_feed.constants import (
    DEFAULT_MAX_FEED_BYTES,
    DEFAULT_MAX_ROWS,
    PARQUET_CONTENT_TYPE,
    PREVIEW_LIMIT,
    RUNTIME_ID,
    STORAGE_CHUNK_SIZE,
)
from core.infrastructure.tabular_feed.readers import (
    _dataframe_from_artifact_path as _read_dataframe_from_artifact_path,
)
from core.infrastructure.tabular_feed.storage_io import (
    _put_file_to_storage,
    _sha256_file,
    _storage_key_path,
    _storage_stream,
)

_PATH_PARQUET = ".parquet"
_PATH_JSON = ".json"
_CURRENT_PLANE: ContextVar[ArtifactDatasetPlane | None] = ContextVar(
    "tabular_feed_plane",
    default=None,
)
_CURRENT_STORAGE: ContextVar[Any | None] = ContextVar("tabular_feed_storage", default=None)


def _plane() -> tuple[ArtifactDatasetPlane, Any]:
    current_plane = _CURRENT_PLANE.get()
    current_storage = _CURRENT_STORAGE.get()
    if current_plane is not None and current_storage is not None:
        return current_plane, current_storage
    raise RuntimeError("tabular_feed_plane_not_injected")


def _owner_scope(action: dict[str, Any]) -> dict[str, Any]:
    context = _as_dict(action.get("runtime_context")) or _as_dict(action.get("context"))
    if action.get("_trusted_core_context") is True:
        scope = _as_dict(context.get("owner_scope"))
    else:
        scope = _as_dict(action.get("scope")) or _as_dict(context.get("owner_scope"))
    for key in (
        "account_id",
        "tenant_id",
        "workspace_id",
        "project_id",
        "workflow_id",
        "job_id",
        "group_id",
        "step_id",
        "step_execution_id",
        "actor_id",
    ):
        value = context.get(key)
        if value is not None and _text(value):
            scope[key] = value
    return scope or {"runtime": RUNTIME_ID}


def _scope_field(scope: dict[str, Any], key: str) -> str:
    return _text(scope.get(key))


def _require_scoped_owner(action: dict[str, Any], meta: dict[str, Any], *, kind: str) -> None:
    caller = _owner_scope(action)
    owner = _as_dict(meta.get("owner_scope"))
    for field in ("account_id", "project_id"):
        caller_value = _scope_field(caller, field)
        owner_value = _scope_field(owner, field)
        if not caller_value or not owner_value or caller_value != owner_value:
            raise PermissionError(f"{kind}_access_denied")
    for field in ("tenant_id", "workspace_id"):
        caller_value = _scope_field(caller, field)
        owner_value = _scope_field(owner, field)
        if caller_value and owner_value and caller_value != owner_value:
            raise PermissionError(f"{kind}_access_denied")


def _snapshot_scope_slug(action: dict[str, Any]) -> str:
    scope = _owner_scope(action)
    account_id = _scope_field(scope, "account_id")
    project_id = _scope_field(scope, "project_id")
    if not account_id or not project_id:
        raise PermissionError("snapshot_scope_required")
    workflow_id = _scope_field(scope, "workflow_id") or "workflow"
    return "/".join(
        base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
        for value in (account_id, project_id, workflow_id)
    )


def _run_id(action: dict[str, Any]) -> str:
    return _text(_input(action).get("run_id")) or _text(action.get("run_id")) or "tabular_feed"


def _step_id(action: dict[str, Any]) -> str:
    return _text(_input(action).get("step_id")) or _text(action.get("step_id")) or "tabular_feed"


def _handle_id(value: Any) -> str:
    text = _text(value)
    if ":" in text:
        _, _, text = text.partition(":")
    return text


def _profile_slug(profile_key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", _text(profile_key)).strip("._-")
    if not slug:
        raise ValueError("profile_key is required")
    return slug[:120]


def _pointer_key(action: dict[str, Any], profile_key: str) -> str:
    return (
        f"tabular_feed/snapshots/{_snapshot_scope_slug(action)}/"
        f"{_profile_slug(profile_key)}/committed.json"
    )


def _candidate_meta_key(action: dict[str, Any], profile_key: str, artifact_id: str) -> str:
    return (
        f"tabular_feed/snapshots/{_snapshot_scope_slug(action)}/"
        f"{_profile_slug(profile_key)}/candidates/{artifact_id}.json"
    )


def _read_json_key(storage: Any, key: str) -> dict[str, Any] | None:
    raw = storage.get(key)
    if not raw:
        return None
    payload = json.loads(raw.decode("utf-8"))
    return payload if isinstance(payload, dict) else None


def _write_json_key(storage: Any, key: str, value: dict[str, Any]) -> None:
    storage.put(
        key,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        content_type=APPLICATION_JSON_VALUE,
    )


def _granted_handle_ids(action: dict[str, Any], *, kind: str) -> set[str]:
    rows: list[Any] = []
    if kind == "artifact":
        rows.extend(_as_list(_as_dict(action.get("artifacts")).get("inputs")))
    else:
        rows.extend(_as_list(_as_dict(action.get("datasets")).get("inputs")))
        rows.extend(_as_list(action.get("dataset_grants")))
    result: set[str] = set()
    id_key = "artifact_id" if kind == "artifact" else "dataset_id"
    handle_key = "artifact_handle" if kind == "artifact" else "dataset_handle"
    for row in rows:
        item = _as_dict(row)
        if not item:
            continue
        role = _text(item.get("role"), "input").lower()
        mode = _text(item.get("mode"), "read").lower()
        grant_kind = _text(item.get("grant_kind") or item.get("artifact_kind") or kind).lower()
        if role not in {"input", "read"} and mode not in {"read", "read_write"}:
            continue
        if grant_kind not in {kind, f"{kind}_handle"}:
            continue
        raw_id = item.get(id_key) or item.get(handle_key) or item.get("handle") or item.get("id")
        handle_id = _handle_id(raw_id)
        if handle_id:
            result.add(handle_id)
    return result


def _require_read_grant(action: dict[str, Any], *, kind: str, handle_id: str) -> None:
    if action.get("_trusted_core_context") is True:
        return
    if handle_id not in _granted_handle_ids(action, kind=kind):
        raise PermissionError(f"{kind}_read_grant_required")


def _meta_size_bytes(meta: dict[str, Any], storage: Any) -> int:
    value = _positive_int(meta.get("size_bytes"))
    if value is not None:
        return value
    object_key = _text(meta.get("object_key"))
    stat = storage.stat(object_key) if object_key and hasattr(storage, "stat") else None
    return int(_as_dict(stat).get("size_bytes") or 0)


def _guard_dataset_materialization(
    meta: dict[str, Any],
    storage: Any,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> None:
    row_count = _positive_int(meta.get("row_count"))
    if max_rows is not None and row_count is not None and row_count > max_rows:
        raise OverflowError("too_many_rows")
    size_bytes = _meta_size_bytes(meta, storage)
    if max_bytes is not None and size_bytes > max_bytes:
        raise ValueError("feed_too_large")


def _dataset_meta_for_read(
    action: dict[str, Any], dataset_handle: str
) -> tuple[str, dict[str, Any], Any]:
    plane, storage = _plane()
    dataset_id = _handle_id(dataset_handle)
    _require_read_grant(action, kind="dataset", handle_id=dataset_id)
    meta = plane.get_dataset(dataset_id)
    if not meta:
        raise FileNotFoundError(f"Dataset '{dataset_handle}' not found")
    _require_scoped_owner(action, meta, kind="dataset")
    return dataset_id, meta, storage


def _artifact_meta_for_read(
    action: dict[str, Any], artifact_handle: str
) -> tuple[dict[str, Any], Any, int]:
    plane, storage = _plane()
    artifact_id = _handle_id(artifact_handle)
    _require_read_grant(action, kind="artifact", handle_id=artifact_id)
    meta = plane.get_artifact(artifact_id)
    if not meta:
        raise FileNotFoundError(f"Artifact '{artifact_handle}' not found")
    _require_scoped_owner(action, meta, kind="artifact")
    max_bytes = _limit_env("FS_FEED_MAX_BYTES", DEFAULT_MAX_FEED_BYTES)
    size_bytes = _meta_size_bytes(meta, storage)
    if size_bytes > max_bytes:
        raise ValueError("feed_too_large")
    return meta, storage, max_bytes


def _read_artifact_body(
    action: dict[str, Any], artifact_handle: str
) -> tuple[bytes, dict[str, Any]]:
    meta, storage, max_bytes = _artifact_meta_for_read(action, artifact_handle)
    object_key = _text(meta.get("object_key"))
    chunks = []
    total = 0
    for chunk in _storage_stream(storage, object_key):
        data = bytes(chunk or b"")
        if not data:
            continue
        total += len(data)
        if total > max_bytes:
            raise ValueError("feed_too_large")
        chunks.append(data)
    if not chunks and _meta_size_bytes(meta, storage) != 0:
        raise FileNotFoundError(f"Artifact body '{artifact_handle}' not found")
    return b"".join(chunks), meta


def _read_dataset_rows(
    action: dict[str, Any],
    dataset_handle: str,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _dataset_id, meta, storage = _dataset_meta_for_read(action, dataset_handle)
    _guard_dataset_materialization(meta, storage, max_rows=max_rows, max_bytes=max_bytes)
    object_key = _text(meta.get("object_key"))
    encoding = _text(meta.get("encoding"), "json").lower()
    suffix = (
        _PATH_PARQUET if encoding == "parquet" or object_key.endswith(_PATH_PARQUET) else _PATH_JSON
    )
    with _storage_key_path(storage, object_key, suffix=suffix, max_bytes=max_bytes) as path:
        if suffix == _PATH_PARQUET:
            frame = _pl().scan_parquet(path).collect(engine="streaming")
            return _df_rows(frame), meta
        with open(path, "rb") as handle:
            rows = json.load(handle)
        if not isinstance(rows, list):
            rows = []
        return [row for row in rows if isinstance(row, dict)], meta


def _read_unfiltered_dataset_page(
    action: dict[str, Any],
    dataset_handle: str,
    *,
    offset: int,
    limit: int,
    max_rows: int,
    max_bytes: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    _dataset_id, meta, storage = _dataset_meta_for_read(action, dataset_handle)
    object_key = _text(meta.get("object_key"))
    encoding = _text(meta.get("encoding"), "json").lower()
    suffix = (
        _PATH_PARQUET if encoding == "parquet" or object_key.endswith(_PATH_PARQUET) else _PATH_JSON
    )
    if suffix != _PATH_PARQUET:
        rows, meta = _read_dataset_rows(
            action,
            dataset_handle,
            max_rows=max_rows,
            max_bytes=max_bytes,
        )
        return rows[offset : offset + limit], meta, len(rows)

    _guard_dataset_materialization(meta, storage, max_bytes=max_bytes)
    with _storage_key_path(storage, object_key, suffix=suffix, max_bytes=max_bytes) as path:
        frame = (
            _pl()
            .scan_parquet(path, cache=False, low_memory=True, rechunk=False)
            .slice(offset, limit)
            .collect(engine="streaming")
        )
    rows = _df_rows(frame)
    total_rows = _positive_int(meta.get("row_count"))
    if total_rows is None:
        total_rows = offset + len(rows)
    return rows, meta, total_rows


def _dataset_dataframe(
    action: dict[str, Any],
    dataset_handle: str,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
):
    pl = _pl()
    _dataset_id, meta, storage = _dataset_meta_for_read(action, dataset_handle)
    _guard_dataset_materialization(meta, storage, max_rows=max_rows, max_bytes=max_bytes)
    object_key = _text(meta.get("object_key"))
    encoding = _text(meta.get("encoding"), "json").lower()
    suffix = (
        _PATH_PARQUET if encoding == "parquet" or object_key.endswith(_PATH_PARQUET) else _PATH_JSON
    )
    with _storage_key_path(storage, object_key, suffix=suffix, max_bytes=max_bytes) as path:
        if suffix == _PATH_PARQUET:
            return pl.scan_parquet(path).collect(engine="streaming"), meta
        return pl.read_json(path, infer_schema_length=None), meta


@contextlib.contextmanager
def _dataset_lazyframe(
    action: dict[str, Any],
    dataset_handle: str,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
):
    pl = _pl()
    _dataset_id, meta, storage = _dataset_meta_for_read(action, dataset_handle)
    _guard_dataset_materialization(meta, storage, max_rows=max_rows, max_bytes=max_bytes)
    object_key = _text(meta.get("object_key"))
    encoding = _text(meta.get("encoding"), "json").lower()
    suffix = (
        _PATH_PARQUET if encoding == "parquet" or object_key.endswith(_PATH_PARQUET) else _PATH_JSON
    )
    with _storage_key_path(storage, object_key, suffix=suffix, max_bytes=max_bytes) as path:
        if suffix == _PATH_PARQUET:
            yield pl.scan_parquet(path), meta
        else:
            yield pl.read_json(path, infer_schema_length=None).lazy(), meta


def _df_rows(df: Any, limit: int | None = None) -> list[dict[str, Any]]:
    frame = df.head(limit) if limit is not None else df

    def jsonable(value: Any) -> Any:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, dict):
            return {key: jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [jsonable(item) for item in value]
        return value

    return [{key: jsonable(value) for key, value in row.items()} for row in frame.to_dicts()]


def _df_schema(df: Any) -> dict[str, Any]:
    collect_schema = getattr(df, "collect_schema", None)
    if callable(collect_schema):
        resolved = collect_schema()
        columns = list(resolved.names())
        dtypes = list(resolved.dtypes())
    else:
        columns = list(df.columns)
        dtypes = list(df.dtypes)
    return {
        "columns": [
            {"name": name, "type": str(dtype)} for name, dtype in zip(columns, dtypes, strict=True)
        ],
        "format": "parquet",
    }


def _new_dataset_id() -> str:
    return f"dst_{uuid.uuid4().hex[:24]}"


def _create_dataset_from_df(
    action: dict[str, Any],
    *,
    df: Any,
    schema: dict[str, Any] | None = None,
    retention_class: str = "run_window",
    max_rows: int | None = None,
    source_metadata: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    pl = _pl()
    dataset_id = _new_dataset_id()
    object_key = f"datasets/{dataset_id}/rows.parquet"
    fd, path = tempfile.mkstemp(suffix=_PATH_PARQUET)
    os.close(fd)
    try:
        lazy_frame = df.lazy() if callable(getattr(df, "lazy", None)) else df
        lazy_frame.sink_parquet(path, compression="zstd", row_group_size=1_000)
        persisted = pl.scan_parquet(path)
        row_count = int(persisted.select(pl.len()).collect(engine="streaming").item())
        row_limit = max_rows or _limit_env("FS_FEED_MAX_ROWS", DEFAULT_MAX_ROWS)
        if row_count > row_limit:
            raise OverflowError("too_many_rows")
        preview_frame = persisted.head(PREVIEW_LIMIT).collect(engine="streaming")
        _plane_obj, storage = _plane()
        stat = _put_file_to_storage(
            storage,
            object_key,
            path,
            content_type=PARQUET_CONTENT_TYPE,
            max_size_bytes=_limit_env("FS_FEED_MAX_BYTES", DEFAULT_MAX_FEED_BYTES),
        )
        now = time.time()
        retention = _text(retention_class, "run_window")
        expires_at = None
        if retention == "ephemeral":
            expires_at = now + 3600
        elif retention == "run_window":
            expires_at = now + 24 * 3600
        elif retention == "audit":
            expires_at = now + 30 * 24 * 3600
        meta = {
            "dataset_id": dataset_id,
            "owner_scope": _owner_scope(action),
            "run_id": _run_id(action),
            "step_id": _step_id(action),
            "object_key": object_key,
            "content_type": PARQUET_CONTENT_TYPE,
            "encoding": "parquet",
            "schema": schema or _df_schema(persisted),
            "row_count": row_count,
            "size_bytes": int(stat.get("size_bytes") or Path(path).stat().st_size),
            "sha256": _text(stat.get("sha256")) or _sha256_file(path),
            "retention_class": retention,
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
            "preview_rows": _df_rows(preview_frame),
            "format": "parquet",
            **(source_metadata or {}),
        }
        storage.put(
            f"datasets/{dataset_id}/meta.json",
            json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type=APPLICATION_JSON_VALUE,
        )
        datasets_cache = getattr(_plane_obj, "_datasets", None)
        if isinstance(datasets_cache, dict):
            datasets_cache[dataset_id] = dict(meta)
        return dataset_id, meta
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _create_dataset(
    action: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    schema: dict[str, Any] | None = None,
    retention_class: str = "run_window",
) -> tuple[str, dict[str, Any]]:
    pl = _pl()
    df = pl.DataFrame(rows, infer_schema_length=None)
    return _create_dataset_from_df(
        action,
        df=df,
        schema=schema or _schema(rows),
        retention_class=retention_class,
    )


def _create_artifact(
    action: dict[str, Any],
    *,
    filename: str,
    content_type: str,
    body: bytes,
    retention_class: str = "run_window",
) -> tuple[str, dict[str, Any]]:
    plane, _storage = _plane()
    handle, meta = plane.create_artifact(
        owner_scope=_owner_scope(action),
        run_id=_run_id(action),
        step_id=_step_id(action),
        filename=filename,
        content_type=content_type,
        retention_class=retention_class,
        body=body,
    )
    return handle.artifact_id, meta


def _create_artifact_from_file(
    action: dict[str, Any],
    *,
    filename: str,
    content_type: str,
    path: str,
    retention_class: str = "run_window",
) -> tuple[str, dict[str, Any]]:
    plane, _storage = _plane()

    def _chunks():
        with open(path, "rb") as stream:
            while chunk := stream.read(STORAGE_CHUNK_SIZE):
                yield chunk

    handle, meta = plane.create_artifact_from_stream(
        owner_scope=_owner_scope(action),
        run_id=_run_id(action),
        step_id=_step_id(action),
        filename=filename,
        content_type=content_type,
        retention_class=retention_class,
        chunks=_chunks(),
        max_size_bytes=_limit_env("FS_FEED_MAX_BYTES", DEFAULT_MAX_FEED_BYTES),
    )
    return handle.artifact_id, meta


def _dataframe_from_artifact_path(
    path: str,
    meta: dict[str, Any],
    options: dict[str, Any],
    *,
    lazy: bool = False,
) -> tuple[Any, dict[str, Any]]:
    return _read_dataframe_from_artifact_path(
        path,
        meta,
        options,
        polars_factory=_pl,
        lazy=lazy,
    )


def _infer_type(values: list[Any]) -> str:
    clean = [_text(value) for value in values if _text(value) != ""]
    if not clean:
        return "empty"
    if all(value.lower() in {"true", "false", "0", "1", "yes", "no"} for value in clean):
        return "boolean"
    for value_type, caster in (("integer", int), ("number", float)):
        try:
            for value in clean:
                caster(value)
            return value_type
        except ValueError:
            pass
    return "text"


def _schema(rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns: list[str] = []
    for row in rows[:100]:
        for key in row:
            if key not in columns:
                columns.append(key)
    return {
        "columns": [
            {
                "name": column,
                "type": _infer_type([row.get(column) for row in rows[:100]]),
            }
            for column in columns
        ]
    }


def _schema_column_names(schema: dict[str, Any]) -> list[str]:
    columns = schema.get("columns")
    if isinstance(columns, list):
        names = []
        for column in columns:
            if isinstance(column, dict) and _text(column.get("name")):
                names.append(_text(column.get("name")))
            elif _text(column):
                names.append(_text(column))
        return names
    return []


def _preview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows[:PREVIEW_LIMIT]


@contextlib.contextmanager
def injected_plane(plane: ArtifactDatasetPlane):
    storage = getattr(plane, "_storage", None)
    plane_token = _CURRENT_PLANE.set(plane)
    storage_token = _CURRENT_STORAGE.set(storage)
    try:
        yield
    finally:
        _CURRENT_STORAGE.reset(storage_token)
        _CURRENT_PLANE.reset(plane_token)
