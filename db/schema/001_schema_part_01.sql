--
-- PostgreSQL database dump
--


-- Dumped from database version 17.8 (Debian 17.8-1.pgdg13+1)
-- Dumped by pg_dump version 17.8 (Debian 17.8-1.pgdg13+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

SELECT pg_catalog.set_config('search_path', '', false);

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS public;


--
-- Name: registry_update_timestamp(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.registry_update_timestamp() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    account_key text DEFAULT 'default'::text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tenant_id uuid DEFAULT '00000000-0000-0000-0000-000000000001'::uuid NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    workspace_key text NOT NULL,
    display_name text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by text,
    CONSTRAINT workspaces_status_check CHECK ((status = ANY (ARRAY['active'::text, 'archived'::text])))
);


--
-- Name: platform_schema_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_schema_state (
    schema_key text NOT NULL,
    schema_fingerprint text DEFAULT ''::text NOT NULL,
    app_version text DEFAULT ''::text NOT NULL,
    applied_by text DEFAULT 'system'::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT platform_schema_state_pkey PRIMARY KEY (schema_key)
);


--
-- Name: auth_email_change_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_email_change_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    current_email text DEFAULT ''::text NOT NULL,
    new_email text NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    requested_ip text DEFAULT ''::text NOT NULL,
    used_at timestamp with time zone,
    verified_at timestamp with time zone,
    invalidated_reason text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: auth_rate_limits; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_rate_limits (
    key text NOT NULL,
    attempts_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL,
    locked_until timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: auth_email_change_requests_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: auth_password_resets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_password_resets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    used_by text DEFAULT ''::text NOT NULL,
    requested_by text DEFAULT ''::text NOT NULL,
    request_ip text DEFAULT ''::text NOT NULL,
    invalidated_reason text DEFAULT ''::text NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: auth_password_resets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: auth_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    session_token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid
);


--
-- Name: auth_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: platform_action_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_action_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    project_id uuid,
    task_id text,
    task_summary text,
    lifecycle_id text,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    agent text NOT NULL,
    action_type text NOT NULL,
    iteration_no integer,
    cycle_no integer,
    status text,
    duration integer,
    payload jsonb,
    result jsonb,
    error text,
    meta jsonb,
    job_id uuid,
    tokens_prompt integer,
    tokens_completion integer,
    tokens_total integer,
    cost_usd numeric(10,6),
    model_used text,
    CONSTRAINT platform_action_log_duration_check CHECK (((duration IS NULL) OR (duration >= 0)))
);


--
-- Name: platform_action_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: platform_agent_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_agent_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    correlation_id text NOT NULL,
    schema_version text DEFAULT 'agent_message_v1'::text NOT NULL,
    from_agent text NOT NULL,
    to_agent text NOT NULL,
    step_id text,
    step_execution_id uuid,
    attempt integer,
    group_id text,
    for_each_id text,
    skill text NOT NULL,
    status text NOT NULL,
    summary text DEFAULT ''::text,
    artifacts jsonb DEFAULT '{}'::jsonb NOT NULL,
    errors jsonb DEFAULT '[]'::jsonb NOT NULL,
    envelope jsonb NOT NULL,
    CONSTRAINT platform_agent_messages_one_owner_check CHECK (((group_id IS NULL) OR (for_each_id IS NULL))),
    CONSTRAINT platform_agent_messages_step_scope_check CHECK ((((step_execution_id IS NULL) AND (attempt IS NULL)) OR ((step_id IS NOT NULL) AND (step_execution_id IS NOT NULL) AND (attempt >= 1))))
);


--
-- Name: platform_agent_messages_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: platform_event_retry_queue; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_event_retry_queue (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    enqueued_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb NOT NULL,
    failure_reason text,
    retry_count integer DEFAULT 0 NOT NULL,
    last_retry_at timestamp with time zone,
    sent_at timestamp with time zone,
    CONSTRAINT platform_event_retry_queue_retry_count_check CHECK ((retry_count >= 0))
);


--
-- Name: platform_event_retry_queue_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: platform_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.platform_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    job_id uuid NOT NULL,
    "timestamp" timestamp with time zone DEFAULT now() NOT NULL,
    phase text NOT NULL,
    agent text NOT NULL,
    event_type text NOT NULL,
    severity text DEFAULT 'info'::text,
    title text NOT NULL,
    detail jsonb
);


--
-- Name: platform_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: checkpoint_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea
);


--
-- Name: checkpoint_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_migrations (
    v integer NOT NULL
);


--
-- Name: checkpoint_writes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text DEFAULT ''::text NOT NULL
);


