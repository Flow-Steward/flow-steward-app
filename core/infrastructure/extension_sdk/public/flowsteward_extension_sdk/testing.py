from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import (
    OCTET_STREAM_VALUE,
    ArtifactDatasetPlane,
    LocalFilesystemStorageAdapter,
    read_artifact_bytes,
)

ERROR_MISSING_SPEC = "missing_integration_spec"
ERROR_NEEDS_CONNECTION = "needs_connection"


@dataclass(frozen=True)
class IntegrationOperation:
    operation_id: str
    side_effect: str = ""
    required_inputs: tuple[str, ...] = ()
    optional_inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    idempotency: str = ""
    accepted_connection_roles: tuple[str, ...] | None = None


@dataclass(frozen=True)
class IntegrationManifest:
    manifest_id: str
    service: str
    primitive_type: str
    semantic_roles: tuple[str, ...] = ()
    supported_url_schemes: tuple[str, ...] = ()
    auth_source: str = ""
    required_connection_roles: tuple[str, ...] = ()
    operations: tuple[IntegrationOperation, ...] = ()


@dataclass(frozen=True)
class IntegrationBuildStep:
    operation_id: str
    step_kind: str
    side_effect: str
    produces: tuple[str, ...] = ()
    input_wiring: Mapping[str, str] = field(default_factory=dict)


@dataclass
class IntegrationDecision:
    decision: str
    primitive_type: str = ""
    manifest_id: str = ""
    steps: list[IntegrationBuildStep] = field(default_factory=list)
    missing_details: list[str] = field(default_factory=list)
    user_message: str = ""

    @property
    def can_apply(self) -> bool:
        return self.decision == "build"

    @property
    def side_effects(self) -> list[str]:
        return [step.side_effect for step in self.steps if step.side_effect]

    def as_response(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "can_apply": self.can_apply,
            "missing_details": list(self.missing_details),
            "user_message": self.user_message,
        }


_URL_SCHEME_RE = re.compile(r"\b([a-z][a-z0-9+.-]*):\/\/", re.IGNORECASE)
_EMBEDDED_CREDENTIAL_RE = re.compile(
    r"\b(?:password|passwd|secret|api[_ -]?key|username|smtp[_ -]?host)\b"
    r"\s*+(?:(?:=|:|\bis\b)\s*+)?\S++",
    re.IGNORECASE,
)


def resolve_integration_contract(
    *,
    service: str,
    requested_action: str,
    integration_kind: str,
    request_text: str,
    manifests: Sequence[IntegrationManifest],
    selected_operation_id: str = "",
    available_connection_roles: Sequence[str] = (),
) -> IntegrationDecision:
    """Resolve an extension contract against a request without host internals.

    This is a public test-kit helper for extension repositories. It intentionally
    checks only the portable contract surface an extension owns: manifest id,
    primitive type, supported URL schemes, semantic roles, and operation ids.
    Host capability visibility policy stays in the host test suite.
    """

    _ = requested_action
    requested_service = _normalize_token(service)
    requested_kind = _normalize_token(integration_kind)
    scheme = _first_url_scheme(request_text)
    operation_filter = str(selected_operation_id or "").strip()
    available_roles = {_normalize_token(role) for role in available_connection_roles}

    for manifest in manifests:
        if requested_kind and _normalize_token(manifest.primitive_type) != requested_kind:
            continue
        manifest_service = _normalize_token(manifest.service)
        roles = {_normalize_token(role) for role in manifest.semantic_roles}
        if requested_service and requested_service not in {manifest_service, *roles}:
            continue
        supported_schemes = {_normalize_token(item) for item in manifest.supported_url_schemes}
        if scheme and supported_schemes and scheme not in supported_schemes:
            continue
        operation = _select_operation(manifest.operations, operation_filter)
        if operation is None:
            continue
        operation_roles = operation.accepted_connection_roles
        required_roles = {
            _normalize_token(role)
            for role in (
                operation_roles
                if operation_roles is not None
                else manifest.required_connection_roles
            )
            if _normalize_token(role)
        }
        if _EMBEDDED_CREDENTIAL_RE.search(str(request_text or "")) or (
            required_roles and available_roles.isdisjoint(required_roles)
        ):
            return IntegrationDecision(
                decision=ERROR_NEEDS_CONNECTION,
                primitive_type=manifest.primitive_type,
                manifest_id=manifest.manifest_id,
                missing_details=sorted(required_roles),
                user_message="Select a configured project connection; embedded credentials are not accepted.",
            )
        return IntegrationDecision(
            decision="build",
            primitive_type=manifest.primitive_type,
            manifest_id=manifest.manifest_id,
            steps=[
                IntegrationBuildStep(
                    operation_id=operation.operation_id,
                    step_kind="connector"
                    if _normalize_token(manifest.primitive_type) == "connector"
                    else manifest.primitive_type,
                    side_effect=operation.side_effect,
                    produces=operation.outputs,
                    input_wiring={name: f"input:{name}" for name in operation.required_inputs},
                )
            ],
        )
    return IntegrationDecision(decision=ERROR_MISSING_SPEC)


