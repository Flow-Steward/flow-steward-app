-- Name: project_settings_revisions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_settings_revisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid NOT NULL,
    author text DEFAULT 'system'::text NOT NULL,
    action text DEFAULT 'save'::text NOT NULL,
    source text DEFAULT 'ui'::text NOT NULL,
    snapshot_jsonb jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: project_settings_revisions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: project_workflows; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_workflows (
    project_id uuid NOT NULL,
    workflow_id text NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    workspace_id uuid DEFAULT '00000000-0000-0000-0000-000000000003'::uuid NOT NULL,
    name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    config jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    extra jsonb,
    agents_config jsonb DEFAULT '{}'::jsonb,
    project_rules text,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by text,
    schedule_timezone character varying(64) DEFAULT 'UTC'::character varying NOT NULL,
    CONSTRAINT projects_pkey PRIMARY KEY (id)
);


--
-- Name: memory_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_records (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    project_id uuid,
    workflow_id text,
    job_id uuid,
    session_id uuid,
    memory_type text NOT NULL,
    visibility text NOT NULL,
    lifecycle_status text NOT NULL,
    title text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    source_type text NOT NULL,
    source_id text DEFAULT ''::text NOT NULL,
    source_trust text NOT NULL,
    evidence_refs_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    confidence double precision,
    embedding public.vector(1536),
    importance double precision DEFAULT 0.5 NOT NULL,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    supersedes_id uuid,
    retention_policy text,
    expires_at timestamp with time zone,
    tags_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_records_pkey PRIMARY KEY (id),
    CONSTRAINT memory_records_memory_type_check CHECK ((memory_type = ANY (ARRAY['fact'::text, 'decision'::text, 'lesson'::text, 'project_rule'::text, 'preference'::text, 'known_issue'::text, 'workflow_pattern'::text, 'extension_note'::text, 'verification_result'::text, 'procedural_playbook'::text, 'docs_note'::text, 'dataset_note'::text]))),
    CONSTRAINT memory_records_visibility_check CHECK ((visibility = ANY (ARRAY['private_user'::text, 'project'::text, 'account'::text, 'tenant'::text, 'system'::text]))),
    CONSTRAINT memory_records_lifecycle_status_check CHECK ((lifecycle_status = ANY (ARRAY['candidate'::text, 'active'::text, 'superseded'::text, 'rejected'::text, 'expired'::text]))),
    CONSTRAINT memory_records_source_trust_check CHECK ((source_trust = ANY (ARRAY['user_supplied'::text, 'agent_inferred'::text, 'tool_observed'::text, 'system_generated'::text, 'human_approved'::text, 'canonical_doc'::text]))),
    CONSTRAINT memory_records_importance_check CHECK (((importance >= (0)::double precision) AND (importance <= (1)::double precision))),
    CONSTRAINT memory_records_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision))))
);


--
-- Name: memory_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_entities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    project_id uuid,
    entity_type text NOT NULL,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_entities_pkey PRIMARY KEY (id)
);


--
-- Name: memory_entity_aliases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_entity_aliases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_id uuid NOT NULL,
    alias text NOT NULL,
    normalized_alias text NOT NULL,
    language_code text DEFAULT ''::text NOT NULL,
    provenance_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_entity_aliases_pkey PRIMARY KEY (id),
    CONSTRAINT memory_entity_aliases_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.memory_entities(id) ON DELETE CASCADE
);


--
-- Name: memory_record_entities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_record_entities (
    record_id uuid NOT NULL,
    entity_id uuid NOT NULL,
    mention_text text DEFAULT ''::text NOT NULL,
    mention_role text DEFAULT 'mentioned'::text NOT NULL,
    confidence double precision,
    provenance_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_record_entities_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))),
    CONSTRAINT memory_record_entities_dedupe_key UNIQUE (record_id, entity_id, mention_role, mention_text),
    CONSTRAINT memory_record_entities_record_id_fkey FOREIGN KEY (record_id) REFERENCES public.memory_records(id) ON DELETE CASCADE,
    CONSTRAINT memory_record_entities_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.memory_entities(id) ON DELETE CASCADE
);


