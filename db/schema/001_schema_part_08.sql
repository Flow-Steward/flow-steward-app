-- Name: idx_log_project_task_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_project_task_ts ON public.platform_action_log USING btree (project_id, task_id, "timestamp" DESC);


--
-- Name: idx_log_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_project_ts ON public.platform_action_log USING btree (project_id, "timestamp" DESC);


--
-- Name: idx_machine_tokens_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_machine_tokens_lookup ON public.machine_tokens USING btree (token_hash) WHERE (revoked = false);


--
-- Name: idx_machine_tokens_service; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_machine_tokens_service ON public.machine_tokens USING btree (service_account_id, revoked, expires_at);

CREATE UNIQUE INDEX ux_external_effects_account_scope_key ON public.external_effects USING btree (account_id, idempotency_scope_hash, idempotency_key);
CREATE INDEX idx_external_effects_project_status ON public.external_effects USING btree (project_id, status, created_at DESC) WHERE (project_id IS NOT NULL);
CREATE INDEX idx_external_effects_extension_operation ON public.external_effects USING btree (account_id, extension_id, operation_id, created_at DESC);
CREATE INDEX idx_external_effects_cleanup ON public.external_effects USING btree (status, expires_at) WHERE (status = ANY (ARRAY['claimed'::text, 'running'::text]));
CREATE INDEX idx_external_effects_claim_token ON public.external_effects USING btree (effect_id, claim_token);
CREATE INDEX idx_external_effect_replays_account_state ON public.external_effect_replays USING btree (account_id, replay_state, updated_at);
CREATE INDEX idx_external_effect_output_reconciliations_sweep ON public.external_effect_output_reconciliations USING btree (updated_at, account_id, effect_id, claim_version);
CREATE INDEX idx_external_effect_maintenance_cursors_attempt ON public.external_effect_maintenance_cursors USING btree (last_attempted_at, account_id);
CREATE INDEX idx_external_effect_rate_windows_cleanup ON public.external_effect_rate_windows USING btree (window_started_at);
CREATE INDEX idx_extension_invocation_logs_external_effect ON public.extension_invocation_logs USING btree (external_effect_id) WHERE (external_effect_id IS NOT NULL);
CREATE INDEX idx_notification_delivery_logs_external_effect ON public.notification_delivery_logs USING btree (external_effect_id) WHERE (external_effect_id IS NOT NULL);
CREATE INDEX idx_event_delivery_attempts_external_effect ON public.event_delivery_attempts USING btree (external_effect_id) WHERE (external_effect_id IS NOT NULL);

CREATE UNIQUE INDEX ux_extension_project_state_install_scope_key ON public.extension_project_state USING btree (account_id, project_id, extension_id, COALESCE(extension_install_id, ''::text), namespace, state_key, COALESCE(connection_id, ''::text));
CREATE INDEX idx_extension_project_state_install ON public.extension_project_state USING btree (extension_install_id, account_id, project_id) WHERE (extension_install_id IS NOT NULL);
CREATE INDEX idx_extension_project_state_list ON public.extension_project_state USING btree (account_id, project_id, extension_id, namespace, state_key);
CREATE INDEX idx_extension_project_state_expiry ON public.extension_project_state USING btree (expires_at) WHERE (expires_at IS NOT NULL);
CREATE INDEX idx_mcp_connector_oauth_states_connector ON public.mcp_connector_oauth_states USING btree (connector_id, expires_at);
CREATE INDEX idx_mcp_connector_oauth_states_expiry ON public.mcp_connector_oauth_states USING btree (expires_at) WHERE (consumed_at IS NULL);


--
-- Name: idx_mcp_connectors_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mcp_connectors_account ON public.mcp_connectors USING btree (account_id, enabled);

CREATE INDEX idx_mcp_connectors_account_live ON public.mcp_connectors USING btree (account_id, enabled) WHERE (deleted_at IS NULL);

CREATE UNIQUE INDEX ux_mcp_connectors_scope_identity
    ON public.mcp_connectors USING btree (connector_id, project_id, tenant_id, account_id);

