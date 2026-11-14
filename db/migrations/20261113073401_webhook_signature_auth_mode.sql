-- A workflow webhook trigger can verify a provider's signed request
-- ("Signed webhook (HMAC)", auth_mode 'webhook_signature'). The application accepts
-- that mode, but the auth_mode CHECK constraint still listed
-- only the four older modes, so saving such a trigger failed. Replace the
-- constraint with one that also allows 'webhook_signature'; every existing row
-- already satisfies it, and running this twice ends with the same constraint.
-- transaction: true
ALTER TABLE ONLY public.workflow_entrypoints DROP CONSTRAINT IF EXISTS workflow_entrypoints_auth_mode_check;
ALTER TABLE ONLY public.workflow_entrypoints ADD CONSTRAINT workflow_entrypoints_auth_mode_check CHECK ((auth_mode = ANY (ARRAY['shared_secret_header'::text, 'bearer_token'::text, 'basic_auth'::text, 'hmac_sha256'::text, 'webhook_signature'::text])));
