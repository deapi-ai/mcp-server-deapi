"""The setup page at GET /: content from the registry, and a page a strict CSP can hold."""

import base64
import hashlib
import re
from types import SimpleNamespace

from deapi_mcp.landing import CSS, render_landing


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


def test_csp_allows_exactly_the_inline_stylesheet(app_client):
    r = app_client.get("/")
    csp = r.headers["content-security-policy"]
    digest = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
    assert "default-src 'none'" in csp
    assert f"style-src 'sha256-{digest}'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src" not in csp
    # The hash covers the <style> element's exact text.
    (style,) = re.findall(r"<style>(.*?)</style>", r.text, re.S)
    assert style == CSS


def test_page_loads_nothing_and_runs_nothing(app_client):
    text = app_client.get("/").text
    assert "<script" not in text
    assert not re.search(r"<(img|link|iframe|object|embed)\b", text)
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
