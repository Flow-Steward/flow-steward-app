"""Explicit approval boundary for extension-supplied top-level browser code."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import stat
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.application.management.errors import (
    ServicePreconditionError,
    ServiceValidationError,
)

from .browser_assets import resolve_declared_browser_asset

BROWSER_AUTHORITY_CONTRACT_VERSION = "extension_browser_authority_v1"
BROWSER_AUTHORITY_PROVENANCE_KEY = "browser_authority_approval"
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class BrowserAuthorityImpact:
    approval_required: bool
    impact_hash: str
    authority_fingerprint: str
    surface_fingerprint: str
    authority_contract_version: str
    extension_id: str
    account_id: str
    project_id: str
    install_id: str
    old_release_id: str
    new_release_id: str
    descriptor_sha256: str
    candidate_tree_sha256: str
    bundle_root: str
    declarations: tuple[dict[str, Any], ...]
    bootstrap_actions: tuple[dict[str, Any], ...]
    remote_script_urls: tuple[str, ...]
    remote_origins: tuple[str, ...]
    local_assets: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class PreparedBrowserAuthorityApproval:
    """Opaque internal handoff from one validated archive candidate."""

    impact: BrowserAuthorityImpact
    approval: dict[str, Any]
    candidate_version: str


def _text(value: Any) -> str:
    return str(value or "").strip()


def _picker_declarations(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        declaration = value.get("external_resource_picker")
        if isinstance(declaration, Mapping):
            yield declaration
        if _text(value.get("type")) == "external_resource_picker":
            data = value.get("data")
            if isinstance(data, Mapping):
                yield data
        for nested in value.values():
            yield from _picker_declarations(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _picker_declarations(nested)


def browser_bootstrap_action_ids(manifest: Mapping[str, Any]) -> frozenset[str]:
    """Return the generic action ids granted browser-bootstrap authority."""
    return frozenset(
        action_id
        for declaration in _picker_declarations(manifest)
        if (action_id := _text(declaration.get("bootstrap_action_id")))
    )


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ServiceValidationError(
            "Extension browser authority declaration is not canonical JSON"
        ) from exc


def _unique_mappings(values: Iterator[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    by_canonical = {_canonical(dict(value)): dict(value) for value in values}
    return tuple(by_canonical[key] for key in sorted(by_canonical))


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _walk_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_strings(nested)
    elif isinstance(value, str):
        yield value


def _remote_authority(
    declarations: tuple[dict[str, Any], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    urls: set[str] = set()
    origins: set[str] = set()
    for declaration in declarations:
        for value in _walk_strings(declaration):
            candidate = value.strip()
            parsed = urlsplit(candidate)
            if parsed.scheme != "https" or not parsed.netloc:
                continue
            origin = f"https://{parsed.netloc}"
            origins.add(origin)
            if parsed.path or parsed.query or parsed.fragment:
                urls.add(candidate)
    return tuple(sorted(urls)), tuple(sorted(origins))


def _asset_refs(declarations: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    refs: set[str] = set()
    for declaration in declarations:
        scripts = declaration.get("scripts")
        if not isinstance(scripts, list):
            continue
        for script in scripts:
            if isinstance(script, Mapping) and set(script) == {"asset_ref"}:
                asset_ref = _text(script.get("asset_ref"))
                if asset_ref:
                    refs.add(asset_ref)
    return tuple(sorted(refs))


def _local_assets(
    manifest: Mapping[str, Any],
    *,
    declarations: tuple[dict[str, Any], ...],
) -> tuple[dict[str, str], ...]:
    assets: list[dict[str, str]] = []
    for asset_ref in _asset_refs(declarations):
        path = resolve_declared_browser_asset(manifest, asset_ref=asset_ref)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assets.append({"asset_ref": asset_ref, "sha256": digest})
    return tuple(assets)


def candidate_browser_tree_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash the exact candidate tree without following links or trusting metadata."""
    bundle_root = _text(manifest.get("bundle_root"))
    if not bundle_root:
        descriptor = _canonical(dict(manifest)).encode("utf-8")
        return hashlib.sha256(descriptor).hexdigest()
    root = Path(bundle_root)
    if root.is_symlink() or not root.is_dir():
        raise ServiceValidationError("Extension browser authority bundle root is invalid")
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            metadata = path.lstat()
            relative = path.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"\0directory\0")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ServiceValidationError(
                    "Extension browser authority tree may contain only regular files"
                )
            digest.update(b"\0file\0")
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
    except OSError as exc:
        raise ServiceValidationError(
            "Extension browser authority tree could not be verified"
        ) from exc
    return digest.hexdigest()


