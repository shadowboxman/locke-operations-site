"""SignWell adapter.

Implements the ESignatureProvider interface against SignWell's REST API
(https://developers.signwell.com). v1 uses email signing from a SignWell
template (embedded signing is a later enhancement).

VERIFY-AGAINST-LIVE notes (SignWell docs are client-rendered and couldn't be
scraped at build time; confirm these on a SignWell test account before launch,
they are isolated here so a fix touches only this file):
  [V1] Create-from-template request fields (template_id vs template_ids,
       recipients[].placeholder_name) and the response id/status field names.
  [V2] Completed-PDF retrieval: GET /documents/{id}/ returns `completed_pdf_url`.
  [V3] Webhook authenticity: SignWell signs each event with an HMAC-SHA256 hash
       of `event.time` using your API key, delivered at payload.event.hash.
       Confirm the exact hashed value + algorithm.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, Optional

import httpx

from .base import (
    ESignatureProvider,
    ESignEvent,
    EnvelopeStatus,
    ProviderEnvelope,
    Signer,
)

log = logging.getLogger("locke.esign.signwell")

API_BASE = "https://www.signwell.com/api/v1"

# Template id per document type. Only NDA is wired for v1; MSA/SOW added later.
_TEMPLATE_ENV = {
    "nda": "SIGNWELL_NDA_TEMPLATE_ID",
    "msa": "SIGNWELL_MSA_TEMPLATE_ID",
    "sow": "SIGNWELL_SOW_TEMPLATE_ID",
}

# SignWell event type -> canonical status. [V3] confirm exact event-type strings.
_EVENT_MAP = {
    "document_created": EnvelopeStatus.DRAFT,
    "document_sent": EnvelopeStatus.SENT,
    "document_viewed": EnvelopeStatus.VIEWED,
    "document_signed": EnvelopeStatus.SIGNED,
    "document_completed": EnvelopeStatus.COMPLETED,
    "document_declined": EnvelopeStatus.DECLINED,
    "document_canceled": EnvelopeStatus.CANCELED,
    "document_expired": EnvelopeStatus.CANCELED,
}

# SignWell document.status -> canonical status, for the create response.
_STATUS_MAP = {
    "draft": EnvelopeStatus.DRAFT,
    "sent": EnvelopeStatus.SENT,
    "viewed": EnvelopeStatus.VIEWED,
    "signed": EnvelopeStatus.SIGNED,
    "completed": EnvelopeStatus.COMPLETED,
    "declined": EnvelopeStatus.DECLINED,
    "canceled": EnvelopeStatus.CANCELED,
}


class SignWellProvider(ESignatureProvider):
    name = "signwell"

    def __init__(self) -> None:
        self.api_key = os.environ.get("SIGNWELL_API_KEY", "").strip().strip('"').strip("'")
        # Test mode keeps documents out of billing while wiring things up.
        self.test_mode = os.environ.get("SIGNWELL_TEST_MODE", "false").strip().lower() in ("1", "true", "yes")
        if not self.api_key:
            log.warning("signwell.config.missing SIGNWELL_API_KEY not set")

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def _template_id(self, doc_type: str) -> str:
        env = _TEMPLATE_ENV.get(doc_type)
        tid = os.environ.get(env, "").strip() if env else ""
        if not tid:
            raise RuntimeError(f"No SignWell template configured for doc_type={doc_type} ({env})")
        return tid

    async def create_request(
        self,
        *,
        doc_type: str,
        signers: list[Signer],
        subject: Optional[str] = None,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ProviderEnvelope:
        # [V1] Create-from-template. Recipients map to template placeholders by
        # role; SignWell emails each recipient since embedded_signing is false.
        recipients = [
            {
                "id": str(i + 1),
                "placeholder_name": s.role,
                "name": s.name,
                "email": s.email,
            }
            for i, s in enumerate(signers)
        ]
        body: dict[str, Any] = {
            "test_mode": self.test_mode,
            "template_id": self._template_id(doc_type),
            "draft": False,
            "embedded_signing": False,
            "recipients": recipients,
        }
        if subject:
            body["subject"] = subject
        if message:
            body["message"] = message
        if metadata:
            # SignWell echoes custom metadata back on webhooks; handy for tracing.
            body["metadata"] = metadata

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{API_BASE}/document_templates/documents/",
                headers=self._headers(),
                json=body,
            )
        if r.status_code >= 300:
            log.error("signwell.create failed status=%s body=%s", r.status_code, r.text[:500])
            r.raise_for_status()
        data = r.json()
        external_id = data.get("id") or data.get("document_id") or ""
        status = _STATUS_MAP.get(str(data.get("status", "")).lower(), EnvelopeStatus.SENT)
        log.info("signwell.create ok id=%s status=%s", external_id, status.value)
        return ProviderEnvelope(external_id=external_id, status=status, raw=data)

    async def fetch_executed_pdf(self, external_id: str) -> bytes:
        # [V2] GET the document, follow completed_pdf_url to download the bytes.
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            meta = await client.get(f"{API_BASE}/documents/{external_id}/", headers=self._headers())
            if meta.status_code >= 300:
                log.error("signwell.fetch_meta failed status=%s body=%s", meta.status_code, meta.text[:300])
                meta.raise_for_status()
            url = meta.json().get("completed_pdf_url")
            if not url:
                raise RuntimeError(f"SignWell document {external_id} has no completed_pdf_url yet")
            pdf = await client.get(url)
            pdf.raise_for_status()
            return pdf.content

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> bool:
        # [V3] SignWell signs each event with HMAC-SHA256 of event.time using the
        # API key, delivered at payload.event.hash. Fail closed on any mismatch.
        if not self.api_key:
            return False
        import json
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            event = payload.get("event", {})
            supplied = event.get("hash", "")
            signed_value = str(event.get("time", ""))
        except Exception as exc:
            log.warning("signwell.webhook.parse_error err=%s", exc)
            return False
        if not supplied or not signed_value:
            return False
        expected = hmac.new(self.api_key.encode(), signed_value.encode(), hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expected, supplied)
        if not ok:
            log.warning("signwell.webhook.bad_signature")
        return ok

    def parse_event(self, payload: dict[str, Any]) -> Optional[ESignEvent]:
        event = payload.get("event", {}) or {}
        etype = str(event.get("type", "")).lower()
        status = _EVENT_MAP.get(etype)
        if status is None:
            return None
        obj = (payload.get("data", {}) or {}).get("object", {}) or {}
        external_id = obj.get("id") or obj.get("document_id") or ""
        return ESignEvent(status=status, external_id=external_id, raw=payload)
