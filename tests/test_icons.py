"""deAPI's own icon (favicon, serverInfo) and the client's picture on the sign-in screen."""

import base64
import json

import httpx
import pytest

from deapi_mcp import client_icons, consent, key_verifier, oauth_clients
from deapi_mcp.config import settings
from deapi_mcp.icons import ICON_SIZES, icon_bytes, mcp_icons
from deapi_mcp.oauth_clients import OAuthClient
from deapi_mcp.utils import InputError
from tests.test_public_clients import REDIRECT, FakeApi, authorize, dcr_client

# The real fetch, taken before conftest's autouse fixture stubs it out
REAL_FETCH_IMAGE = client_icons._fetch_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
ICO = b"\x00\x00\x01\x00" + b"\x00" * 40


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


# --- deAPI's icon ----------------------------------------------------------------------------


def test_icons_are_real_pngs_of_their_size():
    for size in ICON_SIZES:
        body = icon_bytes(size)
        assert body.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(body[16:20], "big") == size


def test_favicon_and_sized_icons_are_served(app_client):
    r = app_client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == icon_bytes(64)
    assert "max-age" in r.headers["cache-control"]
    assert app_client.get("/icon-128.png").content == icon_bytes(128)
    assert app_client.get("/icon-7.png").status_code == 404


def test_server_info_announces_the_icon_and_website(app_client):
    r = app_client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer sk-dp-any-0123456789",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
    )
    text = r.text
    payload = (
        json.loads(text[text.index("{") :].splitlines()[0])
        if text.startswith("event")
        else r.json()
    )
    info = payload["result"]["serverInfo"]
    assert info["websiteUrl"] == "https://deapi.ai"
    assert any(i["src"].startswith("data:image/png;base64,") for i in info["icons"])


def test_mcp_icons_put_https_urls_first_when_the_public_url_is_known():
    icons = mcp_icons("https://mcp.example.com/")
    assert icons[0].src == "https://mcp.example.com/icon-128.png"
    assert icons[-1].src.startswith("data:image/png;base64,")
    assert base64.b64decode(icons[-1].src.split(",", 1)[1]) == icon_bytes(128)
    assert [i.src for i in mcp_icons(None)] == [icons[-1].src]


# --- the client's picture --------------------------------------------------------------------


def cimd(host="claude.ai", logo=None):
    return OAuthClient(f"https://{host}/oauth/client.json", "cimd", "App", (REDIRECT,), logo)


@pytest.mark.parametrize(
    "host, logo, allowed",
    [
        ("chatgpt.com", "https://persistent.oaistatic.com/sonic/misc/openai-logo.png", True),
        ("vscode.dev", "https://code.visualstudio.com/assets/branding/code-stable.png", True),
        ("zed.dev", "https://cdn.zed.dev/images/logo-dark.png", True),
        ("attacker.example", "https://claude.ai/logo.png", False),
        ("attacker.example", "https://persistent.oaistatic.com/logo.png", False),
        ("zed.dev", "http://cdn.zed.dev/logo.png", False),
        ("zed.dev", "https://user@cdn.zed.dev/logo.png", False),
    ],
)
def test_logo_only_from_the_clients_own_site(host, logo, allowed):
    assert client_icons.logo_allowed(host, logo) is allowed


def test_candidates_are_the_allowed_logo_then_the_favicon():
    client = cimd("zed.dev", "https://cdn.zed.dev/images/logo-dark.png")
    assert client_icons.candidate_urls(client) == [
        "https://cdn.zed.dev/images/logo-dark.png",
        "https://zed.dev/favicon.ico",
    ]
    foreign = cimd("attacker.example", "https://claude.ai/logo.png")
    assert client_icons.candidate_urls(foreign) == ["https://attacker.example/favicon.ico"]


@pytest.mark.parametrize(
    "body, mime",
    [
        (PNG, "image/png"),
        (ICO, "image/x-icon"),
        (b"GIF89a" + b"\x00" * 10, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>", None),
        (b"<!doctype html><html>", None),
    ],
)
def test_only_raster_images_are_kept(body, mime):
    assert client_icons._image_type(body) == mime


async def test_icon_is_fetched_once_and_cached(monkeypatch):
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("favicon.ico"):
            return ICO, "image/x-icon"
        raise InputError("no")

    monkeypatch.setattr(client_icons, "_fetch_image", REAL_FETCH_IMAGE)
    monkeypatch.setattr(client_icons, "safe_fetch", fetch)
    client_icons.clear_icon_cache()
    icon = await client_icons.client_icon(cimd())
    assert icon == "data:image/x-icon;base64," + base64.b64encode(ICO).decode()
    assert await client_icons.client_icon(cimd()) == icon
    assert calls == ["https://claude.ai/favicon.ico"]


async def test_a_self_registered_client_never_gets_a_picture(monkeypatch):
    called = []

    async def fetch(url):
        called.append(url)
        return "data:image/png;base64,AAAA"

    monkeypatch.setattr(client_icons, "_fetch_image", fetch)
    dcr = OAuthClient("dcr.x.y", "dcr", "Claude", (REDIRECT,), "https://claude.ai/logo.png")
    assert await client_icons.client_icon(dcr) is None
    assert called == []


def test_sign_in_screen_shows_the_picture_for_a_metadata_client(app_client, api, monkeypatch):
    client_id = "https://client.example.com/oauth/client.json"

    async def document(url):
        body = {"client_id": client_id, "client_name": "Client", "redirect_uris": [REDIRECT]}
        return json.dumps(body).encode(), "application/json"

    async def picture(url):
        return "data:image/png;base64," + base64.b64encode(PNG).decode()

    monkeypatch.setattr(oauth_clients, "_fetch_metadata", document)
    monkeypatch.setattr(client_icons, "_fetch_image", picture)
    page = authorize(app_client, client_id)
    assert page.status_code == 200
    assert '<img class=avatar src="data:image/png;base64,' in page.text
    csp = page.headers["content-security-policy"]
    assert "img-src 'self' data:" in csp and "default-src 'none'" in csp


def test_sign_in_screen_shows_a_letter_for_a_self_registered_client(app_client, api):
    page = authorize(app_client, dcr_client(app_client, client_name="Tool"))
    assert "<img class=avatar" not in page.text
    assert "<div class=avatar>?</div>" in page.text
