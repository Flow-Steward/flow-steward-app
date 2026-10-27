"""Reusable helpers for step replay and re-execution flows."""

from __future__ import annotations

import copy
import difflib
from collections.abc import Callable
from typing import Any


def extract_replay_prompt_capture(trace_payload: dict[str, Any]) -> dict[str, Any]:
    prompt_request = trace_payload.get("prompt_request")
    if not isinstance(prompt_request, dict):
        return {}
    payload = prompt_request.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    meta = prompt_request.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    return {
        "prompt_version": str(payload.get("prompt_version") or "").strip(),
        "provider": str(meta.get("provider") or "").strip().lower(),
        "system_prompt": str(payload.get("system_prompt") or ""),
        "user_prompt": str(payload.get("user_prompt") or ""),
        "skill": str(meta.get("skill") or "").strip(),
        "step_id": str(meta.get("step_id") or "").strip(),
        "correlation_id": str(meta.get("correlation_id") or "").strip(),
        "model_used": str(prompt_request.get("model_used") or "").strip(),
    }


def derive_original_replay_outcome(trace_payload: dict[str, Any]) -> dict[str, Any]:
    message = trace_payload.get("message")
    message = message if isinstance(message, dict) else {}
    artifacts = message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    prompt_response = trace_payload.get("prompt_response")
    prompt_response = prompt_response if isinstance(prompt_response, dict) else {}
    prompt_result = prompt_response.get("result")
    prompt_result = prompt_result if isinstance(prompt_result, dict) else {}

    raw_output = str(artifacts.get("raw_output") or "")
    excerpt = str(prompt_result.get("excerpt") or "") or (raw_output[:4000] if raw_output else "")
    return {
        "message_status": str(message.get("status") or "").strip(),
        "message_summary": str(message.get("summary") or "").strip(),
        "response_text": raw_output,
        "response_excerpt": excerpt,
    }


def build_replay_diff_excerpt(original_text: str, replayed_text: str) -> str:
    original = str(original_text or "")
    replayed = str(replayed_text or "")
    if not original and not replayed:
        return ""
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(),
            replayed.splitlines(),
            fromfile="original",
            tofile="replayed",
            n=2,
            lineterm="",
        )
    )
    if len(diff_lines) > 200:
        diff_lines = [*diff_lines[:200], "... (diff truncated)"]
    return "\n".join(diff_lines)[:16000]


def _build_replay_config(
    *,
    project_config: dict[str, Any],
    policy_snapshot: dict[str, Any] | None,
    prompt_version: str,
    replay_mode: str,
    policy_snapshot_hash,
) -> tuple[dict[str, Any], str, str | None]:
    replay_config = copy.deepcopy(project_config) if isinstance(project_config, dict) else {}
    if isinstance(policy_snapshot, dict) and policy_snapshot:
        replay_config["project_policy"] = copy.deepcopy(policy_snapshot)
    replay_meta = replay_config.get("_replay_context")
    if not isinstance(replay_meta, dict):
        replay_meta = {}
    replay_meta["mode"] = replay_mode
    clean_prompt_version = str(prompt_version or "").strip()
    if clean_prompt_version:
        replay_meta["prompt_version"] = clean_prompt_version
    policy_hash = policy_snapshot_hash(policy_snapshot)
    if policy_hash:
        replay_meta["policy_snapshot_hash"] = policy_hash
    replay_config["_replay_context"] = replay_meta
    return replay_config, clean_prompt_version, policy_hash


def _replay_definition_state(
    bl, job: dict[str, Any], step_id: str, *, replayed_message_id: str
) -> dict[str, Any]:
    """The run state slice that puts a replay at one step of the job's pinned definition.

    The definition is the archive row the job pinned, verified against its
    digest; the cursor starts a fresh execution of the replayed step, derived
    from the replayed message so a repeated replay names the same execution.
    """
    from core.application.jobs.workflow_definition_snapshot_repository import (
        WorkflowDefinitionSnapshotRepository,
    )
    from core.application.orchestration.run_cursor import (
        RUNTIME_SCHEMA_VERSION,
        FrozenDefinition,
        seeded_step_cursor,
    )
    from core.application.orchestration.run_definition import (
        RUNTIME_SCHEMA_VERSION_KEY,
        WORKFLOW_DEFINITION_REF_KEY,
        RunDefinitionIntegrityError,
        prime_run_definition,
    )
    from core.application.workflows.workflow_definition_snapshot import definition_ref

    job_id = str(job.get("id") or "").strip()
    row = WorkflowDefinitionSnapshotRepository(bl.conn).load_for_job(job_id) if job_id else None
    definition = row.get("definition_jsonb") if isinstance(row, dict) else None
    if not isinstance(row, dict) or not isinstance(definition, dict):
        raise RunDefinitionIntegrityError(f"Job '{job_id}' has no pinned workflow definition")
    ref = definition_ref(str(row.get("id")), int(row.get("revision") or 0), str(row.get("digest")))
    prime_run_definition(ref, definition)
    FrozenDefinition(definition).step(step_id)
    return {
        RUNTIME_SCHEMA_VERSION_KEY: RUNTIME_SCHEMA_VERSION,
        WORKFLOW_DEFINITION_REF_KEY: ref,
        "run_cursor": seeded_step_cursor(
            step_id, seed=f"replay:{job_id}:{replayed_message_id}"
        ).dump(),
        "current_step_id": step_id,
    }


