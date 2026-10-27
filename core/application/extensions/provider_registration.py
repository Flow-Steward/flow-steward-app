"""Register an extension-backed provider from what the extension already declares.

A notification or task provider used to be a row in the platform's bootstrap seed,
which meant a clean platform shipped knowing that a particular extension exists.
It does not need to: an extension already declares, in typed manifest fields, that
it *is* a provider (`kind`), what it is called (`extension_id`, `display_name`) and
what it can do (`features`). Enabling it is the moment that becomes true, so that
is where the row is written.

Nothing here is specific to any extension, and no new manifest surface is added.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from core.application.extensions.external_effects import declared_features

#: A manifest `kind` that names a provider role. Anything else — a tool provider
#: that is not offered as a configurable provider — registers nothing.
PROVIDER_KIND_SUFFIX = "_provider"


def _text(value: Any) -> str:
    return str(value or "").strip()


#: The typed fields an install record and a release descriptor both carry. The
#: fallback below reads only these, so it cannot come to depend on anything a
#: particular extension happens to put in its record.
INSTALL_RECORD_PROVIDER_FIELDS = (
    "extension_id",
    "extension_key",
    "kind",
    "display_name",
    "description",
    "features",
)


def typed_provider_source(record: Any) -> dict[str, Any]:
    """Return only the declared fields a provider row is built from.

    Passing a whole install record around invites reading whatever else happens
    to be in it. This narrows it to the manifest-typed fields, so the fallback is
    a projection of the contract rather than of one store's row shape.
    """
    payload = record if isinstance(record, dict) else {}
    return {key: payload[key] for key in INSTALL_RECORD_PROVIDER_FIELDS if key in payload}


def provider_row_for_extension(descriptor: Any) -> dict[str, Any] | None:
    """Return the provider row this extension declares, or None if it declares none."""
    payload = descriptor if isinstance(descriptor, dict) else {}
    compiled = payload.get("compiled_manifest")
    if isinstance(compiled, dict):
        payload = {**compiled, **payload}
    extension_id = _text(payload.get("extension_id") or payload.get("extension_key"))
    kind = _text(payload.get("kind")).lower()
    if not extension_id or not kind.endswith(PROVIDER_KIND_SUFFIX):
        return None
    return {
        "provider_key": extension_id,
        "provider_type_key": kind,
        "display_name": _text(payload.get("display_name")) or extension_id,
        "description": _text(payload.get("description")),
        "implementation_kind": "extension",
        "integration_key": extension_id,
        "config": {"extension_key": extension_id},
        "features": declared_features(payload),
        "enabled": True,
    }


def provider_descriptor_for_extension(
    logger: Any,
    *,
    extension_id: str,
    release_descriptor: Any = None,
) -> dict[str, Any]:
    """Resolve provider metadata from a release or the installed extension row."""
    descriptor = dict(release_descriptor) if isinstance(release_descriptor, dict) else {}
    if provider_row_for_extension(descriptor) is not None:
        return descriptor
    reader = getattr(logger, "get_extension", None)
    if not callable(reader):
        return descriptor
    typed = typed_provider_source(reader(extension_id))
    return typed or descriptor


class ProviderRegistrationError(RuntimeError):
    """The provider catalog could not be brought in line with an extension's state."""


def register_provider_from_extension(logger: Any, descriptor: Any) -> dict[str, Any] | None:
    """Write the declared provider row, and return it. No declaration, no write.

    This is **not** optional. Without the row a notification route cannot be saved
    or delivered, so an extension that enabled but whose provider is missing is an
    extension that appears available and silently does nothing. A write that fails
    raises, and the caller rolls the enable back.
    """
    row = provider_row_for_extension(descriptor)
    if row is None:
        return None
    if logger is None or not hasattr(logger, "save_provider"):
        raise ProviderRegistrationError(
            f"cannot register provider '{row['provider_key']}': no provider catalog is available"
        )
    try:
        logger.save_provider(dict(row))
    except Exception as exc:  # re-raised with the identity that failed
        raise ProviderRegistrationError(
            f"could not register provider '{row['provider_key']}': {exc}"
        ) from exc
    return row


def set_provider_enabled_for_extension(logger: Any, descriptor: Any, *, enabled: bool) -> bool:
    """Follow an extension's state with its provider row.

    A disabled extension whose provider stays enabled is a route the operator can
    still save and that can never deliver.
    """
    row = provider_row_for_extension(descriptor)
    if row is None:
        return False
    if logger is None or not hasattr(logger, "save_provider"):
        raise ProviderRegistrationError(
            f"cannot update provider '{row['provider_key']}': no provider catalog is available"
        )
    row["enabled"] = enabled
    try:
        logger.save_provider(dict(row))
    except Exception as exc:  # re-raised with the identity that failed
        raise ProviderRegistrationError(
            f"could not update provider '{row['provider_key']}': {exc}"
        ) from exc
    return True


