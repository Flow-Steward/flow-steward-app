"""Dynamic readiness for projections that require other extensions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.application.extensions.semver import compare_semver

#: ``(missing_ids, required_dependencies) -> details``: one entry per missing
#: extension, as :func:`missing_extension_details` shapes it.
MissingExtensionDescriber = Callable[[list[str], list[dict[str, Any]]], list[dict[str, Any]]]

#: Words of an extension id a person reads as an acronym, not a capitalized word.
_ID_ACRONYMS = frozenset(
    {
        "ai",
        "api",
        "crm",
        "csv",
        "ftp",
        "http",
        "imap",
        "llm",
        "mcp",
        "pdf",
        "s3",
        "sftp",
        "smtp",
        "sql",
    }
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def active_extension_ids_for_account(db: Any, *, account_id: str) -> set[str] | None:
    """Return active extension identities, or None when readiness cannot be resolved."""
    try:
        from core.application.extensions.service_support import (
            get_default_extension_install_store,
        )

        install_store = get_default_extension_install_store(get_logger=lambda: db)
        rows = install_store.list_for_account(
            _text(account_id),
            include_global=True,
            include_inactive_owned=False,
        )
    except Exception:
        return None
    return {
        extension_id for row in rows if (extension_id := _text(getattr(row, "extension_id", "")))
    }


def active_extension_versions_for_account(
    db: Any, *, account_id: str
) -> dict[str, set[str]] | None:
    """Return active install versions visible to an account by extension identity."""
    try:
        from core.application.extensions.service_support import (
            get_default_extension_install_store,
        )

        install_store = get_default_extension_install_store(get_logger=lambda: db)
        rows = install_store.list_for_account(
            _text(account_id),
            include_global=True,
            include_inactive_owned=False,
        )
    except Exception:
        return None
    versions: dict[str, set[str]] = {}
    for row in rows:
        extension_id = _text(getattr(row, "extension_id", ""))
        version = _text(getattr(row, "version", ""))
        if extension_id and version:
            versions.setdefault(extension_id, set()).add(version)
    return versions


def humanized_extension_id(extension_id: str) -> str:
    """``vendor.data-connector-postgres`` as "Data Connector Postgres".

    The last resort for a name: used only when neither the account's installs,
    the extension catalog nor the dependency declaration names the extension.
    """
    text = _text(extension_id)
    local = text.rsplit(".", 1)[-1]
    words = [word for word in local.replace("_", "-").split("-") if word]
    if not words:
        return text
    return " ".join(
        word.upper() if word.lower() in _ID_ACRONYMS else word[:1].upper() + word[1:]
        for word in words
    )


def missing_extension_details(
    missing_ids: list[str],
    required_dependencies: list[dict[str, Any]],
    *,
    installs: dict[str, dict[str, Any]] | None = None,
    catalog_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Describe each missing extension so a client can word it for a person.

    ``state`` says what the person can do about it: ``not_installed`` (install
    it from the catalog), ``installed_disabled`` (turn it on),
    ``installed_incompatible`` (it is on, but not a version this workflow
    accepts) or ``unknown`` when the account's installs could not be read.
    ``display_name`` is the installed release's name, else the catalog's, else
    the dependency declaration's, else the id made readable; never empty.
    """
    by_id = {_text(item.get("extension_id")): item for item in required_dependencies}
    details: list[dict[str, Any]] = []
    for extension_id in missing_ids:
        dependency = by_id.get(extension_id, {})
        install = installs.get(extension_id) if installs is not None else None
        if installs is None:
            state = "unknown"
        elif install is None:
            state = "not_installed"
        elif install.get("active"):
            state = "installed_incompatible"
        else:
            state = "installed_disabled"
        display_name = (
            _text((install or {}).get("display_name"))
            or _text((catalog_names or {}).get(extension_id))
            or _text(dependency.get("display_name"))
            or humanized_extension_id(extension_id)
        )
        details.append(
            {
                "extension_id": extension_id,
                "display_name": display_name,
                "state": state,
                "min_version": _text(dependency.get("min_version")),
                "max_version_exclusive": _text(dependency.get("max_version_exclusive")),
            }
        )
    return details


