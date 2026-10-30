"""MiniMax-H3 reference inputs on image_to_video, and the video description tools.

Contract source: the deAPI API's image-to-video reference-input rules,
`CalculateImage2VideoPriceRequest`, `VideoDescriptionRequest` and
`CalculateVideoDescriptionPriceRequest`. The model cards below are trimmed copies
of what GET /api/v2/models served on 2026-10-22.
"""

import base64
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deapi_mcp.middleware import (
    INFERENCE_TYPE_TO_TOOLS,
    _cache,
    _fetch_and_index_models,
    _format_model_info,
)
from deapi_mcp.schemas import ModelInfo, ModelsResponse, ToolResult


def b64(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


IMG_A = b64("image/png", b"image-a")
IMG_B = b64("image/png", b"image-b")
VID = b64("video/mp4", b"video")
AUD = b64("audio/mpeg", b"audio")

MINIMAX = ModelInfo(
    name="MiniMax H3 33B Turbo INT8",
    slug="MiniMaxH3_33B_Turbo_INT8",
    inference_types=["img2video", "txt2video"],
    status="standard_model",
    info={
        "limits": {
            "min_fps": 24,
            "max_fps": 24,
            "min_frames": 56,
            "max_frames": 243,
            "max_ref_files": 3,
            "max_ref_audios": 3,
            "max_ref_images": 3,
            "max_ref_videos": 1,
            "min_ref_video_duration": 2,
            "max_ref_video_duration": 5,
            "min_ref_audio_duration": 1,
            "max_ref_audio_duration": 10,
        },
        "defaults": {"fps": 24, "width": 1344, "frames": 124, "height": 768},
        "features": {
            "supports_image_refs": True,
            "supports_video_refs": True,
            "supports_audio_refs": True,
        },
    },
)

# LTX 2.5 carries the ref keys, but all null: it takes no references.
LTX25 = ModelInfo(
    name="LTX-2.5 22B Distilled INT8",
    slug="Ltx2_5_22B_Dist_INT8",
    inference_types=["img2video", "txt2video", "audio2video"],
    status="standard_model",
    info={
        "limits": {
            "min_fps": 24,
            "max_fps": 24,
            "max_ref_files": None,
            "max_ref_images": None,
            "max_ref_videos": None,
            "max_ref_audios": None,
            "min_ref_audio_duration": 1,
            "max_ref_audio_duration": 10,
        },
        "features": {"supports_image_refs": None},
    },
)

MARLIN = ModelInfo(
    name="Marlin 2B",
    slug="Marlin_2B",
    inference_types=["video2desc", "video_file2desc"],
    status="standard_model",
    info={
        "limits": {
            "available_presets": ["fast", "balanced", "detail"],
            "max_video_duration_seconds": 7200,
        },
        "features": {"supports_presets": True},
    },
)


def make_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    job = MagicMock()
    job.data.request_id = "job-1"
    client.submit_job = AsyncMock(return_value=job)
    client.calculate_price = AsyncMock(return_value={"data": {"price": 0.25}})
    return client


@contextmanager
def patched_video(result=None):
    """Run a deapi_mcp.tools.video tool against a mock client; yields the client."""
    client = make_client()
    polling = MagicMock()
    polling.poll_until_complete = AsyncMock(
        return_value=result or ToolResult(success=True, result_url="https://r/v.mp4")
    )
    with (
        patch("deapi_mcp.tools.video.get_client", return_value=client),
        patch("deapi_mcp.tools.video.PollingManager", return_value=polling),
        patch("deapi_mcp.tools.video.ensure_model_cache", new=AsyncMock()),
    ):
        yield client


def file_fields(client) -> list:
    """The multipart field names, in the order they were sent."""
    return [name for name, _ in client.submit_job.call_args.kwargs["files"]]


# =============================================================================
# image_to_video: reference mode
# =============================================================================


class TestImageToVideoReferences:
    async def test_refs_are_sent_as_repeated_array_fields_in_order(self):
        """Order binds each file to its <Picture N> tag, so it must survive the upload."""
        from deapi_mcp.tools.video import image_to_video

        with patched_video() as client:
            result = await image_to_video(
                prompt="<Picture 1> meets <Picture 2>, voiced by <Audio 1>",
                model=MINIMAX.slug,
                ref_images=[IMG_A, IMG_B],
                ref_videos=[VID],
                ref_audios=[AUD],
            )

        assert result["success"] is True
        kwargs = client.submit_job.call_args.kwargs
        assert kwargs["endpoint"] == "videos/animations"
        assert file_fields(client) == [
            "ref_images[]",
            "ref_images[]",
            "ref_videos[]",
            "ref_audios[]",
        ]
        images = [f for name, f in kwargs["files"] if name == "ref_images[]"]
        assert [f[1].getvalue() for f in images] == [b"image-a", b"image-b"]
        assert "first_frame_image" not in file_fields(client)

    async def test_keyframe_mode_is_unchanged(self):
        from deapi_mcp.tools.video import image_to_video

        with patched_video() as client:
            await image_to_video(
                prompt="p", model=LTX25.slug, first_frame_image=IMG_A, last_frame_image=IMG_B
            )

        assert file_fields(client) == ["first_frame_image", "last_frame_image"]

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({"first_frame_image": IMG_A, "ref_images": [IMG_B]}, "not both"),
            ({"last_frame_image": IMG_A, "ref_videos": [VID]}, "not both"),
            ({"ref_audios": [AUD]}, "needs at least one ref_images or ref_videos"),
            ({}, "A source is required"),
        ],
    )
    async def test_invalid_combinations_fail_before_any_upload(self, kwargs, needle):
        from deapi_mcp.tools.video import image_to_video

        with patched_video() as client:
            result = await image_to_video(prompt="p", model=MINIMAX.slug, **kwargs)

        assert result["success"] is False
        assert needle in result["error"]
        client.submit_job.assert_not_called()


