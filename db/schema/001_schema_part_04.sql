-- Name: workspace_data_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_data_connections (
    connection_id text NOT NULL,
    tenant_id uuid NOT NULL,
    account_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    connector_ref_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_kind text DEFAULT ''::text NOT NULL,
    environment text DEFAULT 'dev'::text NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    access_mode text DEFAULT 'read_only'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    health_status text DEFAULT 'unknown'::text NOT NULL,
    last_checked_at timestamp with time zone,
    last_schema_refresh_at timestamp with time zone,
    last_error text DEFAULT ''::text NOT NULL,
    connection_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_refs_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT workspace_data_connections_access_mode_check CHECK ((access_mode = 'read_only'::text)),
    CONSTRAINT workspace_data_connections_pkey PRIMARY KEY (connection_id)
);


-- Name: extension_project_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_project_state (
    state_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    extension_id text NOT NULL,
    extension_install_id text NOT NULL,
    connection_id text,
    namespace text NOT NULL,
    state_key text NOT NULL,
    value_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    expires_at timestamp with time zone,
    created_by text,
    updated_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_project_state_pkey PRIMARY KEY (state_id),
    CONSTRAINT extension_project_state_revision_check CHECK (revision >= 1),
    CONSTRAINT extension_project_state_extension_check CHECK (COALESCE(extension_id, ''::text) <> ''::text),
    CONSTRAINT extension_project_state_namespace_check CHECK (COALESCE(namespace, ''::text) <> ''::text),
    CONSTRAINT extension_project_state_key_check CHECK (COALESCE(state_key, ''::text) <> ''::text)
);


--
-- Name: external_effects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_effects (
    effect_id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid,
    extension_id text NOT NULL,
    extension_install_id text,
    connection_id text,
    operation_id text NOT NULL,
    effect_kind text NOT NULL,
    channel text NOT NULL,
    source_type text NOT NULL,
    source_ref_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text NOT NULL,
    idempotency_scope_hash text NOT NULL,
    request_fingerprint text NOT NULL,
    target_count integer DEFAULT 0 NOT NULL,
    artifact_count integer DEFAULT 0 NOT NULL,
    artifact_total_bytes bigint DEFAULT 0 NOT NULL,
    estimated_payload_bytes bigint DEFAULT 0 NOT NULL,
    target_summary_redacted text DEFAULT ''::text NOT NULL,
    target_hashes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    artifact_refs_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'claimed'::text NOT NULL,
    status_reason text DEFAULT ''::text NOT NULL,
    provider_message_id text,
    external_object_id text,
    error_code text,
    error_text text,
    response_summary_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    test_mode boolean DEFAULT false NOT NULL,
    claimed_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    expires_at timestamp with time zone,
    claim_version bigint DEFAULT 1 NOT NULL,
    claim_token text DEFAULT gen_random_uuid()::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_effects_pkey PRIMARY KEY (effect_id),
    CONSTRAINT external_effects_effect_account_key UNIQUE (effect_id, account_id),
    CONSTRAINT external_effects_status_check CHECK (status = ANY (ARRAY['claimed'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'timeout_unknown'::text, 'suppressed'::text, 'expired'::text])),
    CONSTRAINT external_effects_idempotency_key_check CHECK (COALESCE(idempotency_key, ''::text) <> ''::text),
    CONSTRAINT external_effects_scope_hash_check CHECK (COALESCE(idempotency_scope_hash, ''::text) <> ''::text),
    CONSTRAINT external_effects_target_count_check CHECK (target_count >= 0),
    CONSTRAINT external_effects_artifact_count_check CHECK (artifact_count >= 0),
    CONSTRAINT external_effects_artifact_total_bytes_check CHECK (artifact_total_bytes >= 0),
    CONSTRAINT external_effects_estimated_payload_bytes_check CHECK (estimated_payload_bytes >= 0),
    CONSTRAINT external_effects_claim_version_check CHECK (claim_version >= 1),
    CONSTRAINT external_effects_claim_token_check CHECK (COALESCE(claim_token, ''::text) <> ''::text),
    CONSTRAINT external_effects_connection_requires_project_check CHECK (connection_id IS NULL OR project_id IS NOT NULL)
);


