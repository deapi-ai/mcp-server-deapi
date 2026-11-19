"""OAuth 2.0 endpoints for Authorization Code flow.

This module provides minimal OAuth 2.0 server functionality to support
MCP clients (like Claude Desktop) that require OAuth authentication.

Flow (Authorization Code):
1. Client discovers OAuth metadata at /.well-known/oauth-authorization-server
2. User clicks "Connect" in Claude Desktop
3. Browser opens /authorize with client_id, redirect_uri, state, code_challenge
4. Server immediately redirects back with authorization code (no UI needed)
5. Client POSTs to /token with authorization code grant
   - code: authorization code from step 4
   - client_id: "deapi-mcp"
   - client_secret: user's Deapi API token
   - code_verifier: for PKCE verification
6. Server validates and returns JWT containing the Deapi token
7. Client uses JWT for MCP requests
8. Auth middleware extracts Deapi token from JWT and forwards to Deapi API
"""

import base64
import binascii
import hashlib
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote, urlencode, urlparse

import jwt
from cryptography.fernet import Fernet
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from . import auth_code_store
from .auth_code_store import AUTHORIZATION_CODE_MAX_ENTRIES, AuthCodeStoreUnavailable
from .config import settings
from .consent import error_page as consent_error_page
from .consent import show_consent, submit_consent
from .key_verifier import is_api_key_rejected, is_key_known_invalid
from .oauth_clients import OAuthClient, looks_like_public_client_id, resolve_client

logger = logging.getLogger(__name__)

# OAuth Configuration
DEFAULT_CLIENT_ID = "deapi-mcp"
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_SECONDS = 3600  # 1 hour
REFRESH_TOKEN_EXPIRATION_SECONDS = 2592000  # 30 days
AUTHORIZATION_CODE_EXPIRATION_SECONDS = 600  # 10 minutes
# Retry-After on a 503 from /token while the authorization code store is unreachable
CODE_STORE_RETRY_AFTER_SECONDS = 5

# Allowed redirect URI schemes (prevent javascript:, data:, etc.)
ALLOWED_REDIRECT_SCHEMES = {"http", "https"}
# Native-app callbacks on a private-use scheme (RFC 8252 §7.1), accepted only as these
# exact strings. Cursor's documented callback for a pre-registered OAuth client.
ALLOWED_NATIVE_REDIRECT_URIS = frozenset({"cursor://anysphere.cursor-mcp/oauth/callback"})
# An S256 code_challenge is the unpadded base64url of a SHA-256 digest: 43 characters.
S256_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
# RFC 6749 §5.1: token responses must not be cached.
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
DOCS_URL = "https://docs.deapi.ai/execution-modes-and-integrations/mcp-server"

# JWT signing key - derive from environment or generate
# In production, set DEAPI_JWT_SECRET_KEY environment variable
_jwt_secret_key: Optional[str] = None

# Fernet encryption key derived from JWT secret (for encrypting tokens in JWT claims)
_fernet: Optional[Fernet] = None


def get_jwt_secret_key() -> str:
    """Get or generate JWT signing key.

    Returns:
        JWT signing key as string
    """
    global _jwt_secret_key

    if _jwt_secret_key is None:
        # Try to get from environment
        _jwt_secret_key = os.getenv("DEAPI_JWT_SECRET_KEY")

        if _jwt_secret_key is None:
            # Generate a random key for this session
            # WARNING: Tokens will be invalid after server restart
            _jwt_secret_key = secrets.token_urlsafe(32)
            logger.warning(
                "Using ephemeral JWT signing key. "
                "Set DEAPI_JWT_SECRET_KEY environment variable for production."
            )

    return _jwt_secret_key


def _get_fernet() -> Fernet:
    """Get Fernet cipher for encrypting/decrypting tokens in JWT claims.

    Derives a Fernet-compatible key from the JWT secret key using SHA-256.

    Returns:
        Fernet cipher instance
    """
    global _fernet

    if _fernet is None:
        secret = get_jwt_secret_key()
        # Derive a 32-byte key from the JWT secret, then base64-encode for Fernet
        derived_key = hashlib.sha256(secret.encode()).digest()
        fernet_key = base64.urlsafe_b64encode(derived_key)
        _fernet = Fernet(fernet_key)

    return _fernet


