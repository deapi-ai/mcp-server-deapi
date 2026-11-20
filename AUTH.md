# OAuth Authentication Setup for deAPI MCP Server

This document explains how to use OAuth authentication to connect MCP clients (like Claude Desktop) with your deAPI MCP server.

## Overview

The deAPI MCP server supports **OAuth 2.0 Authorization Code** flow (with PKCE) and the **Client Credentials** grant, in addition to direct Bearer token authentication. This enables MCP clients that require OAuth (like Claude Desktop's custom connectors) to authenticate and use the deAPI platform.

The diagram below shows the Client Credentials grant, the simplest to follow. Connectors that use the Authorization Code flow first call `GET /authorize` (with a PKCE `code_challenge`) and then exchange the returned code at `POST /token` with `grant_type=authorization_code`, `code_verifier` and the deAPI token as `client_secret`; the result is the same JWT.

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

Visit [app.deapi.ai/settings/api-keys](https://app.deapi.ai/settings/api-keys) and create an API token.

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
  "authorization_endpoint": "http://your-server:8000/authorize",
  "token_endpoint": "http://your-server:8000/token",
  "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
  "response_types_supported": ["code"],
  "token_endpoint_auth_methods_supported": [
    "client_secret_post",
    "client_secret_basic"
  ],
  "code_challenge_methods_supported": ["S256"],
  "scopes_supported": [],
  "service_documentation": "https://api.deapi.ai"
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
  "deapi_token_enc": "<encrypted>"  ← Embedded deAPI token (Fernet-encrypted)
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

The endpoint takes MCP JSON-RPC over POST:

```bash
curl http://localhost:8000/mcp \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

## Configuration

### JWT Signing Key (Production)

Without `DEAPI_JWT_SECRET_KEY` the server generates an ephemeral signing key (tokens invalid after
restart). It allows that only on loopback (`MCP_HOST=127.0.0.1`) without Redis; bound to any other
address, or with Redis configured, it refuses to start, because replicas would each sign with a
different key.

**For production**, set a persistent signing key, the same on every replica:

```bash
export DEAPI_JWT_SECRET_KEY="your-secret-key-at-least-32-characters-long"
```

Generate a secure key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Server Configuration

Start the server (OAuth is always enabled):

```bash
python -m deapi_mcp.server_remote
```

Environment variables:
- `MCP_HOST` - Server host (default: `0.0.0.0`)
- `MCP_PORT` - Server port (default: `8000`)
- `DEAPI_JWT_SECRET_KEY` - JWT signing key (required unless bound to loopback)
- `DEAPI_REDIS_HOST` (+ `DEAPI_REDIS_*`) - shared store for authorization codes; needed when more
  than one replica serves `/authorize` and `/token`. Codes are single-use and expire after 10 minutes

## Backward Compatibility

The OAuth implementation is fully backward compatible. You can use:

1. **OAuth flow** (recommended for Claude Desktop):
   - Configure OAuth Client ID and Secret in connector
   - Server issues JWT tokens

2. **Direct Bearer token** (backward compatible):
   - Use `Authorization: Bearer YOUR_DEAPI_TOKEN` header directly, or `X-API-Key: YOUR_DEAPI_TOKEN`
     for a client that only lets you name one header (`Authorization` wins when both are sent)
   - No OAuth flow needed
   - Never put the key in the URL: the server reads it only from these headers
   - The key is checked once against deAPI (see [API key check](#api-key-check)); a key deAPI
     refuses gets `401` at `initialize`

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

### API Key Check

A plain deAPI API key has no signature the server could check, so it is checked with deAPI
instead: one free `GET /api/v2/account/balance` with the key, wherever a key first reaches
the server:

- `Authorization: Bearer <deAPI key>` on an MCP request: a refused key gets `401` with
  `WWW-Authenticate: Bearer error="invalid_token"`, so the client asks for a new key at once
  instead of every tool call failing later with an API error;
- `client_secret` on `POST /token` (`authorization_code` and `client_credentials` grants): a
  refused key gets `401 invalid_client`;
- `grant_type=refresh_token`: the key the grant was issued for is checked again, and a key deAPI
  now refuses (revoked or rotated) gets `400 invalid_grant`, so the client signs in again.

An access token is trusted by its signature between refreshes, so a revoked key stops working
at the latest when its access token expires (1 hour).

The verdict is cached per key, under an HMAC of the key with a per-process random secret (the
key itself is never stored or logged): an accepted key for 10 minutes, a refused one for 60
seconds. Concurrent requests with the same key share one check.

Only an explicit `401` from deAPI refuses a key. If deAPI is down, slow (5-second limit) or
rate-limiting, the key is let through, as if the check did not exist, and that outcome is
cached for 15 seconds. A key that is not printable ASCII is refused without asking. At most
32 checks run at once (beyond that a key is let through unchecked), plus 8 kept for the
sign-in screen, so a flood of keys elsewhere never stops a person from signing in. Only
requests to `/mcp` are checked: a bearer token sent to another route (a metrics scraper's,
say) never reaches deAPI. Set `DEAPI_VERIFY_API_KEYS=false` to turn the check off.

### Public Clients and the Sign-in Screen

Clients that cannot be given a secret by hand (hosted connectors, apps that register
themselves) are public clients. They get a client ID in one of two ways, both advertised in
`/.well-known/oauth-authorization-server`:

- **Dynamic client registration** (`POST /register`, RFC 7591). Every client is registered
  as public (`token_endpoint_auth_method: none`, no secret): one that asks for
  `client_secret_post`, `client_secret_basic` or another method gets `none` back, as RFC 7591
  allows. It must list the `authorization_code` grant and the `code` response type; other
  grants and response types are ignored. 1-16 redirect URIs: `https` on a plain DNS name
  (ASCII; an internationalized name in its `xn--` form) without user info, `http` on loopback
  only, or a native app's private-use scheme (reverse-domain form like `com.example.app:`, or
  a known client's own such as `cursor:` and `raycast:`; OS protocol handlers are refused). The returned `client_id` is the
  registration itself, signed with a key derived from `DEAPI_JWT_SECRET_KEY`: nothing is
  stored, and any replica can verify it. Bodies are limited to 8 kB.
- **Client ID metadata documents.** An `https` `client_id` with a path is fetched as JSON
  (public addresses only, no redirects, uncompressed, 5 kB, 5 seconds in total, at most 16
  at once and 4 for one site) and must name itself as `client_id`. Its host must be a plain
  DNS name, like a redirect's. It must allow `none` as its token endpoint auth method, as its
  own method or in `token_endpoint_auth_methods_supported` (ChatGPT prefers
  `private_key_jwt` and lists `none` too). Tested with the documents of claude.ai, Claude
  Code, ChatGPT, Codex, VS Code, GitHub Copilot CLI, Zed and Goose. A document is reused for 5 minutes; a failed fetch is
  not retried for 1 minute.

For a public client, `GET /authorize` shows a deAPI sign-in screen instead of redirecting at
once. It names the application, says where the access goes (the redirect host), and warns
about unverified (self-registered) applications, apps on this computer (loopback), apps on
the device (native schemes) and metadata documents whose redirect is on another site. Names
lose control and invisible characters, and a name that poses as deAPI is replaced (a
trailing parenthesis naming the server, as in `Claude Code (deapi)`, is allowed). The user
pastes an API key; `POST /authorize` checks it with deAPI and only then issues the code (the
key is kept Fernet-encrypted in the 10-minute code record).

Protections on the screen: the authorization request travels from the page to the form
inside an HMAC-signed, 10-minute token; a double-submit CSRF cookie (`__Host-`, `Secure`,
`SameSite=Strict` over https); at most 10 wrong keys per client address in 15 minutes
(an IPv6 address counts as its /64; each attempt is counted before the key is checked and
given back when the key is good, so concurrent attempts cannot get past the limit; counted
in Redis when configured, so shared by every replica); no script, no framing,
`Cache-Control: no-store`, and a CSP that loads nothing. The CSP has no `form-action`:
Chromium applies it to every redirect after the form's POST, and a client's callback often
redirects again (`vscode.dev/redirect` to `vscode://` or loopback, a web callback to a
desktop deep link), which it would block. Set `DEAPI_CLIENT_IP_HEADER` to a header your edge sets and
clients cannot forge (for example `CF-Connecting-IP` behind Cloudflare); otherwise the
connection address is used, which a proxy that trusts `X-Forwarded-For` blindly lets the
client choose.

A loopback redirect matches on any port, an empty path is taken as `/`, and the registered
query may be left out (Continue registers `http://localhost:3000/?state=...` and then sends
`http://localhost:3000`); a query that is sent must be the registered one, and no redirect
URI may contain `#`. Every other redirect matches character for character. A client's
redirect URIs total at most 2,048 characters.

At `/token` a public client sends its `client_id` in the form or in `Authorization: Basic`
(an `https` client ID sent there without URL-encoding is read correctly too); a client
that sends only a `private_key_jwt` assertion is identified by the assertion's `sub`,
which is not verified, since PKCE and the redirect bind the code. Such a client (ChatGPT)
is treated as public, like every other client here: its refresh token works without the
client's private key, so it must be kept as secret as any other public client's. The discovery documents,
`/register` and `/token` answer CORS requests from any origin (no credentials), for
clients that run in a browser; `/authorize` and `/mcp` do not.

Codes from the sign-in screen are kept apart from the pre-registered client's (each with
its own cap of 10,000), so a flood of anonymous `GET /authorize` requests never pushes out
a code a person has just signed in for.

