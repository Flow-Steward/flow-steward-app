"""Polars tabular feed snapshots ownership."""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import tempfile
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from core.application.extensions.artifact_plane_values import artifact_metadata_key
from core.application.tabular_feed.snapshot_models import SnapshotHead, SnapshotRef, SnapshotScope
from core.application.tabular_feed.snapshot_repository import SnapshotRepository
from core.infrastructure.tabular_feed.common import (
    _limit_env,
    _pl,
    _text,
)
from core.infrastructure.tabular_feed.conditions import _empty
from core.infrastructure.tabular_feed.constants import (
    DEFAULT_MAX_FEED_BYTES,
    PARQUET_CONTENT_TYPE,
)
from core.infrastructure.tabular_feed.engine_plane import (
    _PATH_PARQUET,
    _create_artifact_from_file,
    _owner_scope,
    _plane,
    _pointer_key,
    _profile_slug,
    _require_scoped_owner,
)
from core.infrastructure.tabular_feed.storage_io import (
    _storage_key_path,
)


def _row_key(row: dict[str, Any], key_column: str) -> str:
    value = _text(row.get(key_column))
    if not value:
        raise ValueError(f"missing key column value: {key_column}")
    return value


def _rows_by_key(rows: list[dict[str, Any]], key_column: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row, key_column)
        if key in result:
            raise RuntimeError(f"duplicate_key:{key}")
        result[key] = row
    return result


def _snapshot_body(rows: list[dict[str, Any]]) -> bytes:
    payload = "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)
    if payload:
        payload += "\n"
    return gzip.compress(payload.encode("utf-8"))


def _snapshot_rows(action: dict[str, Any], artifact_id: str) -> list[dict[str, Any]]:
    plane, storage = _plane()
    meta = plane.get_artifact(artifact_id)
    if not meta:
        raise FileNotFoundError(f"Snapshot '{artifact_id}' not found")
    _require_scoped_owner(action, meta, kind="artifact")
    raw = storage.get(_text(meta.get("object_key")))
    if not raw:
        return []
    text = gzip.decompress(raw).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@contextlib.contextmanager
def _snapshot_lazyframe(action: dict[str, Any], artifact_id: str):
    plane, storage = _plane()
    meta = plane.get_artifact(artifact_id)
    if not meta:
        raise FileNotFoundError(f"Snapshot '{artifact_id}' not found")
    _require_scoped_owner(action, meta, kind="artifact")
    object_key = _text(meta.get("object_key"))
    filename = _text(meta.get("filename"))
    content_type = _text(meta.get("content_type"))
    if not (object_key.endswith(_PATH_PARQUET) or filename.endswith(_PATH_PARQUET)):
        raise ValueError("snapshot_format_unsupported")
    if content_type and content_type != PARQUET_CONTENT_TYPE:
        raise ValueError("snapshot_format_unsupported")
    max_bytes = _limit_env("FS_FEED_MAX_BYTES", DEFAULT_MAX_FEED_BYTES)
    with _storage_key_path(
        storage,
        object_key,
        suffix=_PATH_PARQUET,
        max_bytes=max_bytes,
    ) as path:
        yield _pl().scan_parquet(path, cache=False, low_memory=True, rechunk=False), meta


def _lazy_schema_names(frame: Any) -> list[str]:
    return list(frame.collect_schema().names())


def _lazy_key_text(key_column: str) -> Any:
    pl = _pl()
    return pl.col(key_column).cast(pl.String, strict=False).str.strip_chars()


def _validate_lazy_dataset_key(frame: Any, *, key_column: str) -> Any:
    pl = _pl()
    if key_column not in _lazy_schema_names(frame):
        raise ValueError(f"missing key column value: {key_column}")
    normalized = frame.with_columns(_lazy_key_text(key_column).alias(key_column))
    missing = (
        normalized.filter(pl.col(key_column).is_null() | (pl.col(key_column) == ""))
        .select(key_column)
        .limit(1)
        .collect(engine="streaming")
    )
    if missing.height:
        raise ValueError(f"missing key column value: {key_column}")
    duplicate = (
        normalized.group_by(key_column)
        .len()
        .filter(pl.col("len") > 1)
        .select(key_column)
        .limit(1)
        .collect(engine="streaming")
    )
    if duplicate.height:
        raise RuntimeError(f"duplicate_key:{duplicate.item(0, 0)}")
    return normalized


def _lazy_column_expr(frame_columns: set[str], column: str, *, suffix: str = "") -> Any:
    pl = _pl()
    return pl.col(f"{column}{suffix}") if column in frame_columns else pl.lit(None)


