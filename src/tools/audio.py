"""Audio processing tools for deAPI MCP server."""

from typing import Annotated, Optional

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..polling_manager import PollingManager
from ..utils import prepare_audio_upload_async, prepare_video_upload_async


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
            field_name, file_tuple = await prepare_audio_upload_async(audio, "source_file")

            # Prepare form data (non-file parameters)
            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
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
                endpoint="audio/transcriptions/price",
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
                endpoint="audio/speech",
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
                endpoint="audio/speech/price",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_file_transcription(
    video: Annotated[str, Field(description="Video file as URL (preferred), data URI (data:video/mp4;base64,...), or base64 string. URLs are recommended to avoid base64 context bloat.")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[str, Field(description="Whisper model (e.g., 'whisper-3-large')")] = "WhisperLargeV3",
    return_result_in_response: Annotated[bool, Field(description="Return transcription inline. Set to False for large files to get download URL instead")] = True,
) -> dict:
    """Transcribe video file to text using Whisper models.

    Extracts audio from video and converts it to text transcription with optional timestamps.
    Accepts video files as URLs (preferred), data URIs, or base64 strings.
    Automatically polls until transcription is complete.

    Returns:
        dict: Contains 'success', 'result' with transcription text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # v2 unified transcription endpoint takes the video as a multipart file upload
            field_name, file_tuple = await prepare_video_upload_async(video, "source_file")

            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
                files={field_name: file_tuple},
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
                endpoint="audio/transcriptions/price",
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
            form_data = {
                "source_url": video_url,
                "include_ts": str(include_ts).lower(),
                "model": model,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
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
            form_data = {
                "source_url": video_url,
                "include_ts": str(include_ts).lower(),
                "model": model,
            }

            price_response = await client.calculate_price(
                endpoint="audio/transcriptions/price",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_music(
    caption: Annotated[str, Field(description="Text description of the music to generate (max 5000 chars)")],
    model: Annotated[str, Field(description="Music generation model name (must support txt2music inference type)")],
    lyrics: Annotated[str, Field(description="Song lyrics (max 10000 chars). Use '[Instrumental]' for no vocals")],
    duration: Annotated[int, Field(ge=10, le=600, description="Audio duration in seconds (10-600, further constrained by model limits)")],
    inference_steps: Annotated[int, Field(ge=1, le=100, description="Number of inference steps (1-100). Use 8 for turbo, 32+ for base models")],
    guidance_scale: Annotated[float, Field(ge=0.0, le=20.0, description="Guidance scale (0-20, constrained by model limits)")],
    seed: Annotated[int, Field(description="Random seed (-1 for random)")] = -1,
    audio_format: Annotated[str, Field(description="Output format: 'wav', 'flac', or 'mp3'")] = "wav",
    bpm: Annotated[Optional[int], Field(ge=30, le=300, description="Beats per minute (30-300, optional)")] = None,
    keyscale: Annotated[Optional[str], Field(description="Musical key/scale, e.g. 'C major', 'F# minor' (optional)")] = None,
    timesignature: Annotated[Optional[int], Field(description="Time signature: 2, 3, 4, or 6 (optional)")] = None,
    vocal_language: Annotated[Optional[str], Field(description="Vocal language code, e.g. 'en', 'es' (optional)")] = None,
    reference_audio: Annotated[Optional[str], Field(description="Reference audio for style transfer (URL, data URI, or base64). Duration must be within model's ref audio limits (typically 3-10s)")] = None,
) -> dict:
    """Generate music from text description and lyrics.

    Creates music tracks from text prompts with customizable parameters including
    tempo, key, time signature, and optional style reference audio.

    IMPORTANT: Check model specifications using get_available_models() before calling.
    Pay attention to model limits for duration, inference_steps, guidance_scale, and bpm.

    Returns:
        dict: Contains 'success', 'result_url' with audio URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "caption": caption,
                "model": model,
                "lyrics": lyrics,
                "duration": str(duration),
                "inference_steps": str(inference_steps),
                "guidance_scale": str(guidance_scale),
                "seed": str(seed),
                "format": audio_format,
            }

            if bpm is not None:
                form_data["bpm"] = str(bpm)
            if keyscale is not None:
                form_data["keyscale"] = keyscale
            if timesignature is not None:
                form_data["timesignature"] = str(timesignature)
            if vocal_language is not None:
                form_data["vocal_language"] = vocal_language

            files = {}
            if reference_audio:
                field_name, file_tuple = await prepare_audio_upload_async(
                    reference_audio, "reference_audio"
                )
                files[field_name] = file_tuple

            job_response = await client.submit_job(
                endpoint="audio/music",
                data=form_data,
                files=files if files else None,
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

    except ValueError as e:
        return {"success": False, "error": f"Invalid audio format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_music_price(
    model: Annotated[str, Field(description="Music generation model name")],
    duration: Annotated[Optional[int], Field(ge=10, le=600, description="Audio duration in seconds")] = None,
    inference_steps: Annotated[Optional[int], Field(ge=1, le=100, description="Number of inference steps")] = None,
) -> dict:
    """Calculate price for text-to-music generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {"model": model}

            if duration is not None:
                form_data["duration"] = str(duration)
            if inference_steps is not None:
                form_data["inference_steps"] = str(inference_steps)

            price_response = await client.calculate_price(
                endpoint="audio/music/price",
                data=form_data,
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
            form_data = {
                "source_url": audio_url,
                "include_ts": str(include_ts).lower(),
                "model": model,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
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
            form_data = {
                "include_ts": str(include_ts).lower(),
                "model": model,
            }

            if audio_url:
                form_data["source_url"] = audio_url
            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)

            price_response = await client.calculate_price(
                endpoint="audio/transcriptions/price",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}