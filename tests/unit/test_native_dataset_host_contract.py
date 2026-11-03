"""Native Dataset bundles require the structured-query host contract."""

from types import SimpleNamespace

import pytest

from core.application.extensions.extension_descriptor import validate_extension_descriptor
from core.application.extensions.service_support import _runtime_platform_version
from tests.unit.test_extension_contract_v2 import _base_descriptor


@pytest.mark.parametrize("minimum", ["1.2.0", "1.3.0", "1.4.0", "1.5.0"])
def test_default_host_admits_compatible_contract_minors(minimum):
    descriptor = _base_descriptor({})
    descriptor["runtime"]["compatibility"] = {"platform_min": minimum}
    result = validate_extension_descriptor(descriptor)
    assert result["valid"], result["errors"]
    assert validate_extension_descriptor(descriptor, platform_version=_runtime_platform_version())[
        "valid"
    ]


def test_explicit_older_host_refuses_structured_query_bundle():
    descriptor = _base_descriptor({})
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.5.0"}
    result = validate_extension_descriptor(descriptor, platform_version="1.4.0")
    assert result["valid"] is False
    assert any("below required minimum '1.5.0'" in row["message"] for row in result["errors"])


def _runtime_only_descriptor():
    descriptor = _base_descriptor(
        {"resource_query_manifest": [{"query_id": "health", "cursor_mode": "none"}]}
    )
    descriptor.pop("artifacts")
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.5.0"}
    return descriptor


def test_artifact_free_service_cannot_claim_released_host_compatibility():
    descriptor = _runtime_only_descriptor()
    descriptor["runtime"]["compatibility"] = {"platform_min": "1.4.0"}
    result = validate_extension_descriptor(descriptor)
    assert result["valid"] is False
    assert any("1.5.0" in error["message"] for error in result["errors"])


@pytest.mark.parametrize("minimum,valid", [("1.4.0", False), ("1.5.0", True)])
@pytest.mark.parametrize("language", ["json", "JSON"])
def test_json_dataset_with_artifacts_requires_new_host_contract(minimum, valid, language):
    from core.application.extensions.extension_contract_v2 import validate_extension_contract_v2

    descriptor = _base_descriptor(
        {
            "dataset_connectors": [
                {
                    "connector_id": "json_db",
                    "display_name": "JSON database",
                    "connection_schema": {"type": "object"},
                    "secret_schema": {"type": "object"},
                    "query_language": language,
                    "capabilities": {"preview_query": True, "execute_template": True},
                    "safety_profile": {"read_only": True},
                    "preview_support": True,
                    "schema_refresh_support": True,
                    "runtime_adapter_ref": "extensions.warehouse.DatasetConnector",
                }
            ]
        }
    )
    descriptor["runtime"]["compatibility"] = {"platform_min": minimum}
    result = validate_extension_contract_v2(descriptor)
    assert result["valid"] is valid, result["errors"]
    if not valid:
        assert any(error["code"] == "native_dataset_host_minimum" for error in result["errors"])


def test_runtime_only_service_descriptor_imports_without_authoring_artifacts():
    result = validate_extension_descriptor(_runtime_only_descriptor())
    assert result["valid"], result["errors"]
    assert result["descriptor"].artifact_entries() == []
    assert "artifacts" not in result["normalized"]


@pytest.mark.parametrize("artifacts", [None, {}, {"workflows": []}, {"unknown": []}, []])
def test_explicit_empty_or_malformed_artifact_block_is_rejected(artifacts):
    descriptor = _runtime_only_descriptor()
    descriptor["artifacts"] = artifacts
    assert validate_extension_descriptor(descriptor)["valid"] is False


@pytest.mark.parametrize("extension_type", ["artifact_bundle", "workflow_bundle"])
def test_artifact_bundle_requires_authoring_artifacts(extension_type):
    descriptor = _runtime_only_descriptor()
    descriptor["extension_type"] = extension_type
    assert validate_extension_descriptor(descriptor)["valid"] is False


@pytest.mark.parametrize("contract", [None, {}, {"unknown": [{}]}, {"operation_manifest": []}])
def test_artifact_free_service_requires_declared_runtime_interface(contract):
    descriptor = _runtime_only_descriptor()
    descriptor["runtime"]["extension_contract_v2"] = contract
    assert validate_extension_descriptor(descriptor)["valid"] is False


@pytest.mark.parametrize(
    "contract", [{"operation_manifest": [{}]}, {"dataset_connectors": "missing.yaml"}]
)
def test_runtime_only_service_rejects_invalid_runtime_interface(tmp_path, contract):
    import json

    from core.application.extensions.lifecycle import validate_extension_descriptor_file

    descriptor = _runtime_only_descriptor()
    descriptor["runtime"]["extension_contract_v2"] = contract
    path = tmp_path / "extension-descriptor.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    result = validate_extension_descriptor_file(path)
    assert result["valid"] is False
    assert result["errors"]


