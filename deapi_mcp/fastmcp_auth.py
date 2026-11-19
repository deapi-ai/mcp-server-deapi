"""FastMCP-compatible authentication provider for deAPI MCP Server.

This module implements FastMCP's AuthProvider interface to properly integrate
JWT/Bearer token authentication into the MCP server.
"""

import json
import re
from contextvars import ContextVar
from typing import Any, Optional

from fastmcp.server.auth import AccessToken, AuthProvider
from starlette.routing import Route

from .auth import current_deapi_token
from .key_verifier import is_api_key_rejected, is_key_known_invalid
from .oauth_endpoints import (
    decode_jwt,
    decrypt_token,
    is_jwt,
    oauth_protected_resource_metadata,
)

API_KEY_HEADER = b"x-api-key"
MCP_PATH_PREFIX = "/mcp"

# Whether the current request is for the MCP endpoint, set by ApiKeyHeaderMiddleware.
# Authentication runs on every route, but only /mcp takes a deAPI key: a bearer token sent
# elsewhere (a metrics scraper's, say) is never checked with deAPI. None: not set (no
# middleware in front), so every token is checked as before.
_mcp_request: ContextVar[Optional[bool]] = ContextVar("deapi_mcp_request", default=None)


class ApiKeyHeaderMiddleware:
    """Accept the deAPI key in ``X-API-Key`` as well as ``Authorization: Bearer``.

    Some clients only let a user set one named header for a key. When a request
    carries ``X-API-Key`` and no ``Authorization``, this rewrites it to
    ``Authorization: Bearer <key>`` before authentication runs, so the key goes
    through exactly the same checks (``DeapiAuthProvider.verify_token``). A request
    that carries both keeps its ``Authorization`` header untouched.

    It also notes whether the request is for /mcp at all, so a token sent to any
    other route is never taken for a deAPI key.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        on_mcp = scope.get("path", "").startswith(MCP_PATH_PREFIX)
        if on_mcp:
            headers = scope.get("headers") or []
            if not any(name == b"authorization" for name, _ in headers):
                key = next((value for name, value in headers if name == API_KEY_HEADER), b"")
                key = key.strip()
                if key:
                    scope = dict(scope)
                    scope["headers"] = [*headers, (b"authorization", b"Bearer " + key)]
        marker = _mcp_request.set(on_mcp)
        try:
            await self.app(scope, receive, send)
        finally:
            _mcp_request.reset(marker)


# Shown to the user by clients that surface error_description. No double quotes: it sits
# inside a quoted WWW-Authenticate parameter.
INVALID_TOKEN_DESCRIPTION = (
    "The deAPI API key or access token was not accepted. Check the key at "
    "https://app.deapi.ai/settings/api-keys, or sign in again if you connected with OAuth."
)
_DESCRIPTION_PARAM = re.compile(rb'error_description="[^"]*"')


class InvalidTokenDescriptionMiddleware:
    """Replace FastMCP's text in a 401 ``invalid_token`` answer on /mcp with ours.

    FastMCP tells the client to "re-register and obtain new tokens", which suits a
    server with dynamic client registration. This one has none: the fix is a working
    API key, or signing in again with the pre-registered client. Status, error code
    and ``resource_metadata`` are kept; only ``error_description`` changes, in the
    ``WWW-Authenticate`` header and in the JSON body.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        replaced = False

        async def rewriting_send(message: Any) -> None:
            nonlocal replaced
            if replaced:
                return  # the original body; ours has been sent already
            if message["type"] == "http.response.start" and message["status"] == 401:
                headers = list(message.get("headers") or [])
                challenge = next((v for k, v in headers if k.lower() == b"www-authenticate"), b"")
                if b'error="invalid_token"' in challenge:
                    body = json.dumps(
                        {"error": "invalid_token", "error_description": INVALID_TOKEN_DESCRIPTION}
                    ).encode()
                    description = f'error_description="{INVALID_TOKEN_DESCRIPTION}"'.encode()
                    rewritten = []
                    for name, value in headers:
                        lower = name.lower()
                        if lower == b"www-authenticate":
                            value = _DESCRIPTION_PARAM.sub(lambda _: description, value)
                        elif lower == b"content-length":
                            value = str(len(body)).encode()
                        rewritten.append((name, value))
                    replaced = True
                    await send({**message, "headers": rewritten})
                    await send({"type": "http.response.body", "body": body})
                    return
            await send(message)

        await self.app(scope, receive, rewriting_send)


