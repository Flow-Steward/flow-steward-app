"""Release-reference artifact integrity and exact image provenance regressions."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from core.application.tooling.api_reference import (
    encode_json,
    select_verified_image,
    validate_assets,
    write_assets,
)

REVISION = "54a43366bf404d31c163c269af639de789b9dce6"
RELEASE = "v0.9.18"
ROOT = Path(__file__).resolve().parents[3]


def schema():
    return {
        "openapi": "3.1.0",
        "info": {"title": "Example", "version": "v1"},
        "paths": {"/api/jobs": {"get": {"responses": {"200": {"description": "OK"}}}}},
        "components": {"schemas": {"Job": {"type": "object"}}},
    }


def test_deterministic_manifest_counts_and_checksum(tmp_path):
    public = schema()
    application = deepcopy(public)
    application["paths"]["/api/projects"] = {
        "parameters": [],
        "get": {"responses": {"200": {"description": "OK"}}},
        "post": {"responses": {"201": {"description": "Created"}}},
    }
    write_assets(tmp_path, RELEASE, REVISION, public, application)
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    write_assets(tmp_path, RELEASE, REVISION, public, application)
    assert first == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    manifest = validate_assets(tmp_path, RELEASE, REVISION)
    assert set(manifest) == {"schema_version", "release", "revision", "schemas"}
    assert manifest["schemas"]["application"] == {
        "file": "openapi-application.json",
        "paths": 2,
        "operations": 3,
        "sha256": hashlib.sha256(first["openapi-application.json"]).hexdigest(),
    }
    assert json.loads(first["openapi-public.json"])["info"]["version"] == "v1"


@pytest.mark.parametrize(
    "damage", ["hash", "missing", "release", "revision", "count", "filename", "version"]
)
def test_rejects_invalid_or_incomplete_assets(tmp_path, damage):
    write_assets(tmp_path, RELEASE, REVISION, schema(), schema())
    path = tmp_path / "openapi-manifest.json"
    manifest = json.loads(path.read_text())
    if damage == "hash":
        (tmp_path / "openapi-public.json").write_text("{}")
    elif damage == "missing":
        (tmp_path / "openapi-application.json").unlink()
    elif damage == "release":
        manifest["release"] = "v0.9.17"
    elif damage == "revision":
        manifest["revision"] = "a" * 40
    elif damage == "count":
        manifest["schemas"]["public"]["operations"] = 0
    elif damage == "filename":
        manifest["schemas"]["public"]["file"] = "../openapi-public.json"
    else:
        manifest["schema_version"] = 2
    path.write_bytes(encode_json(manifest))
    with pytest.raises((ValueError, FileNotFoundError)):
        validate_assets(tmp_path, RELEASE, REVISION)


@pytest.mark.parametrize(
    "damage", ["external_ref", "dangling_ref", "servers", "paths", "response", "openapi"]
)
def test_rejects_malformed_or_nonlocal_schema(tmp_path, damage):
    public = schema()
    if damage.endswith("ref"):
        public["components"]["schemas"]["Job"]["$ref"] = (
            "https://example.org/schema"
            if damage == "external_ref"
            else "#/components/schemas/Missing"
        )
    elif damage == "servers":
        public["servers"] = [{"url": "https://demo.example.org"}]
    elif damage == "paths":
        public["paths"] = []
    elif damage == "response":
        del public["paths"]["/api/jobs"]["get"]["responses"]
    else:
        public["openapi"] = "not-openapi"
    with pytest.raises(ValueError):
        write_assets(tmp_path, RELEASE, REVISION, public, schema())


def evidence(digest="b" * 64):
    return {
        "version": "0.9.18",
        "revision": REVISION,
        "registries": [
            {
                "status": "verified",
                "namespace": "ghcr.io/flow-steward",
                "images": [
                    {
                        "role": "fs-app",
                        "reference": "ghcr.io/flow-steward/flow-steward-app:0.9.18",
                        "platforms": {
                            "linux/amd64": {
                                "manifest_digest": "sha256:" + digest,
                                "config_digest": "sha256:" + "c" * 64,
                            }
                        },
                    }
                ],
            }
        ],
    }


def test_selects_immutable_verified_platform_image_and_rejects_conflicts():
    selected = select_verified_image([evidence()], RELEASE, REVISION)
    assert selected["image"] == "ghcr.io/flow-steward/flow-steward-app@sha256:" + "b" * 64
    assert selected["config_digest"] == "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="conflict"):
        select_verified_image([evidence(), evidence("d" * 64)], RELEASE, REVISION)
    invalid = evidence()
    invalid["revision"] = "a" * 40
    with pytest.raises(ValueError):
        select_verified_image([invalid], RELEASE, REVISION)
    invalid = evidence()
    invalid["registries"][0]["status"] = "incomplete"
    with pytest.raises(ValueError):
        select_verified_image([invalid], RELEASE, REVISION)


def test_release_workflow_publishes_reference_after_images_and_supports_bootstrap():
    publication = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    job = publication["jobs"]["api-reference"]
    assert "publish" in job["needs"]
    assert job["uses"] == "./.github/workflows/publish-api-reference.yml"
    assert job["secrets"] == "inherit"
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-api-reference.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert {"workflow_call", "workflow_dispatch"} <= set(events)
    text = (ROOT / ".github/workflows/publish-api-reference.yml").read_text()
    assert "api-reference-release" in text
    assert "DOCS_SYNC_APP_PRIVATE_KEY" in text
    assert "--network none" in text
    assert "org.opencontainers.image.revision" in text
    assert "@apidevtools/swagger-parser@13.1.0" in text
    assert "external: false, file: false, http: false" in text
    assert text.index("Validate full OpenAPI") < text.index("Upload immutable API reference assets")
    assert "--clobber" not in text
    assert "deleteReleaseAsset" not in text


def test_offline_export_assembles_real_canonical_routes(tmp_path, monkeypatch):
    import runpy

    monkeypatch.setenv("FS_EMBEDDED_IMAGE_REVISION", REVISION)
    exporter = runpy.run_path(str(ROOT / "core/entrypoints/cli/scripts/export-api-reference.py"))
    exporter["export"](tmp_path, RELEASE, REVISION)
    manifest = validate_assets(tmp_path, RELEASE, REVISION)
    public = json.loads((tmp_path / "openapi-public.json").read_bytes())
    application = json.loads((tmp_path / "openapi-application.json").read_bytes())
    assert manifest["schemas"]["public"]["paths"] == 10
    assert manifest["schemas"]["public"]["operations"] == 10
    assert public["paths"]["/api/workflow-runs"]["post"]["operationId"] == "startWorkflowRun"
    assert manifest["schemas"]["application"]["paths"] > 100
    assert "/api/projects" in application["paths"]
    assert application["components"]["schemas"]


@pytest.mark.parametrize("existing", ["none", "identical", "different"])
def test_release_asset_upload_is_immutable_and_preflights_every_file(tmp_path, existing):
    import subprocess

    for name in ("openapi-public.json", "openapi-application.json", "openapi-manifest.json"):
        (tmp_path / name).write_text("{}\n")
    adapter = ROOT / "core/entrypoints/cli/scripts/api-reference-release.cjs"
    script = r"""
