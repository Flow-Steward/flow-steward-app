"""The review document of a release upgrade run without the automatic workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application.release.service import PreparedReleaseUpgrade

MAINTENANCE_DISABLE_COMMAND = "flow-steward maintenance disable --confirm"


def upgrade_review(prepared: PreparedReleaseUpgrade) -> dict[str, object]:
    installed = prepared.installed_release
    target = prepared.target_release
    if prepared.transition_binding is not None:
        origin = prepared.transition_binding.installed_origin
        return {
            "status": "ready",
            "installed_origin": "source",
            "source_commit_sha": origin.commit_sha,
            "semantic_ordering": "unknown",
            "installation_mode_change": "source -> official",
            "target_version": target.version,
            "target_tag": target.tag,
            "target_commit_sha": target.commit_sha,
            "target_images": [image.model_dump(mode="json") for image in target.images],
            "deployment_profile": prepared.deployment_profile,
            "platform": prepared.platform,
            "release_source": prepared.release_source,
            "maintenance": "enabled (left enabled after success or failure)",
            "migration_policy": (
                "Installer prechecks migrations and stops before service recreation when one "
                "is required; backup and database upgrade remain explicit operator commands."
            ),
            "resume_command": (
                f"flow-steward release upgrade --target-version {target.version} --confirm"
            ),
            "next_steps": [MAINTENANCE_DISABLE_COMMAND],
        }
    assert installed is not None
    return {
        "status": "already_current" if target == installed else "ready",
        "installed_version": installed.version,
        "target_version": target.version,
        "deployment_profile": prepared.deployment_profile,
        "platform": prepared.platform,
        "release_source": prepared.release_source,
        "maintenance": "enabled (left enabled after success or failure)",
        "migration_policy": (
            "Installer prechecks migrations and stops before service recreation when one "
            "is required; backup and database upgrade remain explicit operator commands."
        ),
        "next_steps": [MAINTENANCE_DISABLE_COMMAND],
    }


__all__ = ["MAINTENANCE_DISABLE_COMMAND", "upgrade_review"]
