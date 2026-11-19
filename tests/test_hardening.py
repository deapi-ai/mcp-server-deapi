"""Hardening from the 0.7.1 security review, one test (or a few) per fix."""

import asyncio
import gzip
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import deapi_mcp.utils as u
from deapi_mcp import auth_code_store, consent, deapi_client, key_verifier, oauth_clients
from deapi_mcp.auth_code_store import InMemoryAuthCodeStore
from deapi_mcp.config import Settings, secret_value, settings
from deapi_mcp.deapi_client import DeapiAPIError
from deapi_mcp.utils import InputError, safe_fetch
from tests.test_public_clients import (
    BAD,
    GOOD,
    REDIRECT,
    FakeApi,
    authorize,
    dcr_client,
    exchange,
    form_fields,
    location_query,
    submit,
)


@pytest.fixture
def api(monkeypatch):
    """The fake deAPI of test_public_clients: one good key, the key check on."""
    fake = FakeApi()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(key_verifier, "_shared_http_client", lambda: client)
    monkeypatch.setattr(settings, "verify_api_keys", True)
    consent.reset_limiter(None)
    oauth_clients.clear_cimd_cache()
    yield fake
    consent.reset_limiter(None)
    oauth_clients.clear_cimd_cache()


# --- safe_fetch: compression, deadline, proxies, IPv6 ---------------------------------------

PORT = 0
BOMB = gzip.compress(b"\0" * (2 * 1024 * 1024))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/gzip":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(BOMB)))
            self.end_headers()
            self.wfile.write(BOMB)
        elif self.path == "/trickle":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
                time.sleep(0.2)
        else:
            body = b"\x89PNG" + b"x" * 16
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    global PORT
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{PORT}"
    srv.shutdown()


@pytest.fixture
def allow_loopback(monkeypatch):
    real = u._ip_is_public
    monkeypatch.setattr(u, "_ip_is_public", lambda ip: ip in ("127.0.0.1", "::1") or real(ip))


async def test_a_compressed_body_is_refused_not_inflated(server, allow_loopback):
    # 2 MB of zeros in ~2 kB of gzip: inflating it would blow far past the 5 kB cap
    with pytest.raises(InputError):
        await safe_fetch(f"{server}/gzip", max_bytes=5 * 1024, timeout=2)


async def test_a_trickling_server_hits_the_overall_deadline(server, allow_loopback):
    start = time.monotonic()
    with pytest.raises(InputError):
        await safe_fetch(f"{server}/trickle", max_bytes=1000, timeout=2, total_timeout=0.6)
    assert time.monotonic() - start < 2


async def test_proxy_settings_from_the_environment_are_ignored(server, allow_loopback, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    body, _ = await safe_fetch(f"{server}/ok", max_bytes=1000, timeout=2)
    assert body.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "ip",
    [
        "fec0::1",  # deprecated site-local
        "2002:a9fe:a9fe::",  # 6to4 around 169.254.169.254
        "2002:7f00:1::",  # 6to4 around 127.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo around 10.0.0.1
    ],
)
def test_ipv4_inside_ipv6_is_judged_as_itself(ip):
    assert not u._ip_is_public(ip)


# --- upload limits ---------------------------------------------------------------------------


def test_per_type_caps_follow_the_api_and_the_global_limit(monkeypatch):
    assert u.upload_cap(u.MAX_IMAGE_BYTES) == 10 * 1024 * 1024
    assert u.upload_cap(u.MAX_VIDEO_BYTES) == 50 * 1024 * 1024
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    assert u.upload_cap(u.MAX_VIDEO_BYTES) == 20 * 1024 * 1024


def test_one_call_cannot_upload_more_than_the_global_limit(monkeypatch):
    import io

    monkeypatch.setattr(settings, "max_upload_mb", 1)
    half = ("images[]", ("a.png", io.BytesIO(b"x" * (600 * 1024)), "image/png"))
    u.check_upload_budget([half])
    with pytest.raises(InputError, match="1 MB"):
        u.check_upload_budget([half, half])


def test_max_upload_must_be_positive():
    with pytest.raises(ValueError):
        Settings(max_upload_mb=0, _env_file=None)


