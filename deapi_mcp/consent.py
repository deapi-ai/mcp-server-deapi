"""The sign-in screen at /authorize for public OAuth clients (CIMD or DCR).

A client that registered itself has no secret, so the deAPI key has to come from the
user: ``GET /authorize`` shows who is asking (the client's name, the host that vouches
for it, where the browser will return) and asks for the key; ``POST /authorize`` checks
the key with deAPI and, if it works, issues the authorization code. The pre-registered
``deapi-mcp`` client never sees this page.

Defences, in the order a request meets them:

- the client and its redirect URI are checked *before* anything is shown, and a bad one
  gets an error page, never a redirect (no open redirector);
- the authorization request travels from GET to POST inside an HMAC-signed, 10-minute
  token, so the form cannot be re-pointed at another client or redirect URI;
- a double-submit CSRF cookie (``SameSite=Strict``, ``__Host-`` on https);
- failed key attempts are counted per client IP (Redis when configured, shared by every
  replica) and refused after ``MAX_KEY_FAILURES`` in ``KEY_FAILURE_WINDOW``;
- the page loads nothing, runs no script, cannot be framed, and is never cached; its CSP
  allows the form to post only here and to redirect only to the client's own URI.

The key is never logged and never echoed back into the page.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from html import escape
from typing import Any, Optional
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from . import auth_code_store
from .auth_code_store import AuthCodeStoreUnavailable, build_redis_client
from .config import settings
from .key_verifier import KeyStatus, get_key_verifier
from .landing import CSS, HOME_URL, KEYS_URL, LOGO_SVG, MCP_DOCS_URL
from .oauth_clients import OAuthClient, is_loopback_redirect, resolve_client

logger = logging.getLogger(__name__)

REQUEST_TTL_SECONDS = 600
MAX_KEY_FAILURES = 10
KEY_FAILURE_WINDOW = 900
MAX_KEY_LENGTH = 512
CSRF_COOKIE_HTTPS = "__Host-deapi_mcp_csrf"
CSRF_COOKIE_HTTP = "deapi_mcp_csrf"

CONSENT_CSS = """
.auth{max-width:30rem;margin:2.5rem auto 0;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:1.75rem 1.75rem 1.5rem}
.auth h1{font-size:1.7rem;text-align:left;margin:0 0 .4rem}
.who{display:flex;gap:.9rem;align-items:center;margin:1.2rem 0;padding:.9rem 1rem;
background:var(--bg-deep);border:1px solid var(--line-soft);border-radius:8px}
.avatar{width:2.6rem;height:2.6rem;border-radius:8px;background:#2a2416;color:var(--amber);
display:flex;align-items:center;justify-content:center;font:700 1.1rem var(--sans);flex:none}
.who b{display:block;font-family:var(--sans);font-size:1.05rem;overflow-wrap:anywhere}
.who span{font-size:.8rem;color:var(--dim);overflow-wrap:anywhere}
.dest{font-size:.9rem;margin:.2rem 0 1rem}
.dest b{color:var(--amber);overflow-wrap:anywhere}
.warn,.error{border-radius:8px;padding:.75rem 1rem;font-size:.85rem;margin:1rem 0}
.warn{border:1px solid #6b4a12;background:#2a1e08;color:#ffd38a}
.warn b{color:#ffe2ad}
.error{border:1px solid #7a2433;background:#2a0d12;color:#ffb3c0}
label{display:block;color:var(--fg);font:600 .95rem var(--sans);margin:1.2rem 0 .4rem}
input[type=password]{width:100%;font:1rem var(--mono);color:var(--fg);background:var(--bg-deep);
border:1px solid var(--line);border-radius:6px;padding:.75rem .9rem}
input[type=password]:focus{outline:2px solid var(--amber);outline-offset:1px}
.hint{font-size:.8rem;color:var(--dim);margin:.4rem 0 0}
ul.grants{margin:.6rem 0 1.2rem;padding-left:1.2rem;font-size:.88rem}
.actions{display:flex;gap:.7rem;margin-top:1.4rem}
.actions button{flex:1;cursor:pointer;font:700 .85rem/1 var(--mono);padding:.95rem;
border-radius:4px;border:1px solid var(--amber);color:var(--amber);background:none}
.actions button.solid{background:var(--amber);color:#11100d}
.actions button:hover{background:var(--amber-hi);border-color:var(--amber-hi);color:#11100d}
.fine{font-size:.78rem;color:var(--dim);margin-top:1.2rem}
"""

STYLE = CSS + CONSENT_CSS
_STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()


# --- signed authorization request -----------------------------------------------------------


def _request_key() -> bytes:
    from .oauth_endpoints import get_jwt_secret_key

    return hashlib.sha256(b"deapi-mcp/consent-request\x00" + get_jwt_secret_key().encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign_request(fields: dict[str, Any]) -> str:
    body = _b64(json.dumps({**fields, "exp": int(time.time()) + REQUEST_TTL_SECONDS}).encode())
    signature = _b64(hmac.new(_request_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_request(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    body, sep, signature = token.partition(".")
    if not sep:
        return None
    expected = _b64(hmac.new(_request_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < time.time():
        return None
    return data


# --- failed key attempts -----------------------------------------------------------------------


class KeyFailureLimiter:
    """Counts failed key attempts per client IP; Redis when configured, else this process.

    A Redis error falls back to the in-process count, so the sign-in screen keeps
    working (with a per-replica limit) while Redis is away.
    """

    def __init__(self) -> None:
        self._local: dict[str, tuple[int, float]] = {}
        self._redis: Any = None
        if settings.redis_host:
            self._redis = build_redis_client(settings)

    @staticmethod
    def _bucket(ip: str) -> str:
        return hashlib.sha256(ip.encode()).hexdigest()[:32]

    def _redis_key(self, ip: str) -> str:
        return f"{settings.redis_prefix}oauth_key_fail:{self._bucket(ip)}"

    def _local_count(self, ip: str) -> int:
        count, until = self._local.get(ip, (0, 0.0))
        if until <= time.time():
            self._local.pop(ip, None)
            return 0
        return count

    async def blocked(self, ip: str) -> bool:
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._redis_key(ip))
                return int(raw or 0) >= MAX_KEY_FAILURES
            except Exception:
                logger.warning("Key attempt limiter: Redis unavailable, counting in memory")
        return self._local_count(ip) >= MAX_KEY_FAILURES

    async def record_failure(self, ip: str) -> None:
        if self._redis is not None:
            try:
                key = self._redis_key(ip)
                count = await self._redis.incr(key)
                if int(count) == 1:
                    await self._redis.expire(key, KEY_FAILURE_WINDOW)
                return
            except Exception:
                logger.warning("Key attempt limiter: Redis unavailable, counting in memory")
        count = self._local_count(ip)
        until = self._local.get(ip, (0, time.time() + KEY_FAILURE_WINDOW))[1]
        self._local[ip] = (count + 1, until)
        if len(self._local) > 10_000:
            self._local.pop(next(iter(self._local)))


_limiter: Optional[KeyFailureLimiter] = None


def get_limiter() -> KeyFailureLimiter:
    global _limiter
    if _limiter is None:
        _limiter = KeyFailureLimiter()
    return _limiter


def reset_limiter(limiter: Optional[KeyFailureLimiter] = None) -> None:
    global _limiter
    _limiter = limiter


# --- pages ------------------------------------------------------------------------------------


def _form_action_source(redirect_uri: str) -> str:
    """The CSP source that lets the form's POST redirect on to ``redirect_uri``.

    Browsers check form-action against redirects that follow a form submission, so the
    client's own origin (or, for a native app, its scheme) must be listed.
    """
    parts = urlsplit(redirect_uri)
    if parts.scheme in ("http", "https"):
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    return f"{parts.scheme}:"


def _headers(redirect_uri: Optional[str] = None) -> dict[str, str]:
    form_action = "'self'" + (f" {_form_action_source(redirect_uri)}" if redirect_uri else "")
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; base-uri 'none'; "
            f"form-action {form_action}; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=referrer content=no-referrer>
<meta name=robots content=noindex>
<title>{escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class=bar></div>
<header>
<a class=home href="{HOME_URL}">{LOGO_SVG}</a>
<span class=pill>MCP SERVER</span>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def error_page(message: str, status_code: int = 400) -> HTMLResponse:
    body = f"""<div class=auth>
<h1>Cannot connect</h1>
<div class=error>{escape(message)}</div>
<p class=hint>Start the connection again from your application. If this keeps happening,
see the <a href="{MCP_DOCS_URL}">MCP server docs</a>.</p>
</div>"""
    return HTMLResponse(
        _page("Cannot connect to deAPI", body), status_code=status_code, headers=_headers()
    )


def _host_label(redirect_uri: str) -> str:
    parts = urlsplit(redirect_uri)
    if is_loopback_redirect(redirect_uri):
        return "localhost (an app on this computer)"
    if parts.scheme in ("http", "https"):
        return parts.hostname or redirect_uri
    return f"the app that handles {parts.scheme}: links"


def _same_site(host: str, publisher: str) -> bool:
    """``host`` is ``publisher`` or one of its subdomains."""
    return host == publisher or host.endswith("." + publisher)


def _warnings(client: OAuthClient, redirect_uri: str) -> str:
    """Every warning that applies, most important first."""
    host = escape(_host_label(redirect_uri))
    parts = urlsplit(redirect_uri)
    out = []
    if is_loopback_redirect(redirect_uri):
        out.append(
            "<b>Returns to an app on this computer.</b> Continue only if you just started the "
            "connection from a program you run, such as Claude Code or Cursor."
        )
    elif parts.scheme not in ("http", "https"):
        out.append(
            f"<b>Returns to an app on your device</b> ({escape(parts.scheme)}: links). Continue "
            "only if you just started the connection from that app."
        )
    elif client.verified_host and not _same_site(parts.hostname or "", client.verified_host):
        out.append(
            f"<b>Access goes to a different site.</b> This application is published at "
            f"<b>{escape(client.verified_host)}</b>, but your access goes to <b>{host}</b>. Only "
            "continue if you trust both."
        )
    if client.kind != "cimd":
        out.append(
            "<b>Unverified application.</b> deAPI cannot confirm who runs it; its name was "
            "chosen by the application itself. Only continue if you started this connection "
            f"and you trust <b>{host}</b>."
        )
    return "".join(f"<div class=warn>{w}</div>" for w in out)


def consent_page(
    client: OAuthClient,
    redirect_uri: str,
    signed_request: str,
    csrf: str,
    error: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    returns = escape(_host_label(redirect_uri))
    if client.verified_host:
        # The publisher is the host serving the client's metadata. deAPI does not vouch for
        # it: it is shown as a fact, not as a verification.
        name = escape(client.name)
        initial = escape(client.name[:1].upper() or "?")
        badge = f"published at {escape(client.verified_host)}"
    else:
        name = f"&ldquo;{escape(client.name)}&rdquo; (unverified)"
        initial = "?"
        badge = "registered itself, name not verified"
    error_html = f"<div class=error role=alert>{escape(error)}</div>" if error else ""
    body = f"""<div class=auth>
<h1>Connect to deAPI</h1>
<p>An application wants to use deAPI models on your behalf.</p>
<div class=who><div class=avatar>{initial}</div><div><b>{name}</b><span>{badge}</span></div></div>
<p class=dest>Access goes to <b>{returns}</b></p>
{_warnings(client, redirect_uri)}
<p>If you continue, this application will be able to:</p>
<ul class=grants>
<li>run image, video, audio, transcription, OCR and embedding models</li>
<li>check your balance and job status</li>
<li>spend your deAPI balance on the jobs it runs</li>
</ul>
<form method=post action="/authorize">
<input type=hidden name=request value="{escape(signed_request)}">
<input type=hidden name=csrf value="{escape(csrf)}">
{error_html}
<label for=api_key>deAPI API key</label>
<input id=api_key name=api_key type=password autocomplete=off spellcheck=false
required maxlength={MAX_KEY_LENGTH} placeholder="sk-dp-...">
<p class=hint>Create or copy a key at <a href="{KEYS_URL}">app.deapi.ai/settings/api-keys</a>.
Consider a separate key for each app, so you can revoke one without the others.</p>
<div class=actions><button type=submit name=action value=deny formnovalidate>Cancel</button><button
type=submit name=action value=allow class=solid>Allow access</button></div>
</form>
<p class=fine>Your key is checked with deAPI and reaches the application only inside encrypted
tokens it cannot read. Deleting the key in deAPI ends this connection.</p>
</div>"""
    response = HTMLResponse(
        _page("Connect to deAPI", body), status_code=status_code, headers=_headers(redirect_uri)
    )
    return response


# --- request handling ---------------------------------------------------------------------------


def _is_https(request: Request) -> bool:
    """Whether the browser reached us over https: PUBLIC_BASE_URL decides when it is set.

    A proxy that does not forward the scheme would otherwise downgrade the CSRF cookie
    (no ``Secure``, no ``__Host-`` prefix).
    """
    public = os.getenv("PUBLIC_BASE_URL")
    if public:
        return public.lower().startswith("https://")
    return request.url.scheme == "https"


def _csrf_cookie_name(request: Request) -> str:
    return CSRF_COOKIE_HTTPS if _is_https(request) else CSRF_COOKIE_HTTP


def _client_ip(request: Request) -> str:
    """The address the wrong-key limit counts against.

    ``DEAPI_CLIENT_IP_HEADER`` names a header the edge sets and clients cannot (for example
    ``CF-Connecting-IP`` when every request comes through Cloudflare). Without it the
    connection's address is used, which behind a proxy that trusts ``X-Forwarded-For``
    blindly can be chosen by the client.
    """
    header = settings.client_ip_header
    if header:
        value = request.headers.get(header, "").split(",")[0].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def _with_csrf_cookie(response: Response, request: Request, value: str) -> Response:
    response.set_cookie(
        _csrf_cookie_name(request),
        value,
        max_age=REQUEST_TTL_SECONDS,
        path="/",
        secure=_is_https(request),
        httponly=True,
        samesite="strict",
    )
    return response


async def show_consent(
    request: Request, client: OAuthClient, redirect_uri: str, fields: dict[str, Any]
) -> Response:
    """Render the sign-in screen for an already validated authorization request."""
    csrf = request.cookies.get(_csrf_cookie_name(request)) or secrets.token_urlsafe(32)
    page = consent_page(client, redirect_uri, sign_request(fields), csrf)
    return _with_csrf_cookie(page, request, csrf)


async def submit_consent(request: Request) -> Response:
    """Handle the sign-in form: deny, or check the key and issue the authorization code."""
    from .oauth_endpoints import (
        AUTHORIZATION_CODE_EXPIRATION_SECONDS,
        authorization_redirect,
        encrypt_token,
    )

    form = await request.form()
    raw_request = form.get("request")
    fields = verify_request(raw_request if isinstance(raw_request, str) else None)
    if fields is None:
        return error_page("This sign-in page has expired. Start the connection again.")

    csrf_form = form.get("csrf")
    csrf_cookie = request.cookies.get(_csrf_cookie_name(request))
    if (
        not isinstance(csrf_form, str)
        or not csrf_cookie
        or not hmac.compare_digest(csrf_form, csrf_cookie)
    ):
        return error_page(
            "The sign-in form could not be verified. Make sure cookies are enabled and start "
            "the connection again."
        )

    client = await resolve_client(str(fields.get("client_id", "")))
    redirect_uri = str(fields.get("redirect_uri", ""))
    if client is None or not client.allows_redirect(redirect_uri):
        return error_page("This application is no longer registered. Start the connection again.")
    state = fields.get("state")

    if form.get("action") != "allow":
        return authorization_redirect(
            request,
            redirect_uri,
            {"error": "access_denied", "error_description": "The user declined"},
            state,
            status_code=303,
        )

    signed = raw_request if isinstance(raw_request, str) else ""
    ip = _client_ip(request)
    limiter = get_limiter()
    if await limiter.blocked(ip):
        page = consent_page(
            client,
            redirect_uri,
            signed,
            csrf_cookie,
            "Too many attempts with a wrong key. Wait a few minutes and try again.",
            status_code=429,
        )
        return page

    key_value = form.get("api_key")
    api_key = key_value.strip() if isinstance(key_value, str) else ""
    if len(api_key) < 10 or len(api_key) > MAX_KEY_LENGTH:
        return consent_page(
            client, redirect_uri, signed, csrf_cookie, "Enter your deAPI API key.", 400
        )

    status = (
        await get_key_verifier().verify(api_key) if settings.verify_api_keys else KeyStatus.VALID
    )
    if status is KeyStatus.INVALID:
        await limiter.record_failure(ip)
        return consent_page(
            client,
            redirect_uri,
            signed,
            csrf_cookie,
            "deAPI did not accept this key. Check it and try again.",
            400,
        )
    if status is not KeyStatus.VALID:
        return consent_page(
            client,
            redirect_uri,
            signed,
            csrf_cookie,
            "The key could not be checked with deAPI right now. Try again in a moment.",
            503,
        )

    code = secrets.token_urlsafe(32)
    try:
        stored = await auth_code_store.get_auth_code_store().put(
            code,
            {
                "client_id": client.client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": fields.get("code_challenge"),
                "expires_at": time.time() + AUTHORIZATION_CODE_EXPIRATION_SECONDS,
                "state": state,
                "resource": fields.get("resource"),
                "key_enc": encrypt_token(api_key),
            },
            AUTHORIZATION_CODE_EXPIRATION_SECONDS,
        )
    except AuthCodeStoreUnavailable:
        logger.error("Authorization code store unavailable during sign-in")
        stored = None
    if not stored:
        return consent_page(
            client,
            redirect_uri,
            signed,
            csrf_cookie,
            "deAPI MCP is busy right now. Try again in a moment.",
            503,
        )
    logger.info("Public client signed in (%s)", client.kind)
    response = authorization_redirect(request, redirect_uri, {"code": code}, state, 303)
    response.delete_cookie(_csrf_cookie_name(request), path="/")
    return response
