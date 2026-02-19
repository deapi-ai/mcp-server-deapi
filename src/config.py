"""Configuration management for deAPI MCP Server."""

from typing import Dict
from pydantic import BaseModel, Field, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class PollingConfig(BaseModel):
    """Configuration for smart adaptive polling based on job type."""

    initial_delay: float = Field(description="Initial polling delay in seconds")
    max_delay: float = Field(description="Maximum polling delay in seconds")
    timeout: float = Field(description="Maximum time to wait for job completion in seconds")
    backoff_factor: float = Field(
        default=1.5, description="Multiplier for exponential backoff"
    )


class Settings(BaseSettings):
    """Global settings for deAPI MCP Server."""

    # deAPI Configuration
    deapi_api_base_url: str = Field(
        default="https://api.deapi.ai",
        description="Base URL for deAPI REST API"
    )
    deapi_api_version: str = Field(
        default="v1",
        description="API version"
    )

    # HTTP Client Configuration
    http_timeout: float = Field(
        default=30.0,
        description="HTTP request timeout in seconds"
    )
    max_retries: int = Field(
        default=3,
        description="Maximum number of retry attempts for failed requests"
    )
    retry_backoff_factor: float = Field(
        default=2.0,
        description="Exponential backoff factor for retries"
    )

    # Polling Configuration by Job Type
    polling_audio: PollingConfig = Field(
        default=PollingConfig(
            initial_delay=1.0,
            max_delay=5.0,
            timeout=300.0  # 5 minutes
        ),
        description="Polling config for audio transcription jobs"
    )
    polling_image: PollingConfig = Field(
        default=PollingConfig(
            initial_delay=2.0,
            max_delay=8.0,
            timeout=300.0  # 5 minutes
        ),
        description="Polling config for image generation jobs"
    )
    polling_video: PollingConfig = Field(
        default=PollingConfig(
            initial_delay=5.0,
            max_delay=30.0,
            timeout=900.0  # 15 minutes
        ),
        description="Polling config for video generation jobs"
    )
    polling_embedding: PollingConfig = Field(
        default=PollingConfig(
            initial_delay=0.5,
            max_delay=3.0,
            timeout=120.0  # 2 minutes
        ),
        description="Polling config for embedding jobs"
    )
    polling_default: PollingConfig = Field(
        default=PollingConfig(
            initial_delay=2.0,
            max_delay=10.0,
            timeout=300.0  # 5 minutes
        ),
        description="Default polling config for other job types"
    )

    # Tool Description Enrichment
    enrich_tool_descriptions: bool = Field(
        default=True,
        description="Enrich tool descriptions with available model info (set to false to disable)"
    )
    model_cache_ttl: float = Field(
        default=300.0,
        description="TTL in seconds for cached model info used in description enrichment"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="DEAPI_",
        extra="ignore"  # Ignore env vars not defined in Settings (e.g., MCP_HOST, DEAPI_JWT_SECRET_KEY)
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
