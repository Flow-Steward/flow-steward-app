"""Grants for privileged extension runtime resources and mutations.

Every extension is treated the same way: there is no system, official or
third-party tier and no host policy file naming extensions. An extension
declares the rights it needs over its OWN data in its manifest
(``runtime_grants``), and the host accepts only what ``own_runtime_grants``
allows: grants scoped to the owning extension and to provider keys equal to the
extension's own id. No declaration can reach another extension's connections,
secrets, notification routes or deliveries.
"""

from __future__ import annotations

from typing import Any

RUNTIME_GRANT_FORBIDDEN = "runtime_grant_forbidden"
OWNING_EXTENSION_SCOPE = "owning_extension"

RESOURCE_GRANT_KINDS: frozenset[str] = frozenset(
    {
        "project_connection",
        "provider_secrets",
        "notification_deliveries",
        "notification_routes",
        "callback_registrations",
    }
)
MUTATION_GRANT_KINDS: frozenset[str] = frozenset(
    {
        "project_connection_upsert",
        "project_connection_secrets_save",
        "notification_delivery_log",
        "notification_routes_save",
    }
)
_KINDS_BY_SECTION: dict[str, frozenset[str]] = {
    "resources": RESOURCE_GRANT_KINDS,
    "mutations": MUTATION_GRANT_KINDS,
}
# Kinds whose grant is always limited to the owning extension; scope defaults to it.
_SCOPE_DEFAULT_KINDS: frozenset[str] = frozenset(
    {
        "project_connection",
        "callback_registrations",
        "project_connection_upsert",
        "project_connection_secrets_save",
    }
)
# Kinds keyed by a provider key; it defaults to (and may only be) the extension's own id.
_PROVIDER_KEY_DEFAULT_KINDS: frozenset[str] = frozenset(
    {
        "provider_secrets",
        "notification_deliveries",
        "notification_routes",
        "project_connection_secrets_save",
        "notification_delivery_log",
        "notification_routes_save",
    }
)
_STRING_LIST_FIELDS: tuple[str, ...] = ("secret_keys", "connection_type_ids")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _problem(section: str, kind: str, message: str) -> dict[str, Any]:
    field_path = f"runtime_grants.{section}.{kind}" if kind else f"runtime_grants.{section}"
    return {"code": RUNTIME_GRANT_FORBIDDEN, "field_path": field_path, "message": message}


def _string_list(value: Any) -> list[str] | None:
    """Return a deduplicated, order-preserving list of non-empty strings, or None."""
    if not isinstance(value, list):
        return None
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        text = item.strip()
        if text not in seen:
            seen.append(text)
    return seen