async def test_media_lists_are_bounded_in_the_tool_schema():
    from deapi_mcp.server_remote import mcp

    tools = {t.name: t for t in await mcp.list_tools(run_middleware=False)}
    props = tools["image_to_video"].parameters["properties"]
    for name in ("ref_images", "ref_videos", "ref_audios"):
        schema = props[name]
        options = schema.get("anyOf", [schema])
        assert any(o.get("maxItems") == u.MAX_MEDIA_INPUTS for o in options), name


# --- deAPI client ------------------------------------------------------------------------------


async def test_job_id_stays_one_path_segment():
    client = deapi_client.DeapiClient(api_token="k" * 20)
    client._request = AsyncMock(return_value={"data": {"status": "pending"}})
    await client.get_job_status("../../v1/account/balance?x=1#y")
    endpoint = client._request.call_args.kwargs["endpoint"]
    assert endpoint == "jobs/..%2F..%2Fv1%2Faccount%2Fbalance%3Fx%3D1%23y"


async def test_the_shared_client_keeps_no_cookies():
    client = deapi_client._shared_http_client()
    request = httpx.Request("GET", "https://api.example.com/api/v2/account/balance")
    response = httpx.Response(200, headers={"Set-Cookie": "sess=user-a; Path=/"}, request=request)
    client.cookies.extract_cookies(response)
    assert len(client.cookies) == 0


def test_webhook_secret_never_reaches_a_repr():
    s = Settings(webhook_secret="s" * 40, _env_file=None)
    assert "s" * 40 not in repr(s)
    assert secret_value(s.webhook_secret) == "s" * 40
    assert secret_value("plain") == "plain"


# --- key check ---------------------------------------------------------------------------------


async def test_a_key_that_cannot_be_a_header_is_invalid_without_a_probe(monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(key_verifier, "probe_api_key", probe)
    verifier = key_verifier.KeyVerifier()
    for key in ("klucz-żółw-0123456789", "with space 0123456789", "tab\tkey0123456789"):
        assert await verifier.verify(key) is key_verifier.KeyStatus.INVALID
    probe.assert_not_called()


async def test_sign_in_keeps_probe_slots_a_flood_cannot_take(monkeypatch):
    gate = asyncio.Event()

    async def slow_probe(api_key):
        await gate.wait()
        return key_verifier.KeyStatus.VALID

    monkeypatch.setattr(key_verifier, "probe_api_key", slow_probe)
    verifier = key_verifier.KeyVerifier()
    flood = [
        asyncio.ensure_future(verifier.verify(f"flood-key-{i:04d}"))
        for i in range(key_verifier.MAX_CONCURRENT_PROBES)
    ]
    await asyncio.sleep(0)
    # The general slots are full: one more plain key is let through unchecked...
    assert await verifier.verify("one-more-key-0123") is key_verifier.KeyStatus.UNKNOWN
    # ...but the sign-in screen still gets a real check
    sign_in = asyncio.ensure_future(verifier.verify("person-key-0123456", sign_in=True))
    await asyncio.sleep(0)
    gate.set()
    assert await sign_in is key_verifier.KeyStatus.VALID
    await asyncio.gather(*flood)


def test_a_bearer_on_another_route_is_never_sent_to_deapi(app_client, api, rejected_reasons):
    r = app_client.get("/health", headers={"Authorization": "Bearer metrics-scrape-secret-123"})
    assert r.status_code == 200
    assert api.calls == 0
    assert rejected_reasons == []


def test_a_bearer_on_mcp_is_still_checked(app_client, api):
    r = app_client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {BAD}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 401
    assert api.calls == 1


@pytest.fixture
def rejected_reasons():
    from deapi_mcp.observability import set_auth_rejected_sink

    reasons: list[str] = []
    set_auth_rejected_sink(reasons.append)
    yield reasons
    set_auth_rejected_sink(None)


# --- authorization codes -----------------------------------------------------------------------


@pytest.fixture
def small_pools(monkeypatch):
    monkeypatch.setattr(auth_code_store, "AUTHORIZATION_CODE_MAX_ENTRIES", 3)
    auth_code_store.reset_auth_code_store()
    yield
    auth_code_store.reset_auth_code_store()


def test_a_flood_of_preregistered_codes_never_evicts_a_sign_in_code(app_client, api, small_pools):
    client_id = dcr_client(app_client)
    done = submit(app_client, authorize(app_client, client_id))
    code = location_query(done)["code"]
    for _ in range(10):  # anonymous GETs, each storing a deapi-mcp code
        r = app_client.get(
            "/authorize",
            params={
                "client_id": "deapi-mcp",
                "redirect_uri": "https://evil.example/cb",
                "response_type": "code",
                "code_challenge": "c" * 43,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert r.status_code in (302, 307)
    assert exchange(app_client, client_id, code).status_code == 200


def test_pools_are_separate_stores():
    auth_code_store.reset_auth_code_store()
    try:
        default = auth_code_store.get_auth_code_store()
        sign_in = auth_code_store.get_auth_code_store("sign_in")
        assert default is not sign_in
        assert isinstance(sign_in, InMemoryAuthCodeStore)
        assert auth_code_store.get_auth_code_store("sign_in") is sign_in
    finally:
        auth_code_store.reset_auth_code_store()


def test_a_sign_in_code_for_a_key_refused_since_gets_no_token(app_client, api):
    client_id = dcr_client(app_client)
    done = submit(app_client, authorize(app_client, client_id))
    code = location_query(done)["code"]
    key_verifier.note_key_refused(GOOD)
    try:
        r = exchange(app_client, client_id, code)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"
    finally:
        key_verifier.get_key_verifier().clear()


# --- redirect URIs and client IDs --------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "https://a.example;sandbox/cb",  # would add a CSP directive
        "https://ex€.example/cb",  # not Latin-1: was a 500
        "https://сlaude.ai/cb",  # Cyrillic look-alike
        "https://exé.example/cb",  # Latin-1, still not a DNS name
        "https://claude.ai@evil.example/cb",  # userinfo
    ],
)
def test_redirect_hosts_must_be_plain_dns_names(uri):
    assert not oauth_clients.valid_redirect_uri(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://xn--e1aybc.example/cb",
        "http://127.0.0.1:33418/cb",
        "http://[::1]:5000/cb",
        "cursor://anysphere.cursor-mcp/oauth/callback",
        "com.example.app:/cb",
    ],
)
def test_ordinary_redirects_still_pass(uri):
    assert oauth_clients.valid_redirect_uri(uri)


