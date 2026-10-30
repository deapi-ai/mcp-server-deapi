# Remote Deployment Guide

This guide explains how to deploy the deAPI MCP Server on a remote host and connect to it from Claude Desktop or other MCP clients.

## Overview

The deAPI MCP Server (`deapi_mcp/server_remote.py`) uses HTTP/SSE transport and can run in two deployment scenarios:

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

2. Set environment variables:
```bash
export MCP_HOST=0.0.0.0  # Listen on all interfaces
export MCP_PORT=8000     # Port to listen on
# Required off loopback: OAuth tokens are signed with it, and without it the server
# refuses to start. Use the same value on every replica and keep it across restarts.
export DEAPI_JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

3. Run the remote server:
```bash
python -m deapi_mcp.server_remote
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
Environment="DEAPI_JWT_SECRET_KEY=<a long random secret>"
ExecStart=/usr/bin/python3 -m deapi_mcp.server_remote
Restart=always
# Let in-flight tool calls finish on stop (MCP_GRACEFUL_SHUTDOWN_SECONDS defaults to 930)
TimeoutStopSec=960

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
  --stop-timeout 960 \
  -e DEAPI_JWT_SECRET_KEY="<a long random secret>" \
  mcp-server-deapi
```

3. Check logs:
```bash
docker logs -f mcp-server-deapi
```

#### Using Docker Compose

The repository ships a `docker-compose.yml`. It requires `DEAPI_JWT_SECRET_KEY` (from the shell
or a `.env` file next to it) and gives the container 960 s to stop gracefully:

```bash
export DEAPI_JWT_SECRET_KEY="<a long random secret>"
docker compose up -d
```

### Running More Than One Replica

The transport is stateless (no MCP session id), so any replica can serve any request. Two
settings must be shared:

- **`DEAPI_JWT_SECRET_KEY`**: the same value on every replica, or tokens issued by one are
  rejected by the others.
- **`DEAPI_REDIS_HOST`** (and the other `DEAPI_REDIS_*` settings): OAuth authorization codes are
  single-use and must be redeemable on whichever replica receives `/token`. Set
  `DEAPI_REDIS_REQUIRED=true` so a replica without Redis refuses to start instead of silently
  keeping codes in memory.

Leave `DEAPI_WEBHOOKS_INTERNAL` off with several replicas: the completion callback can reach a
replica that is not waiting for that job (the job still completes, through polling).

### Graceful Shutdown

On SIGTERM the server stops accepting connections and lets in-flight tool calls finish for up to
`MCP_GRACEFUL_SHUTDOWN_SECONDS` (930 s by default). Give the process manager a longer stop timeout
(`TimeoutStopSec`, `--stop-timeout`, `stop_grace_period`, or the pod's
`terminationGracePeriodSeconds`), or lower the setting to fit it.

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

The server speaks MCP over streamable HTTP at `/mcp`. With the FastMCP client:

```python
import asyncio
from fastmcp import Client

async def main():
    # A deAPI API token, or an access token obtained through the OAuth flow
    async with Client("https://mcp-server-deapi.yourdomain.com/mcp", auth="YOUR_DEAPI_TOKEN") as client:
        tools = await client.list_tools()
        balance = await client.call_tool("get_balance", {})

asyncio.run(main())
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

### 2. Authentication

Every MCP request must carry a deAPI token, either directly (`Authorization: Bearer <deAPI token>`)
or as an access token from the built-in OAuth 2.0 server; unauthenticated calls get `401`. The
server never stores tokens. See [AUTH.md](AUTH.md).

If the server sits behind a proxy, set `FORWARDED_ALLOW_IPS` to that proxy's address so only it
can set `X-Forwarded-*` headers (the default `*` trusts any client).

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
python -m deapi_mcp.server_remote 2>&1 | tee /var/log/mcp-server-deapi.log
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
curl http://your-server-ip:8000/health
# {"status": "healthy", "service": "deapi-mcp"}

# The MCP endpoint takes POST only: a bare GET answers 405 and an
# unauthenticated POST answers 401, both of which mean the server is up.
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://your-server-ip:8000/mcp
```

For a full round trip (tool list plus one free call), use the Python client example above.