"""SSRF protections for the server-side URL fetchers (deapi_mcp/utils.safe_fetch)."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import deapi_mcp.utils as u
from deapi_mcp.utils import InputError, _ip_is_public, fetch_image_from_url, safe_fetch

PORT = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body=b""):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._send(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 32)
        elif self.path == "/big":
            self._send(200, "image/png", b"x" * 500)
        elif self.path == "/html":
            self._send(200, "text/html; charset=utf-8", b"<html>internal</html>")
        elif self.path == "/404":
            self._send(404, "text/plain", b"nope")
        elif self.path == "/redir-private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/secret")
            self.end_headers()
        elif self.path == "/redir-ok":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{PORT}/ok")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"?")


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
    """Treat loopback as public so a local test server stands in for a public host."""
    real = u._ip_is_public
    monkeypatch.setattr(u, "_ip_is_public", lambda ip: ip in ("127.0.0.1", "::1") or real(ip))


# --- IP classification ------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "100.64.0.1",
        "::ffff:10.0.0.1",
        "224.0.0.1",
    ],
)
def test_non_public_ips_blocked(ip):
    assert _ip_is_public(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_public_ips_allowed(ip):
    assert _ip_is_public(ip) is True


# --- safe_fetch rejections (no network) -------------------------------------


async def test_non_http_scheme_rejected():
    for url in ["file:///etc/passwd", "gopher://x/1", "ftp://h/f", "dict://h:1/"]:
        with pytest.raises(InputError):
            await safe_fetch(url, max_bytes=1000, timeout=5)


async def test_literal_private_ip_rejected():
    with pytest.raises(InputError):
        await safe_fetch("http://10.0.0.1/x", max_bytes=1000, timeout=5)
    with pytest.raises(InputError):
        await safe_fetch("http://169.254.169.254/latest/meta-data/", max_bytes=1000, timeout=5)


async def test_mixed_resolution_rejected(monkeypatch):
    async def fake_gai(host, port, **kw):
        return [
            (2, 1, 6, "", ("1.2.3.4", port)),
            (2, 1, 6, "", ("10.0.0.1", port)),  # one private -> whole thing refused
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_gai)
    with pytest.raises(InputError):
        await safe_fetch("http://mixed.test/x", max_bytes=1000, timeout=5)


# --- safe_fetch against a local server --------------------------------------


async def test_happy_path(server, allow_loopback):
    body, ctype = await safe_fetch(f"{server}/ok", max_bytes=1_000_000, timeout=5)
    assert body.startswith(b"\x89PNG") and "image/png" in ctype


async def test_size_cap(server, allow_loopback):
    with pytest.raises(InputError):
        await safe_fetch(f"{server}/big", max_bytes=10, timeout=5)  # 500-byte body


async def test_non_media_content_type_refused(server, allow_loopback):
    with pytest.raises(InputError):
        await safe_fetch(f"{server}/html", max_bytes=1_000_000, timeout=5)


async def test_http_error_is_generic(server, allow_loopback):
    with pytest.raises(InputError) as e:
        await safe_fetch(f"{server}/404", max_bytes=1_000_000, timeout=5)
    assert "404" not in str(e.value)  # no status oracle


async def test_redirect_onto_private_blocked(server, allow_loopback):
    with pytest.raises(InputError):
        await safe_fetch(f"{server}/redir-private", max_bytes=1_000_000, timeout=5)


async def test_benign_redirect_followed(server, allow_loopback):
    body, ctype = await safe_fetch(f"{server}/redir-ok", max_bytes=1_000_000, timeout=5)
    assert body.startswith(b"\x89PNG")


async def test_all_failures_share_one_generic_message(server, allow_loopback):
    msgs = set()
    for url in ["file:///x", "http://10.0.0.1/x", f"{server}/404", f"{server}/html"]:
        try:
            await safe_fetch(url, max_bytes=1_000_000, timeout=5)
        except InputError as e:
            msgs.add(str(e))
    assert msgs == {u._FETCH_GENERIC_ERROR}


# --- the image fetcher still returns a sensible filename --------------------


async def test_fetch_image_from_url_filename(server, allow_loopback):
    content, filename = await fetch_image_from_url(f"{server}/ok")
    assert content.startswith(b"\x89PNG") and filename == "image.png"
