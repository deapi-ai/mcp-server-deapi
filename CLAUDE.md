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
- `python -m deapi_mcp.server_remote` - Start the HTTP MCP server (localhost:8000)
- `MCP_HOST=0.0.0.0 MCP_PORT=8000 python -m deapi_mcp.server_remote` - Start for remote access

### Testing
- `pytest` - Run all tests
- `pytest tests/test_tools.py` - Run tool tests only
- `pytest -k "test_name"` - Run specific test

### Code Quality
- `black deapi_mcp/ tests/` - Format code
- `ruff check deapi_mcp/ tests/` - Lint code
- `mypy deapi_mcp` - Type-check (strict; CI runs ruff, black, mypy and pytest on Python 3.11-3.14)

## Architecture

### Server

The primary server is `deapi_mcp/server_remote.py` — an HTTP/SSE-based MCP server built with FastMCP. This is the only server entry point (stdio transport is not used).

### Authentication Flow

1. `DeapiAuthProvider` in `deapi_mcp/fastmcp_auth.py` handles OAuth 2.0 (Authorization Code + PKCE)
2. Authenticated token is stored in `current_deapi_token` ContextVar (`deapi_mcp/auth.py`)
3. Tools call `get_client()` from `deapi_mcp/deapi_client.py` which reads the token from the ContextVar
4. Token is forwarded as Bearer auth to the deAPI REST API

### Core Modules

- **`deapi_mcp/deapi_client.py`** - Async HTTP client (`DeapiClient`) with auth forwarding; HTTP and retries are delegated to the deAPI SDK's transport (`deapi-sdk`) over one shared connection pool, with the token sent per request
- **`deapi_mcp/polling_manager.py`** - Smart adaptive polling for async job completion
- **`deapi_mcp/schemas.py`** - Pydantic models from the OpenAPI spec
- **`deapi_mcp/config.py`** - Configuration via environment variables (prefix: `DEAPI_`)
- **`deapi_mcp/middleware.py`** - Model cache middleware that enriches tool descriptions with available models
- **`deapi_mcp/auth_code_store.py`** - Single-use OAuth authorization codes: Redis when `DEAPI_REDIS_HOST` is set (any replica can redeem a code), in-memory otherwise (single replica only)
- **`deapi_mcp/observability.py`** - Per-call outcome (`CallOutcome` on a ContextVar, `note_*` recorders, `classify`) and injectable no-op sinks; the core has no metrics dependency
- **`deapi_mcp/server_remote.py`** - `register_core_tools(server)`, `create_app(server, *, metrics=None)` and `build_app()`; a deployment can import the core and inject a metrics overlay
- **`deapi_mcp/webhooks.py`** - Internal completion webhooks: HMAC-verified receiver at `/webhooks/deapi` and the job registry the poller waits on. Only mounted when `DEAPI_WEBHOOKS_INTERNAL` is set

### Tool Implementations (`deapi_mcp/tools/`)

Tools are organized by category:
- `audio.py` - Audio transcription, TTS, and music generation (12 tools)
- `image.py` - Image generation, OCR, background removal, upscaling (10 tools)
- `video.py` - Video generation, image-to-video, audio-to-video, description, upscale (10 tools)
- `embedding.py` - Text embeddings (2 tools)
- `utility.py` - Balance, models, job status (3 tools)
- `prompt.py` - Prompt enhancement tools (2 tools, registered in `server_remote.py`)
- `_price_helpers.py` - Resolves unset parameters from the model card, for price and generation tools alike

### Key Patterns

