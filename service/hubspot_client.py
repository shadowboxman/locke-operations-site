"""HubSpot Forms API client.

Uses the public Forms submission endpoint (no auth required), same as the
client-side code did before the cutover. This keeps the moving parts minimal:
no private app, no API key rotation, no rate-limit headaches. If we ever need
contact lookup or property writes outside a form context, swap to the v3 CRM
API with a private-app token.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

HUBSPOT_PORTAL_ID = os.environ["HUBSPOT_PORTAL_ID"].strip().strip('"').strip("'")
HUBSPOT_FORM_ID = os.environ["HUBSPOT_FORM_ID"].strip().strip('"').strip("'")
HUBSPOT_ENDPOINT = (
    f"https://api.hsforms.com/submissions/v3/integration/submit/"
    f"{HUBSPOT_PORTAL_ID}/{HUBSPOT_FORM_ID}"
)

# Optional second form for the website "contact us" message. The assessment
# form requires the scoring fields, so a plain contact note needs its own form.
# Unset until the form is created in HubSpot; submit_contact degrades to a
# clear error the caller treats as best-effort.
HUBSPOT_CONTACT_FORM_ID = (
    os.environ.get("HUBSPOT_CONTACT_FORM_ID", "").strip().strip('"').strip("'")
)
HUBSPOT_CONTACT_ENDPOINT = (
    f"https://api.hsforms.com/submissions/v3/integration/submit/"
    f"{HUBSPOT_PORTAL_ID}/{HUBSPOT_CONTACT_FORM_ID}"
    if HUBSPOT_CONTACT_FORM_ID else ""
)

INDUSTRY_MAP = {
    "trades": "Trades",
    "restoration": "Restoration",
    "hospitality": "Hospitality",
    "ae": "A&E",
    "other": "Other",
}
RESPONSE_MAP = {
    "fast": "Under 5 minutes",
    "hour": "Within an hour",
    "day": "Same day",
    "slow": "Next day or later",
}
BAND_MAP = {"high": "High", "mid": "Mid", "low": "Low"}


def build_payload(contact: dict, answers: dict, result: dict, page_uri: str | None = None) -> dict:
    """Mirror of buildHubSpotPayload() in /assessment.html. Server-side now."""
    top_wins = " | ".join(
        f"{w['name']} - ${w['value']:,}/yr" for w in (result.get("wins") or [])
    )
    fields = [
        {"name": "email",      "value": contact["email"]},
        {"name": "firstname",  "value": contact["first_name"]},
        {"name": "lastname",   "value": contact["last_name"]},
        {"name": "company",    "value": contact["company"]},
        {"name": "assessment_industry",                 "value": INDUSTRY_MAP.get(answers.get("industry"), "Other")},
        {"name": "assessment_team_size",                "value": answers.get("team", 0)},
        {"name": "assessment_selected_tasks",           "value": ", ".join(answers.get("tasks") or [])},
        {"name": "assessment_admin_hours_per_week",     "value": answers.get("hours", 0)},
        {"name": "assessment_loaded_hourly_rate",       "value": answers.get("rate", 0)},
        {"name": "assessment_response_time",            "value": RESPONSE_MAP.get(answers.get("response"), "")},
        {"name": "assessment_monthly_leads",            "value": answers.get("leads", 0)},
        {"name": "assessment_avg_lead_value",           "value": answers.get("value", 0)},
        {"name": "assessment_system_maturity_score",    "value": answers.get("maturity", 0)},
        {"name": "assessment_process_consistency_score","value": answers.get("consistency", 0)},
        {"name": "assessment_readiness_score",          "value": result["readiness"]},
        {"name": "assessment_readiness_band",           "value": BAND_MAP.get(result["band"], "")},
        {"name": "assessment_annual_midpoint",          "value": result["total"]},
        {"name": "assessment_low_estimate",             "value": result["low"]},
        {"name": "assessment_high_estimate",            "value": result["high"]},
        {"name": "assessment_top_wins",                 "value": top_wins},
    ]
    return {
        "fields": fields,
        "context": {
            "pageUri": page_uri or "https://www.lockeoperations.com/assessment.html",
            "pageName": "Locke Operations Assessment",
        },
    }


async def submit(contact: dict, answers: dict, result: dict, page_uri: str | None = None) -> dict[str, Any]:
    """POST a form submission to HubSpot. Raises on non-2xx."""
    payload = build_payload(contact, answers, result, page_uri)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(HUBSPOT_ENDPOINT, json=payload)
    if r.status_code >= 300:
        log.error("hubspot.submit failed status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
    log.info("hubspot.submit ok email=%s status=%s", contact.get("email"), r.status_code)
    return r.json() if r.text else {}


def build_contact_payload(contact: dict, message: str, page_uri: str | None = None) -> dict:
    """Form fields for a plain website contact submission."""
    return {
        "fields": [
            {"name": "email",     "value": contact["email"]},
            {"name": "firstname", "value": contact["first_name"]},
            {"name": "lastname",  "value": contact["last_name"]},
            {"name": "company",   "value": contact["company"]},
            {"name": "message",   "value": message},
        ],
        "context": {
            "pageUri": page_uri or "https://www.lockeoperations.com/#contact",
            "pageName": "Locke Operations Contact",
        },
    }


async def submit_contact(contact: dict, message: str, page_uri: str | None = None) -> dict[str, Any]:
    """POST a contact-form submission to the dedicated HubSpot contact form.

    Raises RuntimeError if HUBSPOT_CONTACT_FORM_ID is not configured, so the
    caller can treat HubSpot capture as best-effort while email is the
    guaranteed path. Raises on non-2xx from HubSpot.
    """
    if not HUBSPOT_CONTACT_ENDPOINT:
        raise RuntimeError("HUBSPOT_CONTACT_FORM_ID not configured")
    payload = build_contact_payload(contact, message, page_uri)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(HUBSPOT_CONTACT_ENDPOINT, json=payload)
    if r.status_code >= 300:
        log.error("hubspot.contact failed status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
    log.info("hubspot.contact ok email=%s status=%s", contact.get("email"), r.status_code)
    return r.json() if r.text else {}
