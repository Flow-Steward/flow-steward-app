"""Pure pre-mutation installation-state classification."""

from __future__ import annotations

from core.application.installer.contracts import (
    InstallationClassification,
    InstallationEvidence,
    InstallationKind,
)
from core.application.installer.errors import InstallerFailure


def classify_installation(evidence: InstallationEvidence) -> InstallationClassification:
    """Classify observed state without generating credentials or changing resources."""
    reasons: list[str] = []
    has_runtime_resources = bool(evidence.owned_containers or evidence.owned_volumes)

    if evidence.journal_exists and not evidence.journal_valid:
        reasons.append("The installer journal is unreadable or invalid.")
    if not evidence.resource_inventory_valid:
        reasons.append("Docker resource inventory could not be read safely.")
    if evidence.protected_backups_exist and not evidence.backup_encryption_key_present:
        reasons.append("Protected backups exist but FS_BACKUP_ENCRYPTION_KEY is unavailable.")
    if has_runtime_resources and not evidence.config_exists:
        reasons.append(
            "Durable Flow Steward resources exist without matching installation configuration."
        )
    if evidence.config_exists and not evidence.journal_exists:
        reasons.append(
            "An existing installation has no Phase 3 installer journal and cannot be adopted automatically."
        )
    if (
        evidence.project_identity
        and evidence.observed_project_identity
        and evidence.project_identity != evidence.observed_project_identity
    ):
        reasons.append("The configured and observed Compose project identities do not match.")
    durable_secrets_expected = bool(
        has_runtime_resources
        or evidence.journal_completed
        or "secrets" in evidence.journal_completed_steps
        or (evidence.config_exists and not evidence.journal_exists)
    )
    if durable_secrets_expected and not evidence.durable_secrets_present:
        reasons.append("A partial installation is missing durable secret material.")
    if evidence.journal_completed and not (evidence.config_exists and evidence.env_exists):
        reasons.append("The completed installer journal has no matching configuration files.")

    resources = (
        *(f"container:{name}" for name in evidence.owned_containers),
        *(f"volume:{name}" for name in evidence.owned_volumes),
    )
    if reasons:
        return InstallationClassification(
            kind=InstallationKind.AMBIGUOUS,
            reasons=tuple(dict.fromkeys(reasons)),
            resources=resources,
        )
    if not evidence.has_owned_resources:
        return InstallationClassification(kind=InstallationKind.FRESH)
    if evidence.journal_completed:
        return InstallationClassification(kind=InstallationKind.INSTALLED, resources=resources)
    return InstallationClassification(kind=InstallationKind.RESUMABLE, resources=resources)


def require_unambiguous_installation(classification: InstallationClassification) -> None:
    """Refuse an unsafe observed state before image policy or any installation change."""
    if classification.kind is not InstallationKind.AMBIGUOUS:
        return
    detail = " ".join(classification.reasons)
    if classification.resources:
        detail += " Detected resources: " + ", ".join(classification.resources) + "."
    if any("no Phase 3 installer journal" in reason for reason in classification.reasons):
        remediation = (
            "The existing pre-Phase3 stack was preserved. Do not delete its volumes or "
            "configuration; continue managing it with ./flow-steward. Automatic adoption "
            "is intentionally unsupported in Phase 3, so use a clean checkout and distinct "
            "Compose project identity for a new installation."
        )
    elif any(
        "without matching installation configuration" in reason for reason in classification.reasons
    ):
        remediation = (
            "The detected Docker resources were preserved. Removing the checkout does not "
            "remove its containers or volumes. Restore their matching installation "
            "configuration, or use a distinct Compose project identity for a separate "
            "installation. Inspect the listed resources before any explicit cleanup."
        )
    else:
        remediation = (
            "The detected resources were preserved. Run ./flow-steward doctor and "
            "resolve the reported identity or secret mismatch without deleting volumes."
        )
    raise InstallerFailure(
        code="ambiguous_installation_state",
        cause=detail,
        remediation=remediation,
        exit_code=9,
        retryable=False,
        step="classification",
    )
