-- Pre-1.0 canonical runtime observation storage; dynamic process incarnations only.
CREATE TABLE public.fs_runtime_status_current (
    id uuid PRIMARY KEY,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    installation_id text NOT NULL CHECK (char_length(installation_id) BETWEEN 1 AND 128),
    deployment_profile text NOT NULL CHECK (deployment_profile IN ('compact', 'scalable')),
    service_type text NOT NULL CHECK (service_type ~ '^[a-z][a-z0-9_.-]{0,127}$'),
    instance_id uuid NOT NULL,
    incarnation_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    observed_at timestamp with time zone NOT NULL,
    received_at timestamp with time zone NOT NULL,
    source text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_.:/-]{0,127}$'),
    payload jsonb NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND payload @> '{"type":"runtime_status","version":1}'::jsonb
    ),
    CONSTRAINT uq_fs_runtime_status_observation UNIQUE
        (installation_id, service_type, instance_id, incarnation_id, source)
);

CREATE INDEX idx_fs_runtime_status_current_scope
    ON public.fs_runtime_status_current (installation_id, deployment_profile, service_type);
CREATE INDEX idx_fs_runtime_status_current_retention
    ON public.fs_runtime_status_current (installation_id, received_at, id);

-- Deliberately no cascading current-row FK: history has independent retention.
CREATE TABLE public.fs_runtime_status_history (
    id uuid PRIMARY KEY,
    current_record_id uuid NOT NULL,
    schema_version integer NOT NULL CHECK (schema_version = 1),
    installation_id text NOT NULL CHECK (char_length(installation_id) BETWEEN 1 AND 128),
    deployment_profile text NOT NULL CHECK (deployment_profile IN ('compact', 'scalable')),
    service_type text NOT NULL CHECK (service_type ~ '^[a-z][a-z0-9_.-]{0,127}$'),
    instance_id uuid NOT NULL,
    incarnation_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    observed_at timestamp with time zone NOT NULL,
    received_at timestamp with time zone NOT NULL,
    source text NOT NULL CHECK (source ~ '^[a-z][a-z0-9_.:/-]{0,127}$'),
    payload jsonb NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND payload @> '{"type":"runtime_status","version":1}'::jsonb
    ),
    CONSTRAINT uq_fs_runtime_status_history_sequence UNIQUE (current_record_id, sequence)
);

CREATE INDEX idx_fs_runtime_status_history_retention
    ON public.fs_runtime_status_history (installation_id, received_at, id);

-- Pre-1.0 tabular snapshot publication authority. Parquet remains in the artifact plane.
CREATE TABLE public.fs_tabular_snapshot_heads (
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    workflow_id text NOT NULL CHECK (btrim(workflow_id) <> ''),
    profile_key text NOT NULL CHECK (btrim(profile_key) <> ''),
    revision bigint DEFAULT 0 NOT NULL CHECK (revision >= 0),
    snapshot_artifact_id text DEFAULT '' NOT NULL,
    metadata_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(metadata_jsonb) = 'object'),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fs_tabular_snapshot_heads_pkey
        PRIMARY KEY (account_id, project_id, workflow_id, profile_key),
    CONSTRAINT fs_tabular_snapshot_heads_ref_check CHECK (
        (revision = 0 AND snapshot_artifact_id = '')
        OR (revision > 0 AND btrim(snapshot_artifact_id) <> '')
    ),
    CONSTRAINT fs_tabular_snapshot_heads_project_account_fkey
        FOREIGN KEY (project_id, account_id) REFERENCES public.projects(id, account_id)
        ON DELETE CASCADE
);

CREATE TABLE public.fs_tabular_snapshot_commits (
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    workflow_id text NOT NULL,
    profile_key text NOT NULL,
    candidate_artifact_id text NOT NULL CHECK (btrim(candidate_artifact_id) <> ''),
    revision bigint NOT NULL CHECK (revision > 0),
    previous_snapshot_artifact_id text DEFAULT '' NOT NULL,
    metadata_jsonb jsonb NOT NULL CHECK (jsonb_typeof(metadata_jsonb) = 'object'),
    committed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fs_tabular_snapshot_commits_pkey
        PRIMARY KEY (account_id, project_id, workflow_id, profile_key, candidate_artifact_id),
    CONSTRAINT fs_tabular_snapshot_commits_revision_key
        UNIQUE (account_id, project_id, workflow_id, profile_key, revision),
    CONSTRAINT fs_tabular_snapshot_commits_scope_fkey
        FOREIGN KEY (account_id, project_id, workflow_id, profile_key)
        REFERENCES public.fs_tabular_snapshot_heads(account_id, project_id, workflow_id, profile_key)
        ON DELETE CASCADE
);

-- Inbound OAuth for the MCP server (ADR 0047). Protocol-owned records, separate from
-- personal_access_tokens. Raw codes and tokens are never stored: only SHA-256 hashes.
-- Not part of the required startup contract: a database without them reports the
-- OAuth readiness blocker storage_missing and keeps serving PATs.
CREATE TABLE public.oauth_clients (
    client_id text PRIMARY KEY CHECK (char_length(client_id) BETWEEN 1 AND 2048),
    client_name text DEFAULT ''::text NOT NULL,
    redirect_uris_jsonb jsonb NOT NULL CHECK (jsonb_typeof(redirect_uris_jsonb) = 'array'),
    metadata_sha256 text NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    cache_expires_at timestamp with time zone NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone
);

CREATE TABLE public.oauth_grants (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE RESTRICT,
    client_name_snapshot text NOT NULL,
    resource text NOT NULL,
    preset text NOT NULL,
    scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(scopes_jsonb) = 'array'),
    allowed_project_ids_jsonb jsonb NOT NULL CHECK (jsonb_typeof(allowed_project_ids_jsonb) = 'array'),
    allowed_workflow_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL
        CHECK (jsonb_typeof(allowed_workflow_ids_jsonb) = 'array'),
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason text,
    CONSTRAINT chk_oauth_grants_lifetime CHECK (
        expires_at > created_at AND expires_at <= created_at + interval '90 days'
    )
);

CREATE TABLE public.oauth_authorizations (
    id uuid PRIMARY KEY,
    client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri text NOT NULL,
    resource text NOT NULL,
    code_challenge text NOT NULL,
    state text,
    requested_scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(requested_scopes_jsonb) = 'array'),
    browser_binding_hash text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone,
    decision text CHECK (decision IN ('approved', 'denied')),
    user_id uuid REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid,
    preset text,
    scopes_jsonb jsonb,
    allowed_project_ids_jsonb jsonb,
    grant_expires_at timestamp with time zone,
    code_hash text UNIQUE,
    code_expires_at timestamp with time zone,
    code_consumed_at timestamp with time zone,
    grant_id uuid REFERENCES public.oauth_grants(id) ON DELETE SET NULL
);

