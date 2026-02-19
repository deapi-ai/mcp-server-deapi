# deAPI MCP Server

Production-ready Model Context Protocol (MCP) server for the [deAPI](https://deapi.ai) REST API. This server exposes all deAPI AI capabilities as MCP tools, enabling LLMs to perform audio transcription, image generation, OCR, video generation, text-to-speech, and more.

## Features

- **Complete API Coverage**: 29 deAPI endpoints exposed as MCP tools
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
- `audio_url_transcription` - Transcribe audio from Twitter Spaces URLs
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
- `text_to_video_price` - Calculate text-to-video cost
- `image_to_video_price` - Calculate image-to-video cost

### Embedding Tools
- `text_to_embedding` - Generate text embeddings for semantic search
- `text_to_embedding_price` - Calculate embedding cost

### Utility Tools
- `get_balance` - Check account balance
- `get_available_models` - List available AI models with specifications
- `check_job_status` - Query async job status by ID

## Installation

### Prerequisites

**For running the MCP server:**
- Python 3.10 or higher
- `uv`, `pip`, or `conda` for package management

**For a deAPI account:**
- Sign up at [api.deapi.ai](https://api.deapi.ai) and get your API token

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
python -m src.server_remote
```

The server will start on `http://localhost:8000` by default.

**Remote Mode** (for deployment to a remote server):
```bash
# Set host to accept external connections
MCP_HOST=0.0.0.0 MCP_PORT=8000 python -m src.server_remote
```

See the [Remote Deployment](#remote-deployment) section for production deployment options.

### Connecting from Claude Desktop / Claude.ai

#### Option 1: Add Connector (Recommended)

Both Claude Desktop and Claude.ai support MCP connectors with built-in OAuth authentication.

1. Get your deAPI token from [api.deapi.ai](https://api.deapi.ai)
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

- **DeapiClient** (`src/deapi_client.py`): HTTP client with auth forwarding and retry logic
- **PollingManager** (`src/polling_manager.py`): Smart adaptive polling for async jobs
- **Schemas** (`src/schemas.py`): Pydantic models for type safety
- **Tools** (`src/tools/`): Organized tool implementations
  - `audio.py` - Audio transcription & TTS tools
  - `image.py` - Image generation, OCR, background removal & upscaling
  - `video.py` - Video generation tools
  - `embedding.py` - Text embedding tools
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

- **HTTP Errors**: Automatic retry (3 attempts) with exponential backoff
- **Timeouts**: Graceful handling with clear error messages
- **Job Failures**: Detected and reported to the client
- **API Errors**: Properly formatted error responses

## Configuration

Configuration can be set via environment variables (prefixed with `DEAPI_`):

```bash
# API Configuration
DEAPI_API_BASE_URL=https://api.deapi.ai
DEAPI_API_VERSION=v1

# HTTP Client
DEAPI_HTTP_TIMEOUT=30.0
DEAPI_MAX_RETRIES=3
DEAPI_RETRY_BACKOFF_FACTOR=2.0

# Polling Configuration (override defaults)
DEAPI_POLLING_AUDIO__INITIAL_DELAY=1.0
DEAPI_POLLING_AUDIO__MAX_DELAY=5.0
DEAPI_POLLING_AUDIO__TIMEOUT=300.0
```

## Development

### Project Structure

```
mcp-server-deapi/
├── src/
│   ├── server_remote.py       # Streamable-HTTP MCP server
│   ├── deapi_client.py        # HTTP client with auth forwarding
│   ├── polling_manager.py     # Smart adaptive polling logic
│   ├── schemas.py             # Pydantic models
│   ├── config.py              # Configuration management
│   ├── auth.py                # Authentication middleware
│   ├── fastmcp_auth.py        # FastMCP OAuth provider
│   ├── oauth_endpoints.py     # OAuth 2.0 endpoints
│   └── tools/                 # Tool implementations
│       ├── audio.py           # Audio transcription & TTS
│       ├── image.py           # Image generation, OCR & processing
│       ├── video.py           # Video generation
│       ├── embedding.py       # Text embeddings
│       ├── utility.py         # Balance, models, status
│       └── _price_helpers.py  # Price calculation helpers
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
black src/
```

Lint with Ruff:
```bash
ruff check src/
```

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
python -m src.server_remote
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