At `/token`, a public client sends its `client_id` and the PKCE verifier, no secret.
Authorization codes and refresh tokens are bound to the `client_id` they were issued to.
Authorization responses carry `iss` (RFC 9207); a `resource` (RFC 8707) other than this
server's `/mcp` is refused with `invalid_target`. `DEAPI_OAUTH_PUBLIC_CLIENTS=false` turns
all of this off and leaves only `deapi-mcp`.

A key deAPI refuses on a real call (revoked or deleted after sign-in) ends the session: the
next request is refused with `401 invalid_token` and the refresh with `invalid_grant`, so
the client opens the sign-in screen again.

### JWT Signing Key

- **Development** (loopback only): Ephemeral key generated at startup (tokens invalid after restart)
- **Production**: Set `DEAPI_JWT_SECRET_KEY`; the server refuses to start without it off loopback
- Key should be at least 32 characters and cryptographically random

### Stateless Design

- No server-side token storage (authorization codes are kept for at most 10 minutes, then burnt on use)
- JWT contains the deAPI token (Fernet-encrypted within the JWT payload)
- Server verifies JWT signature on each request
- A plain key is checked with deAPI once and the verdict cached (see above)
- deAPI API validates the embedded token on every call

### Client ID Validation

- `client_id=deapi-mcp` is the pre-registered client, with the API key as its secret; it gets
  no sign-in screen
