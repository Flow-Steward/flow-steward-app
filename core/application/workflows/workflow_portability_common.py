"""Common workflow portability helpers and constants."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

BUNDLE_WORKFLOWS_VALUE = "bundle.workflows"


SCHEMA_VERSION = "platform.workflow_bundle.v2"
BUNDLE_KIND = "workflow_bundle"
TOP_LEVEL_KEYS = (
    "schema_version",
    "kind",
    "metadata",
    "workflows",
    "dependencies",
    "import_hints",
)
#: The project's hook bindings of the workflows a bundle carries. Only a
#: project-scoped export writes it, so a bundle without it is complete.
HOOK_BINDINGS_KEY = "hook_bindings"
#: The agents, skills, data contracts and guardrails the workflows use; an import
#: creates them. A file without it names them only, as before.
DEFINITIONS_BUNDLE_KEY = "definitions"
OPTIONAL_TOP_LEVEL_KEYS = (HOOK_BINDINGS_KEY, DEFINITIONS_BUNDLE_KEY)
DEPENDENCY_STATUSES = {
    "resolved",
    "create_from_bundle",
    "missing",
    "conflict",
    "requires_mapping",
    "requires_extension_install",
    "requires_project_connection",
    "protected_reference",
    "blocked",
}
BLOCKING_STATUSES = {
    "missing",
    "conflict",
    "requires_mapping",
    "requires_extension_install",
    "requires_project_connection",
    "protected_reference",
    "blocked",
}
RESOLUTION_ACTIONS = {
    "use_existing",
    "map_existing",
    "rename",
    "rename_workflow",
    "install_extension_required",
    "create_connection_placeholder",
    "skip_optional",
    "abort",
}
#: Matched against whole words of a key (snake, kebab or camel case), never as
#: substrings: `access_token` is a credential, `max_tokens` is a number.
_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
    "bearer",
    "credential",
    "credentials",
    "api_key",
    "access_key",
    "private_key",
    "client_secret",
)
_KEY_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|[^A-Za-z0-9]+")
#: Words that make `tokens` a count of model tokens rather than credentials.
_TOKEN_COUNT_WORDS = frozenset(
    {
        "max",
        "min",
        "total",
        "prompt",
        "completion",
        "input",
        "output",
        "count",
        "limit",
        "budget",
        "used",
    }
)
_WORKFLOW_SAVE_FIELDS = (
    "workflow_id",
    "version",
    "display_name",
    "description",
    "trigger",
    "settings",
    "participants",
    "tags",
    "steps",
    "groups",
    "for_each",
    "execution_graph",
)
TRUSTED_WORKFLOW_METADATA_FIELDS = {
    "source_install_id",
    "source_release_id",
    "source_projection_id",
    "local_workflow_id",
    "projection_mode",
    "derived_from_kind",
    "derived_from_ref",
    "sync_status",
    "availability_status",
    "availability_reason_code",
    "availability_reason_text",
    "new_runs_blocked",
    "visible_in_catalog",
}
PROTECTED_WORKFLOW_PATCH_FIELDS = {
    "account_id",
    "scope_kind",
    "trigger",
    "settings",
    "participants",
    "tags",
    *TRUSTED_WORKFLOW_METADATA_FIELDS,
}
TRUSTED_WORKFLOW_DEFAULTS: dict[str, Any] = {
    "source_install_id": None,
    "source_release_id": None,
    "source_projection_id": None,
    "local_workflow_id": None,
    "projection_mode": "manual",
    "derived_from_kind": "",
    "derived_from_ref": "",
    "sync_status": "active",
    "availability_status": "available",
    "availability_reason_code": "none",
    "availability_reason_text": "",
    "new_runs_blocked": False,
    "visible_in_catalog": True,
}


# Routing/classification keys that authored workflow create/update reject. Import
# must apply the same policy so it cannot persist forbidden trigger metadata that
# normal authoring forbids (FS-DEEP-CAND-014). Kept in sync with
# core/entrypoints/admin/workflows_common.FORBIDDEN_TRIGGER_KEYS.
FORBIDDEN_TRIGGER_KEYS = {"task_types", "complexity"}


def _sanitize_imported_trigger(trigger: Any) -> dict[str, Any]:
    """Strip forbidden routing keys and clamp confidence_threshold on an imported trigger.

    Mirrors the admin authoring sanitizer (`_sanitize_workflow_trigger`). Unlike the
    authoring path, which raises on forbidden keys, import silently drops them so a
    preflighted bundle still applies as a disabled draft without the policy-bypass
    metadata reaching persistence.
    """
    if not isinstance(trigger, dict):
        return {}
    cleaned = {key: value for key, value in trigger.items() if key not in FORBIDDEN_TRIGGER_KEYS}
    if "confidence_threshold" in cleaned:
        raw_threshold = cleaned.get("confidence_threshold")
        try:
            value = float(raw_threshold) if isinstance(raw_threshold, (int, float, str)) else 0.0
        except (TypeError, ValueError):
            value = 0.0
        cleaned["confidence_threshold"] = max(0.0, min(1.0, value))
    return cleaned


class WorkflowPortabilityError(ValueError):
    """Structured portability error mapped by the admin transport layer."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "workflow_portability_error",
        status_code: int = 400,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.errors = errors or []

    def detail(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": str(self)}
        if self.errors:
            payload["errors"] = self.errors
        return payload


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _is_mock(value: Any) -> bool:
    return type(value).__module__.startswith("unittest.mock")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _sensitive_key(key: Any) -> bool:
    words = [word.lower() for word in _KEY_WORD_BOUNDARY.split(str(key or "")) if word]
    if "tokens" in words and _TOKEN_COUNT_WORDS.intersection(words):
        words = [word for word in words if word != "tokens"]
    joined = f"_{'_'.join(words)}_"
    return any(f"_{marker}_" in joined for marker in _SENSITIVE_KEY_MARKERS)


#: Keys that are dropped rather than masked. A pointer to a credential is not a
#: credential, but replacing one with "[redacted]" produces a bundle that
#: imports as configured while pointing at nothing — worse than either keeping
#: it or leaving it out, because the workflow then looks ready and is not. The
#: target environment holds no such secret, so absent is the truthful answer.
#: A test-run file is dropped for the same reason: its artifact stays here.
_DROPPED_SENSITIVE_KEYS = frozenset({"default_secret_ref", "sample_file"})


def _redact_sensitive(value: Any, *, key: str = "") -> Any:
    if _sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) not in _DROPPED_SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _workflow_settings(workflow: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(workflow.get("settings"))


def _explicit_full_export_allowed(workflow: dict[str, Any]) -> bool:
    settings = _workflow_settings(workflow)
    candidates = [
        _as_dict(workflow.get("extension_policy")),
        _as_dict(workflow.get("export_policy")),
        _as_dict(workflow.get("source_policy")),
        _as_dict(settings.get("extension_policy")),
        _as_dict(settings.get("export_policy")),
        _as_dict(settings.get("workflow_policy")),
        _as_dict(settings.get("portability")),
    ]
    for policy in candidates:
        for key in (
            "allow_full_export",
            "full_export_allowed",
            "allow_export",
            "export_allowed",
            "allow_fork",
            "fork_allowed",
        ):
            if policy.get(key) is True:
                return True
    return False


def _extension_owned(workflow: dict[str, Any]) -> bool:
    return (
        bool(_text(workflow.get("source_install_id")))
        or _lower(workflow.get("projection_mode")) == "derived"
    )


def _normalize_participants(workflow: dict[str, Any]) -> list[dict[str, str]]:
    settings = _workflow_settings(workflow)
    raw = workflow.get("participants")
    if not isinstance(raw, list):
        raw = settings.get("participants")
    participants: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        participant_key = _text(item.get("participant_key"))
        agent_id = _text(item.get("agent_id") or item.get("agent"))
        if not participant_key or not agent_id or participant_key in seen:
            continue
        seen.add(participant_key)
        participants.append({"participant_key": participant_key, "agent_id": agent_id})
    return participants


def _workflow_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in _as_list(workflow.get("steps")) if isinstance(item, dict)]


