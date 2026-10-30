"""Pydantic schemas for deAPI requests and responses."""

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field


# Enums
class JobStatus(str, Enum):
    """Job status enumeration.

    The API's terminal failure status is `error` (the upstream enum's
    terminal value). `failed` is kept only so that an older or alternate payload
    still parses instead of raising; treat both as terminal failures.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"
    FAILED = "failed"

    @property
    def is_failure(self) -> bool:
        """Whether this status represents a terminal failure."""
        return self in (JobStatus.ERROR, JobStatus.FAILED)


class ImageFormat(str, Enum):
    """Image format enumeration."""

    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"


class OCRFormat(str, Enum):
    """OCR output format enumeration."""

    TEXT = "text"
    JSON = "json"


# Response Schemas
class JobRequestData(BaseModel):
    """Job request response data."""

    request_id: str = Field(description="Unique identifier for the job request (UUID)")


class JobRequestResponse(BaseModel):
    """Response from job submission endpoints."""

    data: JobRequestData


class PromptBoost(BaseModel):
    """What the inline prompt booster (``enhance_prompt``) did to the prompt.

    Present only once the boost has actually run; null while it is queued,
    when it was not requested, or when the booster left the prompt unchanged.
    """

    prompt: Optional[str] = Field(
        None, description="Effective (boosted) prompt used for generation"
    )
    prompt_original: Optional[str] = Field(None, description="Prompt as supplied by the caller")
    negative_prompt: Optional[str] = Field(None, description="Effective (boosted) negative prompt")
    negative_prompt_original: Optional[str] = Field(
        None, description="Negative prompt as supplied by the caller"
    )


class JobStatusData(BaseModel):
    """Job status response data."""

    status: JobStatus = Field(description="Current status of the job request")
    preview: Optional[str] = Field(
        None,
        description=(
            "Inline preview of an in-progress image job as base64 PNG, not a URL. "
            "Parsed but never relayed by the tools (check_job_status reports "
            "preview_url as null): ~20 KB of pixels in every status check would "
            "only burn the caller's context"
        ),
    )
    result_url: Optional[str] = Field(
        None,
        description="URL to the result file (image/audio/video). Available when status is done",
    )
    result: Optional[str] = Field(
        None, description="Generated text (e.g. transcription). Available when status is done"
    )
    progress: Optional[float] = Field(
        None, description="Current progress of the job (0.0 to 100.0)", ge=0.0, le=100.0
    )
    metadata: Optional[Dict[str, Any]] = Field(None, description="Source metadata for the job")

    # Failure surface. All null unless the job actually failed, so their presence
    # is what distinguishes a real failure from a job still on its way.
    error_code: Optional[str] = Field(None, description="Machine-readable failure code")
    error_reason: Optional[str] = Field(None, description="Human-readable reason the job failed")
    retryable: Optional[bool] = Field(
        None, description="Whether resubmitting the same job could succeed"
    )
    refunded: Optional[bool] = Field(None, description="Whether the job's cost was refunded")
    failed_at: Optional[str] = Field(None, description="When the job failed (ISO 8601)")

    price: Optional[Dict[str, Any]] = Field(
        None, description="Actual cost: {'amount': float, 'is_estimated': bool}"
    )
    results_alt_formats: Optional[Dict[str, Any]] = Field(
        None, description="Alternative result formats (e.g. jpg, webp) for image jobs"
    )
    prompt_boosted: Optional[bool] = Field(None, description="Whether the prompt was enhanced")
    prompt_boost: Optional[PromptBoost] = Field(
        None, description="Boosted and original prompts. Non-null only when prompt_boosted is true"
    )


class JobStatusResponse(BaseModel):
    """Response from job status endpoint."""

    data: JobStatusData


class PriceCalculationData(BaseModel):
    """Price calculation response data.

    Mirrors what the price endpoints actually return. The previous shape
    (`estimated_price`, `currency`) matched no endpoint; the price tools return
    the raw dict rather than parsing through this model, so the mismatch was
    invisible at runtime and misleading only to a reader.
    """

    price: float = Field(description="Calculated price for the inference")
    is_estimated: Optional[bool] = Field(
        None,
        description=(
            "True when the price is an estimate rather than an exact quote. "
            "Partner models are billed by the provider after inference, so the "
            "final charge is not knowable up front."
        ),
    )
    estimate_basis: Optional[str] = Field(
        None,
        description=(
            "What the estimate derives from: 'request_params', 'model_history', "
            "'model_default', 'rate_card' or 'unavailable' (no estimate: the price shown is "
            "not the cost). Null for non-estimated prices."
        ),
    )


class PriceCalculationResponse(BaseModel):
    """Response from price calculation endpoints."""

    data: PriceCalculationData


class BalanceData(BaseModel):
    """User balance data."""

    balance: float = Field(description="Current account balance")
    currency: Optional[str] = Field("USD", description="Currency of the balance")


class BalanceResponse(BaseModel):
    """Response from balance endpoint."""

    data: BalanceData


class VoiceInfo(BaseModel):
    """A voice a TTS model can speak with. The API's `voice` field takes the slug."""

    name: str = Field(description="Display name, e.g. 'Alloy'")
    slug: str = Field(description="Value to pass as `voice`, e.g. 'af_alloy'")
    gender: Optional[str] = Field(None, description="Voice gender, if the model reports it")


