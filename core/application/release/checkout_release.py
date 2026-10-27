"""Official release identity for a fresh install of a published release tag."""

from __future__ import annotations

from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import ReleaseCoordinates, ReleaseError
from core.application.release.contracts import ReleaseIdentity
from core.application.release.service import OfficialReleaseSelector

RELEASE_IMAGES_UNAVAILABLE = "release_images_unavailable"


def published_release_for_checkout(
    selector: OfficialReleaseSelector, checkout: ReleaseCoordinates
) -> ReleaseIdentity:
    """Resolve the published stable release whose tag is the checked-out commit.

    A clean checkout exactly at ``v<VERSION>`` installs that release's official images.
    It never falls back to a source build: a release not published as stable, a tag
    GitHub resolves to another commit, an incomplete image set, and an unreachable
    GitHub or registry all stop the install with one stable code and their cause.
    """
    try:
        published = selector.discovery.for_version(str(checkout.version))
        if published.commit_sha != checkout.commit_sha:
            raise ReleaseError(
                f"published {published.tag} is commit {published.commit_sha}, "
                f"the checkout is {checkout.commit_sha}"
            )
        return selector.resolve(published)
    except (ReleaseError, ValueError, OSError) as exc:
        detail = str(exc)
        if exc.__cause__ is not None and str(exc.__cause__) not in detail:
            detail = f"{detail} ({exc.__cause__})"
        raise LifecycleError.dependency(
            code=RELEASE_IMAGES_UNAVAILABLE,
            cause=(
                f"Release {checkout.tag} from {selector.release_repository} has no "
                f"installable official images: {detail}"
            ),
            remediation=(
                f"Wait until {checkout.tag} is published as a stable release with its images "
                "in the configured registries, then rerun ./install.sh; or check out a commit "
                "that is not a release tag to build from source."
            ),
            step="release_policy",
        ) from exc


__all__ = ["RELEASE_IMAGES_UNAVAILABLE", "published_release_for_checkout"]
