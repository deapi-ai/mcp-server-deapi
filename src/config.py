"""Configuration management for deAPI MCP Server."""

from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollingConfig(BaseModel):
    """Configuration for smart adaptive polling based on job type."""

    initial_delay: float = Field(description="Initial polling delay in seconds")
    max_delay: float = Field(description="Maximum polling delay in seconds")
    timeout: float = Field(description="Maximum time to wait for job completion in seconds")
    backoff_factor: float = Field(default=1.5, description="Multiplier for exponential backoff")


class Settings(BaseSettings):
    """Global settings for deAPI MCP Server."""

    # deAPI Configuration
    #
    # These two fields already carry the `deapi_` prefix in their name, so the
    # `DEAPI_` env_prefix below would resolve them to DEAPI_DEAPI_API_BASE_URL /
    # DEAPI_DEAPI_API_VERSION. Every doc (.env.example, CLAUDE.md, deployment
    # manifests) advertises the single-prefix names, which were silently ignored:
    # the server kept talking to the production default no matter what was set.
    # The explicit aliases make the documented names authoritative and keep the
    # accidental double-prefix form working for anything already using it.
    deapi_api_base_url: str = Field(
        default="https://api.deapi.ai",
        validation_alias=AliasChoices("DEAPI_API_BASE_URL", "DEAPI_DEAPI_API_BASE_URL"),
        description="Base URL for deAPI REST API",
    )
    deapi_api_version: str = Field(
        default="v2",
        validation_alias=AliasChoices("DEAPI_API_VERSION", "DEAPI_DEAPI_API_VERSION"),
        description="API version",
    )

    # HTTP Client Configuration
    http_timeout: float = Field(default=30.0, description="HTTP request timeout in seconds")
    max_retries: int = Field(
        default=3, description="Maximum number of retry attempts for failed requests"
    )
    retry_backoff_factor: float = Field(
        default=2.0, description="Exponential backoff factor for retries"
    )

    # Polling Configuration by Job Type
    polling_audio: PollingConfig = Field(
        default=PollingConfig(initial_delay=1.0, max_delay=5.0, timeout=300.0),  # 5 minutes
        description="Polling config for audio transcription jobs",
    )
    polling_image: PollingConfig = Field(
        default=PollingConfig(initial_delay=2.0, max_delay=8.0, timeout=300.0),  # 5 minutes
        description="Polling config for image generation jobs",
    )
    polling_video: PollingConfig = Field(
        default=PollingConfig(initial_delay=5.0, max_delay=30.0, timeout=900.0),  # 15 minutes
        description="Polling config for video generation jobs",
    )
    polling_embedding: PollingConfig = Field(
        default=PollingConfig(initial_delay=0.5, max_delay=3.0, timeout=120.0),  # 2 minutes
        description="Polling config for embedding jobs",
    )
    polling_default: PollingConfig = Field(
        default=PollingConfig(initial_delay=2.0, max_delay=10.0, timeout=300.0),  # 5 minutes
        description="Default polling config for other job types",
    )

    # Tool Description Enrichment
    enrich_tool_descriptions: bool = Field(
        default=True,
        description="Enrich tool descriptions with available model info (set to false to disable)",
    )
    model_cache_ttl: float = Field(
        default=300.0,
        description="TTL in seconds for cached model info used in description enrichment",
    )

    # Internal webhooks: the API calls this server back when a job finishes, so
    # a tool waits on that callback instead of polling the status endpoint every
    # few seconds. Polling stays as a safety net at a much lower rate.
    #
    # Field names deliberately carry no `deapi_` prefix — see the note above on
    # deapi_api_base_url for why. Env vars: DEAPI_WEBHOOKS_INTERNAL,
    # DEAPI_WEBHOOK_RECEIVER_URL, DEAPI_WEBHOOK_SECRET, DEAPI_WEBHOOK_FALLBACK_POLL_INTERVAL.
    webhooks_internal: bool = Field(
        default=False,
        description="Register this server as the completion webhook for jobs it submits",
    )
    webhook_receiver_url: str | None = Field(
        default=None,
        description="Public HTTPS URL of this server's /webhooks/deapi route, as the API must reach it",
    )
    webhook_secret: str | None = Field(
        default=None,
        description="Key the API signs callbacks with (HMAC-SHA256). 32-255 characters",
    )
    webhook_fallback_poll_interval: float = Field(
        default=45.0,
        description="Seconds between safety-net status polls while waiting on a callback",
    )
    webhook_timestamp_tolerance: float = Field(
        default=300.0,
        description="Reject callbacks whose X-DeAPI-Timestamp is older than this many seconds",
    )

    @model_validator(mode="after")
    def _validate_internal_webhooks(self) -> "Settings":
        """A half-configured receiver must fail at startup, not silently poll.

        With the flag on and no URL the server would submit jobs without a
        webhook and simply poll at the slow fallback rate — every job slower,
        nothing logged. Refusing to start is the only outcome the operator
        will notice.
        """
        if not self.webhooks_internal:
            return self

        if not self.webhook_receiver_url:
            raise ValueError("DEAPI_WEBHOOKS_INTERNAL is set but DEAPI_WEBHOOK_RECEIVER_URL is not")
        if not self.webhook_receiver_url.lower().startswith("https://"):
            raise ValueError("DEAPI_WEBHOOK_RECEIVER_URL must use https (the API requires it)")
        if not self.webhook_secret:
            raise ValueError("DEAPI_WEBHOOKS_INTERNAL is set but DEAPI_WEBHOOK_SECRET is not")
        if not 32 <= len(self.webhook_secret) <= 255:
            raise ValueError("DEAPI_WEBHOOK_SECRET must be 32-255 characters (API rule)")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DEAPI_",
        extra="ignore",  # Ignore env vars not defined in Settings (e.g., MCP_HOST, DEAPI_JWT_SECRET_KEY)
    )

    def get_polling_config(self, job_type: str) -> PollingConfig:
        """Get appropriate polling configuration based on job type.

        Args:
            job_type: Type of job (audio, image, video, or other)

        Returns:
            PollingConfig for the specified job type
        """
        job_type_lower = job_type.lower()

        if "audio" in job_type_lower or "speech" in job_type_lower:
            return self.polling_audio
        elif "image" in job_type_lower or "img" in job_type_lower:
            return self.polling_image
        elif "video" in job_type_lower:
            return self.polling_video
        elif "embedding" in job_type_lower:
            return self.polling_embedding
        else:
            return self.polling_default


# Global settings instance
settings = Settings()
