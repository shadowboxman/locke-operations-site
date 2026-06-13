-- =====================================================================
-- E-signature: provider-agnostic signature request tracking
-- =====================================================================
-- Migration: 20260613000000_add_signature_requests
--
-- Tracks an e-signature request (an NDA in v1) sent to a client through a
-- swappable provider (SignWell first). The provider's specifics live in the
-- app's esign/ adapter; this table is deliberately provider-neutral:
--   - provider + external_id identify the envelope at the provider and keep
--     historical rows valid across a provider switch.
--   - status uses a canonical vocabulary (CHECK, not a PG enum) so adding a
--     status later is a one-line change, not an ALTER TYPE.
--   - signers is jsonb ([{email,name,role,status}]).
--   - document_id links to the documents row created when the executed PDF is
--     filed into the org's Contracts (set once, on the 'completed' webhook).
--
-- Admin-only: all reads/writes go through the service role (admin_conn) with
-- app-level authz (require_locke_admin). RLS restricts any direct authenticated
-- access to Locke staff. Clients never query this table; they see the resulting
-- contract document in their Contracts section.
-- =====================================================================

CREATE TABLE signature_requests (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid        NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  provider      text        NOT NULL,                       -- e.g. 'signwell'
  external_id   text,                                       -- provider envelope/document id
  doc_type      text        NOT NULL CHECK (doc_type IN ('nda', 'msa', 'sow')),
  status        text        NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','sent','viewed','signed',
                                  'completed','declined','canceled','error')),
  signers       jsonb       NOT NULL DEFAULT '[]'::jsonb,    -- [{email,name,role,status}]
  template_ref  text,                                       -- provider template id used, if any
  document_id   uuid        REFERENCES documents(id) ON DELETE SET NULL,
  created_by    uuid        REFERENCES users(id) ON DELETE SET NULL,
  metadata      jsonb       NOT NULL DEFAULT '{}'::jsonb,    -- audit_url, provider raw refs
  created_at    timestamptz NOT NULL DEFAULT now(),
  sent_at       timestamptz,
  completed_at  timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, external_id)
);

CREATE TRIGGER signature_requests_set_updated_at
  BEFORE UPDATE ON signature_requests
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX signature_requests_org_created_idx ON signature_requests (org_id, created_at DESC);
-- Webhook lookups arrive keyed by the provider's id.
CREATE INDEX signature_requests_external_idx     ON signature_requests (provider, external_id);

-- RLS: Locke staff only. App endpoints use the service role (admin_conn), which
-- bypasses RLS; this policy is defense-in-depth against any direct authenticated
-- query, since clients have no business reading the signing pipeline directly.
ALTER TABLE signature_requests ENABLE ROW LEVEL SECURITY;

CREATE POLICY signature_requests_select ON signature_requests
  FOR SELECT TO authenticated
  USING (current_user_is_locke_staff());

GRANT SELECT ON signature_requests TO authenticated;
