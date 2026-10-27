from __future__ import annotations

from typing import Any


def composable_to_debug_sql(query: Any) -> str:
    """Render psycopg composables into a readable SQL-like string for assertions."""
    obj = getattr(query, "_obj", None)
    if obj is None:
        return str(query)

    query_type = type(query).__name__
    if query_type == "Identifier":
        parts = obj if isinstance(obj, tuple) else (obj,)
        return ".".join(str(part) for part in parts)
    if query_type == "Literal":
        return repr(obj)
    if query_type == "Placeholder":
        name = str(obj or "").strip()
        return f"%({name})s" if name else "%s"
    if isinstance(obj, (list, tuple)):
        return "".join(composable_to_debug_sql(part) for part in obj)
    return str(obj)