def _carry_forward_enrichment_lazy(
    current: Any,
    baseline: Any,
    *,
    key_column: str,
    invalidate_on: list[str],
) -> Any:
    pl = _pl()
    current_names = _lazy_schema_names(current)
    baseline_names = _lazy_schema_names(baseline)
    enrichment_columns = [name for name in baseline_names if name.startswith("enriched_")]
    if not enrichment_columns:
        return current

    current_set = set(current_names)
    baseline_set = set(baseline_names)
    needed_baseline = sorted(
        (set(enrichment_columns) | set(invalidate_on)) & baseline_set - {key_column}
    )
    prefix = "__fs_baseline_enrichment__"
    baseline_projected = baseline.select(
        [pl.col(key_column)]
        + [pl.col(column).alias(f"{prefix}{column}") for column in needed_baseline]
        + [pl.lit(True).alias(f"{prefix}present")]
    )
    joined = current.join(baseline_projected, on=key_column, how="left")

    changed_conditions = []
    for column in invalidate_on:
        current_expr = pl.col(column) if column in current_set else pl.lit(None)
        baseline_expr = pl.col(f"{prefix}{column}") if column in baseline_set else pl.lit(None)
        changed_conditions.append(current_expr.ne_missing(baseline_expr))
    invalidated = pl.any_horizontal(changed_conditions) if changed_conditions else pl.lit(False)
    baseline_present = pl.col(f"{prefix}present").fill_null(False)

    enrichment_expressions = []
    current_schema = current.collect_schema()
    for column in enrichment_columns:
        previous = pl.col(f"{prefix}{column}")
        if column not in current_set:
            enrichment_expressions.append(
                pl.when(baseline_present & ~invalidated)
                .then(previous)
                .otherwise(pl.lit(None))
                .alias(column)
            )
            continue
        empty = pl.col(column).is_null()
        if current_schema[column] == pl.String:
            empty = empty | (pl.col(column).str.strip_chars() == "")
        enrichment_expressions.append(
            pl.when(baseline_present & ~invalidated & empty)
            .then(previous)
            .otherwise(pl.col(column))
            .alias(column)
        )
    enriched = joined.with_columns(enrichment_expressions)
    output_names = current_names + [
        column for column in enrichment_columns if column not in current_set
    ]
    return enriched.select(output_names)


def _lazy_diff_projection(
    current: Any,
    baseline: Any,
    *,
    key_column: str,
    compare_columns: list[str],
) -> Any:
    pl = _pl()
    current_names = _lazy_schema_names(current)
    baseline_names = _lazy_schema_names(baseline)
    current_set = set(current_names)
    baseline_set = set(baseline_names)
    output_columns = current_names + [
        column for column in baseline_names if column not in current_set
    ]
    prefix = "__fs_diff_baseline__"
    current_marker = "__fs_diff_current_present"
    baseline_marker = "__fs_diff_baseline_present"
    reserved = {current_marker, baseline_marker} | {
        f"{prefix}{column}" for column in baseline_names if column != key_column
    }
    if reserved & (current_set | baseline_set):
        raise ValueError("reserved diff column name is present")

    current_marked = current.with_columns(pl.lit(True).alias(current_marker))
    baseline_projected = baseline.select(
        [pl.col(key_column)]
        + [
            pl.col(column).alias(f"{prefix}{column}")
            for column in baseline_names
            if column != key_column
        ]
        + [pl.lit(True).alias(baseline_marker)]
    )
    joined = current_marked.join(
        baseline_projected,
        on=key_column,
        how="full",
        coalesce=True,
    )
    current_present = pl.col(current_marker).fill_null(False)
    baseline_present = pl.col(baseline_marker).fill_null(False)

    changed_conditions = []
    changed_names = []
    for column in compare_columns:
        current_expr = pl.col(column) if column in current_set else pl.lit(None)
        baseline_expr = pl.col(f"{prefix}{column}") if column in baseline_set else pl.lit(None)
        changed = current_expr.ne_missing(baseline_expr)
        changed_conditions.append(changed)
        changed_names.append(
            pl.when(changed).then(pl.lit(column)).otherwise(pl.lit(None, dtype=pl.String))
        )
    any_changed = pl.any_horizontal(changed_conditions) if changed_conditions else pl.lit(False)
    changed_fields = (
        pl.concat_list(changed_names).list.drop_nulls()
        if changed_names
        else pl.lit([], dtype=pl.List(pl.String))
    )
    status = (
        pl.when(~current_present)
        .then(pl.lit("removed"))
        .when(~baseline_present)
        .then(pl.lit("added"))
        .when(any_changed)
        .then(pl.lit("changed"))
        .otherwise(pl.lit("unchanged"))
    )
    projected_values = []
    for column in output_columns:
        if column == key_column:
            projected_values.append(pl.col(column))
            continue
        current_expr = pl.col(column) if column in current_set else pl.lit(None)
        baseline_expr = pl.col(f"{prefix}{column}") if column in baseline_set else pl.lit(None)
        projected_values.append(
            pl.when(current_present).then(current_expr).otherwise(baseline_expr).alias(column)
        )
    return joined.select(
        status.alias("__status"),
        pl.when(~current_present)
        .then(pl.lit([], dtype=pl.List(pl.String)))
        .when(~baseline_present)
        .then(pl.lit(compare_columns, dtype=pl.List(pl.String)))
        .otherwise(changed_fields)
        .alias("__changed_fields"),
        *projected_values,
    )