def test_registration_refuses_a_redirect_that_would_break_the_csp(app_client, api):
    r = app_client.post(
        "/register",
        json={"client_name": "x", "redirect_uris": ["https://a.example;sandbox/cb"]},
    )
    assert r.status_code == 400


def test_a_metadata_url_on_a_lookalike_host_is_not_a_client_id():
    assert not oauth_clients.is_cimd_client_id("https://сlaude.ai/oauth/client.json")
    assert oauth_clients.is_cimd_client_id("https://claude.ai/oauth/client.json")


async def test_one_site_cannot_take_every_metadata_fetch_slot(monkeypatch):
    oauth_clients.clear_cimd_cache()
    gate = asyncio.Event()
    started = []

    async def stalled(url):
        started.append(url)
        await gate.wait()
        raise InputError("x")

    monkeypatch.setattr(oauth_clients, "_fetch_metadata", stalled)
    try:
        tasks = [
            asyncio.ensure_future(
                oauth_clients.fetch_cimd_client(f"https://h{i}.attacker.example/c")
            )
            for i in range(10)
        ]
        await asyncio.sleep(0.05)
        assert len(started) == oauth_clients.CIMD_MAX_FETCHES_PER_SITE
        gate.set()
        assert await asyncio.gather(*tasks) == [None] * 10
    finally:
        oauth_clients.clear_cimd_cache()


# --- wrong-key limiter -------------------------------------------------------------------------


async def test_ipv6_addresses_in_one_64_share_a_limit():
    limiter = consent.KeyFailureLimiter()
    for i in range(consent.MAX_KEY_FAILURES):
        await limiter.record_failure(f"2001:db8:1:2::{i + 1:x}")
    assert await limiter.blocked("2001:db8:1:2:ffff::1")
    assert not await limiter.blocked("2001:db8:1:3::1")


