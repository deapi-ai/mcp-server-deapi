# deAPI MCP Server

Production-ready Model Context Protocol (MCP) server for the [deAPI](https://deapi.ai) REST API. This server exposes all deAPI AI capabilities as MCP tools, enabling LLMs to perform audio transcription, image generation, OCR, video generation, text-to-speech, and more.

> **deAPI v2** — this server targets the deAPI v2 client API (OpenAI-aligned noun paths under `/api/v2/*`). API v1 is not supported: the tools send v2 payloads only.

## Features

- **Complete API Coverage**: 39 deAPI tools exposed via MCP, covering every v2 client endpoint
- **Smart Adaptive Polling**: Automatically polls async jobs with optimized intervals based on job type
- **OAuth 2.0 Authentication**: Secure token exchange via OAuth Authorization Code flow with PKCE
- **Error Recovery**: Automatic retry logic with exponential backoff
- **Progress Reporting**: Real-time progress updates to MCP clients
- **Type Safety**: Full Pydantic schema validation
- **Production Ready**: Built with FastMCP framework for reliability

## Available Tools

### Audio Tools
- `audio_transcription` - Transcribe audio files to text using Whisper models
- `audio_transcription_price` - Calculate transcription cost
- `text_to_audio` - Convert text to natural speech (TTS)
- `text_to_audio_price` - Calculate TTS cost
- `text_to_music` - Generate music from text description and lyrics
- `text_to_music_price` - Calculate music generation cost
- `audio_url_transcription` - Transcribe audio from URLs of completed Twitter Spaces
- `audio_url_transcription_price` - Calculate Twitter Spaces transcription cost

### Video Transcription Tools
- `video_file_transcription` - Transcribe video files to text
- `video_file_transcription_price` - Calculate video file transcription cost
- `video_url_transcription` - Transcribe videos from URLs (YouTube, Twitter/X, Twitch, Kick)
- `video_url_transcription_price` - Calculate video URL transcription cost

### Image Tools
- `text_to_image` - Generate images from text prompts
- `image_to_image` - Transform images with text guidance
- `image_to_text` - Extract text from images (OCR)
- `image_remove_background` - Remove background from images
- `image_upscale` - Upscale images to higher resolution
- `text_to_image_price` - Calculate image generation cost
- `image_to_image_price` - Calculate image transformation cost
- `image_to_text_price` - Calculate OCR cost
- `image_remove_background_price` - Calculate background removal cost
- `image_upscale_price` - Calculate upscaling cost

### Video Tools
- `text_to_video` - Generate videos from text prompts
- `image_to_video` - Animate static images into videos
- `audio_to_video` - Generate video conditioned on audio content
- `video_description` - Describe a video in text (from an uploaded file or a video URL)
- `video_upscale` - Upscale a video to higher resolution
- `text_to_video_price` - Calculate text-to-video cost
- `image_to_video_price` - Calculate image-to-video cost
- `audio_to_video_price` - Calculate audio-to-video cost
- `video_description_price` - Calculate video description cost
- `video_upscale_price` - Calculate video upscaling cost

### Embedding Tools
- `text_to_embedding` - Generate text embeddings for semantic search
- `text_to_embedding_price` - Calculate embedding cost

### Prompt Tools
- `prompt_booster` - Enhance a prompt for any deAPI inference type using AI guides (synchronous, returns refined prompt directly)
- `prompt_booster_price` - Calculate prompt-enhancement cost

### Utility Tools
- `get_balance` - Check account balance
- `get_available_models` - List available AI models with specifications
- `check_job_status` - Query async job status by ID. A failed job reports `success: false` with `error_reason`, `error_code`, `retryable` and `refunded`

### Webhooks

Every generation tool accepts `webhook_url` (HTTPS) and an optional `webhook_secret` (32–255 chars). deAPI calls that URL when the job finishes, signed with `X-DeAPI-Signature: sha256=<HMAC-SHA256(secret, "<timestamp>.<body>")>`; the tool still returns the result to the caller as usual.

### Model-driven defaults

Generation parameters such as `width`, `height`, `steps`, `frames` and `fps` default to the selected model's own values when left unset — each model has different limits, so there is no fixed default that works everywhere. The `get_available_models` output and every tool description list each model's accepted ranges. Set `enhance_prompt: true` on `text_to_image`/`image_to_image` to let deAPI rewrite the prompt; the result then includes `prompt_boost` with the prompt actually used.

## Installation

### Prerequisites

**For running the MCP server:**
- Python 3.11 or higher
- `uv`, `pip`, or `conda` for package management

**For a deAPI account:**
- Sign up at [deapi.ai](https://deapi.ai/settings/api-keys) and get your API token

### Setup

1. Clone the repository:
```bash
git clone https://github.com/deapi-ai/mcp-server-deapi.git
cd mcp-server-deapi
```

2. **Choose your Python environment setup:**

**Option A: Using `uv` (recommended - fastest)**
```bash
uv pip install -e .
```

**Option B: Using `pip`**
```bash
pip install -e .
```

**Option C: Using `conda`**
```bash
# Create conda environment
conda create -n mcp-server-deapi python=3.11
conda activate mcp-server-deapi

# Install dependencies
pip install -e .
```

3. (Optional) Create a `.env` file for configuration:
```bash
# Copy the example file
cp .env.example .env

# Edit with your preferences (optional - defaults work fine)
# DEAPI_API_BASE_URL=https://api.deapi.ai
# DEAPI_HTTP_TIMEOUT=30.0
# DEAPI_MAX_RETRIES=3
```

## Usage

### Running the Server

The server can run in two modes:

**Local Mode** (for use with Claude Desktop on the same machine):
```bash
python -m deapi_mcp.server_remote
```

The server will start on `http://localhost:8000` by default.

**Remote Mode** (for deployment to a remote server):
```bash
# Set host to accept external connections
MCP_HOST=0.0.0.0 MCP_PORT=8000 python -m deapi_mcp.server_remote
```

See the [Remote Deployment](#remote-deployment) section for production deployment options.

### Connecting from Claude Desktop / Claude.ai

#### Option 1: Add Connector (Recommended)

Both Claude Desktop and Claude.ai support MCP connectors with built-in OAuth authentication.

1. Get your deAPI token from [deapi.ai](https://deapi.ai/settings/api-keys)
2. In Claude Desktop or Claude.ai, go to **Settings → Connectors → Add Connector**
3. Fill in the connector details:

```
Name:              deAPI
Remote MCP server: https://your-server-domain:8000/mcp

▼ Advanced settings
OAuth Client ID:     deapi-mcp
OAuth Client Secret: YOUR_DEAPI_TOKEN
```

4. Click **Add** — Claude will automatically authenticate via OAuth and discover all tools.

For details on the OAuth flow, see [AUTH.md](AUTH.md).

---

#### Option 2: Config File with Bearer Token (Local Development)

**Best for:** Server running on the same machine, quick setup without OAuth.

Edit your Claude Desktop config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "deapi": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_DEAPI_TOKEN"
      }
    }
  }
}
```

Replace `YOUR_DEAPI_TOKEN` with your actual deAPI token. Save the file and restart Claude Desktop.

### Using the Tools

**Authentication is handled at the connection level**, not per-tool-call. Tools do NOT accept a `deapi_api_token` parameter.

Here's an example workflow:

1. **Get available models**:
```
Use get_available_models to see available models
```

2. **Check your balance**:
```
Use get_balance to check remaining credits
```

3. **Generate an image**:
```
Use text_to_image with:
- prompt: "A beautiful sunset over mountains"
- model: "Flux1schnell"
```

4. **Transcribe audio**:
```
Use audio_transcription with:
- audio: "base64-encoded-audio-or-url"
- include_ts: true
```

**Note**: When calling tools via Claude Desktop or MCP SDK, authentication is handled automatically through the server connection (OAuth or HTTP headers). See [AUTH.md](AUTH.md) for detailed OAuth setup.

## Architecture

### Key Components

- **DeapiClient** (`deapi_mcp/deapi_client.py`): HTTP client with auth forwarding, built on the [deAPI Python SDK](https://github.com/deapi-ai/deapi-python-sdk) transport
- **PollingManager** (`deapi_mcp/polling_manager.py`): Smart adaptive polling for async jobs
- **Schemas** (`deapi_mcp/schemas.py`): Pydantic models for type safety
- **Auth code store** (`deapi_mcp/auth_code_store.py`): single-use OAuth authorization codes, in Redis when `DEAPI_REDIS_HOST` is set (so any replica can finish a login), otherwise in process memory
- **Webhooks** (`deapi_mcp/webhooks.py`): optional HMAC-verified receiver for deAPI completion callbacks, so waiting tools wake on the callback instead of polling (`DEAPI_WEBHOOKS_INTERNAL`)
- **Tools** (`deapi_mcp/tools/`): Organized tool implementations
  - `audio.py` - Audio transcription, TTS & music generation tools
  - `image.py` - Image generation, transformation, OCR, background removal & upscaling
  - `video.py` - Video generation, image/audio-to-video, description & upscale tools
  - `embedding.py` - Text embedding tools
  - `prompt.py` - Prompt booster tools
  - `utility.py` - Balance, models, status tools

### Smart Adaptive Polling

The server uses job-type-specific polling strategies:

| Job Type | Initial Delay | Max Delay | Timeout |
|----------|--------------|-----------|---------|
| Audio    | 1s           | 5s        | 5 min   |
| Image    | 2s           | 8s        | 5 min   |
| Video    | 5s           | 30s       | 15 min  |

Polling uses exponential backoff with a configurable multiplier (default: 1.5x).

### Error Handling

- **HTTP Errors**: Reads are retried (3 attempts) with exponential backoff; a job submission is retried only when it provably was not processed (rate limited, or the connection never opened), so a job is never submitted twice
- **Timeouts**: Reported as a failed result that still carries the `job_id`, so the job can be picked up with `check_job_status`
- **Job Failures**: Reported with the API's reason, error code, and whether the job is retryable / was refunded
- **API Errors**: Properly formatted error responses

## Configuration

Configuration can be set via environment variables (prefixed with `DEAPI_`):

```bash
# API Configuration
DEAPI_API_BASE_URL=https://api.deapi.ai
DEAPI_API_VERSION=v2