CREATE INDEX idx_mcp_connectors_project_scope
    ON public.mcp_connectors USING btree (tenant_id, account_id, project_id, enabled)
    WHERE (deleted_at IS NULL);

CREATE INDEX idx_mcp_connector_oauth_states_project_scope
    ON public.mcp_connector_oauth_states USING btree (tenant_id, account_id, project_id, connector_id, expires_at);

CREATE INDEX idx_secret_refs_mcp_project_scope
    ON public.secret_refs USING btree (tenant_id, account_id, project_id, ref_key)
    WHERE starts_with(ref_key, 'mcp_connector:'::text);


--
-- Name: idx_notification_delivery_logs_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_delivery_logs_event ON public.notification_delivery_logs USING btree (event_type, severity, created_at DESC);


--
-- Name: idx_notification_delivery_logs_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_delivery_logs_job ON public.notification_delivery_logs USING btree (job_id, created_at DESC) WHERE (job_id IS NOT NULL);


--
-- Name: idx_notification_delivery_logs_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_delivery_logs_project ON public.notification_delivery_logs USING btree (project_id, created_at DESC);


--
-- Name: idx_notification_routes_account_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_routes_account_project ON public.notification_routes USING btree (account_id, project_id, enabled, event_type, severity);


--
-- Name: idx_notification_routes_project_match; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_routes_project_match ON public.notification_routes USING btree (project_id, enabled, event_type, severity);


--
-- Name: idx_notification_routes_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_notification_routes_workflow ON public.notification_routes USING btree (workflow_id, enabled) WHERE (workflow_id IS NOT NULL);


--
-- Name: idx_extension_artifact_snapshots_release; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_artifact_snapshots_release ON public.extension_artifact_snapshots USING btree (release_id, artifact_kind, artifact_id);


--
-- Name: idx_extension_entitlements_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_entitlements_account_status ON public.extension_entitlements USING btree (account_id, status, updated_at DESC);


--
-- Name: idx_extension_entitlements_install_status_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_entitlements_install_status_updated ON public.extension_entitlements USING btree (install_id, status, updated_at DESC);


--
-- Name: idx_extension_installs_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_account ON public.extension_installs USING btree (account_id, state);


--
-- Name: idx_extension_installs_account_filter_matrix; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_account_filter_matrix ON public.extension_installs USING btree (account_id, state, visibility, runtime_mode, runtime_status, health_status, handshake_status);


--
-- Name: idx_extension_installs_origin_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_origin_source ON public.extension_installs USING btree (account_id, descriptor_origin, descriptor_source);


--
-- Name: idx_extension_installs_release_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_release_id ON public.extension_installs USING btree (release_id);


--
-- Name: idx_extension_installs_runtime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_runtime ON public.extension_installs USING btree (runtime_mode, runtime_status, health_status);


--
-- Name: idx_extension_installs_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_installs_visibility ON public.extension_installs USING btree (visibility, state);


--
-- Name: idx_llm_provider_sources_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_provider_sources_account ON public.llm_provider_sources USING btree (account_id, enabled);


--
-- Name: idx_llm_provider_sources_install; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_provider_sources_install ON public.llm_provider_sources USING btree (account_id, extension_install_id);


--
-- Name: idx_llm_provider_models_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_provider_models_account ON public.llm_provider_models USING btree (account_id, enabled);


--
-- Name: idx_llm_provider_models_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_provider_models_source ON public.llm_provider_models USING btree (provider_source_id);


--
-- Name: idx_llm_provider_models_pending_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_llm_provider_models_pending_generation ON public.llm_provider_models USING btree (account_id, verification_generation) WHERE ((verification_status = 'pending'::text) AND (verification_generation IS NOT NULL));


--
-- Name: idx_llm_provider_models_routable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_llm_provider_models_routable ON public.llm_provider_models USING btree (account_id, provider_source_id, model_id) WHERE ((enabled = true) AND (verification_status = 'passed'::text) AND (lifecycle = 'active'::text));


