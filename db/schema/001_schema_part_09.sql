-- Name: idx_workflow_reactions_account_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_reactions_account_workflow ON public.workflow_reactions USING btree (account_id, workflow_id, enabled, event_type);


--
-- Name: idx_workflow_reactions_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_reactions_workflow ON public.workflow_reactions USING btree (workflow_id, enabled, event_type);


--
-- Name: idx_jobs_workflow_definition_snapshot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jobs_workflow_definition_snapshot ON public.jobs USING btree (workflow_definition_snapshot_id) WHERE (workflow_definition_snapshot_id IS NOT NULL);


--
-- Name: idx_workflow_runtime_dependencies_account; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_runtime_dependencies_account ON public.workflow_runtime_dependencies USING btree (account_id, scope_kind, dependency_kind);


--
-- Name: idx_workflow_runtime_dependencies_workflow; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_workflow_runtime_dependencies_workflow ON public.workflow_runtime_dependencies USING btree (workflow_id, dependency_kind);


--
-- Name: uq_job_decision_overrides_active_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_job_decision_overrides_active_scope ON public.job_decision_overrides USING btree (job_id, domain, COALESCE(step_id, ''::text), COALESCE(step_execution_id, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(workflow_id, ''::text), COALESCE(message_id, '00000000-0000-0000-0000-000000000000'::uuid)) WHERE (active = true);


--
-- Name: ux_extension_entitlements_install; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_extension_entitlements_install ON public.extension_entitlements USING btree (install_id);


--
-- Name: ux_extension_installs_install_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_extension_installs_install_id ON public.extension_installs USING btree (install_id);


--
-- Name: ux_extension_installs_install_id_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_extension_installs_install_id_account_id ON public.extension_installs USING btree (install_id, account_id);

CREATE UNIQUE INDEX ux_extension_installs_install_account_extension ON public.extension_installs USING btree (install_id, account_id, extension_id);

CREATE UNIQUE INDEX ux_extension_releases_release_extension ON public.extension_releases USING btree (release_id, extension_id);


--
-- Name: ux_extension_releases_source_snapshot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_extension_releases_source_snapshot ON public.extension_releases USING btree (extension_id, release_version, source_kind, source_ref, descriptor_sha256);


--
-- Name: ux_project_connection_sessions_boundary; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_project_connection_sessions_boundary ON public.project_connection_sessions USING btree (connection_id, scope_hash, subject_key, credential_revision);


--
-- Name: ux_projects_id_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_projects_id_account_id ON public.projects USING btree (id, account_id);

CREATE UNIQUE INDEX ux_projects_id_tenant_id_account_id ON public.projects USING btree (id, tenant_id, account_id);


--
-- Name: ux_scheduled_job_execution_locks_active_slot; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_scheduled_job_execution_locks_active_slot ON public.scheduled_job_execution_locks USING btree (account_id, project_id, lock_key, slot_ordinal) WHERE (released_at IS NULL);


--
-- Name: accounts trg_accounts_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_accounts_updated BEFORE UPDATE ON public.accounts FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: agent_types trg_agent_types_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agent_types_updated BEFORE UPDATE ON public.agent_types FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_agents trg_agents_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_agents_updated BEFORE UPDATE ON public.registry_agents FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: auth_email_change_requests trg_auth_email_change_requests_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_auth_email_change_requests_updated BEFORE UPDATE ON public.auth_email_change_requests FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: auth_password_resets trg_auth_password_resets_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_auth_password_resets_updated BEFORE UPDATE ON public.auth_password_resets FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: auth_sessions trg_auth_sessions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_auth_sessions_updated BEFORE UPDATE ON public.auth_sessions FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: command_audit_log trg_command_audit_log_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_command_audit_log_updated BEFORE UPDATE ON public.command_audit_log FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: domain_event_ledger trg_domain_event_ledger_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_domain_event_ledger_updated BEFORE UPDATE ON public.domain_event_ledger FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: enabled_extensions trg_enabled_extensions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_enabled_extensions_updated BEFORE UPDATE ON public.enabled_extensions FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();

CREATE TRIGGER trg_external_effects_updated BEFORE UPDATE ON public.external_effects FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();

CREATE TRIGGER trg_external_effect_rate_windows_updated BEFORE UPDATE ON public.external_effect_rate_windows FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();

CREATE TRIGGER trg_extension_project_state_updated BEFORE UPDATE ON public.extension_project_state FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_callback_registrations trg_extension_callback_registrations_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_callback_registrations_updated BEFORE UPDATE ON public.extension_callback_registrations FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: event_dispatches trg_event_dispatches_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_event_dispatches_updated BEFORE UPDATE ON public.event_dispatches FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_bundles trg_extension_bundles_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_bundles_updated BEFORE UPDATE ON public.extension_bundles FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_versions trg_extension_versions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_versions_updated BEFORE UPDATE ON public.extension_versions FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: external_sources trg_external_sources_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_external_sources_updated BEFORE UPDATE ON public.external_sources FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: generated_extension_jobs trg_generated_extension_jobs_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_generated_extension_jobs_updated BEFORE UPDATE ON public.generated_extension_jobs FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_guardrails trg_guardrails_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_guardrails_updated BEFORE UPDATE ON public.registry_guardrails FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_datasets trg_registry_datasets_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_registry_datasets_updated BEFORE UPDATE ON public.registry_datasets FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: machine_tokens trg_machine_tokens_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_machine_tokens_updated BEFORE UPDATE ON public.machine_tokens FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: mcp_connectors trg_mcp_connectors_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_mcp_connectors_updated BEFORE UPDATE ON public.mcp_connectors FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: mcp_connector_oauth_states trg_mcp_connector_oauth_states_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_mcp_connector_oauth_states_updated BEFORE UPDATE ON public.mcp_connector_oauth_states FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: model_providers trg_model_providers_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_model_providers_updated BEFORE UPDATE ON public.model_providers FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: notification_routes trg_notification_routes_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_notification_routes_updated BEFORE UPDATE ON public.notification_routes FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_entitlements trg_extension_entitlements_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_entitlements_updated BEFORE UPDATE ON public.extension_entitlements FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_registry_projections trg_extension_registry_projections_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_registry_projections_updated BEFORE UPDATE ON public.extension_registry_projections FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: personal_access_tokens trg_pat_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pat_updated BEFORE UPDATE ON public.personal_access_tokens FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_connections trg_project_connections_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_connections_updated BEFORE UPDATE ON public.project_connections FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_oauth_configurations trg_extension_oauth_configurations_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_oauth_configurations_updated BEFORE UPDATE ON public.extension_oauth_configurations FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: extension_oauth_authorization_states trg_extension_oauth_authorization_states_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_extension_oauth_authorization_states_updated BEFORE UPDATE ON public.extension_oauth_authorization_states FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_connection_oauth_states trg_project_connection_oauth_states_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_connection_oauth_states_updated BEFORE UPDATE ON public.project_connection_oauth_states FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_connection_sessions trg_project_connection_sessions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_connection_sessions_updated BEFORE UPDATE ON public.project_connection_sessions FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_execution_leases trg_project_execution_leases_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_execution_leases_updated BEFORE UPDATE ON public.project_execution_leases FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_execution_slots trg_project_execution_slots_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_execution_slots_updated BEFORE UPDATE ON public.project_execution_slots FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_policies trg_project_policies_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_policies_updated BEFORE UPDATE ON public.project_policies FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_secrets trg_project_secrets_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_secrets_updated BEFORE UPDATE ON public.project_secrets FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: project_workflows trg_project_workflows_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_project_workflows_updated BEFORE UPDATE ON public.project_workflows FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: provider_secrets trg_provider_secrets_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_provider_secrets_updated BEFORE UPDATE ON public.provider_secrets FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: provider_types trg_provider_types_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_provider_types_updated BEFORE UPDATE ON public.provider_types FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: providers trg_providers_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_providers_updated BEFORE UPDATE ON public.providers FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_fields trg_registry_fields_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_registry_fields_updated BEFORE UPDATE ON public.registry_fields FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_published_block_contracts trg_registry_pub_blocks_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_registry_pub_blocks_updated BEFORE UPDATE ON public.registry_published_block_contracts FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: roles trg_roles_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_roles_updated BEFORE UPDATE ON public.roles FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: scheduled_job_execution_locks trg_scheduled_job_execution_locks_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_scheduled_job_execution_locks_updated BEFORE UPDATE ON public.scheduled_job_execution_locks FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: scheduled_jobs trg_scheduled_jobs_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_scheduled_jobs_updated BEFORE UPDATE ON public.scheduled_jobs FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: service_accounts trg_service_accounts_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_service_accounts_updated BEFORE UPDATE ON public.service_accounts FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_skills trg_skills_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_skills_updated BEFORE UPDATE ON public.registry_skills FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_tools trg_tools_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_tools_updated BEFORE UPDATE ON public.registry_tools FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: user_account_memberships trg_user_account_memberships_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_user_account_memberships_updated BEFORE UPDATE ON public.user_account_memberships FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: users trg_users_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_users_updated BEFORE UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: request_idempotency trg_request_idempotency_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_request_idempotency_updated BEFORE UPDATE ON public.request_idempotency FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: provider_webhook_deliveries trg_provider_webhook_deliveries_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_provider_webhook_deliveries_updated BEFORE UPDATE ON public.provider_webhook_deliveries FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: workflow_provider_triggers trg_workflow_provider_triggers_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workflow_provider_triggers_updated BEFORE UPDATE ON public.workflow_provider_triggers FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: workflow_entrypoints trg_workflow_entrypoints_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workflow_entrypoints_updated BEFORE UPDATE ON public.workflow_entrypoints FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: workflow_reactions trg_workflow_reactions_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workflow_reactions_updated BEFORE UPDATE ON public.workflow_reactions FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: workflow_runtime_dependencies trg_workflow_runtime_dependencies_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workflow_runtime_dependencies_updated BEFORE UPDATE ON public.workflow_runtime_dependencies FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: registry_workflows trg_workflows_updated; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_workflows_updated BEFORE UPDATE ON public.registry_workflows FOR EACH ROW EXECUTE FUNCTION public.registry_update_timestamp();


--
-- Name: auth_email_change_requests auth_email_change_requests_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_email_change_requests
    ADD CONSTRAINT auth_email_change_requests_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: auth_password_resets auth_password_resets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_password_resets
    ADD CONSTRAINT auth_password_resets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: auth_sessions auth_sessions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: auth_sessions auth_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: platform_action_log platform_action_log_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_action_log
    ADD CONSTRAINT platform_action_log_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: platform_action_log platform_action_log_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_action_log
    ADD CONSTRAINT platform_action_log_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: platform_agent_messages platform_agent_messages_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_agent_messages
    ADD CONSTRAINT platform_agent_messages_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: platform_events platform_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_events
    ADD CONSTRAINT platform_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id);


