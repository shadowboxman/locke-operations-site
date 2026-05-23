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

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Locke Operations <hello@lockeoperations.com>")
RESEND_REPLY_TO = os.environ.get("RESEND_REPLY_TO", "hello@lockeoperations.com")
RESEND_BCC = os.environ.get("RESEND_BCC")  # optional, e.g. for archival

RESEND_ENDPOINT = "https://api.resend.com/emails"


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