def invoke_step_replay_llm(
    *,
    project_config: dict[str, Any],
    agent_name: str,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    prompt_version: str = "",
    policy_snapshot: dict[str, Any] | None = None,
    policy_snapshot_hash,
) -> dict[str, Any]:
    provider_name = str(provider or "").strip().lower()
    replay_config, clean_prompt_version, policy_hash = _build_replay_config(
        project_config=project_config,
        policy_snapshot=policy_snapshot,
        prompt_version=prompt_version,
        replay_mode="step_replay",
        policy_snapshot_hash=policy_snapshot_hash,
    )
    agents_cfg = replay_config.get("agents_config")
    if not isinstance(agents_cfg, dict):
        agents_cfg = {}
        replay_config["agents_config"] = agents_cfg
    agents_cfg.setdefault("agents", {})
    agents_cfg["agents"].setdefault("config", {})
    agent_cfg = agents_cfg["agents"]["config"].setdefault(agent_name, {})
    if provider_name:
        agent_cfg["provider"] = provider_name
    if model:
        agent_cfg["model"] = model

    from langchain_core.messages import HumanMessage, SystemMessage

    from core.application.orchestration.model_provider import get_chat_model

    llm = get_chat_model(replay_config, agent_name=agent_name)
    response = llm.invoke(
        [
            SystemMessage(content=str(system_prompt or "")),
            HumanMessage(content=str(user_prompt or "")),
        ]
    )

    from core.application.orchestration.model_response_text import model_response_text

    replayed_text = model_response_text(response)

    response_meta = getattr(response, "response_metadata", None)
    if not isinstance(response_meta, dict):
        response_meta = {}
    usage_meta = getattr(response, "usage_metadata", None)
    if not isinstance(usage_meta, dict):
        usage_meta = {}

    model_used = (
        str(response_meta.get("model_name") or "").strip()
        or str(response_meta.get("model") or "").strip()
        or str(model or "").strip()
    )

    return {
        "response_text": replayed_text,
        "response_excerpt": replayed_text[:4000],
        "provider": provider_name or None,
        "model_used": model_used or None,
        "prompt_version": clean_prompt_version or None,
        "policy_snapshot_hash": policy_hash,
        "response_metadata": response_meta,
        "usage": usage_meta,
    }


