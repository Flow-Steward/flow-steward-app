-- Name: request_idempotency; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.request_idempotency (
    scope_key text NOT NULL,
    key_digest text NOT NULL,
    request_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    job_id uuid,
    last_error text DEFAULT ''::text NOT NULL,
    request_fingerprint text NOT NULL,
    replay_response_jsonb jsonb,
    result_expires_at timestamp with time zone,
    tombstone_expires_at timestamp with time zone,
    lease_expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT request_idempotency_pending_lease_check CHECK ((status <> 'pending'::text) OR (lease_expires_at IS NOT NULL)),
    CONSTRAINT request_idempotency_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'failed'::text])))
);


--
-- Name: provider_webhook_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_webhook_deliveries (
    provider_key text NOT NULL,
    delivery_key text NOT NULL,
    request_id text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    job_id uuid,
    last_error text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_webhook_deliveries_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'failed'::text])))
);


--
-- Name: workflow_entrypoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_entrypoints (
    entrypoint_id text DEFAULT (gen_random_uuid())::text NOT NULL,
    workflow_id text NOT NULL,
    project_id uuid NOT NULL,
    entry_kind text DEFAULT 'webhook'::text NOT NULL,
    entry_key text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    auth_mode text DEFAULT 'bearer_token'::text NOT NULL,
    auth_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_restrictions_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    allowed_content_types_jsonb jsonb DEFAULT '["application/json"]'::jsonb NOT NULL,
    max_body_bytes integer DEFAULT 46137344 NOT NULL,
    max_file_bytes integer DEFAULT 10485760 NOT NULL,
    max_files integer DEFAULT 4 NOT NULL,
    secret_ref text DEFAULT ''::text NOT NULL,
    idempotency_mode text DEFAULT 'none'::text NOT NULL,
    input_mapping_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_mode text DEFAULT 'accepted'::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_entrypoints_auth_mode_check CHECK ((auth_mode = ANY (ARRAY['shared_secret_header'::text, 'bearer_token'::text, 'basic_auth'::text, 'hmac_sha256'::text]))),
    CONSTRAINT workflow_entrypoints_entry_kind_check CHECK ((entry_kind = ANY (ARRAY['webhook'::text, 'mcp'::text, 'api'::text]))),
    CONSTRAINT workflow_entrypoints_idempotency_mode_check CHECK ((idempotency_mode = ANY (ARRAY['none'::text, 'header'::text]))),
    CONSTRAINT workflow_entrypoints_max_body_bytes_check CHECK (
        (max_body_bytes > 0) AND (max_body_bytes <= 104857600)
    ),
    CONSTRAINT workflow_entrypoints_max_file_bytes_check CHECK (((max_file_bytes > 0) AND (max_file_bytes <= 104857600))),
    CONSTRAINT workflow_entrypoints_max_files_check CHECK (((max_files >= 0) AND (max_files <= 32))),
    CONSTRAINT workflow_entrypoints_response_mode_check CHECK ((response_mode = ANY (ARRAY['accepted'::text, 'ok'::text, 'no_content'::text])))
);


--
-- Name: workflow_provider_triggers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_provider_triggers (
    trigger_id text DEFAULT (gen_random_uuid())::text NOT NULL,
    workflow_id text NOT NULL,
    project_id uuid NOT NULL,
    provider_key text NOT NULL,
    trigger_type_id text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    event_name text DEFAULT ''::text NOT NULL,
    trigger_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    readiness_status text DEFAULT 'draft'::text NOT NULL,
    validation_errors_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_provider_triggers_pkey PRIMARY KEY (trigger_id),
    CONSTRAINT workflow_provider_triggers_readiness_status_check CHECK ((readiness_status = ANY (ARRAY['draft'::text, 'ready'::text, 'invalid'::text])))
);


--
-- Name: workflow_reactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_reactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id text NOT NULL,
    reaction_key text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    event_type text NOT NULL,
    conditions_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    action_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    debounce_seconds integer DEFAULT 0 NOT NULL,
    idempotency_key_template text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    CONSTRAINT workflow_reactions_debounce_seconds_check CHECK ((debounce_seconds >= 0))
);