def _workflow_groups(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in _as_list(workflow.get("groups")) if isinstance(item, dict)]


def _workflow_for_each(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in _as_list(workflow.get("for_each")) if isinstance(item, dict)]


def _raw_mcp_connector_issues(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for step_index, step in enumerate(_workflow_steps(workflow)):
        connector_id = _text(step.get("connector_id"))
        if not connector_id.startswith("mcp_"):
            continue
        step_id = _text(step.get("step_id")) or f"step_{step_index + 1}"
        issues.append(
            {
                "code": "workflow.step_connector_raw_mcp_unsupported",
                "path": f"workflow.steps.{step_id}.connector_id",
                "step_id": step_id,
                "connector_id": connector_id,
                "message": (
                    f"Step '{step_id}' uses raw MCP connector_id "
                    f"'{connector_id}'. Bind the projected registry tool id "
                    "(for example 'connector.<tool_id>') instead."
                ),
            }
        )
    return issues


def _raise_if_raw_mcp_connector_bindings(workflow: dict[str, Any]) -> None:
    issues = _raw_mcp_connector_issues(workflow)
    if not issues:
        return
    raise WorkflowPortabilityError(
        "Workflow payload uses raw MCP connector ids. Bind projected registry tool ids instead.",
        code="workflow_raw_mcp_connector_unsupported",
        status_code=400,
        errors=issues,
    )


def _dependency_key(dependency_type: str, ref: str) -> str:
    return f"{dependency_type}:{ref}"


def _invalid_dependency_ref(ref: Any) -> bool:
    return _lower(ref) in {"none", "null", "undefined", "n/a", "-"}


def _step_kind(step: dict[str, Any]) -> str:
    return _lower(step.get("step_kind") or "agent") or "agent"


def _step_invoke_workflow_override(step: dict[str, Any]) -> dict[str, Any]:
    override = _as_dict(step.get("override"))
    return _as_dict(override.get("invoke_workflow"))


def _step_uses_child_workflow(step: dict[str, Any]) -> bool:
    if _step_invoke_workflow_override(step):
        return True
    if _step_kind(step) == "invoke_workflow":
        return True
    return _lower(step.get("agent_id") or step.get("agent")) == "invoke_workflow"


def _step_child_workflow_ref(step: dict[str, Any]) -> str:
    invoke_workflow = _step_invoke_workflow_override(step)
    override_ref = _text(invoke_workflow.get("workflow_id"))
    if override_ref:
        return override_ref
    top_level_ref = _text(step.get("workflow_id") or step.get("child_workflow_id"))
    if _invalid_dependency_ref(top_level_ref) and not _step_uses_child_workflow(step):
        return ""
    return top_level_ref


def _missing_dependency_label(dependency_type: str) -> str:
    labels = {
        "agent": "Missing agent id",
        "skill": "Missing skill id",
        "tool": "Missing tool id",
        "child_workflow": "Missing workflow id",
        "published_block": "Missing published block key",
        "extension": "Missing extension install id",
        "project_connection": "Missing project connection id",
    }
    return labels.get(dependency_type, "Missing dependency id")


def _add_dependency(
    dependencies: dict[tuple[str, str], dict[str, Any]],
    dependency_type: str,
    ref: str,
    *,
    label: str = "",
    source: str = "",
    required: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    normalized_type = _text(dependency_type)
    normalized_ref = _text(ref)
    if not normalized_type or not normalized_ref:
        return
    invalid_ref = _invalid_dependency_ref(normalized_ref)
    metadata_payload = dict(metadata or {})
    if invalid_ref:
        metadata_payload.setdefault("code", "invalid_dependency_ref")
        metadata_payload.setdefault("raw_ref", normalized_ref)
        label = label or _missing_dependency_label(normalized_type)
    key = (normalized_type, normalized_ref)
    row = dependencies.setdefault(
        key,
        {
            "dependency_key": _dependency_key(normalized_type, normalized_ref),
            "dependency_type": normalized_type,
            "ref": normalized_ref,
            "label": label or normalized_ref,
            "sources": [],
            "required": bool(required),
            "metadata": {},
        },
    )
    if source and source not in row["sources"]:
        row["sources"].append(source)
    row["required"] = bool(row.get("required", True) and required)
    if metadata_payload:
        row["metadata"].update(_redact_sensitive(metadata_payload))


def _connection_refs(raw_binding: Any) -> list[str]:
    refs: set[str] = set()

    def _push(value: Any) -> None:
        text = _text(value)
        if text:
            refs.add(text)

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in (
                "connection_id",
                "project_connection_id",
                "connection_key",
                "binding_key",
                "connection_ref",
            ):
                _push(value.get(key))
            for nested_key in ("connections", "items", "bindings"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    for item in nested:
                        _walk(item)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item)
            return
        _push(value)

    _walk(raw_binding)
    return sorted(refs)


def _tool_refs_from_override(override: Any) -> list[str]:
    if not isinstance(override, dict):
        return []
    refs: list[str] = []
    for item in _as_list(override.get("add_tools")):
        if isinstance(item, dict):
            tool_id = _text(item.get("tool_id") or item.get("id") or item.get("name"))
        else:
            tool_id = _text(item)
        if tool_id and tool_id not in refs:
            refs.append(tool_id)
    return refs


def _call_db_get_scoped(
    db: Any, method_name: str, ref: str, *, account_id: str
) -> dict[str, Any] | None:
    """Like _call_db_get, but passes the tenant the lookup must be confined to."""
    method = getattr(db, method_name, None)
    if not callable(method):
        logger.debug("scoped portability lookup %s is absent on %s", method_name, type(db).__name__)
        return None
    try:
        row = method(ref, account_id=account_id)
    except Exception:
        logger.warning(
            "scoped portability lookup %s failed on %s",
            method_name,
            type(db).__name__,
            exc_info=True,
        )
        return None
    if _is_mock(row) or not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "model_dump"):
        dumped = row.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    return None


def _call_db_get(db: Any, method_name: str, ref: str, **kwargs: Any) -> dict[str, Any] | None:
    method = getattr(db, method_name, None)
    if not callable(method):
        # AC17-R67: this is the branch that fired on every deployment, because the store the
        # admin routes pass has none of the probed names. It looked identical to "the row is
        # not there", which is why the defect survived: the operator and the log both only
        # ever saw "Required extension install is missing."
        logger.debug("portability lookup %s is absent on %s", method_name, type(db).__name__)
        return None
    try:
        row = method(ref, **kwargs)
    except Exception:
        # Still swallowed, because a probe must not turn a missing dependency into a 500 --
        # but no longer silently. AC17-R67 was undiagnosable precisely here.
        logger.warning(
            "portability lookup %s failed on %s", method_name, type(db).__name__, exc_info=True
        )
        return None
    if _is_mock(row) or not row:
        return None
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "model_dump"):
        dumped = row.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    return None


