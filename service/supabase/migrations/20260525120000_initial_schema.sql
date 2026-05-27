-- =====================================================================
-- Locke Operations Client Portal — Initial Schema
-- =====================================================================
-- Migration: 20260525120000_initial_schema
--
-- Establishes the multi-tenant data model for the client portal:
--   organizations, users, memberships, documents, invitations, audit_events
--
-- Includes:
--   - Enum types for roles, statuses, document categories
--   - Foreign keys with safe ON DELETE behavior (RESTRICT for orgs with data,
--     SET NULL for audit history)
--   - Updated_at triggers
--   - Indexes for the heavy query patterns
--   - Row-Level Security policies tied to a per-request session variable
--   - Append-only enforcement on audit_events
--
-- Role model:
--   Backend connects with service_role for admin/system operations (bypasses
--   RLS). For user-facing requests, backend issues:
--     SET LOCAL ROLE authenticated;
--     SET LOCAL app.current_user_id = '<users.id>';
--   This makes RLS the active authorization layer for normal traffic.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. Extensions
-- ---------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS citext;
-- gen_random_uuid() is built into Postgres 13+; no extension needed.


-- ---------------------------------------------------------------------
-- 2. Enum types
-- ---------------------------------------------------------------------

CREATE TYPE user_role AS ENUM (
  'locke_admin',
  'locke_staff',
  'client_admin',
  'client_member'
);

CREATE TYPE org_status AS ENUM (
  'active',
  'suspended',
  'archived'
);

CREATE TYPE membership_status AS ENUM (
  'invited',
  'active',
  'suspended',
  'removed'
);

CREATE TYPE document_category AS ENUM (
  'audit_report',
  'runbook',
  'monthly_review',
  'contract'
);


-- ---------------------------------------------------------------------
-- 3. Helper functions and triggers (table-independent)
-- ---------------------------------------------------------------------
-- Functions that reference the memberships table are defined in section 4.5,
-- after the tables exist, because LANGUAGE sql validates the body at create
-- time.

-- Auto-update updated_at on row changes
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Current user UUID from per-request session variable
-- Returns NULL if not set (and the second arg `true` suppresses the error)
CREATE OR REPLACE FUNCTION current_user_id() RETURNS uuid AS $$
  SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid;
$$ LANGUAGE sql STABLE;


-- ---------------------------------------------------------------------
-- 4. Tables
-- ---------------------------------------------------------------------

-- organizations: client companies, plus one "Locke" internal org for staff
CREATE TABLE organizations (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name        text        NOT NULL,
  slug        text        NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
  status      org_status  NOT NULL DEFAULT 'active',
  is_internal boolean     NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);
CREATE TRIGGER organizations_set_updated_at
  BEFORE UPDATE ON organizations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- users: one row per person, linked to Clerk by clerk_user_id
