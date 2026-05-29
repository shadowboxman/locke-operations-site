-- =====================================================================
-- Add documents.source to support client-uploaded files
-- =====================================================================
-- Migration: 20260529130000_add_document_source
--
-- Phase 2 shipped Locke-published documents only: every row was produced
-- by Locke and tagged with one of the four document_category enum values.
-- Clients now need to upload files TO Locke (system docs, templates, the
-- processes we automate). Those are a different kind of object and must
-- never be confused with a Locke deliverable.
--
-- Model:
--   source = 'locke'  -> a Locke deliverable; category is required and is
--                        one of the four document_category values.
--   source = 'client' -> a client-uploaded file; category is NULL (it does
--                        not belong in the deliverable taxonomy) and surfaces
--                        in the portal's separate "Shared with Locke" section.
--
-- Access control is unchanged at the DB layer: documents writes still go
-- through the service role (admin_conn), and the application enforces that
-- a client can only write into their own org and only soft-delete files
-- they uploaded themselves (source='client' AND uploaded_by = caller).
-- The existing documents_select RLS policy already scopes reads by org, so
-- client uploads are visible to the org and to Locke staff with no change.
-- =====================================================================

-- 1. Origin flag. Existing rows are all Locke deliverables.
ALTER TABLE documents
  ADD COLUMN source text NOT NULL DEFAULT 'locke'
  CHECK (source IN ('locke', 'client'));

-- 2. Category no longer applies to client uploads, so it must be nullable.
ALTER TABLE documents
  ALTER COLUMN category DROP NOT NULL;

-- 3. Integrity: a Locke deliverable must carry a category; a client upload
--    must not. This keeps the two kinds from drifting into each other.
ALTER TABLE documents
  ADD CONSTRAINT documents_source_category_ck
  CHECK (
    (source = 'locke'  AND category IS NOT NULL) OR
    (source = 'client' AND category IS NULL)
  );

-- 4. The portal and admin both filter by source; index it alongside org.
CREATE INDEX documents_org_source_idx
  ON documents (org_id, source)
  WHERE deleted_at IS NULL;
