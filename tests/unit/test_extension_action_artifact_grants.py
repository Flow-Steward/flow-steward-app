"""Direct extension actions receive the artifact grants they ask for.

A direct action (``POST /api/extensions/{id}/action/{action}``) accepted
``artifact_output_specs`` and dropped them: no composition root gave the
action service an artifact plane, so File Transfer ``fetch_file`` answered
"Artifact descriptor not found for role 'output'" outside a workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.application.extensions import operation_targets
from core.application.extensions.artifact_dataset_plane_v2 import ArtifactDatasetPlane
from core.application.extensions.artifact_storage import LocalFilesystemStorageAdapter
from core.application.extensions.runtime_grants import (
    FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV,
)
from core.application.management.command_service_runtime import (
    create_extension_binding_service,
)
from core.application.management.errors import ServiceUnavailableError
from core.application.management.requests import ExtensionActionRequest
from core.application.management.scope import ScopeContext
from core.infrastructure.extension_runtime import extension_runtime

ROOT = Path(__file__).resolve().parents[2]


def _extension() -> dict[str, Any]:
    return {
        "extension_key": "fixture.files",
        "extension_id": "fixture.files",
        "extension_install_id": "install-1",
        "enabled": True,
        "required_scopes": ["extension:invoke"],
        "operation_manifest": [
            {"operation_id": "fetch_file", "connection_type_ids": ["dropbox_account"]}
        ],
        "action_definitions": [
            {
                "action_id": "fetch_file",
                "handler": {"mode": "extension"},
                "required_scopes": ["extension:invoke"],
            }
        ],
    }


class _Store:
    def get_project_connection(self, **_kwargs):
        return {
            "connection_id": "connection-1",
            "account_id": "account-1",
            "project_id": "project-1",
            "extension_id": "fixture.files",
            "extension_install_id": "install-1",
            "connection_type": "dropbox_account",
            "enabled": True,
        }


def _service(get_artifact_plane=None):
    extension = _extension()
    return create_extension_binding_service(
        get_logger=_Store,
        get_operation_store=_Store,
        get_project_by_id=lambda _project_id: {"id": "project-1", "account_id": "account-1"},
        resolve_extension_runtime_record=lambda _extension_id: (extension, _Store()),
        extension_identifier=lambda _extension: "fixture.files",
        extension_operation_handler_mode=lambda *_args, **_kwargs: "extension",
        extension_operation_failure_status=lambda _invocation: 400,
        get_artifact_plane=get_artifact_plane,
    )


def _fetch_request() -> ExtensionActionRequest:
    # The body the stand sent for File Transfer fetch_file.
    return ExtensionActionRequest(
        target={"connection_id": "connection-1"},
        input={"path": "IMG_7337.pdf"},
        artifact_output_specs=[
            {
                "binding_key": "fetched_file",
                "filename": "download.bin",
                "content_type": "application/octet-stream",
                "max_size_bytes": 209715200,
            }
        ],
    )


def _scope() -> ScopeContext:
    return ScopeContext(
        account_id="account-1",
        project_id="project-1",
        allowed_project_ids=frozenset({"project-1"}),
    )


@pytest.fixture(autouse=True)
def _no_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        operation_targets,
        "_resolve_extension_owned_oauth_runtime",
        lambda *_args, **_kwargs: {"access_token": "runtime-token"},
    )


def test_direct_action_output_spec_reaches_the_extension_as_a_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(FS_EXTENSION_TRUSTED_LOCAL_FS_ARTIFACT_GRANTS_ENV, "1")
    plane = ArtifactDatasetPlane(
        storage=LocalFilesystemStorageAdapter(root_dir=str(tmp_path / "artifacts"))
    )
    seen: dict[str, Any] = {}

    def run_extension_action(*_args, **kwargs):
        outputs = list(kwargs.get("artifact_outputs") or [])
        seen["outputs"] = outputs
        for descriptor in outputs:
            plane.write_artifact_content(
                str(descriptor["artifact_id"]),
                body=b"%PDF-1.7",
                content_type="application/octet-stream",
            )
        return {"status": "succeeded", "response": {"ok": True, "result": {}}}

    monkeypatch.setattr(extension_runtime, "run_extension_action", run_extension_action)

    result = _service(lambda: plane).execute_action(
        "fixture.files", "fetch_file", _fetch_request(), scope=_scope()
    )

    [descriptor] = seen["outputs"]
    assert descriptor["binding_key"] == "fetched_file"
    assert descriptor["role"] == "output"
    assert descriptor["filename"] == "download.bin"
    artifact_id = str(descriptor["artifact_id"])
    assert result.action["grant_outputs"] == {"fetched_file": f"artifact:{artifact_id}"}
    stored = plane.get_artifact(artifact_id)
    assert stored is not None
    assert stored["size_bytes"] == len(b"%PDF-1.7")


def test_direct_action_with_grant_fields_and_no_plane_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **_kwargs: pytest.fail("grant fields must not be dropped silently"),
    )

    service = _service()
    request = _fetch_request()
    scope = _scope()
    with pytest.raises(ServiceUnavailableError, match="Artifact grants are not available"):
        service.execute_action("fixture.files", "fetch_file", request, scope=scope)


def test_direct_action_without_grant_fields_never_builds_the_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_runtime,
        "run_extension_action",
        lambda *_args, **kwargs: {
            "status": "succeeded",
            "response": {"ok": True, "result": {"files": []}},
        },
    )

    def no_plane():
        raise AssertionError("an action without grants must not build artifact storage")

    result = _service(no_plane).execute_action(
        "fixture.files",
        "fetch_file",
        ExtensionActionRequest(target={"connection_id": "connection-1"}, input={}),
        scope=_scope(),
    )

    assert result.action["status"] == "succeeded"


@pytest.mark.parametrize(
    ("path", "marker", "expected"),
    [
        (
            "core/entrypoints/api/app_factory.py",
            "build_extension_binding_service_factory(",
            "get_artifact_plane=lambda: runtime._artifact_plane",
        ),
        (
            "core/infrastructure/runtime/http_app_request_helpers.py",
            "build_extension_binding_service_factory(",
            "get_artifact_plane=lambda: _artifact_plane",
        ),
        (
            "core/entrypoints/cli/runtime.py",
            "def extension_binding_service",
            "get_artifact_plane=lambda: self.artifact_plane",
        ),
    ],
)
def test_every_composition_root_gives_direct_actions_the_artifact_plane(
    path: str, marker: str, expected: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    binding = source.split(marker, 1)[1].split("extension_identifier=", 1)[0]

    assert expected in binding
