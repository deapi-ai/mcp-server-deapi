"""Image processing tools for deAPI MCP server."""

import json
from typing import Annotated, Optional

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..polling_manager import PollingManager
from ..utils import prepare_image_upload_async
from ._price_helpers import resolve_generation_params


async def text_to_image(
    prompt: Annotated[str, Field(description="Text description of the image you want to generate")],
    model: Annotated[str, Field(description="AI model name (e.g., 'stable-diffusion-xl', 'flux-dev')")],
    negative_prompt: Annotated[
        Optional[str],
        Field(description="Things to exclude from the image (optional)")
    ] = None,
    width: Annotated[int, Field(ge=64, le=2048, description="Image width in pixels (64-2048)")] = 512,
    height: Annotated[int, Field(ge=64, le=2048, description="Image height in pixels (64-2048)")] = 512,
    steps: Annotated[int, Field(ge=1, le=100, description="Number of diffusion steps (higher = better quality)")] = 20,
    guidance_scale: Annotated[float, Field(ge=0.0, le=20.0, description="How closely to follow the prompt (0-20). Check model's features.supports_guidance - if 0, use guidance_scale=0")] = 7.5,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    return_result_in_response: Annotated[bool, Field(description="Request immediate response instead of polling")] = False,
) -> dict:
    """Generate image from text prompt using AI diffusion models.

    Creates images from text descriptions. Returns the generated image URL.

    Returns:
        dict: Contains 'success', 'result_url', 'job_id', and metadata
    """
    try:
        client = get_client()
        async with client:
            request_data = {
                "prompt": prompt,
                "model": model,
                "width": width,
                "height": height,
                "steps": steps,
                "guidance_scale": guidance_scale,
                "seed": seed,
                "return_result_in_response": return_result_in_response,
            }

            if negative_prompt:
                request_data["negative_prompt"] = negative_prompt

            job_response = await client.submit_job(
                endpoint="txt2img",
                json_data=request_data,
            )
            job_id = job_response.data.request_id

            # Poll for completion
            polling_manager = PollingManager(client, job_type="image")
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