--
-- Name: idx_model_providers_pending_generation; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_model_providers_pending_generation ON public.model_providers USING btree (account_id, verification_generation) WHERE ((verification_status = 'pending'::text) AND (verification_generation IS NOT NULL));


--
-- Name: idx_model_providers_routable; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_model_providers_routable ON public.model_providers USING btree (account_id, id) WHERE ((enabled = true) AND (verification_status = 'passed'::text) AND (lifecycle = 'active'::text));


--
-- Name: idx_extension_registry_projections_account_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_registry_projections_account_active ON public.extension_registry_projections USING btree (account_id, is_active, artifact_kind);


--
-- Name: idx_extension_registry_projections_availability; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_registry_projections_availability ON public.extension_registry_projections USING btree (account_id, artifact_kind, visible_in_catalog, new_runs_blocked);


--
-- Name: idx_extension_registry_projections_release; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_registry_projections_release ON public.extension_registry_projections USING btree (release_id, artifact_kind, artifact_id);


--
-- Name: idx_extension_releases_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_releases_account ON public.extension_releases USING btree (account_id, created_at DESC);


--
-- Name: idx_extension_releases_descriptor_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_releases_descriptor_gin ON public.extension_releases USING gin (descriptor_jsonb);


--
-- Name: idx_extension_releases_descriptor_sha256; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_releases_descriptor_sha256 ON public.extension_releases USING btree (descriptor_sha256);


--
-- Name: idx_extension_releases_marketplace_channel_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_releases_marketplace_channel_source ON public.extension_releases USING btree (channel, source_kind, created_at DESC);


--
-- Name: idx_extension_releases_extension; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_releases_extension ON public.extension_releases USING btree (extension_id, created_at DESC);


--
-- Name: idx_extension_usage_events_account_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_usage_events_account_kind ON public.extension_usage_events USING btree (account_id, event_kind, occurred_at DESC);


--
-- Name: idx_extension_usage_events_install_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_usage_events_install_time ON public.extension_usage_events USING btree (install_id, occurred_at DESC);


--
-- Name: idx_pat_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pat_lookup ON public.personal_access_tokens USING btree (token_hash) WHERE (revoked = false);


--
-- Name: idx_pat_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pat_user ON public.personal_access_tokens USING btree (user_id, revoked);


--
-- Name: idx_workflow_steps_owner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_steps_owner ON public.registry_workflow_steps USING btree (owner_workflow_id, sort_order);


--
-- Name: idx_workflow_steps_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_steps_group ON public.registry_workflow_steps USING btree (owner_workflow_id, group_id) WHERE (group_id IS NOT NULL);


--
-- Name: idx_workflow_steps_for_each; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_steps_for_each ON public.registry_workflow_steps USING btree (owner_workflow_id, for_each_id) WHERE (for_each_id IS NOT NULL);


--
-- Name: idx_project_connections_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_account ON public.project_connections USING btree (account_id, connection_kind);


--
-- Name: idx_project_connections_extension; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_extension ON public.project_connections USING btree (extension_id, account_id);


--
-- Name: idx_project_connections_extension_install_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_extension_install_lookup ON public.project_connections USING btree (account_id, connection_kind, extension_install_id, enabled);


--
-- Name: idx_project_connections_extension_runtime; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_extension_runtime ON public.project_connections USING btree (project_id, connection_kind, runtime_mode, health_status, runtime_status);


--
-- Name: idx_project_connections_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_project ON public.project_connections USING btree (project_id, enabled);


--
-- Name: idx_extension_oauth_configurations_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_extension_oauth_configurations_scope ON public.extension_oauth_configurations USING btree (tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id, created_at, configuration_id);

CREATE INDEX idx_extension_oauth_configurations_project_scope ON public.extension_oauth_configurations USING btree (project_id, tenant_id, account_id);

CREATE INDEX idx_extension_oauth_configurations_install ON public.extension_oauth_configurations USING btree (extension_install_id, account_id, extension_id);