class LanguageInfo(BaseModel):
    """A language a TTS model supports, with the voices available in it.

    The API's `lang` field takes the slug, and slugs are per-model: Kokoro uses
    'en-us', the Qwen3 models use 'English'. Voice-cloning models list their
    languages with no voices, since the voice comes from `ref_audio`.
    """

    name: str = Field(description="Display name, e.g. 'English (US)'")
    slug: str = Field(description="Value to pass as `lang`, e.g. 'en-us'")
    voices: List[VoiceInfo] = Field(default_factory=list, description="Voices in this language")


class ModelInfo(BaseModel):
    """Model information from deAPI."""

    model_config = {"extra": "allow"}  # Pydantic v2 syntax - allow additional fields from API

    name: str = Field(description="Model name")
    slug: str = Field(description="Model slug identifier")
    # "standard_model" for the catalogue proper; "test_model" for partner or
    # experimental models that may change or disappear. Absent on older APIs.
    status: Optional[str] = Field(None, description="Model status: standard_model or test_model")
    tags: Optional[List[str]] = Field(None, description="Catalogue tags, e.g. ['experimental']")
    # API returns list of strings, not dict (OpenAPI spec will be updated)
    inference_types: Union[List[str], Dict[str, Any]] = Field(
        description="Available inference types (currently returns array of strings)"
    )
    # API sometimes returns empty list instead of null/dict (will be fixed)
    info: Union[Dict[str, Any], List[Any], None] = Field(
        None,
        description="Model specs, limits, features, and defaults. Can be dict, empty list, or null",
    )
    loras: Optional[List[Dict[str, Any]]] = Field(
        None, description="Available LoRA models if supported"
    )
    languages: Optional[List[LanguageInfo]] = Field(
        None,
        description=(
            "Languages and voices a TTS model supports (eager-loaded by the API as "
            "enabledLanguages.enabledVoices). Absent for non-TTS models"
        ),
    )


class PaginationMeta(BaseModel):
    """Pagination metadata returned alongside paginated collections.

    The API's OpenAPI annotation documents only `data`, but Laravel's paginated
    resource collections always emit this block at runtime.
    """

    total: Optional[int] = Field(None, description="Total number of items across all pages")
    last_page: Optional[int] = Field(None, description="Index of the final page (1-based)")
    per_page: Optional[int] = Field(None, description="Items per page")
    current_page: Optional[int] = Field(None, description="Index of the current page (1-based)")


class ModelsResponse(BaseModel):
    """Response from models endpoint."""

    data: List[ModelInfo] = Field(description="List of available models")
    meta: Optional[PaginationMeta] = Field(
        None, description="Pagination metadata. Absent if the endpoint returns an unpaginated list"
    )


class ErrorResponse(BaseModel):
    """Standard error response."""

    message: str = Field(description="Error message")
    errors: Optional[Dict[str, Any]] = Field(None, description="Detailed error information")


# Request Schemas (for tool parameters)
class AudioTranscriptionRequest(BaseModel):
    """Audio transcription request parameters."""

    audio: str = Field(description="Audio file to transcribe (base64 encoded or URL)")
    include_ts: bool = Field(description="Should transcription include timestamps")
    model: Optional[str] = Field("WhisperLargeV3", description="The model to use for transcription")
    return_result_in_response: Optional[bool] = Field(
        True,
        description=(
            "Return transcription inline. Set to False for large files to get a "
            "download URL instead. Matches the transcription tools' own default."
        ),
    )


class Text2ImageRequest(BaseModel):
    """Text to image generation request parameters."""

    prompt: str = Field(description="Text prompt for image generation")
    model: str = Field(description="The model to use for generation")
    negative_prompt: Optional[str] = Field(None, description="Negative prompt to exclude elements")
    width: Optional[int] = Field(512, description="Image width in pixels")
    height: Optional[int] = Field(512, description="Image height in pixels")
    steps: Optional[int] = Field(20, description="Number of diffusion steps")
    guidance_scale: Optional[float] = Field(7.5, description="Guidance scale for generation")
    seed: Optional[int] = Field(None, description="Random seed for reproducibility")
    return_result_in_response: Optional[bool] = Field(
        False, description="If true, return result directly instead of polling"
    )


