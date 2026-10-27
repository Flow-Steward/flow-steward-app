from __future__ import annotations

from collections import Counter
from typing import Any

from core.application.management.errors import ServiceUnavailableError
from core.application.management.query_models import ExtensionDetailView, ProjectConnectionView

from .browser_authority_status import browser_authority_status
from .extension_runtime import (
    ExtensionArtifactSnapshotRecord,
    ExtensionEntitlementRecord,
    ExtensionReleaseRecord,
)
from .install import ExtensionInstallRecord
from .query_ports import ExtensionDetailListingPort, ExtensionRuntimeStorePort
from .service_support import (
    _artifact_id_from_entry,
    _artifact_kind_from_section,
    _list_extension_runtime_connections,
    _normalize_text,
)


class ExtensionDetailProjector:
    """Build a detail view from one visible install record."""

    def __init__(self, *, listings: ExtensionDetailListingPort) -> None:
        self._listings = listings

    def project(self, *, record: ExtensionInstallRecord, account_id: str) -> ExtensionDetailView:
        runtime_store = self._listings.runtime_store()
        owned = _normalize_text(record.account_id) == _normalize_text(account_id)
        release = self._release(runtime_store=runtime_store, record=record)
        entitlement = (
            self._entitlement(
                runtime_store=runtime_store,
                record=record,
                account_id=account_id,
            )
            if owned
            else None
        )
        artifacts_payload, release_artifacts = self._artifacts(
            runtime_store=runtime_store, release=release, owned=owned
        )
        projections_payload = (
            self._projections(
                runtime_store=runtime_store,
                record=record,
                account_id=account_id,
            )
            if owned
            else []
        )
        connection_rows = (
            _list_extension_runtime_connections(
                self._listings.logger(),
                account_id=account_id,
                install_id=record.install_id,
                enabled_only=False,
            )
            if owned
            else []
        )
        connections = [ProjectConnectionView.model_validate(row) for row in connection_rows]
        summary = self._listings.summary_from_install_record(
            record=record,
            viewer_account_id=account_id,
            release=release,
            entitlement=entitlement,
            release_artifacts=release_artifacts,
            connected_count=len(connections),
        )
        payload = summary.model_dump(mode="json")
        payload.update(
            {
                "artifacts": artifacts_payload,
                "entitlement": self._dump(entitlement),
                "connections": connections,
                "projections": projections_payload,
                "config": record.config if owned else {},
                "metadata": {
                    "release": self._release_payload(release, owned=owned),
                    "projection_scope": "owned" if owned else "public_global",
                    **(
                        {
                            "projection_health": self._projection_health(
                                record=record,
                                release=release,
                                projections=projections_payload,
                            ),
                            "browser_authority": self._browser_authority_status(
                                record=record,
                                release=release,
                            ),
                        }
                        if owned
                        else {}
                    ),
                },
            }
        )
        return ExtensionDetailView(**payload)

    @staticmethod
    def _browser_authority_status(
        *,
        record: ExtensionInstallRecord,
        release: ExtensionReleaseRecord | None,
    ) -> dict[str, Any]:
        return browser_authority_status(record=record, release=release)

    @staticmethod
    def _projection_health(
        *,
        record: ExtensionInstallRecord,
        release: ExtensionReleaseRecord | None,
        projections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        descriptor = dict(getattr(release, "descriptor", {}) or {})
        artifacts = descriptor.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        declared_keys = {
            (_artifact_kind_from_section(section), _artifact_id_from_entry(entry))
            for section, entries in artifacts.items()
            if isinstance(entries, list)
            for entry in entries
            if isinstance(entry, dict)
            and bool(entry.get("publish", True))
            and _artifact_id_from_entry(entry)
        }
        active_keys = {
            (
                _normalize_text(row.get("artifact_kind")),
                _normalize_text(row.get("artifact_id")),
            )
            for row in projections
            if bool(row.get("is_active"))
        }
        declared = Counter(kind for kind, _artifact_id in declared_keys)
        active = Counter(kind for kind, _artifact_id in active_keys)
        kinds = sorted(set(declared) | set(active))
        is_enabled = _normalize_text(getattr(record, "state", "")).lower() == "active"
        missing = sorted(
            f"{kind}:{artifact_id}" for kind, artifact_id in declared_keys - active_keys
        )
        unexpected = sorted(
            f"{kind}:{artifact_id}" for kind, artifact_id in active_keys - declared_keys
        )
        status = "disabled"
        if is_enabled:
            status = "degraded" if missing or unexpected else "ok"
        return {
            "status": status,
            "declared": {kind: declared[kind] for kind in kinds},
            "active": {kind: active[kind] for kind in kinds},
            "missing": missing if is_enabled else [],
            "unexpected": unexpected if is_enabled else [],
        }

    @staticmethod
    def _dump(value: Any | None) -> dict[str, Any] | None:
        return value.model_dump(mode="json") if value is not None else None

    @staticmethod
    def _release_payload(
        release: ExtensionReleaseRecord | None,
        *,
        owned: bool,
    ) -> dict[str, Any] | None:
        if release is None:
            return None
        payload = release.model_dump(mode="json")
        if owned:
            return payload
        public_fields = {
            "release_id",
            "extension_id",
            "release_version",
            "descriptor_schema_version",
            "descriptor_sha256",
            "channel",
            "execution_modes",
            "contract_version",
        }
        public = {key: value for key, value in payload.items() if key in public_fields}
        public["descriptor"] = ExtensionDetailProjector._public_descriptor(release.descriptor)
        return public

    @staticmethod
    def _public_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
        """Project only explicitly public catalog fields from an owner descriptor."""

        def public_text(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            normalized = value.strip()
            return normalized or None

        identity = descriptor.get("identity")
        identity = identity if isinstance(identity, dict) else {}
        vendor = descriptor.get("vendor")
        vendor = vendor if isinstance(vendor, dict) else {}
        listing = descriptor.get("listing")
        listing = listing if isinstance(listing, dict) else {}
        public: dict[str, Any] = {
            key: value
            for key in (
                "extension_id",
                "release_version",
                "descriptor_schema_version",
                "extension_type",
                "kind",
                "display_name",
                "description",
                "vendor_name",
                "author",
                "support_url",
                "website_url",
            )
            if (value := public_text(descriptor.get(key))) is not None
        }
        public["identity"] = {
            key: value
            for key in ("display_name", "description")
            if (value := public_text(identity.get(key))) is not None
        }
        public["vendor"] = {
            key: value
            for key in ("vendor_name", "name", "support_url", "website_url", "url")
            if (value := public_text(vendor.get(key))) is not None
        }
        tags = listing.get("tags")
        public_tags = (
            [value for item in tags if (value := public_text(item)) is not None]
            if isinstance(tags, list)
            else []
        )
        category = public_text(listing.get("category"))
        public["listing"] = {
            **({"category": category} if category is not None else {}),
            **({"tags": public_tags} if public_tags else {}),
        }
        return {key: value for key, value in public.items() if value not in ({}, [], "", None)}

    @staticmethod
    def _release(
        *, runtime_store: ExtensionRuntimeStorePort, record: ExtensionInstallRecord
    ) -> ExtensionReleaseRecord | None:
        release_id = _normalize_text(record.release_id)
        if not release_id:
            return None
        try:
            return runtime_store.get_release(release_id)
        except Exception as exc:
            raise ServiceUnavailableError("Extension release store is unavailable") from exc

    @staticmethod
    def _entitlement(
        *,
        runtime_store: ExtensionRuntimeStorePort,
        record: ExtensionInstallRecord,
        account_id: str,
    ) -> ExtensionEntitlementRecord | None:
        try:
            rows = runtime_store.list_entitlements(
                account_id=account_id,
                install_ids=[record.install_id],
            )
        except Exception as exc:
            raise ServiceUnavailableError("Extension entitlement store is unavailable") from exc
        return next(
            (
                row
                for row in rows
                if _normalize_text(row.install_id) == _normalize_text(record.install_id)
                and _normalize_text(row.account_id) == _normalize_text(account_id)
            ),
            None,
        )

    @staticmethod
    def _artifacts(
        *,
        runtime_store: ExtensionRuntimeStorePort,
        release: ExtensionReleaseRecord | None,
        owned: bool,
    ) -> tuple[list[dict[str, Any]], list[ExtensionArtifactSnapshotRecord]]:
        if release is None:
            return [], []
        try:
            snapshots = list(runtime_store.list_artifact_snapshots(release.release_id))
        except Exception as exc:
            raise ServiceUnavailableError("Extension artifact store is unavailable") from exc
        payload = [
            {
                "artifact_kind": row.artifact_kind,
                "artifact_id": row.artifact_id,
                "publish": row.publish,
                **({"artifact_ref": row.artifact_ref, "snapshot": row.snapshot} if owned else {}),
            }
            for row in snapshots
        ]
        return payload, snapshots

    def _active_extension_ids(self, *, account_id: str) -> set[str]:
        try:
            records = self._listings.install_store().list_for_account(
                account_id,
                include_global=True,
            )
        except Exception as exc:
            raise ServiceUnavailableError("Extension install store is unavailable") from exc
        return {
            _normalize_text(getattr(row, "extension_id", ""))
            for row in records
            if _normalize_text(getattr(row, "state", "")).lower() == "active"
        }

    def _projections(
        self,
        *,
        runtime_store: ExtensionRuntimeStorePort,
        record: ExtensionInstallRecord,
        account_id: str,
    ) -> list[dict[str, Any]]:
        active_ids = self._active_extension_ids(account_id=account_id)
        payload: list[dict[str, Any]] = []
        try:
            rows = runtime_store.list_projections(account_id=account_id, only_active=False)
        except Exception as exc:
            raise ServiceUnavailableError("Extension projection store is unavailable") from exc
        for row in rows:
            if _normalize_text(getattr(row, "install_id", "")) != _normalize_text(
                record.install_id
            ):
                continue
            projection = row.model_dump(mode="json")
            missing = self._missing_extensions(projection=projection, active_ids=active_ids)
            projection["readiness"] = self._readiness(missing)
            payload.append(projection)
        return payload

    @staticmethod
    def _missing_extensions(*, projection: dict[str, Any], active_ids: set[str]) -> list[str]:
        definition = projection.get("projection")
        definition = definition if isinstance(definition, dict) else {}
        dependencies = definition.get("dependencies")
        dependencies = dependencies if isinstance(dependencies, dict) else {}
        extensions = dependencies.get("extensions")
        extensions = extensions if isinstance(extensions, list) else []
        return [
            _normalize_text(item.get("extension_id"))
            for item in extensions
            if isinstance(item, dict)
            and bool(item.get("required", True))
            and _normalize_text(item.get("extension_id")) not in active_ids
        ]

    @staticmethod
    def _readiness(missing: list[str]) -> dict[str, Any]:
        return {
            "available": not missing,
            "reason": (
                "Required extension is not installed or enabled: " + ", ".join(missing)
                if missing
                else ""
            ),
            "missing_extensions": missing,
        }


__all__ = ["ExtensionDetailProjector"]
