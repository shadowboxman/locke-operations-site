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
# Admin-role guard
# ---------------------------------------------------------------
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ADMIN_API_KEY_USER_EMAIL = os.environ.get(
    "ADMIN_API_KEY_USER_EMAIL", "dan@lockeoperations.com",
)


async def require_locke_admin(
    authorization: Optional[str] = Header(None),
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
) -> dict:
    """FastAPI dependency: accepts either a Clerk Bearer JWT (browser/admin
    UI) or an X-Admin-Key header (Retool, scripts). Resolves to a users
    row and confirms locke_admin / locke_staff role. Raises 403 otherwise.

    Why two auth paths: Clerk session JWTs are short-lived and tied to a
    browser refresh flow that Retool can't run. A static admin API key is
    simpler for service-to-service admin tooling. Actions attribute to the
    user whose email matches ADMIN_API_KEY_USER_EMAIL.
    """
    from db import admin_conn  # local import to avoid circular

    user: Optional[dict] = None

    # Path 1: admin API key (Retool, scripts).
    if ADMIN_API_KEY and x_admin_key and x_admin_key == ADMIN_API_KEY:
        async with admin_conn() as conn:
            row = await conn.fetchrow(
                "SELECT id, clerk_user_id, email, name FROM users WHERE email = $1",
                ADMIN_API_KEY_USER_EMAIL,
            )
        if not row:
            raise HTTPException(
                status_code=500,
                detail=f"Admin API key configured but no user row for {ADMIN_API_KEY_USER_EMAIL}",
            )
        user = dict(row)

    # Path 2: Clerk Bearer JWT (browser).
    if user is None:
        user = await get_current_user(authorization=authorization)

    # Role check (applies regardless of auth path).
    async with admin_conn() as conn:
        has_admin = await conn.fetchval(
            """
            SELECT EXISTS (
              SELECT 1 FROM memberships
              WHERE user_id = $1
                AND role IN ('locke_admin', 'locke_staff')
                AND status = 'active'
            )
            """,
            user["id"],
        )
    if not has_admin:
        raise HTTPException(status_code=403, detail="Locke admin role required")
    return user


# ---------------------------------------------------------------
# Clerk REST API helpers (admin operations)
# ---------------------------------------------------------------
CLERK_API_BASE = "https://api.clerk.com/v1"


