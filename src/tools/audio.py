"""Audio processing tools for deAPI MCP server."""

from typing import Annotated, Optional

from pydantic import Field

from ..deapi_client import DeapiAPIError, get_client
from ..middleware import get_cached_model
from ..polling_manager import PollingManager
from ..utils import form_bool, prepare_audio_upload_async, prepare_video_upload_async
from ._price_helpers import _to_number, get_model_defaults, get_model_features
from ._responses import build_job_response

# Each TTS model supports exactly one mode and rejects the others with a 422,
# so the mode can be derived from the model card instead of being asked for.
_TTS_MODE_BY_FEATURE = (
    ("supports_voice_clone", "voice_clone"),
    ("supports_voice_design", "voice_design"),
    ("supports_custom_voice", "custom_voice"),
)


def _add_transcription_params(
    form_data: dict,
    ts_level: Optional[str] = None,
    diarize: Optional[bool] = None,
    lang: Optional[str] = None,
    include_metadata: Optional[bool] = None,
) -> dict:
    """Add the optional transcription controls to a form payload.

    Each is omitted when unset so the API applies its own default, and each is
    supported only by some models — `ts_level='word'` and `diarize` in
    particular are rejected by models that do not declare them in
    `info.limits.timestamp_levels`.
    """
    if ts_level:
        form_data["ts_level"] = ts_level
    if diarize is not None:
        form_data["diarize"] = form_bool(diarize)
    if lang:
        form_data["lang"] = lang
    if include_metadata is not None:
        form_data["include_metadata"] = form_bool(include_metadata)
    return form_data


def _tts_defaults(model_slug: str) -> dict:
    """Voice, language and format defaults from the model's own card.

    These differ per model in ways a fixed default cannot satisfy: Kokoro
    speaks "en-us" while the Qwen3 models want "English", and every current
    TTS model emits mp3 only. A hardcoded lang/format sent the wrong value to
    at least one model in every case, which the API rejected with a 422.
    """
    defaults = get_model_defaults(model_slug)
    card = get_cached_model(model_slug)

    # When the card's defaults are silent, the first listed language and its
    # first voice are at least values the model is known to accept.
    first_lang = card.languages[0] if card and card.languages else None
    first_voice = first_lang.voices[0].slug if first_lang and first_lang.voices else None

    return {
        "voice": defaults.get("voice") or first_voice,
        "lang": defaults.get("lang") or (first_lang.slug if first_lang else None),
        "format": defaults.get("format"),
        "speed": _to_number(defaults.get("speed")),
        "sample_rate": _to_number(defaults.get("sample_rate")),
    }


def _resolve_tts_mode(model_slug: str) -> Optional[str]:
    """Derive the TTS mode a model supports from its cached model card.

    Returns None when the model card is unavailable or advertises nothing, in
    which case the field is omitted and the API applies its own default
    ('custom_voice').
    """
    features = get_model_features(model_slug)
    for feature, mode in _TTS_MODE_BY_FEATURE:
        value = features.get(feature)
        # Feature flags arrive as "0"/"1" strings on some cards and as booleans
        # on others; "0" is truthy as a string, so compare explicitly.
        if value in (True, 1, "1", "true", "True"):
            return mode
    return None


