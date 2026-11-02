-- Migration: secret_catalog
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- Transaction control:
--   -- transaction: true   (default) the runner wraps this file in a
--                          single transaction together with the tracking
--                          insert; any failure rolls the whole migration back.
--   -- transaction: false  use for statements that cannot run inside a
--                          transaction block, e.g. CREATE INDEX CONCURRENTLY.
--                          The runner records durable in-progress state first;
--                          interrupted outcomes require manual reconciliation.
-- transaction: true

-- Secret catalog (ADR 0055) and the upgrade phase ledger (ADR 0054).
--
-- The DDL is the baseline block at the end of 001_schema_part_12.sql with IF NOT
-- EXISTS added, so a fresh installation (which already has the tables) is unchanged
-- and an upgraded one gets exactly the same objects.
--
-- The data move below turns every existing static connection credential into a
-- catalog secret that points at its existing secret_refs row: no ciphertext is read,
-- copied or compared. OAuth grant values (_oauth_*) are not static credentials and
-- stay where they are. A reference whose value row is missing stays unbound, as it is
-- already unusable; preflight lists it. Both inserts are idempotent, and on a fresh
-- installation there is nothing to move.

CREATE TABLE IF NOT EXISTS public.catalog_secrets (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE RESTRICT,
    workspace_id uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE RESTRICT,
    project_id uuid REFERENCES public.projects(id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS public.catalog_secret_bindings (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL,
    secret_id uuid NOT NULL,
    consumer_kind text NOT NULL CHECK (consumer_kind = 'project_connection'),
    project_connection_id text REFERENCES public.project_connections(connection_id) ON DELETE CASCADE,
    field_name text NOT NULL
        CHECK (char_length(btrim(field_name)) BETWEEN 1 AND 200 AND field_name NOT LIKE '\_oauth\_%'),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT catalog_secret_bindings_secret_fkey FOREIGN KEY (secret_id, account_id)
        REFERENCES public.catalog_secrets(id, account_id) ON DELETE RESTRICT,
    CONSTRAINT catalog_secret_bindings_consumer_check
        CHECK ((consumer_kind = 'project_connection') = (project_connection_id IS NOT NULL)),
    CONSTRAINT catalog_secret_bindings_connection_field_key UNIQUE (project_connection_id, field_name)
);

CREATE TABLE IF NOT EXISTS public.catalog_secret_approvals (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE RESTRICT,
    consumer_kind text NOT NULL CHECK (consumer_kind = 'project_connection'),
    project_connection_id text REFERENCES public.project_connections(connection_id) ON DELETE CASCADE,
    fingerprint text NOT NULL CHECK (fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    projection_jsonb jsonb NOT NULL CHECK (jsonb_typeof(projection_jsonb) = 'object'),
    approved_by text NOT NULL CHECK (char_length(btrim(approved_by)) > 0),
    approved_by_kind text NOT NULL CHECK (approved_by_kind = ANY (ARRAY['human'::text, 'upgrade'::text])),
    approved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT catalog_secret_approvals_consumer_check
        CHECK ((consumer_kind = 'project_connection') = (project_connection_id IS NOT NULL)),
    CONSTRAINT catalog_secret_approvals_connection_key UNIQUE (project_connection_id)
);

CREATE TABLE IF NOT EXISTS public.platform_upgrade_phases (
    phase_id text PRIMARY KEY CHECK (phase_id ~ '^[a-z][a-z0-9_]{2,127}$'),
    release_version text NOT NULL,
    summary_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(summary_jsonb) = 'object'),
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalog_secrets_scope ON public.catalog_secrets (account_id, workspace_id, project_id);
CREATE INDEX IF NOT EXISTS idx_catalog_secret_bindings_secret ON public.catalog_secret_bindings (secret_id);

CREATE TEMPORARY TABLE catalog_secret_move ON COMMIT DROP AS
SELECT pc.connection_id,
       pc.account_id,
       pc.project_id,
       p.workspace_id,
       COALESCE(NULLIF(btrim(pc.display_name), ''), pc.connection_id) AS connection_label,
       refs.key AS field_name,
       btrim(refs.value) AS storage_ref_key
  FROM public.project_connections AS pc
  JOIN public.projects AS p ON p.id = pc.project_id
 CROSS JOIN LATERAL jsonb_each_text(
       CASE WHEN jsonb_typeof(pc.secret_refs -> 'refs') = 'object'
            THEN pc.secret_refs -> 'refs' ELSE '{}'::jsonb END) AS refs
 WHERE refs.key NOT LIKE '\_oauth\_%'
   AND btrim(refs.key) <> ''
   AND btrim(refs.value) <> ''
   AND EXISTS (
       SELECT 1 FROM public.secret_refs AS s
        WHERE s.account_id = pc.account_id AND s.ref_key = btrim(refs.value)
   );

-- One stored value becomes one secret, owned by the project its connections are in.
-- A value that connections of more than one project share has no owner this move may
-- choose without changing who can use it, so the move refuses it: preflight reports
-- such references before anything changes, and this check keeps the migration from
-- ever deciding on its own.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM catalog_secret_move
         GROUP BY account_id, storage_ref_key
        HAVING count(DISTINCT project_id) > 1
    ) THEN
        RAISE EXCEPTION 'secret_catalog_shared_value: a stored connection credential is shared by connections of different projects; run database upgrade --preflight for the list';
    END IF;
END
$$;

-- The name is "<connection> / <field>", at most 200 characters. When two moved secrets
-- of one account would share a name, both get the connection id as a suffix, and the
-- base is shortened first so the whole name still fits.
INSERT INTO public.catalog_secrets (
    account_id, workspace_id, project_id, name, storage_ref_key,
    created_by, metadata_updated_by, value_updated_by
)
SELECT owner.account_id,
       owner.workspace_id,
       owner.project_id,
       CASE WHEN count(*) OVER (PARTITION BY owner.account_id, left(owner.base_name, 200)) > 1
            THEN left(owner.base_name, greatest(1, 200 - char_length(owner.suffix))) || owner.suffix
            ELSE left(owner.base_name, 200) END,
       owner.storage_ref_key,
       'system:upgrade:secret_catalog',
       'system:upgrade:secret_catalog',
       'system:upgrade:secret_catalog'
  FROM (
       SELECT DISTINCT ON (m.account_id, m.storage_ref_key)
              m.account_id,
              m.workspace_id,
              m.project_id,
              m.storage_ref_key,
              m.connection_label || ' / ' || m.field_name AS base_name,
              ' (' || m.connection_id || ')' AS suffix
         FROM catalog_secret_move AS m
        ORDER BY m.account_id, m.storage_ref_key, m.connection_id
  ) AS owner
ON CONFLICT (account_id, storage_ref_key) DO NOTHING;

INSERT INTO public.catalog_secret_bindings (
    account_id, secret_id, consumer_kind, project_connection_id, field_name, created_by
)
SELECT m.account_id, cs.id, 'project_connection', m.connection_id, m.field_name,
       'system:upgrade:secret_catalog'
  FROM catalog_secret_move AS m
  JOIN public.catalog_secrets AS cs
    ON cs.account_id = m.account_id AND cs.storage_ref_key = m.storage_ref_key
 WHERE cs.project_id = m.project_id
ON CONFLICT (project_connection_id, field_name) DO NOTHING;
