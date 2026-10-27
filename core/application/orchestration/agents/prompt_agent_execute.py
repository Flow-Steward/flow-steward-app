# ruff: noqa: F405, I001, RUF100
# mypy: ignore-errors
from .prompt_agent_common import *  # noqa: F403,F401  # NOSONAR


class PromptAgentExecutionMixin:
    """Main prompt execution path."""

    def execute(self, input: AgentInput) -> AgentOutput:
        state = input.state_ref or {}
        merged_state = {**state, **input.task_context}
        step_context = input.step_context or {}
        if step_context:
            merged_state.update(step_context)
        sec_project_id, sec_job_id = resolve_security_scope(merged_state)
        correlation_id = (
            str(merged_state.get("_message_correlation_id") or "").strip() or uuid.uuid4().hex
        )
        caused_by = merged_state.get("_message_caused_by")
        if isinstance(caused_by, list):
            caused_by = [dict(x) for x in caused_by if isinstance(x, dict)]
        else:
            caused_by = []

        skill = input.skill or (self.manifest.skills[0] if self.manifest.skills else "")
        step = step_context.get("_step") if step_context else None
        if self.skill_registry is None:
            raise ServiceScopeNotFoundError("Selected skill is unavailable in the current scope.")
        scope = merged_state.get("scope_context", merged_state)
        if not isinstance(scope, Mapping):
            raise ServiceScopeNotFoundError("Selected skill is unavailable in the current scope.")
        skill_manifest = input.selected_skill or self.skill_registry.resolve_assigned_skill(
            self.manifest,
            skill,
            scope=scope,
            project_id=str(merged_state.get("project_id") or "").strip() or None,
        )
        effective_tool_ids = self._get_effective_tool_ids(skill=skill, step=step)
        prompt_state = build_prompt_safe_state(merged_state)

        builder = PromptBuilder()
        system_prompt, user_prompt, prompt_version = builder.build(
            agent=self.manifest,
            skill=skill,
            step=step,
            tool_registry=self.tool_registry,
            state=prompt_state,
            tool_ids=effective_tool_ids,
            feedback=input.feedback,
            skill_manifest=skill_manifest,
            output_contract=input.output_contract,
        )
        # Resolve tools from registry to LangChain tool objects
        lc_tools = self._resolve_tools(merged_state, skill, tool_ids=effective_tool_ids)
        step_override = (
            step.get("override") if isinstance(step, dict) else getattr(step, "override", None)
        ) or {}
        if step_override.get("require_runtime_tools") and not lc_tools:
            summary = "Required read-only runtime tools are unavailable for this workflow step."
            emit_event(
                merged_state,
                self.manifest.agent_id,
                "validation_fail",
                summary,
                {
                    "reason": "required_tool_unavailable",
                    "required_tool_ids": list(effective_tool_ids),
                },
                severity="error",
            )
            return AgentOutput(
                status="blocked",
                artifacts={
                    "code": "required_tool_unavailable",
                    "required_tool_ids": list(effective_tool_ids),
                },
                summary=summary,
                confidence=0.0,
            )
        execution_policy = self._build_execution_policy_state(
            merged_state,
            tool_ids=effective_tool_ids,
            resolved_tools=lc_tools,
        )
        if execution_policy:
            merged_state["execution_policy"] = execution_policy
        if execution_policy and execution_policy.get("tool_resolution_warning"):
            system_prompt = self._strip_available_tools_section(system_prompt)
        emit_event(
            merged_state,
            self.manifest.agent_id,
            "tool_resolution",
            f"{self.manifest.display_name} resolved execution tools",
            {
                "skill": skill,
                "tool_policy_mode": execution_policy.get("tool_policy_mode")
                if execution_policy
                else "",
                "registry_tool_ids": list(effective_tool_ids),
                "resolved_tool_names": [
                    str(getattr(tool, "name", "") or "").strip()
                    for tool in lc_tools
                    if str(getattr(tool, "name", "") or "").strip()
                ],
                "resolved_count": len(lc_tools),
            },
        )

        # ── Inject tool configuration into user prompt ──────────────────────
        tool_config = self._build_tool_config(
            merged_state,
            tool_ids=effective_tool_ids,
            skill=skill,
        )
        if execution_policy and execution_policy.get("tool_resolution_warning"):
            tool_config = (
                f"{tool_config}\n\n{execution_policy['tool_resolution_warning']}"
                if tool_config
                else str(execution_policy["tool_resolution_warning"])
            )
        if tool_config:
            user_prompt = user_prompt + "\n\n" + tool_config

        # Build a minimal persona dict compatible with invoke_agent()
        persona = {
            "name": self.manifest.display_name,
            "system_prompt": system_prompt,
        }

        # Determine which context sections to include
        context_sections = self._get_context_sections(skill, input.feedback)

        # Max iterations from manifest config
        max_rounds = self.manifest.max_iterations
        timeout_seconds = 0
        try:
            raw_timeout_minutes = getattr(self.manifest, "timeout_minutes", None)
            if raw_timeout_minutes in (None, ""):
                raw_timeout_minutes = (getattr(self.manifest, "config", None) or {}).get(
                    "timeout_minutes", 0
                )
            timeout_minutes = float(raw_timeout_minutes or 0)
            if timeout_minutes > 0:
                timeout_seconds = max(1, int(timeout_minutes * 60))
        except (TypeError, ValueError):
            timeout_seconds = 0

        emit_event(
            merged_state,
            self.manifest.agent_id,
            "agent_started",
            f"{self.manifest.display_name} started",
            {"skill": skill, "prompt_version": prompt_version},
        )

        # All shipped providers execute through the canonical API-backed path.
        provider = _resolve_provider(
            merged_state.get("project_config", {}),
            self.manifest.agent_id,
        )
        self._log_prompt_request(
            merged_state,
            skill=skill,
            prompt_version=prompt_version,
            provider=provider,
            correlation_id=correlation_id,
            caused_by=caused_by,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        try:
            self._assert_prompt_guard(merged_state, system_prompt, user_prompt)
            response = invoke_agent(
                state=merged_state,
                persona=persona,
                platform_config={},
                task_input=user_prompt,
                agent_name=self.manifest.agent_id,
                tools=lc_tools,
                context_sections=context_sections,
                max_tool_rounds=max_rounds,
                timeout_seconds=timeout_seconds,
                final_response_schema=self._resolve_final_response_schema(
                    skill, step=step, state=merged_state, output_contract=input.output_contract
                ),
            )
            response_text = model_response_text(response)
            self._log_prompt_response(
                merged_state,
                skill=skill,
                provider=provider,
                correlation_id=correlation_id,
                response_text=response_text,
            )
        except Exception as e:
            error_meta = self._extract_execution_error_metadata(
                e, provider_hint=str(provider or "")
            )
            error_text = self._format_execution_error(e, diagnostics=error_meta)
            safe_error = redact_text_for_persistence(
                error_text,
                project_id=sec_project_id,
                job_id=sec_job_id,
                context_type="display",
            )
            self._log_prompt_response(
                merged_state,
                skill=skill,
                provider=provider,
                correlation_id=correlation_id,
                response_text="",
                error=safe_error,
            )
            logger.exception("PromptAgent[%s] failed: %s", self.manifest.agent_id, safe_error)
            emit_event(
                merged_state,
                self.manifest.agent_id,
                "error",
                f"{self.manifest.display_name} failed",
                {"error": safe_error},
                severity="error",
            )
            artifacts = {
                "error": safe_error,
                "error_type": e.__class__.__name__,
                "_execution_error": True,
                "raw_output": "",
            }
            host_retry_signal = None
            from core.application.orchestration.retry_policy import (
                OperationRetrySignalError,
                normalize_retry_signal,
            )

            if isinstance(e, OperationRetrySignalError):
                retry_signal = normalize_retry_signal(e.retry_signal)
                try:
                    operation_deadline = float(
                        getattr(e, "operation_deadline_epoch_seconds", 0.0) or 0.0
                    )
                except (TypeError, ValueError):
                    operation_deadline = 0.0
                host_retry_signal = {
                    "schema_version": "operation_retry_signal_v1",
                    "provenance": "agent_managed_custom_http",
                    "signal": retry_signal,
                }
                if operation_deadline > 0:
                    host_retry_signal["deadline_epoch_seconds"] = operation_deadline
            else:
                for key in (
                    "provider_name",
                    "http_status",
                    "provider_error_code",
                    "provider_error_type",
                    "request_id",
                    "retryable",
                    "response_excerpt",
                ):
                    value = error_meta.get(key)
                    if value in (None, ""):
                        continue
                    artifacts[key] = value
            return AgentOutput(
                status="fail",
                artifacts=artifacts,
                summary=f"{self.manifest.display_name} failed: {safe_error}",
                confidence=0.0,
                operation_retry_signal=host_retry_signal,
            )

        # ── Scan outbound: redact secrets/PII from LLM response ─────────────
        try:
            from core.infrastructure.security.integration import scan_outbound

            _sec = scan_outbound(
                response_text,
                job_id=sec_job_id,
                project_id=sec_project_id,
            )
            if _sec.action == "block":
                logger.warning(
                    "PromptAgent[%s] outbound response BLOCKED by security policy",
                    self.manifest.agent_id,
                )
                response_text = "[BLOCKED: agent response rejected by security policy]"
            elif _sec.redacted_text is not None:
                response_text = _sec.redacted_text
        except Exception:
            pass

        # ── Parse artifacts and extract verdicts ────────────────────────────
        artifacts = self._parse_artifacts(response_text, skill)
        artifacts["raw_output"] = response_text

        # Extract verdicts declared by the output contract
        verdict_data = self._extract_verdict(response_text, skill)
        artifacts.update(verdict_data)

        # ── Contract validation ───────────────────────────────────────────
        contract_errors = (
            input.output_contract.validate(artifacts)
            if input.output_contract is not None
            else self.validate_agent_output(skill, artifacts)
        )
        if contract_errors:
            logger.warning(
                "PromptAgent[%s] contract validation: %d errors for '%s': %s",
                self.manifest.agent_id,
                len(contract_errors),
                skill,
                "; ".join(contract_errors),
            )
            artifacts["_contract_errors"] = contract_errors

        # ── Determine final status from generic contract validation ─────────
        status = "pass"
        confidence = 0.85
        if contract_errors:
            status = "fail"
            confidence = 0.4

        emit_event(
            merged_state,
            self.manifest.agent_id,
            "agent_completed",
            f"{self.manifest.display_name} completed ({status})",
            {
                "output_length": len(response_text),
                "status": status,
                "skill": skill,
                "contract_errors": len(contract_errors) if contract_errors else 0,
            },
        )

        phase_result_v2, schema_errors = self._build_phase_result_v2(
            response_text=response_text,
            artifacts=artifacts,
            status=status,
            confidence=confidence,
            contract_errors=contract_errors,
        )

        if schema_errors:
            artifacts["_schema_errors"] = schema_errors
            status = "fail"
            confidence = min(confidence, 0.2)
            logger.warning(
                "PromptAgent[%s] phase_result_v2 schema invalid (%d errors): %s",
                self.manifest.agent_id,
                len(schema_errors),
                "; ".join(schema_errors),
            )
        else:
            status = normalize_phase_status(phase_result_v2.get("status"), default=status)
            with contextlib.suppress(TypeError, ValueError):
                confidence = float(phase_result_v2.get("confidence", confidence))

        final_artifacts = phase_result_v2.get("artifacts")
        if not isinstance(final_artifacts, dict):
            final_artifacts = dict(artifacts or {})
        else:
            final_artifacts = dict(final_artifacts)
        if schema_errors:
            final_artifacts["_schema_errors"] = schema_errors

        return AgentOutput(
            status=status,
            artifacts=final_artifacts,
            summary=str(phase_result_v2.get("summary") or response_text[:500] or COMPLETED_PREFIX),
            confidence=confidence,
        )


__all__ = [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