async def audio_transcription(
    audio: Annotated[str, Field(description="Audio file (base64 encoded or URL)")],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[
        str, Field(description="Whisper model (e.g., 'whisper-3-large')")
    ] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. Only models declaring it in info.limits.timestamp_levels accept 'word'; it typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool],
        Field(
            description="Label who is speaking. Supported only by models that declare it; typically costs ~1.5x"
        ),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            max_length=16,
            description="Source language hint, e.g. 'en', 'pl'. Auto-detected when unset",
        ),
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata in the result. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return transcription inline. Set to False for large files to get download URL instead"
        ),
    ] = True,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
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
                "include_ts": form_bool(include_ts),
                "model": model,
                "return_result_in_response": form_bool(return_result_in_response),
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            # Poll for completion
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid audio format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    duration_seconds: Annotated[
        Optional[int], Field(description="Audio duration in seconds - optional")
    ] = None,
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. 'word' typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool], Field(description="Label who is speaking. Typically costs ~1.5x")
    ] = None,
    lang: Annotated[
        Optional[str], Field(max_length=16, description="Source language hint, e.g. 'en', 'pl'")
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    platform: Annotated[
        Optional[str],
        Field(
            description="Source platform: 'youtube', 'twitter', 'twitch', 'kick' or 'tiktok'. Changes the price substantially — tiktok can be ~9x youtube. Only valid together with duration_seconds"
        ),
    ] = None,
) -> dict:
    """Calculate price for audio transcription.

    Provide either audio file or duration_seconds for price calculation.

    `platform` is only accepted alongside `duration_seconds`: given a URL or a
    file the API derives the platform from the source itself.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "include_ts": form_bool(include_ts),
                "model": model,
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)
            if platform:
                form_data["platform"] = platform

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
    voice: Annotated[
        Optional[str],
        Field(description="Voice name (e.g. 'af_sky'). Defaults to the model's own default voice"),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            description="Language code as the model expects it (Kokoro: 'en-us'; Qwen3: 'English'). Defaults to the model's own"
        ),
    ] = None,
    speed: Annotated[
        Optional[float],
        Field(ge=0.1, le=3.0, description="Speech speed (0.1-3.0). Defaults to the model's own"),
    ] = None,
    audio_format: Annotated[
        Optional[str],
        Field(description="Output format. Defaults to the model's own; most models emit mp3 only"),
    ] = None,
    sample_rate: Annotated[
        Optional[int], Field(description="Sample rate in Hz. Defaults to the model's own")
    ] = None,
    mode: Annotated[
        Optional[str],
        Field(
            description="TTS mode: 'custom_voice', 'voice_clone' or 'voice_design'. Leave unset to use the mode the model supports"
        ),
    ] = None,
    ref_audio: Annotated[
        Optional[str],
        Field(
            description="Reference audio sample to clone (data URI, base64 or URL). Requires mode='voice_clone'"
        ),
    ] = None,
    ref_text: Annotated[
        Optional[str], Field(description="Transcript of ref_audio, improves cloning accuracy")
    ] = None,
    instruct: Annotated[
        Optional[str],
        Field(
            description="Style instruction, e.g. 'speak slowly and warmly'. Used by voice_design"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool, Field(description="Request immediate response")
    ] = False,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
) -> dict:
    """Convert text to speech audio using TTS models.

    Generates natural-sounding speech from text with customizable voice and parameters.

    Supports three modes, and each model implements exactly one — passing the
    wrong one is rejected with a 422. Leave `mode` unset and it is taken from
    the model's own card:
      - custom_voice: pick a preset `voice` (e.g. Kokoro, Chatterbox)
      - voice_clone:  reproduce the speaker in `ref_audio` (+ optional `ref_text`)
      - voice_design: build a voice from an `instruct` description

    `lang` and `voice` values are per-model and listed under each model in the
    "Available models" section below (Kokoro: lang 'en-us', voice 'af_alloy';
    Qwen3: lang 'English'). Leave them unset to use the model's own defaults.

    Returns:
        dict: Contains 'success', 'result_url' with audio URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            resolved_mode = mode or _resolve_tts_mode(model)

            card = _tts_defaults(model)
            request_data = {
                "text": text,
                "model": model,
                "voice": voice if voice is not None else card["voice"],
                "lang": lang if lang is not None else (card["lang"] or "en-us"),
                "speed": speed if speed is not None else (card["speed"] or 1.0),
                "format": audio_format if audio_format is not None else (card["format"] or "mp3"),
                "sample_rate": (
                    sample_rate if sample_rate is not None else (card["sample_rate"] or 24000)
                ),
                "return_result_in_response": return_result_in_response,
            }
            if resolved_mode:
                request_data["mode"] = resolved_mode
            if ref_text:
                request_data["ref_text"] = ref_text
            if instruct:
                request_data["instruct"] = instruct

            if ref_audio:
                # ref_audio is a file upload, so the whole request has to become
                # multipart; JSON cannot carry it.
                field_name, file_tuple = await prepare_audio_upload_async(
                    ref_audio, field_name="ref_audio"
                )
                form_data = {
                    key: form_bool(value) if isinstance(value, bool) else str(value)
                    for key, value in request_data.items()
                }
                job_response = await client.submit_job(
                    endpoint="audio/speech",
                    data=form_data,
                    files={field_name: file_tuple},
                    webhook_url=webhook_url,
                    webhook_secret=webhook_secret,
                )
            else:
                job_response = await client.submit_job(
                    endpoint="audio/speech",
                    json_data=request_data,
                    webhook_url=webhook_url,
                    webhook_secret=webhook_secret,
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
    voice: Annotated[
        Optional[str],
        Field(description="Voice name (e.g. 'af_sky'). Defaults to the model's own default voice"),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            description="Language code as the model expects it (Kokoro: 'en-us'; Qwen3: 'English'). Defaults to the model's own"
        ),
    ] = None,
    speed: Annotated[
        Optional[float],
        Field(ge=0.1, le=3.0, description="Speech speed (0.1-3.0). Defaults to the model's own"),
    ] = None,
    audio_format: Annotated[
        Optional[str],
        Field(description="Output format. Defaults to the model's own; most models emit mp3 only"),
    ] = None,
    sample_rate: Annotated[
        Optional[int], Field(description="Sample rate in Hz. Defaults to the model's own")
    ] = None,
    mode: Annotated[
        Optional[str],
        Field(
            description="TTS mode: 'custom_voice', 'voice_clone' or 'voice_design'. Leave unset to use the mode the model supports"
        ),
    ] = None,
    instruct: Annotated[
        Optional[str], Field(description="Style instruction, priced like the generation request")
    ] = None,
    count_text: Annotated[
        Optional[int],
        Field(ge=1, description="Price for this many characters instead of the given text"),
    ] = None,
) -> dict:
    """Calculate price for text-to-audio generation.

    `mode` must match what the job will use, otherwise the quote can differ from
    the charge. Left unset it is taken from the model's card, exactly as
    text_to_audio does.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            resolved_mode = mode or _resolve_tts_mode(model)

            card = _tts_defaults(model)
            request_data = {
                "text": text,
                "model": model,
                "voice": voice if voice is not None else card["voice"],
                "lang": lang if lang is not None else (card["lang"] or "en-us"),
                "speed": speed if speed is not None else (card["speed"] or 1.0),
                "format": audio_format if audio_format is not None else (card["format"] or "mp3"),
                "sample_rate": (
                    sample_rate if sample_rate is not None else (card["sample_rate"] or 24000)
                ),
            }
            if resolved_mode:
                request_data["mode"] = resolved_mode
            if instruct:
                request_data["instruct"] = instruct
            if count_text is not None:
                request_data["count_text"] = count_text

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
    video: Annotated[
        str,
        Field(
            description="Video file as URL (preferred), data URI (data:video/mp4;base64,...), or base64 string. URLs are recommended to avoid base64 context bloat."
        ),
    ],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[
        str, Field(description="Whisper model (e.g., 'whisper-3-large')")
    ] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. Only models declaring it in info.limits.timestamp_levels accept 'word'; it typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool],
        Field(
            description="Label who is speaking. Supported only by models that declare it; typically costs ~1.5x"
        ),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            max_length=16,
            description="Source language hint, e.g. 'en', 'pl'. Auto-detected when unset",
        ),
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata in the result. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return transcription inline. Set to False for large files to get download URL instead"
        ),
    ] = True,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
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
                "include_ts": form_bool(include_ts),
                "model": model,
                "return_result_in_response": form_bool(return_result_in_response),
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type (same processing)
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_file_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    duration_seconds: Annotated[
        Optional[int], Field(description="Video duration in seconds - optional")
    ] = None,
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. 'word' typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool], Field(description="Label who is speaking. Typically costs ~1.5x")
    ] = None,
    lang: Annotated[
        Optional[str], Field(max_length=16, description="Source language hint, e.g. 'en', 'pl'")
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    platform: Annotated[
        Optional[str],
        Field(
            description="Source platform: 'youtube', 'twitter', 'twitch', 'kick' or 'tiktok'. Changes the price substantially — tiktok can be ~9x youtube. Only valid together with duration_seconds"
        ),
    ] = None,
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
                "include_ts": form_bool(include_ts),
                "model": model,
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)
            if platform:
                form_data["platform"] = platform

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
    video_url: Annotated[
        str,
        Field(
            description="URL of video to transcribe. Supports YouTube (e.g., 'https://www.youtube.com/watch?v=...'), Twitter/X (e.g., 'https://twitter.com/user/status/...' or 'https://x.com/user/status/...'), Twitch (e.g., 'https://www.twitch.tv/videos/...'), and Kick (e.g., 'https://kick.com/video/...')"
        ),
    ],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[
        str, Field(description="Whisper model (e.g., 'WhisperLargeV3')")
    ] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. Only models declaring it in info.limits.timestamp_levels accept 'word'; it typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool],
        Field(
            description="Label who is speaking. Supported only by models that declare it; typically costs ~1.5x"
        ),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            max_length=16,
            description="Source language hint, e.g. 'en', 'pl'. Auto-detected when unset",
        ),
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata in the result. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return transcription inline. Set to False for large files to get download URL instead"
        ),
    ] = True,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
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
                "include_ts": form_bool(include_ts),
                "model": model,
                "return_result_in_response": form_bool(return_result_in_response),
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type (same processing)
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_url_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    video_url: Annotated[
        Optional[str], Field(description="URL of video. Provide this or duration_seconds, not both")
    ] = None,
    duration_seconds: Annotated[
        Optional[int],
        Field(
            description="Video duration in seconds, to quote without supplying a URL. Mutually exclusive with video_url"
        ),
    ] = None,
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. 'word' typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool], Field(description="Label who is speaking. Typically costs ~1.5x")
    ] = None,
    lang: Annotated[
        Optional[str], Field(max_length=16, description="Source language hint, e.g. 'en', 'pl'")
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    platform: Annotated[
        Optional[str],
        Field(
            description="Source platform: 'youtube', 'twitter', 'twitch', 'kick' or 'tiktok'. Changes the price substantially — tiktok can be ~9x youtube. Only valid together with duration_seconds"
        ),
    ] = None,
) -> dict:
    """Calculate price for video URL transcription.

    Quote either from a URL or from a duration. The API accepts exactly one:
    given a URL it derives duration and platform from the source itself, so
    `duration_seconds` and `platform` are rejected alongside it.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            if video_url and duration_seconds is not None:
                return {
                    "success": False,
                    "error": "Pass either 'video_url' or 'duration_seconds', not both — the API rejects requests carrying both.",
                }
            if not video_url and duration_seconds is None:
                return {
                    "success": False,
                    "error": "Provide 'video_url' or 'duration_seconds' to calculate a price.",
                }
            if platform and video_url:
                return {
                    "success": False,
                    "error": "'platform' is only accepted with 'duration_seconds'; with a URL the API derives the platform from the source.",
                }

            form_data = {
                "include_ts": form_bool(include_ts),
                "model": model,
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            if video_url:
                form_data["source_url"] = video_url
            if duration_seconds is not None:
                form_data["duration_seconds"] = str(duration_seconds)
            if platform:
                form_data["platform"] = platform

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
    caption: Annotated[
        str, Field(description="Text description of the music to generate (max 5000 chars)")
    ],
    model: Annotated[
        str,
        Field(description="Music generation model name (must support txt2music inference type)"),
    ],
    lyrics: Annotated[
        str, Field(description="Song lyrics (max 10000 chars). Use '[Instrumental]' for no vocals")
    ],
    duration: Annotated[
        int,
        Field(
            ge=10,
            le=600,
            description="Audio duration in seconds (10-600, further constrained by model limits)",
        ),
    ],
    inference_steps: Annotated[
        int,
        Field(
            ge=1,
            le=100,
            description="Number of inference steps (1-100). Use 8 for turbo, 32+ for base models",
        ),
    ],
    guidance_scale: Annotated[
        float,
        Field(ge=0.0, le=20.0, description="Guidance scale (0-20, constrained by model limits)"),
    ],
    seed: Annotated[int, Field(description="Random seed (-1 for random)")] = -1,
    audio_format: Annotated[
        str, Field(description="Output format: 'wav', 'flac', or 'mp3'")
    ] = "wav",
    bpm: Annotated[
        Optional[int], Field(ge=30, le=300, description="Beats per minute (30-300, optional)")
    ] = None,
    keyscale: Annotated[
        Optional[str], Field(description="Musical key/scale, e.g. 'C major', 'F# minor' (optional)")
    ] = None,
    timesignature: Annotated[
        Optional[int], Field(description="Time signature: 2, 3, 4, or 6 (optional)")
    ] = None,
    vocal_language: Annotated[
        Optional[str], Field(description="Vocal language code, e.g. 'en', 'es' (optional)")
    ] = None,
    reference_audio: Annotated[
        Optional[str],
        Field(
            description="Reference audio for style transfer (URL, data URI, or base64). Duration must be within model's ref audio limits (typically 3-10s)"
        ),
    ] = None,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
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
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
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
    duration: Annotated[
        Optional[int], Field(ge=10, le=600, description="Audio duration in seconds")
    ] = None,
    inference_steps: Annotated[
        Optional[int], Field(ge=1, le=100, description="Number of inference steps")
    ] = None,
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
    audio_url: Annotated[
        str,
        Field(
            description="URL of Twitter Spaces audio to transcribe (e.g., 'https://twitter.com/i/spaces/1nAKEERkeLbKL')"
        ),
    ],
    include_ts: Annotated[bool, Field(description="Include timestamps in transcription")],
    model: Annotated[
        str, Field(description="Whisper model (e.g., 'WhisperLargeV3')")
    ] = "WhisperLargeV3",
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. Only models declaring it in info.limits.timestamp_levels accept 'word'; it typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool],
        Field(
            description="Label who is speaking. Supported only by models that declare it; typically costs ~1.5x"
        ),
    ] = None,
    lang: Annotated[
        Optional[str],
        Field(
            max_length=16,
            description="Source language hint, e.g. 'en', 'pl'. Auto-detected when unset",
        ),
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata in the result. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return transcription inline. Set to False for large files to get download URL instead"
        ),
    ] = True,
    webhook_url: Annotated[
        Optional[str],
        Field(
            description="HTTPS URL for deAPI to call when this job finishes. Notifies your own systems; the tool still returns the result here"
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        Field(
            description="32-255 char key deAPI signs the callback with (HMAC-SHA256). Needs webhook_url. A secret given in chat stays in the transcript"
        ),
    ] = None,
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
                "include_ts": form_bool(include_ts),
                "model": model,
                "return_result_in_response": form_bool(return_result_in_response),
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            job_response = await client.submit_job(
                endpoint="audio/transcriptions",
                data=form_data,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            # Poll for completion using audio job type
            polling_manager = PollingManager(client, job_type="audio")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_url_transcription_price(
    include_ts: Annotated[bool, Field(description="Include timestamps")],
    model: Annotated[str, Field(description="Whisper model name")] = "WhisperLargeV3",
    audio_url: Annotated[
        Optional[str],
        Field(description="Twitter Spaces URL (required if duration_seconds not provided)"),
    ] = None,
    duration_seconds: Annotated[
        Optional[int],
        Field(description="Audio duration in seconds (required if audio_url not provided)"),
    ] = None,
    ts_level: Annotated[
        Optional[str],
        Field(
            description="Timestamp granularity: 'segment' or 'word'. 'word' typically costs ~1.5x"
        ),
    ] = None,
    diarize: Annotated[
        Optional[bool], Field(description="Label who is speaking. Typically costs ~1.5x")
    ] = None,
    lang: Annotated[
        Optional[str], Field(max_length=16, description="Source language hint, e.g. 'en', 'pl'")
    ] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Include source metadata. Carries a flat surcharge on models that price it"
        ),
    ] = None,
    platform: Annotated[
        Optional[str],
        Field(
            description="Source platform: 'youtube', 'twitter', 'twitch', 'kick' or 'tiktok'. Changes the price substantially. Only valid together with duration_seconds"
        ),
    ] = None,
) -> dict:
    """Calculate price for Twitter Spaces audio transcription.

    Provide either audio_url or duration_seconds for price calculation.
    `platform` is only accepted alongside `duration_seconds`; with a URL the
    API derives the platform from the source.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            if platform and audio_url:
                return {
                    "success": False,
                    "error": "'platform' is only accepted with 'duration_seconds'; with a URL the API derives the platform from the source.",
                }

            form_data = {
                "include_ts": form_bool(include_ts),
                "model": model,
            }
            _add_transcription_params(form_data, ts_level, diarize, lang, include_metadata)

            if platform:
                form_data["platform"] = platform

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
