-- Migration: registry_account_ownership
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Registry fields and data contracts belong to an account (ADR 0020, step C4a).
-- Every existing row is kept and given to the installation's default account; a
-- key is unique within its account, so two accounts may use the same field key or
-- contract id. References follow the owner: a field may only name a data contract
-- of its own account, and a skill input or output names the contract's account
-- next to its id. The DDL repeats db/schema/; every statement is safe to run again
-- and is a no-op on a fresh installation. Rollback is described in ADR 0020.
ALTER TABLE ONLY public.registry_data_contracts
    ADD COLUMN IF NOT EXISTS account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL;
ALTER TABLE ONLY public.registry_fields
    ADD COLUMN IF NOT EXISTS account_id uuid DEFAULT '00000000-0000-0000-0000-000000000002'::uuid NOT NULL;
ALTER TABLE ONLY public.registry_skill_inputs ADD COLUMN IF NOT EXISTS contract_account_id uuid;
ALTER TABLE ONLY public.registry_skill_outputs ADD COLUMN IF NOT EXISTS contract_account_id uuid;
UPDATE public.registry_skill_inputs
   SET contract_account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE contract_id IS NOT NULL AND contract_account_id IS NULL;
UPDATE public.registry_skill_outputs
   SET contract_account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE contract_id IS NOT NULL AND contract_account_id IS NULL;

ALTER TABLE ONLY public.registry_fields DROP CONSTRAINT IF EXISTS registry_fields_data_contract_id_fkey;
ALTER TABLE ONLY public.registry_skill_inputs DROP CONSTRAINT IF EXISTS registry_skill_inputs_contract_id_fkey;
ALTER TABLE ONLY public.registry_skill_outputs DROP CONSTRAINT IF EXISTS registry_skill_outputs_contract_id_fkey;

ALTER TABLE ONLY public.registry_data_contracts DROP CONSTRAINT IF EXISTS registry_data_contracts_pkey;
ALTER TABLE ONLY public.registry_data_contracts
    ADD CONSTRAINT registry_data_contracts_pkey PRIMARY KEY (account_id, contract_id);

ALTER TABLE ONLY public.registry_fields DROP CONSTRAINT IF EXISTS registry_fields_field_key_key;
ALTER TABLE ONLY public.registry_fields DROP CONSTRAINT IF EXISTS registry_fields_account_id_field_key_key;
ALTER TABLE ONLY public.registry_fields
    ADD CONSTRAINT registry_fields_account_id_field_key_key UNIQUE (account_id, field_key);

ALTER TABLE ONLY public.registry_skill_inputs DROP CONSTRAINT IF EXISTS registry_skill_inputs_contract_owner_check;
ALTER TABLE ONLY public.registry_skill_inputs
    ADD CONSTRAINT registry_skill_inputs_contract_owner_check
    CHECK (((contract_id IS NULL) OR (contract_account_id IS NOT NULL)));
ALTER TABLE ONLY public.registry_skill_outputs DROP CONSTRAINT IF EXISTS registry_skill_outputs_contract_owner_check;
ALTER TABLE ONLY public.registry_skill_outputs
    ADD CONSTRAINT registry_skill_outputs_contract_owner_check
    CHECK (((contract_id IS NULL) OR (contract_account_id IS NOT NULL)));

ALTER TABLE ONLY public.registry_fields
    ADD CONSTRAINT registry_fields_data_contract_id_fkey FOREIGN KEY (account_id, data_contract_id)
    REFERENCES public.registry_data_contracts(account_id, contract_id) ON DELETE SET NULL (data_contract_id);
ALTER TABLE ONLY public.registry_skill_inputs
    ADD CONSTRAINT registry_skill_inputs_contract_id_fkey FOREIGN KEY (contract_account_id, contract_id)
    REFERENCES public.registry_data_contracts(account_id, contract_id) ON DELETE SET NULL (contract_account_id, contract_id);
ALTER TABLE ONLY public.registry_skill_outputs
    ADD CONSTRAINT registry_skill_outputs_contract_id_fkey FOREIGN KEY (contract_account_id, contract_id)
    REFERENCES public.registry_data_contracts(account_id, contract_id) ON DELETE SET NULL (contract_account_id, contract_id);
