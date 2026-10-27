"""
Extension descriptor schema v1 — canonical install/runtime contract.

This schema validates runtime/distribution metadata needed before installing
and binding an Extension runtime.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The llm_provider block lives in its own module; these names stay importable
# from here (explicit re-exports) because callers have always imported them so.
from core.application.extensions.extension_descriptor_llm_provider import (
    LLM_PROVIDER_CONNECTION_CONFIG_FIELDS as LLM_PROVIDER_CONNECTION_CONFIG_FIELDS,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LLM_PROVIDER_CONTRACT_SCHEMA_VERSION as LLM_PROVIDER_CONTRACT_SCHEMA_VERSION,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LLM_PROVIDER_RUNTIME_MODES as LLM_PROVIDER_RUNTIME_MODES,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LlmProviderContractV1 as LlmProviderContractV1,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LlmProviderModelGrouping as LlmProviderModelGrouping,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LlmProviderOperationRef as LlmProviderOperationRef,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    LlmProviderOperations as LlmProviderOperations,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    canonical_llm_field_name as canonical_llm_field_name,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    is_llm_secret_field_name as is_llm_secret_field_name,
)
from core.application.extensions.extension_descriptor_llm_provider import (
    llm_plaintext_secret_paths as llm_plaintext_secret_paths,
)
from core.application.extensions.host_contract import HOST_CONTRACT_VERSION
from core.application.extensions.identity import normalize_extension_id
from core.application.extensions.semver import (
    compare_semver,
    normalize_semver,
    require_nonempty_text,
)
from core.infrastructure.extension_runtime.python_requirement_contract import (
    normalize_python_requirements,
)

RUNTIME_COMPATIBILITY_PLATFORM_MAX_VALUE = "runtime.compatibility.platform_max"
RUNTIME_COMPATIBILITY_PLATFORM_MIN_VALUE = "runtime.compatibility.platform_min"


EXTENSION_DESCRIPTOR_SCHEMA_VERSION = "extension_descriptor_v1"
# The extension host contract version that runtime.compatibility.platform_min and
# platform_max are compared against (not the application build version).
# 1.1.0: an llm_provider may run as an extension subprocess (runtime_mode
# subprocess) and declare optional_connection_config. A bundle that relies on
# that declares platform_min 1.1.0, so an older host marks it incompatible with
# "below required minimum" instead of failing on an unknown contract field.
# 1.2.0: an llm_provider may declare connection_config_defaults, the value it
# uses for an optional setting left empty (e.g. its public Base URL). The host
# compares effective settings, so an emptied field that held its default keeps
# verified models. A bundle that declares it sets platform_min 1.2.0, so an older
# host, whose contract forbids the field, marks it incompatible instead.
# 1.3.0: an llm_provider may declare connection_config_help and
# provider_secret_help, plain text the settings form shows under a setting or a
# secret field (what to enter, in which format). A bundle that declares either
# sets platform_min 1.3.0; an older host forbids the fields and refuses the
# bundle as below its required minimum.
DEFAULT_FS_RUNTIME_VERSION = "1.3.0"

_ARTIFACT_ENTRY_ID_FIELDS = (
    "workflow_id",
    "agent_id",
    "skill_id",
    "skill_id",
    "tool_id",
    "contract_id",
    "data_contract_id",
    "dataset_id",
    "query_template_id",
    "saved_query_id",
    "guardrail_id",
    "extension_id",
    "id",
)
_REMOTE_REQUIRED_ENDPOINTS = ("handshake", "health", "query", "action", "emit_event")


class DistributionChannel(StrEnum):
    marketplace_managed = "marketplace_managed"
    private_byo = "private_byo"


class ExecutionMode(StrEnum):
    local = "local"
    remote_https = "remote_https"


class ExtensionOrigin(StrEnum):
    vendor_published = "vendor_published"
    user_generated = "user_generated"
    imported = "imported"


class ExtensionDescriptorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("display_name", mode="before")
    @classmethod
    def _validate_display_name(cls, value: Any) -> str:
        return require_nonempty_text(value, "identity.display_name")


class ExtensionDescriptorVendor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: str
    support_url: str = ""

    @field_validator("vendor_name", mode="before")
    @classmethod
    def _validate_vendor_name(cls, value: Any) -> str:
        return require_nonempty_text(value, "vendor.vendor_name")


class ExtensionDescriptorListing(BaseModel):
    model_config = ConfigDict(extra="allow")

    visibility: str = ""
    category: str = ""


class ExtensionDescriptorDistribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: DistributionChannel
    execution_modes: list[ExecutionMode]
    allows_self_hosted: bool = False
    requires_connected_control_plane: bool = False
    import_sources: list[str] = Field(default_factory=list)

    @field_validator("execution_modes", mode="after")
    @classmethod
    def _validate_execution_modes(cls, value: list[ExecutionMode]) -> list[ExecutionMode]:
        if not value:
            raise ValueError("distribution.execution_modes must contain at least one mode")
        return list(dict.fromkeys(value))


class ExtensionDescriptorRuntimeCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_min: str = DEFAULT_FS_RUNTIME_VERSION
    platform_max: str | None = None

    @field_validator("platform_min", mode="before")
    @classmethod
    def _validate_platform_min(cls, value: Any) -> str:
        return normalize_semver(
            value or DEFAULT_FS_RUNTIME_VERSION, RUNTIME_COMPATIBILITY_PLATFORM_MIN_VALUE
        )

    @field_validator("platform_max", mode="before")
    @classmethod
    def _validate_platform_max(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return normalize_semver(value, RUNTIME_COMPATIBILITY_PLATFORM_MAX_VALUE)

    @model_validator(mode="after")
    def _validate_range(self) -> ExtensionDescriptorRuntimeCompatibility:
        if self.platform_max is None:
            return self
        if (
            compare_semver(
                self.platform_max,
                self.platform_min,
                field_name=RUNTIME_COMPATIBILITY_PLATFORM_MAX_VALUE,
            )
            < 0
        ):
            raise ValueError("runtime.compatibility.platform_max must be >= platform_min")
        return self


class ExtensionDescriptorRuntimeRemoteEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handshake: str = ""
    health: str = ""
    query: str = ""
    action: str = ""
    emit_event: str = ""


class ExtensionDescriptorRuntimeRemote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    endpoints: ExtensionDescriptorRuntimeRemoteEndpoints = Field(
        default_factory=ExtensionDescriptorRuntimeRemoteEndpoints
    )


class ExtensionDescriptorRuntimeAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_mode: str = "platform_signed_bearer"
    token_ttl_seconds: int = 3600


class ExtensionDescriptorRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    compatibility: ExtensionDescriptorRuntimeCompatibility = Field(
        default_factory=ExtensionDescriptorRuntimeCompatibility
    )
    auth: ExtensionDescriptorRuntimeAuth = Field(default_factory=ExtensionDescriptorRuntimeAuth)
    remote: ExtensionDescriptorRuntimeRemote | None = None
    extension_contract_v2: dict[str, Any] | None = None

    @field_validator("contract_version", mode="before")
    @classmethod
    def _validate_contract_version(cls, value: Any) -> str:
        declared = require_nonempty_text(value, "runtime.contract_version")
        if declared != HOST_CONTRACT_VERSION:
            # One host contract, no fallback: a release built for another one
            # cannot be installed here.
            raise ValueError(
                f"runtime.contract_version must be '{HOST_CONTRACT_VERSION}' (got '{declared}')"
            )
        return declared


class ExtensionDescriptorArtifactEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    workflow_id: str | None = None
    agent_id: str | None = None
    skill_id: str | None = None
    tool_id: str | None = None
    contract_id: str | None = None
    data_contract_id: str | None = None
    dataset_id: str | None = None
    query_template_id: str | None = None
    saved_query_id: str | None = None
    extension_id: str | None = None
    id: str | None = None

    ref: str = ""
    publish: bool = True
    snapshot: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_entry(self) -> ExtensionDescriptorArtifactEntry:
        artifact_id = self.artifact_id()
        if not artifact_id:
            raise ValueError("artifact entry must contain an identifier field")
        if not str(self.ref or "").strip() and not self.snapshot:
            raise ValueError("artifact entry requires ref or snapshot")
        return self

    def artifact_id(self) -> str:
        for field in _ARTIFACT_ENTRY_ID_FIELDS:
            value = getattr(self, field, None)
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""


def _refuse_non_final_workflow(entry: ExtensionDescriptorArtifactEntry) -> None:
    """A packaged workflow is the one final definition: no removed shapes, valid hooks.

    The loop Group and the inline For each step are refused here, before any
    install, and so are an Invoke that is both direct and a hook, an invalid
    hook declaration, a hook inside a container and a hook id used twice (ADR 0038).
    """
    from core.application.workflows.workflow_definition_changes import removed_format_issues
    from core.application.workflows.workflow_hook_contract import invoke_config_issues
    from core.application.workflows.workflow_hook_placement import hook_placement_issues

    snapshot = entry.snapshot if isinstance(entry.snapshot, dict) else {}
    issues = [*removed_format_issues(snapshot), *hook_placement_issues(snapshot)]
    seen: set[str] = set()
    for step in snapshot.get("steps") or []:
        if not isinstance(step, dict) or step.get("step_kind") != "invoke_workflow":
            continue
        override = step.get("override") if isinstance(step.get("override"), dict) else {}
        config = override.get("invoke_workflow") if isinstance(override, dict) else None
        if not isinstance(config, dict) or "hook" not in config:
            continue
        issues.extend(invoke_config_issues(config))
        hook_id = config["hook"].get("hook_id") if isinstance(config["hook"], dict) else None
        if isinstance(hook_id, str) and hook_id in seen:
            issues.append(
                {
                    "code": "workflow.hook.id_duplicate",
                    "message": f"Hook id '{hook_id}' is used twice",
                }
            )
        if isinstance(hook_id, str):
            seen.add(hook_id)
    if issues:
        raise ValueError(
            f"Workflow '{entry.artifact_id()}' is not a final workflow definition: "
            + "; ".join(f"{issue['code']}: {issue['message']}" for issue in issues[:5])
        )


class ExtensionDescriptorArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflows: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    agents: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    skills: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    tools: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    datasets: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    saved_queries: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    data_contracts: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    guardrails: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)
    extensions: list[ExtensionDescriptorArtifactEntry] = Field(default_factory=list)

    @field_validator("workflows")
    @classmethod
    def _validate_workflow_notes(
        cls, entries: list[ExtensionDescriptorArtifactEntry]
    ) -> list[ExtensionDescriptorArtifactEntry]:
        from core.application.workflows.workflow_canvas_annotation_contract import (
            ANNOTATIONS_SETTING,
            normalize_portable_annotations,
        )

        for entry in entries:
            _refuse_non_final_workflow(entry)
            settings = entry.snapshot.get("settings")
            if not isinstance(settings, dict) or ANNOTATIONS_SETTING not in settings:
                continue
            try:
                notes = normalize_portable_annotations(settings[ANNOTATIONS_SETTING])
            except ValueError as exc:
                raise ValueError(
                    f"Workflow '{entry.artifact_id()}' settings.canvas_annotations: {exc}"
                ) from exc
            entry.snapshot = {
                **entry.snapshot,
                "settings": {**settings, ANNOTATIONS_SETTING: notes},
            }
        return entries

    @field_validator("agents")
    @classmethod
    def _refuse_phase_era_agents(
        cls, entries: list[ExtensionDescriptorArtifactEntry]
    ) -> list[ExtensionDescriptorArtifactEntry]:
        from core.application.registry.agent_manifest import refuse_agent_phases

        for entry in entries:
            try:
                refuse_agent_phases(entry.snapshot)
            except ValueError as exc:
                raise ValueError(f"Agent '{entry.artifact_id()}': {exc}") from exc
        return entries

    @model_validator(mode="after")
    def _validate_not_empty(self) -> ExtensionDescriptorArtifacts:
        total = 0
        for entries in self.sections().values():
            total += len(entries)
        if total == 0:
            raise ValueError("artifacts must contain at least one artifact entry")
        return self

    def sections(self) -> dict[str, list[ExtensionDescriptorArtifactEntry]]:
        return {
            "workflows": self.workflows,
            "agents": self.agents,
            "skills": self.skills,
            "tools": self.tools,
            "datasets": self.datasets,
            "saved_queries": self.saved_queries,
            "data_contracts": self.data_contracts,
            "guardrails": self.guardrails,
            "extensions": self.extensions,
        }


class ExtensionDescriptorProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: ExtensionOrigin = ExtensionOrigin.imported
    source_url: str = ""
    source_job_id: str = ""
    notes: str = ""


class ExtensionDescriptorCommerce(BaseModel):
    model_config = ConfigDict(extra="allow")

    plan_id: str = ""
    billing_mode: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtensionDescriptorExtensionDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extension_id: str
    min_version: str | None = None
    max_version_exclusive: str | None = None
    required: bool = True
    reason: str = ""

    @field_validator("extension_id", mode="before")
    @classmethod
    def _validate_dependency_extension_id(cls, value: Any) -> str:
        return normalize_extension_id(value, "dependencies.extensions[].extension_id")

    @field_validator("min_version", "max_version_exclusive", mode="before")
    @classmethod
    def _validate_optional_semver(cls, value: Any, info):
        if value in (None, ""):
            return None
        return normalize_semver(value, f"dependencies.extensions[].{info.field_name}")

    @model_validator(mode="after")
    def _validate_version_range(self) -> ExtensionDescriptorExtensionDependency:
        if (
            self.min_version
            and self.max_version_exclusive
            and compare_semver(
                self.min_version,
                self.max_version_exclusive,
                field_name="dependencies.extensions[] version range",
            )
            >= 0
        ):
            raise ValueError("dependency min_version must be < max_version_exclusive")
        return self


class ExtensionDescriptorDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extensions: list[ExtensionDescriptorExtensionDependency] = Field(default_factory=list)


class ExtensionDescriptorDataMigration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_version: int
    target_version: int
    action_id: str

    @model_validator(mode="after")
    def _validate_direction(self) -> ExtensionDescriptorDataMigration:
        if self.source_version < 1 or self.target_version < 1:
            raise ValueError("data migration versions must be positive integers")
        if self.target_version <= self.source_version:
            raise ValueError("data migration target_version must be greater than source_version")
        self.action_id = require_nonempty_text(
            self.action_id, "lifecycle.data_migrations[].action_id"
        )
        return self


class ExtensionDescriptorLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_schema_version: int = 1
    data_migrations: list[ExtensionDescriptorDataMigration] = Field(default_factory=list)
    project_state_schemas: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_migration_chain(self) -> ExtensionDescriptorLifecycle:
        if self.data_schema_version < 1:
            raise ValueError("lifecycle.data_schema_version must be a positive integer")
        seen_sources: set[int] = set()
        expected_source = 1
        for migration in self.data_migrations:
            if migration.source_version in seen_sources:
                raise ValueError("data migration chain cannot branch from one source version")
            seen_sources.add(migration.source_version)
            if migration.source_version != expected_source:
                raise ValueError("data migration chain is incomplete or out of order")
            expected_source = migration.target_version
        if self.data_schema_version == 1 and self.data_migrations:
            raise ValueError("data schema version 1 must not declare migrations")
        if self.data_schema_version > 1 and expected_source != self.data_schema_version:
            raise ValueError("data migration chain is incomplete for data_schema_version")
        for namespace, schema in self.project_state_schemas.items():
            if not str(namespace or "").strip() or not isinstance(schema, dict):
                raise ValueError("project-state migration schemas require a namespace and object")
            encoded = json.dumps(schema, sort_keys=True)
            if len(encoded.encode("utf-8")) > 262_144 or '"$ref": "http' in encoded.lower():
                raise ValueError("project-state migration schemas must be bounded and bundle-local")
        return self


class ExtensionDescriptor(BaseModel):
    """Root descriptor model for ``extension_descriptor_v1``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTENSION_DESCRIPTOR_SCHEMA_VERSION
    extension_id: str
    release_version: str = "1.0.0"
    extension_type: str = "service_extension"

    identity: ExtensionDescriptorIdentity
    vendor: ExtensionDescriptorVendor
    listing: ExtensionDescriptorListing | None = None
    distribution: ExtensionDescriptorDistribution
    runtime: ExtensionDescriptorRuntime
    extension_contract_v2: dict[str, Any] | None = None
    python_requirements: list[dict[str, Any]] = Field(default_factory=list)
    dependencies: ExtensionDescriptorDependencies = Field(
        default_factory=ExtensionDescriptorDependencies
    )
    lifecycle: ExtensionDescriptorLifecycle = Field(default_factory=ExtensionDescriptorLifecycle)
    artifacts: ExtensionDescriptorArtifacts
    provenance: ExtensionDescriptorProvenance | None = None
    commerce: ExtensionDescriptorCommerce | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if normalized != EXTENSION_DESCRIPTOR_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version '{normalized or value}'; "
                f"expected '{EXTENSION_DESCRIPTOR_SCHEMA_VERSION}'"
            )
        return normalized

    @field_validator("extension_id", mode="before")
    @classmethod
    def _validate_extension_id(cls, value: Any) -> str:
        return normalize_extension_id(value, "extension_id")

    @field_validator("release_version", mode="before")
    @classmethod
    def _validate_release_version(cls, value: Any) -> str:
        return normalize_semver(value or "1.0.0", "release_version")

    @field_validator("python_requirements", mode="before")
    @classmethod
    def _validate_python_requirements(cls, value: Any) -> list[dict[str, Any]]:
        normalized, issues = normalize_python_requirements(
            value,
            manifest_path="extension-descriptor",
        )
        if issues:
            raise ValueError(str(issues[0].get("message") or "invalid python_requirements"))
        return normalized

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())

    def artifact_entries(self) -> list[tuple[str, int, ExtensionDescriptorArtifactEntry]]:
        entries: list[tuple[str, int, ExtensionDescriptorArtifactEntry]] = []
        for section_name, section_entries in self.artifacts.sections().items():
            for index, entry in enumerate(section_entries):
                entries.append((section_name, index, entry))
        return entries


