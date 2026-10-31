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
