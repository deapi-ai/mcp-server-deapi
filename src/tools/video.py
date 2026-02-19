"""Video generation tools for deAPI MCP server."""

from typing import Annotated, Optional

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..polling_manager import PollingManager
from ..utils import prepare_image_upload_async, prepare_video_upload_async
from ._price_helpers import resolve_generation_params


async def image_to_video(
    first_frame_image: Annotated[str, Field(description="First frame image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining from text_to_image to avoid base64 context bloat.")],
    prompt: Annotated[str, Field(description="Text prompt for video generation")],
    model: Annotated[str, Field(description="Video generation model name")],
    negative_prompt: Annotated[Optional[str], Field(description="Things to exclude (optional)")] = None,
    last_frame_image: Annotated[Optional[str], Field(description="Last frame image (optional, URL, data URI, or base64)")] = None,
    width: Annotated[int, Field(ge=64, le=2048, description="Video width in pixels")] = 512,
    height: Annotated[int, Field(ge=64, le=2048, description="Video height in pixels")] = 512,
    frames: Annotated[int, Field(ge=1, le=200, description="Number of video frames")] = 120,
    fps: Annotated[int, Field(ge=1, le=60, description="Frames per second")] = 30,
    steps: Annotated[int, Field(ge=1, le=100, description="Number of inference steps")] = 1,
    guidance_scale: Annotated[float, Field(ge=0.0, le=20.0, description="Guidance scale. Check model's features.supports_guidance - if 0, use guidance_scale=0")] = 0.0,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    return_result_in_response: Annotated[bool, Field(description="Request immediate response")] = False,
) -> dict:
    """Generate video from static image(s).

    Animates images into videos using AI models. Accepts images as:
    - URLs (recommended for chaining with text_to_image - avoids base64 bloat)
    - Data URIs (from Claude Desktop attachments)
    - Base64 strings

    IMPORTANT: Check model specifications using get_available_models() before calling.
    Pay attention to features.supports_guidance and limits for width/height/frames/fps.

    Returns:
        dict: Contains 'success', 'result_url' with video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare first frame image file upload (required)
            # Uses async version that supports URLs
            field_name1, file_tuple1 = await prepare_image_upload_async(first_frame_image, "first_frame_image")
            files = {field_name1: file_tuple1}

            # Prepare last frame image if provided (optional)
            if last_frame_image:
                field_name2, file_tuple2 = await prepare_image_upload_async(last_frame_image, "last_frame_image")
                files[field_name2] = file_tuple2

            # Prepare form data
            form_data = {
                "prompt": prompt,
                "model": model,
                "width": str(width),
                "height": str(height),
                "guidance": str(guidance_scale),
                "steps": str(steps),
                "frames": str(frames),
                "fps": str(fps),
                "seed": str(seed),
            }

            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt

            job_response = await client.submit_job(
                endpoint="img2video",
                data=form_data,
                files=files,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
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
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_video(
    prompt: Annotated[str, Field(description="Text prompt for video generation")],
    model: Annotated[str, Field(description="Video generation model name")],
    negative_prompt: Annotated[Optional[str], Field(description="Things to exclude (optional)")] = None,
    width: Annotated[int, Field(ge=64, le=2048, description="Video width in pixels")] = 512,
    height: Annotated[int, Field(ge=64, le=2048, description="Video height in pixels")] = 512,
    frames: Annotated[int, Field(ge=1, le=200, description="Number of video frames")] = 20,
    fps: Annotated[int, Field(ge=1, le=60, description="Frames per second")] = 24,
    steps: Annotated[int, Field(ge=1, le=100, description="Number of inference steps")] = 20,
    guidance_scale: Annotated[float, Field(ge=0.0, le=20.0, description="Guidance scale. IMPORTANT: Check model's features.supports_guidance first! If supports_guidance=0, you MUST use guidance_scale=0 (or the model's defaults.guidance value). Only models with supports_guidance=1 can use values > 0.")] = 7.5,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    return_result_in_response: Annotated[bool, Field(description="Request immediate response")] = False,
) -> dict:
    """Generate video from text prompt.

    Creates videos from text descriptions. Returns the generated video URL.

    IMPORTANT: Before calling this tool, use get_available_models() to check the model's
    specifications. Pay special attention to:
    - features.supports_guidance: If "0", set guidance_scale to 0
    - limits.max_steps/min_steps: Valid range for steps parameter
    - limits.max_fps/min_fps: Valid range for fps parameter
    - defaults: Recommended values for the model

    Returns:
        dict: Contains 'success', 'result_url' with video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # txt2video uses multipart/form-data
            request_data = {
                "prompt": prompt,
                "model": model,
                "width": str(width),
                "height": str(height),
                "guidance": str(guidance_scale),
                "steps": str(steps),
                "frames": str(frames),
                "fps": str(fps),
                "seed": str(seed),
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            if negative_prompt:
                request_data["negative_prompt"] = negative_prompt

            job_response = await client.submit_job(
                endpoint="txt2video",
                data=request_data,  # Use data for form-data, not json_data
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
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


async def image_to_video_price(
    model: Annotated[str, Field(description="Video generation model name")],
    width: Annotated[Optional[int], Field(ge=64, le=2048, description="Video width in pixels")] = None,
    height: Annotated[Optional[int], Field(ge=64, le=2048, description="Video height in pixels")] = None,
    frames: Annotated[Optional[int], Field(ge=1, le=200, description="Number of video frames")] = None,
    steps: Annotated[Optional[int], Field(ge=1, le=100, description="Number of inference steps")] = None,
    fps: Annotated[Optional[int], Field(ge=1, le=60, description="Frames per second (optional)")] = None,
) -> dict:
    """Calculate price for image-to-video generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            params = resolve_generation_params(model, {
                "width": width,
                "height": height,
                "frames": frames,
                "steps": steps,
                "fps": fps,
            })
            request_data = {
                "model": model,
                **params,
            }
            # seed not required for video price calc
            request_data.pop("seed", None)
            # guidance not required for video price calc
            request_data.pop("guidance", None)

            price_response = await client.calculate_price(
                endpoint="img2video/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_video_price(
    model: Annotated[str, Field(description="Video generation model name")],
    width: Annotated[Optional[int], Field(ge=64, le=2048, description="Video width in pixels")] = None,
    height: Annotated[Optional[int], Field(ge=64, le=2048, description="Video height in pixels")] = None,
    frames: Annotated[Optional[int], Field(ge=1, le=200, description="Number of video frames")] = None,
    steps: Annotated[Optional[int], Field(ge=1, le=100, description="Number of inference steps")] = None,
    fps: Annotated[Optional[int], Field(ge=1, le=60, description="Frames per second (optional)")] = None,
) -> dict:
    """Calculate price for text-to-video generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            params = resolve_generation_params(model, {
                "width": width,
                "height": height,
                "frames": frames,
                "steps": steps,
                "fps": fps,
            })
            request_data = {
                "model": model,
                **params,
            }
            # seed not required for video price calc
            request_data.pop("seed", None)
            # guidance not required for video price calc
            request_data.pop("guidance", None)

            price_response = await client.calculate_price(
                endpoint="txt2video/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_remove_background(
    video: Annotated[str, Field(description="Video as URL, data URI (data:video/mp4;base64,...), or base64 string. URLs are recommended to avoid base64 context bloat.")],
    model: Annotated[str, Field(description="Video background removal model name")],
) -> dict:
    """Remove background from a video.

    Takes a video and removes its background. Accepts videos as:
    - URLs (recommended to avoid base64 bloat)
    - Data URIs (from attachments)
    - Base64 strings

    Returns:
        dict: Contains 'success', 'result_url' with processed video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            field_name, file_tuple = await prepare_video_upload_async(video, "video")

            form_data = {
                "model": model,
            }

            job_response = await client.submit_job(
                endpoint="vid-rmbg",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
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
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_remove_background_price(
    model: Annotated[str, Field(description="Video background removal model name")],
    width: Annotated[Optional[int], Field(ge=1, le=10240, description="Video width in pixels (optional)")] = None,
    height: Annotated[Optional[int], Field(ge=1, le=10240, description="Video height in pixels (optional)")] = None,
) -> dict:
    """Calculate price for video background removal.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "model": model,
            }

            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)

            price_response = await client.calculate_price(
                endpoint="vid-rmbg/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_upscale(
    video: Annotated[str, Field(description="Video as URL, data URI (data:video/mp4;base64,...), or base64 string. URLs are recommended to avoid base64 context bloat.")],
    model: Annotated[str, Field(description="Video upscaling model name")],
) -> dict:
    """Upscale a video to higher resolution.

    Takes a video and upscales it using AI models for enhanced quality.
    Accepts videos as:
    - URLs (recommended to avoid base64 bloat)
    - Data URIs (from attachments)
    - Base64 strings

    Returns:
        dict: Contains 'success', 'result_url' with upscaled video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            field_name, file_tuple = await prepare_video_upload_async(video, "video")

            form_data = {
                "model": model,
            }

            job_response = await client.submit_job(
                endpoint="vid-upscale",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
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
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_upscale_price(
    model: Annotated[str, Field(description="Video upscaling model name")],
    width: Annotated[Optional[int], Field(ge=1, le=10240, description="Video width in pixels (optional)")] = None,
    height: Annotated[Optional[int], Field(ge=1, le=10240, description="Video height in pixels (optional)")] = None,
) -> dict:
    """Calculate price for video upscaling.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "model": model,
            }

            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)

            price_response = await client.calculate_price(
                endpoint="vid-upscale/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}