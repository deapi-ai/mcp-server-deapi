"""OAuth clients other than the pre-registered ``deapi-mcp``: who they are, where they return.

Two ways a client introduces itself, neither of which needs a client database:

- **Client ID Metadata Document (CIMD).** The ``client_id`` is an HTTPS URL; the server
  fetches the JSON document there and takes the client's name and redirect URIs from
  it. The document's own ``client_id`` must equal the URL. The fetch is SSRF-safe (public
  addresses only, pinned IP, no redirects, 5 kB cap, uncompressed) and cached for 5
  minutes; a failure is remembered for a minute. Control of the URL's host is what
  identifies the client, so the sign-in screen names that host.
- **Dynamic Client Registration (RFC 7591).** ``POST /register`` validates the metadata
  and returns a ``client_id`` that *is* the registration: the metadata plus an HMAC
  under a key derived from ``DEAPI_JWT_SECRET_KEY``. Any replica can verify it, nothing
  is stored, and a flood of registrations costs nothing. Its name is whatever the
  client said, so the sign-in screen marks it unverified.

Both kinds are public clients: no secret, PKCE required. Redirect URIs must match a
registered one exactly; a loopback ``http`` URI matches on any port (RFC 8252 §7.3).
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from .utils import InputError, safe_fetch

logger = logging.getLogger(__name__)

PREREGISTERED_CLIENT_ID = "deapi-mcp"

MAX_REDIRECT_URIS = 5
MAX_REDIRECT_URI_LENGTH = 512
MAX_CLIENT_NAME_LENGTH = 80
MAX_METADATA_BYTES = 5 * 1024
CIMD_TIMEOUT_SECONDS = 5.0
# A fetched document is reused this long (failures are never cached).
CIMD_CACHE_TTL = 300
CIMD_CACHE_ENTRIES = 1000
DCR_PREFIX = "dcr."

MAX_REGISTRATION_BYTES = 8 * 1024
# Concurrent metadata fetches per process, and how long a failed one is remembered.
CIMD_MAX_CONCURRENT_FETCHES = 16
CIMD_FAILURE_TTL = 60
# Concurrent fetches for one site (the host's last two labels), so one attacker's slow
# hosts cannot take every slot and lock other clients out.
CIMD_MAX_FETCHES_PER_SITE = 4

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Native apps' private-use schemes (RFC 8252 §7.1) take reverse-domain form, so they contain
# a dot. Schemes without one are accepted only from this list of known MCP clients.
KNOWN_NATIVE_SCHEMES = {"cursor"}
# Never a redirect target: browser-internal, OS protocol handlers, network and messaging.
FORBIDDEN_SCHEMES = {
    "javascript",
    "data",
    "vbscript",
    "file",
    "about",
    "blob",
    "filesystem",
    "view-source",
    "jar",
    "smb",
    "ftp",
    "sftp",
    "ws",
    "wss",
    "intent",
    "mailto",
    "tel",
    "sms",
    "search-ms",
    "search",
}
FORBIDDEN_SCHEME_PREFIXES = ("ms-", "chrome", "edge", "firefox", "opera", "safari", "res")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Bidi overrides, zero-width and other invisible characters a name could hide behind.
_INVISIBLE = re.compile(
    "[\u00ad\u061c\u115f\u1160\u180e\u200b-\u200f\u202a-\u202e"
    "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff]"
)
_LETTERS = re.compile(r"[^a-z0-9]")
# A host name as DNS writes it: ASCII letters, digits and hyphens (an IDN in its xn-- form).
# Anything else could look like another host on the sign-in screen, or break the CSP
# header the redirect's origin is written into.
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.?"
)


def _plain_host(host: Optional[str]) -> bool:
    """An ASCII DNS name or an IP address literal (no IPv6 zone: it takes any text)."""
    if not host or "%" in host:
        return False
    if _HOSTNAME.fullmatch(host):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class OAuthClient:
    """A public client, as the sign-in screen and the token endpoint see it."""

    client_id: str
    kind: str  # "cimd" or "dcr"
    name: str
    redirect_uris: tuple[str, ...]

    @property
    def verified_host(self) -> Optional[str]:
        """The host that serves this client's metadata (CIMD only); deAPI does not vouch for it."""
        return urlsplit(self.client_id).hostname if self.kind == "cimd" else None

    def allows_redirect(self, redirect_uri: str) -> bool:
        return any(redirect_matches(registered, redirect_uri) for registered in self.redirect_uris)


# --- redirect URIs ------------------------------------------------------------------------


