"""Tests for DEV-2019 P3-P6.

Each case pins a payload the API actually validates. The shared failure mode is
that a wrong or missing field is accepted with a 200 and silently dropped, so
asserting on what leaves the tool is the only way to catch a regression here.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.schemas import JobStatus, JobStatusData, JobStatusResponse


def make_client():
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    job = MagicMock()
    job.data.request_id = "job-1"
    client.submit_job = AsyncMock(return_value=job)
    client.calculate_price = AsyncMock(return_value={"data": {"price": 0.01}})
    return client


def make_poll(success=True, result="text", result_url=None):
    r = MagicMock()
    r.success = success
    r.result = result
    r.result_url = result_url
    r.error = None if success else "failed"
    r.error_code = None
    r.retryable = None
    r.refunded = None
    r.price = None
    r.results_alt_formats = None
    r.metadata = {}
    return r


def patched_audio(client, poll=None):
    polling = MagicMock()
    polling.poll_until_complete = AsyncMock(return_value=poll or make_poll())
    return (
        patch("src.tools.audio.get_client", return_value=client),
        patch("src.tools.audio.PollingManager", return_value=polling),
    )


# =============================================================================
# P3: TTS mode
# =============================================================================


class TestTtsMode:

    @pytest.mark.asyncio
    async def test_mode_derived_from_model_card(self):
        """VoiceClone models 422 without mode; the card says which mode to send."""
        client = make_client()
        a, b = patched_audio(client)
        with (
            a,
            b,
            patch(
                "src.tools.audio.get_model_features",
                return_value={"supports_voice_clone": "1"},
            ),
        ):
            from src.tools.audio import text_to_audio

            await text_to_audio(text="hi", model="Qwen3_TTS_12Hz_1_7B_Base", voice="x")

        sent = client.submit_job.call_args.kwargs["json_data"]
        assert sent["mode"] == "voice_clone"

    @pytest.mark.asyncio
    async def test_explicit_mode_wins_over_card(self):
        client = make_client()
        a, b = patched_audio(client)
        with (
            a,
            b,
            patch(
                "src.tools.audio.get_model_features",
                return_value={"supports_voice_clone": "1"},
            ),
        ):
            from src.tools.audio import text_to_audio

            await text_to_audio(text="hi", model="m", voice="x", mode="voice_design")

        assert client.submit_job.call_args.kwargs["json_data"]["mode"] == "voice_design"

    @pytest.mark.asyncio
    async def test_zero_string_feature_is_not_treated_as_enabled(self):
        """Feature flags arrive as strings; "0" is truthy in Python."""
        client = make_client()
        a, b = patched_audio(client)
        with (
            a,
            b,
            patch(
                "src.tools.audio.get_model_features",
                return_value={"supports_voice_clone": "0", "supports_custom_voice": "1"},
            ),
        ):
            from src.tools.audio import text_to_audio

            await text_to_audio(text="hi", model="Kokoro", voice="af_sky")

        assert client.submit_job.call_args.kwargs["json_data"]["mode"] == "custom_voice"

    @pytest.mark.asyncio
    async def test_ref_audio_switches_to_multipart(self):
        """ref_audio is a file upload, so JSON cannot carry the request."""
        client = make_client()
        a, b = patched_audio(client)
        with (
            a,
            b,
            patch("src.tools.audio.get_model_features", return_value={"supports_voice_clone": "1"}),
            patch(
                "src.tools.audio.prepare_audio_upload_async",
                AsyncMock(return_value=("ref_audio", ("r.mp3", b"x", "audio/mpeg"))),
            ),
        ):
            from src.tools.audio import text_to_audio

            await text_to_audio(
                text="hi", model="m", voice="x", ref_audio="data:audio/mp3;base64,AA", ref_text="hi"
            )

        kwargs = client.submit_job.call_args.kwargs
        assert "files" in kwargs, "ref_audio requires multipart"
        assert kwargs["data"]["mode"] == "voice_clone"
        assert kwargs["data"]["ref_text"] == "hi"


# =============================================================================
# P4: job status carries the failure reason
# =============================================================================


class TestJobStatusFailure:

    def test_api_error_status_parses(self):
        """The API's terminal failure status is 'error'; 'failed' never appears."""
        data = JobStatusData(status="error", error_code="OUT_OF_VRAM", error_reason="No VRAM")
        assert data.status is JobStatus.ERROR
        assert data.status.is_failure

    @pytest.mark.asyncio
    async def test_failed_job_reports_reason_and_not_success(self):
        client = make_client()
        client.get_job_status = AsyncMock(
            return_value=JobStatusResponse(
                data=JobStatusData(
                    status="error",
                    error_code="OUT_OF_VRAM",
                    error_reason="Worker ran out of VRAM",
                    retryable=True,
                    refunded=True,
                )
            )
        )
        with patch("src.tools.utility.get_client", return_value=client):
            from src.tools.utility import check_job_status

            result = await check_job_status("job-1")

        assert result["success"] is False, "a failed job is not a successful check"
        assert result["error_reason"] == "Worker ran out of VRAM"
        assert result["error_code"] == "OUT_OF_VRAM"
        assert result["retryable"] is True
        assert result["refunded"] is True

    @pytest.mark.asyncio
    async def test_inline_preview_is_always_null(self):
        """The API's `preview` is a base64 PNG, not a URL; it has no place in an LLM's context.
        The key is kept so the response shape is stable, the value is never filled."""
        client = make_client()
        client.get_job_status = AsyncMock(
            return_value=JobStatusResponse(
                data=JobStatusData(status="processing", progress=40.0, preview="iVBORw0KGgo" * 2000)
            )
        )
        with patch("src.tools.utility.get_client", return_value=client):
            from src.tools.utility import check_job_status

            result = await check_job_status("job-1")

        assert result["progress"] == 40.0
        assert result["preview_url"] is None
        assert "preview" not in result

    @pytest.mark.asyncio
    async def test_done_job_reports_price_and_alt_formats(self):
        client = make_client()
        client.get_job_status = AsyncMock(
            return_value=JobStatusResponse(
                data=JobStatusData(
                    status="done",
                    progress=100.0,
                    result_url="https://r/x.png",
                    price={"amount": 0.001, "is_estimated": False},
                    results_alt_formats={"jpg": "https://r/x.jpg"},
                )
            )
        )
        with patch("src.tools.utility.get_client", return_value=client):
            from src.tools.utility import check_job_status

            result = await check_job_status("job-1")

        assert result["success"] is True
        assert result["price"]["amount"] == 0.001
        assert result["results_alt_formats"]["jpg"] == "https://r/x.jpg"


