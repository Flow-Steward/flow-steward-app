"""Catalog secrets in workflow inputs: launch authority and the use check (ADR 0057).

A workflow input is bound to a catalog secret only in `catalog_secret_bindings`; the
workflow definition never carries the binding, so saving, AI Builder, import, export,
clone and revision restore cannot move or forge it.

Approval covers the definition a run executes: the same `frozen_definition` a job pins,
steps included. Nothing is decided at launch and carried in run state. Each stored read
at the HTTP and connector boundaries looks up the job's own pin, the current bindings
and the current approval in the host database, so an edit after enqueue never changes
what a run may do, and a retry, redelivery or resume of the same job is checked again
against the same pin.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from core.application.canonical_json import canonical_json_hash
from core.application.secrets.approval_gate import SecretConfigurationUnapproved
from core.application.secrets.projection import normalized_origin
from core.infrastructure.security.stored_secrets import StoredSecretError

WORKFLOW_PROJECTION_VERSION = "workflow.v2"
#: Settings that are presentation or owned by other services, not executable content.
_COSMETIC_SETTINGS = frozenset(
    {"canvas_annotations", "canvas_annotations_revision", "canvas_layout", "layout"}
)
#: Step keys that only place or label a step on the canvas.
_COSMETIC_STEP_KEYS = frozenset(
    {"position", "label", "display_name", "description", "ui", "layout", "notes"}
)


class SecretDestinationNotAllowed(StoredSecretError):
    """A request would carry a catalog secret to a destination no person approved."""

    code = "secret_destination_not_allowed"
    safe_message = "This request's destination is not approved for the stored secret it uses."


class CatalogSecretReceiverUnpinned(StoredSecretError):
    """A catalog secret would reach code whose release no approval can pin.

    A connector step's input goes to whatever extension release, tool or MCP server
    is selected when it is dispatched, after the value is read. Until that choice
    is bound to an approved immutable identity, a catalog secret does not go there;
    a connection's own stored credentials carry their release approval instead.
    """

    code = "catalog_secret_receiver_unpinned"
    safe_message = (
        "A stored catalog secret cannot be passed to a connector step input. "
        "Store it in the connection the step uses."
    )


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


def _static_origin(url: str) -> dict[str, Any] | None:
    """The origin of a URL whose scheme and authority are literal; None when computed.

    A template in the path or query keeps the origin known: the HTTP step itself
    refuses a secret-bearing request whose rendered origin differs from it.
    """
    if "{{" not in url:
        return normalized_origin(url)
    scheme_end = url.find("://")
    if scheme_end < 0 or "{{" in url[:scheme_end]:
        return None
    rest = url[scheme_end + 3 :]
    authority_end = min(
        (index for index in (rest.find("/"), rest.find("?"), rest.find("#")) if index >= 0),
        default=-1,
    )
    if authority_end < 0 or "{{" in rest[:authority_end]:
        return None
    return normalized_origin(url[: scheme_end + 3 + authority_end])


def http_destinations(definition: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """(origins of HTTP steps, ids of HTTP steps whose scheme or host is computed).

    `definition` is a pinned or current `frozen_definition`: its steps, Groups and
    For each containers are all searched.
    """
    rules: list[dict[str, Any]] = []
    dynamic: list[str] = []
    for step in _steps(definition):
        if _text(step.get("step_kind")) != "http_request":
            continue
        override = _mapping(step.get("override"))
        config = _mapping(override.get("http_request"))
        url = _text(config.get("url"))
        origin = _static_origin(url) if url else None
        if origin is None:
            dynamic.append(_text(step.get("step_id")) or "<unnamed step>")
            continue
        rule = {"kind": "exact_host", **origin}
        if rule not in rules:
            rules.append(rule)
    return rules, sorted(set(dynamic))


def connector_steps(definition: Any) -> list[dict[str, Any]]:
    """The connector steps of a definition, in Groups and For each containers too."""
    return [step for step in _steps(definition) if _text(step.get("step_kind")) == "connector"]


def connector_step_ids(definition: Any) -> list[str]:
    return sorted(
        {_text(step.get("step_id")) or "<unnamed step>" for step in connector_steps(definition)}
    )


def workflow_projection(definition: Any, *, bindings: dict[str, str]) -> dict[str, Any]:
    """The approved part of a `frozen_definition`: everything it executes, plus bindings.

    Connector receivers are approved beside it, per project (`approved_receivers`).
    """
    body = _mapping(definition)
    settings = _mapping(body.get("settings"))
    executable = {key: item for key, item in body.items() if key not in {"settings", "protected"}}
    executable["settings"] = {
        key: item for key, item in settings.items() if key not in _COSMETIC_SETTINGS
    }
    return {
        "version": WORKFLOW_PROJECTION_VERSION,
        "workflow_id": _text(body.get("workflow_id")),
        "definition": _strip_cosmetic(executable),
        "bindings": {str(k): str(v) for k, v in sorted(bindings.items())},
    }


def current_projection(
    cur: Any, *, account_id: str, workflow_id: str, definition: Any
) -> dict[str, Any]:
    """The projection of `definition` with the workflow's current bindings."""
    bindings = workflow_bindings(cur, account_id=account_id, workflow_id=workflow_id)
    return workflow_projection(
        definition, bindings={row["field"]: row["secret_id"] for row in bindings}
    )


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
    if prefix and _request_path(prefix) != prefix:
        raise ValueError("destination rule: path_prefix must be a plain path")
    normalized = {
        "kind": "exact_host",
        "scheme": origin["scheme"],
        "host": origin["host"],
        "port": port,
    }
    if prefix and prefix != "/":
        normalized["path_prefix"] = prefix.rstrip("/")
    return normalized


