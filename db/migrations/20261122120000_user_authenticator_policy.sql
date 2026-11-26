-- Record an administrator's authenticator policy and outstanding reset separately.
-- transaction: true

ALTER TABLE ONLY public.users
    ADD COLUMN IF NOT EXISTS authenticator_required boolean DEFAULT false NOT NULL,
    ADD COLUMN IF NOT EXISTS authenticator_reset_pending boolean DEFAULT false NOT NULL;
