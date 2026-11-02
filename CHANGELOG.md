# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/);
releases are git tags `vX.Y.Z`.

## 0.5.2

### Added
- `deapi_mcp.__version__`. The server reports its version in the MCP `serverInfo`.
- `py.typed`: type checkers now use the package's annotations.
- `CHANGELOG.md` and `SECURITY.md` (how to report a vulnerability privately).

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