--
-- Name: external_effect_replays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_effect_replays (
    effect_id uuid NOT NULL,
    account_id uuid NOT NULL,
    replay_state text DEFAULT 'available'::text NOT NULL,
    replay_reason text,
    encrypted_payload text,
    replay_binding_hash text NOT NULL,
    plaintext_bytes integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_effect_replays_pkey PRIMARY KEY (effect_id),
    CONSTRAINT external_effect_replays_state_check CHECK (replay_state = ANY (ARRAY['available'::text, 'nonreplayable'::text])),
    CONSTRAINT external_effect_replays_payload_state_check CHECK ((replay_state = 'available'::text AND encrypted_payload IS NOT NULL AND replay_reason IS NULL) OR (replay_state = 'nonreplayable'::text AND encrypted_payload IS NULL AND COALESCE(replay_reason, ''::text) <> ''::text)),
    CONSTRAINT external_effect_replays_plaintext_bytes_check CHECK (plaintext_bytes >= 0 AND plaintext_bytes <= 524288),
    CONSTRAINT external_effect_replays_ciphertext_bytes_check CHECK (encrypted_payload IS NULL OR octet_length(encrypted_payload) <= 700000)
);

--
-- Name: external_effect_output_reconciliations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_effect_output_reconciliations (
    effect_id uuid NOT NULL,
    account_id uuid NOT NULL,
    claim_token text NOT NULL,
    claim_version bigint NOT NULL,
    minted_artifact_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    minted_dataset_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    lifecycle_state text DEFAULT 'pending'::text NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + interval '24 hours') NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_effect_output_reconciliations_pkey PRIMARY KEY (effect_id, account_id, claim_version),
    CONSTRAINT external_effect_output_reconciliations_state_check CHECK (lifecycle_state = 'pending'::text),
    CONSTRAINT external_effect_output_reconciliations_claim_version_check CHECK (claim_version >= 1),
    CONSTRAINT external_effect_output_reconciliations_artifacts_check CHECK (jsonb_typeof(minted_artifact_ids_jsonb) = 'array'::text AND jsonb_array_length(minted_artifact_ids_jsonb) <= 128),
    CONSTRAINT external_effect_output_reconciliations_datasets_check CHECK (jsonb_typeof(minted_dataset_ids_jsonb) = 'array'::text AND jsonb_array_length(minted_dataset_ids_jsonb) <= 128)
);

CREATE TABLE public.external_effect_maintenance_cursors (
    account_id uuid NOT NULL,
    last_attempted_at timestamp with time zone NOT NULL,
    CONSTRAINT external_effect_maintenance_cursors_pkey PRIMARY KEY (account_id)
);


--
-- Name: external_effect_rate_windows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.external_effect_rate_windows (
    scope_hash text NOT NULL,
    scope_type text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid,
    connection_id text,
    effect_kind text NOT NULL,
    channel text NOT NULL,
    window_started_at timestamp with time zone NOT NULL,
    effect_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT external_effect_rate_windows_pkey PRIMARY KEY (scope_hash, window_started_at),
    CONSTRAINT external_effect_rate_windows_scope_type_check CHECK (scope_type = ANY (ARRAY['account'::text, 'project'::text, 'connection'::text])),
    CONSTRAINT external_effect_rate_windows_count_check CHECK (effect_count >= 0),
    CONSTRAINT external_effect_rate_windows_project_scope_check CHECK ((scope_type = 'account'::text AND project_id IS NULL AND connection_id IS NULL) OR (scope_type = 'project'::text AND project_id IS NOT NULL AND connection_id IS NULL) OR (scope_type = 'connection'::text AND project_id IS NOT NULL AND connection_id IS NOT NULL))
);


--
-- Name: project_data_connections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_data_connections (
    project_id uuid NOT NULL,
    connection_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT project_data_connections_pkey PRIMARY KEY (project_id, connection_id),
    CONSTRAINT project_data_connections_connection_id_fkey FOREIGN KEY (connection_id) REFERENCES public.workspace_data_connections(connection_id) ON DELETE CASCADE,
    CONSTRAINT project_data_connections_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE
);


