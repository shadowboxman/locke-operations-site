-- =====================================================================
-- Profile photos
-- =====================================================================
-- Migration: 20260530200000_add_user_avatar
--
-- Stores the R2 object key for a user's profile photo. The image lives in R2
-- (private), served via short-lived signed URLs the API mints, same as
-- documents. Key convention: avatars/{user_id}/{uuid}.
--
-- Load-bearing once deployed: /api/me and the team endpoint read this column,
-- so apply before the code deploys.
-- =====================================================================

ALTER TABLE users
  ADD COLUMN avatar_key text;
