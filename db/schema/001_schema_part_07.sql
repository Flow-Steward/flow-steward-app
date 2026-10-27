-- Name: workflow_provider_triggers workflow_provider_triggers_trigger_scope_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX workflow_provider_triggers_trigger_scope_key ON public.workflow_provider_triggers USING btree (workflow_id, project_id, provider_key, trigger_type_id, COALESCE(event_name, ''::text));


--
-- Name: scheduled_jobs uq_scheduled_jobs_id_project_account; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT uq_scheduled_jobs_id_project_account UNIQUE (id, project_id, account_id);


--
-- Name: user_account_memberships user_account_memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_pkey PRIMARY KEY (id);


--
-- Name: user_account_memberships user_account_memberships_user_id_account_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_user_id_account_id_key UNIQUE (user_id, account_id);


--
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (id);


ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_role_id_key UNIQUE (user_id, role_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: workflow_reactions workflow_reactions_account_workflow_reaction_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_reactions
    ADD CONSTRAINT workflow_reactions_account_workflow_reaction_key_key UNIQUE (account_id, workflow_id, reaction_key);


--
-- Name: workflow_reactions workflow_reactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_reactions
    ADD CONSTRAINT workflow_reactions_pkey PRIMARY KEY (id);


--
-- Name: workflow_definition_snapshots workflow_definition_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definition_snapshots
    ADD CONSTRAINT workflow_definition_snapshots_pkey PRIMARY KEY (id);


--
-- Name: workflow_definition_snapshots workflow_definition_snapshots_identity_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_definition_snapshots
    ADD CONSTRAINT workflow_definition_snapshots_identity_key UNIQUE (account_id, workflow_registry_id, revision, digest);


--
-- Name: workflow_runtime_dependencies workflow_runtime_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_pkey PRIMARY KEY (dependency_id);


--
-- Name: workflow_runtime_dependencies workflow_runtime_dependencies_workflow_id_step_id_kind_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_workflow_id_step_id_kind_ref_key UNIQUE (workflow_id, step_id, dependency_kind, dependency_ref);


--
-- Name: checkpoint_blobs_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_blobs_thread_id_idx ON public.checkpoint_blobs USING btree (thread_id);


--
-- Name: checkpoint_writes_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoint_writes_thread_id_idx ON public.checkpoint_writes USING btree (thread_id);


--
-- Name: checkpoints_thread_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX checkpoints_thread_id_idx ON public.checkpoints USING btree (thread_id);


--
-- Name: idx_action_log_cost; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_action_log_cost ON public.platform_action_log USING btree (job_id, action_type) WHERE (action_type = 'llm_call'::text);


--
-- Name: idx_action_log_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_action_log_job_id ON public.platform_action_log USING btree (job_id, id);


--
-- Name: idx_agent_caps_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_caps_agent ON public.registry_agent_skills USING btree (agent_id);


--
-- Name: idx_agent_caps_cap; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_caps_cap ON public.registry_agent_skills USING btree (skill_id);


--
-- Name: idx_agent_inputs_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_inputs_agent ON public.registry_agent_inputs USING btree (agent_id);


--
-- Name: idx_agent_messages_corr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_messages_corr ON public.platform_agent_messages USING btree (correlation_id);


--
-- Name: idx_agent_messages_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_messages_job ON public.platform_agent_messages USING btree (job_id, id);


--
-- Name: idx_agent_messages_job_from_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_messages_job_from_agent ON public.platform_agent_messages USING btree (job_id, from_agent, id DESC);


--
-- Name: idx_agent_messages_job_step; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_messages_job_step ON public.platform_agent_messages USING btree (job_id, step_id, id DESC);


--
-- Name: idx_agent_messages_job_execution; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_messages_job_execution ON public.platform_agent_messages USING btree (job_id, step_execution_id, attempt);


--
-- Name: idx_agent_outputs_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_outputs_agent ON public.registry_agent_outputs USING btree (agent_id);


--
-- Name: idx_agent_tools_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tools_agent ON public.registry_agent_tools USING btree (agent_id);


--
-- Name: idx_agent_tools_tool; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_agent_tools_tool ON public.registry_agent_tools USING btree (tool_id);


--
-- Name: idx_auth_email_change_requests_new_email_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_email_change_requests_new_email_pending ON public.auth_email_change_requests USING btree (lower(new_email)) WHERE (used_at IS NULL);


--
-- Name: idx_auth_email_change_requests_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_email_change_requests_user_active ON public.auth_email_change_requests USING btree (user_id, expires_at DESC) WHERE (used_at IS NULL);


--
-- Name: idx_auth_rate_limits_locked_until; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_rate_limits_locked_until ON public.auth_rate_limits USING btree (locked_until) WHERE (locked_until IS NOT NULL);


--
-- Name: idx_auth_rate_limits_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_rate_limits_updated_at ON public.auth_rate_limits USING btree (updated_at);


--
-- Name: idx_auth_password_resets_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_password_resets_expires ON public.auth_password_resets USING btree (expires_at, used_at);


--
-- Name: idx_auth_password_resets_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_password_resets_user_active ON public.auth_password_resets USING btree (user_id, expires_at DESC) WHERE (used_at IS NULL);


--
-- Name: idx_auth_sessions_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_sessions_expiry ON public.auth_sessions USING btree (expires_at, revoked);


--
-- Name: idx_auth_sessions_user_account_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_sessions_user_account_active ON public.auth_sessions USING btree (user_id, account_id, expires_at DESC) WHERE (revoked = false);


--
-- Name: idx_auth_sessions_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_sessions_user_active ON public.auth_sessions USING btree (user_id, expires_at DESC) WHERE (revoked = false);


--
-- Name: idx_cap_inputs_cap; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cap_inputs_cap ON public.registry_skill_inputs USING btree (skill_id);


--
-- Name: idx_cap_outputs_cap; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cap_outputs_cap ON public.registry_skill_outputs USING btree (skill_id);


--
-- Name: idx_changelog_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_changelog_created ON public.registry_changelog USING btree (created_at DESC);


--
-- Name: idx_changelog_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_changelog_entity ON public.registry_changelog USING btree (entity_type, entity_id);


--
-- Name: registry_changelog_account_entity_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX registry_changelog_account_entity_idx ON public.registry_changelog USING btree (account_id, entity_type, entity_id);


--
-- Name: idx_command_audit_log_account_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_audit_log_account_created ON public.command_audit_log USING btree (account_id, created_at DESC);


--
-- Name: idx_command_audit_log_command_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_command_audit_log_command_id ON public.command_audit_log USING btree (command_id);


--
-- Name: idx_command_audit_log_dispatch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_audit_log_dispatch ON public.command_audit_log USING btree (dispatch_id, created_at DESC);


--
-- Name: idx_command_audit_log_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_command_audit_log_event ON public.command_audit_log USING btree (event_id, created_at DESC);


--
-- Name: idx_dashboard_job_facts_account_agent_finished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_agent_finished ON public.dashboard_job_facts USING btree (account_id, agent, finished_at DESC);


--
-- Name: idx_dashboard_job_facts_account_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_date ON public.dashboard_job_facts USING btree (account_id, finished_date DESC);


--
-- Name: idx_dashboard_job_facts_account_finished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_finished ON public.dashboard_job_facts USING btree (account_id, finished_at DESC);


--
-- Name: idx_dashboard_job_facts_account_project_finished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_project_finished ON public.dashboard_job_facts USING btree (account_id, project_name, finished_at DESC);


--
-- Name: idx_dashboard_job_facts_account_result_finished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_result_finished ON public.dashboard_job_facts USING btree (account_id, result, finished_at DESC);


--
-- Name: idx_dashboard_job_facts_account_workflow_finished; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_dashboard_job_facts_account_workflow_finished ON public.dashboard_job_facts USING btree (account_id, workflow, finished_at DESC);


--
-- Name: idx_domain_event_ledger_account_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_ledger_account_created ON public.domain_event_ledger USING btree (account_id, created_at DESC);


--
-- Name: idx_domain_event_ledger_event_type_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_ledger_event_type_time ON public.domain_event_ledger USING btree (event_type, occurred_at DESC, id DESC);


--
-- Name: idx_domain_event_ledger_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_ledger_scope ON public.domain_event_ledger USING btree (account_id, project_id, workflow_id, job_id, occurred_at DESC);


--
-- Name: idx_domain_event_ledger_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_ledger_status ON public.domain_event_ledger USING btree (status, occurred_at DESC);


--
-- Name: idx_domain_event_outbox_account_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_outbox_account_pending ON public.domain_event_outbox USING btree (account_id, status, available_at) WHERE (status = 'pending'::text);


--
-- Name: idx_domain_event_outbox_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_outbox_event ON public.domain_event_outbox USING btree (event_id, created_at DESC);


--
-- Name: idx_domain_event_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_domain_event_outbox_pending ON public.domain_event_outbox USING btree (status, available_at, created_at) WHERE (status = 'pending'::text);


--
-- Name: idx_job_dispatch_outbox_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_dispatch_outbox_pending ON public.job_dispatch_outbox USING btree (status, available_at, created_at) WHERE (status = 'pending'::text);

CREATE INDEX idx_job_dispatch_outbox_preparing ON public.job_dispatch_outbox USING btree (status, preparation_lease_expires_at, available_at) WHERE (status = ANY (ARRAY['preparing'::text, 'cleanup_pending'::text]));


--
-- Name: idx_enabled_extensions_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_enabled_extensions_enabled ON public.enabled_extensions USING btree (enabled, updated_at DESC);


--
-- Name: idx_extension_callback_registrations_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_callback_registrations_lookup ON public.extension_callback_registrations USING btree (extension_id, callback_id, project_id, enabled);


--
-- Name: idx_extension_callback_registrations_unique_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_extension_callback_registrations_unique_scope ON public.extension_callback_registrations USING btree (extension_id, callback_id, project_id, COALESCE(connection_id, ''::text));


--
-- Name: idx_extension_callback_deliveries_received; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_callback_deliveries_received ON public.extension_callback_deliveries USING btree (received_at DESC);


--
-- Name: idx_event_delivery_attempts_dispatch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_delivery_attempts_dispatch ON public.event_delivery_attempts USING btree (dispatch_id, attempt_no DESC, created_at DESC);


--
-- Name: idx_event_delivery_attempts_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_delivery_attempts_event ON public.event_delivery_attempts USING btree (event_id, created_at DESC, id DESC);


--
-- Name: idx_event_dispatches_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_dispatches_account_status ON public.event_dispatches USING btree (account_id, status, created_at DESC);


--
-- Name: idx_event_dispatches_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_dispatches_event ON public.event_dispatches USING btree (event_id, created_at DESC, id DESC);


--
-- Name: idx_event_dispatches_retry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_dispatches_retry ON public.event_dispatches USING btree (status, next_attempt_at, created_at) WHERE (status = ANY (ARRAY['queued'::text, 'dispatching'::text, 'failed'::text]));


--
-- Name: idx_event_retry_queue_unsent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_event_retry_queue_unsent ON public.platform_event_retry_queue USING btree (enqueued_at, id) WHERE (sent_at IS NULL);


--
-- Name: idx_events_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_events_job ON public.platform_events USING btree (job_id, id);


--
-- Name: idx_extension_bundles_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_bundles_kind ON public.extension_bundles USING btree (kind, status);


--
-- Name: idx_extension_invocation_logs_bundle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_invocation_logs_bundle ON public.extension_invocation_logs USING btree (bundle_id, created_at DESC);


--
-- Name: idx_extension_invocation_logs_job_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_invocation_logs_job_created ON public.extension_invocation_logs USING btree (job_id, created_at DESC, id DESC) WHERE (job_id IS NOT NULL);


--
-- Name: idx_extension_invocation_logs_workflow_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_invocation_logs_workflow_created ON public.extension_invocation_logs USING btree (workflow_id, created_at DESC, id DESC) WHERE (workflow_id IS NOT NULL);


--
-- Name: idx_extension_versions_bundle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_versions_bundle ON public.extension_versions USING btree (bundle_id, scanned_at DESC);


--
-- Name: idx_external_sources_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_external_sources_enabled ON public.external_sources USING btree (enabled, source_key);


--
-- Name: idx_generated_extension_jobs_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_generated_extension_jobs_account_status ON public.generated_extension_jobs USING btree (account_id, status, created_at DESC);


--
-- Name: idx_guardrail_agents_agent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guardrail_agents_agent ON public.registry_guardrail_agents USING btree (agent_id);


--
-- Name: idx_guardrails_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guardrails_enabled ON public.registry_guardrails USING btree (enabled) WHERE (enabled = true);


--
-- Name: idx_guardrails_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_guardrails_project ON public.registry_guardrails USING btree (project_id);


--
-- Name: idx_job_attachments_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_attachments_job_id ON public.job_attachments USING btree (job_id, upload_order, uploaded_at, id);


--
-- Name: idx_job_decision_overrides_job_active_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_decision_overrides_job_active_domain ON public.job_decision_overrides USING btree (job_id, domain, active, created_at DESC);


--
-- Name: idx_job_decision_overrides_job_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_decision_overrides_job_created ON public.job_decision_overrides USING btree (job_id, created_at DESC, id DESC);


--
-- Name: idx_job_errors_job_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_errors_job_created ON public.job_errors USING btree (job_id, created_at DESC, id DESC);


--
-- Name: idx_job_errors_job_source_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_errors_job_source_category ON public.job_errors USING btree (job_id, source, category, created_at DESC);


--
-- Name: idx_job_errors_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_errors_request_id ON public.job_errors USING btree (request_id) WHERE ((request_id IS NOT NULL) AND (length(TRIM(BOTH FROM request_id)) > 0));


--
-- Name: idx_job_external_links_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_external_links_job ON public.job_external_links USING btree (job_id, created_at DESC);


--
-- Name: idx_job_reaction_events_dedupe; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_job_reaction_events_dedupe ON public.job_reaction_events USING btree (dedupe_key) WHERE ((dedupe_key IS NOT NULL) AND (dedupe_key <> ''::text));


--
-- Name: idx_job_reaction_events_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_reaction_events_job ON public.job_reaction_events USING btree (job_id, created_at DESC) WHERE (job_id IS NOT NULL);


--
-- Name: idx_job_reaction_events_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_reaction_events_workflow ON public.job_reaction_events USING btree (workflow_id, event_type, created_at DESC) WHERE (workflow_id IS NOT NULL);


--
-- Name: idx_job_step_replays_job_corr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_step_replays_job_corr ON public.job_step_replays USING btree (job_id, correlation_id, created_at DESC) WHERE ((correlation_id IS NOT NULL) AND (length(TRIM(BOTH FROM correlation_id)) > 0));


--
-- Name: idx_job_step_replays_job_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_step_replays_job_created ON public.job_step_replays USING btree (job_id, created_at DESC, id DESC);


--
-- Name: idx_job_step_replays_job_message; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_job_step_replays_job_message ON public.job_step_replays USING btree (job_id, message_id, created_at DESC);


--
-- Name: idx_jobs_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_active ON public.jobs USING btree (id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jobs_invoke_cancel_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_invoke_cancel_pending ON public.jobs USING btree (id) WHERE (meta ? 'invoke_cancel_pending'::text);


--
-- Name: idx_jobs_meta_workflow_selection_reason; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_meta_workflow_selection_reason ON public.jobs USING btree (((meta ->> 'workflow_selection_reason'::text)));


--
-- Name: idx_jobs_meta_workflow_selection_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_meta_workflow_selection_source ON public.jobs USING btree (((meta ->> 'workflow_selection_source'::text)));


--
-- Name: idx_jobs_project_finished_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_project_finished_at ON public.jobs USING btree (project_id, finished_at DESC) WHERE (finished_at IS NOT NULL);


--
-- Name: idx_jobs_project_status_finished_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_project_status_finished_at ON public.jobs USING btree (project_id, status, finished_at DESC) WHERE (finished_at IS NOT NULL);


--
-- Name: idx_jobs_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_project_ts ON public.jobs USING btree (project_id, started_at DESC);
CREATE INDEX idx_jobs_workspace_project_ts ON public.jobs USING btree (account_id, tenant_id, workspace_id, project_id, started_at DESC);


--
-- Name: idx_jobs_queue; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_queue ON public.jobs USING btree (project_id, status, queued_at) WHERE (status = ANY (ARRAY['pending'::text, 'queued'::text]));


--
-- Name: idx_jobs_runtime_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_runtime_active ON public.jobs USING btree (status, heartbeat_at DESC) WHERE (status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'awaiting_approval'::text, 'awaiting_timer'::text, 'awaiting_child'::text]));