--
-- Name: project_datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_datasets (
    dataset_id text NOT NULL,
    project_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: dataset_query_templates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dataset_query_templates (
    query_template_id text NOT NULL,
    dataset_id text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    query_language text DEFAULT 'sql'::text NOT NULL,
    query_definition_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    params_schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    validation_result_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    approved_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: registry_fields; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_fields (
    field_id uuid DEFAULT gen_random_uuid() NOT NULL,
    field_key text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    value_type text DEFAULT 'text'::text NOT NULL,
    data_contract_id text,
    created_from_kind text DEFAULT 'manual'::text NOT NULL,
    created_from_ref text DEFAULT ''::text,
    source_hints_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    manual_allowed boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    projection_mode text DEFAULT 'manual'::text NOT NULL,
    derived_from_kind text DEFAULT ''::text,
    derived_from_ref text DEFAULT ''::text,
    derived_hash text DEFAULT ''::text,
    sync_status text DEFAULT 'active'::text NOT NULL,
    last_synced_at timestamp with time zone,
    last_sync_error text
);


--
-- Name: registry_fields_field_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_guardrail_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_guardrail_agents (
    guardrail_id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL
);


--
-- Name: registry_guardrails; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_guardrails (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    mode text NOT NULL,
    rule_type text NOT NULL,
    severity text DEFAULT 'error'::text NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    prompt_text text DEFAULT ''::text,
    agent_prompt text DEFAULT ''::text,
    project_id uuid,
    sort_order integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    created_by text,
    updated_by text,
    deleted_by text,
    CONSTRAINT registry_guardrails_mode_check CHECK ((mode = ANY (ARRAY['deterministic'::text, 'semantic'::text]))),
    CONSTRAINT registry_guardrails_severity_check CHECK ((severity = ANY (ARRAY['error'::text, 'warning'::text])))
);


--
-- Name: registry_guardrails_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_published_block_contracts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_published_block_contracts (
    block_key text NOT NULL,
    display_name text NOT NULL,
    block_type text DEFAULT 'agent'::text NOT NULL,
    source_kind text NOT NULL,
    source_ref text NOT NULL,
    description text DEFAULT ''::text,
    family text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    projection_mode text DEFAULT 'manual'::text NOT NULL,
    derived_from_kind text DEFAULT ''::text,
    derived_from_ref text DEFAULT ''::text,
    derived_hash text DEFAULT ''::text,
    sync_status text DEFAULT 'active'::text NOT NULL,
    last_synced_at timestamp with time zone,
    last_sync_error text,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    source_install_id text,
    source_release_id text,
    source_projection_id uuid,
    local_block_key text DEFAULT ''::text NOT NULL,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL
);


--
-- Name: registry_published_block_inputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_published_block_inputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    block_key text NOT NULL,
    field_id uuid DEFAULT gen_random_uuid() NOT NULL,
    required boolean DEFAULT false NOT NULL,
    multiple_allowed boolean DEFAULT false NOT NULL,
    manual_allowed_override boolean,
    default_source_hint text DEFAULT ''::text,
    notes text DEFAULT ''::text,
    sort_order integer DEFAULT 0 NOT NULL
);


--
-- Name: registry_published_block_inputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_published_block_outputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_published_block_outputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    block_key text NOT NULL,
    field_id uuid DEFAULT gen_random_uuid() NOT NULL,
    required_by_default boolean DEFAULT false NOT NULL,
    exposed_by_default boolean DEFAULT true NOT NULL,
    notes text DEFAULT ''::text,
    sort_order integer DEFAULT 0 NOT NULL
);


--
-- Name: registry_published_block_outputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_skill_inputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_skill_inputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    skill_id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text,
    required boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    type text DEFAULT 'text'::text NOT NULL,
    contract_id text
);


--
-- Name: registry_skill_inputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_skill_outputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_skill_outputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    skill_id text NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text,
    sort_order integer DEFAULT 0 NOT NULL,
    type text DEFAULT 'text'::text NOT NULL,
    required boolean DEFAULT false NOT NULL,
    contract_id text
);


--
-- Name: registry_skill_outputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_skills (
    skill_id text NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    category text DEFAULT ''::text,
    instructions text[] DEFAULT '{}'::text[] NOT NULL,
    prompt_template text DEFAULT ''::text,
    examples jsonb DEFAULT '[]'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    input_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_schema jsonb DEFAULT '{}'::jsonb NOT NULL,
    deleted_at timestamp with time zone,
    skill_type text DEFAULT 'secondary'::text NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid,
    project_id uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    source_install_id text,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL
);


