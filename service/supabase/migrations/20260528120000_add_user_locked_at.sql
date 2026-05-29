-- =====================================================================
-- Add users.locked_at for the suspend/unsuspend admin flow
-- =====================================================================
-- Migration: 20260528120000_add_user_locked_at
--
-- Source of truth for "is this user allowed to sign in" is Clerk
-- (users.locked is set via Clerk's lock-user API). We cache the lock
-- state here so the admin member list can render status without an
-- extra Clerk round-trip per row. Cleared on unsuspend, set on suspend.
--
-- Hard delete still uses the existing FK cascade behavior
-- (memberships ON DELETE CASCADE; documents/audit_events/invitations
-- attribution columns ON DELETE SET NULL) so attribution gracefully
-- nulls out when a user row is removed.
-- =====================================================================

ALTER TABLE users
  ADD COLUMN locked_at timestamptz;

COMMENT ON COLUMN users.locked_at IS
  'When non-null, the user is suspended (Clerk-locked) and cannot sign in. '
  'Cleared by the unsuspend action. Hard delete removes the row entirely.';
