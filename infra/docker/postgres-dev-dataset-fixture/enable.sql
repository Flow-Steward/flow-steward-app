DO $fixture$
DECLARE
    stack_environment text := current_setting('flow_steward.stack_environment', true);
    reader_password text := current_setting(
        'flow_steward.dev_dataset_reader_password',
        true
    );
    reader_role text := COALESCE(
        NULLIF(current_setting('flow_steward.dev_dataset_reader_role', true), ''),
        'flow_steward_dev_dataset_reader'
    );
    fixture_schema text := COALESCE(
        NULLIF(current_setting('flow_steward.dev_dataset_reader_schema', true), ''),
        'flow_steward_dev_dataset'
    );
BEGIN
    IF stack_environment <> 'development' THEN
        RAISE EXCEPTION 'The local dataset reader fixture is development only';
    END IF;
    IF reader_password IS NULL OR reader_password = '' THEN
        RAISE EXCEPTION 'The local dataset reader fixture password is required';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        EXECUTE format(
            'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
            'NOCREATEROLE NOREPLICATION CONNECTION LIMIT 5 PASSWORD %L',
            reader_role,
            reader_password
        );
    ELSE
        EXECUTE format(
            'ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
            'NOCREATEROLE NOREPLICATION CONNECTION LIMIT 5 PASSWORD %L',
            reader_role,
            reader_password
        );
    END IF;

    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', fixture_schema);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', fixture_schema);
    EXECUTE format(
        'CREATE OR REPLACE VIEW %I.recent_jobs WITH (security_barrier = true) AS '
        'SELECT id, status, selected_workflow, started_at, finished_at '
        'FROM public.jobs WHERE deleted_at IS NULL',
        fixture_schema
    );
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', fixture_schema, reader_role);
    EXECUTE format(
        'REVOKE ALL ON TABLE %I.recent_jobs FROM PUBLIC, %I',
        fixture_schema,
        reader_role
    );
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO %I',
        current_database(),
        reader_role
    );
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', fixture_schema, reader_role);
    EXECUTE format(
        'GRANT SELECT ON TABLE %I.recent_jobs TO %I',
        fixture_schema,
        reader_role
    );
    EXECUTE format('REVOKE ALL ON TABLE public.jobs FROM %I', reader_role);
    EXECUTE format(
        'GRANT SELECT (id, status, selected_workflow, started_at, finished_at) '
        'ON TABLE public.jobs TO %I',
        reader_role
    );
    EXECUTE format(
        'ALTER ROLE %I SET search_path TO %I, public, pg_catalog',
        reader_role,
        fixture_schema
    );
    EXECUTE format('ALTER ROLE %I SET statement_timeout TO %L', reader_role, '30s');
    EXECUTE format(
        'ALTER ROLE %I SET idle_in_transaction_session_timeout TO %L',
        reader_role,
        '30s'
    );
END
$fixture$;
