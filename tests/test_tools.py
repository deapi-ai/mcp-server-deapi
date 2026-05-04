"""Tests for new and fixed MCP tool functions.

Verifies:
- Bug fixes: audio_transcription and image_to_text use multipart (not JSON)
- New tools: correct endpoints, parameter mapping, content-types
"""

import base64
import io
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# =============================================================================
# Helpers
# =============================================================================


def make_base64_audio():
    """Return a valid base64 data URI for audio."""
    raw = b"fake-mp3-audio-data"
    encoded = base64.b64encode(raw).decode()
    return f"data:audio/mp3;base64,{encoded}"


def make_base64_image():
    """Return a valid base64 data URI for image."""
    raw = b"fake-png-image-data"
    encoded = base64.b64encode(raw).decode()
    return f"data:image/png;base64,{encoded}"


def make_base64_video():
    """Return a valid base64 data URI for video."""
    raw = b"fake-mp4-video-data"
    encoded = base64.b64encode(raw).decode()
    return f"data:video/mp4;base64,{encoded}"


def make_mock_client():
    """Create a mock DeapiClient with proper async context manager."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    # Mock submit_job response
    job_response = MagicMock()
    job_response.data.request_id = "test-job-id-123"
    client.submit_job = AsyncMock(return_value=job_response)

    # Mock calculate_price response
    client.calculate_price = AsyncMock(return_value={"data": {"price": 0.05, "currency": "USD"}})

    return client


def make_mock_poll_result(success=True, result="test-result", result_url="https://result.url/file"):
    """Create a mock polling result."""
    result_mock = MagicMock()
    result_mock.success = success
    result_mock.result = result
    result_mock.result_url = result_url
    result_mock.error = None if success else "Job failed"
    result_mock.metadata = {"processing_time": 1.5}
    return result_mock


# =============================================================================
# BUG FIX: audio_transcription must use multipart/form-data
# =============================================================================


class TestAudioTranscriptionBugFix:
    """Verify audio_transcription sends multipart, not JSON."""

    @pytest.mark.asyncio
    async def test_sends_multipart_not_json(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import audio_transcription

            result = await audio_transcription(
                audio=make_base64_audio(),
                include_ts=True,
                model="whisper-3-large",
            )

        assert result["success"] is True
        assert result["job_id"] == "test-job-id-123"

        # THE KEY ASSERTION: must use data= and files=, NOT json_data=
        call_kwargs = mock_client.submit_job.call_args
        assert call_kwargs.kwargs.get("endpoint") == "audio/transcriptions"
        assert "data" in call_kwargs.kwargs, "Must use 'data' param (multipart form)"
        assert "files" in call_kwargs.kwargs, "Must use 'files' param (multipart file)"
        assert "json_data" not in call_kwargs.kwargs or call_kwargs.kwargs["json_data"] is None, \
            "Must NOT use json_data (was the bug)"

    @pytest.mark.asyncio
    async def test_form_data_boolean_serialization(self):
        """Verify booleans are serialized as lowercase strings in form data."""
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import audio_transcription

            await audio_transcription(
                audio=make_base64_audio(),
                include_ts=False,
                return_result_in_response=True,
            )

        call_kwargs = mock_client.submit_job.call_args.kwargs
        form_data = call_kwargs["data"]
        assert form_data["include_ts"] == "false"
        assert form_data["return_result_in_response"] == "true"

    @pytest.mark.asyncio
    async def test_files_contain_source_file_field(self):
        """Verify the audio file is sent under the v2 'source_file' field name."""
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import audio_transcription

            await audio_transcription(audio=make_base64_audio(), include_ts=True)

        call_kwargs = mock_client.submit_job.call_args.kwargs
        files = call_kwargs["files"]
        assert "source_file" in files
        filename, file_obj, mime_type = files["source_file"]
        assert filename == "audio.mp3"
        assert mime_type == "audio/mpeg"
        assert isinstance(file_obj, io.BytesIO)


# =============================================================================
# BUG FIX: image_to_text must use multipart/form-data
# =============================================================================


class TestImageToTextBugFix:
    """Verify image_to_text sends multipart, not JSON."""

    @pytest.mark.asyncio
    async def test_sends_multipart_not_json(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.image.get_client", return_value=mock_client), \
             patch("src.tools.image.PollingManager", return_value=mock_polling):
            from src.tools.image import image_to_text

            result = await image_to_text(
                image=make_base64_image(),
                model="Nanonets_Ocr_S_F16",
            )

        assert result["success"] is True

        call_kwargs = mock_client.submit_job.call_args
        assert call_kwargs.kwargs.get("endpoint") == "images/ocr"
        assert "data" in call_kwargs.kwargs, "Must use 'data' param (multipart form)"
        assert "files" in call_kwargs.kwargs, "Must use 'files' param (multipart file)"
        assert "json_data" not in call_kwargs.kwargs or call_kwargs.kwargs["json_data"] is None, \
            "Must NOT use json_data (was the bug)"

    @pytest.mark.asyncio
    async def test_form_data_contains_model_and_format(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.image.get_client", return_value=mock_client), \
             patch("src.tools.image.PollingManager", return_value=mock_polling):
            from src.tools.image import image_to_text

            await image_to_text(
                image=make_base64_image(),
                model="Nanonets_Ocr_S_F16",
                format="json",
                language="en",
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert form_data["model"] == "Nanonets_Ocr_S_F16"
        assert form_data["format"] == "json"
        assert form_data["language"] == "en"

    @pytest.mark.asyncio
    async def test_files_contain_image_field(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.image.get_client", return_value=mock_client), \
             patch("src.tools.image.PollingManager", return_value=mock_polling):
            from src.tools.image import image_to_text

            await image_to_text(image=make_base64_image(), model="test")

        files = mock_client.submit_job.call_args.kwargs["files"]
        assert "image" in files
        filename, file_obj, mime_type = files["image"]
        assert filename == "image.png"
        assert mime_type == "image/png"


# =============================================================================
# NEW TOOL: text_to_audio_price
# =============================================================================


class TestTextToAudioPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint_and_params(self):
        mock_client = make_mock_client()

        with patch("src.tools.audio.get_client", return_value=mock_client):
            from src.tools.audio import text_to_audio_price

            result = await text_to_audio_price(
                text="Hello world",
                model="Kokoro",
                voice="af_sky",
                lang="en-us",
                speed=1.0,
                audio_format="flac",
                sample_rate=24000,
            )

        assert result["success"] is True
        assert "price" in result

        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "audio/speech/price"
        json_data = call_kwargs["json_data"]
        assert json_data["text"] == "Hello world"
        assert json_data["model"] == "Kokoro"
        assert json_data["voice"] == "af_sky"
        assert json_data["format"] == "flac"  # audio_format → format


# =============================================================================
# NEW TOOL: text_to_embedding
# =============================================================================


class TestTextToEmbedding:
    @pytest.mark.asyncio
    async def test_single_string_input(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result(result=[[0.1, 0.2, 0.3]])
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.embedding.get_client", return_value=mock_client), \
             patch("src.tools.embedding.PollingManager", return_value=mock_polling):
            from src.tools.embedding import text_to_embedding

            result = await text_to_embedding(input="Hello world")

        assert result["success"] is True

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "embeddings"
        assert call_kwargs["json_data"]["input"] == "Hello world"
        assert call_kwargs["json_data"]["model"] == "Bge_M3_FP16"

    @pytest.mark.asyncio
    async def test_list_input(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.embedding.get_client", return_value=mock_client), \
             patch("src.tools.embedding.PollingManager", return_value=mock_polling):
            from src.tools.embedding import text_to_embedding

            await text_to_embedding(input=["Hello", "World"])

        json_data = mock_client.submit_job.call_args.kwargs["json_data"]
        assert json_data["input"] == ["Hello", "World"]

    @pytest.mark.asyncio
    async def test_uses_json_not_multipart(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.embedding.get_client", return_value=mock_client), \
             patch("src.tools.embedding.PollingManager", return_value=mock_polling):
            from src.tools.embedding import text_to_embedding

            await text_to_embedding(input="test")

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert "json_data" in call_kwargs
        assert call_kwargs.get("data") is None
        assert call_kwargs.get("files") is None


class TestTextToEmbeddingPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.embedding.get_client", return_value=mock_client):
            from src.tools.embedding import text_to_embedding_price

            result = await text_to_embedding_price(input="Hello")

        assert result["success"] is True
        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "embeddings/price"


# =============================================================================
# NEW TOOL: text_to_video_price
# =============================================================================


class TestTextToVideoPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint_and_json_data(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import text_to_video_price

            result = await text_to_video_price(
                model="test-model",
                width=512,
                height=512,
                frames=20,
                steps=20,
            )

        assert result["success"] is True

        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/generations/price"
        # txt2video price uses JSON (not form-data)
        json_data = call_kwargs["json_data"]
        assert json_data["model"] == "test-model"
        assert json_data["width"] == 512
        assert json_data["height"] == 512

    @pytest.mark.asyncio
    async def test_optional_fps(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import text_to_video_price

            # Without fps — may be populated from model cache defaults
            await text_to_video_price(model="test")
            json_data = mock_client.calculate_price.call_args.kwargs["json_data"]
            # fps is optional per API spec

            # With fps — should be included
            await text_to_video_price(model="test", fps=30)
            json_data = mock_client.calculate_price.call_args.kwargs["json_data"]
            assert json_data["fps"] == 30


# =============================================================================
# NEW TOOL: video_remove_background
# =============================================================================


class TestVideoRemoveBackground:
    @pytest.mark.asyncio
    async def test_sends_multipart_to_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_remove_background

            result = await video_remove_background(
                video=make_base64_video(),
                model="test-rmbg-model",
            )

        assert result["success"] is True
        assert result["result_url"] == "https://result.url/file"

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/background-removals"
        assert "files" in call_kwargs
        assert "video" in call_kwargs["files"]
        assert call_kwargs["data"]["model"] == "test-rmbg-model"

    @pytest.mark.asyncio
    async def test_uses_video_polling_type(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling_cls = MagicMock()
        mock_polling_instance = MagicMock()
        mock_polling_instance.poll_until_complete = AsyncMock(return_value=mock_poll_result)
        mock_polling_cls.return_value = mock_polling_instance

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", mock_polling_cls):
            from src.tools.video import video_remove_background

            await video_remove_background(video=make_base64_video(), model="test")

        mock_polling_cls.assert_called_once_with(mock_client, job_type="video")


class TestVideoRemoveBackgroundPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_remove_background_price

            result = await video_remove_background_price(model="test", width=1920, height=1080)

        assert result["success"] is True
        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/background-removals/price"
        assert call_kwargs["data"]["width"] == "1920"
        assert call_kwargs["data"]["height"] == "1080"


# =============================================================================
# NEW TOOL: video_upscale
# =============================================================================


class TestVideoUpscale:
    @pytest.mark.asyncio
    async def test_sends_multipart_to_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_upscale

            result = await video_upscale(
                video=make_base64_video(),
                model="test-upscale-model",
            )

        assert result["success"] is True

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/upscales"
        assert "files" in call_kwargs
        assert "video" in call_kwargs["files"]
        assert call_kwargs["data"]["model"] == "test-upscale-model"
        # scale is optional and omitted when not provided
        assert "scale" not in call_kwargs["data"]

    @pytest.mark.asyncio
    async def test_scale_param_forwarded_when_provided(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_upscale

            await video_upscale(
                video=make_base64_video(),
                model="RealESRGAN",
                scale=4,
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert form_data["scale"] == "4"


class TestVideoUpscalePrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_upscale_price

            result = await video_upscale_price(model="test")

        assert result["success"] is True
        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/upscales/price"

    @pytest.mark.asyncio
    async def test_optional_dimensions(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_upscale_price

            # Without dimensions
            await video_upscale_price(model="test")
            form_data = mock_client.calculate_price.call_args.kwargs["data"]
            assert "width" not in form_data
            assert "height" not in form_data

            # With dimensions
            await video_upscale_price(model="test", width=3840, height=2160)
            form_data = mock_client.calculate_price.call_args.kwargs["data"]
            assert form_data["width"] == "3840"
            assert form_data["height"] == "2160"

    @pytest.mark.asyncio
    async def test_scale_and_duration_forwarded(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_upscale_price

            await video_upscale_price(
                model="RealESRGAN", width=1920, height=1080, scale=4, duration=12.5
            )

        form_data = mock_client.calculate_price.call_args.kwargs["data"]
        assert form_data["scale"] == "4"
        assert form_data["duration"] == "12.5"


# =============================================================================
# NEW TOOL: video_replace
# =============================================================================


class TestVideoReplace:
    @pytest.mark.asyncio
    async def test_sends_multipart_to_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_replace

            result = await video_replace(
                video=make_base64_video(),
                ref_image=make_base64_image(),
                model="test-replace-model",
                prompt="replace character",
                steps=4,
                seed=42,
            )

        assert result["success"] is True
        assert result["result_url"] == "https://result.url/file"

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/replacements"
        assert "files" in call_kwargs
        assert "video" in call_kwargs["files"]
        assert "ref_image" in call_kwargs["files"]
        assert call_kwargs["data"]["model"] == "test-replace-model"
        assert call_kwargs["data"]["prompt"] == "replace character"
        assert call_kwargs["data"]["steps"] == "4"
        assert call_kwargs["data"]["seed"] == "42"

    @pytest.mark.asyncio
    async def test_optional_params(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_replace

            # Without optional params
            await video_replace(
                video=make_base64_video(),
                ref_image=make_base64_image(),
                model="test-model",
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert "prompt" not in form_data
        assert "width" not in form_data
        assert "height" not in form_data

    @pytest.mark.asyncio
    async def test_with_dimensions(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import video_replace

            await video_replace(
                video=make_base64_video(),
                ref_image=make_base64_image(),
                model="test-model",
                width=1024,
                height=768,
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert form_data["width"] == "1024"
        assert form_data["height"] == "768"

    @pytest.mark.asyncio
    async def test_uses_video_polling_type(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling_cls = MagicMock()
        mock_polling_instance = MagicMock()
        mock_polling_instance.poll_until_complete = AsyncMock(return_value=mock_poll_result)
        mock_polling_cls.return_value = mock_polling_instance

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", mock_polling_cls):
            from src.tools.video import video_replace

            await video_replace(
                video=make_base64_video(),
                ref_image=make_base64_image(),
                model="test",
            )

        mock_polling_cls.assert_called_once_with(mock_client, job_type="video")


class TestVideoReplacePrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_replace_price

            result = await video_replace_price(model="test", duration=5.0)

        assert result["success"] is True
        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/replacements/price"
        assert call_kwargs["data"]["model"] == "test"
        assert call_kwargs["data"]["duration"] == "5.0"

    @pytest.mark.asyncio
    async def test_optional_dimensions(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_replace_price

            # Without dimensions
            await video_replace_price(model="test", duration=3.0)
            form_data = mock_client.calculate_price.call_args.kwargs["data"]
            assert "width" not in form_data
            assert "height" not in form_data

            # With dimensions
            await video_replace_price(model="test", duration=3.0, width=1920, height=1080)
            form_data = mock_client.calculate_price.call_args.kwargs["data"]
            assert form_data["width"] == "1920"
            assert form_data["height"] == "1080"


# =============================================================================
# ERROR HANDLING: verify all new tools handle errors consistently
# =============================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_audio_transcription_invalid_input(self):
        """audio_transcription should catch ValueError from bad audio input."""
        mock_client = make_mock_client()

        with patch("src.tools.audio.get_client", return_value=mock_client):
            from src.tools.audio import audio_transcription

            result = await audio_transcription(
                audio="not-valid-base64!!!",
                include_ts=True,
            )

        assert result["success"] is False
        assert "Invalid audio format" in result["error"]

    @pytest.mark.asyncio
    async def test_image_to_text_invalid_input(self):
        """image_to_text should catch ValueError from bad image input."""
        mock_client = make_mock_client()

        with patch("src.tools.image.get_client", return_value=mock_client):
            from src.tools.image import image_to_text

            result = await image_to_text(
                image="not-valid-base64!!!",
                model="test",
            )

        assert result["success"] is False
        assert "Invalid image format" in result["error"]

    @pytest.mark.asyncio
    async def test_video_replace_invalid_video(self):
        """video_replace should catch ValueError from bad video input."""
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_replace

            result = await video_replace(
                video="not-valid-base64!!!",
                ref_image=make_base64_image(),
                model="test",
            )

        assert result["success"] is False
        assert "Invalid file format" in result["error"]

    @pytest.mark.asyncio
    async def test_video_replace_invalid_ref_image(self):
        """video_replace should catch ValueError from bad ref_image input."""
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_replace

            result = await video_replace(
                video=make_base64_video(),
                ref_image="not-valid-base64!!!",
                model="test",
            )

        assert result["success"] is False
        assert "Invalid file format" in result["error"]

    @pytest.mark.asyncio
    async def test_video_remove_background_invalid_input(self):
        """video_remove_background should catch ValueError from bad video input."""
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_remove_background

            result = await video_remove_background(
                video="not-valid-base64!!!",
                model="test",
            )

        assert result["success"] is False
        assert "Invalid video format" in result["error"]

    @pytest.mark.asyncio
    async def test_video_upscale_invalid_input(self):
        """video_upscale should catch ValueError from bad video input."""
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_upscale

            result = await video_upscale(
                video="not-valid-base64!!!",
                model="test",
            )

        assert result["success"] is False
        assert "Invalid video format" in result["error"]

    @pytest.mark.asyncio
    async def test_text_to_music_invalid_reference_audio(self):
        """text_to_music should catch ValueError from bad reference audio."""
        mock_client = make_mock_client()

        with patch("src.tools.audio.get_client", return_value=mock_client):
            from src.tools.audio import text_to_music

            result = await text_to_music(
                caption="upbeat pop song",
                model="test-music-model",
                lyrics="[Instrumental]",
                duration=30,
                inference_steps=32,
                guidance_scale=3.5,
                reference_audio="not-valid-base64!!!",
            )

        assert result["success"] is False
        assert "Invalid audio format" in result["error"]

    @pytest.mark.asyncio
    async def test_audio_to_video_invalid_audio(self):
        """audio_to_video should catch ValueError from bad audio input."""
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import audio_to_video

            result = await audio_to_video(
                prompt="music video",
                model="test-model",
                audio="not-valid-base64!!!",
                width=512,
                height=512,
                frames=120,
                fps=30,
            )

        assert result["success"] is False
        assert "Invalid file format" in result["error"]

    @pytest.mark.asyncio
    async def test_embedding_api_error(self):
        """text_to_embedding should catch DeapiAPIError."""
        mock_client = make_mock_client()
        from src.deapi_client import DeapiAPIError
        mock_client.submit_job.side_effect = DeapiAPIError("Auth failed", status_code=401)

        with patch("src.tools.embedding.get_client", return_value=mock_client):
            from src.tools.embedding import text_to_embedding

            result = await text_to_embedding(input="test")

        assert result["success"] is False
        assert "API error" in result["error"]


# =============================================================================
# NEW TOOL: text_to_music
# =============================================================================


class TestTextToMusic:
    @pytest.mark.asyncio
    async def test_sends_multipart_to_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import text_to_music

            result = await text_to_music(
                caption="upbeat pop song",
                model="test-music-model",
                lyrics="La la la",
                duration=30,
                inference_steps=32,
                guidance_scale=3.5,
                seed=42,
                audio_format="wav",
            )

        assert result["success"] is True
        assert result["result_url"] == "https://result.url/file"
        assert result["job_id"] == "test-job-id-123"

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "audio/music"
        assert "data" in call_kwargs
        form_data = call_kwargs["data"]
        assert form_data["caption"] == "upbeat pop song"
        assert form_data["model"] == "test-music-model"
        assert form_data["lyrics"] == "La la la"
        assert form_data["duration"] == "30"
        assert form_data["inference_steps"] == "32"
        assert form_data["guidance_scale"] == "3.5"
        assert form_data["seed"] == "42"
        assert form_data["format"] == "wav"

    @pytest.mark.asyncio
    async def test_optional_params_included_when_set(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import text_to_music

            await text_to_music(
                caption="jazz tune",
                model="test",
                lyrics="[Instrumental]",
                duration=60,
                inference_steps=8,
                guidance_scale=0.0,
                bpm=120,
                keyscale="C major",
                timesignature=4,
                vocal_language="en",
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert form_data["bpm"] == "120"
        assert form_data["keyscale"] == "C major"
        assert form_data["timesignature"] == "4"
        assert form_data["vocal_language"] == "en"

    @pytest.mark.asyncio
    async def test_optional_params_omitted_when_none(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import text_to_music

            await text_to_music(
                caption="test",
                model="test",
                lyrics="test",
                duration=10,
                inference_steps=8,
                guidance_scale=0.0,
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert "bpm" not in form_data
        assert "keyscale" not in form_data
        assert "timesignature" not in form_data
        assert "vocal_language" not in form_data

    @pytest.mark.asyncio
    async def test_no_files_when_no_reference_audio(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import text_to_music

            await text_to_music(
                caption="test",
                model="test",
                lyrics="test",
                duration=10,
                inference_steps=8,
                guidance_scale=0.0,
            )

        call_kwargs = mock_client.submit_job.call_args.kwargs
        # files should be None when no reference_audio
        assert call_kwargs.get("files") is None

    @pytest.mark.asyncio
    async def test_reference_audio_sent_as_file(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", return_value=mock_polling):
            from src.tools.audio import text_to_music

            await text_to_music(
                caption="test",
                model="test",
                lyrics="test",
                duration=10,
                inference_steps=8,
                guidance_scale=0.0,
                reference_audio=make_base64_audio(),
            )

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["files"] is not None
        assert "reference_audio" in call_kwargs["files"]

    @pytest.mark.asyncio
    async def test_uses_audio_polling_type(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling_cls = MagicMock()
        mock_polling_instance = MagicMock()
        mock_polling_instance.poll_until_complete = AsyncMock(return_value=mock_poll_result)
        mock_polling_cls.return_value = mock_polling_instance

        with patch("src.tools.audio.get_client", return_value=mock_client), \
             patch("src.tools.audio.PollingManager", mock_polling_cls):
            from src.tools.audio import text_to_music

            await text_to_music(
                caption="test", model="test", lyrics="test",
                duration=10, inference_steps=8, guidance_scale=0.0,
            )

        mock_polling_cls.assert_called_once_with(mock_client, job_type="audio")


class TestTextToMusicPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint_and_form_data(self):
        mock_client = make_mock_client()

        with patch("src.tools.audio.get_client", return_value=mock_client):
            from src.tools.audio import text_to_music_price

            result = await text_to_music_price(
                model="test-music-model",
                duration=60,
                inference_steps=32,
            )

        assert result["success"] is True
        assert "price" in result

        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "audio/music/price"
        form_data = call_kwargs["data"]
        assert form_data["model"] == "test-music-model"
        assert form_data["duration"] == "60"
        assert form_data["inference_steps"] == "32"

    @pytest.mark.asyncio
    async def test_optional_params(self):
        mock_client = make_mock_client()

        with patch("src.tools.audio.get_client", return_value=mock_client):
            from src.tools.audio import text_to_music_price

            # Without optional params
            await text_to_music_price(model="test")
            form_data = mock_client.calculate_price.call_args.kwargs["data"]
            assert "duration" not in form_data
            assert "inference_steps" not in form_data


# =============================================================================
# NEW TOOL: audio_to_video
# =============================================================================


class TestAudioToVideo:
    @pytest.mark.asyncio
    async def test_sends_multipart_to_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import audio_to_video

            result = await audio_to_video(
                prompt="music video",
                model="test-aud2vid-model",
                audio=make_base64_audio(),
                width=768,
                height=512,
                frames=120,
                fps=30,
                seed=42,
            )

        assert result["success"] is True
        assert result["result_url"] == "https://result.url/file"
        assert result["job_id"] == "test-job-id-123"

        call_kwargs = mock_client.submit_job.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/audio-syncs"
        assert "data" in call_kwargs
        assert "files" in call_kwargs

        form_data = call_kwargs["data"]
        assert form_data["prompt"] == "music video"
        assert form_data["model"] == "test-aud2vid-model"
        assert form_data["width"] == "768"
        assert form_data["height"] == "512"
        assert form_data["frames"] == "120"
        assert form_data["fps"] == "30"
        assert form_data["seed"] == "42"

        # Audio file must be present
        assert "audio" in call_kwargs["files"]

    @pytest.mark.asyncio
    async def test_optional_params_included_when_set(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import audio_to_video

            await audio_to_video(
                prompt="test",
                model="test",
                audio=make_base64_audio(),
                width=512,
                height=512,
                frames=60,
                fps=24,
                negative_prompt="blurry",
                guidance=7.5,
                steps=20,
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert form_data["negative_prompt"] == "blurry"
        assert form_data["guidance"] == "7.5"
        assert form_data["steps"] == "20"

    @pytest.mark.asyncio
    async def test_optional_params_omitted_when_none(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import audio_to_video

            await audio_to_video(
                prompt="test",
                model="test",
                audio=make_base64_audio(),
                width=512,
                height=512,
                frames=60,
                fps=24,
            )

        form_data = mock_client.submit_job.call_args.kwargs["data"]
        assert "negative_prompt" not in form_data
        assert "guidance" not in form_data
        assert "steps" not in form_data

    @pytest.mark.asyncio
    async def test_frame_images_sent_as_files(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling = MagicMock()
        mock_polling.poll_until_complete = AsyncMock(return_value=mock_poll_result)

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", return_value=mock_polling):
            from src.tools.video import audio_to_video

            await audio_to_video(
                prompt="test",
                model="test",
                audio=make_base64_audio(),
                width=512,
                height=512,
                frames=60,
                fps=24,
                first_frame_image=make_base64_image(),
                last_frame_image=make_base64_image(),
            )

        files = mock_client.submit_job.call_args.kwargs["files"]
        assert "audio" in files
        assert "first_frame_image" in files
        assert "last_frame_image" in files

    @pytest.mark.asyncio
    async def test_uses_video_polling_type(self):
        mock_client = make_mock_client()
        mock_poll_result = make_mock_poll_result()
        mock_polling_cls = MagicMock()
        mock_polling_instance = MagicMock()
        mock_polling_instance.poll_until_complete = AsyncMock(return_value=mock_poll_result)
        mock_polling_cls.return_value = mock_polling_instance

        with patch("src.tools.video.get_client", return_value=mock_client), \
             patch("src.tools.video.PollingManager", mock_polling_cls):
            from src.tools.video import audio_to_video

            await audio_to_video(
                prompt="test", model="test", audio=make_base64_audio(),
                width=512, height=512, frames=60, fps=24,
            )

        mock_polling_cls.assert_called_once_with(mock_client, job_type="video")


class TestAudioToVideoPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import audio_to_video_price

            result = await audio_to_video_price(
                model="test-model",
                width=768,
                height=512,
                frames=120,
                steps=20,
                fps=30,
            )

        assert result["success"] is True
        assert "price" in result

        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "videos/audio-syncs/price"
        json_data = call_kwargs["json_data"]
        assert json_data["model"] == "test-model"
        assert json_data["width"] == 768
        assert json_data["height"] == 512
        assert json_data["frames"] == 120

    @pytest.mark.asyncio
    async def test_seed_and_guidance_excluded(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import audio_to_video_price

            await audio_to_video_price(model="test", width=512, height=512, frames=60)

        json_data = mock_client.calculate_price.call_args.kwargs["json_data"]
        assert "seed" not in json_data
        assert "guidance" not in json_data


# =============================================================================
# NEW TOOL: prompt_booster (synchronous — returns enhanced prompt directly)
# =============================================================================


class TestPromptBooster:
    @pytest.mark.asyncio
    async def test_text_only_call_uses_post_sync(self):
        mock_client = make_mock_client()
        mock_client.post_sync = AsyncMock(return_value={
            "prompt": "an enhanced beautiful landscape",
            "negative_prompt": None,
        })

        with patch("src.tools.prompt.get_client", return_value=mock_client):
            from src.tools.prompt import prompt_booster

            result = await prompt_booster(
                prompt="a landscape",
                type="images.generations",
                model_slug="Flux1schnell",
            )

        assert result["success"] is True
        assert result["prompt"] == "an enhanced beautiful landscape"
        assert result["negative_prompt"] is None

        call_kwargs = mock_client.post_sync.call_args.kwargs
        assert call_kwargs["endpoint"] == "prompts/enhancements"
        form_data = call_kwargs["data"]
        assert form_data["prompt"] == "a landscape"
        assert form_data["type"] == "images.generations"
        assert form_data["model_slug"] == "Flux1schnell"
        assert "negative_prompt" not in form_data
        assert call_kwargs.get("files") is None

    @pytest.mark.asyncio
    async def test_with_image_uploads_multipart(self):
        mock_client = make_mock_client()
        mock_client.post_sync = AsyncMock(return_value={"prompt": "x"})

        with patch("src.tools.prompt.get_client", return_value=mock_client):
            from src.tools.prompt import prompt_booster

            await prompt_booster(
                prompt="edit this",
                type="images.edits",
                model_slug="Flux1schnell",
                negative_prompt="ugly",
                image=make_base64_image(),
            )

        call_kwargs = mock_client.post_sync.call_args.kwargs
        assert call_kwargs["data"]["negative_prompt"] == "ugly"
        assert call_kwargs["files"] is not None
        assert "image" in call_kwargs["files"]

    @pytest.mark.asyncio
    async def test_api_error_returned_in_dict(self):
        mock_client = make_mock_client()
        from src.deapi_client import DeapiAPIError
        mock_client.post_sync = AsyncMock(
            side_effect=DeapiAPIError("No guide available", status_code=422)
        )

        with patch("src.tools.prompt.get_client", return_value=mock_client):
            from src.tools.prompt import prompt_booster

            result = await prompt_booster(
                prompt="test",
                type="images.generations",
                model_slug="UnknownModel",
            )

        assert result["success"] is False
        assert "API error" in result["error"]


class TestPromptBoosterPrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()
        mock_client.calculate_price = AsyncMock(return_value={"price": 0.0001})

        with patch("src.tools.prompt.get_client", return_value=mock_client):
            from src.tools.prompt import prompt_booster_price

            result = await prompt_booster_price(
                prompt="a landscape",
                type="images.generations",
                model_slug="Flux1schnell",
            )

        assert result["success"] is True
        assert result["price"] == 0.0001

        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "prompts/enhancements/price"
        assert call_kwargs["data"]["type"] == "images.generations"