def validate_extension_descriptor(
    data: dict[str, Any],
    *,
    platform_version: str = DEFAULT_FS_RUNTIME_VERSION,
    allow_insecure_remote: bool = False,
    artifact_resolver: Callable[[str, str, int, ExtensionDescriptorArtifactEntry], bool]
    | None = None,
) -> dict[str, Any]:
    """Validate raw dict against ExtensionDescriptor schema and runtime rules.

    Returns ``{"valid": True, "descriptor": ExtensionDescriptor, "normalized": dict, "errors": []}``
    on success, or ``{"valid": False, "descriptor": None, "normalized": None, "errors": [...]}``
    on failure.
    """
    try:
        descriptor = ExtensionDescriptor(**data)
    except Exception as exc:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": [{"path": "descriptor", "message": str(exc)}],
        }

    errors: list[dict[str, str]] = []
    _validate_runtime_compatibility(descriptor, platform_version=platform_version, errors=errors)
    _validate_remote_https_runtime(
        descriptor,
        allow_insecure_remote=allow_insecure_remote,
        errors=errors,
    )
    _validate_artifact_resolution(
        descriptor,
        artifact_resolver=artifact_resolver,
        errors=errors,
    )
    _validate_llm_provider_contract(descriptor, errors=errors)

    if errors:
        return {
            "valid": False,
            "descriptor": None,
            "normalized": None,
            "errors": errors,
        }
    return {
        "valid": True,
        "descriptor": descriptor,
        "normalized": descriptor.to_dict(),
        "errors": [],
    }


