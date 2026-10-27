"""Read exact production release coordinates from an existing tagged checkout."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import (
    ReleaseCoordinates,
    ReleaseError,
    ReleaseVersion,
    validated_release_repository,
)
from core.infrastructure.installer.process import ProcessOutcome, SubprocessExecutor

GitRunner = Callable[[Sequence[str]], ProcessOutcome]
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class LocalCheckoutReleaseSource:
    """Validate one local exact tag without fetching or changing Git state."""

    def __init__(
        self,
        *,
        repo_root: Path,
        repository: str,
        git_runner: GitRunner | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.repository = validated_release_repository(repository)
        executor = SubprocessExecutor()
        self.git_runner = git_runner or (
            lambda command: executor.run(command, cwd=self.repo_root, timeout_seconds=30)
        )

    def exact(self) -> ReleaseCoordinates:
        version = self._version()
        head = self._head()
        tag = version.tag
        tagged = self._git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
        if tagged.returncode != 0:
            raise self._failure(
                "production_checkout_tag_missing",
                f"Exact local production tag {tag} is unavailable.",
                f"Fetch tags and check out {tag}, then rerun ./install.sh.",
            )
        tagged_commit = tagged.stdout.strip().lower()
        if _FULL_GIT_SHA_RE.fullmatch(tagged_commit) is None or tagged_commit != head:
            raise self._failure(
                "production_checkout_tag_mismatch",
                f"Exact local production tag {tag} does not point to checkout HEAD.",
                f"Check out {tag} exactly, then rerun ./install.sh.",
            )
        for args in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
            outcome = self._git(*args)
            if outcome.returncode == 1:
                raise self._failure(
                    "production_checkout_dirty",
                    "Tracked release files contain local modifications.",
                    "Commit, stash, or revert tracked changes before production installation.",
                )
            if outcome.returncode != 0:
                raise self._failure(
                    "production_checkout_dirty",
                    "Git could not verify the production checkout state.",
                    "Repair the checkout and rerun ./install.sh.",
                )
        return ReleaseCoordinates(
            version=version,
            tag=tag,
            commit_sha=head,
            repository=self.repository,
            source="local tagged checkout",
        )

    def _version(self) -> ReleaseVersion:
        try:
            raw = (self.repo_root / "VERSION").read_text(encoding="utf-8").strip()
            return ReleaseVersion.parse(raw)
        except (OSError, UnicodeError, ReleaseError) as exc:
            raise self._failure(
                "production_checkout_version_invalid",
                "The production checkout VERSION is absent or not canonical X.Y.Z.",
                "Check out an exact production release tag and rerun ./install.sh.",
            ) from exc

    def _head(self) -> str:
        outcome = self._git("rev-parse", "HEAD^{commit}")
        head = outcome.stdout.strip().lower()
        if outcome.returncode != 0 or _FULL_GIT_SHA_RE.fullmatch(head) is None:
            raise self._failure(
                "production_checkout_tag_mismatch",
                "The production checkout HEAD is not a full Git commit.",
                "Repair the Git checkout and rerun ./install.sh.",
            )
        return head

    def _git(self, *args: str) -> ProcessOutcome:
        return self.git_runner(("git", "-C", str(self.repo_root), *args))

    @staticmethod
    def _failure(code: str, cause: str, remediation: str) -> LifecycleError:
        return LifecycleError.stable_failure(
            code=code,
            cause=cause,
            remediation=remediation,
            step="production_checkout",
        )


__all__ = ["LocalCheckoutReleaseSource"]
