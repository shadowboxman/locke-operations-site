-- =====================================================================
-- Read-only snapshot of HubSpot contacts (admin fallback cache)
-- =====================================================================
-- Migration: 20260720120000_add_hubspot_contacts_snapshot
--
-- HubSpot is the ONLY source of truth for contacts (decision 2026-07-20;
-- see hubspot_crm.py). This table is a nightly snapshot used exclusively
-- as a read fallback when the HubSpot API is unreachable. It is written
-- ONLY by the snapshot job (full delete + reinsert) and must never be
-- edited by hand or by any other code path. It is NOT a mirror, has no
-- sync-back, and its contents may be up to 24h stale by design.
-- =====================================================================

CREATE TABLE hubspot_contacts_snapshot (
  hubspot_id  text        PRIMARY KEY,
  properties  jsonb       NOT NULL,
  snapshot_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE hubspot_contacts_snapshot IS
  'Nightly read-only cache of HubSpot contacts. Fallback for the admin '
  'contacts view only. Written solely by the snapshot job; never edit.';

-- Locke-staff data via the service role only. RLS on, no policies:
-- the authenticated role can see nothing here.
ALTER TABLE hubspot_contacts_snapshot ENABLE ROW LEVEL SECURITY;
