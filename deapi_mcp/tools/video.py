"""Video generation tools for deAPI MCP server."""

from typing import Annotated, Any, List, Literal, Optional

from pydantic import Field

from ..deapi_client import DeapiAPIError, get_client
from ..middleware import ensure_model_cache
from ..polling_manager import PollingManager
from ..utils import (
    form_bool,
    prepare_audio_upload_async,
    prepare_image_upload_async,
    prepare_video_upload_async,
)
from ._price_helpers import (
    feature_enabled,
    resolve_from_card,
    resolve_generation_params,
    resolve_upscale_factor,
)
from ._responses import build_job_response, build_price_response, input_error_response


async def image_to_video(
    prompt: Annotated[str, Field(description="Text prompt for video generation")],
    model: Annotated[str, Field(description="Video generation model name")],
    first_frame_image: Annotated[
        Optional[str],
        Field(
            description="First frame image as URL (e.g., from text_to_image result), data URI (data:image/png;base64,...), or base64 string. URLs are recommended when chaining from text_to_image to avoid base64 context bloat. Required unless ref_images or ref_videos are given; the two modes are mutually exclusive"
        ),
    ] = None,
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude (optional)")
    ] = None,
    last_frame_image: Annotated[
        Optional[str],
        Field(
            description="Last frame image (optional, URL, data URI, or base64). Not allowed with reference inputs"
        ),
    ] = None,
    ref_images: Annotated[
        Optional[List[str]],
        Field(
            description="Reference images (URL, data URI or base64), in order: ref_images[0] is <Picture 1> in the prompt, ref_images[1] is <Picture 2>, and so on. Each within a 1:4-4:1 aspect ratio. Only for models listing image refs in the model list; replaces first_frame_image"
        ),
    ] = None,
    ref_videos: Annotated[
        Optional[List[str]],
        Field(
            description="Reference videos (URL, data URI or base64), in order: ref_videos[0] is <Video 1>. Usually at most one, a few seconds long (see the model list). A video with a soundtrack takes <Audio 1> and shifts every ref_audios label by one"
        ),
    ] = None,
    ref_audios: Annotated[
        Optional[List[str]],
        Field(
            description="Reference audio clips (URL, data URI or base64), in order: ref_audios[0] is <Audio 1> (or <Audio 2> after a sounded reference video). Needs at least one ref_images or ref_videos entry"
        ),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video width in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video height in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of video frames. Defaults to the model's own value (see the model list); each model has its own range",
        ),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Frames per second. Defaults to the model's own value (see the model list); most models fix it",
        ),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of inference steps. Defaults to the model's own value (see the model list); ignored by models that do not support steps",
        ),
    ] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="Guidance scale. Leave unset to use the model's own default; no model in the catalogue currently supports guidance",
        ),
    ] = None,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    enhance_prompt: Annotated[
        Optional[bool],
        Field(
            description="Let the API rewrite the prompt before generating. Only on models with a prompt-booster guide (422 otherwise); its fee is charged on the job and is not in the /price quote"
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
    """Generate video from static image(s), or from reference images, videos and audio.

    Two modes, mutually exclusive:
    - Keyframes: first_frame_image (and optionally last_frame_image) is animated.
    - References: ref_images / ref_videos / ref_audios, on models that support them
      (the model list shows "refs: ..." for those). The prompt addresses each reference
      by its tag: <Picture 1>, <Picture 2> for images, <Video 1> for a video, <Audio 1>
      for audio, numbered from 1 within each type in the order given. Order matters:
      swapping two images swaps what the tags refer to. Example prompt:
      "<Picture 1> walks into the room from <Picture 2>, speaking with the voice of <Audio 1>".

    Accepts files as:
    - Public URLs (recommended: chains with text_to_image, and the ONLY way in for a
      file of your own - chat clients do not pass files through to tools)
    - Data URIs or raw base64 (for callers that already hold the bytes)

    IMPORTANT: Check model specifications using get_available_models() before calling.
    Pay attention to limits for width/height/frames/fps and, in reference mode, the
    max_ref_* counts and durations.

    Returns:
        dict: Contains 'success', 'result_url' with video URL, 'job_id'
    """
    has_refs = bool(ref_images or ref_videos or ref_audios)
    if has_refs and (first_frame_image or last_frame_image):
        return input_error_response(
            "Pass either first_frame_image/last_frame_image or ref_images/ref_videos/ref_audios, not both: the API rejects requests that mix the two modes."
        )
    if ref_audios and not (ref_images or ref_videos):
        return input_error_response("ref_audios needs at least one ref_images or ref_videos entry.")
    if not first_frame_image and not (ref_images or ref_videos):
        return input_error_response(
            "A source is required: pass first_frame_image, or ref_images/ref_videos on a model that supports references."
        )

    try:
        client = get_client()
        async with client:
            # A list of (field, file) pairs: repeated `ref_images[]` parts are what
            # Laravel parses back into an array, in the order sent — and that order
            # is what binds each file to its <Picture N> / <Video N> / <Audio N> tag.
            files = []
            if first_frame_image:
                files.append(
                    await prepare_image_upload_async(first_frame_image, "first_frame_image")
                )
            if last_frame_image:
                files.append(await prepare_image_upload_async(last_frame_image, "last_frame_image"))
            for item in ref_images or []:
                files.append(("ref_images[]", (await prepare_image_upload_async(item))[1]))
            for item in ref_videos or []:
                files.append(("ref_videos[]", (await prepare_video_upload_async(item))[1]))
            for item in ref_audios or []:
                files.append(("ref_audios[]", (await prepare_audio_upload_async(item))[1]))

            # Anything unset comes from the model card: the API requires these
            # fields and validates them against the card, and no fixed number
            # satisfies every model (fps alone is 30 on Ltxv and 24 on Ltx2).
            await ensure_model_cache(model=model)
            card = resolve_from_card(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "fps": fps,
                    "steps": steps,
                    "guidance": guidance_scale,
                },
            )
            form_data = {
                "prompt": prompt,
                "model": model,
                **{key: str(value) for key, value in card.items()},
                "seed": str(seed),
            }

            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt
            if enhance_prompt is not None:
                form_data["enhance_prompt"] = form_bool(enhance_prompt)

            job_response = await client.submit_job(
                endpoint="videos/animations",
                data=form_data,
                files=files,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid file format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_video(
    prompt: Annotated[str, Field(description="Text prompt for video generation")],
    model: Annotated[str, Field(description="Video generation model name")],
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude (optional)")
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video width in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video height in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of video frames. Defaults to the model's own value (see the model list); each model has its own range",
        ),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Frames per second. Defaults to the model's own value (see the model list); most models fix it",
        ),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of inference steps. Defaults to the model's own value (see the model list); ignored by models that do not support steps",
        ),
    ] = None,
    guidance_scale: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="Guidance scale. Leave unset to use the model's own default; no model in the catalogue currently supports guidance",
        ),
    ] = None,
    seed: Annotated[int, Field(description="Random seed for reproducibility")] = -1,
    enhance_prompt: Annotated[
        Optional[bool],
        Field(
            description="Let the API rewrite the prompt before generating. Only on models with a prompt-booster guide (422 otherwise); its fee is charged on the job and is not in the /price quote"
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
    """Generate video from text prompt.

    Creates videos from text descriptions. Returns the generated video URL.

    IMPORTANT: Before calling this tool, use get_available_models() to check the model's
    specifications. Pay special attention to:
    - limits.max_steps/min_steps: Valid range for steps parameter
    - limits.max_fps/min_fps: Valid range for fps parameter
    - defaults: Recommended values for the model

    Returns:
        dict: Contains 'success', 'result_url' with video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Anything unset comes from the model card (see image_to_video).
            await ensure_model_cache(model=model)
            card = resolve_from_card(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "fps": fps,
                    "steps": steps,
                    "guidance": guidance_scale,
                },
            )
            # txt2video uses multipart/form-data
            request_data = {
                "prompt": prompt,
                "model": model,
                **{key: str(value) for key, value in card.items()},
                "seed": str(seed),
            }

            if negative_prompt:
                request_data["negative_prompt"] = negative_prompt
            if enhance_prompt is not None:
                request_data["enhance_prompt"] = form_bool(enhance_prompt)

            job_response = await client.submit_job(
                endpoint="videos/generations",
                data=request_data,  # Use data for form-data, not json_data
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def image_to_video_price(
    model: Annotated[str, Field(description="Video generation model name")],
    width: Annotated[
        Optional[int],
        Field(ge=1, description="Video width in pixels. Limits per model (see the model list)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(ge=1, description="Video height in pixels. Limits per model (see the model list)"),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(ge=1, description="Number of video frames. Limits per model (see the model list)"),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(ge=1, description="Number of inference steps. Limits per model (see the model list)"),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1, description="Frames per second (optional; limits per model, see the model list)"
        ),
    ] = None,
    ref_images_count: Annotated[
        Optional[int],
        Field(
            ge=0,
            description="How many ref_images the job will send. More than 2 doubles the price",
        ),
    ] = None,
    ref_videos_count: Annotated[
        Optional[int],
        Field(
            ge=0, description="How many ref_videos the job will send. Any video triples the price"
        ),
    ] = None,
    ref_audios_count: Annotated[
        Optional[int],
        Field(ge=0, description="How many ref_audios the job will send. No surcharge"),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for image-to-video generation.

    For reference mode, pass the number of reference files the job will carry
    (not the files themselves). Surcharges do not stack: the highest applies, so
    4 images plus a video is x3, not x6.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            await ensure_model_cache(model=model)
            params = resolve_generation_params(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "steps": steps,
                    "fps": fps,
                },
            )
            request_data = {
                "model": model,
                **params,
            }
            # seed not required for video price calc
            request_data.pop("seed", None)
            # Guidance is only required (and priced) on a model that turns it on;
            # every current card has it off, where the API drops it anyway.
            if not feature_enabled(model, "supports_guidance"):
                request_data.pop("guidance", None)
            for key, count in (
                ("ref_images_count", ref_images_count),
                ("ref_videos_count", ref_videos_count),
                ("ref_audios_count", ref_audios_count),
            ):
                if count:
                    request_data[key] = count

            price_response = await client.calculate_price(
                endpoint="videos/animations/price",
                json_data=request_data,
            )

            return build_price_response(price_response)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def text_to_video_price(
    model: Annotated[str, Field(description="Video generation model name")],
    width: Annotated[
        Optional[int],
        Field(ge=1, description="Video width in pixels. Limits per model (see the model list)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(ge=1, description="Video height in pixels. Limits per model (see the model list)"),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(ge=1, description="Number of video frames. Limits per model (see the model list)"),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(ge=1, description="Number of inference steps. Limits per model (see the model list)"),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1, description="Frames per second (optional; limits per model, see the model list)"
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for text-to-video generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            await ensure_model_cache(model=model)
            params = resolve_generation_params(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "steps": steps,
                    "fps": fps,
                },
            )
            request_data = {
                "model": model,
                **params,
            }
            # seed not required for video price calc
            request_data.pop("seed", None)
            # Guidance is only required (and priced) on a model that turns it on;
            # every current card has it off, where the API drops it anyway.
            if not feature_enabled(model, "supports_guidance"):
                request_data.pop("guidance", None)

            price_response = await client.calculate_price(
                endpoint="videos/generations/price",
                json_data=request_data,
            )

            return build_price_response(price_response)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_to_video(
    prompt: Annotated[str, Field(description="Text prompt for video generation")],
    model: Annotated[
        str,
        Field(description="Video generation model name (must support audio2video inference type)"),
    ],
    audio: Annotated[
        str,
        Field(
            description="Audio file as URL, data URI (data:audio/mpeg;base64,...), or base64 string. Conditions the video generation on the audio content"
        ),
    ],
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video width in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Video height in pixels. Defaults to the model's own value (see the model list)",
        ),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of video frames. Defaults to the model's own value (see the model list); each model has its own range",
        ),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Frames per second. Defaults to the model's own value (see the model list); most models fix it",
        ),
    ] = None,
    seed: Annotated[int, Field(description="Random seed (-1 for random)")] = -1,
    negative_prompt: Annotated[
        Optional[str], Field(description="Things to exclude (optional)")
    ] = None,
    guidance: Annotated[
        Optional[float],
        Field(
            ge=0.0,
            description="Guidance scale. Only used if model's features.supports_guidance is true. Limits per model (see the model list)",
        ),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Inference steps. Only used if model's features.supports_steps is true. Limits per model (see the model list)",
        ),
    ] = None,
    first_frame_image: Annotated[
        Optional[str],
        Field(description="First frame anchor image (URL, data URI, or base64, optional)"),
    ] = None,
    last_frame_image: Annotated[
        Optional[str],
        Field(description="Last frame anchor image (URL, data URI, or base64, optional)"),
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
    """Generate video from audio and text prompt.

    Creates videos conditioned on audio content. The audio drives the video generation,
    allowing synchronized audio-visual output. Optionally accepts anchor frame images.

    IMPORTANT: Check model specifications using get_available_models() before calling.
    Pay attention to features.supports_guidance, features.supports_steps, and model limits.

    Returns:
        dict: Contains 'success', 'result_url' with video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            # Prepare audio file upload (required)
            audio_field, audio_tuple = await prepare_audio_upload_async(audio, "audio")
            files = {audio_field: audio_tuple}

            # Prepare optional frame images
            if first_frame_image:
                ff_field, ff_tuple = await prepare_image_upload_async(
                    first_frame_image, "first_frame_image"
                )
                files[ff_field] = ff_tuple
            if last_frame_image:
                lf_field, lf_tuple = await prepare_image_upload_async(
                    last_frame_image, "last_frame_image"
                )
                files[lf_field] = lf_tuple

            # Anything unset comes from the model card (see image_to_video).
            await ensure_model_cache(model=model)
            card = resolve_from_card(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "fps": fps,
                    "steps": steps,
                    "guidance": guidance,
                },
            )
            form_data = {
                "prompt": prompt,
                "model": model,
                **{key: str(value) for key, value in card.items()},
                "seed": str(seed),
            }

            if negative_prompt:
                form_data["negative_prompt"] = negative_prompt

            job_response = await client.submit_job(
                endpoint="videos/audio-syncs",
                data=form_data,
                files=files,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid file format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def audio_to_video_price(
    model: Annotated[str, Field(description="Video generation model name")],
    width: Annotated[
        Optional[int],
        Field(ge=1, description="Video width in pixels. Limits per model (see the model list)"),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(ge=1, description="Video height in pixels. Limits per model (see the model list)"),
    ] = None,
    frames: Annotated[
        Optional[int],
        Field(ge=1, description="Number of video frames. Limits per model (see the model list)"),
    ] = None,
    steps: Annotated[
        Optional[int],
        Field(
            ge=1,
            description="Number of inference steps (optional; limits per model, see the model list)",
        ),
    ] = None,
    fps: Annotated[
        Optional[int],
        Field(
            ge=1, description="Frames per second (optional; limits per model, see the model list)"
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for audio-to-video generation.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            await ensure_model_cache(model=model)
            params = resolve_generation_params(
                model,
                {
                    "width": width,
                    "height": height,
                    "frames": frames,
                    "steps": steps,
                    "fps": fps,
                },
            )
            request_data = {
                "model": model,
                **params,
            }
            # seed is not part of the quote; guidance only on a model that turns it on.
            request_data.pop("seed", None)
            if not feature_enabled(model, "supports_guidance"):
                request_data.pop("guidance", None)

            price_response = await client.calculate_price(
                endpoint="videos/audio-syncs/price",
                json_data=request_data,
            )

            return build_price_response(price_response)

    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


VideoDescriptionPreset = Literal["fast", "balanced", "detail"]
DescriptionPlatform = Literal["youtube", "twitter", "twitch", "kick", "tiktok"]

_VIDEO_URL_HELP = (
    "Link to a video on YouTube, Twitter/X, Twitch, Kick or TikTok; the API fetches it "
    "itself. Not for a direct link to a video file: pass that as video_file. "
    "Live streams and audio-only sources (Twitter Spaces) are rejected"
)
_VIDEO_FILE_HELP = (
    "Video file as a direct URL to the file (preferred), data URI (data:video/mp4;base64,...) "
    "or base64 string; this server uploads it. Must contain a video stream"
)
_PRESET_HELP = (
    "Detail level: 'fast', 'balanced' or 'detail'. Higher presets read small on-screen text "
    "and logos but cost more (roughly x1 / x1.7 / x3.75 machine time). Unset: 'balanced', "
    "or the cheapest preset the model offers. Ignored by models without presets"
)


def _description_source_error(video_url: Optional[str], video_file: Optional[str]) -> Optional[str]:
    """The API takes exactly one source; say which rule was broken before uploading anything."""
    if video_url and video_file:
        return "Pass either 'video_url' or 'video_file', not both: the API rejects requests carrying both."
    if not video_url and not video_file:
        return (
            "A source is required: pass 'video_url' for a platform link or 'video_file' for a file."
        )
    return None


async def video_description(
    model: Annotated[str, Field(description="Video description model (e.g., 'Marlin_2B')")],
    video_url: Annotated[Optional[str], Field(description=_VIDEO_URL_HELP)] = None,
    video_file: Annotated[Optional[str], Field(description=_VIDEO_FILE_HELP)] = None,
    preset: Annotated[Optional[VideoDescriptionPreset], Field(description=_PRESET_HELP)] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Return the source's title, channel, upload date and view/like counts. URL sources only; carries a flat surcharge"
        ),
    ] = None,
    return_result_in_response: Annotated[
        bool,
        Field(
            description="Return the description JSON inline in 'result'. Set to False for long videos to get only the download URL"
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
    """Describe what happens on screen in a video, as a timestamped index of events.

    The visual counterpart of transcription: reads the picture, not the audio track
    (for speech use the transcription tools). Pass exactly one source: video_url for a
    YouTube, Twitter/X, Twitch, Kick or TikTok link, or video_file for a file.

    The result is a JSON document:
    {"events": [{"start", "end", "description", ...}], "scenes": [...], "meta": {"duration", "preset", ...}},
    with start/end in seconds. Priced on machine time, so longer videos and higher
    presets cost more; quote first with video_description_price. A long video can
    take several minutes: if the tool times out, follow up with check_job_status.

    Returns:
        dict: Contains 'success', 'result' (description JSON, when returned inline),
              'result_url', 'job_id'
    """
    error = _description_source_error(video_url, video_file)
    if error:
        return input_error_response(error)

    try:
        client = get_client()
        async with client:
            form_data = {
                "model": model,
                "return_result_in_response": form_bool(return_result_in_response),
            }
            if preset:
                form_data["preset"] = preset
            if include_metadata is not None:
                form_data["include_metadata"] = form_bool(include_metadata)

            files = None
            if video_url:
                form_data["video_url"] = video_url
            else:
                assert video_file  # _description_source_error requires exactly one source
                field_name, file_tuple = await prepare_video_upload_async(video_file, "video_file")
                files = {field_name: file_tuple}

            job_response = await client.submit_job(
                endpoint="videos/descriptions",
                data=form_data,
                files=files,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_description_price(
    model: Annotated[str, Field(description="Video description model (e.g., 'Marlin_2B')")],
    video_url: Annotated[
        Optional[str],
        Field(
            description="Platform link to quote (YouTube, Twitter/X, Twitch, Kick). TikTok links are refused: quote those by duration_seconds with platform='tiktok'"
        ),
    ] = None,
    video_file: Annotated[
        Optional[str],
        Field(description="Video file to quote (URL, data URI or base64); only its length is read"),
    ] = None,
    duration_seconds: Annotated[
        Optional[float],
        Field(gt=0, description="Video length in seconds, to quote without a source"),
    ] = None,
    platform: Annotated[
        Optional[DescriptionPlatform],
        Field(
            description="Where the video comes from; only with duration_seconds. 'tiktok' uses a fixed price card (max 10 minutes). Omitted, the quote is for an uploaded file and can come in under a URL job's charge"
        ),
    ] = None,
    preset: Annotated[Optional[VideoDescriptionPreset], Field(description=_PRESET_HELP)] = None,
    include_metadata: Annotated[
        Optional[bool],
        Field(
            description="Whether the job will ask for source metadata (flat surcharge on URL sources)"
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate the price of a video description.

    Pass exactly one source: video_url, video_file or duration_seconds (with an optional
    platform). Use the same preset and include_metadata the job will use, since both
    change the price.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    sources = [s for s in (video_url, video_file, duration_seconds) if s is not None and s != ""]
    if len(sources) != 1:
        return input_error_response(
            "Provide exactly one of 'video_url', 'video_file' or 'duration_seconds'."
        )
    if platform and duration_seconds is None:
        return input_error_response(
            "'platform' is only accepted with 'duration_seconds'; with a URL or file the API derives it from the source."
        )

    try:
        client = get_client()
        async with client:
            form_data = {"model": model}
            if preset:
                form_data["preset"] = preset
            if include_metadata is not None:
                form_data["include_metadata"] = form_bool(include_metadata)

            files = None
            if video_url:
                form_data["video_url"] = video_url
            elif video_file:
                field_name, file_tuple = await prepare_video_upload_async(video_file, "video_file")
                files = {field_name: file_tuple}
            else:
                form_data["duration_seconds"] = str(duration_seconds)
                if platform:
                    form_data["platform"] = platform

            price_response = await client.calculate_price(
                endpoint="videos/descriptions/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_upscale(
    video: Annotated[
        str,
        Field(
            description="Video as URL, data URI (data:video/mp4;base64,...), or base64 string. URLs are recommended to avoid base64 context bloat."
        ),
    ],
    model: Annotated[str, Field(description="Video upscaling model name")],
    scale: Annotated[
        Optional[int],
        Field(
            ge=1,
            le=16,
            description="Upscale factor, only for models with a 'scale' range in the model list (FlashVSR_Tiny: 2-4). Unset, those models use their smallest factor. Models without a range have a fixed factor (e.g. RealESRGAN_Vid_x4) and refuse this field",
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
    """Upscale a video to higher resolution.

    Takes a video and upscales it using AI models for enhanced quality.
    Accepts videos as:
    - Public URLs (recommended, and the ONLY way in for a file of your own - chat
      clients do not pass files through to tools)
    - Data URIs or raw base64 (for callers that already hold the bytes)

    Returns:
        dict: Contains 'success', 'result_url' with upscaled video URL, 'job_id'
    """
    try:
        client = get_client()
        async with client:
            field_name, file_tuple = await prepare_video_upload_async(video, "video")

            await ensure_model_cache(model=model)
            scale, error = resolve_upscale_factor(model, scale)
            if error:
                return input_error_response(error)

            form_data = {
                "model": model,
            }
            if scale is not None:
                form_data["scale"] = str(scale)

            job_response = await client.submit_job(
                endpoint="videos/upscales",
                data=form_data,
                files={field_name: file_tuple},
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )
            job_id = job_response.data.request_id

            polling_manager = PollingManager(client, job_type="video")
            result = await polling_manager.poll_until_complete(job_id)

            return build_job_response(result, job_id)

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


async def video_upscale_price(
    model: Annotated[str, Field(description="Video upscaling model name")],
    video: Annotated[
        Optional[str],
        Field(
            description="Video to quote (URL, data URI or base64); its size and length are read from it. Or pass width, height and duration instead"
        ),
    ] = None,
    width: Annotated[
        Optional[int],
        Field(
            ge=1,
            le=10240,
            description="Video width in pixels. Provide width+height to estimate without uploading a file.",
        ),
    ] = None,
    height: Annotated[
        Optional[int],
        Field(
            ge=1,
            le=10240,
            description="Video height in pixels. Provide width+height to estimate without uploading a file.",
        ),
    ] = None,
    scale: Annotated[
        Optional[int],
        Field(
            ge=1,
            le=16,
            description="Upscale factor, only for models with a 'scale' range in the model list (FlashVSR_Tiny: 2-4). Unset, those models use their smallest factor. Models without a range have a fixed factor (e.g. RealESRGAN_Vid_x4) and refuse this field",
        ),
    ] = None,
    duration: Annotated[
        Optional[float],
        Field(
            gt=0,
            description="Video length in seconds; the price is linear in frames. Required unless video is given",
        ),
    ] = None,
) -> dict[str, Any]:
    """Calculate price for video upscaling.

    Pass the video itself, or its width, height and duration.

    Returns:
        dict: Contains 'success' and 'price' information
    """
    try:
        client = get_client()
        async with client:
            if not video and (width is None or height is None or duration is None):
                return input_error_response(
                    "Pass the video, or its width, height and duration: the price follows its size and length."
                )
            await ensure_model_cache(model=model)
            scale, error = resolve_upscale_factor(model, scale)
            if error:
                return input_error_response(error)

            form_data = {
                "model": model,
            }
            files = None
            if video:
                field_name, file_tuple = await prepare_video_upload_async(video, "video")
                files = {field_name: file_tuple}

            if width is not None:
                form_data["width"] = str(width)
            if height is not None:
                form_data["height"] = str(height)
            if scale is not None:
                form_data["scale"] = str(scale)
            if duration is not None:
                form_data["duration"] = str(duration)

            price_response = await client.calculate_price(
                endpoint="videos/upscales/price",
                data=form_data,
                files=files,
            )

            return build_price_response(price_response)

    except ValueError as e:
        return {"success": False, "error": f"Invalid video format: {str(e)}"}
    except DeapiAPIError as e:
        return {"success": False, "error": f"API error: {str(e)}"}
    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}