CREATE TABLE public.oauth_access_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    grant_id uuid NOT NULL REFERENCES public.oauth_grants(id) ON DELETE CASCADE,
    scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(scopes_jsonb) = 'array'),
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE public.oauth_refresh_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    grant_id uuid NOT NULL REFERENCES public.oauth_grants(id) ON DELETE CASCADE,
    parent_id uuid REFERENCES public.oauth_refresh_tokens(id) ON DELETE SET NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);

CREATE INDEX idx_oauth_grants_user_revoked ON public.oauth_grants (user_id, revoked_at);
CREATE INDEX idx_oauth_access_tokens_grant ON public.oauth_access_tokens (grant_id);
CREATE INDEX idx_oauth_refresh_tokens_grant ON public.oauth_refresh_tokens (grant_id);
CREATE INDEX idx_oauth_authorizations_expires ON public.oauth_authorizations (expires_at);

-- Secret catalog (ADR 0055): identity, metadata, bindings and approved use of shared
-- secrets. Values stay in secret_refs under storage_ref_key. platform_upgrade_phases
-- records the data phases of database upgrade (ADR 0054). This block is
-- db/migrations/20261103090000_secret_catalog.sql without IF NOT EXISTS, after
-- 20261104120000_secret_catalog_installation_wide.sql emptied the owner columns.
CREATE TABLE public.catalog_secrets (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE RESTRICT,
    -- Unused since 20261104120000: a secret belongs to the installation. Kept, empty,
    -- because db/migrations/20261103090000_secret_catalog.sql names them.
    workspace_id uuid,
    project_id uuid,
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
    description text DEFAULT ''::text NOT NULL CHECK (char_length(description) <= 2000),
    storage_ref_key text NOT NULL CHECK (char_length(storage_ref_key) BETWEEN 1 AND 512),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    metadata_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    metadata_updated_by text DEFAULT ''::text NOT NULL,
    value_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    value_updated_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT catalog_secrets_storage_ref_key UNIQUE (account_id, storage_ref_key),
    CONSTRAINT catalog_secrets_id_account_key UNIQUE (id, account_id)
);

CREATE TABLE public.catalog_secret_bindings (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL,
    secret_id uuid NOT NULL,
    consumer_kind text NOT NULL,
    project_connection_id text REFERENCES public.project_connections(connection_id) ON DELETE CASCADE,
    workflow_id text,
    consumer_project_id uuid REFERENCES public.projects(id) ON DELETE CASCADE,
    data_connection_id text REFERENCES public.workspace_data_connections(connection_id) ON DELETE CASCADE,
    mcp_connector_id text REFERENCES public.mcp_connectors(connector_id) ON DELETE CASCADE,
    llm_provider_install_id text,
    field_name text NOT NULL
        CHECK (char_length(btrim(field_name)) BETWEEN 1 AND 200 AND field_name NOT LIKE '\_oauth\_%'),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT catalog_secret_bindings_secret_fkey FOREIGN KEY (secret_id, account_id)
        REFERENCES public.catalog_secrets(id, account_id) ON DELETE RESTRICT,
    CONSTRAINT catalog_secret_bindings_consumer_kind_check CHECK (consumer_kind = ANY (ARRAY[
        'project_connection'::text, 'workflow_input'::text, 'data_connection'::text,
        'mcp_connector'::text, 'llm_provider'::text])),
    CONSTRAINT catalog_secret_bindings_consumer_check CHECK (
        num_nonnulls(project_connection_id, workflow_id, data_connection_id,
                     mcp_connector_id, llm_provider_install_id) = 1
        AND ((consumer_kind = 'project_connection' AND project_connection_id IS NOT NULL)
          OR (consumer_kind = 'workflow_input'
              AND workflow_id IS NOT NULL AND consumer_project_id IS NOT NULL)
          OR (consumer_kind = 'data_connection' AND data_connection_id IS NOT NULL)
          OR (consumer_kind = 'mcp_connector' AND mcp_connector_id IS NOT NULL)
          OR (consumer_kind = 'llm_provider' AND llm_provider_install_id IS NOT NULL))),
    CONSTRAINT catalog_secret_bindings_connection_field_key UNIQUE (project_connection_id, field_name),
    CONSTRAINT catalog_secret_bindings_workflow_field_key UNIQUE (account_id, workflow_id, field_name),
    CONSTRAINT catalog_secret_bindings_data_connection_field_key UNIQUE (data_connection_id, field_name),
    CONSTRAINT catalog_secret_bindings_mcp_connector_field_key UNIQUE (mcp_connector_id, field_name),
    CONSTRAINT catalog_secret_bindings_llm_provider_field_key UNIQUE (llm_provider_install_id, field_name)
);

CREATE TABLE public.catalog_secret_approvals (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE RESTRICT,
    consumer_kind text NOT NULL
        CHECK (consumer_kind = ANY (ARRAY['project_connection'::text, 'workflow'::text])),
    project_connection_id text REFERENCES public.project_connections(connection_id) ON DELETE CASCADE,
    workflow_id text,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    projection_jsonb jsonb NOT NULL CHECK (jsonb_typeof(projection_jsonb) = 'object'),
    approved_by text NOT NULL CHECK (char_length(btrim(approved_by)) > 0),
    approved_by_kind text NOT NULL CHECK (approved_by_kind = ANY (ARRAY['human'::text, 'upgrade'::text])),
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT catalog_secret_approvals_consumer_check CHECK (
        (consumer_kind = 'project_connection'
            AND project_connection_id IS NOT NULL AND workflow_id IS NULL)
        OR (consumer_kind = 'workflow' AND workflow_id IS NOT NULL
            AND project_connection_id IS NULL)),
    CONSTRAINT catalog_secret_approvals_connection_key UNIQUE (project_connection_id),
    CONSTRAINT catalog_secret_approvals_workflow_key UNIQUE (account_id, workflow_id)
);

-- A workflow consumer names a workflow of its own account (ADR 0020, step C4b).
-- Migration 20261126180000_workflow_account_identity.sql makes the same change.
ALTER TABLE ONLY public.catalog_secret_bindings
    ADD CONSTRAINT catalog_secret_bindings_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.catalog_secret_approvals
    ADD CONSTRAINT catalog_secret_approvals_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;

