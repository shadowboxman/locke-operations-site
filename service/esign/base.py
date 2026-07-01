"""Provider-agnostic e-signature interface.

Everything outside this package (endpoints, DB, webhook handler) speaks only
these canonical types. Each provider lives in its own adapter module and is the
only place that knows the provider's auth, payload shapes, webhook signing, and
event names. Switching providers = add an adapter + flip ESIGN_PROVIDER.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EnvelopeStatus(str, Enum):
    """Canonical lifecycle status, independent of any provider's vocabulary."""
    DRAFT = "draft"
    SENT = "sent"
    VIEWED = "viewed"
    SIGNED = "signed"          # a signer signed (multi-signer: not all done yet)
    COMPLETED = "completed"    # fully executed
    DECLINED = "declined"
    CANCELED = "canceled"
    ERROR = "error"


@dataclass
class Signer:
    email: str
    name: str
    role: str = "signer"       # maps to a provider template placeholder/role
    order: Optional[int] = None


@dataclass
class ProviderEnvelope:
    """Result of creating a signature request at the provider."""
    external_id: str
    status: EnvelopeStatus
    # email -> embedded signing URL, only populated when embedded signing is used.
    signing_urls: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ESignEvent:
    """A normalized webhook event."""
    status: EnvelopeStatus     # the canonical status this event implies
    external_id: str
    signer_email: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class ESignatureProvider(ABC):
    """Interface every e-signature provider adapter implements."""

    name: str = "base"

    @abstractmethod
    async def create_request(
        self,
        *,
        doc_type: str,
        signers: list[Signer],
        subject: Optional[str] = None,
        message: Optional[str] = None,
        fields: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ProviderEnvelope:
        """Create (and send) a signature request. Returns the envelope handle.

        `fields` is an optional map of document merge-field key -> value (e.g.
        counterparty_name -> "Acme LLC"). Adapters map these onto the provider's
        prefill/template fields. Keys must match the api_ids configured on the
        provider template. Empty/None values are ignored by the adapter."""

    @abstractmethod
    async def fetch_executed_pdf(self, external_id: str) -> bytes:
        """Download the fully-executed PDF (with audit trail) for filing."""

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> bool:
        """Verify a webhook callback is authentic before we act on it."""

    @abstractmethod
    def parse_event(self, payload: dict[str, Any]) -> Optional[ESignEvent]:
        """Normalize a provider webhook payload to an ESignEvent, or None if
        it's an event type we don't track."""

    # Optional capabilities; default to "unsupported".
    def get_signing_url(self, external_id: str, signer_email: str) -> Optional[str]:
        """Embedded signing URL for a signer, if the provider/flow supports it."""
        return None

    async def fetch_status(self, external_id: str) -> Optional[EnvelopeStatus]:
        """Authoritatively fetch the envelope's current status from the provider
        using our own credentials. Used to verify webhooks without trusting the
        payload signature. Returns None if unsupported or the envelope is gone."""
        return None

    async def cancel(self, external_id: str) -> None:
        raise NotImplementedError(f"{self.name} does not support cancel")

    async def send_reminder(self, external_id: str) -> None:
        raise NotImplementedError(f"{self.name} does not support reminders")
