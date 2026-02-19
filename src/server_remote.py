"""Remote FastMCP server for deAPI with streamable-http transport."""

import asyncio
import logging
import os

import uvicorn
from fastmcp import FastMCP

# Import FastMCP-compatible auth provider
from .fastmcp_auth import DeapiAuthProvider

# Import tool functions
from .tools.audio import (
    audio_transcription,
    audio_transcription_price,
    audio_url_transcription,
    audio_url_transcription_price,
    text_to_audio,
    text_to_audio_price,
    video_file_transcription,
    video_file_transcription_price,
    video_url_transcription,
    video_url_transcription_price,
)
from .tools.image import (
    image_to_image,
    image_to_image_price,
    image_to_text,
    image_to_text_price,
    image_remove_background,
    image_remove_background_price,
    image_upscale,
    image_upscale_price,
    text_to_image,
    text_to_image_price,
)
from .tools.embedding import text_to_embedding, text_to_embedding_price
from .tools.utility import check_job_status, get_available_models, get_balance
from .tools.video import (
    image_to_video,
    image_to_video_price,
    text_to_video,
    text_to_video_price,
    video_remove_background,
    video_remove_background_price,
    video_upscale,
    video_upscale_price,
)

# Initialize FastMCP server WITHOUT auth (will be added later)
# Auth requires knowing the base URL which we don't have until runtime
mcp = FastMCP(name="deAPI AI API", auth=None)


# ============================================================================
# AUDIO & VIDEO TRANSCRIPTION TOOLS
# ============================================================================

mcp.tool()(audio_transcription)
mcp.tool()(text_to_audio)
mcp.tool()(audio_transcription_price)
mcp.tool()(text_to_audio_price)

# Audio URL transcription (Twitter Spaces)
mcp.tool()(audio_url_transcription)
mcp.tool()(audio_url_transcription_price)

# Video transcription tools (use Whisper models like audio)
mcp.tool()(video_file_transcription)
mcp.tool()(video_url_transcription)
mcp.tool()(video_file_transcription_price)
mcp.tool()(video_url_transcription_price)


# ============================================================================
# IMAGE TOOLS
# ============================================================================

mcp.tool()(text_to_image)
mcp.tool()(image_to_image)
mcp.tool()(image_to_text)
mcp.tool()(image_remove_background)
mcp.tool()(image_upscale)
mcp.tool()(text_to_image_price)
mcp.tool()(image_to_image_price)
mcp.tool()(image_to_text_price)
mcp.tool()(image_remove_background_price)
mcp.tool()(image_upscale_price)


# ============================================================================
# VIDEO TOOLS
# ============================================================================

mcp.tool()(text_to_video)
mcp.tool()(image_to_video)
mcp.tool()(image_to_video_price)
mcp.tool()(text_to_video_price)
# video_remove_background and video_upscale not yet implemented in API (no models deployed)
# mcp.tool()(video_remove_background)
# mcp.tool()(video_remove_background_price)
# mcp.tool()(video_upscale)
# mcp.tool()(video_upscale_price)


# ============================================================================
# EMBEDDING TOOLS
# ============================================================================

mcp.tool()(text_to_embedding)
mcp.tool()(text_to_embedding_price)


# ============================================================================
# UTILITY TOOLS
# ============================================================================

mcp.tool()(get_balance)
mcp.tool()(get_available_models)
mcp.tool()(check_job_status)


# ============================================================================
# SERVER ENTRY POINT FOR REMOTE HOSTING
# ============================================================================

def main():
    """Entry point for the MCP server (used by console_scripts and __main__)."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    # Get configuration from environment
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))

    logger.info("Starting deAPI MCP Server with HTTP transport")
    logger.info(f"Server will run on {host}:{port}")

    # Create FastMCP auth provider
    base_url = f"http://{host}:{port}"
    auth_provider = DeapiAuthProvider(base_url=base_url)

    # Set auth on mcp instance BEFORE calling http_app
    mcp.auth = auth_provider

    # Conditionally enable model description enrichment middleware
    from .config import settings as deapi_settings
    if deapi_settings.enrich_tool_descriptions:
        from .middleware import ModelEnrichmentMiddleware
        mcp.add_middleware(ModelEnrichmentMiddleware(ttl=deapi_settings.model_cache_ttl))
        logger.info(
            "Model description enrichment enabled (TTL: %ss)",
            deapi_settings.model_cache_ttl,
        )

    # Get the Starlette app from FastMCP with streamable-http transport
    starlette_app = mcp.http_app(transport='streamable-http')

    # Add health check endpoint for Docker/monitoring
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from .oauth_endpoints import (
        authorize_endpoint,
        oauth_authorization_server_metadata,
        oauth_protected_resource_metadata,
        token_endpoint,
    )

    async def health_check(request):
        """Health check endpoint for Docker and monitoring."""
        return JSONResponse({"status": "healthy", "service": "deapi-mcp"})

    # Add the health route to the app
    starlette_app.routes.append(Route("/health", health_check))

    # Add OAuth endpoints for MCP clients (Claude Desktop, etc.)
    # These enable OAuth Authorization Code flow for authentication
    starlette_app.routes.append(
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server_metadata)
    )
    # Claude Desktop also tries path-based discovery on auth server
    starlette_app.routes.append(
        Route("/.well-known/oauth-authorization-server/mcp", oauth_authorization_server_metadata)
    )
    starlette_app.routes.append(
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource_metadata)
    )
    # Support path-based discovery (RFC 9728) - Claude Desktop uses this
    starlette_app.routes.append(
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource_metadata)
    )
    # OAuth Authorization Code flow endpoints
    starlette_app.routes.append(
        Route("/authorize", authorize_endpoint, methods=["GET"])
    )
    starlette_app.routes.append(
        Route("/token", token_endpoint, methods=["POST"])
    )

    logger.info(f"MCP endpoint: http://{host}:{port}/mcp")
    logger.info(f"OAuth server: http://{host}:{port}/.well-known/oauth-authorization-server")
    logger.info("Authentication: OAuth 2.0 (Authorization Code + PKCE) and Bearer tokens")

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
    )
    server = uvicorn.Server(config)

    # Run the server
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
