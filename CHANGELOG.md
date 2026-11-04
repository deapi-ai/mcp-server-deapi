# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/);
releases are git tags `vX.Y.Z`.

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
