from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.infrastructure.release.github_credentials import GitHubReleaseCredentialLoader


def test_github_release_credential_is_optional_and_owner_only(tmp_path: Path) -> None:
    loader = GitHubReleaseCredentialLoader(tmp_path)
    assert loader.load() is None

    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    token = root / "github-release-token"
    token.write_text("private-token\n", encoding="utf-8")
    token.chmod(0o600)

    assert loader.load() == "private-token"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-only contract")
@pytest.mark.parametrize("mode", (0o640, 0o604, 0o644))
def test_github_release_credential_rejects_unsafe_file_mode(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    token = root / "github-release-token"
    token.write_text("must-not-appear", encoding="utf-8")
    token.chmod(mode)

    loader = GitHubReleaseCredentialLoader(tmp_path)
    with pytest.raises(ValueError, match="owner-only") as raised:
        loader.load()

    assert "must-not-appear" not in str(raised.value)


def test_github_release_credential_rejects_symlink_empty_and_oversized(
    tmp_path: Path,
) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    outside.chmod(0o600)
    token = root / "github-release-token"
    token.symlink_to(outside)
    loader = GitHubReleaseCredentialLoader(tmp_path)
    with pytest.raises(ValueError, match="owner-only"):
        loader.load()

    token.unlink()
    token.write_text("\n", encoding="utf-8")
    token.chmod(0o600)
    loader = GitHubReleaseCredentialLoader(tmp_path)
    with pytest.raises(ValueError, match="non-empty"):
        loader.load()

    token.write_bytes(b"x" * 4097)
    loader = GitHubReleaseCredentialLoader(tmp_path)
    with pytest.raises(ValueError, match="maximum size"):
        loader.load()


@pytest.mark.parametrize(
    "value",
    (
        "private-token\nsecond-line",
        "private-token\n\n",
        "private token",
        "private-token\t",
        "privaté-token",
        "private-token\x00suffix",
    ),
)
def test_github_release_credential_rejects_header_unsafe_contents_without_echoing_secret(
    tmp_path: Path,
    value: str,
) -> None:
    root = tmp_path / "secret-input"
    root.mkdir(mode=0o700)
    token = root / "github-release-token"
    token.write_bytes(value.encode("utf-8"))
    token.chmod(0o600)

    loader = GitHubReleaseCredentialLoader(tmp_path)
    with pytest.raises(ValueError, match="header-safe ASCII") as raised:
        loader.load()

    assert value not in str(raised.value)
