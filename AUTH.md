# OAuth Authentication Setup for deAPI MCP Server

This document explains how to use OAuth authentication to connect MCP clients (like Claude Desktop) with your deAPI MCP server.

## Overview

The deAPI MCP server supports **OAuth 2.0 Authorization Code** flow (with PKCE) in addition to direct Bearer token authentication. This enables MCP clients that require OAuth (like Claude Desktop's custom connectors) to authenticate and use the deAPI platform.

### How It Works

```
┌─────────────────┐                    ┌──────────────────┐                    ┌─────────────┐
│  Claude Desktop │                    │  deAPI MCP Server│                    │  deAPI API  │
│   (MCP Client)  │                    │  (OAuth Server)  │                    │             │
└────────┬────────┘                    └────────┬─────────┘                    └──────┬──────┘
         │                                      │                                      │
         │ 1. Discover OAuth metadata          │                                      │
         ├─────────────────────────────────────>│                                      │
         │    GET /.well-known/oauth-           │                                      │
         │        authorization-server          │                                      │
         │                                      │                                      │
         │ 2. Returns OAuth endpoints           │                                      │
         │<─────────────────────────────────────┤                                      │
         │                                      │                                      │
         │ 3. Request access token              │                                      │
         ├─────────────────────────────────────>│                                      │
         │    POST /token                       │                                      │
         │    grant_type=client_credentials     │                                      │
         │    client_id=deapi-mcp               │                                      │
         │    client_secret=YOUR_TOKEN           │   (deAPI token)                      │
         │                                      │                                      │
         │ 4. Returns JWT (contains deAPI token)│                                      │
         │<─────────────────────────────────────┤                                      │
         │    { "access_token": "<JWT>", ... }  │                                      │
         │                                      │                                      │
         │ 5. Call MCP tool with JWT            │                                      │
         ├─────────────────────────────────────>│                                      │
         │    Authorization: Bearer <JWT>       │                                      │
         │                                      │                                      │
         │                                      │ 6. Extract deAPI token from JWT      │
         │                                      │    Call deAPI API                    │
         │                                      ├─────────────────────────────────────>│
         │                                      │    Authorization: Bearer <token>     │
         │                                      │                                      │
         │                                      │ 7. deAPI API response                │
         │                                      │<─────────────────────────────────────┤
         │                                      │                                      │
         │ 8. Return tool result                │                                      │
         │<─────────────────────────────────────┤                                      │
         │                                      │                                      │
```

**Key Points:**
- User provides their deAPI token as the OAuth `client_secret` in connector config
- MCP server creates a JWT containing the deAPI token
- MCP client uses JWT for all tool calls
- MCP server extracts deAPI token from JWT and forwards to deAPI API
- **Fully stateless** - no server-side token storage required

## Claude Desktop Configuration

### Step 1: Get Your deAPI Token

Visit [https://api.deapi.ai](https://api.deapi.ai) and obtain your API token.

### Step 2: Configure Custom Connector

In Claude Desktop, add a custom connector with these settings:

```
Name: deAPI

Remote MCP server URL:
  http://your-server-domain:8000/mcp

▼ Advanced settings
OAuth Client ID: deapi-mcp
OAuth Client Secret: YOUR_DEAPI_TOKEN
```

### Step 3: Connect

Click "Add" and Claude Desktop will automatically:
1. Discover OAuth endpoints from your server
2. Exchange your deAPI token for a JWT
3. Use the JWT for all MCP tool calls

## OAuth Endpoints

Your MCP server exposes these OAuth 2.0 endpoints:

### Metadata Endpoint

```
GET /.well-known/oauth-authorization-server
```

Returns OAuth server capabilities:

```json
{
  "issuer": "http://your-server:8000",
  "token_endpoint": "http://your-server:8000/token",
  "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
  "token_endpoint_auth_methods_supported": [
    "client_secret_post",
    "client_secret_basic"
  ],
  "response_types_supported": ["code"],
  "scopes_supported": []
}
```

### Token Endpoint

```
POST /token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=deapi-mcp&client_secret=YOUR_DEAPI_TOKEN
```

Returns JWT access token with refresh token:

```json
{
  "access_token": "eyJhbGc...long_jwt_token...xyz",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "eyJhbGc...refresh_token...abc"
}
```

**JWT Payload:**
```json
{
  "iss": "https://api.deapi.ai",
  "sub": "deapi-mcp",
  "aud": "deapi-mcp-api",
  "exp": 1763025038,
  "iat": 1763021438,
  "deapi_token": "<encrypted>"  ← Embedded deAPI token (Fernet-encrypted)
}
```

## Manual Testing

You can test the OAuth flow manually using curl:

### 1. Test Metadata Endpoint

```bash
curl http://localhost:8000/.well-known/oauth-authorization-server | jq
```

### 2. Get Access Token

```bash
curl -X POST http://localhost:8000/token \
  -d "grant_type=client_credentials" \
  -d "client_id=deapi-mcp" \
  -d "client_secret=YOUR_DEAPI_TOKEN" | jq

# Save the access_token from response
export JWT_TOKEN="eyJhbGc...xyz"
```

### 3. Use Token with MCP Endpoint

```bash
curl http://localhost:8000/mcp \
  -H "Authorization: Bearer $JWT_TOKEN"
```

## Configuration

### JWT Signing Key (Production)

By default, the server generates an ephemeral JWT signing key (tokens invalid after restart).

**For production**, set a persistent signing key:

```bash
export DEAPI_JWT_SECRET_KEY="your-secret-key-at-least-32-characters-long"
```

Generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Server Configuration

Start the server with OAuth enabled (automatically enabled):

```bash
python -m src.server_remote
```

Environment variables:
- `MCP_HOST` - Server host (default: `0.0.0.0`)
- `MCP_PORT` - Server port (default: `8000`)
- `DEAPI_JWT_SECRET_KEY` - JWT signing key (optional, recommended for production)

## Backward Compatibility

The OAuth implementation is fully backward compatible. You can use:

1. **OAuth flow** (recommended for Claude Desktop):
   - Configure OAuth Client ID and Secret in connector
   - Server issues JWT tokens

2. **Direct Bearer token** (backward compatible):
   - Use `Authorization: Bearer YOUR_DEAPI_TOKEN` header directly
   - No OAuth flow needed

Both methods work simultaneously!

## Security Considerations

### Token Lifetime

- **Access tokens expire after 1 hour** (3600 seconds)
- **Refresh tokens expire after 30 days** (2592000 seconds)
- MCP clients automatically use refresh tokens to obtain new access tokens
- When refreshing, a new refresh token is also issued (token rotation)
- Users only need to re-authenticate when the refresh token expires
- deAPI tokens have their own expiration (set by deAPI)

**Refresh Token Flow:**
```
POST /token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&client_id=deapi-mcp&refresh_token=eyJhbGc...
```

### JWT Signing Key

- **Development**: Ephemeral key generated at startup (tokens invalid after restart)
- **Production**: Set `DEAPI_JWT_SECRET_KEY` environment variable
- Key should be at least 32 characters and cryptographically random

### Stateless Design

- No server-side token storage
- JWT contains the deAPI token (Fernet-encrypted within the JWT payload)
- Server verifies JWT signature on each request
- deAPI API validates the embedded token

### Client ID Validation

- Only `client_id=deapi-mcp` is accepted
- This prevents unauthorized OAuth clients
- Can be customized in `src/oauth_endpoints.py`

## Troubleshooting

### "Invalid or expired JWT token"

- JWT has expired (1 hour lifetime)
- JWT signing key changed (server restarted without persistent key)
- Solution: Request a new token from `/token` endpoint

### "Invalid client_id"

- Using wrong client_id (must be `deapi-mcp`)
- Check Claude Desktop connector configuration

### "client_secret appears invalid"

- Token too short (< 10 characters)
- Check that you're using your actual deAPI API token

### Server logs don't show OAuth endpoints

- Check server startup logs for:
  ```
  OAuth endpoints enabled:
    - Metadata: http://0.0.0.0:8000/.well-known/oauth-authorization-server
    - Token: http://0.0.0.0:8000/token
  ```

## Support

For issues or questions:
- GitHub Issues: [https://github.com/deapi-ai/mcp-server-deapi/issues](https://github.com/deapi-ai/mcp-server-deapi/issues)
- deAPI Documentation: [https://docs.deapi.ai/](https://docs.deapi.ai/)