CREATE TABLE public.platform_upgrade_phases (
    phase_id text PRIMARY KEY CHECK (phase_id ~ '^[a-z][a-z0-9_]{2,127}$'),
    release_version text NOT NULL,
    summary_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(summary_jsonb) = 'object'),
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX idx_catalog_secrets_scope ON public.catalog_secrets (account_id, workspace_id, project_id);
CREATE INDEX idx_catalog_secret_bindings_secret ON public.catalog_secret_bindings (secret_id);

-- Connection slots of extension workflows (ADR 0068): one connection a project
-- chose per declared slot, keyed by install and local workflow id.
CREATE TABLE public.workflow_connection_slot_choices (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    source_install_id text NOT NULL CHECK (btrim(source_install_id) <> ''::text),
    local_workflow_id text NOT NULL CHECK (btrim(local_workflow_id) <> ''::text),
    slot text NOT NULL CHECK (slot ~ '^[a-z][a-z0-9_]{0,62}$'::text),
    connection_id text NOT NULL,
    chosen_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_connection_slot_choices_pkey
        PRIMARY KEY (account_id, project_id, source_install_id, local_workflow_id, slot),
    CONSTRAINT workflow_connection_slot_choices_connection_fkey
        FOREIGN KEY (connection_id, project_id, account_id)
        REFERENCES public.project_connections(connection_id, project_id, account_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_workflow_connection_slot_choices_install
    ON public.workflow_connection_slot_choices (account_id, source_install_id);
CREATE INDEX idx_workflow_connection_slot_choices_connection
    ON public.workflow_connection_slot_choices (connection_id);

-- Events delivered to Wait steps (ADR 0071): one resume_waits item of an
-- extension response, kept until a run of the same account and project that waits
-- for that source, key and type takes it, and deleted 7 days after it arrived.
CREATE TABLE public.workflow_wait_events (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (btrim(source) <> ''::text),
    extension_install_id text DEFAULT ''::text NOT NULL,
    correlation_key text NOT NULL CHECK (char_length(correlation_key) BETWEEN 1 AND 256),
    event_type text NOT NULL CHECK (event_type ~ '^[a-z0-9][a-z0-9_.:-]{0,127}$'::text),
    payload jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'::text),
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_at timestamp with time zone,
    consumed_job_id uuid,
    consumed_wait_id text,
    CONSTRAINT workflow_wait_events_delivery_key
        UNIQUE (project_id, source, correlation_key, event_type)
);

CREATE INDEX idx_workflow_wait_events_open
    ON public.workflow_wait_events (project_id, source, correlation_key)
    WHERE consumed_at IS NULL;
CREATE INDEX idx_workflow_wait_events_received
    ON public.workflow_wait_events (received_at);
CREATE INDEX idx_jobs_workflow_wait_event_key
    ON public.jobs USING btree (((meta -> 'workflow_wait'::text) ->> 'correlation_key'::text))
    WHERE (status = 'awaiting_event'::text);
CREATE INDEX idx_jobs_workflow_wait_event_due
    ON public.jobs USING btree (((((meta -> 'workflow_wait'::text) ->> 'due_epoch_ms'::text))::bigint), id)
    WHERE (status = 'awaiting_event'::text);

-- One local authenticator slot and user-bound recovery codes.
CREATE TABLE public.auth_totp_factors (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    secret_enc text,
    pending_secret_enc text,
    pending_expires_at timestamp with time zone,
    last_used_step bigint
);

CREATE TABLE public.auth_mfa_recovery_codes (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    PRIMARY KEY (user_id, code_hash)
);

-- Password age follows writes to the stored credential, including resets.
CREATE FUNCTION public.update_password_changed_at() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
DECLARE
    old_deadline timestamp with time zone := OLD.password_changed_at + make_interval(hours => 8760);
BEGIN
    NEW.password_changed_at = clock_timestamp();
    -- A pending enrollment was authorized by a password that has changed.
    UPDATE public.auth_totp_factors
    SET pending_secret_enc = NULL, pending_expires_at = NULL
    WHERE user_id = NEW.id AND pending_secret_enc IS NOT NULL;
    IF old_deadline <= NEW.password_changed_at THEN
        -- Updating the credential must not restore access through old sessions.
        UPDATE public.auth_sessions
        SET expires_at = old_deadline
        WHERE user_id = NEW.id
          AND revoked = false
          AND expires_at > old_deadline;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_users_password_changed
    BEFORE UPDATE OF password_hash ON public.users
    FOR EACH ROW
    WHEN (old.password_hash IS DISTINCT FROM new.password_hash)
    EXECUTE FUNCTION public.update_password_changed_at();

-- Retain only the ten most recent prior password hashes per user.
CREATE TABLE public.auth_password_history (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    id bigint GENERATED ALWAYS AS IDENTITY,
    password_hash text NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE FUNCTION public.archive_previous_password_hash() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
BEGIN
    INSERT INTO public.auth_password_history (user_id, password_hash)
    VALUES (OLD.id, OLD.password_hash);

    DELETE FROM public.auth_password_history AS history
    WHERE history.user_id = OLD.id
      AND history.id IN (
          SELECT older.id
          FROM public.auth_password_history AS older
          WHERE older.user_id = OLD.id
          ORDER BY older.id DESC
          OFFSET 10
      );
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_users_archive_previous_password
    AFTER UPDATE OF password_hash ON public.users
    FOR EACH ROW
    WHEN (old.password_hash IS DISTINCT FROM new.password_hash)
    EXECUTE FUNCTION public.archive_previous_password_hash();

-- A connection_poll result is judged against its connection as it is now. The
-- poll transaction locks the connection row, compares the state_version and
-- config_generation the poll started from, and writes the events, their
-- dispatch records and the cursor together. config_generation moves whenever
-- the polled configuration changes: the connection is enabled or disabled,
-- changes type, gets other settings (the oauth lifecycle block excluded, which
-- every token refresh rewrites) or is reauthorized. Every such UPDATE holds the
-- same row lock as the poll transaction.
CREATE FUNCTION public.project_connection_bump_config_generation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
BEGIN
    IF NEW.config_generation = OLD.config_generation AND (
        NEW.enabled IS DISTINCT FROM OLD.enabled
        OR NEW.connection_type IS DISTINCT FROM OLD.connection_type
        OR (NEW.connection_config - 'oauth') IS DISTINCT FROM (OLD.connection_config - 'oauth')
        OR NEW.oauth_authorization_revision IS DISTINCT FROM OLD.oauth_authorization_revision
    ) THEN
        NEW.config_generation := OLD.config_generation + 1;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_project_connections_config_generation
    BEFORE UPDATE ON public.project_connections
    FOR EACH ROW
    EXECUTE FUNCTION public.project_connection_bump_config_generation();

-- One row per provider event, unique on its provider-given source_id. A replayed
-- poll page writes no second row and no new dispatch records.
CREATE TABLE public.provider_event_inbox (
    event_row_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    provider_key text NOT NULL CHECK (btrim(provider_key) <> ''::text),
    source_id text NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 512),
    event_type text NOT NULL CHECK (btrim(event_type) <> ''::text),
    payload jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'::text),
    connection_id text,
    config_generation bigint,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_event_inbox_source_key UNIQUE (project_id, provider_key, source_id)
);

CREATE INDEX idx_provider_event_inbox_received ON public.provider_event_inbox USING btree (received_at);

-- One row per workflow a provider event starts, keyed like its delivery
-- (project|workflow|task). A job is created, the delivery recorded and the row
-- marked accepted in one transaction; a periodic sweep retries pending rows.
CREATE TABLE public.provider_event_dispatches (
    delivery_key text PRIMARY KEY CHECK (char_length(delivery_key) BETWEEN 1 AND 1024),
    event_row_id uuid NOT NULL REFERENCES public.provider_event_inbox(event_row_id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    provider_key text NOT NULL,
    workflow_id text NOT NULL,
    trigger_id text NOT NULL,
    connection_id text,
    config_generation bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    job_id uuid,
    reason text DEFAULT ''::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_event_dispatches_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'failed'::text])))
);

