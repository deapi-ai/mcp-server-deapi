"""Internal completion webhooks: the API calls this server back when a job ends.

With DEAPI_WEBHOOKS_INTERNAL on, every job this server submits carries our own
receiver URL, and the tool that submitted it waits on the callback instead of
polling the status endpoint every few seconds. The callback is only a wake-up
signal: on receiving it the tool makes one final, authoritative status request,
because the webhook payload carries `result_url` but not inline `result`
(e.g. a transcript), and reusing the status path keeps the two modes identical.

Polling continues underneath at a much lower rate as a safety net, so a lost
or misrouted callback costs latency, never correctness.

Contract, from depin-shared `WebhookDeliveryService` / `WebhookService`:

    POST <webhook_url>
    Content-Type:        application/json
    X-DeAPI-Signature:   sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")>
    X-DeAPI-Timestamp:   <unix seconds>
    X-DeAPI-Event:       job.processing | job.completed | job.failed
    X-DeAPI-Delivery-Id: <id>

    {"event": ..., "delivery_id": ..., "timestamp": <iso8601>,
     "data": {"job_request_id": <uuid>, "status": ..., ...}}

Any 2xx counts as delivered; anything else is retried up to 10 times.

Known limitation: the job registry is in-process. With more than one replica a
callback can land on a pod that is not waiting for it, in which case that job
falls back to the safety-net poll. The upgrade path is a shared pub/sub
(e.g. Redis) behind the same JobCompletionRegistry interface.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

logger = logging.getLogger(__name__)

WEBHOOK_ROUTE = "/webhooks/deapi"

# Events that mean the job has reached a terminal state. `job.processing` is
# also delivered but says nothing a waiting tool can act on.
TERMINAL_EVENTS = frozenset({"job.completed", "job.failed"})

# How long a registry entry survives after its last touch. Long enough to outlive
# the longest polling timeout (video: 900s) so a legitimately slow job is never
# forgotten, short enough that abandoned entries do not accumulate.
REGISTRY_ENTRY_TTL = 3600.0


class JobCompletionRegistry:
    """Maps job ids to events that a waiting tool can block on.

    Order-independent by design: the callback can arrive before the submitting
    tool has learned its job id (the API assigns it in the submit response, and
    a fast job could finish in the gap). Both `track` and `signal` therefore
    create the entry if it is missing, and a signal that arrives first is not
    lost — the later `track` finds the event already set.
    """

    def __init__(self) -> None:
        self._events: Dict[str, asyncio.Event] = {}
        self._touched: Dict[str, float] = {}

    def track(self, job_id: str) -> asyncio.Event:
        """Register interest in a job and return the event to wait on."""
        self._sweep()
        event = self._events.setdefault(job_id, asyncio.Event())
        self._touched[job_id] = time.monotonic()
        return event

    def signal(self, job_id: str) -> None:
        """Mark a job as finished, waking any waiter."""
        self._sweep()
        event = self._events.setdefault(job_id, asyncio.Event())
        self._touched[job_id] = time.monotonic()
        event.set()

    def is_tracked(self, job_id: str) -> bool:
        return job_id in self._events

    def forget(self, job_id: str) -> None:
        """Drop a job once its tool has returned."""
        self._events.pop(job_id, None)
        self._touched.pop(job_id, None)

    def _sweep(self) -> None:
        cutoff = time.monotonic() - REGISTRY_ENTRY_TTL
        stale = [job_id for job_id, touched in self._touched.items() if touched < cutoff]
        for job_id in stale:
            self.forget(job_id)


registry = JobCompletionRegistry()


def internal_webhooks_enabled() -> bool:
    return bool(settings.webhooks_internal and settings.webhook_receiver_url)


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Produce the value the API puts in X-DeAPI-Signature.

    Mirrors `WebhookDeliveryService::signPayload`: HMAC-SHA256 over the
    timestamp, a literal ".", and the body bytes exactly as sent.
    """
    message = timestamp.encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    timestamp: Optional[str],
    signature: Optional[str],
    raw_body: bytes,
    tolerance: float,
    now: Optional[float] = None,
) -> bool:
    """Check a callback's signature and freshness.

    The body must be the raw bytes received, not a re-serialisation: the API
    signed what it sent, and any re-encoding (key order, whitespace, unicode
    escaping) breaks the match.
    """
    if not timestamp or not signature:
        return False

    try:
        sent_at = float(timestamp)
    except ValueError:
        return False

    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance:
        return False

    expected = compute_signature(secret, timestamp, raw_body)
    return hmac.compare_digest(expected, signature)


async def webhook_receiver(request: Request) -> JSONResponse:
    """Starlette endpoint the API posts job events to.

    Returns 2xx for everything it accepts, including events for jobs it does
    not know about (a different replica may be waiting, and a retry from the
    API would not change that) and non-terminal events. Only a bad signature is
    refused, and that is the one case where the API retrying is harmless.
    """
    secret = settings.webhook_secret
    if not internal_webhooks_enabled() or not secret:
        # The route is only mounted when enabled, so this is belt and braces.
        return JSONResponse({"error": "webhook receiver not enabled"}, status_code=404)

    raw_body = await request.body()
    if not verify_signature(
        secret,
        request.headers.get("X-DeAPI-Timestamp"),
        request.headers.get("X-DeAPI-Signature"),
        raw_body,
        settings.webhook_timestamp_tolerance,
    ):
        logger.warning(
            "Rejected webhook with bad signature (delivery %s)",
            request.headers.get("X-DeAPI-Delivery-Id"),
        )
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        payload = json.loads(raw_body)
        event = payload.get("event") or request.headers.get("X-DeAPI-Event")
        job_id = payload["data"]["job_request_id"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    if event in TERMINAL_EVENTS:
        registry.signal(job_id)
        logger.debug("Webhook %s for job %s: waiter signalled", event, job_id)
    else:
        logger.debug("Webhook %s for job %s: not terminal, ignored", event, job_id)

    return JSONResponse({"received": True})
