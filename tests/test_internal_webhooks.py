"""Tests for internal completion webhooks (DEV-2019, part B).

With DEAPI_WEBHOOKS_INTERNAL on, the API calls this server back when a job
ends and the waiting tool wakes at once instead of polling every few seconds.
Polling continues underneath at a low rate as a safety net.

The signature contract is taken from the deAPI webhook delivery scheme:
    X-DeAPI-Signature = "sha256=" + hex(HMAC-SHA256(secret, f"{ts}.{raw_body}"))
"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from deapi_mcp import webhooks
from deapi_mcp.config import Settings, settings
from deapi_mcp.deapi_client import DeapiClient
from deapi_mcp.schemas import JobStatusData, JobStatusResponse
from deapi_mcp.webhooks import (
    WEBHOOK_ROUTE,
    JobCompletionRegistry,
    compute_signature,
    verify_signature,
    webhook_receiver,
)

SECRET = "s" * 40
RECEIVER = "https://mcp.example.com/webhooks/deapi"


@pytest.fixture
def internal_on(monkeypatch):
    """Enable internal webhooks on the shared settings singleton for one test."""
    monkeypatch.setattr(settings, "webhooks_internal", True)
    monkeypatch.setattr(settings, "webhook_receiver_url", RECEIVER)
    monkeypatch.setattr(settings, "webhook_secret", SECRET)
    monkeypatch.setattr(settings, "webhook_fallback_poll_interval", 5.0)
    yield


@pytest.fixture
def fresh_registry(monkeypatch):
    """Isolate the module-level registry so tests cannot leak jobs into each other."""
    reg = JobCompletionRegistry()
    monkeypatch.setattr(webhooks, "registry", reg)
    monkeypatch.setattr("deapi_mcp.deapi_client.registry", reg)
    monkeypatch.setattr("deapi_mcp.polling_manager.registry", reg)
    return reg


def signed_headers(
    body: bytes, secret: str = SECRET, ts: float | None = None, event="job.completed"
):
    ts_str = str(int(ts if ts is not None else time.time()))
    return {
        "X-DeAPI-Signature": compute_signature(secret, ts_str, body),
        "X-DeAPI-Timestamp": ts_str,
        "X-DeAPI-Event": event,
        "X-DeAPI-Delivery-Id": "d-1",
        "Content-Type": "application/json",
    }


def payload(job_id="job-1", event="job.completed") -> bytes:
    return json.dumps(
        {
            "event": event,
            "delivery_id": "d-1",
            "timestamp": "2026-09-12T10:00:00+00:00",
            "data": {"job_request_id": job_id, "status": "done", "job_type": "txt2img"},
        }
    ).encode()


# =============================================================================
# Signature
# =============================================================================


class TestSignature:

    def test_format_matches_php(self):
        """signPayload returns 'sha256=' + hex; the verifier must expect the same."""
        sig = compute_signature(SECRET, "1700000000", b'{"a":1}')
        assert sig.startswith("sha256=")
        assert len(sig) == len("sha256=") + 64

    def test_valid_signature_accepted(self):
        body = payload()
        ts = str(int(time.time()))
        assert verify_signature(SECRET, ts, compute_signature(SECRET, ts, body), body, 300)

    def test_tampered_body_rejected(self):
        """The body must be the raw bytes received; any change breaks the match."""
        body = payload()
        ts = str(int(time.time()))
        sig = compute_signature(SECRET, ts, body)
        assert not verify_signature(SECRET, ts, sig, body + b" ", 300)

    def test_wrong_secret_rejected(self):
        body = payload()
        ts = str(int(time.time()))
        sig = compute_signature("wrong" * 8, ts, body)
        assert not verify_signature(SECRET, ts, sig, body, 300)

    def test_stale_timestamp_rejected(self):
        """Replay protection: a valid signature on an old timestamp is refused."""
        body = payload()
        old = str(int(time.time()) - 600)
        sig = compute_signature(SECRET, old, body)
        assert not verify_signature(SECRET, old, sig, body, tolerance=300)

    def test_timestamp_within_tolerance_accepted(self):
        body = payload()
        recent = str(int(time.time()) - 60)
        sig = compute_signature(SECRET, recent, body)
        assert verify_signature(SECRET, recent, sig, body, tolerance=300)

    @pytest.mark.parametrize("sig", ["sha256=é" * 8, "\ud800", "sha256=日本語"])
    def test_non_ascii_signature_is_rejected_not_raised(self, sig):
        """compare_digest raises TypeError on a non-ASCII str; that must read as a bad signature."""
        ts = str(int(time.time()))
        assert verify_signature(SECRET, ts, sig, payload(), 300) is False

    @pytest.mark.parametrize(
        "ts,sig", [(None, "sha256=x"), ("123", None), ("not-a-number", "sha256=x")]
    )
    def test_missing_or_malformed_headers_rejected(self, ts, sig):
        assert not verify_signature(SECRET, ts, sig, b"{}", 300)


# =============================================================================
# Registry
# =============================================================================


class TestRegistry:

    @pytest.mark.asyncio
    async def test_track_then_signal_wakes_waiter(self):
        reg = JobCompletionRegistry()
        event = reg.track("j")
        assert not event.is_set()
        reg.signal("j")
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_signal_before_track_is_not_lost(self):
        """A fast job can finish before submit_job has returned the job id."""
        reg = JobCompletionRegistry()
        reg.signal("j")
        event = reg.track("j")
        assert event.is_set(), "early callback must be found already set"

    def test_forget_removes_entry(self):
        reg = JobCompletionRegistry()
        reg.track("j")
        assert reg.is_tracked("j")
        reg.forget("j")
        assert not reg.is_tracked("j")

    def test_stale_entries_are_swept(self, monkeypatch):
        reg = JobCompletionRegistry()
        reg.track("old")
        # Age the entry past the TTL, then touch the registry so it sweeps.
        reg._touched["old"] -= webhooks.REGISTRY_ENTRY_TTL + 1
        reg.track("new")
        assert not reg.is_tracked("old")
        assert reg.is_tracked("new")


# =============================================================================
# Receiver endpoint
# =============================================================================


class TestReceiver:

    @pytest.fixture
    def client(self, internal_on, fresh_registry):
        app = Starlette(routes=[Route(WEBHOOK_ROUTE, webhook_receiver, methods=["POST"])])
        return TestClient(app)

    def test_valid_terminal_event_signals_job(self, client, fresh_registry):
        body = payload("job-9")
        r = client.post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 200
        assert fresh_registry.is_tracked("job-9")
        assert fresh_registry.track("job-9").is_set()

    def test_failed_event_also_signals(self, client, fresh_registry):
        body = payload("job-f", event="job.failed")
        r = client.post(
            WEBHOOK_ROUTE, content=body, headers=signed_headers(body, event="job.failed")
        )
        assert r.status_code == 200
        assert fresh_registry.track("job-f").is_set()

    def test_processing_event_is_acknowledged_but_not_signalled(self, client, fresh_registry):
        """job.processing says nothing a waiter can act on; 2xx stops the API retrying."""
        body = payload("job-p", event="job.processing")
        r = client.post(
            WEBHOOK_ROUTE, content=body, headers=signed_headers(body, event="job.processing")
        )
        assert r.status_code == 200
        assert not fresh_registry.is_tracked("job-p")

    def test_bad_signature_is_401(self, client, fresh_registry):
        body = payload("job-x")
        headers = signed_headers(body, secret="wrong" * 8)
        r = client.post(WEBHOOK_ROUTE, content=body, headers=headers)
        assert r.status_code == 401
        assert not fresh_registry.is_tracked("job-x")

    def test_non_ascii_signature_header_is_401_not_500(self, client, fresh_registry):
        body = payload("job-n")
        headers = signed_headers(body)
        headers["X-DeAPI-Signature"] = "sha256=é".encode("latin-1")
        r = client.post(WEBHOOK_ROUTE, content=body, headers=headers)
        assert r.status_code == 401
        assert not fresh_registry.is_tracked("job-n")

    def test_oversized_declared_body_is_413(self, client):
        body = b"x" * (webhooks.MAX_BODY_BYTES + 1)
        r = client.post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 413

    def test_body_at_limit_is_still_verified(self, client):
        """The cap is not an early 401/400: a signed body exactly at the limit is accepted."""
        pad = webhooks.MAX_BODY_BYTES - len(payload("job-L"))
        body = payload("job-L")[:-1] + b" " * pad + b"}"
        assert len(body) == webhooks.MAX_BODY_BYTES
        r = client.post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 200

    def test_stale_timestamp_is_401(self, client):
        body = payload()
        r = client.post(
            WEBHOOK_ROUTE, content=body, headers=signed_headers(body, ts=time.time() - 900)
        )
        assert r.status_code == 401

    def test_malformed_payload_is_400(self, client):
        body = b'{"event": "job.completed"}'  # no data.job_request_id
        r = client.post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 400

    def test_unknown_job_is_still_200(self, client):
        """Another replica may be waiting; a retry from the API cannot help."""
        body = payload("never-tracked")
        r = client.post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 200

    def test_disabled_receiver_is_404(self, monkeypatch, fresh_registry):
        monkeypatch.setattr(settings, "webhooks_internal", False)
        app = Starlette(routes=[Route(WEBHOOK_ROUTE, webhook_receiver, methods=["POST"])])
        body = payload()
        r = TestClient(app).post(WEBHOOK_ROUTE, content=body, headers=signed_headers(body))
        assert r.status_code == 404


class TestReadBodyLimited:

    @staticmethod
    def fake_request(chunks, headers=None):
        consumed = []

        async def stream():
            for chunk in chunks:
                consumed.append(chunk)
                yield chunk

        request = SimpleNamespace(headers=headers or {}, stream=stream)
        return request, consumed

    @pytest.mark.asyncio
    async def test_stops_reading_once_over_limit(self):
        """No Content-Length (chunked): the cap holds while streaming, not after buffering."""
        request, consumed = self.fake_request([b"x" * 1024] * 1000)
        assert await webhooks.read_body_limited(request, 4096) is None
        assert len(consumed) == 5, "must stop at the first chunk that crosses the limit"

    @pytest.mark.asyncio
    async def test_declared_length_over_limit_reads_nothing(self):
        request, consumed = self.fake_request([b"x"], {"content-length": "999999"})
        assert await webhooks.read_body_limited(request, 4096) is None
        assert consumed == []

    @pytest.mark.asyncio
    async def test_body_within_limit_is_returned_whole(self):
        request, _ = self.fake_request([b"ab", b"cd"])
        assert await webhooks.read_body_limited(request, 4) == b"abcd"


# =============================================================================
# submit_job injection rule
# =============================================================================


class TestSubmitJobInternalInjection:

    @pytest.fixture
    def client(self):
        c = DeapiClient(api_token="t")
        c._request = AsyncMock(return_value={"data": {"request_id": "job-1"}})
        return c

    @pytest.mark.asyncio
    async def test_injects_receiver_when_enabled_and_caller_gave_none(
        self, client, internal_on, fresh_registry
    ):
        await client.submit_job("images/generations", json_data={"prompt": "p"})
        sent = client._request.call_args.kwargs["json_data"]
        assert sent["webhook_url"] == RECEIVER
        assert sent["webhook_secret"] == SECRET
        assert fresh_registry.is_tracked("job-1"), "job must be tracked so the poller waits"

    @pytest.mark.asyncio
    async def test_caller_url_wins_and_job_is_not_tracked(
        self, client, internal_on, fresh_registry
    ):
        """One webhook per job: the caller's own URL takes precedence, we poll normally."""
        await client.submit_job(
            "images/generations", json_data={"prompt": "p"}, webhook_url="https://theirs.example/h"
        )
        sent = client._request.call_args.kwargs["json_data"]
        assert sent["webhook_url"] == "https://theirs.example/h"
        assert "webhook_secret" not in sent
        assert not fresh_registry.is_tracked("job-1")

    @pytest.mark.asyncio
    async def test_nothing_injected_when_disabled(self, client, fresh_registry, monkeypatch):
        monkeypatch.setattr(settings, "webhooks_internal", False)
        await client.submit_job("images/generations", json_data={"prompt": "p"})
        assert "webhook_url" not in client._request.call_args.kwargs["json_data"]
        assert not fresh_registry.is_tracked("job-1")


