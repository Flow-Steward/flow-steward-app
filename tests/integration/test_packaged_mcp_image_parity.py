"""Source/artifact parity for the packaged MCP image.

Remediation epic 2026-07-29, WS-07 — `AUDIT-064` (P0), and the gate requirement 11
puts on every other MCP row.

The audit found the packaged image shipping MCP entrypoint code that differed
from the repository and missed current scope helpers. Source-only evidence is
therefore not evidence at all for this surface.

These tests hash the MCP entrypoint tree inside a built image and compare it to
the same files in the working tree, and assert the guards this epic added are
actually present in the artifact.

Point them at an image with `FS_TEST_MCP_IMAGE`; they skip when it is unset or
the image is absent, so the suite stays runnable without Docker.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = "core/entrypoints/mcp"

#: Files whose divergence caused, or would hide, AUDIT-064.
CRITICAL_FILES = (
    f"{MCP_DIR}/server_discovery.py",
    f"{MCP_DIR}/server_api_auth.py",
    f"{MCP_DIR}/server_api_client.py",
    f"{MCP_DIR}/server_api_tool.py",
    f"{MCP_DIR}/server_caller_identity.py",
    f"{MCP_DIR}/server_maintenance.py",
    f"{MCP_DIR}/server_runtime.py",
    f"{MCP_DIR}/server_tool_scopes.py",
)

#: Guards this epic added. Their absence from the artifact is the AUDIT-064
#: failure mode restated: the image would answer differently from the source.
REQUIRED_GUARDS = (
    (f"{MCP_DIR}/server_tool_scopes.py", "MCP_TOOL_SCOPES"),
    (f"{MCP_DIR}/server_discovery.py", "McpToolDiscoveryMiddleware"),
    (f"{MCP_DIR}/server_api_auth.py", "AppApiTokenVerifier"),
    (f"{MCP_DIR}/server_api_client.py", "caller credentials"),
    (f"{MCP_DIR}/server_api_tool.py", "_caller_bearer"),
    (f"{MCP_DIR}/server_caller_identity.py", "McpCallerIdentityMiddleware"),
    (f"{MCP_DIR}/server_maintenance.py", "McpMaintenanceMiddleware"),
)


def _image() -> str:
    return os.environ.get("FS_TEST_MCP_IMAGE", "").strip()


def _skip_reason() -> str | None:
    if not _image():
        return "FS_TEST_MCP_IMAGE is not set — no packaged image to verify"
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    probe = subprocess.run(
        ["docker", "image", "inspect", _image()], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        return f"image {_image()!r} is not available locally"
    return None


@pytest.fixture(scope="module", autouse=True)
def require_image():
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def _read_from_image(path: str) -> bytes:
    """Read a file out of the image without running its entrypoint."""
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "cat", _image(), f"/app/{path}"],
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"{path} is missing from the image: {result.stderr.decode()[:300]}")
    return result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestPackagedFilesMatchTheSource:
    @pytest.mark.parametrize("path", CRITICAL_FILES)
    def test_the_packaged_copy_is_byte_identical_to_the_repository(self, path):
        """The exact comparison the audit made, generalized to the whole guard set."""
        packaged = _sha256(_read_from_image(path))
        repository = _sha256((REPO_ROOT / path).read_bytes())

        assert packaged == repository, (
            f"{path} differs between the image and the repository "
            f"(image {packaged[:6]}… vs source {repository[:6]}…)"
        )

    def test_the_whole_mcp_entrypoint_tree_matches(self):
        """A per-file list can go stale; this catches anything else that drifts."""
        listing = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                _image(),
                "-c",
                f"ls /app/{MCP_DIR}/*.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        packaged_names = sorted(
            Path(line).name for line in listing.stdout.split() if line.endswith(".py")
        )
        source_names = sorted(p.name for p in (REPO_ROOT / MCP_DIR).glob("*.py"))

        assert packaged_names == source_names, (
            "the image and the repository disagree on which MCP modules exist"
        )

        mismatched = [
            name
            for name in source_names
            if _sha256(_read_from_image(f"{MCP_DIR}/{name}"))
            != _sha256((REPO_ROOT / MCP_DIR / name).read_bytes())
        ]
        assert not mismatched, f"packaged MCP modules differ from source: {mismatched}"


class TestPackagedGuardsArePresent:
    @pytest.mark.parametrize(("path", "symbol"), REQUIRED_GUARDS)
    def test_the_guard_exists_in_the_artifact(self, path, symbol):
        assert symbol.encode() in _read_from_image(path), (
            f"{symbol} is missing from the packaged {path} — "
            "the image would authorize differently from the source"
        )

    def test_the_scope_table_covers_the_whole_tool_inventory_in_the_image(self):
        packaged = _read_from_image(f"{MCP_DIR}/server_tool_scopes.py").decode()

        # Count mapping entries rather than importing, so this reads the artifact
        # rather than the local interpreter's copy.
        entries = [line for line in packaged.splitlines() if line.strip().startswith('"')]
        assert len(entries) >= 37, f"scope table in the image has only {len(entries)} entries"

    def test_the_image_records_its_build_revision(self):
        """AUDIT-064 asks for immutable, visible build provenance."""
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                _image(),
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        revision = result.stdout.strip()

        assert revision, (
            "the image carries no org.opencontainers.image.revision label, so a deployment cannot tell which commit it is running"
        )
        assert revision != "<no value>", (
            "the image carries no org.opencontainers.image.revision label, so a deployment cannot tell which commit it is running"
        )