def encrypt_token(token: str) -> str:
    """Encrypt a token for safe embedding in JWT claims.

    Args:
        token: Plaintext token to encrypt

    Returns:
        Encrypted token as base64 string
    """
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a token from JWT claims.

    Args:
        encrypted_token: Encrypted token string

    Returns:
        Decrypted plaintext token

    Raises:
        Exception: If decryption fails (invalid key or corrupted data)
    """
    return _get_fernet().decrypt(encrypted_token.encode()).decode()


def issuer_url(request: Request) -> str:
    """This server's public base URL: PUBLIC_BASE_URL, else the request's own."""
    return (os.getenv("PUBLIC_BASE_URL") or f"{request.url.scheme}://{request.url.netloc}").rstrip(
        "/"
    )


def resource_is_ours(resource: Optional[str], request: Request) -> bool:
    """RFC 8707: a ``resource`` the client names must be this MCP server.

    Host and path decide; scheme and port do not, so a proxy that hides the original
    scheme (``http`` behind TLS termination) cannot turn a correct request away.
    """
    if not resource:
        return True
    try:
        named = urlparse(resource)
        ours = urlparse(issuer_url(request))
    except ValueError:
        return False
    if (named.hostname or "").lower() != (ours.hostname or "").lower():
        return False
    return named.path.rstrip("/") in ("", "/mcp")


def authorization_redirect(
    request: Request,
    redirect_uri: str,
    params: dict[str, str],
    state: Optional[str],
    status_code: int = 307,
) -> RedirectResponse:
    """Redirect back to the client with ``params``, its ``state`` and our ``iss`` (RFC 9207)."""
    query = dict(params)
    if state:
        query["state"] = state
    query["iss"] = issuer_url(request)
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=status_code)


def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Validate that a redirect URI uses an allowed scheme.

    Prevents open redirect attacks via javascript:, data:, or other dangerous schemes.
    A private-use scheme is accepted only for the exact native-app callbacks listed in
    ALLOWED_NATIVE_REDIRECT_URIS.

    Args:
        redirect_uri: The redirect URI to validate

    Returns:
        True if the URI uses an allowed scheme
    """
    if redirect_uri in ALLOWED_NATIVE_REDIRECT_URIS:
        return True
    try:
        parsed = urlparse(redirect_uri)
        return parsed.scheme.lower() in ALLOWED_REDIRECT_SCHEMES and bool(parsed.netloc)
    except Exception:
        return False


def create_jwt(deapi_token: str, client_id: str = DEFAULT_CLIENT_ID) -> str:
    """Create JWT token containing Deapi API token.

    Args:
        deapi_token: User's Deapi API token (from client_secret)
        client_id: OAuth client ID

    Returns:
        Signed JWT token string
    """
    now = datetime.now(timezone.utc)
    expiration = now + timedelta(seconds=JWT_EXPIRATION_SECONDS)

    payload = {
        "iss": str(settings.deapi_api_base_url),  # Issuer
        "sub": client_id,  # Subject (client ID)
        "aud": "deapi-mcp-api",  # Audience
        "exp": int(expiration.timestamp()),  # Expiration time
        "iat": int(now.timestamp()),  # Issued at
        "deapi_token_enc": encrypt_token(deapi_token),  # Encrypted Deapi token
    }

    token = jwt.encode(
        payload,
        get_jwt_secret_key(),
        algorithm=JWT_ALGORITHM,
    )

    return token