def disable_provider_row_cur(cur: Any, descriptor: Any) -> bool:
    """Disable the declared provider row on an open cursor, and say whether it applied.

    Written through the caller's cursor on purpose. An uninstall commits its
    removal in one transaction, and a provider row disabled outside it can end up
    on the wrong side of a later failure — extension gone, provider enabled, which
    is a route the operator can save and that can never deliver. Inside, the two
    move together or neither does.
    """
    row = provider_row_for_extension(descriptor)
    if row is None or cur is None:
        return False
    cur.execute(
        "UPDATE providers SET enabled = false, updated_at = now() WHERE provider_key = %s",
        (row["provider_key"],),
    )
    return True


def provider_state_snapshot(logger: Any, descriptor: Any) -> dict[str, Any] | None:
    """Capture the catalog row as it stands, so a failed change can be undone.

    `None` means "there was no row": restoring that state means disabling the one
    that was written, because the catalog has no delete and a leftover enabled row
    is a route that can be saved and never delivered.

    A read that *fails* is not the same as a row that is absent, and returning
    `None` for it would make the rollback disable a row that may have been enabled
    all along. It raises, so the caller never starts a change it cannot undo.
    """
    row = provider_row_for_extension(descriptor)
    if row is None:
        return None
    if logger is None or not hasattr(logger, "get_provider"):
        raise ProviderRegistrationError(
            f"cannot read provider '{row['provider_key']}': no provider catalog is available"
        )
    try:
        existing = logger.get_provider(row["provider_key"])
    except Exception as exc:  # re-raised with the identity that failed
        raise ProviderRegistrationError(
            f"could not read provider '{row['provider_key']}': {exc}"
        ) from exc
    return dict(existing) if isinstance(existing, dict) else None


def restore_provider_state(
    logger: Any,
    descriptor: Any,
    snapshot: dict[str, Any] | None,
    *,
    on_error: Callable[[str], None] | None = None,
) -> bool:
    """Put the catalog back the way `provider_state_snapshot` found it.

    Returns whether the catalog now matches the snapshot. It does not raise: this
    runs while another failure is already unwinding, and replacing that error with
    this one hides what actually went wrong. But it must not be silent either — a
    failed restore leaves the catalog disagreeing with the extension, which is the
    state this whole mechanism exists to prevent — so the caller is told through
    `on_error` and the return value.
    """
    row = provider_row_for_extension(descriptor)
    if row is None:
        return True
    if logger is None or not hasattr(logger, "save_provider"):
        if on_error is not None:
            on_error(f"cannot restore provider '{row['provider_key']}': no catalog is available")
        return False
    if snapshot is None:
        row["enabled"] = False
    else:
        row = {**row, **{key: snapshot[key] for key in ("enabled",) if key in snapshot}}
    try:
        logger.save_provider(dict(row))
    except Exception as exc:  # reported, not raised over the failure being unwound
        if on_error is not None:
            on_error(f"could not restore provider '{row['provider_key']}': {exc}")
        return False
    return True


def reconcile_extension_providers(
    logger: Any,
    installed: Iterable[tuple[Any, bool]],
) -> list[dict[str, Any]]:
    """Bring the catalog in line with what is installed, and report what changed.

    Run at startup. A row is derived state, so it can drift: a release upgraded
    while the platform was down, a catalog restored from an older backup, a write
    lost to a crash between the install and the row. Reconciling makes the catalog
    a function of what is installed rather than of what happened to succeed.

    `installed` yields `(descriptor, enabled)` pairs.
    """
    reconciled: list[dict[str, Any]] = []
    for descriptor, enabled in installed:
        row = provider_row_for_extension(descriptor)
        if row is None:
            continue
        row["enabled"] = bool(enabled)
        if logger is None or not hasattr(logger, "save_provider"):
            raise ProviderRegistrationError(
                f"cannot reconcile provider '{row['provider_key']}': no catalog is available"
            )
        try:
            logger.save_provider(dict(row))
        except Exception as exc:  # re-raised with the identity that failed
            raise ProviderRegistrationError(
                f"could not reconcile provider '{row['provider_key']}': {exc}"
            ) from exc
        reconciled.append(row)
    return reconciled


def reconcile_extension_providers_for_logger(logger: Any) -> list[dict[str, Any]]:
    """Reconcile the catalog against every extension the platform has installed.

    Called at startup. `list_extensions` reports each installed extension with the
    `kind` and `enabled` state the platform holds, which is exactly what a provider
    row is derived from — so the catalog becomes a function of what is installed
    rather than of which writes happened to succeed.
    """
    if logger is None or not hasattr(logger, "list_extensions"):
        return []
    try:
        rows = logger.list_extensions() or []
    except Exception as exc:  # the catalog cannot be trusted if the inventory fails
        raise ProviderRegistrationError(f"could not list installed extensions: {exc}") from exc
    installed = [(row, bool(row.get("enabled", False))) for row in rows if isinstance(row, dict)]
    return reconcile_extension_providers(logger, installed)


__all__ = [
    "PROVIDER_KIND_SUFFIX",
    "ProviderRegistrationError",
    "disable_provider_row_cur",
    "provider_descriptor_for_extension",
    "provider_row_for_extension",
    "provider_state_snapshot",
    "reconcile_extension_providers",
    "reconcile_extension_providers_for_logger",
    "register_provider_from_extension",
    "restore_provider_state",
    "set_provider_enabled_for_extension",
    "typed_provider_source",
]