@pytest.mark.parametrize(
    "boundary,with_descriptor", [("archive", True), ("archive", False), ("import", True)]
)
def test_archive_artifact_bundle_feature_requires_authoring_artifacts(
    tmp_path, boundary, with_descriptor
):
    import io
    import json
    import zipfile

    from core.application.extensions.archive_validation_service import (
        ExtensionArchiveValidationService,
    )
    from core.application.extensions.command_core import ExtensionCommandCore
    from core.application.management.errors import ServiceValidationError
    from tests.unit.test_command_services import TestExtensionCommandServices

    with zipfile.ZipFile(
        io.BytesIO(TestExtensionCommandServices._extension_zip_bytes())
    ) as archive:
        archive.extractall(tmp_path)
    bundle_dir = tmp_path / "acme_vendor_demo"
    import yaml

    path = bundle_dir / "extension.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["features"] = [*manifest.get("features", []), "artifact_bundle"]
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    descriptor = _runtime_only_descriptor()
    descriptor["extension_id"] = "acme.vendor-demo"
    if with_descriptor:
        (bundle_dir / "extension-descriptor.yaml").write_text(
            json.dumps(descriptor), encoding="utf-8"
        )
    command = ExtensionCommandCore(
        context=None, install_release=None, cleanup_release_artifacts=lambda _: None
    )
    validator = ExtensionArchiveValidationService(
        context=SimpleNamespace(command_service=lambda _: command),
        projection=SimpleNamespace(
            extension_version_from_payload=lambda payload: payload["version"]
        ),
    )
    with pytest.raises(
        ServiceValidationError, match=r"artifact_bundle.*artifacts|runtime bundle validation"
    ):
        if boundary == "archive":
            validator.validate_extracted_bundle(extracted_root=tmp_path, bl=None, scope=None)
        else:
            from core.application.management.command_models import (
                ExtensionDescriptorImportFileInput,
            )
            from tests.unit.test_extension_api import _ADMIN_SCOPE, _make_services

            service, *_ = _make_services()
            service.import_descriptor_file(
                ExtensionDescriptorImportFileInput(
                    descriptor_path=str(bundle_dir / "extension-descriptor.yaml")
                ),
                scope=_ADMIN_SCOPE,
                source_kind_override="extension_scan",
            )


@pytest.mark.parametrize("case", ["empty_reference", "shadowing_empty_top_level"])
def test_artifact_free_service_requires_effective_resolved_runtime_interface(tmp_path, case):
    import json

    from core.application.extensions.lifecycle import validate_extension_descriptor_file

    descriptor = _runtime_only_descriptor()
    if case == "empty_reference":
        (tmp_path / "queries.json").write_text("[]", encoding="utf-8")
        descriptor["runtime"]["extension_contract_v2"]["resource_query_manifest"] = "queries.json"
    else:
        descriptor["extension_contract_v2"] = {}
    path = tmp_path / "extension-descriptor.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    result = validate_extension_descriptor_file(path)
    assert result["valid"] is False, result
    assert result["errors"]


def test_artifact_free_service_accepts_nonempty_resolved_runtime_reference(tmp_path):
    import json

    from core.application.extensions.lifecycle import validate_extension_descriptor_file

    descriptor = _runtime_only_descriptor()
    (tmp_path / "queries.json").write_text(
        json.dumps([{"query_id": "health", "cursor_mode": "none"}]), encoding="utf-8"
    )
    descriptor["runtime"]["extension_contract_v2"]["resource_query_manifest"] = "queries.json"
    path = tmp_path / "extension-descriptor.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    result = validate_extension_descriptor_file(path)
    assert result["valid"] is True, result["errors"]
    assert (
        result["normalized"]["extension_contract_v2_normalized"]["resource_query_manifest"][0][
            "query_id"
        ]
        == "health"
    )


@pytest.mark.parametrize("location", ["nested", "top_level"])
def test_artifact_free_service_preserves_valid_effective_llm_interface(tmp_path, location):
    import json

    from core.application.extensions.lifecycle import validate_extension_descriptor_file
    from tests.unit.test_llm_provider_in_process import _subprocess_descriptor

    descriptor = _subprocess_descriptor()
    descriptor.pop("artifacts")
    if location == "top_level":
        descriptor["extension_contract_v2"] = descriptor["runtime"].pop("extension_contract_v2")
    path = tmp_path / "extension-descriptor.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    result = validate_extension_descriptor_file(path)
    assert result["valid"] is True, result["errors"]


@pytest.mark.parametrize("reference", [False, True])
def test_artifact_bearing_bundle_preserves_empty_runtime_contract(tmp_path, reference):
    from core.application.extensions.lifecycle import validate_extension_descriptor_file
    from tests.unit.test_extension_contract_v2 import _write_descriptor

    if reference:
        (tmp_path / "queries.json").write_text("[]", encoding="utf-8")
    contract = {"resource_query_manifest": "queries.json"} if reference else {}
    path = _write_descriptor(tmp_path, _base_descriptor(contract))
    result = validate_extension_descriptor_file(path)
    assert result["valid"] is True, result["errors"]
    assert result["normalized"]["artifacts"]["workflows"][0]["workflow_id"] == "wf_v2"
