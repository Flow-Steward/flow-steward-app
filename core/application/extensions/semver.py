from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SEMVER_RE = re.compile(
    r"^(?P<core>\d+(?:\.\d+){0,2})(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def require_nonempty_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def normalize_semver(value: Any, field_name: str) -> str:
    normalized = require_nonempty_text(value, field_name)
    if not _SEMVER_RE.match(normalized):
        raise ValueError(f"{field_name} must be a semantic version")
    return normalized


@dataclass(frozen=True)
class SemverValue:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()


def parse_semver(value: Any, field_name: str) -> SemverValue:
    normalized = normalize_semver(value, field_name)
    match = _SEMVER_RE.match(normalized)
    if match is None:
        raise ValueError(f"{field_name} must be a semantic version")
    parts = [int(part) for part in match.group("core").split(".")]
    while len(parts) < 3:
        parts.append(0)
    prerelease_raw = str(match.group("prerelease") or "").strip()
    prerelease = tuple(part for part in prerelease_raw.split(".") if part) if prerelease_raw else ()
    return SemverValue(
        major=parts[0],
        minor=parts[1],
        patch=parts[2],
        prerelease=prerelease,
    )


def compare_semver(left: Any, right: Any, *, field_name: str = "version") -> int:
    left_value = parse_semver(left, field_name)
    right_value = parse_semver(right, field_name)
    left_core = (left_value.major, left_value.minor, left_value.patch)
    right_core = (right_value.major, right_value.minor, right_value.patch)
    if left_core < right_core:
        return -1
    if left_core > right_core:
        return 1
    return _compare_prerelease(left_value.prerelease, right_value.prerelease)


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left and not right:
        return 0
    if not left:
        return 1
    if not right:
        return -1
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_is_num = left_part.isdigit()
        right_is_num = right_part.isdigit()
        if left_is_num and right_is_num:
            left_num = int(left_part)
            right_num = int(right_part)
            if left_num < right_num:
                return -1
            if left_num > right_num:
                return 1
            continue
        if left_is_num and not right_is_num:
            return -1
        if right_is_num and not left_is_num:
            return 1
        if left_part < right_part:
            return -1
        if left_part > right_part:
            return 1
    if len(left) < len(right):
        return -1
    if len(left) > len(right):
        return 1
    return 0