# HTTP Client
DEAPI_HTTP_TIMEOUT=30.0
DEAPI_MAX_RETRIES=3
DEAPI_RETRY_BACKOFF_FACTOR=2.0

# Polling Configuration (override defaults)
DEAPI_POLLING_AUDIO__INITIAL_DELAY=1.0
DEAPI_POLLING_AUDIO__MAX_DELAY=5.0
DEAPI_POLLING_AUDIO__TIMEOUT=300.0

# Internal completion webhooks (off by default). The receiver URL must be
# reachable from api.deapi.ai; a half-configured set refuses to start.
DEAPI_WEBHOOKS_INTERNAL=false
DEAPI_WEBHOOK_RECEIVER_URL=https://mcp.example.com/webhooks/deapi
DEAPI_WEBHOOK_SECRET=<32-255 chars>
DEAPI_WEBHOOK_FALLBACK_POLL_INTERVAL=45.0

# Shared store for OAuth authorization codes (optional). Unset host = in-memory
# store, which only works with a single replica.
DEAPI_REDIS_HOST=redis.example.com
DEAPI_REDIS_PORT=6379
DEAPI_REDIS_PASSWORD=
DEAPI_REDIS_DB=11
DEAPI_REDIS_SCHEME=plain          # plain | tls (tls needs DEAPI_REDIS_CA_FILE)
DEAPI_REDIS_REQUIRED=false        # true = refuse to start without a Redis host

