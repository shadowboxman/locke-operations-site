-- =====================================================================
-- Seed data for local dev and Phase 1 smoke testing
-- =====================================================================
-- Creates:
--   1. The Locke internal org and one Locke admin user
--   2. Two fake client orgs ("Acme Trades", "Bedrock Restoration")
--   3. One client_admin user in each
--
-- The clerk_user_id values use the format `user_seed_*` so they're easy to
-- identify as fake. Replace with real Clerk IDs once Clerk is wired up,
-- or run this seed against a fresh dev DB.
--
-- Apply: psql $DATABASE_URL -f supabase/seed.sql
--   or:  supabase db reset (which auto-loads seed.sql)
-- =====================================================================

BEGIN;

-- ---------- Organizations ----------
INSERT INTO organizations (id, name, slug, status, is_internal)
VALUES
  ('00000000-0000-0000-0000-000000000001', 'Locke Operations',     'locke',     'active', true),
  ('00000000-0000-0000-0000-000000000002', 'Acme Trades',          'acme',      'active', false),
  ('00000000-0000-0000-0000-000000000003', 'Bedrock Restoration',  'bedrock',   'active', false);

-- ---------- Users ----------
INSERT INTO users (id, clerk_user_id, email, name)
VALUES
  -- Locke staff
  ('10000000-0000-0000-0000-000000000001', 'user_seed_dan',   'dan@lockeoperations.com',   'Dan Lee'),
  -- Client primaries
  ('10000000-0000-0000-0000-000000000002', 'user_seed_acme',  'alice@acme-trades.test',    'Alice (Acme)'),
  ('10000000-0000-0000-0000-000000000003', 'user_seed_bed',   'bob@bedrock.test',          'Bob (Bedrock)');

-- ---------- Memberships ----------
INSERT INTO memberships (user_id, org_id, role, status, activated_at)
VALUES
  -- Dan is locke_admin in the internal org
  ('10000000-0000-0000-0000-000000000001',
   '00000000-0000-0000-0000-000000000001',
   'locke_admin', 'active', now()),
  -- Alice is client_admin at Acme
  ('10000000-0000-0000-0000-000000000002',
   '00000000-0000-0000-0000-000000000002',
   'client_admin', 'active', now()),
  -- Bob is client_admin at Bedrock
  ('10000000-0000-0000-0000-000000000003',
   '00000000-0000-0000-0000-000000000003',
   'client_admin', 'active', now());

-- ---------- An initial audit event so the table isn't empty ----------
INSERT INTO audit_events (actor_user_id, action, resource_type, outcome, metadata)
VALUES
  ('10000000-0000-0000-0000-000000000001',
   'system.seed_loaded',
   'system',
   'success',
   '{"note": "Phase 1 seed loaded", "orgs": 3, "users": 3}'::jsonb);

COMMIT;

-- ---------- Smoke check queries ----------
-- Run these manually in the Supabase SQL editor to verify the seed loaded
-- and RLS behaves as expected.
--
-- 1. Count check (as service_role, RLS bypassed):
--    SELECT
--      (SELECT count(*) FROM organizations) AS orgs,
--      (SELECT count(*) FROM users)         AS users,
--      (SELECT count(*) FROM memberships)   AS memberships;
--    -- Expected: 3, 3, 3
--
-- 2. RLS as Alice (client_admin at Acme) — should see only Acme + Locke (via the
--    user-shares-org rule) and her own row:
--    SET ROLE authenticated;
--    SET LOCAL app.current_user_id = '10000000-0000-0000-0000-000000000002';
--    SELECT name FROM organizations;   -- Expected: just Acme Trades
--    SELECT email FROM users;          -- Expected: just alice
--    RESET ROLE;
--
-- 3. RLS as Dan (locke_admin) — should see everything:
--    SET ROLE authenticated;
--    SET LOCAL app.current_user_id = '10000000-0000-0000-0000-000000000001';
--    SELECT name FROM organizations;   -- Expected: all 3
--    SELECT email FROM users;          -- Expected: all 3
--    RESET ROLE;
--
-- 4. Append-only audit:
--    DELETE FROM audit_events WHERE action = 'system.seed_loaded';
--    -- Expected: ERROR: audit_events is append-only: DELETE is not permitted
