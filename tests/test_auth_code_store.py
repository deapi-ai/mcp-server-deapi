"""Tests for the OAuth authorization code store (in-memory and Redis backends)."""

import asyncio
import hashlib
import json
import socket
import sys
import time
from types import SimpleNamespace

import fakeredis
import pytest
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from deapi_mcp.auth_code_store import (
    AuthCodeStoreUnavailable,
    InMemoryAuthCodeStore,
    RedisAuthCodeStore,
    build_redis_client,
    get_auth_code_store,
    reset_auth_code_store,
)

PREFIX = "deapi_mcp_database_"
INDEX = f"{PREFIX}oauth_codes_live"
REAL_NOW = 1_800_000_000.0
DATA = {
    "client_id": "client-1",
    "redirect_uri": "https://example.com/cb",
    "code_challenge": "challenge",
    "state": "state-1",
    "expires_at": REAL_NOW + 600,
}


def code_key(code):
    return f"{PREFIX}oauth_code:{hashlib.sha256(code.encode()).hexdigest()}"


async def live_codes(store):
    if isinstance(store, InMemoryAuthCodeStore):
        return len(store._codes)
    return await store._client.zcard(INDEX)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr("deapi_mcp.auth_code_store.time.time", lambda: REAL_NOW)


@pytest.fixture
def ticking_clock(monkeypatch):
    """Each call is a millisecond later, so the Redis index orders codes by insertion."""
    ticks = iter(REAL_NOW + n / 1000 for n in range(10_000))
    monkeypatch.setattr("deapi_mcp.auth_code_store.time.time", lambda: next(ticks))


@pytest.fixture
def srv():
    return fakeredis.FakeServer()


@pytest.fixture
def raw_client(srv):
    return fakeredis.FakeAsyncRedis(server=srv, decode_responses=True)


@pytest.fixture
def redis_store(raw_client):
    return RedisAuthCodeStore(raw_client, PREFIX, 3)


@pytest.fixture(params=["memory", "redis"])
def store(request, redis_store):
    if request.param == "memory":
        return InMemoryAuthCodeStore(max_entries=3)
    return redis_store


async def test_put_then_consume_returns_data(store):
    assert await store.put("c1", DATA, 600) is True
    assert await store.consume("c1") == DATA


async def test_consume_is_single_use(store):
    await store.put("c1", DATA, 600)
    await store.consume("c1")
    assert await store.consume("c1") is None


async def test_consume_unknown_code_returns_none(store):
    await store.put("c1", DATA, 600)
    assert await store.consume("nope") is None
    assert await store.consume("c1") == DATA  # an unknown code disturbs nothing


async def test_concurrent_consume_only_one_wins(store):
    await store.put("c1", DATA, 600)
    results = await asyncio.gather(store.consume("c1"), store.consume("c1"))
    assert sorted(r is None for r in results) == [False, True]


async def test_full_store_evicts_oldest_instead_of_rejecting(store, ticking_clock):
    assert [await store.put(f"c{i}", DATA, 600) for i in range(5)] == [True] * 5
    assert await store.consume("c0") is None
    assert await store.consume("c1") is None
    assert await store.consume("c4") == DATA


async def test_eviction_keeps_redis_keys_bounded(redis_store, raw_client, ticking_clock):
    for i in range(10):
        await redis_store.put(f"c{i}", DATA, 600)
    keys = await raw_client.keys("*")
    assert len(keys) == 3 + 1  # three live codes plus the index
    assert await raw_client.zcard(f"{PREFIX}oauth_codes_live") == 3


async def test_zero_cap_rejects_in_memory():
    assert await InMemoryAuthCodeStore(0).put("c", DATA, 600) is False


async def test_zero_cap_rejects_in_redis(raw_client):
    assert await RedisAuthCodeStore(raw_client, PREFIX, 0).put("c", DATA, 600) is False


async def test_cap_frees_after_consume(store):
    for i in range(3):
        await store.put(f"c{i}", DATA, 600)
    await store.consume("c0")
    assert await store.put("c9", DATA, 600) is True
    assert await store.consume("c1") == DATA  # the consumed slot was reused, nothing evicted
    assert await store.consume("c2") == DATA


