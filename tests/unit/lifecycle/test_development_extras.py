"""Development extras of an official release, built for `environment switch`.

Owner 2026-11-21: development is the same official Core images, the development extras
(fs-frontend, fs-test-runner, fs-audit) built locally at the release tag, and Core code
bind-mounted from the checkout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import StackGatewayError
from core.infrastructure.lifecycle.development_extras import (
    build_development_extras,
    release_checkout_state,
)

_COMMIT = "1" * 40
_FRONTEND_ID = "sha256:" + "f" * 64


def _target() -> ComposeTarget:
    return ComposeTarget(
        stack_environment="development",
        file_storage="bundled",
        env_file=".env",
        compose_files=("infra/docker/docker-compose.yaml",),
        compose_profiles=("development",),
        required_services=("fs-app", "fs-frontend"),
        runtime_services=("fs-app", "fs-frontend"),
        project_name="dev-project",
    )


class _Docker:
    def __init__(self, *, revisions: dict[str, str], head: str = _COMMIT, status: str = ""):
        self.revisions = revisions
        self.head = head
        self.status = status
        self.commands: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, command, **kwargs):
        command = tuple(command)
        self.commands.append((command, kwargs))
        if command[-2:] == ("rev-parse", "HEAD^{commit}"):
            return SimpleNamespace(stdout=self.head + "\n")
        if "status" in command:
            return SimpleNamespace(stdout=self.status)
        if command[:5] == ("docker", "image", "inspect", "--format", "{{.Id}}"):
            return SimpleNamespace(stdout=_FRONTEND_ID + "\n")
        if command[:4] == ("docker", "image", "inspect", "--format"):
            if command[-1] not in self.revisions:
                raise StackGatewayError(action="target_image_reuse", detail="missing")
            return SimpleNamespace(stdout=self.revisions[command[-1]] + "\n")
        return SimpleNamespace(stdout="")


def _build(tmp_path: Path, docker: _Docker) -> dict[str, str]:
    return build_development_extras(
        _target(),
        project_root=tmp_path,
        environment={"FS_BUILD_SHA": _COMMIT, "FS_FRONTEND_IMAGE": ""},
        release_commit=_COMMIT,
        version="0.9.23",
        run=docker.run,
        compose_command=lambda arguments: ("docker", "compose", *arguments),
    )


_CURRENT = {
    _FRONTEND_ID: _COMMIT,
    "dev-project-fs-test-runner": _COMMIT,
    "dev-project-fs-audit": _COMMIT,
}


@pytest.mark.parametrize(
    ("status", "head", "expected"),
    (("", _COMMIT, "ready"), (" M core/x.py\n", _COMMIT, "dirty"), ("", "2" * 40, "moved")),
)
def test_release_checkout_state_reads_cleanliness_and_head(
    tmp_path: Path, status: str, head: str, expected: str
) -> None:
    """The bind mounts run checkout code: it must be clean and at the release commit."""
    docker = _Docker(revisions={}, head=head, status=status)

    state, observed_head = release_checkout_state(tmp_path, _COMMIT, run=docker.run)

    assert (state, observed_head) == (expected, head)


def test_extras_build_only_the_development_images_and_pin_the_frontend(tmp_path: Path) -> None:
    """Only the three unpublished images are built, the frontend under a local tag."""
    docker = _Docker(revisions=_CURRENT)

    pin = _build(tmp_path, docker)

    assert pin == {"FS_FRONTEND_IMAGE": _FRONTEND_ID, "FS_FRONTEND_IMAGE_REVISION": _COMMIT}
    builds = [(c, k) for c, k in docker.commands if c[:3] == ("docker", "compose", "build")]
    assert [c for c, _k in builds] == [
        ("docker", "compose", "build", "fs-frontend", "fs-test-runner", "fs-audit")
    ]
    assert builds[0][1]["environment"]["FS_FRONTEND_IMAGE"] == "flow-steward-frontend:0.9.23-local"


@pytest.mark.parametrize("stale", (_FRONTEND_ID, "dev-project-fs-audit"))
def test_extras_built_at_another_revision_are_refused(tmp_path: Path, stale: str) -> None:
    docker = _Docker(revisions={**_CURRENT, stale: "2" * 40})

    with pytest.raises(StackGatewayError, match="release commit"):
        _build(tmp_path, docker)


def test_a_resumed_switch_reuses_the_frontend_it_staged(tmp_path: Path) -> None:
    """A resumed switch must render the environment it staged, so nothing is rebuilt."""
    (tmp_path / ".env").write_text(f"FS_FRONTEND_IMAGE={_FRONTEND_ID}\n", encoding="utf-8")
    docker = _Docker(revisions=_CURRENT)

    pin = _build(tmp_path, docker)

    assert pin["FS_FRONTEND_IMAGE"] == _FRONTEND_ID
    assert not any(c[:3] == ("docker", "compose", "build") for c, _k in docker.commands)


@pytest.mark.parametrize(
    ("pending", "handoff_target", "expected"),
    (
        (None, None, "installed"),
        ("installed", "installed", "installed"),
        ("other", None, "refused"),
        (None, "other", "refused"),
    ),
)
def test_switch_keeps_the_installed_release_and_refuses_one_in_flight(
    pending: str | None, handoff_target: str | None, expected: str
) -> None:
    """The switch keeps the installed identity; mid-upgrade it is refused by name."""
    from core.application.lifecycle.errors import LifecycleError
    from core.entrypoints.cli.lifecycle_runtime import _installed_official_release

    releases = {"installed": object(), "other": object(), None: None}
    state = SimpleNamespace(
        load_installed_release=lambda: releases["installed"],
        load_pending_release=lambda: releases[pending],
        load_release_handoff=lambda: (
            SimpleNamespace(target=releases[handoff_target]) if handoff_target else None
        ),
    )

    if expected == "refused":
        with pytest.raises(LifecycleError) as raised:
            _installed_official_release(state)
        assert raised.value.code == "environment_switch_release_in_flight"
    else:
        assert _installed_official_release(state) is releases["installed"]