--
-- Name: command_audit_log command_audit_log_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.command_audit_log
    ADD CONSTRAINT command_audit_log_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: command_audit_log command_audit_log_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.command_audit_log
    ADD CONSTRAINT command_audit_log_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.event_dispatches(dispatch_id) ON DELETE SET NULL;


--
-- Name: command_audit_log command_audit_log_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.command_audit_log
    ADD CONSTRAINT command_audit_log_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.domain_event_ledger(event_id) ON DELETE SET NULL;


--
-- Name: dashboard_job_facts dashboard_job_facts_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_job_facts
    ADD CONSTRAINT dashboard_job_facts_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: dashboard_job_facts dashboard_job_facts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_job_facts
    ADD CONSTRAINT dashboard_job_facts_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: domain_event_ledger domain_event_ledger_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: domain_event_ledger domain_event_ledger_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: domain_event_ledger domain_event_ledger_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: domain_event_ledger domain_event_ledger_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE SET NULL;


--
-- Name: domain_event_outbox domain_event_outbox_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_outbox
    ADD CONSTRAINT domain_event_outbox_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: job_dispatch_outbox job_dispatch_outbox_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_dispatch_outbox
    ADD CONSTRAINT job_dispatch_outbox_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: enabled_extensions enabled_extensions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enabled_extensions
    ADD CONSTRAINT enabled_extensions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: enabled_extensions enabled_extensions_bundle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enabled_extensions
    ADD CONSTRAINT enabled_extensions_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.extension_bundles(id) ON DELETE CASCADE;


