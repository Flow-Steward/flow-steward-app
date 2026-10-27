"""The archive release smoke installs is a valid bundle, built without a repo.

Release smoke must prove install, enable, projection and uninstall without
asserting on a shipped bundle. It therefore brings its own, uploaded through the
public install endpoint like any operator archive. What is pinned here is that
the archive it builds is well-formed and complete — a probe that fails to install
would turn every check that depends on it into a mystery on a live stand.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "smoke"))

from synthetic_extension import (
    CONNECTION_TYPE_ID,
    EXTENSION_PREFIX,
    WORKFLOW_ID,
    build_archive,
)


def _entries(archive_bytes: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_the_archive_carries_a_complete_bundle() -> None:
    entries = _entries(build_archive("acme.release-probe-test", "acme_release_probe_test"))

    assert set(entries) == {
        "acme_release_probe_test/extension.yaml",
        "acme_release_probe_test/extension-descriptor.yaml",
        "acme_release_probe_test/ui/ui_manifest.yaml",
        "acme_release_probe_test/ui/actions/actions.yaml",
        "acme_release_probe_test/contracts/connection_types.yaml",
        f"acme_release_probe_test/artifacts/workflows/{WORKFLOW_ID}.yaml",
        "acme_release_probe_test/main.py",
        "acme_release_probe_test/health.py",
        "acme_release_probe_test/README.md",
    }


def test_the_manifest_declares_the_identity_it_was_asked_for() -> None:
    entries = _entries(build_archive("acme.release-probe-test", "acme_release_probe_test"))
    manifest = yaml.safe_load(entries["acme_release_probe_test/extension.yaml"])

    assert manifest["extension_id"] == "acme.release-probe-test"
    assert manifest["kind"] == "tool_provider"
    assert manifest["features"] == ["tool", "action"]


def test_the_probe_declares_a_connection_type_for_project_connections() -> None:
    """A populated-project scenario needs a connection, so the probe offers one."""
    entries = _entries(build_archive("acme.release-probe-test", "acme_release_probe_test"))
    contract = yaml.safe_load(entries["acme_release_probe_test/contracts/connection_types.yaml"])
    declared = contract["connection_types"][0]

    assert declared["connection_type_id"] == CONNECTION_TYPE_ID
    assert declared["config_schema"]["required"] == ["base_url"]


def test_the_probe_identity_is_synthetic() -> None:
    """A probe must never collide with, or be mistaken for, a shipped bundle."""
    assert EXTENSION_PREFIX.startswith("acme.")
    assert "flowsteward" not in EXTENSION_PREFIX


def test_the_probe_publishes_a_workflow_so_a_project_can_be_populated() -> None:
    """A generic project-storage scenario needs something to populate with.

    `WORKFLOW_ID` was declared before the archive carried the artifact, which
    would have left every check that expected a projection with nothing to find.
    """
    entries = _entries(build_archive("acme.release-probe-test", "acme_release_probe_test"))
    descriptor = yaml.safe_load(entries["acme_release_probe_test/extension-descriptor.yaml"])
    declared = descriptor["artifacts"]["workflows"]

    assert [row["workflow_id"] for row in declared] == [WORKFLOW_ID]
    assert all(row["publish"] is True for row in declared)

    workflow = yaml.safe_load(
        entries[f"acme_release_probe_test/artifacts/workflows/{WORKFLOW_ID}.yaml"]
    )
    assert workflow["workflow_id"] == WORKFLOW_ID
    # Readiness rejects a graph with no entry, no real work, or no exit.
    kinds = [step["step_kind"] for step in workflow["steps"]]
    assert "phases" not in workflow
    assert workflow["settings"]["execution_graph"]["entry"] == {
        "kind": "step",
        "id": "probe_transform",
    }
    assert "stop_success" in kinds
    assert any(kind not in {"stop_success", "set_variable"} for kind in kinds)