def _release_display_name(runtime_store: Any, release_id: str) -> str:
    if runtime_store is None or not release_id:
        return ""
    try:
        from core.application.extensions.display_labels import descriptor_display_name

        release = runtime_store.get_release(release_id)
        return descriptor_display_name(getattr(release, "descriptor", None) if release else None)
    except Exception:
        return ""


def _cached_catalog_names() -> dict[str, str]:
    try:
        from core.application.extensions.extension_catalog import cached_extension_catalog_names

        return cached_extension_catalog_names()
    except Exception:
        return {}


def extension_installs_for_account(
    db: Any, *, account_id: str, extension_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Each of ``extension_ids`` the account has installed, on or off, with its name.

    None when the installs cannot be read, so a caller says "unknown" rather
    than wrongly telling the person to install something that is installed.
    """
    wanted = {_text(item) for item in extension_ids if _text(item)}
    try:
        from core.application.extensions.service_support import (
            get_default_extension_install_store,
        )

        install_store = get_default_extension_install_store(get_logger=lambda: db)
        rows = install_store.list_for_account(
            _text(account_id),
            include_global=True,
            include_inactive_owned=True,
        )
    except Exception:
        return None
    runtime_store = None
    try:
        from core.application.extensions.service_support import (
            get_default_extension_runtime_store,
        )

        runtime_store = get_default_extension_runtime_store(get_logger=lambda: db)
    except Exception:
        runtime_store = None
    installs: dict[str, dict[str, Any]] = {}
    for row in rows:
        extension_id = _text(getattr(row, "extension_id", ""))
        if extension_id not in wanted:
            continue
        entry = installs.setdefault(extension_id, {"active": False, "display_name": ""})
        if _text(getattr(row, "state", "")) == "active":
            entry["active"] = True
        if not entry["display_name"]:
            entry["display_name"] = _release_display_name(
                runtime_store, _text(getattr(row, "release_id", ""))
            )
    return installs


def missing_extension_describer(db: Any, *, account_id: str) -> MissingExtensionDescriber:
    """A describer reading this account's installs and the cached catalog, on demand.

    Nothing is read until a projection actually misses an extension, and the
    extension catalog is never fetched here: only an index this process already
    holds names an extension that is not installed.
    """

    def describe(
        missing_ids: list[str], required_dependencies: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return missing_extension_details(
            missing_ids,
            required_dependencies,
            installs=extension_installs_for_account(
                db, account_id=account_id, extension_ids=missing_ids
            ),
            catalog_names=_cached_catalog_names(),
        )

    return describe


def resolve_projection_dependency_availability(
    projection: dict[str, Any] | None,
    *,
    active_extension_ids: set[str] | None,
    active_extension_versions: dict[str, set[str]] | None = None,
    describe_missing: MissingExtensionDescriber | None = None,
) -> dict[str, Any]:
    """Resolve required dependencies dynamically so reinstall restores readiness.

    An unavailable result also carries ``missing_extension_details``, the
    structured reason a client words for a person; ``availability_reason_text``
    stays for the clients that show the sentence as is.
    """
    snapshot = dict(projection or {})
    dependencies = snapshot.get("dependencies")
    dependencies = dict(dependencies) if isinstance(dependencies, dict) else {}
    raw_extensions = dependencies.get("extensions")
    required_dependencies = [
        dict(item)
        for item in (raw_extensions if isinstance(raw_extensions, list) else [])
        if isinstance(item, dict)
        and bool(item.get("required", True))
        and _text(item.get("extension_id"))
    ]
    required_ids = {_text(item.get("extension_id")) for item in required_dependencies}
    marker = snapshot.get("extension_dependency_availability")
    marker = dict(marker) if isinstance(marker, dict) else {}
    marker_missing = {
        extension_id
        for item in (marker.get("missing_extensions") or [])
        if (extension_id := _text(item))
    }
    dependency_ids = required_ids or marker_missing

    unresolved_blocker = bool(marker.get("new_runs_blocked")) and not dependency_ids
    if active_extension_ids is None:
        missing = marker_missing
        if not missing and bool(marker.get("new_runs_blocked")):
            missing = dependency_ids
    elif active_extension_versions is not None and any(
        _text(dependency.get("min_version")) or _text(dependency.get("max_version_exclusive"))
        for dependency in required_dependencies
    ):
        active_ids = set(active_extension_ids)
        missing = dependency_ids - active_ids
        for dependency in required_dependencies:
            dependency_id = _text(dependency.get("extension_id"))
            if (
                not dependency_id
                or dependency_id in missing
                or (
                    not _text(dependency.get("min_version"))
                    and not _text(dependency.get("max_version_exclusive"))
                )
            ):
                continue
            if not _dependency_has_compatible_active_version(
                dependency,
                active_extension_versions.get(dependency_id, set()),
            ):
                missing.add(dependency_id)
    else:
        missing = dependency_ids - set(active_extension_ids)

    if missing:
        ordered = sorted(missing)
        reason_text = _text(marker.get("availability_reason_text"))
        if not reason_text:
            reason_text = missing_dependency_reason(ordered, required_dependencies)
        describe = describe_missing or missing_extension_details
        try:
            details = describe(ordered, required_dependencies)
        except Exception:
            details = missing_extension_details(ordered, required_dependencies)
        return {
            "availability_status": "unavailable",
            "availability_reason_code": "required_extension_uninstalled",
            "availability_reason_text": reason_text,
            "missing_extensions": ordered,
            "missing_extension_details": details,
            "new_runs_blocked": True,
            "visible_in_catalog": bool(marker.get("visible_in_catalog", True)),
        }

    if unresolved_blocker:
        return {
            "availability_status": "unavailable",
            "availability_reason_code": _text(marker.get("availability_reason_code"))
            or "required_extension_uninstalled",
            "availability_reason_text": _text(marker.get("availability_reason_text"))
            or "Required extension availability could not be resolved",
            "missing_extensions": [],
            "new_runs_blocked": True,
            "visible_in_catalog": bool(marker.get("visible_in_catalog", True)),
        }

    return {
        "availability_status": "available",
        "availability_reason_code": "none",
        "availability_reason_text": "",
        "missing_extensions": [],
        "new_runs_blocked": False,
        "visible_in_catalog": True,
    }


def _version_range(dependency: dict[str, Any]) -> str:
    min_version = _text(dependency.get("min_version"))
    max_version = _text(dependency.get("max_version_exclusive"))
    if min_version and max_version:
        return f" {min_version} or later, below {max_version}"
    if min_version:
        return f" {min_version} or later"
    if max_version:
        return f" below {max_version}"
    return ""


def missing_dependency_reason(
    missing_ids: list[str], required_dependencies: list[dict[str, Any]]
) -> str:
    """Say which extensions are missing and what to do, in words a user can act on.

    Names each missing extension with the version range it must have, then
    what to do: install it from the extension catalog, or turn it on if it is
    installed but off. The author's own ``reason`` follows when there is one.
    """
    by_id = {_text(item.get("extension_id")): item for item in required_dependencies}
    names = [
        f"{extension_id}{_version_range(by_id.get(extension_id, {}))}"
        for extension_id in missing_ids
    ]
    if len(names) == 1:
        text = (
            f"Needs the extension {names[0]}, which this account does not have active. "
            "Install it from Extensions > Catalog, or turn it on if it is installed."
        )
    else:
        text = (
            f"Needs the extensions {', '.join(names)}, which this account does not have "
            "active. Install them from Extensions > Catalog, or turn them on if they are "
            "installed."
        )
    reasons = [
        reason
        for extension_id in missing_ids
        if (reason := _text(by_id.get(extension_id, {}).get("reason")))
    ]
    if reasons:
        text = f"{text} {' '.join(reasons)}"
    return text


def _dependency_has_compatible_active_version(
    dependency: dict[str, Any], versions: set[str]
) -> bool:
    min_version = _text(dependency.get("min_version"))
    max_version = _text(dependency.get("max_version_exclusive"))
    for version in versions:
        try:
            if (
                min_version
                and compare_semver(version, min_version, field_name="extension version") < 0
            ):
                continue
            if (
                max_version
                and compare_semver(version, max_version, field_name="extension version") >= 0
            ):
                continue
        except ValueError:
            continue
        return True
    return False


__all__ = [
    "MissingExtensionDescriber",
    "active_extension_ids_for_account",
    "active_extension_versions_for_account",
    "extension_installs_for_account",
    "humanized_extension_id",
    "missing_dependency_reason",
    "missing_extension_describer",
    "missing_extension_details",
    "resolve_projection_dependency_availability",
]
