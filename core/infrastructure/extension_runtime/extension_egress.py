"""Where an extension says it connects: the manifest's optional ``egress`` block.

```yaml
egress:
  hosts:                       # fixed destinations
  - api.telegram.org
  - "*.googleapis.com"
  connection_fields:           # the host a project sets on its connection
  - custom_smtp.host           # <connection_type_id>.<config field>, or just <field>
```

Nothing enforces it in this release: an installed extension is code its owner
trusts. The declaration is shown to the administrator, and it is what a later
egress allowlist (isolation layer 3) will apply. An extension that declares no
``egress`` installs with a visible warning; ``egress: {}`` declares that it
connects nowhere.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

_HOST_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME = re.compile(rf"^(?:\*\.)?(?:{_HOST_LABEL}\.)*{_HOST_LABEL}$")
_FIELD = re.compile(r"^(?:[a-z0-9_][a-z0-9_.-]*\.)?[A-Za-z_](?a:\w)*$")
_ALLOWED_KEYS = frozenset({"hosts", "connection_fields"})
MAX_EGRESS_ENTRIES = 100


def _issue(code: str, message: str, field_path: str, severity: str = "error") -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "file_path": "extension.yaml",
        "field_path": field_path,
        "severity": severity,
    }


def _valid_host(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME.match(value)) and value != "*"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _connection_fields(connection_types: list[Any]) -> dict[str, set[str]]:
    """``connection_type_id`` -> the fields its config schema declares."""
    fields: dict[str, set[str]] = {}
    for row in connection_types:
        if not isinstance(row, dict):
            continue
        type_id = str(row.get("connection_type_id") or "").strip()
        schema = _mapping(row.get("config_schema"))
        properties = _mapping(schema.get("properties"))
        if type_id:
            fields[type_id] = {str(name) for name in properties}
    return fields


def validate_egress(
    manifest: dict[str, Any], connection_types: list[Any]
) -> tuple[dict[str, list[str]] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """``(declared egress or None, errors, warnings)`` for a manifest."""
    errors: list[dict[str, Any]] = []
    if "egress" not in manifest or manifest.get("egress") is None:
        warning = _issue(
            "egress_undeclared",
            "The extension does not declare where it connects (egress). It can still "
            "reach any address; declare egress.hosts and egress.connection_fields so "
            "an administrator can see its destinations.",
            "egress",
            "warning",
        )
        return None, errors, [warning]
    raw = manifest.get("egress")
    if not isinstance(raw, dict):
        errors.append(_issue("egress_invalid", "egress must be a mapping", "egress"))
        return None, errors, []
    for key in sorted(set(raw) - _ALLOWED_KEYS):
        errors.append(
            _issue(
                "egress_invalid",
                f"egress.{key} is not supported (hosts, connection_fields)",
                f"egress.{key}",
            )
        )
    declared: dict[str, list[str]] = {"hosts": [], "connection_fields": []}
    for key in ("hosts", "connection_fields"):
        values = raw.get(key, [])
        if not isinstance(values, list):
            errors.append(_issue("egress_invalid", f"egress.{key} must be a list", f"egress.{key}"))
            continue
        if len(values) > MAX_EGRESS_ENTRIES:
            errors.append(
                _issue(
                    "egress_invalid",
                    f"egress.{key} lists more than {MAX_EGRESS_ENTRIES} entries",
                    f"egress.{key}",
                )
            )
            continue
        for value in values:
            text = str(value or "").strip()
            declared[key].append(text.lower() if key == "hosts" else text)
    known = _connection_fields(connection_types)
    all_fields = set().union(*known.values()) if known else set()
    for index, host in enumerate(declared["hosts"]):
        if not _valid_host(host):
            errors.append(
                _issue(
                    "egress_host_invalid",
                    f"'{host}' is not a host name: give a name such as api.example.com or "
                    "*.example.com, without a scheme, port or path",
                    f"egress.hosts[{index}]",
                )
            )
    for index, field in enumerate(declared["connection_fields"]):
        path = f"egress.connection_fields[{index}]"
        if not _FIELD.match(field):
            errors.append(
                _issue("egress_connection_field_invalid", f"'{field}' is not a field name", path)
            )
            continue
        type_id, _, name = field.rpartition(".")
        if type_id:
            if type_id not in known:
                errors.append(
                    _issue(
                        "egress_connection_field_unknown",
                        f"The extension declares no connection type '{type_id}'",
                        path,
                    )
                )
            elif name not in known[type_id]:
                errors.append(
                    _issue(
                        "egress_connection_field_unknown",
                        f"Connection type '{type_id}' has no config field '{name}'",
                        path,
                    )
                )
        elif name not in all_fields:
            errors.append(
                _issue(
                    "egress_connection_field_unknown",
                    f"No connection type of the extension has a config field '{name}'",
                    path,
                )
            )
    return declared, errors, []


__all__ = ["MAX_EGRESS_ENTRIES", "validate_egress"]
