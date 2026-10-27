"""ADR 0029, owner decision B: every runtime process creates files group-writable.

Compact and Scalable share the artifact, project and extension trees through fixed
groups on setgid directories; under the default umask 022 a new file lost g+w and the
other profile could read it and not change it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from core.infrastructure.installer.gateway import _COMPACT_BUILD_TARGET

ROOT = Path(__file__).resolve().parents[2]
SITECUSTOMIZE = "infra/docker/sitecustomize.py"
_INSTALL = (
    f"COPY --chmod=u=rwX,go=rX {SITECUSTOMIZE} "
    "/usr/local/lib/python3.12/site-packages/sitecustomize.py"
)


def test_the_runtime_sitecustomize_sets_a_group_writable_umask(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text((ROOT / SITECUSTOMIZE).read_text(encoding="utf-8"))
    environment = dict(os.environ, PYTHONPATH=str(site))

    result = subprocess.run(
        [sys.executable, "-c", "import os; print(oct(os.umask(0)))"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "0o2"


def _stages(dockerfile: Path) -> dict[str, tuple[str, str]]:
    """stage -> (parent, body)"""
    text = dockerfile.read_text(encoding="utf-8")
    parts = re.split(r"^FROM (\S+) AS (\S+)\s*$", text, flags=re.MULTILINE)
    return {parts[i + 1]: (parts[i], parts[i + 2]) for i in range(1, len(parts) - 2, 3)}


def _installs(stages: dict[str, tuple[str, str]], stage: str) -> bool:
    while stage in stages:
        parent, body = stages[stage]
        if _INSTALL in body:
            return True
        stage = parent
    return False


def test_every_runtime_image_the_release_builds_installs_it() -> None:
    """Derived from the targets the release build and the installer actually build."""
    script = (ROOT / "scripts/release/build_candidate.sh").read_text(encoding="utf-8")
    builds = re.findall(
        r"docker build --target (\S+).*?--file \"\$SOURCE_ROOT/(\S+)\"", script, re.S
    )
    targets = {(target, dockerfile) for target, dockerfile in builds}
    targets.add((_COMPACT_BUILD_TARGET, "infra/docker/Dockerfile"))
    assert {target for target, _file in targets} >= {
        "app-runtime",
        "worker-runtime",
        "control-runtime",
        "cli",
        "runtime",
        "compact-runtime",
    }  # non-vacuity: every image of both profiles, MCP included

    # The release ships no extensions, so there is no initial-extensions image.
    assert ("initial-extensions", "infra/docker/Dockerfile") not in targets

    missing = [
        f"{dockerfile}:{target}"
        for target, dockerfile in sorted(targets)
        if not _installs(_stages(ROOT / dockerfile), target)
    ]
    assert missing == []


def test_the_test_and_audit_images_keep_the_default_umask() -> None:
    """Their tests assert the modes code produces under the ordinary umask."""
    stages = _stages(ROOT / "infra/docker/Dockerfile")
    assert not _installs(stages, "audit")
    assert not _installs(stages, "base")
