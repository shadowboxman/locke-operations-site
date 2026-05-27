"""Clerk authentication and webhook helpers.

Three responsibilities:
  1. Verify Clerk session JWTs against the Clerk JWKS (no network call per
     request once the keys are cached).
  2. Resolve the signed-in Clerk user to a row in our users table, with a
     bootstrap path that claims an existing seeded row by email on first
     sign-in.
  3. Verify Clerk webhook signatures via svix.

This module talks to Clerk's REST API only for the bootstrap email lookup.
Everything else is local crypto.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient
from svix.webhooks import Webhook, WebhookVerificationError

from db import admin_conn

log = logging.getLogger("locke.clerk")

CLERK_FRONTEND_API_URL = os.environ.get("CLERK_FRONTEND_API_URL", "").rstrip("/")
CLERK_SECRET_KEY = os.environ.get("CLERK_SECRET_KEY", "")
CLERK_WEBHOOK_SECRET = os.environ.get("CLERK_WEBHOOK_SECRET", "")

if not CLERK_FRONTEND_API_URL:
    log.warning("clerk.config.missing CLERK_FRONTEND_API_URL is not set")
    _jwks_client: Optional[PyJWKClient] = None
else:
    _jwks_client = PyJWKClient(
        f"{CLERK_FRONTEND_API_URL}/.well-known/jwks.json",
        cache_keys=True,
        lifespan=600,  # cache keys for 10 minutes
    )


# ---------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------
def verify_jwt(token: str) -> dict[str, Any]:
    """Validate a Clerk session JWT and return the decoded claims dict."""
    if _jwks_client is None:
        raise HTTPException(status_code=500, detail="Clerk not configured")

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={
                # Clerk doesn't set `aud` on session tokens by default.
                # Verify issuer instead.
                "verify_aud": False,
            },
            issuer=CLERK_FRONTEND_API_URL,
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid token issuer")
    except jwt.InvalidTokenError as exc:
        log.warning("clerk.jwt.invalid err=%s", exc)
        raise HTTPException(status_code=401, detail="Invalid token")
    return claims


# ---------------------------------------------------------------
# FastAPI dependency: get the current signed-in user
# ---------------------------------------------------------------
async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Resolve the Authorization bearer token to a users row.

    Returns the users row as a dict. Raises 401 if no token, invalid token,
    or no matching user in our DB (after the bootstrap claim path).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    claims = verify_jwt(token)
    clerk_user_id = claims.get("sub")
    if not clerk_user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    async with admin_conn() as conn:
        # Fast path: existing match by clerk_user_id
        row = await conn.fetchrow(
            "SELECT id, clerk_user_id, email, name "
            "FROM users WHERE clerk_user_id = $1",
            clerk_user_id,
        )
        if row:
            return dict(row)

        # Bootstrap path: a seeded row with this email exists. Claim it by
        # updating its clerk_user_id to the real one. This handles the
        # Phase 1 case where Dan signs up via Clerk after seed data was loaded.
        email = await _fetch_clerk_user_email(clerk_user_id)
        if email:
            row = await conn.fetchrow(
                "UPDATE users "
                "SET clerk_user_id = $1, updated_at = now() "
                "WHERE email = $2 AND clerk_user_id LIKE 'user_seed_%' "
                "RETURNING id, clerk_user_id, email, name",
                clerk_user_id, email,
            )
            if row:
                log.info(
                    "clerk.user.claimed_seed email=%s clerk_id=%s",
                    email, clerk_user_id,
                )
                return dict(row)

    log.warning("clerk.user.not_in_db clerk_id=%s", clerk_user_id)
    raise HTTPException(
        status_code=401,
        detail="Your account is not provisioned. Contact hello@lockeoperations.com.",
    )


async def _fetch_clerk_user_email(clerk_user_id: str) -> Optional[str]:
    """Look up a user's primary email from Clerk's REST API."""
    if not CLERK_SECRET_KEY:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}"},
        )
    if resp.status_code != 200:
        log.warning(
            "clerk.api.user_fetch_failed clerk_id=%s status=%d",
            clerk_user_id, resp.status_code,
        )
        return None
    data = resp.json()
    primary_id = data.get("primary_email_address_id")
    for addr in data.get("email_addresses", []):
        if addr.get("id") == primary_id:
            return addr.get("email_address")
    addrs = data.get("email_addresses", [])
    return addrs[0].get("email_address") if addrs else None


# ---------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------
def verify_webhook(headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """Verify a Clerk webhook payload using svix. Returns the parsed event."""
    if not CLERK_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")

    wh = Webhook(CLERK_WEBHOOK_SECRET)
    try:
        return wh.verify(body, headers)
    except WebhookVerificationError as exc:
        log.warning("clerk.webhook.bad_signature err=%s", exc)
        raise HTTPException(status_code=400, detail="Invalid webhook signature")


# ---------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------
async def handle_webhook_event(event: dict[str, Any]) -> None:
    """Dispatch a verified Clerk webhook to the right handler.

    Only the events we care about for Phase 1 are handled; others are logged
    and ignored.
    """
    event_type = event.get("type")
    data = event.get("data", {})
    log.info("clerk.webhook.received type=%s id=%s", event_type, data.get("id"))

    handlers = {
        "user.created": _on_user_created,
        "user.updated": _on_user_updated,
        "organization.created": _on_org_created,
        "organization.updated": _on_org_updated,
        "organizationMembership.created": _on_membership_created,
        "organizationMembership.deleted": _on_membership_deleted,
    }
    handler = handlers.get(event_type)
    if handler is None:
        log.info("clerk.webhook.ignored type=%s", event_type)
        return
    await handler(data)


async def _on_user_created(data: dict[str, Any]) -> None:
    clerk_user_id = data["id"]
    email = _primary_email(data)
    name = _full_name(data)
    async with admin_conn() as conn:
        # Try to claim a seeded row first (same logic as get_current_user)
        claimed = await conn.fetchval(
            "UPDATE users SET clerk_user_id = $1, name = COALESCE($3, name), updated_at = now() "
            "WHERE email = $2 AND clerk_user_id LIKE 'user_seed_%' RETURNING id",
            clerk_user_id, email, name,
        )
        if claimed:
            log.info("clerk.webhook.user.claimed_seed email=%s", email)
            return
        # Otherwise insert a fresh row
        await conn.execute(
            "INSERT INTO users (clerk_user_id, email, name) VALUES ($1, $2, $3) "
            "ON CONFLICT (clerk_user_id) DO NOTHING",
            clerk_user_id, email, name,
        )
        log.info("clerk.webhook.user.created clerk_id=%s email=%s", clerk_user_id, email)


async def _on_user_updated(data: dict[str, Any]) -> None:
    clerk_user_id = data["id"]
    email = _primary_email(data)
    name = _full_name(data)
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE users SET email = $2, name = $3, updated_at = now() "
            "WHERE clerk_user_id = $1",
            clerk_user_id, email, name,
        )


async def _on_org_created(data: dict[str, Any]) -> None:
    clerk_org_id = data["id"]
    name = data.get("name", "Unnamed")
    slug = data.get("slug") or clerk_org_id.lower()
    async with admin_conn() as conn:
        await conn.execute(
            "INSERT INTO organizations (clerk_org_id, name, slug, status) "
            "VALUES ($1, $2, $3, 'active') "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            clerk_org_id, name, slug,
        )
        log.info("clerk.webhook.org.created clerk_org_id=%s slug=%s", clerk_org_id, slug)


async def _on_org_updated(data: dict[str, Any]) -> None:
    clerk_org_id = data["id"]
    name = data.get("name")
    slug = data.get("slug")
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE organizations SET name = COALESCE($2, name), "
            "slug = COALESCE($3, slug), updated_at = now() "
            "WHERE clerk_org_id = $1",
            clerk_org_id, name, slug,
        )


async def _on_membership_created(data: dict[str, Any]) -> None:
    clerk_org_id = data["organization"]["id"]
    clerk_user_id = data["public_user_data"]["user_id"]

    # Default Locke role: client_member. The admin invitation flow will
    # eventually set this from the invitations table; for Phase 1 manual
    # adjustments are fine.
    locke_role = "client_member"

    async with admin_conn() as conn:
        org_id = await conn.fetchval(
            "SELECT id FROM organizations WHERE clerk_org_id = $1",
            clerk_org_id,
        )
        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE clerk_user_id = $1",
            clerk_user_id,
        )
        if not org_id or not user_id:
            log.warning(
                "clerk.webhook.membership.skipped missing org=%s user=%s",
                org_id, user_id,
            )
            return
        await conn.execute(
            "INSERT INTO memberships (user_id, org_id, role, status, activated_at) "
            "VALUES ($1, $2, $3, 'active', now()) "
            "ON CONFLICT (user_id, org_id) DO UPDATE "
            "SET status = 'active', activated_at = COALESCE(memberships.activated_at, now()), "
            "    updated_at = now()",
            user_id, org_id, locke_role,
        )
        log.info(
            "clerk.webhook.membership.created user=%s org=%s",
            user_id, org_id,
        )


async def _on_membership_deleted(data: dict[str, Any]) -> None:
    clerk_org_id = data["organization"]["id"]
    clerk_user_id = data["public_user_data"]["user_id"]
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE memberships SET status = 'removed', updated_at = now() "
            "WHERE user_id = (SELECT id FROM users WHERE clerk_user_id = $1) "
            "  AND org_id = (SELECT id FROM organizations WHERE clerk_org_id = $2)",
            clerk_user_id, clerk_org_id,
        )


def _primary_email(data: dict[str, Any]) -> str:
    primary_id = data.get("primary_email_address_id")
    for addr in data.get("email_addresses", []):
        if addr.get("id") == primary_id:
            return addr.get("email_address", "")
    addrs = data.get("email_addresses", [])
    return addrs[0].get("email_address", "") if addrs else ""


def _full_name(data: dict[str, Any]) -> Optional[str]:
    first = data.get("first_name") or ""
    last = data.get("last_name") or ""
    full = f"{first} {last}".strip()
    return full or None
