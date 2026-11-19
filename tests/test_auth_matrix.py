"""Every way to authenticate, through the full app, with the API key check on.

The deAPI API is replaced by a fake that knows one good key. Cases:

- no token, a bad JWT, a bad plain key  -> 401 at initialize;
- a good plain key                      -> 200 for initialize and tools/list;
- the API down or slow                  -> a plain key is let through (fail-open);
- OAuth Authorization Code + PKCE and refresh with a good key -> tokens that work;
- a bad key as client_secret            -> invalid_client, for both grants that take one;
- the key in X-API-Key instead of Authorization -> the same checks;
- client_secret_basic, refresh with a revoked key, Cursor's callback, PKCE format;
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
        self.good = {GOOD}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "down":
            return httpx.Response(503, json={"message": "Service Unavailable"})
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key in self.good:
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


def mcp(app_client, body, token=None, api_key=None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return app_client.post("/mcp", headers=headers, json=body)


def authorize_params(**overrides):
    params = {
        "client_id": "deapi-mcp",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
    }
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def authorize(app_client, redirect_uri=REDIRECT_URI):
    r = app_client.get(
        "/authorize", params=authorize_params(redirect_uri=redirect_uri), follow_redirects=False
    )
    assert r.status_code in (302, 307)
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def exchange(app_client, code, secret, redirect_uri=REDIRECT_URI, basic=False):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": redirect_uri,
    }
    if basic:
        return app_client.post("/token", data=data, auth=("deapi-mcp", secret))
    return app_client.post(
        "/token", data={**data, "client_id": "deapi-mcp", "client_secret": secret}
    )


def refresh(app_client, refresh_token):
    return app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "deapi-mcp",
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

    r = refresh(app_client, tokens["refresh_token"])
    assert r.status_code == 200
    refreshed = r.json()
    assert refreshed["refresh_token"]
    assert mcp(app_client, TOOLS_LIST, token=refreshed["access_token"]).status_code == 200
    # The key was checked at /token, and at refresh again from the cache; the JWT
    # itself is trusted by its signature.
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


# --- X-API-Key --------------------------------------------------------------


@pytest.mark.parametrize("body", [INITIALIZE, TOOLS_LIST], ids=["initialize", "tools/list"])
def test_good_key_in_x_api_key_is_accepted(app_client, api, body):
    assert mcp(app_client, body, api_key=GOOD).status_code == 200


def test_bad_key_in_x_api_key_is_401(app_client, api, rejected):
    r = mcp(app_client, INITIALIZE, api_key=BAD)
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]
    assert rejected == ["invalid_key"]


def test_authorization_wins_over_x_api_key(app_client, api):
    assert mcp(app_client, INITIALIZE, token=BAD, api_key=GOOD).status_code == 401
    assert mcp(app_client, INITIALIZE, token=GOOD, api_key=BAD).status_code == 200


def test_blank_x_api_key_is_no_token(app_client, api):
    r = mcp(app_client, INITIALIZE, api_key="  ")
    assert r.status_code == 401
    assert api.calls == 0


def test_jwt_in_x_api_key_is_accepted(app_client, api):
    tokens = exchange(app_client, authorize(app_client), GOOD).json()
    assert mcp(app_client, INITIALIZE, api_key=tokens["access_token"]).status_code == 200


# --- token endpoint ---------------------------------------------------------


def test_token_responses_are_not_cached(app_client, api):
    ok = exchange(app_client, authorize(app_client), GOOD)
    bad = exchange(app_client, authorize(app_client), BAD)
    for r in (ok, bad):
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["pragma"] == "no-cache"


def test_client_secret_basic_issues_tokens(app_client, api):
    r = exchange(app_client, authorize(app_client), GOOD, basic=True)
    assert r.status_code == 200
    assert mcp(app_client, INITIALIZE, token=r.json()["access_token"]).status_code == 200


def test_client_secret_basic_with_a_bad_key_gets_the_basic_challenge(app_client, api):
    r = exchange(app_client, authorize(app_client), BAD, basic=True)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
    assert r.headers["www-authenticate"].startswith("Basic ")


def test_client_secret_basic_decodes_form_encoding(app_client, api):
    api.good.add("sk|dp:key-0123456789")
    r = exchange(app_client, authorize(app_client), "sk%7Cdp%3Akey-0123456789", basic=True)
    assert r.status_code == 200


def test_malformed_basic_is_invalid_client(app_client, api):
    r = app_client.post(
        "/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": "Basic !!!not-base64"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_basic_and_form_client_ids_must_agree(app_client, api):
    r = app_client.post(
        "/token",
        data={"grant_type": "client_credentials", "client_id": "other"},
        auth=("deapi-mcp", GOOD),
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_refresh_with_a_revoked_key_is_invalid_grant(app_client, api):
    tokens = exchange(app_client, authorize(app_client), GOOD).json()
    api.good.clear()
    key_verifier.get_key_verifier().clear()
    r = refresh(app_client, tokens["refresh_token"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_refresh_during_an_api_outage_still_works(app_client, api):
    tokens = exchange(app_client, authorize(app_client), GOOD).json()
    api.mode = "down"
    key_verifier.get_key_verifier().clear()
    assert refresh(app_client, tokens["refresh_token"]).status_code == 200


def test_metadata_documents_only_what_is_supported(app_client):
    meta = app_client.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert set(meta["token_endpoint_auth_methods_supported"]) == {
        "client_secret_post",
        "client_secret_basic",
    }
    assert "registration_endpoint" not in meta
    assert "client_id_metadata_document_supported" not in meta
    assert meta["service_documentation"].startswith("https://docs.deapi.ai/")


# --- authorize --------------------------------------------------------------

CURSOR_CALLBACK = "cursor://anysphere.cursor-mcp/oauth/callback"


def test_cursor_callback_completes_the_flow(app_client, api):
    code = authorize(app_client, redirect_uri=CURSOR_CALLBACK)
    r = exchange(app_client, code, GOOD, redirect_uri=CURSOR_CALLBACK)
    assert r.status_code == 200


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "cursor://anysphere.cursor-mcp/oauth/other",
        "cursor://evil.example/oauth/callback",
        "javascript:alert(1)",
        "data:text/html,hi",
        "myapp://callback",
    ],
)
def test_other_private_scheme_callbacks_are_refused(app_client, redirect_uri):
    r = app_client.get(
        "/authorize", params=authorize_params(redirect_uri=redirect_uri), follow_redirects=False
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


@pytest.mark.parametrize(
    "overrides",
    [
        {"code_challenge_method": "plain", "code_challenge": VERIFIER},
        {"code_challenge_method": None},
        {"code_challenge": None},
        {"code_challenge": "too-short"},
        {"code_challenge": CHALLENGE + "x"},
        {"code_challenge": CHALLENGE[:-1] + "="},
    ],
    ids=["plain", "no-method", "no-challenge", "short", "long", "padding"],
)
def test_pkce_must_be_a_well_formed_s256_challenge(app_client, overrides):
    r = app_client.get("/authorize", params=authorize_params(**overrides), follow_redirects=False)
    location = parse_qs(urlparse(r.headers["location"]).query)
    assert location["error"] == ["invalid_request"]
    assert "code" not in location
