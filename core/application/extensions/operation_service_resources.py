"""Resource cleanup and effect confirmation for direct extension actions."""

from __future__ import annotations

import copy
from typing import Any


def cleanup_action_resources(
    service: Any,
    context: Any,
    invocation: dict[str, Any],
    *,
    confirm_external_effect: bool,
    protected_values: frozenset[str],
    finalized_grants: Any,
    client_action_output_keys: frozenset[str],
    target_connection_id: Any,
    logger: Any,
) -> None:
    if (
        service._artifact_plane is not None
        and context.prepared_grants is not None
        and invocation.get("status") != "succeeded"
    ):
        service._cleanup_prepared_action_grants(
            context,
            log_message="failed to cleanup prepared extension output grants: %s",
        )
        invocation.pop("produced_artifacts", None)
        invocation.pop("produced_datasets", None)
        invocation.pop("grant_outputs", None)

    if not confirm_external_effect or not context.external_effect_id:
        if invocation.get("status") != "succeeded" and context.prepared_grants is not None:
            from .artifact_presentation import strip_unaccepted_presentation_claims

            strip_unaccepted_presentation_claims(context.prepared_grants, invocation)
        return
    confirmed_success = False
    try:
        from core.application.extensions.external_effects import (
            apply_external_effect_confirmation,
            confirm_external_effect_from_invocation,
            public_confirmed_external_effect,
        )

        prepared_grants = context.prepared_grants
        has_host_finalized_outputs = bool(
            invocation.get("status") == "succeeded"
            and service._artifact_plane is not None
            and prepared_grants is not None
            and (prepared_grants.artifact_output_ids or prepared_grants.dataset_output_ids)
        )
        host_outputs = (
            {key: copy.deepcopy(invocation.get(key)) for key in client_action_output_keys}
            if has_host_finalized_outputs
            else {}
        )
        confirmed_effect = confirm_external_effect_from_invocation(
            context.bl,
            effect_id=context.external_effect_id,
            invocation=invocation,
            claim_token=context.external_effect_claim_token,
            account_id=str(context.audit.get("account_id") or "").strip(),
            extension=context.extension,
            operation_id=context.action_id,
            protected_values=protected_values,
            host_outputs=host_outputs,
            expected_project_id=str(context.audit.get("project_id") or "").strip() or None,
            expected_connection_id=str(
                context.audit.get("connection_id") or target_connection_id(context.target) or ""
            ).strip()
            or None,
        )
        if confirmed_effect:
            checked_invocation = apply_external_effect_confirmation(
                invocation,
                confirmed_effect,
            )
            context.external_effect = public_confirmed_external_effect(confirmed_effect)
            if checked_invocation is not invocation:
                invocation.clear()
                invocation.update(checked_invocation)
            confirmed_success = (
                str(confirmed_effect.get("status") or "").strip().lower() == "succeeded"
                and invocation.get("status") == "succeeded"
            )
    except Exception as exc:
        logger.warning("failed to confirm external effect: %s", exc)
        if getattr(exc, "commit_outcome", "unknown") == "rolled_back":
            service._cleanup_prepared_action_grants(
                context,
                log_message=("failed to cleanup rollback-confirmed extension output grants: %s"),
            )
        invocation.pop("produced_artifacts", None)
        invocation.pop("produced_datasets", None)
        invocation.pop("grant_outputs", None)
        from core.application.extensions.external_effects import (
            external_effect_confirmation_failure_invocation,
        )

        invocation.clear()
        invocation.update(external_effect_confirmation_failure_invocation(exc))

    prepared_grants = context.prepared_grants
    if prepared_grants is not None and getattr(prepared_grants, "artifact_presentations", False):
        from .artifact_presentation import finalize_external_effect_presentations

        finalize_external_effect_presentations(
            service._artifact_plane,
            prepared_grants,
            finalized_grants,
            invocation,
            confirmed_success=confirmed_success,
        )