CREATE INDEX idx_extension_oauth_configurations_release ON public.extension_oauth_configurations USING btree (extension_release_id, extension_id);

CREATE INDEX idx_extension_oauth_authorization_states_scope ON public.extension_oauth_authorization_states USING btree (tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id, configuration_id, state_hash);

CREATE INDEX idx_extension_oauth_authorization_states_configuration_scope ON public.extension_oauth_authorization_states USING btree (configuration_id, tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id);

CREATE INDEX idx_extension_oauth_authorization_states_release ON public.extension_oauth_authorization_states USING btree (extension_release_id, extension_id);

CREATE INDEX idx_extension_oauth_authorization_states_expiry ON public.extension_oauth_authorization_states USING btree (expires_at) WHERE (consumed_at IS NULL);

CREATE INDEX idx_extension_oauth_authorization_states_existing_connection ON public.extension_oauth_authorization_states USING btree (existing_connection_id, configuration_id) WHERE (existing_connection_id IS NOT NULL);

CREATE INDEX idx_project_connections_oauth_configuration ON public.project_connections USING btree (oauth_configuration_id, account_id, project_id, extension_install_id, extension_id, oauth_provider_id) WHERE (oauth_configuration_id IS NOT NULL);

CREATE INDEX idx_project_connections_oauth_release ON public.project_connections USING btree (oauth_extension_release_id, extension_id) WHERE (oauth_configuration_id IS NOT NULL);


--
-- Name: idx_project_connections_project_display; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_project_display ON public.project_connections USING btree (project_id, display_name);


--
-- Name: idx_project_connections_project_extension_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_project_extension_type ON public.project_connections USING btree (project_id, extension_id, connection_type, enabled);


--
-- Name: idx_project_connections_project_install_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connections_project_install_type ON public.project_connections USING btree (project_id, extension_install_id, connection_type, enabled) WHERE (extension_install_id IS NOT NULL);


--
-- Name: idx_project_connection_oauth_states_connection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connection_oauth_states_connection ON public.project_connection_oauth_states USING btree (connection_id, expires_at);


--
-- Name: idx_project_connection_oauth_states_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connection_oauth_states_expiry ON public.project_connection_oauth_states USING btree (expires_at) WHERE (consumed_at IS NULL);


--
-- Name: idx_project_connection_sessions_lease; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connection_sessions_lease ON public.project_connection_sessions USING btree (lease_expires_at) WHERE (lease_owner IS NOT NULL);


--
-- Name: idx_project_connection_sessions_valid_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_connection_sessions_valid_lookup ON public.project_connection_sessions USING btree (connection_id, account_id, project_id, scope_hash, subject_key, credential_revision, expires_at);


--
-- Name: idx_project_execution_leases_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_execution_leases_expires ON public.project_execution_leases USING btree (expires_at);


--
-- Name: idx_project_execution_leases_worker; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_execution_leases_worker ON public.project_execution_leases USING btree (worker_instance_id, expires_at DESC);


--
-- Name: idx_project_execution_slots_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_execution_slots_expires ON public.project_execution_slots USING btree (expires_at);


--
-- Name: idx_project_execution_slots_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_execution_slots_project ON public.project_execution_slots USING btree (project_id, expires_at DESC);


--
-- Name: idx_project_execution_slots_worker; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_execution_slots_worker ON public.project_execution_slots USING btree (worker_instance_id, expires_at DESC);


--
-- Name: idx_project_secrets_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_secrets_lookup ON public.project_secrets USING btree (project_id, secret_key) WHERE (active = true);


--
-- Name: idx_project_settings_revisions_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_settings_revisions_project_created ON public.project_settings_revisions USING btree (project_id, created_at DESC, id DESC);


--
-- Name: idx_project_workflows_one_active_default; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_project_workflows_one_active_default ON public.project_workflows USING btree (project_id) WHERE ((is_default = true) AND (enabled = true) AND (deleted_at IS NULL));


