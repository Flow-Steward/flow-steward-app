-- Name: scheduled_job_runs fk_scheduled_job_runs_scheduled_job_scope; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_runs
    ADD CONSTRAINT fk_scheduled_job_runs_scheduled_job_scope FOREIGN KEY (scheduled_job_id, project_id, account_id) REFERENCES public.scheduled_jobs(id, project_id, account_id) ON DELETE CASCADE;


--
-- Name: scheduled_jobs fk_scheduled_jobs_workflow; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT fk_scheduled_jobs_workflow FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE RESTRICT;


--
-- Name: generated_extension_jobs generated_extension_jobs_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generated_extension_jobs
    ADD CONSTRAINT generated_extension_jobs_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extension_project_state
    ADD CONSTRAINT extension_project_state_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.extension_project_state
    ADD CONSTRAINT extension_project_state_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.extension_project_state
    ADD CONSTRAINT extension_project_state_connection_scope_fkey FOREIGN KEY (connection_id, project_id, account_id) REFERENCES public.project_connections(connection_id, project_id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.extension_project_state
    ADD CONSTRAINT extension_project_state_install_id_fkey FOREIGN KEY (extension_install_id) REFERENCES public.extension_installs(install_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.external_effects
    ADD CONSTRAINT external_effects_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.external_effects
    ADD CONSTRAINT external_effects_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.external_effects
    ADD CONSTRAINT external_effects_connection_scope_fkey FOREIGN KEY (connection_id, project_id, account_id) REFERENCES public.project_connections(connection_id, project_id, account_id) ON DELETE SET NULL (connection_id);
ALTER TABLE ONLY public.external_effects
    ADD CONSTRAINT external_effects_install_fkey FOREIGN KEY (extension_install_id) REFERENCES public.extension_installs(install_id) ON DELETE SET NULL;

ALTER TABLE ONLY public.external_effect_replays
    ADD CONSTRAINT external_effect_replays_effect_account_fkey FOREIGN KEY (effect_id, account_id) REFERENCES public.external_effects(effect_id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.external_effect_output_reconciliations
    ADD CONSTRAINT external_effect_output_reconciliations_effect_account_fkey FOREIGN KEY (effect_id, account_id) REFERENCES public.external_effects(effect_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.external_effect_maintenance_cursors
    ADD CONSTRAINT external_effect_maintenance_cursors_account_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.external_effect_rate_windows
    ADD CONSTRAINT external_effect_rate_windows_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.external_effect_rate_windows
    ADD CONSTRAINT external_effect_rate_windows_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.external_effect_rate_windows
    ADD CONSTRAINT external_effect_rate_windows_connection_scope_fkey FOREIGN KEY (connection_id, project_id, account_id) REFERENCES public.project_connections(connection_id, project_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_external_effect_id_fkey FOREIGN KEY (external_effect_id) REFERENCES public.external_effects(effect_id) ON DELETE SET NULL;
ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_external_effect_id_fkey FOREIGN KEY (external_effect_id) REFERENCES public.external_effects(effect_id) ON DELETE SET NULL;
ALTER TABLE ONLY public.event_delivery_attempts
    ADD CONSTRAINT event_delivery_attempts_external_effect_id_fkey FOREIGN KEY (external_effect_id) REFERENCES public.external_effects(effect_id) ON DELETE SET NULL;


--
-- Name: generated_extension_jobs generated_extension_jobs_result_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generated_extension_jobs
    ADD CONSTRAINT generated_extension_jobs_result_release_id_fkey FOREIGN KEY (result_release_id) REFERENCES public.extension_releases(release_id) ON DELETE SET NULL;


--
-- Name: generated_extension_jobs generated_extension_jobs_source_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.generated_extension_jobs
    ADD CONSTRAINT generated_extension_jobs_source_job_id_fkey FOREIGN KEY (source_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: job_attachments job_attachments_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_attachments
    ADD CONSTRAINT job_attachments_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_decision_overrides job_decision_overrides_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_decision_overrides
    ADD CONSTRAINT job_decision_overrides_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_decision_overrides job_decision_overrides_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_decision_overrides
    ADD CONSTRAINT job_decision_overrides_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.platform_agent_messages(id) ON DELETE SET NULL;


--
-- Name: job_errors job_errors_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_errors
    ADD CONSTRAINT job_errors_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_errors job_errors_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_errors
    ADD CONSTRAINT job_errors_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.platform_agent_messages(id) ON DELETE SET NULL;


--
-- Name: job_external_links job_external_links_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_external_links
    ADD CONSTRAINT job_external_links_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_external_links job_external_links_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_external_links
    ADD CONSTRAINT job_external_links_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.external_sources(id) ON DELETE SET NULL;


--
-- Name: job_reaction_events job_reaction_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_reaction_events
    ADD CONSTRAINT job_reaction_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_reaction_events job_reaction_events_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_reaction_events
    ADD CONSTRAINT job_reaction_events_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE SET NULL;


--
-- Name: job_step_replays job_step_replays_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_step_replays
    ADD CONSTRAINT job_step_replays_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: job_step_replays job_step_replays_message_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_step_replays
    ADD CONSTRAINT job_step_replays_message_id_fkey FOREIGN KEY (message_id) REFERENCES public.platform_agent_messages(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id);


--
-- Name: machine_tokens machine_tokens_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_tokens
    ADD CONSTRAINT machine_tokens_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: machine_tokens machine_tokens_service_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_tokens
    ADD CONSTRAINT machine_tokens_service_account_id_fkey FOREIGN KEY (service_account_id) REFERENCES public.service_accounts(id) ON DELETE CASCADE;


--
-- Name: mcp_connectors mcp_connectors_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_connectors
    ADD CONSTRAINT mcp_connectors_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.mcp_connectors
    ADD CONSTRAINT mcp_connectors_project_scope_fkey
    FOREIGN KEY (project_id, tenant_id, account_id)
    REFERENCES public.projects(id, tenant_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.mcp_connector_oauth_states
    ADD CONSTRAINT mcp_connector_oauth_states_connector_scope_fkey
    FOREIGN KEY (connector_id, project_id, tenant_id, account_id)
    REFERENCES public.mcp_connectors(connector_id, project_id, tenant_id, account_id)
    ON DELETE CASCADE;

ALTER TABLE ONLY public.mcp_connector_oauth_states
    ADD CONSTRAINT mcp_connector_oauth_states_project_scope_fkey
    FOREIGN KEY (project_id, tenant_id, account_id)
    REFERENCES public.projects(id, tenant_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.secret_refs
    ADD CONSTRAINT secret_refs_project_scope_fkey
    FOREIGN KEY (project_id, tenant_id, account_id)
    REFERENCES public.projects(id, tenant_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.secret_refs
    ADD CONSTRAINT mcp_secret_refs_require_scope
    CHECK (NOT starts_with(ref_key, 'mcp_connector:'::text) OR (project_id IS NOT NULL AND tenant_id IS NOT NULL));


--
-- Name: notification_delivery_logs notification_delivery_logs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: notification_delivery_logs notification_delivery_logs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: notification_delivery_logs notification_delivery_logs_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE SET NULL;


--
-- Name: notification_delivery_logs notification_delivery_logs_route_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_delivery_logs
    ADD CONSTRAINT notification_delivery_logs_route_id_fkey FOREIGN KEY (route_id) REFERENCES public.notification_routes(id) ON DELETE SET NULL;


--
-- Name: notification_routes notification_routes_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: notification_routes notification_routes_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: notification_routes notification_routes_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE CASCADE;


--
-- Name: notification_routes notification_routes_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: extension_artifact_snapshots extension_artifact_snapshots_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_artifact_snapshots
    ADD CONSTRAINT extension_artifact_snapshots_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.extension_releases(release_id) ON DELETE CASCADE;


--
-- Name: extension_entitlements extension_entitlements_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_entitlements
    ADD CONSTRAINT extension_entitlements_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: extension_entitlements extension_entitlements_install_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_entitlements
    ADD CONSTRAINT extension_entitlements_install_account_fkey FOREIGN KEY (install_id, account_id) REFERENCES public.extension_installs(install_id, account_id) ON DELETE CASCADE;


--
-- Name: extension_entitlements extension_entitlements_install_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_entitlements
    ADD CONSTRAINT extension_entitlements_install_id_fkey FOREIGN KEY (install_id) REFERENCES public.extension_installs(install_id) ON DELETE CASCADE;


--
-- Name: extension_installs extension_installs_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_installs
    ADD CONSTRAINT extension_installs_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.extension_releases(release_id) ON DELETE RESTRICT;


--
-- Name: extension_registry_projections extension_registry_projections_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: extension_registry_projections extension_registry_projections_install_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_install_account_fkey FOREIGN KEY (install_id, account_id) REFERENCES public.extension_installs(install_id, account_id) ON DELETE CASCADE;


--
-- Name: extension_registry_projections extension_registry_projections_install_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_install_id_fkey FOREIGN KEY (install_id) REFERENCES public.extension_installs(install_id) ON DELETE CASCADE;


--
-- Name: extension_registry_projections extension_registry_projections_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_registry_projections
    ADD CONSTRAINT extension_registry_projections_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.extension_releases(release_id) ON DELETE CASCADE;


--
-- Name: extension_releases extension_releases_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_releases
    ADD CONSTRAINT extension_releases_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE SET NULL;


--
-- Name: extension_usage_events extension_usage_events_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: extension_usage_events extension_usage_events_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.project_connections(connection_id) ON DELETE SET NULL;


--
-- Name: extension_usage_events extension_usage_events_install_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_install_id_fkey FOREIGN KEY (install_id) REFERENCES public.extension_installs(install_id) ON DELETE CASCADE;


--
-- Name: extension_usage_events extension_usage_events_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: extension_usage_events extension_usage_events_release_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_usage_events
    ADD CONSTRAINT extension_usage_events_release_id_fkey FOREIGN KEY (release_id) REFERENCES public.extension_releases(release_id) ON DELETE SET NULL;


--
-- Name: extension_oauth_configurations extension_oauth_configurations_project_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_project_scope_fkey FOREIGN KEY (project_id, tenant_id, account_id) REFERENCES public.projects(id, tenant_id, account_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_install_scope_fkey FOREIGN KEY (extension_install_id, account_id, extension_id) REFERENCES public.extension_installs(install_id, account_id, extension_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extension_oauth_configurations
    ADD CONSTRAINT extension_oauth_configurations_release_scope_fkey FOREIGN KEY (extension_release_id, extension_id) REFERENCES public.extension_releases(release_id, extension_id) ON DELETE RESTRICT;


--
-- Name: extension_oauth_authorization_states extension_oauth_authorization_states_configuration_scope_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_oauth_authorization_states
    ADD CONSTRAINT extension_oauth_authorization_states_configuration_scope_fkey FOREIGN KEY (configuration_id, tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id) REFERENCES public.extension_oauth_configurations(configuration_id, tenant_id, account_id, project_id, extension_install_id, extension_id, provider_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.extension_oauth_authorization_states
    ADD CONSTRAINT extension_oauth_authorization_states_release_scope_fkey FOREIGN KEY (extension_release_id, extension_id) REFERENCES public.extension_releases(release_id, extension_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.extension_oauth_authorization_states
    ADD CONSTRAINT extension_oauth_authorization_states_existing_connection_scope_fkey FOREIGN KEY (existing_connection_id, configuration_id, account_id, project_id, extension_install_id, extension_id, provider_id, connection_type) REFERENCES public.project_connections(connection_id, oauth_configuration_id, account_id, project_id, extension_install_id, extension_id, oauth_provider_id, connection_type) ON DELETE CASCADE;


--
-- Name: personal_access_tokens personal_access_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: project_connections project_connections_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: project_connections project_connections_extension_install_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_extension_install_fkey FOREIGN KEY (extension_install_id) REFERENCES public.extension_installs(install_id) ON DELETE RESTRICT NOT VALID;


--
-- Name: project_connections project_connections_project_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE NOT VALID;


--
-- Name: project_connections project_connections_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_oauth_configuration_scope_fkey FOREIGN KEY (oauth_configuration_id, account_id, project_id, extension_install_id, extension_id, oauth_provider_id) REFERENCES public.extension_oauth_configurations(configuration_id, account_id, project_id, extension_install_id, extension_id, provider_id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.project_connections
    ADD CONSTRAINT project_connections_oauth_release_scope_fkey FOREIGN KEY (oauth_extension_release_id, extension_id) REFERENCES public.extension_releases(release_id, extension_id) ON DELETE RESTRICT;


--
-- Name: project_connection_oauth_states project_connection_oauth_states_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_oauth_states
    ADD CONSTRAINT project_connection_oauth_states_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: project_connection_oauth_states project_connection_oauth_states_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_oauth_states
    ADD CONSTRAINT project_connection_oauth_states_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.project_connections(connection_id) ON DELETE CASCADE;


--
-- Name: project_connection_oauth_states project_connection_oauth_states_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_oauth_states
    ADD CONSTRAINT project_connection_oauth_states_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_connection_sessions project_connection_sessions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_sessions
    ADD CONSTRAINT project_connection_sessions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: project_connection_sessions project_connection_sessions_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_sessions
    ADD CONSTRAINT project_connection_sessions_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.project_connections(connection_id) ON DELETE CASCADE;


--
-- Name: project_connection_sessions project_connection_sessions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_connection_sessions
    ADD CONSTRAINT project_connection_sessions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_datasets project_datasets_dataset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasets
    ADD CONSTRAINT project_datasets_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES public.registry_datasets(dataset_id) ON DELETE CASCADE;


--
-- Name: dataset_query_templates dataset_query_templates_dataset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dataset_query_templates
    ADD CONSTRAINT dataset_query_templates_dataset_id_fkey FOREIGN KEY (dataset_id) REFERENCES public.registry_datasets(dataset_id) ON DELETE CASCADE;


--
-- Name: project_datasets project_datasets_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_datasets
    ADD CONSTRAINT project_datasets_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_execution_leases project_execution_leases_active_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_leases
    ADD CONSTRAINT project_execution_leases_active_job_id_fkey FOREIGN KEY (active_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: project_execution_leases project_execution_leases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_leases
    ADD CONSTRAINT project_execution_leases_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_execution_slots project_execution_slots_active_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_slots
    ADD CONSTRAINT project_execution_slots_active_job_id_fkey FOREIGN KEY (active_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: project_execution_slots project_execution_slots_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_execution_slots
    ADD CONSTRAINT project_execution_slots_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_policies project_policies_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_policies
    ADD CONSTRAINT project_policies_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_secrets project_secrets_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_secrets
    ADD CONSTRAINT project_secrets_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_settings_revisions project_settings_revisions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_settings_revisions
    ADD CONSTRAINT project_settings_revisions_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_workflows project_workflows_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: project_workflows project_workflows_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: projects projects_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: projects projects_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE RESTRICT;


--
-- Name: workspaces workspaces_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: provider_health_events provider_health_events_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_health_events
    ADD CONSTRAINT provider_health_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: provider_health_events provider_health_events_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_health_events
    ADD CONSTRAINT provider_health_events_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE CASCADE;


--
-- Name: provider_secrets provider_secrets_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_secrets
    ADD CONSTRAINT provider_secrets_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: provider_secrets provider_secrets_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.provider_secrets
    ADD CONSTRAINT provider_secrets_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE CASCADE;


--
-- Name: providers providers_provider_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.providers
    ADD CONSTRAINT providers_provider_type_id_fkey FOREIGN KEY (provider_type_id) REFERENCES public.provider_types(id) ON DELETE RESTRICT;


--
-- Name: registry_agent_inputs registry_agent_inputs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_inputs
    ADD CONSTRAINT registry_agent_inputs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.registry_agents(agent_id) ON DELETE CASCADE;


--
-- Name: registry_agent_outputs registry_agent_outputs_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_outputs
    ADD CONSTRAINT registry_agent_outputs_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.registry_agents(agent_id) ON DELETE CASCADE;


--
-- Name: registry_agent_skills registry_agent_skills_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_skills
    ADD CONSTRAINT registry_agent_skills_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.registry_agents(agent_id) ON DELETE CASCADE;


--
-- Name: registry_agent_skills registry_agent_skills_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_skills
    ADD CONSTRAINT registry_agent_skills_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.registry_skills(skill_id) ON DELETE CASCADE;


--
-- Name: registry_agent_tools registry_agent_tools_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_tools
    ADD CONSTRAINT registry_agent_tools_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.registry_agents(agent_id) ON DELETE CASCADE;


--
-- Name: registry_agent_tools registry_agent_tools_tool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agent_tools
    ADD CONSTRAINT registry_agent_tools_tool_id_fkey FOREIGN KEY (tool_id) REFERENCES public.registry_tools(tool_id) ON DELETE CASCADE;


--
-- Name: registry_agents registry_agents_agent_type_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_agents
    ADD CONSTRAINT registry_agents_agent_type_id_fkey FOREIGN KEY (agent_type_id) REFERENCES public.agent_types(id);


--
-- Name: registry_fields registry_fields_data_contract_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_fields
    ADD CONSTRAINT registry_fields_data_contract_id_fkey FOREIGN KEY (data_contract_id) REFERENCES public.registry_data_contracts(contract_id) ON DELETE SET NULL;


--
-- Name: registry_guardrail_agents registry_guardrail_agents_agent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_guardrail_agents
    ADD CONSTRAINT registry_guardrail_agents_agent_id_fkey FOREIGN KEY (agent_id) REFERENCES public.registry_agents(agent_id) ON DELETE CASCADE;


--
-- Name: registry_guardrail_agents registry_guardrail_agents_guardrail_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_guardrail_agents
    ADD CONSTRAINT registry_guardrail_agents_guardrail_id_fkey FOREIGN KEY (guardrail_id) REFERENCES public.registry_guardrails(id) ON DELETE CASCADE;


--
-- Name: registry_guardrails registry_guardrails_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_guardrails
    ADD CONSTRAINT registry_guardrails_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--


--
-- Name: registry_published_block_contracts registry_pub_blocks_source_install_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_contracts
    ADD CONSTRAINT registry_pub_blocks_source_install_account_fkey FOREIGN KEY (source_install_id, account_id) REFERENCES public.extension_installs(install_id, account_id) ON DELETE SET NULL;


--
-- Name: registry_published_block_inputs registry_published_block_inputs_block_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_key_fkey FOREIGN KEY (block_key) REFERENCES public.registry_published_block_contracts(block_key) ON DELETE CASCADE;


--
-- Name: registry_published_block_inputs registry_published_block_inputs_field_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_field_id_fkey FOREIGN KEY (field_id) REFERENCES public.registry_fields(field_id) ON DELETE RESTRICT;


--
-- Name: registry_published_block_outputs registry_published_block_outputs_block_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_key_fkey FOREIGN KEY (block_key) REFERENCES public.registry_published_block_contracts(block_key) ON DELETE CASCADE;


--
-- Name: registry_published_block_outputs registry_published_block_outputs_field_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_field_id_fkey FOREIGN KEY (field_id) REFERENCES public.registry_fields(field_id) ON DELETE RESTRICT;


--
-- Name: registry_skill_inputs registry_skill_inputs_contract_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_inputs
    ADD CONSTRAINT registry_skill_inputs_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES public.registry_data_contracts(contract_id) ON DELETE SET NULL;


--
-- Name: registry_skill_inputs registry_skill_inputs_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_inputs
    ADD CONSTRAINT registry_skill_inputs_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.registry_skills(skill_id) ON DELETE CASCADE;


--
-- Name: registry_skill_outputs registry_skill_outputs_contract_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_outputs
    ADD CONSTRAINT registry_skill_outputs_contract_id_fkey FOREIGN KEY (contract_id) REFERENCES public.registry_data_contracts(contract_id) ON DELETE SET NULL;


--
-- Name: registry_skill_outputs registry_skill_outputs_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_skill_outputs
    ADD CONSTRAINT registry_skill_outputs_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.registry_skills(skill_id) ON DELETE CASCADE;


--
-- Name: registry_workflow_for_each registry_workflow_for_each_group_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_group_fkey FOREIGN KEY (workflow_id, group_id) REFERENCES public.registry_workflow_groups(workflow_id, id) ON DELETE CASCADE;


--
-- Name: registry_workflow_for_each registry_workflow_for_each_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: registry_workflow_hook_binding_revisions registry_workflow_hook_binding_revisions_binding_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_binding_fkey FOREIGN KEY (binding_id) REFERENCES public.registry_workflow_hook_bindings(id) ON DELETE CASCADE;


--
-- Name: registry_workflow_hook_binding_revisions registry_workflow_hook_binding_revisions_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_project_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_account_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE CASCADE;


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_active_revision_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_active_revision_fkey FOREIGN KEY (active_revision_id) REFERENCES public.registry_workflow_hook_binding_revisions(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_staged_revision_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_staged_revision_fkey FOREIGN KEY (staged_revision_id) REFERENCES public.registry_workflow_hook_binding_revisions(id) DEFERRABLE INITIALLY DEFERRED;


--
-- Name: registry_workflow_hook_bindings registry_workflow_hook_bindings_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_project_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: registry_workflow_groups registry_workflow_groups_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: registry_workflow_steps registry_workflow_steps_for_each_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_for_each_fkey FOREIGN KEY (owner_workflow_id, for_each_id) REFERENCES public.registry_workflow_for_each(workflow_id, id) ON DELETE CASCADE;


--
-- Name: registry_workflow_steps registry_workflow_steps_group_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_group_fkey FOREIGN KEY (owner_workflow_id, group_id) REFERENCES public.registry_workflow_groups(workflow_id, id) ON DELETE CASCADE;


--
-- Name: registry_workflow_steps registry_workflow_steps_owner_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_owner_workflow_id_fkey FOREIGN KEY (owner_workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: registry_workflow_steps registry_workflow_steps_skill_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_skill_id_fkey FOREIGN KEY (skill_id) REFERENCES public.registry_skills(skill_id) ON DELETE RESTRICT;


--
-- Name: registry_workflows registry_workflows_source_install_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_source_install_account_fkey FOREIGN KEY (source_install_id, account_id) REFERENCES public.extension_installs(install_id, account_id) ON DELETE SET NULL;


--
-- Name: registry_workflows registry_workflows_source_projection_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_source_projection_fkey FOREIGN KEY (source_projection_id) REFERENCES public.extension_registry_projections(projection_id) ON DELETE SET NULL;


--
-- Name: registry_workflows registry_workflows_source_release_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_source_release_fkey FOREIGN KEY (source_release_id) REFERENCES public.extension_releases(release_id) ON DELETE SET NULL;


--
-- Name: report_runs report_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.report_runs
    ADD CONSTRAINT report_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: scheduled_job_execution_locks scheduled_job_execution_locks_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_execution_locks
    ADD CONSTRAINT scheduled_job_execution_locks_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: scheduled_job_execution_locks scheduled_job_execution_locks_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_execution_locks
    ADD CONSTRAINT scheduled_job_execution_locks_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: scheduled_job_execution_locks scheduled_job_execution_locks_scheduled_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_execution_locks
    ADD CONSTRAINT scheduled_job_execution_locks_scheduled_job_id_fkey FOREIGN KEY (scheduled_job_id) REFERENCES public.scheduled_jobs(id) ON DELETE CASCADE;


--
-- Name: scheduled_job_runs scheduled_job_runs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
