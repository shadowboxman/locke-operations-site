"""FastAPI entrypoint for the Locke Operations assessment pipeline.

POST /api/submit  - receives assessment form payload; writes to HubSpot
                    synchronously, queues PDF gen + email as a background task.
GET  /api/health  - liveness probe for Railway.

The split (sync HubSpot, async PDF+email) is intentional. If the background
task ever crashes or times out, the lead is still captured in HubSpot and the
manual playbook 06 workflow remains a working fallback.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from html import escape as _h
from typing import Any, Literal, Optional

import asyncpg

from dotenv import load_dotenv

load_dotenv()  # pull .env in local dev; no-op on Railway where env vars are injected

# Configure logging BEFORE any other module imports so import-time log lines
# (notably the masked Resend key preview in email_client.py) actually surface.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("locke.submit")

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field, field_validator

import hubspot_client
import r2
from clerk import (
    create_clerk_invitation,
    create_clerk_organization,
    delete_clerk_membership,
    delete_clerk_organization,
    delete_clerk_user,
    get_current_user,
    handle_webhook_event,
    list_clerk_organization_members,
    list_clerk_pending_invitations,
    lock_clerk_user,
    require_locke_admin,
    revoke_clerk_invitation,
    unlock_clerk_user,
    update_clerk_membership_role,
    update_clerk_organization,
    verify_webhook,
)
from db import admin_conn, close_pool, init_pool, user_conn
from email_client import send_assessment_email, send_email
from pdf_generator import calculate, fmt_money, generate_pdf, pdf_filename

# ---------------------------------------------------------------
# CORS — allowlist driven by env, comma-separated.
# Defaults cover Vercel previews + the production domain.
# ---------------------------------------------------------------
_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://www.lockeoperations.com,https://lockeoperations.com,https://portal.lockeoperations.com",
).split(",")
ALLOWED_ORIGINS = [o.strip() for o in _origins if o.strip()]
# Allow any *.vercel.app preview URL for the locke-operations-site project.
# Regex matches the SHA-style preview subdomains Vercel issues.
ALLOWED_ORIGIN_REGEX = os.environ.get(
    "ALLOWED_ORIGIN_REGEX",
    r"^https://locke-operations-site(-[a-z0-9-]+)?\.vercel\.app$",
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Portal endpoints need a Postgres pool. Assessment-only deployments
    # can run without DATABASE_URL; init_pool will raise loudly if missing.
    if os.environ.get("DATABASE_URL"):
        await init_pool()
    else:
        log.warning("startup.no_database_url portal endpoints will 500")
    yield
    await close_pool()


app = FastAPI(
    title="Locke Operations Service",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
    max_age=86400,
)

# ---------------------------------------------------------------
# Request model — matches sample-lead.json plus a few server-trusted fields.
# Pydantic gives us free input validation and clear 422 errors.
# ---------------------------------------------------------------
class Contact(BaseModel):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    company: str = Field(min_length=1, max_length=120)
    email: EmailStr


class Answers(BaseModel):
    industry: Literal["trades", "restoration", "hospitality", "ae", "other"]
    team: int = Field(ge=0, le=100000)
    tasks: list[str] = Field(default_factory=list, max_length=50)
    hours: int = Field(ge=0, le=400)
    rate: int = Field(ge=0, le=1000)
    response: Literal["fast", "hour", "day", "slow"]
    leads: int = Field(ge=0, le=100000)
    value: int = Field(ge=0, le=10_000_000)
    maturity: int = Field(ge=0, le=100)
    consistency: int = Field(ge=0, le=100)

    @field_validator("tasks")
    @classmethod
    def _strip_blanks(cls, v: list[str]) -> list[str]:
        return [t.strip() for t in v if t and t.strip()]


class SubmitPayload(BaseModel):
    contact: Contact
    answers: Answers
    page_uri: str | None = None  # browser-supplied; we'll pass it to HubSpot

# ---------------------------------------------------------------
# Health
# ---------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "locke-api", "version": app.version}


# ---------------------------------------------------------------
# Background worker — runs after the HTTP response is sent.
# Failures here log loudly but do NOT affect the user-facing 200.
# ---------------------------------------------------------------
async def _generate_and_send(lead: dict, submission_id: str) -> None:
    try:
        log.info("bg.pdf.start submission_id=%s email=%s", submission_id, lead["contact"]["email"])
        pdf_bytes = generate_pdf(lead)
        log.info("bg.pdf.ok submission_id=%s bytes=%d", submission_id, len(pdf_bytes))

        result = calculate(lead["answers"])
        msg_id = await send_assessment_email(
            to_email=lead["contact"]["email"],
            first_name=lead["contact"]["first_name"],
            midpoint_formatted=fmt_money(result["total"]),
            pdf_bytes=pdf_bytes,
            pdf_filename=pdf_filename(lead),
            idempotency_key=submission_id,
        )
        log.info("bg.send.ok submission_id=%s resend_id=%s", submission_id, msg_id)
    except Exception as exc:
        # Never crash the background runner without a useful log line.
        log.exception("bg.failed submission_id=%s err=%s", submission_id, exc)


# ---------------------------------------------------------------
# Submit endpoint
# ---------------------------------------------------------------
@app.post("/api/submit")
async def submit(payload: SubmitPayload, background: BackgroundTasks, request: Request):
    submission_id = str(uuid.uuid4())
    contact = payload.contact.model_dump()
    answers = payload.answers.model_dump()

    # Compute the scored result once — used for HubSpot fields AND the email body.
    result = calculate(answers)

    # Step 1 — synchronous HubSpot write. If this fails, the user sees an error
    # and we never claim "check your inbox" for a lead that wasn't captured.
    try:
        await hubspot_client.submit(
            contact=contact,
            answers=answers,
            result=result,
            page_uri=payload.page_uri or str(request.headers.get("referer", "")) or None,
        )
    except Exception as exc:
        log.exception("submit.hubspot_failed submission_id=%s err=%s", submission_id, exc)
        # 502 — upstream (HubSpot) failure. The browser surfaces a retry hint.
        raise HTTPException(
            status_code=502,
            detail="We couldn't save your submission. Please try again, or email hello@lockeoperations.com.",
        )

    # Step 2 — queue PDF + email. Returns immediately; runs after response sent.
    lead = {"contact": contact, "answers": answers}
    background.add_task(_generate_and_send, lead, submission_id)

    log.info(
        "submit.ok submission_id=%s email=%s industry=%s total=%d",
        submission_id, contact["email"], answers["industry"], result["total"],
    )
    return {
        "ok": True,
        "submission_id": submission_id,
        "message": "Submission received. Your full report will arrive by email shortly.",
    }


# ===============================================================
# Phase 1 portal endpoints
# ===============================================================

def _avatar_url(key):
    """Short-lived signed GET URL for a profile photo, or None. 1h TTL so it
    persists on the page; the browser caches the image once loaded.
    """
    if not key:
        return None
    try:
        return r2.presign_get(key, ttl=3600)
    except Exception:
        return None


@app.get("/api/me")
async def me(user: dict = Depends(get_current_user)):
    """Return the current user's identity, orgs, and per-org role.

    Uses user_conn so RLS applies: the user can only see orgs they belong
    to (Locke staff see all). Frontend uses this for the topbar avatar,
    the org switcher, and routing logic.
    """
    user_id = user["id"]
    async with user_conn(user_id) as conn:
        memberships = await conn.fetch(
            """
            SELECT m.role::text AS role,
                   m.status::text AS status,
                   o.id AS org_id,
                   o.name AS org_name,
                   o.slug AS org_slug,
                   o.is_internal AS org_is_internal,
                   o.features AS org_features
              FROM memberships m
              JOIN organizations o ON o.id = m.org_id
             WHERE m.user_id = $1
               AND m.status = 'active'
             ORDER BY o.is_internal DESC, o.name ASC
            """,
            user_id,
        )

    rows = [dict(r) for r in memberships]
    primary = rows[0] if rows else None

    async with admin_conn() as conn:
        prefs = await conn.fetchrow(
            "SELECT notify_requests, avatar_key FROM users WHERE id = $1", user_id
        )
    notify_requests = prefs["notify_requests"] if prefs else None

    return {
        "user": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user["name"],
            "clerk_user_id": user["clerk_user_id"],
            "notify_requests": bool(notify_requests) if notify_requests is not None else True,
            "avatar_url": _avatar_url(prefs["avatar_key"]) if prefs else None,
        },
        "primary": _serialize_membership(primary) if primary else None,
        "memberships": [_serialize_membership(r) for r in rows],
    }


class NotificationPrefRequest(BaseModel):
    notify_requests: bool


@app.patch("/api/me/notifications")
async def update_notification_pref(
    payload: NotificationPrefRequest,
    user: dict = Depends(get_current_user),
):
    """Set the caller's own request-notification preference (per-user)."""
    async with admin_conn() as conn:
        await conn.execute(
            "UPDATE users SET notify_requests = $2, updated_at = now() WHERE id = $1",
            user["id"], payload.notify_requests,
        )
    return {"ok": True, "notify_requests": payload.notify_requests}