const {publishAssets} = require(process.argv[1]);
const mode = process.argv[3];
let uploads = 0;
const existing = mode === 'none' ? [] : [{id: 1, name: 'openapi-manifest.json'}];
const github = {
  paginate: async () => existing,
  rest: {repos: {
    listReleaseAssets: () => {},
    getReleaseAsset: async () => ({data: Buffer.from(mode === 'identical' ? '{}\n' : 'changed')}),
    uploadReleaseAsset: async () => {uploads++;},
  }},
};
(async () => {
  try {
    await publishAssets({github, context: {repo: {}}, release: {id: 2}, directory: process.argv[2]});
    if (uploads !== (mode === 'none' ? 3 : 2)) throw new Error('wrong upload count');
    if (mode === 'different') throw new Error('expected conflict');
  } catch (error) {
    if (mode !== 'different' || !error.message.includes('Immutable release asset differs') || uploads !== 0) throw error;
  }
})();
"""
    subprocess.run(
        ["node", "-e", script, str(adapter), str(tmp_path), existing], check=True, timeout=15
    )


def test_schema_property_named_servers_is_preserved(tmp_path):
    public = schema()
    public["components"]["schemas"]["Job"]["properties"] = {
        "servers": {"type": "array", "items": {"type": "string"}}
    }
    write_assets(tmp_path, RELEASE, REVISION, public, schema())
    validate_assets(tmp_path, RELEASE, REVISION)


def test_release_metadata_moves_misplaced_media_description_without_contract_changes():
    from core.application.tooling.api_reference import normalize_release_metadata

    original = schema()
    body = {
        "required": True,
        "description": "Existing request documentation.",
        "content": {
            "application/json": {
                "description": "Approval identity and idempotency requirements.",
                "schema": {"$ref": "#/components/schemas/Job"},
                "example": {"approved": True},
            }
        },
    }
    original["paths"]["/api/jobs"]["get"]["requestBody"] = deepcopy(body)
    normalized = normalize_release_metadata(original)
    actual = normalized["paths"]["/api/jobs"]["get"]["requestBody"]
    assert (
        actual["description"]
        == body["description"] + "\n\n" + body["content"]["application/json"]["description"]
    )
    assert actual["required"] is True
    assert actual["content"]["application/json"] == {
        "schema": {"$ref": "#/components/schemas/Job"},
        "example": {"approved": True},
    }
    assert original["paths"]["/api/jobs"]["get"]["requestBody"] == body
    assert normalize_release_metadata(normalized) == normalized


def test_api_reference_runs_after_reused_publication_with_skipped_native_build():
    publication = yaml.safe_load((ROOT / ".github/workflows/release-images.yml").read_text())
    jobs = publication["jobs"]
    # Retained archive publication intentionally skips native builds. Its child
    # must override GitHub's implicit success() across skipped ancestors too.
    assert jobs["native"]["if"] == "needs.resolve.outputs.reuse == 'false'"
    assert "always()" in jobs["publish"]["if"]
    assert "needs.native.result == 'skipped'" in jobs["publish"]["if"]
    reference = jobs["api-reference"]
    assert reference["needs"] == ["resolve", "publish"]
    assert " ".join(reference["if"].split()) == (
        "${{ always() && needs.resolve.result == 'success' && needs.publish.result == 'success' }}"
    )