# =============================================================================
# Polling: wait on callback with a safety net
# =============================================================================


def status(s: str) -> JobStatusResponse:
    return JobStatusResponse(data=JobStatusData(status=s, progress=50.0, result_url="u"))


class TestPollingWaitsOnCallback:

    @pytest.mark.asyncio
    async def test_callback_wakes_poller_before_fallback_interval(
        self, internal_on, fresh_registry
    ):
        """The point of the feature: completion is noticed at once, not after a sleep."""
        from deapi_mcp.polling_manager import PollingManager

        api = AsyncMock()
        api.get_job_status = AsyncMock(side_effect=[status("processing"), status("done")])
        pm = PollingManager(api, job_type="image")
        fresh_registry.track("job-1")

        async def api_calls_back_after(delay):
            await asyncio.sleep(delay)
            fresh_registry.signal("job-1")

        started = time.monotonic()
        result, _ = await asyncio.gather(
            pm.poll_until_complete("job-1"), api_calls_back_after(0.05)
        )
        elapsed = time.monotonic() - started

        assert result.success is True
        assert elapsed < 1.0, f"woke after {elapsed:.2f}s; fallback interval is 5s"
        assert (
            api.get_job_status.await_count == 2
        ), "one poll before waiting, one after the callback"

    @pytest.mark.asyncio
    async def test_lost_callback_falls_back_to_polling(
        self, internal_on, fresh_registry, monkeypatch
    ):
        """A callback that never arrives costs latency, never correctness."""
        from deapi_mcp.polling_manager import PollingManager

        monkeypatch.setattr(settings, "webhook_fallback_poll_interval", 0.05)
        api = AsyncMock()
        api.get_job_status = AsyncMock(
            side_effect=[status("processing"), status("processing"), status("done")]
        )
        pm = PollingManager(api, job_type="image")
        fresh_registry.track("job-1")

        result = await asyncio.wait_for(pm.poll_until_complete("job-1"), timeout=2.0)

        assert result.success is True
        assert api.get_job_status.await_count == 3

    @pytest.mark.asyncio
    async def test_untracked_job_polls_as_before(self, internal_on, fresh_registry, monkeypatch):
        """A job whose caller supplied their own webhook is not tracked and must not block."""
        from deapi_mcp.polling_manager import PollingManager

        api = AsyncMock()
        api.get_job_status = AsyncMock(side_effect=[status("processing"), status("done")])
        pm = PollingManager(api, job_type="image")
        pm.config = pm.config.model_copy(update={"initial_delay": 0.01})

        started = time.monotonic()
        result = await pm.poll_until_complete("job-1")

        assert result.success is True
        assert time.monotonic() - started < 1.0

    @pytest.mark.asyncio
    async def test_job_is_forgotten_after_poll_returns(self, internal_on, fresh_registry):
        from deapi_mcp.polling_manager import PollingManager

        api = AsyncMock()
        api.get_job_status = AsyncMock(return_value=status("done"))
        pm = PollingManager(api, job_type="image")
        fresh_registry.track("job-1")

        await pm.poll_until_complete("job-1")

        assert not fresh_registry.is_tracked("job-1")


