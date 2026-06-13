"""E-signature provider abstraction.

`get_provider()` returns the configured adapter, or None when e-sign is not
configured (so the feature degrades gracefully, same pattern as HubSpot/
Turnstile). Add a provider by writing a new adapter module and a branch here.
"""

from __future__ import annotations

import functools
import os
from typing import Optional

from .base import (
    ESignatureProvider,
    ESignEvent,
    EnvelopeStatus,
    ProviderEnvelope,
    Signer,
)

__all__ = [
    "ESignatureProvider",
    "ESignEvent",
    "EnvelopeStatus",
    "ProviderEnvelope",
    "Signer",
    "get_provider",
]


@functools.lru_cache(maxsize=1)
def get_provider() -> Optional[ESignatureProvider]:
    name = os.environ.get("ESIGN_PROVIDER", "").strip().lower()
    if name == "signwell":
        from .signwell import SignWellProvider
        return SignWellProvider()
    # Unconfigured (or unknown): feature off.
    return None