CREATE INDEX idx_provider_event_dispatches_pending ON public.provider_event_dispatches USING btree (updated_at) WHERE (status = 'pending'::text);
CREATE INDEX idx_provider_event_dispatches_event ON public.provider_event_dispatches USING btree (event_row_id);

-- The system email sender is confirmed against counters that only grow, never
-- against a hash of current values that an undone change would match again.
-- sender_revision moves on every change that can alter who a connection sends
-- as: it is enabled or disabled (either direction), retyped, moved to another
-- install, project or OAuth configuration, given other settings (the oauth
-- lifecycle block excluded) or other static secrets, or reconnected to another
-- account. A token refresh rewrites only the oauth lifecycle block, the
-- _oauth_* token refs and credential_revision of an OAuth connection, so it does
-- not move. For a connection without OAuth every credential_revision bump does
-- (a catalog secret rotated under the same ref is one).
CREATE FUNCTION public.project_connection_bump_sender_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
DECLARE
    old_static_refs jsonb;
    new_static_refs jsonb;
BEGIN
    IF NEW.sender_revision <> OLD.sender_revision THEN
        RETURN NEW;
    END IF;
    SELECT COALESCE(jsonb_object_agg(item.key, item.value), '{}'::jsonb)
      INTO old_static_refs
      FROM jsonb_each(COALESCE(OLD.secret_refs -> 'refs', '{}'::jsonb)) AS item
     WHERE item.key NOT LIKE '\_oauth\_%';
    SELECT COALESCE(jsonb_object_agg(item.key, item.value), '{}'::jsonb)
      INTO new_static_refs
      FROM jsonb_each(COALESCE(NEW.secret_refs -> 'refs', '{}'::jsonb)) AS item
     WHERE item.key NOT LIKE '\_oauth\_%';
    IF NEW.enabled IS DISTINCT FROM OLD.enabled
        OR NEW.connection_type IS DISTINCT FROM OLD.connection_type
        OR NEW.extension_install_id IS DISTINCT FROM OLD.extension_install_id
        OR NEW.extension_id IS DISTINCT FROM OLD.extension_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.oauth_configuration_id IS DISTINCT FROM OLD.oauth_configuration_id
        OR NEW.credentials_configuration_id IS DISTINCT FROM OLD.credentials_configuration_id
        OR NEW.oauth_provider_id IS DISTINCT FROM OLD.oauth_provider_id
        OR NEW.oauth_authorization_revision IS DISTINCT FROM OLD.oauth_authorization_revision
        OR (NEW.connection_config - 'oauth') IS DISTINCT FROM (OLD.connection_config - 'oauth')
        OR (NEW.connection_config -> 'oauth' -> 'subject')
            IS DISTINCT FROM (OLD.connection_config -> 'oauth' -> 'subject')
        OR (NEW.connection_config -> 'oauth' -> 'authorization_intent_id')
            IS DISTINCT FROM (OLD.connection_config -> 'oauth' -> 'authorization_intent_id')
        OR (COALESCE(NEW.secret_refs, '{}'::jsonb) - 'refs')
            IS DISTINCT FROM (COALESCE(OLD.secret_refs, '{}'::jsonb) - 'refs')
        OR new_static_refs IS DISTINCT FROM old_static_refs
        OR (NEW.oauth_configuration_id IS NULL
            AND NEW.credential_revision IS DISTINCT FROM OLD.credential_revision)
    THEN
        NEW.sender_revision := OLD.sender_revision + 1;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_project_connections_sender_revision
    BEFORE UPDATE ON public.project_connections
    FOR EACH ROW
    EXECUTE FUNCTION public.project_connection_bump_sender_revision();

-- An OAuth configuration keeps its id across edits, so the system email
-- sender's confirmation records its configuration_revision. Every change to
-- what signs in (client settings, client secrets, provider, provider security)
-- moves the revision, even through a write path that forgot to.
CREATE FUNCTION public.oauth_configuration_bump_revision() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
BEGIN
    IF NEW.configuration_revision <= OLD.configuration_revision AND (
        NEW.non_secret_config IS DISTINCT FROM OLD.non_secret_config
        OR NEW.secret_refs IS DISTINCT FROM OLD.secret_refs
        OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
        OR NEW.provider_security_fingerprint IS DISTINCT FROM OLD.provider_security_fingerprint
    ) THEN
        NEW.configuration_revision := OLD.configuration_revision + 1;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_extension_oauth_configurations_revision
    BEFORE UPDATE ON public.extension_oauth_configurations
    FOR EACH ROW
    EXECUTE FUNCTION public.oauth_configuration_bump_revision();

-- A connection type that authenticates as an application may take its client
-- values from one OAuth configuration of its install (credentials_from). The
-- link is its own column: oauth_configuration_id marks a connection that holds
-- its own OAuth grant. No foreign key: a deleted configuration leaves the link
-- in place, and the connection then reports credentials_unavailable.
CREATE INDEX idx_project_connections_credentials_configuration
    ON public.project_connections USING btree (credentials_configuration_id)
    WHERE (credentials_configuration_id IS NOT NULL);

-- Role permissions and the scope of a role assignment (ADR 0076). The permission
-- catalogue is code (core/application/auth/permissions.py); role_permissions records
-- which keys a role holds. Migration 20261120090000_role_permissions.sql writes the
-- system roles: admin holds every permission implicitly and has no rows, operator and
-- viewer hold explicit lists. An assignment may be limited to a workspace, a project or
-- one workflow of a project; this edition refuses such assignments and reads only the
-- global ones. user_roles.role_is_system copies roles.is_system (the trigger fills it,
-- the composite foreign key follows a change of the role) so that one partial unique
-- index allows at most one system role per user and scope.
ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_id_is_system_key UNIQUE (id, is_system);