# Seconds in-flight tool calls may run after SIGTERM before they are cancelled.
# Keep it below your platform's termination grace period.
MCP_GRACEFUL_SHUTDOWN_SECONDS=930
```

With internal webhooks on, every job the server submits carries its own receiver URL and the API calls back the moment the job ends; the waiting tool wakes at once, with a slow poll underneath as a safety net. A caller-supplied `webhook_url` takes precedence for that job (the API takes one webhook per job), which then polls normally.

## Development

### Project Structure

```
mcp-server-deapi/
├── deapi_mcp/
│   ├── server_remote.py       # Streamable-HTTP MCP server
│   ├── deapi_client.py        # HTTP client with auth forwarding
│   ├── polling_manager.py     # Smart adaptive polling logic
│   ├── schemas.py             # Pydantic models
│   ├── config.py              # Configuration management
│   ├── auth.py                # Authentication middleware
│   ├── fastmcp_auth.py        # FastMCP OAuth provider
│   ├── oauth_endpoints.py     # OAuth 2.0 endpoints
│   ├── auth_code_store.py     # OAuth authorization codes (Redis or in-memory)
│   ├── observability.py       # Per-call outcome, injectable metrics sinks
│   ├── webhooks.py            # Internal completion-webhook receiver
│   └── tools/                 # Tool implementations
│       ├── audio.py           # Audio transcription, TTS & music generation
│       ├── image.py           # Image generation, OCR & processing
│       ├── video.py           # Video generation, image/audio-to-video, description & upscale
│       ├── embedding.py       # Text embeddings
│       ├── prompt.py          # Prompt booster
│       ├── utility.py         # Balance, models, status
│       ├── _price_helpers.py  # Resolve parameters from the model card
│       └── _responses.py      # Shared tool response shape
├── tests/                     # Test suite
│   ├── __init__.py
│   └── conftest.py           # Pytest fixtures
├── pyproject.toml             # Dependencies
├── Dockerfile                 # Container build
├── docker-compose.yml         # Container orchestration
├── .env.example              # Environment config template
├── README.md                  # This file
├── DEPLOYMENT.md              # Deployment guide
├── AUTH.md                    # OAuth authentication setup
└── CLAUDE.md                  # Claude Code guidance
```

### Running Tests

Install dev dependencies:
```bash
uv pip install -e ".[dev]"
```

Run tests:
```bash
pytest
```

Run smoke tests (requires a running server):
```bash
python tests/smoke_test.py
```

### Code Formatting

Format code with Black:
```bash
black deapi_mcp/ tests/
```

Lint with Ruff:
```bash
ruff check deapi_mcp/ tests/
```

Type-check with mypy (strict):
```bash
mypy deapi_mcp
```

CI runs all three plus the test suite on Python 3.11–3.14.

### Building your own server on the core

`deapi_mcp` is importable, so a deployment can assemble its own server from the same tools
and add what it needs (for example its own metrics) without forking:

```python
from fastmcp import FastMCP
from deapi_mcp.server_remote import create_app, register_core_tools