--
-- Name: idx_project_workflows_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_workflows_project ON public.project_workflows USING btree (project_id) WHERE ((enabled = true) AND (deleted_at IS NULL));


--
-- Name: idx_project_workflows_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_workflows_workflow ON public.project_workflows USING btree (workflow_id) WHERE ((enabled = true) AND (deleted_at IS NULL));


--
-- Name: idx_project_workflows_project_workflow_id_pattern_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_workflows_project_workflow_id_pattern_enabled ON public.project_workflows USING btree (project_id, workflow_id text_pattern_ops) WHERE (enabled = true);


--
-- Name: idx_projects_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_account ON public.projects USING btree (account_id, id);
CREATE INDEX idx_projects_workspace ON public.projects USING btree (tenant_id, account_id, workspace_id, name);
CREATE INDEX idx_workspaces_account_status ON public.workspaces USING btree (tenant_id, account_id, status, updated_at DESC);


--
-- Name: idx_projects_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_projects_active ON public.projects USING btree (id) WHERE (deleted_at IS NULL);


--
-- Name: idx_provider_health_events_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_health_events_project ON public.provider_health_events USING btree (project_id, created_at DESC) WHERE (project_id IS NOT NULL);


--
-- Name: idx_provider_health_events_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_health_events_provider ON public.provider_health_events USING btree (provider_id, created_at DESC);


--
-- Name: idx_provider_secrets_lookup; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_secrets_lookup ON public.provider_secrets USING btree (provider_id, project_id, secret_key) WHERE (active = true);


--
-- Name: idx_providers_type_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_providers_type_enabled ON public.providers USING btree (provider_type_id, provider_key) WHERE (enabled = true);


