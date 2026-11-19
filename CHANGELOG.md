# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/);
releases are git tags `vX.Y.Z`.

## 0.7.0

### Added
- A plain deAPI API key is checked against the API before it is accepted
  (`deapi_mcp/key_verifier.py`). Until now any string that was not a JWT passed as a bearer
  token: `initialize` and `tools/list` succeeded and every tool call then failed with
  `success: false` and "Unauthenticated.", which gave a client no reason to ask the user for
  a new key. Now:
  - `Authorization: Bearer <key>` with a key the API refuses gets `401`
    (`WWW-Authenticate: Bearer error="invalid_token"`), already at `initialize`;
  - `POST /token` answers `401 invalid_client` when the `client_secret` (the key) is refused,
    for both the `authorization_code` and `client_credentials` grants.

  The check is one free `GET /api/v2/account/balance` with the key. The verdict is cached
  under an HMAC of the key (accepted 10 minutes, refused 60 seconds, at most 10,000 keys) and
  concurrent requests with one key share a single check. Only a `401` refuses a key: when the
  API is down, slow (5-second limit) or rate-limiting, the key is let through as before. JWTs,
  refresh tokens and the OAuth flow with a valid key are unchanged.
- `DEAPI_VERIFY_API_KEYS` (default `true`) turns the check off.
- Authentication failures report the new reason `invalid_key` to the auth-rejected sink
  (`observability.set_auth_rejected_sink`).
- The key is also accepted in an `X-API-Key` header, for clients that let a user set one named
  header. It goes through the same checks as `Authorization: Bearer`, which wins when a request
  carries both. A key in the URL is still never read.
- `GET /` serves a setup page: the endpoint, where to get a key, a section per client (Claude,
  ChatGPT, Claude Code, Cursor, VS Code, Codex CLI, Gemini CLI, any other client), a way to check the
  connection and the tool list, generated from the server's own registry. One static document
  with no script and no external resource, under a strict Content-Security-Policy. It follows
  the deapi.ai look (dark, amber accents, the deAPI logo inline as SVG).

- OAuth for clients that register themselves: hosted connectors and apps no longer need a
  Client ID and Secret typed in by hand.
  - `POST /register` (RFC 7591 dynamic client registration) for public clients. The returned
    `client_id` is the registration itself, signed with a key derived from
    `DEAPI_JWT_SECRET_KEY`, so nothing is stored and any replica can verify it.
  - Client ID Metadata Documents: an `https` `client_id` is fetched (public addresses only, no
    redirects, 5 kB, cached 5 minutes) and its `redirect_uris` and name are used.
  - A deAPI sign-in screen at `/authorize` for these clients: it names the application and
    where the browser will return, warns about unverified applications and apps on this
    computer, and asks for an API key, which is checked with deAPI before a code is issued.
    Signed authorization request, CSRF cookie, a limit on wrong keys per client IP (shared
    through Redis), no script, no framing, nothing cached.
  - Public clients use PKCE without a secret; their codes and refresh tokens are bound to the
    `client_id` they were issued to. Redirect URIs must match a registered one exactly
    (loopback `http` on any port, RFC 8252).
  - Authorization responses carry `iss` (RFC 9207), and a `resource` (RFC 8707) that names
    another server is refused with `invalid_target`.
  - `DEAPI_OAUTH_PUBLIC_CLIENTS` (default `true`) turns all of this off.
  - `DEAPI_CLIENT_IP_HEADER` names a header with the real client address from a trusted edge
    (e.g. `CF-Connecting-IP`), for the wrong-key limit.
- A `401` from deAPI on a real call (a key revoked or deleted after sign-in) ends the session:
  the client's next request is refused with `401 invalid_token`, its refresh with
  `invalid_grant`, so it signs in again. The tool result says what happened.

### Changed
- Published on PyPI as `deapi-mcp` (`pip install deapi-mcp`, `uvx deapi-mcp`). A GitHub Release
  (tag `vX.Y.Z`) builds and uploads it with PyPI trusted publishing; the sdist ships only the
  package and its docs.
- The deAPI SDK comes from PyPI (`deapi-sdk>=0.6.1,<0.7`) instead of a GitHub tag tarball.
- A `401 invalid_token` on `/mcp` now says how to fix it: check the deAPI API key, or sign in
  again with OAuth. FastMCP's own text told clients to re-register, which this server does not
  support. Status, error code and `resource_metadata` are unchanged.
- OAuth stays a pre-registered client (`client_id=deapi-mcp`, the API key as `client_secret`),
  now tightened where it fell short of the spec:
  - `POST /token` accepts `client_secret_basic`, which the metadata already advertised but the
    endpoint ignored. A failed Basic authentication gets the `WWW-Authenticate: Basic` challenge.
  - Every `/token` answer carries `Cache-Control: no-store` (RFC 6749 §5.1).
  - A refresh whose key deAPI now refuses (revoked or rotated) gets `invalid_grant`, so the
    client signs in again instead of minting access tokens no tool call can use. An API outage
    still lets the refresh through.
  - `/authorize` accepts Cursor's documented callback `cursor://anysphere.cursor-mcp/oauth/callback`
    as an exact string (RFC 8252 private-use scheme); any other non-http(s) callback is refused
    as before.
  - `/authorize` checks that `code_challenge` is a well-formed S256 value (43 base64url
    characters), not only that one is present.
  - `service_documentation` in the authorization server metadata points to the MCP server docs.

  Nothing is removed: tokens issued by 0.6.x stay valid, and the `deapi-mcp` flow works as
  before (no sign-in screen).

### Fixed
- README: Claude Desktop takes remote servers from Connectors; the `claude_desktop_config.json`
  example with a URL and headers, which Claude Desktop does not read for remote servers, is gone.

