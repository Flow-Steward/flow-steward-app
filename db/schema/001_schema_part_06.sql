-- Name: extension_bundles extension_bundles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_bundles
    ADD CONSTRAINT extension_bundles_pkey PRIMARY KEY (id);


--
-- Name: extension_invocation_logs extension_invocation_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_pkey PRIMARY KEY (id);


--
-- Name: extension_versions extension_versions_bundle_id_version_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_versions
    ADD CONSTRAINT extension_versions_bundle_id_version_key UNIQUE (bundle_id, version);


--
-- Name: extension_versions extension_versions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_versions
    ADD CONSTRAINT extension_versions_pkey PRIMARY KEY (id);


--
-- Name: external_sources external_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_sources
    ADD CONSTRAINT external_sources_pkey PRIMARY KEY (id);


--
-- Name: external_sources external_sources_source_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_sources
    ADD CONSTRAINT external_sources_source_key_key UNIQUE (source_key);


--
-- Name: generated_extension_jobs generated_extension_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generated_extension_jobs
    ADD CONSTRAINT generated_extension_jobs_pkey PRIMARY KEY (generated_job_id);


--
-- Name: job_attachments job_attachments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_attachments
    ADD CONSTRAINT job_attachments_pkey PRIMARY KEY (id);


--
-- Name: job_decision_overrides job_decision_overrides_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_decision_overrides
    ADD CONSTRAINT job_decision_overrides_pkey PRIMARY KEY (id);


--
-- Name: job_errors job_errors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_errors
    ADD CONSTRAINT job_errors_pkey PRIMARY KEY (id);


--
-- Name: job_external_links job_external_links_job_id_source_id_external_ref_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_external_links
    ADD CONSTRAINT job_external_links_job_id_source_id_external_ref_key UNIQUE (job_id, source_id, external_ref);


--
-- Name: job_external_links job_external_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_external_links
    ADD CONSTRAINT job_external_links_pkey PRIMARY KEY (id);


--
-- Name: job_reaction_events job_reaction_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_reaction_events
    ADD CONSTRAINT job_reaction_events_pkey PRIMARY KEY (id);


--
-- Name: job_step_replays job_step_replays_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_step_replays
    ADD CONSTRAINT job_step_replays_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_thread_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_thread_id_key UNIQUE (thread_id);


--
-- Name: machine_tokens machine_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_tokens
    ADD CONSTRAINT machine_tokens_pkey PRIMARY KEY (id);


--
-- Name: machine_tokens machine_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_tokens
    ADD CONSTRAINT machine_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: mcp_connectors mcp_connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_connectors
    ADD CONSTRAINT mcp_connectors_pkey PRIMARY KEY (connector_id);

CREATE TABLE public.mcp_connector_oauth_states (
    state_hash text NOT NULL,
    connector_id text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    oauth_credential_revision integer DEFAULT 1 NOT NULL,
    code_verifier_encrypted text NOT NULL,
    redirect_uri text NOT NULL,
    return_path text DEFAULT ''::text NOT NULL,
    auth_payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mcp_connector_oauth_states_pkey PRIMARY KEY (state_hash),
    CONSTRAINT mcp_connector_oauth_states_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT,
    CONSTRAINT mcp_connector_oauth_states_state_hash_check CHECK ((COALESCE(state_hash, ''::text) <> ''::text)),
    CONSTRAINT mcp_connector_oauth_states_verifier_check CHECK ((COALESCE(code_verifier_encrypted, ''::text) <> ''::text)),
    CONSTRAINT mcp_connector_oauth_states_redirect_uri_check CHECK ((COALESCE(redirect_uri, ''::text) <> ''::text)),
    CONSTRAINT mcp_connector_oauth_states_revision_check CHECK ((oauth_credential_revision >= 1))
);


--
-- Name: model_providers model_providers_account_id_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_providers
    ADD CONSTRAINT model_providers_account_id_name_key UNIQUE (account_id, name);