# =============================================================================
# P5: parameters that change the result or the price
# =============================================================================


class TestTranscriptionParameters:

    @pytest.mark.asyncio
    async def test_ts_level_and_diarize_are_sent(self):
        client = make_client()
        a, b = patched_audio(client)
        with (
            a,
            b,
            patch(
                "src.tools.audio.prepare_audio_upload_async",
                AsyncMock(return_value=("source_file", ("a.mp3", b"x", "audio/mpeg"))),
            ),
        ):
            from src.tools.audio import audio_transcription

            await audio_transcription(
                audio="data:audio/mp3;base64,AA",
                include_ts=True,
                ts_level="word",
                diarize=True,
                lang="pl",
                include_metadata=True,
            )

        sent = client.submit_job.call_args.kwargs["data"]
        assert sent["ts_level"] == "word"
        assert sent["diarize"] == "1"
        assert sent["lang"] == "pl"
        assert sent["include_metadata"] == "1"

    @pytest.mark.asyncio
    async def test_platform_priced_with_duration(self):
        """platform carries up to a ~9x multiplier and is valid with duration only."""
        client = make_client()
        with patch("src.tools.audio.get_client", return_value=client):
            from src.tools.audio import video_url_transcription_price

            result = await video_url_transcription_price(
                include_ts=True, duration_seconds=240, platform="tiktok"
            )

        assert result["success"] is True
        assert client.calculate_price.call_args.kwargs["data"]["platform"] == "tiktok"

    @pytest.mark.asyncio
    async def test_platform_rejected_alongside_url(self):
        """The API marks platform 'missing' when a source URL is present."""
        client = make_client()
        with patch("src.tools.audio.get_client", return_value=client):
            from src.tools.audio import video_url_transcription_price

            result = await video_url_transcription_price(
                include_ts=True, video_url="https://x/y.mp4", platform="tiktok"
            )

        assert result["success"] is False
        assert "platform" in result["error"]
        client.calculate_price.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_url_and_duration_are_mutually_exclusive(self):
        client = make_client()
        with patch("src.tools.audio.get_client", return_value=client):
            from src.tools.audio import video_url_transcription_price

            result = await video_url_transcription_price(
                include_ts=True, video_url="https://x/y.mp4", duration_seconds=10
            )

        assert result["success"] is False
        client.calculate_price.assert_not_awaited()


