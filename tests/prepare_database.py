"""Bootstrap only the isolated loopback test database; never connect to a remote project."""
from pathlib import Path

import psycopg

TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:55434/birdnet_s4_test"


def prepare():
    with psycopg.connect("postgresql://postgres@127.0.0.1:55434/postgres", autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname='birdnet_s4_test'").fetchone():
            connection.execute("CREATE DATABASE birdnet_s4_test")
        for role in ("authenticated", "anon", "service_role"):
            if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                connection.execute(psycopg.sql.SQL("CREATE ROLE {} NOLOGIN").format(psycopg.sql.Identifier(role)))
    with psycopg.connect(TEST_DATABASE_URL) as connection:
        if connection.execute("SELECT to_regclass('public.detections')").fetchone()[0]:
            return
        connection.execute("""CREATE SCHEMA auth;
            CREATE TABLE auth.users (id uuid PRIMARY KEY, email text, raw_user_meta_data jsonb DEFAULT '{}');
            CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
            SELECT (nullif(current_setting('request.jwt.claims', true), '')::jsonb->>'sub')::uuid $$;
            GRANT USAGE ON SCHEMA auth, public TO authenticated, anon, service_role;
            GRANT EXECUTE ON FUNCTION auth.uid() TO authenticated, anon, service_role;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO authenticated, service_role;
            ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anon;""")
        for migration in sorted((Path(__file__).resolve().parents[1] / "supabase" / "migrations").glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    prepare()
    print("Local test database and migrations ready on loopback port 55434.")
