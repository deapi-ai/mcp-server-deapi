"""Check a plain deAPI API key against the API before a connection is accepted.

A JWT issued by ``/token`` is checked by its signature. A plain key sent as
``Authorization: Bearer <key>`` has no signature to check, so without this module
any string is accepted: ``initialize`` and ``tools/list`` succeed and the first
tool call fails with a generic API error. A client cannot tell from that that its
key is wrong, and so never asks the user for a new one.

The check is one ``GET /account/balance`` with the key, an endpoint that costs
nothing. The verdict is cached per key, under an HMAC of the key with a
per-process random secret, so the raw key is never held as a cache key:

- 2xx, or 403 (the key authenticated but is not allowed this endpoint): valid,
  cached for ``VALID_TTL``;
- 401: invalid, cached for ``INVALID_TTL``;
- anything else (5xx, 429, a timeout, a network error): unknown. The key is let
  through, as before this check existed, so an API outage never locks users out
  of the server; the verdict is cached for ``UNKNOWN_TTL`` so an outage does not
  add a probe to every request.

Concurrent requests carrying the same key share one probe. Turn the whole check
off with ``DEAPI_VERIFY_API_KEYS=false``.
"""

import asyncio
import enum
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

import httpx

from . import __version__
from .config import settings
from .deapi_client import _shared_http_client

logger = logging.getLogger(__name__)

VALID_TTL = 600.0
INVALID_TTL = 60.0
UNKNOWN_TTL = 15.0
# Seconds one probe may take. The probe runs before the request is served, so it
# is never retried and must not stall a connection the way a tool's own call may.
PROBE_TIMEOUT = 5.0
# Distinct keys remembered at once, least recently used evicted first.
MAX_ENTRIES = 10_000

# Monotonic clock for verdict expiry; a seam the tests replace.
_clock = time.monotonic

# The endpoint the probe reads, relative to /api/<version>/. Same one get_balance uses.
PROBE_ENDPOINT = "account/balance"


class KeyStatus(enum.Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


_TTL = {
    KeyStatus.VALID: VALID_TTL,
    KeyStatus.INVALID: INVALID_TTL,
    KeyStatus.UNKNOWN: UNKNOWN_TTL,
}


@dataclass
class _Verdict:
    status: KeyStatus
    expires_at: float


class KeyVerifier:
    """Verdict cache plus single-flight probe, one per process."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._hmac_key = secrets.token_bytes(32)
        self._verdicts: "OrderedDict[str, _Verdict]" = OrderedDict()
        self._inflight: dict[str, "asyncio.Future[KeyStatus]"] = {}

    def _cache_key(self, api_key: str) -> str:
        return hmac.new(self._hmac_key, api_key.encode(), hashlib.sha256).hexdigest()

    def __len__(self) -> int:
        return len(self._verdicts)

    def clear(self) -> None:
        self._verdicts.clear()
        self._inflight.clear()

    def _cached(self, cache_key: str) -> Optional[KeyStatus]:
        verdict = self._verdicts.get(cache_key)
        if verdict is None:
            return None
        if verdict.expires_at <= _clock():
            del self._verdicts[cache_key]
            return None
        self._verdicts.move_to_end(cache_key)
        return verdict.status

    def _store(self, cache_key: str, status: KeyStatus) -> None:
        self._verdicts[cache_key] = _Verdict(status, _clock() + _TTL[status])
        self._verdicts.move_to_end(cache_key)
        while len(self._verdicts) > self.max_entries:
            self._verdicts.popitem(last=False)

    async def verify(self, api_key: str) -> KeyStatus:
        """The key's status, from the cache or from one probe shared by every caller."""
        cache_key = self._cache_key(api_key)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        probe = self._inflight.get(cache_key)
        if probe is None:
            probe = asyncio.ensure_future(self._probe_and_store(cache_key, api_key))
            self._inflight[cache_key] = probe

            def _done(future: "asyncio.Future[KeyStatus]") -> None:
                if self._inflight.get(cache_key) is future:
                    del self._inflight[cache_key]

            probe.add_done_callback(_done)

        # A caller that goes away (client disconnect) must not cancel the probe the
        # other callers are waiting on.
        return await asyncio.shield(probe)

    async def _probe_and_store(self, cache_key: str, api_key: str) -> KeyStatus:
        try:
            status = await probe_api_key(api_key)
        except Exception as exc:  # never let the check itself refuse a connection
            logger.warning("API key check failed unexpectedly: %s", type(exc).__name__)
            status = KeyStatus.UNKNOWN
        self._store(cache_key, status)
        return status


async def probe_api_key(api_key: str) -> KeyStatus:
    """Ask the API whether it accepts this key. Never raises for an HTTP or network error."""
    url = f"{settings.deapi_api_base_url}/api/{settings.deapi_api_version}/{PROBE_ENDPOINT}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": f"deapi-mcp/{__version__}",
    }
    try:
        response = await _shared_http_client().get(url, headers=headers, timeout=PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning(
            "API key check: deAPI unreachable (%s), letting the key through", type(exc).__name__
        )
        return KeyStatus.UNKNOWN

    code = response.status_code
    if 200 <= code < 300 or code == 403:
        return KeyStatus.VALID
    if code == 401:
        logger.info("API key check: deAPI rejected the key (401)")
        return KeyStatus.INVALID
    logger.warning("API key check: deAPI answered %d, letting the key through", code)
    return KeyStatus.UNKNOWN


_verifier: Optional[KeyVerifier] = None


def get_key_verifier() -> KeyVerifier:
    global _verifier
    if _verifier is None:
        _verifier = KeyVerifier()
    return _verifier


def set_key_verifier(verifier: Optional[KeyVerifier]) -> None:
    """Replace the process-wide verifier (``None`` resets it to a fresh default)."""
    global _verifier
    _verifier = verifier


async def is_api_key_rejected(api_key: str) -> bool:
    """True only when the check is on and the API positively refused the key."""
    if not settings.verify_api_keys:
        return False
    return await get_key_verifier().verify(api_key) is KeyStatus.INVALID
