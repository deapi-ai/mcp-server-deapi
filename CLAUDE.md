# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a production-ready MCP (Model Context Protocol) server for the [deAPI](https://deapi.ai) REST API — a DePIN AI inference service. It exposes all deAPI endpoints as MCP tools, enabling LLMs to perform audio transcription, image generation, OCR, video generation, text-to-speech, and more.

The OpenAPI 3.0 specification is available at https://docs.deapi.ai/.

## Development Commands

### Setup
- `uv pip install -e .` - Install dependencies (recommended)
- `uv pip install -e ".[dev]"` - Install with dev dependencies
- `pip install -e .` - Alternative install via pip

### Running
- `python -m src.server_remote` - Start the HTTP MCP server (localhost:8000)
- `MCP_HOST=0.0.0.0 MCP_PORT=8000 python -m src.server_remote` - Start for remote access

### Testing
- `pytest` - Run all tests
- `pytest tests/test_tools.py` - Run tool tests only
- `pytest -k "test_name"` - Run specific test

### Code Quality
- `black src/` - Format code
- `ruff check src/` - Lint code

## Architecture

### Server

The primary server is `src/server_remote.py` — an HTTP/SSE-based MCP server built with FastMCP. This is the only server entry point (stdio transport is not used).

### Authentication Flow

1. `DeapiAuthProvider` in `src/fastmcp_auth.py` handles OAuth 2.0 (Authorization Code + PKCE)
2. Authenticated token is stored in `current_deapi_token` ContextVar (`src/auth.py`)
3. Tools call `get_client()` from `src/deapi_client.py` which reads the token from the ContextVar
4. Token is forwarded as Bearer auth to the deAPI REST API

### Core Modules

- **`src/deapi_client.py`** - Async HTTP client (`DeapiClient`) with retry logic and auth forwarding
- **`src/polling_manager.py`** - Smart adaptive polling for async job completion
- **`src/schemas.py`** - Pydantic models from the OpenAPI spec
- **`src/config.py`** - Configuration via environment variables (prefix: `DEAPI_`)
- **`src/middleware.py`** - Model cache middleware that enriches tool descriptions with available models

### Tool Implementations (`src/tools/`)

Tools are organized by category:
- `audio.py` - Audio transcription and TTS (6 tools)
- `image.py` - Image generation, OCR, background removal, upscaling (10 tools)
- `video.py` - Video generation (4 tools)
- `embedding.py` - Text embeddings (2 tools)
- `utility.py` - Balance, models, job status (3 tools)
- `_price_helpers.py` - Shared helper for price calculation parameter resolution

### Key Patterns

- **Stateless**: Server never stores API tokens; they're passed per-request via HTTP headers
- **Async polling**: `PollingManager` polls job status with type-specific strategies (audio: 1-5s, image: 2-8s, video: 5-30s)
- **Error handling**: `DeapiAPIError` for API errors; tools always return `{"success": bool, "error"?: str, "result_url"?: str, "job_id"?: str}`
- **Model cache**: Middleware fetches available models on first `list_tools` call and enriches tool descriptions
- **Price helpers**: `resolve_generation_params()` in `_price_helpers.py` pulls defaults (seed, guidance, steps, dimensions) from model cache for price calculation tools

## Adding New Tools

1. Check the OpenAPI spec at https://docs.deapi.ai/ for endpoint details
2. Add request/response schemas to `src/schemas.py` if needed
3. Create tool function in the appropriate file under `src/tools/`
4. Register in `src/server_remote.py` using `@mcp.tool()` decorator
5. Tool must:
   - NOT accept `deapi_api_token` parameter (handled by server auth)
   - Use `get_client()` to get the authenticated client
   - Use `PollingManager` for async jobs
   - Return dict with `success`, `error` (if failed), `result_url`/`result`, `job_id`
6. Add tests in `tests/`
7. Update README.md with new tool documentation

## Configuration

Environment variables (prefix `DEAPI_`):

```bash
DEAPI_API_BASE_URL=https://api.deapi.ai
DEAPI_API_VERSION=v1
DEAPI_HTTP_TIMEOUT=30.0
DEAPI_MAX_RETRIES=3
DEAPI_RETRY_BACKOFF_FACTOR=2.0

# Per-job-type polling overrides
DEAPI_POLLING_AUDIO__INITIAL_DELAY=1.0
DEAPI_POLLING_AUDIO__MAX_DELAY=5.0
DEAPI_POLLING_AUDIO__TIMEOUT=300.0
```

## Testing Notes

- pytest-asyncio in AUTO mode (no `@pytest.mark.asyncio` decorator needed)
- Fixtures in `tests/conftest.py`: `mock_deapi_client`, `mock_context`, `sample_*_response`
- When patching imports, patch at the source module (`src.deapi_client.get_client`), not the consumer

## Important Notes

- Always check model's `info.features.supports_guidance` before setting `guidance_scale`
  - `supports_guidance="0"`: Use `guidance_scale=0` (or value from `defaults.guidance`)
  - `supports_guidance="1"`: Use any value within `limits.min_guidance` to `limits.max_guidance`
- After every code change, run tests and linter before committing
