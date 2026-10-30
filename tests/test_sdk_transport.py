"""The MCP client delegates transport to the deAPI SDK over a shared pool.

These exercise the real path (no mocking of ``_request``): a request must reach
the right URL as the right method with per-request auth, the response must parse,
and a non-2xx must come back as the ``DeapiAPIError`` shape the tools handle,
with the message preserved.
"""

import asyncio

import httpx
import pytest

import deapi_mcp.deapi_client as dc
from deapi_mcp.deapi_client import DeapiAPIError, DeapiClient


def _route(monkeypatch, handler):
    """Point the process-wide shared transport at a per-test MockTransport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dc, "_shared_http_client", lambda: client)
    return client


async def test_submit_job_goes_through_sdk_with_per_request_auth(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"request_id": "req-1"}})

    transport = _route(monkeypatch, handler)
    try:
        async with DeapiClient(api_token="tok") as client:
            resp = await client.submit_job("images/generations", json_data={"model": "m"})
    finally:
        await transport.aclose()

    assert resp.data.request_id == "req-1"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v2/images/generations")
    assert seen["auth"] == "Bearer tok"  # auth rides the request, not the shared pool


async def test_http_error_maps_to_deapi_error_with_message(monkeypatch):
    def handler(request):
        return httpx.Response(422, json={"message": "bad", "errors": {"model": ["required"]}})

    transport = _route(monkeypatch, handler)
    try:
        async with DeapiClient(api_token="tok") as client:
            with pytest.raises(DeapiAPIError) as excinfo:
                await client.calculate_price("images/generations/price", json_data={})
    finally:
        await transport.aclose()

    assert excinfo.value.status_code == 422
    assert "model: required" in str(excinfo.value)


async def test_get_balance_parses_through_sdk(monkeypatch):
    def handler(request):
        assert request.url.path.endswith("/api/v2/account/balance")
        return httpx.Response(200, json={"data": {"balance": 12.5}})

    transport = _route(monkeypatch, handler)
    try:
        async with DeapiClient(api_token="tok") as client:
            balance = await client.get_balance()
    finally:
        await transport.aclose()

    assert balance.data.balance == 12.5


async def test_shared_transport_carries_no_auth_state(monkeypatch):
    """Auth lives on each per-user SDK client, never on the shared pool."""
    transport = _route(monkeypatch, lambda r: httpx.Response(200, json={"data": {"balance": 1}}))
    try:
        async with DeapiClient(api_token="tok-A") as a, DeapiClient(api_token="tok-B") as b:
            # the shared httpx client has no Authorization of its own
            assert "authorization" not in {k.lower() for k in transport.headers}
            # each per-user client carries its own token...
            assert a._http._headers["Authorization"] == "Bearer tok-A"  # noqa: SLF001
            assert b._http._headers["Authorization"] == "Bearer tok-B"  # noqa: SLF001
            # ...while sharing one underlying transport (the pool)
            assert a._http._http is transport  # noqa: SLF001
            assert b._http._http is transport  # noqa: SLF001
    finally:
        await transport.aclose()


async def test_concurrent_clients_do_not_leak_auth(monkeypatch):
    """A dozen+ users hitting the shared pool at once each see only their own token."""

    def handler(request):
        # echo back the exact auth the server received for this request
        return httpx.Response(
            200, json={"data": {"request_id": request.headers.get("authorization", "")}}
        )

    transport = _route(monkeypatch, handler)
    tokens = [f"tok-{i}" for i in range(16)]

    async def call(token):
        async with DeapiClient(api_token=token) as client:
            resp = await client.submit_job("images/generations", json_data={"model": "m"})
        return token, resp.data.request_id

    try:
        results = await asyncio.gather(*(call(t) for t in tokens))
    finally:
        await transport.aclose()

    # every request carried its own token — no cross-contamination
    for token, seen in results:
        assert seen == f"Bearer {token}", f"auth leak: {token} saw {seen!r}"
    # and every token was seen exactly once (none dropped or duplicated)
    assert sorted(seen for _, seen in results) == sorted(f"Bearer {t}" for t in tokens)
