"""Image processing tools for deAPI MCP server."""

from typing import Annotated, Any, Dict, Optional

from pydantic import Field

from ..deapi_client import DeapiAPIError, get_client
from ..middleware import ensure_model_cache
from ..polling_manager import PollingManager
from ..utils import InputError, form_bool, prepare_image_upload_async
from ._price_helpers import resolve_from_card, resolve_generation_params
from ._responses import build_job_response, build_price_response, input_error_response


def _lora_form_fields(loras: list[Any]) -> Dict[str, str]:
    """LoRAs as bracketed multipart keys, which Laravel parses into an array.

    The edit endpoints validate `loras` as an array (`loras.*.name`,
    `loras.*.weight`); a JSON string in one form field is a 422.
    """
    fields: Dict[str, str] = {}
    for i, lora in enumerate(loras):
        fields[f"loras[{i}][name]"] = str(lora["name"])
        fields[f"loras[{i}][weight]"] = str(lora.get("weight", 1))
    return fields


async def _image_or_size(
    image: Optional[str], width: Optional[int], height: Optional[int], form_data: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Fill a size-priced quote from an image upload or from width and height.

    The API reads the size from the file when there is one and requires both
    dimensions otherwise. Returns the `files` to send, or None.
    """
    if image:
        field_name, file_tuple = await prepare_image_upload_async(image, "image")
        return {field_name: file_tuple}
    if width is None or height is None:
        raise InputError("pass the image, or its width and height: the price follows its size")
    form_data["width"] = str(width)
    form_data["height"] = str(height)
    return None


async def text_to_image(
    prompt: Annotated[str, Field(description="Text description of the image you want to generate")],
    model: Annotated[
        str, Field(description="AI model name (e.g., 'Flux1schnell', 'ZImageTurbo_INT8')")
    ],
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude from the image (optional)")
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Image width in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Image height in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of diffusion steps. Defaults to the model's own value (see the model list); each model has its own range and some fix it",
        ),
    ] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="How closely to follow the prompt. Leave unset to use the model's own default",
        ),
    ] = None,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    quality: Annotated[
        Optional[str],
        Field(
            description="Quality tier for models that price by tier, e.g. 'low', 'medium', 'high' on gpt-image-1.5. Check the model card; costs vary sharply between tiers"
        ),
    ] = None,
    loras: Annotated[
        Optional[list[Any]],
        Field(
            description="LoRA adapters to apply: [{'name': str, 'weight': float}]. Only for models that list them"
        ),
    ] = None,
    enhance_prompt: Annotated[
        Optional[bool], Field(description="Let the API expand the prompt before generating")
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
) -> dict[str, Any]:
    """Generate image from text prompt using AI diffusion models.

    Creates images from text descriptions. Returns the generated image URL.

    Returns:
        dict: Contains 'success', 'result_url', 'job_id', and metadata
    """
    try:
        client = get_client()
        async with client:
            # Anything unset comes from the model card: the API requires these
            # fields and validates them against the card, and no fixed number
            # satisfies every model. The API field is `guidance`;
            # `guidance_scale` is only this tool's parameter name.
            await ensure_model_cache(model=model)
            request_data = {
                "prompt": prompt,
                "model": model,
                **resolve_from_card(
                    model,
                    {"width": width, "height": height, "steps": steps, "guidance": guidance_scale},
                ),
                "seed": seed,
            }

            if negative_prompt:
                request_data["negative_prompt"] = negative_prompt

            if quality:
                request_data["quality"] = quality

            if loras:
                request_data["loras"] = loras

            if enhance_prompt is not None:
                request_data["enhance_prompt"] = enhance_prompt

            job_response = await client.submit_job(
                endpoint="images/generations",
                json_data=request_data,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            # Poll for completion
            polling_manager = PollingManager(client, job_type="image")
            result = await polling_manager.poll_until_complete(job_id)

            # Carries prompt_boost when enhance_prompt rewrote the prompt.
            return build_job_response(result, job_id)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_image(
    prompt: Annotated[str, Field(description="Text description of desired transformation")],
    model: Annotated[str, Field(description="AI model name for image transformation")],
    image: Annotated[
        Optional[str],
        Field(
            description="Source image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. IMPORTANT: Pass the image directly without displaying or printing the base64 data."
        ),
    ] = None,
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude from the transformed image (optional)")
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of diffusion steps. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="How closely to follow the prompt. Leave unset to use the model's own default",
        ),
    ] = None,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    loras: Annotated[
        Optional[list[Any]],
        Field(
            description="List of LoRA models (optional, e.g., [{'name': 'style_lora', 'weight': 0.75}])"
        ),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Output width in pixels. Defaults to the first input image's size; ignored by models that keep the input size (Qwen, gpt-image-1.5/2)",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Output height in pixels. Defaults to the first input image's size; ignored by models that keep the input size (Qwen, gpt-image-1.5/2)",
        ),
    ] = None,
    quality: Annotated[
        Optional[str],
        Field(
            description="Quality tier for models that price by tier, e.g. 'low', 'medium', 'high'. Costs vary sharply between tiers"
        ),
    ] = None,
    enhance_prompt: Annotated[
        Optional[bool], Field(description="Let the API expand the prompt before generating")
    ] = None,
    images: Annotated[
        Optional[list[Any]],
        Field(
            description="Several source images to edit together, each a URL, data URI or base64 string. How many a model takes is 'up to N input image(s)' in the model list (the API caps it at 5). Mutually exclusive with 'image'"
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
) -> dict[str, Any]:
    """Transform an existing image using a text prompt.

    Modifies images based on text descriptions. Useful for style transfer and editing.
    Accepts images as URLs (preferred), data URIs (data:image/...;base64,...), or base64 strings.
    When a URL is available (e.g., from a previous text_to_image result), always prefer passing the URL directly.

    Pass either `image` for a single source or `images` for up to five; the API
    rejects a request carrying both.

    Returns:
        dict: Contains 'success', 'result_url', 'job_id', and metadata
    """
    try:
        client = get_client()
        async with client:
            if images and image:
                return input_error_response(
                    "Pass either 'image' or 'images', not both — the API rejects requests carrying both."
                )
            if not images and not image:
                return input_error_response(
                    "A source image is required: pass 'image' for one, or 'images' for up to five."
                )

            # The API field is `guidance`; `guidance_scale` is only this tool's
            # parameter name. Width and height stay optional: unset, the output
            # keeps the source image's size.
            await ensure_model_cache(model=model)
            card = resolve_from_card(model, {"steps": steps, "guidance": guidance_scale})

            # Repeated `images[]` parts are what Laravel parses back into an array.
            if images:
                upload_files = [
                    ("images[]", (await prepare_image_upload_async(item, "images"))[1])
                    for item in images
                ]
            else:
                assert image  # guarded above: one of image / images is set
                field_name, file_tuple = await prepare_image_upload_async(image, "image")
                upload_files = [(field_name, file_tuple)]

            # Prepare form data (all other parameters)
            form_data = {
                "prompt": prompt,
                "model": model,
                **{key: str(value) for key, value in card.items()},
                "seed": str(seed),
            }

            # Add optional parameters
            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt
            if loras:
                form_data.update(_lora_form_fields(loras))
            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)
            if quality:
                form_data["quality"] = quality
            if enhance_prompt is not None:
                form_data["enhance_prompt"] = form_bool(enhance_prompt)

            job_response = await client.submit_job(
                endpoint="images/edits",
                data=form_data,
                files=upload_files,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
            result = await polling_manager.poll_until_complete(job_id)

            # Carries prompt_boost when enhance_prompt rewrote the prompt.
            return build_job_response(result, job_id)

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
        Field(description="Language code for OCR (e.g., 'en', 'es', 'fr') - optional"),
    ] = None,
    format: Annotated[str, Field(description="Output format: 'text' or 'json'")] = "text",
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return the extracted text inline in 'result'. Set to False to get only a download URL"
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
) -> dict[str, Any]:
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
                "return_result_in_response": form_bool(return_result_in_response),
            }

            if language:
                form_data["language"] = language

            job_response = await client.submit_job(
                endpoint="images/ocr",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
            result = await polling_manager.poll_until_complete(job_id)

            # Inline by default, as for transcripts: a chat client often cannot
            # fetch result_url, and an OCR'd page is small. With False the text
            # goes to a file and only result_url carries it.
            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_image_price(
    prompt: Annotated[str, Field(description="Text description for price calculation")],
    model: Annotated[str, Field(description="AI model name")],
    width: Annotated[
        Optional[int],
        Field(ge=1, description="Image width in pixels. Limits per model (see the model list)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(ge=1, description="Image height in pixels. Limits per model (see the model list)"),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(ge=1, description="Number of diffusion steps. Limits per model (see the model list)"),
    ] = None,
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude — quote it if the job will use it")
    ] = None,
    seed: Annotated[Optional[int], Field(description="Random seed the job will use")] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="Guidance the job will use. Defaults to the model's own value; limits per model (see the model list)",
        ),
    ] = None,
    quality: Annotated[
        Optional[str],
        Field(
            description="Quality tier, e.g. 'low', 'medium', 'high'. On gpt-image-1.5 the tiers differ ~14x in price, so quoting without it can be badly wrong"
        ),
    ] = None,
    loras: Annotated[
        Optional[list[Any]],
        Field(description="LoRA adapters the job will use: [{'name': str, 'weight': float}]"),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for text-to-image generation.

    Pass the same parameters the generation call will use. Fields that affect
    the charge — `quality` above all — change the quote, so omitting them here
    produces a number the job will not honour.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            # Omit rather than pass None: resolve_generation_params reads seed with
            # a .get default, so an explicit None would override it and send a null
            # seed, which the API rejects.
            user_params: Dict[str, Any] = {"width": width, "height": height, "steps": steps}
            if seed is not None:
                user_params["seed"] = seed
            if guidance_scale is not None:
                user_params["guidance"] = guidance_scale

            await ensure_model_cache(model=model)
            params = resolve_generation_params(model, user_params)
            request_data = {
                "prompt": prompt,
                "model": model,
                **params,
            }

            if negative_prompt:
                request_data["negative_prompt"] = negative_prompt
            if quality:
                request_data["quality"] = quality
            if loras:
                request_data["loras"] = loras

            price_response = await client.calculate_price(
                endpoint="images/generations/price",
                json_data=request_data,
            )

            return build_price_response(price_response)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_image_price(
    prompt: Annotated[str, Field(description="Transformation description")],
    model: Annotated[str, Field(description="AI model name")],
    image: Annotated[
        Optional[str],
        Field(
            description="Source image (URL, data URI or base64). Some models price by its size, so pass the one the job will use"
        ),
    ] = None,
    images: Annotated[
        Optional[list[Any]],
        Field(
            description="Several source images, as the job will send them. Mutually exclusive with 'image'"
        ),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(ge=1, description="Number of diffusion steps. Limits per model (see the model list)"),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1, description="Output width the job will use. Limits per model (see the model list)"
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Output height the job will use. Limits per model (see the model list)",
        ),
    ] = None,
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude — quote it if the job will use it")
    ] = None,
    seed: Annotated[Optional[int], Field(description="Random seed the job will use")] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="Guidance the job will use. Defaults to the model's own value; limits per model (see the model list)",
        ),
    ] = None,
    quality: Annotated[
        Optional[str],
        Field(
            description="Quality tier, e.g. 'low', 'medium', 'high'. Tiers differ sharply in price, so omitting it can misquote badly"
        ),
    ] = None,
    loras: Annotated[
        Optional[list[Any]],
        Field(description="LoRA adapters the job will use: [{'name': str, 'weight': float}]"),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for image-to-image transformation.

    Pass the same parameters and source image(s) the edit call will use:
    `quality` and the input size both change the charge on some models, so a
    quote without them may not match the job.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            if image and images:
                return input_error_response(
                    "Pass either 'image' or 'images', not both — the API rejects requests carrying both."
                )

            # Output size is optional here and the API prices the input image, so
            # width/height go only when given; a guessed 512 is below some models' minimum.
            await ensure_model_cache(model=model)
            card = resolve_from_card(model, {"steps": steps, "guidance": guidance_scale})
            form_data = {
                "prompt": prompt,
                "model": model,
                **{key: str(value) for key, value in card.items()},
                "seed": str(seed if seed is not None else -1),
            }
            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)
            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt
            if quality:
                form_data["quality"] = quality
            if loras:
                form_data.update(_lora_form_fields(loras))

            files = None
            if images:
                files = [
                    ("images[]", (await prepare_image_upload_async(item, "images"))[1])
                    for item in images
                ]
            elif image:
                field_name, file_tuple = await prepare_image_upload_async(image, "image")
                files = [(field_name, file_tuple)]

            price_response = await client.calculate_price(
                endpoint="images/edits/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_text_price(
    model: Annotated[str, Field(description="OCR model name")],
    image: Annotated[
        Optional[str],
        Field(description="Image to quote (URL, data URI or base64); its size is read from it"),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1, le=10240, description="Image height in pixels (required if image not provided)"
        ),
    ] = None,
    language: Annotated[Optional[str], Field(description="Language code (optional)")] = None,
) -> dict[str, Any]:
    """Calculate price for OCR text extraction.

    Pass the image, or its width and height.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {"model": model}

            files = await _image_or_size(image, width, height, form_data)
            if language:
                form_data["language"] = language

            price_response = await client.calculate_price(
                endpoint="images/ocr/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid input: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_remove_background(
    image: Annotated[
        str,
        Field(
            description="Image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining tools to avoid base64 context bloat. Supported formats: JPG, JPEG, PNG, GIF, BMP, WebP. Max 10MB."
        ),
    ],
    model: Annotated[str, Field(description="Background removal model (e.g., 'Ben2')")],
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
) -> dict[str, Any]:
    """Remove background from an image.

    Takes an image and removes its background, returning a transparent PNG.
    Accepts images as:
    - Public URLs (recommended: chains with text_to_image, and the ONLY way in for a
      file of your own - chat clients do not pass files through to tools)
    - Data URIs or raw base64 (for callers that already hold the bytes)

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
                endpoint="images/background-removals",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_remove_background_price(
    model: Annotated[str, Field(description="Background removal model name")],
    image: Annotated[
        Optional[str],
        Field(description="Image to quote (URL, data URI or base64); its size is read from it"),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1, le=10240, description="Image height in pixels (required if image not provided)"
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for image background removal.

    Pass the image, or its width and height.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "model": model,
            }

            files = await _image_or_size(image, width, height, form_data)

            price_response = await client.calculate_price(
                endpoint="images/background-removals/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid input: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_upscale(
    image: Annotated[
        str,
        Field(
            description="Image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining tools to avoid base64 context bloat. Supported formats: JPG, JPEG, PNG, GIF, BMP, WebP. Max 10MB."
        ),
    ],
    model: Annotated[str, Field(description="Upscaling model (e.g., 'RealESRGAN_x4')")],
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
) -> dict[str, Any]:
    """Upscale an image to higher resolution.

    Takes an image and upscales it using AI models for enhanced quality.
    Accepts images as:
    - Public URLs (recommended: chains with text_to_image, and the ONLY way in for a
      file of your own - chat clients do not pass files through to tools)
    - Data URIs or raw base64 (for callers that already hold the bytes)

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
                endpoint="images/upscales",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="image")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid image format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_upscale_price(
    model: Annotated[str, Field(description="Upscaling model name")],
    image: Annotated[
        Optional[str],
        Field(description="Image to quote (URL, data URI or base64); its size is read from it"),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(ge=1, le=10240, description="Image width in pixels (required if image not provided)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1, le=10240, description="Image height in pixels (required if image not provided)"
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for image upscaling.

    Pass the image, or its width and height.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            form_data = {
                "model": model,
            }

            files = await _image_or_size(image, width, height, form_data)

            price_response = await client.calculate_price(
                endpoint="images/upscales/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid input: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
