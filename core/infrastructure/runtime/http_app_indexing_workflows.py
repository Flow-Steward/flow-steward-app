# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .http_app_runtime_helpers import *  # noqa: F403,F401  # NOSONAR


_serialize_projects_grid_row = _serialize_projects_grid_row_impl


_AGENT_MODEL_OVERRIDE_TARGETS = list(
    _serve_workflow_helpers_runtime.agent_model_override_targets(DEFAULT_MODEL_GPT_4_1_NANO)
)


def _model_catalog_conn() -> Any | None:
    try:
        bl = _get_connection_store()
    except Exception:
        return None
    return getattr(bl, "conn", None)


def _request_account_id(request: Any | None = None) -> str:
    auth_user = getattr(getattr(request, "state", None), "auth_user", None)
    if isinstance(auth_user, dict):
        account_id = str(auth_user.get("account_id") or "").strip()
        if account_id:
            return account_id
        for candidate in auth_user.get("account_ids") or []:
            normalized = str(candidate or "").strip()
            if normalized:
                return normalized
    return DEFAULT_ACCOUNT_ID


def _request_model_selection_context(request: Any | None = None) -> dict[str, Any]:
    return _resolve_model_selection_context(
        DEFAULT_MODEL_GPT_4_1_NANO,
        conn=_model_catalog_conn(),
        account_id=_request_account_id(request),
    )


# Canonical per-agent defaults and dropdown values shared across UI/admin endpoints.
def _parse_agent_model_overrides(form_data: dict) -> dict:
    return _parse_agent_model_overrides_impl(form_data, _AGENT_MODEL_OVERRIDE_TARGETS)


def _list_workflow_choices(
    enabled_only: bool = True,
    account_id: str | None = None,
) -> list[dict]:
    return _serve_workflow_helpers_runtime.list_workflow_choices(
        enabled_only=enabled_only,
        logger=logger,
        account_id=account_id,
    )


_form_flag = _form_flag_impl


_form_int = _form_int_impl


_form_float = _form_float_impl


_normalize_cron_expr = _normalize_cron_expr_impl


_parse_cron_expression = _parse_cron_expression_impl


_cron_human_summary = _cron_human_summary_impl


_cron_next_run_at = _cron_next_run_at_impl


_build_project_policy_from_form = _build_project_policy_from_form_impl


_parse_project_policy_enum = _parse_project_policy_enum_impl


_resolve_workflow_selection = _serve_workflow_helpers_runtime.build_resolve_workflow_selection(
    list_workflow_choices=lambda: _list_workflow_choices(enabled_only=False),
    logger=logger,
)


_validate_explicit_workflow_override = (
    _workflow_override_runtime.build_explicit_workflow_override_validator(
        build_task_envelope_for_preview=lambda task_summary: _build_task_envelope_for_preview(
            task_summary
        ),
    )
)


def _build_rerun_workflow_selection(
    job: dict,
    *,
    replay_snapshot: dict,
) -> tuple[dict, dict]:
    from core.infrastructure.runtime.test_mode import parse_test_mode

    return _build_rerun_workflow_selection_impl(
        job,
        replay_snapshot=replay_snapshot,
        to_json_obj=_to_json_obj,
        parse_test_mode=parse_test_mode,
    )


def _build_task_envelope_for_preview(task_summary: str) -> dict:
    return _build_task_envelope_for_preview_impl(task_summary)


def _plan_project_workflow_preview(
    envelope: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    return _plan_project_workflow_preview_impl(envelope, state)


# Project/provider + notification routes are now mounted from
# core.entrypoints.api.project_integrations.create_project_integrations_router().

__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