--
-- Name: registry_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_tools (
    tool_id text NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    category text DEFAULT 'general'::text NOT NULL,
    prompt text DEFAULT ''::text,
    interface jsonb DEFAULT '{}'::jsonb NOT NULL,
    implementation jsonb DEFAULT '{}'::jsonb NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid,
    project_id uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    source_install_id text,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL
);


--
-- Name: registry_workflow_for_each; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflow_for_each (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id text NOT NULL,
    for_each_id text NOT NULL,
    group_id uuid,
    display_name text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text,
    sort_order integer DEFAULT 0 NOT NULL,
    over_selector text NOT NULL,
    item_name text NOT NULL,
    index_name text,
    body_jsonb jsonb NOT NULL,
    error_policy_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    boundary_contract_jsonb jsonb,
    CONSTRAINT registry_workflow_for_each_for_each_id_nonblank_check CHECK ((btrim(for_each_id) <> ''::text)),
    CONSTRAINT registry_workflow_for_each_item_name_nonblank_check CHECK ((btrim(item_name) <> ''::text))
);


--
-- Name: registry_workflow_groups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflow_groups (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_id text NOT NULL,
    group_id text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text,
    sort_order integer DEFAULT 0 NOT NULL,
    body_jsonb jsonb NOT NULL,
    error_policy_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    boundary_contract_jsonb jsonb,
    CONSTRAINT registry_workflow_groups_group_id_nonblank_check CHECK ((btrim(group_id) <> ''::text))
);


--
-- Name: registry_workflow_hook_binding_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflow_hook_binding_revisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    binding_id uuid NOT NULL,
    revision integer NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    target_workflow_id text,
    input_mapping_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_mapping_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    hook_contract_version integer NOT NULL,
    hook_semantic_digest text NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT registry_workflow_hook_binding_revisions_revision_check CHECK ((revision >= 1)),
    CONSTRAINT registry_workflow_hook_binding_revisions_contract_version_check CHECK ((hook_contract_version >= 1)),
    CONSTRAINT registry_workflow_hook_binding_revisions_target_nonblank_check CHECK (((target_workflow_id IS NULL) OR (btrim(target_workflow_id) <> ''::text))),
    CONSTRAINT registry_workflow_hook_binding_revisions_cleared_check CHECK (((target_workflow_id IS NOT NULL) OR ((input_mapping_jsonb = '{}'::jsonb) AND (output_mapping_jsonb = '{}'::jsonb))))
);


--
-- Name: registry_workflow_hook_bindings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflow_hook_bindings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    source_install_id text DEFAULT ''::text NOT NULL,
    local_workflow_id text NOT NULL,
    hook_id text NOT NULL,
    revision integer DEFAULT 0 NOT NULL,
    active_revision_id uuid,
    staged_revision_id uuid,
    staged_release_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT registry_workflow_hook_bindings_staged_check CHECK (((staged_revision_id IS NULL) = (staged_release_id IS NULL))),
    CONSTRAINT registry_workflow_hook_bindings_hook_id_check CHECK ((hook_id ~ '^[a-z][a-z0-9_]*$'::text)),
    CONSTRAINT registry_workflow_hook_bindings_local_workflow_id_nonblank_check CHECK ((btrim(local_workflow_id) <> ''::text)),
    CONSTRAINT registry_workflow_hook_bindings_revision_check CHECK ((revision >= 0))
);