--
-- Name: memory_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    account_id uuid NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    project_id uuid,
    workflow_id text,
    job_id uuid,
    session_id uuid,
    edge_type text NOT NULL,
    from_record_id uuid,
    to_record_id uuid,
    from_entity_id uuid,
    to_entity_id uuid,
    confidence double precision,
    provenance_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    valid_from timestamp with time zone,
    valid_to timestamp with time zone,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT memory_edges_pkey PRIMARY KEY (id),
    CONSTRAINT memory_edges_confidence_check CHECK (((confidence IS NULL) OR ((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))),
    CONSTRAINT memory_edges_from_record_id_fkey FOREIGN KEY (from_record_id) REFERENCES public.memory_records(id) ON DELETE CASCADE,
    CONSTRAINT memory_edges_to_record_id_fkey FOREIGN KEY (to_record_id) REFERENCES public.memory_records(id) ON DELETE CASCADE,
    CONSTRAINT memory_edges_from_entity_id_fkey FOREIGN KEY (from_entity_id) REFERENCES public.memory_entities(id) ON DELETE CASCADE,
    CONSTRAINT memory_edges_to_entity_id_fkey FOREIGN KEY (to_entity_id) REFERENCES public.memory_entities(id) ON DELETE CASCADE
);


--
-- Name: projects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: idx_memory_records_scope_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_scope_created ON public.memory_records USING btree (account_id, tenant_id, project_id, created_at DESC, id DESC);


--
-- Name: idx_memory_records_scope_lifecycle; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_scope_lifecycle ON public.memory_records USING btree (account_id, tenant_id, project_id, lifecycle_status, memory_type, visibility);


--
-- Name: idx_memory_records_source_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_source_ref ON public.memory_records USING btree (account_id, source_type, source_id);


--
-- Name: idx_memory_records_project_workflow_job; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_project_workflow_job ON public.memory_records USING btree (project_id, workflow_id, job_id, session_id);


--
-- Name: idx_memory_records_embedding_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_embedding_cosine ON public.memory_records USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_memory_records_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_records_search ON public.memory_records USING gin (to_tsvector('simple'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || (COALESCE(summary, ''::text) || ' '::text)) || COALESCE(content, ''::text)));


--
-- Name: uidx_memory_records_source_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uidx_memory_records_source_ref ON public.memory_records USING btree (account_id, source_type, source_id) WHERE (source_id <> ''::text);


--
-- Name: idx_memory_entities_scope_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_entities_scope_name ON public.memory_entities USING btree (account_id, project_id, entity_type, normalized_name);


--
-- Name: uidx_memory_entity_aliases_normalized; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uidx_memory_entity_aliases_normalized ON public.memory_entity_aliases USING btree (entity_id, normalized_alias, language_code);


--
-- Name: idx_memory_record_entities_record; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_record_entities_record ON public.memory_record_entities USING btree (record_id, entity_id, mention_role);


--
-- Name: idx_memory_edges_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_edges_scope ON public.memory_edges USING btree (account_id, project_id, edge_type, created_at DESC, id DESC);


--
-- Name: idx_memory_edges_records; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_memory_edges_records ON public.memory_edges USING btree (from_record_id, to_record_id, from_entity_id, to_entity_id);


--
-- Name: platform_workflow_knowledge_nodes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_workflow_knowledge_nodes (
    account_id text NOT NULL,
    tenant_id text DEFAULT ''::text NOT NULL,
    project_scope text DEFAULT ''::text NOT NULL,
    entity_id text NOT NULL,
    entity_kind text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    content_text text DEFAULT ''::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_workflow_knowledge_nodes_pkey PRIMARY KEY (account_id, tenant_id, project_scope, entity_id)
);


