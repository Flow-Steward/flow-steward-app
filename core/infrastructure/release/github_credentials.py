"""Deployment-local credential for private GitHub release discovery."""

from __future__ import annotations

import re
from pathlib import Path

from core.infrastructure.security.owner_only_secret import read_owner_only_secret

GITHUB_RELEASE_TOKEN_NAME = "github-release-token"
_HEADER_SAFE_TOKEN_RE = re.compile(r"^[!-~]+$", re.ASCII)


class GitHubReleaseCredentialLoader:
    """Load the optional updater-only token without exposing its value in errors."""

    def __init__(self, installer_state_root: Path) -> None:
        self.path = installer_state_root / "secret-input" / GITHUB_RELEASE_TOKEN_NAME

    def load(self) -> str | None:
        raw = read_owner_only_secret(self.path, optional=True)
        if raw is None:
            return None
        try:
            token = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("GitHub release token must be valid UTF-8") from exc
        if not token:
            raise ValueError("GitHub release token must be non-empty")
        if _HEADER_SAFE_TOKEN_RE.fullmatch(token) is None:
            raise ValueError("GitHub release token must contain header-safe ASCII characters only")
        return token


__all__ = ["GITHUB_RELEASE_TOKEN_NAME", "GitHubReleaseCredentialLoader"]