CREATE TABLE public.role_permissions (
    role_id uuid NOT NULL REFERENCES public.roles(id) ON DELETE CASCADE,
    permission_key text NOT NULL
        CHECK (permission_key ~ '^[a-z][a-z_]*(\.[a-z][a-z_]*){1,2}$'::text),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_key)
);

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workspace_id_fkey FOREIGN KEY (workspace_id)
        REFERENCES public.workspaces(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_project_id_fkey FOREIGN KEY (project_id)
        REFERENCES public.projects(id) ON DELETE CASCADE;
-- A workflow-scoped assignment names a workflow of its project's account; the trigger
-- below fills workflow_account_id (ADR 0020, step C4b; migration
-- 20261126180000_workflow_account_identity.sql makes the same change).
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_id_fkey FOREIGN KEY (workflow_account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_account_check
    CHECK (((workflow_id IS NULL) OR (workflow_account_id IS NOT NULL)));
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_is_system_fkey FOREIGN KEY (role_id, role_is_system)
        REFERENCES public.roles(id, is_system) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_scope_check
        CHECK (workflow_id IS NULL OR project_id IS NOT NULL);

CREATE FUNCTION public.user_roles_copy_role_is_system() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    SELECT r.is_system INTO NEW.role_is_system FROM public.roles AS r WHERE r.id = NEW.role_id;
    NEW.role_is_system := COALESCE(NEW.role_is_system, false);
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_user_roles_role_is_system
    BEFORE INSERT OR UPDATE OF role_id ON public.user_roles
    FOR EACH ROW EXECUTE FUNCTION public.user_roles_copy_role_is_system();

CREATE FUNCTION public.user_roles_copy_workflow_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.workflow_id IS NULL THEN
        NEW.workflow_account_id := NULL;
    ELSE
        SELECT p.account_id INTO NEW.workflow_account_id
          FROM public.projects AS p WHERE p.id = NEW.project_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_user_roles_workflow_account
    BEFORE INSERT OR UPDATE OF project_id, workflow_id, workflow_account_id ON public.user_roles
    FOR EACH ROW EXECUTE FUNCTION public.user_roles_copy_workflow_account();

-- A project binding takes its project's account when the writer names none, and
-- names a workflow of that account (ADR 0020, step C4b).
CREATE FUNCTION public.project_workflows_copy_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.account_id IS NULL THEN
        SELECT p.account_id INTO NEW.account_id FROM public.projects AS p WHERE p.id = NEW.project_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_project_workflows_account
    BEFORE INSERT OR UPDATE OF project_id, account_id ON public.project_workflows
    FOR EACH ROW EXECUTE FUNCTION public.project_workflows_copy_account();

ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_project_account_fkey FOREIGN KEY (project_id, account_id)
    REFERENCES public.projects(id, account_id) ON DELETE CASCADE;

CREATE UNIQUE INDEX ux_user_roles_one_system_role_per_scope
    ON public.user_roles (user_id, workspace_id, project_id, workflow_id) NULLS NOT DISTINCT
    WHERE role_is_system;
-- End of block: migration 20261120090000_role_permissions.sql.

-- When the owner was reminded that an AI agent connection is about to stop working
-- (ADR 0076 §5): one email per personal agent token or OAuth grant, seven days before
-- its end. Set before the email is sent, so a restart never sends a second one.
-- Migration 20261120140000_agent_expiry_reminders.sql adds the same columns.
ALTER TABLE ONLY public.personal_access_tokens
    ADD COLUMN expiry_reminder_sent_at timestamp with time zone;
ALTER TABLE ONLY public.oauth_grants
    ADD COLUMN expiry_reminder_sent_at timestamp with time zone;

-- Approvers assigned to an Approval step (ADR 0082 section 3a). Core's approval
-- check reads it; only a platform module writes it. No rows for a step means anyone
-- with approvals.decide decides, as before. Rows keep applying after the feature's
-- entitlement ends (ADR 0082 section 4). Removing a user removes their assignment.
-- Migration 20261125120000_approval_assignees_journal.sql adds the same tables.
CREATE TABLE public.approval_step_approvers (
    workflow_id text NOT NULL,
    step_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL,
    CONSTRAINT approval_step_approvers_pkey PRIMARY KEY (account_id, workflow_id, step_id, user_id)
);

CREATE INDEX idx_approval_step_approvers_user
    ON public.approval_step_approvers USING btree (user_id);

-- One append-only row per applied approval decision. It has no foreign key to the
-- job or the account: by default its rows are purged with their job, an account
-- entitled to a longer journal retention keeps them past the job, and rows outlive
-- the deletion of their account. Core never updates a row. decided_by_kind
-- 'automation' is a reaction, dispatched command or other caller with no person.
CREATE TABLE public.approval_decision_journal (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL,
    job_id uuid NOT NULL,
    workflow_id text,
    workflow_name text,
    project_id text,
    step_id text,
    step_label text,
    started_by text,
    started_by_display_name text,
    started_at timestamp with time zone,
    decided_by text NOT NULL,
    decided_by_display_name text,
    decided_by_user_id uuid,
    decided_by_kind text NOT NULL
        CHECK (decided_by_kind = ANY (ARRAY['user'::text, 'service_account'::text, 'external'::text, 'automation'::text])),
    external_actor jsonb,
    action text NOT NULL,
    comment text,
    reviewed_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    changed_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX idx_approval_decision_journal_account_decided
    ON public.approval_decision_journal USING btree (account_id, decided_at DESC);
CREATE INDEX idx_approval_decision_journal_account_workflow
    ON public.approval_decision_journal USING btree (account_id, workflow_id);

-- Project-scoped role limits (ADR 0076 section 8, ADR 0082). A row marks a person
-- whose access is limited to the projects that have a project-scoped user_roles
-- row; in each, the effective role is the lower of the global and the scoped role.
-- The marker is durable: deleting the last listed project removes the scoped
-- user_roles rows by cascade but keeps the person limited. Only a platform module
-- writes it; rows keep applying after the feature's entitlement ends.
-- Migration 20261126120000_scoped_role_users.sql makes the same change.
CREATE TABLE public.scoped_role_users (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    created_by text DEFAULT ''::text NOT NULL
);

CREATE INDEX idx_user_roles_project
    ON public.user_roles USING btree (project_id)
    WHERE (project_id IS NOT NULL);
-- End of block: migration 20261126120000_scoped_role_users.sql.

-- External sign-in (ADR 0083). Core implements no sign-in protocol; platform
-- modules register sign-in methods. An external identity is keyed by issuer and
-- subject within the account it was linked in (one account can never block another
-- account's link), never by an email address, records the sign-in method that
-- verified it and names the one user it signs in. A
-- sign-in attempt of any redirecting method is a single-use state row bound to the
-- browser that started it, with the method's own data encrypted. A session records
-- whether it was issued locally or by external sign-in: the local password and
-- authenticator rules apply only to sessions with auth_method 'local'. Unlinking an
-- identity deletes the sessions it issued.
-- Migration 20261126150000_external_sign_in.sql makes the same change.
CREATE TABLE public.external_identities (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    method text NOT NULL,
    option_key text DEFAULT ''::text NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    last_login_at timestamp with time zone,
    CONSTRAINT external_identities_account_issuer_subject_key
        UNIQUE (account_id, issuer, subject)
);

CREATE INDEX idx_external_identities_user
    ON public.external_identities USING btree (user_id);

CREATE INDEX idx_external_identities_account
    ON public.external_identities USING btree (account_id);

CREATE TABLE public.external_sign_in_states (
    state_hash text PRIMARY KEY,
    method text NOT NULL,
    option_key text NOT NULL,
    browser_binding_hash text NOT NULL,
    method_data_encrypted text DEFAULT ''::text NOT NULL,
    redirect_uri text NOT NULL,
    return_path text DEFAULT '/'::text NOT NULL,
    link_user_id uuid REFERENCES public.users(id) ON DELETE CASCADE,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX idx_external_sign_in_states_expires
    ON public.external_sign_in_states USING btree (expires_at);

ALTER TABLE ONLY public.auth_sessions
    ADD COLUMN auth_method text DEFAULT 'local'::text NOT NULL
        CONSTRAINT auth_sessions_auth_method_check
        CHECK (auth_method = ANY (ARRAY['local'::text, 'external'::text]));

ALTER TABLE ONLY public.auth_sessions
    ADD COLUMN external_identity_id uuid
        REFERENCES public.external_identities(id) ON DELETE CASCADE;

CREATE INDEX idx_auth_sessions_external_identity
    ON public.auth_sessions USING btree (external_identity_id)
    WHERE (external_identity_id IS NOT NULL);
-- End of block: migration 20261126150000_external_sign_in.sql.

-- A step names agents, skills and published blocks of its own account or platform
-- ones (no account); a hook binding and its revisions belong to one project of one
-- account (ADR 0020, step C4c). Migration
-- 20261126190000_registry_same_account_refs.sql makes the same change.
CREATE FUNCTION public.registry_workflow_steps_same_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    block text;
BEGIN
    IF COALESCE(NEW.agent_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_agents AS a
         WHERE a.agent_id = NEW.agent_id
           AND a.account_id IS NOT NULL AND a.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names an agent of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF COALESCE(NEW.skill_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_skills AS s
         WHERE s.skill_id = NEW.skill_id
           AND s.account_id IS NOT NULL AND s.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names a skill of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    FOREACH block IN ARRAY ARRAY[
        NEW.block_key,
        NEW.override ->> 'published_block_key',
        NEW.override ->> 'block_key'
    ] LOOP
        IF COALESCE(block, '') <> '' AND EXISTS (
            SELECT 1 FROM public.registry_published_block_contracts AS b
             WHERE b.block_key = block
               AND b.account_id IS NOT NULL AND b.account_id <> NEW.account_id
        ) THEN
            RAISE EXCEPTION 'step % names a published block of another account', NEW.step_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_registry_workflow_steps_same_account
    BEFORE INSERT OR UPDATE OF account_id, agent_id, skill_id, block_key, override
    ON public.registry_workflow_steps
    FOR EACH ROW EXECUTE FUNCTION public.registry_workflow_steps_same_account();

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_project_account_fkey FOREIGN KEY (project_id, account_id)
    REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_id_scope_key UNIQUE (id, account_id, project_id);
ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_scope_fkey FOREIGN KEY (binding_id, account_id, project_id)
    REFERENCES public.registry_workflow_hook_bindings(id, account_id, project_id) ON DELETE CASCADE;
-- End of block: migration 20261126190000_registry_same_account_refs.sql.

-- Secret backends (ADR 0066 amendment 2026-11-26). A platform module may
-- keep stored-secret values in an external store. A configuration names the
-- backend kind a module registered and its non-secret settings for one account;
-- the backend's credentials never come from this database. A locator maps an
-- account-qualified secret_refs reference to the configuration that keeps its value
-- and the backend's own locator; a reference without one is read locally. A remote
-- value has no secret_refs row and moving it back never deletes it remotely.
-- Migration 20261126200000_secret_backends.sql makes the same change.
CREATE TABLE public.secret_backend_configs (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    backend_kind text NOT NULL CHECK (backend_kind ~ '^[a-z][a-z0-9_.-]{0,63}$' AND backend_kind <> 'local'),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
    non_secret_config jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(non_secret_config) = 'object'),
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT secret_backend_configs_account_name_key UNIQUE (account_id, name),
    CONSTRAINT secret_backend_configs_id_account_kind_key UNIQUE (id, account_id, backend_kind)
);

CREATE TABLE public.secret_backend_locators (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    ref_key text NOT NULL CHECK (char_length(ref_key) BETWEEN 1 AND 512),
    backend_config_id uuid NOT NULL,
    backend_kind text NOT NULL,
    locator jsonb NOT NULL CHECK (jsonb_typeof(locator) = 'object'),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT secret_backend_locators_pkey PRIMARY KEY (account_id, ref_key),
    CONSTRAINT secret_backend_locators_config_fkey FOREIGN KEY (backend_config_id, account_id, backend_kind)
        REFERENCES public.secret_backend_configs(id, account_id, backend_kind)
);

CREATE INDEX idx_secret_backend_locators_config
    ON public.secret_backend_locators USING btree (backend_config_id);
-- End of block: migration 20261126200000_secret_backends.sql.

-- Scoped role limits on workspaces, projects and workflows (ADR 0076 section 8a).
-- A limited person's grants are user_roles rows with workspace_id (a whole
-- workspace, its future projects included), project_id (one project) or
-- project_id + workflow_id (chosen workflows of a project). Deleting a workspace
-- or a workflow removes its rows by cascade: both foreign keys get an index.
-- Migration 20261126210000_multilevel_scoped_roles.sql makes the same change.
CREATE INDEX idx_user_roles_workspace
    ON public.user_roles USING btree (workspace_id)
    WHERE (workspace_id IS NOT NULL);

CREATE INDEX idx_user_roles_workflow
    ON public.user_roles USING btree (workflow_account_id, workflow_id)
    WHERE (workflow_id IS NOT NULL);
-- End of block: migration 20261126210000_multilevel_scoped_roles.sql.

-- A workflow, a field and a data contract have a key that is unique only within an
-- account, so every history entry of one names its account (ADR 0020, C5).
-- Migration 20261127090000_account_scoped_registry_ops.sql makes the same change.
ALTER TABLE ONLY public.registry_changelog
    ADD CONSTRAINT registry_changelog_account_keyed_owner_check CHECK (((entity_type <> ALL (ARRAY['workflow'::text, 'field'::text, 'data_contract'::text])) OR (account_id IS NOT NULL)));
-- End of block: migration 20261127090000_account_scoped_registry_ops.sql.

-- Extensions per account (ADR 0020, step C6). An extension's code is installed once
-- per installation: one extension_installs row, in the installation account. Any
-- other account uses that install only after its own administrator enabled it there;
-- the row also keeps that account's non-secret settings. Disabling keeps the row and
-- its settings; uninstalling the code removes every account's row.
-- Migration 20261127130000_extensions_per_account.sql makes the same change.
CREATE UNIQUE INDEX ux_extension_installs_install_extension
    ON public.extension_installs USING btree (install_id, extension_id);

CREATE TABLE public.extension_account_enablements (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    extension_id text NOT NULL,
    install_id text NOT NULL,
    state text DEFAULT 'enabled'::text NOT NULL CHECK (state IN ('enabled', 'disabled')),
    settings_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(settings_jsonb) = 'object'),
    enabled_at timestamp with time zone,
    enabled_by text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT extension_account_enablements_pkey PRIMARY KEY (account_id, extension_id),
    CONSTRAINT extension_account_enablements_install_fkey FOREIGN KEY (install_id, extension_id)
        REFERENCES public.extension_installs(install_id, extension_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX idx_extension_account_enablements_install
    ON public.extension_account_enablements USING btree (install_id, account_id)
    WHERE (state = 'enabled'::text);
-- End of block: migration 20261127130000_extensions_per_account.sql.

-- Published block keys per account (ADR 0020): (owner_key, block_key) is the key, so
-- two accounts may each publish a block with the same key; a platform block (no
-- account) keeps a key no account block uses. Inputs, outputs and runtime
-- dependencies name the block by (block_owner, block_key). Migration
-- 20261127150000_account_scoped_block_keys.sql makes the same change.
ALTER TABLE ONLY public.registry_published_block_contracts
    ADD COLUMN IF NOT EXISTS owner_key text GENERATED ALWAYS AS (COALESCE((account_id)::text, ''::text)) STORED;

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD COLUMN IF NOT EXISTS block_owner text DEFAULT ''::text NOT NULL;

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD COLUMN IF NOT EXISTS block_owner text DEFAULT ''::text NOT NULL;

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD COLUMN IF NOT EXISTS block_owner text;

UPDATE public.registry_published_block_inputs AS i
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE b.block_key = i.block_key
   AND NOT EXISTS (
       SELECT 1 FROM public.registry_published_block_contracts AS own
        WHERE own.block_key = i.block_key AND own.owner_key = i.block_owner);

UPDATE public.registry_published_block_outputs AS o
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE b.block_key = o.block_key
   AND NOT EXISTS (
       SELECT 1 FROM public.registry_published_block_contracts AS own
        WHERE own.block_key = o.block_key AND own.owner_key = o.block_owner);

UPDATE public.workflow_runtime_dependencies AS d
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE d.block_key IS NOT NULL
   AND d.block_owner IS NULL
   AND b.block_key = d.block_key
   AND (b.account_id IS NULL OR b.account_id = d.account_id);

UPDATE public.workflow_runtime_dependencies
   SET block_key = NULL
 WHERE block_key IS NOT NULL
   AND block_owner IS NULL;

ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_key_fkey;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_fkey;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_key_fkey;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_fkey;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_key_fkey;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_fkey;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_key_field_id_key;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_field_key;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_key_field_id_key;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_field_key;
ALTER TABLE ONLY public.registry_published_block_contracts
    DROP CONSTRAINT IF EXISTS registry_published_block_contracts_pkey;

ALTER TABLE ONLY public.registry_published_block_contracts
    ADD CONSTRAINT registry_published_block_contracts_pkey PRIMARY KEY (owner_key, block_key);
ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_field_key UNIQUE (block_owner, block_key, field_id);
ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_field_key UNIQUE (block_owner, block_key, field_id);
ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_owner_check;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_block_owner_check CHECK (((block_key IS NULL) OR (block_owner IS NOT NULL)));

CREATE OR REPLACE FUNCTION public.registry_published_block_key_scope() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Writers of one key queue on a transaction lock, so the second one checks
    -- after the first committed (READ COMMITTED: each statement sees the commits
    -- before it) and two concurrent writers cannot both pass.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('flow-steward:published-block-key:' || NEW.block_key, 0));
    IF EXISTS (
        SELECT 1 FROM public.registry_published_block_contracts AS b
         WHERE b.block_key = NEW.block_key AND b.account_id IS NULL
    ) AND EXISTS (
        SELECT 1 FROM public.registry_published_block_contracts AS b
         WHERE b.block_key = NEW.block_key AND b.account_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'published block key % is used by a platform block and an account block', NEW.block_key
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_registry_published_block_key_scope ON public.registry_published_block_contracts;
CREATE TRIGGER trg_registry_published_block_key_scope
    AFTER INSERT OR UPDATE OF account_id, block_key
    ON public.registry_published_block_contracts
    FOR EACH ROW EXECUTE FUNCTION public.registry_published_block_key_scope();

CREATE OR REPLACE FUNCTION public.registry_workflow_steps_same_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    block text;
BEGIN
    IF COALESCE(NEW.agent_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_agents AS a
         WHERE a.agent_id = NEW.agent_id
           AND a.account_id IS NOT NULL AND a.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names an agent of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF COALESCE(NEW.skill_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_skills AS s
         WHERE s.skill_id = NEW.skill_id
           AND s.account_id IS NOT NULL AND s.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names a skill of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    FOREACH block IN ARRAY ARRAY[
        NEW.block_key,
        NEW.override ->> 'published_block_key',
        NEW.override ->> 'block_key'
    ] LOOP
        IF COALESCE(block, '') <> '' AND EXISTS (
            SELECT 1 FROM public.registry_published_block_contracts AS b
             WHERE b.block_key = block
        ) AND NOT EXISTS (
            SELECT 1 FROM public.registry_published_block_contracts AS b
             WHERE b.block_key = block
               AND (b.account_id IS NULL OR b.account_id = NEW.account_id)
        ) THEN
            RAISE EXCEPTION 'step % names a published block of another account', NEW.step_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
-- End of block: migration 20261127150000_account_scoped_block_keys.sql.

-- Roles per account (ADR 0085): the membership carries the person's role in that
-- account, user_roles keeps scoped rows only, and a scope limit is per account.
-- Migration 20261127170000_roles_per_account.sql makes the same change.
ALTER TABLE ONLY public.user_account_memberships
    ADD COLUMN IF NOT EXISTS role_id uuid;

ALTER TABLE ONLY public.user_account_memberships
    DROP CONSTRAINT IF EXISTS user_account_memberships_role_id_fkey;
ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_role_id_fkey FOREIGN KEY (role_id)
        REFERENCES public.roles(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_user_account_memberships_role
    ON public.user_account_memberships USING btree (account_id, role_id);

INSERT INTO public.user_account_memberships (user_id, account_id, is_default)
SELECT u.id,
       u.account_id,
       NOT EXISTS (
           SELECT 1 FROM public.user_account_memberships AS d
            WHERE d.user_id = u.id AND d.is_default)
  FROM public.users AS u
  JOIN public.accounts AS a ON a.id = u.account_id
 WHERE u.deleted_at IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM public.user_account_memberships AS m
        WHERE m.user_id = u.id AND m.account_id = u.account_id)
ON CONFLICT (user_id, account_id) DO NOTHING;

UPDATE public.user_account_memberships AS m
   SET role_id = best.role_id
  FROM (
      SELECT DISTINCT ON (ur.user_id) ur.user_id, ur.role_id
        FROM public.user_roles AS ur
        JOIN public.roles AS r ON r.id = ur.role_id
       WHERE ur.workspace_id IS NULL
         AND ur.project_id IS NULL
         AND ur.workflow_id IS NULL
         AND r.key IN ('admin', 'operator', 'viewer')
       ORDER BY ur.user_id,
                CASE r.key WHEN 'admin' THEN 3 WHEN 'operator' THEN 2 ELSE 1 END DESC
  ) AS best
 WHERE best.user_id = m.user_id
   AND m.role_id IS NULL;

DO $$
DECLARE
    without_role bigint;
    other_global bigint;
BEGIN
    SELECT count(*) INTO without_role
      FROM public.user_account_memberships AS m
      JOIN public.users AS u ON u.id = m.user_id
     WHERE m.role_id IS NULL
       AND u.deleted_at IS NULL;
    SELECT count(*) INTO other_global
      FROM public.user_roles AS ur
      JOIN public.roles AS r ON r.id = ur.role_id
     WHERE ur.workspace_id IS NULL
       AND ur.project_id IS NULL
       AND ur.workflow_id IS NULL
       AND r.key NOT IN ('admin', 'operator', 'viewer');
    RAISE NOTICE 'roles_per_account: % membership(s) of live people have no role (no access); % global row(s) of other roles removed',
        without_role, other_global;
END;
$$;

DELETE FROM public.user_roles AS ur
 WHERE ur.workspace_id IS NULL
   AND ur.project_id IS NULL
   AND ur.workflow_id IS NULL;

ALTER TABLE ONLY public.user_roles
    DROP CONSTRAINT IF EXISTS user_roles_scoped_only_check;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_scoped_only_check
        CHECK (((workspace_id IS NOT NULL) OR (project_id IS NOT NULL) OR (workflow_id IS NOT NULL)));

ALTER TABLE ONLY public.scoped_role_users
    DROP CONSTRAINT IF EXISTS scoped_role_users_pkey;
ALTER TABLE ONLY public.scoped_role_users
    ADD CONSTRAINT scoped_role_users_pkey PRIMARY KEY (user_id, account_id);
-- End of block: migration 20261127170000_roles_per_account.sql.

-- Invitations to join an account (ADR 0085, consent): a person outside the accounts
-- an administrator manages becomes a member only when they accept with the single-use
-- link of the invitation (token_hash is its SHA-256). invited_email is the address as
-- typed, for the administrators' list; user_id is the person who answered it.
-- Migration 20261127190000_account_invitations.sql makes the same change.
CREATE TABLE IF NOT EXISTS public.account_invitations (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    invited_email text NOT NULL,
    user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
    role text NOT NULL,
    invited_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    CONSTRAINT account_invitations_token_hash_key UNIQUE (token_hash),
    CONSTRAINT account_invitations_email_check
        CHECK (invited_email <> '' AND length(invited_email) <= 320),
    CONSTRAINT account_invitations_token_hash_check CHECK (length(token_hash) = 64),
    CONSTRAINT account_invitations_role_check
        CHECK (role IN ('admin', 'operator', 'viewer')),
    CONSTRAINT account_invitations_status_check
        CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
    CONSTRAINT account_invitations_lifetime_check CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS account_invitations_pending_key
    ON public.account_invitations USING btree (account_id, lower(invited_email))
    WHERE status = 'pending';
-- End of block: migration 20261127190000_account_invitations.sql.

-- Requests to install an extension (ADR 0084, "Requesting an extension"): an account
-- administrator of a company account asks the installation administrator to install
-- one catalog extension; installing it enables it for that account. extension_id is
-- the catalog id; extension_name is the catalog name when the request was made, for
-- the installation administrator's list. One pending request per account and
-- extension; approved, declined and cancelled requests are kept as the record.
-- Migration 20261127210000_extension_install_requests.sql makes the same change.
CREATE TABLE IF NOT EXISTS public.extension_install_requests (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    extension_id text NOT NULL,
    extension_name text DEFAULT ''::text NOT NULL,
    requested_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    note text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    decided_at timestamp with time zone,
    decline_reason text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_install_requests_extension_id_check
        CHECK (extension_id <> '' AND length(extension_id) <= 200),
    CONSTRAINT extension_install_requests_extension_name_check
        CHECK (length(extension_name) <= 200),
    CONSTRAINT extension_install_requests_note_check CHECK (length(note) <= 500),
    CONSTRAINT extension_install_requests_decline_reason_check
        CHECK (length(decline_reason) <= 500),
    CONSTRAINT extension_install_requests_status_check
        CHECK (status IN ('pending', 'approved', 'declined', 'cancelled')),
    CONSTRAINT extension_install_requests_decided_check
        CHECK ((status = 'pending') = (decided_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS extension_install_requests_pending_key
    ON public.extension_install_requests USING btree (account_id, extension_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_extension_install_requests_account
    ON public.extension_install_requests USING btree (account_id, created_at);
-- End of block: migration 20261127210000_extension_install_requests.sql.