def _own_grant(
    section: str,
    kind: str,
    declared: Any,
    extension_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        return None, _problem(section, kind, f"Grant '{kind}' must be an object.")
    grant = dict(declared)

    if "scope" in grant:
        scope = grant.get("scope")
        if not isinstance(scope, str) or scope.strip() != OWNING_EXTENSION_SCOPE:
            return None, _problem(
                section,
                kind,
                f"Grant '{kind}' scope must be '{OWNING_EXTENSION_SCOPE}'.",
            )
        grant["scope"] = OWNING_EXTENSION_SCOPE
    elif kind in _SCOPE_DEFAULT_KINDS:
        grant["scope"] = OWNING_EXTENSION_SCOPE

    if "provider_key" in grant:
        provider_key = grant.get("provider_key")
        if (
            not isinstance(provider_key, str)
            or provider_key.strip().lower() != extension_id.lower()
        ):
            return None, _problem(
                section,
                kind,
                f"Grant '{kind}' provider_key must be the extension's own id '{extension_id}'.",
            )
        grant["provider_key"] = extension_id
    elif kind in _PROVIDER_KEY_DEFAULT_KINDS:
        grant["provider_key"] = extension_id

    for field in _STRING_LIST_FIELDS:
        if field not in grant:
            continue
        values = _string_list(grant.get(field))
        if values is None:
            return None, _problem(
                section,
                kind,
                f"Grant '{kind}' {field} must be a list of non-empty strings.",
            )
        grant[field] = values
    return grant, None


def own_runtime_grants(
    declared: Any,
    extension_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Accept the grants an extension declared over its own namespace only.

    Returns ``(accepted, problems)``. ``accepted`` has the shape
    ``{"resources": {...}, "mutations": {...}}`` (sections only when non-empty).
    Every declaration that names an unknown kind, a scope other than the owning
    extension, a provider key other than the extension's own id, or a malformed
    list is dropped and reported as a ``runtime_grant_forbidden`` problem.
    """
    canonical_id = str(extension_id or "").strip()
    accepted: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []
    if declared is None or declared == {}:
        return accepted, problems
    if not isinstance(declared, dict):
        return accepted, [
            {
                "code": RUNTIME_GRANT_FORBIDDEN,
                "field_path": "runtime_grants",
                "message": "runtime_grants must be an object with resources and mutations.",
            }
        ]
    for section in sorted(set(declared) - set(_KINDS_BY_SECTION)):
        problems.append(_problem(str(section), "", f"Unknown runtime_grants section '{section}'."))
    for section, allowed_kinds in _KINDS_BY_SECTION.items():
        if section not in declared:
            continue
        rows = declared.get(section)
        if rows is None:
            continue
        if not isinstance(rows, dict):
            problems.append(_problem(section, "", f"runtime_grants.{section} must be an object."))
            continue
        section_grants: dict[str, Any] = {}
        for raw_kind, raw_grant in rows.items():
            kind = str(raw_kind or "").strip()
            if kind not in allowed_kinds:
                problems.append(
                    _problem(section, kind, f"Unknown runtime grant kind '{kind}' in {section}.")
                )
                continue
            if not canonical_id:
                problems.append(
                    _problem(section, kind, "A runtime grant requires a known extension id.")
                )
                continue
            grant, problem = _own_grant(section, kind, raw_grant, canonical_id)
            if problem is not None:
                problems.append(problem)
                continue
            if grant is not None:
                section_grants[kind] = grant
        if section_grants:
            accepted[section] = section_grants
    return accepted, problems


def _extension_id(payload: dict[str, Any]) -> str:
    compiled = _as_dict(payload.get("compiled_manifest"))
    return str(
        payload.get("extension_key")
        or payload.get("extension_id")
        or compiled.get("extension_id")
        or ""
    ).strip()


def trusted_runtime_grants(
    extension: dict[str, Any] | None,
    *,
    extension_id: str | None = None,
) -> dict[str, Any]:
    """Return the extension's accepted grants over its own namespace.

    ``extension_id`` is the host's authoritative id for the invocation; when it is
    given it decides the namespace, otherwise the record's own id does.

    Grants come from the extension's manifest ``runtime_grants`` (carried as
    ``install_metadata.runtime_grants`` once compiled) and are re-limited by
    ``own_runtime_grants`` on every read, so a grant over another extension's
    namespace is never returned whatever the record holds.
    """
    payload = _as_dict(extension)
    metadata = _as_dict(payload.get("install_metadata"))
    if "runtime_grants" in metadata:
        declared = metadata.get("runtime_grants")
    else:
        compiled = _as_dict(payload.get("compiled_manifest"))
        declared = compiled.get("runtime_grants", payload.get("runtime_grants"))
    namespace = str(extension_id or "").strip() or _extension_id(payload)
    accepted, _problems = own_runtime_grants(declared, namespace)
    return accepted


def trusted_resource_grant(
    extension: dict[str, Any] | None,
    resource_kind: str,
    *,
    extension_id: str | None = None,
) -> dict[str, Any] | None:
    grants = trusted_runtime_grants(extension, extension_id=extension_id)
    grant = _as_dict(_as_dict(grants.get("resources")).get(resource_kind))
    return grant or None


def trusted_mutation_grant(
    extension: dict[str, Any] | None,
    mutation_kind: str,
    *,
    extension_id: str | None = None,
) -> dict[str, Any] | None:
    grants = trusted_runtime_grants(extension, extension_id=extension_id)
    grant = _as_dict(_as_dict(grants.get("mutations")).get(mutation_kind))
    return grant or None


__all__ = [
    "MUTATION_GRANT_KINDS",
    "OWNING_EXTENSION_SCOPE",
    "RESOURCE_GRANT_KINDS",
    "RUNTIME_GRANT_FORBIDDEN",
    "own_runtime_grants",
    "trusted_mutation_grant",
    "trusted_resource_grant",
    "trusted_runtime_grants",
]