def decode_jwt(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate JWT token.

    Args:
        token: JWT token string

    Returns:
        Decoded payload dict if valid, None otherwise
    """
    try:
        payload = jwt.decode(
            token,
            get_jwt_secret_key(),
            algorithms=[JWT_ALGORITHM],
            audience="deapi-mcp-api",
        )
        return payload
    except jwt.InvalidTokenError:
        return None


def is_jwt(token: str) -> bool:
    """Check if a token is a JWT (vs plain Bearer token).

    Args:
        token: Token string to check

    Returns:
        True if token appears to be JWT format
    """
    # JWT has 3 parts separated by dots: header.payload.signature
    return token.count(".") == 2


def create_refresh_token(deapi_token: str, client_id: str = DEFAULT_CLIENT_ID) -> str:
    """Create a refresh token containing Deapi API token.

    The refresh token is a JWT with a longer expiration time and different audience
    to distinguish it from access tokens.

    Args:
        deapi_token: User's Deapi API token
        client_id: OAuth client ID

    Returns:
        Signed refresh token string
    """
    now = datetime.now(timezone.utc)
    expiration = now + timedelta(seconds=REFRESH_TOKEN_EXPIRATION_SECONDS)

    payload = {
        "iss": str(settings.deapi_api_base_url),  # Issuer
        "sub": client_id,  # Subject (client ID)
        "aud": "deapi-mcp-refresh",  # Different audience for refresh tokens
        "exp": int(expiration.timestamp()),  # Expiration time
        "iat": int(now.timestamp()),  # Issued at
        "deapi_token_enc": encrypt_token(deapi_token),  # Encrypted Deapi token
        "token_type": "refresh",  # Explicit token type marker
    }

    token = jwt.encode(
        payload,
        get_jwt_secret_key(),
        algorithm=JWT_ALGORITHM,
    )

    return token


def decode_refresh_token(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate a refresh token.

    Args:
        token: Refresh token string

    Returns:
        Decoded payload dict if valid, None otherwise
    """
    try:
        payload = jwt.decode(
            token,
            get_jwt_secret_key(),
            algorithms=[JWT_ALGORITHM],
            audience="deapi-mcp-refresh",
        )
        # Verify it's actually a refresh token
        if payload.get("token_type") != "refresh":
            return None
        return payload
    except jwt.InvalidTokenError:
        return None


async def oauth_authorization_server_metadata(request: Request) -> JSONResponse:
    """OAuth 2.0 Authorization Server Metadata endpoint.

    Returns OAuth server metadata as per RFC 8414.
    Mounted at: /.well-known/oauth-authorization-server

    Args:
        request: Starlette request object

    Returns:
        JSON response with OAuth metadata
    """
    # Get base URL - prefer explicit PUBLIC_BASE_URL env var, fallback to request URL
    # This is important for production deployments behind reverse proxies/ingress
    base_url = os.getenv("PUBLIC_BASE_URL")
    if not base_url:
        # Derive from request (works for direct connections)
        base_url = f"{request.url.scheme}://{request.url.netloc}"

    metadata: dict[str, Any] = {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [],  # No scopes for our use case
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": DOCS_URL,
    }
    if settings.oauth_public_clients:
        # Clients that register themselves (DCR) or are identified by a metadata URL (CIMD)
        # are public: no secret, PKCE, and the user signs in with a key on our screen.
        metadata["registration_endpoint"] = f"{base_url}/register"
        metadata["client_id_metadata_document_supported"] = True
        metadata["token_endpoint_auth_methods_supported"] = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]

    return JSONResponse(metadata)


async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    """OAuth 2.0 Protected Resource Metadata endpoint.

    Returns protected resource metadata as per RFC 9728.
    This tells MCP clients which authorization servers are trusted
    to issue tokens for this resource.

    Mounted at: /.well-known/oauth-protected-resource

    Args:
        request: Starlette request object

    Returns:
        JSON response with protected resource metadata
    """
    # Get base URL - prefer explicit PUBLIC_BASE_URL env var, fallback to request URL
    base_url = os.getenv("PUBLIC_BASE_URL")
    if not base_url:
        # Derive from request (works for direct connections)
        base_url = f"{request.url.scheme}://{request.url.netloc}"

    # The actual MCP resource is at /mcp (HTTP transport)
    resource_url = f"{base_url}/mcp"

    metadata = {
        "resource": resource_url,  # Point to the actual MCP endpoint
        "authorization_servers": [base_url],  # We are our own authorization server
        "scopes_supported": [],  # No scopes for our use case
    }

    return JSONResponse(metadata)