# =============================================================================
# P6: cleanup — the field names the API actually reads
# =============================================================================


class TestImagePayloadCleanup:

    @pytest.mark.asyncio
    async def test_text_to_image_sends_guidance_not_guidance_scale(self):
        """The API reads `guidance`; `guidance_scale` was silently discarded."""
        client = make_client()
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=make_poll(result_url="u"))
        with (
            patch("src.tools.image.get_client", return_value=client),
            patch("src.tools.image.PollingManager", return_value=polling),
            patch("src.tools._price_helpers.get_model_defaults", return_value={}),
            patch("src.tools.image.ensure_model_cache", new=AsyncMock()),
        ):
            from src.tools.image import text_to_image

            await text_to_image(prompt="a cat", model="m", guidance_scale=3.5)

        sent = client.submit_job.call_args.kwargs["json_data"]
        assert sent["guidance"] == 3.5
        assert "guidance_scale" not in sent, "guidance_scale is not a field the API knows"

    @pytest.mark.asyncio
    async def test_text_to_image_sends_quality_and_enhance_prompt(self):
        client = make_client()
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=make_poll(result_url="u"))
        with (
            patch("src.tools.image.get_client", return_value=client),
            patch("src.tools.image.PollingManager", return_value=polling),
            patch("src.tools._price_helpers.get_model_defaults", return_value={}),
            patch("src.tools.image.ensure_model_cache", new=AsyncMock()),
        ):
            from src.tools.image import text_to_image

            await text_to_image(
                prompt="a cat", model="gpt-image-1.5", quality="high", enhance_prompt=True
            )

        sent = client.submit_job.call_args.kwargs["json_data"]
        assert sent["quality"] == "high"
        assert sent["enhance_prompt"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag,expected", [(True, "1"), (False, "0")])
    async def test_image_to_image_enhance_prompt_is_a_laravel_form_boolean(self, flag, expected):
        """QA blocker B1b: image_to_image(enhance_prompt=…) → 422 "must be true or false".

        /images/edits is multipart, and Laravel's built-in `boolean` rule (BaseClientRequest)
        accepts only "1"/"0" in a form field — "true"/"True"/"on" all fail it.
        """
        client = make_client()
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=make_poll(result_url="u"))
        with (
            patch("src.tools.image.get_client", return_value=client),
            patch("src.tools.image.PollingManager", return_value=polling),
            patch("src.tools._price_helpers.get_model_defaults", return_value={}),
            patch("src.tools.image.ensure_model_cache", new=AsyncMock()),
            patch(
                "src.tools.image.prepare_image_upload_async",
                new=AsyncMock(return_value=("image", ("i.png", b"x", "image/png"))),
            ),
        ):
            from src.tools.image import image_to_image

            await image_to_image(
                prompt="p", model="Flux_2_Klein_4B_BF16", image="data:x", enhance_prompt=flag
            )

        assert client.submit_job.call_args.kwargs["data"]["enhance_prompt"] == expected

    @pytest.mark.asyncio
    async def test_image_to_image_no_longer_sends_strength(self):
        """The API does not know `strength`; it was accepted and dropped."""
        client = make_client()
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(return_value=make_poll(result_url="u"))
        with (
            patch("src.tools.image.get_client", return_value=client),
            patch("src.tools.image.PollingManager", return_value=polling),
            patch("src.tools._price_helpers.get_model_defaults", return_value={}),
            patch("src.tools.image.ensure_model_cache", new=AsyncMock()),
            patch(
                "src.tools.image.prepare_image_upload_async",
                AsyncMock(return_value=("image", ("a.png", b"x", "image/png"))),
            ),
        ):
            from src.tools.image import image_to_image

            await image_to_image(prompt="p", model="m", image="data:image/png;base64,AA")

        assert "strength" not in client.submit_job.call_args.kwargs["data"]

    @pytest.mark.asyncio
    async def test_image_and_images_are_mutually_exclusive(self):
        client = make_client()
        with patch("src.tools.image.get_client", return_value=client):
            from src.tools.image import image_to_image

            result = await image_to_image(prompt="p", model="m", image="a", images=["b", "c"])

        assert result["success"] is False
        client.submit_job.assert_not_awaited()


