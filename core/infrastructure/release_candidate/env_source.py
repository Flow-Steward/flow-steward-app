"""Canonical environment source parsing for release candidates."""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_](?a:\w)*)=(.*)$")


@dataclass(frozen=True)
class EnvironmentSource:
    """Decoded last-wins values and the exact source identity."""

    values: OrderedDict[str, str]
    sha256: str


def read_last_wins(path: Path) -> EnvironmentSource:
    """Read dotenv assignments with deterministic last-assignment precedence."""

    content = path.read_bytes()
    values: OrderedDict[str, str] = OrderedDict()
    for raw_line in content.decode("utf-8").splitlines():
        match = _ASSIGNMENT.match(raw_line.strip())
        if match is None:
            continue
        name, raw_value = match.groups()
        values.pop(name, None)
        values[name] = decode_env_value(raw_value)
    return EnvironmentSource(
        values=values,
        sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
    )


def decode_env_value(value: object) -> str:
    """Decode the simple quoting convention supported by candidate env files."""

    normalized = str(value or "").strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in "'\"":
        return normalized[1:-1]
    return normalized