CREATE TABLE users (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  clerk_user_id text        NOT NULL UNIQUE,
  email         citext      NOT NULL UNIQUE,
  name          text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER users_set_updated_at
  BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- memberships: a user belongs to an org with a role
CREATE TABLE memberships (
  id            uuid              PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid              NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  org_id        uuid              NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
  role          user_role         NOT NULL,
  status        membership_status NOT NULL DEFAULT 'invited',
  invited_at    timestamptz       NOT NULL DEFAULT now(),
  activated_at  timestamptz,
  created_at    timestamptz       NOT NULL DEFAULT now(),
  updated_at    timestamptz       NOT NULL DEFAULT now(),
  UNIQUE (user_id, org_id)
);
CREATE TRIGGER memberships_set_updated_at
  BEFORE UPDATE ON memberships
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX memberships_user_active_idx
  ON memberships(user_id)
  WHERE status = 'active';

CREATE INDEX memberships_org_active_idx
  ON memberships(org_id)
  WHERE status = 'active';


-- documents: metadata for files stored in R2
CREATE TABLE documents (
  id           uuid              PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid              NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
  category     document_category NOT NULL,
  name         text              NOT NULL,
  storage_key  text              NOT NULL,   -- R2 object key: {org_id}/{doc_id}/{version}
  version      int               NOT NULL DEFAULT 1,
  size_bytes   bigint,
  content_type text,
  uploaded_by  uuid              REFERENCES users(id) ON DELETE SET NULL,
  uploaded_at  timestamptz       NOT NULL DEFAULT now(),
  deleted_at   timestamptz,
  created_at   timestamptz       NOT NULL DEFAULT now(),
  updated_at   timestamptz       NOT NULL DEFAULT now(),
  CONSTRAINT documents_storage_key_unique UNIQUE (storage_key)
);
CREATE TRIGGER documents_set_updated_at
  BEFORE UPDATE ON documents
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX documents_org_category_active_idx
  ON documents(org_id, category)
  WHERE deleted_at IS NULL;

CREATE INDEX documents_org_uploaded_idx
  ON documents(org_id, uploaded_at DESC)
  WHERE deleted_at IS NULL;


-- invitations: one-time signed tokens for onboarding new users to an org
CREATE TABLE invitations (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email       citext      NOT NULL,
  role        user_role   NOT NULL,
  token       text        NOT NULL UNIQUE,
  invited_by  uuid        REFERENCES users(id) ON DELETE SET NULL,
  expires_at  timestamptz NOT NULL,
  accepted_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX invitations_org_idx ON invitations(org_id);
CREATE INDEX invitations_pending_token_idx
  ON invitations(token)
  WHERE accepted_at IS NULL;


-- audit_events: append-only log of every action, success or denied
CREATE TABLE audit_events (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid        REFERENCES organizations(id) ON DELETE SET NULL,
  actor_user_id  uuid        REFERENCES users(id) ON DELETE SET NULL,
  action         text        NOT NULL,                    -- 'org.created', 'document.downloaded', 'auth.denied'
  resource_type  text,                                     -- 'organization', 'user', 'document', 'membership'
  resource_id    uuid,
  outcome        text        NOT NULL DEFAULT 'success',   -- 'success', 'denied', 'error'
  metadata       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  ip             inet,
  user_agent     text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_events_org_time_idx
  ON audit_events(org_id, created_at DESC);

CREATE INDEX audit_events_actor_time_idx
  ON audit_events(actor_user_id, created_at DESC);

CREATE INDEX audit_events_resource_idx
  ON audit_events(resource_type, resource_id);

CREATE INDEX audit_events_action_time_idx
  ON audit_events(action, created_at DESC);


-- Append-only enforcement on audit_events
CREATE OR REPLACE FUNCTION prevent_audit_modification() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update
  BEFORE UPDATE ON audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION prevent_audit_modification();

CREATE TRIGGER audit_events_no_delete
  BEFORE DELETE ON audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION prevent_audit_modification();


-- ---------------------------------------------------------------------
-- 4.5 Helper functions that reference tables
-- ---------------------------------------------------------------------
-- Defined here (not in section 3) because LANGUAGE sql validates the body
-- at create time and these reference the memberships table.

-- Is the current user Locke internal staff (admin or staff role anywhere)?
CREATE OR REPLACE FUNCTION current_user_is_locke_staff() RETURNS boolean AS $$
  SELECT EXISTS (
    SELECT 1 FROM memberships
    WHERE user_id = current_user_id()
      AND role IN ('locke_admin', 'locke_staff')
      AND status = 'active'
  );
$$ LANGUAGE sql STABLE SECURITY DEFINER;

-- Can the current user see this org? (Locke staff see all; clients see their own)
CREATE OR REPLACE FUNCTION current_user_can_see_org(org uuid) RETURNS boolean AS $$
  SELECT current_user_is_locke_staff() OR EXISTS (
    SELECT 1 FROM memberships
    WHERE user_id = current_user_id()
      AND org_id = org
      AND status = 'active'
  );
$$ LANGUAGE sql STABLE SECURITY DEFINER;


-- ---------------------------------------------------------------------
-- 5. Row-Level Security
-- ---------------------------------------------------------------------
-- Policies cover SELECT for the `authenticated` role. INSERT/UPDATE/DELETE
-- are not granted to `authenticated` in Phase 1 — those go through the
-- backend running as service_role with app-code authorization.
-- Phase 2+ may grant writes to `authenticated` with corresponding policies.

ALTER TABLE organizations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE users          ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships    ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents      ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events   ENABLE ROW LEVEL SECURITY;


-- organizations: visible if Locke staff or member
CREATE POLICY organizations_select ON organizations
  FOR SELECT TO authenticated
  USING (current_user_can_see_org(id));

-- users: visible to self, to Locke staff, or to anyone sharing an org membership
CREATE POLICY users_select ON users
  FOR SELECT TO authenticated
  USING (
    id = current_user_id()
    OR current_user_is_locke_staff()
    OR EXISTS (
      SELECT 1
      FROM memberships m1
      JOIN memberships m2 ON m1.org_id = m2.org_id
      WHERE m1.user_id = current_user_id()
        AND m2.user_id = users.id
        AND m1.status = 'active'
        AND m2.status = 'active'
    )
  );

-- memberships: visible if you can see the org
CREATE POLICY memberships_select ON memberships
  FOR SELECT TO authenticated
  USING (current_user_can_see_org(org_id));

-- documents: visible if you can see the org and it isn't soft-deleted
CREATE POLICY documents_select ON documents
  FOR SELECT TO authenticated
  USING (
    current_user_can_see_org(org_id)
    AND deleted_at IS NULL
  );

-- invitations: visible to Locke staff or client_admin of the org
CREATE POLICY invitations_select ON invitations
  FOR SELECT TO authenticated
  USING (
    current_user_is_locke_staff()
    OR EXISTS (
      SELECT 1 FROM memberships
      WHERE user_id = current_user_id()
        AND org_id = invitations.org_id
        AND role = 'client_admin'
        AND status = 'active'
    )
  );

-- audit_events: visible to Locke staff (everywhere) or client_admin (own org only)
CREATE POLICY audit_events_select ON audit_events
  FOR SELECT TO authenticated
  USING (
    current_user_is_locke_staff()
    OR (org_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM memberships
      WHERE user_id = current_user_id()
        AND org_id = audit_events.org_id
        AND role = 'client_admin'
        AND status = 'active'
    ))
  );


-- ---------------------------------------------------------------------
-- 6. Grants for the `authenticated` role
-- ---------------------------------------------------------------------
-- Read-only access. Writes go through service_role from the backend.

GRANT USAGE ON SCHEMA public TO authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT EXECUTE ON FUNCTION current_user_id, current_user_is_locke_staff,
  current_user_can_see_org TO authenticated;

-- Also grant to anon for the case where Phase 2 adds an anonymous-token flow
-- (e.g. accepting an invitation). For now anon has no policies so it sees nothing.
GRANT USAGE ON SCHEMA public TO anon;
