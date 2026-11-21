-- Keep the ten most recent prior local password hashes for each user.
-- transaction: true

CREATE TABLE IF NOT EXISTS public.auth_password_history (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    id bigint GENERATED ALWAYS AS IDENTITY,
    password_hash text NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE OR REPLACE FUNCTION public.archive_previous_password_hash() RETURNS trigger
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

CREATE OR REPLACE TRIGGER trg_users_archive_previous_password
    AFTER UPDATE OF password_hash ON public.users
    FOR EACH ROW
    WHEN (OLD.password_hash IS DISTINCT FROM NEW.password_hash)
    EXECUTE FUNCTION public.archive_previous_password_hash();
