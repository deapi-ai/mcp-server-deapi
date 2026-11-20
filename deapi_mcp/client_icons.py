"""The picture of the application on the sign-in screen, for clients whose host is known.

Only a client identified by a metadata document (CIMD) gets one: its host is the one that
serves the document, so a picture from that host stands for that host. A client that
registered itself (DCR) chose its own name and gets a letter, so nobody can sign in under
someone else's logo.

Where the picture comes from, first that works:

1. the document's ``logo_uri``, if it is on the client's own site, or on a host listed in
   ``KNOWN_LOGO_HOSTS`` for that site (ChatGPT's logo lives on oaistatic.com);
2. ``https://<client host>/favicon.ico``.

The server fetches it with ``safe_fetch`` (public addresses only, 160 kB, 2.5 seconds),
keeps it only if its bytes are PNG, ICO, GIF, JPEG or WebP (never SVG, which can carry
script), and puts it into the page as a ``data:`` URI, so the browser loads nothing from
elsewhere. Results are cached per client: a picture for a day, a miss for an hour.
"""

import asyncio
import base64
import logging
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlsplit

from .oauth_clients import OAuthClient, _plain_host, _site
from .utils import InputError, safe_fetch

logger = logging.getLogger(__name__)

MAX_ICON_BYTES = 160 * 1024
ICON_TIMEOUT_SECONDS = 2.5
ICON_TTL = 24 * 3600
MISS_TTL = 3600
CACHE_ENTRIES = 500
MAX_CONCURRENT_FETCHES = 8

# Logo hosts a client's own site uses, beyond its own domain.
KNOWN_LOGO_HOSTS = {
    "chatgpt.com": {"persistent.oaistatic.com"},
    "vscode.dev": {"code.visualstudio.com"},
}

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
)

_cache: "OrderedDict[str, tuple[Optional[str], float]]" = OrderedDict()
_inflight: "dict[str, asyncio.Future[Optional[str]]]" = {}
_slots: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = None


def _image_type(body: bytes) -> Optional[str]:
    for magic, mime in _MAGIC:
        if body.startswith(magic):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def logo_allowed(client_host: str, logo_uri: str) -> bool:
    """A logo URL on the client's own site, or on a host known to serve that site's logo."""
    parts = urlsplit(logo_uri)
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https" or not _plain_host(host) or parts.username is not None:
        return False
    client_site = _site(f"https://{client_host}/")
    return _site(f"https://{host}/") == client_site or host in KNOWN_LOGO_HOSTS.get(
        client_site, set()
    )


def candidate_urls(client: OAuthClient) -> list[str]:
    host = client.verified_host
    if not host:
        return []
    urls = []
    if client.logo_uri and logo_allowed(host, client.logo_uri):
        urls.append(client.logo_uri)
    urls.append(f"https://{host}/favicon.ico")
    return urls


def _fetch_slots() -> asyncio.Semaphore:
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(MAX_CONCURRENT_FETCHES))
    return _slots[1]


async def _fetch_image(url: str) -> Optional[str]:
    body, _ = await safe_fetch(
        url,
        max_bytes=MAX_ICON_BYTES,
        timeout=ICON_TIMEOUT_SECONDS,
        total_timeout=ICON_TIMEOUT_SECONDS,
        max_redirects=2,
        schemes=("https",),
        refused_content_types=(),
        accept="image/png,image/x-icon,image/*;q=0.8",
    )
    mime = _image_type(body)
    if mime is None:
        return None
    return f"data:{mime};base64,{base64.b64encode(body).decode()}"


class _BusyError(Exception):
    """Every fetch slot is taken; the letter is shown and nothing is cached."""


async def _load(client: OAuthClient) -> Optional[str]:
    slots = _fetch_slots()
    if slots.locked():
        raise _BusyError()
    async with slots:
        for url in candidate_urls(client):
            try:
                icon = await _fetch_image(url)
            except (InputError, asyncio.TimeoutError):
                icon = None
            if icon:
                return icon
    return None


async def client_icon(client: OAuthClient) -> Optional[str]:
    """A ``data:`` URI with the client's picture, or None (the page shows a letter)."""
    if client.kind != "cimd" or not client.verified_host:
        return None
    key = client.client_id
    cached = _cache.get(key)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    fetch = _inflight.get(key)
    if fetch is None:
        fetch = asyncio.ensure_future(_load(client))
        _inflight[key] = fetch

        def _done(future: "asyncio.Future[Optional[str]]") -> None:
            if _inflight.get(key) is future:
                del _inflight[key]
            if not future.cancelled() and future.exception() is None:
                icon = future.result()
                _cache[key] = (icon, time.monotonic() + (ICON_TTL if icon else MISS_TTL))
                _cache.move_to_end(key)
                while len(_cache) > CACHE_ENTRIES:
                    _cache.popitem(last=False)

        fetch.add_done_callback(_done)
    try:
        return await asyncio.shield(fetch)
    except _BusyError:
        return None
    except Exception as exc:  # never let a picture break the sign-in
        logger.info("Client icon unavailable (%s)", type(exc).__name__)
        return None


def clear_icon_cache() -> None:
    _cache.clear()
    _inflight.clear()