async def image_to_image(
    image: Annotated[str, Field(description="Source image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. IMPORTANT: Pass the image directly without displaying or printing the base64 data.")],
    prompt: Annotated[str, Field(description="Text description of desired transformation")],
    model: Annotated[str, Field(description="AI model name for image transformation")],
    negative_prompt: Annotated[
        Optional[str],
        Field(description="Things to exclude from the transformed image (optional)")
    ] = None,
    strength: Annotated[
        float,
        Field(ge=0.0, le=1.0, description="Transformation strength (0.0-1.0, higher = more change)")
    ] = 0.8,
    steps: Annotated[int, Field(ge=1, le=100, description="Number of diffusion steps")] = 20,
    guidance_scale: Annotated[float, Field(ge=0.0, le=20.0, description="How closely to follow the prompt (0-20). Check model's features.supports_guidance - if 0, use guidance_scale=0")] = 7.5,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    loras: Annotated[Optional[list], Field(description="List of LoRA models (optional, e.g., [{'name': 'style_lora', 'weight': 0.75}])")] = None,
    return_result_in_response: Annotated[bool, Field(description="Request immediate response")] = False,
) -> dict:
    """Transform an existing image using a text prompt.

    Modifies images based on text descriptions. Useful for style transfer and editing.
    Accepts images as data URIs (from Claude Desktop attachments) or base64 strings.

    Returns:
        dict: Contains 'success', 'result_url', 'job_id', and metadata
    """
    try:
        client = get_client()
        async with client:
            # Prepare image file upload (async version supports URLs)
            field_name, file_tuple = await prepare_image_upload_async(image, "image")

            # Prepare form data (all other parameters)
            form_data = {
                "prompt": prompt,
                "model": model,
                "steps": str(steps),
                "seed": str(seed),
            }

            # Add optional parameters
            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt
            if guidance_scale is not None:
                form_data["guidance"] = str(guidance_scale)
            if strength is not None:
                form_data["strength"] = str(strength)
            if loras:
                form_data["loras"] = json.dumps(loras)

            job_response = await client.submit_job(
                endpoint="img2img",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
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


async def image_to_text(
    image: Annotated[str, Field(description="Image file (base64 encoded or URL)")],
    model: Annotated[str, Field(description="OCR model (e.g., 'Nanonets_Ocr_S_F16')")],
    language: Annotated[
        Optional[str],
        Field(description="Language code for OCR (e.g., 'en', 'es', 'fr') - optional")
    ] = None,
    format: Annotated[str, Field(description="Output format: 'text' or 'json'")] = "text",
    return_result_in_response: Annotated[bool, Field(description="Request immediate response")] = False,
) -> dict:
    """Extract text from image using OCR (Optical Character Recognition).

    Reads text from images like documents, screenshots, signs, etc.

    Returns:
        dict: Contains 'success', 'result' with extracted text, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare image file for multipart upload
            field_name, file_tuple = await prepare_image_upload_async(image, "image")

            # Prepare form data (non-file parameters)
            form_data = {
                "model": model,
                "format": format,
                "return_result_in_response": str(return_result_in_response).lower(),
            }

            if language:
                form_data["language"] = language

            job_response = await client.submit_job(
                endpoint="img2txt",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
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
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_image_price(
    prompt: Annotated[str, Field(description="Text description for price calculation")],
    model: Annotated[str, Field(description="AI model name")],
    width: Annotated[Optional[int], Field(ge=64, le=2048, description="Image width in pixels")] = None,
    height: Annotated[Optional[int], Field(ge=64, le=2048, description="Image height in pixels")] = None,
    steps: Annotated[Optional[int], Field(ge=1, le=100, description="Number of diffusion steps")] = None,
) -> dict:
    """Calculate price for text-to-image generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            params = resolve_generation_params(model, {
                "width": width,
                "height": height,
                "steps": steps,
            })
            request_data = {
                "prompt": prompt,
                "model": model,
                **params,
            }

            price_response = await client.calculate_price(
                endpoint="txt2img/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_image_price(
    image: Annotated[str, Field(description="Source image (base64 or URL)")],
    prompt: Annotated[str, Field(description="Transformation description")],
    model: Annotated[str, Field(description="AI model name")],
    steps: Annotated[Optional[int], Field(ge=1, le=100, description="Number of diffusion steps")] = None,
    strength: Annotated[float, Field(ge=0.0, le=1.0, description="Transformation strength")] = 0.8,
) -> dict:
    """Calculate price for image-to-image transformation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            params = resolve_generation_params(model, {"steps": steps})
            request_data = {
                "prompt": prompt,
                "model": model,
                **params,
            }

            price_response = await client.calculate_price(
                endpoint="img2img/price-calculation",
                json_data=request_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_text_price(
    model: Annotated[str, Field(description="OCR model name")],
    width: Annotated[Optional[int], Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)")] = None,
    height: Annotated[Optional[int], Field(ge=1, le=10240, description="Image height in pixels (required if image not provided)")] = None,
    language: Annotated[Optional[str], Field(description="Language code (optional)")] = None,
) -> dict:
    """Calculate price for OCR text extraction.

    Provide width and height for price calculation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {"model": model}

            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)
            if language:
                form_data["language"] = language

            price_response = await client.calculate_price(
                endpoint="img2txt/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_remove_background(
    image: Annotated[str, Field(description="Image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining tools to avoid base64 context bloat. Supported formats: JPG, JPEG, PNG, GIF, BMP, WebP. Max 10MB.")],
    model: Annotated[str, Field(description="Background removal model (e.g., 'RMBG-1.4')")],
) -> dict:
    """Remove background from an image.

    Takes an image and removes its background, returning a transparent PNG.
    Accepts images as:
    - URLs (recommended for chaining with text_to_image - avoids base64 bloat)
    - Data URIs (from Claude Desktop attachments)
    - Base64 strings

    Returns:
        dict: Contains 'success', 'result_url' with processed image URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare image file upload (async version supports URLs)
            field_name, file_tuple = await prepare_image_upload_async(image, "image")

            # Prepare form data
            form_data = {
                "model": model,
            }

            job_response = await client.submit_job(
                endpoint="img-rmbg",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
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


async def image_remove_background_price(
    model: Annotated[str, Field(description="Background removal model name")],
    width: Annotated[Optional[int], Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)")] = None,
    height: Annotated[Optional[int], Field(ge=1, le=10240, description="Image height in pixels (required if image not provided)")] = None,
) -> dict:
    """Calculate price for image background removal.

    Provide width and height for price calculation.

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
                endpoint="img-rmbg/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_upscale(
    image: Annotated[str, Field(description="Image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining tools to avoid base64 context bloat. Supported formats: JPG, JPEG, PNG, GIF, BMP, WebP. Max 10MB.")],
    model: Annotated[str, Field(description="Upscaling model (e.g., 'RealESRGAN_x4plus')")],
) -> dict:
    """Upscale an image to higher resolution.

    Takes an image and upscales it using AI models for enhanced quality.
    Accepts images as:
    - URLs (recommended for chaining with text_to_image - avoids base64 bloat)
    - Data URIs (from Claude Desktop attachments)
    - Base64 strings

    Returns:
        dict: Contains 'success', 'result_url' with upscaled image URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare image file upload (async version supports URLs)
            field_name, file_tuple = await prepare_image_upload_async(image, "image")

            # Prepare form data
            form_data = {
                "model": model,
            }

            job_response = await client.submit_job(
                endpoint="img-upscale",
                data=form_data,
                files={field_name: file_tuple},
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
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


async def image_upscale_price(
    model: Annotated[str, Field(description="Upscaling model name")],
    width: Annotated[Optional[int], Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)")] = None,
    height: Annotated[Optional[int], Field(ge=1, le=10240, description="Image height in pixels (required if image not provided)")] = None,
) -> dict:
    """Calculate price for image upscaling.

    Provide width and height for price calculation.

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
                endpoint="img-upscale/price-calculation",
                data=form_data,
            )

            return {"success": True, "price": price_response.get("data", {})}

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}