# =============================================================================
# P3 follow-up from live verification: languages/voices per TTS model
# =============================================================================


def make_tts_card(slug, languages, features=None, defaults=None):
    from src.schemas import ModelInfo

    return ModelInfo(
        name=slug,
        slug=slug,
        inference_types=["txt2audio"],
        info={
            "features": features or {},
            "defaults": defaults or {},
            "limits": {"output_formats": ["mp3"]},
        },
        languages=languages,
    )


class TestTtsLanguagesAndVoices:

    def test_schema_parses_languages_and_voices(self):
        card = make_tts_card(
            "Kokoro",
            [
                {
                    "name": "English (US)",
                    "slug": "en-us",
                    "voices": [{"name": "Alloy", "slug": "af_alloy", "gender": "male"}],
                }
            ],
        )
        assert card.languages[0].slug == "en-us"
        assert card.languages[0].voices[0].slug == "af_alloy"

    def test_enrichment_lists_languages_and_voice_slugs(self):
        """An LLM must see the slugs — the API takes slugs, not display names."""
        from src.middleware import _format_model_info

        card = make_tts_card(
            "Kokoro",
            [
                {
                    "name": "English (US)",
                    "slug": "en-us",
                    "voices": [
                        {"name": "Alloy", "slug": "af_alloy"},
                        {"name": "Bella", "slug": "af_bella"},
                    ],
                },
                {"name": "Spain", "slug": "es", "voices": [{"name": "Dora", "slug": "ef_dora"}]},
            ],
        )
        text = _format_model_info(card)
        assert "en-us [2: af_alloy, af_bella]" in text
        assert "es [1: ef_dora]" in text
        assert "Alloy" not in text.replace(
            "af_alloy", ""
        ), "display names must not masquerade as values"

    def test_enrichment_collapses_identical_voice_lists(self):
        from src.middleware import _format_model_info

        langs = [
            {"name": n, "slug": s, "voices": [{"name": "Default", "slug": "default"}]}
            for n, s in [("Arabic", "ar"), ("German", "de"), ("English", "en")]
        ]
        langs.append({"name": "Greek", "slug": "el", "voices": []})
        text = _format_model_info(
            make_tts_card("Chatterbox", langs, {"supports_custom_voice": True})
        )
        assert "ar, de, en, el (4)" in text
        assert "same in every language): default" in text
        assert "no voice listed for: el" in text
        assert "mode=custom_voice" in text

    def test_enrichment_voice_clone_lists_languages_only(self):
        from src.middleware import _format_model_info

        langs = [
            {"name": "English", "slug": "English", "voices": []},
            {"name": "Italian", "slug": "Italian", "voices": []},
        ]
        text = _format_model_info(
            make_tts_card("Qwen3_TTS_12Hz_1_7B_Base", langs, {"supports_voice_clone": True})
        )
        assert "langs: English, Italian (voice from ref_audio)" in text
        assert "mode=voice_clone" in text

    def test_non_tts_model_unaffected(self):
        from src.middleware import _format_model_info
        from src.schemas import ModelInfo

        card = ModelInfo(
            name="Flux",
            slug="Flux1schnell",
            inference_types=["txt2img"],
            info={"limits": {"min_steps": 1, "max_steps": 10}},
        )
        assert "langs" not in _format_model_info(card)

    def test_tts_defaults_come_from_card(self):
        """Hardcoded 'en-us'/'flac' 422'd on every model; the card knows the right values."""
        from src.tools import audio

        card = make_tts_card(
            "Qwen3_TTS_12Hz_1_7B_CustomVoice",
            [
                {
                    "name": "English",
                    "slug": "English",
                    "voices": [{"name": "Vivian", "slug": "Vivian"}],
                }
            ],
            defaults={
                "lang": "English",
                "voice": "Vivian",
                "format": "mp3",
                "speed": 1,
                "sample_rate": 24000,
            },
        )
        with (
            patch("src.tools.audio.get_cached_model", return_value=card),
            patch("src.tools.audio.get_model_defaults", return_value=card.info["defaults"]),
        ):
            d = audio._tts_defaults("Qwen3_TTS_12Hz_1_7B_CustomVoice")
        assert d == {
            "voice": "Vivian",
            "lang": "English",
            "format": "mp3",
            "speed": 1,
            "sample_rate": 24000,
        }

    def test_tts_defaults_fall_back_to_first_language_and_voice(self):
        from src.tools import audio

        card = make_tts_card(
            "X", [{"name": "Polish", "slug": "pl", "voices": [{"name": "Ala", "slug": "pf_ala"}]}]
        )
        with (
            patch("src.tools.audio.get_cached_model", return_value=card),
            patch("src.tools.audio.get_model_defaults", return_value={}),
        ):
            d = audio._tts_defaults("X")
        assert d["lang"] == "pl"
        assert d["voice"] == "pf_ala"

    @pytest.mark.asyncio
    async def test_text_to_audio_price_uses_card_defaults_when_unset(self):
        """Kokoro emits mp3 only; the old hardcoded 'flac' default was a guaranteed 422."""
        from src.tools import audio

        client = make_client()
        card = make_tts_card(
            "Kokoro",
            [
                {
                    "name": "English (US)",
                    "slug": "en-us",
                    "voices": [{"name": "Alloy", "slug": "af_alloy"}],
                }
            ],
            defaults={"lang": "en-us", "voice": "af_alloy", "format": "mp3"},
        )
        with (
            patch("src.tools.audio.get_client", return_value=client),
            patch("src.tools.audio.get_cached_model", return_value=card),
            patch("src.tools.audio.get_model_defaults", return_value=card.info["defaults"]),
            patch("src.tools.audio.get_model_features", return_value={}),
        ):
            await audio.text_to_audio_price(text="hi", model="Kokoro")

        sent = client.calculate_price.call_args.kwargs["json_data"]
        assert sent["format"] == "mp3"
        assert sent["lang"] == "en-us"
        assert sent["voice"] == "af_alloy"

    def test_enrichment_shows_ref_audio_duration_bound_for_voice_clone(self):
        """The API rejects clips outside min/max_ref_audio_duration; say so up front."""
        from src.middleware import _format_model_info

        card = make_tts_card(
            "Qwen3_TTS_12Hz_1_7B_Base",
            [{"name": "English", "slug": "English", "voices": []}],
            {"supports_voice_clone": True},
        )
        card.info["limits"].update({"min_ref_audio_duration": 5, "max_ref_audio_duration": 15})
        assert "(voice from ref_audio, 5-15s)" in _format_model_info(card)