# ---------------------------------------------------------------
# Profile photo (per-user). Presigned PUT upload to R2; served via signed GET.
# ---------------------------------------------------------------
AVATAR_MAX_BYTES = int(os.environ.get("AVATAR_MAX_BYTES", str(5 * 1024 * 1024)))


class AvatarPresignRequest(BaseModel):
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=1)


class AvatarConfirmRequest(BaseModel):
    key: str = Field(min_length=1, max_length=200)


@app.post("/api/me/avatar/presign")
async def presign_avatar(payload: AvatarPresignRequest, user: dict = Depends(get_current_user)):
    if not payload.content_type.lower().startswith("image/"):
        raise HTTPException(status_code=415, detail="Profile photo must be an image.")
    if payload.size_bytes > AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Image exceeds max size of {AVATAR_MAX_BYTES} bytes")
    key = f"avatars/{user['id']}/{uuid.uuid4()}"
    upload_url = r2.presign_put(key, content_type=payload.content_type)
    return {"key": key, "upload_url": upload_url, "expires_in": r2.UPLOAD_URL_TTL_SECONDS}


@app.post("/api/me/avatar/confirm")
async def confirm_avatar(payload: AvatarConfirmRequest, user: dict = Depends(get_current_user)):
    # Key must be in the caller's own avatar namespace; never trust it blindly.
    if not payload.key.startswith(f"avatars/{user['id']}/"):
        raise HTTPException(status_code=422, detail="Invalid avatar key")
    async with admin_conn() as conn:
        old_key = await conn.fetchval("SELECT avatar_key FROM users WHERE id = $1", user["id"])
        await conn.execute(
            "UPDATE users SET avatar_key = $2, updated_at = now() WHERE id = $1",
            user["id"], payload.key,
        )
    if old_key and old_key != payload.key:
        try:
            r2.delete(old_key)
        except Exception as exc:
            log.warning("avatar.old_delete_failed key=%s err=%s", old_key, exc)
    return {"ok": True, "avatar_url": _avatar_url(payload.key)}


@app.delete("/api/me/avatar")
async def delete_avatar(user: dict = Depends(get_current_user)):
    async with admin_conn() as conn:
        old_key = await conn.fetchval("SELECT avatar_key FROM users WHERE id = $1", user["id"])
        await conn.execute(
            "UPDATE users SET avatar_key = NULL, updated_at = now() WHERE id = $1",
            user["id"],
        )
    if old_key:
        try:
            r2.delete(old_key)
        except Exception as exc:
            log.warning("avatar.delete_failed key=%s err=%s", old_key, exc)
    return {"ok": True}


def _serialize_membership(row: dict) -> dict:
    return {
        "role": row["role"],
        "status": row["status"],
        "org": {
            "id": str(row["org_id"]),
            "name": row["org_name"],
            "slug": row["org_slug"],
            "is_internal": row["org_is_internal"],
            "features": row.get("org_features") or {},
        },
    }


# ===============================================================
# Admin endpoints (Phase 1 minimum CRUD, gated to locke_admin)
# ===============================================================

class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")


class InviteUserRequest(BaseModel):
    email: EmailStr
    role: Literal["locke_admin", "locke_staff", "client_admin", "client_member"] = "client_member"


class UpdateOrgRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=60,
                                pattern=r"^[a-z0-9-]+$")
    status: Optional[Literal["active", "suspended", "archived"]] = None


class UpdateMemberRoleRequest(BaseModel):
    role: Literal["locke_admin", "locke_staff", "client_admin", "client_member"]


def _locke_role_to_clerk_role(locke_role: str) -> str:
    """Map our four-role model to Clerk's two defaults (free tier)."""
    return "org:admin" if locke_role in ("locke_admin", "client_admin") else "org:member"


# Landing URL embedded in Clerk invitation emails. Clerk appends
# `?__clerk_ticket=<ticket>` to this URL; the page MUST mount Clerk's SignUp
# component so it can consume the ticket. The post-signup hand-off to /portal
# is configured separately via signUpFallbackRedirectUrl in signup.html.
#
# Previously this pointed at /portal, which sent invitees to a page that
# expects an authenticated session — Clerk then bounced them to /login,
# where mountSignIn can't process invitation tickets.
SIGNUP_URL = os.environ.get(
    "SIGNUP_URL",
    "https://locke-operations-site.vercel.app/signup",
)