class DeapiAuthProvider(AuthProvider):
    """FastMCP AuthProvider for deAPI token passthrough.

    This provider:
    1. Accepts Bearer tokens (JWT or plain deAPI tokens)
    2. Extracts deAPI token from JWT if applicable
    3. Stores it in context for tools to use
    4. Validates a JWT by its signature, and a plain key by asking the API once
       (cached; see key_verifier.py). An API outage lets a plain key through.
    """

    def __init__(self, base_url: str | None = None):
        """Initialize the deAPI auth provider.

        Args:
            base_url: Base URL of the server (optional)
        """
        super().__init__(base_url=base_url)

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        # fastmcp >= 4.1 puts resource_metadata in the 401 challenge only when the
        # provider itself serves the RFC 9728 metadata route.
        return [
            Route(
                "/.well-known/oauth-protected-resource",
                oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource/mcp",
                oauth_protected_resource_metadata,
                methods=["GET"],
            ),
            *super().get_routes(mcp_path),
        ]

    async def verify_token(self, token: str | None) -> AccessToken | None:
        """Verify a bearer token and return access info if valid.

        This method is called by FastMCP for every authenticated request.

        Args:
            token: The token string to validate (without "Bearer " prefix)

        Returns:
            AccessToken object if valid, None if invalid
        """
        import logging

        from .observability import record_auth_rejected

        logger = logging.getLogger(__name__)

        if token is None:
            logger.error("No Authorization token provided")
            return None

        if _mcp_request.get() is False:
            # Not the MCP endpoint: no route here takes a deAPI key, so the token is left
            # alone (not sent to deAPI, not counted as a rejection)
            return None

        try:
            deapi_token = token  # Default: use token directly

            if is_jwt(token):
                # Decode JWT to extract embedded deAPI token
                payload = decode_jwt(token)
                if payload is None:
                    logger.error("Invalid or expired JWT token")
                    record_auth_rejected("invalid_or_expired")
                    return None

                # Extract and decrypt deAPI token from JWT payload
                encrypted_token = payload.get("deapi_token_enc")
                if not encrypted_token:
                    logger.error("JWT missing 'deapi_token_enc' claim")
                    record_auth_rejected("bad_claims")
                    return None

                try:
                    deapi_token = decrypt_token(encrypted_token)
                except Exception:
                    logger.error("Failed to decrypt deapi token from JWT")
                    record_auth_rejected("bad_claims")
                    return None

                # deAPI refused this key on a recent call (revoked or deleted): end the
                # session now, so the client refreshes, is refused, and signs in again.
                # A memory read only: no request to deAPI on this path.
                if is_key_known_invalid(deapi_token):
                    logger.warning("Access token for an API key deAPI refuses")
                    record_auth_rejected("invalid_key")
                    return None
            elif await is_api_key_rejected(token):
                # A plain key carries no signature, so the API is asked once whether
                # it knows the key. A refusal here makes the client prompt for a new
                # key now, instead of every tool call failing later.
                logger.warning("Plain API key rejected by deAPI")
                record_auth_rejected("invalid_key")
                return None

            # Store deAPI token in context for tools to access
            current_deapi_token.set(deapi_token)

            return AccessToken(
                client_id="deapi-mcp",
                token=deapi_token,
                scopes=[],
            )

        except Exception as e:
            # The type only: a message could quote part of the token
            logger.error("Token verification failed: %s", type(e).__name__)
            record_auth_rejected("error")
            return None

    def get_middleware(self) -> list[Any]:
        """Get HTTP middleware for this auth provider.

        Returns:
            List of Starlette Middleware instances
        """
        # Return FastMCP's default middleware which uses our verify_token method
        return super().get_middleware()
