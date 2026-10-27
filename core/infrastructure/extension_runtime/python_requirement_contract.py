"""Canonical manifest contract for pinned and hashed Python requirements."""

from __future__ import annotations

import re
from typing import Any

try:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except ImportError:
    from pip._vendor.packaging.specifiers import InvalidSpecifier, SpecifierSet
    from pip._vendor.packaging.utils import canonicalize_name
    from pip._vendor.packaging.version import InvalidVersion, Version


PYTHON_REQUIREMENTS_FIELD = "python_requirements"


PYTHON_DEPS_REQUIREMENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _issue(
    *,
    severity: str,
    code: str,
    message: str,
    file_path: str = "",
    field_path: str = "",
    hint: str = "",
) -> dict[str, str]:
    payload = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if file_path:
        payload["file_path"] = file_path
    if field_path:
        payload["field_path"] = field_path
    if hint:
        payload["hint"] = hint
    return payload


def _is_version_specifier(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        specifier_set = SpecifierSet(raw)
    except InvalidSpecifier:
        specifier_set = None
    if specifier_set is not None:
        for _specifier in specifier_set:
            return True
    try:
        wildcard_exact = SpecifierSet(f"=={raw}")
    except InvalidSpecifier:
        return False
    return any("*" in str(specifier.version) for specifier in wildcard_exact)


def normalize_python_requirements(
    value: Any,
    *,
    manifest_path: str = "",
    field_path: str = PYTHON_REQUIREMENTS_FIELD,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if value in (None, ""):
        return [], []
    issues: list[dict[str, str]] = []
    if not isinstance(value, list):
        return [], [
            _issue(
                severity="error",
                code="invalid_python_requirements",
                message="python_requirements must be a list",
                file_path=manifest_path,
                field_path=field_path,
            )
        ]

    normalized: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for idx, raw_item in enumerate(value):
        item_path = f"{field_path}[{idx}]"
        if not isinstance(raw_item, dict):
            issues.append(
                _issue(
                    severity="error",
                    code="invalid_python_requirement",
                    message="python_requirements entries must be objects",
                    file_path=manifest_path,
                    field_path=item_path,
                )
            )
            continue
        unknown_keys = sorted(set(raw_item) - {"name", "version", "hashes"})
        for unknown_key in unknown_keys:
            issues.append(
                _issue(
                    severity="error",
                    code="unknown_python_requirement_field",
                    message=f"Unsupported python_requirements field '{unknown_key}'",
                    file_path=manifest_path,
                    field_path=f"{item_path}.{unknown_key}",
                )
            )

        raw_name = str(raw_item.get("name") or "").strip()
        name = str(canonicalize_name(raw_name)) if raw_name else ""
        if not raw_name:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_python_requirement_name",
                    message="python_requirements entries require name",
                    file_path=manifest_path,
                    field_path=f"{item_path}.name",
                )
            )
        elif raw_name != name:
            issues.append(
                _issue(
                    severity="error",
                    code="python_requirement_name_not_normalized",
                    message=f"Python requirement name '{raw_name}' must be normalized as '{name}'",
                    file_path=manifest_path,
                    field_path=f"{item_path}.name",
                    hint="Use the PEP 503-normalized lowercase package name.",
                )
            )
        elif name in seen_names:
            issues.append(
                _issue(
                    severity="error",
                    code="duplicate_python_requirement",
                    message=f"Duplicate python requirement '{name}'",
                    file_path=manifest_path,
                    field_path=f"{item_path}.name",
                )
            )
        seen_names.add(name)

        raw_version = str(raw_item.get("version") or "").strip()
        version = raw_version
        if not raw_version:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_python_requirement_version",
                    message="python_requirements entries require version",
                    file_path=manifest_path,
                    field_path=f"{item_path}.version",
                )
            )
        else:
            try:
                version = str(Version(raw_version))
            except InvalidVersion as exc:
                if _is_version_specifier(raw_version):
                    issues.append(
                        _issue(
                            severity="error",
                            code="python_requirement_version_not_exact",
                            message=(
                                "python_requirements version must be an exact version, "
                                "not a specifier"
                            ),
                            file_path=manifest_path,
                            field_path=f"{item_path}.version",
                        )
                    )
                else:
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_python_requirement_version",
                            message=f"Invalid python requirement version '{raw_version}': {exc}",
                            file_path=manifest_path,
                            field_path=f"{item_path}.version",
                        )
                    )

        hashes: list[str] = []
        raw_hashes = raw_item.get("hashes")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            issues.append(
                _issue(
                    severity="error",
                    code="missing_python_requirement_hashes",
                    message="python_requirements entries require at least one sha256 hash",
                    file_path=manifest_path,
                    field_path=f"{item_path}.hashes",
                )
            )
        else:
            seen_hashes: set[str] = set()
            for hash_idx, raw_hash in enumerate(raw_hashes):
                digest = str(raw_hash or "").strip().lower()
                if not PYTHON_DEPS_REQUIREMENT_HASH_RE.match(digest):
                    issues.append(
                        _issue(
                            severity="error",
                            code="invalid_python_requirement_hash",
                            message=(
                                "python_requirements hashes must use "
                                "sha256:<64 lowercase hex chars>"
                            ),
                            file_path=manifest_path,
                            field_path=f"{item_path}.hashes[{hash_idx}]",
                        )
                    )
                    continue
                if digest not in seen_hashes:
                    hashes.append(digest)
                    seen_hashes.add(digest)

        if name and version and hashes:
            normalized.append(
                {
                    "name": name,
                    "version": version,
                    "hashes": hashes,
                }
            )
    return normalized, issues


def python_requirements_by_name(requirements: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("name") or "").strip(): dict(item) for item in requirements}