--
-- Name: idx_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_status ON public.jobs USING btree (status, started_at DESC);


--
-- Name: idx_jobs_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_task_id ON public.jobs USING btree (task_id, started_at DESC);


--
-- Name: idx_jobs_thread_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_thread_id ON public.jobs USING btree (thread_id);


--
-- Name: idx_jobs_workflow_wait_child; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_workflow_wait_child ON public.jobs USING btree (((meta -> 'workflow_wait'::text) ->> 'child_job_id'::text)) WHERE (status = 'awaiting_child'::text);


--
-- Name: idx_jobs_workflow_wait_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_workflow_wait_due ON public.jobs USING btree (((((meta -> 'workflow_wait'::text) ->> 'due_epoch_ms'::text))::bigint), id) WHERE (status = 'awaiting_timer'::text);


--
-- Name: idx_log_agent_action_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_agent_action_ts ON public.platform_action_log USING btree (agent, action_type, "timestamp" DESC);


--
-- Name: idx_log_failures_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_failures_project_ts ON public.platform_action_log USING btree (project_id, "timestamp" DESC) WHERE (status = ANY (ARRAY['failed'::text, 'error'::text]));


--
-- Name: idx_log_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_job_id ON public.platform_action_log USING btree (job_id, "timestamp");


--
-- Name: idx_log_lifecycle_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_lifecycle_ts ON public.platform_action_log USING btree (lifecycle_id, "timestamp");


--