--
-- Name: enabled_extensions enabled_extensions_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enabled_extensions
    ADD CONSTRAINT enabled_extensions_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.extension_versions(id) ON DELETE CASCADE;


--
-- Name: extension_callback_registrations extension_callback_registrations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_callback_registrations
    ADD CONSTRAINT extension_callback_registrations_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: extension_callback_registrations extension_callback_registrations_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_callback_registrations
    ADD CONSTRAINT extension_callback_registrations_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.project_connections(connection_id) ON DELETE SET NULL;


--
-- Name: extension_callback_deliveries extension_callback_deliveries_registration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_callback_deliveries
    ADD CONSTRAINT extension_callback_deliveries_registration_id_fkey FOREIGN KEY (registration_id) REFERENCES public.extension_callback_registrations(id) ON DELETE CASCADE;


--
-- Name: event_delivery_attempts event_delivery_attempts_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_delivery_attempts
    ADD CONSTRAINT event_delivery_attempts_dispatch_id_fkey FOREIGN KEY (dispatch_id) REFERENCES public.event_dispatches(dispatch_id) ON DELETE CASCADE;


--
-- Name: event_delivery_attempts event_delivery_attempts_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_delivery_attempts
    ADD CONSTRAINT event_delivery_attempts_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.domain_event_ledger(event_id) ON DELETE CASCADE;