- **Stateless**: Server never stores API tokens; they're passed per-request via HTTP headers
- **Async polling**: `PollingManager` polls job status with type-specific strategies (audio: 1-5s, image: 2-8s, video: 5-30s)
- **Error handling**: `DeapiAPIError` for API errors; tools always return `{"success": bool, "error"?: str, "result_url"?: str, "job_id"?: str}`
- **Model cache**: Per account — accounts see different models, so each deAPI token has its own catalogue (keyed by an HMAC of the token, LRU-bounded, TTL 300 s, in process memory). Middleware enriches `tools/list` from the caller's catalogue. Tools call `ensure_model_cache(model=model)`: a slug missing from a fresh catalogue refetches it, at most once per 30 s
- **Long jobs**: one tool call waits at most `DEAPI_TOOL_MAX_WAIT` (45 s; many MCP clients drop a call at 60 s). A job still running then returns `success: true, done: false, job_id`, and `check_job_status(job_id, wait_seconds)` resumes the wait
- **Graceful drain**: on SIGTERM uvicorn lets in-flight tool calls finish for up to `MCP_GRACEFUL_SHUTDOWN_SECONDS` (930 s). `create_app()` disables sse-starlette's automatic drain on purpose, since every tool call answers as an SSE stream
- **Webhooks**: two independent features. Callers may pass `webhook_url`/`webhook_secret` to any generation tool (validated once in `DeapiClient.submit_job`); the API notifies *their* systems. Separately, with `DEAPI_WEBHOOKS_INTERNAL` the server registers itself and the poller waits on the callback instead of sleeping, polling only every `webhook_fallback_poll_interval` as a safety net. The API takes one webhook per job, so a caller's URL wins and that job polls normally. The callback is a wake-up signal only — the tool always makes one final `get_job_status()`, because the webhook payload carries `result_url` but not inline `result`
- **Card-driven defaults**: `steps`, `fps`, `frames`, `width`, `height` and `guidance` have no fixed defaults in the tools — the API requires them and validates them against the model card, and no single number suits every model (Flux1schnell caps `steps` at 10, Ltxv fixes `fps` at 30, Ltx2 at 24). Generation tools call `ensure_model_cache()` then `resolve_from_card()`; price tools use `resolve_generation_params()`, which also guesses when the card is silent
- **Form booleans**: a multipart field is a string, and Laravel's built-in `boolean` rule accepts only `"1"`/`"0"` — always render with `form_bool()` from `deapi_mcp/utils.py`

## Adding New Tools

1. Check the OpenAPI spec at https://docs.deapi.ai/ for endpoint details
2. Add request/response schemas to `deapi_mcp/schemas.py` if needed
3. Create tool function in the appropriate file under `deapi_mcp/tools/`
4. Register in `deapi_mcp/server_remote.py` with `mcp.tool()(your_function)` — registration is call-style at module level, NOT the `@mcp.tool()` decorator
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

# Per-job-type polling overrides
DEAPI_POLLING_AUDIO__INITIAL_DELAY=1.0
DEAPI_POLLING_AUDIO__MAX_DELAY=5.0
DEAPI_POLLING_AUDIO__TIMEOUT=300.0

# Internal completion webhooks (see deapi_mcp/webhooks.py). Off by default.
# Receiver URL must be reachable from api.deapi.ai; half-configured refuses to start.
DEAPI_WEBHOOKS_INTERNAL=false
DEAPI_WEBHOOK_RECEIVER_URL=https://mcp.example.com/webhooks/deapi
DEAPI_WEBHOOK_SECRET=<32-255 chars>
DEAPI_WEBHOOK_FALLBACK_POLL_INTERVAL=45.0

# OAuth authorization code store. Unset host = in-memory store, single replica only.
DEAPI_REDIS_HOST=redis.example.com
DEAPI_REDIS_DB=11
DEAPI_REDIS_PREFIX=deapi_mcp_database_
DEAPI_REDIS_REQUIRED=false

# Seconds in-flight tool calls may finish after SIGTERM
MCP_GRACEFUL_SHUTDOWN_SECONDS=930
```

## Testing Notes

- pytest-asyncio in AUTO mode (no `@pytest.mark.asyncio` decorator needed)
- Fixtures in `tests/conftest.py`: `mock_deapi_client`, `mock_context`, `sample_*_response`
- When patching imports, patch at the source module (`deapi_mcp.deapi_client.get_client`), not the consumer

## Important Notes

- The API field is **`guidance`**, not `guidance_scale`. `guidance_scale` is only the tools' own
  parameter name; a payload key of `guidance_scale` is silently dropped.
- `supports_guidance` is `False` on every model in the catalogue, so guidance has no effect today.
  Leave it unset and the model's `info.defaults` applies. Re-check this before assuming it still
  holds — the code must start honouring it the day a model ships with it enabled.
- An unknown field is accepted with `200` and silently discarded; only a bad value on a *known*
  field returns `422`. Verify new fields against the API's validation rules, not by observing a `200`.
- After every code change, run tests and linter before committing
