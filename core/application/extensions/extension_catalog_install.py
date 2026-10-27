"""Join the static extension catalog with local installs, and install from it.

Two responsibilities, both thin:

* compute a per-card display state by joining catalog entries with the existing
  account-scoped install records — no new persistence, no new columns;
* fetch one catalog release archive, verify its declared size and SHA-256, and
  hand the verified bytes to the existing extension installer exactly once.

Nothing here reimplements install, manifest validation, compatibility,
permission, health, enable/disable, uninstall, or upgrade. Updating to a newer
catalog release goes through the same archive installer as an uploaded ZIP,
which already updates an existing install in place; this module only refuses
a same-version reinstall and a downgrade before anything is downloaded.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from typing import Any

import yaml

from core.application.extensions.extension_catalog import (
    CAPABILITY_LABELS,
    PERMISSION_LABELS,
    ExtensionCatalogError,
    ExtensionCatalogItem,
    extension_catalog_availability,
    fetch_extension_release_artifact,
    load_extension_catalog,
)
from core.application.extensions.install import InstallState
from core.application.extensions.semver import compare_semver
from core.application.management.scope import ScopeContext, normalize_scope
from core.infrastructure.config.version import get_app_version
from core.infrastructure.static_catalog.http_fetch import (
    CatalogArtifactMismatchError,
    CatalogFetchError,
)

#: The display states the Marketplace card can be in. ``update_available`` is
#: the only one the extension page turns into an action (Update).
STATE_INSTALL = "install"
STATE_INSTALLED = "installed"
STATE_UPDATE_AVAILABLE = "update_available"
STATE_INSTALLED_NEWER = "installed_newer"
STATE_INSTALLED_DISABLED = "installed_disabled"
STATE_INCOMPATIBLE = "incompatible"

#: States that assert the catalog release itself could be installed. Only these
#: are overridden by an incompatible release — the other three describe what is
#: installed locally and stay true whatever the catalog offers.
_INSTALLABLE_STATES = frozenset({STATE_INSTALL, STATE_UPDATE_AVAILABLE})


def catalog_release_compatible(item: ExtensionCatalogItem, *, platform_version: str) -> bool:
    """Whether this deployment's version falls inside the entry's declared range.

    Bounds are inclusive at both ends and mirror the installer's own
    ``platform_min`` / ``platform_max`` reading, so the badge and the installer
    cannot disagree. An entry that declares no range is compatible. An entry that
    ships per-target archives and none for this host is never compatible.
    """
    if not item.available_for_host:
        return False
    try:
        if item.compatible_min_version and (
            compare_semver(platform_version, item.compatible_min_version, field_name="platform") < 0
        ):
            return False
        if item.compatible_max_version and (
            compare_semver(platform_version, item.compatible_max_version, field_name="platform") > 0
        ):
            return False
    except ValueError:
        return False
    return True


def catalog_display_state(
    item: ExtensionCatalogItem,
    install: Any | None,
    *,
    platform_version: str,
) -> str:
    """Compute one card's state from the catalog entry and the local install."""
    compatible = catalog_release_compatible(item, platform_version=platform_version)
    state = _state_from_install(item, install)
    if not compatible and state in _INSTALLABLE_STATES:
        return STATE_INCOMPATIBLE
    return state


def _installed_version_comparison(install: Any, catalog_version: str) -> int | None:
    """-1/0/1 for the installed version against the catalog's; None when unreadable."""
    local_version = str(getattr(install, "version", "") or "").strip()
    try:
        # SemVer, not string order: '1.9.0' is older than '1.10.0', which a
        # lexical comparison gets backwards.
        return compare_semver(local_version, catalog_version, field_name="extension version")
    except ValueError:
        return None


def _state_from_install(item: ExtensionCatalogItem, install: Any | None) -> str:
    if install is None:
        return STATE_INSTALL
    comparison = _installed_version_comparison(install, item.version)
    if comparison is not None and comparison < 0:
        # A turned-off install can be updated too; it stays off afterwards.
        return STATE_UPDATE_AVAILABLE
    if str(getattr(install, "state", "") or "") == str(InstallState.disabled):
        return STATE_INSTALLED_DISABLED
    if comparison is None:
        # An install whose recorded version cannot be read is still installed;
        # claiming an update is available would be a guess.
        return STATE_INSTALLED
    if comparison > 0:
        return STATE_INSTALLED_NEWER
    return STATE_INSTALLED


