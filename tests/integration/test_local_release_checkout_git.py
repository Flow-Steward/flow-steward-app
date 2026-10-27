from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.infrastructure.installer.release_handoff import InstallerReleaseHandoff
from core.infrastructure.installer.state_store import InstallerStateStore
from core.infrastructure.release.local_checkout import LocalCheckoutReleaseSource


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _tagged_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "--initial-branch=main")
    _git(origin, "config", "user.name", "Flow Steward Test")
    _git(origin, "config", "user.email", "flow-steward@example.invalid")
    (origin / "VERSION").write_text("0.3.0\n", encoding="utf-8")
    (origin / "README.md").write_text("release\n", encoding="utf-8")
    _git(origin, "add", "VERSION", "README.md")
    _git(origin, "commit", "-m", "release")
    _git(origin, "tag", "-a", "v0.3.0", "-m", "Flow Steward v0.3.0")
    return origin


def test_real_shallow_annotated_tag_checkout_produces_exact_coordinates(tmp_path: Path) -> None:
    origin = _tagged_origin(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(
        (
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "v0.3.0",
            f"file://{origin}",
            str(checkout),
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    coordinates = LocalCheckoutReleaseSource(
        repo_root=checkout,
        repository="example/private",
    ).exact()

    assert str(coordinates.version) == "0.3.0"
    assert coordinates.tag == "v0.3.0"
    assert coordinates.commit_sha == _git(checkout, "rev-parse", "HEAD^{commit}")


def test_real_checkout_rejects_missing_tag_and_tracked_drift(tmp_path: Path) -> None:
    origin = _tagged_origin(tmp_path)
    checkout = tmp_path / "checkout"
    subprocess.run(
        ("git", "clone", f"file://{origin}", str(checkout)),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    source = LocalCheckoutReleaseSource(repo_root=checkout, repository="example/private")

    _git(checkout, "tag", "-d", "v0.3.0")
    with pytest.raises(LifecycleError) as missing:
        source.exact()
    assert missing.value.code == "production_checkout_tag_missing"

    _git(checkout, "fetch", "--depth", "1", "origin", "refs/tags/v0.3.0:refs/tags/v0.3.0")
    (checkout / "README.md").write_text("modified\n", encoding="utf-8")
    with pytest.raises(LifecycleError) as dirty:
        source.exact()
    assert dirty.value.code == "production_checkout_dirty"


def test_real_git_credential_exchange_is_origin_bound_and_ignores_helpers(
    tmp_path: Path,
) -> None:
    repo = _tagged_origin(tmp_path)
    state = InstallerStateStore(tmp_path / "state")
    secret_root = state.root / "secret-input"
    secret_root.mkdir(mode=0o700, parents=True)
    credential = secret_root / "github-release-token"
    secret = "private-release-token"  # pragma: allowlist secret
    credential.write_text(secret + "\n", encoding="utf-8")
    credential.chmod(0o600)

    isolated = tmp_path / "isolated.git"
    _git(repo, "init", "--bare", "--template=", str(isolated))
    helper_marker = tmp_path / "helper-called"
    helper = tmp_path / "hostile-helper.sh"
    helper.write_text(
        "#!/bin/sh\nprintf '%s\\n' called >> \"$HELPER_MARKER\"\nexit 1\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    _git(repo, f"--git-dir={isolated}", "config", "credential.helper", str(helper))

    handoff = InstallerReleaseHandoff(repo_root=repo, state=state)
    with handoff._github_askpass_environment() as isolated_environment:
        environment = dict(os.environ)
        environment.update(isolated_environment)
        environment["HELPER_MARKER"] = str(helper_marker)
        command = (
            "git",
            f"--git-dir={isolated}",
            "-c",
            "credential.helper=",
            "-c",
            "credential.useHttpPath=true",
            "credential",
            "fill",
        )
        accepted = subprocess.run(
            command,
            input=(
                "protocol=https\n"
                "host=github.com\n"
                "username=x-access-token\n"
                "path=example/private.git\n\n"
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
            check=False,
        )
        rejected = subprocess.run(
            command,
            input=(
                "protocol=https\n"
                "host=github.com.evil.invalid\n"
                "username=x-access-token\n"
                "path=example/private.git\n\n"
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
            check=False,
        )

    assert accepted.returncode == 0
    assert f"password={secret}" in accepted.stdout
    assert rejected.returncode != 0
    assert secret not in rejected.stdout + rejected.stderr
    assert not helper_marker.exists()