# =============================================================================
# Level 1: per-request bounds the API enforces, shown in the enrichment
# =============================================================================


def make_card(slug, limits, inference_types=("txt2img",)):
    from src.schemas import ModelInfo

    return ModelInfo(
        name=slug, slug=slug, inference_types=list(inference_types), info={"limits": limits}
    )


class TestRequestBoundsEnrichment:

    def test_image_bounds(self):
        """The video-upscale smoke test failed on exactly this: 1024x1024 > 1,032,192 px."""
        from src.middleware import _format_model_info

        text = _format_model_info(
            make_card(
                "RealESRGAN_Vid_x4",
                {"max_pixels": 1032192, "max_side": 1344, "resolution_step": 16},
            )
        )
        assert "≤1.03Mpx" in text
        assert "side≤1344" in text
        assert "w/h multiple of 16" in text

    def test_input_images_and_quality_options(self):
        """gpt-image-2 accepts quality=auto, gpt-image-1.5 does not — a hardcoded enum would be wrong."""
        from src.middleware import _format_model_info

        t15 = _format_model_info(
            make_card(
                "gpt-image-1.5",
                {"max_input_images": 2, "quality_options": ["low", "medium", "high"]},
            )
        )
        t2 = _format_model_info(
            make_card(
                "gpt-image-2",
                {"max_input_images": 2, "quality_options": ["low", "medium", "high", "auto"]},
            )
        )
        assert "up to 2 input image(s)" in t15
        assert "quality: low|medium|high" in t15 and "auto" not in t15
        assert "quality: low|medium|high|auto" in t2

    def test_timestamp_levels(self):
        """P5's caveat made visible: only Ct2 does word-level."""
        from src.middleware import _format_model_info

        assert "ts_level: segment" in _format_model_info(
            make_card("WhisperLargeV3", {"timestamp_levels": ["segment"]})
        )
        assert "ts_level: segment|word" in _format_model_info(
            make_card("WhisperLargeV3Ct2", {"timestamp_levels": ["segment", "word"]})
        )

    def test_tts_text_speed_and_sample_rate(self):
        """Our speed Field says 0.1-3.0; Kokoro accepts 0.5-2. available_ratios holds sample rates."""
        from src.middleware import _format_model_info

        text = _format_model_info(
            make_card(
                "Kokoro",
                {
                    "min_text": 3,
                    "max_text": 10001,
                    "min_speed": 0.5,
                    "max_speed": 2,
                    "available_ratios": [24000],
                },
            )
        )
        assert "text=3-10001 chars" in text
        assert "speed=0.5-2" in text
        assert "sample_rate: 24000" in text

    def test_fixed_bounds_read_as_fixed(self):
        from src.middleware import _format_model_info

        text = _format_model_info(
            make_card(
                "Flux_2_Klein_4B_BF16",
                {"min_steps": 4, "max_steps": 4, "min_speed": 1, "max_speed": 1},
            )
        )
        assert "steps=4 (fixed)" in text
        assert "speed=1 (fixed)" in text

    def test_video_scale_caption_tokens(self):
        from src.middleware import _format_model_info

        assert "video≤8s" in _format_model_info(make_card("Wan", {"max_video_duration_seconds": 8}))
        assert "scale=2-4x" in _format_model_info(
            make_card("FlashVSR", {"min_scale": 2, "max_scale": 4})
        )
        assert "caption=3-300 chars" in _format_model_info(
            make_card("Ace", {"min_caption": 3, "max_caption": 300})
        )
        assert "≤8192 tokens/input" in _format_model_info(
            make_card("Bge", {"max_input_tokens": 8192})
        )

    def test_null_limits_render_nothing(self):
        """Most of these are null on most cards; a null must not produce text."""
        from src.middleware import _format_model_info

        text = _format_model_info(
            make_card(
                "ZImageTurbo_INT8",
                {
                    "max_side": None,
                    "max_pixels": None,
                    "max_ratio": None,
                    "quality_options": [],
                    "min_scale": None,
                    "max_scale": None,
                },
            )
        )
        assert text.strip() == "- `ZImageTurbo_INT8`"


