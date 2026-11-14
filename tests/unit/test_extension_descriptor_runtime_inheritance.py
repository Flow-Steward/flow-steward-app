"""Artifact descriptors must preserve the owning executable OAuth contract."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from core.application.extensions.command_core import _descriptor_with_bundle_runtime
from core.application.extensions.lifecycle import validate_extension_descriptor_file
from core.application.extensions.oauth_contract import compile_oauth_providers
from core.application.extensions.oauth_release_upgrade import resolve_oauth_release_contract
from core.infrastructure.extension_runtime.extension_runtime import compile_extension_bundle
from tests.support.synthetic_extension_bundle import write_bundle


def test_artifact_descriptor_inherits_executable_oauth_without_changing_fingerprints(tmp_path):
    bundle = write_bundle(tmp_path)
    source_root = Path(
        os.environ.get("PYTEST_SOURCE_REPO_ROOT", Path(__file__).resolve().parents[2])
    )
    shutil.copytree(
        source_root / "tests/fixtures/extensions/fixture_extension_oauth",
        bundle,
        dirs_exist_ok=True,
    )
    manifest_file = bundle / "extension.yaml"
    manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    manifest["extension_id"] = "acme.showcase"
    manifest_file.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    descriptor_file = bundle / "extension-descriptor.yaml"
    validated = validate_extension_descriptor_file(descriptor_file)
    assert validated["valid"], validated.get("errors")
    # Pydantic emits this default even though the artifact descriptor declares
    # no runtime override. This is the import boundary that lost OAuth.
    assert validated["normalized"]["runtime"]["extension_contract_v2"] is None
    compiled = compile_extension_bundle(bundle)
    assert compiled["status"] == "valid", compiled.get("compile_errors")
    executable = compiled["compiled_manifest"]

    persisted, errors = _descriptor_with_bundle_runtime(
        descriptor_path=descriptor_file,
        normalized_descriptor=validated["normalized"],
    )

    assert errors == []
    providers = compile_oauth_providers(persisted)
    assert [provider.provider_id for provider in providers] == ["example"]
    assert [provider.to_dict() for provider in providers] == [
        provider.to_dict() for provider in compile_oauth_providers(executable)
    ]
    assert persisted["callbacks"] == executable["callbacks"]
    assert persisted["action_definitions"] == executable["action_definitions"]
    assert persisted["connection_types"] == executable["connection_types"]
    release_contract = resolve_oauth_release_contract(persisted)
    assert [row["provider_id"] for row in release_contract.get("oauth_providers", [])] == [
        "example"
    ]
    assert release_contract == resolve_oauth_release_contract(executable)

    # An explicit artifact contract remains authoritative, including an empty
    # contract that deliberately declares no OAuth providers.
    explicit = dict(validated["normalized"])
    explicit["runtime"] = {**explicit["runtime"], "extension_contract_v2": {}}
    overridden, errors = _descriptor_with_bundle_runtime(
        descriptor_path=descriptor_file, normalized_descriptor=explicit
    )
    assert errors == []
    assert overridden["runtime"]["extension_contract_v2"] == {}
    assert compile_oauth_providers(overridden) == ()
    assert resolve_oauth_release_contract(overridden) == {}
