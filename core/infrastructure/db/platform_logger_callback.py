from __future__ import annotations

import math
import time
import traceback
from collections.abc import Mapping
from typing import Any

from core.application.jobs.public_progress_service import PublicStepProgressRecorder
from core.application.jobs.workflow_wait_contract import (
    STATUS_AWAITING_APPROVAL,
    WAIT_STATUS_BY_KIND,
)
from core.application.observability.llm_costs import estimate_llm_cost_usd
from core.infrastructure.db.platform_logger import (
    BaseCallbackHandler,
    _trace_id_for_job,
    logger,
)


class PlatformLoggerCallback(BaseCallbackHandler):
    """LangGraph callback that writes structured action logs to PostgreSQL
    and captures the root run id for platform trace linkage.

    Usage:
        logger = PlatformJobStore(dsn)
        project_id = logger.add_project("my-project")
        job_id = logger.create_job(project_id, task_id, thread_id=thread_id, ...)
        callback = PlatformLoggerCallback(logger, project_id, job_id)
        graph.invoke(state, config={"callbacks": [callback]})
    """

    # Node statuses that come from graph-level state and should NOT bleed
    # into per-node log rows. We always record "done" for a clean node end.
    _GRAPH_STATUSES = frozenset(
        {
            "in_progress",
            "resume_pending",
            "completed",
            "awaiting_approval",
            "failed",
            "max_iterations",
        }
    )

    def __init__(self, logger: Any, project_id: str, job_id: str):
        super().__init__()
        self.logger = logger
        self.project_id = project_id
        self.job_id = job_id
        self._trace_id = _trace_id_for_job(job_id) or f"job:{job_id}"
        # Maps run_id -> (start_time, node_name)
        self._node_starts: dict = {}
        self._run_parents: dict[str, str | None] = {}
        # Runs nested inside a graph node (an agent's own chain, its output
        # parser). They are not workflow steps and must not appear as ones.
        self._nested_runs: set[str] = set()
        self._llm_providers: dict[str, str] = {}
        self._llm_provider_types: dict[str, str] = {}
        self._llm_models: dict[str, str] = {}
        # The very first run_id seen is the root graph run.
        self._root_run_id: str | None = None
        # Track most-recent active node name for associating LLM calls
        self._current_node: str = "unknown"
        self._public_step_recorder = PublicStepProgressRecorder(
            gateway=logger,
            project_id=str(project_id),
            job_id=str(job_id),
        )
        self._last_public_error_context: dict[str, object] = {}

    def on_chat_model_start(
        self,
        serialized,
        messages,
        *,
        run_id=None,
        metadata=None,
        **_kwargs,
    ) -> None:
        """Remember the selected route identity for exact cost resolution."""
        del serialized, messages
        run_key = self._run_id_str(run_id)
        provider = str((metadata or {}).get("flow_steward_provider") or "").strip()
        provider_type = str((metadata or {}).get("flow_steward_provider_policy") or "").strip()
        model = str((metadata or {}).get("flow_steward_model") or "").strip()
        if run_key and provider:
            self._llm_providers[run_key] = provider
        if run_key and provider_type:
            self._llm_provider_types[run_key] = provider_type
        if run_key and model:
            self._llm_models[run_key] = model

    @staticmethod
    def _step_scope_from_state(data: dict | None) -> tuple[str, str]:
        """The step and real group the run cursor points at, as mirrored in state."""
        if not isinstance(data, dict):
            return "", ""
        step_id = data.get("current_step_id")
        group_id = data.get("current_group_id")
        return (
            step_id.strip() if isinstance(step_id, str) else "",
            group_id.strip() if isinstance(group_id, str) else "",
        )

    @staticmethod
    def _response_excerpt(outputs: dict | None, node_name: str) -> str:
        if not isinstance(outputs, dict):
            return ""
        agent_messages = outputs.get("agent_messages") or []
        if isinstance(agent_messages, list):
            for msg in reversed(agent_messages):
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("from_agent") or "") != node_name:
                    continue
                message_excerpt = PlatformLoggerCallback._response_excerpt_from_message(msg)
                if message_excerpt:
                    return message_excerpt
        return PlatformLoggerCallback._response_excerpt_from_outputs(outputs)

    @staticmethod
    def _response_excerpt_from_message(message: dict) -> str:
        summary = message.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()[:1000]
        artifacts = message.get("artifacts")
        if not isinstance(artifacts, dict):
            return ""
        raw_output = artifacts.get("raw_output")
        if isinstance(raw_output, str) and raw_output.strip():
            return raw_output.strip()[:1000]
        return ""

    @staticmethod
    def _response_excerpt_from_outputs(outputs: dict) -> str:
        for key in ("final_report", "task_summary"):
            value = outputs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:1000]
        return ""

    @staticmethod
    def _patch_put_if_set(patch: dict, key: str, value) -> None:
        if value is None:
            return
        if isinstance(value, str) and not value:
            return
        patch[key] = value

    def _selection_meta_patch(self, outputs: dict | None) -> dict:
        if not isinstance(outputs, dict):
            return {}

        patch: dict = {}
        self._patch_put_if_set(
            patch,
            "workflow_selection_source",
            str(outputs.get("workflow_selection_source") or "").strip(),
        )
        self._patch_put_if_set(
            patch,
            "workflow_selection_reason",
            str(outputs.get("routing_reason") or "").strip(),
        )

        return patch

    @staticmethod
    def _run_id_str(run_id) -> str | None:
        return str(run_id) if run_id else None

    @staticmethod
    def _active_agent(node_name: str) -> str | None:
        return node_name if node_name not in ("unknown", "LangGraph") else None

    @staticmethod
    def _as_output_dict(outputs) -> dict:
        return outputs if isinstance(outputs, dict) else {}

    @staticmethod
    def _workflow_id_from_payload(payload: dict) -> str:
        workflow_id = payload.get("workflow_id")
        return workflow_id if isinstance(workflow_id, str) else ""

    @staticmethod
    def _node_status(outputs: dict, graph_statuses: frozenset[str]) -> str:
        status = outputs.get("status")
        return status if status and status not in graph_statuses else "done"

    @staticmethod
    def _extract_error_text(error) -> str:
        if isinstance(error, BaseException):
            return "".join(
                traceback.format_exception(type(error), error, error.__traceback__)
            ).strip()
        return str(error)

    @staticmethod
    def _is_graph_interrupt(error_text: str) -> bool:
        lowered = error_text.lower()
        return "graphinterrupt" in lowered and "interrupt(value=" in lowered

    @staticmethod
    def _interrupt_pause_status(error: Any) -> str:
        """The pause a GraphInterrupt names: a Wait's timer or child, else an approval.

        A Wait interrupts with its recorded wait (``gate_kind: "wait"`` and its
        ``kind``); every other interrupt is an approval gate.
        """
        args = getattr(error, "args", None) or ()
        interrupts = args[0] if args and isinstance(args[0], (list, tuple)) else ()
        for item in interrupts:
            value = getattr(item, "value", None)
            if isinstance(value, Mapping) and str(value.get("gate_kind") or "") == "wait":
                status = WAIT_STATUS_BY_KIND.get(str(value.get("kind") or ""))
                if status:
                    return status
        return STATUS_AWAITING_APPROVAL

    @staticmethod
    def _llm_usage_from_output(llm_output: dict) -> dict:
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        return usage if isinstance(usage, dict) else {}

    @staticmethod
    def _response_generations(response) -> list:
        generations = getattr(response, "generations", [[]])
        return generations if isinstance(generations, list) else [generations]

    @staticmethod
    def _generation_items(gen_list) -> list:
        return gen_list if isinstance(gen_list, list) else [gen_list]

    def _tracked_node_context(
        self, run_id, name: str | None
    ) -> tuple[str | None, str | None, str, int | None]:
        run_id_str = self._run_id_str(run_id)
        start_entry = self._node_starts.pop(run_id_str, None) if run_id_str else None
        parent_run_id = self._run_parents.pop(run_id_str, None) if run_id_str else None
        start_time, tracked_name = start_entry if start_entry else (None, None)
        node_name = name or tracked_name or "unknown"
        duration = int((time.perf_counter() - start_time) * 1000) if start_time else None
        return run_id_str, parent_run_id, node_name, duration

    def _record_public_step(
        self,
        *,
        run_id,
        node_name: str,
        payload: dict,
        status: str,
        starting: bool = False,
    ) -> dict[str, object] | None:
        run_id_str = self._run_id_str(run_id)
        if run_id_str and run_id_str in self._nested_runs:
            if not starting:
                self._nested_runs.discard(run_id_str)
            return None
        return self._public_step_recorder.record(
            run_id=run_id,
            node_name=node_name,
            payload=payload,
            status=status,
            starting=starting,
        )

    def terminal_error_context(self) -> dict[str, object]:
        """Return the last failed public step identity for terminal persistence."""
        return dict(self._last_public_error_context)

    def _trace_meta(
        self,
        *,
        span_kind: str,
        run_id_str: str | None,
        parent_run_id: str | None,
        span_id_prefix: str = "run",
        extra: dict | None = None,
    ) -> dict:
        meta = {
            "trace_id": self._trace_id,
            "span_kind": span_kind,
            "span_id": f"{span_id_prefix}:{run_id_str}" if run_id_str else None,
            "parent_span_id": f"run:{parent_run_id}" if parent_run_id else f"{self._trace_id}:root",
            "run_id": run_id_str,
            "parent_run_id": parent_run_id,
            "root_run_id": self._root_run_id,
        }
        if extra:
            meta.update(extra)
        return meta

    def _normalize_chain_error(
        self,
        *,
        error,
        error_text: str,
        run_id_str: str | None,
        parent_run_id: str | None,
        node_name: str,
    ) -> dict:
        source_hint, category_hint = ("system", "unknown")
        inferred = self.logger.infer_exception_hints(
            error if isinstance(error, BaseException) else error_text,
            context={"run_id": run_id_str, "node_name": node_name},
        )
        if isinstance(inferred, tuple) and len(inferred) == 2:
            source_hint = str(inferred[0] or "system")
            category_hint = str(inferred[1] or "unknown")
        if run_id_str:
            parent_span_id = f"run:{run_id_str}"
        elif parent_run_id:
            parent_span_id = f"run:{parent_run_id}"
        else:
            parent_span_id = f"{self._trace_id}:root"
        return self.logger.normalize_exception_error(
            error if isinstance(error, BaseException) else error_text,
            source_hint=source_hint,
            category_hint=category_hint,
            context={
                "run_id": run_id_str,
                "parent_run_id": parent_run_id,
                "root_run_id": self._root_run_id,
                "trace_id": self._trace_id,
                "span_id": f"error:{run_id_str}" if run_id_str else None,
                "parent_span_id": parent_span_id,
                "node_name": node_name,
            },
        )

    def _extract_usage_from_generations(self, response) -> tuple[dict, dict]:
        usage: dict = {}
        input_token_details: dict = {}
        for gen_list in self._response_generations(response):
            for gen in self._generation_items(gen_list):
                msg = getattr(gen, "message", None)
                usage_metadata = getattr(msg, "usage_metadata", None) or {}
                response_metadata = getattr(msg, "response_metadata", None) or {}
                meta = usage_metadata if usage_metadata else response_metadata
                if not isinstance(meta, dict) or not meta:
                    continue
                usage = meta
                details = meta.get("input_token_details")
                if isinstance(details, dict):
                    input_token_details = details
                if not input_token_details and isinstance(response_metadata, dict):
                    raw_usage = response_metadata.get("usage", {})
                    if isinstance(raw_usage, dict):
                        input_token_details = {
                            "cache_creation": raw_usage.get("cache_creation_input_tokens", 0),
                            "cache_read": raw_usage.get("cache_read_input_tokens", 0),
                        }
                return usage, input_token_details
        return usage, input_token_details

    def _extract_llm_usage(self, response) -> tuple[dict, dict, dict]:
        llm_output = getattr(response, "llm_output", None) or {}
        llm_output_dict = llm_output if isinstance(llm_output, dict) else {}
        usage = self._llm_usage_from_output(llm_output_dict)
        if usage:
            raw_details = usage.get("prompt_tokens_details") or usage.get("input_token_details")
            return usage, raw_details if isinstance(raw_details, dict) else {}, llm_output_dict
        usage_from_gens, input_token_details = self._extract_usage_from_generations(response)
        return usage_from_gens, input_token_details, llm_output_dict

    @staticmethod
    def _usage_token_counts(usage: dict) -> tuple[int, int]:
        prompt_tokens = (
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("prompt_token_count")
            or 0
        )
        completion_tokens = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("candidates_token_count")
            or 0
        )
        return int(prompt_tokens or 0), int(completion_tokens or 0)

    @staticmethod
    def _cache_token_counts(usage: dict, input_token_details: dict) -> tuple[int, int]:
        details = input_token_details
        if not details:
            inline_details = usage.get("input_token_details")
            details = inline_details if isinstance(inline_details, dict) else {}
        cache_creation = int(
            details.get("cache_creation", 0) or details.get("cache_write_tokens", 0) or 0
        )
        cache_read = int(details.get("cache_read", 0) or details.get("cached_tokens", 0) or 0)
        if not cache_creation:
            cache_creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
        if not cache_read:
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
        return cache_creation, cache_read

    @staticmethod
    def _reported_cost_usd(llm_output: dict) -> float | None:
        value = llm_output.get("cost_usd")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        normalized = float(value)
        return normalized if math.isfinite(normalized) and normalized >= 0 else None

    # ------------------------------------------------------------------
    # LangChain callbacks
    # ------------------------------------------------------------------

    def on_chain_start(self, serialized=None, inputs=None, *, run_id=None, name=None, **kwargs):
        """Called when a LangGraph node (or the whole graph) starts."""
        run_id_str = self._run_id_str(run_id)
        parent_run_id = self._run_id_str(kwargs.get("parent_run_id"))
        node_name = name or "unknown"
        payload = self._as_output_dict(inputs)
        active_agent = self._active_agent(node_name)
        metadata = kwargs.get("metadata")
        graph_node = (
            str(metadata.get("langgraph_node") or "").strip() if isinstance(metadata, dict) else ""
        )
        if run_id_str and graph_node and node_name != graph_node:
            self._nested_runs.add(run_id_str)

        # The first call is the root graph invocation — capture its run_id
        if run_id_str and self._root_run_id is None:
            self._root_run_id = run_id_str

        if run_id_str:
            self._node_starts[run_id_str] = (time.perf_counter(), node_name)
            self._run_parents[run_id_str] = parent_run_id

        # Track most-recent node name for LLM call association
        if active_agent:
            self._current_node = active_agent

        step_id, group_id = self._step_scope_from_state(payload)
        try:
            self.logger.update_job_runtime(
                self.job_id,
                selected_workflow=self._workflow_id_from_payload(payload) or None,
                current_step_id=step_id or None,
                current_group_id=group_id or None,
                current_agent=active_agent,
                heartbeat_now=True,
            )
        except Exception as e:
            logger.debug("on_chain_start runtime update failed (non-fatal): %s", e)

        self._record_public_step(
            run_id=run_id,
            node_name=node_name,
            payload=payload,
            status="in_progress",
            starting=True,
        )

        # node_start rows are NOT logged — they create orphaned "running…" entries
        # in the dashboard because node_start and node_end are separate rows that
        # are never paired/updated. node_end already captures agent, duration and
        # status so node_start adds no value and only creates visual noise.

    def on_chain_end(self, outputs=None, *, run_id=None, name=None, **kwargs):
        """Called when a LangGraph node completes."""
        outputs_dict = self._as_output_dict(outputs)
        run_id_str, parent_run_id, node_name, duration = self._tracked_node_context(run_id, name)
        active_agent = self._active_agent(node_name)
        # Keep a node-local status only when the callback payload provides one
        # and it does not collide with graph/job lifecycle statuses.
        status = self._node_status(outputs_dict, self._GRAPH_STATUSES)
        workflow_id = self._workflow_id_from_payload(outputs_dict)
        step_id, group_id = self._step_scope_from_state(outputs_dict)

        try:
            self.logger.log_action(
                self.project_id,
                outputs_dict.get("task_id", ""),
                outputs_dict.get("task_summary", ""),
                agent=node_name,
                action_type="node_end",
                status=status,
                duration=duration,
                meta=self._trace_meta(
                    span_kind="node",
                    run_id_str=run_id_str,
                    parent_run_id=parent_run_id,
                    extra={
                        "workflow_id": workflow_id or None,
                        "step_id": step_id or None,
                        "group_id": group_id or None,
                    },
                ),
                job_id=self.job_id,
            )
        except Exception as e:
            logger.warning("on_chain_end log failed: %s", e)

        try:
            self.logger.update_job_runtime(
                self.job_id,
                selected_workflow=workflow_id or None,
                current_step_id=step_id or None,
                current_group_id=group_id or None,
                current_agent=active_agent,
                heartbeat_now=True,
                last_response_excerpt=self._response_excerpt(outputs_dict, node_name),
                clear_last_error=status != "error",
            )
        except Exception as e:
            logger.debug("on_chain_end runtime update failed (non-fatal): %s", e)

        try:
            meta_patch = self._selection_meta_patch(outputs_dict)
            if meta_patch:
                self.logger.merge_job_meta(self.job_id, meta_patch)
        except Exception as e:
            logger.debug("on_chain_end meta selection update failed (non-fatal): %s", e)

        public_status = {
            "cancelled": "cancelled",
            "failed": "failed",
            "skipped": "skipped",
        }.get(str(status).strip().lower(), "completed")
        # Node commands carry partial state updates rather than the input state.
        # Keep generic logging unchanged; normalize the public writer boundary.
        from langgraph.types import Command

        public_payload = (
            self._as_output_dict(outputs.update) if isinstance(outputs, Command) else outputs_dict
        )
        self._record_public_step(
            run_id=run_id,
            node_name=node_name,
            payload=public_payload,
            status=public_status,
        )

    def on_chain_error(self, error=None, *, run_id=None, name=None, **kwargs):
        """Called when a LangGraph node errors."""
        run_id_str, parent_run_id, node_name, _ = self._tracked_node_context(run_id, name)
        active_agent = self._active_agent(node_name)
        error_text = self._extract_error_text(error)

        if self._is_graph_interrupt(error_text):
            # GraphInterrupt is expected control flow for a durable pause: an
            # approval gate, a Wait timer or a child invocation. Log as
            # node_interrupt and clear runtime error so UI does not show it as a failure.
            pause_status = self._interrupt_pause_status(error)
            try:
                self.logger.log_action(
                    self.project_id,
                    "",
                    "",
                    agent=node_name,
                    action_type="node_interrupt",
                    status=pause_status,
                    result={"interrupt_code": "graph_interrupt"},
                    meta=self._trace_meta(
                        span_kind="node",
                        run_id_str=run_id_str,
                        parent_run_id=parent_run_id,
                    ),
                    job_id=self.job_id,
                )
            except Exception as e:
                logger.warning("on_chain_error interrupt log failed: %s", e)
            try:
                self.logger.update_job_runtime(
                    self.job_id,
                    current_agent=active_agent,
                    heartbeat_now=True,
                    clear_last_error=True,
                )
            except Exception as e:
                logger.debug("on_chain_error interrupt runtime update failed (non-fatal): %s", e)
            self._record_public_step(
                run_id=run_id,
                node_name=node_name,
                payload={},
                status=pause_status,
            )
            return

        public_error_context = self._record_public_step(
            run_id=run_id,
            node_name=node_name,
            payload={},
            status="failed",
        )
        self._last_public_error_context = dict(public_error_context or {})

        try:
            normalized = self._normalize_chain_error(
                error=error,
                error_text=error_text,
                run_id_str=run_id_str,
                parent_run_id=parent_run_id,
                node_name=node_name,
            )
            error_context = dict(normalized.get("context_jsonb") or {})
            occurrence = self._last_public_error_context.get("occurrence")
            if occurrence is not None:
                error_context["occurrence"] = occurrence
            self.logger.log_job_error(
                self.job_id,
                agent_id=active_agent,
                source=normalized.get("source", "unknown"),
                category=normalized.get("category", "unknown"),
                provider_name=normalized.get("provider_name"),
                http_status=normalized.get("http_status"),
                provider_error_code=normalized.get("provider_error_code"),
                provider_error_type=normalized.get("provider_error_type"),
                request_id=normalized.get("request_id"),
                retryable=normalized.get("retryable"),
                error_message=normalized.get("error_message", error_text),
                response_excerpt=normalized.get("response_excerpt"),
                traceback_text=normalized.get("traceback_text"),
                context_jsonb=error_context,
                step_id=str(self._last_public_error_context.get("step_id") or "") or None,
                step_execution_id=str(
                    self._last_public_error_context.get("step_execution_id") or ""
                )
                or None,
                group_id=str(self._last_public_error_context.get("group_id") or "") or None,
            )
        except Exception as e:
            logger.debug("on_chain_error structured log failed (non-fatal): %s", e)

        try:
            self.logger.log_action(
                self.project_id,
                "",
                "",
                agent=node_name,
                action_type="node_error",
                status="error",
                error=error_text,
                meta=self._trace_meta(
                    span_kind="node",
                    run_id_str=run_id_str,
                    parent_run_id=parent_run_id,
                ),
                job_id=self.job_id,
            )
        except Exception as e:
            logger.warning("on_chain_error log failed: %s", e)
        try:
            self.logger.update_job_runtime(
                self.job_id,
                current_agent=active_agent,
                heartbeat_now=True,
                last_error=error_text[:16000],
            )
        except Exception as e:
            logger.debug("on_chain_error runtime update failed (non-fatal): %s", e)

    def on_llm_end(self, response=None, *, run_id=None, name=None, **kwargs):
        """Called when an LLM call completes. Captures token usage and estimates cost.

        Extracts cache_creation and cache_read tokens from Anthropic prompt caching
        for accurate cost estimation. These tokens are billed at different rates
        (1.25x and 0.1x base input rate respectively) and were previously untracked.
        """
        try:
            if response is None:
                return
            run_id_str = self._run_id_str(run_id)
            started_provider = self._llm_providers.pop(run_id_str, "") if run_id_str else ""
            started_provider_type = (
                self._llm_provider_types.pop(run_id_str, "") if run_id_str else ""
            )
            started_model = self._llm_models.pop(run_id_str, "") if run_id_str else ""
            usage, input_token_details, llm_output = self._extract_llm_usage(response)
            if not usage:
                return

            pt, ct = self._usage_token_counts(usage)
            cache_creation, cache_read = self._cache_token_counts(usage, input_token_details)

            # Model name from response metadata
            model = (
                llm_output.get("model_name")
                or llm_output.get("model")
                or started_model
                or name
                or ""
            )
            provider_identity = (
                llm_output.get("provider_key")
                or llm_output.get("provider")
                or started_provider
                or ""
            )
            provider_type = started_provider_type or provider_identity

            # OpenAI prompt/input tokens are aggregate counts and cached tokens
            # are a billed subset. Anthropic input_tokens excludes its separate
            # cache creation/read buckets, so those remain additive.
            pricing_prompt_tokens = pt
            if provider_type == "openai":
                pricing_prompt_tokens = max(pt - cache_creation - cache_read, 0)
            total = usage.get("total_tokens")
            if not total:
                total = (
                    pt + ct if provider_type == "openai" else pt + ct + cache_creation + cache_read
                )

            cost = self._reported_cost_usd(llm_output)
            if cost is None:
                cost = estimate_llm_cost_usd(
                    model,
                    pricing_prompt_tokens,
                    ct,
                    cache_creation,
                    cache_read,
                    provider=provider_type,
                )

            if cache_creation or cache_read:
                logger.debug(
                    "on_llm_end[%s]: cache tokens — create=%d read=%d (input=%d output=%d cost=$%.4f)",
                    self._current_node,
                    cache_creation,
                    cache_read,
                    pt,
                    ct,
                    cost or 0,
                )

            parent_run_id = self._run_id_str(kwargs.get("parent_run_id"))
            self.logger.log_action(
                self.project_id,
                "",
                "",
                agent=self._current_node,
                action_type="llm_call",
                status="done",
                meta=self._trace_meta(
                    span_kind="llm",
                    run_id_str=run_id_str,
                    parent_run_id=parent_run_id,
                    extra={
                        "node_name": self._current_node,
                        "provider_identity": provider_identity,
                        "provider_type": provider_type,
                    },
                ),
                job_id=self.job_id,
                tokens_prompt=pt or None,
                tokens_completion=ct or None,
                tokens_total=total or None,
                cost_usd=cost,
                model_used=model or None,
            )
        except Exception as e:
            logger.debug("on_llm_end log failed (non-fatal): %s", e)

    def on_llm_error(self, error=None, *, run_id=None, **_kwargs) -> None:
        """Release provider correlation state for failed LLM runs."""
        del error
        run_id_str = self._run_id_str(run_id)
        if run_id_str:
            self._llm_providers.pop(run_id_str, None)
            self._llm_provider_types.pop(run_id_str, None)
            self._llm_models.pop(run_id_str, None)
