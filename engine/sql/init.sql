-- Runs once, before any Alembic migration, via the postgres image entrypoint.
--
-- The application connects as healops_app, NOT as the superuser. That separation is what makes
-- `REVOKE UPDATE, DELETE ON audit_ledger` meaningful: an audit table the application can rewrite
-- is not an audit table. Migrations run as the owner; the app runs with less.

CREATE ROLE healops_app WITH LOGIN PASSWORD 'healops';

GRANT CONNECT ON DATABASE healops TO healops_app;
GRANT USAGE ON SCHEMA public TO healops_app;

-- Default privileges for tables Alembic creates later. The ledger migration narrows its own
-- grants immediately after creating the table.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO healops_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO healops_app;

-- uuidv7() is built in as of PostgreSQL 18, so no extension is required. Fail loudly here rather
-- than at the first ledger insert if someone points this at an older server.
DO $$
BEGIN
    IF current_setting('server_version_num')::int < 180000 THEN
        RAISE EXCEPTION
            'HealOps requires PostgreSQL 18 or newer for uuidv7() and temporal constraints (found %)',
            current_setting('server_version');
    END IF;
END
$$;
