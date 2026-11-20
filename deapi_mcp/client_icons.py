"""The picture of the application on the sign-in screen, for well-known clients only.

A picture is a strong cue, and whoever runs a host decides what its favicon shows: a
metadata document on ``claude-connect.example`` could serve Claude's logo. So only clients
whose metadata document lives on a site in ``PICTURE_SITES`` (Claude, ChatGPT, VS Code...)
get their picture; every other client, and every self-registered (DCR) one, gets a letter.

Where the picture comes from, first that works:

1. the document's ``logo_uri``, if it is on the client's own site, or on a host listed in
   ``KNOWN_LOGO_HOSTS`` for that site (ChatGPT's logo lives on oaistatic.com);
2. ``https://<client host>/favicon.ico``.

The server fetches it with ``safe_fetch`` (public addresses only, no redirects, 160 kB,
1.5 seconds), keeps it only if its bytes are PNG, ICO, GIF, JPEG or WebP (never SVG, which
can carry script), and puts it into the page as a ``data:`` URI, so the browser loads
nothing from elsewhere. Results are cached per host and logo: a picture for a day, a miss
for an hour.
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
ICON_TIMEOUT_SECONDS = 1.5
ICON_TTL = 24 * 3600
MISS_TTL = 3600
CACHE_ENTRIES = 64
MAX_CONCURRENT_FETCHES = 4

# Sites (the host's last two labels) whose clients get their picture: well-known MCP
# clients, none of them on a shared public suffix. Anyone else gets a letter.
PICTURE_SITES = {
    "claude.ai",
    "chatgpt.com",
    "vscode.dev",
    "github.com",
    "zed.dev",
    "raycast.com",
    "goose-docs.ai",
    "cursor.com",
}

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


def gets_picture(client: OAuthClient) -> bool:
    host = client.verified_host
    return client.kind == "cimd" and host is not None and _site(f"https://{host}/") in PICTURE_SITES


def candidate_urls(client: OAuthClient) -> list[str]:
    host = client.verified_host
    if not host or not gets_picture(client):
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
        max_redirects=0,
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
            except (InputError, ValueError, asyncio.TimeoutError):
                icon = None
            if icon:
                return icon
    return None


async def client_icon(client: OAuthClient) -> Optional[str]:
    """A ``data:`` URI with the client's picture, or None (the page shows a letter)."""
    if not gets_picture(client):
        return None
    # Keyed by what is fetched, not by client_id: many client IDs on one site share a picture
    key = "|".join(candidate_urls(client))
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
