-- Name: extension_versions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_versions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bundle_id uuid DEFAULT gen_random_uuid() NOT NULL,
    version text NOT NULL,
    manifest_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    features_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    schemas_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    healthcheck_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    entrypoint_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    checksum text,
    status text DEFAULT 'valid'::text NOT NULL,
    scan_error text,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    manifest_version integer DEFAULT 1 NOT NULL,
    required_scopes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    supported_events_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    ui_manifest_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    query_definitions_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    action_definitions_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    compiled_manifest_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    compile_errors_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    compile_warnings_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    validation_errors_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    validation_warnings_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    machine_auth_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    account_scope_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT extension_versions_status_check CHECK ((status = ANY (ARRAY['valid'::text, 'invalid'::text])))
);


--
-- Name: extension_versions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: external_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source_key text NOT NULL,
    provider_id uuid,
    display_name text NOT NULL,
    source_type text DEFAULT 'task'::text NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_sources_source_type_check CHECK ((source_type = ANY (ARRAY['task'::text, 'alert'::text, 'ci'::text, 'review'::text, 'custom'::text])))
);


--
-- Name: external_sources_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: generated_extension_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.generated_extension_jobs (
    generated_job_id text NOT NULL,
    account_id uuid NOT NULL,
    requester_user text DEFAULT ''::text NOT NULL,
    source_job_id uuid,
    status text DEFAULT 'requested'::text NOT NULL,
    visibility text DEFAULT 'private'::text NOT NULL,
    request_payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    result_release_id text,
    error_text text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT generated_extension_jobs_status_check CHECK ((status = ANY (ARRAY['requested'::text, 'queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'cancelled'::text]))),
    CONSTRAINT generated_extension_jobs_visibility_check CHECK ((visibility = 'private'::text))
);


--
-- Name: job_attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    filename text NOT NULL,
    mime_type text NOT NULL,
    size_bytes integer NOT NULL,
    upload_order integer DEFAULT 0 NOT NULL,
    content bytea,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    content_ciphertext bytea,
    content_nonce bytea,
    key_version text
);


--
-- Name: job_attachments_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: job_decision_overrides; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_decision_overrides (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    message_id uuid,
    workflow_id text,
    step_id text,
    step_execution_id uuid,
    domain text NOT NULL,
    original_value jsonb DEFAULT '{}'::jsonb NOT NULL,
    override_value jsonb NOT NULL,
    reason text NOT NULL,
    created_by text DEFAULT 'operator'::text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_decision_overrides_domain_check CHECK ((domain = ANY (ARRAY['gate_outcome'::text]))),
    CONSTRAINT job_decision_overrides_reason_check CHECK ((length(TRIM(BOTH FROM reason)) > 0))
);


--
-- Name: job_decision_overrides_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: job_errors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_errors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    message_id uuid,
    agent_id text,
    group_id text,
    step_id text,
    step_execution_id uuid,
    source text DEFAULT 'unknown'::text NOT NULL,
    category text DEFAULT 'unknown'::text NOT NULL,
    provider_name text,
    http_status integer,
    provider_error_code text,
    provider_error_type text,
    request_id text,
    retryable boolean,
    error_message text NOT NULL,
    response_excerpt text,
    traceback text,
    context_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_errors_category_check CHECK ((category = ANY (ARRAY['transient'::text, 'contract'::text, 'tool'::text, 'validation'::text, 'provider'::text, 'human_blocked'::text, 'unknown'::text]))),
    CONSTRAINT job_errors_http_status_check CHECK (((http_status IS NULL) OR ((http_status >= 100) AND (http_status <= 599)))),
    CONSTRAINT job_errors_source_check CHECK ((source = ANY (ARRAY['provider'::text, 'tool'::text, 'system'::text, 'http'::text, 'unknown'::text])))
);


--
-- Name: job_errors_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: job_external_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_external_links (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    source_id uuid,
    external_ref text NOT NULL,
    external_url text DEFAULT ''::text NOT NULL,
    link_type text DEFAULT 'task'::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_external_links_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: job_reaction_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_reaction_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid,
    workflow_id text,
    event_type text NOT NULL,
    source text DEFAULT 'external'::text NOT NULL,
    source_id text,
    event_key text,
    payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    dedupe_key text,
    processed boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_reaction_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: job_step_replays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_step_replays (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    message_id uuid,
    correlation_id text,
    requested_by text DEFAULT 'operator'::text NOT NULL,
    replay_mode text DEFAULT 'prompt_reinvoke'::text NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    prompt_version text,
    policy_snapshot jsonb DEFAULT '{}'::jsonb NOT NULL,
    captured_input jsonb DEFAULT '{}'::jsonb NOT NULL,
    original_outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    replay_outcome jsonb DEFAULT '{}'::jsonb NOT NULL,
    diff_excerpt text,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    CONSTRAINT job_step_replays_replay_mode_check CHECK ((replay_mode = ANY (ARRAY['prompt_reinvoke'::text, 'step_reexecute'::text]))),
    CONSTRAINT job_step_replays_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text])))
);