# =============================================================================
# Config: half-configured must fail loudly
# =============================================================================


class TestConfigValidation:

    def test_flag_without_url_refuses_to_start(self):
        with pytest.raises(ValueError, match="RECEIVER_URL"):
            Settings(webhooks_internal=True, webhook_secret=SECRET, _env_file=None)

    def test_flag_without_secret_refuses_to_start(self):
        with pytest.raises(ValueError, match="SECRET"):
            Settings(webhooks_internal=True, webhook_receiver_url=RECEIVER, _env_file=None)

    def test_http_receiver_refused(self):
        with pytest.raises(ValueError, match="https"):
            Settings(
                webhooks_internal=True,
                webhook_receiver_url="http://mcp.example.com/webhooks/deapi",
                webhook_secret=SECRET,
                _env_file=None,
            )

    def test_short_secret_refused(self):
        with pytest.raises(ValueError, match="32-255"):
            Settings(
                webhooks_internal=True,
                webhook_receiver_url=RECEIVER,
                webhook_secret="short",
                _env_file=None,
            )

    def test_disabled_needs_nothing(self):
        s = Settings(webhooks_internal=False, _env_file=None)
        assert s.webhook_receiver_url is None

    def test_env_var_names_have_single_prefix(self, monkeypatch):
        """Guard against the env_prefix trap that bit deapi_api_base_url."""
        monkeypatch.setenv("DEAPI_WEBHOOKS_INTERNAL", "true")
        monkeypatch.setenv("DEAPI_WEBHOOK_RECEIVER_URL", RECEIVER)
        monkeypatch.setenv("DEAPI_WEBHOOK_SECRET", SECRET)
        s = Settings(_env_file=None)
        assert s.webhooks_internal is True
        assert s.webhook_receiver_url == RECEIVER