--
-- Name: model_providers model_providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_providers
    ADD CONSTRAINT model_providers_pkey PRIMARY KEY (id);


--
-- Name: model_providers model_providers_lifecycle_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_providers
    ADD CONSTRAINT model_providers_lifecycle_check CHECK ((lifecycle = ANY (ARRAY['active'::text, 'deprecated'::text, 'shutdown'::text])));


--
-- Name: model_providers model_providers_verification_status_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.model_providers
    ADD CONSTRAINT model_providers_verification_status_check CHECK ((verification_status = ANY (ARRAY['unverified'::text, 'pending'::text, 'passed'::text, 'failed'::text, 'stale'::text])));


--
-- Name: llm_provider_sources llm_provider_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_sources
    ADD CONSTRAINT llm_provider_sources_pkey PRIMARY KEY (id);


--
-- Name: llm_provider_sources llm_provider_sources_account_id_provider_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_sources
    ADD CONSTRAINT llm_provider_sources_account_id_provider_key_key UNIQUE (account_id, provider_key);


--
-- Name: llm_provider_models llm_provider_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_models
    ADD CONSTRAINT llm_provider_models_pkey PRIMARY KEY (id);


--
-- Name: llm_provider_models llm_provider_models_account_source_model_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_models
    ADD CONSTRAINT llm_provider_models_account_source_model_key UNIQUE (account_id, provider_source_id, model_id);


--
-- Name: llm_provider_models llm_provider_models_source_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_models
    ADD CONSTRAINT llm_provider_models_source_fkey FOREIGN KEY (provider_source_id) REFERENCES public.llm_provider_sources(id) ON DELETE CASCADE;


--
-- Name: llm_provider_models llm_provider_models_lifecycle_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_models
    ADD CONSTRAINT llm_provider_models_lifecycle_check CHECK ((lifecycle = ANY (ARRAY['active'::text, 'deprecated'::text, 'shutdown'::text])));


--
-- Name: llm_provider_models llm_provider_models_verification_status_check; Type: CHECK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_provider_models
    ADD CONSTRAINT llm_provider_models_verification_status_check CHECK ((verification_status = ANY (ARRAY['unverified'::text, 'pending'::text, 'passed'::text, 'failed'::text, 'stale'::text])));


--
-- Name: notification_delivery_logs notification_delivery_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_pkey PRIMARY KEY (id);


--
-- Name: notification_routes notification_routes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_pkey PRIMARY KEY (id);


--
-- Name: notification_routes notification_routes_project_id_route_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_project_id_route_key_key UNIQUE (project_id, route_key);


--
-- Name: extension_artifact_snapshots extension_artifact_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_artifact_snapshots
    ADD CONSTRAINT extension_artifact_snapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: extension_artifact_snapshots extension_artifact_snapshots_release_id_artifact_kind_artifact_i_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_artifact_snapshots
    ADD CONSTRAINT extension_artifact_snapshots_release_id_artifact_kind_artifact_i_key UNIQUE (release_id, artifact_kind, artifact_id);


--
-- Name: extension_entitlements extension_entitlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_entitlements
    ADD CONSTRAINT extension_entitlements_pkey PRIMARY KEY (entitlement_id);


--
-- Name: extension_installs extension_installs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_installs
    ADD CONSTRAINT extension_installs_pkey PRIMARY KEY (extension_id, account_id);


--
-- Name: extension_registry_projections extension_registry_projections_install_id_artifact_kind_artifact_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_install_id_artifact_kind_artifact_key UNIQUE (install_id, artifact_kind, artifact_id);


--
-- Name: extension_registry_projections extension_registry_projections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_pkey PRIMARY KEY (projection_id);


--
-- Name: extension_releases extension_releases_extension_id_release_version_source_kind_source_re_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_releases
    ADD CONSTRAINT extension_releases_extension_id_release_version_source_kind_source_re_key UNIQUE (extension_id, release_version, source_kind, source_ref, descriptor_sha256);


--
-- Name: extension_releases extension_releases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_releases
    ADD CONSTRAINT extension_releases_pkey PRIMARY KEY (release_id);


