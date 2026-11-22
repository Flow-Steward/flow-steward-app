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
    workflow_id text REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE,
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
    CONSTRAINT catalog_secret_bindings_workflow_field_key UNIQUE (workflow_id, field_name),
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
    workflow_id text REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE,
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
    CONSTRAINT catalog_secret_approvals_workflow_key UNIQUE (workflow_id)
);

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