def is_loopback_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def valid_redirect_uri(uri: Any) -> bool:
    """https anywhere, http only on loopback, or a native app's private-use scheme."""
    if not isinstance(uri, str) or not uri or len(uri) > MAX_REDIRECT_URI_LENGTH:
        return False
    if _CONTROL.search(uri) or " " in uri:
        return False
    try:
        parts = urlsplit(uri)
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if parts.fragment or not _SCHEME.fullmatch(scheme) or scheme in FORBIDDEN_SCHEMES:
        return False
    if scheme in ("http", "https") and (
        parts.username is not None or not _plain_host(parts.hostname)
    ):
        return False
    if scheme == "https":
        return True
    if scheme == "http":
        return (parts.hostname or "") in LOOPBACK_HOSTS
    # Private-use scheme (RFC 8252 §7.1): com.example.app:/cb, or a known client's own
    # (cursor://anysphere.cursor-mcp/oauth/callback). OS protocol handlers stay out.
    if scheme.startswith(FORBIDDEN_SCHEME_PREFIXES):
        return False
    if "." not in scheme and scheme not in KNOWN_NATIVE_SCHEMES:
        return False
    return bool(parts.netloc or parts.path)


def redirect_matches(registered: str, requested: str) -> bool:
    """Exact match; a loopback http redirect may use any port (RFC 8252 §7.3)."""
    if not valid_redirect_uri(requested):
        return False
    if registered == requested:
        return True
    if not (is_loopback_redirect(registered) and is_loopback_redirect(requested)):
        return False
    a, b = urlsplit(registered), urlsplit(requested)
    return (a.hostname, a.path, a.query) == (b.hostname, b.path, b.query)


# Cyrillic and Greek letters that look like the Latin ones in "deapi".
_LOOKALIKES = str.maketrans(
    {
        "а": "a",
        "е": "e",
        "р": "p",
        "і": "i",
        "ԁ": "d",
        "ӏ": "i",
        "α": "a",
        "ε": "e",
        "ρ": "p",
        "ι": "i",
        "ı": "i",
        "ɑ": "a",
        "ⅾ": "d",
        "ⅰ": "i",
    }
)


def _clean_name(value: Any, fallback: str) -> str:
    """A name safe to show: no control or invisible characters, short, not posing as deAPI."""
    if not isinstance(value, str):
        return fallback
    name = unicodedata.normalize("NFKC", value)
    name = " ".join(_INVISIBLE.sub("", _CONTROL.sub("", name)).split())
    skeleton = _LETTERS.sub("", name.lower().translate(_LOOKALIKES))
    if "deapi" in skeleton:
        return fallback  # only deAPI may call itself deAPI on its own sign-in screen
    return name[:MAX_CLIENT_NAME_LENGTH] or fallback


def _validated_metadata(data: Any) -> tuple[str, tuple[str, ...]]:
    """(client name, redirect URIs) from registration or CIMD metadata, or ValueError."""
    if not isinstance(data, dict):
        raise ValueError("client metadata must be a JSON object")
    uris = data.get("redirect_uris")
    if (
        not isinstance(uris, list)
        or not uris
        or len(uris) > MAX_REDIRECT_URIS
        or not all(valid_redirect_uri(u) for u in uris)
    ):
        raise ValueError(
            f"redirect_uris must list 1-{MAX_REDIRECT_URIS} https, loopback http or "
            "native-app URIs without fragments"
        )
    # Every client here is public (PKCE, no secret). One that prefers another method but
    # lists "none" among those it supports (ChatGPT: private_key_jwt, or none) uses "none"
    # with this server, which offers no other method to such clients.
    method = data.get("token_endpoint_auth_method", "none")
    supported = data.get("token_endpoint_auth_methods_supported")
    if method != "none" and not (isinstance(supported, list) and "none" in supported):
        raise ValueError("token_endpoint_auth_method must be none (public client with PKCE)")
    # Only authorization_code (and refresh_token) are ever honoured; any other grant a client
    # lists is ignored, not refused. Claude's own metadata document lists a JWT-bearer grant
    # too, and refusing it would refuse Claude.
    grants = data.get("grant_types", ["authorization_code"])
    if (
        not isinstance(grants, list)
        or not all(isinstance(g, str) for g in grants)
        or "authorization_code" not in grants
    ):
        raise ValueError("grant_types must include authorization_code")
    responses = data.get("response_types", ["code"])
    if (
        not isinstance(responses, list)
        or not all(isinstance(r, str) for r in responses)
        or "code" not in responses
    ):
        raise ValueError("response_types must include code")
    return _clean_name(data.get("client_name"), "Unnamed application"), tuple(uris)


# --- Dynamic Client Registration (stateless) ----------------------------------------------


