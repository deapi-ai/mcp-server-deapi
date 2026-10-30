"""Utility tools for deAPI MCP server."""

from typing import Annotated, Any, Optional

from pydantic import Field

from ..config import settings
from ..deapi_client import DeapiAPIError, get_client
from ..observability import note_job_failure
from ..polling_manager import PollingManager
from ._responses import build_job_response, decode_transcript


async def get_balance() -> dict[str, Any]:
    """Get current account balance.

    Check your deAPI account balance and remaining credits.

    Returns:
        dict: Contains 'success' and balance information with amount and currency
    """
    try:
        client = get_client()
        async with client:
            balance_response = await client.get_balance()
            balance_data = balance_response.data

            return {
                "success": True,
                "balance": balance_data.balance,
                "currency": balance_data.currency or "USD",
            }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def get_available_models() -> dict[str, Any]:
    """Get list of available AI models with detailed specifications.

    Retrieves all available models for different inference types (txt2img, txt2video,
    img2img, img2video, audio_file2text, img2txt, video2desc, etc.) along with their capabilities,
    limits, defaults, and features.

    IMPORTANT - How to use model specifications:

    Each model's 'info' field contains critical information you MUST check before using:

    1. **inference_types**: List of supported operations (e.g., ["txt2img", "img2img"])

    2. **info.limits**: Min/max constraints for ALL parameters. Examples:
       - max_guidance/min_guidance: Valid range for guidance_scale
       - max_steps/min_steps: Valid range for inference steps
       - max_width/min_width, max_height/min_height: Image/video dimensions
       - max_frames/min_frames: Number of video frames
       - max_fps/min_fps: Video frame rate

    3. **info.defaults**: Recommended default values for the model. Examples:
       - If defaults.guidance="0", use guidance_scale=0
       - If defaults.steps="1", use steps=1
       - Use these when model has specific requirements

    4. **info.features**: Boolean flags (0=false, 1=true) indicating support:
       - supports_steps: If "0", model has fixed steps
       - supports_negative_prompt: If "0", don't pass negative_prompt
       - supports_last_frame: If "1", img2video supports initial frame control
       - supports_voice_clone / supports_voice_design / supports_custom_voice:
         which TTS mode the model implements. It implements exactly one and
         returns 422 for the others
       - supports_guidance: no model in the catalogue currently sets this to
         "1", so leave guidance unset and the model's own default is used
       - supports_image_refs / supports_video_refs / supports_audio_refs:
         img2video reference mode (ref_images / ref_videos / ref_audios);
         limits.max_ref_* give the counts, 0 meaning that type is not taken
       - supports_presets: video description presets, listed in
         limits.available_presets

    General rule: rather than assuming a value, omit the parameter and let the
    model's info.defaults apply, or read the bound from info.limits.

    Example workflow for txt2video with LTX-Video model:
    1. Check inference_types includes "txt2video"
    2. Check limits.max_steps = "1" → must use steps=1
    3. Check limits.min_fps = "30", max_fps = "30" → must use fps=30
    4. Use defaults for other parameters (width=512, height=512, frames=120)

    Returns:
        dict: Contains 'success', 'models' (list of model objects with name, slug,
              inference_types, info.limits, info.defaults, info.features), and 'count'
    """
    try:
        client = get_client()
        async with client:
            models_response = await client.get_models()
            models_list = models_response.data  # data is now directly a list

            # Report the catalogue's own total rather than the length of what we
            # happened to fetch: while those can differ, a count taken from the
            # list would present a truncated page as the complete catalogue.
            meta = models_response.meta
            count = meta.total if meta and meta.total is not None else len(models_list)

            return {
                "success": True,
                "models": [model.model_dump(mode="json") for model in models_list],
                "count": count,
            }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def check_job_status(
    job_id: Annotated[str, Field(description="Job request ID (UUID) to check")],
    wait_seconds: Annotated[
        Optional[int],
        Field(
            ge=0,
            description="Wait up to this many seconds for the job to finish (capped by the server, typically 45). Unset or 0: return the current status at once",
        ),
    ] = None,
) -> dict[str, Any]:
    """Check status of a submitted job, optionally waiting for it to finish.

    Every generation tool waits a limited time for its job. A job that takes
    longer (a long video, a long transcript, up to an hour) comes back with
    done=false and status 'processing'; call this with the job_id and
    wait_seconds to keep waiting, and repeat while done is false. Do not submit
    the job again — it is already running and paid for.

    Returns:
        dict: With wait_seconds: the same shape the generation tool returns when
              its job finishes ('result' / 'result_url', and for structured
              transcripts 'segments'), plus 'done' and 'status'; or done=false
              again if the job is still running. Without it: 'success', 'status',
              'progress' and the result if available. A failed job reports
              success=False with 'error_reason', 'error_code', 'retryable' and
              'refunded'.
    """
    if wait_seconds:
        return await _wait_for_job(job_id, wait_seconds)

    try:
        client = get_client()
        async with client:
            status_response = await client.get_job_status(job_id)
            status_data = status_response.data

            # A failed job is not a successful status check with an empty result:
            # reporting success here leaves the caller no signal to act on.
            failed = status_data.status.is_failure

            result = {
                "success": not failed,
                "job_id": job_id,
                "status": status_data.status.value,
            }

            if failed:
                note_job_failure(status_data.error_reason)
                result["error"] = status_data.error_reason or "Job failed"

            optional_fields = (
                ("progress", status_data.progress),
                ("result", status_data.result),
                ("result_url", status_data.result_url),
                ("results_alt_formats", status_data.results_alt_formats),
                ("price", status_data.price),
                ("error_code", status_data.error_code),
                ("error_reason", status_data.error_reason),
                ("retryable", status_data.retryable),
                ("refunded", status_data.refunded),
                ("failed_at", status_data.failed_at),
                ("prompt_boosted", status_data.prompt_boosted),
                (
                    "prompt_boost",
                    status_data.prompt_boost.model_dump() if status_data.prompt_boost else None,
                ),
                ("metadata", status_data.metadata),
            )
            for key, value in optional_fields:
                if value is not None:
                    result[key] = value

            # The API's `preview` is a base64 PNG of the in-progress image, not a
            # URL: ~20 KB of pixels an LLM cannot look at, on every status check.
            # The key stays so the response shape does not change; the value is
            # deliberately never filled.
            result["preview_url"] = None

            return result

    except DeapiAPIError as e:
        if e.status_code == 404:
            # The API's own message names the Laravel model class; say what
            # the caller can act on instead.
            return {"success": False, "job_id": job_id, "error": f"Job {job_id} not found"}
        return {"success": False, "job_id": job_id, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "job_id": job_id, "error": f"Unexpected error: {str(e)}"}


async def _wait_for_job(job_id: str, wait_seconds: int) -> dict[str, Any]:
    """Wait for a running job and return its result as its own tool would.

    The job type is not known here, so the default polling cadence is used;
    with internal webhooks on, a job submitted by this server still wakes the
    wait the moment it finishes.
    """
    try:
        client = get_client()
        async with client:
            wait = min(float(wait_seconds), settings.tool_max_wait)
            polling_manager = PollingManager(client, job_type="default", max_wait=wait)
            result = await polling_manager.poll_until_complete(job_id)

            response = decode_transcript(build_job_response(result, job_id))
            if not result.in_progress:
                response["done"] = True
                response["status"] = result.status.value if result.status else None
            return response

    except DeapiAPIError as e:
        return {"success": False, "job_id": job_id, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "job_id": job_id, "error": f"Unexpected error: {str(e)}"}