--
-- Name: registry_workflow_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflow_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_workflow_id text NOT NULL,
    group_id uuid,
    for_each_id uuid,
    lane_id text,
    agent_id text DEFAULT ''::text,
    skill_id text,
    sort_order integer DEFAULT 0 NOT NULL,
    optional boolean DEFAULT false NOT NULL,
    disabled boolean DEFAULT false NOT NULL,
    context_inject jsonb,
    override jsonb,
    step_kind text DEFAULT 'agent'::text NOT NULL,
    participant_key text DEFAULT ''::text NOT NULL,
    connector_id text,
    workflow_id text,
    step_id text NOT NULL,
    block_key text DEFAULT ''::text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    input_mapping_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    output_mapping_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    connection_binding_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_policy_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    boundary_contract_jsonb jsonb,
    connector_install_id text DEFAULT ''::text NOT NULL,
    CONSTRAINT registry_workflow_steps_lane_requires_group_check CHECK (((group_id IS NOT NULL) OR (for_each_id IS NOT NULL) OR (lane_id IS NULL))),
    CONSTRAINT registry_workflow_steps_one_owner_check CHECK (((group_id IS NULL) OR (for_each_id IS NULL))),
    CONSTRAINT registry_workflow_steps_lane_id_nonblank_check CHECK (((lane_id IS NULL) OR (btrim(lane_id) <> ''::text))),
    CONSTRAINT registry_workflow_steps_step_id_nonblank_check CHECK ((btrim(step_id) <> ''::text)),
    CONSTRAINT registry_workflow_steps_step_kind_check CHECK ((step_kind = ANY (ARRAY['agent'::text, 'connector'::text, 'invoke_workflow'::text, 'if'::text, 'switch'::text, 'stop_success'::text, 'stop_fail'::text, 'set_variable'::text, 'batch'::text, 'break'::text, 'continue'::text, 'collect'::text, 'http_request'::text, 'dataset_query'::text, 'transform'::text, 'tabular_feed'::text, 'file_from_base64'::text, 'template'::text, 'approval'::text, 'wait'::text])))
);


--
-- Name: registry_workflows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_workflows (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workflow_key text NOT NULL,
    workflow_id text GENERATED ALWAYS AS (workflow_key) STORED,
    version text DEFAULT '1.0.0'::text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    trigger jsonb DEFAULT '{}'::jsonb NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    source_install_id text,
    source_release_id text,
    source_projection_id uuid,
    local_workflow_id text DEFAULT ''::text NOT NULL,
    projection_mode text DEFAULT 'manual'::text NOT NULL,
    derived_from_kind text DEFAULT ''::text NOT NULL,
    derived_from_ref text DEFAULT ''::text NOT NULL,
    sync_status text DEFAULT 'active'::text NOT NULL,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    CONSTRAINT registry_workflows_revision_positive CHECK ((revision >= 1))
);


--
-- Name: registry_step_kind_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_step_kind_profiles (
    step_kind_key text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    config_schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: report_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.report_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    project_id uuid,
    report_type text NOT NULL,
    params_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    summary_text text DEFAULT ''::text NOT NULL,
    chart_payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'generated'::text NOT NULL,
    generated_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    payload_jsonb jsonb
);


--
-- Name: roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key text NOT NULL,
    label text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: roles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: scheduled_job_execution_locks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_job_execution_locks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scheduled_job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    workflow_id text DEFAULT ''::text NOT NULL,
    lock_key text NOT NULL,
    slot_ordinal integer DEFAULT 1 NOT NULL,
    job_id uuid,
    run_type text DEFAULT 'scheduled'::text NOT NULL,
    status text DEFAULT 'pre_publish'::text NOT NULL,
    scheduled_for timestamp with time zone,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    activated_at timestamp with time zone,
    released_at timestamp with time zone,
    release_reason text DEFAULT ''::text NOT NULL,
    scheduler_instance_id text DEFAULT ''::text NOT NULL,
    worker_instance_id text DEFAULT ''::text NOT NULL,
    expires_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_scheduled_job_execution_locks_run_type CHECK ((run_type = ANY (ARRAY['scheduled'::text, 'manual'::text]))),
    CONSTRAINT chk_scheduled_job_execution_locks_slot_ordinal CHECK ((slot_ordinal >= 1)),
    CONSTRAINT chk_scheduled_job_execution_locks_status CHECK ((status = ANY (ARRAY['pre_publish'::text, 'active'::text, 'released'::text, 'stale'::text]))),
    CONSTRAINT scheduled_job_execution_locks_run_type_check CHECK ((run_type = ANY (ARRAY['scheduled'::text, 'manual'::text]))),
    CONSTRAINT scheduled_job_execution_locks_slot_ordinal_check CHECK ((slot_ordinal >= 1)),
    CONSTRAINT scheduled_job_execution_locks_status_check CHECK ((status = ANY (ARRAY['pre_publish'::text, 'active'::text, 'released'::text, 'stale'::text])))
);


