"""MCP connector headers that send a stored secret from the catalog (ADR 0057).

A person may choose a catalog secret for a request header instead of typing its
value, with an optional literal prefix (``Authorization`` = ``"Bearer "`` + secret).
The choice is a ``catalog_secret_bindings`` row of kind ``mcp_connector`` plus its
projection in the connector's ``header_secret_refs_jsonb`` (see
``mcp_connector_header_secrets``). Both are written in the transaction that writes
the connector row, after the row exists.

Choosing, changing or removing a choice needs bind authority
(``require_secret_bind``); the connector's own project access check stays as it is.
The value is never read here and never leaves the host.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.application.management.command_models import McpHeaderSecretChoice
from core.application.management.errors import ServiceValidationError
from core.application.management.mcp_connector_header_secrets import (
    HEADER_SECRET_REFS_FIELD,
    MCP_CONNECTOR_CONSUMER_KIND,
    catalog_ref_entry,
    connector_header_catalog_refs,
    connector_header_names,
    connector_header_typed_refs,
    delete_connector_header_secret_refs,
    header_secret_ref_prefix,
)
from core.application.management.scope import ScopeContext

#: An HTTP header field name (RFC 9110 token).
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,200}$")

ChoiceState = dict[str, tuple[str, str]]


def _invalid(message: str, *, field: str = "header_secret_choices") -> ServiceValidationError:
    return ServiceValidationError(
        message,
        payload={"error": "invalid_header_secret_choices", "field": field, "message": message},
    )


def normalize_header_choices(
    choices: Mapping[str, Any] | None,
) -> dict[str, McpHeaderSecretChoice]:
    """Validated ``{header_name: choice}``; names are unique without regard to case."""
    normalized: dict[str, McpHeaderSecretChoice] = {}
    seen: set[str] = set()
    for raw_name, raw_choice in (choices or {}).items():
        name = str(raw_name or "").strip()
        if not _HEADER_NAME.match(name):
            raise _invalid(f"'{raw_name}' is not a valid header name")
        if name.lower() in seen:
            raise _invalid(f"{name} is chosen twice")
        seen.add(name.lower())
        normalized[name] = McpHeaderSecretChoice.model_validate(
            raw_choice.model_dump() if isinstance(raw_choice, McpHeaderSecretChoice) else raw_choice
        )
    return normalized


def reject_typed_overlap(typed_names: Iterable[str], choices: Mapping[str, Any]) -> None:
    """A header either takes a typed value or a stored secret in one request, not both."""
    chosen = {str(name).strip().lower() for name in choices}
    for name in typed_names:
        if str(name or "").strip().lower() in chosen:
            raise _invalid(
                f"{name} has both a typed value and a stored secret; choose one",
                field=f"headers.{name}",
            )


def choice_state(choices: Mapping[str, McpHeaderSecretChoice]) -> ChoiceState:
    return {name: (choice.secret_id, choice.prefix) for name, choice in choices.items()}


def existing_choice_state(conn: Any, row: dict[str, Any]) -> ChoiceState:
    """``{header: (secret id, prefix)}`` the connector uses now; no query without choices."""
    chosen = connector_header_catalog_refs(row)
    if not chosen:
        return {}
    from core.application.secrets import reference_consumers

    with conn.cursor() as cur:
        secret_ids = reference_consumers.bound_fields(
            cur,
            account_id=str(row.get("account_id") or ""),
            kind=MCP_CONNECTOR_CONSUMER_KIND,
            consumer_id=str(row.get("connector_id") or ""),
        )
    return {name: (secret_ids.get(name, ""), prefix) for name, (_key, prefix) in chosen.items()}


def choices_view(state: ChoiceState) -> dict[str, dict[str, str]]:
    """What GET responses show: header names, the chosen secret id and the prefix."""
    return {
        name: {"secret_id": secret_id, "prefix": prefix}
        for name, (secret_id, prefix) in sorted(state.items())
    }


def apply_header_choices(
    conn: Any,
    scope: ScopeContext,
    *,
    connector_id: str,
    account_id: str,
    choices: Mapping[str, McpHeaderSecretChoice],
    existing_row: dict[str, Any] | None = None,
    existing_state: ChoiceState | None = None,
) -> dict[str, dict[str, str]]:
    """Bind the chosen headers, unbind the others; return their ``refs`` entries.

    Run after the connector row exists, in the transaction that writes it. A choice
    whose secret did not change keeps its storage key without a new binding.
    """
    previous_refs = connector_header_catalog_refs(existing_row or {})
    previous = dict(existing_state or {})
    if not choices and not previous_refs and not previous:
        return {}
    from core.application.secrets import reference_consumers
    from core.application.secrets.authority import require_secret_bind

    removed = sorted(name for name in set(previous) | set(previous_refs) if name not in choices)
    kept_keys = {
        name: previous_refs[name][0]
        for name, choice in choices.items()
        if name in previous_refs and previous.get(name, ("", ""))[0] == choice.secret_id
    }
    if choice_state(choices) != previous or removed:
        # Kept unchanged (a typed header replaced next to it) needs no bind authority.
        require_secret_bind(scope)
    owned_prefix = header_secret_ref_prefix(connector_id)
    entries: dict[str, dict[str, str]] = {}
    with conn.cursor() as cur:
        for name in removed:
            reference_consumers.unbind(
                cur,
                account_id=account_id,
                kind=MCP_CONNECTOR_CONSUMER_KIND,
                consumer_id=connector_id,
                field=name,
            )
        for name, choice in sorted(choices.items()):
            if name in kept_keys:
                storage_key = kept_keys[name]
            else:
                storage_key = reference_consumers.bind(
                    cur,
                    scope,
                    kind=MCP_CONNECTOR_CONSUMER_KIND,
                    consumer_id=connector_id,
                    field=name,
                    secret_id=choice.secret_id,
                )
            if not storage_key or storage_key.startswith(owned_prefix):
                # A key under the connector's own prefix would be deleted with its
                # typed values; a catalog secret never lives there.
                raise _invalid(f"The stored secret chosen for {name} cannot be used here")
            entries[name] = catalog_ref_entry(storage_key, choice.prefix)
    return entries


def create_choices(
    scope: ScopeContext,
    choices: Mapping[str, Any] | None,
    *,
    typed_headers: Mapping[str, Any] | None,
) -> dict[str, McpHeaderSecretChoice]:
    """The choices of a new connector, checked before anything is written."""
    normalized = normalize_header_choices(choices)
    reject_typed_overlap(typed_headers or {}, normalized)
    if normalized:
        from core.application.secrets.authority import require_secret_bind

        require_secret_bind(scope)
    return normalized


def bind_created_choices(
    conn: Any,
    scope: ScopeContext,
    row: dict[str, Any],
    choices: Mapping[str, McpHeaderSecretChoice],
) -> dict[str, Any]:
    """Bind a new connector's choices once its row exists; return the row's complete
    ref map: its typed values plus the chosen headers."""
    catalog_refs = apply_header_choices(
        conn,
        scope,
        connector_id=str(row.get("connector_id") or ""),
        account_id=str(row.get("account_id") or ""),
        choices=choices,
    )
    typed_refs = (row.get(HEADER_SECRET_REFS_FIELD) or {}).get("refs") or {}
    return {"refs": {**dict(typed_refs), **catalog_refs}}


@dataclass(frozen=True)
class HeaderChoicePlan:
    """What one connector update does with the headers that send a stored secret."""

    #: ``{header: (storage key, prefix)}`` in the row before the update.
    existing_refs: dict[str, tuple[str, str]]
    #: ``{header: (secret id, prefix)}`` the connector uses before the update.
    current: ChoiceState
    #: The complete set after the update.
    final: dict[str, McpHeaderSecretChoice]
    #: Whether the request named the choices (a mapping, not ``None``).
    requested: bool

    @property
    def changed(self) -> bool:
        return choice_state(self.final) != self.current


def plan_update_choices(
    conn: Any,
    scope: ScopeContext,
    existing: dict[str, Any],
    *,
    requested: Mapping[str, Any] | None,
    headers_mode: str,
    typed_headers: Mapping[str, Any] | None,
) -> HeaderChoicePlan:
    """``None`` keeps the choices (a typed header of the same name replaces one), a
    mapping is the complete new set, and ``headers_mode`` clear removes them all.
    Checks bind authority before anything is written when the choices change."""
    existing_refs = connector_header_catalog_refs(existing)
    current = existing_choice_state(conn, existing) if existing_refs else {}
    asked = normalize_header_choices(requested) if requested is not None else None
    if headers_mode == "clear":
        if asked:
            raise ServiceValidationError(
                "header_secret_choices must be omitted when headers_mode is clear",
                payload={
                    "error": "invalid_headers_mode_payload",
                    "field": "header_secret_choices",
                    "message": "header_secret_choices must be omitted when headers_mode is clear",
                },
            )
        final: dict[str, McpHeaderSecretChoice] = {}
    elif asked is not None:
        final = asked
        reject_typed_overlap(typed_headers or {}, final)
    else:
        overridden = {str(name).strip().lower() for name in typed_headers or {}}
        final = normalize_header_choices(
            {
                name: {"secret_id": secret_id, "prefix": prefix}
                for name, (secret_id, prefix) in current.items()
                if secret_id and name.lower() not in overridden
            }
        )
    plan = HeaderChoicePlan(
        existing_refs=existing_refs, current=current, final=final, requested=asked is not None
    )
    if plan.changed:
        from core.application.secrets.authority import require_secret_bind

        require_secret_bind(scope)
    return plan


def header_names_after(
    plan: HeaderChoicePlan, existing: dict[str, Any], typed_headers: Mapping[str, Any] | None
) -> dict[str, str]:
    """Every header name the connector sends after the update, for name-only checks."""
    typed = (
        dict(typed_headers)
        if typed_headers is not None
        else {
            name: "" for name in connector_header_names(existing) if name not in plan.existing_refs
        }
    )
    return {**typed, **dict.fromkeys(plan.final, "")}


def typed_only_row(plan: HeaderChoicePlan, existing: dict[str, Any]) -> dict[str, Any]:
    """The row with its typed values only, so comparing them never reads a chosen secret."""
    if not plan.existing_refs:
        return existing
    return {**existing, HEADER_SECRET_REFS_FIELD: {"refs": connector_header_typed_refs(existing)}}


def write_connector_header_refs(
    conn: Any,
    scope: ScopeContext,
    plan: HeaderChoicePlan,
    *,
    existing: dict[str, Any],
    lineage: tuple[str, str, str, str],
    typed_headers: Mapping[str, Any] | None,
    save_typed: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Write typed values and choices of one update; return the row's new ref map.

    ``typed_headers`` replaces every typed value through ``save_typed``; ``None``
    keeps them, except one now replaced by a stored secret, which is deleted.
    """
    connector_id, account_id, project_id, tenant_id = lineage
    if typed_headers is not None:
        typed_refs = dict(
            save_typed(
                conn,
                connector_id=connector_id,
                account_id=account_id,
                project_id=project_id,
                tenant_id=tenant_id,
                headers=dict(typed_headers),
            ).get("refs")
            or {}
        )
    else:
        chosen = {name.lower() for name in plan.final}
        kept = connector_header_typed_refs(existing)
        typed_refs = {name: ref for name, ref in kept.items() if name.lower() not in chosen}
        delete_connector_header_secret_refs(
            conn,
            connector_id=connector_id,
            account_id=account_id,
            project_id=project_id,
            tenant_id=tenant_id,
            ref_keys=[ref for name, ref in kept.items() if name not in typed_refs],
        )
    catalog_refs = apply_header_choices(
        conn,
        scope,
        connector_id=connector_id,
        account_id=account_id,
        choices=plan.final,
        existing_row=existing,
        existing_state=plan.current,
    )
    refs = {**typed_refs, **catalog_refs}
    return {"refs": refs} if refs else {}


__all__ = [
    "ChoiceState",
    "HeaderChoicePlan",
    "apply_header_choices",
    "bind_created_choices",
    "choice_state",
    "choices_view",
    "create_choices",
    "existing_choice_state",
    "header_names_after",
    "normalize_header_choices",
    "plan_update_choices",
    "reject_typed_overlap",
    "typed_only_row",
    "write_connector_header_refs",
]
