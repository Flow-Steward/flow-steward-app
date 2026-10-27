"""Consumer-owned requirements for candidate smoke checks."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

_SMOKE_ENV_READ = re.compile(r"\bFS_SMOKE_[A-Z0-9_]+\b")
_MANIFEST_SCHEMA = "flow-steward-release-smoke-v1"
_DECLARATION_SCHEMA = "flow-steward-smoke-requirements-v1"
_DECLARATION_FIELDS = frozenset({"schema_version", "group", "sources", "common", "checks"})
_CONSUMER_FIELDS = frozenset(
    {
        "required_env",
        "optional_env",
        "services",
        "scopes",
        "mutable_resources",
        "cleanup_obligations",
    }
)


class RequirementContractError(ValueError):
    """A smoke manifest or consumer declaration is invalid."""


class RequirementDriftError(RequirementContractError):
    """A smoke consumer reads an input absent from its declaration."""


@dataclass(frozen=True)
class ConsumerRequirements:
    """Requirements owned by one smoke consumer."""

    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    mutable_resources: tuple[str, ...] = ()
    cleanup_obligations: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroupRequirementDeclaration:
    """Common and per-check requirements owned beside one smoke group."""

    group: str
    sources: tuple[Path, ...]
    common: ConsumerRequirements
    checks: Mapping[str, ConsumerRequirements]


@dataclass(frozen=True)
class AggregatedRequirements:
    """Stable requirement union for the exact selected checks."""

    check_ids: tuple[str, ...]
    groups: tuple[str, ...]
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...]
    services: tuple[str, ...]
    scopes: tuple[str, ...]
    mutable_resources: tuple[str, ...]
    cleanup_obligations: tuple[str, ...]


def load_group_declarations(root: Path) -> dict[str, GroupRequirementDeclaration]:
    """Load closed declarations from the group directories under one repository root."""

    smoke_root = (root / "scripts" / "smoke").resolve()
    declarations: dict[str, GroupRequirementDeclaration] = {}
    declaration_paths = [smoke_root / "requirements.json"]
    declaration_paths.extend(sorted(smoke_root.glob("group*/requirements.json")))
    for declaration_path in declaration_paths:
        if not declaration_path.is_file():
            continue
        payload = json.loads(declaration_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RequirementContractError(
                f"smoke declaration must be an object: {declaration_path}"
            )
        _reject_unknown_fields(payload, _DECLARATION_FIELDS, context=str(declaration_path))
        if payload.get("schema_version") != _DECLARATION_SCHEMA:
            raise RequirementContractError(
                f"smoke declaration schema is invalid: {declaration_path}"
            )
        group = str(payload.get("group") or "").strip()
        if not re.fullmatch(r"(?:shared|[A-Z])", group) or group in declarations:
            raise RequirementContractError(f"smoke declaration group is invalid: {group}")
        raw_sources = payload.get("sources")
        raw_checks = payload.get("checks")
        if not isinstance(raw_sources, list) or not isinstance(raw_checks, dict):
            raise RequirementContractError(
                f"smoke declaration sources/checks are invalid: {declaration_path}"
            )
        sources: list[Path] = []
        for raw_source in raw_sources:
            if not isinstance(raw_source, str) or not raw_source.strip():
                raise RequirementContractError(
                    f"smoke declaration source is invalid: {declaration_path}"
                )
            source = (declaration_path.parent / raw_source).resolve()
            if not source.is_relative_to(smoke_root) or not source.is_file():
                raise RequirementContractError(f"smoke declaration source is unsafe: {source}")
            sources.append(source)
        checks: dict[str, ConsumerRequirements] = {}
        for raw_check_id, raw_requirements in raw_checks.items():
            check_id = str(raw_check_id or "").strip()
            expected_identity = r"[A-E][0-9]{2}" if group == "shared" else rf"{group}[0-9]{{2}}"
            if not re.fullmatch(expected_identity, check_id):
                raise RequirementContractError(
                    f"smoke check declaration identity is invalid: {check_id}"
                )
            checks[check_id] = _consumer_from_json(
                raw_requirements,
                context=f"{declaration_path}:{check_id}",
            )
        declarations[group] = GroupRequirementDeclaration(
            group=group,
            sources=tuple(sources),
            common=_consumer_from_json(
                payload.get("common"),
                context=f"{declaration_path}:common",
            ),
            checks=checks,
        )
    return declarations


def aggregate_requirements(
    check_manifest: Path,
    declarations: Mapping[str, GroupRequirementDeclaration],
    *,
    profiles: Collection[str],
    check_ids: Collection[str] | None = None,
) -> AggregatedRequirements:
    """Aggregate group-common and selected check-specific requirements."""

    payload = json.loads(check_manifest.read_text(encoding="utf-8"))
    if payload.get("schema_version") != _MANIFEST_SCHEMA:
        raise RequirementContractError("release smoke manifest schema is invalid")
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise RequirementContractError("release smoke manifest checks must be a list")

    selected_ids = set(check_ids) if check_ids is not None else None
    enabled_profiles = set(profiles)
    selected: list[tuple[str, str]] = []
    manifest_ids: set[str] = set()
    for raw_check in checks:
        if not isinstance(raw_check, dict):
            raise RequirementContractError("release smoke manifest check must be an object")
        check_id = str(raw_check.get("id") or "").strip()
        group = str(raw_check.get("group") or "").strip()
        profile = str(raw_check.get("profile") or "").strip()
        if not check_id or not group or not profile or check_id in manifest_ids:
            raise RequirementContractError("release smoke manifest check identity is invalid")
        manifest_ids.add(check_id)
        if profile not in enabled_profiles:
            continue
        if selected_ids is not None and check_id not in selected_ids:
            continue
        selected.append((check_id, group))
    if selected_ids is not None:
        unknown = selected_ids - manifest_ids
        if unknown:
            raise RequirementContractError(
                "selected smoke checks are absent from manifest: " + ", ".join(sorted(unknown))
            )

    consumers: list[ConsumerRequirements] = []
    groups: set[str] = set()
    shared = declarations.get("shared")
    if shared is not None and shared.group != "shared":
        raise RequirementContractError("shared smoke declaration identity is invalid")
    for check_id, group in selected:
        declaration = declarations.get(group)
        if declaration is None or declaration.group != group:
            raise RequirementContractError(f"smoke group declaration is missing: {group}")
        check_requirements = declaration.checks.get(check_id)
        if check_requirements is None:
            raise RequirementContractError(f"smoke check declaration is missing: {check_id}")
        groups.add(group)
        consumers.extend((declaration.common, check_requirements))
        if shared is not None:
            shared_check = shared.checks.get(check_id)
            if shared_check is not None:
                consumers.append(shared.common)
                consumers.append(shared_check)

    return AggregatedRequirements(
        check_ids=tuple(sorted(check_id for check_id, _group in selected)),
        groups=tuple(sorted(groups)),
        required_env=_union(consumers, "required_env"),
        optional_env=_union(consumers, "optional_env"),
        services=_union(consumers, "services"),
        scopes=_union(consumers, "scopes"),
        mutable_resources=_union(consumers, "mutable_resources"),
        cleanup_obligations=_union(consumers, "cleanup_obligations"),
    )


def validate_declaration_coverage(
    root: Path,
    declaration: GroupRequirementDeclaration,
) -> None:
    """Reject smoke environment reads missing from the owning declaration."""

    declared = set(declaration.common.required_env) | set(declaration.common.optional_env)
    for requirements in declaration.checks.values():
        declared.update(requirements.required_env)
        declared.update(requirements.optional_env)

    discovered: set[str] = set()
    resolved_root = root.resolve()
    for source in declaration.sources:
        resolved_source = source.resolve()
        if not resolved_source.is_relative_to(resolved_root):
            raise RequirementContractError(f"smoke requirement source escapes root: {source}")
        discovered.update(_SMOKE_ENV_READ.findall(resolved_source.read_text(encoding="utf-8")))
    missing = discovered - declared
    if missing:
        raise RequirementDriftError(
            f"smoke group {declaration.group} has undeclared environment reads: "
            + ", ".join(sorted(missing))
        )


def _union(
    consumers: Collection[ConsumerRequirements],
    field_name: str,
) -> tuple[str, ...]:
    return tuple(
        sorted({value for consumer in consumers for value in getattr(consumer, field_name)})
    )


def _consumer_from_json(raw: object, *, context: str) -> ConsumerRequirements:
    if not isinstance(raw, dict):
        raise RequirementContractError(f"smoke consumer requirements must be an object: {context}")
    _reject_unknown_fields(raw, _CONSUMER_FIELDS, context=context)
    values: dict[str, tuple[str, ...]] = {}
    for field_name in _CONSUMER_FIELDS:
        raw_values = raw.get(field_name, [])
        if not isinstance(raw_values, list) or any(
            not isinstance(value, str) or not value.strip() for value in raw_values
        ):
            raise RequirementContractError(
                f"smoke consumer field must be a string list: {context}:{field_name}"
            )
        values[field_name] = tuple(sorted(set(raw_values)))
    return ConsumerRequirements(**values)


def _reject_unknown_fields(
    payload: Mapping[str, object],
    allowed: Collection[str],
    *,
    context: str,
) -> None:
    unknown = set(payload) - set(allowed)
    if unknown:
        raise RequirementContractError(
            f"smoke declaration has unknown fields at {context}: " + ", ".join(sorted(unknown))
        )
