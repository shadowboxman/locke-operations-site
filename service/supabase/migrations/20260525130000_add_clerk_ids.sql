-- =====================================================================
-- Add Clerk linkage columns to organizations and invitations
-- =====================================================================
-- Migration: 20260525130000_add_clerk_ids
--
-- The initial schema added clerk_user_id to users. Organizations and
-- invitations also need to round-trip to Clerk, so add the corresponding
-- columns now (before any production data exists).
-- =====================================================================

ALTER TABLE organizations
  ADD COLUMN clerk_org_id text UNIQUE;

COMMENT ON COLUMN organizations.clerk_org_id IS
  'Clerk organization ID (org_xxx). Set when the org is mirrored into Clerk.';

ALTER TABLE invitations
  ADD COLUMN clerk_invitation_id text UNIQUE;

COMMENT ON COLUMN invitations.clerk_invitation_id IS
  'Clerk invitation ID (inv_xxx). Set when the invitation is mirrored into Clerk.';

-- Optional but useful: index on clerk_org_id for webhook handlers
CREATE INDEX organizations_clerk_org_idx ON organizations(clerk_org_id)
  WHERE clerk_org_id IS NOT NULL;
