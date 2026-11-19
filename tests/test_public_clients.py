"""OAuth for public clients: registration (DCR, CIMD), the sign-in screen, code and refresh.

The deAPI API is a fake that knows one good key; the key check is on.
"""

import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from deapi_mcp import consent, key_verifier, oauth_clients
from deapi_mcp.config import settings
from deapi_mcp.observability import set_auth_rejected_sink

GOOD = "sk-dp-good-0123456789"
BAD = "sk-dp-bad-0123456789"
REDIRECT = "https://client.example.com/oauth/callback"
# PUBLIC_BASE_URL is https in these tests, so the CSRF cookie is Secure: talk https.
AUTHORIZE = "https://testserver/authorize"
VERIFIER = "w" * 50
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


class FakeApi:
    def __init__(self):
        self.mode = "up"
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "down":
            return httpx.Response(503, json={"message": "Service Unavailable"})
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
    consent.reset_limiter(None)
    oauth_clients.clear_cimd_cache()
    yield fake
    consent.reset_limiter(None)
    oauth_clients.clear_cimd_cache()


@pytest.fixture
def rejected():
    reasons: list[str] = []
    set_auth_rejected_sink(reasons.append)
    yield reasons
    set_auth_rejected_sink(None)


def register(app_client, **metadata):
    body = {"client_name": "Test App", "redirect_uris": [REDIRECT], **metadata}
    return app_client.post("/register", json=body)


def dcr_client(app_client, **metadata) -> str:
    r = register(app_client, **metadata)
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize(app_client, client_id, redirect_uri=REDIRECT, **extra):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "st-1",
        **extra,
    }
    params = {k: v for k, v in params.items() if v is not None}
    return app_client.get(AUTHORIZE, params=params, follow_redirects=False)


def form_fields(page: str) -> dict:
    return dict(re.findall(r'<input type=hidden name=(\w+) value="([^"]*)"', page))


def submit(app_client, page_response, api_key=GOOD, action="allow", **override):
    fields = {**form_fields(page_response.text), "api_key": api_key, "action": action, **override}
    return app_client.post(AUTHORIZE, data=fields, follow_redirects=False)


def location_query(response) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


def exchange(app_client, client_id, code, redirect_uri=REDIRECT, verifier=VERIFIER):
    return app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        },
    )


def sign_in(app_client, client_id, redirect_uri=REDIRECT) -> dict:
    page = authorize(app_client, client_id, redirect_uri)
    assert page.status_code == 200, page.text
    done = submit(app_client, page)
    assert done.status_code == 303, done.text
    code = location_query(done)["code"]
    r = exchange(app_client, client_id, code, redirect_uri)
    assert r.status_code == 200, r.text
    return r.json()


def mcp(app_client, token):
    return app_client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        json=INITIALIZE,
    )


# --- dynamic client registration -----------------------------------------------------------


def test_registration_returns_a_public_client(app_client):
    r = register(app_client)
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"].startswith("dcr.")
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [REDIRECT]
    assert "client_secret" not in body
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "uris",
    [
        [],
        ["javascript:alert(1)"],
        ["data:text/html,x"],
        ["http://client.example.com/cb"],  # plain http only on loopback
        ["https://client.example.com/cb#frag"],
        ["https://a.example.com/cb"] * 6,
        ["https://client.example.com/" + "x" * 600],
        "https://client.example.com/cb",
    ],
)
def test_registration_refuses_bad_redirect_uris(app_client, uris):
    r = register(app_client, redirect_uris=uris)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "metadata",
    [
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"grant_types": ["client_credentials"]},
        {"response_types": ["token"]},
    ],
)
def test_registration_refuses_confidential_or_implicit_clients(app_client, metadata):
    r = register(app_client, **metadata)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize(
    "uri",
    [
        "https://client.example.com/cb",
        "http://127.0.0.1/callback",
        "http://localhost:8080/callback",
        "cursor://anysphere.cursor-mcp/oauth/callback",
    ],
)
def test_registration_accepts_web_loopback_and_native_redirects(app_client, uri):
    assert register(app_client, redirect_uris=[uri]).status_code == 201


def test_registration_rejects_non_json(app_client):
    r = app_client.post("/register", content=b"not json", headers={"Content-Type": "text/plain"})
    assert r.status_code == 400