def _validate_runtime_compatibility(
    descriptor: ExtensionDescriptor,
    *,
    platform_version: str,
    errors: list[dict[str, str]],
) -> None:
    try:
        normalized_platform = normalize_semver(platform_version, "platform_version")
    except Exception as exc:
        errors.append(
            {
                "path": "runtime.compatibility",
                "message": f"Invalid platform_version '{platform_version}': {exc}",
            }
        )
        return

    compatibility = descriptor.runtime.compatibility
    if (
        compare_semver(
            normalized_platform,
            compatibility.platform_min,
            field_name=RUNTIME_COMPATIBILITY_PLATFORM_MIN_VALUE,
        )
        < 0
    ):
        errors.append(
            {
                "path": RUNTIME_COMPATIBILITY_PLATFORM_MIN_VALUE,
                "message": (
                    f"Platform version '{normalized_platform}' is below required minimum "
                    f"'{compatibility.platform_min}'"
                ),
            }
        )
    if (
        compatibility.platform_max
        and compare_semver(
            normalized_platform,
            compatibility.platform_max,
            field_name=RUNTIME_COMPATIBILITY_PLATFORM_MAX_VALUE,
        )
        > 0
    ):
        errors.append(
            {
                "path": RUNTIME_COMPATIBILITY_PLATFORM_MAX_VALUE,
                "message": (
                    f"Platform version '{normalized_platform}' exceeds supported maximum "
                    f"'{compatibility.platform_max}'"
                ),
            }
        )


