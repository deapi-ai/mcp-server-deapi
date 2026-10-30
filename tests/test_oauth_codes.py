"""OAuth endpoints keep authorization codes in the shared store, not in process memory."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import fakeredis
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from deapi_mcp.auth_code_store import (
    InMemoryAuthCodeStore,
    RedisAuthCodeStore,
    reset_auth_code_store,
)
from deapi_mcp.oauth_endpoints import (
    CODE_STORE_RETRY_AFTER_SECONDS,
    authorize_endpoint,
    token_endpoint,
)

PREFIX = "deapi_mcp_database_"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 50
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)
PARAMS = {
    "client_id": "deapi-mcp",
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "code_challenge": CHALLENGE,
    "code_challenge_method": "S256",
    "state": "xyz",
}


def make_store(srv):
    return RedisAuthCodeStore(
        fakeredis.FakeAsyncRedis(server=srv, decode_responses=True), PREFIX, 1000
    )


def code_key(code):
    return f"{PREFIX}oauth_code:{hashlib.sha256(code.encode()).hexdigest()}"


def make_client():
    app = Starlette(
        routes=[
            Route("/authorize", authorize_endpoint),
            Route("/token", token_endpoint, methods=["POST"]),
        ]
    )
    return TestClient(app, follow_redirects=False)


def authorize(client):
    r = client.get("/authorize", params=PARAMS)
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def exchange(client, code, verifier=VERIFIER, redirect_uri=REDIRECT_URI):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": "deapi-mcp",
            "client_secret": "test-deapi-token-123",
        },
    )


@pytest.fixture
def srv():
    return fakeredis.FakeServer()


@pytest.fixture
def client(srv):
    reset_auth_code_store(make_store(srv))
    try:
        with make_client() as test_client:
            yield test_client
    finally:
        reset_auth_code_store()


def test_full_flow_issues_tokens(client):
    r = exchange(client, authorize(client))
    assert r.status_code == 200
    assert "access_token" in r.json()
    assert "refresh_token" in r.json()


def test_code_works_once(client):
    code = authorize(client)
    assert exchange(client, code).status_code == 200
    assert exchange(client, code).json()["error"] == "invalid_grant"


def test_code_is_burnt_by_a_failed_attempt(client):
    code = authorize(client)
    assert exchange(client, code, redirect_uri="https://evil.example/cb").status_code == 400
    assert exchange(client, code).json()["error"] == "invalid_grant"


def test_wrong_verifier_is_rejected(client):
    r = exchange(client, authorize(client), verifier="w" * 50)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_login_across_two_store_instances(srv):
    raw = fakeredis.FakeRedis(server=srv, decode_responses=True)
    reset_auth_code_store(make_store(srv))
    try:
        with make_client() as client_a:
            code = authorize(client_a)
        # The code lives in Redis under its hash, not in the issuing process
        assert raw.exists(code_key(code)) == 1
        reset_auth_code_store(make_store(srv))
        with make_client() as client_b:
            assert exchange(client_b, code).status_code == 200
        assert raw.exists(code_key(code)) == 0
    finally:
        reset_auth_code_store()


def test_login_fails_when_the_shared_store_lost_the_code(srv):
    """Negative control: the redeeming instance only knows what Redis holds."""
    raw = fakeredis.FakeRedis(server=srv, decode_responses=True)
    reset_auth_code_store(make_store(srv))
    try:
        with make_client() as client_a:
            code = authorize(client_a)
        raw.delete(code_key(code))
        reset_auth_code_store(make_store(srv))
        with make_client() as client_b:
            assert exchange(client_b, code).json()["error"] == "invalid_grant"
    finally:
        reset_auth_code_store()


@pytest.mark.parametrize("client_id", ["evil", "", None])
def test_authorize_bad_client_id_returns_400_never_redirects(client_id):
    params = {**PARAMS, "redirect_uri": "https://evil.example/cb"}
    if client_id is None:
        del params["client_id"]
    else:
        params["client_id"] = client_id
    with make_client() as client:
        r = client.get("/authorize", params=params)
    assert r.status_code == 400
    assert "location" not in r.headers
    assert r.json()["error"] == "unauthorized_client"


def test_authorize_at_capacity_redirects_server_error():
    reset_auth_code_store(InMemoryAuthCodeStore(max_entries=0))
    try:
        with make_client() as client:
            loc = client.get("/authorize", params=PARAMS).headers["location"]
    finally:
        reset_auth_code_store()
    assert "error=server_error" in loc
    assert "state=xyz" in loc


def test_authorize_store_down_redirects_temporarily_unavailable(srv, caplog):
    srv.connected = False
    reset_auth_code_store(make_store(srv))
    try:
        with make_client() as client:
            loc = client.get("/authorize", params=PARAMS).headers["location"]
    finally:
        reset_auth_code_store()
    assert "ConnectionError" in caplog.text
    assert "unavailable" in caplog.text
    assert parse_qs(urlparse(loc).query).get("code") is None
    assert "error=temporarily_unavailable" in loc
    assert "state=xyz" in loc


def test_token_store_down_returns_503(client, srv, caplog):
    code = authorize(client)
    srv.connected = False
    reset_auth_code_store(make_store(srv))
    r = exchange(client, code)
    assert r.status_code == 503
    assert r.json()["error"] == "temporarily_unavailable"
    assert r.headers["retry-after"] == str(CODE_STORE_RETRY_AFTER_SECONDS)
    assert 0 < CODE_STORE_RETRY_AFTER_SECONDS <= 60
    assert "ConnectionError" in caplog.text
    assert code[:6] not in caplog.text


def test_logs_carry_no_code_fragment(client, caplog):
    caplog.set_level("DEBUG")
    exchange(client, "abcdefghijklmnop")
    real = authorize(client)
    exchange(client, real, redirect_uri="https://evil.example/cb")
    assert "Invalid authorization code" in caplog.text  # the failures were logged at all
    assert "abcdefghij" not in caplog.text
    assert real[:6] not in caplog.text


def test_corrupt_stored_code_is_invalid_grant(client, srv, caplog):
    code = authorize(client)
    fakeredis.FakeRedis(server=srv).set(code_key(code), b"{truncated", ex=600)
    r = exchange(client, code)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
    assert "unreadable authorization code" in caplog.text
    assert code[:6] not in caplog.text
    assert "truncated" not in caplog.text


def test_health_and_mcp_work_when_redis_down(app_client, auth_headers, srv):
    srv.connected = False
    reset_auth_code_store(make_store(srv))
    try:
        assert app_client.get("/health").status_code == 200
        r = app_client.post(
            "/mcp",
            headers=auth_headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 200
    finally:
        reset_auth_code_store()


@pytest.mark.parametrize("field", ["client_secret", "code", "code_verifier", "redirect_uri"])
def test_token_field_sent_as_file_part_is_a_client_error(client, field):
    """A token-request field sent as a multipart file part is treated as missing, never a 500."""
    data = {
        "grant_type": "authorization_code",
        "code": authorize(client),
        "code_verifier": VERIFIER,
        "redirect_uri": REDIRECT_URI,
        "client_id": "deapi-mcp",
        "client_secret": "test-deapi-token-123",
    }
    data.pop(field)
    r = client.post("/token", data=data, files={field: ("x.txt", b"x" * 20, "text/plain")})
    assert r.status_code in (400, 401)


def test_client_credentials_secret_as_file_part_is_401(client):
    r = client.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "deapi-mcp"},
        files={"client_secret": ("s.txt", b"x" * 20, "text/plain")},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