async def test_an_attempt_is_given_back_when_the_key_was_good():
    limiter = consent.KeyFailureLimiter()
    for _ in range(consent.MAX_KEY_FAILURES - 1):
        assert await limiter.attempt("198.51.100.7")
    assert await limiter.attempt("198.51.100.7")  # the last one allowed
    await limiter.refund("198.51.100.7")
    assert await limiter.attempt("198.51.100.7")
    assert not await limiter.attempt("198.51.100.7")


async def test_redis_refund_never_goes_below_zero():
    import fakeredis

    limiter = consent.KeyFailureLimiter()
    limiter._redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    limiter._refund_script = limiter._redis.register_script(consent._REFUND_SCRIPT)
    await limiter.refund("198.51.100.8")
    assert await limiter._redis.get(limiter._redis_key("198.51.100.8")) is None
    assert await limiter.attempt("198.51.100.8")
    await limiter.refund("198.51.100.8")
    assert await limiter._redis.get(limiter._redis_key("198.51.100.8")) == "0"


async def test_concurrent_wrong_keys_cannot_outrun_the_limit(app_client, api, monkeypatch):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    fields = {**form_fields(page.text), "action": "allow"}
    checked = 0

    async def slow_probe(api_key):
        nonlocal checked
        checked += 1
        await asyncio.sleep(0.05)
        return key_verifier.KeyStatus.INVALID

    monkeypatch.setattr(key_verifier, "probe_api_key", slow_probe)
    transport = httpx.ASGITransport(app=app_client.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://testserver",
        cookies={c.name: c.value for c in app_client.cookies.jar},
    ) as client:
        answers = await asyncio.gather(
            *[
                client.post("/authorize", data={**fields, "api_key": f"wrong-key-{i:04d}"})
                for i in range(30)
            ]
        )
    assert checked == consent.MAX_KEY_FAILURES
    assert sorted({a.status_code for a in answers}) == [400, 429]


def test_a_good_key_does_not_use_up_attempts(app_client, api):
    client_id = dcr_client(app_client)
    for _ in range(consent.MAX_KEY_FAILURES + 3):
        # A fresh page each time: a successful sign-in clears the CSRF cookie
        assert submit(app_client, authorize(app_client, client_id)).status_code == 303


# --- logs and errors ---------------------------------------------------------------------------


def test_bad_authorize_values_cannot_forge_log_lines(app_client, caplog):
    caplog.set_level(logging.ERROR)
    app_client.get(
        "/authorize",
        params={
            "client_id": "deapi-mcp",
            "redirect_uri": REDIRECT,
            "response_type": "x\n2026-01-01 - deapi_mcp.consent - INFO - forged",
        },
        follow_redirects=False,
    )
    assert "\n2026-01-01" not in caplog.text


def test_token_creation_errors_are_not_echoed(monkeypatch):
    from deapi_mcp import oauth_endpoints

    def boom(*a, **k):
        raise RuntimeError("internal detail sk-secret")

    monkeypatch.setattr(oauth_endpoints, "create_jwt", boom)
    r = oauth_endpoints._token_success("k" * 20, "deapi-mcp")
    assert r.status_code == 500
    assert b"internal detail" not in r.body


def test_a_weak_jwt_secret_is_warned_about(monkeypatch, caplog):
    from deapi_mcp import server_remote

    monkeypatch.setenv("DEAPI_JWT_SECRET_KEY", "your-secret-key-here")
    server_remote.require_jwt_secret_key("0.0.0.0")
    assert "weak" in caplog.text
    caplog.clear()
    monkeypatch.setenv("DEAPI_JWT_SECRET_KEY", "x" * 40)
    server_remote.require_jwt_secret_key("0.0.0.0")
    assert "weak" not in caplog.text


# --- job registry ------------------------------------------------------------------------------


async def test_another_accounts_404_does_not_untrack_a_job(monkeypatch):
    from deapi_mcp import polling_manager
    from deapi_mcp.webhooks import JobCompletionRegistry, set_job_registry

    registry = JobCompletionRegistry()
    set_job_registry(registry)
    try:
        registry.track("job-a")
        client = MagicMock()
        client.get_job_status = AsyncMock(side_effect=DeapiAPIError("Not found", status_code=404))
        result = await polling_manager.PollingManager(client, "image").poll_until_complete("job-a")
        assert result.success is False
        assert registry.is_tracked("job-a")
    finally:
        set_job_registry(JobCompletionRegistry())