def _lazy_diff_summary(projected: Any) -> dict[str, int]:
    pl = _pl()
    counts = {
        str(row["__status"]): int(row["count"])
        for row in (
            projected.group_by("__status")
            .agg(pl.len().alias("count"))
            .collect(engine="streaming")
            .to_dicts()
        )
    }
    summary = {
        "added": counts.get("added", 0),
        "changed": counts.get("changed", 0),
        "removed": counts.get("removed", 0),
        "unchanged": counts.get("unchanged", 0),
        "total": 0,
    }
    summary["total"] = summary["added"] + summary["changed"] + summary["removed"]
    return summary


def _create_snapshot_parquet_artifact(
    action: dict[str, Any],
    *,
    profile_key: str,
    frame: Any,
) -> tuple[str, dict[str, Any]]:
    fd, path = tempfile.mkstemp(suffix=_PATH_PARQUET)
    os.close(fd)
    try:
        frame.sink_parquet(path, compression="zstd", row_group_size=1_000)
        artifact_id, metadata = _create_artifact_from_file(
            action,
            filename=f"{_profile_slug(profile_key)}-candidate.parquet",
            content_type=PARQUET_CONTENT_TYPE,
            path=path,
            retention_class="persistent",
        )
        return artifact_id, _mark_snapshot_artifact(action, profile_key, artifact_id, metadata)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def _diff_rows(
    current_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    *,
    key_column: str,
    compare_columns: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    current = _rows_by_key(current_rows, key_column)
    baseline = _rows_by_key(baseline_rows, key_column) if baseline_rows else {}
    columns = compare_columns or sorted(
        {key for row in [*current_rows, *baseline_rows] for key in row if not key.startswith("__")}
    )
    diff: list[dict[str, Any]] = []
    summary = {"added": 0, "changed": 0, "removed": 0, "unchanged": 0, "total": 0}
    for key, row in current.items():
        previous = baseline.get(key)
        if previous is None:
            diff.append({"__status": "added", "__changed_fields": columns, **row})
            summary["added"] += 1
            continue
        changed = [column for column in columns if row.get(column) != previous.get(column)]
        if changed:
            diff.append({"__status": "changed", "__changed_fields": changed, **row})
            summary["changed"] += 1
        else:
            summary["unchanged"] += 1
    for key, row in baseline.items():
        if key not in current:
            diff.append({"__status": "removed", "__changed_fields": [], **row})
            summary["removed"] += 1
    summary["total"] = len(diff)
    return diff, summary


def _carry_forward_enrichment(
    current_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    *,
    key_column: str,
    invalidate_on: list[str],
) -> list[dict[str, Any]]:
    if not baseline_rows:
        return [dict(row) for row in current_rows]
    baseline = _rows_by_key(baseline_rows, key_column)
    output: list[dict[str, Any]] = []
    for row in current_rows:
        next_row = dict(row)
        previous = baseline.get(_text(row.get(key_column)))
        if not previous:
            output.append(next_row)
            continue
        invalidated = any(row.get(column) != previous.get(column) for column in invalidate_on)
        if not invalidated:
            for column, value in previous.items():
                if column.startswith("enriched_") and _empty(next_row.get(column)):
                    next_row[column] = value
        output.append(next_row)
    return output


_CURRENT_SNAPSHOTS: ContextVar[SnapshotRepository | None] = ContextVar(
    "tabular_snapshot_repository", default=None
)


@contextlib.contextmanager
def injected_snapshot_repository(repository: SnapshotRepository | None) -> Iterator[None]:
    token = _CURRENT_SNAPSHOTS.set(repository)
    try:
        yield
    finally:
        _CURRENT_SNAPSHOTS.reset(token)


def _snapshot_repository() -> SnapshotRepository:
    repository = _CURRENT_SNAPSHOTS.get()
    if repository is None:
        raise RuntimeError("snapshot_storage_unavailable")
    return repository


def _snapshot_scope(action: dict[str, Any], profile_key: str) -> SnapshotScope:
    owner = _owner_scope(action)
    return SnapshotScope(
        str(owner.get("account_id") or ""),
        str(owner.get("project_id") or ""),
        str(owner.get("workflow_id") or ""),
        profile_key,
    )


def _snapshot_scope_metadata(action: dict[str, Any], profile_key: str) -> dict[str, str]:
    return dict(
        zip(
            ("account_id", "project_id", "workflow_id", "profile_key"),
            _snapshot_scope(action, profile_key).as_tuple(),
            strict=True,
        )
    )


def _parse_snapshot_ref(value: Any) -> SnapshotRef:
    if not isinstance(value, dict) or set(value) != {"revision", "snapshot_artifact_id"}:
        raise ValueError("invalid_snapshot_ref")
    return SnapshotRef(value["revision"], value["snapshot_artifact_id"])


def _current_snapshot(action: dict[str, Any], profile_key: str) -> SnapshotHead:
    current = _snapshot_repository().read_current(_snapshot_scope(action, profile_key))
    if current.ref.revision == 0:
        _plane_obj, storage = _plane()
        # Existence is a transition guard, never a second source of head data.
        if storage.stat(_pointer_key(action, profile_key)) is not None:
            raise RuntimeError("snapshot_dev_data_transition_required")
    return current


def _snapshot_read_metadata(
    action: dict[str, Any], artifact: dict[str, Any], *, canonical: bool
) -> dict[str, Any]:
    marker = artifact.get("_tabular_snapshot_scope")
    eligible = False
    if canonical and isinstance(marker, dict) and marker.get("profile_key"):
        eligible = marker == _snapshot_scope_metadata(action, marker["profile_key"])
    return {
        "source_artifact_id": str(artifact.get("artifact_id") or ""),
        "snapshot_baseline_eligible": eligible,
        **({"snapshot_scope": dict(marker)} if eligible and isinstance(marker, dict) else {}),
    }


def _validate_snapshot_baseline(
    action: dict[str, Any],
    *,
    profile_key: str,
    expected: SnapshotRef,
    baseline: Any,
    metadata: dict[str, Any],
) -> None:
    if expected.revision == 0:
        actual_rows = baseline.select(_pl().len()).collect(engine="streaming").item()
        if actual_rows != 0:
            raise ValueError("snapshot_baseline_mismatch")
        return
    wanted_scope = _snapshot_scope_metadata(action, profile_key)
    if (
        metadata.get("snapshot_baseline_eligible") is not True
        or metadata.get("source_artifact_id") != expected.snapshot_artifact_id
        or metadata.get("snapshot_scope") != wanted_scope
    ):
        raise ValueError("snapshot_baseline_mismatch")
    plane, _storage = _plane()
    artifact = plane.get_artifact(expected.snapshot_artifact_id)
    if not artifact or artifact.get("_tabular_snapshot_scope") != wanted_scope:
        raise ValueError("snapshot_baseline_mismatch")
    _require_scoped_owner(action, artifact, kind="artifact")


def _publication_snapshot_ref(
    action: dict[str, Any],
    *,
    profile_key: str,
    baseline: Any,
    metadata: dict[str, Any],
    supplied: SnapshotRef | None,
) -> SnapshotRef:
    repository = _snapshot_repository()
    missing_baseline = (
        "snapshot_baseline_mismatch" if supplied is not None else "snapshot_baseline_required"
    )
    artifact_id = _text(metadata.get("source_artifact_id"))
    plane, _storage = _plane()
    artifact = plane.get_artifact(artifact_id) if artifact_id else None
    # A canonical snapshot keeps its historical positive reference even when
    # empty. An uncommitted or mapped snapshot cannot acquire revision zero.
    marked = bool(artifact and artifact.get("_tabular_snapshot_scope"))
    if marked or metadata.get("snapshot_baseline_eligible") is True:
        if metadata.get("snapshot_baseline_eligible") is not True or not artifact_id:
            raise ValueError("snapshot_baseline_mismatch")
        receipt = repository.read_commit(_snapshot_scope(action, profile_key), artifact_id)
        if receipt is None:
            raise ValueError(missing_baseline)
        expected = receipt.ref
        _validate_snapshot_baseline(
            action,
            profile_key=profile_key,
            expected=expected,
            baseline=baseline,
            metadata=metadata,
        )
    else:
        actual_rows = baseline.select(_pl().len()).collect(engine="streaming").item()
        if actual_rows != 0:
            raise ValueError(missing_baseline)
        expected = SnapshotRef(0)
    if supplied is not None and supplied != expected:
        raise ValueError("snapshot_baseline_mismatch")
    return expected


def _mark_snapshot_artifact(
    action: dict[str, Any], profile_key: str, artifact_id: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    # Only host code writes the reserved metadata sidecar. Upload options and
    # user-supplied dataset metadata never flow into this marker.
    plane, storage = _plane()
    marked = {**metadata, "_tabular_snapshot_scope": _snapshot_scope_metadata(action, profile_key)}
    storage.put(
        artifact_metadata_key(artifact_id),
        json.dumps(marked, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    plane._artifacts[artifact_id] = dict(marked)
    return marked
