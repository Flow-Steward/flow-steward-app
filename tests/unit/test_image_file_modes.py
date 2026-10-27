"""R144: the Scalable UI was empty on every installation.

`/static/platform-kit/components/*.js` answered 401. In the fs-app image that directory
was drwx------ owned by `fs` (uid 1000), and the service runs as the operator's uid --
1001 on the audited host. Git records no directory modes, a checkout or the candidate
build context takes them from the building host's umask, and COPY keeps whatever mode
the source has (measured: 700 stays 700). Under umask 077 every copied directory is
0700 and every checked-out file 0600.

So the images normalise modes themselves instead of trusting the build host. The rule
is derived from the stages: in every runtime stage, every COPY sets modes, unless its
source is normalised by construction -- the source slices, whose materialiser writes
644/755 itself, or the Traefik binary, which is chmod-ed explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_STAGES = {
    "infra/docker/Dockerfile": {
        "cli",
        "app-runtime",
        "worker-runtime",
        "control-runtime",
        "compact-runtime",
    },
    "infra/mcp/Dockerfile": {"runtime"},
}
NORMALISED_SOURCES = ("--from=source-slices", "--from=compact-traefik")
MODE = "--chmod=u=rwX,go=rX"


def _copies(dockerfile: str) -> dict[str, list[str]]:
    stage = ""
    found: dict[str, list[str]] = {}
    for line in (ROOT / dockerfile).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^FROM\s+\S+(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if match:
            stage = (match.group(1) or "").lower()
            continue
        if line.startswith("COPY "):
            found.setdefault(stage, []).append(line)
    return found


@pytest.mark.parametrize("dockerfile", sorted(RUNTIME_STAGES))
def test_every_runtime_copy_normalises_modes(dockerfile: str) -> None:
    copies = _copies(dockerfile)
    unnormalised = [
        f"{stage}: {line}"
        for stage in RUNTIME_STAGES[dockerfile]
        for line in copies.get(stage, [])
        if MODE not in line
        and "--chmod=0755 " not in line
        and not any(source in line for source in NORMALISED_SOURCES)
    ]

    assert unnormalised == []


@pytest.mark.parametrize("dockerfile", sorted(RUNTIME_STAGES))
def test_the_rule_reaches_every_runtime_stage(dockerfile: str) -> None:
    """Without this the rule above passes on a stage it never found."""
    copies = _copies(dockerfile)

    for stage in RUNTIME_STAGES[dockerfile]:
        assert copies.get(stage), f"{dockerfile}: no COPY found in stage {stage}"


def test_the_ui_assets_that_were_unreadable_are_normalised() -> None:
    """The measured case, named: platform-kit into the app and Compact images."""
    copies = _copies("infra/docker/Dockerfile")

    for stage in ("app-runtime", "compact-runtime"):
        kit = [line for line in copies[stage] if "assets/platform-kit" in line]
        assert kit, (stage, kit)
        assert all(MODE in line for line in kit), (stage, kit)


# --- the other half: an unreadable asset answered 401 ---


def _client(tmp_path: Path):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from core.infrastructure.runtime.static_assets import ServerFaultStaticFiles

    (tmp_path / "kit").mkdir()
    (tmp_path / "kit" / "a.js").write_text("export {};\n", encoding="utf-8")
    app = Starlette(routes=[Mount("/static", ServerFaultStaticFiles(directory=str(tmp_path)))])
    return TestClient(app, raise_server_exceptions=False)


def test_an_unreadable_asset_is_a_server_fault_not_an_authentication_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette maps PermissionError to 401, which reads as "not signed in".

    The denial is simulated at os.stat for that one path: tests may run as root, which
    reads a mode-000 file regardless, and would never see the error.
    """
    import os

    client = _client(tmp_path)
    target = str(tmp_path / "kit" / "a.js")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path) == target:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)

    response = client.get("/static/kit/a.js")

    assert response.status_code == 500


def test_readable_and_missing_assets_answer_as_before(tmp_path: Path) -> None:
    """The green direction: nothing about ordinary serving changes."""
    client = _client(tmp_path)

    assert client.get("/static/kit/a.js").status_code == 200
    assert client.get("/static/kit/missing.js").status_code == 404
