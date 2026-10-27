from __future__ import annotations

import pytest

from core.application.installer import (
    InstallationEvidence,
    InstallationKind,
    classify_installation,
)


def test_no_owned_resources_is_fresh() -> None:
    result = classify_installation(InstallationEvidence())

    assert result.kind is InstallationKind.FRESH
    assert result.reasons == ()


def test_matching_incomplete_journal_is_resumable() -> None:
    result = classify_installation(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=False,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward",
            observed_project_identity="flow-steward",
            durable_secrets_present=True,
            owned_volumes=("flow-steward_pg-fs",),
        )
    )

    assert result.kind is InstallationKind.RESUMABLE
    assert result.reasons == ()


def test_early_journal_before_secret_step_remains_resumable() -> None:
    result = classify_installation(
        InstallationEvidence(
            journal_exists=True,
            config_exists=True,
            env_exists=True,
            journal_completed_steps=("preflight", "configuration"),
            durable_secrets_present=False,
        )
    )

    assert result.kind is InstallationKind.RESUMABLE


def test_completed_journal_with_matching_identity_is_installed() -> None:
    result = classify_installation(
        InstallationEvidence(
            journal_exists=True,
            journal_completed=True,
            config_exists=True,
            env_exists=True,
            project_identity="flow-steward",
            observed_project_identity="flow-steward",
            durable_secrets_present=True,
            owned_volumes=("flow-steward_pg-fs", "flow-steward_rabbitmq-fs"),
        )
    )

    assert result.kind is InstallationKind.INSTALLED


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (
            InstallationEvidence(
                owned_volumes=("flow-steward_pg-fs",),
                config_exists=False,
            ),
            "Durable Flow Steward resources exist without matching installation configuration.",
        ),
        (
            InstallationEvidence(
                journal_exists=True,
                config_exists=True,
                env_exists=True,
                project_identity="flow-steward-a",
                observed_project_identity="flow-steward-b",
                durable_secrets_present=True,
            ),
            "The configured and observed Compose project identities do not match.",
        ),
        (
            InstallationEvidence(
                config_exists=True,
                env_exists=True,
                project_identity="flow-steward",
                observed_project_identity="flow-steward",
                protected_backups_exist=True,
                backup_encryption_key_present=False,
            ),
            "Protected backups exist but FS_BACKUP_ENCRYPTION_KEY is unavailable.",
        ),
        (
            InstallationEvidence(
                journal_exists=True,
                config_exists=True,
                env_exists=True,
                project_identity="flow-steward",
                observed_project_identity="flow-steward",
                journal_completed_steps=("secrets",),
                durable_secrets_present=False,
            ),
            "A partial installation is missing durable secret material.",
        ),
        (
            InstallationEvidence(
                journal_exists=True,
                journal_valid=False,
                config_exists=True,
                durable_secrets_present=True,
            ),
            "The installer journal is unreadable or invalid.",
        ),
        (
            InstallationEvidence(resource_inventory_valid=False),
            "Docker resource inventory could not be read safely.",
        ),
        (
            InstallationEvidence(
                config_exists=True,
                env_exists=True,
                durable_secrets_present=True,
                owned_volumes=("flow-steward_pg-fs",),
            ),
            "An existing installation has no Phase 3 installer journal and cannot be adopted automatically.",
        ),
    ],
)
def test_unsafe_evidence_is_ambiguous(
    evidence: InstallationEvidence,
    reason: str,
) -> None:
    result = classify_installation(evidence)

    assert result.kind is InstallationKind.AMBIGUOUS
    assert reason in result.reasons
