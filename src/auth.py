"""Authentication context for deAPI MCP Server.

Provides a context variable to store and retrieve the deAPI token
for the current request. The token is set by DeapiAuthProvider during
authentication and retrieved by tools when calling the deAPI.
"""

from contextvars import ContextVar
from typing import Optional


# Context variable to store the current request's deAPI token
current_deapi_token: ContextVar[Optional[str]] = ContextVar('deapi_token', default=None)


def get_current_token() -> str:
    """Get the deAPI token for the current request.

    This function retrieves the token from the request context that was
    set by the authentication middleware.

    Returns:
        The deAPI Bearer token

    Raises:
        ValueError: If no token is available (not authenticated)
    """
    token = current_deapi_token.get()
    if not token:
        raise ValueError(
            "No deAPI token available. "
            "Please authenticate with 'Authorization: Bearer <your-deapi-token>' header."
        )
    return token