def active_install_by_install_id(
    install_store: Any,
    *,
    install_id: str,
    account_id: str,
) -> Any | None:
    """Return the account-visible *active* install with this id, if any."""
    normalized = str(install_id or "").strip()
    if not normalized:
        return None
    records = install_store.list_for_account(
        account_id,
        include_global=True,
        include_inactive_owned=True,
    )
    for record in records:
        if str(getattr(record, "install_id", "") or "").strip() == normalized and str(
            getattr(record, "state", "") or ""
        ) == str(InstallState.active):
            return record
    return None


def active_installs_for_extension(
    install_store: Any,
    *,
    extension_id: str,
    account_id: str,
) -> list[Any]:
    """Return the account-visible *active* installs carrying this logical id."""
    normalized = str(extension_id or "").strip()
    if not normalized:
        return []
    records = install_store.list_for_account(
        account_id,
        include_global=True,
        include_inactive_owned=True,
    )
    return [
        record
        for record in records
        if str(getattr(record, "extension_id", "") or "").strip() == normalized
        and str(getattr(record, "state", "") or "") == str(InstallState.active)
    ]


def install_option_rows(
    install_store: Any,
    *,
    account_id: str,
    bundle_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Describe this account's extension installs as import-wizard candidates.

    A workflow bundle names an extension dependency by install id, so the wizard's
    target must be an install id too. Listing bundles instead offered values the
    resolver could never match. `bundle_rows` only lends the human display name.
    """
    labels = {
        str(row.get("extension_id") or "").strip(): str(row.get("display_name") or "").strip()
        for row in bundle_rows or []
        if isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    for record in install_store.list_for_account(
        account_id,
        include_global=True,
        include_inactive_owned=True,
    ):
        install_id = str(getattr(record, "install_id", "") or "").strip()
        extension_id = str(getattr(record, "extension_id", "") or "").strip()
        if not install_id:
            continue
        version = str(getattr(record, "version", "") or "").strip()
        state = str(getattr(record, "state", "") or "").strip()
        rows.append(
            {
                "install_id": install_id,
                "extension_id": extension_id,
                "display_name": labels.get(extension_id) or extension_id or install_id,
                "description": " · ".join(part for part in (extension_id, version, state) if part),
            }
        )
    return rows


#: A manifest is a small YAML header. Reading more than this from an archive
#: member would be reading the extension, not checking its identity.
_MAX_MANIFEST_BYTES = 512 * 1024
_MANIFEST_NAME = "extension.yaml"


def _assert_archive_declares(archive: bytes, *, extension_id: str, version: str) -> None:
    """Refuse an archive whose own manifest is not the release we offered.

    The digest proves the bytes are the ones the catalog published; it says
    nothing about whether the catalog's metadata describes them honestly. A
    mislabelled entry would otherwise install something other than the card the
    user read, and the installer would accept it because the archive is
    internally valid.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            manifests = [
                info
                for info in bundle.infolist()
                if not info.is_dir()
                and info.filename.split("/")[-1] == _MANIFEST_NAME
                and info.filename.count("/") <= 1
            ]
            if len(manifests) != 1:
                raise ExtensionCatalogError(
                    "Extension archive does not contain exactly one canonical extension.yaml.",
                    code="extension_catalog_archive_manifest_missing",
                    status_code=422,
                )
            info = manifests[0]
            if info.file_size > _MAX_MANIFEST_BYTES:
                raise ExtensionCatalogError(
                    "Extension archive manifest is implausibly large.",
                    code="extension_catalog_archive_manifest_invalid",
                    status_code=422,
                )
            with bundle.open(info) as handle:
                declared = yaml.safe_load(handle.read(_MAX_MANIFEST_BYTES + 1))
    except ExtensionCatalogError:
        raise
    except (zipfile.BadZipFile, yaml.YAMLError, OSError, ValueError) as exc:
        raise ExtensionCatalogError(
            "Extension archive could not be read.",
            code="extension_catalog_archive_unreadable",
            status_code=422,
        ) from exc

    if not isinstance(declared, dict):
        raise ExtensionCatalogError(
            "Extension archive manifest is not a mapping.",
            code="extension_catalog_archive_manifest_invalid",
            status_code=422,
        )
    archive_id = str(declared.get("extension_id") or "").strip()
    archive_version = str(declared.get("version") or "").strip()
    if archive_id != extension_id or archive_version != version:
        seen_id = archive_id or "?"
        seen_version = archive_version or "?"
        raise ExtensionCatalogError(
            (
                f"Catalog entry '{extension_id}' {version} ships an archive declaring "
                f"'{seen_id}' {seen_version}."
            ),
            code="extension_catalog_archive_mismatch",
            status_code=422,
        )


class ExtensionCatalogInstallService:
    """Serve the joined catalog listing and install one verified release."""

    def __init__(
        self,
        *,
        get_install_store: Callable[[], Any],
        get_binding_service: Callable[[], Any],
        load_index: Callable[[], Any] = load_extension_catalog,
        availability: Callable[..., dict[str, Any]] = extension_catalog_availability,
        fetch_artifact: Callable[..., bytes] = fetch_extension_release_artifact,
        # `compatible_flow_steward` is a product range, so it is checked against the
        # product version, not the extension host contract version.
        platform_version: Callable[[], str] = get_app_version,
        # Reads an installed release's descriptor, so an update can say which
        # permissions the newer release adds. Optional: without it the listing
        # simply names none.
        get_runtime_store: Callable[[], Any] | None = None,
    ) -> None:
        self._get_install_store = get_install_store
        self._get_binding_service = get_binding_service
        self._load_index = load_index
        self._availability = availability
        self._fetch_artifact = fetch_artifact
        self._platform_version = platform_version
        self._get_runtime_store = get_runtime_store

    def list_catalog(self, *, scope: ScopeContext | None = None) -> dict[str, Any]:
        """Return every catalog entry joined with this account's install state."""
        # One read of the index serves both the listing and its availability,
        # so the two cannot describe different revisions.
        try:
            index = self._load_index()
        except ExtensionCatalogError:
            return {
                "kind": "extension_catalog_listing",
                "availability": self._availability(),
                "items": [],
            }
        availability = self._availability(index)
        installs = self._installs_by_extension_id(scope=scope)
        platform_version = self._platform_version()
        return {
            "kind": "extension_catalog_listing",
            "availability": availability,
            "catalog_revision": index.catalog_revision,
            "schema_version": index.schema_version,
            "category_labels": dict(index.category_labels),
            # Core's own names for the contract's ids; the index carries none.
            "capability_labels": dict(CAPABILITY_LABELS),
            "permission_labels": dict(PERMISSION_LABELS),
            "items": [
                self._project_item(item, installs.get(item.extension_id), platform_version)
                for item in index.items
            ],
        }

    def install_from_catalog(
        self,
        extension_id: str,
        *,
        version: str,
        release_sha256: str,
        scope: ScopeContext | None = None,
        approved_browser_authority_impact_hash: str = "",
    ) -> Any:
        """Fetch, verify, and install the exact catalog release the card showed.

        The client pins both the version and the digest it was shown. The
        listing is served from one process's cache and the install may run in
        another with its own, so the two can disagree — and a republished
        version keeps its number while changing its bytes, which a version
        check alone would wave through.
        """
        item = self._require_item(extension_id)
        requested = str(version or "").strip()
        requested_digest = str(release_sha256 or "").strip().lower()
        if not requested or not requested_digest:
            raise ExtensionCatalogError(
                "A catalog install must name the version and digest it was offered.",
                code="extension_catalog_release_pin_required",
                status_code=422,
            )
        if requested != item.version or requested_digest != item.release_zip_sha256:
            raise ExtensionCatalogError(
                (
                    f"The catalog entry for '{item.extension_id}' changed since it was "
                    f"listed (now {item.version}). Reload the catalog and try again."
                ),
                code="extension_catalog_release_changed",
                status_code=409,
            )
        if not item.available_for_host:
            raise ExtensionCatalogError(
                (
                    f"Extension '{item.extension_id}' {item.version} is published for "
                    f"{', '.join(item.runtime_targets)} only, not for this server."
                ),
                code="extension_catalog_release_target_unavailable",
                status_code=409,
            )
        platform_version = self._platform_version()
        if not catalog_release_compatible(item, platform_version=platform_version):
            raise ExtensionCatalogError(
                (
                    f"Extension '{item.extension_id}' {item.version} is not compatible with "
                    f"Flow Steward {platform_version}."
                ),
                code="extension_catalog_release_incompatible",
                status_code=409,
            )
        # A newer catalog release updates the existing install through the same
        # archive installer an uploaded ZIP uses. Only the cases that installer
        # would refuse anyway are refused here, before anything is downloaded.
        existing = self._installs_by_extension_id(scope=scope).get(item.extension_id)
        if existing is not None:
            self._require_update(item, existing)

        try:
            archive = self._fetch_artifact(
                item.release_zip_url,
                expected_bytes=item.release_zip_bytes,
                expected_sha256=item.release_zip_sha256,
                purpose=f"extension catalog release '{item.extension_id}'",
            )
        except CatalogArtifactMismatchError as exc:
            # The installer is never reached: a release whose bytes do not match
            # what the catalog published is not the release we are offering.
            raise ExtensionCatalogError(
                str(exc),
                code="extension_catalog_artifact_mismatch",
                status_code=422,
            ) from exc
        except CatalogFetchError as exc:
            raise ExtensionCatalogError(
                str(exc),
                code="extension_catalog_artifact_unavailable",
            ) from exc

        _assert_archive_declares(archive, extension_id=item.extension_id, version=item.version)
        return self._get_binding_service().install_extension_archive(
            filename=f"{item.extension_id}-{item.version}.zip",
            content=archive,
            scope=scope,
            approved_browser_authority_impact_hash=approved_browser_authority_impact_hash,
        )

    @staticmethod
    def _require_update(item: ExtensionCatalogItem, existing: Any) -> None:
        """Refuse a same-version reinstall, a downgrade or an unreadable version."""
        installed_version = str(getattr(existing, "version", "") or "").strip() or "unknown"
        comparison = _installed_version_comparison(existing, item.version)
        if comparison is None:
            raise ExtensionCatalogError(
                (
                    f"Extension '{item.extension_id}' is installed with a version that "
                    f"cannot be read ({installed_version}), so it cannot be updated to "
                    f"{item.version} from the catalog."
                ),
                code="extension_catalog_installed_version_unreadable",
                status_code=409,
            )
        if comparison == 0:
            raise ExtensionCatalogError(
                (
                    f"Extension '{item.extension_id}' {item.version} is already installed. "
                    "There is nothing to update."
                ),
                code="extension_catalog_already_installed",
                status_code=409,
            )
        if comparison > 0:
            raise ExtensionCatalogError(
                (
                    f"Extension '{item.extension_id}' {installed_version} is newer than the "
                    f"catalog release {item.version}. Downgrading from the catalog is not "
                    "supported."
                ),
                code="extension_catalog_downgrade_refused",
                status_code=409,
            )

    def _installed_permissions(self, install: Any) -> list[str] | None:
        """The permissions the installed release asks for, or None when unknown."""
        release_id = str(getattr(install, "release_id", "") or "").strip()
        if self._get_runtime_store is None or not release_id:
            return None
        try:
            release = self._get_runtime_store().get_release(release_id)
        except Exception:
            return None
        if release is None:
            return None
        scopes = (getattr(release, "descriptor", None) or {}).get("required_scopes")
        if not isinstance(scopes, (list, tuple)):
            return None
        return sorted({str(scope).strip() for scope in scopes if str(scope or "").strip()})

    def _require_item(self, extension_id: str) -> ExtensionCatalogItem:
        index = self._load_index()
        item = index.item(extension_id)
        if item is None:
            raise ExtensionCatalogError(
                f"Extension '{str(extension_id or '').strip()}' is not in the catalog.",
                code="extension_catalog_item_not_found",
                status_code=404,
            )
        return item

    def _installs_by_extension_id(self, *, scope: ScopeContext | None) -> dict[str, Any]:
        account_id = normalize_scope(scope).account_id or "acc_default"
        records = self._get_install_store().list_for_account(
            account_id,
            include_global=True,
            include_inactive_owned=True,
        )
        installs: dict[str, Any] = {}
        for record in records:
            key = str(getattr(record, "extension_id", "") or "").strip()
            if not key:
                continue
            # An active record always wins over a disabled one for the same id,
            # so a leftover disabled row cannot mask a working install.
            existing = installs.get(key)
            if existing is not None and str(getattr(existing, "state", "")) == str(
                InstallState.active
            ):
                continue
            installs[key] = record
        return installs

    def _project_item(
        self,
        item: ExtensionCatalogItem,
        install: Any | None,
        platform_version: str,
    ) -> dict[str, Any]:
        display_state = catalog_display_state(item, install, platform_version=platform_version)
        # What the newer release asks for that the installed one did not: the
        # extension page lists it above Update, because permissions are the
        # trust decision an update can change.
        added_permissions: list[str] = []
        if display_state == STATE_UPDATE_AVAILABLE and install is not None:
            installed = self._installed_permissions(install)
            if installed is not None:
                added_permissions = sorted(set(item.permissions_summary) - set(installed))
        return {
            **item.as_dict(),
            "display_state": display_state,
            "added_permissions": added_permissions,
            "compatible": catalog_release_compatible(item, platform_version=platform_version),
            "installed_version": str(getattr(install, "version", "") or "") if install else "",
            "installed_install_id": str(getattr(install, "install_id", "") or "")
            if install
            else "",
            "installed_state": str(getattr(install, "state", "") or "") if install else "",
        }


__all__ = [
    "STATE_INCOMPATIBLE",
    "STATE_INSTALL",
    "STATE_INSTALLED",
    "STATE_INSTALLED_DISABLED",
    "STATE_INSTALLED_NEWER",
    "STATE_UPDATE_AVAILABLE",
    "ExtensionCatalogInstallService",
    "active_install_by_install_id",
    "active_installs_for_extension",
    "catalog_display_state",
    "catalog_release_compatible",
]