--
-- Name: extension_usage_events extension_usage_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_pkey PRIMARY KEY (usage_event_id);


--
-- Name: extension_oauth_configurations extension_oauth_configurations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_pkey PRIMARY KEY (configuration_id);

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_scope_key UNIQUE (configuration_id, tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id);

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_connection_scope_key UNIQUE (configuration_id, account_id, project_id, extension_install_id, extension_id, provider_id);


--
-- Name: extension_oauth_authorization_states extension_oauth_authorization_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_oauth_authorization_states
    ADD CONSTRAINT extension_oauth_authorization_states_pkey PRIMARY KEY (state_hash);


--
-- Name: personal_access_tokens personal_access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_pkey PRIMARY KEY (id);


--
-- Name: personal_access_tokens personal_access_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: project_connections project_connections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_pkey PRIMARY KEY (connection_id);

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT uq_project_connections_scope UNIQUE (connection_id, project_id, account_id);

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_oauth_scope_key UNIQUE (connection_id, oauth_configuration_id, account_id, project_id, extension_install_id, extension_id, oauth_provider_id, connection_type);


--
-- Name: project_connection_oauth_states project_connection_oauth_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_oauth_states
    ADD CONSTRAINT project_connection_oauth_states_pkey PRIMARY KEY (state_hash);


--
-- Name: project_connection_sessions project_connection_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_sessions
    ADD CONSTRAINT project_connection_sessions_pkey PRIMARY KEY (session_id);


--
-- Name: project_execution_leases project_execution_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_leases
    ADD CONSTRAINT project_execution_leases_pkey PRIMARY KEY (project_id);


--
-- Name: project_execution_slots project_execution_slots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_slots
    ADD CONSTRAINT project_execution_slots_pkey PRIMARY KEY (id);


--
-- Name: project_execution_slots project_execution_slots_project_id_worker_instance_id_lease_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_slots
    ADD CONSTRAINT project_execution_slots_project_id_worker_instance_id_lease_key UNIQUE (project_id, worker_instance_id, lease_token);


--
-- Name: project_policies project_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_policies
    ADD CONSTRAINT project_policies_pkey PRIMARY KEY (project_id);


--
-- Name: project_secrets project_secrets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_secrets
    ADD CONSTRAINT project_secrets_pkey PRIMARY KEY (id);


--
-- Name: project_settings_revisions project_settings_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_settings_revisions
    ADD CONSTRAINT project_settings_revisions_pkey PRIMARY KEY (id);


--
-- Name: project_workflows project_workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_pkey PRIMARY KEY (project_id, workflow_id);


--
-- Name: projects projects_workspace_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_workspace_name_key UNIQUE (tenant_id, account_id, workspace_id, name);


--
-- Name: provider_health_events provider_health_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_health_events
    ADD CONSTRAINT provider_health_events_pkey PRIMARY KEY (id);


--
-- Name: provider_secrets provider_secrets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_secrets
    ADD CONSTRAINT provider_secrets_pkey PRIMARY KEY (id);


--
-- Name: provider_types provider_types_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_types
    ADD CONSTRAINT provider_types_key_key UNIQUE (key);


--
-- Name: provider_types provider_types_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_types
    ADD CONSTRAINT provider_types_pkey PRIMARY KEY (id);


--
-- Name: providers providers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT providers_pkey PRIMARY KEY (id);


--
-- Name: providers providers_provider_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT providers_provider_key_key UNIQUE (provider_key);


--
-- Name: registry_agent_inputs registry_agent_inputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_inputs
    ADD CONSTRAINT registry_agent_inputs_pkey PRIMARY KEY (id);


--
-- Name: registry_agent_outputs registry_agent_outputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_outputs
    ADD CONSTRAINT registry_agent_outputs_pkey PRIMARY KEY (id);


--
-- Name: registry_agent_skills registry_agent_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_skills
    ADD CONSTRAINT registry_agent_skills_pkey PRIMARY KEY (agent_id, skill_id);


