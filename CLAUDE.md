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
- **`src/webhooks.py`** - Internal completion webhooks: HMAC-verified receiver at `/webhooks/deapi` and the job registry the poller waits on. Only mounted when `DEAPI_WEBHOOKS_INTERNAL` is set

### Tool Implementations (`src/tools/`)

Tools are organized by category:
- `audio.py` - Audio transcription, TTS, and music generation (12 tools)
- `image.py` - Image generation, OCR, background removal, upscaling (10 tools)
- `video.py` - Video generation, image-to-video, audio-to-video, upscale, replace (12 tools)
- `embedding.py` - Text embeddings (2 tools)
- `utility.py` - Balance, models, job status (3 tools)
- `prompt.py` - Prompt enhancement tools (2 tools, registered in `server_remote.py`)
- `_price_helpers.py` - Resolves unset parameters from the model card, for price and generation tools alike

### Key Patterns

- **Stateless**: Server never stores API tokens; they're passed per-request via HTTP headers
- **Async polling**: `PollingManager` polls job status with type-specific strategies (audio: 1-5s, image: 2-8s, video: 5-30s)
- **Error handling**: `DeapiAPIError` for API errors; tools always return `{"success": bool, "error"?: str, "result_url"?: str, "job_id"?: str}`
- **Model cache**: Middleware fetches available models on first `list_tools` call and enriches tool descriptions
- **Webhooks**: two independent features. Callers may pass `webhook_url`/`webhook_secret` to any generation tool (validated once in `DeapiClient.submit_job`); the API notifies *their* systems. Separately, with `DEAPI_WEBHOOKS_INTERNAL` the server registers itself and the poller waits on the callback instead of sleeping, polling only every `webhook_fallback_poll_interval` as a safety net. The API takes one webhook per job, so a caller's URL wins and that job polls normally. The callback is a wake-up signal only — the tool always makes one final `get_job_status()`, because the webhook payload carries `result_url` but not inline `result`
- **Card-driven defaults**: `steps`, `fps`, `frames`, `width`, `height` and `guidance` have no fixed defaults in the tools — the API requires them and validates them against the model card, and no single number suits every model (Flux1schnell caps `steps` at 10, Ltxv fixes `fps` at 30, Ltx2 at 24). Generation tools call `ensure_model_cache()` then `resolve_from_card()`; price tools use `resolve_generation_params()`, which also guesses when the card is silent
- **Form booleans**: a multipart field is a string, and Laravel's built-in `boolean` rule accepts only `"1"`/`"0"` — always render with `form_bool()` from `src/utils.py`

## Adding New Tools

1. Check the OpenAPI spec at https://docs.deapi.ai/ for endpoint details
2. Add request/response schemas to `src/schemas.py` if needed
3. Create tool function in the appropriate file under `src/tools/`
4. Register in `src/server_remote.py` with `mcp.tool()(your_function)` — registration is call-style at module level, NOT the `@mcp.tool()` decorator
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
DEAPI_API_VERSION=v2
DEAPI_HTTP_TIMEOUT=30.0
DEAPI_MAX_RETRIES=3
DEAPI_RETRY_BACKOFF_FACTOR=2.0

# Per-job-type polling overrides
DEAPI_POLLING_AUDIO__INITIAL_DELAY=1.0
DEAPI_POLLING_AUDIO__MAX_DELAY=5.0
DEAPI_POLLING_AUDIO__TIMEOUT=300.0

# Internal completion webhooks (see src/webhooks.py). Off by default.
# Receiver URL must be reachable from api.deapi.ai; half-configured refuses to start.
DEAPI_WEBHOOKS_INTERNAL=false
DEAPI_WEBHOOK_RECEIVER_URL=https://mcp.example.com/webhooks/deapi
DEAPI_WEBHOOK_SECRET=<32-255 chars>
DEAPI_WEBHOOK_FALLBACK_POLL_INTERVAL=45.0
```

## Testing Notes

- pytest-asyncio in AUTO mode (no `@pytest.mark.asyncio` decorator needed)
- Fixtures in `tests/conftest.py`: `mock_deapi_client`, `mock_context`, `sample_*_response`
- When patching imports, patch at the source module (`src.deapi_client.get_client`), not the consumer

## Important Notes

- The API field is **`guidance`**, not `guidance_scale`. `guidance_scale` is only the tools' own
  parameter name; a payload key of `guidance_scale` is silently dropped.
- `supports_guidance` is `False` on every model in the catalogue, so guidance has no effect today.
  Leave it unset and the model's `info.defaults` applies. Re-check this before assuming it still
  holds — the code must start honouring it the day a model ships with it enabled.
- An unknown field is accepted with `200` and silently discarded; only a bad value on a *known*
  field returns `422`. Verify new fields against the API's validation rules, not by observing a `200`.
- After every code change, run tests and linter before committing
