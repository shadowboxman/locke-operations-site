-- =====================================================================
-- Phase 5: Requests (issues + feature requests) + per-org feature flag
-- =====================================================================
-- Migration: 20260530120000_add_requests_and_features
--
-- Adds:
--   - organizations.features (jsonb) for per-org feature flags. First flag:
--     {"requests": true} gates the Requests surface for that client org.
--   - requests table: one pipeline for both issues and feature requests,
--     distinguished by `kind`. RLS mirrors documents (visible if you can see
--     the org; Locke staff see all). Writes go through the service role
--     (admin_conn) with app-level authz, same as documents.
--
-- Status vocabulary is kind-aware but stored in one enum:
--   issue:           open -> in_progress -> resolved / closed
--   feature_request: open -> under_consideration -> planned -> shipped / declined
-- =====================================================================

-- 1. Per-org feature flags. Existing orgs default to no flags ('{}').
ALTER TABLE organizations
  ADD COLUMN features jsonb NOT NULL DEFAULT '{}'::jsonb;

-- 2. Enums.
CREATE TYPE request_kind AS ENUM ('issue', 'feature_request');
CREATE TYPE request_status AS ENUM (
  'open', 'in_progress', 'resolved', 'closed',          -- issue lifecycle
  'under_consideration', 'planned', 'shipped', 'declined' -- feature_request lifecycle
);

-- 3. Requests table.
CREATE TABLE requests (
  id          uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid           NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kind        request_kind   NOT NULL,
  category    text,                       -- issue sub-type; NULL for feature_request
  subject     text           NOT NULL,
  body        text           NOT NULL,
  priority    text,                       -- low | normal | high (optional)
  status      request_status NOT NULL DEFAULT 'open',
  created_by  uuid           REFERENCES users(id) ON DELETE SET NULL,
  created_at  timestamptz    NOT NULL DEFAULT now(),
  updated_at  timestamptz    NOT NULL DEFAULT now()
);

CREATE TRIGGER requests_set_updated_at
  BEFORE UPDATE ON requests
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX requests_org_created_idx ON requests (org_id, created_at DESC);
CREATE INDEX requests_org_kind_idx    ON requests (org_id, kind);

-- 4. RLS: visible if the caller can see the org (Locke staff see all). Writes
--    happen via the service role, which bypasses RLS.
ALTER TABLE requests ENABLE ROW LEVEL SECURITY;

CREATE POLICY requests_select ON requests
  FOR SELECT TO authenticated
  USING (current_user_can_see_org(org_id));

-- 5. The blanket GRANT in the initial schema only covered tables that existed
--    then, so grant SELECT on this new table explicitly.
GRANT SELECT ON requests TO authenticated;
