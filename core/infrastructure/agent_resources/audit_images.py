"""Read-only discovery of ready audit images; canonical shell guard revalidates before use."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from core.infrastructure.agent_resources.docker import DockerResources

TOOLCHAIN_LABEL = "org.flowsteward.audit-toolchain"


def select(script: Path, docker: DockerResources) -> str | None:
    try:
        result = subprocess.run(
            ["bash", str(script), "--audit-toolchain-digest"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("canonical audit toolchain lookup failed") from exc
    wanted = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[a-f0-9]{40}", wanted):
        raise ValueError("canonical audit toolchain lookup returned no valid digest")
    candidates = docker.command(
        "image", "ls", "-q", "--no-trunc", "--filter", f"label={TOOLCHAIN_LABEL}={wanted}"
    ).split()
    for identity in sorted(set(candidates)):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
            raise ValueError("audit image lookup returned no immutable identity")
        data = docker.inspect("image_tag", identity)
        if (
            data
            and data.get("Id") == identity
            and ((data.get("Config") or {}).get("Labels") or {}).get(TOOLCHAIN_LABEL) == wanted
        ):
            return identity
    return None
