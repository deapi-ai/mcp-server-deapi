"""The plain API key check: probe verdicts, cache, single flight, fail-open and the switch."""

import asyncio

import httpx
import pytest

from deapi_mcp import key_verifier
from deapi_mcp.config import settings
from deapi_mcp.key_verifier import (
    INVALID_TTL,
    UNKNOWN_TTL,
    VALID_TTL,
    KeyStatus,
    KeyVerifier,
    is_api_key_rejected,
    probe_api_key,
)

GOOD = "sk-dp-good-0123456789"
BAD = "sk-dp-bad-0123456789"


class FakeApi:
    """Answers the balance endpoint by key; counts requests and can hold them."""

    def __init__(self, statuses=None, exc=None):
        self.statuses = statuses or {GOOD: 200}
        self.exc = exc
        self.calls = 0
        self.requests: list[httpx.Request] = []
        self.release: asyncio.Event | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        if self.release is not None:
            await self.release.wait()
        if self.exc is not None:
            raise self.exc
        key = request.headers["authorization"].removeprefix("Bearer ")
        status = self.statuses.get(key, 401)
        body = {"data": {"balance": 1.0}} if status == 200 else {"message": "Unauthenticated."}
        return httpx.Response(status, json=body)


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(key_verifier, "_shared_http_client", lambda: client)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(key_verifier, "_clock", lambda: now[0])
    return now


# --- probe verdicts ---------------------------------------------------------


@pytest.mark.parametrize(
    "status, verdict",
    [
        (200, KeyStatus.VALID),
        (204, KeyStatus.VALID),
        # Authenticated but not allowed this endpoint: the key itself is real.
        (403, KeyStatus.VALID),
        (401, KeyStatus.INVALID),
        (429, KeyStatus.UNKNOWN),
        (500, KeyStatus.UNKNOWN),
        (502, KeyStatus.UNKNOWN),
        (503, KeyStatus.UNKNOWN),
        (404, KeyStatus.UNKNOWN),
    ],
)
async def test_probe_maps_status_to_verdict(api, status, verdict):
    api.statuses = {GOOD: status}
    assert await probe_api_key(GOOD) is verdict


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.RemoteProtocolError("broken"),
    ],
)
async def test_probe_network_failure_is_unknown(api, exc):
    api.exc = exc
    assert await probe_api_key(GOOD) is KeyStatus.UNKNOWN