#: Percent-encodings a server may decode into a dot segment or a separator.
_AMBIGUOUS_PATH_ENCODINGS = ("%2e", "%2f", "%5c")


def _request_path(path: str) -> str | None:
    """The path as the client sends it, dot segments resolved (RFC 3986, 5.2.4).

    None when a server could still read it differently: a backslash, or an encoded
    dot, slash or backslash.
    """
    lowered = path.lower()
    if "\\" in path or any(code in lowered for code in _AMBIGUOUS_PATH_ENCODINGS):
        return None
    resolved: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == ".":
            if last:
                resolved.append("")
        elif segment == "..":
            if len(resolved) > 1:
                resolved.pop()
            if last:
                resolved.append("")
        else:
            resolved.append(segment)
    return "/".join(resolved) or "/"


def url_allowed(url: Any, rules: list[dict[str, Any]]) -> bool:
    origin = normalized_origin(url)
    if origin is None:
        return False
    path = _request_path(urlsplit(str(url)).path or "/")
    for rule in rules:
        if (rule.get("scheme"), rule.get("host"), int(rule.get("port") or 0)) != (
            origin["scheme"],
            origin["host"],
            origin["port"],
        ):
            continue
        prefix = _text(rule.get("path_prefix"))
        if not prefix:
            return True
        # Segment boundaries: "/api" admits "/api" and "/api/x", never "/api-x".
        if path is not None and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


# -- launch --------------------------------------------------------------------


