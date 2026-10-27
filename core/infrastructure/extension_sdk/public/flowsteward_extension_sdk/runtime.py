from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HOST_CONTRACT_VERSION = "extension_host_v2"
# The one final execution scope of the host contract, in contract order. The
# handshake confirms exactly this list and every runtime context carries all
# four keys (string or null).
EXECUTION_SCOPE_FIELDS: tuple[str, ...] = (
    "group_id",
    "for_each_id",
    "step_id",
    "step_execution_id",
)

_MIGRATION_TARGET_TYPES = {
    "install_config",
    "project_connection_config",
    "workspace_data_connection_config",
    "project_state",
}
_FORBIDDEN_MIGRATION_KEYS = {
    "secret",
    "secrets",
    "secret_ref",
    "secret_refs",
    "token",
    "access_token",
    "refresh_token",
    "runtime_token",
    "credential",
    "credentials",
    "encrypted_value",
    "database",
    "db",
    "dsn",
    "host_mutations",
}


@dataclass(frozen=True)
class RuntimeContext:
    """Where the host runs this invocation.

    ``group_id`` is the business id of the step's immediate Group and
    ``for_each_id`` of its immediate For each; at most one is set and both are
    ``None`` for a root step. ``step_id`` and ``step_execution_id`` are ``None``
    outside a step.
    """

    account_id: str = ""
    project_id: str = ""
    workflow_id: str = ""
    job_id: str = ""
    group_id: str | None = None
    for_each_id: str | None = None
    step_id: str | None = None
    step_execution_id: str | None = None
    contract_version: str = ""
    artifact_storage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeResources:
    """Typed access to host-authorized resource snapshots."""

    values: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_context(cls, context: dict[str, Any] | None) -> RuntimeResources:
        payload = context if isinstance(context, dict) else {}
        resources = payload.get("resources")
        return cls(dict(resources) if isinstance(resources, dict) else {})

    def object(self, name: str) -> dict[str, Any]:
        value = self.values.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def rows(self, name: str) -> list[dict[str, Any]]:
        value = self.values.get(name)
        return (
            [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []
        )


def _reject_migration_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in _FORBIDDEN_MIGRATION_KEYS:
                raise ValueError(f"Migration value contains forbidden secret field '{key}'")
            _reject_migration_secrets(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_migration_secrets(nested)


@dataclass(frozen=True)
class MigrationEnvelope:
    envelope_version: str
    source_version: int
    target_version: int
    target_type: str
    record_id: str
    value: dict[str, Any]
    namespace: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MigrationEnvelope:
        root = dict(payload or {})
        envelope_version = _text(root.get("envelope_version"))
        if envelope_version != "extension_data_migration_v1":
            raise ValueError("Unsupported migration envelope version")
        target_type = _text(root.get("target_type"))
        if target_type not in _MIGRATION_TARGET_TYPES:
            raise ValueError("Unsupported migration target type")
        record_id = _text(root.get("record_id"))
        if not record_id:
            raise ValueError("Migration record_id is required")
        value = root.get("value")
        if not isinstance(value, dict):
            raise ValueError("Migration value must be an object")
        _reject_migration_secrets(value)
        source_version = int(root.get("source_version") or 0)
        target_version = int(root.get("target_version") or 0)
        if source_version < 1 or target_version <= source_version:
            raise ValueError("Migration versions must move forward")
        return cls(
            envelope_version=envelope_version,
            source_version=source_version,
            target_version=target_version,
            target_type=target_type,
            record_id=record_id,
            namespace=_text(root.get("namespace")),
            value=dict(value),
        )


def migration_result(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Migration result value must be an object")
    _reject_migration_secrets(value)
    return {"value": dict(value)}


def host_mutation(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a typed host mutation envelope for host-side grant validation."""
    normalized_kind = _text(kind)
    if not normalized_kind:
        raise ValueError("host mutation kind is required")
    return {"kind": normalized_kind, "payload": dict(payload or {})}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    return _text(value) or None


def handshake_result() -> dict[str, Any]:
    """The handshake confirmation a runtime returns as ``result``."""
    return {
        "contract_version": HOST_CONTRACT_VERSION,
        "execution_scope_fields": list(EXECUTION_SCOPE_FIELDS),
    }


def _execution_scope_error(context: dict[str, Any]) -> str:
    missing = [field for field in EXECUTION_SCOPE_FIELDS if field not in context]
    if missing:
        return f"Runtime context misses execution scope field(s) {', '.join(missing)}"
    if _optional_text(context.get("group_id")) and _optional_text(context.get("for_each_id")):
        return "Runtime context names both a Group and a For each as the immediate owner"
    if bool(_optional_text(context.get("step_id"))) != bool(
        _optional_text(context.get("step_execution_id"))
    ):
        return "Runtime context names a step without its logical execution, or the reverse"
    return ""


def runtime_context_from_payload(payload: dict[str, Any] | None) -> RuntimeContext:
    """Read the host's invocation envelope.

    An envelope without an explicit ``extension_host_v2`` contract, or whose
    runtime context is not the final four-field execution scope, raises
    ``ValueError`` before the extension does any work.
    """
    root = payload if isinstance(payload, dict) else {}
    contract_version = _text(root.get("contract_version"))
    if contract_version != HOST_CONTRACT_VERSION:
        raise ValueError(
            f"Unsupported host contract '{contract_version}'; "
            f"this SDK speaks '{HOST_CONTRACT_VERSION}'"
        )
    raw_context = root.get("runtime_context")
    context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
    problem = _execution_scope_error(context)
    if problem:
        raise ValueError(problem)
    raw_artifact_storage = context.get("artifact_storage")
    artifact_storage = dict(raw_artifact_storage) if isinstance(raw_artifact_storage, dict) else {}
    return RuntimeContext(
        account_id=_text(context.get("account_id")),
        project_id=_text(context.get("project_id")),
        workflow_id=_text(context.get("workflow_id")),
        job_id=_text(context.get("job_id")),
        group_id=_optional_text(context.get("group_id")),
        for_each_id=_optional_text(context.get("for_each_id")),
        step_id=_optional_text(context.get("step_id")),
        step_execution_id=_optional_text(context.get("step_execution_id")),
        contract_version=contract_version,
        artifact_storage=artifact_storage,
        raw=dict(context),
    )