def invoke_step_reexecute_runtime(
    *,
    bl,
    job: dict[str, Any],
    message: dict[str, Any],
    trace_payload: dict[str, Any],
    project_config: dict[str, Any],
    policy_snapshot: dict[str, Any] | None = None,
    prompt_version: str = "",
    provider: str = "",
    model: str = "",
    policy_snapshot_hash,
    to_json_obj,
    serialize_agent_message_row,
) -> dict[str, Any]:
    """Replay one step by re-running the real agent node runtime path."""
    from core.application.orchestration.graph.nodes import (
        load_agent_implementation,
        wrap_agent_as_node,
    )
    from core.application.orchestration.state import build_initial_orchestrator_state
    from core.application.registry.registry_manager import RegistryManager

    agent_id = str(message.get("from_agent") or "").strip()
    step_id = str(message.get("step_id") or "").strip()
    skill = str(message.get("skill") or "").strip()
    if not agent_id:
        raise ValueError("Replay message is missing from_agent")
    if not skill:
        raise ValueError("Replay message is missing skill")
    if not step_id:
        raise ValueError("Replay message is missing step_id")

    replay_config, clean_prompt_version, policy_hash = _build_replay_config(
        project_config=project_config,
        policy_snapshot=policy_snapshot,
        prompt_version=prompt_version,
        replay_mode="step_reexecute",
        policy_snapshot_hash=policy_snapshot_hash,
    )

    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip()
    if provider_name or model_name:
        agents_cfg = replay_config.get("agents_config")
        if not isinstance(agents_cfg, dict):
            agents_cfg = {}
            replay_config["agents_config"] = agents_cfg
        agents_cfg.setdefault("agents", {})
        agents_cfg["agents"].setdefault("config", {})
        agent_cfg = agents_cfg["agents"]["config"].setdefault(agent_id, {})
        if provider_name:
            agent_cfg["provider"] = provider_name
        if model_name:
            agent_cfg["model"] = model_name

    history_messages: list[dict[str, Any]] = []
    target_message_id = str(message.get("id") or "").strip()
    try:
        rows = bl.get_agent_messages(str(job.get("id") or ""), since_id=0, limit=2000) or []
    except Exception:
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "").strip()
        if target_message_id and row_id == target_message_id:
            break
        envelope = to_json_obj(row.get("envelope"))
        if (
            isinstance(envelope, dict)
            and str(envelope.get("schema_version") or "") == "agent_message_v1"
        ):
            history_messages.append(dict(envelope))
            continue
        history_messages.append(serialize_agent_message_row(dict(row)))
    if not history_messages:
        related = trace_payload.get("related_messages")
        if isinstance(related, list):
            for item in related:
                if isinstance(item, dict):
                    history_messages.append(dict(item))

    project_name = str(replay_config.get("project_name") or job.get("project_name") or "").strip()
    replay_state = build_initial_orchestrator_state(
        task_id=str(job.get("task_id") or f"replay-{job.get('id') or ''}"),
        task_summary=str(job.get("task_summary") or "Replay step execution"),
        task_source="step_replay",
        project_name=project_name,
        project_config=replay_config,
        extra_state={
            "project_id": 0,
            "job_id": 0,
            "workflow_id": str(job.get("selected_workflow") or ""),
            **_replay_definition_state(bl, job, step_id, replayed_message_id=target_message_id),
            "agent_messages": history_messages,
        },
    )

    registry = RegistryManager.instance()
    manifest = registry.agents.get_agent(agent_id)
    if manifest is None or not bool(getattr(manifest, "enabled", True)):
        raise ValueError(f"Replay agent '{agent_id}' is missing or disabled")

    node_fn = wrap_agent_as_node(
        load_agent_implementation(manifest, registry.tools, registry.skills),
        manifest,
    )
    update = node_fn(replay_state)
    update = dict(update or {}) if isinstance(update, dict) else {}
    replay_messages = update.get("agent_messages")
    if not isinstance(replay_messages, list):
        replay_messages = (
            replay_state.get("agent_messages") if isinstance(replay_state, dict) else []
        )
    replay_message = (
        replay_messages[-1] if isinstance(replay_messages, list) and replay_messages else None
    )
    if not isinstance(replay_message, dict):
        raise RuntimeError("Step re-execution did not return an AgentMessageV1 payload")

    artifacts = replay_message.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    response_text = str(artifacts.get("raw_output") or replay_message.get("summary") or "")
    return {
        "response_text": response_text,
        "response_excerpt": response_text[:4000],
        "provider": provider_name or None,
        "model_used": model_name or None,
        "prompt_version": clean_prompt_version or None,
        "policy_snapshot_hash": policy_hash,
        "message_status": str(replay_message.get("status") or "").strip(),
        "message_summary": str(replay_message.get("summary") or "").strip(),
        "agent_id": agent_id,
        "step_id": step_id,
        "skill": skill,
        "replay_message": replay_message,
    }


def build_runtime_invokers(
    *,
    policy_snapshot_hash,
    to_json_obj,
    serialize_agent_message_row,
) -> tuple[Callable[..., dict[str, Any]], Callable[..., dict[str, Any]]]:
    def _invoke_step_replay_llm(**kwargs) -> dict[str, Any]:
        return invoke_step_replay_llm(
            policy_snapshot_hash=policy_snapshot_hash,
            **kwargs,
        )

    def _invoke_step_reexecute_runtime(**kwargs) -> dict[str, Any]:
        return invoke_step_reexecute_runtime(
            policy_snapshot_hash=policy_snapshot_hash,
            to_json_obj=to_json_obj,
            serialize_agent_message_row=serialize_agent_message_row,
            **kwargs,
        )

    return _invoke_step_replay_llm, _invoke_step_reexecute_runtime
