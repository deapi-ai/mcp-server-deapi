"""Audio processing tools for deAPI MCP server."""

from typing import Annotated, Optional

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..polling_manager import PollingManager
from ..utils import prepare_audio_upload_async


async def audio_transcription(
    audio: Annotated[str, Field(description="Audio file (base64 encoded or URL)")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[str, Field(description="Whisper model (e.g., 'whisper-3-large')")] = "WhisperLargeV3",
    return_result_in_response: Annotated[bool, Field(description="Return transcription inline. Set to False for large files to get download URL instead")] = True,
) -> dict:
    """Transcribe audio file to text using Whisper models.

    Converts audio files to text transcription with optional timestamps.
    Automatically polls until transcription is complete.

    Returns:
        dict: Contains 'success', 'result' with transcription text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare audio file for multipart upload
            field_name, file_tuple = await prepare_audio_upload_async(audio, "audio")

            # Prepare form data (non-file parameters)
            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            job_response = await client.submit_job(
                endpoint="audiofile2txt",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            # Poll for completion
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result": result.result,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except ValueError as e:
        return {"success": False, "error": f"Invalid audio format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    duration_seconds: Annotated[Optional[int], Field(description="Audio duration in seconds - optional")] = None,
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
) -> dict:
    """Calculate price for audio transcription.

    Provide either audio file or duration_seconds for price calculation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
            }

            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)

            price_response = await client.calculate_price(
                endpoint="audiofile2txt/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_audio(
    text: Annotated[str, Field(description="Text to convert to speech")],
    model: Annotated[str, Field(description="TTS model name (e.g., 'Kokoro')")],
    voice: Annotated[str, Field(description="Voice name (e.g., 'af_sky')")],
    lang: Annotated[str, Field(description="Language code (e.g., 'en-us', 'es-es')")] = "en-us",
    speed: Annotated[float, Field(ge=0.1, le=3.0, description="Speech speed (0.1-3.0)")] = 1.0,
    audio_format: Annotated[str, Field(description="Audio format (flac, mp3, wav)")] = "flac",
    sample_rate: Annotated[int, Field(description="Sample rate in Hz")] = 24000,
    return_result_in_response: Annotated[bool, Field(description="Request immediate response")] = False,
) -> dict:
    """Convert text to speech audio using TTS models.

    Generates natural-sounding speech from text with customizable voice and parameters.

    Returns:
        dict: Contains 'success', 'result_url' with audio URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "text": text,
                "model": model,
                "voice": voice,
                "lang": lang,
                "speed": speed,
                "format": audio_format,
                "sample_rate": sample_rate,
                "return_result_in_response": return_result_in_response,
            }

            job_response = await client.submit_job(
                endpoint="txt2audio",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result_url": result.result_url,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_audio_price(
    text: Annotated[str, Field(description="Text for price calculation")],
    model: Annotated[str, Field(description="TTS model name (e.g., 'Kokoro')")],
    voice: Annotated[str, Field(description="Voice name (e.g., 'af_sky')")],
    lang: Annotated[str, Field(description="Language code (e.g., 'en-us')")] = "en-us",
    speed: Annotated[float, Field(ge=0.1, le=3.0, description="Speech speed (0.1-3.0)")] = 1.0,
    audio_format: Annotated[str, Field(description="Audio format (flac, mp3, wav)")] = "flac",
    sample_rate: Annotated[int, Field(description="Sample rate in Hz")] = 24000,
) -> dict:
    """Calculate price for text-to-audio generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "text": text,
                "model": model,
                "voice": voice,
                "lang": lang,
                "speed": speed,
                "format": audio_format,
                "sample_rate": sample_rate,
            }

            price_response = await client.calculate_price(
                endpoint="txt2audio/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_file_transcription(
    video: Annotated[str, Field(description="Video file as data URI (data:video/mp4;base64,...) or base64 string. IMPORTANT: Pass the video directly without displaying or printing the base64 data.")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[str, Field(description="Whisper model (e.g., 'whisper-3-large')")] = "WhisperLargeV3",
    return_result_in_response: Annotated[bool, Field(description="Return transcription inline. Set to False for large files to get download URL instead")] = True,
) -> dict:
    """Transcribe video file to text using Whisper models.

    Extracts audio from video and converts it to text transcription with optional timestamps.
    Accepts video files as data URIs (from Claude Desktop attachments) or base64 strings.
    Automatically polls until transcription is complete.

    Returns:
        dict: Contains 'success', 'result' with transcription text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare video file upload (need to check if API expects file or base64)
            # Based on OpenAPI spec, videofile2txt uses application/json with binary format
            # So we'll send as JSON like audiofile2txt
            request_data = {
                "video": video,
                "include_ts": include_ts,
                "model": model,
                "return_result_in_response": return_result_in_response,
            }

            job_response = await client.submit_job(
                endpoint="videofile2txt",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type (same processing)
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result": result.result,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_file_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    duration_seconds: Annotated[Optional[int], Field(description="Video duration in seconds - optional")] = None,
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
) -> dict:
    """Calculate price for video file transcription.

    Provide either video file or duration_seconds for price calculation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
            }

            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)

            price_response = await client.calculate_price(
                endpoint="videofile2txt/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_url_transcription(
    video_url: Annotated[str, Field(description="URL of video to transcribe. Supports YouTube (e.g., 'https://www.youtube.com/watch?v=...'), Twitter/X (e.g., 'https://twitter.com/user/status/...' or 'https://x.com/user/status/...'), Twitch (e.g., 'https://www.twitch.tv/videos/...'), and Kick (e.g., 'https://kick.com/video/...')")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[str, Field(description="Whisper model (e.g., 'WhisperLargeV3')")] = "WhisperLargeV3",
    return_result_in_response: Annotated[bool, Field(description="Return transcription inline. Set to False for large files to get download URL instead")] = True,
) -> dict:
    """Transcribe video from URL to text using Whisper models.

    Extracts audio from video URL and converts it to text transcription with optional timestamps.
    Supports YouTube, Twitter/X, Twitch, and Kick videos.
    Automatically polls until transcription is complete.

    Returns:
        dict: Contains 'success', 'result' with transcription text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "video_url": video_url,
                "include_ts": include_ts,
                "model": model,
                "return_result_in_response": return_result_in_response,
            }

            job_response = await client.submit_job(
                endpoint="vid2txt",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type (same processing)
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result": result.result,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_url_transcription_price(
    video_url: Annotated[str, Field(description="URL of video")],
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
) -> dict:
    """Calculate price for video URL transcription.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "video_url": video_url,
                "include_ts": include_ts,
                "model": model,
            }

            price_response = await client.calculate_price(
                endpoint="vid2txt/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_url_transcription(
    audio_url: Annotated[str, Field(description="URL of Twitter Spaces audio to transcribe (e.g., 'https://twitter.com/i/spaces/1nAKEERkeLbKL')")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[str, Field(description="Whisper model (e.g., 'WhisperLargeV3')")] = "WhisperLargeV3",
    return_result_in_response: Annotated[bool, Field(description="Return transcription inline. Set to False for large files to get download URL instead")] = True,
) -> dict:
    """Transcribe audio from Twitter Spaces URL to text using Whisper models.

    Extracts and transcribes audio from Twitter Spaces URLs.
    NOTE: This endpoint only works with Twitter Spaces URLs. For video content,
    use video_url_transcription instead.

    Returns:
        dict: Contains 'success', 'result' with transcription text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "audio_url": audio_url,
                "include_ts": include_ts,
                "model": model,
                "return_result_in_response": return_result_in_response,
            }

            job_response = await client.submit_job(
                endpoint="aud2txt",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            if result.success:
                return {
                    "success": True,
                    "result": result.result,
                    "job_id": job_id,
                    "metadata": result.metadata,
                }
            else:
                return {
                    "success": False,
                    "error": result.error,
                    "job_id": job_id,
                }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_url_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
    audio_url: Annotated[Optional[str], Field(description="Twitter Spaces URL (required if duration_seconds not provided)")] = None,
    duration_seconds: Annotated[Optional[int], Field(description="Audio duration in seconds (required if audio_url not provided)")] = None,
) -> dict:
    """Calculate price for Twitter Spaces audio transcription.

    Provide either audio_url or duration_seconds for price calculation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "include_ts": include_ts,
                "model": model,
            }

            if audio_url:
                request_data["audio_url"] = audio_url
            if duration_seconds is not None:
                request_data["duration_seconds"] = duration_seconds

            price_response = await client.calculate_price(
                endpoint="aud2txt/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}