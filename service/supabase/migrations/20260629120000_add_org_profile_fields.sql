-- =====================================================================
-- Add company-profile fields to organizations
-- =====================================================================
-- Migration: 20260629120000_add_org_profile_fields
--
-- Until now `organizations` carried only name/slug/status. Two needs:
--   1. The NDA (and later MSA/SOW) merge-fields need contract-grade legal
--      details for the counterparty: legal name, entity type, state of
--      organization, and principal address. These feed the SignWell
--      `template_fields` at send time (see SIGNWELL_SETUP_STEPS.md). The
--      portal Postgres is the authoritative source for these; HubSpot may
--      seed them at onboarding but is not in the signing path.
--   2. A light company profile (phone, email, website, industry) so we
--      hold a bit more context on each client than just a display name.
--
-- All columns are nullable and additive, so this is safe on existing rows
-- (they become NULL / the country default). Reversible by dropping the
-- columns. No RLS change: org writes go through the service role
-- (admin_conn) and reads are already org-scoped.
-- =====================================================================

-- --- Legal / contract details (map to NDA placeholders) --------------
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS legal_name      text;  -- full legal entity name, vs. display `name`  -> [COUNTERPARTY NAME]
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS entity_type     text;  -- e.g. "a New York limited liability company" -> [ENTITY TYPE]
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS formation_state text;  -- US state the entity is organized under        -> [STATE]

-- --- Principal address (composed into the NDA [ADDRESS] string) -------
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS address_line1   text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS address_line2   text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS city            text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS state           text;  -- mailing-address state/region (distinct from formation_state)
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS postal_code     text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS country         text NOT NULL DEFAULT 'USA';

-- --- Light company profile -------------------------------------------
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS phone           text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS email           text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS website         text;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS industry        text;  -- free-form for now; could later constrain to Locke's verticals

-- --- Column documentation --------------------------------------------
COMMENT ON COLUMN organizations.legal_name      IS 'Full legal entity name used on contracts; falls back to name if null.';
COMMENT ON COLUMN organizations.entity_type     IS 'Legal entity descriptor, e.g. "a New York limited liability company".';
COMMENT ON COLUMN organizations.formation_state IS 'US state the entity is organized under (NDA "organized under the laws of").';
COMMENT ON COLUMN organizations.state           IS 'Mailing-address state/region; distinct from formation_state.';
COMMENT ON COLUMN organizations.industry        IS 'Free-form industry/sector label for context and filtering.';