--
-- Name: workflow_reactions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: workflow_definition_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_definition_snapshots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    workflow_registry_id uuid NOT NULL,
    workflow_id text NOT NULL,
    revision integer NOT NULL,
    digest text NOT NULL,
    schema_version integer NOT NULL,
    definition_jsonb jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_definition_snapshots_digest_check CHECK ((digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT workflow_definition_snapshots_revision_check CHECK ((revision >= 1))
);


--
-- Name: workflow_kept_test_outputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_kept_test_outputs (
    account_id uuid NOT NULL,
    workflow_id text NOT NULL,
    step_id text NOT NULL,
    outputs_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_kind text DEFAULT 'manual'::text NOT NULL,
    source_job_id text DEFAULT ''::text NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_kept_test_outputs_pkey PRIMARY KEY (account_id, workflow_id, step_id),
    CONSTRAINT workflow_kept_test_outputs_object_check CHECK ((jsonb_typeof(outputs_jsonb) = 'object'::text)),
    CONSTRAINT workflow_kept_test_outputs_source_kind_check CHECK ((source_kind = ANY (ARRAY['run'::text, 'manual'::text]))),
    CONSTRAINT workflow_kept_test_outputs_step_id_nonblank_check CHECK ((btrim(step_id) <> ''::text)),
    CONSTRAINT workflow_kept_test_outputs_workflow_id_nonblank_check CHECK ((btrim(workflow_id) <> ''::text))
);


--
-- Name: workflow_runtime_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_runtime_dependencies (
    dependency_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    workflow_id text NOT NULL,
    group_id text,
    step_id text DEFAULT ''::text NOT NULL,
    dependency_kind text NOT NULL,
    dependency_ref text NOT NULL,
    install_id text,
    block_key text,
    required_for_builder boolean DEFAULT true NOT NULL,
    required_for_execution boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: workflow_runtime_dependencies_dependency_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: auth_email_change_requests id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: auth_password_resets id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: auth_sessions id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: platform_action_log id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: platform_agent_messages id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: platform_event_retry_queue id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: platform_events id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: command_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: domain_event_ledger id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: domain_event_outbox id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: enabled_extensions id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: event_delivery_attempts id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: event_dispatches id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: extension_bundles id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: extension_invocation_logs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: extension_versions id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: external_sources id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_attachments id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_decision_overrides id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_errors id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_external_links id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_reaction_events id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: job_step_replays id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: jobs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: machine_tokens id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: model_providers id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: notification_delivery_logs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: notification_routes id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: extension_artifact_snapshots snapshot_id; Type: DEFAULT; Schema: public; Owner: -
--

--
-- Name: extension_registry_projections projection_id; Type: DEFAULT; Schema: public; Owner: -
--

--
-- Name: extension_usage_events usage_event_id; Type: DEFAULT; Schema: public; Owner: -
--

--
-- Name: personal_access_tokens id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: project_execution_slots id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: project_secrets id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: project_settings_revisions id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: projects id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: provider_health_events id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: provider_secrets id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: provider_types id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: providers id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_agent_inputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_agent_outputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_changelog id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_fields field_id; Type: DEFAULT; Schema: public; Owner: -
--

--
-- Name: registry_guardrails id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_published_block_inputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_published_block_outputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_skill_inputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: registry_skill_outputs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: roles id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: scheduled_job_execution_locks id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: scheduled_job_runs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: scheduled_jobs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: secret_refs id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: security_events id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: security_findings id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: security_posture_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: service_accounts id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: user_account_memberships id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: user_roles id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: workflow_reactions id; Type: DEFAULT; Schema: public; Owner: -
--



--
-- Name: workflow_runtime_dependencies dependency_id; Type: DEFAULT; Schema: public; Owner: -
--

--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (id);


--
-- Name: accounts accounts_tenant_account_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_tenant_account_key_key UNIQUE (tenant_id, account_key);


--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);


--
-- Name: workspaces workspaces_tenant_account_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_tenant_account_key_key UNIQUE (tenant_id, account_id, workspace_key);


--
-- Name: auth_email_change_requests auth_email_change_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_email_change_requests
    ADD CONSTRAINT auth_email_change_requests_pkey PRIMARY KEY (id);


--
-- Name: auth_email_change_requests auth_email_change_requests_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_email_change_requests
    ADD CONSTRAINT auth_email_change_requests_token_hash_key UNIQUE (token_hash);


--
-- Name: auth_rate_limits auth_rate_limits_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_rate_limits
    ADD CONSTRAINT auth_rate_limits_pkey PRIMARY KEY (key);


--
-- Name: auth_password_resets auth_password_resets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_password_resets
    ADD CONSTRAINT auth_password_resets_pkey PRIMARY KEY (id);


--
-- Name: auth_password_resets auth_password_resets_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_password_resets
    ADD CONSTRAINT auth_password_resets_token_hash_key UNIQUE (token_hash);


--
-- Name: auth_sessions auth_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (id);


--
-- Name: auth_sessions auth_sessions_session_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_sessions
    ADD CONSTRAINT auth_sessions_session_token_hash_key UNIQUE (session_token_hash);


--
-- Name: platform_action_log platform_action_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_action_log
    ADD CONSTRAINT platform_action_log_pkey PRIMARY KEY (id);


--
-- Name: platform_agent_messages platform_agent_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_agent_messages
    ADD CONSTRAINT platform_agent_messages_pkey PRIMARY KEY (id);


--
-- Name: platform_event_retry_queue platform_event_retry_queue_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_event_retry_queue
    ADD CONSTRAINT platform_event_retry_queue_pkey PRIMARY KEY (id);


--
-- Name: platform_events platform_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.platform_events
    ADD CONSTRAINT platform_events_pkey PRIMARY KEY (id);


--
-- Name: checkpoint_blobs checkpoint_blobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_blobs
    ADD CONSTRAINT checkpoint_blobs_pkey PRIMARY KEY (thread_id, checkpoint_ns, channel, version);


--
-- Name: checkpoint_migrations checkpoint_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_migrations
    ADD CONSTRAINT checkpoint_migrations_pkey PRIMARY KEY (v);


--
-- Name: checkpoint_writes checkpoint_writes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoint_writes
    ADD CONSTRAINT checkpoint_writes_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx);