--
-- Name: registry_agent_tools registry_agent_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_tools
    ADD CONSTRAINT registry_agent_tools_pkey PRIMARY KEY (agent_id, tool_id);


--
-- Name: registry_agents registry_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agents
    ADD CONSTRAINT registry_agents_pkey PRIMARY KEY (agent_id);


--
-- Name: registry_changelog registry_changelog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_changelog
    ADD CONSTRAINT registry_changelog_pkey PRIMARY KEY (id);


--
-- Name: registry_data_contracts registry_data_contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_data_contracts
    ADD CONSTRAINT registry_data_contracts_pkey PRIMARY KEY (contract_id);


--
-- Name: registry_datasets registry_datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_datasets
    ADD CONSTRAINT registry_datasets_pkey PRIMARY KEY (id);


--
-- Name: dataset_query_templates dataset_query_templates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_query_templates
    ADD CONSTRAINT dataset_query_templates_pkey PRIMARY KEY (query_template_id);


--
-- Name: registry_datasets registry_datasets_dataset_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_datasets
    ADD CONSTRAINT registry_datasets_dataset_key_key UNIQUE (dataset_key);


--
-- Name: registry_datasets registry_datasets_dataset_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_datasets
    ADD CONSTRAINT registry_datasets_dataset_id_key UNIQUE (dataset_id);


--
-- Name: project_datasets project_datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasets
    ADD CONSTRAINT project_datasets_pkey PRIMARY KEY (dataset_id, project_id);


--
-- Name: registry_fields registry_fields_field_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_fields
    ADD CONSTRAINT registry_fields_field_key_key UNIQUE (field_key);


--
-- Name: registry_fields registry_fields_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_fields
    ADD CONSTRAINT registry_fields_pkey PRIMARY KEY (field_id);


--
-- Name: registry_guardrail_agents registry_guardrail_agents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_guardrail_agents
    ADD CONSTRAINT registry_guardrail_agents_pkey PRIMARY KEY (guardrail_id, agent_id);


--
-- Name: registry_guardrails registry_guardrails_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_guardrails
    ADD CONSTRAINT registry_guardrails_pkey PRIMARY KEY (id);


--
-- Name: registry_published_block_contracts registry_published_block_contracts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_contracts
    ADD CONSTRAINT registry_published_block_contracts_pkey PRIMARY KEY (block_key);


--
-- Name: registry_published_block_inputs registry_published_block_inputs_block_key_field_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_key_field_id_key UNIQUE (block_key, field_id);


--
-- Name: registry_published_block_inputs registry_published_block_inputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_pkey PRIMARY KEY (id);


--
-- Name: registry_published_block_outputs registry_published_block_outputs_block_key_field_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_key_field_id_key UNIQUE (block_key, field_id);


--
-- Name: registry_published_block_outputs registry_published_block_outputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_pkey PRIMARY KEY (id);


--
-- Name: registry_skill_inputs registry_skill_inputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_inputs
    ADD CONSTRAINT registry_skill_inputs_pkey PRIMARY KEY (id);


--
-- Name: registry_skill_outputs registry_skill_outputs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_outputs
    ADD CONSTRAINT registry_skill_outputs_pkey PRIMARY KEY (id);


--
-- Name: registry_skills registry_skills_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skills
    ADD CONSTRAINT registry_skills_pkey PRIMARY KEY (skill_id);


--
-- Name: registry_tools registry_tools_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_tools
    ADD CONSTRAINT registry_tools_pkey PRIMARY KEY (tool_id);


--
-- Name: registry_step_kind_profiles registry_step_kind_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_step_kind_profiles
    ADD CONSTRAINT registry_step_kind_profiles_pkey PRIMARY KEY (step_kind_key);


--
-- Name: registry_workflow_for_each registry_workflow_for_each_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_pkey PRIMARY KEY (id);


--
-- Name: registry_workflow_for_each registry_workflow_for_each_workflow_id_for_each_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_workflow_id_for_each_id_key UNIQUE (workflow_id, for_each_id);


