-- Migration: connection_credentials_from
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Connections that take their client values from an OAuth configuration
-- (credentials_from). The DDL is the credentials_configuration_id column in
-- db/schema/001_schema_part_02.sql, its index at the end of
-- db/schema/001_schema_part_12.sql, and the sender_revision trigger function,
-- which now also counts a change of that link. Idempotent.
ALTER TABLE ONLY public.project_connections ADD COLUMN IF NOT EXISTS credentials_configuration_id uuid;

CREATE INDEX IF NOT EXISTS idx_project_connections_credentials_configuration
    ON public.project_connections USING btree (credentials_configuration_id)
    WHERE (credentials_configuration_id IS NOT NULL);

CREATE OR REPLACE FUNCTION public.project_connection_bump_sender_revision() RETURNS trigger
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