def test_tampered_dcr_client_id_is_unknown(app_client, api):
    client_id = dcr_client(app_client)
    payload, signature = client_id[len("dcr.") :].split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["r"] = ["https://evil.example/cb"]
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    forged = f"dcr.{forged_payload}.{signature}"
    r = authorize(app_client, forged, "https://evil.example/cb")
    assert r.status_code == 400
    assert "location" not in r.headers


def test_client_name_is_cleaned_and_shortened(app_client, api):
    client_id = dcr_client(app_client, client_name="  Evil\x00\x1b Name " + "x" * 200)
    page = authorize(app_client, client_id)
    assert "\x00" not in page.text and "\x1b" not in page.text
    assert "Evil Name" in page.text


# --- the sign-in screen ------------------------------------------------------------------


def test_unregistered_redirect_gets_an_error_page_not_a_redirect(app_client, api):
    client_id = dcr_client(app_client)
    r = authorize(app_client, client_id, "https://evil.example/steal")
    assert r.status_code == 400
    assert "location" not in r.headers
    assert "has not registered" in r.text


def test_unresolvable_public_client_gets_an_error_page(app_client, api):
    r = authorize(app_client, "dcr.garbage.garbage")
    assert r.status_code == 400
    assert "location" not in r.headers


def test_sign_in_page_for_a_registered_client(app_client, api):
    client_id = dcr_client(app_client, client_name="<b>Tool</b>")
    page = authorize(app_client, client_id)
    assert page.status_code == 200
    assert "&lt;b&gt;Tool&lt;/b&gt;" in page.text and "<b>Tool</b>" not in page.text
    assert "Unverified application" in page.text
    assert "type=password" in page.text
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://client.example.com" in csp
    assert "frame-ancestors 'none'" in csp and "script-src" not in csp
    assert page.headers["cache-control"] == "no-store"
    cookie = page.headers["set-cookie"].lower()
    assert cookie.startswith("__host-deapi_mcp_csrf=")
    assert "samesite=strict" in cookie and "secure" in cookie and "httponly" in cookie
    assert "<script" not in page.text


def test_missing_redirect_uri_uses_the_only_registered_one(app_client, api):
    client_id = dcr_client(app_client)
    assert authorize(app_client, client_id, redirect_uri=None).status_code == 200


def test_loopback_redirect_matches_on_any_port(app_client, api):
    client_id = dcr_client(app_client, redirect_uris=["http://127.0.0.1/callback"])
    page = authorize(app_client, client_id, "http://127.0.0.1:53682/callback")
    assert page.status_code == 200
    assert "app on this computer" in page.text
    assert "form-action 'self' http://127.0.0.1:53682" in page.headers["content-security-policy"]
    assert authorize(app_client, client_id, "http://127.0.0.1:53682/other").status_code == 400


def test_native_scheme_redirect(app_client, api):
    uri = "cursor://anysphere.cursor-mcp/oauth/callback"
    client_id = dcr_client(app_client, redirect_uris=[uri])
    page = authorize(app_client, client_id, uri)
    assert page.status_code == 200
    assert "form-action 'self' cursor:" in page.headers["content-security-policy"]
    tokens = sign_in(app_client, client_id, uri)
    assert tokens["access_token"]


@pytest.mark.parametrize(
    "extra, error",
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
    ],
)
def test_bad_requests_go_back_to_the_registered_redirect(app_client, api, extra, error):
    client_id = dcr_client(app_client)
    r = authorize(app_client, client_id, **extra)
    assert r.status_code in (302, 307)
    q = location_query(r)
    assert r.headers["location"].startswith(REDIRECT)
    assert q["error"] == error and q["state"] == "st-1" and q["iss"] == "https://mcp.test"


def test_our_resource_is_accepted(app_client, api):
    client_id = dcr_client(app_client)
    for resource in ("https://mcp.test/mcp", "https://mcp.test/mcp/", "https://mcp.test"):
        assert authorize(app_client, client_id, resource=resource).status_code == 200


