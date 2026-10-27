"""Canonical JSON and database-row values for extension OAuth repositories."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _json_payload(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        payload = dict(row)
    elif isinstance(row, Mapping):
        payload = dict(row.items())
    elif isinstance(row, (tuple, list)):
        columns = [str(item[0]) for item in (cursor.description or [])]
        if len(columns) != len(row):
            raise TypeError("OAuth repository row does not match its cursor description")
        payload = {columns[index]: row[index] for index in range(len(columns))}
    else:
        raise TypeError("Unsupported OAuth repository row type")

    json_keys = {
        "non_secret_config",
        "secret_refs",
        "intent_payload",
        "requested_scopes",
        "oauth_requested_scopes",
        "oauth_granted_scopes",
    }
    if "connection_id" not in payload:
        json_keys.add("last_error")
    for key in json_keys:
        value = payload.get(key)
        if isinstance(value, str):
            payload[key] = json.loads(value)
    return payload
