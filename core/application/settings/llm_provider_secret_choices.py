"""Bind or unbind stored secrets for an LLM provider install's secret fields (ADR 0057).

Split from `llm_provider_extension_use_cases`; `save_credentials` calls it inside the
save's transaction.
"""

from __future__ import annotations

from typing import Any

from core.application.settings import llm_provider_catalog_refs as catalog_refs


def apply_secret_choices(
    conn: Any,
    scope: Any,
    *,
    account_id: str,
    install_id: str,
    existing_refs: dict[str, str],
    choices: dict[str, str],
    typed_names: set[str],
) -> dict[str, str]:
    """Bind or unbind stored secrets; return the install's new name -> storage key map.

    Runs in the save's transaction, so the bindings and the map change together.
    A typed value for a name stops that name's stored-secret use.
    """
    to_bind = {name: secret_id for name, secret_id in choices.items() if secret_id}
    to_unbind = {name for name, secret_id in choices.items() if not secret_id}
    to_unbind |= typed_names & set(existing_refs)
    refs = {name: key for name, key in existing_refs.items() if name not in to_unbind}
    if not to_bind and not to_unbind:
        return refs
    from core.application.management.errors import (
        ServiceForbiddenError,
        ServiceNotFoundError,
        ServiceValidationError,
    )
    from core.application.secrets import reference_consumers
    from core.application.secrets.authority import require_secret_bind
    from core.application.settings.llm_provider_extension_use_cases import (
        LlmProviderExtensionError,
    )

    kind = catalog_refs.CONSUMER_KIND
    try:
        with conn.cursor() as cur:
            bound = reference_consumers.bound_fields(
                cur, account_id=account_id, kind=kind, consumer_id=install_id
            )
            changes = [
                name
                for name, secret_id in to_bind.items()
                if bound.get(name) != secret_id or name not in existing_refs
            ] + [name for name in to_unbind if name in bound or name in existing_refs]
            if not changes:
                return {**refs, **{name: existing_refs[name] for name in to_bind}}
            if scope is None or str(getattr(scope, "account_id", "") or "") != account_id:
                raise LlmProviderExtensionError(
                    403, "Choosing a stored secret requires a signed-in administrator"
                )
            require_secret_bind(scope)
            for name, secret_id in sorted(to_bind.items()):
                if bound.get(name) == secret_id and name in existing_refs:
                    refs[name] = existing_refs[name]
                    continue
                refs[name] = reference_consumers.bind(
                    cur,
                    scope,
                    kind=kind,
                    consumer_id=install_id,
                    field=name,
                    secret_id=secret_id,
                )
            for name in sorted(to_unbind):
                if reference_consumers.unbind(
                    cur, account_id=account_id, kind=kind, consumer_id=install_id, field=name
                ):
                    catalog_refs.record_unbind_audit(cur, scope, install_id=install_id, field=name)
    except ServiceForbiddenError as exc:
        raise LlmProviderExtensionError(403, str(exc)) from exc
    except ServiceNotFoundError as exc:
        raise LlmProviderExtensionError(404, str(exc)) from exc
    except ServiceValidationError as exc:
        raise LlmProviderExtensionError(422, str(exc)) from exc
    return refs
