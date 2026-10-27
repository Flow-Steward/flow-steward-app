from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.application.auth.config import DEFAULT_ACCOUNT_ID
from core.application.management.project_policy_validation import project_policy_numbers_from_form
from core.application.settings.timezone_service import resolve_effective_project_tz
from core.application.workflows.workflow_hook_contract import is_hook_storage

_MESSAGE_THIS_JOB_REQUIRES_A_SECRET_INPUT_PROVIDE_IT_AGAIN_IN_A_NEW_LAUNCH = (
    "This job requires a secret input; provide it again in a new launch."
)


def _unconfigured_agent_model() -> dict[str, str]:
    """Represent an agent whose model must be selected explicitly.

    This is intentionally not a provider/model fallback.  The runtime turns an
    unconfigured route into ``model_unavailable`` rather than silently selecting
    an unrelated global or first-catalog model.
    """
    return {"provider": "", "model": ""}


def default_agent_models(
    default_model_gpt_4_1_nano: str,
    *,
    registry_manager_factory: Callable[[], Any] | None = None,
) -> dict[str, dict[str, str]]:
    del default_model_gpt_4_1_nano
    try:
        if registry_manager_factory is None:
            from core.application.registry.registry_manager import RegistryManager

            registry_manager_factory = RegistryManager.instance

        registry = registry_manager_factory()
        manifests = list(registry.agents.list_all() or [])
    except Exception:
        manifests = []

    defaults: dict[str, dict[str, str]] = {}
    for manifest in manifests:
        agent_id = str(getattr(manifest, "agent_id", "") or "").strip()
        if not agent_id or not bool(getattr(manifest, "enabled", True)):
            continue
        defaults[agent_id] = _unconfigured_agent_model()
    return defaults


def available_providers() -> list[tuple[str, str]]:
    return []


def available_models(default_model_gpt_4_1_nano: str) -> list[str]:
    del default_model_gpt_4_1_nano
    return []


def resolve_model_selection_context(
    default_model_gpt_4_1_nano: str,
    *,
    conn: Any = None,
    account_id: str = "acc_default",
) -> dict[str, Any]:
    payload = {
        "default_agent_models": default_agent_models(default_model_gpt_4_1_nano),
        "available_providers": [],
        "available_models": [],
        "providers_with_models": {"provider_options": [], "provider_models": {}, "all_models": []},
    }
    from core.application.settings.model_catalog import ModelCatalogUnavailableError

    try:
        from core.application.settings import model_catalog as model_catalog_runtime
    except Exception:
        return payload

    resolver = getattr(model_catalog_runtime, "resolve_model_selection_context", None)
    if not callable(resolver):
        resolver = getattr(model_catalog_runtime, "resolve_effective_model_selection", None)
    if not callable(resolver):
        resolver = getattr(model_catalog_runtime, "build_model_selection_context", None)
    if not callable(resolver):
        return payload

    try:
        resolved = resolver(
            default_model_gpt_4_1_nano=default_model_gpt_4_1_nano,
            conn=conn,
            account_id=account_id,
        )
    except TypeError:
        try:
            resolved = resolver(default_model_gpt_4_1_nano, conn=conn, account_id=account_id)
        except ModelCatalogUnavailableError:
            raise
        except Exception:
            return payload
    except ModelCatalogUnavailableError:
        raise
    except Exception:
        return payload

    if not isinstance(resolved, dict):
        return payload

    for key in tuple(payload):
        if key in resolved:
            payload[key] = resolved[key]
    return payload


def agent_model_override_targets(
    default_model_gpt_4_1_nano: str,
    *,
    conn: Any = None,
    account_id: str = "acc_default",
) -> list[str]:
    """Return enabled registry agent ids without loading account model options."""
    del conn, account_id
    defaults = default_agent_models(default_model_gpt_4_1_nano)
    if not isinstance(defaults, dict):
        return []
    return [str(agent_id).strip() for agent_id in defaults if str(agent_id).strip()]


