"""The development-only images an official installation builds at its release commit.

Owner 2026-11-21: development is the same official Core images (pinned by digest) plus
three images no release publishes -- the frontend dev server (fs-frontend) and the
audit/test images (fs-test-runner, fs-audit) -- built locally at the release tag, plus
Core code bind-mounted from the checkout. The installer builds them on an official
development install or upgrade; `environment switch production -> development` builds
them for the same release. The frontend is pinned by its local image ID so the exact
image selection stays complete; the other two keep their Compose tags and are verified
by revision label, because lint.sh rebuilds fs-audit itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.application.lifecycle.contracts import ComposeTarget
from core.application.lifecycle.stack_service import StackGatewayError

from . import docker_runtime_support

DEVELOPMENT_EXTRA_SERVICES = ("fs-frontend", "fs-test-runner", "fs-audit")
DEVELOPMENT_UNPINNED_EXTRAS = ("fs-test-runner", "fs-audit")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_FULL_GIT_REVISION = re.compile(r"[0-9a-f]{40}")
_REVISION_FORMAT = '{{index .Config.Labels "org.opencontainers.image.revision"}}'
_EXTRAS_TIMEOUT_SECONDS = 3600
_INSPECT_TIMEOUT_SECONDS = 30


def frontend_local_tag(version: str) -> str:
    """The local tag a development frontend build gets before it is pinned by ID."""
    return f"flow-steward-frontend:{version}-local"


def release_checkout_state(
    project_root: Path, release_commit: str, *, run: Callable[..., Any]
) -> tuple[str, str]:
    """("ready" | "dirty" | "moved", HEAD) for a checkout that must sit at a release.

    Development Core services bind-mount the checkout, so it runs the release only
    when HEAD is the release commit and no tracked file is modified.
    """
    root = str(project_root)
    head = str(
        run(
            ("git", "-C", root, "rev-parse", "HEAD^{commit}"),
            action="release_checkout",
            timeout_seconds=_INSPECT_TIMEOUT_SECONDS,
        ).stdout
        or ""
    ).strip()
    status = str(
        run(
            ("git", "-C", root, "status", "--porcelain", "--untracked-files=no"),
            action="release_checkout",
            timeout_seconds=_INSPECT_TIMEOUT_SECONDS,
        ).stdout
        or ""
    ).strip()
    if status:
        return "dirty", head
    if _FULL_GIT_REVISION.fullmatch(head) is None or head != release_commit:
        return "moved", head
    return "ready", head


def _revision(run: Callable[..., Any], image: str) -> str:
    try:
        outcome = run(
            ("docker", "image", "inspect", "--format", _REVISION_FORMAT, image),
            action="target_image_reuse",
            timeout_seconds=_INSPECT_TIMEOUT_SECONDS,
        )
    except StackGatewayError:
        return ""
    return str(outcome.stdout or "").strip()


def _unpinned_extras_current(
    run: Callable[..., Any], target: ComposeTarget, release_commit: str
) -> bool:
    return all(
        _revision(run, f"{target.project_name}-{service}") == release_commit
        for service in DEVELOPMENT_UNPINNED_EXTRAS
    )


def build_development_extras(
    target: ComposeTarget,
    *,
    project_root: Path,
    environment: Mapping[str, str],
    release_commit: str,
    version: str,
    run: Callable[..., Any],
    compose_command: Callable[[tuple[str, ...]], tuple[str, ...]],
    input_text: str | None = None,
) -> dict[str, str]:
    """Build (or reuse) the extras for `target` and return the frontend pin.

    A frontend already pinned in the target's env file is reused when it exists and
    carries the release commit, together with current audit/test images: a resumed
    switch must render the same environment it staged.
    """
    staged = _staged_frontend(project_root, target)
    if (
        staged
        and _revision(run, staged) == release_commit
        and _unpinned_extras_current(run, target, release_commit)
    ):
        return {"FS_FRONTEND_IMAGE": staged, "FS_FRONTEND_IMAGE_REVISION": release_commit}
    tag = frontend_local_tag(version)
    with docker_runtime_support.temporary_candidate_environment(
        project_root, env_file=target.env_file, environment=environment
    ):
        for action, arguments, overrides in (
            (
                "target_image_pull",
                ("pull", "--ignore-buildable", "--policy", "missing", "sonar-scanner"),
                {},
            ),
            # The process environment wins over any pin: the build gets a local tag.
            (
                "target_image_build",
                ("build", *DEVELOPMENT_EXTRA_SERVICES),
                {"FS_FRONTEND_IMAGE": tag},
            ),
        ):
            run(
                compose_command(arguments),
                action=action,
                timeout_seconds=_EXTRAS_TIMEOUT_SECONDS,
                environment={**environment, **overrides},
                input_text=input_text,
            )
    frontend_id = str(
        run(
            ("docker", "image", "inspect", "--format", "{{.Id}}", tag),
            action="target_image_verify",
            timeout_seconds=_INSPECT_TIMEOUT_SECONDS,
        ).stdout
        or ""
    ).strip()
    if _IMAGE_ID.fullmatch(frontend_id) is None or _revision(run, frontend_id) != release_commit:
        raise StackGatewayError(
            action="target_image_verify",
            detail="The development frontend image was not built at the release commit.",
        )
    if not _unpinned_extras_current(run, target, release_commit):
        raise StackGatewayError(
            action="target_image_verify",
            detail="The development test and audit images were not built at the release commit.",
        )
    return {"FS_FRONTEND_IMAGE": frontend_id, "FS_FRONTEND_IMAGE_REVISION": release_commit}


def _staged_frontend(project_root: Path, target: ComposeTarget) -> str:
    try:
        path = docker_runtime_support.project_path(project_root, target.env_file)
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, StackGatewayError):
        return ""
    for line in lines:
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() == "FS_FRONTEND_IMAGE":
            candidate = value.strip().strip("'\"")
            if _IMAGE_ID.fullmatch(candidate):
                return candidate
    return ""
