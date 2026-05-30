-- =====================================================================
-- Document internal-visibility (Locke build records)
-- =====================================================================
-- Migration: 20260530140000_add_document_visibility
--
-- Adds a visibility axis to documents so Locke can store internal build
-- records (stack, architecture, references) per client org that the CLIENT
-- never sees. Credentials do NOT belong here; they live in a secrets manager
-- and these docs only reference their location.
--
-- - documents.visibility: 'client' (default, client-facing) | 'internal'
--   (Locke staff only).
-- - new 'implementation' document_category for these records.
-- - documents_select RLS updated: clients see only client-visible docs;
--   Locke staff see everything (client + internal). This is the security
--   boundary — without it, internal docs in a client's org would be visible
--   to that client, since current_user_can_see_org() is true for their org.
--
-- NOTE: ALTER TYPE ... ADD VALUE is fine on PG12+ as long as the new value
-- isn't USED in the same transaction (it isn't here). If your migration
-- runner objects, run the ADD VALUE line on its own first.
-- =====================================================================

ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'implementation';

ALTER TABLE documents
  ADD COLUMN visibility text NOT NULL DEFAULT 'client'
  CHECK (visibility IN ('client', 'internal'));

-- Clients see only client-visible, non-deleted docs in orgs they can see.
-- Locke staff (admin or staff) see all docs, including internal.
DROP POLICY IF EXISTS documents_select ON documents;
CREATE POLICY documents_select ON documents
  FOR SELECT TO authenticated
  USING (
    current_user_can_see_org(org_id)
    AND deleted_at IS NULL
    AND (visibility = 'client' OR current_user_is_locke_staff())
  );

CREATE INDEX documents_org_visibility_idx
  ON documents (org_id, visibility) WHERE deleted_at IS NULL;