# =============================================================================
# QA round 1: prompt_boost is an object; a submitted job always keeps its id
# =============================================================================


PROMPT_BOOST = {
    "prompt": "Serene mountain lake at sunrise, 85mm lens, f/2.8",
    "prompt_original": "a serene mountain lake at sunrise",
    "negative_prompt": "blurry, low quality",
    "negative_prompt_original": None,
}


class TestPromptBoost:
    """QA blocker: enhance_prompt=true made every status read fail.

    The API returns prompt_boost as an object (JobRequestResultResource::getPromptBoost),
    the schema said str, so Pydantic rejected the payload after the job had run and
    been paid for. The caller got "Unexpected error" with no job_id.
    """

    def test_status_with_prompt_boost_object_parses(self):
        data = JobStatusData(
            status="done",
            result_url="https://r/x.png",
            prompt_boosted=True,
            prompt_boost=PROMPT_BOOST,
        )
        assert data.prompt_boost.prompt.startswith("Serene")
        assert data.prompt_boost.prompt_original == "a serene mountain lake at sunrise"

    @pytest.mark.asyncio
    async def test_check_job_status_returns_prompt_boost_as_dict(self):
        client = make_client()
        client.get_job_status = AsyncMock(
            return_value=JobStatusResponse(
                data=JobStatusData(
                    status="done",
                    result_url="https://r/x.png",
                    prompt_boosted=True,
                    prompt_boost=PROMPT_BOOST,
                )
            )
        )
        with patch("src.tools.utility.get_client", return_value=client):
            from src.tools.utility import check_job_status

            result = await check_job_status("job-1")

        assert result["success"] is True
        assert result["prompt_boosted"] is True
        assert result["prompt_boost"] == PROMPT_BOOST

    @pytest.mark.asyncio
    async def test_text_to_image_surfaces_the_boosted_prompt(self):
        from src.schemas import PromptBoost, ToolResult

        client = make_client()
        polling = MagicMock()
        polling.poll_until_complete = AsyncMock(
            return_value=ToolResult(
                success=True,
                job_id="job-1",
                result_url="https://r/x.png",
                prompt_boost=PromptBoost(**PROMPT_BOOST),
            )
        )
        with (
            patch("src.tools.image.get_client", return_value=client),
            patch("src.tools.image.PollingManager", return_value=polling),
        ):
            from src.tools.image import text_to_image

            result = await text_to_image(prompt="lake", model="Flux1schnell", enhance_prompt=True)

        assert result["success"] is True
        assert result["result_url"] == "https://r/x.png"
        assert result["prompt_boost"]["prompt"].startswith("Serene")


