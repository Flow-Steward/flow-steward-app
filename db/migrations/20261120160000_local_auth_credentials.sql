-- Track local password age and store local authenticator enrollment and recovery codes.
-- transaction: true

-- Existing installations lack credential history. Account creation bounds the
-- possible age from above, without giving old credentials a fresh 365 days.
ALTER TABLE ONLY public.users
    ADD COLUMN IF NOT EXISTS password_changed_at timestamp with time zone;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.users WHERE password_changed_at IS NULL) THEN
        ALTER TABLE ONLY public.users DISABLE TRIGGER trg_users_updated;
        UPDATE public.users SET password_changed_at = created_at
            WHERE password_changed_at IS NULL;
        ALTER TABLE ONLY public.users ENABLE TRIGGER trg_users_updated;
    END IF;
END;
$$;

ALTER TABLE ONLY public.users
    ALTER COLUMN password_changed_at SET DEFAULT now(),
    ALTER COLUMN password_changed_at SET NOT NULL;

CREATE TABLE IF NOT EXISTS public.auth_totp_factors (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    secret_enc text,
    pending_secret_enc text,
    pending_expires_at timestamp with time zone,
    last_used_step bigint
);

CREATE TABLE IF NOT EXISTS public.auth_mfa_recovery_codes (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    PRIMARY KEY (user_id, code_hash)
);

CREATE OR REPLACE FUNCTION public.update_password_changed_at() RETURNS trigger
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

CREATE OR REPLACE TRIGGER trg_users_password_changed
    BEFORE UPDATE OF password_hash ON public.users
    FOR EACH ROW
    WHEN (OLD.password_hash IS DISTINCT FROM NEW.password_hash)
    EXECUTE FUNCTION public.update_password_changed_at();