async def _audit(
    actor_user_id, action: str, resource_type: str = None,
    resource_id=None, org_id=None, outcome: str = "success",
    metadata: Optional[dict] = None,
) -> None:
    async with admin_conn() as conn:
        await conn.execute(
            """
            INSERT INTO audit_events
              (actor_user_id, action, resource_type, resource_id,
               org_id, outcome, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            actor_user_id, action, resource_type, resource_id,
            org_id, outcome, (metadata or {}),
        )


@app.post("/api/admin/orgs")
async def create_org(
    payload: CreateOrgRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Create an organization in Clerk + Supabase."""
    # 1. Create in Clerk
    clerk_org = await create_clerk_organization(
        name=payload.name,
        slug=payload.slug,
        created_by_clerk_id=admin["clerk_user_id"],
    )
    clerk_org_id = clerk_org["id"]

    # 2. Insert into Supabase
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO organizations
              (clerk_org_id, name, slug, status, is_internal)
            VALUES ($1, $2, $3, 'active', false)
            ON CONFLICT (clerk_org_id) DO UPDATE
              SET name = EXCLUDED.name, slug = EXCLUDED.slug, updated_at = now()
            RETURNING id, clerk_org_id, name, slug, status::text, created_at
            """,
            clerk_org_id, payload.name, payload.slug,
        )

    org_id = row["id"]
    await _audit(
        actor_user_id=admin["id"], action="org.created",
        resource_type="organization", resource_id=org_id, org_id=org_id,
        metadata={"name": payload.name, "slug": payload.slug,
                  "clerk_org_id": clerk_org_id},
    )

    return {
        "id": str(row["id"]),
        "clerk_org_id": row["clerk_org_id"],
        "name": row["name"],
        "slug": row["slug"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
    }


@app.get("/api/admin/orgs")
async def list_orgs(admin: dict = Depends(require_locke_admin)):
    """List all organizations with active member counts."""
    async with admin_conn() as conn:
        rows = await conn.fetch(
            """
            SELECT
              o.id, o.clerk_org_id, o.name, o.slug,
              o.status::text AS status, o.is_internal,
              o.created_at, o.features,
              (SELECT count(*) FROM memberships m
                WHERE m.org_id = o.id AND m.status = 'active') AS member_count
              FROM organizations o
              ORDER BY o.is_internal DESC, o.created_at DESC
            """
        )
    return {
        "orgs": [
            {
                "id": str(r["id"]),
                "clerk_org_id": r["clerk_org_id"],
                "name": r["name"],
                "slug": r["slug"],
                "status": r["status"],
                "is_internal": r["is_internal"],
                "created_at": r["created_at"].isoformat(),
                "member_count": r["member_count"],
                "features": r["features"] or {},
            }
            for r in rows
        ]
    }


@app.get("/api/admin/orgs/{org_id}")
async def get_org(org_id: str, admin: dict = Depends(require_locke_admin)):
    """Org detail: name, members, pending invitations."""
    async with admin_conn() as conn:
        org = await conn.fetchrow(
            """
            SELECT id, clerk_org_id, name, slug, status::text AS status,
                   is_internal, created_at, features
              FROM organizations WHERE id = $1
            """,
            uuid.UUID(org_id),
        )
        if not org:
            raise HTTPException(status_code=404, detail="Org not found")

        members = await conn.fetch(
            """
            SELECT u.id, u.email, u.name, u.clerk_user_id, u.locked_at, u.avatar_key,
                   m.role::text AS role, m.status::text AS membership_status,
                   m.activated_at
              FROM memberships m
              JOIN users u ON u.id = m.user_id
             WHERE m.org_id = $1
             ORDER BY m.activated_at NULLS LAST, u.email
            """,
            org["id"],
        )

    # Pending invitations live in Clerk; fetch them live.
    pending = []
    if org["clerk_org_id"]:
        try:
            clerk_invites = await list_clerk_pending_invitations(org["clerk_org_id"])
            pending = [
                {
                    "id": inv.get("id"),
                    "email": inv.get("email_address"),
                    "clerk_role": inv.get("role"),
                    "created_at": inv.get("created_at"),
                }
                for inv in clerk_invites
            ]
        except Exception as exc:
            log.warning("admin.orgs.invitations_fetch_failed org=%s err=%s",
                        org_id, exc)

    return {
        "org": {
            "id": str(org["id"]),
            "clerk_org_id": org["clerk_org_id"],
            "name": org["name"],
            "slug": org["slug"],
            "status": org["status"],
            "is_internal": org["is_internal"],
            "created_at": org["created_at"].isoformat(),
            "features": org["features"] or {},
        },
        "members": [
            {
                "id": str(m["id"]),
                "email": m["email"],
                "name": m["name"],
                "clerk_user_id": m["clerk_user_id"],
                "role": m["role"],
                "status": m["membership_status"],
                "activated_at": m["activated_at"].isoformat() if m["activated_at"] else None,
                "locked_at": m["locked_at"].isoformat() if m["locked_at"] else None,
                "avatar_url": _avatar_url(m["avatar_key"]),
            }
            for m in members
        ],
        "pending_invitations": pending,
    }


class SetFeatureRequest(BaseModel):
    feature: Literal["requests"]
    enabled: bool


@app.patch("/api/admin/orgs/{org_id}/features")
async def set_org_feature(
    org_id: str,
    payload: SetFeatureRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Toggle a per-org feature flag (e.g. the Requests surface). Merges the
    single key into organizations.features so other flags are preserved.
    """
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE organizations
               SET features = jsonb_set(coalesce(features, '{}'::jsonb),
                                        ARRAY[$2::text], to_jsonb($3::boolean), true),
                   updated_at = now()
             WHERE id = $1
            RETURNING id, features
            """,
            uuid.UUID(org_id), payload.feature, payload.enabled,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Org not found")

    await _audit(
        actor_user_id=admin["id"], action="org.feature_set",
        resource_type="organization", resource_id=row["id"], org_id=row["id"],
        metadata={"feature": payload.feature, "enabled": payload.enabled},
    )
    return {"ok": True, "features": row["features"] or {}}


@app.post("/api/admin/internal-org/link-clerk")
async def link_internal_org_clerk(admin: dict = Depends(require_locke_admin)):
    """One-time setup: give the internal Locke org a real Clerk organization so
    staff can be invited through the standard flow. Full locke_admin only.

    The internal org was seeded without a Clerk org, so it had no clerk_org_id
    and invitations had nothing to target. This creates the Clerk org (with the
    caller as its creator/admin) and links it to the existing internal row.
    """
    async with admin_conn() as conn:
        await _require_full_admin(conn, admin)
        org = await conn.fetchrow(
            "SELECT id, name, slug, clerk_org_id FROM organizations WHERE is_internal = true LIMIT 1"
        )
        if not org:
            raise HTTPException(status_code=404, detail="No internal Locke organization found")
        if org["clerk_org_id"]:
            return {"ok": True, "already_linked": True, "clerk_org_id": org["clerk_org_id"]}

        clerk_org = await create_clerk_organization(
            name=org["name"],
            slug=org["slug"],
            created_by_clerk_id=admin["clerk_user_id"],
        )
        clerk_org_id = clerk_org["id"]
        # Set our row's link first; the async organization.created webhook then
        # hits ON CONFLICT (clerk_org_id) DO NOTHING and won't create a duplicate.
        await conn.execute(
            "UPDATE organizations SET clerk_org_id = $1, updated_at = now() WHERE id = $2",
            clerk_org_id, org["id"],
        )

    await _audit(
        actor_user_id=admin["id"], action="org.clerk_linked",
        resource_type="organization", resource_id=org["id"], org_id=org["id"],
        metadata={"clerk_org_id": clerk_org_id, "internal": True},
    )
    return {"ok": True, "already_linked": False, "clerk_org_id": clerk_org_id}


@app.post("/api/admin/orgs/{org_id}/invitations")
async def invite_user(
    org_id: str,
    payload: InviteUserRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Invite a user to an org. Clerk sends the email; webhook handles
    membership creation on acceptance.

    One-user-one-org invariant is enforced here: an email tied to any
    existing user row is rejected, regardless of which org. Hard-deleted
    users (users row gone) re-invite cleanly with no extra handling.
    """
    async with admin_conn() as conn:
        org = await conn.fetchrow(
            "SELECT id, clerk_org_id, name, is_internal FROM organizations WHERE id = $1",
            uuid.UUID(org_id),
        )
        if not org:
            raise HTTPException(status_code=404, detail="Org not found")
        if not org["clerk_org_id"]:
            raise HTTPException(status_code=409, detail="Org has no Clerk link")

        # Role/org-type guard. Locke roles only in the internal org and only a
        # full locke_admin may grant them; client roles only in client orgs.
        invite_is_locke = payload.role in ("locke_admin", "locke_staff")
        if invite_is_locke:
            if not org["is_internal"]:
                raise HTTPException(status_code=409, detail="Locke roles can only be assigned in the internal Locke organization.")
            await _require_full_admin(conn, admin)
        elif org["is_internal"]:
            raise HTTPException(status_code=409, detail="Client roles cannot be assigned in the internal Locke organization.")

        existing = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.locked_at, o.name AS org_name
              FROM users u
              LEFT JOIN memberships m ON m.user_id = u.id AND m.status = 'active'
              LEFT JOIN organizations o ON o.id = m.org_id
             WHERE u.email = $1
             LIMIT 1
            """,
            payload.email,
        )
        if existing:
            org_label = existing["org_name"] or "another organization"
            detail = (
                f"{payload.email} is already on the platform "
                f"(member of {org_label}). Hard-delete that user first to re-invite."
            )
            raise HTTPException(status_code=409, detail=detail)

    clerk_invite = await create_clerk_invitation(
        clerk_org_id=org["clerk_org_id"],
        email=payload.email,
        clerk_role=_locke_role_to_clerk_role(payload.role),
        redirect_url=SIGNUP_URL,
    )

    await _audit(
        actor_user_id=admin["id"], action="invitation.sent",
        resource_type="invitation", org_id=org["id"],
        metadata={
            "email": payload.email,
            "role": payload.role,
            "clerk_invitation_id": clerk_invite.get("id"),
        },
    )

    return {
        "ok": True,
        "clerk_invitation_id": clerk_invite.get("id"),
        "email": payload.email,
        "role": payload.role,
    }


@app.patch("/api/admin/orgs/{org_id}")
async def update_org(
    org_id: str,
    payload: UpdateOrgRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Update org name, slug, and/or status.

    Status transitions cascade to memberships:
      active     -> suspended : all active memberships -> suspended
      suspended  -> active    : all suspended memberships -> active
      anything   -> archived  : all non-removed memberships -> removed, archived_at set
    """
    async with admin_conn() as conn:
        org = await conn.fetchrow(
            "SELECT id, clerk_org_id, name, slug, status::text AS status, "
            "       is_internal, archived_at "
            "FROM organizations WHERE id = $1",
            uuid.UUID(org_id),
        )
        if not org:
            raise HTTPException(status_code=404, detail="Org not found")
        if org["is_internal"]:
            raise HTTPException(
                status_code=403,
                detail="The Locke internal organization cannot be modified through the admin API.",
            )

        old_status = org["status"]
        new_status = payload.status or old_status

        # Mirror name/slug to Clerk if they're changing.
        if (payload.name and payload.name != org["name"]) or \
           (payload.slug and payload.slug != org["slug"]):
            if org["clerk_org_id"]:
                await update_clerk_organization(
                    clerk_org_id=org["clerk_org_id"],
                    name=payload.name,
                    slug=payload.slug,
                )

        # Build dynamic UPDATE for org row.
        sets = []
        params: list = []
        if payload.name is not None:
            params.append(payload.name)
            sets.append(f"name = ${len(params)}")
        if payload.slug is not None:
            params.append(payload.slug)
            sets.append(f"slug = ${len(params)}")
        if payload.status is not None and payload.status != old_status:
            params.append(payload.status)
            sets.append(f"status = ${len(params)}::org_status")
            if payload.status == "archived":
                sets.append("archived_at = now()")
            elif old_status == "archived":
                sets.append("archived_at = NULL")
        if not sets:
            return await _serialize_org(conn, org["id"])

        params.append(org["id"])
        await conn.execute(
            f"UPDATE organizations SET {', '.join(sets)}, updated_at = now() "
            f"WHERE id = ${len(params)}",
            *params,
        )

        # Cascade membership status if org status changed.
        if payload.status and payload.status != old_status:
            if new_status == "suspended":
                await conn.execute(
                    "UPDATE memberships SET status = 'suspended', updated_at = now() "
                    "WHERE org_id = $1 AND status = 'active'",
                    org["id"],
                )
            elif new_status == "active" and old_status == "suspended":
                await conn.execute(
                    "UPDATE memberships SET status = 'active', updated_at = now() "
                    "WHERE org_id = $1 AND status = 'suspended'",
                    org["id"],
                )
            elif new_status == "archived":
                await conn.execute(
                    "UPDATE memberships SET status = 'removed', updated_at = now() "
                    "WHERE org_id = $1 AND status IN ('active', 'suspended', 'invited')",
                    org["id"],
                )

    await _audit(
        actor_user_id=admin["id"], action="org.updated",
        resource_type="organization", resource_id=org["id"], org_id=org["id"],
        metadata={
            "fields_changed": [k for k, v in payload.model_dump().items() if v is not None],
            "old_status": old_status, "new_status": new_status,
        },
    )

    async with admin_conn() as conn:
        return await _serialize_org(conn, org["id"])


@app.delete("/api/admin/orgs/{org_id}")
async def delete_org(
    org_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Hard delete an org.

    Refuses to delete:
    - The internal Locke org (would be catastrophic).
    - Any org with documents, including soft-deleted ones (RESTRICT FK
      applies to all rows, not just deleted_at IS NULL).

    For orgs that pass the guards:
    1. Deletes the Clerk org (so admins don't see a stale entry there).
    2. Deletes our memberships in a transaction (FK to organizations is
       RESTRICT, requires explicit). invitations CASCADE; audit_events
       SET NULL to preserve history with a hole.
    3. Catches FK violations on the final DELETE to surface the offending
       constraint instead of bubbling a raw 500.

    Idempotent with _on_org_deleted webhook: whichever side runs first wins,
    the other becomes a no-op.
    """
    async with admin_conn() as conn:
        org = await conn.fetchrow(
            "SELECT id, clerk_org_id, name, slug, is_internal "
            "FROM organizations WHERE id = $1",
            uuid.UUID(org_id),
        )
        if not org:
            raise HTTPException(status_code=404, detail="Org not found")
        if org["is_internal"]:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete the internal Locke organization.",
            )

        # RESTRICT FK applies to ALL rows including soft-deleted, so count
        # all of them. (The display elsewhere filters by deleted_at IS NULL,
        # but for FK purposes the soft-deleted rows still block.)
        doc_count = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE org_id = $1",
            org["id"],
        )
        if doc_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete: org has {doc_count} document row(s) "
                    f"(including any soft-deleted). Archive the org or purge "
                    f"the documents first."
                ),
            )

        if org["clerk_org_id"]:
            await delete_clerk_organization(org["clerk_org_id"])

        # Transaction so a partial failure (memberships gone, org still here)
        # is impossible. Catch FK violations specifically so the error body
        # names the constraint instead of returning an opaque 500.
        try:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM memberships WHERE org_id = $1", org["id"],
                )
                await conn.execute(
                    "DELETE FROM organizations WHERE id = $1", org["id"],
                )
        except asyncpg.exceptions.ForeignKeyViolationError as exc:
            log.warning(
                "delete_org.fk_violation org_id=%s detail=%s",
                org["id"], exc,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete org: a foreign key constraint is still "
                    f"holding it. Details: {exc}"
                ),
            ) from exc

    await _audit(
        actor_user_id=admin["id"], action="organization.deleted",
        resource_type="organization", resource_id=org["id"],
        metadata={"name": org["name"], "slug": org["slug"]},
    )

    return {"ok": True}


async def _serialize_org(conn, org_id) -> dict:
    """Helper: fetch a single org row and serialize to dict."""
    row = await conn.fetchrow(
        "SELECT id, clerk_org_id, name, slug, status::text AS status, is_internal, "
        "       created_at, archived_at "
        "FROM organizations WHERE id = $1",
        org_id,
    )
    return {
        "id": str(row["id"]),
        "clerk_org_id": row["clerk_org_id"],
        "name": row["name"],
        "slug": row["slug"],
        "status": row["status"],
        "is_internal": row["is_internal"],
        "created_at": row["created_at"].isoformat(),
        "archived_at": row["archived_at"].isoformat() if row["archived_at"] else None,
    }


@app.patch("/api/admin/orgs/{org_id}/members/{user_id}")
async def update_member_role(
    org_id: str,
    user_id: str,
    payload: UpdateMemberRoleRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Change a user's role within an org. Mirrors to Clerk."""
    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT m.id AS membership_id, m.role::text AS old_role,
                   o.id AS org_id, o.clerk_org_id, o.is_internal,
                   u.id AS user_id, u.clerk_user_id, u.email
              FROM memberships m
              JOIN organizations o ON o.id = m.org_id
              JOIN users u ON u.id = m.user_id
             WHERE m.org_id = $1 AND m.user_id = $2
            """,
            uuid.UUID(org_id), uuid.UUID(user_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Membership not found")

        # Locke staff are managed only from the admin surface, not a client org;
        # internal-org role changes require a full locke_admin caller.
        await _guard_locke_staff_managed_internally(conn, admin, row)

        # Keep roles consistent with org type: internal org holds Locke roles,
        # client orgs hold client roles. Prevents privilege crossover.
        new_is_locke = payload.role in ("locke_admin", "locke_staff")
        if row["is_internal"] and not new_is_locke:
            raise HTTPException(status_code=409, detail="Internal Locke org members must hold a Locke role.")
        if not row["is_internal"] and new_is_locke:
            raise HTTPException(status_code=409, detail="Client org members cannot hold Locke roles.")

        # Sanity: don't allow downgrading the last locke_admin out of the internal org.
        if row["is_internal"] and row["old_role"] == "locke_admin" and \
           payload.role != "locke_admin":
            other_admins = await conn.fetchval(
                """
                SELECT count(*) FROM memberships
                 WHERE org_id = $1 AND role = 'locke_admin'
                   AND status = 'active' AND user_id <> $2
                """,
                row["org_id"], row["user_id"],
            )
            if other_admins == 0:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot demote the only remaining locke_admin.",
                )

        # Mirror to Clerk.
        if row["clerk_org_id"] and row["clerk_user_id"]:
            clerk_role = _locke_role_to_clerk_role(payload.role)
            await update_clerk_membership_role(
                clerk_org_id=row["clerk_org_id"],
                clerk_user_id=row["clerk_user_id"],
                clerk_role=clerk_role,
            )

        # Update our row.
        await conn.execute(
            "UPDATE memberships SET role = $1::user_role, updated_at = now() "
            "WHERE id = $2",
            payload.role, row["membership_id"],
        )

    await _audit(
        actor_user_id=admin["id"], action="membership.role_changed",
        resource_type="membership", resource_id=row["membership_id"],
        org_id=row["org_id"],
        metadata={"email": row["email"], "old_role": row["old_role"], "new_role": payload.role},
    )

    return {"ok": True, "role": payload.role}


async def _load_member_for_admin_action(
    conn, org_id: str, user_id: str,
) -> dict[str, Any]:
    """Shared loader for member admin actions (suspend/unsuspend/delete).

    Returns membership + org + user fields, or raises 404.
    """
    row = await conn.fetchrow(
        """
        SELECT m.id AS membership_id, m.role::text AS role, m.status::text AS status,
               o.id AS org_id, o.clerk_org_id, o.is_internal,
               u.id AS user_id, u.clerk_user_id, u.email, u.locked_at
          FROM memberships m
          JOIN organizations o ON o.id = m.org_id
          JOIN users u ON u.id = m.user_id
         WHERE m.org_id = $1 AND m.user_id = $2
        """,
        uuid.UUID(org_id), uuid.UUID(user_id),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Membership not found")
    return row


async def _guard_last_locke_admin(conn, row: dict[str, Any]) -> None:
    """Block the action if it would leave the internal org with no locke_admin."""
    if row["is_internal"] and row["role"] == "locke_admin":
        other_admins = await conn.fetchval(
            """
            SELECT count(*) FROM memberships
             WHERE org_id = $1 AND role = 'locke_admin'
               AND status = 'active' AND user_id <> $2
            """,
            row["org_id"], row["user_id"],
        )
        if other_admins == 0:
            raise HTTPException(
                status_code=409,
                detail="Cannot remove or suspend the only remaining locke_admin.",
            )


def _guard_not_self(admin: dict, row: dict[str, Any]) -> None:
    """Block an admin from suspending or deleting their own account, so they
    can't lock themselves out. Distinct from the last-admin guard: this fires
    even when other admins exist. Standard self-action protection.
    """
    if str(row["user_id"]) == str(admin["id"]):
        raise HTTPException(
            status_code=409,
            detail="You cannot suspend or delete your own account.",
        )


async def _guard_locke_staff_managed_internally(conn, admin: dict, row: dict[str, Any]) -> None:
    """Locke staff/admins are managed only from the internal Locke org, and only
    by a full locke_admin. Two cases:
      - Action within the internal Locke org -> require full locke_admin caller.
      - Action within a client org on a user who holds any Locke role -> blocked
        (such a user is managed from the internal org, not here).
    """
    if row["is_internal"]:
        await _require_full_admin(conn, admin)
        return
    is_staff = await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1 FROM memberships
           WHERE user_id = $1
             AND role IN ('locke_admin', 'locke_staff')
             AND status = 'active'
        )
        """,
        row["user_id"],
    )
    if is_staff:
        raise HTTPException(
            status_code=409,
            detail="This user is Locke staff and can only be managed from the admin area, not a client organization.",
        )


async def _require_full_admin(conn, admin: dict) -> None:
    """Require the CALLER to be a full locke_admin (not merely locke_staff).
    Gate for the most sensitive operations: managing Locke staff/admins. The
    require_locke_admin dependency accepts locke_staff too; this is stricter.
    """
    is_admin = await conn.fetchval(
        """
        SELECT EXISTS (
          SELECT 1 FROM memberships
           WHERE user_id = $1 AND role = 'locke_admin' AND status = 'active'
        )
        """,
        admin["id"],
    )
    if not is_admin:
        raise HTTPException(
            status_code=403,
            detail="This action requires the full locke_admin role.",
        )


@app.post("/api/admin/orgs/{org_id}/members/{user_id}/suspend")
async def suspend_member(
    org_id: str,
    user_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Suspend a user: Clerk-lock them so they can't sign in. Reversible."""
    async with admin_conn() as conn:
        row = await _load_member_for_admin_action(conn, org_id, user_id)
        await _guard_last_locke_admin(conn, row)
        _guard_not_self(admin, row)
        await _guard_locke_staff_managed_internally(conn, admin, row)

        if row["clerk_user_id"]:
            await lock_clerk_user(row["clerk_user_id"])

        await conn.execute(
            "UPDATE users SET locked_at = now(), updated_at = now() "
            "WHERE id = $1",
            row["user_id"],
        )

    await _audit(
        actor_user_id=admin["id"], action="user.suspended",
        resource_type="user", resource_id=row["user_id"],
        org_id=row["org_id"],
        metadata={"email": row["email"], "role": row["role"]},
    )

    return {"ok": True}


@app.post("/api/admin/orgs/{org_id}/members/{user_id}/unsuspend")
async def unsuspend_member(
    org_id: str,
    user_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Reverse a suspension: Clerk-unlock + clear users.locked_at."""
    async with admin_conn() as conn:
        row = await _load_member_for_admin_action(conn, org_id, user_id)

        if row["clerk_user_id"]:
            await unlock_clerk_user(row["clerk_user_id"])

        await conn.execute(
            "UPDATE users SET locked_at = NULL, updated_at = now() "
            "WHERE id = $1",
            row["user_id"],
        )

    await _audit(
        actor_user_id=admin["id"], action="user.unsuspended",
        resource_type="user", resource_id=row["user_id"],
        org_id=row["org_id"],
        metadata={"email": row["email"], "role": row["role"]},
    )

    return {"ok": True}


@app.delete("/api/admin/orgs/{org_id}/members/{user_id}")
async def delete_member(
    org_id: str,
    user_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Hard delete a user.

    Removes the Clerk user (so the email can be re-invited fresh) and deletes
    our users row. memberships cascade via ON DELETE CASCADE.
    documents.uploaded_by / audit_events.actor_user_id / invitations.invited_by
    set to NULL via existing FKs — audit history survives, attribution does not.
    """
    async with admin_conn() as conn:
        row = await _load_member_for_admin_action(conn, org_id, user_id)
        await _guard_last_locke_admin(conn, row)
        _guard_not_self(admin, row)
        await _guard_locke_staff_managed_internally(conn, admin, row)

        # Capture audit fields before the row is gone.
        deleted_email = row["email"]
        deleted_role = row["role"]
        deleted_user_id = row["user_id"]
        deleted_org_id = row["org_id"]

        # Delete from Clerk first. If our DB delete fails afterwards we'd
        # have a stale users row pointing at a now-gone clerk_user_id, but
        # that's recoverable; the reverse (DB gone, Clerk still has the user)
        # would block the email from being re-invited.
        if row["clerk_user_id"]:
            await delete_clerk_user(row["clerk_user_id"])

        await conn.execute("DELETE FROM users WHERE id = $1", deleted_user_id)

    await _audit(
        actor_user_id=admin["id"], action="user.deleted",
        resource_type="user", resource_id=deleted_user_id,
        org_id=deleted_org_id,
        metadata={"email": deleted_email, "role": deleted_role},
    )

    return {"ok": True}


@app.delete("/api/admin/orgs/{org_id}/invitations/{clerk_invitation_id}")
async def revoke_invitation(
    org_id: str,
    clerk_invitation_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Revoke a pending Clerk invitation."""
    async with admin_conn() as conn:
        org = await conn.fetchrow(
            "SELECT id, clerk_org_id FROM organizations WHERE id = $1",
            uuid.UUID(org_id),
        )
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    if not org["clerk_org_id"]:
        raise HTTPException(status_code=409, detail="Org has no Clerk link")

    await revoke_clerk_invitation(
        clerk_org_id=org["clerk_org_id"],
        clerk_invitation_id=clerk_invitation_id,
    )

    await _audit(
        actor_user_id=admin["id"], action="invitation.revoked",
        resource_type="invitation", org_id=org["id"],
        metadata={"clerk_invitation_id": clerk_invitation_id},
    )

    return {"ok": True}


# ===============================================================
# Phase 2: Document core
#
# Upload is presigned-PUT: the admin browser uploads bytes directly to R2,
# then calls /confirm to record the documents row. Keeps large files off
# Railway. Downloads always proxy through the API to mint a short-lived
# signed URL (the access-control + revocation boundary; never hand the
# browser a raw key or a long-lived URL).
# ===============================================================

DocumentCategory = Literal["audit_report", "runbook", "monthly_review", "contract", "implementation", "general"]
# 'implementation' docs are Locke-internal build records; never client-visible.
INTERNAL_CATEGORIES = {"implementation"}

R2_MAX_UPLOAD_BYTES = int(os.environ.get("R2_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))


class PresignUploadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: DocumentCategory
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)


class ConfirmUploadRequest(BaseModel):
    doc_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)
    category: DocumentCategory
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)


async def _load_active_org(conn, org_id: str) -> dict:
    """Fetch an org by id or raise 404. Used by document admin endpoints."""
    try:
        oid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Org not found")
    org = await conn.fetchrow(
        "SELECT id, name, status::text AS status FROM organizations WHERE id = $1",
        oid,
    )
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    return dict(org)


@app.post("/api/admin/orgs/{org_id}/documents/presign")
async def presign_document_upload(
    org_id: str,
    payload: PresignUploadRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Step 1 of upload: validate, mint a presigned PUT URL, return the doc_id
    and storage_key the browser must use. No DB row yet; that happens at confirm.
    """
    if payload.size_bytes > R2_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max upload size of {R2_MAX_UPLOAD_BYTES} bytes",
        )

    async with admin_conn() as conn:
        org = await _load_active_org(conn, org_id)
    if org["status"] != "active":
        raise HTTPException(status_code=409, detail="Org is not active")

    doc_id = str(uuid.uuid4())
    storage_key = r2.build_storage_key(org["id"], doc_id, version=1)
    upload_url = r2.presign_put(storage_key, content_type=payload.content_type)

    return {
        "doc_id": doc_id,
        "storage_key": storage_key,
        "upload_url": upload_url,
        "expires_in": r2.UPLOAD_URL_TTL_SECONDS,
    }


@app.post("/api/admin/orgs/{org_id}/documents/confirm")
async def confirm_document_upload(
    org_id: str,
    payload: ConfirmUploadRequest,
    admin: dict = Depends(require_locke_admin),
):
    """Step 2 of upload: record the documents row after the browser PUT to R2.

    storage_key is recomputed server-side from org_id + doc_id (never trusted
    from the client) so a caller can't point a row at someone else's object.
    """
    try:
        doc_uuid = uuid.UUID(payload.doc_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid doc_id")

    async with admin_conn() as conn:
        org = await _load_active_org(conn, org_id)
        storage_key = r2.build_storage_key(org["id"], doc_uuid, version=1)
        visibility = "internal" if payload.category in INTERNAL_CATEGORIES else "client"
        row = await conn.fetchrow(
            """
            INSERT INTO documents
              (id, org_id, category, name, storage_key, version,
               size_bytes, content_type, uploaded_by, visibility)
            VALUES ($1, $2, $3, $4, $5, 1, $6, $7, $8, $9)
            RETURNING id, uploaded_at
            """,
            doc_uuid, org["id"], payload.category, payload.name, storage_key,
            payload.size_bytes, payload.content_type, admin["id"], visibility,
        )

    await _audit(
        actor_user_id=admin["id"], action="document.uploaded",
        resource_type="document", resource_id=row["id"], org_id=org["id"],
        metadata={"name": payload.name, "category": payload.category,
                  "size_bytes": payload.size_bytes},
    )

    return {
        "id": str(row["id"]),
        "org_id": str(org["id"]),
        "name": payload.name,
        "category": payload.category,
        "size_bytes": payload.size_bytes,
        "uploaded_at": row["uploaded_at"].isoformat(),
    }


@app.get("/api/admin/orgs/{org_id}/documents")
async def admin_list_org_documents(
    org_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """List one org's live documents for the admin UI.

    Distinct from GET /api/documents (user/RLS-scoped). Admin reads through
    admin_conn and filters explicitly by org_id, so it returns exactly this
    org's non-deleted documents regardless of the admin's own memberships.
    """
    async with admin_conn() as conn:
        org = await _load_active_org(conn, org_id)
        rows = await conn.fetch(
            """
            SELECT d.id, d.category::text AS category, d.name, d.size_bytes,
                   d.content_type, d.uploaded_at, d.source, d.note, d.visibility,
                   u.email AS uploader_email
              FROM documents d
              LEFT JOIN users u ON u.id = d.uploaded_by
             WHERE d.org_id = $1 AND d.deleted_at IS NULL
             ORDER BY d.uploaded_at DESC
            """,
            org["id"],
        )

    return {
        "documents": [
            {
                "id": str(r["id"]),
                "category": r["category"],
                "name": r["name"],
                "size_bytes": r["size_bytes"],
                "content_type": r["content_type"],
                "uploaded_at": r["uploaded_at"].isoformat(),
                "source": r["source"],
                "uploader_email": r["uploader_email"],
                "note": r["note"],
                "visibility": r["visibility"],
            }
            for r in rows
        ],
    }


@app.get("/api/documents")
async def list_documents(user: dict = Depends(get_current_user)):
    """List the caller's documents.

    Two distinct kinds, kept separate so a client upload can never look like a
    Locke deliverable:
      - categories: Locke deliverables (source='locke'), grouped by category.
      - shared:     client uploads (source='client') for the caller's org, each
                    flagged `mine` if this user uploaded it (drives delete UI).

    Reads through user_conn so RLS scopes rows to the caller's org and hides
    soft-deleted rows. No org_id param: it's derived from the session.
    """
    async with user_conn(user["id"]) as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, category::text AS category, name, size_bytes,
                   content_type, uploaded_at, source, uploaded_by, note
              FROM documents
             ORDER BY uploaded_at DESC
            """,
        )

    grouped: dict[str, list[dict]] = {
        "audit_report": [], "runbook": [], "monthly_review": [], "contract": [], "general": [],
    }
    shared: list[dict] = []
    for r in rows:
        base = {
            "id": str(r["id"]),
            "name": r["name"],
            "size_bytes": r["size_bytes"],
            "content_type": r["content_type"],
            "uploaded_at": r["uploaded_at"].isoformat(),
        }
        if r["source"] == "client":
            shared.append({
                **base,
                "note": r["note"],
                "mine": r["uploaded_by"] is not None
                        and str(r["uploaded_by"]) == str(user["id"]),
            })
        elif r["category"] in grouped:
            grouped[r["category"]].append({**base, "category": r["category"]})

    return {"categories": grouped, "shared": shared, "total": len(rows)}


# ---------------------------------------------------------------
# Client-side uploads ("Shared with Locke")
#
# Clients upload files TO Locke (system docs, templates, the processes we
# automate). These are source='client', carry no Locke category, and live in
# a separate portal section. Security model:
#   - org is derived from the caller's own active membership, never from input,
#     so a client cannot write into another org's namespace;
#   - rows are tagged source='client' and can never overwrite or impersonate a
#     Locke deliverable (DB CHECK enforces category IS NULL for client rows);
#   - a client may soft-delete only files they uploaded (source='client' AND
#     uploaded_by = caller); Locke deliverables are untouchable from here.
# ---------------------------------------------------------------

class ClientPresignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)


class ClientConfirmRequest(BaseModel):
    doc_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1)
    note: Optional[str] = Field(default=None, max_length=2000)


async def _resolve_caller_upload_org(conn, user_id) -> dict:
    """Determine which org a client upload belongs to, from the caller's own
    active memberships. Returns the org row or raises.

    Unambiguous for clients (one org). If a caller belongs to several orgs
    (e.g. Locke staff), refuse rather than guess; staff upload via /admin.
    """
    rows = await conn.fetch(
        """
        SELECT o.id, o.status::text AS status, o.is_internal
          FROM memberships m
          JOIN organizations o ON o.id = m.org_id
         WHERE m.user_id = $1 AND m.status = 'active'
        """,
        user_id,
    )
    if not rows:
        raise HTTPException(status_code=400, detail="No active organization for this user")

    candidates = [dict(r) for r in rows]
    if len(candidates) > 1:
        non_internal = [c for c in candidates if not c["is_internal"]]
        if len(non_internal) != 1:
            raise HTTPException(
                status_code=409,
                detail="Caller belongs to multiple organizations; upload via admin",
            )
        candidates = non_internal

    org = candidates[0]
    if org["status"] != "active":
        raise HTTPException(status_code=409, detail="Organization is not active")
    return org


@app.post("/api/documents/presign")
async def client_presign_upload(
    payload: ClientPresignRequest,
    user: dict = Depends(get_current_user),
):
    """Step 1 of a client upload: validate, resolve the caller's org, mint a
    presigned PUT URL. No DB row yet.
    """
    if payload.size_bytes > R2_MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds max upload size of {R2_MAX_UPLOAD_BYTES} bytes",
        )
    async with admin_conn() as conn:
        org = await _resolve_caller_upload_org(conn, user["id"])

    doc_id = str(uuid.uuid4())
    storage_key = r2.build_storage_key(org["id"], doc_id, version=1)
    upload_url = r2.presign_put(storage_key, content_type=payload.content_type)

    return {
        "doc_id": doc_id,
        "storage_key": storage_key,
        "upload_url": upload_url,
        "expires_in": r2.UPLOAD_URL_TTL_SECONDS,
    }


@app.post("/api/documents/confirm")
async def client_confirm_upload(
    payload: ClientConfirmRequest,
    user: dict = Depends(get_current_user),
):
    """Step 2 of a client upload: record the row as source='client' for the
    caller's org. storage_key is recomputed server-side from the resolved org
    and doc_id, never trusted from the client.
    """
    try:
        doc_uuid = uuid.UUID(payload.doc_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid doc_id")

    async with admin_conn() as conn:
        org = await _resolve_caller_upload_org(conn, user["id"])
        storage_key = r2.build_storage_key(org["id"], doc_uuid, version=1)
        note = (payload.note or "").strip() or None
        row = await conn.fetchrow(
            """
            INSERT INTO documents
              (id, org_id, category, name, storage_key, version,
               size_bytes, content_type, uploaded_by, source, note)
            VALUES ($1, $2, NULL, $3, $4, 1, $5, $6, $7, 'client', $8)
            RETURNING id, uploaded_at
            """,
            doc_uuid, org["id"], payload.name, storage_key,
            payload.size_bytes, payload.content_type, user["id"], note,
        )

    await _audit(
        actor_user_id=user["id"], action="document.uploaded",
        resource_type="document", resource_id=row["id"], org_id=org["id"],
        metadata={"name": payload.name, "source": "client",
                  "size_bytes": payload.size_bytes},
    )

    return {
        "id": str(row["id"]),
        "name": payload.name,
        "size_bytes": payload.size_bytes,
        "uploaded_at": row["uploaded_at"].isoformat(),
        "note": note,
        "mine": True,
    }


@app.delete("/api/documents/{document_id}")
async def client_delete_document(
    document_id: str,
    user: dict = Depends(get_current_user),
):
    """Soft-delete a client upload the caller made. Scoped to source='client'
    AND uploaded_by = caller, so Locke deliverables and other people's uploads
    are untouchable through this path.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found")

    async with admin_conn() as conn:
        doc = await conn.fetchrow(
            """
            UPDATE documents
               SET deleted_at = now()
             WHERE id = $1 AND source = 'client'
               AND uploaded_by = $2 AND deleted_at IS NULL
            RETURNING id, org_id, name
            """,
            doc_uuid, user["id"],
        )

    if not doc:
        # Either it doesn't exist, isn't a client upload, or isn't theirs.
        await _audit(
            actor_user_id=user["id"], action="document.delete",
            resource_type="document", resource_id=doc_uuid, outcome="denied",
        )
        raise HTTPException(status_code=404, detail="Document not found")

    await _audit(
        actor_user_id=user["id"], action="document.deleted",
        resource_type="document", resource_id=doc["id"], org_id=doc["org_id"],
        metadata={"name": doc["name"], "source": "client"},
    )

    return {"ok": True, "id": str(doc["id"])}


class UpdateDocumentNoteRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@app.patch("/api/documents/{document_id}")
async def client_update_document_note(
    document_id: str,
    payload: UpdateDocumentNoteRequest,
    user: dict = Depends(get_current_user),
):
    """Edit the description (note) on a client upload the caller made. Scoped to
    source='client' AND uploaded_by = caller, identical to the delete path, so
    Locke deliverables and other people's uploads can't be edited here.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found")

    note = (payload.note or "").strip() or None

    async with admin_conn() as conn:
        doc = await conn.fetchrow(
            """
            UPDATE documents
               SET note = $3
             WHERE id = $1 AND source = 'client'
               AND uploaded_by = $2 AND deleted_at IS NULL
            RETURNING id, org_id, name
            """,
            doc_uuid, user["id"], note,
        )

    if not doc:
        await _audit(
            actor_user_id=user["id"], action="document.update",
            resource_type="document", resource_id=doc_uuid, outcome="denied",
        )
        raise HTTPException(status_code=404, detail="Document not found")

    await _audit(
        actor_user_id=user["id"], action="document.note_updated",
        resource_type="document", resource_id=doc["id"], org_id=doc["org_id"],
        metadata={"name": doc["name"], "has_note": note is not None},
    )

    return {"ok": True, "id": str(doc["id"]), "note": note}


@app.get("/api/documents/{document_id}/download")
async def download_document(
    document_id: str,
    user: dict = Depends(get_current_user),
):
    """Mint a short-lived signed URL for a document the caller may see.

    The read goes through user_conn, so RLS is the access gate: a document the
    caller can't see (wrong org, or soft-deleted) returns no row -> 404, and we
    log the denied attempt per the append-only-audit commitment.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found")

    async with user_conn(user["id"]) as conn:
        doc = await conn.fetchrow(
            "SELECT id, org_id, name, storage_key FROM documents WHERE id = $1",
            doc_uuid,
        )

    if not doc:
        await _audit(
            actor_user_id=user["id"], action="document.download",
            resource_type="document", resource_id=doc_uuid, outcome="denied",
        )
        raise HTTPException(status_code=404, detail="Document not found")

    url = r2.presign_get(doc["storage_key"], download_filename=doc["name"])

    await _audit(
        actor_user_id=user["id"], action="document.downloaded",
        resource_type="document", resource_id=doc["id"], org_id=doc["org_id"],
        metadata={"name": doc["name"]},
    )

    return {"url": url, "expires_in": r2.DOWNLOAD_URL_TTL_SECONDS, "name": doc["name"]}


@app.delete("/api/admin/documents/{document_id}")
async def delete_document(
    document_id: str,
    admin: dict = Depends(require_locke_admin),
):
    """Soft-delete a document. The R2 object stays put (retention/purge is a
    later job); RLS already hides soft-deleted rows from clients.
    """
    try:
        doc_uuid = uuid.UUID(document_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found")

    async with admin_conn() as conn:
        doc = await conn.fetchrow(
            """
            UPDATE documents
               SET deleted_at = now()
             WHERE id = $1 AND deleted_at IS NULL
            RETURNING id, org_id, name
            """,
            doc_uuid,
        )

    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    await _audit(
        actor_user_id=admin["id"], action="document.deleted",
        resource_type="document", resource_id=doc["id"], org_id=doc["org_id"],
        metadata={"name": doc["name"]},
    )

    return {"ok": True, "id": str(doc["id"])}


# ===============================================================
# Phase 5: Requests (issues + feature requests), per-org flagged
#
# One pipeline, two kinds. Client-facing submit/list is gated by the org's
# `requests` feature flag (checked server-side, not just hidden in the UI).
# Admins view per-org and manage status. Email notifications are deferred.
# ===============================================================

class CreateRequestRequest(BaseModel):
    kind: Literal["issue", "feature_request"]
    category: Optional[str] = Field(default=None, max_length=60)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    priority: Optional[Literal["low", "normal", "high"]] = None


RequestStatus = Literal[
    "open", "in_progress", "resolved", "closed",
    "under_consideration", "planned", "shipped", "declined",
]


class UpdateRequestStatusRequest(BaseModel):
    status: RequestStatus


async def _require_requests_enabled(conn, org_id) -> None:
    """403 unless the org has the `requests` feature flag on. Server-side gate;
    the hidden UI tab is not the boundary.
    """
    enabled = await conn.fetchval(
        "SELECT coalesce((features->>'requests')::boolean, false) "
        "FROM organizations WHERE id = $1",
        org_id,
    )
    if not enabled:
        raise HTTPException(
            status_code=403,
            detail="The Requests feature is not enabled for this organization.",
        )


def _serialize_request(r: dict, user_id=None) -> dict:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "category": r["category"],
        "subject": r["subject"],
        "body": r["body"],
        "priority": r["priority"],
        "status": r["status"],
        "created_at": r["created_at"].isoformat(),
        "mine": user_id is not None and r.get("created_by") is not None
                and str(r["created_by"]) == str(user_id),
    }


REQUEST_STATUS_EMAIL_LABEL = {
    "open": "Open", "in_progress": "In progress", "resolved": "Resolved", "closed": "Closed",
    "under_consideration": "Under consideration", "planned": "Planned",
    "shipped": "Shipped", "declined": "Declined",
}


async def _notify_new_request(org_id, kind: str, subject: str) -> None:
    """Email Locke staff who opted in that a client submitted a request.
    Best-effort: swallows errors so a Resend hiccup never affects the request.
    """
    try:
        async with admin_conn() as conn:
            org_name = await conn.fetchval(
                "SELECT name FROM organizations WHERE id = $1", org_id
            ) or "A client"
            recips = await conn.fetch(
                """
                SELECT DISTINCT u.email
                  FROM users u
                  JOIN memberships m ON m.user_id = u.id
                  JOIN organizations o ON o.id = m.org_id
                 WHERE o.is_internal = true AND m.status = 'active'
                   AND m.role IN ('locke_admin', 'locke_staff')
                   AND u.notify_requests = true AND u.email IS NOT NULL
                """,
            )
        if not recips:
            return
        kind_label = "feature request" if kind == "feature_request" else "issue"
        subj = f"New {kind_label} from {org_name}: {subject}"
        text = (f"{org_name} submitted a {kind_label}:\n\n{subject}\n\n"
                f"View and respond in the Locke portal.")
        html = (f"<p><strong>{_h(org_name)}</strong> submitted a {kind_label}:</p>"
                f"<p>{_h(subject)}</p><p>View and respond in the Locke portal.</p>")
        for r in recips:
            try:
                await send_email(to_email=r["email"], subject=subj, text=text, html=html)
            except Exception as exc:
                log.warning("notify.new_request.send_failed to=%s err=%s", r["email"], exc)
    except Exception as exc:
        log.warning("notify.new_request.failed err=%s", exc)


async def _notify_request_status(submitter_user_id, status: str, subject: str) -> None:
    """Email the request's submitter (if opted in) that its status changed."""
    if not submitter_user_id:
        return
    try:
        async with admin_conn() as conn:
            row = await conn.fetchrow(
                "SELECT email, notify_requests FROM users WHERE id = $1",
                submitter_user_id,
            )
        if not row or not row["notify_requests"] or not row["email"]:
            return
        label = REQUEST_STATUS_EMAIL_LABEL.get(status, status)
        subj = f"Your request '{subject}' is now {label}"
        text = (f"The status of your request '{subject}' changed to: {label}.\n\n"
                f"View it in your Locke portal.")
        html = (f"<p>The status of your request '<strong>{_h(subject)}</strong>' changed to: "
                f"<strong>{_h(label)}</strong>.</p><p>View it in your Locke portal.</p>")
        try:
            await send_email(to_email=row["email"], subject=subj, text=text, html=html)
        except Exception as exc:
            log.warning("notify.status.send_failed to=%s err=%s", row["email"], exc)
    except Exception as exc:
        log.warning("notify.status.failed err=%s", exc)


@app.get("/api/requests")
async def list_requests(kind: Optional[str] = None, user: dict = Depends(get_current_user)):
    """Client-facing: the caller's org requests (RLS-scoped). Flag-gated."""
    async with admin_conn() as conn:
        org = await _resolve_caller_upload_org(conn, user["id"])
        await _require_requests_enabled(conn, org["id"])
    async with user_conn(user["id"]) as conn:
        rows = await conn.fetch(
            """
            SELECT id, kind::text AS kind, category, subject, body, priority,
                   status::text AS status, created_by, created_at
              FROM requests
             ORDER BY created_at DESC
            """,
        )
    items = [_serialize_request(dict(r), user["id"]) for r in rows]
    if kind in ("issue", "feature_request"):
        items = [i for i in items if i["kind"] == kind]
    return {"requests": items}


@app.post("/api/requests")
async def create_request(
    payload: CreateRequestRequest,
    background: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Client-facing submit. Org derived from the caller; flag-gated."""
    category = payload.category if payload.kind == "issue" else None
    async with admin_conn() as conn:
        org = await _resolve_caller_upload_org(conn, user["id"])
        await _require_requests_enabled(conn, org["id"])
        row = await conn.fetchrow(
            """
            INSERT INTO requests (org_id, kind, category, subject, body, priority, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, kind::text AS kind, category, subject, body, priority,
                      status::text AS status, created_by, created_at
            """,
            org["id"], payload.kind, category, payload.subject, payload.body,
            payload.priority, user["id"],
        )

    await _audit(
        actor_user_id=user["id"], action="request.created",
        resource_type="request", resource_id=row["id"], org_id=org["id"],
        metadata={"kind": payload.kind, "subject": payload.subject},
    )
    # Email opted-in Locke staff after the response is sent (best-effort).
    background.add_task(_notify_new_request, org["id"], payload.kind, payload.subject)
    return _serialize_request(dict(row), user["id"])


@app.get("/api/admin/orgs/{org_id}/requests")
async def admin_list_org_requests(org_id: str, admin: dict = Depends(require_locke_admin)):
    """Admin per-org list (all requests for the org, with submitter email)."""
    async with admin_conn() as conn:
        org = await _load_active_org(conn, org_id)
        rows = await conn.fetch(
            """
            SELECT r.id, r.kind::text AS kind, r.category, r.subject, r.body,
                   r.priority, r.status::text AS status, r.created_at,
                   u.email AS submitter_email
              FROM requests r
              LEFT JOIN users u ON u.id = r.created_by
             WHERE r.org_id = $1
             ORDER BY r.created_at DESC
            """,
            org["id"],
        )
    return {
        "requests": [
            {
                "id": str(r["id"]),
                "kind": r["kind"],
                "category": r["category"],
                "subject": r["subject"],
                "body": r["body"],
                "priority": r["priority"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
                "submitter_email": r["submitter_email"],
            }
            for r in rows
        ],
    }


@app.patch("/api/admin/requests/{request_id}")
async def admin_update_request_status(
    request_id: str,
    payload: UpdateRequestStatusRequest,
    background: BackgroundTasks,
    admin: dict = Depends(require_locke_admin),
):
    """Locke updates a request's status (any org)."""
    try:
        req_uuid = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Request not found")

    async with admin_conn() as conn:
        row = await conn.fetchrow(
            """
            UPDATE requests SET status = $2::request_status
             WHERE id = $1
            RETURNING id, org_id, kind::text AS kind, subject, created_by
            """,
            req_uuid, payload.status,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")

    await _audit(
        actor_user_id=admin["id"], action="request.status_changed",
        resource_type="request", resource_id=row["id"], org_id=row["org_id"],
        metadata={"status": payload.status, "kind": row["kind"]},
    )
    # Email the submitter (if opted in) after the response is sent.
    background.add_task(_notify_request_status, row["created_by"], payload.status, row["subject"])
    return {"ok": True, "id": str(row["id"]), "status": payload.status}


@app.post("/webhooks/clerk")
async def clerk_webhook(request: Request):
    """Receive Clerk events and sync them into Supabase.

    The endpoint is unauthenticated at the HTTP layer; svix signature
    verification is the auth. Returns 200 even on handler errors (with
    the error logged) so Clerk doesn't retry forever on a bad event.
    """
    body = await request.body()
    # svix expects these specific header names
    headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    event = verify_webhook(headers, body)
    try:
        await handle_webhook_event(event)
    except Exception as exc:
        # Log loudly, but acknowledge so Clerk doesn't retry indefinitely.
        # Real failures should surface via Sentry once it's wired in Phase 4.
        log.exception("clerk.webhook.handler_failed type=%s err=%s",
                      event.get("type"), exc)
    return {"ok": True}
