"""Remote FastMCP server for deAPI with streamable-http transport.

This is the server seam of the ``deapi-mcp`` core. No metrics exposition lives
in the core: ``create_app`` takes one only as an optional injected overlay, and
the stock ``build_app`` passes nothing.
"""

import asyncio
import logging
import os
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette

# Import FastMCP-compatible auth provider
from . import auth_code_store
from .fastmcp_auth import DeapiAuthProvider

# Import tool functions
from .tools.audio import (
    audio_transcription,
    audio_transcription_price,
    audio_url_transcription,
    audio_url_transcription_price,
    text_to_audio,
    text_to_audio_price,
    text_to_music,
    text_to_music_price,
    video_file_transcription,
    video_file_transcription_price,
    video_url_transcription,
    video_url_transcription_price,
)
from .tools.embedding import text_to_embedding, text_to_embedding_price
from .tools.image import (
    image_remove_background,
    image_remove_background_price,
    image_to_image,
    image_to_image_price,
    image_to_text,
    image_to_text_price,
    image_upscale,
    image_upscale_price,
    text_to_image,
    text_to_image_price,
)
from .tools.prompt import prompt_booster, prompt_booster_price
from .tools.utility import check_job_status, get_available_models, get_balance
from .tools.video import (
    audio_to_video,
    audio_to_video_price,
    image_to_video,
    image_to_video_price,
    text_to_video,
    text_to_video_price,
    video_description,
    video_description_price,
    video_upscale,
    video_upscale_price,
)

# Initialize FastMCP server WITHOUT auth (will be added later)
# Auth requires knowing the base URL which we don't have until runtime
mcp = FastMCP(name="deAPI AI API", auth=None)


def register_core_tools(server: FastMCP) -> None:
    """Register every deAPI core tool on the given FastMCP instance.

    Exposed so any consumer -- such as a thin deployment wrapper -- can assemble
    a server from the same tool set. The module registers them on its own ``mcp``
    just below, which preserves the current import-time behaviour.
    """
    # Audio & video transcription
    server.tool()(audio_transcription)
    server.tool()(text_to_audio)
    server.tool()(text_to_music)
    server.tool()(audio_transcription_price)
    server.tool()(text_to_audio_price)
    server.tool()(text_to_music_price)
    server.tool()(audio_url_transcription)
    server.tool()(audio_url_transcription_price)
    server.tool()(video_file_transcription)
    server.tool()(video_url_transcription)
    server.tool()(video_file_transcription_price)
    server.tool()(video_url_transcription_price)
    # Image
    server.tool()(text_to_image)
    server.tool()(image_to_image)
    server.tool()(image_to_text)
    server.tool()(image_remove_background)
    server.tool()(image_upscale)
    server.tool()(text_to_image_price)
    server.tool()(image_to_image_price)
    server.tool()(image_to_text_price)
    server.tool()(image_remove_background_price)
    server.tool()(image_upscale_price)
    # Video
    server.tool()(text_to_video)
    server.tool()(image_to_video)
    server.tool()(audio_to_video)
    server.tool()(image_to_video_price)
    server.tool()(text_to_video_price)
    server.tool()(audio_to_video_price)
    server.tool()(video_description)
    server.tool()(video_description_price)
    server.tool()(video_upscale)
    server.tool()(video_upscale_price)
    # Embedding
    server.tool()(text_to_embedding)
    server.tool()(text_to_embedding_price)
    # Prompt booster
    server.tool()(prompt_booster)
    server.tool()(prompt_booster_price)
    # Utility
    server.tool()(get_balance)
    server.tool()(get_available_models)
    server.tool()(check_job_status)


# Register the core tools on this module's server instance (import-time behaviour).
register_core_tools(mcp)


# ============================================================================
# SERVER ENTRY POINT FOR REMOTE HOSTING
# ============================================================================