def workflow_row(cur: Any, *, account_id: str, workflow_id: str) -> dict[str, Any] | None:
    """The registry row's identity and settings (the interface), not its steps."""
    cur.execute(
        """
        SELECT workflow_id, settings
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
    return {"workflow_id": row[0], "settings": row[1]}


def current_definition(conn: Any, *, account_id: str, workflow_id: str) -> dict[str, Any] | None:
    """The `frozen_definition` a job pinned now would execute, read like the pin reads it."""
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionPinError,
        WorkflowDefinitionSnapshotRepository,
    )

    try:
        return WorkflowDefinitionSnapshotRepository(conn).current_definition(
            workflow_id, account_id=account_id
        )
    except WorkflowDefinitionPinError:
        return None


def workflow_bindings(cur: Any, *, account_id: str, workflow_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT b.field_name AS field, b.secret_id::text AS secret_id, s.storage_ref_key,
               s.project_id::text AS project_id, s.workspace_id::text AS workspace_id,
               b.consumer_project_id::text AS consumer_project_id
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
    # A dict cursor (the registry store's) and a tuple cursor give the same rows.
    return [
        {key: row.get(key) for key in keys}
        if isinstance(row, dict)
        else dict(zip(keys, row, strict=False))
        for row in cur.fetchall() or []
    ]


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


def overlay_storage_keys(cur: Any, *, account_id: str, workflow_id: str) -> dict[str, str]:
    """field -> catalog storage key, for the launch interface; carries no value."""
    return {
        row["field"]: row["storage_ref_key"]
        for row in workflow_bindings(cur, account_id=account_id, workflow_id=workflow_id)
    }


def catalog_bound_interface(interface: Any, *, workflow_id: Any, account_id: Any) -> Any:
    """`interface` with each catalog-bound secret input defaulting to its catalog secret.

    The default is a storage reference, never a value, and public contract payloads
    reduce it to `default_configured`: a bound input is then not asked for again,
    in the builder's Test Run, the operator contract or an Invoke mapping.
    Whether a run may read it is decided at the step boundary.
    """
    from core.application.registry.registry_manager import RegistryManager

    try:
        db = getattr(RegistryManager.instance(), "db_store", None)
    except Exception:
        return interface
    cursor = registry_cursor(db)
    if not callable(cursor) or not _text(workflow_id) or not _text(account_id):
        return interface
    with cursor() as cur:
        overlay = overlay_storage_keys(
            cur, account_id=_text(account_id), workflow_id=_text(workflow_id)
        )
    if not overlay:
        return interface
    rows = [
        row.model_copy(update={"default_secret_ref": overlay[row.name]})
        if row.name in overlay and _text(row.type).lower() == "secret"
        else row
        for row in interface.expected_inputs
    ]
    return interface.model_copy(update={"expected_inputs": rows})


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


def registry_cursor(db: Any) -> Any:
    """A cursor factory of the registry store: its `_cursor()`, or a plain `cursor()`.

    The registry store keeps its cursor helper private; a lookup of `cursor` alone
    missed it and skipped every overlay silently.
    """
    for name in ("_cursor", "cursor"):
        factory = getattr(db, name, None)
        if callable(factory):
            return factory
    return None


def overlay_workflow_row(db: Any, workflow: dict[str, Any]) -> dict[str, Any]:
    """A copy of the row whose bound secret inputs point at their catalog secrets.

    Used only to build the interface of a launch. Nothing writes the copy back.
    """
    import copy

    cursor = registry_cursor(db)
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


def _catalog_secret(cur: Any, ref_key: str) -> dict[str, str] | None:
    """The catalog secret a storage key belongs to, in any account; None when it is not one."""
    cur.execute(
        """
        SELECT id::text, account_id::text, project_id::text, workspace_id::text
          FROM catalog_secrets
         WHERE storage_ref_key = %s
        """,
        (ref_key,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    values = list(row.values()) if isinstance(row, dict) else list(row)
    keys = ("id", "account_id", "project_id", "workspace_id")
    return {key: _text(value) for key, value in zip(keys, values, strict=False)}


def run_approval(conn: Any, cur: Any, *, job_id: str, ref_key: str) -> tuple[dict, str]:
    """(approval, job project) that let this job read a catalog secret; raises otherwise.

    Everything comes from the host database: the job's pin, the secret's owner, the
    workflow's current bindings and approval. Run state contributes only the job id,
    which steps cannot write.
    """
    from core.application.secrets.run_authority import load_run_job, run_secret_authority

    secret = _catalog_secret(cur, ref_key)
    if secret is None:
        raise SecretConfigurationUnapproved()
    job = load_run_job(cur, job_id) if _text(job_id) else None
    if job is None or job.definition is None or job.account_id != secret["account_id"]:
        raise SecretConfigurationUnapproved()
    if secret["project_id"]:
        available = secret["project_id"] == job.project_id
    else:
        available = bool(job.workspace_id) and secret["workspace_id"] == job.workspace_id
    if not available:
        raise SecretConfigurationUnapproved()
    if ref_key not in run_secret_authority(conn, cur, job_id=job.job_id).stored_ref_keys:
        raise SecretConfigurationUnapproved()
    projection = current_projection(
        cur, account_id=job.account_id, workflow_id=job.workflow_id, definition=job.definition
    )
    approval = workflow_approval(cur, account_id=job.account_id, workflow_id=job.workflow_id)
    if not approval or approval.get("fingerprint") != workflow_fingerprint(projection):
        raise SecretConfigurationUnapproved()
    return approval, job.project_id


#: Who receives a resolved value: an HTTP request, checked against the approved
#: destinations, or a connector step's input, checked against the approved release
#: of the extension it was pinned to. A connector input with no pinned extension (a
#: tool or an MCP server) never receives a catalog secret.
RECEIVER_HTTP = "http"
RECEIVER_CONNECTOR = "connector"


def approval_token(fingerprint: str, receivers: list[dict[str, Any]]) -> str:
    """What a person is shown and approves: the definition and this project's receivers."""
    return "sha256:" + canonical_json_hash({"fingerprint": fingerprint, "receivers": receivers})


def receivers_approved(approval: dict[str, Any] | None, receivers: list[dict[str, Any]]) -> bool:
    """Whether every selectable receiver of this project is in the approved set."""
    approved = approved_receivers(approval)
    return all(row in approved for row in receivers if row.get("kind") != "unpinned")


def merged_receivers(
    approval: dict[str, Any] | None, fingerprint: str, receivers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The receivers an approval holds after approving `receivers` for their project.

    Another project's entries survive only while the definition stays the approved one.
    """
    project_ids = {row.get("project_id") for row in receivers}
    kept = (
        [row for row in approved_receivers(approval) if row.get("project_id") not in project_ids]
        if approval and approval.get("fingerprint") == fingerprint
        else []
    )
    return [*kept, *receivers]


def approved_receivers(approval: dict[str, Any] | None) -> list[dict[str, Any]]:
    projection = (approval or {}).get("projection_jsonb") or {}
    rows = projection.get("receivers") if isinstance(projection, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def catalog_only_refs(conn: Any, keys: set[str]) -> bool:
    """Whether every key is a catalog secret's storage key, by the host database."""
    if not keys or conn is None or not callable(getattr(conn, "cursor", None)):
        return False
    from core.application.secrets.run_authority import catalog_storage_keys

    with conn.cursor() as cur:
        return catalog_storage_keys(cur, set(keys)) == set(keys)


def require_catalog_ref_authority(
    conn: Any,
    runtime_state: dict[str, Any],
    *,
    ref_key: str,
    receiver: str,
    url: Any = None,
    receiver_identity: dict[str, str] | None = None,
    require_catalog: bool = False,
) -> None:
    """Refuse a catalog-owned reference this run may not read, or may not send there.

    `require_catalog` comes from a request whose computed origin was let through
    because its references were catalog secrets: a key that is not one any more is
    refused rather than read without an allowlist.
    """
    if not ref_key:
        return
    if conn is None or not callable(getattr(conn, "cursor", None)):
        # Without the host database the reference cannot be classified: refuse.
        raise SecretConfigurationUnapproved()
    state = runtime_state if isinstance(runtime_state, dict) else {}
    with conn.cursor() as cur:
        if _catalog_secret(cur, ref_key) is None:
            if require_catalog:
                raise SecretDestinationNotAllowed()
            return
        if receiver == RECEIVER_CONNECTOR and not receiver_identity:
            raise CatalogSecretReceiverUnpinned()
        approval, project_id = run_approval(
            conn, cur, job_id=_text(state.get("job_id")), ref_key=ref_key
        )
    if receiver == RECEIVER_CONNECTOR:
        # Exactly the receiver the step will dispatch to, selected before this read,
        # approved for the job's own project.
        identity = {**dict(receiver_identity or {}), "project_id": project_id}
        if not any(
            {key: row.get(key) for key in identity} == identity
            for row in approved_receivers(approval)
        ):
            raise SecretConfigurationUnapproved()
        return
    # An HTTP read with no validated URL yet has no destination to approve.
    if url is None or not url_allowed(url, approved_rules(approval)):
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
        receiver: str,
        url_provider: Any = None,
        receiver_identity: dict[str, str] | None = None,
        computed_origin_refs: Any = None,
    ) -> None:
        self._reader = reader
        self._conn_provider = conn_provider
        self._state = runtime_state
        self._receiver = receiver
        self._url_provider = url_provider
        self._receiver_identity = receiver_identity
        self._computed_origin_refs = computed_origin_refs

    def read(self, context: Any, ref_key: str) -> str:
        relaxed = frozenset(self._computed_origin_refs() if self._computed_origin_refs else ())
        if relaxed and ref_key not in relaxed:
            # A computed origin was let through for exactly these keys.
            raise SecretDestinationNotAllowed()
        require_catalog_ref_authority(
            self._conn_provider(),
            self._state,
            ref_key=ref_key,
            receiver=self._receiver,
            url=self._url_provider() if self._url_provider is not None else None,
            receiver_identity=self._receiver_identity,
            require_catalog=bool(relaxed),
        )
        return self._reader.read(context, ref_key)


__all__ = [
    "RECEIVER_CONNECTOR",
    "RECEIVER_HTTP",
    "RESERVED_INPUT_KEYS",
    "CatalogAuthorityReader",
    "CatalogSecretReceiverUnpinned",
    "SecretDestinationNotAllowed",
    "approved_receivers",
    "approved_rules",
    "catalog_bound_interface",
    "catalog_only_refs",
    "connector_step_ids",
    "connector_steps",
    "current_definition",
    "current_projection",
    "http_destinations",
    "normalize_rule",
    "overlay_storage_keys",
    "overlay_workflow_row",
    "require_catalog_ref_authority",
    "reserved_binding_keys",
    "run_approval",
    "url_allowed",
    "workflow_bindings",
    "workflow_fingerprint",
    "workflow_projection",
    "workflow_row",
]
