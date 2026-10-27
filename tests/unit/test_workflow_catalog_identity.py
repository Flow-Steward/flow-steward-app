from __future__ import annotations

from core.application.workflows.workflow_catalog_identity import (
    parse_projected_identity,
    projected_block_key,
    projected_workflow_id,
)


def test_projected_workflow_id_and_block_key_include_prefix_and_parts() -> None:
    assert projected_workflow_id("inst-1", "wf.local") == "wfproj:inst-1:wf.local"
    assert projected_block_key("inst-1", "block.local") == "blkproj:inst-1:block.local"


def test_parse_projected_identity_returns_parts_for_valid_value() -> None:
    parsed = parse_projected_identity("wfproj:inst-1:wf.local")
    assert parsed == {
        "prefix": "wfproj",
        "install_id": "inst-1",
        "local_id": "wf.local",
    }


def test_parse_projected_identity_returns_empty_for_invalid_value() -> None:
    assert parse_projected_identity("") == {}
    assert parse_projected_identity("wfproj:inst-only") == {}
    assert parse_projected_identity("wfproj::local") == {}


def test_projected_identity_respects_length_limit_with_stable_suffix() -> None:
    long_local = "wf-" + ("x" * 400)
    value = projected_workflow_id("install-xyz", long_local)
    assert len(value) <= 255
    assert value.startswith("wfproj:install-xyz:")
    parsed = parse_projected_identity(value)
    assert parsed["prefix"] == "wfproj"
    assert parsed["install_id"] == "install-xyz"
    assert parsed["local_id"]