--
-- Name: job_step_replays_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    workspace_id uuid DEFAULT '00000000-0000-0000-0000-000000000003'::uuid NOT NULL,
    project_id uuid NOT NULL,
    task_id text NOT NULL,
    task_summary text,
    task_source text DEFAULT 'manual'::text NOT NULL,
    thread_id text,
    mode text,
    workflow_path jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    duration_ms integer,
    final_report text,
    error text,
    meta jsonb,
    queue_name text,
    queued_at timestamp with time zone,
    worker_id text,
    selected_workflow_id uuid,
    selected_workflow_key text,
    selected_workflow text,
    current_step_id text,
    current_group_id text,
    current_agent text,
    heartbeat_at timestamp with time zone,
    last_error text,
    last_response_excerpt text,
    acknowledged_at timestamp with time zone,
    acknowledged_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    workflow_definition_snapshot_id uuid,
    CONSTRAINT jobs_duration_ms_check CHECK (((duration_ms IS NULL) OR (duration_ms >= 0)))
);


--
-- Name: job_dispatch_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_dispatch_outbox (
    job_id uuid NOT NULL,
    encrypted_envelope text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    last_error text,
    preparation_owner_token text,
    preparation_lease_expires_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_dispatch_outbox_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT job_dispatch_outbox_status_check CHECK ((status = ANY (ARRAY['preparing'::text, 'pending'::text, 'cleanup_pending'::text, 'published'::text, 'failed'::text, 'superseded'::text])))
);


--
-- Name: jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--


--
-- Name: machine_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machine_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    service_account_id uuid NOT NULL,
    account_id uuid NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    token_hash text NOT NULL,
    scopes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_project_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_workflow_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: machine_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: mcp_connectors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_connectors (
    connector_id text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    transport text DEFAULT 'streamable_http'::text NOT NULL,
    base_url text NOT NULL,
    auth_mode text DEFAULT 'none'::text NOT NULL,
    auth_status text DEFAULT 'not_configured'::text NOT NULL,
    header_secret_refs_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    oauth_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    oauth_secret_refs_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    oauth_credential_revision integer DEFAULT 1 NOT NULL,
    oauth_connected_identity text DEFAULT ''::text NOT NULL,
    oauth_last_error text DEFAULT ''::text NOT NULL,
    allowlisted_tools jsonb DEFAULT '[]'::jsonb NOT NULL,
    namespace_prefix text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'unchecked'::text NOT NULL,
    discovered_tools_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_synced_at timestamp with time zone,
    last_sync_status text DEFAULT 'never'::text NOT NULL,
    last_sync_error text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by text,
    lifecycle_child_tool_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    catalog_item_id text DEFAULT ''::text NOT NULL,
    catalog_remote_id text DEFAULT ''::text NOT NULL,
    catalog_item_revision integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mcp_connectors_auth_mode_check CHECK ((auth_mode = ANY (ARRAY['none'::text, 'bearer'::text, 'api_key'::text, 'oauth2'::text]))),
    CONSTRAINT mcp_connectors_auth_status_check CHECK ((auth_status = ANY (ARRAY['not_configured'::text, 'discovery_required'::text, 'auth_required'::text, 'connected'::text, 'refresh_failed'::text]))),
    CONSTRAINT mcp_connectors_header_secret_refs_shape_check CHECK (((jsonb_typeof(header_secret_refs_jsonb) = 'object'::text) AND ((header_secret_refs_jsonb - 'refs'::text) = '{}'::jsonb))),
    CONSTRAINT mcp_connectors_last_sync_status_check CHECK ((last_sync_status = ANY (ARRAY['never'::text, 'success'::text, 'failed'::text]))),
    CONSTRAINT mcp_connectors_status_check CHECK ((status = ANY (ARRAY['active'::text, 'degraded'::text, 'offline'::text, 'unchecked'::text]))),
    CONSTRAINT mcp_connectors_transport_check CHECK ((transport = 'streamable_http'::text))
);