--
-- Name: platform_workflow_knowledge_edges; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_workflow_knowledge_edges (
    account_id text NOT NULL,
    tenant_id text DEFAULT ''::text NOT NULL,
    project_scope text DEFAULT ''::text NOT NULL,
    edge_id text NOT NULL,
    edge_kind text NOT NULL,
    source_id text NOT NULL,
    target_id text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_workflow_knowledge_edges_pkey PRIMARY KEY (account_id, tenant_id, project_scope, edge_id)
);


--
-- Name: platform_workflow_knowledge_documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_workflow_knowledge_documents (
    account_id text NOT NULL,
    tenant_id text DEFAULT ''::text NOT NULL,
    project_scope text DEFAULT ''::text NOT NULL,
    doc_id text NOT NULL,
    entity_id text NOT NULL,
    entity_kind text NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    content_text text DEFAULT ''::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    embedding_jsonb jsonb,
    embedding public.vector(1536),
    indexed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_workflow_knowledge_documents_pkey PRIMARY KEY (account_id, tenant_id, project_scope, doc_id)
);


--
-- Name: idx_platform_workflow_knowledge_nodes_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_workflow_knowledge_nodes_scope ON public.platform_workflow_knowledge_nodes USING btree (account_id, tenant_id, project_scope, entity_kind, entity_id);


--
-- Name: idx_platform_workflow_knowledge_edges_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_workflow_knowledge_edges_scope ON public.platform_workflow_knowledge_edges USING btree (account_id, tenant_id, project_scope, source_id, target_id, edge_kind);


--
-- Name: idx_platform_workflow_knowledge_documents_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_workflow_knowledge_documents_scope ON public.platform_workflow_knowledge_documents USING btree (account_id, tenant_id, project_scope, entity_kind, entity_id);


--
-- Name: idx_platform_workflow_knowledge_documents_search; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_workflow_knowledge_documents_search ON public.platform_workflow_knowledge_documents USING gin (to_tsvector('simple'::regconfig, ((COALESCE(title, ''::text) || ' '::text) || COALESCE(content_text, ''::text))));


--
-- Name: idx_platform_workflow_knowledge_docs_embedding_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_platform_workflow_knowledge_docs_embedding_cosine ON public.platform_workflow_knowledge_documents USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: provider_health_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_health_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider_id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    status text DEFAULT 'unknown'::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_health_events_status_check CHECK ((status = ANY (ARRAY['healthy'::text, 'degraded'::text, 'failed'::text, 'unknown'::text])))
);


--
-- Name: provider_health_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: provider_secrets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_secrets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider_id uuid,
    project_id uuid,
    secret_key text NOT NULL,
    secret_value_ciphertext text DEFAULT ''::text NOT NULL,
    secret_value_nonce text,
    key_version text,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_secrets_check CHECK (((provider_id IS NOT NULL) OR (project_id IS NOT NULL)))
);


--
-- Name: provider_secrets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: provider_types; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.provider_types (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    key text NOT NULL,
    label text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: provider_types_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: providers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.providers (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provider_key text NOT NULL,
    provider_type_id uuid DEFAULT gen_random_uuid() NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    implementation_kind text DEFAULT 'builtin'::text NOT NULL,
    integration_key text,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    features_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: providers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_agent_inputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_agent_inputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    name text NOT NULL,
    type text DEFAULT 'text'::text NOT NULL,
    description text DEFAULT ''::text,
    required boolean DEFAULT false NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    source text DEFAULT 'own'::text NOT NULL
);


--
-- Name: registry_agent_inputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_agent_outputs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_agent_outputs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    agent_id text NOT NULL,
    name text NOT NULL,
    type text DEFAULT 'text'::text NOT NULL,
    description text DEFAULT ''::text,
    required boolean DEFAULT true NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    source text DEFAULT 'own'::text NOT NULL
);


