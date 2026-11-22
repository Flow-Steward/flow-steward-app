-- Migration: system_email_sender
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- An explicitly confirmed system email sender. The DDL is the sender_revision
-- column in db/schema/001_schema_part_02.sql and the block at the end of
-- db/schema/001_schema_part_12.sql, made idempotent. Existing connections start
-- at sender_revision 1; an install that already sends system email is pinned
-- once, at runtime, to the connection the earlier rule picked (with an audit
-- entry), so its confirmation records that starting revision.
ALTER TABLE ONLY public.project_connections ADD COLUMN IF NOT EXISTS sender_revision bigint DEFAULT 1 NOT NULL;

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

CREATE OR REPLACE TRIGGER trg_project_connections_sender_revision
    BEFORE UPDATE ON public.project_connections
    FOR EACH ROW
    EXECUTE FUNCTION public.project_connection_bump_sender_revision();

-- An OAuth configuration keeps its id across edits, so the system email
-- sender's confirmation records its configuration_revision. Every change to
-- what signs in (client settings, client secrets, provider, provider security)
-- moves the revision, even through a write path that forgot to.
CREATE OR REPLACE FUNCTION public.oauth_configuration_bump_revision() RETURNS trigger
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

CREATE OR REPLACE TRIGGER trg_extension_oauth_configurations_revision
    BEFORE UPDATE ON public.extension_oauth_configurations
    FOR EACH ROW
    EXECUTE FUNCTION public.oauth_configuration_bump_revision();
