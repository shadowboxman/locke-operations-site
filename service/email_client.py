"""Resend email client.

Direct HTTP calls (no SDK) to keep the dependency surface tiny. The Resend
attachment API takes base64-encoded content; we encode the PDF bytes once
per send. Idempotency keys are passed through so retries don't double-send.
"""

from __future__ import annotations

import base64
import logging
import os
import uuid

import httpx

log = logging.getLogger(__name__)


def _clean_env(name: str, default: str | None = None) -> str:
    """Read an env var and strip the things people commonly paste by accident:
    whitespace at either end, surrounding double or single quotes, and (defensively)
    a leading 'Bearer ' prefix on tokens. Cheap defense against secret-store paste bugs.
    """
    raw = os.environ.get(name, default)
    if raw is None:
        raise KeyError(name)
    cleaned = raw.strip().strip('"').strip("'").strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


RESEND_API_KEY = _clean_env("RESEND_API_KEY")
RESEND_FROM_EMAIL = _clean_env("RESEND_FROM_EMAIL", "Locke Operations <hello@lockeoperations.com>")
RESEND_REPLY_TO = _clean_env("RESEND_REPLY_TO", "hello@lockeoperations.com")
RESEND_BCC = os.environ.get("RESEND_BCC")  # optional, e.g. for archival
if RESEND_BCC:
    RESEND_BCC = RESEND_BCC.strip().strip('"').strip("'")

# Boot-time visibility — masked so we never leak the key, but we can confirm what
# Railway actually loaded. Compare 'len' and the prefix/suffix against your
# password manager copy if 401s persist.
_masked = (
    f"{RESEND_API_KEY[:4]}...{RESEND_API_KEY[-4:]}"
    if len(RESEND_API_KEY) >= 8
    else "<too-short>"
)
log.info(
    "resend.env loaded key_len=%d key_preview=%s from=%r",
    len(RESEND_API_KEY), _masked, RESEND_FROM_EMAIL,
)

RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_email(
    *,
    to_email: str,
    subject: str,
    text: str,
    html: str,
    idempotency_key: str | None = None,
) -> str:
    """Generic transactional send (no attachment). Returns the Resend message ID.

    Used for product notifications (e.g. request alerts). Auth/identity emails
    go through Clerk, not here.
    """
    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "reply_to": RESEND_REPLY_TO,
        "subject": subject,
        "text": text,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    if r.status_code >= 300:
        log.error("resend.send failed status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
    msg_id = r.json().get("id", "<no-id>")
    log.info("resend.send ok to=%s id=%s subject=%r", to_email, msg_id, subject)
    return msg_id


def _email_body(first_name: str, midpoint_formatted: str) -> dict:
    """Plain-text + HTML versions of the email body. Mirrors playbook 06."""
    text = (
        f"{first_name},\n\n"
        f"Here's your full assessment. The headline: your business is sitting on "
        f"roughly {midpoint_formatted} per year in admin work that could be automated.\n\n"
        f"The attached PDF walks through where that number comes from, what the top "
        f"three opportunities are, and what we'd build first if we were starting today.\n\n"
        f"If you'd like a 30-minute fit call to walk through it, reply to this email "
        f"with a couple of times that work for you. No pressure either way.\n\n"
        f"Dan\n"
        f"Locke Operations\n"
    )
    html = (
        f"<p>{first_name},</p>"
        f"<p>Here's your full assessment. The headline: your business is sitting on "
        f"roughly <strong>{midpoint_formatted} per year</strong> in admin work that "
        f"could be automated.</p>"
        f"<p>The attached PDF walks through where that number comes from, what the "
        f"top three opportunities are, and what we'd build first if we were starting today.</p>"
        f"<p>If you'd like a 30-minute fit call to walk through it, reply to this "
        f"email with a couple of times that work for you. No pressure either way.</p>"
        f"<p>Dan<br>Locke Operations</p>"
    )
    return {"text": text, "html": html}


async def send_assessment_email(
    *,
    to_email: str,
    first_name: str,
    midpoint_formatted: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    idempotency_key: str | None = None,
) -> str:
    """Send the assessment results email with PDF attached. Returns Resend message ID."""
    body = _email_body(first_name, midpoint_formatted)
    payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "reply_to": RESEND_REPLY_TO,
        "subject": "Your Locke Operations assessment results",
        "text": body["text"],
        "html": body["html"],
        "attachments": [
            {
                "filename": pdf_filename,
                "content": base64.b64encode(pdf_bytes).decode("ascii"),
                "content_type": "application/pdf",
            }
        ],
    }
    if RESEND_BCC:
        payload["bcc"] = [RESEND_BCC]

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
        # Resend honors Idempotency-Key to prevent duplicate sends on retry.
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    if r.status_code >= 300:
        log.error("resend.send failed status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
    data = r.json()
    msg_id = data.get("id", "<no-id>")
    log.info("resend.send ok to=%s id=%s", to_email, msg_id)
    return msg_id
