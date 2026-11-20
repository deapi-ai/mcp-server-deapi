"""The setup page at GET /: content from the registry, and a page a strict CSP can hold."""

import base64
import hashlib
import re
from types import SimpleNamespace

from deapi_mcp.landing import CSS, SCRIPT, render_landing


def test_root_serves_the_setup_page(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "https://mcp.test/mcp" in r.text


def test_root_answers_head(app_client):
    assert app_client.head("/").status_code == 200


def test_every_tool_is_on_the_page(app_client):
    import asyncio

    from deapi_mcp.server_remote import mcp

    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    text = app_client.get("/").text
    for tool in tools:
        if not tool.name.endswith("_price"):
            assert f"<code>{tool.name}</code>" in text
    price_tools = sum(t.name.endswith("_price") for t in tools)
    assert f"Plus {price_tools} <code>*_price</code> tools" in text
    assert f"{len(tools)} tools over one" in text


def test_csp_allows_exactly_the_inline_stylesheet_and_script(app_client):
    r = app_client.get("/")
    csp = r.headers["content-security-policy"]
    style_digest = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
    script_digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    assert "default-src 'none'" in csp
    assert f"style-src 'sha256-{style_digest}'" in csp
    assert f"script-src 'sha256-{script_digest}';" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    # The hashes cover the <style> and <script> elements' exact text.
    (style,) = re.findall(r"<style>(.*?)</style>", r.text, re.S)
    assert style == CSS
    (script,) = re.findall(r"<script>(.*?)</script>", r.text, re.S)
    assert script == SCRIPT


def test_page_loads_nothing_and_runs_only_its_own_script(app_client):
    text = app_client.get("/").text
    assert text.count("<script") == 1
    assert "<script src" not in text
    assert not re.search(r"<(img|iframe|object|embed)\b", text)
    # One link element: the favicon, served by this server
    assert re.findall(r"<link\b[^>]*>", text) == [
        '<link rel=icon type=image/png sizes=64x64 href="/icon-64.png">'
    ]
    assert " style=" not in text  # inline style attributes would break the CSP


def test_security_headers(app_client):
    headers = app_client.get("/").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-cache"


def test_page_never_shows_sse_or_a_key_in_the_url(app_client):
    text = app_client.get("/").text
    assert "/sse" not in text
    assert not re.search(r"[?&](api_?key|key|token)=", text)


def test_base_url_from_the_request_is_escaped():
    html = render_landing('https://evil.example/"><script>x</script>', [])
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


def test_tool_descriptions_are_escaped():
    tool = SimpleNamespace(
        name="text_to_image",
        description="<b>bold</b> first line\nsecond line",
        fn=SimpleNamespace(__module__="deapi_mcp.tools.image"),
    )
    html = render_landing("https://mcp.example.com", [tool])
    assert "&lt;b&gt;bold&lt;/b&gt; first line" in html
    assert "second line" not in html


def test_page_carries_the_inline_logo(app_client):
    text = app_client.get("/").text
    assert text.count("aria-label=deAPI") == 2  # header and footer
    assert 'href="https://deapi.ai/"' in text


def test_every_url_and_snippet_has_a_copy_button(app_client):
    text = app_client.get("/").text
    url = "https://mcp.test/mcp"
    # Every place the endpoint is shown carries a button that copies exactly it
    shown = text.count(f"<code>{url}</code>")
    assert shown >= 4
    assert text.count(f'data-copy="{url}"') == shown
    assert text.count('data-copy="deapi-mcp"') == text.count("<code>deapi-mcp</code>")
    # Every snippet has its own button with the snippet's full text
    assert text.count("<pre>") == text.count("<div class=snippet>")
    assert 'data-copy="claude mcp add --transport http deapi https://mcp.test/mcp' in text
    # Hidden until the script runs, so a page without JavaScript shows no dead buttons
    assert "class=copy-btn hidden data-copy" in text
    assert "class=copy-btn data-copy" not in text


def test_copy_values_are_escaped():
    html = render_landing('https://evil.example/"><img src=x onerror=alert(1)>', [])
    assert "<img" not in html
    assert 'data-copy="https://evil.example/&quot;&gt;&lt;img' in html


def test_a_host_header_that_is_not_a_plain_host_is_not_used(monkeypatch):
    from starlette.requests import Request

    from deapi_mcp.landing import PLACEHOLDER_BASE_URL, public_base_url

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    def request(host):
        scope = {
            "type": "http",
            "scheme": "https",
            "server": ("10.0.0.1", 443),
            "path": "/",
            "headers": [(b"host", host.encode())],
        }
        return Request(scope)

    assert public_base_url(request("mcp.example.com")) == "https://mcp.example.com"
    assert public_base_url(request("[::1]:8000")) == "https://[::1]:8000"
    for bad in ("a.example;id;x", "a.example$(id)", "a.example&x", "a.example'x"):
        assert public_base_url(request(bad)) == PLACEHOLDER_BASE_URL, bad


def test_key_management_note_follows_the_switch(monkeypatch):
    import asyncio

    from deapi_mcp.config import settings
    from deapi_mcp.server_remote import mcp

    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    page = render_landing("https://mcp.example.com", tools)
    assert "Creating, changing and deleting keys is off on this server" in page
    assert 'href="https://app.deapi.ai/settings/api-keys">API keys page</a>' in page
    monkeypatch.setattr(settings, "key_management_tools", True)
    page = render_landing("https://mcp.example.com", tools)
    assert "Key management is on." in page
    assert "is off on this server" not in page