def _form_str(form: FormData, key: str) -> Optional[str]:
    """A text form field, or None when it is missing or was sent as a file part.

    A file part arrives as an UploadFile; treating it as absent turns it into the usual
    400/401 instead of a crash further down.
    """
    value = form.get(key)
    return value if isinstance(value, str) else None


def _basic_credentials(request: Request) -> Optional[tuple[str, str]]:
    """The client_id and client_secret from an ``Authorization: Basic`` header.

    RFC 6749 §2.3.1 (``client_secret_basic``): base64 of ``id:secret``, each part
    form-urlencoded first. Returns None when there is no Basic header; raises
    ValueError when there is one that cannot be decoded.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("malformed Basic credentials") from exc
    client_id, sep, client_secret = decoded.partition(":")
    if not sep:
        raise ValueError("malformed Basic credentials")
    # unquote, not unquote_plus: a client that skips the encoding step must not have a
    # literal '+' in the key turned into a space.
    return unquote(client_id), unquote(client_secret)


async def token_endpoint(request: Request) -> JSONResponse:
    """OAuth 2.0 token endpoint: the grant handling below, plus the headers every answer needs.

    Token responses are never cached (RFC 6749 §5.1), and a client that authenticated with
    HTTP Basic and failed gets the Basic challenge back (RFC 6749 §5.2).
    """
    response = await _token_response(request)
    response.headers.update(NO_STORE_HEADERS)
    if response.status_code == 401 and request.headers.get("authorization", "").lower().startswith(
        "basic "
    ):
        response.headers["WWW-Authenticate"] = f'Basic realm="{DEFAULT_CLIENT_ID}"'
    return response


async def _token_response(request: Request) -> JSONResponse:
    """OAuth 2.0 Token endpoint for Authorization Code, Client Credentials, and Refresh Token grants.

    Mounted at: /token

    Accepts three grant types:
    1. authorization_code:
       - code: authorization code from /authorize
       - client_id: "deapi-mcp"
       - client_secret: user's Deapi API token
       - code_verifier: PKCE verifier
       - redirect_uri: must match authorization request

    2. client_credentials:
       - client_id: "deapi-mcp"
       - client_secret: user's Deapi API token

    3. refresh_token:
       - refresh_token: refresh token from previous token response
       - client_id: "deapi-mcp"

    The client authenticates with client_id and client_secret in the form
    (``client_secret_post``) or in an ``Authorization: Basic`` header
    (``client_secret_basic``); the header wins when both are sent.

    Returns JWT containing the Deapi token, plus a refresh token for non-refresh grants.

    Args:
        request: Starlette request object

    Returns:
        JSON response with access_token, refresh_token, or error
    """
    # Parse form data
    form_data = await request.form()
    grant_type = _form_str(form_data, "grant_type")
    client_id = _form_str(form_data, "client_id")
    client_secret = _form_str(form_data, "client_secret")
    try:
        basic = _basic_credentials(request)
    except ValueError:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "Malformed Basic credentials"},
            status_code=401,
        )
    if basic is not None:
        if client_id and client_id != basic[0]:
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "client_id in the form and in Basic credentials differ",
                },
                status_code=400,
            )
        client_id, client_secret = basic

    # Validate grant_type
    if grant_type not in ["authorization_code", "client_credentials", "refresh_token"]:
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": "Supported grant types: authorization_code, client_credentials, refresh_token",
            },
            status_code=400,
        )

    # Validate client_id: the pre-registered client, or a public one (CIMD / DCR)
    public_client: Optional[OAuthClient] = None
    if client_id and client_id != DEFAULT_CLIENT_ID and settings.oauth_public_clients:
        public_client = await resolve_client(client_id)
    if not client_id or (client_id != DEFAULT_CLIENT_ID and public_client is None):
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "Unknown client_id: register the client, use a client ID "
                f"metadata document, or use '{DEFAULT_CLIENT_ID}' with your API key as secret",
            },
            status_code=401,
        )
    if public_client is not None and grant_type == "client_credentials":
        return JSONResponse(
            {
                "error": "unauthorized_client",
                "error_description": "A public client cannot use client_credentials",
            },
            status_code=400,
        )
    if not resource_is_ours(_form_str(form_data, "resource"), request):
        if public_client is None:
            # 0.6.x ignored resource; keep the pre-registered client working, but say so
            logger.warning("Token request for client deapi-mcp names another resource")
        else:
            return JSONResponse(
                {"error": "invalid_target", "error_description": "resource is not this MCP server"},
                status_code=400,
            )

    # Handle refresh_token grant
    if grant_type == "refresh_token":
        refresh_token = _form_str(form_data, "refresh_token")

        if not refresh_token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "refresh_token is required"},
                status_code=400,
            )

        # Decode and validate refresh token
        payload = decode_refresh_token(refresh_token)
        if not payload:
            logger.error("Invalid or expired refresh token")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired refresh token"},
                status_code=400,
            )

        # A refresh token belongs to the client it was issued to
        if payload.get("sub") != client_id:
            logger.warning("Refresh token presented by another client")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid refresh token"},
                status_code=400,
            )

        # Extract and decrypt the deapi token from the refresh token
        encrypted_token = payload.get("deapi_token_enc")
        if not encrypted_token:
            logger.error("Refresh token missing deapi_token_enc claim")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid refresh token"},
                status_code=400,
            )

        try:
            deapi_token = decrypt_token(encrypted_token)
        except Exception:
            logger.error("Failed to decrypt deapi token from refresh token")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid refresh token"},
                status_code=400,
            )

        # The grant carries the key it was issued for. A key deAPI now refuses (revoked
        # or rotated) ends the grant, so the client signs in again with a working key
        # instead of minting access tokens no tool call can use.
        if await is_api_key_rejected(deapi_token):
            logger.warning("Refresh with an API key deAPI rejects")
            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "The API key of this grant was rejected by deAPI; "
                    "authorize again",
                },
                status_code=400,
            )

        return _token_success(deapi_token, client_id)

    # Handle authorization_code grant
    if grant_type == "authorization_code":
        code = _form_str(form_data, "code")
        code_verifier = _form_str(form_data, "code_verifier")
        redirect_uri = _form_str(form_data, "redirect_uri")

        # Validate required parameters
        if not code:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "code is required"},
                status_code=400,
            )

        # Fetch and burn the code in one step: a code is single-use even if a later check fails
        try:
            store = auth_code_store.get_auth_code_store(
                "sign_in" if public_client is not None else "default"
            )
            auth_data = await store.consume(code)
        except AuthCodeStoreUnavailable as exc:
            cause = exc.__cause__ or exc
            logger.error(
                "Authorization code store unavailable during token exchange: %s: %s",
                type(cause).__name__,
                cause,
            )
            return JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Authorization temporarily unavailable, try again",
                },
                status_code=503,
                headers={"Retry-After": str(CODE_STORE_RETRY_AFTER_SECONDS)},
            )
        if not auth_data:
            logger.error("Invalid authorization code")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                status_code=400,
            )

        # Check expiration
        if time.time() > auth_data["expires_at"]:
            logger.error("Authorization code expired")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Authorization code expired"},
                status_code=400,
            )

        # A code belongs to the client it was issued to
        if auth_data.get("client_id") != client_id:
            logger.warning("Authorization code presented by another client")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                status_code=400,
            )

        # Verify redirect_uri matches. RFC 6749 §4.1.3: required here only when the
        # authorization request carried one; when it was left out, it may be left out again.
        supplied = auth_data.get("redirect_uri_supplied", True)
        if (supplied or redirect_uri is not None) and redirect_uri != auth_data["redirect_uri"]:
            logger.error("Redirect URI mismatch: %r vs %r", redirect_uri, auth_data["redirect_uri"])
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
                status_code=400,
            )

        # Verify PKCE
        if not code_verifier:
            logger.error("Missing code_verifier")
            return JSONResponse(
                {"error": "invalid_request", "error_description": "code_verifier is required"},
                status_code=400,
            )

        if not verify_pkce_challenge(code_verifier, auth_data["code_challenge"]):
            logger.error("PKCE verification failed")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid code_verifier"},
                status_code=400,
            )

        if public_client is not None:
            # The key came from the sign-in screen, where it was already checked
            try:
                deapi_token = decrypt_token(str(auth_data["key_enc"]))
            except Exception:
                logger.error("Authorization code without a usable key")
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                    status_code=400,
                )
            # deAPI may have refused the key since (a tool call got 401 in the meantime)
            if is_key_known_invalid(deapi_token):
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "The API key was refused"},
                    status_code=400,
                )
            return _token_success(deapi_token, client_id)

        # Client secret IS the Deapi token
        if not client_secret or len(client_secret) < 10:
            return JSONResponse(
                {
                    "error": "invalid_client",
                    "error_description": "Invalid client_secret (Deapi token)",
                },
                status_code=401,
            )

        deapi_token = client_secret

    # Handle client_credentials grant
    else:  # client_credentials
        # Validate client_secret (Deapi token)
        if not client_secret:
            return JSONResponse(
                {
                    "error": "invalid_client",
                    "error_description": "client_secret is required (your Deapi API token)",
                },
                status_code=401,
            )

        if len(client_secret) < 10:
            return JSONResponse(
                {
                    "error": "invalid_client",
                    "error_description": "client_secret appears invalid (Deapi API token expected)",
                },
                status_code=401,
            )

        deapi_token = client_secret

    # The secret IS the deAPI key: ask the API once whether it knows it, so a wrong
    # key fails here, where the client can show it, not on every later tool call.
    if await is_api_key_rejected(deapi_token):
        logger.warning("Token request with a client_secret deAPI rejects")
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "client_secret was rejected by deAPI (invalid API key)",
            },
            status_code=401,
        )

    return _token_success(deapi_token, client_id)


def _token_success(deapi_token: str, client_id: str) -> JSONResponse:
    """An access token and a refresh token for ``client_id``, both carrying the key."""
    try:
        access_token = create_jwt(deapi_token, client_id)
        refresh_token = create_refresh_token(deapi_token, client_id)
    except Exception as e:
        # The type only: the message of a failure here is no business of the client's
        logger.error("Failed to create tokens: %s", type(e).__name__)
        return JSONResponse(
            {"error": "server_error", "error_description": "Failed to create tokens"},
            status_code=500,
        )
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": JWT_EXPIRATION_SECONDS,
            "refresh_token": refresh_token,
        }
    )


def verify_pkce_challenge(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE code_challenge matches code_verifier.

    Args:
        code_verifier: The code verifier from token request
        code_challenge: The code challenge from authorization request

    Returns:
        True if challenge matches verifier
    """
    # Compute SHA256 hash of verifier
    verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
    # Base64-URL encode (without padding)
    computed_challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip("=")
    return computed_challenge == code_challenge


