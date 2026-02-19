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

import hashlib
import base64
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse

import jwt
from cryptography.fernet import Fernet
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .config import settings

logger = logging.getLogger(__name__)

# OAuth Configuration
DEFAULT_CLIENT_ID = "deapi-mcp"
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_SECONDS = 3600  # 1 hour
REFRESH_TOKEN_EXPIRATION_SECONDS = 2592000  # 30 days
AUTHORIZATION_CODE_EXPIRATION_SECONDS = 600  # 10 minutes
AUTHORIZATION_CODE_MAX_ENTRIES = 1000  # Max auth codes before forced cleanup

# Allowed redirect URI schemes (prevent javascript:, data:, etc.)
ALLOWED_REDIRECT_SCHEMES = {"http", "https"}

# JWT signing key - derive from environment or generate
# In production, set DEAPI_JWT_SECRET_KEY environment variable
_jwt_secret_key: Optional[str] = None

# Fernet encryption key derived from JWT secret (for encrypting tokens in JWT claims)
_fernet: Optional[Fernet] = None

# In-memory storage for authorization codes
# Format: {code: {"client_id": str, "redirect_uri": str, "code_challenge": str, "expires_at": float, "state": str}}
_authorization_codes: dict[str, dict] = {}


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


def _prune_expired_authorization_codes() -> None:
    """Remove expired authorization codes to prevent memory leaks."""
    now = time.time()
    expired = [code for code, data in _authorization_codes.items() if now > data["expires_at"]]
    for code in expired:
        del _authorization_codes[code]
    if expired:
        logger.debug("Pruned %d expired authorization codes", len(expired))


