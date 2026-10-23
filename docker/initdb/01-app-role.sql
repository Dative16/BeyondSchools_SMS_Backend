-- Row-level security is bypassed by superusers and table owners.
-- The application connects as this NON-superuser, NON-owner role so the
-- policies in the migrations actually bite.
CREATE ROLE bs_app LOGIN PASSWORD 'bs_app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE bs_sms TO bs_app;
GRANT USAGE ON SCHEMA public TO bs_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bs_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bs_app;