def _clerk_headers() -> dict[str, str]:
    if not CLERK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Clerk secret key not configured")
    return {
        "Authorization": f"Bearer {CLERK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


async def create_clerk_organization(
    name: str, slug: str, created_by_clerk_id: str,
) -> dict[str, Any]:
    """Create an organization in Clerk. Returns the Clerk org payload.

    Raises HTTPException(400) on validation failure (e.g., slug taken).
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CLERK_API_BASE}/organizations",
            headers=_clerk_headers(),
            json={"name": name, "slug": slug, "created_by": created_by_clerk_id},
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.create_org_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def list_clerk_organization_members(clerk_org_id: str) -> list[dict[str, Any]]:
    """List active members of a Clerk org."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/memberships",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.list_members_failed status=%d", resp.status_code)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    body = resp.json()
    # Clerk returns either a list or a paginated object depending on endpoint version.
    return body if isinstance(body, list) else body.get("data", [])


async def create_clerk_invitation(
    clerk_org_id: str, email: str, clerk_role: str = "org:member",
    redirect_url: Optional[str] = None,
) -> dict[str, Any]:
    """Send an invitation to join a Clerk organization.

    Clerk emails the invitee. On acceptance, user.created and
    organizationMembership.created webhooks fire.
    """
    payload: dict[str, Any] = {
        "email_address": email,
        "role": clerk_role,
    }
    if redirect_url:
        payload["redirect_url"] = redirect_url

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/invitations",
            headers=_clerk_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.invite_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def list_clerk_pending_invitations(clerk_org_id: str) -> list[dict[str, Any]]:
    """List pending (not yet accepted) invitations for a Clerk org."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/invitations",
            headers=_clerk_headers(),
            params={"status": "pending"},
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.list_invitations_failed status=%d", resp.status_code)
        return []
    body = resp.json()
    return body if isinstance(body, list) else body.get("data", [])


async def update_clerk_organization(
    clerk_org_id: str, name: Optional[str] = None, slug: Optional[str] = None,
) -> dict[str, Any]:
    """Update name and/or slug on a Clerk organization."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if slug is not None:
        payload["slug"] = slug
    if not payload:
        return {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}",
            headers=_clerk_headers(),
            json=payload,
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.update_org_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def update_clerk_membership_role(
    clerk_org_id: str, clerk_user_id: str, clerk_role: str,
) -> dict[str, Any]:
    """Update a user's role within a Clerk org. clerk_role = org:admin or org:member."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.patch(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/memberships/{clerk_user_id}",
            headers=_clerk_headers(),
            json={"role": clerk_role},
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.update_membership_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


async def delete_clerk_membership(clerk_org_id: str, clerk_user_id: str) -> None:
    """Remove a user from a Clerk org."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/memberships/{clerk_user_id}",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        # 404 is fine — membership already gone
        log.warning("clerk.api.delete_membership_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def delete_clerk_organization(clerk_org_id: str) -> None:
    """Permanently delete a Clerk organization.

    Used for hard-deleting client orgs that should disappear entirely (e.g.
    orphan orgs created by users with no membership). 404 is treated as
    success (already gone).

    Network/timeout errors are converted to HTTPException(502) so the
    request returns a clean error response with CORS headers instead of
    a raw 500 that the browser blocks as a CORS violation.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{CLERK_API_BASE}/organizations/{clerk_org_id}",
                headers=_clerk_headers(),
            )
    except httpx.TimeoutException as exc:
        log.warning("clerk.api.delete_organization_timeout clerk_org_id=%s", clerk_org_id)
        raise HTTPException(
            status_code=504,
            detail=f"Clerk org delete timed out: {exc}",
        ) from exc
    except httpx.HTTPError as exc:
        log.warning("clerk.api.delete_organization_network clerk_org_id=%s err=%s",
                    clerk_org_id, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Clerk org delete failed (network): {exc}",
        ) from exc

    if resp.status_code >= 400 and resp.status_code != 404:
        log.warning("clerk.api.delete_organization_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def lock_clerk_user(clerk_user_id: str) -> None:
    """Lock a Clerk user. Locked users cannot sign in until unlocked.

    Reversible suspend. Idempotent: re-locking an already-locked user is a no-op.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CLERK_API_BASE}/users/{clerk_user_id}/lock",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        log.warning("clerk.api.lock_user_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def unlock_clerk_user(clerk_user_id: str) -> None:
    """Unlock a previously-locked Clerk user."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CLERK_API_BASE}/users/{clerk_user_id}/unlock",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        log.warning("clerk.api.unlock_user_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def delete_clerk_user(clerk_user_id: str) -> None:
    """Permanently delete a Clerk user.

    This frees the email so the same address can be re-invited and creates a
    fresh Clerk identity. 404 is treated as success (already gone).
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{CLERK_API_BASE}/users/{clerk_user_id}",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400 and resp.status_code != 404:
        log.warning("clerk.api.delete_user_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


async def revoke_clerk_invitation(
    clerk_org_id: str, clerk_invitation_id: str,
) -> dict[str, Any]:
    """Revoke a pending Clerk invitation."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CLERK_API_BASE}/organizations/{clerk_org_id}/invitations/{clerk_invitation_id}/revoke",
            headers=_clerk_headers(),
        )
    if resp.status_code >= 400:
        log.warning("clerk.api.revoke_invitation_failed status=%d body=%s",
                    resp.status_code, resp.text)
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json() if resp.content else {}


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
        "organization.deleted": _on_org_deleted,
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


async def _on_org_deleted(data: dict[str, Any]) -> None:
    """Mirror Clerk org deletion into our DB.

    Fires both as a consequence of our own delete_org endpoint (which calls
    Clerk first) and when an admin deletes an org directly via Clerk dashboard.
    Idempotent: if our row is already gone, returns silently.

    Refuses to hard-delete if documents exist (would violate the RESTRICT FK).
    In that case the org is archived instead and a warning is logged so an
    admin can investigate. memberships are deleted explicitly because their
    FK to organizations is RESTRICT.
    """
    clerk_org_id = data["id"]
    async with admin_conn() as conn:
        org_row = await conn.fetchrow(
            "SELECT id, is_internal FROM organizations WHERE clerk_org_id = $1",
            clerk_org_id,
        )
        if not org_row:
            log.info("clerk.webhook.org.deleted no_local_row clerk_org_id=%s", clerk_org_id)
            return

        org_id = org_row["id"]
        if org_row["is_internal"]:
            log.warning("clerk.webhook.org.deleted refused_internal org_id=%s", org_id)
            return

        doc_count = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE org_id = $1 AND deleted_at IS NULL",
            org_id,
        )
        if doc_count:
            log.warning(
                "clerk.webhook.org.deleted has_documents archiving org_id=%s doc_count=%s",
                org_id, doc_count,
            )
            await conn.execute(
                "UPDATE organizations SET status = 'archived', archived_at = now(), "
                "updated_at = now() WHERE id = $1",
                org_id,
            )
            return

        await conn.execute("DELETE FROM memberships WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
        log.info("clerk.webhook.org.deleted org_id=%s clerk_org_id=%s", org_id, clerk_org_id)


async def _on_membership_created(data: dict[str, Any]) -> None:
    clerk_org_id = data["organization"]["id"]
    public_user_data = data.get("public_user_data") or {}
    clerk_user_id = public_user_data.get("user_id")
    clerk_role = data.get("role", "org:member")  # e.g. "org:admin" or "org:member"

    if not clerk_user_id:
        log.warning("clerk.webhook.membership.skipped no clerk_user_id in payload")
        return

    async with admin_conn() as conn:
        org_row = await conn.fetchrow(
            "SELECT id, is_internal FROM organizations WHERE clerk_org_id = $1",
            clerk_org_id,
        )
        if not org_row:
            log.warning(
                "clerk.webhook.membership.skipped missing org clerk_org_id=%s",
                clerk_org_id,
            )
            return

        user_id = await conn.fetchval(
            "SELECT id FROM users WHERE clerk_user_id = $1",
            clerk_user_id,
        )

        # Race-condition fix: Clerk fires user.created and
        # organizationMembership.created near-simultaneously when an
        # invitation is accepted. If membership arrives first, the user row
        # doesn't exist yet and we'd skip silently. Create the user inline
        # from the data already in this payload so the membership lands.
        if not user_id:
            email = (public_user_data.get("identifier")
                     or _primary_email(public_user_data)
                     or "")
            first = public_user_data.get("first_name") or ""
            last = public_user_data.get("last_name") or ""
            full_name = f"{first} {last}".strip() or None

            if not email:
                log.warning(
                    "clerk.webhook.membership.skipped no email for clerk_user_id=%s",
                    clerk_user_id,
                )
                return

            user_id = await conn.fetchval(
                "INSERT INTO users (clerk_user_id, email, name) "
                "VALUES ($1, $2, $3) "
                "ON CONFLICT (clerk_user_id) DO UPDATE SET updated_at = now() "
                "RETURNING id",
                clerk_user_id, email, full_name,
            )
            log.info(
                "clerk.webhook.user.created_inline clerk_id=%s email=%s "
                "(race fix in membership handler)",
                clerk_user_id, email,
            )

        # Map Clerk's 2-role model back to our 4-role model based on whether
        # this is an internal Locke org or a client org.
        is_internal = org_row["is_internal"]
        is_admin = clerk_role == "org:admin"
        if is_internal:
            locke_role = "locke_admin" if is_admin else "locke_staff"
        else:
            locke_role = "client_admin" if is_admin else "client_member"

        await conn.execute(
            "INSERT INTO memberships (user_id, org_id, role, status, activated_at) "
            "VALUES ($1, $2, $3, 'active', now()) "
            "ON CONFLICT (user_id, org_id) DO UPDATE "
            "SET status = 'active', role = EXCLUDED.role, "
            "    activated_at = COALESCE(memberships.activated_at, now()), "
            "    updated_at = now()",
            user_id, org_row["id"], locke_role,
        )
        log.info(
            "clerk.webhook.membership.created user=%s org=%s clerk_role=%s locke_role=%s",
            user_id, org_row["id"], clerk_role, locke_role,
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