--
-- Name: checkpoints; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text DEFAULT ''::text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: command_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.command_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    command_id text NOT NULL,
    command_type text NOT NULL,
    account_id uuid NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    target_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'accepted'::text NOT NULL,
    result_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    event_id text,
    dispatch_id text,
    CONSTRAINT command_audit_log_status_check CHECK ((status = ANY (ARRAY['accepted'::text, 'executed'::text, 'failed'::text, 'rejected'::text, 'skipped'::text])))
);


--
-- Name: command_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: dashboard_daily_rollups; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_daily_rollups (
    account_id uuid NOT NULL,
    rollup_date date NOT NULL,
    completed_count integer DEFAULT 0 NOT NULL,
    failed_count integer DEFAULT 0 NOT NULL,
    spend_usd double precision DEFAULT 0 NOT NULL,
    p50_seconds double precision,
    p95_seconds double precision,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: dashboard_job_facts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_job_facts (
    job_id uuid NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    project_name text NOT NULL,
    workflow text NOT NULL,
    agent text NOT NULL,
    status text NOT NULL,
    result text NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone NOT NULL,
    finished_date date NOT NULL,
    duration_seconds double precision,
    cost_usd double precision DEFAULT 0 NOT NULL,
    refreshed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT dashboard_job_facts_result_check CHECK ((result = ANY (ARRAY['completed'::text, 'failed'::text])))
);


--
-- Name: dashboard_rollup_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_rollup_state (
    account_id uuid NOT NULL,
    facts_covered_from timestamp with time zone,
    facts_covered_until timestamp with time zone,
    facts_refreshed_at timestamp with time zone,
    daily_refreshed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: domain_event_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.domain_event_ledger (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id text NOT NULL,
    event_type text NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid,
    workflow_id text,
    job_id uuid,
    group_id text,
    for_each_id text,
    step_id text,
    step_execution_id uuid,
    severity text DEFAULT 'info'::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    actor_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    urls_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    envelope_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    source text DEFAULT 'runtime'::text NOT NULL,
    aggregate_type text DEFAULT ''::text NOT NULL,
    aggregate_id text DEFAULT ''::text NOT NULL,
    correlation_id text,
    status text DEFAULT 'recorded'::text NOT NULL,
    dispatch_count integer DEFAULT 0 NOT NULL,
    last_dispatch_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT domain_event_ledger_dispatch_count_check CHECK ((dispatch_count >= 0)),
    CONSTRAINT domain_event_ledger_severity_check CHECK ((severity = ANY (ARRAY['debug'::text, 'info'::text, 'warning'::text, 'error'::text, 'critical'::text]))),
    CONSTRAINT domain_event_ledger_status_check CHECK ((status = ANY (ARRAY['recorded'::text, 'dispatching'::text, 'succeeded'::text, 'partially_failed'::text, 'failed'::text, 'dead_lettered'::text])))
);


--
-- Name: domain_event_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: domain_event_outbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.domain_event_outbox (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_type text NOT NULL,
    aggregate_type text DEFAULT 'job'::text NOT NULL,
    aggregate_id text DEFAULT ''::text NOT NULL,
    payload_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    published_at timestamp with time zone,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    project_id uuid,
    workflow_id text,
    job_id uuid,
    severity text DEFAULT 'info'::text NOT NULL,
    summary text,
    actor_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_envelope_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    event_id text,
    claim_owner text,
    claim_token text,
    claim_version bigint DEFAULT 0 NOT NULL,
    claim_expires_at timestamp with time zone,
    CONSTRAINT domain_event_outbox_claim_version_check CHECK ((claim_version >= 0)),
    CONSTRAINT domain_event_outbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'published'::text, 'failed'::text])))
);


--
-- Name: domain_event_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: enabled_extensions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.enabled_extensions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bundle_id uuid DEFAULT gen_random_uuid() NOT NULL,
    version_id uuid DEFAULT gen_random_uuid() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    enabled_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    required_scopes_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL
);


--
-- Name: extension_callback_registrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_callback_registrations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    extension_id text NOT NULL,
    callback_id text NOT NULL,
    callback_token_hash text NOT NULL,
    project_id uuid NOT NULL,
    connection_id text,
    enabled boolean DEFAULT true NOT NULL,
    auth_type text DEFAULT 'none'::text NOT NULL,
    auth_config_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_seen_at timestamp with time zone,
    last_error text DEFAULT ''::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_callback_registrations_auth_type_check CHECK ((auth_type = ANY (ARRAY['none'::text, 'shared_secret_header'::text, 'hmac_sha256'::text, 'hub_signature'::text]))),
    CONSTRAINT extension_callback_registrations_pkey PRIMARY KEY (id),
    CONSTRAINT extension_callback_registrations_token_hash_key UNIQUE (callback_token_hash)
);


