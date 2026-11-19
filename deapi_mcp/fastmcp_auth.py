"""FastMCP-compatible authentication provider for deAPI MCP Server.

This module implements FastMCP's AuthProvider interface to properly integrate
JWT/Bearer token authentication into the MCP server.
"""

from typing import Any

from fastmcp.server.auth import AccessToken, AuthProvider
from starlette.routing import Route

from .auth import current_deapi_token
from .key_verifier import is_api_key_rejected
from .oauth_endpoints import (
    decode_jwt,
    decrypt_token,
    is_jwt,
    oauth_protected_resource_metadata,
)


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
            logger.error(f"Token verification failed: {str(e)}")
            record_auth_rejected("error")
            return None

    def get_middleware(self) -> list[Any]:
        """Get HTTP middleware for this auth provider.

        Returns:
            List of Starlette Middleware instances
        """
        # Return FastMCP's default middleware which uses our verify_token method
        return super().get_middleware()