def _validate_remote_https_runtime(
    descriptor: ExtensionDescriptor,
    *,
    allow_insecure_remote: bool,
    errors: list[dict[str, str]],
) -> None:
    if ExecutionMode.remote_https not in descriptor.distribution.execution_modes:
        return

    runtime_remote = descriptor.runtime.remote
    if runtime_remote is None:
        errors.append(
            {
                "path": "runtime.remote",
                "message": "runtime.remote is required for remote_https execution mode",
            }
        )
        return

    base_url = str(runtime_remote.base_url or "").strip()
    # A reference bundle must not advertise a fake endpoint.  Install-time
    # remote_base_url remains mandatory and is URL-safety validated by the
    # install service; a descriptor may intentionally leave its default blank.
    if base_url and not allow_insecure_remote:
        parsed = urlparse(base_url)
        if parsed.scheme.lower() != "https":
            errors.append(
                {
                    "path": "runtime.remote.base_url",
                    "message": "runtime.remote.base_url must use HTTPS in non-dev validation mode",
                }
            )

    for endpoint_name in _REMOTE_REQUIRED_ENDPOINTS:
        endpoint_value = str(getattr(runtime_remote.endpoints, endpoint_name, "") or "").strip()
        if not endpoint_value:
            errors.append(
                {
                    "path": f"runtime.remote.endpoints.{endpoint_name}",
                    "message": f"runtime.remote.endpoints.{endpoint_name} is required for remote_https mode",
                }
            )
            continue
        if not _is_safe_remote_endpoint_path(endpoint_value):
            errors.append(
                {
                    "path": f"runtime.remote.endpoints.{endpoint_name}",
                    "message": (
                        f"runtime.remote.endpoints.{endpoint_name} must be an absolute-path template "
                        "(e.g. '/actions/{{action_id}}') and cannot be an absolute URL"
                    ),
                }
            )