def env_int(name: str, default: int) -> int:
    """Read an integer environment variable; unset or blank means the default.

    A secrets env may carry `NAME=` with no value, which reaches the process as an empty
    string -- a bare int() on it would crash-loop the rollout.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_jwt_secret_key(host: str) -> None:
    """Exit non-zero when replicas could each sign with a different ephemeral JWT key.

    Without DEAPI_JWT_SECRET_KEY every process generates its own key, so a token issued by
    one pod is rejected by another. That is fatal for a multi-replica deploy (bound beyond
    loopback, or Redis configured); a local dev run keeps the warning and the ephemeral key.
    """
    from .config import settings as deapi_settings

    if os.getenv("DEAPI_JWT_SECRET_KEY"):
        return
    if host in LOOPBACK_HOSTS and not deapi_settings.redis_host:
        return
    logging.getLogger(__name__).error(
        "DEAPI_JWT_SECRET_KEY is not set (host=%s, redis=%s). Each replica would sign tokens "
        "with its own ephemeral key and reject the others'. Set the same key on every replica.",
        host,
        "configured" if deapi_settings.redis_host else "unset",
    )
    raise SystemExit(1)


def create_app(server: "FastMCP | None" = None, *, metrics: Any = None) -> Starlette:
    """Build the Starlette app from the core server, plus an optional overlay.

    ``server`` defaults to this module's ``mcp``. ``metrics`` is an optional overlay
    that instruments tools and OAuth, adds the missing-token counter and mounts
    /metrics; the core carries no metrics dependency of its own, and the stock
    server passes nothing. Mutates ``server`` (auth, middleware), so call it once
    per process per server.
    """
    server = server if server is not None else mcp
    logger = logging.getLogger(__name__)

    # Get configuration from environment
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = env_int("MCP_PORT", 8000)

    require_jwt_secret_key(host)

    logger.info("Starting deAPI MCP Server with HTTP transport")
    logger.info(f"Server will run on {host}:{port}")

    # Log which OAuth code store backs this process at startup (no I/O)
    auth_code_store.get_auth_code_store()

    # Create FastMCP auth provider
    #
    # FastMCP builds `resource_metadata` in every 401 challenge from this URL. The bind
    # address (0.0.0.0:8000) means nothing to a client behind the gateway, so the public
    # URL wins whenever it is set -- the same one oauth_endpoints.py advertises in discovery.
    base_url = os.getenv("PUBLIC_BASE_URL") or f"http://{host}:{port}"
    auth_provider = DeapiAuthProvider(base_url=base_url)

    # Set auth on the server instance BEFORE calling http_app
    server.auth = auth_provider

    # Conditionally enable model description enrichment middleware
    from .config import settings as deapi_settings

    if deapi_settings.enrich_tool_descriptions:
        from .middleware import ModelEnrichmentMiddleware

        server.add_middleware(ModelEnrichmentMiddleware(ttl=deapi_settings.model_cache_ttl))
        logger.info(
            "Model description enrichment enabled (TTL: %ss)",
            deapi_settings.model_cache_ttl,
        )

    if metrics is not None:
        metrics.install(server)

    # Get the Starlette app from FastMCP with streamable-http transport
    # stateless_http: no Mcp-Session-Id, so any replica can serve any request
    starlette_app = server.http_app(transport="streamable-http", stateless_http=True)

    # Every tool call answers as an sse-starlette stream, and sse-starlette ends all of them
    # on SIGTERM by default, so uvicorn's graceful shutdown would find nothing to wait for and
    # a rollout would cut every in-flight call. Stateless mode has no standalone GET stream
    # (405), so each stream ends with its call; timeout_graceful_shutdown caps the wait.
    from sse_starlette.sse import AppStatus

    # sse-starlette ships py.typed but leaves this staticmethod unannotated.
    AppStatus.disable_automatic_graceful_drain()  # type: ignore[no-untyped-call]

    # Add health check endpoint for Docker/monitoring
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from .oauth_endpoints import (
        authorize_endpoint,
        oauth_authorization_server_metadata,
        token_endpoint,
    )
    from .webhooks import WEBHOOK_ROUTE, internal_webhooks_enabled, webhook_receiver

    async def health_check(request: Request) -> JSONResponse:
        """Health check endpoint for Docker and monitoring."""
        return JSONResponse({"status": "healthy", "service": "deapi-mcp"})

    # Add the health route to the app
    starlette_app.routes.append(Route("/health", health_check))

    if metrics is not None:
        metrics.wrap_app(starlette_app, server)

    # Add OAuth endpoints for MCP clients (Claude Desktop, etc.)
    # These enable OAuth Authorization Code flow for authentication
    starlette_app.routes.append(
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server_metadata)
    )
    # Claude Desktop also tries path-based discovery on auth server
    starlette_app.routes.append(
        Route("/.well-known/oauth-authorization-server/mcp", oauth_authorization_server_metadata)
    )
    # OAuth Authorization Code flow endpoints
    authorize_handler = (
        metrics.instrument(authorize_endpoint, "authorize")
        if metrics is not None
        else authorize_endpoint
    )
    token_handler = (
        metrics.instrument(token_endpoint, metrics.token_step)
        if metrics is not None
        else token_endpoint
    )
    starlette_app.routes.append(Route("/authorize", authorize_handler, methods=["GET"]))
    starlette_app.routes.append(Route("/token", token_handler, methods=["POST"]))

    # Completion webhooks: only mounted when configured, so a server
    # that isn't using them exposes no receiver at all.
    if internal_webhooks_enabled():
        starlette_app.routes.append(Route(WEBHOOK_ROUTE, webhook_receiver, methods=["POST"]))
        logger.info(
            f"Completion webhooks: ON -- jobs will call back {deapi_settings.webhook_receiver_url} "
            f"(safety-net poll every {deapi_settings.webhook_fallback_poll_interval:.0f}s)"
        )
    else:
        logger.info("Completion webhooks: off -- jobs are polled to completion")

    logger.info(f"MCP endpoint: http://{host}:{port}/mcp")
    logger.info(f"OAuth server: http://{host}:{port}/.well-known/oauth-authorization-server")
    logger.info("Authentication: OAuth 2.0 (Authorization Code + PKCE) and Bearer tokens")

    return starlette_app


def build_app() -> Starlette:
    """The stock server app: the core with no overlay.

    A deployment with its own metrics builds its app with
    ``create_app(mcp, metrics=<overlay>)`` instead.
    """
    return create_app(mcp)


def main() -> None:
    """Entry point for the MCP server (used by console_scripts and __main__)."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = env_int("MCP_PORT", 8000)
    starlette_app = build_app()

    # Configure and run uvicorn
    # proxy_headers=True: Trust X-Forwarded-Proto, X-Forwarded-Host from ingress/proxy
    # forwarded_allow_ips="*": Accept forwarded headers from any IP.
    #   IMPORTANT: Only safe when the server is behind a reverse proxy (nginx, ingress, etc.)
    #   that is the sole entry point. If the server is directly exposed to the internet,
    #   an attacker can forge X-Forwarded-For headers to spoof their IP.
    #   For direct exposure, set FORWARDED_ALLOW_IPS to your proxy's IP(s) instead.
    forwarded_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=forwarded_ips,
        # Keep uvicorn's graceful drain inside the platform's pod termination grace period
        # so long polling calls can finish before the process is killed. Tune via the env var
        # to match your deployment's grace/preStop settings.
        timeout_graceful_shutdown=env_int("MCP_GRACEFUL_SHUTDOWN_SECONDS", 930),
    )
    server = uvicorn.Server(config)

    # Run the server
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
