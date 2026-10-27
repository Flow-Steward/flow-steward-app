DO $fixture$
DECLARE
    reader_role text := COALESCE(
        NULLIF(current_setting('flow_steward.dev_dataset_reader_role', true), ''),
        'flow_steward_dev_dataset_reader'
    );
    fixture_schema text := COALESCE(
        NULLIF(current_setting('flow_steward.dev_dataset_reader_schema', true), ''),
        'flow_steward_dev_dataset'
    );
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        PERFORM pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE usename = reader_role
          AND pid <> pg_backend_pid();
    END IF;

    EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', fixture_schema);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = reader_role) THEN
        EXECUTE format('DROP OWNED BY %I', reader_role);
        EXECUTE format('DROP ROLE %I', reader_role);
    END IF;
END
$fixture$;
