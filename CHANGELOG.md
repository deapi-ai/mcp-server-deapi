# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/);
releases are git tags `vX.Y.Z`.

## 0.8.1

### Changed
- Tool descriptions name current models: `LightOnOcr_3_4B_Q8` for OCR (instead of the
  retired `Nanonets_Ocr_S_F16`), `Ltx2_5_22B_Dist_INT8` for video and
  `AceStep_1_5_XL_Turbo_INT8` for music. The catalogue snapshot used by
  `test_description_slugs` is refreshed (2026-11-24).
- The remote URL in `server.json` is `https://mcp.deapi.ai/mcp?source=mcp-registry`, so
  sessions started from MCP Registry imports can be counted.
- `server.json` declares the key as an `Authorization` header instead of the OAuth
  `oauth_client_id` / `oauth_client_secret` URL variables. Clients that import the MCP Registry
  (goose among them) skip remote servers with `variables`; a secret header is what they read.
  OAuth sign-in is unchanged: clients still discover it from the server.

## 0.8.0

### Added
- Account and API key tools (`deapi_mcp/tools/account.py`): `get_account`,
  `get_account_usage`, `list_payments`, `list_api_keys`, `get_api_key` and
  `get_api_key_usage` read, marked read-only. They need an ADMIN key, except a key's own
  details and usage; an API-role key gets a message with the dashboard link.
- `create_api_key`, `update_api_key` and `delete_api_key`, behind
  `DEAPI_KEY_MANAGEMENT_TOOLS` (default `false`). Off, they change nothing and answer with
  the dashboard link (marked read-only, so clients do not ask first). On, they act and are
  marked destructive, so clients ask the user before each call. A created key's secret is
  returned once, in the tool result.
- The sign-in screen shows the application's picture for well-known clients (Claude,
  ChatGPT, VS Code, GitHub Copilot, Zed, Raycast, Goose, Cursor) identified by their metadata
  document: its `logo_uri` when that is on the client's own site (or a logo host known for
  it, such as ChatGPT's), else the favicon of its host. The server fetches it (public
  addresses only, no redirects, 160 kB, 1.5 s), keeps only PNG, ICO, GIF, JPEG or WebP
  (never SVG), and puts it into the page as a `data:` URI, so the browser loads nothing
  from elsewhere. Cached a day. Every other client, and every self-registered one, keeps a
  letter: whoever runs a host decides what its favicon shows, so a picture is shown only
  where it cannot be someone else's.
- deAPI's icon: served at `/favicon.ico` and `/icon-{32,64,128}.png`, linked from the setup
  page and the sign-in screen, and announced in the MCP `serverInfo` (`icons`, `websiteUrl`)
  for clients that show a connector's picture.

### Changed
- The links on the sign-in screen (API keys page, docs, logo) open in a new tab, so
  getting a key never leaves the sign-in under way.
- The setup page gives each client its choices side by side: the deAPI sign-in window
  (recommended: the key never goes into a config file) and the key in a header, with the
  commands for each (`claude mcp add --scope user`, `codex mcp login`, `gemini mcp add`,
  `/mcp auth`, VS Code's MCP: Add Server). It also says that key management is off on this
  server, and why.

## 0.7.3

Connecting from more clients. Each popular MCP client's own registration or metadata
document, and the requests it sends, was run through the whole sign-in flow; these are the
fixes that run turned up.

### Fixed
- ChatGPT can connect with the sign-in window. Its client metadata document prefers
  `private_key_jwt` and lists `none` among its supported methods; a document is now
  accepted when it allows `none` either way. A token request that carries only a
  `private_key_jwt` assertion and no `client_id` is matched to the client named in the
  assertion (not verified: PKCE and the redirect bind the code).
- Dynamic registration accepts any requested token endpoint auth method and registers the
  client as public (`none`), which the response says (RFC 7591 §3.2.1). LibreChat and Open
  WebUI ask for `client_secret_post` and were refused.
- Up to 16 redirect URIs per client (was 5), 2,048 characters in all: the Gemini app
  registers six, Raycast lists eight. `raycast:` is a known native scheme.
- VS Code in remote or web mode (and any client whose callback redirects again, such as a
  desktop deep link) could not finish the sign-in in Chromium browsers: the sign-in page's
  CSP `form-action` was applied to the second redirect too. The page no longer sets
  `form-action`; it still loads nothing and runs no script.
- A loopback redirect may leave out its registered query and takes an empty path as `/`
  (Continue registers `http://localhost:3000/?state=...` and then sends
  `http://localhost:3000`). A query that is sent must be the registered one, and a `#`
  anywhere in a redirect URI is refused, so no `code` of someone else's can be slipped in.
- CORS on the discovery documents, `/register` and `/token` (any origin, no credentials),
  for clients that run in a browser such as MCP Inspector.
- A client name may end with exactly `(deapi)` or `(deapi mcp)` after its own name, so
  `Claude Code (deapi)` keeps its name instead of becoming "Unnamed application". Any other
  mention of deAPI still replaces the name.
- `Authorization: Basic` with an `https` client ID that was not URL-encoded is read
  correctly; an empty `Authorization` header no longer hides an `X-API-Key`.

- Without `PUBLIC_BASE_URL`, the setup page uses the request's Host only when it is a plain
  host and port; otherwise it shows a placeholder, so no `;` or `$(...)` can end up in the
  commands people copy from it. Set `PUBLIC_BASE_URL` in production.

### Added
- The setup page has copy buttons for the endpoint, the client ID, the headers and every
  snippet. One small inline script, allowed by its hash in the CSP, shows them; without
  JavaScript they stay hidden and each value is still selected with one click.

## 0.7.2

### Fixed
- Claude (claude.ai and Claude Desktop) can connect with the sign-in window. Its client
  metadata document, and its dynamic registration, list a JWT-bearer grant besides
  `authorization_code` and `refresh_token`, and the server refused any client that listed a
  grant it does not offer, so both ways ended on "This application is not registered". A
  client must now list `authorization_code` (and the `code` response type); other grants and
  response types are ignored, and only the code flow and refresh are ever honoured.

## 0.7.1

Security hardening after a review of 0.7.0. No known exploit; nothing changes for a client
that worked before, except that media uploads now stop at deAPI's own limits.

### Security
- A client ID metadata document (or a media URL) served compressed could make the server
  inflate far more than the size cap, since the body was decompressed a network chunk at a
  time before the cap was checked. Fetched bodies are now read raw: `Accept-Encoding:
  identity` is sent and a compressed answer is refused, never inflated.
- A media fetch has an overall deadline (four times the per-read timeout), so a server that
  trickles bytes cannot hold it open; proxy settings from the environment are ignored, so
  the connection always goes to the address that was checked; IPv6 site-local addresses and
  IPv4 addresses wrapped in 6to4 or Teredo are judged like the address inside.
- Media uploads stop at deAPI's limits: an image at 10 MB, audio at 20 MB, video at 50 MB,
  and one tool call's files together at `DEAPI_MAX_UPLOAD_MB` (new, default 75, deAPI's own
  request cap; it also caps each file). List parameters (`images`, `ref_images`,
  `ref_videos`, `ref_audios`) take at most 16 entries. Before, one call could make the
  server hold several hundred megabytes.