--
-- Name: checkpoints checkpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.checkpoints
    ADD CONSTRAINT checkpoints_pkey PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id);


--
-- Name: command_audit_log command_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.command_audit_log
    ADD CONSTRAINT command_audit_log_pkey PRIMARY KEY (id);


--
-- Name: dashboard_daily_rollups dashboard_daily_rollups_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_daily_rollups
    ADD CONSTRAINT dashboard_daily_rollups_pkey PRIMARY KEY (account_id, rollup_date);


--
-- Name: dashboard_job_facts dashboard_job_facts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_job_facts
    ADD CONSTRAINT dashboard_job_facts_pkey PRIMARY KEY (job_id);


--
-- Name: dashboard_rollup_state dashboard_rollup_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_rollup_state
    ADD CONSTRAINT dashboard_rollup_state_pkey PRIMARY KEY (account_id);


--
-- Name: domain_event_ledger domain_event_ledger_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_event_id_key UNIQUE (event_id);


--
-- Name: domain_event_ledger domain_event_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_pkey PRIMARY KEY (id);


--
-- Name: domain_event_outbox domain_event_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_outbox
    ADD CONSTRAINT domain_event_outbox_pkey PRIMARY KEY (id);


--
-- Name: domain_event_outbox domain_event_outbox_event_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domain_event_outbox
    ADD CONSTRAINT domain_event_outbox_event_id_key UNIQUE (event_id);


--
-- Name: job_dispatch_outbox job_dispatch_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_dispatch_outbox
    ADD CONSTRAINT job_dispatch_outbox_pkey PRIMARY KEY (job_id);


--
-- Name: enabled_extensions enabled_extensions_bundle_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enabled_extensions
    ADD CONSTRAINT enabled_extensions_bundle_id_key UNIQUE (bundle_id);


--
-- Name: enabled_extensions enabled_extensions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.enabled_extensions
    ADD CONSTRAINT enabled_extensions_pkey PRIMARY KEY (id);


--
-- Name: event_delivery_attempts event_delivery_attempts_dispatch_id_attempt_no_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_delivery_attempts
    ADD CONSTRAINT event_delivery_attempts_dispatch_id_attempt_no_key UNIQUE (dispatch_id, attempt_no);


--
-- Name: event_delivery_attempts event_delivery_attempts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_delivery_attempts
    ADD CONSTRAINT event_delivery_attempts_pkey PRIMARY KEY (id);


--
-- Name: event_dispatches event_dispatches_dispatch_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_dispatch_id_key UNIQUE (dispatch_id);


--
-- Name: event_dispatches event_dispatches_event_id_target_type_target_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_event_id_target_type_target_id_key UNIQUE (event_id, target_type, target_id);


--
-- Name: event_dispatches event_dispatches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_pkey PRIMARY KEY (id);


--
-- Name: extension_bundles extension_bundles_extension_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extension_bundles
    ADD CONSTRAINT extension_bundles_extension_key_key UNIQUE (extension_key);


--
