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
from typing import Literal

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
from clerk import get_current_user, handle_webhook_event, verify_webhook
from db import close_pool, init_pool, user_conn
from email_client import send_assessment_email
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
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
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
    return {"ok": True, "service": "locke-assessment", "version": app.version}


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
                   o.is_internal AS org_is_internal
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

    return {
        "user": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user["name"],
            "clerk_user_id": user["clerk_user_id"],
        },
        "primary": _serialize_membership(primary) if primary else None,
        "memberships": [_serialize_membership(r) for r in rows],
    }


def _serialize_membership(row: dict) -> dict:
    return {
        "role": row["role"],
        "status": row["status"],
        "org": {
            "id": str(row["org_id"]),
            "name": row["org_name"],
            "slug": row["org_slug"],
            "is_internal": row["org_is_internal"],
        },
    }


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
