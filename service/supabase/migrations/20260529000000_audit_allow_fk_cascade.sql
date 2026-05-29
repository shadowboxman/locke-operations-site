-- =====================================================================
-- Allow FK cascade SET NULL on audit_events while keeping append-only
-- =====================================================================
-- Migration: 20260529000000_audit_allow_fk_cascade
--
-- The original schema added two FKs with `ON DELETE SET NULL`:
--   audit_events.org_id         REFERENCES organizations(id) ON DELETE SET NULL
--   audit_events.actor_user_id  REFERENCES users(id)         ON DELETE SET NULL
--
-- It also added an append-only trigger that blocks ALL UPDATEs on
-- audit_events. These two are mutually incompatible: when an org or
-- user is deleted, Postgres tries to UPDATE audit_events.<fk> to NULL
-- as the cascade, the trigger fires, and the originating DELETE is
-- rolled back with:
--
--     audit_events is append-only: UPDATE is not permitted
--
-- Net effect prior to this migration: orgs and users with any audit
-- history could not be hard-deleted. Surfaced by the delete-org endpoint.
--
-- Fix: replace the BEFORE UPDATE trigger with a row-level version that
-- inspects what changed and ONLY allows the FK-cascade shape (a non-NULL
-- value transitioning to NULL on org_id or actor_user_id, with every
-- other column unchanged). The DELETE trigger keeps blocking everything.
-- Content append-only semantics are preserved for action/metadata/etc.
-- =====================================================================

CREATE OR REPLACE FUNCTION prevent_audit_modification() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'audit_events is append-only: DELETE is not permitted';
  END IF;

  -- UPDATE: only allow the FK-cascade-SET-NULL shape on org_id or
  -- actor_user_id. Every content column must be unchanged.
  IF TG_OP = 'UPDATE' THEN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.action IS DISTINCT FROM OLD.action
       OR NEW.resource_type IS DISTINCT FROM OLD.resource_type
       OR NEW.resource_id IS DISTINCT FROM OLD.resource_id
       OR NEW.outcome IS DISTINCT FROM OLD.outcome
       OR NEW.metadata IS DISTINCT FROM OLD.metadata
       OR NEW.ip IS DISTINCT FROM OLD.ip
       OR NEW.user_agent IS DISTINCT FROM OLD.user_agent
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
      RAISE EXCEPTION 'audit_events is append-only: content UPDATE is not permitted';
    END IF;

    -- FK columns: only non-NULL -> NULL is allowed (cascade shape).
    -- Going from NULL to a value, or from one value to another, is blocked.
    IF NEW.org_id IS DISTINCT FROM OLD.org_id AND NEW.org_id IS NOT NULL THEN
      RAISE EXCEPTION 'audit_events.org_id may only transition to NULL';
    END IF;
    IF NEW.actor_user_id IS DISTINCT FROM OLD.actor_user_id AND NEW.actor_user_id IS NOT NULL THEN
      RAISE EXCEPTION 'audit_events.actor_user_id may only transition to NULL';
    END IF;

    RETURN NEW;
  END IF;

  -- Defensive default: refuse anything else.
  RAISE EXCEPTION 'audit_events: unexpected trigger operation %', TG_OP;
END;
$$ LANGUAGE plpgsql;

-- The UPDATE trigger must be row-level so OLD and NEW are populated.
-- The original was statement-level, which is why it couldn't inspect
-- column changes and had to blanket-block.
DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events;
CREATE TRIGGER audit_events_no_update
  BEFORE UPDATE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();

-- DELETE trigger stays as-is (statement-level, doesn't need OLD/NEW).
-- The function's DELETE branch still raises unconditionally.
