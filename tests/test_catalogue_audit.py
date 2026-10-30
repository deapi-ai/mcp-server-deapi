"""Fixes from the 2026-10-22 audit of every tool against the deAPI API and the live catalogue.

Each test names the API rule it mirrors; the cards are trimmed copies of what
GET /api/v2/models served that day.
"""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deapi_mcp.deapi_client import _error_message
from deapi_mcp.middleware import _cache, _format_model_info
from deapi_mcp.schemas import LanguageInfo, ModelInfo, ToolResult, VoiceInfo
from deapi_mcp.tools._responses import build_job_response, build_price_response


def card(slug, types, limits=None, defaults=None, features=None, **extra):
    return ModelInfo(
        name=slug,
        slug=slug,
        inference_types=types,
        info={"limits": limits or {}, "defaults": defaults or {}, "features": features or {}},
        **extra,
    )


def lang(slug, *voices):
    return LanguageInfo(name=slug, slug=slug, voices=[VoiceInfo(name=v, slug=v) for v in voices])


WHISPER = card(
    "WhisperLargeV3",
    ["audio_file2text"],
    limits={"timestamp_levels": ["segment"]},
    features={"supports_timestamps": True, "supports_diarization": False},
)
WHISPER_CT2 = card(
    "WhisperLargeV3Ct2",
    ["audio_file2text"],
    limits={"timestamp_levels": ["segment", "word"], "output_format": "json"},
    features={"supports_timestamps": True, "supports_diarization": True},
)
KOKORO = card(
    "Kokoro",
    ["txt2audio"],
    limits={"min_speed": 0.5, "max_speed": 2},
    defaults={"lang": "en-us", "voice": "af_alloy", "format": "mp3", "sample_rate": 24000},
    features={"supports_custom_voice": True},
    languages=[lang("en-us", "af_alloy", "af_sky"), lang("fr-fr", "ff_siwis")],
)
QWEN_BASE = card(
    "Qwen3_TTS_12Hz_1_7B_Base",
    ["txt2audio"],
    defaults={"lang": "English", "voice": "default", "format": "mp3"},
    features={"supports_voice_clone": True, "supports_custom_voice": False},
    languages=[lang("English")],
)
ACESTEP = card(
    "AceStep_1_5_Turbo",
    ["txt2music"],
    limits={"min_steps": 8, "max_steps": 8, "min_guidance": 1, "max_guidance": 1},
    defaults={"format": "mp3"},
)
FLASHVSR = card("FlashVSR_Tiny", ["vid-upscale"], limits={"min_scale": 2, "max_scale": 4})
ESRGAN_X4 = card("RealESRGAN_Vid_x4", ["vid-upscale"])


def make_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    job = MagicMock()
    job.data.request_id = "job-1"
    client.submit_job = AsyncMock(return_value=job)
    client.calculate_price = AsyncMock(return_value={"data": {"price": 0.01}})
    return client


@contextmanager
def tool_env(module, *cards, result=None):
    """A mock client, a cache holding `cards`, and polling that returns `result`."""
    for c in cards:
        _cache.models_by_slug[c.slug] = c
    client = make_client()
    polling = MagicMock()
    polling.poll_until_complete = AsyncMock(
        return_value=result or ToolResult(success=True, result_url="https://r/x")
    )
    try:
        with (
            patch(f"deapi_mcp.tools.{module}.get_client", return_value=client),
            patch(f"deapi_mcp.tools.{module}.PollingManager", return_value=polling),
            patch(f"deapi_mcp.tools.{module}.ensure_model_cache", new=AsyncMock()) as ensure,
        ):
            client.ensure = ensure
            yield client
    finally:
        for c in cards:
            _cache.models_by_slug.pop(c.slug, None)


B64_AUDIO = "data:audio/mpeg;base64,AAAA"
B64_VIDEO = "data:video/mp4;base64,AAAA"
B64_IMAGE = "data:image/png;base64,AAAA"


# =============================================================================
# Transcription: the two Whisper response shapes
# =============================================================================