def parse_agent_model_overrides(form_data: dict, agent_names: list[str]) -> dict:
    """Extract per-agent provider/model overrides from form data."""
    agent_cfg: dict[str, dict[str, str]] = {}
    for agent in agent_names:
        provider = (form_data.get(f"agent_provider_{agent}") or "").strip()
        model = (form_data.get(f"agent_model_{agent}") or "").strip()
        if provider or model:
            entry: dict[str, str] = {}
            if provider:
                entry["provider"] = provider
            if model:
                entry["model"] = model
            agent_cfg[agent] = entry
    return agent_cfg


def list_workflow_choices(
    enabled_only: bool = True,
    *,
    logger,
    account_id: str | None = None,
    registry_manager_factory=None,
) -> list[dict]:
    """Load workflow choices from the registry store on a best-effort basis."""
    normalized_account_id = str(account_id or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    if normalized_account_id != DEFAULT_ACCOUNT_ID:
        return []
    try:
        if registry_manager_factory is None:
            from core.application.registry.registry_manager import RegistryManager

            registry_manager_factory = RegistryManager.instance

        registry_manager = registry_manager_factory()
        db_store = getattr(registry_manager, "db_store", None)
        if not db_store:
            return []
        rows = list(
            db_store.list_workflows(
                enabled_only=enabled_only,
                include_definitions=False,
            )
            or []
        )
        from core.application.registry.projected_artifact_rows import (
            projection_aware_catalog_rows,
        )

        rows = projection_aware_catalog_rows(
            db_store,
            "workflow",
            "workflow_id",
            rows,
            account_id=normalized_account_id,
            enabled_only=enabled_only,
        )
        filtered: list[dict] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if "visible_in_catalog" in row and not bool(row.get("visible_in_catalog")):
                continue
            # A hook's storage workflow is a place inside another workflow (R2-11).
            if is_hook_storage(row):
                continue
            if (
                "visible_in_catalog" not in row
                and str(row.get("availability_status") or "available").strip().lower() == "removed"
            ):
                continue
            filtered.append(dict(row))
        return filtered
    except Exception as exc:
        logger.debug("Could not load workflow choices: %s", exc)
        return []


def form_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def form_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def form_float(
    value: Any,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


_CRON_MONTH_ALIASES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_CRON_DOW_ALIASES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


_CRON_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def normalize_cron_expr(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _cron_parse_value(
    token: str,
    *,
    minimum: int,
    maximum: int,
    aliases: dict[str, int] | None = None,
    day_of_week: bool = False,
) -> int:
    text = str(token or "").strip().lower()
    if aliases and text in aliases:
        return int(aliases[text])
    try:
        parsed = int(text)
    except Exception as e:
        raise ValueError(f"Invalid cron value '{token}'") from e
    if day_of_week and parsed == 7:
        parsed = 0
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Cron value '{token}' is out of range [{minimum}, {maximum}]")
    return parsed


def _cron_parse_field(
    field: str,
    *,
    minimum: int,
    maximum: int,
    aliases: dict[str, int] | None = None,
    day_of_week: bool = False,
) -> tuple[set[int], bool]:
    normalized = str(field or "").strip().lower()
    if not normalized:
        raise ValueError("Cron field cannot be empty")

    all_values = set(range(minimum, maximum + 1))
    values: set[int] = set()
    wildcard = False
    for item in normalized.split(","):
        token = item.strip()
        if not token:
            raise ValueError("Cron list contains an empty token")
        if token in {"*", "?"}:
            values.update(all_values)
            wildcard = True
            continue

        if "/" in token:
            base, step_text = token.split("/", 1)
            try:
                step = int(step_text)
            except Exception as e:
                raise ValueError(f"Invalid cron step '{token}'") from e
            if step <= 0:
                raise ValueError(f"Cron step must be > 0 in '{token}'")
        else:
            base = token
            step = 1

        base = base.strip()
        if base in {"*", "?"} or not base:
            start, end = minimum, maximum
            wildcard = True
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _cron_parse_value(
                start_text,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            end = _cron_parse_value(
                end_text,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            if start > end:
                raise ValueError(f"Cron range start must be <= end in '{token}'")
        else:
            parsed = _cron_parse_value(
                base,
                minimum=minimum,
                maximum=maximum,
                aliases=aliases,
                day_of_week=day_of_week,
            )
            start, end = parsed, parsed

        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"Cron field '{field}' resolved to no values")
    return values, wildcard


def parse_cron_expression(cron_expr: str) -> dict:
    normalized = normalize_cron_expr(cron_expr)
    parts = normalized.split(" ")
    if len(parts) != 5:
        raise ValueError(
            "Cron expression must have exactly 5 fields: minute hour day-of-month month day-of-week"
        )

    minute_values, minute_any = _cron_parse_field(parts[0], minimum=0, maximum=59)
    hour_values, hour_any = _cron_parse_field(parts[1], minimum=0, maximum=23)
    dom_values, dom_any = _cron_parse_field(parts[2], minimum=1, maximum=31)
    month_values, month_any = _cron_parse_field(
        parts[3], minimum=1, maximum=12, aliases=_CRON_MONTH_ALIASES
    )
    dow_values, dow_any = _cron_parse_field(
        parts[4], minimum=0, maximum=6, aliases=_CRON_DOW_ALIASES, day_of_week=True
    )

    return {
        "expr": normalized,
        "minute": minute_values,
        "hour": hour_values,
        "dom": dom_values,
        "month": month_values,
        "dow": dow_values,
        "minute_any": minute_any,
        "hour_any": hour_any,
        "dom_any": dom_any,
        "month_any": month_any,
        "dow_any": dow_any,
    }


def cron_human_summary(cron_expr: str, schedule_tz: str = "UTC") -> str:
    parsed = parse_cron_expression(cron_expr)
    parts = normalize_cron_expr(cron_expr).split()

    minutes = sorted(parsed["minute"])
    hours = sorted(parsed["hour"])
    doms = sorted(parsed["dom"])
    dows = sorted(parsed["dow"])

    def _time_str(h: int, m: int) -> str:
        return f"{h:02d}:{m:02d}"

    has_time = len(minutes) == 1 and len(hours) == 1
    time_label = _time_str(hours[0], minutes[0]) if has_time else ""

    minute_step = parts[0].startswith("*/")
    hour_step = parts[1].startswith("*/")

    if (
        minute_step
        and parsed["hour_any"]
        and parsed["dom_any"]
        and parsed["dow_any"]
        and parsed["month_any"]
    ):
        step = parts[0].split("/")[1]
        return f"Every {step} minutes ({schedule_tz})"

    if hour_step and parsed["dom_any"] and parsed["dow_any"] and parsed["month_any"]:
        step = parts[1].split("/")[1]
        m = minutes[0] if len(minutes) == 1 else 0
        return f"Every {step} hours at :{m:02d} ({schedule_tz})"

    if (
        not minute_step
        and parsed["hour_any"]
        and parsed["dom_any"]
        and parsed["dow_any"]
        and parsed["month_any"]
    ):
        if len(minutes) == 1:
            return f"Every hour at :{minutes[0]:02d} ({schedule_tz})"
        return f"Every hour at minutes {', '.join(str(m) for m in minutes)} ({schedule_tz})"

    if has_time and parsed["dom_any"] and parsed["dow_any"] and parsed["month_any"]:
        return f"Daily at {time_label} ({schedule_tz})"

    if has_time and parsed["dom_any"] and not parsed["dow_any"] and parsed["month_any"]:
        day_names = [_CRON_DOW_NAMES[d] for d in dows]
        return f"Every {', '.join(day_names)} at {time_label} ({schedule_tz})"

    if has_time and not parsed["dom_any"] and parsed["dow_any"] and parsed["month_any"]:
        dom_strs = [f"{d}" for d in doms]
        day_suffixes = {1: "st", 2: "nd", 3: "rd"}
        suffix = day_suffixes.get(doms[0], "th")
        if len(doms) == 1:
            return f"Monthly on the {doms[0]}{suffix} at {time_label} ({schedule_tz})"
        return f"Monthly on days {', '.join(dom_strs)} at {time_label} ({schedule_tz})"

    return f"Cron: {cron_expr} ({schedule_tz})"


def _cron_matches_datetime(dt: datetime, parsed: dict) -> bool:
    if dt.minute not in parsed["minute"]:
        return False
    if dt.hour not in parsed["hour"]:
        return False
    if dt.month not in parsed["month"]:
        return False

    dom_match = dt.day in parsed["dom"]
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = cron_dow in parsed["dow"]
    dom_any = bool(parsed.get("dom_any"))
    dow_any = bool(parsed.get("dow_any"))
    if dom_any and dow_any:
        return True
    if dom_any:
        return dow_match
    if dow_any:
        return dom_match
    return dom_match or dow_match


def cron_next_run_at(
    cron_expr: str,
    reference_at: datetime | None = None,
    *,
    schedule_tz: str | None = None,
) -> datetime:
    from zoneinfo import ZoneInfo

    parsed = parse_cron_expression(cron_expr)
    tz_name = resolve_effective_project_tz(schedule_tz)
    tz = ZoneInfo(tz_name)

    base = reference_at or datetime.now(UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    base_local = base.astimezone(tz)
    candidate = (base_local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    max_minutes = 366 * 24 * 60
    for _ in range(max_minutes):
        candidate = candidate.replace(fold=0)
        if _cron_matches_datetime(candidate, parsed):
            return candidate.astimezone(UTC)
        candidate += timedelta(minutes=1)
    raise ValueError(f"Cron expression '{cron_expr}' has no next run in the next 366 days")


def parse_project_policy_enum(
    value: Any,
    *,
    field_name: str,
    allowed: set[str],
    default: str,
    aliases: dict[str, str] | None = None,
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if aliases:
        normalized = aliases.get(normalized, normalized)
    if normalized not in allowed:
        raise ValueError(
            f"Invalid {field_name}='{value}'. Allowed values: {', '.join(sorted(allowed))}"
        )
    return normalized


def build_project_policy_from_form(form_data: dict) -> dict:
    autonomy_mode = parse_project_policy_enum(
        form_data.get("policy_autonomy_mode"),
        field_name="policy_autonomy_mode",
        allowed={"manual", "supervised", "auto"},
        default="supervised",
        aliases={
            "assist": "supervised",
            "assisted": "supervised",
            "human": "manual",
            "human_only": "manual",
        },
    )
    retry_strategy = parse_project_policy_enum(
        form_data.get("policy_retry_strategy"),
        field_name="policy_retry_strategy",
        allowed={"bounded", "flat", "failure_class"},
        default="bounded",
    )
    tool_policy_mode = parse_project_policy_enum(
        form_data.get("policy_tool_policy_mode"),
        field_name="policy_tool_policy_mode",
        allowed={"registry_only", "restricted", "permissive"},
        default="registry_only",
        aliases={"allow_all": "permissive"},
    )

    return {
        "autonomy_mode": autonomy_mode,
        **project_policy_numbers_from_form(form_data),
        "retry_strategy": retry_strategy,
        "retry_max_attempts": form_int(form_data.get("policy_retry_max_attempts"), 2, minimum=0),
        "retry_backoff_initial_seconds": form_int(
            form_data.get("policy_retry_backoff_initial_seconds"),
            2,
            minimum=0,
        ),
        "retry_backoff_max_seconds": form_int(
            form_data.get("policy_retry_backoff_max_seconds"),
            120,
            minimum=0,
        ),
        "tool_policy_mode": tool_policy_mode,
    }


def resolve_workflow_selection(
    requested_workflow_id: str,
    project_workflows: list[dict] | None = None,
    *,
    list_workflow_choices: Callable[[], list[dict]],
    logger,
) -> tuple[str, str]:
    _ = logger
    requested_raw = (requested_workflow_id or "").strip()
    requested = str(requested_raw).strip()
    allowed_workflow_ids = {
        str(item.get("workflow_id") or "").strip()
        for item in (project_workflows or [])
        if isinstance(item, dict)
        and bool(item.get("enabled", True))
        and not item.get("deleted_at")
        and str(item.get("workflow_id") or "").strip()
    }

    if not requested:
        default_link = next(
            (
                str(item.get("workflow_id") or "").strip()
                for item in (project_workflows or [])
                if isinstance(item, dict)
                and bool(item.get("enabled", True))
                and not item.get("deleted_at")
                and bool(item.get("is_default"))
                and str(item.get("workflow_id") or "").strip()
            ),
            "",
        )
        if default_link:
            return default_link, "project_default"
        first_linked = next(iter(allowed_workflow_ids), "")
        if first_linked:
            return first_linked, "project_linked"
        raise ValueError("workflow_id is required")

    source = "explicit_request"
    selected = requested

    if allowed_workflow_ids and selected not in allowed_workflow_ids:
        raise ValueError(f"Workflow '{selected}' is not linked to this project")

    available = {(wf.get("workflow_id") or ""): wf for wf in list_workflow_choices()}
    if not available:
        return selected, source
    wf = available.get(selected)
    if not wf:
        raise ValueError(f"Workflow '{selected}' not found")
    if not bool(wf.get("enabled", True)):
        raise ValueError(f"Workflow '{selected}' is disabled")
    return selected, source


def build_resolve_workflow_selection(
    *,
    list_workflow_choices: Callable[[], list[dict]],
    logger,
):
    def _resolver(
        requested_workflow_id: str,
        project_workflows: list[dict] | None = None,
    ) -> tuple[str, str]:
        return resolve_workflow_selection(
            requested_workflow_id,
            project_workflows,
            list_workflow_choices=list_workflow_choices,
            logger=logger,
        )

    return _resolver


def workflow_exists(workflow_id: str) -> bool:
    if not workflow_id:
        return False
    try:
        from core.application.registry.registry_manager import RegistryManager

        registry = RegistryManager.instance()
        return bool(registry.workflows.get_workflow(workflow_id))
    except Exception:
        return False


class WorkflowRerunSecretInputRequired(ValueError):
    """A restart cannot replay a caller-supplied write-only secret."""


class WorkflowStepTestRunRetryUnsupported(WorkflowRerunSecretInputRequired):
    """A step test must be relaunched so its saved step is resolved afresh."""


def _materialize_rerun_secret_defaults(
    *,
    workflow_id: str,
    safe_inputs: dict,
) -> tuple[dict, list[str]]:
    from core.infrastructure.security.secret_reference import REDACTED_SECRET_REFERENCE_MARKER

    redacted_fields = {
        str(key): str(value.get("kind") or "")
        for key, value in safe_inputs.items()
        if isinstance(value, dict) and value.get("__type__") == REDACTED_SECRET_REFERENCE_MARKER
    }
    if not redacted_fields:
        return dict(safe_inputs), []
    if any(kind != "configured" for kind in redacted_fields.values()):
        raise WorkflowRerunSecretInputRequired(
            _MESSAGE_THIS_JOB_REQUIRES_A_SECRET_INPUT_PROVIDE_IT_AGAIN_IN_A_NEW_LAUNCH
        )

    from core.application.registry.workflow_contract_service import WorkflowInterfaceService
    from core.application.registry.workflow_secret_defaults import workflow_secret_launch_snapshot

    service = WorkflowInterfaceService()
    interface = service.interface_for_workflow_id(workflow_id)
    configured_secret_fields = {
        str(row.name)
        for row in interface.expected_inputs
        if row.type == "secret" and row.default_secret_ref
    }
    if not set(redacted_fields).issubset(configured_secret_fields):
        raise WorkflowRerunSecretInputRequired(
            _MESSAGE_THIS_JOB_REQUIRES_A_SECRET_INPUT_PROVIDE_IT_AGAIN_IN_A_NEW_LAUNCH
        )
    caller_inputs = {
        key: value for key, value in safe_inputs.items() if str(key) not in redacted_fields
    }
    validation = service.validate_inputs(interface, caller_inputs, channel="")
    if not validation.ok:
        raise WorkflowRerunSecretInputRequired(
            _MESSAGE_THIS_JOB_REQUIRES_A_SECRET_INPUT_PROVIDE_IT_AGAIN_IN_A_NEW_LAUNCH
        )
    materialized = dict(validation.inputs)
    return materialized, workflow_secret_launch_snapshot(interface, inputs=materialized)


def build_rerun_workflow_selection(
    job: dict,
    *,
    replay_snapshot: dict[str, Any],
    to_json_obj: Callable[[Any], dict],
    parse_test_mode: Callable[[Any, bool], bool],
) -> tuple[dict, dict]:
    del parse_test_mode
    from core.application.jobs.job_invocation_artifacts import (
        JobReplaySnapshotError,
        _normalize_replay_snapshot,
    )
    from core.application.workflows.workflow_start_runtime import build_invocation_snapshot

    if replay_snapshot is None:
        raise JobReplaySnapshotError("job.replay_snapshot_absent")
    meta = to_json_obj(job.get("meta")) if isinstance(job, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    if (
        str(job.get("task_source") or "").strip().lower() == "step_test_run"
        or meta.get("step_test_run") is True
    ):
        raise WorkflowStepTestRunRetryUnsupported(
            "Step test runs cannot be retried; start a new step test run so the selected "
            "saved step is resolved again."
        )
    snapshot = _normalize_replay_snapshot(replay_snapshot)
    requested = str(snapshot["workflow_id"]).strip()
    inputs = dict(snapshot["inputs"])
    files = [dict(item) for item in snapshot["files"]]
    extra_state: dict[str, Any] = {
        "workflow_id": requested,
        "workflow_selection_source": "explicit_request",
        "inputs": inputs,
        "files": files,
    }
    rerun_meta: dict[str, Any] = {
        "requested_workflow_id": requested,
        "selected_workflow_id": requested,
        "workflow_selection_source": "explicit_request",
    }
    if snapshot["test_mode"]:
        extra_state["test_mode"] = True
        rerun_meta["test_mode"] = True
    input_contract = str(snapshot["input_contract"])
    rerun_meta["input_contract"] = input_contract
    runtime_envelope = dict(snapshot["task_envelope"])
    runtime_envelope["inputs"] = inputs
    extra_state["task_envelope"] = runtime_envelope
    extra_state["_workflow_secret_ref_fingerprints"] = list(
        snapshot["_workflow_secret_ref_fingerprints"]
    )
    raw_job_id = job.get("id") if isinstance(job, dict) else None
    if raw_job_id is not None:
        rerun_meta["rerun_from_job_id"] = str(raw_job_id).strip() or raw_job_id
    prior_invocation = (
        to_json_obj(meta.get("invocation")) if isinstance(meta.get("invocation"), dict) else {}
    )
    if not isinstance(prior_invocation, dict):
        prior_invocation = {}
    for correlation_key in (
        "source_system",
        "source_event_id",
        "source_object_id",
        "input_contract",
        "task_envelope_id",
        "idempotency_key_digest",
    ):
        correlation_value = meta.get(correlation_key)
        if correlation_value not in (None, ""):
            rerun_meta[correlation_key] = correlation_value
    invocation_patch = dict(prior_invocation)
    invocation_patch.update(
        {
            "channel": "job_restart",
            "trigger_family": "restart",
            "entry_path": "job_restart",
            "origin_job_id": rerun_meta.get("rerun_from_job_id"),
            "request_context_summary": dict(prior_invocation.get("request_context_summary") or {})
            if isinstance(prior_invocation.get("request_context_summary"), dict)
            else {},
        }
    )
    rerun_meta["invocation"] = build_invocation_snapshot(
        task_source="job_restart",
        entry_path="job_restart",
        source_system=str(meta.get("source_system") or "restart"),
        selected_workflow_id=requested,
        workflow_selection_source="rerun",
        inputs=inputs,
        files=files,
        invocation_patch=invocation_patch,
    )
    return extra_state, rerun_meta


def build_task_envelope_for_preview(task_summary: str) -> dict:
    title = task_summary.strip()
    return {
        "envelope_id": "",
        "entry_path": "manual_ui",
        "source_system": "manual",
        "source_event_id": "",
        "source_object_id": "",
        "title": title,
        "description": title,
        "priority": "medium",
        "project_id": "",
        "requested_workflow_id": "",
        "inputs": {
            "title": title,
            "description": title,
            "priority": "medium",
        },
        "files": [],
        "metadata": {},
        "entry_context": {},
    }
