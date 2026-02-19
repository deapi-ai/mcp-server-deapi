"""Utility tools for deAPI MCP server."""

from ..deapi_client import get_client, DeapiAPIError


async def get_balance() -> dict:
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


async def get_available_models() -> dict:
    """Get list of available AI models with detailed specifications.

    Retrieves all available models for different inference types (txt2img, txt2video,
    img2img, img2video, audiofile2txt, img2txt, etc.) along with their capabilities,
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
       - supports_guidance: If "0", you MUST set guidance_scale to the default (usually 0)
       - supports_steps: If "0", model has fixed steps
       - supports_negative_prompt: If "0", don't pass negative_prompt
       - supports_last_frame: If "1", img2video supports initial frame control

    CRITICAL: Always check features.supports_guidance before setting guidance_scale!
    - If supports_guidance="0": Use the value from defaults.guidance (usually 0)
    - If supports_guidance="1": Can use any value within limits.min_guidance to limits.max_guidance

    Example workflow for txt2video with LTX-Video model:
    1. Check inference_types includes "txt2video"
    2. Check features.supports_guidance = "0" → must use guidance=0
    3. Check limits.max_steps = "1" → must use steps=1
    4. Check limits.min_fps = "30", max_fps = "30" → must use fps=30
    5. Use defaults for other parameters (width=512, height=512, frames=120)

    Returns:
        dict: Contains 'success', 'models' (list of model objects with name, slug,
              inference_types, info.limits, info.defaults, info.features), and 'count'
    """
    try:
        client = get_client()
        async with client:
            models_response = await client.get_models()
            models_list = models_response.data  # data is now directly a list

            return {
                "success": True,
                "models": [model.model_dump(mode='json') for model in models_list],
                "count": len(models_list),
            }

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def check_job_status(
    job_id: str,
) -> dict:
    """Check status of a submitted job.

    Query current status of any job by ID. Useful for long-running jobs.

    Args:
        job_id: Job request ID (UUID) to check status for

    Returns:
        dict: Contains 'success', job status, progress, and result if available
    """
    try:
        client = get_client()
        async with client:
            status_response = await client.get_job_status(job_id)
            status_data = status_response.data

            result = {
                "success": True,
                "job_id": job_id,
                "status": status_data.status.value,
            }

            if status_data.progress is not None:
                result["progress"] = status_data.progress

            if status_data.preview:
                result["preview_url"] = status_data.preview

            if status_data.result:
                result["result"] = status_data.result

            if status_data.result_url:
                result["result_url"] = status_data.result_url

            return result

    except DeapiAPIError as e:
        return {"success": False, "job_id": job_id, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "job_id": job_id, "error": f"Unexpected error: {str(e)}"}