_LLM_PROVIDER_CONTRACT_PATH = "runtime.extension_contract_v2.llm_provider"


def _llm_provider_contract_raw(descriptor: ExtensionDescriptor) -> dict[str, Any] | None:
    """Return the raw ``llm_provider`` block if present (runtime block preferred)."""
    for container in (
        descriptor.runtime.extension_contract_v2,
        descriptor.extension_contract_v2,
    ):
        if isinstance(container, dict):
            block = container.get("llm_provider")
            if isinstance(block, dict):
                return block
    return None


def extract_llm_provider_contract(
    data: ExtensionDescriptor | dict[str, Any],
) -> LlmProviderContractV1 | None:
    """Parse the LLM provider contract from a descriptor (or its dict form).

    Returns the typed contract, or ``None`` when the descriptor declares no
    ``llm_provider`` block. Raises ``ValueError``/``ValidationError`` on a
    malformed block — callers that need a clean boolean should pre-validate via
    ``validate_extension_descriptor``.
    """
    if isinstance(data, ExtensionDescriptor):
        raw = _llm_provider_contract_raw(data)
    else:
        raw = None
        runtime = (data or {}).get("runtime") if isinstance(data, dict) else None
        for container in (
            (runtime or {}).get("extension_contract_v2") if isinstance(runtime, dict) else None,
            (data or {}).get("extension_contract_v2") if isinstance(data, dict) else None,
        ):
            if isinstance(container, dict) and isinstance(container.get("llm_provider"), dict):
                raw = container["llm_provider"]
                break
    if raw is None:
        return None
    return LlmProviderContractV1(**raw)