--
-- Name: model_providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.model_providers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    name text NOT NULL,
    provider_type text NOT NULL,
    base_url text,
    api_key_ref text,
    api_version text,
    deployment_name text,
    default_model text,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    verification_status text DEFAULT 'unverified'::text NOT NULL,
    verified_capabilities_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    verified_at timestamp with time zone,
    verification_started_at timestamp with time zone,
    verification_error_safe text,
    verification_generation text,
    upstream_revision text,
    lifecycle text DEFAULT 'active'::text NOT NULL,
    is_self_hosted boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_provider_sources; Type: TABLE; Schema: public; Owner: -
--
-- Account-scoped LLM provider groups contributed by remote HTTPS extensions.
-- Each row is one entry in the first ("provider") model selector. The stable
-- provider_key (llmext_ prefix) is what gets saved in agent/project model selections
-- configs; display_label is never parsed at runtime.

CREATE TABLE public.llm_provider_sources (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    extension_install_id text NOT NULL,
    extension_id text NOT NULL,
    provider_key text NOT NULL,
    display_label text DEFAULT ''::text NOT NULL,
    supplier_key text DEFAULT ''::text NOT NULL,
    supplier_label text DEFAULT ''::text NOT NULL,
    upstream_owner_key text,
    upstream_owner_label text,
    transport text DEFAULT 'extension_llm'::text NOT NULL,
    runtime_mode text DEFAULT 'remote_https'::text NOT NULL,
    models_query_id text DEFAULT 'llm.list_models'::text NOT NULL,
    chat_action_id text DEFAULT 'llm.chat'::text NOT NULL,
    structured_chat_action_id text,
    credential_secret_ref text,
    connection_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    capabilities_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_provider_models; Type: TABLE; Schema: public; Owner: -
--
-- Provider-returned models discovered inside an llm_provider_sources group.
-- model_id is the exact upstream value and is never rewritten. Only enabled
-- rows with supports_structured_output = true may enter global routing in v1.

CREATE TABLE public.llm_provider_models (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    provider_source_id uuid NOT NULL,
    model_id text NOT NULL,
    display_label text DEFAULT ''::text NOT NULL,
    upstream_owner_key text,
    upstream_owner_label text,
    capabilities_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    declared_capabilities_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    verified_capabilities_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    supports_structured_output boolean DEFAULT false NOT NULL,
    verification_status text DEFAULT 'unverified'::text NOT NULL,
    verified_at timestamp with time zone,
    verification_started_at timestamp with time zone,
    verification_error_safe text,
    verification_generation text,
    lifecycle text DEFAULT 'active'::text NOT NULL,
    deprecation_at timestamp with time zone,
    shutdown_at timestamp with time zone,
    upstream_revision text,
    upstream_digest text,
    raw_provider_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    discovered_at timestamp with time zone DEFAULT now() NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: model_providers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: notification_delivery_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_delivery_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    route_id uuid,
    project_id uuid NOT NULL,
    workflow_id text,
    job_id uuid,
    provider_id uuid,
    event_type text NOT NULL,
    severity text DEFAULT 'info'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    request_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    provider_message_id text,
    error_text text,
    external_effect_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notification_delivery_logs_severity_check CHECK ((severity = ANY (ARRAY['debug'::text, 'info'::text, 'warning'::text, 'error'::text, 'critical'::text]))),
    CONSTRAINT notification_delivery_logs_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'delivered'::text, 'failed'::text, 'skipped'::text])))
);


