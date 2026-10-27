"""AC17-R145: no service an update leaves running may bind a single file from the checkout.

`git checkout` replaces a tracked file with a new inode. A single-file bind mount keeps
the inode it was created with, so the container goes on reading the previous release's
file until it is recreated. Services on our own images are recreated by every update,
because the image changes. Services on a third-party image are not -- their image and
their Compose definition are the same across releases -- and bundled Traefik kept
serving the previous release's routes after an update, which the operator saw as the
OAuth discovery route answering 404 while the update said it had succeeded.

The guard is derived: every service whose image is not one of ours, every bind mount it
has, and whether the source is a regular file of the checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "infra/docker"
COMPOSE_FILES = ("docker-compose.yaml", "docker-compose.prod.yaml")
# Our images: rebuilt for every release, so an update recreates the service.
_OWN_IMAGE_MARKERS = ("${FS_", "${MCP_IMAGE")


def _checkout_file(path: Path) -> bool:
    """A regular file that is part of the source tree -- what a checkout replaces.

    Not `git ls-files`: the gate runs on an exported tree without .git. Installer state
    is never bound by a relative path (it comes through FS_INSTALLER_STATE_DIR), so a
    relative source that is a file is a source-tree file.
    """
    return path.is_file() and path.is_relative_to(ROOT)


def _bind_sources(service: dict) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    for volume in service.get("volumes") or []:
        raw = volume.split(":", 1)[0] if isinstance(volume, str) else volume.get("source", "")
        if isinstance(volume, dict) and volume.get("type") not in (None, "bind"):
            continue
        if not raw.startswith("."):
            continue  # named volume, absolute host path, or an operator-set variable
        sources.append((str(volume), (COMPOSE_DIR / raw).resolve()))
    return sources


def _long_running_third_party(services: dict) -> dict[str, dict]:
    return {
        name: service
        for name, service in services.items()
        if not any(marker in str(service.get("image") or "") for marker in _OWN_IMAGE_MARKERS)
        and str(service.get("restart") or "no") != "no"
    }


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_services_an_update_does_not_recreate_bind_no_tracked_single_file(
    compose_file: str,
) -> None:
    services = yaml.safe_load((COMPOSE_DIR / compose_file).read_text(encoding="utf-8"))["services"]
    candidates = _long_running_third_party(services)

    # Non-vacuity: the service this was found on is examined, and it binds the checkout.
    assert "traefik" in candidates
    assert _bind_sources(candidates["traefik"])

    offenders = [
        f"{name}: {volume}"
        for name, service in candidates.items()
        for volume, path in _bind_sources(service)
        if _checkout_file(path)
    ]

    assert offenders == []


def test_traefik_loads_the_file_the_directory_mount_provides() -> None:
    for compose_file in COMPOSE_FILES:
        traefik = yaml.safe_load((COMPOSE_DIR / compose_file).read_text(encoding="utf-8"))[
            "services"
        ]["traefik"]
        assert "../traefik:/etc/traefik/dynamic:ro" in traefik["volumes"]
        assert (
            "--providers.file.filename=/etc/traefik/dynamic/fs.dynamic.yaml" in (traefik["command"])
        )
    # The overlay that replaces the command must name the same file, and the file exists.
    acme = yaml.safe_load(
        (COMPOSE_DIR / "scalable/tls-bundled-acme.yaml").read_text(encoding="utf-8")
    )["services"]["traefik"]
    assert "--providers.file.filename=/etc/traefik/dynamic/fs.dynamic.yaml" in acme["command"]
    assert (ROOT / "infra/traefik/fs.dynamic.yaml").is_file()
