"""Prompt enhancement tools for deAPI MCP server.

Synchronous endpoints (no job polling) — the deAPI server returns the
enhanced prompt directly in the response body.
"""

from typing import Annotated, Literal, Optional

from pydantic import Field

from ..deapi_client import get_client, DeapiAPIError
from ..utils import prepare_image_upload_async


PromptBoosterType = Literal[
    "images.generations",
    "images.edits",
    "images.upscales",
    "images.background-removals",
    "images.ocr",
    "videos.generations",
    "videos.animations",
    "videos.upscales",
    "videos.background-removals",
    "videos.transcriptions",
    "audio.speech",
    "audio.music",
    "audio.transcriptions",
    "embeddings",
]


async def prompt_booster(
    prompt: Annotated[str, Field(min_length=3, description="The prompt to enhance.")],
    type: Annotated[PromptBoosterType, Field(description="Inference type the enhanced prompt is intended for, in v2 dot notation (e.g., 'images.generations', 'videos.animations', 'audio.speech').")],
    model_slug: Annotated[str, Field(description="Slug of the target deAPI model the enhanced prompt will be used with (e.g., 'Flux1schnell'). The booster picks a guide that matches this model + type.")],
    negative_prompt: Annotated[Optional[str], Field(min_length=3, description="Optional negative prompt to enhance alongside the main prompt.")] = None,
    image: Annotated[Optional[str], Field(description="Optional reference image as URL, data URI, or base64. Required for type='images.edits' and 'videos.animations'. Supported formats: JPEG, PNG, BMP, GIF, WebP.")] = None,
) -> dict:
    """Enhance a prompt using AI guides tailored to a specific deAPI model + inference type.

    Synchronous — returns the enhanced prompt(s) directly. Charged per call;
    use `prompt_booster_price` first if you want a quote.

    Returns:
        dict: Contains 'success', 'prompt' (enhanced), and 'negative_prompt' (enhanced, if applicable).
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "prompt": prompt,
                "type": type,
                "model_slug": model_slug,
            }
            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt

            files = None
            if image:
                field_name, file_tuple = await prepare_image_upload_async(image, "image")
                files = {field_name: file_tuple}

            response = await client.post_sync(
                endpoint="prompts/enhancements",
                data=form_data,
                files=files,
            )

            return {
                "success": True,
                "prompt": response.get("prompt"),
                "negative_prompt": response.get("negative_prompt"),
            }

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def prompt_booster_price(
    prompt: Annotated[str, Field(min_length=3, description="The prompt to enhance.")],
    type: Annotated[PromptBoosterType, Field(description="Inference type in v2 dot notation (e.g., 'images.generations').")],
    model_slug: Annotated[str, Field(description="Slug of the target deAPI model.")],
    negative_prompt: Annotated[Optional[str], Field(min_length=3, description="Optional negative prompt.")] = None,
    image: Annotated[Optional[str], Field(description="Optional reference image (URL, data URI, or base64) — relevant for image-based types.")] = None,
) -> dict:
    """Calculate the price for a prompt-booster call.

    Returns:
        dict: Contains 'success' and 'price'.
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "prompt": prompt,
                "type": type,
                "model_slug": model_slug,
            }
            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt

            files = None
            if image:
                field_name, file_tuple = await prepare_image_upload_async(image, "image")
                files = {field_name: file_tuple}

            price_response = await client.calculate_price(
                endpoint="prompts/enhancements/price",
                data=form_data,
                files=files,
            )

            return {"success": True, "price": price_response.get("price")}

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