--
-- Name: registry_agent_outputs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_agent_skills; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_agent_skills (
    agent_id text NOT NULL,
    skill_id text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    skill_prompt text DEFAULT ''::text
);


--
-- Name: registry_agent_tools; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_agent_tools (
    agent_id text NOT NULL,
    tool_id text NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL
);


--
-- Name: registry_agents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_agents (
    agent_id text NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    enabled boolean DEFAULT true NOT NULL,
    prompts jsonb DEFAULT '{}'::jsonb NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    quality jsonb DEFAULT '{}'::jsonb NOT NULL,
    inputs jsonb DEFAULT '{"optional": [], "required": []}'::jsonb NOT NULL,
    outputs text[] DEFAULT '{}'::text[] NOT NULL,
    builder_metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    depends_on jsonb DEFAULT '{"hard": [], "soft": []}'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    agent_type_id uuid DEFAULT gen_random_uuid() NOT NULL,
    deleted_at timestamp with time zone,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid,
    project_id uuid,
    scope_kind text DEFAULT 'system'::text NOT NULL,
    source_install_id text,
    availability_status text DEFAULT 'available'::text NOT NULL,
    availability_reason_code text DEFAULT 'none'::text NOT NULL,
    availability_reason_text text DEFAULT ''::text NOT NULL,
    new_runs_blocked boolean DEFAULT false NOT NULL,
    visible_in_catalog boolean DEFAULT true NOT NULL,
    category text DEFAULT ''::text NOT NULL
);


--
-- Name: registry_changelog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_changelog (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    action text NOT NULL,
    snapshot jsonb NOT NULL,
    author text DEFAULT 'system'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    actor_id text,
    actor_type text,
    account_id uuid
);


--
-- Name: registry_changelog_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: registry_data_contracts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_data_contracts (
    contract_id text NOT NULL,
    version text DEFAULT '1.0.0'::text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    kind text DEFAULT 'json'::text NOT NULL,
    schema_json jsonb DEFAULT '{"type": "object", "additionalProperties": true}'::jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    projection_mode text DEFAULT 'manual'::text NOT NULL,
    derived_from_kind text DEFAULT ''::text,
    derived_from_ref text DEFAULT ''::text,
    derived_hash text DEFAULT ''::text,
    sync_status text DEFAULT 'active'::text NOT NULL,
    last_synced_at timestamp with time zone,
    last_sync_error text,
    deleted_at timestamp with time zone
);


--
-- Name: registry_datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.registry_datasets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dataset_key text NOT NULL,
    dataset_id text GENERATED ALWAYS AS (dataset_key) STORED,
    workspace_id uuid,
    display_name text NOT NULL,
    description text DEFAULT ''::text,
    connector_kind text DEFAULT 'extension'::text NOT NULL,
    connector_id text DEFAULT ''::text NOT NULL,
    runtime_kind text DEFAULT 'local_extension'::text NOT NULL,
    query_language text DEFAULT 'sql'::text NOT NULL,
    availability_mode text DEFAULT 'specific_projects_only'::text NOT NULL,
    read_only boolean DEFAULT true NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    schema_snapshot_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    connection_schema_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    CONSTRAINT registry_datasets_connector_kind_check CHECK ((connector_kind = ANY (ARRAY['builtin'::text, 'extension'::text, 'manual'::text, 'mcp_connector'::text]))),
    CONSTRAINT registry_datasets_runtime_kind_check CHECK ((runtime_kind = ANY (ARRAY['builtin'::text, 'local_extension'::text, 'manual'::text, 'mcp_bridge'::text, 'remote_extension'::text]))),
    CONSTRAINT registry_datasets_availability_mode_check CHECK ((availability_mode = ANY (ARRAY['all_workspace_projects'::text, 'specific_projects_only'::text]))),
    CONSTRAINT registry_datasets_query_language_check CHECK ((COALESCE(query_language, ''::text) <> ''::text))
);


--