--
-- Name: registry_workflow_for_each registry_workflow_for_each_workflow_id_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_workflow_id_id_key UNIQUE (workflow_id, id);


--
-- Name: registry_workflow_hook_binding_revisions registry_workflow_hook_binding_revisions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_pkey PRIMARY KEY (id);


--
-- Name: registry_workflow_hook_binding_revisions registry_workflow_hook_binding_revisions_binding_id_revision_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_binding_id_revision_key UNIQUE (binding_id, revision);


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_pkey PRIMARY KEY (id);


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_scope_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_scope_key UNIQUE (account_id, project_id, source_install_id, local_workflow_id, hook_id);


--
-- Name: registry_workflow_groups registry_workflow_groups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_pkey PRIMARY KEY (id);


--
-- Name: registry_workflow_groups registry_workflow_groups_workflow_id_group_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_group_id_key UNIQUE (workflow_id, group_id);


--
-- Name: registry_workflow_groups registry_workflow_groups_workflow_id_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_id_key UNIQUE (workflow_id, id);


--
-- Name: registry_workflow_steps registry_workflow_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_pkey PRIMARY KEY (id);


--
-- Name: registry_workflow_steps registry_workflow_steps_owner_workflow_id_step_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_owner_workflow_id_step_id_key UNIQUE (owner_workflow_id, step_id);


--
-- Name: registry_workflows registry_workflows_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_pkey PRIMARY KEY (id);


--
-- Name: registry_workflows registry_workflows_workflow_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_workflow_key_key UNIQUE (workflow_key);


--
-- Name: registry_workflows registry_workflows_workflow_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_workflow_id_key UNIQUE (workflow_id);


--
-- Name: report_runs report_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_pkey PRIMARY KEY (id);


--
-- Name: roles roles_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_key_key UNIQUE (key);


--
-- Name: roles roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_pkey PRIMARY KEY (id);


--
-- Name: scheduled_job_execution_locks scheduled_job_execution_locks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_execution_locks
    ADD CONSTRAINT scheduled_job_execution_locks_pkey PRIMARY KEY (id);


--
-- Name: scheduled_job_runs scheduled_job_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_pkey PRIMARY KEY (id);


--
-- Name: scheduled_jobs scheduled_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_pkey PRIMARY KEY (id);


--
-- Name: secret_refs secret_refs_account_id_ref_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_refs
    ADD CONSTRAINT secret_refs_account_id_ref_key_key UNIQUE (account_id, ref_key);


--
-- Name: secret_refs secret_refs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.secret_refs
    ADD CONSTRAINT secret_refs_pkey PRIMARY KEY (id);


--
-- Name: security_events security_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_pkey PRIMARY KEY (id);


--
-- Name: security_findings security_findings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_findings
    ADD CONSTRAINT security_findings_pkey PRIMARY KEY (id);


--
-- Name: security_posture_snapshots security_posture_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_posture_snapshots
    ADD CONSTRAINT security_posture_snapshots_pkey PRIMARY KEY (id);


--
-- Name: service_accounts service_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT service_accounts_pkey PRIMARY KEY (id);


--
-- Name: service_accounts service_accounts_service_account_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT service_accounts_service_account_key_key UNIQUE (service_account_key);


--
-- Name: system_settings system_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (account_id, setting_key);


--
-- Name: provider_webhook_deliveries provider_webhook_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_webhook_deliveries
    ADD CONSTRAINT provider_webhook_deliveries_pkey PRIMARY KEY (provider_key, delivery_key);


--
-- Name: request_idempotency request_idempotency_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.request_idempotency
    ADD CONSTRAINT request_idempotency_pkey PRIMARY KEY (scope_key, key_digest);


--
-- Name: workflow_entrypoints workflow_entrypoints_entry_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_entry_key_key UNIQUE (entry_key);


--
-- Name: workflow_entrypoints workflow_entrypoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_pkey PRIMARY KEY (entrypoint_id);


--