--
-- Name: idx_registry_agents_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_agents_active ON public.registry_agents USING btree (agent_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_agents_agent_type_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_agents_agent_type_id ON public.registry_agents USING btree (agent_type_id);


--
-- Name: idx_registry_data_contracts_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_data_contracts_active ON public.registry_data_contracts USING btree (contract_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_data_contracts_projection_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_data_contracts_projection_mode ON public.registry_data_contracts USING btree (projection_mode, sync_status);


--
-- Name: idx_registry_datasets_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_datasets_active ON public.registry_datasets USING btree (dataset_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_datasets_connector; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_datasets_connector ON public.registry_datasets USING btree (connector_kind, connector_id, enabled);


--
-- Name: idx_project_datasets_project; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_project_datasets_project ON public.project_datasets USING btree (project_id, dataset_id);


--
-- Name: idx_registry_fields_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_fields_enabled ON public.registry_fields USING btree (enabled);


--
-- Name: idx_registry_fields_projection_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_fields_projection_mode ON public.registry_fields USING btree (projection_mode, sync_status);


--
-- Name: idx_registry_fields_value_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_fields_value_type ON public.registry_fields USING btree (value_type);


--
-- Name: idx_registry_guardrails_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_guardrails_active ON public.registry_guardrails USING btree (id) WHERE (deleted_at IS NULL);


--
-- Name: registry_guardrails_account_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX registry_guardrails_account_id_idx ON public.registry_guardrails USING btree (account_id);


--
-- Name: registry_guardrails_account_project_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX registry_guardrails_account_project_idx ON public.registry_guardrails USING btree (account_id, project_id);


--
-- Name: idx_registry_pub_block_inputs_block; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_block_inputs_block ON public.registry_published_block_inputs USING btree (block_key, sort_order);


--
-- Name: idx_registry_pub_block_inputs_field; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_block_inputs_field ON public.registry_published_block_inputs USING btree (field_id);


--
-- Name: idx_registry_pub_block_outputs_block; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_block_outputs_block ON public.registry_published_block_outputs USING btree (block_key, sort_order);


--
-- Name: idx_registry_pub_block_outputs_field; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_block_outputs_field ON public.registry_published_block_outputs USING btree (field_id);


--
-- Name: idx_registry_pub_blocks_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_enabled ON public.registry_published_block_contracts USING btree (enabled);


--
-- Name: idx_registry_pub_blocks_enabled_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_enabled_visibility ON public.registry_published_block_contracts USING btree (enabled, visible_in_catalog, new_runs_blocked);


--
-- Name: idx_registry_pub_blocks_install_availability; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_install_availability ON public.registry_published_block_contracts USING btree (source_install_id, availability_status);


--
-- Name: idx_registry_pub_blocks_projection_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_projection_mode ON public.registry_published_block_contracts USING btree (projection_mode, sync_status);


--
-- Name: idx_registry_pub_blocks_scope_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_scope_visibility ON public.registry_published_block_contracts USING btree (account_id, scope_kind, visible_in_catalog, new_runs_blocked);


--
-- Name: idx_registry_pub_blocks_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_pub_blocks_source ON public.registry_published_block_contracts USING btree (source_kind, source_ref);


--
-- Name: idx_registry_skill_inputs_contract_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_skill_inputs_contract_id ON public.registry_skill_inputs USING btree (contract_id);


--
-- Name: idx_registry_skill_outputs_contract_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_skill_outputs_contract_id ON public.registry_skill_outputs USING btree (contract_id);


--
-- Name: idx_registry_skills_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_skills_active ON public.registry_skills USING btree (skill_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_tools_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_tools_active ON public.registry_tools USING btree (tool_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_workflows_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_workflows_active ON public.registry_workflows USING btree (workflow_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_workflows_active_workflow_id_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_workflows_active_workflow_id_pattern ON public.registry_workflows USING btree (workflow_id text_pattern_ops) WHERE (deleted_at IS NULL);


--
-- Name: idx_registry_workflows_enabled_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_workflows_enabled_visibility ON public.registry_workflows USING btree (enabled, visible_in_catalog, new_runs_blocked);


--
-- Name: idx_registry_workflows_install_availability; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_workflows_install_availability ON public.registry_workflows USING btree (source_install_id, availability_status);


--
-- Name: idx_registry_workflows_scope_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_registry_workflows_scope_visibility ON public.registry_workflows USING btree (account_id, scope_kind, visible_in_catalog, new_runs_blocked);


--
-- Name: idx_report_runs_account_tenant_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_report_runs_account_tenant_created ON public.report_runs USING btree (account_id, tenant_id, created_at DESC);


--
-- Name: idx_report_runs_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_report_runs_project_created ON public.report_runs USING btree (project_id, created_at DESC) WHERE (project_id IS NOT NULL);


--
-- Name: idx_report_runs_type_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_report_runs_type_created ON public.report_runs USING btree (report_type, created_at DESC);


--
-- Name: idx_scheduled_job_execution_locks_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_execution_locks_expires ON public.scheduled_job_execution_locks USING btree (expires_at) WHERE ((released_at IS NULL) AND (expires_at IS NOT NULL));


--
-- Name: idx_scheduled_job_execution_locks_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_execution_locks_job ON public.scheduled_job_execution_locks USING btree (job_id) WHERE (released_at IS NULL);


--
-- Name: idx_scheduled_job_execution_locks_project_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_execution_locks_project_active ON public.scheduled_job_execution_locks USING btree (project_id, acquired_at DESC) WHERE (released_at IS NULL);


--
-- Name: idx_scheduled_job_runs_account_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_runs_account_project_created ON public.scheduled_job_runs USING btree (account_id, project_id, created_at DESC);


--
-- Name: idx_scheduled_job_runs_job_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_runs_job_created ON public.scheduled_job_runs USING btree (scheduled_job_id, created_at DESC);


--
-- Name: idx_scheduled_job_runs_project_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_job_runs_project_created ON public.scheduled_job_runs USING btree (project_id, created_at DESC);


--
-- Name: idx_scheduled_jobs_account_project_enabled_next; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_jobs_account_project_enabled_next ON public.scheduled_jobs USING btree (account_id, project_id, enabled, next_run_at);


--
-- Name: idx_scheduled_jobs_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_jobs_due ON public.scheduled_jobs USING btree (enabled, status, next_run_at) WHERE (enabled = true);


--
-- Name: idx_scheduled_jobs_project_enabled_next; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_scheduled_jobs_project_enabled_next ON public.scheduled_jobs USING btree (project_id, enabled, next_run_at);


--
-- Name: idx_security_events_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_account ON public.security_events USING btree (account_id, created_at DESC);


--
-- Name: idx_security_events_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_job ON public.security_events USING btree (job_id) WHERE (job_id IS NOT NULL);


--
-- Name: idx_security_events_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_project_ts ON public.security_events USING btree (project_id, created_at DESC);


--
-- Name: idx_security_events_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_events_severity ON public.security_events USING btree (severity, created_at DESC);


--
-- Name: idx_security_findings_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_findings_event ON public.security_findings USING btree (event_id);


--
-- Name: idx_security_findings_scanner; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_findings_scanner ON public.security_findings USING btree (scanner, severity);


--
-- Name: idx_provider_webhook_deliveries_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_webhook_deliveries_job ON public.provider_webhook_deliveries USING btree (job_id) WHERE (job_id IS NOT NULL);


--
-- Name: idx_provider_webhook_deliveries_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_provider_webhook_deliveries_status ON public.provider_webhook_deliveries USING btree (status, updated_at);


--
-- Name: idx_security_posture_project_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_security_posture_project_ts ON public.security_posture_snapshots USING btree (project_id, created_at DESC);


--
-- Name: idx_service_accounts_account_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_service_accounts_account_active ON public.service_accounts USING btree (account_id, is_active, service_account_key);


--
-- Name: idx_system_settings_account_updated_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_system_settings_account_updated_at ON public.system_settings USING btree (account_id, updated_at DESC);


--
-- Name: idx_request_idempotency_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_request_idempotency_job ON public.request_idempotency USING btree (job_id) WHERE (job_id IS NOT NULL);


--
-- Name: idx_request_idempotency_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_request_idempotency_expiry ON public.request_idempotency USING btree (tombstone_expires_at) WHERE (tombstone_expires_at IS NOT NULL);


--
-- Name: idx_request_idempotency_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_request_idempotency_status ON public.request_idempotency USING btree (status, updated_at);


--
-- Name: idx_workflow_entrypoints_account_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_entrypoints_account_workflow ON public.workflow_entrypoints USING btree (account_id, workflow_id, enabled);


--
-- Name: idx_workflow_entrypoints_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_entrypoints_workflow ON public.workflow_entrypoints USING btree (workflow_id, enabled);


--
-- Name: idx_workflow_provider_triggers_provider; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_provider_triggers_provider ON public.workflow_provider_triggers USING btree (provider_key, enabled, event_name);


--
-- Name: idx_workflow_provider_triggers_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_provider_triggers_workflow ON public.workflow_provider_triggers USING btree (workflow_id, enabled);


--
-- Name: idx_user_account_memberships_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_account_memberships_account ON public.user_account_memberships USING btree (account_id, user_id);


--
-- Name: idx_user_account_memberships_default; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_user_account_memberships_default ON public.user_account_memberships USING btree (user_id) WHERE (is_default = true);


--
-- Name: idx_users_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_account_id ON public.users USING btree (account_id);


--
-- Name: idx_users_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_active ON public.users USING btree (id) WHERE (deleted_at IS NULL);


--
-- Name: idx_users_timezone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_timezone ON public.users USING btree (timezone) WHERE (timezone IS NOT NULL);


--
-- Name: idx_wf_groups_wf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wf_groups_wf ON public.registry_workflow_groups USING btree (workflow_id, sort_order);


--
-- Name: idx_wf_for_each_wf; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wf_for_each_wf ON public.registry_workflow_for_each USING btree (workflow_id, sort_order);


--
-- Name: idx_wf_hook_binding_revisions_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_wf_hook_binding_revisions_target ON public.registry_workflow_hook_binding_revisions USING btree (account_id, target_workflow_id) WHERE (target_workflow_id IS NOT NULL);


--
