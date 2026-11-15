"""Combine an extension descriptor with its co-located executable bundle runtime.

An artifact descriptor (``extension-descriptor.yaml``) and the owning package's
``extension.yaml`` describe one release. Import and validation compile the
bundle, check that both name the same extension and version, and carry the
bundle's runtime contract (actions, callbacks, OAuth providers, slots, ...)
into the descriptor that is persisted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .service_support import _normalize_text

_RUNTIME_DESCRIPTOR_FIELD_NAMES = (
    "manifest_version kind display_name description features "
    "entrypoint healthcheck required_scopes supported_events "
    "supported_url_schemes event_subscriptions schemas query_definitions action_definitions "
    "machine_auth account_scope callbacks operation_manifest connection_types workflow_triggers "
    "external_effects artifact_policies project_state"
)
_RUNTIME_DESCRIPTOR_FIELDS = _RUNTIME_DESCRIPTOR_FIELD_NAMES.split()


def _logical_bundle_dir_name(extension_id: str) -> str:
    if not extension_id:
        return ""
    from core.application.extensions.identity import extension_folder_name

    try:
        return extension_folder_name(extension_id)
    except ValueError:
        # Descriptor validation reports malformed extension ids before this
        # enrichment path; compilation must still fail closed when called alone.
        return ""


def _runtime_identity_errors(
    *,
    expected_extension_id: str,
    expected_version: str,
    compiled_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    checks = (
        (
            _normalize_text(compiled_manifest.get("extension_id")),
            expected_extension_id,
            "extension.yaml.extension_id",
            "extension_runtime_identity_mismatch",
            "extension.yaml and extension-descriptor.yaml must use the same extension_id.",
        ),
        (
            _normalize_text(compiled_manifest.get("version")),
            expected_version,
            "extension.yaml.version",
            "extension_runtime_version_mismatch",
            "extension.yaml version must match extension-descriptor.yaml release_version.",
        ),
    )
    return [
        {"path": path, "code": code, "message": message}
        for actual, expected, path, code, message in checks
        if actual != expected
    ]


def _compiled_bundle_manifest(
    bundle_root: Path,
    descriptor: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compile the co-located bundle and check it names the descriptor's release."""
    from core.infrastructure.extension_runtime.extension_runtime_compile import (
        compile_extension_bundle,
    )

    expected_extension_id = _normalize_text(descriptor.get("extension_id"))
    compiled = compile_extension_bundle(
        bundle_root,
        logical_bundle_dir_name=_logical_bundle_dir_name(expected_extension_id),
    )
    if str(compiled.get("status") or "").strip().lower() != "valid":
        raw_errors = [
            *list(compiled.get("compile_errors") or []),
            *list(compiled.get("validation_errors") or []),
        ]
        errors = [dict(item) for item in raw_errors if isinstance(item, dict)]
        return {}, errors or [
            {
                "path": "extension.yaml",
                "code": "extension_runtime_manifest_invalid",
                "message": "The co-located extension runtime manifest is invalid.",
            }
        ]

    compiled_manifest = dict(compiled.get("compiled_manifest") or {})
    identity_errors = _runtime_identity_errors(
        expected_extension_id=expected_extension_id,
        expected_version=_normalize_text(descriptor.get("release_version")),
        compiled_manifest=compiled_manifest,
    )
    if identity_errors:
        return {}, identity_errors

    if "artifact_bundle" in (compiled_manifest.get("features") or []) and not descriptor.get(
        "artifacts"
    ):
        return {}, [
            {
                "path": "extension.yaml.features",
                "code": "artifact_bundle_missing_artifacts",
                "message": "artifact_bundle requires nonempty descriptor artifacts",
            }
        ]
    return compiled_manifest, []


def _merged_runtime_descriptor(
    descriptor: dict[str, Any],
    compiled_manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Carry the bundle's validated runtime contract into the descriptor."""
    compiled_runtime = compiled_manifest.get("runtime")
    descriptor_runtime = descriptor.get("runtime")
    descriptor_runtime = dict(descriptor_runtime) if isinstance(descriptor_runtime, dict) else {}
    # A normalized artifact descriptor supplies None for an undeclared runtime
    # contract. Preserve the owning bundle's validated executable declaration.
    inherit_contract = descriptor_runtime.get("extension_contract_v2") is None
    if inherit_contract:
        descriptor_runtime.pop("extension_contract_v2", None)
    merged_runtime = {
        **(dict(compiled_runtime) if isinstance(compiled_runtime, dict) else {}),
        **descriptor_runtime,
    }
    runtime_descriptor = dict(descriptor)
    for field in _RUNTIME_DESCRIPTOR_FIELDS:
        if field in compiled_manifest:
            runtime_descriptor[field] = compiled_manifest[field]
    if inherit_contract and "oauth_providers" in compiled_manifest:
        # Release upgrades consume the compiled fingerprints, not raw provider
        # references. Keep that same contract after artifact import/persistence.
        runtime_descriptor["oauth_providers"] = compiled_manifest["oauth_providers"]
    runtime_descriptor["runtime"] = merged_runtime
    from core.application.extensions.composite_actions import composite_action_descriptor_issues
    from core.application.extensions.workflow_connection_slot_bundle import (
        connection_slot_descriptor_issues,
    )

    # An action implemented by a workflow needs both halves of the bundle (ADR 0065);
    # a workflow's connector steps read only the slots it declares (ADR 0068).
    return runtime_descriptor, [
        *composite_action_descriptor_issues(runtime_descriptor),
        *connection_slot_descriptor_issues(runtime_descriptor),
    ]


def descriptor_with_bundle_runtime(
    *,
    descriptor_path: Path,
    normalized_descriptor: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Combine artifact snapshots and the owning executable package runtime."""
    bundle_root = descriptor_path.resolve().parent
    descriptor = {
        **normalized_descriptor,
        "bundle_root": str(bundle_root),
    }
    if not (bundle_root / "extension.yaml").is_file():
        return descriptor, []
    compiled_manifest, errors = _compiled_bundle_manifest(bundle_root, descriptor)
    if errors:
        return descriptor, errors
    return _merged_runtime_descriptor(descriptor, compiled_manifest)