- The authorization codes of the sign-in screen are kept apart from those of the
  pre-registered client, each with a cap of 10,000 (was one shared cap of 1,000). Anyone can
  have a pre-registered code issued, so a flood of them could push out a code a person had
  just signed in for.
- A redirect URI's host must be a plain DNS name (ASCII, an IDN in `xn--` form) or an IP
  literal, without user info; the same holds for a metadata document's host. A `;` in the
  host could add a directive to the sign-in page's CSP header, and a host outside Latin-1
  failed with a 500. Look-alike hosts in other scripts cannot appear on the sign-in screen.
  An IPv6 zone (`%...`) is refused too. A dynamically registered `client_id` issued by 0.7.0
  with a redirect URI these rules now refuse stops working; that client registers again.
- Wrong-key limit on the sign-in screen: an attempt is counted before the key is checked and
  given back when the key is good, so concurrent attempts no longer get past the limit; an
  IPv6 address counts as its /64. The sign-in screen has key-check slots of its own, so a
  flood of keys on `/token` or `/mcp` cannot make it answer "could not be checked".
- Metadata documents: one fetch per `client_id` at a time (everyone signing in with that
  client meanwhile shares it), and at most 4 at once for one site (waiting up to 2 seconds
  for a slot), so one party's slow hosts cannot take every fetch slot.
- Only requests to `/mcp` have their bearer token checked with deAPI. Authentication runs on
  every route, so a token sent elsewhere (a metrics scraper's, for example) used to be sent
  to deAPI as if it were an API key.
- The shared HTTP client keeps no cookies. It serves every user's requests, so a cookie set
  on one user's response would have been sent with the next user's.
- A key that is not printable ASCII is refused without a check (it was let through with a
  token, because the check itself failed).
- A sign-in code is refused at `/token` when deAPI has refused its key since it was issued.
- `check_job_status` sends the job id as one path segment (`../`, `?` and `#` are escaped).
- A status read that fails (a 404 for another account's job id, an API error) no longer stops
  the internal webhook from waking the call that is waiting on that job.
- Two log lines quote the client's `response_type` and `code_challenge_method` with `repr`,
  so they cannot forge a log line. A failure to create tokens no longer returns its error
  message to the client, and an unexpected error while verifying a token logs only its type.
- `DEAPI_WEBHOOK_SECRET` is a `SecretStr`: it no longer appears in a `repr` of the settings.
  Read it with `config.secret_value()`.
- A warning at startup when `DEAPI_JWT_SECRET_KEY` is shorter than 32 characters or an
  example value.

### Upgrade notes
- Sign-in codes move to their own Redis keys (`<prefix>signin_oauth_code:*`). During a
  rolling upgrade from 0.7.0, a sign-in finished on one version and redeemed on the other
  fails with `invalid_grant`; the client signs in again. Codes live 10 minutes.
- A media URL whose server sends the body compressed (for example an object stored with
  `Content-Encoding: gzip`) is refused instead of inflated.
- Redirect hosts with an underscore are refused: a CSP host source cannot name them, so the
  sign-in form could never have redirected there anyway.

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
