-- Postgres login roles + grants for the one-database consolidation
-- (plan: ~/altguard/docs/DB-CONSOLIDATION-PLAN.md).
--
-- Idempotent: run as the postgres superuser against peeposredemption, as often as
-- you like:   sudo -u postgres psql -d peeposredemption -f tools/pg_roles.sql
--
-- Passwords are NOT in this file. They live in /root/torvex-pg-roles.env (root, 0600)
-- and are set with ALTER ROLE … PASSWORD from there.
--
-- Grants are listed PER TABLE on purpose — no ALTER DEFAULT PRIVILEGES. This file
-- is the audit of who can touch what; every night that adds tables adds its lines.

-- ── logins ──────────────────────────────────────────────────────────────────────
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['torvex_web', 'torvex_bot', 'torvex_dash', 'torvex_gate'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', r);
    END IF;
  END LOOP;
END $$;

GRANT CONNECT ON DATABASE peeposredemption TO torvex_web, torvex_bot, torvex_dash, torvex_gate;

-- ── table grants ────────────────────────────────────────────────────────────────
-- None yet (Phase 0). Each store's night adds e.g.
--   GRANT SELECT, INSERT, UPDATE, DELETE ON config_guild_security TO torvex_bot, torvex_dash;
-- torvex_dash never gets archive_*; torvex_gate gets gate_* only; torvex_bot gets no gate_*.
