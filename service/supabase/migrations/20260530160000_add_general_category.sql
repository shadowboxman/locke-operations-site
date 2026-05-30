-- =====================================================================
-- Add a client-visible "General" document category
-- =====================================================================
-- Migration: 20260530160000_add_general_category
--
-- A generic, client-visible bucket for documents that don't fit the named
-- categories. Client-visible by default (documents.visibility defaults to
-- 'client'; only 'implementation' is forced internal in app code).
--
-- The 'monthly_review' category is being hidden in the UI for now (its model
-- is still undecided), but the enum value is left in place so any existing
-- rows remain valid; this is a UI-only hide, not a data change.
--
-- ALTER TYPE ... ADD VALUE is safe on PG12+ as long as the value isn't used
-- in the same transaction (it isn't). If the runner objects, run this line
-- on its own first.
-- =====================================================================

ALTER TYPE document_category ADD VALUE IF NOT EXISTS 'general';