async def test_probe_calls_balance_once_with_the_key_and_a_short_timeout(api, monkeypatch):
    seen = {}
    real_get = httpx.AsyncClient.get

    async def spy(self, url, **kwargs):
        seen.update(kwargs)
        return await real_get(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", spy)
    await probe_api_key(GOOD)

    assert api.calls == 1
    request = api.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/api/{settings.deapi_api_version}/account/balance"
    assert request.headers["authorization"] == f"Bearer {GOOD}"
    assert seen["timeout"] == key_verifier.PROBE_TIMEOUT


async def test_probe_never_logs_the_key(api, caplog):
    caplog.set_level("DEBUG")
    api.statuses = {}
    await probe_api_key(BAD)
    api.exc = httpx.ConnectError("refused")
    await probe_api_key(BAD)
    assert BAD not in caplog.text


# --- cache ------------------------------------------------------------------


async def test_valid_verdict_is_cached_for_ten_minutes(api, clock):
    verifier = KeyVerifier()
    assert await verifier.verify(GOOD) is KeyStatus.VALID
    clock[0] += VALID_TTL - 1
    assert await verifier.verify(GOOD) is KeyStatus.VALID
    assert api.calls == 1
    clock[0] += 2
    assert await verifier.verify(GOOD) is KeyStatus.VALID
    assert api.calls == 2


async def test_invalid_verdict_is_cached_for_a_minute(api, clock):
    verifier = KeyVerifier()
    assert await verifier.verify(BAD) is KeyStatus.INVALID
    clock[0] += INVALID_TTL - 1
    assert await verifier.verify(BAD) is KeyStatus.INVALID
    assert api.calls == 1
    clock[0] += 2
    # The key was fixed on the API side meanwhile: the next probe sees it.
    api.statuses[BAD] = 200
    assert await verifier.verify(BAD) is KeyStatus.VALID
    assert api.calls == 2


async def test_unknown_verdict_is_cached_briefly(api, clock):
    verifier = KeyVerifier()
    api.statuses = {GOOD: 503}
    assert await verifier.verify(GOOD) is KeyStatus.UNKNOWN
    assert await verifier.verify(GOOD) is KeyStatus.UNKNOWN
    assert api.calls == 1
    clock[0] += UNKNOWN_TTL + 1
    api.statuses = {GOOD: 200}
    assert await verifier.verify(GOOD) is KeyStatus.VALID
    assert api.calls == 2


async def test_cache_is_bounded_least_recently_used_first(api, clock):
    verifier = KeyVerifier(max_entries=2)
    api.statuses = {"k1-xxxxxxxxxx": 200, "k2-xxxxxxxxxx": 200, "k3-xxxxxxxxxx": 200}
    await verifier.verify("k1-xxxxxxxxxx")
    await verifier.verify("k2-xxxxxxxxxx")
    await verifier.verify("k1-xxxxxxxxxx")  # k1 is now the most recent
    await verifier.verify("k3-xxxxxxxxxx")  # evicts k2
    assert len(verifier) == 2
    assert api.calls == 3
    await verifier.verify("k1-xxxxxxxxxx")
    assert api.calls == 3
    await verifier.verify("k2-xxxxxxxxxx")
    assert api.calls == 4


async def test_cache_never_holds_the_raw_key(api):
    verifier = KeyVerifier()
    await verifier.verify(GOOD)
    (cache_key,) = verifier._verdicts
    assert GOOD not in cache_key
    # Keyed by a per-process secret: another verifier derives a different key.
    assert KeyVerifier()._cache_key(GOOD) != cache_key


# --- single flight ----------------------------------------------------------


async def test_concurrent_checks_of_one_key_share_a_probe(api):
    verifier = KeyVerifier()
    api.release = asyncio.Event()
    waiters = [asyncio.create_task(verifier.verify(GOOD)) for _ in range(20)]
    await asyncio.sleep(0)
    api.release.set()
    assert set(await asyncio.gather(*waiters)) == {KeyStatus.VALID}
    assert api.calls == 1
    assert not verifier._inflight


async def test_different_keys_probe_separately(api):
    verifier = KeyVerifier()
    api.release = asyncio.Event()
    good = asyncio.create_task(verifier.verify(GOOD))
    bad = asyncio.create_task(verifier.verify(BAD))
    await asyncio.sleep(0)
    api.release.set()
    assert await good is KeyStatus.VALID
    assert await bad is KeyStatus.INVALID
    assert api.calls == 2


async def test_a_cancelled_waiter_does_not_cancel_the_shared_probe(api):
    verifier = KeyVerifier()
    api.release = asyncio.Event()
    first = asyncio.create_task(verifier.verify(GOOD))
    second = asyncio.create_task(verifier.verify(GOOD))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    api.release.set()
    assert await second is KeyStatus.VALID
    assert api.calls == 1


async def test_an_unexpected_error_in_the_probe_lets_the_key_through(monkeypatch):
    async def boom(api_key):
        raise RuntimeError("bug")

    monkeypatch.setattr(key_verifier, "probe_api_key", boom)
    assert await KeyVerifier().verify(GOOD) is KeyStatus.UNKNOWN


# --- switch -----------------------------------------------------------------


async def test_rejected_only_on_a_positive_refusal(api, monkeypatch):
    monkeypatch.setattr(settings, "verify_api_keys", True)
    assert await is_api_key_rejected(BAD) is True
    assert await is_api_key_rejected(GOOD) is False
    api.statuses = {"sk-dp-down-0123456789": 503}
    assert await is_api_key_rejected("sk-dp-down-0123456789") is False


async def test_switched_off_never_probes(api, monkeypatch):
    monkeypatch.setattr(settings, "verify_api_keys", False)
    assert await is_api_key_rejected(BAD) is False
    assert api.calls == 0


@pytest.mark.parametrize(
    "raw, expected",
    [("false", False), ("0", False), ("true", True), ("1", True), ("", True)],
)
def test_switch_reads_the_environment(monkeypatch, raw, expected):
    from deapi_mcp.config import Settings

    monkeypatch.setenv("DEAPI_VERIFY_API_KEYS", raw)
    assert Settings(_env_file=None).verify_api_keys is expected
