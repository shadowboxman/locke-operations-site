"""HubSpot CRM proxy for the admin contacts surface.

Design (decided 2026-07-20): HubSpot is the ONLY source of truth for
contacts. This module is a thin live proxy over the CRM v3 API plus a
read-only snapshot in Postgres used exclusively as a fallback when the
HubSpot API is unreachable. The snapshot is written by the snapshot job
and never edited by anything else — it is a cache, not a mirror.

Auth: HUBSPOT_PRIVATE_APP_TOKEN (per Railway environment; staging token →
dev portal 246275387, production token → prod portal 246274191). Scopes:
crm.objects.contacts.read, crm.objects.contacts.write,
crm.schemas.contacts.read. If unset, endpoints return 503 and the
snapshot job no-ops.

Stage model: custom contact property `locke_stage` mirrors the internal
engagement arc (playbook 00). Values are stable internal keys; labels are
what HubSpot displays. `locke_notes` is a free-text scratch field.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from db import admin_conn

log = logging.getLogger("locke.hubspot_crm")

HUBSPOT_TOKEN = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "").strip()
API = "https://api.hubapi.com"

# Canonical engagement stages, in funnel order. Key = internal value stored
# in HubSpot; label = display. Adding a stage: add here AND to the
# locke_stage property options in BOTH portals.
STAGES: list[tuple[str, str]] = [
    ("lead", "Lead"),
    ("assessed", "Assessed"),
    ("fit_call", "Fit call"),
    ("audit", "Audit"),
    ("build", "Build"),
    ("run", "Run"),
    ("lost", "Lost"),
]
STAGE_KEYS = [k for k, _ in STAGES]

# Properties the admin surface reads.
READ_PROPERTIES = [
    "email", "firstname", "lastname", "company", "phone",
    "locke_stage", "locke_notes",
    "assessment_industry", "assessment_readiness_band",
    "assessment_readiness_score", "assessment_annual_midpoint",
    "createdate", "lastmodifieddate",
]

# Properties the admin surface may WRITE. Assessment properties are
# deliberately absent: they are measurements, not opinions.
EDITABLE_PROPERTIES = {
    "email", "firstname", "lastname", "company", "phone",
    "locke_stage", "locke_notes",
}


class HubSpotNotConfigured(Exception):
    pass


class HubSpotUnavailable(Exception):
    """Network / 5xx failure talking to HubSpot. Callers may fall back
    to the snapshot."""


def _client() -> httpx.AsyncClient:
    if not HUBSPOT_TOKEN:
        raise HubSpotNotConfigured()
    return httpx.AsyncClient(
        base_url=API,
        headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"},
        timeout=15.0,
    )


async def _request(method: str, path: str, **kwargs) -> dict[str, Any]:
    try:
        async with _client() as c:
            r = await c.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        log.warning("hubspot_crm.unreachable %s %s: %s", method, path, exc)
        raise HubSpotUnavailable(str(exc)) from exc
    if r.status_code >= 500:
        log.warning("hubspot_crm.5xx %s %s: %s", method, path, r.status_code)
        raise HubSpotUnavailable(f"HubSpot {r.status_code}")
    if r.status_code == 404:
        raise KeyError(path)
    if r.status_code >= 400:
        # 4xx is a caller error (bad property, dup email, rate limit) —
        # surface the message, don't fall back to snapshot.
        detail = r.json().get("message", r.text[:300]) if r.text else str(r.status_code)
        raise ValueError(detail)
    if r.status_code == 204 or not r.text:
        return {}
    return r.json()


def _slim(contact: dict[str, Any]) -> dict[str, Any]:
    p = contact.get("properties", {})
    return {
        "id": contact.get("id"),
        **{k: p.get(k) for k in READ_PROPERTIES},
    }


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------

async def list_contacts(q: str | None = None, stage: str | None = None,
                        after: str | None = None, limit: int = 50) -> dict[str, Any]:
    body: dict[str, Any] = {
        "properties": READ_PROPERTIES,
        "limit": min(max(limit, 1), 100),
        "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
    }
    if q:
        body["query"] = q
    if stage:
        body["filterGroups"] = [{"filters": [
            {"propertyName": "locke_stage", "operator": "EQ", "value": stage}
        ]}]
    if after:
        body["after"] = after
    data = await _request("POST", "/crm/v3/objects/contacts/search", json=body)
    return {
        "source": "hubspot",
        "total": data.get("total"),
        "results": [_slim(c) for c in data.get("results", [])],
        "after": (data.get("paging") or {}).get("next", {}).get("after"),
    }


async def get_contact(contact_id: str) -> dict[str, Any]:
    data = await _request(
        "GET", f"/crm/v3/objects/contacts/{contact_id}",
        params={"properties": ",".join(READ_PROPERTIES)},
    )
    return _slim(data)


def _clean_writes(properties: dict[str, Any]) -> dict[str, str]:
    unknown = set(properties) - EDITABLE_PROPERTIES
    if unknown:
        raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")
    if "locke_stage" in properties and properties["locke_stage"] not in (*STAGE_KEYS, "", None):
        raise ValueError(f"Unknown stage: {properties['locke_stage']}")
    return {k: ("" if v is None else str(v)) for k, v in properties.items()}


async def create_contact(properties: dict[str, Any]) -> dict[str, Any]:
    props = _clean_writes(properties)
    props.setdefault("locke_stage", "lead")
    data = await _request("POST", "/crm/v3/objects/contacts", json={"properties": props})
    return _slim(data)


async def update_contact(contact_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    props = _clean_writes(properties)
    data = await _request(
        "PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props}
    )
    return _slim(data)


async def archive_contact(contact_id: str) -> None:
    await _request("DELETE", f"/crm/v3/objects/contacts/{contact_id}")


# ------------------------------------------------------------------
# Full fetch (funnel + snapshot share it)
# ------------------------------------------------------------------

async def _fetch_all(max_pages: int = 50) -> list[dict[str, Any]]:
    """Page through every contact (100/page, capped at max_pages)."""
    out: list[dict[str, Any]] = []
    after: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": 100, "properties": ",".join(READ_PROPERTIES)}
        if after:
            params["after"] = after
        data = await _request("GET", "/crm/v3/objects/contacts", params=params)
        out.extend(_slim(c) for c in data.get("results", []))
        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    else:
        log.warning("hubspot_crm.fetch_all hit page cap; funnel/snapshot truncated")
    return out


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def funnel() -> dict[str, Any]:
    contacts = await _fetch_all()
    stages = {k: {"key": k, "label": lbl, "count": 0, "pipeline_value": 0}
              for k, lbl in STAGES}
    unstaged = 0
    bands: dict[str, int] = {}
    industries: dict[str, int] = {}
    for c in contacts:
        s = c.get("locke_stage")
        if s in stages:
            stages[s]["count"] += 1
            stages[s]["pipeline_value"] += _num(c.get("assessment_annual_midpoint"))
        else:
            unstaged += 1
        band = c.get("assessment_readiness_band")
        if band:
            bands[band] = bands.get(band, 0) + 1
        ind = c.get("assessment_industry")
        if ind:
            industries[ind] = industries.get(ind, 0) + 1
    return {
        "total": len(contacts),
        "unstaged": unstaged,
        "stages": [stages[k] for k in STAGE_KEYS],
        "readiness_bands": bands,
        "industries": industries,
    }


# ------------------------------------------------------------------
# Snapshot (read-only fallback; written ONLY here)
# ------------------------------------------------------------------

async def run_snapshot() -> dict[str, Any]:
    if not HUBSPOT_TOKEN:
        log.info("snapshot.skipped reason=no_token")
        return {"ok": False, "reason": "not configured"}
    contacts = await _fetch_all()
    now = datetime.now(timezone.utc)
    async with admin_conn() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM hubspot_contacts_snapshot")
            await conn.executemany(
                """
                INSERT INTO hubspot_contacts_snapshot (hubspot_id, properties, snapshot_at)
                VALUES ($1, $2::jsonb, $3)
                """,
                [(c["id"], json.dumps(c), now) for c in contacts],
            )
    log.info("snapshot.ok contacts=%d", len(contacts))
    return {"ok": True, "contacts": len(contacts), "snapshot_at": now.isoformat()}


async def snapshot_fallback() -> dict[str, Any]:
    async with admin_conn() as conn:
        rows = await conn.fetch(
            "SELECT properties, snapshot_at FROM hubspot_contacts_snapshot "
            "ORDER BY properties->>'createdate' DESC NULLS LAST"
        )
    results = [json.loads(r["properties"]) for r in rows]
    snapshot_at = rows[0]["snapshot_at"].isoformat() if rows else None
    return {"source": "snapshot", "snapshot_at": snapshot_at,
            "total": len(results), "results": results, "after": None}