class ExtensionWorkflowHarness:
    """Portable contract harness for extension-local workflow composition tests."""

    def __init__(self, *, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)
        self.plane = ArtifactDatasetPlane(
            storage=LocalFilesystemStorageAdapter(root_dir=str(self.artifact_root))
        )

    def initial_state(
        self,
        *,
        job_id: str,
        workflow_id: str,
        account_id: str,
        project_id: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "workflow_id": workflow_id,
            "account_id": account_id,
            "project_id": project_id,
            "_artifact_plane": self.plane,
            **extra,
        }

    def execute_connector_step(
        self,
        *,
        step: Mapping[str, Any],
        state: Mapping[str, Any],
        extensions: Mapping[str, Mapping[str, Any]],
        run_action: Callable[..., Mapping[str, Any]],
    ) -> dict[str, Any]:
        connector_id = str(step.get("connector_id") or "").strip()
        extension_id, action_id, extension = _resolve_connector(
            connector_id,
            extensions,
        )
        input_payload = {
            str(key): _resolve_mapping_value(value, state)
            for key, value in _as_mapping(step.get("input_mapping_jsonb")).items()
        }
        policies = _artifact_policies(extension)
        policy = _operation_policy(policies, action_id)
        artifact_outputs = [
            self._artifact_output_descriptor(policy_row, state=state)
            for policy_row in _as_list(policy.get("outputs"))
        ]
        artifact_inputs = [
            self._artifact_input_descriptor(policy_row, input_payload=input_payload)
            for policy_row in _as_list(policy.get("inputs"))
        ]
        response = run_action(
            dict(extension),
            action_id,
            input_payload,
            artifact_inputs=artifact_inputs,
            artifact_outputs=artifact_outputs,
        )
        if str(response.get("status") or "").strip() not in {"", "succeeded"}:
            return {"ok": False, "response": dict(response)}

        grant_outputs = {
            str(item.get("binding_key") or "").strip(): str(
                item.get("artifact_handle") or ""
            ).strip()
            for item in artifact_outputs
        }
        state_patch: dict[str, Any] = {}
        output_mapping = _as_mapping(step.get("output_mapping_jsonb"))
        for source, target in output_mapping.items():
            if str(source or "").strip() == "artifact_handle" and artifact_outputs:
                _set_state_patch(
                    state_patch,
                    str(target or ""),
                    artifact_outputs[0]["artifact_handle"],
                )
        return {
            "ok": True,
            "extension_id": extension_id,
            "action_id": action_id,
            "response": dict(response),
            "state_patch": state_patch,
            "grant_outputs": grant_outputs,
            "grant_inputs": {
                str(item.get("binding_key") or "").strip(): str(
                    item.get("artifact_handle") or ""
                ).strip()
                for item in artifact_inputs
            },
        }

    def execute_tabular_feed_step(
        self,
        *,
        step: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        config = _as_mapping(_as_mapping(step.get("override")).get("tabular_feed"))
        operation = str(config.get("operation") or "").strip().lower()
        result_target = str(config.get("result_target") or "").strip()
        if operation == "read":
            return self._read_tabular_artifact(config=config, state=state, target=result_target)
        if operation == "write":
            return self._write_tabular_artifact(
                config=config,
                state=state,
                target=result_target,
            )
        raise ValueError(f"Unsupported tabular_feed operation: {operation}")

    def _artifact_output_descriptor(
        self,
        policy: Mapping[str, Any],
        *,
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        handle, _meta = self.plane.create_artifact(
            owner_scope={
                "account_id": state.get("account_id"),
                "project_id": state.get("project_id"),
            },
            run_id=str(state.get("job_id") or ""),
            step_id=str(policy.get("binding_key") or "output"),
            filename=str(policy.get("filename") or "artifact.bin"),
            content_type=str(policy.get("content_type") or OCTET_STREAM_VALUE),
            retention_class=str(policy.get("retention_class") or "run_window"),
        )
        max_size = _positive_int(policy.get("max_size_bytes"))
        access: dict[str, Any] = {
            "transport": "presigned_url",
            "mode": "write",
            "upload_url": self.plane.artifact_presigned_url(
                handle.artifact_id,
                method="PUT",
            ),
        }
        if max_size is not None:
            access["max_size_bytes"] = max_size
        return {
            "kind": "artifact",
            "artifact_id": handle.artifact_id,
            "artifact_handle": f"artifact:{handle.artifact_id}",
            "binding_key": str(policy.get("binding_key") or "").strip(),
            "filename": str(policy.get("filename") or "artifact.bin"),
            "content_type": str(policy.get("content_type") or OCTET_STREAM_VALUE),
            "max_size_bytes": max_size,
            "access": access,
        }

    def _artifact_input_descriptor(
        self,
        policy: Mapping[str, Any],
        *,
        input_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        field = str(policy.get("field") or "artifact_handle").strip()
        artifact_id = _strip_handle(input_payload.get(field), prefix="artifact")
        meta = self.plane.get_artifact(artifact_id)
        if not meta:
            raise ValueError(f"Artifact does not exist: {artifact_id}")
        return {
            "kind": "artifact",
            "artifact_id": artifact_id,
            "artifact_handle": f"artifact:{artifact_id}",
            "binding_key": str(policy.get("binding_key") or "").strip(),
            "filename": str(meta.get("filename") or "artifact.bin"),
            "content_type": str(meta.get("content_type") or OCTET_STREAM_VALUE),
            "size_bytes": int(meta.get("size_bytes") or 0),
            "access": {
                "transport": "presigned_url",
                "mode": "read",
                "download_url": self.plane.artifact_presigned_url(
                    artifact_id,
                    method="GET",
                ),
            },
        }

    def _read_tabular_artifact(
        self,
        *,
        config: Mapping[str, Any],
        state: Mapping[str, Any],
        target: str,
    ) -> dict[str, Any]:
        artifact_handle = _resolve_mapping_value(
            _as_mapping(config.get("input")).get("artifact_handle"),
            state,
        )
        artifact_id = _strip_handle(artifact_handle, prefix="artifact")
        descriptor = self._artifact_input_descriptor(
            {"binding_key": "source_artifact", "field": "artifact_handle"},
            input_payload={"artifact_handle": artifact_id},
        )
        body = read_artifact_bytes({"artifacts": {"inputs": [descriptor]}})
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        dataset, _meta = self.plane.create_dataset(
            owner_scope={
                "account_id": state.get("account_id"),
                "project_id": state.get("project_id"),
            },
            run_id=str(state.get("job_id") or ""),
            step_id="tabular_read",
            schema={
                "columns": [{"name": name, "type": "text"} for name in (rows[0] if rows else {})]
            },
            rows=rows,
        )
        state_patch: dict[str, Any] = {}
        _set_state_patch(state_patch, target, f"dataset:{dataset.dataset_id}")
        return {
            "ok": True,
            "result": {"dataset_handle": dataset.dataset_id},
            "state_patch": state_patch,
        }

    def _write_tabular_artifact(
        self,
        *,
        config: Mapping[str, Any],
        state: Mapping[str, Any],
        target: str,
    ) -> dict[str, Any]:
        dataset_handle = _resolve_mapping_value(
            _as_mapping(config.get("input")).get("dataset_handle"),
            state,
        )
        dataset_id = _strip_handle(dataset_handle, prefix="dataset")
        rows = self._dataset_rows(dataset_id)
        body = _rows_to_csv(rows)
        artifact, _meta = self.plane.create_artifact(
            owner_scope={
                "account_id": state.get("account_id"),
                "project_id": state.get("project_id"),
            },
            run_id=str(state.get("job_id") or ""),
            step_id="tabular_write",
            filename="tabular-export.csv",
            content_type="text/csv",
            body=body,
        )
        result = {
            "artifact_handle": artifact.artifact_id,
            "content_type": "text/csv",
            "size_bytes": len(body),
        }
        state_patch: dict[str, Any] = {}
        _set_state_patch(state_patch, target, result)
        return {"ok": True, "result": result, "state_patch": state_patch}

    def _dataset_rows(self, dataset_id: str) -> list[dict[str, Any]]:
        meta = self.plane.get_dataset(dataset_id)
        if not meta:
            raise ValueError(f"Dataset does not exist: {dataset_id}")
        storage = self.plane._storage
        raw = storage.get(str(meta.get("object_key") or ""))
        rows = json.loads(raw.decode("utf-8")) if raw else []
        return [dict(row) for row in rows if isinstance(row, dict)]


def _select_operation(
    operations: Sequence[IntegrationOperation],
    operation_id: str,
) -> IntegrationOperation | None:
    if operation_id:
        return next((item for item in operations if item.operation_id == operation_id), None)
    return operations[0] if operations else None


def _first_url_scheme(text: str) -> str:
    match = _URL_SCHEME_RE.search(str(text or ""))
    return _normalize_token(match.group(1)) if match else ""


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _resolve_connector(
    connector_id: str,
    extensions: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, Mapping[str, Any]]:
    for extension_id in sorted(extensions, key=len, reverse=True):
        prefix = f"{extension_id}."
        if connector_id.startswith(prefix):
            return extension_id, connector_id[len(prefix) :], extensions[extension_id]
    raise ValueError(f"Unknown connector id: {connector_id}")


def _artifact_policies(extension: Mapping[str, Any]) -> list[dict[str, Any]]:
    compiled = _as_mapping(extension.get("compiled_manifest"))
    rows = compiled.get("artifact_policies") or extension.get("artifact_policies")
    return [dict(item) for item in _as_list(rows) if isinstance(item, Mapping)]


def _operation_policy(
    policies: Sequence[Mapping[str, Any]],
    operation_id: str,
) -> dict[str, Any]:
    for policy in policies:
        if str(policy.get("operation_id") or "").strip() == operation_id:
            return dict(policy)
    return {}


def _resolve_mapping_value(value: Any, state: Mapping[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("literal:"):
        return _literal_value(text.split(":", 1)[1])
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2].strip()
    if text.startswith("state."):
        return _state_value(state, text.split(".", 1)[1])
    return value


def _literal_value(value: str) -> Any:
    text = str(value)
    lowered = text.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return text


def _state_value(state: Mapping[str, Any], path: str) -> Any:
    current: Any = state
    for part in str(path or "").split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _set_state_patch(state_patch: dict[str, Any], target: str, value: Any) -> None:
    text = str(target or "").strip()
    if text.startswith("state."):
        text = text.split(".", 1)[1]
    if not text:
        return
    parts = text.split(".")
    current = state_patch
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _strip_handle(value: Any, *, prefix: str) -> str:
    text = str(value or "").strip()
    expected = f"{prefix}:"
    return text.split(":", 1)[1] if text.startswith(expected) else text


def _rows_to_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(str(key))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    return buffer.getvalue().encode("utf-8")