def test_full_sign_in_issues_working_tokens(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    done = submit(app_client, page)
    assert done.status_code == 303
    q = location_query(done)
    assert done.headers["location"].startswith(REDIRECT + "?")
    assert q["state"] == "st-1" and q["iss"] == "https://mcp.test" and q["code"]
    r = exchange(app_client, client_id, q["code"])
    assert r.status_code == 200
    tokens = r.json()
    assert mcp(app_client, tokens["access_token"]).status_code == 200

    refreshed = app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200
    assert mcp(app_client, refreshed.json()["access_token"]).status_code == 200


def test_cancel_returns_access_denied(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    r = submit(app_client, page, api_key="", action="deny")
    assert r.status_code == 303
    q = location_query(r)
    assert q["error"] == "access_denied" and q["state"] == "st-1"
    assert api.calls == 0


def test_wrong_key_is_shown_on_the_page_and_never_echoed(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    r = submit(app_client, page, api_key=BAD)
    assert r.status_code == 400
    assert "did not accept this key" in r.text
    assert BAD not in r.text
    assert "location" not in r.headers


def test_too_many_wrong_keys_are_refused(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    for i in range(consent.MAX_KEY_FAILURES):
        assert submit(app_client, page, api_key=f"sk-dp-wrong-{i:010d}").status_code == 400
    blocked = submit(app_client, page, api_key=GOOD)
    assert blocked.status_code == 429
    assert "Too many attempts" in blocked.text


def test_deapi_down_does_not_issue_a_code(app_client, api):
    api.mode = "down"
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    r = submit(app_client, page)
    assert r.status_code == 503
    assert "location" not in r.headers


def test_short_key_is_asked_again(app_client, api):
    client_id = dcr_client(app_client)
    r = submit(app_client, authorize(app_client, client_id), api_key="short")
    assert r.status_code == 400
    assert api.calls == 0


def test_form_without_the_csrf_cookie_is_refused(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    app_client.cookies.clear()
    r = submit(app_client, page)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_form_with_a_different_csrf_value_is_refused(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    r = submit(app_client, page, csrf="x" * 43)
    assert r.status_code == 400


def test_tampered_request_is_refused(app_client, api):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    fields = form_fields(page.text)
    body, _, signature = fields["request"].partition(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    data["redirect_uri"] = "https://evil.example/cb"
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    r = submit(app_client, page, request=f"{forged}.{signature}")
    assert r.status_code == 400
    assert "location" not in r.headers


def test_expired_request_is_refused(app_client, api, monkeypatch):
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    monkeypatch.setattr(consent.time, "time", lambda: 10**12)
    assert submit(app_client, page).status_code == 400


# --- token endpoint ------------------------------------------------------------------------


def test_code_is_bound_to_its_client(app_client, api):
    client_a = dcr_client(app_client)
    client_b = dcr_client(app_client, client_name="Other")
    done = submit(app_client, authorize(app_client, client_a))
    r = exchange(app_client, client_b, location_query(done)["code"])
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_public_client_code_cannot_be_redeemed_as_deapi_mcp(app_client, api):
    client_id = dcr_client(app_client)
    done = submit(app_client, authorize(app_client, client_id))
    r = app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": "deapi-mcp",
            "client_secret": GOOD,
            "code": location_query(done)["code"],
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert r.json()["error"] == "invalid_grant"


def test_wrong_verifier_is_refused(app_client, api):
    client_id = dcr_client(app_client)
    done = submit(app_client, authorize(app_client, client_id))
    r = exchange(app_client, client_id, location_query(done)["code"], verifier="v" * 50)
    assert r.json()["error"] == "invalid_grant"


def test_refresh_token_is_bound_to_its_client(app_client, api):
    client_a = dcr_client(app_client)
    client_b = dcr_client(app_client, client_name="Other")
    tokens = sign_in(app_client, client_a)
    r = app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_b,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert r.json()["error"] == "invalid_grant"


def test_public_client_cannot_use_client_credentials(app_client, api):
    client_id = dcr_client(app_client)
    r = app_client.post("/token", data={"grant_type": "client_credentials", "client_id": client_id})
    assert r.status_code == 400
    assert r.json()["error"] == "unauthorized_client"


def test_unknown_client_at_token_is_invalid_client(app_client, api):
    r = app_client.post(
        "/token",
        data={"grant_type": "authorization_code", "client_id": "dcr.x.y", "code": "c"},
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_token_resource_must_be_ours(app_client, api):
    client_id = dcr_client(app_client)
    done = submit(app_client, authorize(app_client, client_id))
    r = app_client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": location_query(done)["code"],
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
            "resource": "https://other.example/mcp",
        },
    )
    assert r.json()["error"] == "invalid_target"


def test_preregistered_flow_also_returns_iss(app_client, api):
    r = app_client.get(
        "/authorize",
        params={
            "client_id": "deapi-mcp",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "x",
        },
        follow_redirects=False,
    )
    q = location_query(r)
    assert q["iss"] == "https://mcp.test" and q["code"] and q["state"] == "x"


# --- client ID metadata documents --------------------------------------------------------

CIMD_ID = "https://app.example.com/oauth/client.json"


@pytest.fixture
def cimd(monkeypatch):
    docs = {}
    fetched = []

    async def fake_fetch(url):
        fetched.append(url)
        if url not in docs:
            raise oauth_clients.InputError("nope")
        return json.dumps(docs[url]).encode(), "application/json"

    monkeypatch.setattr(oauth_clients, "_fetch_metadata", fake_fetch)
    docs[CIMD_ID] = {
        "client_id": CIMD_ID,
        "client_name": "Example App",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    }
    return docs, fetched


def test_cimd_client_signs_in_and_shows_its_domain(app_client, api, cimd):
    page = authorize(app_client, CIMD_ID)
    assert page.status_code == 200
    assert "Example App" in page.text
    assert "published at app.example.com" in page.text
    assert "&#10003;" not in page.text
    assert "Unverified application" not in page.text
    # The document is on app.example.com, the redirect on client.example.com
    assert "Access goes to a different site" in page.text
    tokens = sign_in(app_client, CIMD_ID)
    assert mcp(app_client, tokens["access_token"]).status_code == 200


def test_cimd_document_is_cached(app_client, api, cimd):
    _, fetched = cimd
    authorize(app_client, CIMD_ID)
    authorize(app_client, CIMD_ID)
    assert fetched == [CIMD_ID]


def test_cimd_document_for_another_client_id_is_refused(app_client, api, cimd):
    docs, _ = cimd
    docs[CIMD_ID]["client_id"] = "https://other.example/client.json"
    r = authorize(app_client, CIMD_ID)
    assert r.status_code == 400
    assert "location" not in r.headers


def test_cimd_confidential_client_is_refused(app_client, api, cimd):
    docs, _ = cimd
    docs[CIMD_ID]["token_endpoint_auth_method"] = "private_key_jwt"
    assert authorize(app_client, CIMD_ID).status_code == 400


def test_failed_cimd_fetch_is_retried_only_after_a_pause(app_client, api, cimd, monkeypatch):
    docs, fetched = cimd
    other = "https://app.example.com/oauth/missing.json"
    authorize(app_client, other)
    authorize(app_client, other)
    assert fetched.count(other) == 1
    monkeypatch.setattr(oauth_clients, "CIMD_FAILURE_TTL", 0)
    oauth_clients.clear_cimd_cache()
    docs[other] = {**docs[CIMD_ID], "client_id": other}
    assert authorize(app_client, other).status_code == 200


@pytest.mark.parametrize(
    "client_id, expected",
    [
        ("https://app.example.com/client.json", True),
        ("https://app.example.com/", False),
        ("https://app.example.com", False),
        ("http://app.example.com/client.json", False),
        ("https://user:pw@app.example.com/client.json", False),
        ("https://app.example.com/client.json#x", False),
        ("https://203.0.113.5/client.json", False),
        ("deapi-mcp", False),
    ],
)
def test_cimd_client_id_shape(client_id, expected):
    assert oauth_clients.is_cimd_client_id(client_id) is expected


# --- switched off ---------------------------------------------------------------------------


def test_switched_off_hides_registration_and_public_clients(app_client, api, monkeypatch):
    client_id = dcr_client(app_client)
    monkeypatch.setattr(settings, "oauth_public_clients", False)
    meta = app_client.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in meta
    assert "client_id_metadata_document_supported" not in meta
    r = authorize(app_client, client_id)
    assert r.status_code == 400 and r.json()["error"] == "unauthorized_client"


# --- a key deAPI refuses on a real call ends the session --------------------------------------


def test_refused_key_ends_an_oauth_session(app_client, api, rejected):
    client_id = dcr_client(app_client)
    tokens = sign_in(app_client, client_id)
    assert mcp(app_client, tokens["access_token"]).status_code == 200

    key_verifier.note_key_refused(GOOD)  # what a 401 on a tool call does

    r = mcp(app_client, tokens["access_token"])
    assert r.status_code == 401
    assert rejected == ["invalid_key"]
    refreshed = app_client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.json()["error"] == "invalid_grant"


async def test_a_401_from_deapi_marks_the_key_and_explains(api, monkeypatch):
    from deapi_mcp.deapi_client import DeapiAPIError, DeapiClient

    async def refused(self, *args, **kwargs):
        from deapi_sdk._exceptions import AuthenticationError

        raise AuthenticationError(
            "Unauthenticated.", status_code=401, body={"message": "Unauthenticated."}
        )

    monkeypatch.setattr("deapi_sdk._client.AsyncHTTPClient.request", refused)
    async with DeapiClient(BAD) as client:
        with pytest.raises(DeapiAPIError) as info:
            await client.get_balance()
    assert info.value.status_code == 401
    assert "app.deapi.ai/settings/api-keys" in str(info.value)
    assert key_verifier.is_key_known_invalid(BAD)
    assert not key_verifier.is_key_known_invalid(GOOD)


def test_cimd_redirect_on_its_own_site_has_no_warning(app_client, api, cimd):
    docs, _ = cimd
    own = "https://login.app.example.com/oauth/callback"
    docs[CIMD_ID]["redirect_uris"] = [own]
    page = authorize(app_client, CIMD_ID, own)
    assert page.status_code == 200
    assert "class=warn" not in page.text


@pytest.mark.parametrize(
    "uri",
    [
        "ms-msdt:/id",
        "search-ms:query=x",
        "smb://attacker.example/x",
        "intent://x#Intent;end",
        "mailto:a@example.com",
        "vscodeish://x/cb",
        "ftp://example.com/x",
    ],
)
def test_os_protocol_handlers_are_refused(app_client, uri):
    assert register(app_client, redirect_uris=[uri]).status_code == 400


def test_reverse_domain_native_scheme_is_accepted(app_client):
    assert register(app_client, redirect_uris=["com.example.app:/oauth/cb"]).status_code == 201


@pytest.mark.parametrize(
    "name",
    ["deAPI", "deAPI Login", "de-api official", "D E A P I", "de​api"],
)
def test_names_posing_as_deapi_are_replaced(app_client, api, name):
    client_id = dcr_client(app_client, client_name=name)
    page = authorize(app_client, client_id)
    assert "Unnamed application" in page.text


def test_bidi_and_invisible_characters_are_removed(app_client, api):
    client_id = dcr_client(app_client, client_name="Safe‮ppA​ Tool")
    page = authorize(app_client, client_id)
    assert "‮" not in page.text and "​" not in page.text
    assert "SafeppA Tool" in page.text


def test_dcr_name_is_marked_unverified(app_client, api):
    client_id = dcr_client(app_client, client_name="Claude")
    page = authorize(app_client, client_id)
    assert "&ldquo;Claude&rdquo; (unverified)" in page.text
    assert "Access goes to <b>client.example.com</b>" in page.text


def test_malformed_loopback_port_is_refused_not_a_crash(app_client, api):
    client_id = dcr_client(app_client, redirect_uris=["http://127.0.0.1/callback"])
    for uri in ("http://127.0.0.1:abc/callback", "http://127.0.0.1:99999/callback"):
        r = authorize(app_client, client_id, uri)
        assert r.status_code == 400
        assert "location" not in r.headers


def test_oversized_registration_is_refused(app_client):
    big = {"client_name": "x", "redirect_uris": [REDIRECT], "padding": "x" * 9000}
    r = app_client.post("/register", json=big)
    assert r.status_code == 400


def test_limiter_counts_the_trusted_header_when_configured(app_client, api, monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", "CF-Connecting-IP")
    client_id = dcr_client(app_client)
    page = authorize(app_client, client_id)
    fields = {**form_fields(page.text), "action": "allow"}
    for i in range(consent.MAX_KEY_FAILURES):
        app_client.post(
            AUTHORIZE,
            data={**fields, "api_key": f"sk-dp-wrong-{i:010d}"},
            headers={"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": f"10.0.0.{i}"},
            follow_redirects=False,
        )
    # Rotating X-Forwarded-For does not help: the trusted header is what counts
    blocked = app_client.post(
        AUTHORIZE,
        data={**fields, "api_key": GOOD},
        headers={"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": "10.9.9.9"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    other = app_client.post(
        AUTHORIZE,
        data={**fields, "api_key": GOOD},
        headers={"CF-Connecting-IP": "203.0.113.9"},
        follow_redirects=False,
    )
    assert other.status_code == 303


async def test_slow_metadata_server_times_out(monkeypatch):
    import asyncio

    async def slow(url):
        await asyncio.sleep(10)

    monkeypatch.setattr(oauth_clients, "CIMD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(oauth_clients, "_fetch_metadata", slow)
    oauth_clients.clear_cimd_cache()
    assert await oauth_clients.fetch_cimd_client(CIMD_ID) is None
    oauth_clients.clear_cimd_cache()
