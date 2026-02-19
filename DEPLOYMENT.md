# Remote Deployment Guide

This guide explains how to deploy the deAPI MCP Server on a remote host and connect to it from Claude Desktop or other MCP clients.

## Overview

The deAPI MCP Server (`src/server_remote.py`) uses HTTP/SSE transport and can run in two deployment scenarios:

1. **Local Deployment** - Server runs on your local machine (localhost:8000)
2. **Remote Deployment** - Server deployed to a remote host accessible over the network

This guide focuses on **remote deployment** for production environments. For local usage, see the main [README.md](README.md).

## Remote Deployment Options

### Option 1: Direct Python Deployment

#### On the Remote Server

1. Clone the repository and install dependencies:
```bash
git clone https://github.com/deapi-ai/mcp-server-deapi.git
cd mcp-server-deapi
uv pip install -e .
# or
pip install -e .
```

2. Set environment variables (optional):
```bash
export MCP_HOST=0.0.0.0  # Listen on all interfaces
export MCP_PORT=8000     # Port to listen on
```

3. Run the remote server:
```bash
python -m src.server_remote
```

The server will be available at `http://your-server-ip:8000/mcp`

4. For production, use a process manager like systemd:

Create `/etc/systemd/system/mcp-server-deapi.service`:
```ini
[Unit]
Description=deAPI MCP Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/mcp-server-deapi
Environment="MCP_HOST=0.0.0.0"
Environment="MCP_PORT=8000"
ExecStart=/usr/bin/python3 -m src.server_remote
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable mcp-server-deapi
sudo systemctl start mcp-server-deapi
sudo systemctl status mcp-server-deapi
```

### Option 2: Docker Deployment

#### Build and Run with Docker

1. Build the Docker image:
```bash
docker build -t mcp-server-deapi .
```

2. Run the container:
```bash
docker run -d \
  -p 8000:8000 \
  --name mcp-server-deapi \
  --restart unless-stopped \
  mcp-server-deapi
```

3. Check logs:
```bash
docker logs -f mcp-server-deapi
```

#### Using Docker Compose

Create `docker-compose.yml`:
```yaml
version: '3.8'

services:
  mcp-server-deapi:
    build: .
    ports:
      - "8000:8000"
    environment:
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8000
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

Run with:
```bash
docker-compose up -d
```

### Option 3: Cloud Platform Deployment

#### Railway.app
1. Push code to GitHub
2. Connect Railway to your repository
3. Set port to 8000
4. Deploy automatically

#### Fly.io
```bash
fly launch
fly deploy
```

#### Heroku
```bash
heroku create mcp-server-deapi
git push heroku main
```

## Connecting MCP Clients

### From Claude Desktop

Edit your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "deapi-remote": {
      "url": "http://your-server-ip:8000/mcp"
    }
  }
}
```

Or if using HTTPS with a domain:
```json
{
  "mcpServers": {
    "deapi-remote": {
      "url": "https://mcp-server-deapi.yourdomain.com/mcp"
    }
  }
}
```

### From Other MCP Clients

Use the MCP Python SDK to connect:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async def connect_to_remote_server():
    async with sse_client("http://your-server-ip:8000/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools
            tools = await session.list_tools()

            # Call a tool (authentication is handled at the connection level)
            result = await session.call_tool(
                "get_available_models",
                arguments={}
            )
```

## Security Considerations

### 1. Use HTTPS in Production

Place the MCP server behind a reverse proxy (nginx, Caddy, etc.) with SSL:

**Nginx example:**
```nginx
server {
    listen 443 ssl http2;
    server_name mcp-server-deapi.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE specific
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400;
    }
}
```

**Caddy example (simpler):**
```
mcp-server-deapi.yourdomain.com {
    reverse_proxy localhost:8000
}
```

### 2. Add Authentication (Optional)

For production, you may want to add authentication to the MCP server itself. FastMCP supports this:

```python
from fastmcp.server.auth.providers import APIKeyProvider

# Add to server_remote.py
auth = APIKeyProvider(api_keys=["your-secret-key"])
mcp = FastMCP(
    name="deAPI AI API",
    auth=auth,
    # ... rest of config
)
```

Then clients need to include the API key:
```json
{
  "mcpServers": {
    "deapi-remote": {
      "url": "https://mcp-server-deapi.yourdomain.com/mcp",
      "headers": {
        "X-API-Key": "your-secret-key"
      }
    }
  }
}
```

### 3. Firewall Configuration

Only expose port 8000 (or your chosen port) and limit access:

```bash
# UFW example (Ubuntu)
sudo ufw allow 8000/tcp
sudo ufw enable

# Or restrict to specific IPs
sudo ufw allow from YOUR_CLIENT_IP to any port 8000
```

### 4. Rate Limiting

Consider adding rate limiting in your reverse proxy or using a service like Cloudflare.

## Monitoring

### Health Check Endpoint

The server includes a built-in health check endpoint at `/health`:

```bash
curl http://localhost:8000/health
# {"status": "healthy", "service": "deapi-mcp"}
```

This is used by the Docker `HEALTHCHECK` and can be integrated with your monitoring system.

### Logging

The server logs to stdout. Capture with your logging system:

```bash
# For systemd
journalctl -u mcp-server-deapi -f

# For Docker
docker logs -f mcp-server-deapi

# Or redirect to file
python -m src.server_remote 2>&1 | tee /var/log/mcp-server-deapi.log
```

## Troubleshooting

### Connection Refused
- Check firewall rules
- Verify server is running: `netstat -tlnp | grep 8000`
- Check server logs

### SSE Connection Drops
- Increase proxy timeouts (nginx: `proxy_read_timeout`)
- Check network stability
- Enable reconnection logic in client

## Cost Optimization

For lower-traffic scenarios:
- Use serverless platforms (Railway, Fly.io free tier)
- Scale down during off-hours
- Use spot instances on AWS/GCP

## Testing Remote Connection

Test the remote server is working:

```bash
# Test SSE endpoint
curl -N http://your-server-ip:8000/mcp

# Should return SSE stream
```

From Python:
```python
import httpx

response = httpx.get("http://your-server-ip:8000/mcp", timeout=None)
print(response.status_code)  # Should be 200
```