def _callable_db_method(db: Any, method_name: str) -> Any:
    method = getattr(db, method_name, None)
    if not callable(method) or _is_mock(method):
        return None
    return method


def _row_dict(row: Any) -> dict[str, Any] | None:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "model_dump"):
        dumped = row.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    return None


def _call_db_list(
    db: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    method = _callable_db_method(db, method_name)
    if method is None:
        return []
    try:
        rows = method(*args, **kwargs) or []
    except TypeError:
        try:
            rows = method(*args) or []
        except Exception:
            return []
    except Exception:
        return []
    if hasattr(rows, "model_dump"):
        dumped = rows.model_dump()
        if isinstance(dumped, dict):
            for value in dumped.values():
                if isinstance(value, list):
                    rows = value
                    break
    if not isinstance(rows, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        row_dict = _row_dict(row)
        if row_dict is not None:
            normalized.append(row_dict)
    return normalized


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _dependency_option(
    row: dict[str, Any],
    *,
    value_keys: tuple[str, ...],
    label_keys: tuple[str, ...] = ("display_name", "name", "label"),
    description_keys: tuple[str, ...] = ("description_preview", "description", "summary"),
) -> dict[str, Any] | None:
    value = _first_text(row, value_keys)
    if not value:
        return None
    label = _first_text(row, label_keys) or value
    option: dict[str, Any] = {
        "value": value,
        "label": label,
    }
    description = _first_text(row, description_keys)
    if description:
        option["description"] = description
    if "enabled" in row:
        option["enabled"] = bool(row.get("enabled"))
    status = _first_text(row, ("status", "availability_status", "sync_status"))
    if status:
        option["status"] = status
    metadata = {
        key: _redact_sensitive(row.get(key), key=key)
        for key in (
            "category",
            "model",
            "skill_count",
            "tool_count",
            "impl_type",
            "cost_tier",
            "step_count",
            "block_type",
            "source_kind",
            "project_id",
            "extension_id",
            "extension_install_id",
            "connector_id",
        )
        if row.get(key) not in (None, "", [], {})
    }
    if metadata:
        option["metadata"] = metadata
    return option


def _sorted_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(options, key=lambda item: (_lower(item.get("label")), _lower(item.get("value"))))


def _agent_tool_refs(db: Any, agent_id: str) -> list[str]:
    refs: list[str] = []
    get_agent_tools = getattr(db, "get_agent_tools", None)
    if callable(get_agent_tools):
        try:
            rows = get_agent_tools(agent_id) or []
        except Exception:
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    tool_id = _text(row.get("tool_id") or row.get("id"))
                else:
                    tool_id = _text(row)
                if tool_id and tool_id not in refs:
                    refs.append(tool_id)
    agent = _call_db_get(db, "get_agent", agent_id) if db is not None else None
    for item in _as_list((agent or {}).get("tools")):
        if isinstance(item, dict):
            tool_id = _text(item.get("tool_id") or item.get("id"))
        else:
            tool_id = _text(item)
        if tool_id and tool_id not in refs:
            refs.append(tool_id)
    return refs


def _list_runtime_dependencies(db: Any, workflow_id: str) -> list[dict[str, Any]]:
    method = getattr(db, "list_workflow_runtime_dependencies", None)
    if callable(method):
        try:
            rows = method(workflow_id) or []
        except Exception:
            rows = []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]

    if not callable(getattr(db, "_cursor", None)) or not callable(
        getattr(db, "_table_exists", None)
    ):
        return []
    try:
        if not db._table_exists("workflow_runtime_dependencies"):
            return []
        with db._cursor() as cur:
            cur.execute(
                """
                SELECT dependency_kind, dependency_ref, install_id, block_key, group_id, step_id
                  FROM workflow_runtime_dependencies
                 WHERE workflow_id = %s
                """,
                (workflow_id,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []
