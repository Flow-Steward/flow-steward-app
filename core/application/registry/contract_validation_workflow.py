# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from __future__ import annotations

import logging


from core.application.registry.contract_validation_step_values import (
    INLINE_HTTP_RESPONSE_MAX_BYTES as INLINE_HTTP_RESPONSE_MAX_BYTES,
    ARTIFACT_HTTP_RESPONSE_DEFAULT_BYTES as ARTIFACT_HTTP_RESPONSE_DEFAULT_BYTES,
    ARTIFACT_HTTP_RESPONSE_MAX_BYTES as ARTIFACT_HTTP_RESPONSE_MAX_BYTES,
    _http_auth_ref_uses_process_env as _http_auth_ref_uses_process_env,
    _http_response_mode as _http_response_mode,
    _http_max_response_bytes_error as _http_max_response_bytes_error,
    _http_url_has_template_in_scheme_or_authority as _http_url_has_template_in_scheme_or_authority,
    _http_auth_url_static_origin_error as _http_auth_url_static_origin_error,
    _validate_collect_step_config as _validate_collect_step_config,
)
from core.application.orchestration.dataset_cursor_runtime import MAX_DATASET_PAGE_SIZE
from core.application.orchestration.state_path_guards import (
    canonical_state_write_parts,
    is_reserved_state_target,
)

from .contract_validation_common import *  # noqa: F403,F401  # NOSONAR


logger = logging.getLogger(__name__)


