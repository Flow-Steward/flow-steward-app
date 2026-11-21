"""Empty JSON objects at the Parquet dataset storage boundary.

Parquet cannot store a group without children, so Polars refuses to write a
zero-field struct, the type it infers for a JSON field whose objects are all
``{}``. Polars 1.36 also loses nulls in zero-field structs: ``scan_ndjson`` and
``DataFrame(rows)`` read ``null`` there as ``{}``, and casting a zero-field
struct read back from Parquet drops its nulls.

Representation: an empty object is stored as a one-field struct whose only
child is :data:`EMPTY_OBJECT_FIELD`, typed ``Null`` (always null). That type is
the in-memory and on-disk form of every dataset; a zero-field struct is never
kept. ``null`` stays a null struct, so ``{}`` and ``null`` remain distinct.
The reserved field is dropped wherever rows leave the dataset as JSON (row
previews, ``read_rows``, NDJSON/JSON exports, NDJSON representations for
extensions) and from displayed schema types, so ``{}`` comes back as ``{}``.
A Parquet export keeps the reserved field, because Parquet has no other way to
hold it; reading that file back in Flow Steward restores ``{}``.

Columnar storage gives one struct type per column: when some objects of a
field are ``{}`` and others have keys, ``{}`` is stored with those keys set to
null and comes back as ``{"key": null}``, never as ``null``. An input object
whose only key is the reserved name with a null value is indistinguishable
from ``{}``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any

EMPTY_OBJECT_FIELD = "__flow_steward_empty_object__"

_MARKER_MEMBER = f'"{EMPTY_OBJECT_FIELD}":null'.encode()
# Compact JSON as Polars writes it. Inside a JSON string a quote is always
# escaped, so these byte sequences only occur as a struct member.
_JSON_REPLACEMENTS = (
    (b"{" + _MARKER_MEMBER + b"}", b"{}"),
    (_MARKER_MEMBER + b",", b""),
    (b"," + _MARKER_MEMBER, b""),
)
_JSON_CARRY_BYTES = max(len(pattern) for pattern, _ in _JSON_REPLACEMENTS) - 1
_COPY_CHUNK_BYTES = 1024 * 1024


def _polars() -> Any:
    import polars as pl

    return pl


def _is_struct(pl: Any, dtype: Any) -> bool:
    return isinstance(dtype, pl.Struct)


def _is_nested_list(pl: Any, dtype: Any) -> bool:
    return isinstance(dtype, (pl.List, pl.Array))


def contains_empty_struct(dtype: Any) -> bool:
    """Whether *dtype* holds a zero-field struct at any depth."""
    pl = _polars()
    if _is_struct(pl, dtype):
        return not dtype.fields or any(contains_empty_struct(field.dtype) for field in dtype.fields)
    if _is_nested_list(pl, dtype):
        return contains_empty_struct(dtype.inner)
    return False


def contains_empty_object_field(dtype: Any) -> bool:
    """Whether *dtype* holds the reserved empty-object field at any depth."""
    pl = _polars()
    if _is_struct(pl, dtype):
        return any(
            field.name == EMPTY_OBJECT_FIELD or contains_empty_object_field(field.dtype)
            for field in dtype.fields
        )
    if _is_nested_list(pl, dtype):
        return contains_empty_object_field(dtype.inner)
    return False


def storage_dtype(dtype: Any) -> Any:
    """*dtype* with every zero-field struct replaced by the stored empty-object form."""
    pl = _polars()
    if _is_struct(pl, dtype):
        if not dtype.fields:
            return pl.Struct({EMPTY_OBJECT_FIELD: pl.Null})
        return pl.Struct(
            [pl.Field(field.name, storage_dtype(field.dtype)) for field in dtype.fields]
        )
    if isinstance(dtype, pl.List):
        return pl.List(storage_dtype(dtype.inner))
    if isinstance(dtype, pl.Array):
        return pl.Array(storage_dtype(dtype.inner), dtype.shape)
    return dtype


def _display_dtype(dtype: Any) -> Any:
    pl = _polars()
    if _is_struct(pl, dtype):
        return pl.Struct(
            [
                pl.Field(field.name, _display_dtype(field.dtype))
                for field in dtype.fields
                if field.name != EMPTY_OBJECT_FIELD
            ]
        )
    if isinstance(dtype, pl.List):
        return pl.List(_display_dtype(dtype.inner))
    if isinstance(dtype, pl.Array):
        return pl.Array(_display_dtype(dtype.inner), dtype.shape)
    return dtype


def display_dtype(dtype: Any) -> str:
    """The type name shown to users: the empty-object form reads ``Struct({})``."""
    return str(_display_dtype(dtype))


def storable_frame(frame: Any) -> Any:
    """Cast remaining zero-field structs of a (lazy) frame to the stored form.

    Readers that lose nulls in zero-field structs convert before this point;
    this cast is the storage-boundary net for any other producer.
    """
    pl = _polars()
    schema = frame.collect_schema() if hasattr(frame, "collect_schema") else frame.schema
    casts = [
        pl.col(name).cast(storage_dtype(dtype))
        for name, dtype in schema.items()
        if contains_empty_struct(dtype)
    ]
    return frame.with_columns(casts) if casts else frame


def unstorable_columns(schema: Any) -> list[str]:
    """Columns whose type the Parquet writer refuses, after :func:`storable_frame`."""
    pl = _polars()
    return [
        name
        for name, dtype in schema.items()
        if isinstance(dtype, pl.Object) or contains_empty_struct(dtype)
    ]


def _decode_json_text_columns(frame: Any, schema: Any, columns: list[str]) -> Any:
    pl = _polars()
    return frame.with_columns(
        [pl.col(name).str.json_decode(storage_dtype(schema[name])) for name in columns]
    )


def scan_ndjson(path: str, pl: Any = None) -> Any:
    """``pl.scan_ndjson`` over the whole file that keeps ``{}`` apart from ``null``.

    A column holding empty objects is rescanned as its raw JSON text, which keeps
    nulls, and decoded into the stored type. The frame stays lazy.
    """
    pl = pl or _polars()
    # Types come from the whole file: a column null in the first rows (Polars samples
    # 100) and filled later, such as Shopify's template_suffix, is not typed Null.
    lazy_frame = pl.scan_ndjson(path, infer_schema_length=None)
    schema = lazy_frame.collect_schema()
    affected = [name for name, dtype in schema.items() if contains_empty_struct(dtype)]
    if not affected:
        return lazy_frame
    text_schema = {name: pl.String if name in affected else dtype for name, dtype in schema.items()}
    return _decode_json_text_columns(pl.scan_ndjson(path, schema=text_schema), schema, affected)


def read_json(path: str, pl: Any = None) -> Any:
    """``pl.read_json`` with empty objects in the stored form (this reader keeps nulls)."""
    return storable_frame((pl or _polars()).read_json(path, infer_schema_length=None))


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def frame_from_rows(rows: list[dict[str, Any]]) -> Any:
    """``pl.DataFrame(rows)`` that keeps ``{}`` apart from ``null``."""
    pl = _polars()
    frame = pl.DataFrame(rows, infer_schema_length=None)
    affected = [name for name, dtype in frame.schema.items() if contains_empty_struct(dtype)]
    if not affected:
        return frame
    text = pl.DataFrame(
        {name: [_json_text(row.get(name)) for row in rows] for name in affected},
        schema=dict.fromkeys(affected, pl.String),
    )
    decoded = _decode_json_text_columns(text, frame.schema, affected)
    return frame.with_columns(decoded.get_columns())


def render_value(value: Any) -> Any:
    """A row value as JSON data: the reserved empty-object field is dropped."""
    if isinstance(value, dict):
        return {
            key: render_value(item)
            for key, item in value.items()
            if not (key == EMPTY_OBJECT_FIELD and item is None)
        }
    if isinstance(value, list):
        return [render_value(item) for item in value]
    return value


class EmptyObjectJsonWriter:
    """File-like writer that drops the reserved field from compact JSON text.

    A member can span two ``write`` calls, so the last bytes are carried over;
    call :meth:`finish` after the producer is done.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._carry = b""

    def write(self, value: bytes) -> int:
        data = bytes(value)
        buffer = self._carry + data
        for pattern, replacement in _JSON_REPLACEMENTS:
            buffer = buffer.replace(pattern, replacement)
        ready = len(buffer) - _JSON_CARRY_BYTES
        if ready > 0:
            self._handle.write(buffer[:ready])
            buffer = buffer[ready:]
        self._carry = buffer
        return len(data)

    def flush(self) -> None:
        self._handle.flush()

    def finish(self) -> None:
        if self._carry:
            self._handle.write(self._carry)
            self._carry = b""
        self._handle.flush()


def render_json_file(path: str, schema: Any) -> None:
    """Rewrite a Polars NDJSON/JSON export in place so stored ``{}`` reads ``{}``."""
    if not any(contains_empty_object_field(dtype) for dtype in schema.values()):
        return
    fd, rendered_path = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as target, open(path, "rb") as source:
            writer = EmptyObjectJsonWriter(target)
            for chunk in iter(lambda: source.read(_COPY_CHUNK_BYTES), b""):
                writer.write(chunk)
            writer.finish()
        os.replace(rendered_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(rendered_path)
