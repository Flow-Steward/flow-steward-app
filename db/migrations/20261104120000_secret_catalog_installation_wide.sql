-- A catalog secret belongs to the installation (ADR 0057): every project may choose
-- it, so the secret keeps no owning workspace or project. Data connections, MCP
-- connectors and LLM provider installs may now use one too. Bindings still record the
-- consumer and its project. Every secret, value and binding is kept; a secret that a
-- project used to own is now available to all projects.
--
-- The two owner columns stay, empty and without their foreign keys, because
-- 20261103090000_secret_catalog.sql names them and also runs on a fresh install,
-- after db/schema/. Without the keys, deleting a project or a workspace no longer
-- deletes or blocks on a secret.
-- transaction: true
ALTER TABLE ONLY public.catalog_secrets DROP CONSTRAINT IF EXISTS catalog_secrets_workspace_id_fkey;
ALTER TABLE ONLY public.catalog_secrets DROP CONSTRAINT IF EXISTS catalog_secrets_project_id_fkey;
ALTER TABLE ONLY public.catalog_secrets ALTER COLUMN workspace_id DROP NOT NULL;
UPDATE public.catalog_secrets SET workspace_id = NULL, project_id = NULL
 WHERE workspace_id IS NOT NULL OR project_id IS NOT NULL;
ALTER TABLE ONLY public.catalog_secret_bindings
    ADD COLUMN IF NOT EXISTS data_connection_id text
        REFERENCES public.workspace_data_connections(connection_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.catalog_secret_bindings
    ADD COLUMN IF NOT EXISTS mcp_connector_id text
        REFERENCES public.mcp_connectors(connector_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.catalog_secret_bindings ADD COLUMN IF NOT EXISTS llm_provider_install_id text;
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_consumer_kind_check;
ALTER TABLE ONLY public.catalog_secret_bindings ADD CONSTRAINT catalog_secret_bindings_consumer_kind_check
    CHECK (consumer_kind = ANY (ARRAY[
        'project_connection'::text, 'workflow_input'::text, 'data_connection'::text,
        'mcp_connector'::text, 'llm_provider'::text]));
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_consumer_check;
ALTER TABLE ONLY public.catalog_secret_bindings ADD CONSTRAINT catalog_secret_bindings_consumer_check CHECK (
    num_nonnulls(project_connection_id, workflow_id, data_connection_id,
                 mcp_connector_id, llm_provider_install_id) = 1
    AND ((consumer_kind = 'project_connection' AND project_connection_id IS NOT NULL)
      OR (consumer_kind = 'workflow_input'
          AND workflow_id IS NOT NULL AND consumer_project_id IS NOT NULL)
      OR (consumer_kind = 'data_connection' AND data_connection_id IS NOT NULL)
      OR (consumer_kind = 'mcp_connector' AND mcp_connector_id IS NOT NULL)
      OR (consumer_kind = 'llm_provider' AND llm_provider_install_id IS NOT NULL)));
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_data_connection_field_key;
ALTER TABLE ONLY public.catalog_secret_bindings ADD CONSTRAINT catalog_secret_bindings_data_connection_field_key
    UNIQUE (data_connection_id, field_name);
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_mcp_connector_field_key;
ALTER TABLE ONLY public.catalog_secret_bindings ADD CONSTRAINT catalog_secret_bindings_mcp_connector_field_key
    UNIQUE (mcp_connector_id, field_name);
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_llm_provider_field_key;
ALTER TABLE ONLY public.catalog_secret_bindings ADD CONSTRAINT catalog_secret_bindings_llm_provider_field_key
    UNIQUE (llm_provider_install_id, field_name);