class TestTranscriptShapes:
    async def test_ct2_json_is_unpacked(self):
        """output_format=json: the artifact is {"text", "segments", ...} even without timestamps."""
        from deapi_mcp.tools.audio import audio_transcription

        doc = {
            "text": "Hello world",
            "language": "en",
            "ts_level": "word",
            "segments": [{"start": 0.0, "end": 1.2, "text": "Hello world", "speaker": "S1"}],
        }
        done = ToolResult(success=True, result=json.dumps(doc), result_url="https://r/t.json")
        with tool_env("audio", WHISPER_CT2, result=done):
            out = await audio_transcription(
                audio=B64_AUDIO, include_ts=True, model="WhisperLargeV3Ct2"
            )

        assert out["result"] == "Hello world"
        assert out["segments"] == doc["segments"]
        assert out["language"] == "en"
        assert out["result_url"] == "https://r/t.json"

    async def test_plain_text_is_left_alone(self):
        """WhisperLargeV3 writes timestamps into the text itself."""
        from deapi_mcp.tools.audio import audio_transcription

        text = "[0:00 - 0:05] Hello world"
        with tool_env("audio", WHISPER, result=ToolResult(success=True, result=text)):
            out = await audio_transcription(audio=B64_AUDIO, include_ts=True)

        assert out["result"] == text
        assert "segments" not in out

    async def test_ts_level_turns_timestamps_on(self):
        """Without include_ts the API collapses ts_level to 'segment' and returns none at all."""
        from deapi_mcp.tools.audio import audio_transcription

        with tool_env("audio", WHISPER_CT2) as client:
            await audio_transcription(
                audio=B64_AUDIO, include_ts=False, ts_level="word", model="WhisperLargeV3Ct2"
            )

        sent = client.submit_job.call_args.kwargs["data"]
        assert sent["ts_level"] == "word"
        assert sent["include_ts"] == "1"

    async def test_upload_price_needs_a_duration_and_takes_no_platform(self):
        """An upload is priced as one; `platform` mis-quoted it (up to ~9x)."""
        import inspect

        from deapi_mcp.tools.audio import audio_transcription_price, video_file_transcription_price

        for tool in (audio_transcription_price, video_file_transcription_price):
            params = inspect.signature(tool).parameters
            assert params["duration_seconds"].default is inspect.Parameter.empty
            assert "platform" not in params
            assert "include_metadata" not in params

    async def test_spaces_price_needs_a_source(self):
        from deapi_mcp.tools.audio import audio_url_transcription_price

        with tool_env("audio") as client:
            out = await audio_url_transcription_price(include_ts=False)

        assert out["success"] is False
        client.calculate_price.assert_not_called()

    def test_enrichment_tells_the_two_whispers_apart(self):
        ct2 = _format_model_info(WHISPER_CT2)
        v3 = _format_model_info(WHISPER)
        assert "diarize" in ct2 and "output=json" in ct2
        assert "diarize" not in v3 and "output=" not in v3


# =============================================================================
# TTS and music
# =============================================================================


class TestSpeech:
    async def test_voice_follows_the_language(self):
        """Text2AudioRequest checks the voice against that language's own list."""
        from deapi_mcp.tools.audio import text_to_audio

        with tool_env("audio", KOKORO) as client:
            await text_to_audio(text="Bonjour tout le monde", model="Kokoro", lang="fr-fr")

        sent = client.submit_job.call_args.kwargs["json_data"]
        assert sent["lang"] == "fr-fr"
        assert sent["voice"] == "ff_siwis"
        assert "return_result_in_response" not in sent  # not a TTS field

    async def test_default_voice_kept_when_the_language_has_it(self):
        from deapi_mcp.tools.audio import text_to_audio

        with tool_env("audio", KOKORO) as client:
            await text_to_audio(text="Hello there", model="Kokoro")

        sent = client.submit_job.call_args.kwargs["json_data"]
        assert (sent["lang"], sent["voice"]) == ("en-us", "af_alloy")

    async def test_card_is_loaded_before_the_mode_is_read(self):
        """A cold cache sent no mode, and the API's custom_voice default 422s Qwen3 Base."""
        from deapi_mcp.tools.audio import text_to_audio_price

        with tool_env("audio", QWEN_BASE) as client:
            await text_to_audio_price(text="Hello there", model=QWEN_BASE.slug)

        client.ensure.assert_awaited()
        assert client.calculate_price.call_args.kwargs["json_data"]["mode"] == "voice_clone"

    async def test_music_takes_pinned_steps_and_guidance_from_the_card(self):
        from deapi_mcp.tools.audio import text_to_music

        with tool_env("audio", ACESTEP) as client:
            out = await text_to_music(
                caption="lofi beat", model=ACESTEP.slug, lyrics="[Instrumental]", duration=30
            )

        assert out["success"] is True
        sent = client.submit_job.call_args.kwargs["data"]
        assert sent["inference_steps"] == "8"
        assert sent["guidance_scale"] == "1"
        assert sent["format"] == "mp3"

    async def test_music_price_sends_both_required_fields(self):
        from deapi_mcp.tools.audio import text_to_music_price

        with tool_env("audio", ACESTEP) as client:
            await text_to_music_price(model=ACESTEP.slug, duration=60)

        assert client.calculate_price.call_args.kwargs["data"] == {
            "model": ACESTEP.slug,
            "duration": "60",
            "inference_steps": "8",
        }

    def test_music_reference_window_is_shown(self):
        assert "ref_audio=5-60s" in _format_model_info(
            card(
                "AceStep",
                ["txt2music"],
                limits={"min_ref_audio_duration": 5, "max_ref_audio_duration": 60},
            )
        )


# =============================================================================
# Video
# =============================================================================