class TestImageToVideoPriceRefCounts:
    async def test_counts_are_forwarded(self):
        from deapi_mcp.tools.video import image_to_video_price

        with patched_video() as client:
            await image_to_video_price(
                model=MINIMAX.slug, ref_images_count=3, ref_videos_count=1, ref_audios_count=2
            )

        body = client.calculate_price.call_args.kwargs["json_data"]
        assert body["ref_images_count"] == 3
        assert body["ref_videos_count"] == 1
        assert body["ref_audios_count"] == 2

    async def test_counts_left_out_when_unset_or_zero(self):
        from deapi_mcp.tools.video import image_to_video_price

        with patched_video() as client:
            await image_to_video_price(model=LTX25.slug, ref_images_count=0)

        body = client.calculate_price.call_args.kwargs["json_data"]
        assert not any(key.startswith("ref_") for key in body)


# =============================================================================
# video_description
# =============================================================================


class TestVideoDescription:
    async def test_platform_url_is_passed_through(self):
        from deapi_mcp.tools.video import video_description

        description = '{"events": [{"start": 0, "end": 2.5, "description": "A cat"}]}'
        done = ToolResult(success=True, result=description, result_url="https://r/d.json")
        with patched_video(done) as client:
            result = await video_description(
                model=MARLIN.slug,
                video_url="https://www.youtube.com/watch?v=jNQXAC9IVRw",
                preset="detail",
                include_metadata=True,
            )

        kwargs = client.submit_job.call_args.kwargs
        assert kwargs["endpoint"] == "videos/descriptions"
        assert kwargs["files"] is None
        assert kwargs["data"] == {
            "model": "Marlin_2B",
            "video_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "preset": "detail",
            "include_metadata": "1",
            "return_result_in_response": "1",
        }
        assert result["success"] is True
        assert result["result"] == description
        assert result["result_url"] == "https://r/d.json"

    async def test_file_is_uploaded_as_video_file(self):
        from deapi_mcp.tools.video import video_description

        with patched_video(ToolResult(success=True, result_url="u")) as client:
            await video_description(model=MARLIN.slug, video_file=VID)

        kwargs = client.submit_job.call_args.kwargs
        assert list(kwargs["files"]) == ["video_file"]
        assert "video_url" not in kwargs["data"]
        assert "preset" not in kwargs["data"]  # unset: the API pins the model default

    async def test_uses_video_polling(self):
        from deapi_mcp.tools.video import video_description

        polling_cls = MagicMock()
        polling_cls.return_value.poll_until_complete = AsyncMock(
            return_value=ToolResult(success=True, result_url="u")
        )
        client = make_client()
        with (
            patch("deapi_mcp.tools.video.get_client", return_value=client),
            patch("deapi_mcp.tools.video.PollingManager", polling_cls),
        ):
            await video_description(model=MARLIN.slug, video_url="https://youtu.be/x")

        polling_cls.assert_called_once_with(client, job_type="video")

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({"video_url": "https://youtu.be/x", "video_file": VID}, "not both"),
            ({}, "A source is required"),
        ],
    )
    async def test_exactly_one_source(self, kwargs, needle):
        from deapi_mcp.tools.video import video_description

        with patched_video() as client:
            result = await video_description(model=MARLIN.slug, **kwargs)

        assert result["success"] is False
        assert needle in result["error"]
        client.submit_job.assert_not_called()

    async def test_rejects_an_unknown_preset_by_signature(self):
        """The preset is a closed enum on the API; the tool schema says so up front."""
        import inspect
        import typing

        from deapi_mcp.tools.video import video_description

        annotation = inspect.signature(video_description).parameters["preset"].annotation
        literal = typing.get_args(typing.get_args(annotation)[0])[0]
        assert set(typing.get_args(literal)) == {"fast", "balanced", "detail"}


