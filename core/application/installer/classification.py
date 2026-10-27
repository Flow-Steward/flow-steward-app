"""Pure pre-mutation installation-state classification."""

from __future__ import annotations

from core.application.installer.contracts import (
    InstallationClassification,
    InstallationEvidence,
    InstallationKind,
)


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
