from __future__ import annotations

from pathlib import Path

import pytest

from core.application.lifecycle.errors import LifecycleError
from core.application.release.catalog import ReleaseError
from core.infrastructure.installer.process import ProcessOutcome
from core.infrastructure.release.local_checkout import LocalCheckoutReleaseSource


def _runner(
    *,
    head: str = "a" * 40,
    tag: str | None = "a" * 40,
    dirty: bool = False,
):
    def run(command):
        args = tuple(command)
        if args[-2:] == ("rev-parse", "HEAD^{commit}"):
            return ProcessOutcome(args, 0, head + "\n")
        if args[-3:-1] == ("rev-parse", "--verify"):
            return ProcessOutcome(args, 0 if tag is not None else 128, (tag or "") + "\n")
        if args[-2:] in (("diff", "--quiet"), ("--cached", "--quiet")):
            return ProcessOutcome(args, 1 if dirty else 0)
        raise AssertionError(args)

    return run


def test_local_checkout_builds_exact_release_coordinates_without_remote_discovery(
    tmp_path: Path,
) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    coordinates = LocalCheckoutReleaseSource(
        repo_root=tmp_path,
        repository="example/private",
        git_runner=_runner(),
    ).exact()

    assert str(coordinates.version) == "1.2.3"
    assert coordinates.tag == "v1.2.3"
    assert coordinates.commit_sha == "a" * 40
    assert coordinates.repository == "example/private"
    assert coordinates.source == "local tagged checkout"


@pytest.mark.parametrize(
    ("version", "tag", "head", "dirty", "code"),
    [
        ("not-semver", "a" * 40, "a" * 40, False, "production_checkout_version_invalid"),
        ("1.2.3", None, "a" * 40, False, "production_checkout_tag_missing"),
        ("1.2.3", "b" * 40, "a" * 40, False, "production_checkout_tag_mismatch"),
        ("1.2.3", "a" * 40, "a" * 40, True, "production_checkout_dirty"),
    ],
)
def test_local_checkout_fails_closed_before_release_resolution(
    tmp_path: Path,
    version: str,
    tag: str | None,
    head: str,
    dirty: bool,
    code: str,
) -> None:
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")

    release_source = LocalCheckoutReleaseSource(
        repo_root=tmp_path,
        repository="example/private",
        git_runner=_runner(head=head, tag=tag, dirty=dirty),
    )
    with pytest.raises(LifecycleError) as raised:
        release_source.exact()

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("version", "tag", "head", "dirty"),
    [
        ("not-semver", "a" * 40, "a" * 40, False),
        ("1.2.3", None, "a" * 40, False),
        ("1.2.3", "b" * 40, "a" * 40, False),
        ("1.2.3", "a" * 40, "a" * 40, True),
    ],
    ids=["version-invalid", "tag-missing", "tag-elsewhere", "dirty"],
)
def test_probe_answers_none_for_an_ordinary_development_checkout(
    tmp_path: Path, version: str, tag: str | None, head: str, dirty: bool
) -> None:
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")

    assert (
        LocalCheckoutReleaseSource(
            repo_root=tmp_path,
            repository="example/private",
            git_runner=_runner(head=head, tag=tag, dirty=dirty),
        ).probe()
        is None
    )


def test_probe_answers_none_without_version_or_git(tmp_path: Path) -> None:
    def no_git(_command):
        raise FileNotFoundError("git")

    assert (
        LocalCheckoutReleaseSource(repo_root=tmp_path, repository="example/private").probe() is None
    )
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    assert (
        LocalCheckoutReleaseSource(
            repo_root=tmp_path, repository="example/private", git_runner=no_git
        ).probe()
        is None
    )


def test_probe_returns_exact_coordinates_for_a_clean_release_tag(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    coordinates = LocalCheckoutReleaseSource(
        repo_root=tmp_path, repository="example/private", git_runner=_runner()
    ).probe()

    assert coordinates is not None
    assert coordinates.tag == "v1.2.3"
    assert coordinates.commit_sha == "a" * 40


def test_invalid_release_repository_matters_only_at_a_release_tag(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    untagged = LocalCheckoutReleaseSource(
        repo_root=tmp_path, repository="not a repository", git_runner=_runner(tag=None)
    )
    assert untagged.probe() is None

    tagged = LocalCheckoutReleaseSource(
        repo_root=tmp_path, repository="not a repository", git_runner=_runner()
    )
    with pytest.raises(ReleaseError):
        tagged.probe()