class TestPollerKeepsJobId:
    """Whatever goes wrong after submission, the caller must get the job_id back:
    the job exists, may finish, and has been charged for.
    """

    @pytest.mark.asyncio
    async def test_unreadable_status_payload_returns_failed_result_with_job_id(self):
        from src.polling_manager import PollingManager

        client = AsyncMock()

        def bad_payload(job_id):
            # What the old str-typed prompt_boost produced: a parse error, not an API error.
            JobStatusData(status="done", prompt_boost="not an object")

        client.get_job_status = AsyncMock(side_effect=bad_payload)
        with patch("src.polling_manager.settings") as settings:
            from src.config import PollingConfig

            settings.get_polling_config.return_value = PollingConfig(
                initial_delay=0, max_delay=0, timeout=60
            )
            result = await PollingManager(client, "image").poll_until_complete("job-9")

        assert result.success is False
        assert result.job_id == "job-9"
        assert "job-9" in result.error and "check_job_status" in result.error

    @pytest.mark.asyncio
    async def test_timeout_returns_failed_result_with_job_id_instead_of_raising(self):
        from src.polling_manager import PollingManager

        client = AsyncMock()
        client.get_job_status = AsyncMock(
            return_value=JobStatusResponse(data=JobStatusData(status="processing"))
        )
        with patch("src.polling_manager.settings") as settings:
            from src.config import PollingConfig

            settings.get_polling_config.return_value = PollingConfig(
                initial_delay=0, max_delay=0, timeout=-1  # elapsed > timeout on the first pass
            )
            result = await PollingManager(client, "video").poll_until_complete("job-9")

        assert result.success is False
        assert result.job_id == "job-9"
        assert "timed out" in result.error
        assert result.metadata["timed_out"] is True

    @pytest.mark.asyncio
    async def test_unknown_job_id_does_not_leak_the_laravel_class(self):
        from src.deapi_client import DeapiAPIError

        client = make_client()
        client.get_job_status = AsyncMock(
            side_effect=DeapiAPIError(
                "No query results for model [App\\Models\\JobRequest] job-x", status_code=404
            )
        )
        with patch("src.tools.utility.get_client", return_value=client):
            from src.tools.utility import check_job_status

            result = await check_job_status("job-x")

        assert result["success"] is False
        assert result["job_id"] == "job-x"
        assert result["error"] == "Job job-x not found"


# =============================================================================
# QA round 1: tool defaults must come from the model card, not the signature
# =============================================================================


def _card(slug, types, defaults):
    from src.schemas import ModelInfo

    return ModelInfo(name=slug, slug=slug, inference_types=types, info={"defaults": defaults})