class Image2ImageRequest(BaseModel):
    """Image to image transformation request parameters."""

    image: str = Field(description="Source image (base64 encoded or URL)")
    prompt: str = Field(description="Text prompt for transformation")
    model: str = Field(description="The model to use for generation")
    negative_prompt: Optional[str] = Field(None, description="Negative prompt to exclude elements")
    strength: Optional[float] = Field(0.8, description="Transformation strength (0.0 to 1.0)")
    steps: Optional[int] = Field(20, description="Number of diffusion steps")
    guidance_scale: Optional[float] = Field(7.5, description="Guidance scale for generation")
    seed: Optional[int] = Field(None, description="Random seed for reproducibility")
    return_result_in_response: Optional[bool] = Field(
        False, description="If true, return result directly instead of polling"
    )


class Image2TextRequest(BaseModel):
    """Image to text (OCR) request parameters."""

    image: str = Field(description="Image file to extract text from (base64 encoded or URL)")
    model: str = Field(description="The OCR model to use for text extraction")
    language: Optional[str] = Field(None, description="Language code for OCR processing")
    format: Optional[OCRFormat] = Field(
        OCRFormat.TEXT, description="Output format for extracted text"
    )
    return_result_in_response: Optional[bool] = Field(
        False, description="If true, return result directly instead of polling"
    )


class Image2VideoRequest(BaseModel):
    """Image to video generation request parameters."""

    image: str = Field(description="Source image for video generation (base64 encoded or URL)")
    model: str = Field(description="The model to use for video generation")
    prompt: Optional[str] = Field(None, description="Text prompt for video generation")
    duration: Optional[float] = Field(3.0, description="Video duration in seconds")
    fps: Optional[int] = Field(24, description="Frames per second")
    return_result_in_response: Optional[bool] = Field(
        False, description="If true, return result directly instead of polling"
    )


class PromptImageRequest(BaseModel):
    """Prompt-based image generation request."""

    prompt: str = Field(description="Text prompt for image generation")
    model: str = Field(description="The model to use for generation")
    negative_prompt: Optional[str] = Field(None, description="Negative prompt")
    width: Optional[int] = Field(512, description="Image width")
    height: Optional[int] = Field(512, description="Image height")
    return_result_in_response: Optional[bool] = Field(False)


class PromptVideoRequest(BaseModel):
    """Prompt-based video generation request."""

    prompt: str = Field(description="Text prompt for video generation")
    model: str = Field(description="The model to use for generation")
    duration: Optional[float] = Field(3.0, description="Video duration in seconds")
    fps: Optional[int] = Field(24, description="Frames per second")
    return_result_in_response: Optional[bool] = Field(False)


class PromptSpeechRequest(BaseModel):
    """Prompt-based speech generation request."""

    prompt: str = Field(description="Text to convert to speech")
    model: str = Field(description="The model to use for speech generation")
    voice: Optional[str] = Field(None, description="Voice identifier")
    language: Optional[str] = Field("en", description="Language code")
    return_result_in_response: Optional[bool] = Field(False)


# Result Schemas (what we return to the LLM)
class ToolResult(BaseModel):
    """Standard tool result schema."""

    success: bool = Field(description="Whether the operation was successful")
    job_id: Optional[str] = Field(None, description="Job request ID (UUID)")
    status: Optional[JobStatus] = Field(None, description="Final job status")
    result: Optional[str] = Field(None, description="Result text (for transcription, OCR)")
    result_url: Optional[str] = Field(
        None, description="Result file URL (for images, videos, audio)"
    )
    error: Optional[str] = Field(None, description="Error message if failed")
    error_code: Optional[str] = Field(None, description="Machine-readable failure code")
    retryable: Optional[bool] = Field(
        None, description="Whether resubmitting the same job could succeed"
    )
    refunded: Optional[bool] = Field(None, description="Whether the job's cost was refunded")
    price: Optional[Dict[str, Any]] = Field(None, description="Actual cost of the job")
    results_alt_formats: Optional[Dict[str, Any]] = Field(
        None, description="Alternative result formats (e.g. jpg, webp)"
    )
    prompt_boost: Optional[PromptBoost] = Field(
        None, description="Boosted and original prompts when enhance_prompt ran"
    )
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    in_progress: bool = Field(
        False,
        description="The wait ended before the job did; it is still running and job_id resumes it",
    )
    progress: Optional[float] = Field(None, description="Last progress seen (0-100)")
    source_metadata: Optional[Dict[str, Any]] = Field(
        None, description="Source title/channel/counts, when the job asked for include_metadata"
    )
