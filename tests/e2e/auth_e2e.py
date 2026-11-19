"""End-to-end auth matrix against a running server.

In Docker (``docker compose -f tests/e2e/docker-compose.yml up --build --exit-code-from e2e``)
it runs against the server image and the fake API in ``mock_deapi.py``, outage included.

Against a deployed server it needs a real key and skips the cases that drive the fake API::

    E2E_MCP_URL=https://mcp.example.com E2E_GOOD_KEY=<a valid deAPI key> python tests/e2e/auth_e2e.py

Keys are read from the environment only and never printed. Exits non-zero on the first
failed case.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

MCP_URL = os.environ.get("E2E_MCP_URL", "http://localhost:8000").rstrip("/")
GOOD_KEY = os.environ["E2E_GOOD_KEY"]
# Set only in Docker, where the fake API can be switched into an outage.
MOCK_URL = os.environ.get("E2E_MOCK_URL", "").rstrip("/")

# Random per run, so a verdict cached by an earlier run never answers for this one.
BAD_KEY = f"sk-dp-e2e-bad-{secrets.token_hex(8)}"
REDIRECT_URI = "https://client.example.com/oauth/callback"
VERIFIER = secrets.token_urlsafe(48)
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
        "clientInfo": {"name": "auth-e2e", "version": "0"},
    },
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
GET_BALANCE = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "get_balance", "arguments": {}},
}

http = httpx.Client(timeout=30.0, follow_redirects=False)
failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok else f'  ({detail})'}")
    if not ok:
        failures += 1


def mcp(
    body: dict[str, Any], token: str | None = None, api_key: str | None = None
) -> httpx.Response:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return http.post(f"{MCP_URL}/mcp", headers=headers, json=body)


def rpc_result(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message, from a JSON or an SSE answer."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return dict(json.loads(line[5:]))
        return {}
    return dict(response.json())


def tool_payload(response: httpx.Response) -> dict[str, Any]:
    result = rpc_result(response).get("result", {})
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return dict(structured.get("result", structured))
    return dict(json.loads(result["content"][0]["text"]))


def challenge(response: httpx.Response) -> str:
    return response.headers.get("www-authenticate", "")


def oauth_tokens(secret: str) -> httpx.Response:
    r = http.get(
        f"{MCP_URL}/authorize",
        params={
            "client_id": "deapi-mcp",
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "e2e",
        },
    )
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    return http.post(
        f"{MCP_URL}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT_URI,
            "client_id": "deapi-mcp",
            "client_secret": secret,
        },
    )


def main() -> int:
    # No token, bad JWT
    r = mcp(INITIALIZE)
    check("no token: initialize -> 401", r.status_code == 401, str(r.status_code))
    check("no token: challenge names resource_metadata", "resource_metadata=" in challenge(r))
    r = mcp(INITIALIZE, token="aaa.bbb.ccc")
    check("bad JWT: initialize -> 401", r.status_code == 401, str(r.status_code))
    check("bad JWT: invalid_token", 'error="invalid_token"' in challenge(r))

    # Bad plain key
    for label, body in (("initialize", INITIALIZE), ("tools/list", TOOLS_LIST)):
        r = mcp(body, token=BAD_KEY)
        check(f"bad key: {label} -> 401", r.status_code == 401, str(r.status_code))
    check("bad key: invalid_token", 'error="invalid_token"' in challenge(r))

    # Good plain key
    r = mcp(INITIALIZE, token=GOOD_KEY)
    check("good key: initialize -> 200", r.status_code == 200, str(r.status_code))
    r = mcp(TOOLS_LIST, token=GOOD_KEY)
    tools = rpc_result(r).get("result", {}).get("tools", [])
    check("good key: tools/list -> 200 with tools", r.status_code == 200 and bool(tools))
    r = mcp(GET_BALANCE, token=GOOD_KEY)
    payload = tool_payload(r) if r.status_code == 200 else {}
    check("good key: get_balance success", payload.get("success") is True, str(payload)[:200])

    # The same key in X-API-Key
    r = mcp(INITIALIZE, api_key=GOOD_KEY)
    check("X-API-Key good: initialize -> 200", r.status_code == 200, str(r.status_code))
    r = mcp(GET_BALANCE, api_key=GOOD_KEY)
    payload = tool_payload(r) if r.status_code == 200 else {}
    check("X-API-Key good: get_balance success", payload.get("success") is True)
    r = mcp(INITIALIZE, api_key=BAD_KEY)
    check("X-API-Key bad: initialize -> 401", r.status_code == 401, str(r.status_code))

    # OAuth: Authorization Code + PKCE, then refresh
    r = oauth_tokens(GOOD_KEY)
    check("oauth: good key -> tokens", r.status_code == 200, str(r.status_code))
    tokens = r.json() if r.status_code == 200 else {}
    if tokens:
        r = mcp(INITIALIZE, token=tokens["access_token"])
        check("oauth: JWT initialize -> 200", r.status_code == 200, str(r.status_code))
        r = mcp(GET_BALANCE, token=tokens["access_token"])
        payload = tool_payload(r) if r.status_code == 200 else {}
        check("oauth: JWT get_balance success", payload.get("success") is True)
        r = http.post(
            f"{MCP_URL}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": "deapi-mcp",
            },
        )
        check("oauth: refresh -> 200", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            r = mcp(TOOLS_LIST, token=r.json()["access_token"])
            check("oauth: refreshed JWT tools/list -> 200", r.status_code == 200)

    r = http.post(
        f"{MCP_URL}/token",
        data={"grant_type": "client_credentials"},
        auth=("deapi-mcp", GOOD_KEY),
    )
    check("client_secret_basic: good key -> 200", r.status_code == 200, str(r.status_code))
    check("token response: no-store", r.headers.get("cache-control") == "no-store")

    r = oauth_tokens(BAD_KEY)
    check(
        "oauth: bad key -> 401 invalid_client",
        r.status_code == 401 and r.json().get("error") == "invalid_client",
        f"{r.status_code} {r.text[:120]}",
    )
    r = http.post(
        f"{MCP_URL}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "deapi-mcp",
            "client_secret": BAD_KEY,
        },
    )
    check(
        "client_credentials: bad key -> 401 invalid_client",
        r.status_code == 401 and r.json().get("error") == "invalid_client",
        f"{r.status_code}",
    )

    # Setup page
    r = http.get(f"{MCP_URL}/")
    check(
        "GET / -> setup page",
        r.status_code == 200 and f"{MCP_URL}/mcp" in r.text,
        str(r.status_code),
    )
    check(
        "GET / -> strict CSP",
        "default-src 'none'" in r.headers.get("content-security-policy", ""),
    )

    if MOCK_URL:
        calls = http.get(f"{MOCK_URL}/_control/calls").json()
        # Bad key: Bearer and X-API-Key, /token twice -> one probe, the rest from the cache.
        check("bad key probed once", calls.get("other") == 1, str(calls))

        # Outage: a never-seen key is let through, as before the check existed.
        http.post(f"{MOCK_URL}/_control/mode", json={"mode": "down"})
        try:
            unseen = f"sk-dp-e2e-unseen-{secrets.token_hex(8)}"
            r = mcp(INITIALIZE, token=unseen)
            check(
                "API down: unseen key initialize -> 200", r.status_code == 200, str(r.status_code)
            )
        finally:
            http.post(f"{MOCK_URL}/_control/mode", json={"mode": "up"})

    print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
