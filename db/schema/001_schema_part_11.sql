-- Name: scheduled_job_runs scheduled_job_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: scheduled_job_runs scheduled_job_runs_scheduled_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_job_runs
    ADD CONSTRAINT scheduled_job_runs_scheduled_job_id_fkey FOREIGN KEY (scheduled_job_id) REFERENCES public.scheduled_jobs(id) ON DELETE CASCADE;


--
-- Name: scheduled_jobs scheduled_jobs_last_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_last_job_id_fkey FOREIGN KEY (last_job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: scheduled_jobs scheduled_jobs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: scheduled_jobs scheduled_jobs_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE RESTRICT;


--
-- Name: security_events security_events_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: security_events security_events_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_events
    ADD CONSTRAINT security_events_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE SET NULL;


--
-- Name: security_findings security_findings_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_findings
    ADD CONSTRAINT security_findings_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.security_events(id) ON DELETE CASCADE;


--
-- Name: security_posture_snapshots security_posture_snapshots_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.security_posture_snapshots
    ADD CONSTRAINT security_posture_snapshots_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: service_accounts service_accounts_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_accounts
    ADD CONSTRAINT service_accounts_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: system_settings system_settings_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: request_idempotency request_idempotency_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.request_idempotency
    ADD CONSTRAINT request_idempotency_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE SET NULL;


--
-- Name: workflow_entrypoints workflow_entrypoints_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: workflow_entrypoints workflow_entrypoints_project_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE;


--
-- Name: workflow_provider_triggers workflow_provider_triggers_project_account_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_provider_triggers
    ADD CONSTRAINT workflow_provider_triggers_project_account_fkey FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id) ON DELETE CASCADE;


--
-- Name: workflow_provider_triggers workflow_provider_triggers_project_workflow_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_provider_triggers
    ADD CONSTRAINT workflow_provider_triggers_project_workflow_fkey FOREIGN KEY (project_id, workflow_id) REFERENCES public.project_workflows(project_id, workflow_id) ON DELETE CASCADE;


--
-- Name: workflow_entrypoints workflow_entrypoints_project_workflow_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_project_workflow_fkey FOREIGN KEY (project_id, workflow_id) REFERENCES public.project_workflows(project_id, workflow_id) ON DELETE CASCADE;


--
-- Name: workflow_entrypoints workflow_entrypoints_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: user_account_memberships user_account_memberships_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: user_account_memberships user_account_memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_roles user_roles_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.roles(id) ON DELETE CASCADE;


--
-- Name: user_roles user_roles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workflow_reactions workflow_reactions_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_reactions
    ADD CONSTRAINT workflow_reactions_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.accounts(id) ON DELETE RESTRICT;


--
-- Name: workflow_reactions workflow_reactions_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_reactions
    ADD CONSTRAINT workflow_reactions_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
-- Name: jobs jobs_workflow_definition_snapshot_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_workflow_definition_snapshot_id_fkey FOREIGN KEY (workflow_definition_snapshot_id) REFERENCES public.workflow_definition_snapshots(id) ON DELETE RESTRICT;


--
-- Name: workflow_runtime_dependencies workflow_runtime_dependencies_block_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_block_key_fkey FOREIGN KEY (block_key) REFERENCES public.registry_published_block_contracts(block_key) ON DELETE CASCADE;


--
-- Name: workflow_runtime_dependencies workflow_runtime_dependencies_install_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_install_id_fkey FOREIGN KEY (install_id) REFERENCES public.extension_installs(install_id) ON DELETE CASCADE;


--
-- Name: workflow_runtime_dependencies workflow_runtime_dependencies_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;


--
--
-- PostgreSQL database dump complete
--