--
-- Name: notification_delivery_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: notification_routes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_routes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    route_key text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    workflow_id text,
    provider_id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_type text NOT NULL,
    severity text DEFAULT 'any'::text NOT NULL,
    route_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    CONSTRAINT notification_routes_severity_check CHECK ((severity = ANY (ARRAY['any'::text, 'debug'::text, 'info'::text, 'warning'::text, 'error'::text, 'critical'::text])))
);


--
-- Name: notification_routes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: extension_artifact_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_artifact_snapshots (
    snapshot_id uuid DEFAULT gen_random_uuid() NOT NULL,
    release_id text NOT NULL,
    artifact_kind text NOT NULL,
    artifact_id text NOT NULL,
    artifact_ref text DEFAULT ''::text NOT NULL,
    publish boolean DEFAULT true NOT NULL,
    snapshot_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: extension_artifact_snapshots_snapshot_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: extension_entitlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_entitlements (
    entitlement_id text NOT NULL,
    install_id text NOT NULL,
    account_id uuid NOT NULL,
    status text DEFAULT 'trial_active'::text NOT NULL,
    trial_ends_at timestamp with time zone,
    grace_ends_at timestamp with time zone,
    suspended_reason text DEFAULT ''::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_entitlements_status_check CHECK ((status = ANY (ARRAY['trial_active'::text, 'active'::text, 'grace_period'::text, 'trial_expired'::text, 'suspended_nonpayment'::text, 'disabled'::text])))
);


--
-- Name: extension_installs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_installs (
    extension_id text NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    account_id uuid NOT NULL,
    visibility text DEFAULT 'account'::text NOT NULL,
    state text DEFAULT 'active'::text NOT NULL,
    installed_by text DEFAULT ''::text NOT NULL,
    installed_at timestamp with time zone DEFAULT now() NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    install_id text NOT NULL,
    release_id text,
    descriptor_schema_version text DEFAULT 'extension_descriptor_v1'::text,
    descriptor_origin text DEFAULT 'imported'::text,
    descriptor_source text DEFAULT 'file'::text,
    provenance_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    runtime_mode text,
    remote_base_url text,
    handshake_status text DEFAULT 'unknown'::text NOT NULL,
    health_status text DEFAULT 'unknown'::text NOT NULL,
    runtime_status text DEFAULT 'unknown'::text NOT NULL,
    last_handshake_at timestamp with time zone,
    last_health_at timestamp with time zone,
    last_error text,
    state_reason text DEFAULT ''::text NOT NULL,
    disabled_at timestamp with time zone,
    uninstall_requested_at timestamp with time zone,
    drain_deadline_at timestamp with time zone,
    uninstalled_at timestamp with time zone,
    applied_data_schema_version integer DEFAULT 1 NOT NULL,
    CONSTRAINT extension_installs_applied_data_schema_version_check CHECK ((applied_data_schema_version >= 1)),
    CONSTRAINT extension_installs_runtime_status_check CHECK ((runtime_status = ANY (ARRAY['unknown'::text, 'compatible'::text, 'incompatible'::text]))),
    CONSTRAINT extension_installs_handshake_status_check CHECK ((handshake_status = ANY (ARRAY['unknown'::text, 'ok'::text, 'failed'::text]))),
    CONSTRAINT extension_installs_health_status_check CHECK ((health_status = ANY (ARRAY['unknown'::text, 'ok'::text, 'failed'::text]))),
    CONSTRAINT extension_installs_remote_activation_url_check CHECK (((state <> 'active'::text) OR (runtime_mode IS DISTINCT FROM 'remote_https'::text) OR (COALESCE(btrim(remote_base_url), ''::text) <> ''::text))),
    CONSTRAINT extension_installs_state_check CHECK ((state = ANY (ARRAY['active'::text, 'disabled'::text, 'pending'::text, 'uninstall_pending_drain'::text, 'uninstalled'::text])))
);


--
-- Name: extension_registry_projections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_registry_projections (
    projection_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    install_id text NOT NULL,
    release_id text NOT NULL,
    extension_id text NOT NULL,
    artifact_kind text NOT NULL,
    artifact_id text NOT NULL,
    artifact_ref text DEFAULT ''::text NOT NULL,
    projection_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL
);


--
-- Name: extension_registry_projections_projection_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: extension_releases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_releases (
    release_id text NOT NULL,
    extension_id text NOT NULL,
    release_version text NOT NULL,
    descriptor_schema_version text DEFAULT 'extension_descriptor_v1'::text NOT NULL,
    descriptor_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    descriptor_sha256 text DEFAULT ''::text NOT NULL,
    channel text DEFAULT 'private_byo'::text NOT NULL,
    execution_modes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    contract_version text DEFAULT ''::text NOT NULL,
    provenance_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_kind text DEFAULT 'file'::text NOT NULL,
    source_ref text DEFAULT ''::text NOT NULL,
    account_id uuid,
    created_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: extension_usage_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_usage_events (
    usage_event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    install_id text NOT NULL,
    release_id text,
    project_id uuid,
    connection_id text,
    event_kind text NOT NULL,
    quantity numeric(18,6) DEFAULT 1 NOT NULL,
    unit text DEFAULT 'unit'::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: extension_oauth_configurations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_oauth_configurations (
    configuration_id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    extension_install_id text NOT NULL,
    extension_id text NOT NULL,
    extension_release_id text NOT NULL,
    provider_id text NOT NULL,
    display_name text NOT NULL,
    configuration_revision integer DEFAULT 1 NOT NULL,
    authorization_revision integer DEFAULT 1 NOT NULL,
    status text NOT NULL,
    non_secret_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_refs jsonb DEFAULT '{}'::jsonb NOT NULL,
    provider_security_fingerprint text NOT NULL,
    provider_revocation_fingerprint text NOT NULL,
    last_error jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_oauth_configurations_configuration_revision_check CHECK ((configuration_revision >= 1)),
    CONSTRAINT extension_oauth_configurations_authorization_revision_check CHECK ((authorization_revision >= 1)),
    CONSTRAINT extension_oauth_configurations_status_check CHECK ((status = ANY (ARRAY['incomplete'::text, 'ready_for_authorization'::text, 'verified'::text, 'needs_attention'::text]))),
    CONSTRAINT extension_oauth_configurations_identity_check CHECK (((btrim(extension_install_id) <> ''::text) AND (btrim(extension_id) <> ''::text) AND (btrim(extension_release_id) <> ''::text) AND (btrim(provider_id) <> ''::text) AND (btrim(display_name) <> ''::text))),
    CONSTRAINT extension_oauth_configurations_json_shape_check CHECK (((jsonb_typeof(non_secret_config) = 'object'::text) AND (jsonb_typeof(secret_refs) = 'object'::text) AND (jsonb_typeof(last_error) = 'object'::text))),
    CONSTRAINT extension_oauth_configurations_fingerprint_check CHECK (((btrim(provider_security_fingerprint) <> ''::text) AND (btrim(provider_revocation_fingerprint) <> ''::text)))
);


--
-- Name: extension_oauth_authorization_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_oauth_authorization_states (
    state_hash text NOT NULL,
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    extension_install_id text NOT NULL,
    extension_id text NOT NULL,
    extension_release_id text NOT NULL,
    callback_id text NOT NULL,
    provider_id text NOT NULL,
    configuration_id uuid NOT NULL,
    configuration_revision integer NOT NULL,
    authorization_revision integer NOT NULL,
    connection_type text NOT NULL,
    intent_id text NOT NULL,
    existing_connection_id text,
    display_name text NOT NULL,
    intent_payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    requested_scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    redirect_uri text NOT NULL,
    pkce_verifier_encrypted text,
    provider_security_fingerprint text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_oauth_authorization_states_revision_check CHECK (((configuration_revision >= 1) AND (authorization_revision >= 1))),
    CONSTRAINT extension_oauth_authorization_states_identity_check CHECK (((btrim(state_hash) <> ''::text) AND (btrim(extension_install_id) <> ''::text) AND (btrim(extension_id) <> ''::text) AND (btrim(extension_release_id) <> ''::text) AND (btrim(callback_id) <> ''::text) AND (btrim(provider_id) <> ''::text) AND (btrim(connection_type) <> ''::text) AND (btrim(intent_id) <> ''::text) AND (btrim(display_name) <> ''::text) AND (btrim(redirect_uri) <> ''::text) AND ((pkce_verifier_encrypted IS NULL) OR (btrim(pkce_verifier_encrypted) <> ''::text)) AND (btrim(provider_security_fingerprint) <> ''::text))),
    CONSTRAINT extension_oauth_authorization_states_json_shape_check CHECK (((jsonb_typeof(intent_payload) = 'object'::text) AND (jsonb_typeof(requested_scopes) = 'array'::text))),
    CONSTRAINT extension_oauth_authorization_states_expiry_check CHECK ((expires_at > created_at))
);


--
-- Name: extension_usage_events_usage_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: personal_access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.personal_access_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    token_hash text NOT NULL,
    scopes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_project_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_workflow_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: personal_access_tokens_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: project_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_connections (
    connection_id text NOT NULL,
    project_id uuid NOT NULL,
    extension_id text,
    account_id uuid NOT NULL,
    connection_kind text DEFAULT 'extension'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    connection_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_refs jsonb DEFAULT '{}'::jsonb NOT NULL,
    connection_routes jsonb DEFAULT '[]'::jsonb NOT NULL,
    ingress_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    workflow_mappings jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    extension_install_id text,
    display_name text DEFAULT ''::text NOT NULL,
    connection_type text DEFAULT ''::text NOT NULL,
    environment text DEFAULT ''::text NOT NULL,
    credential_revision integer DEFAULT 1 NOT NULL,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    runtime_mode text,
    remote_base_url text,
    health_status text,
    runtime_status text,
    last_seen_ok_at timestamp with time zone,
    last_error text,
    oauth_configuration_id uuid,
    oauth_provider_id text,
    oauth_extension_release_id text,
    oauth_authorization_revision integer,
    oauth_requested_scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    oauth_granted_scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    oauth_provider_security_fingerprint text,
    oauth_provider_revocation_fingerprint text,
    CONSTRAINT project_connections_connection_kind_check CHECK ((connection_kind = ANY (ARRAY['extension'::text, 'mcp_connector'::text, 'extension_runtime'::text]))),
    CONSTRAINT project_connections_credential_revision_check CHECK ((credential_revision >= 1)),
    CONSTRAINT project_connections_extension_runtime_require_install_check CHECK (((connection_kind <> 'extension_runtime'::text) OR (COALESCE(extension_install_id, ''::text) <> ''::text))),
    CONSTRAINT project_connections_remote_https_base_url_check CHECK (((runtime_mode IS DISTINCT FROM 'remote_https'::text) OR (COALESCE(remote_base_url, ''::text) <> ''::text))),
    CONSTRAINT project_connections_runtime_mode_check CHECK (((runtime_mode IS NULL) OR (runtime_mode = ANY (ARRAY['local'::text, 'remote_https'::text])))),
    CONSTRAINT project_connections_oauth_metadata_check CHECK ((((oauth_configuration_id IS NULL) AND (oauth_provider_id IS NULL) AND (oauth_extension_release_id IS NULL) AND (oauth_authorization_revision IS NULL) AND (oauth_provider_security_fingerprint IS NULL) AND (oauth_provider_revocation_fingerprint IS NULL) AND (jsonb_array_length(oauth_requested_scopes) = 0) AND (jsonb_array_length(oauth_granted_scopes) = 0)) OR ((oauth_configuration_id IS NOT NULL) AND (extension_install_id IS NOT NULL) AND (btrim(extension_install_id) <> ''::text) AND (extension_id IS NOT NULL) AND (btrim(extension_id) <> ''::text) AND (oauth_provider_id IS NOT NULL) AND (btrim(oauth_provider_id) <> ''::text) AND (oauth_extension_release_id IS NOT NULL) AND (btrim(oauth_extension_release_id) <> ''::text) AND (oauth_authorization_revision IS NOT NULL) AND (oauth_authorization_revision >= 1) AND (jsonb_typeof(oauth_requested_scopes) = 'array'::text) AND (jsonb_typeof(oauth_granted_scopes) = 'array'::text) AND (oauth_provider_security_fingerprint IS NOT NULL) AND (btrim(oauth_provider_security_fingerprint) <> ''::text) AND (oauth_provider_revocation_fingerprint IS NOT NULL) AND (btrim(oauth_provider_revocation_fingerprint) <> ''::text))))
);


--
-- Name: project_connection_oauth_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_connection_oauth_states (
    state_hash text NOT NULL,
    connection_id text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    code_verifier_encrypted text NOT NULL,
    redirect_uri text NOT NULL,
    return_path text DEFAULT ''::text NOT NULL,
    auth_payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_connection_oauth_states_redirect_uri_check CHECK ((COALESCE(redirect_uri, ''::text) <> ''::text)),
    CONSTRAINT project_connection_oauth_states_state_hash_check CHECK ((COALESCE(state_hash, ''::text) <> ''::text)),
    CONSTRAINT project_connection_oauth_states_verifier_check CHECK ((COALESCE(code_verifier_encrypted, ''::text) <> ''::text))
);


--
-- Name: project_connection_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_connection_sessions (
    session_id uuid DEFAULT gen_random_uuid() NOT NULL,
    connection_id text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    scope_hash text DEFAULT 'default'::text NOT NULL,
    subject_key text DEFAULT ''::text NOT NULL,
    credential_revision integer DEFAULT 1 NOT NULL,
    encrypted_session_payload text DEFAULT ''::text NOT NULL,
    expires_at timestamp with time zone,
    refresh_after timestamp with time zone,
    lease_owner text,
    lease_expires_at timestamp with time zone,
    last_error text DEFAULT ''::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_connection_sessions_credential_revision_check CHECK ((credential_revision >= 1)),
    CONSTRAINT project_connection_sessions_scope_hash_check CHECK ((COALESCE(scope_hash, ''::text) <> ''::text))
);


--
-- Name: project_execution_leases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_execution_leases (
    project_id uuid NOT NULL,
    worker_instance_id text NOT NULL,
    lease_token text NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    active_job_id uuid,
    drain_status text DEFAULT 'acquired'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: project_execution_slots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_execution_slots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    worker_instance_id text NOT NULL,
    lease_token text NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    active_job_id uuid,
    drain_status text DEFAULT 'acquired'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: project_execution_slots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: project_policies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_policies (
    project_id uuid NOT NULL,
    autonomy_mode text DEFAULT 'supervised'::text NOT NULL,
    autonomy_confidence_threshold double precision DEFAULT 0.80 NOT NULL,
    retry_strategy text DEFAULT 'bounded'::text NOT NULL,
    retry_max_attempts integer DEFAULT 2 NOT NULL,
    retry_backoff_initial_seconds integer DEFAULT 2 NOT NULL,
    retry_backoff_max_seconds integer DEFAULT 120 NOT NULL,
    concurrency_cap integer DEFAULT 1 NOT NULL,
    tool_policy_mode text DEFAULT 'registry_only'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_project_policies_autonomy_mode CHECK ((autonomy_mode = ANY (ARRAY['manual'::text, 'supervised'::text, 'auto'::text]))),
    CONSTRAINT chk_project_policies_retry_strategy CHECK ((retry_strategy = ANY (ARRAY['bounded'::text, 'flat'::text, 'failure_class'::text]))),
    CONSTRAINT chk_project_policies_tool_policy_mode CHECK ((tool_policy_mode = ANY (ARRAY['registry_only'::text, 'restricted'::text, 'permissive'::text]))),
    CONSTRAINT project_policies_autonomy_confidence_threshold_check CHECK (((autonomy_confidence_threshold >= (0.0)::double precision) AND (autonomy_confidence_threshold <= (1.0)::double precision))),
    CONSTRAINT project_policies_concurrency_cap_check CHECK ((concurrency_cap >= 1)),
    CONSTRAINT project_policies_retry_backoff_initial_seconds_check CHECK ((retry_backoff_initial_seconds >= 0)),
    CONSTRAINT project_policies_retry_backoff_max_seconds_check CHECK ((retry_backoff_max_seconds >= 0)),
    CONSTRAINT project_policies_retry_max_attempts_check CHECK ((retry_max_attempts >= 0))
);


--
-- Name: project_secrets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_secrets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    secret_key text NOT NULL,
    secret_value_ciphertext text DEFAULT ''::text NOT NULL,
    secret_value_nonce text,
    key_version text,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: project_secrets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
