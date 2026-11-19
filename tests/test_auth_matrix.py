"""Every way to authenticate, through the full app, with the API key check on.

The deAPI API is replaced by a fake that knows one good key. Cases:

- no token, a bad JWT, a bad plain key  -> 401 at initialize;
- a good plain key                      -> 200 for initialize and tools/list;
- the API down or slow                  -> a plain key is let through (fail-open);
- OAuth Authorization Code + PKCE and refresh with a good key -> tokens that work;
- a bad key as client_secret            -> invalid_client, for both grants that take one;
- the check switched off                -> a bad plain key is let through, as before.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from deapi_mcp import key_verifier
from deapi_mcp.config import settings
from deapi_mcp.observability import set_auth_rejected_sink

GOOD = "sk-dp-good-0123456789"
BAD = "sk-dp-bad-0123456789"

REDIRECT_URI = "https://client.example.com/oauth/callback"
VERIFIER = "v" * 50
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


class FakeApi:
    def __init__(self):
        self.mode = "up"
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "down":
            return httpx.Response(503, json={"message": "Service Unavailable"})
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key == GOOD:
            return httpx.Response(200, json={"data": {"balance": 1.0}})
        return httpx.Response(401, json={"message": "Unauthenticated."})


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(key_verifier, "_shared_http_client", lambda: client)
    monkeypatch.setattr(settings, "verify_api_keys", True)
    return fake


@pytest.fixture
def rejected():
    reasons: list[str] = []
    set_auth_rejected_sink(reasons.append)
    yield reasons
    set_auth_rejected_sink(None)


def mcp(app_client, body, token=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return app_client.post("/mcp", headers=headers, json=body)


def authorize(app_client):
    r = app_client.get(
        "/authorize",
        params={
            "client_id": "deapi-mcp",
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 307)
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def exchange(app_client, code, secret):
    return app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT_URI,
            "client_id": "deapi-mcp",
            "client_secret": secret,
        },
    )


# --- bearer -----------------------------------------------------------------


def test_no_token_is_401(app_client, api):
    r = mcp(app_client, INITIALIZE)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert api.calls == 0


def test_bad_jwt_is_401_without_asking_the_api(app_client, api, rejected):
    r = mcp(app_client, INITIALIZE, token="aaa.bbb.ccc")
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert api.calls == 0
    assert rejected == ["invalid_or_expired"]


@pytest.mark.parametrize("body", [INITIALIZE, TOOLS_LIST], ids=["initialize", "tools/list"])
def test_bad_plain_key_is_401(app_client, api, rejected, body):
    r = mcp(app_client, body, token=BAD)
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert "resource_metadata=" in r.headers["www-authenticate"]
    assert rejected == ["invalid_key"]


@pytest.mark.parametrize("body", [INITIALIZE, TOOLS_LIST], ids=["initialize", "tools/list"])
def test_good_plain_key_is_accepted(app_client, api, rejected, body):
    r = mcp(app_client, body, token=GOOD)
    assert r.status_code == 200
    assert rejected == []


def test_good_plain_key_is_checked_once_across_requests(app_client, api):
    for _ in range(5):
        assert mcp(app_client, TOOLS_LIST, token=GOOD).status_code == 200
    assert api.calls == 1


def test_bad_plain_key_is_checked_once_across_requests(app_client, api):
    for _ in range(5):
        assert mcp(app_client, TOOLS_LIST, token=BAD).status_code == 401
    assert api.calls == 1


@pytest.mark.parametrize("mode", ["down", "timeout"])
def test_api_outage_lets_a_plain_key_through(app_client, api, mode):
    api.mode = mode
    assert mcp(app_client, INITIALIZE, token=BAD).status_code == 200


def test_switched_off_lets_a_bad_plain_key_through(app_client, api, monkeypatch):
    monkeypatch.setattr(settings, "verify_api_keys", False)
    assert mcp(app_client, INITIALIZE, token=BAD).status_code == 200
    assert api.calls == 0


# --- OAuth ------------------------------------------------------------------


def test_oauth_pkce_and_refresh_with_a_good_key(app_client, api):
    r = exchange(app_client, authorize(app_client), GOOD)
    assert r.status_code == 200
    tokens = r.json()

    assert mcp(app_client, INITIALIZE, token=tokens["access_token"]).status_code == 200
    assert mcp(app_client, TOOLS_LIST, token=tokens["access_token"]).status_code == 200

    r = app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": "deapi-mcp",
        },
    )
    assert r.status_code == 200
    refreshed = r.json()
    assert refreshed["refresh_token"]
    assert mcp(app_client, TOOLS_LIST, token=refreshed["access_token"]).status_code == 200
    # The key was checked once, at /token; the JWT is trusted by its signature.
    assert api.calls == 1


def test_oauth_with_a_bad_key_is_invalid_client(app_client, api):
    r = exchange(app_client, authorize(app_client), BAD)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
    assert BAD not in r.text


def test_client_credentials_with_a_bad_key_is_invalid_client(app_client, api):
    r = app_client.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "deapi-mcp", "client_secret": BAD},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_client_credentials_with_a_good_key_issues_tokens(app_client, api):
    r = app_client.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "deapi-mcp", "client_secret": GOOD},
    )
    assert r.status_code == 200
    assert mcp(app_client, INITIALIZE, token=r.json()["access_token"]).status_code == 200


def test_oauth_during_an_api_outage_still_issues_tokens(app_client, api):
    api.mode = "down"
    r = exchange(app_client, authorize(app_client), BAD)
    assert r.status_code == 200