--
-- Name: scheduled_job_execution_locks_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: scheduled_job_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_job_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    scheduled_job_id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    workflow_id text NOT NULL,
    run_type text DEFAULT 'scheduled'::text NOT NULL,
    status text NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    job_id uuid,
    scheduled_for timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    detail_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    account_id uuid,
    CONSTRAINT chk_scheduled_job_runs_run_type CHECK ((run_type = ANY (ARRAY['scheduled'::text, 'manual'::text]))),
    CONSTRAINT chk_scheduled_job_runs_status CHECK ((status = ANY (ARRAY['enqueued'::text, 'skipped'::text, 'failed'::text]))),
    CONSTRAINT scheduled_job_runs_run_type_check CHECK ((run_type = ANY (ARRAY['scheduled'::text, 'manual'::text]))),
    CONSTRAINT scheduled_job_runs_status_check CHECK ((status = ANY (ARRAY['enqueued'::text, 'skipped'::text, 'failed'::text])))
);


--
-- Name: scheduled_job_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: scheduled_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.scheduled_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    workflow_id text NOT NULL,
    cron_expr text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_job_id uuid,
    last_run_status text DEFAULT ''::text NOT NULL,
    last_skip_reason text DEFAULT ''::text NOT NULL,
    last_error text DEFAULT ''::text NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid,
    inputs jsonb DEFAULT '{}'::jsonb NOT NULL,
    schedule_timezone text DEFAULT ''::text NOT NULL,
    admission_allowed boolean DEFAULT false NOT NULL,
    admission_reason text DEFAULT 'not_evaluated'::text NOT NULL,
    admission_workflow_revision integer,
    admission_checked_at timestamp with time zone,
    CONSTRAINT chk_scheduled_jobs_status CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'error'::text]))),
    CONSTRAINT scheduled_jobs_status_check CHECK ((status = ANY (ARRAY['active'::text, 'paused'::text, 'error'::text])))
);


--
-- Name: scheduled_jobs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: secret_refs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.secret_refs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    project_id uuid,
    tenant_id uuid,
    ref_key text NOT NULL,
    encrypted_value text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: secret_refs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: security_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    project_id uuid,
    job_id uuid,
    surface text NOT NULL,
    scanner text NOT NULL,
    rule_id text DEFAULT ''::text NOT NULL,
    severity text NOT NULL,
    action text NOT NULL,
    redacted_preview text,
    trace_id text,
    actor_id text,
    scan_id text,
    policy_level text,
    duration_ms double precision,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT security_events_action_check CHECK ((action = ANY (ARRAY['allow'::text, 'warn'::text, 'redact'::text, 'block'::text]))),
    CONSTRAINT security_events_severity_check CHECK ((severity = ANY (ARRAY['low'::text, 'medium'::text, 'high'::text, 'critical'::text])))
);


--
-- Name: security_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: security_findings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_findings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid DEFAULT gen_random_uuid() NOT NULL,
    scanner text NOT NULL,
    rule_id text NOT NULL,
    severity text NOT NULL,
    confidence double precision DEFAULT 1.0 NOT NULL,
    context jsonb,
    redacted_snippet text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: security_findings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: security_posture_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.security_posture_snapshots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    project_id uuid NOT NULL,
    score integer NOT NULL,
    level text NOT NULL,
    category_scores jsonb DEFAULT '{}'::jsonb NOT NULL,
    finding_counts jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT security_posture_snapshots_level_check CHECK ((level = ANY (ARRAY['hardened'::text, 'secure'::text, 'needs_attention'::text, 'at_risk'::text]))),
    CONSTRAINT security_posture_snapshots_score_check CHECK (((score >= 0) AND (score <= 100)))
);


--
-- Name: security_posture_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: service_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    service_account_key text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    scopes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_project_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    allowed_workflow_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by text DEFAULT 'system'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: service_accounts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: system_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_settings (
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    setting_key text NOT NULL,
    setting_value_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text DEFAULT 'system'::text NOT NULL
);


--
-- Name: user_account_memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_account_memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    account_id uuid NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_account_memberships_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_roles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    role_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_roles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    username text NOT NULL,
    password_hash text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    must_change_password boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by text,
    timezone character varying(64),
    locale character varying(16),
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
