"""Internal completion webhooks: the API calls this server back when a job ends.

With DEAPI_WEBHOOKS_INTERNAL on, every job this server submits carries our own
receiver URL, and the tool that submitted it waits on the callback instead of
polling the status endpoint every few seconds. The callback is only a wake-up
signal: on receiving it the tool makes one final, authoritative status request,
because the webhook payload carries `result_url` but not inline `result`
(e.g. a transcript), and reusing the status path keeps the two modes identical.

Polling continues underneath at a much lower rate as a safety net, so a lost
or misrouted callback costs latency, never correctness.

Contract, from the deAPI webhook delivery scheme:

    POST <webhook_url>
    Content-Type:        application/json
    X-DeAPI-Signature:   sha256=<hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")>
    X-DeAPI-Timestamp:   <unix seconds>
    X-DeAPI-Event:       job.processing | job.completed | job.failed
    X-DeAPI-Delivery-Id: <id>

    {"event": ..., "delivery_id": ..., "timestamp": <iso8601>,
     "data": {"job_request_id": <uuid>, "status": ..., ...}}

Any 2xx counts as delivered; anything else is retried up to 10 times.

The job registry is pluggable. The default, ``JobCompletionRegistry``, lives in
process memory: with more than one replica a callback can land on a replica
that is not waiting for it, and that job then completes on the safety-net poll.
A deployment with several replicas passes a shared implementation of
``JobRegistry`` to ``create_app(server, job_registry=...)``, for example one that
relays signals over a pub/sub channel.

Deliveries are deduplicated on ``delivery_id``: a signature stays valid for the
whole timestamp tolerance window, so the same delivery can arrive again. Only
the first terminal delivery wakes the waiter.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from typing import Dict, Optional, Protocol

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

# Real callbacks are a few hundred bytes. The cap applies before the signature is
# checked, so an unauthenticated sender cannot make us buffer an arbitrary body.
MAX_BODY_BYTES = 64 * 1024


# Upper bound on remembered delivery ids in the in-process registry. Reached only
# under a flood of distinct signed deliveries; the oldest ids are dropped first.
MAX_REMEMBERED_DELIVERIES = 10_000


class JobRegistry(Protocol):
    """What the client, the poller and the receiver need from a job registry.

    ``track``, ``is_tracked`` and ``forget`` run on the replica that submitted the
    job and waits for it. ``signal`` runs on the replica that received the
    callback, which may be another one: a shared implementation relays it to
    every replica and sets ``track``'s event there. ``claim_delivery`` returns
    True the first time it sees a delivery id and False afterwards.

    ``start`` and ``stop`` run with the app's lifespan while webhooks are on, so an
    implementation can open and close its connections there.
    """

    def track(self, job_id: str) -> asyncio.Event: ...

    def is_tracked(self, job_id: str) -> bool: ...

    def forget(self, job_id: str) -> None: ...

    async def signal(self, job_id: str) -> None: ...

    async def claim_delivery(self, delivery_id: str) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class JobCompletionRegistry:
    """The default ``JobRegistry``: job ids mapped to events, in process memory.

    Order-independent by design: the callback can arrive before the submitting
    tool has learned its job id (the API assigns it in the submit response, and
    a fast job could finish in the gap). Both `track` and `wake` therefore
    create the entry if it is missing, and a signal that arrives first is not
    lost — the later `track` finds the event already set.

    A shared implementation can subclass it, keep the local events, relay
    ``signal`` to other replicas, and call ``wake`` where the relay arrives.
    """

    def __init__(
        self,
        *,
        delivery_ttl: Optional[float] = None,
        max_deliveries: int = MAX_REMEMBERED_DELIVERIES,
    ) -> None:
        self._events: Dict[str, asyncio.Event] = {}
        self._touched: Dict[str, float] = {}
        # delivery id -> monotonic expiry, oldest first
        self._deliveries: "OrderedDict[str, float]" = OrderedDict()
        self._delivery_ttl = delivery_ttl
        self._max_deliveries = max_deliveries

    def track(self, job_id: str) -> asyncio.Event:
        """Register interest in a job and return the event to wait on."""
        self._sweep()
        event = self._events.setdefault(job_id, asyncio.Event())
        self._touched[job_id] = time.monotonic()
        return event

    def wake(self, job_id: str) -> None:
        """Mark a job as finished in this process, waking any local waiter."""
        self._sweep()
        event = self._events.setdefault(job_id, asyncio.Event())
        self._touched[job_id] = time.monotonic()
        event.set()

    async def signal(self, job_id: str) -> None:
        """Mark a job as finished. In process memory this is ``wake``."""
        self.wake(job_id)

    async def claim_delivery(self, delivery_id: str) -> bool:
        """True the first time a delivery id is seen within the dedupe window.

        The window defaults to twice the signature tolerance: a replay older than
        the tolerance already fails the signature check, and twice covers clock
        skew in either direction.
        """
        now = time.monotonic()
        ttl = self._delivery_ttl
        if ttl is None:
            ttl = 2 * settings.webhook_timestamp_tolerance
        while self._deliveries:
            expires = next(iter(self._deliveries.values()))
            if expires > now:
                break
            self._deliveries.popitem(last=False)
        if delivery_id in self._deliveries:
            return False
        self._deliveries[delivery_id] = now + ttl
        while len(self._deliveries) > self._max_deliveries:
            self._deliveries.popitem(last=False)
        return True

    async def start(self) -> None:
        """Nothing to open in process memory."""

    async def stop(self) -> None:
        """Nothing to close in process memory."""

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


_job_registry: JobRegistry = JobCompletionRegistry()


def get_job_registry() -> JobRegistry:
    """The registry the client, the poller and the receiver use."""
    return _job_registry


def set_job_registry(job_registry: JobRegistry) -> None:
    """Replace the registry. ``create_app(job_registry=...)`` calls this at startup."""
    global _job_registry
    _job_registry = job_registry


def internal_webhooks_enabled() -> bool:
    return bool(settings.webhooks_internal and settings.webhook_receiver_url)


def compute_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Produce the value the API puts in X-DeAPI-Signature.

    Mirrors the deAPI webhook signing scheme: HMAC-SHA256 over the
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

    # Compare bytes: compare_digest raises TypeError on a non-ASCII str, which would be a 500.
    expected = compute_signature(secret, timestamp, raw_body).encode("ascii")
    received = signature if isinstance(signature, bytes) else signature.encode("utf-8", "replace")
    return hmac.compare_digest(expected, received)


async def read_body_limited(request: Request, limit: int) -> Optional[bytes]:
    """Read the request body, or return None as soon as it exceeds `limit` bytes.

    Checks Content-Length first, then counts while streaming, so an oversized or
    unannounced chunked body is never buffered in full.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None

    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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

    raw_body = await read_body_limited(request, MAX_BODY_BYTES)
    if raw_body is None:
        logger.warning(
            "Rejected oversized webhook (delivery %s)", request.headers.get("X-DeAPI-Delivery-Id")
        )
        return JSONResponse({"error": "payload too large"}, status_code=413)

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
        delivery_id = payload.get("delivery_id") or request.headers.get("X-DeAPI-Delivery-Id")
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"error": "malformed payload"}, status_code=400)
    if not isinstance(job_id, str) or not job_id:
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    if event not in TERMINAL_EVENTS:
        logger.debug("Webhook %s for job %s: not terminal, ignored", event, job_id)
        return JSONResponse({"received": True})

    # The callback only wakes a waiter, which then reads the job status itself. A registry
    # failure is therefore logged and acknowledged: the job completes on the fallback poll,
    # and a retry from the API would not fix the registry.
    job_registry = get_job_registry()
    if isinstance(delivery_id, str) and delivery_id:
        try:
            first = await job_registry.claim_delivery(delivery_id)
        except Exception:
            logger.warning(
                "Could not check webhook delivery %s for reuse", delivery_id, exc_info=True
            )
            first = True
        if not first:
            logger.info("Webhook delivery %s for job %s already handled", delivery_id, job_id)
            return JSONResponse({"received": True, "duplicate": True})

    try:
        await job_registry.signal(job_id)
    except Exception:
        logger.warning(
            "Could not signal job %s; it completes on the fallback poll", job_id, exc_info=True
        )
    else:
        logger.debug("Webhook %s for job %s: waiter signalled", event, job_id)

    return JSONResponse({"received": True})
