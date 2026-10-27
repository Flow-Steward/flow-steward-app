"""One-time installer application contracts and services."""

from core.application.installer.classification import classify_installation
from core.application.installer.contracts import (
    BootstrapVerification,
    ImageCurrency,
    InstallationClassification,
    InstallationEvidence,
    InstallationKind,
    InstallConfig,
    InstallJournal,
    InstallRunResult,
)
from core.application.release.contracts import ReleaseIdentity, ReleaseImageIdentity

__all__ = [
    "BootstrapVerification",
    "ImageCurrency",
    "InstallConfig",
    "InstallJournal",
    "InstallRunResult",
    "InstallationClassification",
    "InstallationEvidence",
    "InstallationKind",
    "ReleaseIdentity",
    "ReleaseImageIdentity",
    "classify_installation",
]
