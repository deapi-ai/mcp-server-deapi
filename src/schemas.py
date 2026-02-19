"""Pydantic schemas for deAPI requests and responses."""

from enum import Enum
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field


# Enums
class JobStatus(str, Enum):
    """Job status enumeration."""
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


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


class JobStatusData(BaseModel):
    """Job status response data."""
    status: JobStatus = Field(description="Current status of the job request")
    preview: Optional[str] = Field(None, description="Preview URL if available")
    result_url: Optional[str] = Field(
        None, description="URL to the result file (image/audio/video). Available when status is done"
    )
    result: Optional[str] = Field(
        None, description="Generated text (e.g. transcription). Available when status is done"
    )
    progress: Optional[float] = Field(
        None, description="Current progress of the job (0.0 to 100.0)", ge=0.0, le=100.0
    )


class JobStatusResponse(BaseModel):
    """Response from job status endpoint."""
    data: JobStatusData


class PriceCalculationData(BaseModel):
    """Price calculation response data."""
    estimated_price: float = Field(description="Estimated price for the operation")
    currency: Optional[str] = Field("USD", description="Currency of the price")


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


class ModelInfo(BaseModel):
    """Model information from deAPI."""
    model_config = {"extra": "allow"}  # Pydantic v2 syntax - allow additional fields from API

    name: str = Field(description="Model name")
    slug: str = Field(description="Model slug identifier")
    # API returns list of strings, not dict (OpenAPI spec will be updated)
    inference_types: Union[List[str], Dict[str, Any]] = Field(
        description="Available inference types (currently returns array of strings)"
    )
    # API sometimes returns empty list instead of null/dict (will be fixed)
    info: Union[Dict[str, Any], List, None] = Field(
        None,
        description="Model specs, limits, features, and defaults. Can be dict, empty list, or null"
    )
    loras: Optional[List[Dict[str, Any]]] = Field(None, description="Available LoRA models if supported")


class ModelsResponse(BaseModel):
    """Response from models endpoint."""
    data: List[ModelInfo] = Field(description="List of available models")


class ErrorResponse(BaseModel):
    """Standard error response."""
    message: str = Field(description="Error message")
    errors: Optional[Dict[str, Any]] = Field(None, description="Detailed error information")


# Request Schemas (for tool parameters)
class AudioTranscriptionRequest(BaseModel):
    """Audio transcription request parameters."""
    audio: str = Field(description="Audio file to transcribe (base64 encoded or URL)")
    include_ts: bool = Field(description="Should transcription include timestamps")
    model: Optional[str] = Field("whisper-3-large", description="The model to use for transcription")
    return_result_in_response: Optional[bool] = Field(
        False, description="If true, return result directly instead of polling"
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
    format: Optional[OCRFormat] = Field(OCRFormat.TEXT, description="Output format for extracted text")
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
    result_url: Optional[str] = Field(None, description="Result file URL (for images, videos, audio)")
    error: Optional[str] = Field(None, description="Error message if failed")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")