def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Validate that a redirect URI uses an allowed scheme.

    Prevents open redirect attacks via javascript:, data:, or other dangerous schemes.

    Args:
        redirect_uri: The redirect URI to validate

    Returns:
        True if the URI uses an allowed scheme
    """
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


def decode_jwt(token: str) -> Optional[dict]:
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


def decode_refresh_token(token: str) -> Optional[dict]:
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

    metadata = {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [],  # No scopes for our use case
        "service_documentation": "https://api.deapi.ai",
    }

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


async def token_endpoint(request: Request) -> JSONResponse:
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

    Returns JWT containing the Deapi token, plus a refresh token for non-refresh grants.

    Args:
        request: Starlette request object

    Returns:
        JSON response with access_token, refresh_token, or error
    """
    # Parse form data
    form_data = await request.form()
    grant_type = form_data.get("grant_type")
    client_id = form_data.get("client_id")
    client_secret = form_data.get("client_secret")

    # Validate grant_type
    if grant_type not in ["authorization_code", "client_credentials", "refresh_token"]:
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": "Supported grant types: authorization_code, client_credentials, refresh_token",
            },
            status_code=400,
        )

    # Validate client_id
    if not client_id or client_id != DEFAULT_CLIENT_ID:
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": f"Invalid client_id. Use '{DEFAULT_CLIENT_ID}'",
            },
            status_code=401,
        )

    # Handle refresh_token grant
    if grant_type == "refresh_token":
        refresh_token = form_data.get("refresh_token")

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

        # Create new access token (and optionally rotate refresh token)
        try:
            access_token = create_jwt(deapi_token, client_id)
            # Issue a new refresh token (token rotation for security)
            new_refresh_token = create_refresh_token(deapi_token, client_id)
        except Exception as e:
            logger.error(f"Failed to create tokens: {str(e)}")
            return JSONResponse(
                {"error": "server_error", "error_description": f"Failed to create tokens: {str(e)}"},
                status_code=500,
            )

        return JSONResponse(
            {
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": JWT_EXPIRATION_SECONDS,
                "refresh_token": new_refresh_token,
            }
        )

    # Handle authorization_code grant
    if grant_type == "authorization_code":
        code = form_data.get("code")
        code_verifier = form_data.get("code_verifier")
        redirect_uri = form_data.get("redirect_uri")

        # Validate required parameters
        if not code:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "code is required"},
                status_code=400,
            )

        # Look up authorization code
        auth_data = _authorization_codes.get(code)
        if not auth_data:
            logger.error(f"Invalid authorization code: {code[:10]}...")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                status_code=400,
            )

        # Check expiration
        if time.time() > auth_data["expires_at"]:
            logger.error("Authorization code expired")
            del _authorization_codes[code]
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Authorization code expired"},
                status_code=400,
            )

        # Verify redirect_uri matches
        if redirect_uri != auth_data["redirect_uri"]:
            logger.error(f"Redirect URI mismatch: {redirect_uri} vs {auth_data['redirect_uri']}")
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

        # Delete authorization code (one-time use)
        del _authorization_codes[code]

        # Client secret IS the Deapi token
        if not client_secret or len(client_secret) < 10:
            return JSONResponse(
                {"error": "invalid_client", "error_description": "Invalid client_secret (Deapi token)"},
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

    # Create JWT containing the Deapi token and refresh token
    try:
        access_token = create_jwt(deapi_token, client_id)
        refresh_token = create_refresh_token(deapi_token, client_id)
    except Exception as e:
        logger.error(f"Failed to create tokens: {str(e)}")
        return JSONResponse(
            {
                "error": "server_error",
                "error_description": f"Failed to create tokens: {str(e)}",
            },
            status_code=500,
        )

    # Return OAuth token response with refresh token
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


async def authorize_endpoint(request: Request) -> RedirectResponse:
    """OAuth 2.0 Authorization endpoint.

    Mounted at: /authorize

    Receives authorization request from Claude Desktop, immediately
    generates an authorization code and redirects back.

    Query parameters:
    - client_id: must be "deapi-mcp"
    - redirect_uri: where to redirect with authorization code
    - state: opaque value from client
    - code_challenge: PKCE challenge
    - code_challenge_method: should be "S256"
    - response_type: should be "code"

    Returns:
        Redirect response to redirect_uri with authorization code
    """
    # Extract query parameters
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")
    code_challenge = request.query_params.get("code_challenge")
    code_challenge_method = request.query_params.get("code_challenge_method")
    response_type = request.query_params.get("response_type")

    # Validate client_id
    if not client_id or client_id != DEFAULT_CLIENT_ID:
        logger.error(f"Invalid client_id: {client_id}")
        # Redirect with error if we have redirect_uri
        if redirect_uri:
            error_params = {"error": "unauthorized_client", "error_description": "Invalid client_id"}
            if state:
                error_params["state"] = state
            return RedirectResponse(f"{redirect_uri}?{urlencode(error_params)}")
        return JSONResponse({"error": "unauthorized_client"}, status_code=401)

    # Validate redirect_uri
    if not redirect_uri:
        logger.error("Missing redirect_uri")
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri is required"}, status_code=400)

    if not _validate_redirect_uri(redirect_uri):
        logger.error("Invalid redirect_uri scheme: %s", redirect_uri)
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri must use http or https scheme"},
            status_code=400,
        )

    # Validate response_type
    if response_type != "code":
        logger.error(f"Invalid response_type: {response_type}")
        error_params = {"error": "unsupported_response_type", "error_description": "Only 'code' response_type is supported"}
        if state:
            error_params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(error_params)}")

    # Validate PKCE challenge
    if not code_challenge or code_challenge_method != "S256":
        logger.error(f"Invalid PKCE: challenge={code_challenge}, method={code_challenge_method}")
        error_params = {"error": "invalid_request", "error_description": "PKCE with S256 is required"}
        if state:
            error_params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(error_params)}")

    # Prune expired authorization codes to prevent memory leaks
    _prune_expired_authorization_codes()

    # Enforce max entries to prevent DoS via excessive authorize requests
    if len(_authorization_codes) >= AUTHORIZATION_CODE_MAX_ENTRIES:
        logger.warning("Authorization code store at capacity (%d), rejecting new request", AUTHORIZATION_CODE_MAX_ENTRIES)
        error_params = {"error": "server_error", "error_description": "Server is busy, please try again later"}
        if state:
            error_params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(error_params)}")

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)
    expires_at = time.time() + AUTHORIZATION_CODE_EXPIRATION_SECONDS

    # Store authorization code
    _authorization_codes[auth_code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "expires_at": expires_at,
        "state": state,
    }

    # Build redirect URL with authorization code
    redirect_params = {"code": auth_code}
    if state:
        redirect_params["state"] = state

    redirect_url = f"{redirect_uri}?{urlencode(redirect_params)}"

    return RedirectResponse(redirect_url)