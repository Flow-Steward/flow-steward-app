from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.application.management.query_models import ExtensionMarketplaceItemView

from .service_support import (
    _compatibility_for_descriptor,
    _contains_kind_from_artifact_kind,
    _entitlement_status_value,
    _normalize_lower,
    _normalize_text,
    _safe_iso_from_timestamp,
    _source_label,
    _status_group_for_install,
)


@dataclass(frozen=True)
class MarketplaceDescriptorFields:
    descriptor: dict[str, Any]
    identity: dict[str, Any]
    vendor: dict[str, Any]
    listing: dict[str, Any]
    distribution: dict[str, Any]
    provenance: dict[str, Any]
    commerce: dict[str, Any]
    tags: list[str]
    execution_modes: list[str]
    import_sources: list[str]


@dataclass(frozen=True)
class MarketplaceInstallFields:
    installed: bool
    install_id: str
    state: Any | None
    entitlement_status: str | None
    status_group: str | None
    source_label: str
    connected: bool


class ExtensionMarketplaceProjector:
    """Project one release plus its account-local install overlay."""

    def project(
        self,
        *,
        release: Any,
        release_artifacts: list[Any],
        install_record: Any | None,
        entitlements_by_install: dict[str, Any],
        connection_counts: dict[str, int],
    ) -> ExtensionMarketplaceItemView:
        fields = self._descriptor_fields(release=release)
        install = self._install_fields(
            release=release,
            install_record=install_record,
            entitlements_by_install=entitlements_by_install,
            connection_counts=connection_counts,
        )
        plan_id = self._plan_id(fields.commerce)
        origin = _normalize_lower(fields.provenance.get("origin"))
        if not origin:
            origin = _normalize_lower((fields.descriptor.get("provenance") or {}).get("origin"))
        return ExtensionMarketplaceItemView(
            extension_id=_normalize_text(getattr(release, "extension_id", "")),
            release_id=_normalize_text(getattr(release, "release_id", "")),
            release_version=_normalize_text(getattr(release, "release_version", "")),
            display_name=_normalize_text(fields.identity.get("display_name"))
            or _normalize_text(getattr(release, "extension_id", "")),
            description=_normalize_text(fields.identity.get("description")),
            extension_type=_normalize_text(fields.descriptor.get("extension_type")),
            category=_normalize_lower(fields.listing.get("category")),
            tags=fields.tags,
            vendor=_normalize_lower(fields.vendor.get("vendor_name")),
            channel=_normalize_lower(
                getattr(release, "channel", "") or fields.distribution.get("channel")
            ),
            execution_modes=fields.execution_modes,
            allows_self_hosted=bool(fields.distribution.get("allows_self_hosted")),
            requires_connected_control_plane=bool(
                fields.distribution.get("requires_connected_control_plane")
            ),
            import_sources=fields.import_sources,
            compatibility=_compatibility_for_descriptor(fields.descriptor),
            origin=origin,
            source=_normalize_lower(getattr(release, "source_kind", "")),
            source_label=install.source_label,
            billing_mode=_normalize_lower(fields.commerce.get("billing_mode")),
            plan_id=_normalize_lower(plan_id),
            contains=self._contains(
                descriptor=fields.descriptor, release_artifacts=release_artifacts
            ),
            installed=install.installed,
            install_id=install.install_id or None,
            install_state=_normalize_lower(install.state) if install.state is not None else None,
            entitlement_status=install.entitlement_status,
            status_group=install.status_group,
            updated_at=_safe_iso_from_timestamp(getattr(release, "created_at", None)),
            flags={"connected": install.connected},
        )

    @staticmethod
    def _descriptor_fields(*, release: Any) -> MarketplaceDescriptorFields:
        descriptor = getattr(release, "descriptor", {})
        descriptor = descriptor if isinstance(descriptor, dict) else {}
        identity = ExtensionMarketplaceProjector._dict_field(descriptor, "identity")
        distribution = ExtensionMarketplaceProjector._dict_field(descriptor, "distribution")
        tags_raw = identity.get("tags")
        modes_raw = getattr(release, "execution_modes", None)
        if not isinstance(modes_raw, list):
            modes_raw = distribution.get("execution_modes")
        imports_raw = distribution.get("import_sources")
        provenance = getattr(release, "provenance", {})
        return MarketplaceDescriptorFields(
            descriptor=descriptor,
            identity=identity,
            vendor=ExtensionMarketplaceProjector._dict_field(descriptor, "vendor"),
            listing=ExtensionMarketplaceProjector._dict_field(descriptor, "listing"),
            distribution=distribution,
            provenance=provenance if isinstance(provenance, dict) else {},
            commerce=ExtensionMarketplaceProjector._dict_field(descriptor, "commerce"),
            tags=ExtensionMarketplaceProjector._normalized_list(tags_raw),
            execution_modes=ExtensionMarketplaceProjector._normalized_list(modes_raw),
            import_sources=ExtensionMarketplaceProjector._normalized_list(imports_raw),
        )

    @staticmethod
    def _install_fields(
        *,
        release: Any,
        install_record: Any | None,
        entitlements_by_install: dict[str, Any],
        connection_counts: dict[str, int],
    ) -> MarketplaceInstallFields:
        install_id = (
            _normalize_text(getattr(install_record, "install_id", ""))
            if install_record is not None
            else ""
        )
        entitlement = entitlements_by_install.get(install_id) if install_id else None
        entitlement_status = (
            _entitlement_status_value(entitlement) if entitlement is not None else None
        )
        state = getattr(install_record, "state", None) if install_record is not None else None
        state = getattr(state, "value", state) if state is not None else None
        status_group = (
            _status_group_for_install(
                record=install_record, entitlement_status=entitlement_status or "unknown"
            )
            if install_record is not None and entitlement_status is not None
            else None
        )
        source_label = _source_label(getattr(release, "source_kind", ""))
        if install_record is not None and not source_label:
            source_label = _source_label(getattr(install_record, "descriptor_source", ""))
        return MarketplaceInstallFields(
            installed=install_record is not None,
            install_id=install_id,
            state=state,
            entitlement_status=entitlement_status,
            status_group=status_group,
            source_label=source_label,
            connected=int(connection_counts.get(install_id, 0) or 0) > 0,
        )

    @staticmethod
    def _dict_field(source: dict[str, Any], name: str) -> dict[str, Any]:
        value = source.get(name)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _normalized_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [_normalize_lower(item) for item in value if _normalize_lower(item)]

    @staticmethod
    def _plan_id(commerce: dict[str, Any]) -> str:
        plan_id = _normalize_text(commerce.get("plan_id"))
        plan = commerce.get("plan")
        if not plan_id and isinstance(plan, dict):
            return _normalize_text(plan.get("id"))
        return plan_id

    @staticmethod
    def _contains(*, descriptor: dict[str, Any], release_artifacts: list[Any]) -> list[str]:
        kinds = {
            mapped
            for row in release_artifacts
            if (mapped := _contains_kind_from_artifact_kind(getattr(row, "artifact_kind", "")))
        }
        artifacts = ExtensionMarketplaceProjector._dict_field(descriptor, "artifacts")
        section_map = {
            "workflows": "workflow",
            "agents": "agent",
            "skills": "skill",
            "tools": "tool",
            "data_contracts": "data_contract",
            "guardrails": "guardrail",
            "extensions": "runtime_module",
        }
        kinds.update(
            kind
            for section, kind in section_map.items()
            if isinstance(artifacts.get(section), list) and artifacts.get(section)
        )
        runtime = ExtensionMarketplaceProjector._dict_field(descriptor, "runtime")
        if _normalize_text(runtime.get("contract_version")):
            kinds.add("runtime_module")
        return sorted(kinds)


__all__ = ["ExtensionMarketplaceProjector"]