async def test_cap_frees_after_expiry(store, monkeypatch):
    for i in range(3):
        await store.put(f"c{i}", DATA, 600)
    monkeypatch.setattr("deapi_mcp.auth_code_store.time.time", lambda: REAL_NOW + 601)
    assert await store.put("c9", DATA, 600) is True
    assert await live_codes(store) == 1  # all three expired codes were pruned, not one evicted


async def test_expired_code_is_not_consumable_in_memory(monkeypatch):
    store = InMemoryAuthCodeStore(max_entries=3)
    await store.put("c1", DATA, 600)
    monkeypatch.setattr("deapi_mcp.auth_code_store.time.time", lambda: REAL_NOW + 601)
    assert await store.consume("c1") is None


async def test_cap_is_global_across_instances(srv, ticking_clock):
    a = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    b = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    assert [
        await a.put("a1", DATA, 600),
        await b.put("b1", DATA, 600),
        await a.put("a2", DATA, 600),
        await b.put("b2", DATA, 600),
    ] == [True, True, True, True]
    assert await b.consume("a1") is None  # the shared cap evicted the oldest across instances
    assert await a.consume("b2") == DATA


async def test_code_issued_on_one_instance_is_redeemable_on_another(srv):
    a = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    b = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    await a.put("shared", DATA, 600)
    assert await b.consume("shared") == DATA
    assert await a.consume("shared") is None


async def test_keys_hashed_prefixed_and_expiring(redis_store, raw_client):
    await redis_store.put("secret-code", DATA, 600)
    keys = await raw_client.keys("*")
    assert keys and all(k.startswith(PREFIX) for k in keys)
    assert not any("secret-code" in k for k in keys)
    assert json.loads(await raw_client.get(code_key("secret-code"))) == DATA
    for k in keys:
        assert await raw_client.ttl(k) > 0


async def test_index_scores_come_from_the_redis_clock(redis_store, raw_client, monkeypatch):
    skewed = SimpleNamespace(time=lambda: REAL_NOW - 3600)  # app host an hour behind Redis
    monkeypatch.setattr("deapi_mcp.auth_code_store.time", skewed)
    await redis_store.put("c1", DATA, 600)
    digest = hashlib.sha256(b"c1").hexdigest()
    assert await raw_client.zscore(INDEX, digest) == pytest.approx(REAL_NOW + 600)


async def test_cap_holds_when_one_app_clock_runs_ahead(srv, raw_client, monkeypatch):
    a = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    b = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    for i in range(3):
        await b.put(f"b{i}", DATA, 600)
    ahead = SimpleNamespace(time=lambda: REAL_NOW + 3600)
    monkeypatch.setattr("deapi_mcp.auth_code_store.time", ahead)
    await a.put("a1", DATA, 600)
    # An app-side clock would prune b's live codes from the index and let the keys outgrow the cap
    assert len(await raw_client.keys(f"{PREFIX}oauth_code:*")) == 3
    assert await raw_client.zcard(INDEX) == 3


async def test_short_ttl_put_never_shortens_index_ttl(redis_store, raw_client):
    await redis_store.put("long", DATA, 600)
    await redis_store.put("short", DATA, 60)
    assert await raw_client.ttl(INDEX) == 600
    assert await raw_client.ttl(code_key("short")) == 60


async def test_put_restores_a_missing_index_ttl(redis_store, raw_client):
    await redis_store.put("c1", DATA, 600)
    await raw_client.persist(INDEX)
    await redis_store.put("c2", DATA, 600)
    assert await raw_client.ttl(INDEX) == 600


async def test_code_written_by_previous_version_stays_redeemable(redis_store, raw_client):
    # The previous release scored the index with the app clock; the key format is unchanged
    digest = hashlib.sha256(b"legacy").hexdigest()
    await raw_client.set(code_key("legacy"), json.dumps(DATA), ex=600)
    await raw_client.zadd(INDEX, {digest: REAL_NOW + 600})
    await raw_client.expire(INDEX, 600)
    assert await redis_store.put("new", DATA, 600) is True
    assert await raw_client.zcard(INDEX) == 2
    assert await redis_store.consume("legacy") == DATA
    assert await raw_client.zcard(INDEX) == 1