def _validate_llm_provider_contract(
    descriptor: ExtensionDescriptor,
    *,
    errors: list[dict[str, str]],
) -> None:
    """Validate the optional ``llm_provider`` contract; no-op when absent.

    V1: the provider runs as an extension subprocess or behind a remote HTTPS
    runtime, structured output is mandatory for enablement, and the
    list_models/chat operations must be bound.
    """
    raw = _llm_provider_contract_raw(descriptor)
    if raw is None:
        return

    try:
        contract = LlmProviderContractV1(**raw)
    except Exception as exc:
        errors.append({"path": _LLM_PROVIDER_CONTRACT_PATH, "message": str(exc)})
        return

    required_mode = (
        ExecutionMode.remote_https
        if str(contract.runtime_mode).strip() == ExecutionMode.remote_https.value
        else ExecutionMode.local
    )
    if required_mode not in descriptor.distribution.execution_modes:
        errors.append(
            {
                "path": _LLM_PROVIDER_CONTRACT_PATH,
                "message": (
                    f"llm_provider.runtime_mode '{contract.runtime_mode}' requires "
                    f"the {required_mode.value} execution mode"
                ),
            }
        )

    if str(contract.schema_version).strip() != LLM_PROVIDER_CONTRACT_SCHEMA_VERSION:
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.schema_version",
                "message": (
                    f"Unsupported llm_provider schema_version '{contract.schema_version}'; "
                    f"expected '{LLM_PROVIDER_CONTRACT_SCHEMA_VERSION}'"
                ),
            }
        )
    if str(contract.runtime_mode).strip() not in LLM_PROVIDER_RUNTIME_MODES:
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.runtime_mode",
                "message": "llm_provider.runtime_mode must be 'subprocess' or 'remote_https'",
            }
        )
    if not str(contract.operations.list_models.query_id).strip():
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.operations.list_models",
                "message": "operations.list_models.query_id is required",
            }
        )
    if not str(contract.operations.chat.action_id).strip():
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.operations.chat",
                "message": "operations.chat.action_id is required",
            }
        )
    from core.application.settings.llm_verification_policy import REQUIRED_ROUTING_CAPABILITIES

    required_caps = {str(cap).strip() for cap in contract.required_capabilities_for_enablement}
    supported_caps = set(REQUIRED_ROUTING_CAPABILITIES)
    missing_caps = supported_caps - required_caps
    if missing_caps:
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.required_capabilities_for_enablement",
                "message": (
                    "chat, tools, and structured output are mandatory in v1: "
                    f"required_capabilities_for_enablement is missing {sorted(missing_caps)}"
                ),
            }
        )
    unsupported_caps = required_caps - supported_caps
    if unsupported_caps:
        errors.append(
            {
                "path": f"{_LLM_PROVIDER_CONTRACT_PATH}.required_capabilities_for_enablement",
                "message": (
                    "required_capabilities_for_enablement contains unsupported v1 "
                    f"capabilities: {sorted(unsupported_caps)}"
                ),
            }
        )


def _is_safe_remote_endpoint_path(value: str) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc:
        return False
    if not normalized.startswith("/") or normalized.startswith("//"):
        return False
    segments = [segment for segment in normalized.split("/") if segment]
    return not any(segment == ".." for segment in segments)


def _validate_artifact_resolution(
    descriptor: ExtensionDescriptor,
    *,
    artifact_resolver: Callable[[str, str, int, ExtensionDescriptorArtifactEntry], bool] | None,
    errors: list[dict[str, str]],
) -> None:
    if artifact_resolver is None:
        return
    for section_name, index, entry in descriptor.artifact_entries():
        artifact_ref = str(entry.ref or "").strip()
        if not artifact_ref:
            continue
        try:
            resolved = bool(artifact_resolver(artifact_ref, section_name, index, entry))
        except Exception as exc:
            errors.append(
                {
                    "path": f"artifacts.{section_name}[{index}].ref",
                    "message": f"Artifact resolver raised an error: {exc}",
                }
            )
            continue
        if not resolved:
            errors.append(
                {
                    "path": f"artifacts.{section_name}[{index}].ref",
                    "message": f"Artifact reference '{artifact_ref}' could not be resolved",
                }
            )