class TestVideoUpscale:
    async def test_ranged_model_gets_its_smallest_scale(self):
        """VideoUpscaleRequest: a model with min/max_scale requires `scale`."""
        from deapi_mcp.tools.video import video_upscale

        with tool_env("video", FLASHVSR) as client:
            await video_upscale(video=B64_VIDEO, model=FLASHVSR.slug)

        assert client.submit_job.call_args.kwargs["data"]["scale"] == "2"

    async def test_fixed_model_refuses_a_scale(self):
        """...and a model with neither rejects it."""
        from deapi_mcp.tools.video import video_upscale

        with tool_env("video", ESRGAN_X4) as client:
            out = await video_upscale(video=B64_VIDEO, model=ESRGAN_X4.slug, scale=4)

        assert out["success"] is False
        assert "fixed upscale factor" in out["error"]
        client.submit_job.assert_not_called()

    async def test_price_can_read_the_video(self):
        from deapi_mcp.tools.video import video_upscale_price

        with tool_env("video", ESRGAN_X4) as client:
            await video_upscale_price(model=ESRGAN_X4.slug, video=B64_VIDEO)

        kwargs = client.calculate_price.call_args.kwargs
        assert list(kwargs["files"]) == ["video"]


class TestVideoGeneration:
    async def test_enhance_prompt_is_forwarded(self):
        from deapi_mcp.tools.video import text_to_video

        with tool_env("video") as client:
            await text_to_video(prompt="a fox", model="m", enhance_prompt=True)

        sent = client.submit_job.call_args.kwargs["data"]
        assert sent["enhance_prompt"] == "1"
        assert "return_result_in_response" not in sent

    async def test_job_failure_keeps_the_actionable_fields(self):
        """Hand-built dicts dropped retryable/refunded; build_job_response keeps them."""
        from deapi_mcp.tools.video import text_to_video

        failed = ToolResult(success=False, error="boom", retryable=True, refunded=True)
        with tool_env("video", result=failed):
            out = await text_to_video(prompt="a fox", model="m")

        assert out["retryable"] is True and out["refunded"] is True

    @pytest.mark.parametrize("frames,width", [(721, 2206)])
    async def test_price_accepts_what_the_cards_allow(self, frames, width):
        """seedance-2.5 goes to 721 frames and 2206 px; le=200/2048 refused them."""
        from deapi_mcp.tools.video import image_to_video_price

        with tool_env("video") as client:
            out = await image_to_video_price(model="seedance-2.5", frames=frames, width=width)

        assert out["success"] is True
        assert client.calculate_price.call_args.kwargs["json_data"]["frames"] == frames


# =============================================================================
# Images
# =============================================================================


class TestImages:
    async def test_edit_loras_are_bracketed_form_fields(self):
        """Image2ImageRequest validates loras as an array; a JSON string is a 422."""
        from deapi_mcp.tools.image import image_to_image

        with tool_env("image") as client:
            await image_to_image(
                prompt="p", model="m", image=B64_IMAGE, loras=[{"name": "style", "weight": 0.7}]
            )

        sent = client.submit_job.call_args.kwargs["data"]
        assert sent["loras[0][name]"] == "style"
        assert sent["loras[0][weight]"] == "0.7"
        assert "loras" not in sent

    async def test_edit_price_uploads_the_source_image(self):
        """The price follows the input size on gpt-image; without the file it guessed."""
        from deapi_mcp.tools.image import image_to_image_price

        with tool_env("image") as client:
            await image_to_image_price(prompt="p", model="gpt-image-2", image=B64_IMAGE)

        kwargs = client.calculate_price.call_args.kwargs
        assert [name for name, _ in kwargs["files"]] == ["image"]
        assert "width" not in kwargs["data"]

    async def test_ocr_returns_the_text_inline_by_default(self):
        """A chat client often cannot fetch result_url; transcripts are inline by default too."""
        from deapi_mcp.tools.image import image_to_text

        with tool_env("image") as client:
            await image_to_text(image=B64_IMAGE, model="Nanonets_Ocr_S_F16")

        assert client.submit_job.call_args.kwargs["data"]["return_result_in_response"] == "1"

    async def test_size_priced_tools_take_an_image_or_both_sides(self):
        from deapi_mcp.tools.image import image_upscale_price

        with tool_env("image") as client:
            refused = await image_upscale_price(model="RealESRGAN_x4", width=512)
            await image_upscale_price(model="RealESRGAN_x4", image=B64_IMAGE)

        assert refused["success"] is False
        assert list(client.calculate_price.call_args.kwargs["files"]) == ["image"]


# =============================================================================
# Shared plumbing
# =============================================================================


class TestPlumbing:
    def test_source_metadata_reaches_the_caller(self):
        """include_metadata is charged; the poller used to overwrite what it bought."""
        result = ToolResult(
            success=True, result="text", source_metadata={"title": "A video", "view_count": 7}
        )
        assert build_job_response(result, "j")["source_metadata"]["title"] == "A video"

    def test_unavailable_price_is_flagged(self):
        out = build_price_response({"data": {"price": 0.0, "estimate_basis": "unavailable"}})
        assert "not the cost" in out["warning"]
        assert "warning" not in build_price_response({"data": {"price": 0.5}})

    def test_validation_errors_carry_every_field_message(self):
        body = {
            "message": "The given data was invalid.",
            "errors": {"model": ["No prompt booster guide for this model."]},
        }
        assert "No prompt booster guide" in _error_message(body, "HTTP 422")
        assert _error_message({"error": "Prompt too long"}, "HTTP 422") == "Prompt too long"
