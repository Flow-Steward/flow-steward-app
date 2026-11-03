"""Catalog secrets in workflow inputs: launch authority and the use check (ADR 0055).

A workflow input is bound to a catalog secret only in `catalog_secret_bindings`; the
workflow definition never carries the binding, so saving, AI Builder, import, export,
clone and revision restore cannot move or forge it. At launch, the binding is honoured
only when a person approved the definition being launched and the secret is available
to the launching project. The decision is recorded in protected run state, so the
step boundary checks what was decided for the revision that actually runs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from core.application.canonical_json import canonical_json_hash
from core.application.secrets.approval_gate import SecretConfigurationUnapproved
from core.application.secrets.projection import normalized_origin
from core.infrastructure.security.stored_secrets import StoredSecretError

#: Protected run state (see state_path_guards): fingerprint of an authorized catalog
#: storage key -> the destination rules a person approved for it.
AUTHORITY_STATE_KEY = "_workflow_catalog_secret_authority"
WORKFLOW_PROJECTION_VERSION = "workflow.v1"
#: Settings that are presentation or owned by other services, not executable content.
_COSMETIC_SETTINGS = frozenset({"canvas_annotations", "canvas_annotations_revision", "layout"})
#: Step keys that only place or label a step on the canvas.
_COSMETIC_STEP_KEYS = frozenset({"position", "label", "description", "ui", "layout", "notes"})


class SecretDestinationNotAllowed(StoredSecretError):
    """A request would carry a catalog secret to a destination no person approved."""

    code = "secret_destination_not_allowed"
    safe_message = "This request's destination is not approved for the stored secret it uses."


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strip_cosmetic(value: Any) -> Any:
    if isinstance(value, dict):
        is_step = "step_kind" in value or "step_id" in value
        return {
            key: _strip_cosmetic(item)
            for key, item in value.items()
            if not (is_step and key in _COSMETIC_STEP_KEYS)
        }
    if isinstance(value, list):
        return [_strip_cosmetic(item) for item in value]
    return value


def _steps(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _text(value.get("step_kind")):
            found.append(value)
        for item in value.values():
            found.extend(_steps(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_steps(item))
    return found


def http_destinations(settings: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """(rules for literal HTTP step URLs, ids of HTTP steps whose URL is computed)."""
    rules: list[dict[str, Any]] = []
    dynamic: list[str] = []
    for step in _steps(settings):
        if _text(step.get("step_kind")) != "http_request":
            continue
        override = _mapping(step.get("override"))
        config = _mapping(override.get("http_request"))
        url = _text(config.get("url"))
        origin = normalized_origin(url) if url and "{{" not in url else None
        if origin is None:
            dynamic.append(_text(step.get("step_id")) or "<unnamed step>")
            continue
        rule = {"kind": "exact_host", **origin}
        if rule not in rules:
            rules.append(rule)
    return rules, sorted(set(dynamic))


def workflow_projection(workflow: dict[str, Any], *, bindings: dict[str, str]) -> dict[str, Any]:
    settings = _mapping(workflow.get("settings"))
    executable = {key: item for key, item in settings.items() if key not in _COSMETIC_SETTINGS}
    return {
        "version": WORKFLOW_PROJECTION_VERSION,
        "workflow_id": _text(workflow.get("workflow_id")),
        "settings": _strip_cosmetic(executable),
        "trigger": workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {},
        "bindings": {str(k): str(v) for k, v in sorted(bindings.items())},
    }


def workflow_fingerprint(projection: dict[str, Any]) -> str:
    return "sha256:" + canonical_json_hash(projection)


def storage_ref_fingerprint(ref_key: str) -> str:
    from core.application.registry.workflow_secret_defaults import secret_ref_fingerprint

    return secret_ref_fingerprint(ref_key)


# -- destination rules -------------------------------------------------------


def normalize_rule(rule: Any) -> dict[str, Any]:
    """One approved destination: scheme, host, effective port, optional path prefix."""
    if not isinstance(rule, dict):
        raise ValueError("destination rule: an object is required")
    kind = _text(rule.get("kind")) or "exact_host"
    if kind != "exact_host":
        # Subdomain rules need a pinned Public Suffix List snapshot, which this
        # release does not ship; they are refused rather than approximated.
        raise ValueError(f"destination rule: '{kind}' is not supported")
    origin = normalized_origin(f"{_text(rule.get('scheme')).lower()}://{_text(rule.get('host'))}")
    if origin is None:
        raise ValueError("destination rule: scheme and host are required")
    port = int(rule.get("port") or origin["port"])
    prefix = _text(rule.get("path_prefix"))
    if prefix and not prefix.startswith("/"):
        raise ValueError("destination rule: path_prefix must start with '/'")
    normalized = {
        "kind": "exact_host",
        "scheme": origin["scheme"],
        "host": origin["host"],
        "port": port,
    }
    if prefix and prefix != "/":
        normalized["path_prefix"] = prefix.rstrip("/")
    return normalized


def url_allowed(url: Any, rules: list[dict[str, Any]]) -> bool:
    origin = normalized_origin(url)
    if origin is None:
        return False
    path = urlsplit(str(url)).path or "/"
    for rule in rules:
        if (rule.get("scheme"), rule.get("host"), int(rule.get("port") or 0)) != (
            origin["scheme"],
            origin["host"],
            origin["port"],
        ):
            continue
        prefix = _text(rule.get("path_prefix"))
        # Segment boundaries: "/api" admits "/api" and "/api/x", never "/api-x".
        if not prefix or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


# -- launch --------------------------------------------------------------------


def _workflow_row(cur: Any, *, account_id: str, workflow_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT workflow_id, settings, trigger
          FROM registry_workflows
         WHERE workflow_id = %s AND deleted_at IS NULL
           AND (account_id IS NULL OR account_id::text = %s)
        """,
        (workflow_id, account_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {"workflow_id": row[0], "settings": row[1], "trigger": row[2]}


def workflow_bindings(cur: Any, *, account_id: str, workflow_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT b.field_name, b.secret_id::text, s.storage_ref_key, s.project_id::text,
               s.workspace_id::text, b.consumer_project_id::text
          FROM catalog_secret_bindings AS b
          JOIN catalog_secrets AS s ON s.id = b.secret_id
         WHERE b.consumer_kind = 'workflow_input'
           AND b.workflow_id = %s AND b.account_id::text = %s
         ORDER BY b.field_name
        """,
        (workflow_id, account_id),
    )
    keys = (
        "field",
        "secret_id",
        "storage_ref_key",
        "project_id",
        "workspace_id",
        "consumer_project_id",
    )
    return [dict(zip(keys, row, strict=False)) for row in cur.fetchall() or []]


def workflow_approval(cur: Any, *, account_id: str, workflow_id: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT fingerprint, projection_jsonb
          FROM catalog_secret_approvals
         WHERE consumer_kind = 'workflow' AND workflow_id = %s AND account_id::text = %s
        """,
        (workflow_id, account_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (
        dict(row) if isinstance(row, dict) else {"fingerprint": row[0], "projection_jsonb": row[1]}
    )


def approved_rules(approval: dict[str, Any] | None) -> list[dict[str, Any]]:
    projection = (approval or {}).get("projection_jsonb") or {}
    rules = projection.get("approved_destinations") if isinstance(projection, dict) else None
    return list(rules) if isinstance(rules, list) else []


def launch_authority(
    cur: Any,
    *,
    account_id: str,
    workflow_id: str,
    project_id: str,
    received_storage_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """What this launch may use; raises when a binding or received secret is not approved."""
    bindings = workflow_bindings(cur, account_id=account_id, workflow_id=workflow_id)
    if not bindings and not received_storage_keys:
        return {}
    workflow = _workflow_row(cur, account_id=account_id, workflow_id=workflow_id)
    if workflow is None:
        raise SecretConfigurationUnapproved()
    projection = workflow_projection(
        workflow, bindings={row["field"]: row["secret_id"] for row in bindings}
    )
    approval = workflow_approval(cur, account_id=account_id, workflow_id=workflow_id)
    if not approval or approval.get("fingerprint") != workflow_fingerprint(projection):
        raise SecretConfigurationUnapproved()
    cur.execute(
        "SELECT workspace_id::text FROM projects WHERE id::text = %s AND account_id::text = %s",
        (project_id, account_id),
    )
    project_row = cur.fetchone()
    workspace_id = _text(project_row[0] if project_row else "")
    for row in bindings:
        if _text(row["project_id"]):
            available = _text(row["project_id"]) == _text(project_id)
        else:
            available = bool(workspace_id) and _text(row["workspace_id"]) == workspace_id
        if not available:
            raise SecretConfigurationUnapproved()
    rules = approved_rules(approval)
    authority = {
        storage_ref_fingerprint(row["storage_ref_key"]): {"rules": rules} for row in bindings
    }
    for key in received_storage_keys:
        authority[storage_ref_fingerprint(key)] = {"rules": rules}
    return authority


def launch_authority_for(
    project_id: Any, workflow_id: Any, *, received_storage_keys: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The authority to record in a new run's state; empty when nothing is approved.

    An unapproved or unavailable binding records no authority, so the step that
    would use the secret fails with `secret_configuration_unapproved`.
    """
    from core.application.registry.registry_manager import RegistryManager

    db = getattr(RegistryManager.instance(), "db_store", None)
    cursor = getattr(db, "cursor", None)
    if not callable(cursor) or not _text(project_id) or not _text(workflow_id):
        return {}
    with cursor() as cur:
        cur.execute(
            "SELECT account_id::text FROM projects WHERE id::text = %s", (_text(project_id),)
        )
        row = cur.fetchone()
        account_id = _text(
            row[0] if row and not isinstance(row, dict) else (row or {}).get("account_id")
        )
        if not account_id:
            return {}
        try:
            return launch_authority(
                cur,
                account_id=account_id,
                workflow_id=_text(workflow_id),
                project_id=_text(project_id),
                received_storage_keys=received_storage_keys,
            )
        except SecretConfigurationUnapproved:
            return {}


def child_launch_authority(
    parent_state: Any, child_workflow_id: Any, child_inputs: Any
) -> dict[str, Any]:
    """A child's authority: its own bindings plus catalog secrets the parent passes on.

    A passed catalog secret reaches the child only when a person approved the child's
    own definition too; each level of the call is checked.
    """
    from core.infrastructure.security.secret_reference import as_secret_reference

    state = parent_state if isinstance(parent_state, dict) else {}
    parent_authority = state.get(AUTHORITY_STATE_KEY)
    parent_authority = parent_authority if isinstance(parent_authority, dict) else {}
    received = sorted(
        {
            reference.ref_key
            for value in (child_inputs or {}).values()
            if (reference := as_secret_reference(value)) is not None
            and reference.ref_key
            and storage_ref_fingerprint(reference.ref_key) in parent_authority
        }
    )
    return launch_authority_for(
        state.get("project_id"), child_workflow_id, received_storage_keys=tuple(received)
    )


def overlay_storage_keys(cur: Any, *, account_id: str, workflow_id: str) -> dict[str, str]:
    """field -> catalog storage key, for the launch interface; carries no value."""
    return {
        row["field"]: row["storage_ref_key"]
        for row in workflow_bindings(cur, account_id=account_id, workflow_id=workflow_id)
    }


#: Input row keys that would claim a catalog binding or approval from inside a
#: definition. Bindings are rows of their own; a definition naming one is refused.
RESERVED_INPUT_KEYS = frozenset(
    {"catalog_secret", "catalog_secret_id", "secret_binding", "secret_approval"}
)


def reserved_binding_keys(settings: Any) -> list[str]:
    callable_block = settings.get("callable") if isinstance(settings, dict) else None
    rows = callable_block.get("expected_inputs") if isinstance(callable_block, dict) else None
    found = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            found.extend(
                f"{_text(row.get('name'))}.{key}" for key in RESERVED_INPUT_KEYS & set(row)
            )
    return sorted(found)


def overlay_workflow_row(db: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    """A copy of the row whose bound secret inputs point at their catalog secrets.

    Used only to build the interface of a launch. Nothing writes the copy back.
    """
    import copy

    cursor = getattr(db, "cursor", None)
    workflow_id = _text(workflow.get("workflow_id"))
    account_id = _text(workflow.get("account_id"))
    if not callable(cursor) or not workflow_id:
        return workflow
    with cursor() as cur:
        overlay = overlay_storage_keys(cur, account_id=account_id, workflow_id=workflow_id)
    if not overlay:
        return workflow
    copied = copy.deepcopy(workflow)
    callable_block = (copied.get("settings") or {}).get("callable") or {}
    for row in callable_block.get("expected_inputs") or []:
        name = _text(row.get("name")) if isinstance(row, dict) else ""
        if name in overlay and _text(row.get("type")).lower() == "secret":
            row["default_secret_ref"] = overlay[name]
    return copied


# -- step boundary ------------------------------------------------------------


def is_catalog_storage_key(cur: Any, *, account_id: str, ref_key: str) -> bool:
    cur.execute(
        "SELECT 1 FROM catalog_secrets WHERE account_id::text = %s AND storage_ref_key = %s",
        (account_id, ref_key),
    )
    return cur.fetchone() is not None


def require_catalog_ref_authority(
    conn: Any, runtime_state: dict[str, Any], *, ref_key: str, url: Any = None
) -> None:
    """Refuse a catalog-owned reference this run was not authorized to use, or send there."""
    context = runtime_state.get("runtime_context") if isinstance(runtime_state, dict) else None
    account_id = _text((context or {}).get("account_id"))
    if conn is None or not ref_key:
        return
    with conn.cursor() as cur:
        if not is_catalog_storage_key(cur, account_id=account_id, ref_key=ref_key):
            return
    authority = runtime_state.get(AUTHORITY_STATE_KEY) if isinstance(runtime_state, dict) else None
    entry = (
        (authority or {}).get(storage_ref_fingerprint(ref_key))
        if isinstance(authority, dict)
        else None
    )
    if not isinstance(entry, dict):
        raise SecretConfigurationUnapproved()
    if url is not None and not url_allowed(url, list(entry.get("rules") or [])):
        raise SecretDestinationNotAllowed()


class CatalogAuthorityReader:
    """A stored-secret reader that first checks the run's catalog authority.

    Wraps the host-selected reader at the HTTP and connector boundaries, so the check
    runs before every stored read and never for an inline secret.
    """

    def __init__(
        self,
        reader: Any,
        *,
        conn_provider: Any,
        runtime_state: dict[str, Any],
        url_provider: Any = None,
    ) -> None:
        self._reader = reader
        self._conn_provider = conn_provider
        self._state = runtime_state
        self._url_provider = url_provider

    def read(self, context: Any, ref_key: str) -> str:
        conn = self._conn_provider()
        if callable(getattr(conn, "cursor", None)):
            require_catalog_ref_authority(
                conn,
                self._state,
                ref_key=ref_key,
                url=self._url_provider() if self._url_provider is not None else None,
            )
        return self._reader.read(context, ref_key)


__all__ = [
    "AUTHORITY_STATE_KEY",
    "RESERVED_INPUT_KEYS",
    "CatalogAuthorityReader",
    "SecretDestinationNotAllowed",
    "approved_rules",
    "child_launch_authority",
    "http_destinations",
    "launch_authority",
    "launch_authority_for",
    "normalize_rule",
    "overlay_storage_keys",
    "overlay_workflow_row",
    "require_catalog_ref_authority",
    "reserved_binding_keys",
    "url_allowed",
    "workflow_bindings",
    "workflow_fingerprint",
    "workflow_projection",
]
