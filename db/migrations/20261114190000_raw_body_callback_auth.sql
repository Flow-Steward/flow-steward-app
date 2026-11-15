-- Admit the provider-neutral raw-body HMAC profile without changing legacy rows.
-- transaction: true
ALTER TABLE ONLY public.extension_callback_registrations
    DROP CONSTRAINT IF EXISTS extension_callback_registrations_auth_type_check;
ALTER TABLE ONLY public.extension_callback_registrations
    ADD CONSTRAINT extension_callback_registrations_auth_type_check
    CHECK (auth_type = ANY (ARRAY['none'::text, 'shared_secret_header'::text, 'hmac_sha256'::text, 'hub_signature'::text, 'raw_body_hmac_sha256'::text]));