def _dcr_key() -> bytes:
    from .oauth_endpoints import get_jwt_secret_key

    return hashlib.sha256(b"deapi-mcp/dcr-client-id\x00" + get_jwt_secret_key().encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_dcr_client_id(name: str, redirect_uris: tuple[str, ...], issued_at: int) -> str:
    payload = _b64(
        json.dumps(
            {"n": name, "r": list(redirect_uris), "t": issued_at}, separators=(",", ":")
        ).encode()
    )
    signature = _b64(hmac.new(_dcr_key(), payload.encode(), hashlib.sha256).digest())
    return f"{DCR_PREFIX}{payload}.{signature}"


def decode_dcr_client_id(client_id: str) -> Optional[OAuthClient]:
    if not client_id.startswith(DCR_PREFIX):
        return None
    payload, sep, signature = client_id[len(DCR_PREFIX) :].partition(".")
    if not sep:
        return None
    expected = _b64(hmac.new(_dcr_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return None
    try:
        data = json.loads(_unb64(payload))
        name = _clean_name(data["n"], "Unnamed application")
        uris = tuple(str(u) for u in data["r"])
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    # Issued by an older version under looser rules: such a client registers again
    if not uris or not all(valid_redirect_uri(u) for u in uris):
        return None
    return OAuthClient(client_id, "dcr", name, uris)


async def register_endpoint(request: Request) -> JSONResponse:
    """RFC 7591 dynamic client registration for public clients."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REGISTRATION_BYTES:
            return _registration_error(
                "invalid_client_metadata", f"The body is limited to {MAX_REGISTRATION_BYTES} bytes"
            )
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return _registration_error("invalid_client_metadata", "The body must be a JSON object")
    try:
        name, uris = _validated_metadata(data)
    except ValueError as exc:
        error = "invalid_redirect_uri" if "redirect_uris" in str(exc) else "invalid_client_metadata"
        return _registration_error(error, str(exc))
    issued_at = int(time.time())
    client_id = encode_dcr_client_id(name, uris, issued_at)
    logger.info("Registered a public OAuth client (%d redirect URIs)", len(uris))
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": issued_at,
            "client_name": name,
            "redirect_uris": list(uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def _registration_error(error: str, description: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


# --- Client ID Metadata Documents ---------------------------------------------------------


def is_cimd_client_id(client_id: str) -> bool:
    """An https URL with a path, no fragment and no credentials (CIMD client_id rules)."""
    if len(client_id) > MAX_REDIRECT_URI_LENGTH:
        return False
    try:
        parts = urlsplit(client_id)
    except ValueError:
        return False
    host = parts.hostname
    if parts.scheme != "https" or host is None or not _plain_host(host) or parts.fragment:
        return False
    if parts.username or parts.password or parts.path in ("", "/"):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # a client is identified by a domain, not an address
    except ValueError:
        return True


_cimd_cache: "OrderedDict[str, tuple[OAuthClient, float]]" = OrderedDict()


_cimd_failures: "OrderedDict[str, float]" = OrderedDict()
# How long a fetch may wait for a free slot before the client is refused for now.
CIMD_SLOT_WAIT_SECONDS = 2.0
_cimd_fetches: Optional[tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = None
# Per site: its semaphore and how many fetches hold or wait for it (dropped at zero).
_site_fetches: dict[str, tuple[asyncio.Semaphore, int]] = {}
# One fetch per client_id at a time; everyone asking meanwhile shares its answer.
_cimd_inflight: "dict[str, asyncio.Future[Optional[OAuthClient]]]" = {}


def _site(client_id: str) -> str:
    host = (urlsplit(client_id).hostname or "").rstrip(".")
    return ".".join(host.split(".")[-2:])


def _fetch_slots() -> asyncio.Semaphore:
    """The fetch semaphore of the running event loop (created on first use there)."""
    global _cimd_fetches
    loop = asyncio.get_running_loop()
    if _cimd_fetches is None or _cimd_fetches[0] is not loop:
        _cimd_fetches = (loop, asyncio.Semaphore(CIMD_MAX_CONCURRENT_FETCHES))
    return _cimd_fetches[1]


def _note_failure(client_id: str) -> None:
    _cimd_failures[client_id] = time.monotonic() + CIMD_FAILURE_TTL
    _cimd_failures.move_to_end(client_id)
    while len(_cimd_failures) > CIMD_CACHE_ENTRIES:
        _cimd_failures.popitem(last=False)


async def fetch_cimd_client(client_id: str) -> Optional[OAuthClient]:
    """The client described at ``client_id``, or None when it cannot be used.

    Bounded so it cannot be turned into a request reflector: one fetch per URL per
    ``CIMD_FAILURE_TTL`` after a failure, one fetch per URL at a time (concurrent callers
    share it), ``CIMD_MAX_CONCURRENT_FETCHES`` at once (at most
    ``CIMD_MAX_FETCHES_PER_SITE`` of them for one site, each waiting up to
    ``CIMD_SLOT_WAIT_SECONDS`` for a slot), and the whole fetch (not each read) within
    ``CIMD_TIMEOUT_SECONDS``.
    """
    cached = _cimd_cache.get(client_id)
    if cached is not None and cached[1] > time.monotonic():
        _cimd_cache.move_to_end(client_id)
        return cached[0]
    failed_until = _cimd_failures.get(client_id)
    if failed_until is not None and failed_until > time.monotonic():
        return None
    fetch = _cimd_inflight.get(client_id)
    if fetch is None:
        fetch = asyncio.ensure_future(_load_cimd_client(client_id))
        _cimd_inflight[client_id] = fetch

        def _done(future: "asyncio.Future[Optional[OAuthClient]]") -> None:
            if _cimd_inflight.get(client_id) is future:
                del _cimd_inflight[client_id]

        fetch.add_done_callback(_done)
    # One caller going away must not cancel the fetch the others wait on
    return await asyncio.shield(fetch)


async def _acquire_site(site: str) -> Optional[asyncio.Semaphore]:
    """A fetch slot for ``site``, waiting up to ``CIMD_SLOT_WAIT_SECONDS``; None if none."""
    semaphore, users = _site_fetches.get(site, (None, 0))
    if semaphore is None:
        semaphore = asyncio.Semaphore(CIMD_MAX_FETCHES_PER_SITE)
    _site_fetches[site] = (semaphore, users + 1)
    try:
        await asyncio.wait_for(semaphore.acquire(), CIMD_SLOT_WAIT_SECONDS)
        return semaphore
    except asyncio.TimeoutError:
        _release_site(site, None)
        return None


def _release_site(site: str, semaphore: Optional[asyncio.Semaphore]) -> None:
    if semaphore is not None:
        semaphore.release()
    entry = _site_fetches.get(site)
    if entry is None:
        return
    if entry[1] <= 1:
        del _site_fetches[site]
    else:
        _site_fetches[site] = (entry[0], entry[1] - 1)


async def _load_cimd_client(client_id: str) -> Optional[OAuthClient]:
    site = _site(client_id)
    site_slot = await _acquire_site(site)
    if site_slot is None:
        logger.warning(
            "Client metadata fetches for one site at capacity; refusing %r for now",
            client_id[:80],
        )
        return None
    try:
        slots = _fetch_slots()
        try:
            await asyncio.wait_for(slots.acquire(), CIMD_SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "Client metadata fetches at capacity; refusing %r for now", client_id[:80]
            )
            return None
        try:
            body, _ = await asyncio.wait_for(_fetch_metadata(client_id), CIMD_TIMEOUT_SECONDS)
        finally:
            slots.release()
    except (InputError, asyncio.TimeoutError) as exc:
        logger.info("Client metadata document rejected (%s)", type(exc).__name__)
        _note_failure(client_id)
        return None
    finally:
        _release_site(site, site_slot)
    try:
        data = json.loads(body)
        if not isinstance(data, dict) or data.get("client_id") != client_id:
            raise ValueError("client_id in the document does not match its URL")
        name, uris = _validated_metadata(data)
    except (ValueError, TypeError, RecursionError) as exc:
        logger.info("Client metadata document rejected (%s)", type(exc).__name__)
        _note_failure(client_id)
        return None
    client = OAuthClient(client_id, "cimd", name, uris)
    _cimd_cache[client_id] = (client, time.monotonic() + CIMD_CACHE_TTL)
    _cimd_cache.move_to_end(client_id)
    while len(_cimd_cache) > CIMD_CACHE_ENTRIES:
        _cimd_cache.popitem(last=False)
    return client


async def _fetch_metadata(url: str) -> tuple[bytes, str]:
    body, content_type = await safe_fetch(
        url,
        max_bytes=MAX_METADATA_BYTES,
        timeout=CIMD_TIMEOUT_SECONDS,
        max_redirects=0,
        schemes=("https",),
        refused_content_types=(),
        accept="application/json",
    )
    return body, content_type


def looks_like_public_client_id(client_id: Optional[str]) -> bool:
    """A client ID in one of the two public forms (it may still fail to resolve)."""
    return bool(client_id) and (
        str(client_id).startswith(DCR_PREFIX) or is_cimd_client_id(str(client_id))
    )


async def resolve_client(client_id: Optional[str]) -> Optional[OAuthClient]:
    """The public client behind ``client_id`` (CIMD or DCR), or None."""
    if not client_id or client_id == PREREGISTERED_CLIENT_ID:
        return None
    if client_id.startswith(DCR_PREFIX):
        return decode_dcr_client_id(client_id)
    if is_cimd_client_id(client_id):
        return await fetch_cimd_client(client_id)
    return None


def clear_cimd_cache() -> None:
    _cimd_cache.clear()
    _cimd_failures.clear()
    _site_fetches.clear()
    _cimd_inflight.clear()