@pytest.mark.parametrize(
    "raw",
    ["not json", '"a string"', "[1, 2]", b"\xff\xfe"],
    ids=["invalid-json", "json-string", "json-list", "invalid-utf8"],
)
async def test_corrupt_value_is_treated_as_unknown(redis_store, srv, caplog, raw):
    bytes_client = fakeredis.FakeAsyncRedis(server=srv)
    await bytes_client.set(code_key("c1"), raw, ex=600)
    assert await redis_store.consume("c1") is None
    assert await bytes_client.exists(code_key("c1")) == 0  # burnt like any redeemed code
    assert "unreadable authorization code" in caplog.text
    assert "c1" not in caplog.text
    assert "not json" not in caplog.text and "a string" not in caplog.text


async def test_redis_down_raises_unavailable(srv):
    srv.connected = False
    s = RedisAuthCodeStore(fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 3)
    with pytest.raises(AuthCodeStoreUnavailable) as put_exc:
        await s.put("c", DATA, 600)
    assert put_exc.value.__cause__ is not None
    with pytest.raises(AuthCodeStoreUnavailable) as consume_exc:
        await s.consume("c")
    assert consume_exc.value.__cause__ is not None


@pytest.fixture
def tls_settings(monkeypatch):
    values = dict(
        redis_host="10.0.0.5",
        redis_port=6380,
        redis_username="app",
        redis_password=None,
        redis_db=11,
        redis_scheme="tls",
        redis_ca_file="/etc/redis-ca/ca.pem",
        redis_prefix=PREFIX,
    )
    for name, value in values.items():
        monkeypatch.setattr(f"deapi_mcp.auth_code_store.settings.{name}", value, raising=False)
    return SimpleNamespace(**values)


@pytest.fixture
def plain_settings(monkeypatch):
    monkeypatch.setattr("deapi_mcp.auth_code_store.settings.redis_host", None, raising=False)


def test_build_redis_client_sets_short_timeouts(tls_settings):
    client = build_redis_client(tls_settings)
    kw = client.connection_pool.connection_kwargs
    assert kw["socket_timeout"] == 2 and kw["socket_connect_timeout"] == 2
    assert kw["db"] == 11 and kw["ssl_ca_certs"] == "/etc/redis-ca/ca.pem"
    assert kw["ssl_cert_reqs"] == "required"
    assert kw["ssl_check_hostname"] is True
    assert client.get_retry() == Retry(NoBackoff(), 1)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows retries a refused TCP connect for ~2 s; deployments and CI run on Linux",
)
async def test_unreachable_redis_fails_fast():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    s = SimpleNamespace(
        redis_host="127.0.0.1",
        redis_port=port,
        redis_username=None,
        redis_password=None,
        redis_db=11,
        redis_scheme="plain",
        redis_ca_file=None,
    )
    client = build_redis_client(s)
    store = RedisAuthCodeStore(client, PREFIX, 3)
    try:
        for name, op in (
            ("put", lambda: store.put("c", DATA, 600)),
            ("consume", lambda: store.consume("c")),
        ):
            started = time.monotonic()
            with pytest.raises(AuthCodeStoreUnavailable):
                await op()
            elapsed = time.monotonic() - started
            assert elapsed < 1.0, f"{name} took {elapsed:.1f} s against a refused port"
    finally:
        await client.aclose()


def test_build_redis_client_unwraps_secret_password():
    from pydantic import SecretStr

    s = SimpleNamespace(
        redis_host="h",
        redis_port=6379,
        redis_username=None,
        redis_password=SecretStr("pw"),
        redis_db=11,
        redis_scheme="plain",
        redis_ca_file=None,
    )
    kw = build_redis_client(s).connection_pool.connection_kwargs
    assert kw["password"] == "pw"
    assert "ssl_ca_certs" not in kw


def test_store_without_host_is_in_memory_and_warns(caplog, plain_settings):
    reset_auth_code_store()
    try:
        assert isinstance(get_auth_code_store(), InMemoryAuthCodeStore)
        assert "single-replica" in caplog.text
        assert get_auth_code_store() is get_auth_code_store()
    finally:
        reset_auth_code_store()


def test_store_with_host_is_redis_and_never_pings(caplog, tls_settings, monkeypatch):
    caplog.set_level("INFO")
    reset_auth_code_store()
    try:
        assert isinstance(get_auth_code_store(), RedisAuthCodeStore)
        assert "OAuth code store: Redis 10.0.0.5:6380 db=11" in caplog.text
    finally:
        reset_auth_code_store()