--
-- Name: event_dispatches event_dispatches_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: event_dispatches event_dispatches_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.domain_event_ledger(event_id) ON DELETE CASCADE;


--
-- Name: event_dispatches event_dispatches_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: event_dispatches event_dispatches_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: event_dispatches event_dispatches_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE SET NULL;


--
-- Name: extension_bundles extension_bundles_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_bundles
    ADD CONSTRAINT extension_bundles_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: extension_invocation_logs extension_invocation_logs_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: extension_invocation_logs extension_invocation_logs_bundle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.extension_bundles(id) ON DELETE CASCADE;


--
-- Name: extension_invocation_logs extension_invocation_logs_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE;


--
-- Name: extension_invocation_logs extension_invocation_logs_version_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_version_id_fkey FOREIGN KEY (version_id) REFERENCES public.extension_versions(id) ON DELETE SET NULL;


--
-- Name: extension_invocation_logs extension_invocation_logs_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE SET NULL;


--
-- Name: extension_versions extension_versions_bundle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_versions
    ADD CONSTRAINT extension_versions_bundle_id_fkey FOREIGN KEY (bundle_id) REFERENCES public.extension_bundles(id) ON DELETE CASCADE;


--
-- Name: external_sources external_sources_provider_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.external_sources
    ADD CONSTRAINT external_sources_provider_id_fkey FOREIGN KEY (provider_id) REFERENCES public.providers(id) ON DELETE SET NULL;


--
-- Name: scheduled_job_execution_locks fk_scheduled_job_execution_locks_scheduled_job_scope; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_execution_locks
    ADD CONSTRAINT fk_scheduled_job_execution_locks_scheduled_job_scope FOREIGN KEY (scheduled_job_id, project_id, account_id) REFERENCES public.scheduled_jobs(id, project_id, account_id) ON DELETE CASCADE;


--