def _iter_workflow_steps(workflow: Any) -> list[dict]:
    """Every step the workflow owns, root and grouped alike, in display order."""
    if not isinstance(workflow, dict):
        return []
    steps = workflow.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _iter_workflow_groups(workflow: Any) -> list[dict]:
    if not isinstance(workflow, dict):
        return []
    groups = workflow.get("groups")
    return (
        [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []
    )


def _workflow_is_expected_failure(workflow: Any) -> bool:
    if not isinstance(workflow, dict):
        return False
    settings = _to_object(workflow.get("settings"))
    traits = _to_object(settings.get("traits"))
    if bool(traits.get("expected_failure")):
        return True
    tags = workflow.get("tags")
    if isinstance(tags, str):
        try:
            parsed = json.loads(tags)
        except Exception:
            parsed = []
        tags = parsed
    return any(
        str(tag or "").strip().lower() in {"expected-failure", "expected_failure"}
        for tag in (tags or [])
    )


def _workflow_terminal_step_counts(steps: list[dict]) -> tuple[int, int]:
    success_count = 0
    fail_count = 0
    for step in steps:
        step_kind = _step_kind(step)
        if step_kind == STEP_KIND_STOP_SUCCESS:
            success_count += 1
        elif step_kind == STEP_KIND_STOP_FAIL:
            fail_count += 1
    return success_count, fail_count


def _workflow_participant_bindings(workflow: Any) -> dict[str, str]:
    if not isinstance(workflow, dict):
        return {}
    participants = workflow.get("participants")
    if not isinstance(participants, list):
        settings = _to_object(workflow.get("settings"))
        participants = settings.get("participants")
    if not isinstance(participants, list):
        return {}
    bindings: dict[str, str] = {}
    for item in participants:
        if not isinstance(item, dict):
            continue
        key = str(item.get("participant_key") or "").strip()
        agent_id = str(item.get("agent_id") or item.get("agent") or "").strip()
        if key and agent_id:
            bindings[key] = agent_id
    return bindings


def _step_kind(step: dict) -> str:
    if not isinstance(step, dict):
        return "agent"
    raw = step.get("step_kind") or step.get("executor_kind") or ""
    normalized = normalize_step_kind(raw, default="", preserve_unknown=True)
    return normalized or "agent"


def _validate_control_flow_step_config(
    *,
    step_kind: str,
    step: dict,
    step_path: str,
) -> list[dict]:
    errors: list[dict] = []
    override = _to_object(step.get("override"))
    if step_kind == STEP_KIND_IF:
        config = _to_object(override.get(STEP_KIND_IF))
        errors.extend(
            _if_condition_issues(
                config.get("condition"),
                f"{step_path}.override.if.condition",
            )
        )
    elif step_kind == STEP_KIND_SWITCH:
        config = _to_object(override.get(STEP_KIND_SWITCH))
        if not str(config.get("selector") or "").strip():
            errors.append(
                _issue(
                    code="workflow.step_switch_selector_missing",
                    message="switch step requires override.switch.selector",
                    path=f"{step_path}.override.switch.selector",
                )
            )
        cases = config.get("cases")
        if cases is not None:
            valid_cases = isinstance(cases, list) and all(isinstance(item, dict) for item in cases)
            if not valid_cases:
                errors.append(
                    _issue(
                        code="workflow.step_switch_cases_invalid",
                        message=(
                            "switch step override.switch.cases must be a list of case objects, "
                            "each with a stable case_id and a when value; targets are graph edges"
                        ),
                        path=f"{step_path}.override.switch.cases",
                    )
                )
    elif step_kind in {STEP_KIND_STOP_SUCCESS, STEP_KIND_STOP_FAIL}:
        # Optional message/reason only.
        pass
    elif step_kind == STEP_KIND_SET_VARIABLE:
        config = _to_object(override.get(STEP_KIND_SET_VARIABLE))
        assignments = config.get("assignments")
        target = str(config.get("target") or "").strip()
        if not target and not isinstance(assignments, dict):
            errors.append(
                _issue(
                    code="workflow.step_set_variable_target_missing",
                    message="set_variable requires override.set_variable.target or assignments",
                    path=f"{step_path}.override.set_variable",
                )
            )
        if target and is_reserved_state_target(target):
            errors.append(
                _issue(
                    code="workflow.step_set_variable_target_reserved",
                    message="set_variable cannot write security-sensitive runtime state",
                    path=f"{step_path}.override.set_variable.target",
                )
            )
        if isinstance(assignments, dict):
            for assignment_target in assignments:
                if is_reserved_state_target(assignment_target):
                    errors.append(
                        _issue(
                            code="workflow.step_set_variable_target_reserved",
                            message="set_variable cannot write security-sensitive runtime state",
                            path=f"{step_path}.override.set_variable.assignments.{assignment_target}",
                        )
                    )
    elif step_kind == STEP_KIND_COLLECT:
        errors.extend(
            _validate_collect_step_config(_to_object(override.get(STEP_KIND_COLLECT)), step_path)
        )
    elif step_kind == STEP_KIND_BATCH:
        config = _to_object(override.get(STEP_KIND_BATCH))
        if not str(config.get("source_path") or "").strip():
            errors.append(
                _issue(
                    code="workflow.step_batch_source_path_missing",
                    message="batch requires override.batch.source_path",
                    path=f"{step_path}.override.batch.source_path",
                )
            )
        if not str(config.get("result_target") or "").strip():
            errors.append(
                _issue(
                    code="workflow.step_batch_result_target_missing",
                    message="batch requires override.batch.result_target",
                    path=f"{step_path}.override.batch.result_target",
                )
            )
        batch_size = config.get("batch_size")
        normalized_batch_size: int | None = None
        try:
            if batch_size in (None, ""):
                raise ValueError
            if isinstance(batch_size, bool):
                raise ValueError
            normalized_batch_size = int(str(batch_size))
            if normalized_batch_size <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(
                _issue(
                    code="workflow.step_batch_size_invalid",
                    message="batch requires a positive integer override.batch.batch_size",
                    path=f"{step_path}.override.batch.batch_size",
                )
            )
        if normalized_batch_size is not None and normalized_batch_size > MAX_DATASET_PAGE_SIZE:
            errors.append(
                _issue(
                    code="workflow.step_batch_size_exceeds_page_limit",
                    message=(
                        "batch_size must not exceed the bounded dataset page limit "
                        f"of {MAX_DATASET_PAGE_SIZE}"
                    ),
                    path=f"{step_path}.override.batch.batch_size",
                )
            )
        emit_partial = config.get("emit_partial_batch")
        if emit_partial not in (True, False, None):
            errors.append(
                _issue(
                    code="workflow.step_batch_emit_partial_invalid",
                    message="batch override.batch.emit_partial_batch must be boolean when provided",
                    path=f"{step_path}.override.batch.emit_partial_batch",
                )
            )
        if isinstance(config.get("steps"), list) and config.get("steps"):
            errors.append(
                _issue(
                    code="workflow.step_batch_steps_unsupported",
                    message=(
                        "batch does not execute nested steps. Put executable work in "
                        "ordinary workflow steps, or in a For each body."
                    ),
                    path=f"{step_path}.override.batch.steps",
                )
            )
    elif step_kind == STEP_KIND_HTTP_REQUEST:
        config = _to_object(override.get(STEP_KIND_HTTP_REQUEST))
        response_mode = _http_response_mode(config)
        if not response_mode:
            errors.append(
                _issue(
                    code="workflow.step_http_request_response_mode_invalid",
                    message="http_request override.http_request.response_mode must be inline or artifact",
                    path=f"{step_path}.override.http_request.response_mode",
                )
            )
            response_mode = "inline"
        max_response_error = _http_max_response_bytes_error(config, mode=response_mode)
        if max_response_error:
            errors.append(
                _issue(
                    code="workflow.step_http_request_max_response_bytes_invalid",
                    message=max_response_error,
                    path=f"{step_path}.override.http_request.max_response_bytes",
                )
            )
        if not _is_valid_http_method_config(config.get("method")):
            errors.append(
                _issue(
                    code="workflow.step_http_request_method_invalid",
                    message="http_request requires a valid HTTP method in override.http_request.method",
                    path=f"{step_path}.override.http_request.method",
                )
            )
        if not str(config.get("url") or "").strip():
            errors.append(
                _issue(
                    code="workflow.step_http_request_url_missing",
                    message="http_request requires override.http_request.url",
                    path=f"{step_path}.override.http_request.url",
                )
            )
        if not str(config.get("response_target") or "").strip():
            errors.append(
                _issue(
                    code="workflow.step_http_request_response_target_missing",
                    message="http_request requires override.http_request.response_target",
                    path=f"{step_path}.override.http_request.response_target",
                )
            )
        auth_mode = str(config.get("auth_mode") or "none").strip().lower()
        if auth_mode not in {"none", "bearer", "basic"}:
            errors.append(
                _issue(
                    code="workflow.step_http_request_auth_mode_invalid",
                    message="http_request override.http_request.auth_mode must be one of none|bearer|basic",
                    path=f"{step_path}.override.http_request.auth_mode",
                )
            )
        auth_ref = str(config.get("auth_ref") or "").strip()
        if auth_mode != "none" and not auth_ref:
            errors.append(
                _issue(
                    code="workflow.step_http_request_auth_ref_missing",
                    message="http_request requires override.http_request.auth_ref for non-none auth_mode",
                    path=f"{step_path}.override.http_request.auth_ref",
                )
            )
        if auth_mode != "none":
            static_origin_error = _http_auth_url_static_origin_error(str(config.get("url") or ""))
            if static_origin_error:
                errors.append(
                    _issue(
                        code="workflow.step_http_request_auth_url_origin_invalid",
                        message=static_origin_error,
                        path=f"{step_path}.override.http_request.url",
                    )
                )
        if auth_mode != "none" and _http_auth_ref_uses_process_env(auth_ref, auth_mode=auth_mode):
            errors.append(
                _issue(
                    code="workflow.step_http_request_auth_ref_env_forbidden",
                    message="http_request auth_ref must reference project secrets, not process environment variables",
                    path=f"{step_path}.override.http_request.auth_ref",
                )
            )
        timeout_seconds = config.get("timeout_seconds")
        try:
            if timeout_seconds in (None, ""):
                raise ValueError
            if isinstance(timeout_seconds, bool):
                raise ValueError
            if float(str(timeout_seconds)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(
                _issue(
                    code="workflow.step_http_request_timeout_invalid",
                    message="http_request requires positive override.http_request.timeout_seconds",
                    path=f"{step_path}.override.http_request.timeout_seconds",
                )
            )
        expected_statuses = config.get("expected_statuses")
        if (
            not isinstance(expected_statuses, list)
            or not expected_statuses
            or not all(isinstance(code, int) and 100 <= code <= 599 for code in expected_statuses)
        ):
            errors.append(
                _issue(
                    code="workflow.step_http_request_expected_statuses_invalid",
                    message="http_request requires override.http_request.expected_statuses as non-empty list of HTTP codes",
                    path=f"{step_path}.override.http_request.expected_statuses",
                )
            )
    elif step_kind == STEP_KIND_TRANSFORM:
        from core.application.orchestration.transform_step_runtime import validate_transform_config

        config = _to_object(override.get(STEP_KIND_TRANSFORM))
        errors.extend(
            validate_transform_config(
                config,
                path_prefix=f"{step_path}.override.transform",
            )
        )
    elif step_kind == STEP_KIND_TABULAR_FEED:
        from core.application.orchestration.tabular_feed_step_runtime import (
            validate_tabular_feed_config,
        )

        config = _to_object(override.get(STEP_KIND_TABULAR_FEED))
        errors.extend(
            validate_tabular_feed_config(
                config,
                path_prefix=f"{step_path}.override.tabular_feed",
            )
        )
    elif step_kind == STEP_KIND_FILE_FROM_BASE64:
        from core.application.orchestration.file_from_base64_step_runtime import (
            validate_file_from_base64_config,
        )

        errors.extend(
            validate_file_from_base64_config(
                _to_object(override.get(STEP_KIND_FILE_FROM_BASE64)),
                path_prefix=f"{step_path}.override.{STEP_KIND_FILE_FROM_BASE64}",
            )
        )
    elif step_kind == STEP_KIND_TEMPLATE:
        from core.application.orchestration.template_step_runtime import (
            validate_template_config,
        )

        errors.extend(
            validate_template_config(
                _to_object(override.get(STEP_KIND_TEMPLATE)),
                path_prefix=f"{step_path}.override.{STEP_KIND_TEMPLATE}",
            )
        )
    return errors


def _validate_step_state_write_targets(step: dict, step_path: str) -> list[dict]:
    """Reject every step-owned write target that points at host runtime state."""
    errors: list[dict] = []
    override = _to_object(step.get("override"))
    mapping_candidates = [
        (
            step.get("output_mapping_jsonb") or step.get("output_mapping"),
            f"{step_path}.output_mapping_jsonb",
        )
    ]
    for owner in ("agent", "connector"):
        owner_config = _to_object(override.get(owner))
        mapping_candidates.append(
            (
                owner_config.get("output_mapping_jsonb") or owner_config.get("output_mapping"),
                f"{step_path}.override.{owner}.output_mapping",
            )
        )

    seen: set[tuple[str, str]] = set()
    for raw_mapping, mapping_path in mapping_candidates:
        if not isinstance(raw_mapping, dict):
            continue
        for output_name, target in raw_mapping.items():
            target_text = str(target or "").strip()
            if not is_reserved_state_target(target_text):
                continue
            issue_key = (str(output_name), target_text)
            if issue_key in seen:
                continue
            seen.add(issue_key)
            errors.append(
                _issue(
                    code="workflow.step_output_mapping_target_reserved",
                    message="output mapping cannot write security-sensitive runtime state",
                    path=f"{mapping_path}.{str(output_name or '').strip()}",
                )
            )

    step_kind = str(step.get("step_kind") or "").strip().lower()
    target_fields = {
        STEP_KIND_BATCH: ("result_target",),
        STEP_KIND_DATASET_QUERY: ("result_target",),
        STEP_KIND_COLLECT: ("target",),
        STEP_KIND_HTTP_REQUEST: ("response_target",),
        STEP_KIND_TABULAR_FEED: ("result_target",),
        STEP_KIND_TRANSFORM: ("result_target",),
        STEP_KIND_FILE_FROM_BASE64: ("result_target",),
        STEP_KIND_TEMPLATE: ("result_target",),
    }.get(step_kind, ())
    config = _to_object(override.get(step_kind))
    for field in target_fields:
        target = config.get(field) if field in config else step.get(field)
        if is_reserved_state_target(target):
            errors.append(
                _issue(
                    code="workflow.step_state_target_reserved",
                    message=f"{step_kind} cannot write security-sensitive runtime state",
                    path=f"{step_path}.override.{step_kind}.{field}",
                )
            )
    return errors


def _resolve_step_agent_for_workflow(
    *,
    step: dict,
    participant_bindings: dict[str, str],
) -> tuple[str, str]:
    participant_key = str((step or {}).get("participant_key") or "").strip()
    agent_id = str((step or {}).get("agent_id") or (step or {}).get("agent") or "").strip()
    if participant_key:
        bound_agent_id = str((participant_bindings or {}).get(participant_key) or "").strip()
        if bound_agent_id:
            if agent_id and agent_id != bound_agent_id:
                return "", participant_key
            agent_id = bound_agent_id
    return agent_id, participant_key


def _workflow_invoke_children(db, workflow_id: str, cache: dict[str, set[str]]) -> set[str]:
    wf_id = str(workflow_id or "").strip()
    if not wf_id:
        return set()
    cached = cache.get(wf_id)
    if isinstance(cached, set):
        return set(cached)

    from .workflow_lookup import get_workflow_row

    workflow = get_workflow_row(db, wf_id)
    children: set[str] = set()
    for step in _iter_workflow_steps(workflow):
        spec = _extract_invoke_workflow_spec(step)
        if not isinstance(spec, dict):
            continue
        child_id = str(spec.get("workflow_id") or "").strip()
        if child_id:
            children.add(child_id)
    cache[wf_id] = set(children)
    return children


def _workflow_reaches_target_via_invoke(
    db,
    *,
    current_workflow_id: str,
    target_workflow_id: str,
    seen: set[str],
    cache: dict[str, set[str]],
) -> bool:
    current = str(current_workflow_id or "").strip()
    target = str(target_workflow_id or "").strip()
    if not current or not target:
        return False
    if current == target:
        return True
    if current in seen:
        return False
    seen.add(current)
    for child_id in _workflow_invoke_children(db, current, cache):
        if _workflow_reaches_target_via_invoke(
            db,
            current_workflow_id=child_id,
            target_workflow_id=target,
            seen=seen,
            cache=cache,
        ):
            return True
    return False


def _normalize_producer_type(raw_type: Any) -> str:
    """Keep generic producer JSON distinct from the public workflow object alias."""
    if str(raw_type or "").strip().lower() == "json":
        return "json"
    return _normalize_type(raw_type)


def _collect_step_declared_outputs(db, step: dict, *, account_id: str = "") -> list[dict]:
    """Use the same authority even for minimal registry adapters."""
    from core.application.registry.step_output_contract import resolve_step_output_contract

    override = _to_object(step.get("override"))
    contract = resolve_step_output_contract(
        db,
        agent_id=str(step.get("agent_id") or step.get("agent") or "").strip(),
        skill_id=str(step.get("skill_id") or step.get("skill") or "").strip(),
        block_key=str(
            override.get("published_block_key") or override.get("block_key") or ""
        ).strip(),
        account_id=account_id,
    )
    return [{**row, "step_id": str(step.get("step_id") or "")} for row in contract.rows]


def _collect_workflow_output_types(db, workflow: dict) -> dict[str, set[str]]:
    def _expanded_output_types(raw_type: Any) -> set[str]:
        normalized = _normalize_type(raw_type)
        types = {normalized}
        original_type = str(raw_type or "").strip().lower()
        if original_type == "json":
            # Older published blocks and skill contracts used `json` as a
            # generic JSON carrier. Keep its breadth (including scalar values)
            # for the shared compatibility check without widening precise types.
            types.update({"object", "array", "json"})
        elif original_type == "list":
            types.update({"array", "list"})
        return types

    output_types: dict[str, set[str]] = {}
    resolver = IOResolver(db)
    for step in _iter_workflow_steps(workflow):
        step_outputs: list[dict] = []
        get_declared_outputs = getattr(resolver, "get_step_declared_outputs", None)
        if callable(get_declared_outputs):
            try:
                step_outputs = (
                    get_declared_outputs(
                        step, workflow_account_id=str(workflow.get("account_id") or "").strip()
                    )
                    or []
                )
            except AttributeError:
                # Minimal registry stores can expose declarations without resolver lookups.
                logger.debug("Using declared producer contracts for step %s", step.get("step_id"))
                step_outputs = _collect_step_declared_outputs(
                    db, step, account_id=str(workflow.get("account_id") or "").strip()
                )
        for item in step_outputs:
            if not isinstance(item, dict):
                continue
            name = _normalize_name(item.get("name"))
            if not name:
                continue
            # Match collect_runtime_outputs' public fields, keeping full producer
            # paths as well. A descendant such as customer.text is not output text.
            names = {name}
            parts = canonical_state_write_parts(name)
            if parts and parts[0] == "workflow_data":
                parts = parts[1:]
            if parts and parts[0] == "outputs":
                parts = parts[1:]
            if parts and len(parts) == 1:
                names.add(parts[0])
            for producer_name in names:
                output_types.setdefault(producer_name, set()).update(
                    _expanded_output_types(item.get("type"))
                )

    return output_types


def _validate_callable_outputs_produced(
    *,
    db,
    workflow: dict,
    callable_outputs: list[dict],
    path_prefix: str,
    code_prefix: str,
    warnings: list[dict] | None = None,
) -> list[dict]:
    """Declared callable outputs no step can produce, or produce with a wrong type.

    An optional output (``required: false``) that no step produces is only a
    warning in *warnings*: a run may leave it unset, and its caller keeps its
    own value (a hook keeps its input, R2-11). A produced value of a wrong
    type is always an error.
    """
    issues: list[dict] = []
    from core.application.registry.step_output_contract import OutputContractResolutionError

    try:
        output_types = _collect_workflow_output_types(db, workflow)
    except OutputContractResolutionError as exc:
        return [
            _issue(
                code=f"{code_prefix}.output_contract_unavailable",
                message=str(exc),
                path=path_prefix,
            )
        ]
    workflow_id = str((workflow or {}).get("workflow_id") or "").strip() or "<unknown>"
    for idx, output_item in enumerate(callable_outputs):
        if not isinstance(output_item, dict):
            continue
        output_name = _normalize_name(output_item.get("name"))
        if not output_name:
            continue
        expected_type = _normalize_type(output_item.get("type"))
        produced_types = sorted(output_types.get(output_name) or [])
        if not produced_types:
            issue = _issue(
                code=f"{code_prefix}.output_not_produced",
                message=(
                    f"Callable output '{output_name}' is declared but workflow '{workflow_id}' "
                    "does not produce it in any step contract"
                ),
                path=f"{path_prefix}[{idx}].name",
            )
            if output_item.get("required") is False:
                if warnings is not None:
                    warnings.append(issue)
                continue
            issues.append(issue)
            continue
        if not any(
            IOResolver._check_type_compat(expected_type, source_type)
            for source_type in produced_types
        ):
            issues.append(
                _issue(
                    code=f"{code_prefix}.output_type_mismatch",
                    message=(
                        f"Callable output '{output_name}' expects type '{expected_type}', "
                        f"but workflow '{workflow_id}' can only produce {', '.join(produced_types)}"
                    ),
                    path=f"{path_prefix}[{idx}].type",
                )
            )
    return issues


def _infer_literal_selector_type(source_selector: str) -> str:
    raw = str(source_selector or "").strip()
    if not raw.startswith(LITERAL_PREFIX):
        return "text"
    payload = raw[len(LITERAL_PREFIX) :].strip()
    if not payload:
        return "text"
    try:
        parsed = json.loads(payload)
    except Exception:
        return "text"
    if isinstance(parsed, bool):
        return "boolean"
    if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
        return "number"
    if isinstance(parsed, list):
        return "list"
    if isinstance(parsed, dict):
        return "object"
    return "text"


def _resolve_invoke_mapping_source_type(
    *,
    source_selector: str,
    child_input_name: str,
    available_type_map: dict[str, str],
) -> tuple[str | None, str]:
    selector = str(source_selector or "").strip()
    input_name = str(child_input_name or "").strip()
    if not selector:
        return None, "missing_source_selector"
    if selector.startswith(LITERAL_PREFIX):
        return _infer_literal_selector_type(selector), ""
    if selector.startswith("message."):
        if selector.startswith("message.step."):
            normalized = selector
            if normalized in available_type_map:
                return _normalize_producer_type(available_type_map.get(normalized)), ""
            return None, "source_not_available"
        # Agent- and group-scoped message selectors depend on runtime envelopes
        # (a group's latest step result exists only after it runs) and cannot be
        # fully validated statically at save-time.
        return None, "dynamic_message_selector"

    candidates: list[str] = []
    if selector == "state":
        if input_name:
            candidates.append(input_name)
    elif selector.startswith(STATE_PREFIX):
        state_path = selector[len(STATE_PREFIX) :].strip()
        if state_path:
            candidates.append(state_path)
        candidates.append(selector)
    else:
        candidates.append(selector)

    for key in candidates:
        if key in available_type_map:
            return _normalize_producer_type(available_type_map.get(key)), ""
    return None, "source_not_available"


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
