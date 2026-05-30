-- =====================================================================
-- Per-user request-notification preference
-- =====================================================================
-- Migration: 20260530180000_add_notify_requests
--
-- Notification preferences are per-user (not per-org): each user decides
-- whether they get request emails. Default true (opt-out):
--   - Locke staff with it on are emailed when a client submits a request.
--   - Clients with it on are emailed when their request's status changes.
-- The org `features.requests` flag still controls whether Requests exists at
-- all; this is the per-recipient on/off on top of that.
--
-- Load-bearing once deployed: /api/me and the preference endpoint read this
-- column, so apply before the code deploys.
-- =====================================================================

ALTER TABLE users
  ADD COLUMN notify_requests boolean NOT NULL DEFAULT true;