server = FastMCP("my-deapi")
register_core_tools(server)            # every deAPI tool
app = create_app(server, metrics=None) # Starlette app: /mcp, /health, OAuth routes
```

As with the stock server, set `DEAPI_JWT_SECRET_KEY` first: without it the app refuses to start
on a non-local host.

`metrics` is an optional overlay that instruments tool calls and OAuth and mounts its own
routes; the stock server (`build_app()`) passes none. Recorders for per-call outcomes and
rejected logins plug in through `deapi_mcp.observability`.

## API Token Security

**Important**: The MCP server does NOT store API tokens. Authentication works as follows:

1. **For Remote HTTP Server**: Authentication is handled via OAuth 2.0 (Authorization Code with PKCE) or HTTP headers (Authorization: Bearer token)
2. **Token forwarding**: The server forwards authentication to the deAPI API for each request
3. **No persistence**: Tokens are used only for the specific request and never persisted or logged
4. **Per-connection auth**: Tools do NOT accept `deapi_api_token` parameters - authentication is managed at the connection level

Always keep your API tokens secure and never commit them to version control. See [AUTH.md](AUTH.md) for detailed OAuth setup.

## Remote Deployment

For production environments or when you want to host the MCP server on a remote machine, use the remote server mode.

### Quick Start with Docker

1. **Build and run with Docker:**
```bash
docker build -t mcp-server-deapi .
docker run -d -p 8000:8000 --name mcp-server-deapi mcp-server-deapi
```

2. **Or use Docker Compose:**
```bash
docker-compose up -d
```

3. **Configure Claude Desktop to connect:**
```json
{
  "mcpServers": {
    "deapi": {
      "url": "http://your-server-ip:8000/mcp"
    }
  }
}
```

### Manual Remote Deployment

1. **On your remote server:**
```bash
git clone https://github.com/deapi-ai/mcp-server-deapi.git
cd mcp-server-deapi
pip install -e .
python -m deapi_mcp.server_remote
```

2. **For production with systemd:**
```bash
# Create /etc/systemd/system/mcp-server-deapi.service
sudo systemctl enable mcp-server-deapi
sudo systemctl start mcp-server-deapi
```

3. **Behind a reverse proxy (nginx + SSL):**
```nginx
server {
    listen 443 ssl http2;
    server_name mcp.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }
}
```

### Cloud Deployment Options

- **Railway.app**: Push to GitHub, connect repository, deploy automatically
- **Fly.io**: `fly launch && fly deploy`
- **Heroku**: `heroku create && git push heroku main`
- **DigitalOcean**: Use App Platform or Droplets with Docker
- **AWS/GCP/Azure**: Deploy with container services (ECS, Cloud Run, Container Instances)

For detailed deployment instructions, security considerations, monitoring, and troubleshooting, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Troubleshooting

### Connection Issues

If the server fails to connect:
1. Check your API token is valid
2. Verify network connectivity to api.deapi.ai
3. Check the logs for specific error messages
4. For remote servers: verify firewall rules and that port 8000 is accessible

### Job Timeouts

If jobs are timing out:
1. Check your balance with `get_balance`
2. Verify the job type timeout is appropriate
3. Use `check_job_status` to check if the job is still processing

### Model Not Found

If you get model errors:
1. Use `get_available_models` to see available models
2. Ensure you're using the correct model name
3. Check if the model supports your requested operation

### Remote Connection Issues

If remote MCP connection fails:
1. Test the endpoint: `curl -N http://your-server:8000/mcp`
2. Check server logs: `docker logs mcp-server-deapi` or `journalctl -u mcp-server-deapi`
3. Verify firewall rules and SSL certificates (if using HTTPS)
4. Ensure MCP endpoint is accessible from your client

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues related to:
- **This MCP Server**: [Open an issue](https://github.com/deapi-ai/mcp-server-deapi/issues)
- **deAPI Platform**: Visit [docs.deapi.ai](https://docs.deapi.ai/)
- **MCP Protocol**: Visit [modelcontextprotocol.io](https://modelcontextprotocol.io)
<!-- mcp-name: io.github.deapi-ai/mcp-server-deapi -->
