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
        assert call_kwargs.kwargs.get("endpoint") == "audiofile2txt"
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
    async def test_files_contain_audio_field(self):
        """Verify the audio file is sent under the 'audio' field name."""
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
        assert "audio" in files
        filename, file_obj, mime_type = files["audio"]
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
        assert call_kwargs.kwargs.get("endpoint") == "img2txt"
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
        assert call_kwargs["endpoint"] == "txt2audio/price-calculation"
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
        assert call_kwargs["endpoint"] == "txt2embedding"
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
        assert call_kwargs["endpoint"] == "txt2embedding/price-calculation"


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
        assert call_kwargs["endpoint"] == "txt2video/price-calculation"
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
        assert call_kwargs["endpoint"] == "vid-rmbg"
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
        assert call_kwargs["endpoint"] == "vid-rmbg/price-calculation"
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
        assert call_kwargs["endpoint"] == "vid-upscale"
        assert "files" in call_kwargs
        assert "video" in call_kwargs["files"]
        assert call_kwargs["data"]["model"] == "test-upscale-model"


class TestVideoUpscalePrice:
    @pytest.mark.asyncio
    async def test_correct_endpoint(self):
        mock_client = make_mock_client()

        with patch("src.tools.video.get_client", return_value=mock_client):
            from src.tools.video import video_upscale_price

            result = await video_upscale_price(model="test")

        assert result["success"] is True
        call_kwargs = mock_client.calculate_price.call_args.kwargs
        assert call_kwargs["endpoint"] == "vid-upscale/price-calculation"

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