# The cards QA hit, as served by /api/v2/models on 2026-09-15.
FLUX_SCHNELL = _card("Flux1schnell", ["txt2img"], {"steps": 4, "width": 768, "height": 768})
LTXV = _card(
    "Ltxv_13B_0_9_8_Distilled_FP8",
    ["txt2video", "img2video"],
    {"fps": 30, "steps": 1, "width": 512, "frames": 120, "height": 512},
)
LTX25 = _card(
    "Ltx2_5_22B_Dist_INT8",
    ["txt2video", "img2video", "audio2video"],
    {"fps": 24, "width": 768, "frames": 120, "height": 768},
)


@contextmanager
def patched_with_card(module, card):
    """Run a tool against one cached card; yields the mock client to read the payload."""
    from src.middleware import _cache

    _cache.models_by_slug[card.slug] = card
    client = make_client()
    polling = MagicMock()
    polling.poll_until_complete = AsyncMock(return_value=make_poll(result_url="u"))
    with (
        patch(f"src.tools.{module}.get_client", return_value=client),
        patch(f"src.tools.{module}.PollingManager", return_value=polling),
        patch(f"src.tools.{module}.ensure_model_cache", new=AsyncMock()),
    ):
        yield client


class TestDefaultsComeFromTheCard:
    """QA blocker: a call with no parameters returned 422 on Flux1schnell (steps=20,
    max 10), Ltxv (frames=20/fps=24, both fixed elsewhere) and Ltx2.5 (fps=30, max 24).
    The API requires these fields and validates them against the card, so the only
    default that works is the card's own.
    """

    def teardown_method(self):
        from src.middleware import _cache

        _cache.models_by_slug.clear()

    @pytest.mark.asyncio
    async def test_text_to_image_flux_schnell(self):
        with patched_with_card("image", FLUX_SCHNELL) as client:
            from src.tools.image import text_to_image

            await text_to_image(prompt="a cat", model="Flux1schnell")
            sent = client.submit_job.call_args.kwargs["json_data"]

        assert sent["steps"] == 4
        assert sent["width"] == 768 and sent["height"] == 768
        assert "guidance" not in sent, "the card has no guidance default; nothing is invented"

    @pytest.mark.asyncio
    async def test_text_to_video_ltxv(self):
        with patched_with_card("video", LTXV) as client:
            from src.tools.video import text_to_video

            await text_to_video(prompt="a cat", model=LTXV.slug)
            sent = client.submit_job.call_args.kwargs["data"]

        assert (sent["fps"], sent["frames"], sent["steps"]) == ("30", "120", "1")
        assert (sent["width"], sent["height"]) == ("512", "512")

    @pytest.mark.asyncio
    async def test_image_to_video_ltx25(self):
        with patched_with_card("video", LTX25) as client:
            with patch(
                "src.tools.video.prepare_image_upload_async",
                new=AsyncMock(return_value=("first_frame_image", ("f.png", b"x", "image/png"))),
            ):
                from src.tools.video import image_to_video

                await image_to_video(first_frame_image="data:x", prompt="p", model=LTX25.slug)
            sent = client.submit_job.call_args.kwargs["data"]

        assert (sent["fps"], sent["frames"]) == ("24", "120")
        assert "steps" not in sent, "Ltx2.5 neither defaults nor supports steps"

    @pytest.mark.asyncio
    async def test_audio_to_video_no_longer_requires_dimensions(self):
        with patched_with_card("video", LTX25) as client:
            with patch(
                "src.tools.video.prepare_audio_upload_async",
                new=AsyncMock(return_value=("audio", ("a.mp3", b"x", "audio/mpeg"))),
            ):
                from src.tools.video import audio_to_video

                await audio_to_video(prompt="p", model=LTX25.slug, audio="data:x")
            sent = client.submit_job.call_args.kwargs["data"]

        assert (sent["width"], sent["height"], sent["fps"], sent["frames"]) == (
            "768",
            "768",
            "24",
            "120",
        )

    @pytest.mark.asyncio
    async def test_explicit_value_still_wins(self):
        with patched_with_card("video", LTXV) as client:
            from src.tools.video import text_to_video

            await text_to_video(prompt="a cat", model=LTXV.slug, frames=60)
            sent = client.submit_job.call_args.kwargs["data"]

        assert sent["frames"] == "60"
        assert sent["fps"] == "30"