- Any other `client_id` must be a dynamically registered client or a client ID metadata
  document URL (see [Public Clients](#public-clients-and-the-sign-in-screen)); anything else is
  refused with `unauthorized_client` and never redirected
- `/token` takes the client credentials in the form (`client_secret_post`) or in an
  `Authorization: Basic` header (`client_secret_basic`); token responses are never cached
  (`Cache-Control: no-store`)
- Can be customized in `deapi_mcp/oauth_endpoints.py`

### Redirect URIs and PKCE

- `redirect_uri` must use `http` or `https`, except Cursor's documented callback
  `cursor://anysphere.cursor-mcp/oauth/callback`, accepted as that exact string. `javascript:`,
  `data:` and every other scheme are refused without a redirect
- `/token` requires the same `redirect_uri` as `/authorize`, character for character
- PKCE is mandatory and S256 only: `code_challenge_method=S256` and a 43-character base64url
  `code_challenge`; `plain` is refused

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

### "client_secret was rejected by deAPI (invalid API key)"

- deAPI does not know the key, or it was revoked
- Create or copy a key at https://app.deapi.ai/settings/api-keys and connect again

### `401 invalid_token` with a plain Bearer key

- deAPI refused the key at connect (see [API key check](#api-key-check))
- A key fixed or re-enabled on the deAPI side is accepted again within 60 seconds

### Server logs don't show OAuth endpoints

- Check server startup logs for:
  ```
  MCP endpoint: http://0.0.0.0:8000/mcp
  OAuth server: http://0.0.0.0:8000/.well-known/oauth-authorization-server
  Authentication: OAuth 2.0 (Authorization Code + PKCE) and Bearer tokens
  ```

## Support

For issues or questions:
- GitHub Issues: [https://github.com/deapi-ai/mcp-server-deapi/issues](https://github.com/deapi-ai/mcp-server-deapi/issues)
- deAPI Documentation: [https://docs.deapi.ai/](https://docs.deapi.ai/)