def browser_authority_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a compiled candidate without extraction-directory identities."""

    def stable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): stable(nested)
                for key, nested in value.items()
                if str(key) not in {"bundle_root", "manifest_path"}
            }
        if isinstance(value, list):
            return [stable(nested) for nested in value]
        return value

    return hashlib.sha256(_canonical(stable(manifest)).encode("utf-8")).hexdigest()


def resolve_browser_authority_manifest(
    release: Any,
    *,
    require_bundle_manifest: bool = False,
) -> dict[str, Any]:
    """Resolve the browser surface from the referenced runtime bundle, when present."""
    descriptor = dict(getattr(release, "descriptor", None) or {})
    bundle_root = _text(descriptor.get("bundle_root"))
    if not bundle_root:
        return descriptor
    manifest_path = Path(bundle_root) / "extension.yaml"
    if not manifest_path.is_file():
        if require_bundle_manifest:
            raise ServiceValidationError("Extension browser authority bundle manifest is missing")
        return descriptor

    from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle

    from .identity import extension_folder_name

    try:
        logical_bundle_dir_name = extension_folder_name(getattr(release, "extension_id", ""))
    except ValueError as exc:
        raise ServiceValidationError(
            "Extension browser authority identity could not be verified"
        ) from exc
    compiled = compile_extension_bundle(
        bundle_root,
        logical_bundle_dir_name=logical_bundle_dir_name,
    )
    if _text(compiled.get("status")).lower() != "valid":
        errors = list(compiled.get("compile_errors") or []) + list(
            compiled.get("validation_errors") or []
        )
        raise ServiceValidationError(
            "Extension browser authority bundle could not be verified",
            payload={"errors": errors},
        )
    return dict(compiled)


def _bootstrap_actions(
    manifest: Mapping[str, Any],
    *,
    declarations: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    action_ids = {
        _text(declaration.get("bootstrap_action_id"))
        for declaration in declarations
        if _text(declaration.get("bootstrap_action_id"))
    }
    rows = manifest.get("action_definitions")
    if not isinstance(rows, list):
        rows = []
    actions = (
        row
        for row in rows
        if isinstance(row, Mapping) and _text(row.get("action_id")) in action_ids
    )
    return _unique_mappings(actions)


def _sha256(value: Any, *, field_name: str) -> str:
    normalized = _text(value).lower()
    if _SHA256_HEX.fullmatch(normalized) is None:
        raise ServiceValidationError(f"{field_name} must be a sha256 digest")
    return normalized


def browser_authority_impact(
    *,
    account_id: str,
    project_id: str,
    extension_id: str,
    install_id: str,
    old_release_id: str,
    new_release_id: str,
    descriptor_sha256: str,
    candidate_tree_sha256: str,
    manifest: Mapping[str, Any],
) -> BrowserAuthorityImpact:
    """Fingerprint the complete authority granted to one candidate browser bundle."""
    declarations = _unique_mappings(_picker_declarations(manifest))
    bundle_root = _text(manifest.get("bundle_root"))
    if not declarations:
        return BrowserAuthorityImpact(
            approval_required=False,
            impact_hash="",
            authority_fingerprint="",
            surface_fingerprint="",
            authority_contract_version=BROWSER_AUTHORITY_CONTRACT_VERSION,
            extension_id=_text(extension_id),
            account_id=_text(account_id),
            project_id=_text(project_id),
            install_id=_text(install_id),
            old_release_id=_text(old_release_id),
            new_release_id=_text(new_release_id),
            descriptor_sha256=_text(descriptor_sha256).lower(),
            candidate_tree_sha256=_text(candidate_tree_sha256).lower(),
            bundle_root=bundle_root,
            declarations=(),
            bootstrap_actions=(),
            remote_script_urls=(),
            remote_origins=(),
            local_assets=(),
        )
    descriptor_digest = _sha256(descriptor_sha256, field_name="descriptor_sha256")
    tree_digest = _sha256(candidate_tree_sha256, field_name="candidate_tree_sha256")
    actions = _bootstrap_actions(manifest, declarations=declarations)
    local_assets = _local_assets(manifest, declarations=declarations)
    remote_urls, remote_origins = _remote_authority(declarations)
    surface_payload = {
        "declarations": declarations,
        "bootstrap_actions": actions,
        "remote_script_urls": remote_urls,
        "remote_origins": remote_origins,
        "local_assets": local_assets,
    }
    surface_fingerprint = hashlib.sha256(_canonical(surface_payload).encode("utf-8")).hexdigest()
    authority_payload = {
        "authority_contract_version": BROWSER_AUTHORITY_CONTRACT_VERSION,
        "account_id": _text(account_id),
        "extension_id": _text(extension_id),
        "descriptor_sha256": descriptor_digest,
        "surface_fingerprint": surface_fingerprint,
    }
    authority_fingerprint = hashlib.sha256(
        _canonical(authority_payload).encode("utf-8")
    ).hexdigest()
    transition_payload = {
        "authority_fingerprint": authority_fingerprint,
        "account_id": _text(account_id),
        "project_id": _text(project_id),
        "install_id": _text(install_id),
        "old_release_id": _text(old_release_id),
        "new_release_id": _text(new_release_id),
        "candidate_tree_sha256": tree_digest,
    }
    impact_hash = hashlib.sha256(_canonical(transition_payload).encode("utf-8")).hexdigest()
    return BrowserAuthorityImpact(
        approval_required=True,
        impact_hash=impact_hash,
        authority_fingerprint=authority_fingerprint,
        surface_fingerprint=surface_fingerprint,
        authority_contract_version=BROWSER_AUTHORITY_CONTRACT_VERSION,
        extension_id=_text(extension_id),
        account_id=_text(account_id),
        project_id=_text(project_id),
        install_id=_text(install_id),
        old_release_id=_text(old_release_id),
        new_release_id=_text(new_release_id),
        descriptor_sha256=descriptor_digest,
        candidate_tree_sha256=tree_digest,
        bundle_root=str(Path(bundle_root).resolve()) if bundle_root else "",
        declarations=declarations,
        bootstrap_actions=actions,
        remote_script_urls=remote_urls,
        remote_origins=remote_origins,
        local_assets=local_assets,
    )


def browser_authority_required(manifest: Mapping[str, Any]) -> bool:
    """Return whether a manifest exposes extension-supplied top-level browser code."""
    return any(_picker_declarations(manifest))


def current_browser_authority_impact(
    *,
    record: Any,
    release: Any,
) -> BrowserAuthorityImpact | None:
    """Recompute the authority of one installed release from its current bundle bytes."""
    descriptor = dict(getattr(release, "descriptor", None) or {})
    manifest = resolve_browser_authority_manifest(release)
    return browser_authority_impact(
        account_id=_text(getattr(record, "account_id", "")),
        project_id="",
        extension_id=_text(getattr(record, "extension_id", "")),
        install_id=_text(getattr(record, "install_id", "")),
        old_release_id=_text(getattr(release, "release_id", "")),
        new_release_id=_text(getattr(release, "release_id", "")),
        descriptor_sha256=_text(getattr(release, "descriptor_sha256", "")),
        candidate_tree_sha256=candidate_browser_tree_sha256(descriptor),
        manifest=manifest,
    )


def require_current_browser_authority(*, record: Any, release: Any) -> None:
    """Block executable browser surfaces until current bundle authority is approved."""
    impact = current_browser_authority_impact(record=record, release=release)
    if impact is None or not impact.approval_required:
        return
    state = _text(getattr(getattr(record, "state", ""), "value", None) or record.state).lower()
    if state == "active" and browser_authority_approval_matches(record.provenance, impact):
        return
    require_browser_authority_approval(impact, approved_impact_hash="")


def require_browser_authority_approval(
    impact: BrowserAuthorityImpact,
    *,
    approved_impact_hash: str,
) -> BrowserAuthorityImpact:
    """Require an exact retry-bound approval before browser authority is published."""
    if not impact.approval_required:
        return impact
    approved = _text(approved_impact_hash).lower()
    if not approved or not hmac.compare_digest(approved, impact.impact_hash):
        raise ServicePreconditionError(
            "External browser code approval required",
            payload={
                "error": "browser_authority_approval_required",
                "impact_hash": impact.impact_hash,
                "authority_contract_version": impact.authority_contract_version,
                "extension_id": impact.extension_id,
                "remote_script_urls": list(impact.remote_script_urls),
                "remote_origins": list(impact.remote_origins),
                "local_asset_refs": [row["asset_ref"] for row in impact.local_assets],
            },
        )
    return impact


def browser_authority_approval_provenance(
    impact: BrowserAuthorityImpact,
    *,
    actor_id: str,
    approved_at: float | None = None,
    approved_impact_hash: str = "",
) -> dict[str, Any]:
    """Return host-owned, auditable install provenance for an approved impact."""
    if not impact.approval_required:
        return {}
    return {
        "authority_contract_version": impact.authority_contract_version,
        "approved_impact_hash": _text(approved_impact_hash) or impact.impact_hash,
        "authority_fingerprint": impact.authority_fingerprint,
        "actor_id": _text(actor_id),
        "approved_at": float(time.time() if approved_at is None else approved_at),
        "extension_id": impact.extension_id,
        "account_id": impact.account_id,
        "descriptor_sha256": impact.descriptor_sha256,
    }


def with_browser_authority_approval(
    provenance: Mapping[str, Any] | None,
    *,
    approval: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Strip extension-supplied approval data and attach only host-prepared metadata."""
    result = dict(provenance or {})
    result.pop(BROWSER_AUTHORITY_PROVENANCE_KEY, None)
    if approval:
        result[BROWSER_AUTHORITY_PROVENANCE_KEY] = dict(approval)
    return result


def browser_authority_approval_matches(
    provenance: Mapping[str, Any] | None,
    impact: BrowserAuthorityImpact,
) -> bool:
    if not impact.approval_required:
        return True
    row = (provenance or {}).get(BROWSER_AUTHORITY_PROVENANCE_KEY)
    if not isinstance(row, Mapping):
        return False
    return hmac.compare_digest(
        _text(row.get("authority_contract_version")), impact.authority_contract_version
    ) and hmac.compare_digest(_text(row.get("authority_fingerprint")), impact.authority_fingerprint)


__all__ = [
    "BROWSER_AUTHORITY_CONTRACT_VERSION",
    "BROWSER_AUTHORITY_PROVENANCE_KEY",
    "BrowserAuthorityImpact",
    "PreparedBrowserAuthorityApproval",
    "browser_authority_approval_matches",
    "browser_authority_approval_provenance",
    "browser_authority_impact",
    "browser_authority_manifest_sha256",
    "browser_authority_required",
    "candidate_browser_tree_sha256",
    "current_browser_authority_impact",
    "require_browser_authority_approval",
    "require_current_browser_authority",
    "resolve_browser_authority_manifest",
    "with_browser_authority_approval",
]