async def authorize_endpoint(request: Request) -> Response:
    """OAuth 2.0 authorization endpoint (``GET`` and, for the sign-in form, ``POST``).

    - ``client_id=deapi-mcp`` (pre-registered; the API key is its secret at /token): the
      code is issued at once and the browser is sent straight back, as before.
    - Any other client_id, when public clients are enabled, must be a client ID metadata
      document URL or a dynamically registered client: the user signs in with an API key
      on the screen in ``consent.py``, which posts back here.

    Query parameters: client_id, redirect_uri, state, code_challenge,
    code_challenge_method (S256), response_type (code), resource (optional, RFC 8707).
    """
    if request.method == "POST":
        if not settings.oauth_public_clients:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Unsupported method"},
                status_code=405,
            )
        return await submit_consent(request)

    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    code_challenge = request.query_params.get("code_challenge")
    code_challenge_method = request.query_params.get("code_challenge_method")
    response_type = request.query_params.get("response_type")
    resource = request.query_params.get("resource")

    public_client: Optional[OAuthClient] = None
    redirect_uri_supplied = True
    if settings.oauth_public_clients and looks_like_public_client_id(client_id):
        public_client = await resolve_client(client_id)
        if public_client is None:
            return consent_error_page(
                "This application is not registered with deAPI MCP, or its registration could "
                "not be read."
            )
        if not redirect_uri and len(public_client.redirect_uris) == 1:
            redirect_uri = public_client.redirect_uris[0]
            redirect_uri_supplied = False
        if not redirect_uri or not public_client.allows_redirect(redirect_uri):
            # Never redirect to a URI the client did not register (open redirect)
            return consent_error_page(
                "The application asked to return to an address it has not registered."
            )
    elif not client_id or client_id != DEFAULT_CLIENT_ID:
        logger.error("Invalid client_id: %r", (client_id or "")[:80])
        # RFC 6749 4.1.2.1: never redirect to a redirect_uri that is not tied to a valid
        # client (open redirect), so answer directly.
        return JSONResponse(
            {"error": "unauthorized_client", "error_description": "Invalid client_id"},
            status_code=400,
        )
    else:
        if not redirect_uri:
            logger.error("Missing redirect_uri")
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uri is required"},
                status_code=400,
            )
        if not _validate_redirect_uri(redirect_uri):
            logger.error("Invalid redirect_uri scheme: %r", redirect_uri[:200])
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "redirect_uri must use http or https scheme",
                },
                status_code=400,
            )

    # From here on the redirect URI is trusted for this client, so errors go back to it
    if response_type != "code":
        logger.error("Invalid response_type: %r", (response_type or "")[:100])
        return authorization_redirect(
            request,
            redirect_uri,
            {
                "error": "unsupported_response_type",
                "error_description": "Only 'code' response_type is supported",
            },
            state,
        )

    # PKCE: S256 only (no "plain"), and a well-formed S256 value
    if (
        not code_challenge
        or code_challenge_method != "S256"
        or not S256_CHALLENGE.fullmatch(code_challenge)
    ):
        logger.error("Invalid PKCE: method=%r", (code_challenge_method or "")[:100])
        return authorization_redirect(
            request,
            redirect_uri,
            {"error": "invalid_request", "error_description": "PKCE with S256 is required"},
            state,
        )

    if not resource_is_ours(resource, request) and public_client is None:
        logger.warning("Authorization request for client deapi-mcp names another resource")
    elif not resource_is_ours(resource, request):
        return authorization_redirect(
            request,
            redirect_uri,
            {"error": "invalid_target", "error_description": "resource is not this MCP server"},
            state,
        )

    if public_client is not None:
        return await show_consent(
            request,
            public_client,
            redirect_uri,
            {
                "client_id": public_client.client_id,
                "redirect_uri": redirect_uri,
                "redirect_uri_supplied": redirect_uri_supplied,
                "state": state,
                "code_challenge": code_challenge,
                "resource": resource,
            },
        )

    # Pre-registered client: issue the code at once
    auth_code = secrets.token_urlsafe(32)
    expires_at = time.time() + AUTHORIZATION_CODE_EXPIRATION_SECONDS

    try:
        stored = await auth_code_store.get_auth_code_store().put(
            auth_code,
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "expires_at": expires_at,
                "state": state,
                "resource": resource,
            },
            AUTHORIZATION_CODE_EXPIRATION_SECONDS,
        )
    except AuthCodeStoreUnavailable as exc:
        cause = exc.__cause__ or exc
        logger.error(
            "Authorization code store unavailable, rejecting authorize request: %s: %s",
            type(cause).__name__,
            cause,
        )
        return authorization_redirect(
            request,
            redirect_uri,
            {
                "error": "temporarily_unavailable",
                "error_description": "Authorization temporarily unavailable, try again",
            },
            state,
        )

    if not stored:
        # Capacity protects against DoS via excessive authorize requests
        logger.warning(
            "Authorization code store at capacity (%d), rejecting new request",
            AUTHORIZATION_CODE_MAX_ENTRIES,
        )
        return authorization_redirect(
            request,
            redirect_uri,
            {
                "error": "server_error",
                "error_description": "Server is busy, please try again later",
            },
            state,
        )

    return authorization_redirect(request, redirect_uri, {"code": auth_code}, state)