class TestVideoDescriptionPrice:
    async def test_duration_with_platform(self):
        from deapi_mcp.tools.video import video_description_price

        with patched_video() as client:
            result = await video_description_price(
                model=MARLIN.slug, duration_seconds=600, platform="tiktok", preset="fast"
            )

        assert result == {"success": True, "price": {"price": 0.25}}
        kwargs = client.calculate_price.call_args.kwargs
        assert kwargs["endpoint"] == "videos/descriptions/price"
        assert kwargs["data"] == {
            "model": "Marlin_2B",
            "preset": "fast",
            "duration_seconds": "600",
            "platform": "tiktok",
        }

    async def test_url_source(self):
        from deapi_mcp.tools.video import video_description_price

        with patched_video() as client:
            await video_description_price(model=MARLIN.slug, video_url="https://youtu.be/x")

        kwargs = client.calculate_price.call_args.kwargs
        assert kwargs["data"]["video_url"] == "https://youtu.be/x"
        assert kwargs["files"] is None

    async def test_file_source(self):
        from deapi_mcp.tools.video import video_description_price

        with patched_video() as client:
            await video_description_price(model=MARLIN.slug, video_file=VID)

        assert list(client.calculate_price.call_args.kwargs["files"]) == ["video_file"]

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({}, "exactly one"),
            ({"video_url": "https://youtu.be/x", "duration_seconds": 60}, "exactly one"),
            ({"video_url": "https://youtu.be/x", "platform": "youtube"}, "only accepted with"),
        ],
    )
    async def test_source_rules(self, kwargs, needle):
        from deapi_mcp.tools.video import video_description_price

        with patched_video() as client:
            result = await video_description_price(model=MARLIN.slug, **kwargs)

        assert result["success"] is False
        assert needle in result["error"]
        client.calculate_price.assert_not_called()


# =============================================================================
# Model descriptions (enrichment)
# =============================================================================


class TestEnrichment:
    def test_reference_limits_are_listed(self):
        line = _format_model_info(MINIMAX)
        assert "refs: ≤3 images, ≤1 video (2-5s), ≤3 audio (1-10s), ≤3 files total" in line, line

    def test_model_without_references_shows_none(self):
        assert "refs" not in _format_model_info(LTX25)

    def test_unsupported_type_published_as_zero_is_omitted(self):
        card = MINIMAX.model_copy(deep=True)
        card.info["limits"].update(max_ref_videos=0, max_ref_audios=0)
        line = _format_model_info(card)
        assert "refs: ≤3 images, ≤3 files total" in line

    def test_presets_are_listed(self):
        line = _format_model_info(MARLIN)
        assert "presets: fast|balanced|detail" in line
        assert "video≤7200s" in line

    async def test_model_with_both_description_types_is_listed_once(self):
        client = AsyncMock()
        client.get_models = AsyncMock(return_value=ModelsResponse(data=[MARLIN]))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("deapi_mcp.deapi_client.get_client", return_value=client):
            await _fetch_and_index_models()

        try:
            for tool in ("video_description", "video_description_price"):
                assert [m.slug for m in _cache.tool_models[tool]] == ["Marlin_2B"]
                assert _cache.enrichments[tool].count("Marlin_2B") == 1
        finally:
            _cache.tool_models.clear()
            _cache.models_by_slug.clear()
            _cache.enrichments.clear()
            _cache.last_fetched = None


def test_every_mapped_tool_is_registered():
    """A mapping left behind for a removed tool (video_replace) would enrich nothing."""
    import deapi_mcp.server_remote as server

    mapped = {name for names in INFERENCE_TYPE_TO_TOOLS.values() for name in names}
    missing = sorted(name for name in mapped if not callable(getattr(server, name, None)))
    assert not missing, f"mapped but not imported into server_remote: {missing}"
    assert "video-replace" not in INFERENCE_TYPE_TO_TOOLS