--
-- Name: extension_callback_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_callback_deliveries (
    registration_id uuid NOT NULL,
    delivery_id text NOT NULL,
    delivered_at timestamp with time zone NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_callback_deliveries_pkey PRIMARY KEY (registration_id, delivery_id)
);


--
-- Name: enabled_extensions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: event_delivery_attempts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_delivery_attempts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id text NOT NULL,
    dispatch_id text NOT NULL,
    attempt_no integer NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    worker_id text,
    handler text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    duration_ms integer,
    request_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_text text,
    external_effect_id uuid,
    retry_in_seconds integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT event_delivery_attempts_attempt_no_check CHECK ((attempt_no > 0)),
    CONSTRAINT event_delivery_attempts_duration_ms_check CHECK (((duration_ms IS NULL) OR (duration_ms >= 0))),
    CONSTRAINT event_delivery_attempts_retry_in_seconds_check CHECK (((retry_in_seconds IS NULL) OR (retry_in_seconds >= 0))),
    CONSTRAINT event_delivery_attempts_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'timeout'::text, 'dead_lettered'::text, 'skipped'::text])))
);


--
-- Name: event_delivery_attempts_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: event_dispatches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_dispatches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    dispatch_id text NOT NULL,
    event_id text NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid,
    workflow_id text,
    job_id uuid,
    target_type text NOT NULL,
    target_id text NOT NULL,
    target_label text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'matched'::text NOT NULL,
    reason_code text,
    reason_detail text,
    scope_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    last_attempt_at timestamp with time zone,
    next_attempt_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT event_dispatches_attempt_count_check CHECK ((attempt_count >= 0)),
    CONSTRAINT event_dispatches_max_attempts_check CHECK ((max_attempts > 0)),
    CONSTRAINT event_dispatches_status_check CHECK ((status = ANY (ARRAY['matched'::text, 'skipped'::text, 'queued'::text, 'dispatching'::text, 'succeeded'::text, 'failed'::text, 'dead_lettered'::text])))
);


--
-- Name: event_dispatches_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: extension_bundles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_bundles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    extension_key text NOT NULL,
    kind text NOT NULL,
    display_name text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    bundle_root text NOT NULL,
    manifest_path text NOT NULL,
    latest_version text,
    status text DEFAULT 'discovered'::text NOT NULL,
    last_scan_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    manifest_version integer DEFAULT 1 NOT NULL,
    latest_compile_status text DEFAULT 'unknown'::text NOT NULL,
    latest_validation_status text DEFAULT 'unknown'::text NOT NULL,
    CONSTRAINT extension_bundles_kind_check CHECK ((kind = ANY (ARRAY['task_provider'::text, 'notification_provider'::text, 'tool_provider'::text]))),
    CONSTRAINT extension_bundles_status_check CHECK ((status = ANY (ARRAY['discovered'::text, 'valid'::text, 'invalid'::text, 'disabled'::text])))
);


--
-- Name: extension_bundles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
-- Name: extension_invocation_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extension_invocation_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bundle_id uuid DEFAULT gen_random_uuid() NOT NULL,
    version_id uuid,
    invocation_type text DEFAULT 'health_check'::text NOT NULL,
    status text DEFAULT 'unknown'::text NOT NULL,
    request_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    response_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    error_text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    actor_type text,
    actor_id text,
    request_context_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL,
    query_id text,
    action_id text,
    command_id text,
    external_effect_id uuid,
    job_id uuid,
    workflow_id text,
    group_id text,
    for_each_id text,
    step_id text,
    step_execution_id uuid,
    CONSTRAINT extension_invocation_logs_workflow_id_nonblank_check CHECK (((workflow_id IS NULL) OR (btrim(workflow_id) <> ''::text))),
    CONSTRAINT extension_invocation_logs_group_id_nonblank_check CHECK (((group_id IS NULL) OR (btrim(group_id) <> ''::text))),
    CONSTRAINT extension_invocation_logs_for_each_id_nonblank_check CHECK (((for_each_id IS NULL) OR (btrim(for_each_id) <> ''::text))),
    CONSTRAINT extension_invocation_logs_one_owner_check CHECK (((group_id IS NULL) OR (for_each_id IS NULL))),
    CONSTRAINT extension_invocation_logs_step_id_nonblank_check CHECK (((step_id IS NULL) OR (btrim(step_id) <> ''::text))),
    CONSTRAINT extension_invocation_logs_status_check CHECK ((status = ANY (ARRAY['ok'::text, 'failed'::text, 'unknown'::text])))
);


--
-- Name: extension_invocation_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--



--