## 0.6.3

### Fixed
- The links to the deAPI API key page (README, `AUTH.md` and the `server.json` key description
  shown in the MCP Registry) pointed to a page that does not exist. They now go to
  https://app.deapi.ai/settings/api-keys.

## 0.6.2

### Changed
- Every transcription tool and its price tool (`audio_transcription`, `audio_url_transcription`,
  `video_file_transcription`, `video_url_transcription`) defaults to `WhisperLargeV3Ct2` instead
  of `WhisperLargeV3`. WhisperLargeV3 ignores `lang`; Ct2 costs the same, honours `lang`,
  returns segments and supports `diarize` and word timestamps.
- `lang` now tells the agent to set it whenever the language is known (auto-detection can pick
  the wrong one) and warns that it forces the output language, so a wrong code returns a
  translation instead of a transcript.
- `server.json`: the API key placeholder shows the current format `sk-dp-<id>-<secret>`
  (`<id>|<secret>` keys keep working), and the description fits the registry's 100-character
  limit and names video description and music.
- README lists TikTok among the `video_url_transcription` sources.

## 0.6.1

### Fixed
- A tool call whose wait ran to the end of its budget gave up without reading the job status
  again. With completion webhooks on and a callback that never arrived, the wait on that callback
  could take the whole budget, so a job that had long finished came back as still running
  (`done: false`). The last wait is now always followed by one more status read.

### Changed
- `DEAPI_WEBHOOK_FALLBACK_POLL_INTERVAL` defaults to 15 s (was 45 s, the same as
  `DEAPI_TOOL_MAX_WAIT`). A callback that does not arrive now costs at most about 15 s.

## 0.6.0

### Added
- Pluggable job registry for completion webhooks: `create_app(server, job_registry=...)` takes any
  implementation of `deapi_mcp.webhooks.JobRegistry`, so replicas that share one receiver URL can
  relay callbacks to each other. Its `start`/`stop` run with the app's lifespan while webhooks
  are on. The default stays in process memory.
- Webhook deliveries are deduplicated on `delivery_id`: a replayed delivery is acknowledged and
  wakes nobody.

### Changed
- `JobCompletionRegistry.signal` is a coroutine; the synchronous local wake-up is `wake`.
- The module-level `deapi_mcp.webhooks.registry` is gone: use `get_job_registry()` /
  `set_job_registry()`.
- The receiver answers `400` when `job_request_id` is not a non-empty string. A registry failure
  is logged and the callback acknowledged; the job completes on the fallback poll.

## 0.5.2

### Added
- `deapi_mcp.__version__`. The server reports its version in the MCP `serverInfo`.
- `py.typed`: type checkers now use the package's annotations.
- `CHANGELOG.md` and `SECURITY.md` (how to report a vulnerability privately).

### Changed
- Default Redis databases are now `0` for OAuth codes (`DEAPI_REDIS_DB`) and `1` for a metrics
  overlay (`DEAPI_METRICS_REDIS_DB`). If you relied on the previous defaults, set both explicitly.

### Removed
- The `DEAPI_RETRY_BACKOFF_FACTOR` setting. Nothing read it since retries moved to the deAPI SDK.
  An existing value is ignored.

### Docs
- `.env.example` lists the embedding and default polling settings and the settings an optional
  metrics overlay can use (`DEAPI_METRICS_*`).
- README: polling table covers every job type; `PUBLIC_BASE_URL` and `FORWARDED_ALLOW_IPS` in
  the configuration block.
- DEPLOYMENT.md: set `PUBLIC_BASE_URL` behind a reverse proxy, or OAuth discovery fails.

## 0.5.1

### Changed
- Polling status is sent as MCP progress notifications instead of log notifications.
- A blank `DEAPI_*` value means the default.
- deapi-sdk 0.5.2.

### Fixed
- `/token` answers `503` with `Retry-After` when the code store is unreachable.
- The authorization code store takes the time from Redis, not from the application clock.
- The code index TTL can only grow.
- A corrupt stored code counts as an unknown code (`invalid_grant`) instead of a `500`.
- A call's outcome is labelled by the note that ended it. Tolerated catalogue errors and retried
  status errors no longer change it.

## 0.5.0

### Breaking
- The import package is `deapi_mcp` (was `src`). Run the server with
  `python -m deapi_mcp.server_remote`.
- Python 3.11 is the minimum.
- `video_replace` and `video_remove_background` (and their price tools) are removed. The API no
  longer offers them.

### Added
- HTTP goes through the deAPI SDK over one shared connection pool. A job submission is retried
  only when it provably was not processed, so a job is never submitted twice.
- Importable server: `register_core_tools(server)`, `create_app(server, *, metrics=None)` and
  `build_app()`. A deployment can inject its own metrics overlay.
- Optional Redis store for OAuth authorization codes, so several replicas can serve one login.
- Optional completion webhooks (`DEAPI_WEBHOOKS_INTERNAL`) wake the poller.
- Graceful drain of in-flight tool calls on SIGTERM (`MCP_GRACEFUL_SHUTDOWN_SECONDS`).
- `video_description` and `video_description_price`.
- mypy `--strict` and GitHub Actions CI (ruff, black, mypy, pytest on Python 3.11-3.14).

### Fixed
- `/token` answered `500` when a form field was sent as a multipart file part. It now answers
  `400`/`401`.
- The model catalogue treated "never fetched" as fresh on a host up for less than the cache TTL.

## Before 0.5.0

Untagged releases from February to May 2026: the initial server, deAPI API v2, music,
audio-to-video, video upscale, prompt boosters and MCP registry metadata.
